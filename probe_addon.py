# Copyright (C) 2017 AMM
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
probe_addon.py  —  Stream codec probing and pre-play URL resolution for FlaskyIPTV_Player_byGG.py
==================================================================================================
Provides:
  /api/resolve   — Resolve a portal item URL, probe its codecs, and return either the
                   raw URL or a /api/hls_proxy transcode URL for browser compatibility.

Also exports as module-level functions (consumed by download_addon and main):
  probe_stream_codecs(url, ...)   — Run ffprobe on a URL, return codec/duration dict.
  _probe_ts_streams(url)          — Pure-Python MPEG-TS byte parser fallback (no ffprobe).
  _check_codecs(codecs)           — Inspect a probe result dict, return transcode decision.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRATION  (changes to FlaskyIPTV_Player_byGG.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — add import block BEFORE the download_addon import block:

    try:
        from probe_addon import register_probe_routes, probe_stream_codecs
        _PROBE_AVAILABLE = True
    except ImportError:
        _PROBE_AVAILABLE = False
        def register_probe_routes(*a, **kw): pass
        def probe_stream_codecs(*a, **kw): return None

STEP 2 — remove probe_stream_codecs from the download_addon import block:

    try:
        from download_addon import (
            register_download_routes,
            safe_filename,
            run_ffmpeg_download, run_yt_dlp_download,
        )
        ...

STEP 3 — remove the probe_stream_codecs stub from the download_addon except block.

STEP 4 — register routes after _FFPROBE_PATH is resolved, before register_download_routes:

    register_probe_routes(flask_app, state, run_async, _make_client, _FFPROBE_PATH)

STEP 5 — remove _probe_ts_streams() function definition from main.

STEP 6 — remove api_resolve() route from main.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRATION  (changes to download_addon.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — add at the top of download_addon.py (after stdlib imports):

    from probe_addon import probe_stream_codecs

STEP 2 — remove the probe_stream_codecs function definition from download_addon.py.

STEP 3 — remove probe_stream_codecs from the module docstring "Also exports" section.
"""

import json
import subprocess
from urllib.parse import quote

import requests as _requests_lib
from flask import request, jsonify


# ── Codec classification constants ───────────────────────────────────────────
# Centralised here so probe_addon, api_resolve, and any future consumer all
# use the exact same set without copy-pasting.

_HEVC_CODECS = ("hevc", "h265", "h.265", "hev1", "hvc1", "x265")
_SAFE_AUDIO  = ("aac", "mp3", "mp2", "opus", "vorbis", "flac")
_BAD_AUDIO   = ("ac3", "eac3", "dts", "dca", "truehd", "mlp", "pcm")

# MPEG-TS stream_type bytes that map to browser-incompatible audio codecs.
# Used by _probe_ts_streams (raw byte scan) — kept here alongside the
# ffprobe-based constants so the two approaches stay in sync.
_TS_BAD_AUDIO_TYPES = {
    0x81: "ac3",
    0x82: "dts",
    0x83: "truehd",
}


# ── FPS parser ────────────────────────────────────────────────────────────────

def _parse_fps(fps_str: str):
    """Parse a rational FPS string like '25/1' or '30000/1001' → float, or None."""
    try:
        parts = fps_str.split('/')
        if len(parts) != 2:
            return None
        num, den = int(parts[0]), int(parts[1])
        if den == 0:
            return None
        fps = num / den
        # Sanity-check: ignore placeholder 0/0, 90000/1 (PTS rate, not frame rate), etc.
        if fps <= 0 or fps > 300:
            return None
        return round(fps, 3)
    except Exception:
        return None


# ── ffprobe codec inspector ───────────────────────────────────────────────────

def probe_stream_codecs(url: str, pre_input_args=None, timeout=15,
                        ffprobe_path="ffprobe"):
    """Run ffprobe on *url* and return a codec/stream-info dict, or None on failure.

    Return value on success:
        {
          "audio":         [str, ...],   # codec names for each audio stream
          "video":         [str, ...],   # codec names for each video stream
          "subtitle":      [str, ...],   # codec names for each subtitle stream
          "duration":      float | None, # container duration in seconds
          "width":         int   | None, # video width in pixels
          "height":        int   | None, # video height in pixels
          "fps":           float | None, # frame rate (parsed from r_frame_rate)
          "video_bitrate": int   | None, # video stream bitrate in bps
          "audio_bitrate": int   | None, # audio stream bitrate in bps
          "total_bitrate": int   | None, # container total bitrate in bps
        }
    """
    cmd = [ffprobe_path, "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format"]
    if pre_input_args:
        cmd = [ffprobe_path, "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format"] + pre_input_args + ["-i", url]
    else:
        cmd += ["-i", url]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout)
        if proc.returncode != 0:
            return None
        data    = json.loads(proc.stdout)
        streams = data.get("streams", [])
        result  = {
            "audio": [], "video": [], "subtitle": [], "duration": None,
            "width": None, "height": None, "fps": None,
            "video_bitrate": None, "audio_bitrate": None, "total_bitrate": None,
        }
        for s in streams:
            typ   = s.get("codec_type")
            codec = s.get("codec_name")
            if typ == "video" and codec:
                result["video"].append(codec)
                # Capture resolution and FPS from the first video stream only
                if result["width"] is None:
                    try:
                        result["width"]  = int(s["width"])
                        result["height"] = int(s["height"])
                    except Exception:
                        pass
                if result["fps"] is None:
                    # Prefer r_frame_rate (declared); fall back to avg_frame_rate
                    fps = _parse_fps(s.get("r_frame_rate", ""))
                    if fps is None:
                        fps = _parse_fps(s.get("avg_frame_rate", ""))
                    result["fps"] = fps
                if result["video_bitrate"] is None:
                    try:
                        result["video_bitrate"] = int(s["bit_rate"])
                    except Exception:
                        pass
            elif typ == "audio" and codec:
                result["audio"].append(codec)
                if result["audio_bitrate"] is None:
                    try:
                        result["audio_bitrate"] = int(s["bit_rate"])
                    except Exception:
                        pass
            elif typ == "subtitle" and codec:
                result["subtitle"].append(codec)
        # Container-level fields
        fmt = data.get("format", {})
        dur = fmt.get("duration")
        if dur:
            try:
                result["duration"] = float(dur)
            except Exception:
                pass
        try:
            result["total_bitrate"] = int(fmt["bit_rate"])
        except Exception:
            pass
        return result
    except Exception:
        return None


# ── Pure-Python MPEG-TS byte-scan fallback ────────────────────────────────────

def _probe_ts_streams(url: str) -> dict:
    """Read the first ~1880 bytes of a MPEG-TS stream and parse the PMT to identify
    video and audio stream types — without invoking ffprobe.

    Returns a dict:
        {
          "hevc":        bool,  # True if video stream type is 0x24 (HEVC/H.265)
          "bad_audio":   bool,  # True if audio codec is unsupported by browsers
          "audio_codec": str,   # human-readable codec name, e.g. "ac3"
        }

    Audio stream types that browsers cannot decode natively:
        0x81        AC3  (Dolby Digital) — common on North American cable
        0x82        DTS
        0x83        TrueHD / DTS-HD (used by some Dolby streams)
        0x06 + 0x7a EAC3 (Dolby Digital Plus) — identified via descriptor tag in PMT

    Times out quickly — failure is non-fatal, caller should catch exceptions.
    """
    result = {"hevc": False, "bad_audio": False, "audio_codec": ""}
    try:
        hdrs = {"User-Agent": "VLC/3.0", "Accept": "*/*"}
        r = _requests_lib.get(url, headers=hdrs, stream=True, timeout=5, verify=False,
                              proxies={"http": None, "https": None})
        raw = b""
        for chunk in r.iter_content(1880):
            raw += chunk
            if len(raw) >= 1880:
                break
        r.close()
        pmt_pid = None
        i = 0
        while i + 188 <= len(raw):
            pkt = raw[i:i+188]; i += 188
            if pkt[0] != 0x47: continue
            pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
            has_adapt = bool(pkt[3] & 0x20); has_pay = bool(pkt[3] & 0x10)
            if not has_pay: continue
            off = 4
            if has_adapt: off = 5 + pkt[4]
            if off >= 188: continue
            if pkt[1] & 0x40: off += 1  # pointer field
            # ── PAT: find first non-NIT program PID ──────────────────────────
            if pid == 0 and pmt_pid is None:
                pos = off + 8
                while pos + 3 < 188:
                    pn = (pkt[pos] << 8) | pkt[pos+1]
                    pp = ((pkt[pos+2] & 0x1f) << 8) | pkt[pos+3]
                    pos += 4
                    if pn != 0: pmt_pid = pp; break
            # ── PMT: walk stream entries ─────────────────────────────────────
            elif pmt_pid and pid == pmt_pid:
                sec = pkt[off:]
                if len(sec) < 12: continue
                pi_len = ((sec[10] & 0x0f) << 8) | sec[11]
                pos = 12 + pi_len
                while pos + 4 < len(sec) - 4:
                    st  = sec[pos]                                  # stream_type byte
                    ei  = ((sec[pos+3] & 0x0f) << 8) | sec[pos+4] # ES info length
                    # Video: HEVC
                    if st == 0x24:
                        result["hevc"] = True
                    # Audio: hard-coded bad types
                    elif st in _TS_BAD_AUDIO_TYPES:
                        result["bad_audio"]  = True
                        result["audio_codec"] = _TS_BAD_AUDIO_TYPES[st]
                    # Audio: stream_type 0x06 (private PES) — scan descriptors for
                    # EAC3 (tag 0x7a).  Many European and IPTV portals use this.
                    elif st == 0x06 and ei > 0:
                        desc_end = pos + 5 + ei
                        dp = pos + 5
                        while dp + 1 < desc_end and dp + 1 < len(sec):
                            tag  = sec[dp]
                            dlen = sec[dp+1]
                            if tag == 0x7a:  # EAC3 descriptor
                                result["bad_audio"]  = True
                                result["audio_codec"] = "eac3"
                            dp += 2 + dlen
                    pos += 5 + ei
                return result   # PMT found and parsed — done
        return result
    except Exception:
        return result


# ── Codec decision helper ─────────────────────────────────────────────────────

def _check_codecs(codecs: dict):
    """Inspect an ffprobe result dict and return a transcode decision.

    Args:
        codecs: dict as returned by probe_stream_codecs — must not be None.

    Returns:
        (needs_transcode: bool, transcode_reason: str | None, detected_codec: str | None)

    This is a pure function with no side-effects.  Logging is left to the caller
    so it can prefix messages with the appropriate context label (first pass,
    HLS retry, etc.).
    """
    needs_transcode  = False
    transcode_reason = None
    detected_codec   = None

    if codecs.get("video"):
        vcodec = codecs["video"][0].lower()
        detected_codec = vcodec
        if vcodec in _HEVC_CODECS or any(h in vcodec for h in _HEVC_CODECS):
            needs_transcode  = True
            transcode_reason = f"hevc video ({vcodec})"

    if not needs_transcode and codecs.get("audio"):
        acodec = codecs["audio"][0].lower()
        if acodec not in _SAFE_AUDIO and (
                acodec in _BAD_AUDIO or any(b in acodec for b in _BAD_AUDIO)):
            needs_transcode  = True
            transcode_reason = f"incompatible audio ({acodec})"

    return needs_transcode, transcode_reason, detected_codec


# ── Stream info builder ───────────────────────────────────────────────────────

def _build_stream_info(codecs: dict, transcode_reason: str | None, is_hls: bool = False) -> dict:
    """Build the stream_info dict that api_resolve includes in its JSON response
    and logs to the activity log.

    Always returns a dict — safe to pass to JS even when codecs is sparse.
    Keys mirror what the probe overlay JS expects.
    """
    vcodec = codecs["video"][0].upper() if codecs.get("video") else None
    acodec = codecs["audio"][0].upper() if codecs.get("audio") else None

    # Resolution label: 2160p/1080p/720p/480p/SD + raw WxH
    w = codecs.get("width")
    h = codecs.get("height")
    if h:
        if h >= 2160:
            res_label = "4K"
        elif h >= 1080:
            res_label = "FHD"
        elif h >= 720:
            res_label = "HD"
        elif h >= 480:
            res_label = "SD"
        else:
            res_label = "SD"
    else:
        res_label = None

    res_str = f"{w}×{h}" if (w and h) else None

    # FPS: round to common values (23.976 → "23.976", 25.0 → "25", 29.97 → "29.97")
    fps = codecs.get("fps")
    fps_str = None
    if fps:
        # Show integer if whole number, else 3 sig-figs
        fps_str = str(int(fps)) if fps == int(fps) else f"{fps:.3f}".rstrip('0')

    # Bitrate: prefer total_bitrate, fall back to video+audio sum, then video alone
    total_bps = codecs.get("total_bitrate")
    video_bps = codecs.get("video_bitrate")
    audio_bps = codecs.get("audio_bitrate")
    if not total_bps and video_bps and audio_bps:
        total_bps = video_bps + audio_bps
    elif not total_bps and video_bps:
        total_bps = video_bps

    def _fmt_bitrate(bps):
        if not bps:
            return None
        if bps >= 1_000_000:
            return f"{bps / 1_000_000:.1f} Mbps"
        return f"{bps // 1000} kbps"

    return {
        "vcodec":       vcodec,
        "acodec":       acodec,
        "res_label":    res_label,
        "res_str":      res_str,
        "width":        w,
        "height":       h,
        "fps":          fps_str,
        "bitrate":      _fmt_bitrate(total_bps),
        "video_bitrate": _fmt_bitrate(video_bps),
        "audio_bitrate": _fmt_bitrate(audio_bps),
        "transcode":    bool(transcode_reason),
        "transcode_reason": transcode_reason or "",
        "is_hls":       is_hls,
    }


def _log_stream_info(state, info: dict, source: str = "ffprobe") -> None:
    """Write a clean, multi-line stream info block to the activity log."""
    lines = [f"[PROBE] ── Stream info ({source}) ──────────────"]
    if info.get("vcodec"):
        res  = f"  {info['res_str']}" if info.get("res_str") else ""
        lbl  = f" ({info['res_label']})" if info.get("res_label") else ""
        fps  = f"  {info['fps']} fps" if info.get("fps") else ""
        vbr  = f"  {info['video_bitrate']}" if info.get("video_bitrate") else ""
        lines.append(f"[PROBE] Video : {info['vcodec']}{res}{lbl}{fps}{vbr}")
    if info.get("acodec"):
        abr  = f"  {info['audio_bitrate']}" if info.get("audio_bitrate") else ""
        lines.append(f"[PROBE] Audio : {info['acodec']}{abr}")
    if info.get("bitrate") and not (info.get("video_bitrate") and info.get("audio_bitrate")):
        lines.append(f"[PROBE] Total : {info['bitrate']}")
    if info.get("transcode"):
        lines.append(f"[PROBE] Action: transcode ({info['transcode_reason']})")
    else:
        lines.append(f"[PROBE] Action: direct play")
    lines.append(f"[PROBE] ────────────────────────────────────────")
    for line in lines:
        state.log(line)


# ── Flask route registration ──────────────────────────────────────────────────
# CSS is injected by the JS via a <style> element — same pattern as multiview.
# The script tag <script src="/api/probe/ui.js"> must be present in
# HTML_TEMPLATE in FlaskyIPTV_Player_byGG.py (alongside the other addon tags).

_PROBE_UI_JS_BYTES: bytes = b""   # filled once in register_probe_routes

_PROBE_UI_JS = r"""
/* ── probe_addon — stream-info toggle button + stats panel ─────────────── */
(function(){

  /* ── CSS ─────────────────────────────────────────────────────────────── */
  (function(){
    const s = document.createElement('style');
    s.textContent = `
#si-btn{
  position:absolute;top:8px;right:8px;
  display:flex;
  align-items:center;gap:4px;
  background:rgba(10,12,20,.68);
  border:1px solid rgba(255,255,255,.13);
  border-radius:4px;
  padding:3px 7px 3px 6px;
  cursor:pointer;
  z-index:31;
  font-family:'Cascadia Code','JetBrains Mono','Courier New',monospace;
  font-size:10px;font-weight:700;
  color:#93c5fd;
  user-select:none;
  white-space:nowrap;
  backdrop-filter:blur(3px);
  -webkit-backdrop-filter:blur(3px);
  /* hidden by default — revealed on hover or when sticky */
  opacity:0;
  pointer-events:none;
  transition:opacity .2s ease, background .15s, border-color .15s;
}
#si-btn.si-hover  { opacity:1; pointer-events:auto; }
#si-btn.si-sticky { opacity:1; pointer-events:auto; }
#si-btn:hover     { background:rgba(30,35,55,.82); border-color:rgba(255,255,255,.25); }
#si-btn.si-open   { background:rgba(30,35,55,.88); border-color:rgba(147,197,253,.35); }
#si-btn.si-warn   { color:#f87171; border-color:rgba(248,113,113,.35); }
#si-btn.si-warn:hover{ border-color:rgba(248,113,113,.6); }
#si-btn .si-btn-icon{ font-size:9px; opacity:.7; }

#si-panel{
  position:absolute;top:32px;right:8px;
  display:none;
  flex-direction:column;gap:1px;
  background:rgba(10,12,20,.82);
  border:1px solid rgba(255,255,255,.10);
  border-radius:5px;
  padding:7px 10px 6px;
  z-index:30;
  font-family:'Cascadia Code','JetBrains Mono','Courier New',monospace;
  font-size:10.5px;line-height:1.65;
  color:#dde4f0;
  white-space:nowrap;
  backdrop-filter:blur(4px);
  -webkit-backdrop-filter:blur(4px);
  pointer-events:none;
  animation:si-drop .15s ease;
}
#si-panel.si-open{ display:flex; }
@keyframes si-drop{
  from{ opacity:0; transform:translateY(-4px); }
  to{   opacity:1; transform:translateY(0); }
}
#si-panel .si-row    { display:flex; gap:6px; align-items:baseline; }
#si-panel .si-label  { color:#5a6a88; font-size:9px; text-transform:uppercase; letter-spacing:.6px; min-width:12px; flex-shrink:0; }
#si-panel .si-val    { color:#c8d2e8; }
#si-panel .si-codec  { color:#6ee7b7; font-weight:700; }
#si-panel .si-bad    { color:#f87171; font-weight:700; }
#si-panel .si-res    { color:#93c5fd; }
#si-panel .si-qlbl   { color:#a78bfa; font-size:9px; margin-left:3px; }
#si-panel .si-br     { color:#64748b; font-size:9.5px; margin-left:3px; }
#si-panel .si-abr    { color:#64748b; font-size:9px; margin-left:3px; font-style:italic; }
#si-panel .si-tx     { color:#fb923c; font-size:9px; margin-left:4px; }
#si-panel .si-divider{ height:1px; background:rgba(255,255,255,.07); margin:3px 0 2px; }
    `;
    document.head.appendChild(s);
  })();

  /* ── State ──────────────────────────────────────────────────────────── */
  let _btn     = null;
  let _panel   = null;
  let _curInfo = null;
  let _open    = false;   // panel visible
  let _sticky  = false;   // user clicked — keep button + panel visible on mouseleave
  let _hasData = false;   // false until first probe result arrives

  /* ── DOM setup ──────────────────────────────────────────────────────── */
  function _ensureEls(){
    if(_btn) return true;
    const vwrap = document.getElementById('vwrap');
    if(!vwrap) return false;

    _btn = document.createElement('div');
    _btn.id = 'si-btn';
    _btn.innerHTML = '<span class="si-btn-icon">&#9654;</span><span id="si-btn-lbl">&#x2014;</span>';
    vwrap.appendChild(_btn);

    _panel = document.createElement('div');
    _panel.id = 'si-panel';
    vwrap.appendChild(_panel);

    /* ── hover: reveal/hide button when player is in focus ── */
    vwrap.addEventListener('mouseenter', function(){
      if(!_hasData) return;
      _btn.classList.add('si-hover');
    });
    vwrap.addEventListener('mouseleave', function(){
      _btn.classList.remove('si-hover');
      // If not sticky, collapse the panel too
      if(!_sticky && _open){
        _btn.classList.remove('si-open');
        _panel.classList.remove('si-open');
        _open = false;
      }
    });

    /* ── click: toggle sticky panel ── */
    _btn.addEventListener('click', function(e){
      e.stopPropagation();
      if(_open){
        // Close and exit sticky mode
        _btn.classList.remove('si-open','si-sticky');
        _panel.classList.remove('si-open');
        _open   = false;
        _sticky = false;
      } else {
        // Open and enter sticky mode
        _btn.classList.add('si-open','si-sticky');
        _panel.classList.add('si-open');
        _open   = true;
        _sticky = true;
      }
    });

    return true;
  }

  /* ── Codec sets ─────────────────────────────────────────────────────── */
  const _BAD_ACODEC = new Set(['AC3','EAC3','DTS','DCA','TRUEHD','MLP','PCM']);
  const _BAD_VCODEC = new Set(['HEVC','H265','HEV1','HVC1','X265']);

  /* ── Button label ───────────────────────────────────────────────────── */
  function _btnLabel(info){
    const parts = [];
    if(info.res_label) parts.push(info.res_label);
    else if(info.res_str) parts.push(info.res_str);
    if(info.fps) parts.push(info.fps + 'fps');
    const hasWarn = (info.vcodec && _BAD_VCODEC.has(info.vcodec.toUpperCase().replace(/[.\-]/g,'')))
                 || (info.acodec && _BAD_ACODEC.has(info.acodec.toUpperCase().replace('-','')));
    return { text: parts.join(' ') || (info.vcodec || '\u2014'), warn: hasWarn };
  }

  /* ── Panel HTML ─────────────────────────────────────────────────────── */
  function _buildPanel(info){
    let html = '';
    const isHLS = info.is_hls;

    if(info.res_str || info.fps){
      const res = info.res_str   ? '<span class="si-res">'  + info.res_str  + '</span>' : '';
      const lbl = info.res_label ? '<span class="si-qlbl">' + info.res_label + '</span>' : '';
      const fps = info.fps       ? '<span class="si-val"> @ ' + info.fps + ' fps</span>' : '';
      html += '<div class="si-row"><span class="si-label">V</span>' + res + lbl + fps + '</div>';
    }
    if(info.vcodec){
      const bad = _BAD_VCODEC.has(info.vcodec.toUpperCase().replace(/[.\-]/g,''));
      const vbr = info.video_bitrate ? '<span class="si-br">'  + info.video_bitrate + '</span>'
                : (info.bitrate      ? '<span class="si-br">'  + info.bitrate       + '</span>'
                : (isHLS             ? '<span class="si-abr">ABR</span>' : ''));
      const tx  = (info.transcode && !(info.transcode_reason||'').startsWith('incompatible'))
                  ? '<span class="si-tx">\u2192 transcode</span>' : '';
      html += '<div class="si-row"><span class="si-label"></span>'
            + '<span class="si-codec' + (bad?' si-bad':'') + '">' + info.vcodec + '</span>'
            + vbr + tx + '</div>';
    }
    if(info.acodec) html += '<div class="si-divider"></div>';
    if(info.acodec){
      const bad = _BAD_ACODEC.has(info.acodec.toUpperCase().replace('-',''));
      const abr = info.audio_bitrate ? '<span class="si-br">'  + info.audio_bitrate + '</span>'
                : (isHLS             ? '<span class="si-abr">ABR</span>' : '');
      const tx  = (info.transcode && (info.transcode_reason||'').startsWith('incompatible'))
                  ? '<span class="si-tx">\u2192 transcode</span>' : '';
      html += '<div class="si-row"><span class="si-label">A</span>'
            + '<span class="si-codec' + (bad?' si-bad':'') + '">' + info.acodec + '</span>'
            + abr + tx + '</div>';
    }
    return html;
  }

  /* ── Public: show/update ────────────────────────────────────────────── */
  function _siShow(info){
    if(!_ensureEls()) return;
    if(!info || (!info.res_str && !info.vcodec && !info.acodec)){ _siHide(); return; }
    _curInfo  = info;
    _hasData  = true;

    const { text, warn } = _btnLabel(info);
    document.getElementById('si-btn-lbl').textContent = text;
    _btn.classList.toggle('si-warn', warn);
    _panel.innerHTML = _buildPanel(info);

    // Exit any previous sticky state so the auto-open below starts clean
    _sticky = false;
    _btn.classList.remove('si-sticky');

    // Auto-open panel briefly (non-sticky) — hover-out will close it after 6s
    _btn.classList.add('si-hover','si-open');
    _panel.classList.add('si-open');
    _open = true;
    const snap = info;
    setTimeout(function(){
      // Only auto-close if the user hasn't clicked to go sticky and channel unchanged
      if(!_sticky && _open && _curInfo === snap){
        _btn.classList.remove('si-open');
        _panel.classList.remove('si-open');
        _open = false;
        // Also remove hover highlight so button fades if mouse not over vwrap
        const vwrap = document.getElementById('vwrap');
        if(vwrap && !vwrap.matches(':hover')) _btn.classList.remove('si-hover');
      }
    }, 6000);
  }

  /* ── Public: hide (on stop) ─────────────────────────────────────────── */
  function _siHide(){
    _open = false; _sticky = false; _hasData = false; _curInfo = null;
    if(_btn){ _btn.classList.remove('si-hover','si-open','si-sticky','si-warn'); }
    if(_panel){ _panel.classList.remove('si-open'); }
  }

  /* ── Intercept /api/resolve ──────────────────────────────────────────── */
  const _origFetch = window.fetch;
  window.fetch = async function(resource, opts){
    const res = await _origFetch.call(this, resource, opts);
    const url = (typeof resource === 'string') ? resource : (resource.url || '');
    if(url.includes('/api/resolve') && !url.includes('/api/resolve_url')){
      const clone = res.clone();
      clone.json().then(function(d){
        if(d && d.stream_info && (d.stream_info.vcodec || d.stream_info.acodec))
          _siShow(d.stream_info);
      }).catch(function(){});
    }
    return res;
  };

  /* ── Hook playerStop / _destroyPlayers ───────────────────────────────── */
  function _patchStop(name){
    const orig = window[name];
    if(typeof orig !== 'function') return;
    window[name] = function(){ _siHide(); return orig.apply(this, arguments); };
  }
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', function(){
      _patchStop('playerStop'); _patchStop('_destroyPlayers');
    });
  } else {
    _patchStop('playerStop'); _patchStop('_destroyPlayers');
  }

  window._streamInfoShow = _siShow;
  window._streamInfoHide = _siHide;

})();
"""


def register_probe_routes(flask_app, state, run_async, _make_client, ffprobe_path):
    """Register /api/resolve and /api/probe/ui.js.

    Parameters
    ----------
    flask_app    : Flask application instance
    state        : shared AppState object
    run_async    : helper that runs a coroutine on the worker event loop
    _make_client : async context-manager factory returning a portal client
    ffprobe_path : absolute path (or bare name) of the ffprobe binary
    """
    global _PROBE_UI_JS_BYTES
    _PROBE_UI_JS_BYTES = _PROBE_UI_JS.encode("utf-8")

    # Capture ffprobe_path in closure so the route handler doesn't need a global.
    _FFPROBE_PATH = ffprobe_path

    @flask_app.route("/api/probe/ui.js")
    def _probe_ui_js():
        from flask import Response
        return Response(
            _PROBE_UI_JS_BYTES,
            content_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @flask_app.route("/api/resolve", methods=["POST"])
    def api_resolve():
        data = request.get_json(force=True)
        item = data.get("item", {})
        mode = data.get("mode", "live")
        if mode not in ("live", "vod", "series"):
            mode = "live"
        cat = data.get("category", {})

        try:
            async def resolve():
                async with _make_client() as client:
                    return await client.resolve_item_url(mode, item, cat)

            url = run_async(resolve())
            is_multiview = request.args.get('mv') == '1'

            if url and isinstance(url, str):
                needs_transcode  = False
                detected_codec   = None
                transcode_reason = None
                is_vod           = mode in ('vod', 'series')

                url_lower_full = url.lower()
                url_lower      = url_lower_full.split('?')[0]

                codecs      = None   # populated when ffprobe runs
                stream_info = {}     # always included in JSON response

                # ── Fast path: HEVC by extension ─────────────────────────────
                if any(ext in url_lower for ext in ['.hevc', '.265', '.h265']):
                    needs_transcode  = True
                    transcode_reason = "hevc by extension"
                    state.log(f"[PROBE] HEVC suspected by extension: {url_lower[-20:]}")

                else:
                    # ── ffprobe pass 1 ────────────────────────────────────────
                    # Timeout kept short (8 s) so channel-switch latency stays
                    # acceptable even for slow live streams.
                    # Pass User-Agent and protocol_whitelist — many MAC/Xtream
                    # portals reject ffprobe's default Lavf user-agent, and ffprobe
                    # needs the whitelist to open http/https/tcp streams without a
                    # config file present.
                    _probe_pre_args = [
                        "-user_agent", "VLC/3.0.0 LibVLC/3.0.0",
                        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                    ]
                    codecs = probe_stream_codecs(url, pre_input_args=_probe_pre_args,
                                                 timeout=8, ffprobe_path=_FFPROBE_PATH)

                    if codecs:
                        needs_transcode, transcode_reason, detected_codec = _check_codecs(codecs)
                        _is_hls_url = '.m3u8' in url_lower or '.m3u8' in url_lower_full
                        stream_info = _build_stream_info(codecs, transcode_reason, is_hls=_is_hls_url)
                        _log_stream_info(state, stream_info, "ffprobe")

                    else:
                        # ffprobe timed-out or couldn't open the stream.
                        # For HLS (.m3u8) URLs, ffprobe needs additional protocol
                        # whitelist entries (hls, applehttp) and -allowed_extensions ALL
                        # to follow playlist redirects.  The TS byte-scan cannot read
                        # HLS manifests at all — it always returns all-False, which is
                        # misleading — so we retry ffprobe with HLS-specific args instead.
                        _is_hls_url = '.m3u8' in url_lower or '.m3u8' in url_lower_full
                        if _is_hls_url:
                            # ── ffprobe pass 2: HLS retry ─────────────────────
                            state.log("[PROBE] ffprobe failed on HLS URL — retrying with HLS args")
                            _hls_probe_args = [
                                "-user_agent", "VLC/3.0.0 LibVLC/3.0.0",
                                "-protocol_whitelist", "file,http,https,tcp,tls,crypto,hls,applehttp",
                                "-allowed_extensions", "ALL",
                            ]
                            codecs = probe_stream_codecs(url, pre_input_args=_hls_probe_args,
                                                         timeout=12, ffprobe_path=_FFPROBE_PATH)
                            if codecs:
                                state.log("[PROBE] ffprobe HLS retry succeeded")
                                needs_transcode, transcode_reason, detected_codec = _check_codecs(codecs)
                                stream_info = _build_stream_info(codecs, transcode_reason, is_hls=True)
                                _log_stream_info(state, stream_info, "ffprobe HLS retry")
                            else:
                                state.log("[PROBE] ffprobe HLS retry also failed — playing direct")
                        else:
                            state.log("[PROBE] ffprobe failed — falling back to TS byte scan")

                # ── TS byte-scan fallback (non-HLS, ffprobe failed) ───────────
                # When ffprobe failed (codecs is None) we fall back to reading the
                # raw MPEG-TS packet headers directly.  Previously this only ran for
                # play_token= URLs, leaving every other URL type uncovered when
                # ffprobe timed out.  Now it runs for ALL non-HLS URLs — if the
                # stream is not MPEG-TS the parser returns all-False within 1880
                # bytes and we fall through to direct play as before.
                # Guard: skip for HLS (.m3u8) URLs — the TS parser cannot read HLS
                # manifests and always returns all-False.  The ffprobe HLS retry
                # above is the correct fallback; if that also failed, play direct.
                _is_hls_url = '.m3u8' in url_lower or '.m3u8' in url_lower_full
                if not needs_transcode and codecs is None and not _is_hls_url:
                    try:
                        ts_info = _probe_ts_streams(url)
                        if ts_info["hevc"]:
                            needs_transcode  = True
                            transcode_reason = "hevc (ts probe)"
                            if 'play_token=' in url:
                                is_vod = False
                            state.log("[PROBE] TS scan: HEVC video detected")
                        elif ts_info["bad_audio"]:
                            needs_transcode  = True
                            transcode_reason = f"incompatible audio ({ts_info['audio_codec']}) (ts probe)"
                            if 'play_token=' in url:
                                is_vod = False
                            state.log(f"[PROBE] TS scan: bad audio — {ts_info['audio_codec']}")
                        else:
                            state.log("[PROBE] TS scan: no issue detected — playing direct")
                    except Exception as pe:
                        state.log(f"[PROBE] TS scan failed: {pe}")

                # ── Fresh token for short-lived Stalker CDN URLs ──────────────
                # CDNs like lx20.net issue single-use or connection-bound tokens.
                # ffprobe above opened a connection and consumed/bound the token,
                # so the URL is no longer valid for actual playback.
                # Re-call resolve_item_url now to get a fresh token URL — skip
                # all probing this time since we already have the codec info.
                # Only applies to MAC/Stalker portals with token= in URL and not
                # an .m3u8 (HLS playlists issue a new segment URL per request, so
                # token re-use is not a problem there).
                _is_stalker_token_url = (
                    state.conn_type == 'mac'
                    and 'token=' in url
                    and not url.lower().split('?')[0].endswith('.m3u8')
                )
                if _is_stalker_token_url:
                    try:
                        async def _refetch():
                            async with _make_client() as client:
                                return await client.resolve_item_url(mode, item, cat)
                        fresh_url = run_async(_refetch())
                        if fresh_url and isinstance(fresh_url, str) and fresh_url != url:
                            state.log("[PROBE] Fresh token obtained (probe consumed previous token)")
                            url = fresh_url
                    except Exception as _rfe:
                        state.log(f"[PROBE] Fresh token re-fetch failed ({_rfe}) — using probe URL")

                # ── Route result ──────────────────────────────────────────────
                if needs_transcode:
                    vod_flag = "1" if is_vod else "0"
                    audio_only_issue = (transcode_reason or "").startswith("incompatible audio")
                    if is_multiview:
                        if audio_only_issue:
                            state.log(f"[PROBE] MV audio → hls_proxy: {transcode_reason}")
                            audio_url = f"/api/hls_proxy?audio_only=1&vod={vod_flag}&url={quote(url, safe='')}"
                            return jsonify({"url": audio_url, "hevc": False, "stream_info": stream_info})
                        else:
                            return jsonify({"url": url, "hevc": True, "stream_info": stream_info})
                    else:
                        state.log(f"[PROBE] Routing to transcode proxy: {transcode_reason}")
                        if audio_only_issue:
                            # Copy video stream, re-encode audio only — much cheaper
                            # than a full libx264 video re-encode.
                            transcode_url = f"/api/hls_proxy?audio_only=1&vod={vod_flag}&url={quote(url, safe='')}"
                        else:
                            transcode_url = f"/api/hls_proxy?transcode=1&vod={vod_flag}&url={quote(url, safe='')}"
                        return jsonify({"url": transcode_url, "hevc": True, "stream_info": stream_info})

            return jsonify({"url": url, "stream_info": stream_info})
        except Exception as e:
            state.log(f"[PROBE] Error: {type(e).__name__}: {e}")
            return jsonify({"url": "", "error": str(e)})
