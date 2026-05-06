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
          "audio_bitrate": int   | None, # first audio stream bitrate in bps
          "total_bitrate": int   | None, # container total bitrate in bps
          "audio_tracks":  [             # per-track audio metadata
            {
              "index":    int,           # stream index within the container
              "codec":    str,           # codec name e.g. "aac", "ac3"
              "language": str | None,    # ISO 639-2 language code e.g. "eng"
              "title":    str | None,    # human-readable name if tagged
              "channels": int | None,    # channel count e.g. 2, 6
              "layout":   str | None,    # channel layout e.g. "stereo", "5.1"
            }, ...
          ],
          "subtitle_tracks": [           # per-track subtitle metadata
            {
              "index":          int,
              "codec":          str,     # e.g. "subrip", "dvd_subtitle", "webvtt"
              "language":       str | None,
              "title":          str | None,
              "is_image_based": bool,    # True for PGS/DVD image subs (not renderable in browser)
            }, ...
          ],
        }
    """
    # Image-based subtitle codecs — cannot be rendered in a browser without
    # custom demuxing/rendering.  Text-based codecs (subrip, webvtt, ass…)
    # can potentially be surfaced via addTextTrack() cue injection.
    _IMAGE_SUB_CODECS = {"dvd_subtitle", "hdmv_pgs_subtitle", "dvbsub",
                         "xsub", "pgssub", "dvb_subtitle"}
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
            "audio_tracks": [], "subtitle_tracks": [],
        }
        for s in streams:
            typ   = s.get("codec_type")
            codec = s.get("codec_name")
            tags  = s.get("tags", {}) or {}
            idx   = s.get("index", len(result["audio_tracks"]) + len(result["subtitle_tracks"]))
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
                # Collect full per-track metadata for the track-selector UI
                lang     = (tags.get("language") or tags.get("LANGUAGE") or "").strip() or None
                title    = (tags.get("title")    or tags.get("TITLE")    or "").strip() or None
                channels = None
                layout   = None
                try:
                    channels = int(s["channels"])
                except Exception:
                    pass
                layout = s.get("channel_layout") or None
                result["audio_tracks"].append({
                    "index":    idx,
                    "codec":    codec,
                    "language": lang,
                    "title":    title,
                    "channels": channels,
                    "layout":   layout,
                })
            elif typ == "subtitle" and codec:
                result["subtitle"].append(codec)
                lang  = (tags.get("language") or tags.get("LANGUAGE") or "").strip() or None
                title = (tags.get("title")    or tags.get("TITLE")    or "").strip() or None
                result["subtitle_tracks"].append({
                    "index":          idx,
                    "codec":          codec,
                    "language":       lang,
                    "title":          title,
                    "is_image_based": codec.lower() in _IMAGE_SUB_CODECS,
                })
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

def _probe_ts_streams(url: str, ua: str = "VLC/3.0.0 LibVLC/3.0.0") -> dict:
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
        hdrs = {"User-Agent": ua, "Accept": "*/*"}
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
        "vcodec":             vcodec,
        "acodec":             acodec,
        "res_label":          res_label,
        "res_str":            res_str,
        "width":              w,
        "height":             h,
        "fps":                fps_str,
        "bitrate":            _fmt_bitrate(total_bps),
        "video_bitrate":      _fmt_bitrate(video_bps),
        "audio_bitrate":      _fmt_bitrate(audio_bps),
        "transcode":          bool(transcode_reason),
        "transcode_reason":   transcode_reason or "",
        "is_hls":             is_hls,
        "audio_tracks":       codecs.get("audio_tracks", []),
        "subtitle_tracks":    codecs.get("subtitle_tracks", []),
        "active_audio_track": codecs.get("active_audio_track", 0),
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
    # Always log audio track count and per-track detail regardless of count or tags
    audio_tracks = info.get("audio_tracks", [])
    active_track = info.get("active_audio_track", 0)
    lines.append(f"[PROBE] Audio tracks: {len(audio_tracks)} (active: track {active_track})")
    for i, t in enumerate(audio_tracks):
        lang   = t.get("language") or f"track {i+1}"
        title  = (' "' + t["title"] + '"') if t.get("title") else ""
        ch     = f"  {t['channels']}ch" if t.get("channels") else ""
        active = " ◀ active" if i == active_track else ""
        lines.append(f"[PROBE]   [{lang}] {t['codec'].upper()}{title}{ch}{active}")
    if info.get("bitrate") and not (info.get("video_bitrate") and info.get("audio_bitrate")):
        lines.append(f"[PROBE] Total : {info['bitrate']}")
    # Always log subtitle streams — explicitly say none when absent
    sub_tracks = info.get("subtitle_tracks", [])
    if sub_tracks:
        lines.append(f"[PROBE] Subs  : {len(sub_tracks)} embedded stream(s)")
        for i, t in enumerate(sub_tracks):
            lang  = t.get("language") or f"track {i+1}"
            title = (' "' + t["title"] + '"') if t.get("title") else ""
            kind  = " (image-based)" if t.get("is_image_based") else " (text)"
            lines.append(f"[PROBE]   [{lang}] {t['codec']}{title}{kind}")
    else:
        lines.append(f"[PROBE] Subs  : none embedded")
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
/* ── probe_addon — stream-info toggle button + stats + track selector ───── */
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

/* Touch / no-hover devices (mobile): button always visible when data is present.
   mouseenter never fires on touch, so the si-hover mechanism doesn't work.
   si-touch-visible is added by JS when _hasData becomes true. */
@media (hover: none) {
  #si-btn.si-touch-visible { opacity:1; pointer-events:auto; }
}

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
  /* pointer-events:auto — panel contains clickable track selector buttons */
  pointer-events:auto;
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

/* Track selector section */
#si-panel .si-section-hdr{
  color:#4a5a78; font-size:8.5px; text-transform:uppercase; letter-spacing:.8px;
  margin-top:4px; margin-bottom:1px;
}
#si-panel .si-tracks{ display:flex; flex-direction:column; gap:2px; margin-top:1px; }
#si-panel .si-track-btn{
  display:flex; align-items:center; gap:5px;
  padding:2px 6px 2px 4px;
  border-radius:3px;
  cursor:pointer;
  pointer-events:auto;
  transition:background .12s;
  user-select:none;
}
#si-panel .si-track-btn:hover{ background:rgba(255,255,255,.07); }
#si-panel .si-track-btn.si-active{
  background:rgba(99,179,237,.12);
  border-left:2px solid #60a5fa;
  padding-left:2px;
}
#si-panel .si-track-btn.si-info-only{ cursor:default; opacity:.6; }
#si-panel .si-track-btn.si-info-only:hover{ background:none; }
#si-panel .si-track-dot{
  width:5px;height:5px;border-radius:50%;
  background:rgba(255,255,255,.18);flex-shrink:0;
}
#si-panel .si-track-btn.si-active .si-track-dot{ background:#60a5fa; }
#si-panel .si-track-lang{
  color:#93c5fd; font-size:9px; text-transform:uppercase; font-weight:700;
  min-width:24px;
}
#si-panel .si-track-name{ color:#94a3b8; font-size:9.5px; }
#si-panel .si-track-codec{ color:#475569; font-size:8.5px; margin-left:auto; }
#si-panel .si-track-ch{ color:#475569; font-size:8.5px; margin-left:3px; }
#si-panel .si-img-badge{
  color:#f59e0b; font-size:8px; margin-left:3px; opacity:.7;
}
    `;
    document.head.appendChild(s);
  })();

  /* ── State ──────────────────────────────────────────────────────────── */
  let _btn     = null;
  let _panel   = null;
  let _curInfo = null;
  let _open    = false;
  let _sticky  = false;
  let _hasData = false;
  // Runtime HLS.js track lists — updated via AUDIO_TRACKS_UPDATED event
  let _hlsAudioTracks = [];   // [{id, name, lang}]
  let _activeAudio    = -1;   // currently selected HLS audio track index
  let _hlsSubTracks   = [];   // [{id, name, lang}]
  let _activeSub      = -1;
  // Last resolved stream URL — captured by fetch interceptor so _switchAudio
  // can rebuild the hls_proxy URL without a full re-resolve round-trip.
  let _curStreamUrl   = null;
  let _curStreamName  = null;
  let _curStreamLive  = true;
  let _snapRestoreTimer = null;  // handle for the fast-path panel-restore setTimeout
  // Touch reveal timer — drives si-touch-visible lifecycle on mobile
  let _revealTimer = null;

  /* ── Touch device detection ─────────────────────────────────────────── */
  const _isTouch = window.matchMedia('(hover: none)').matches;

  /* ── Touch reveal helper ────────────────────────────────────────────── */
  // Shows the button on touch devices for `ms` milliseconds, then fades it.
  // Cancels any pending fade timer first.  When sticky, the fade is never
  // scheduled — button stays until user closes the panel.
  function _revealButton(ms){
    if(!_btn || !_hasData) return;
    if(_revealTimer){ clearTimeout(_revealTimer); _revealTimer = null; }
    _btn.classList.add('si-touch-visible');
    if(_sticky) return;   // don't schedule fade while panel is pinned open
    _revealTimer = setTimeout(function(){
      _revealTimer = null;
      if(!_sticky) _btn.classList.remove('si-touch-visible');
    }, ms);
  }

  function _cancelReveal(){
    if(_revealTimer){ clearTimeout(_revealTimer); _revealTimer = null; }
    _btn.classList.remove('si-touch-visible');
  }

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

    // ── Desktop: hover reveals/hides button ───────────────────────────
    if(!_isTouch){
      vwrap.addEventListener('mouseenter', function(){
        if(!_hasData) return;
        _btn.classList.add('si-hover');
      });
      vwrap.addEventListener('mouseleave', function(){
        _btn.classList.remove('si-hover');
        if(!_sticky && _open){
          _btn.classList.remove('si-open');
          _panel.classList.remove('si-open');
          _open = false;
        }
      });
    }

    // ── Mobile: any tap on the player area re-reveals the button ─────
    // This is the fix for Bug 1 — after auto-close removes si-touch-visible,
    // the next tap on vwrap brings the button back for 4 s.
    // We listen on vwrap (not document) to avoid triggering on unrelated taps.
    if(_isTouch){
      vwrap.addEventListener('touchstart', function(e){
        if(!_hasData) return;
        // If the tap is NOT on the button/panel, treat it as "tap to reveal"
        if(!_btn.contains(e.target) && !_panel.contains(e.target)){
          _revealButton(4000);
        }
      }, {passive: true});

      // Tap outside the panel while open — close panel and brief-reveal button
      document.addEventListener('touchstart', function(e){
        if(_open && _panel && !_panel.contains(e.target) && !_btn.contains(e.target)){
          _btn.classList.remove('si-open','si-sticky');
          _panel.classList.remove('si-open');
          _open = false; _sticky = false;
          // Brief reveal so user can see button fade — gives feedback that close happened
          _revealButton(2000);
        }
      }, {passive: true});
    }

    // ── Track selector: event delegation on _panel ──────────────────────
    // A single persistent listener on _panel catches clicks on any
    // .si-track-btn[data-track-idx] child regardless of innerHTML resets.
    // This eliminates the need for window._siSwitchAudio to be globally
    // reachable at click time (inline onclick has that fragile dependency).
    _panel.addEventListener('click', function(e){
      const btn = e.target.closest('.si-track-btn[data-track-idx]');
      if(!btn) return;
      e.stopPropagation();
      const idx = parseInt(btn.getAttribute('data-track-idx'), 10);
      if(!isNaN(idx)){
        console.log('[probe] track row clicked → _switchAudio(' + idx + ')');
        _switchAudio(idx);
      }
    });

    // ── Button click/tap: toggle panel ───────────────────────────────
    // Fix for Bug 2: close no longer permanently removes si-touch-visible.
    // Instead it calls _revealButton(2000) so the button stays briefly visible,
    // giving the user a chance to tap again — same feel as tap-outside dismiss.
    _btn.addEventListener('click', function(e){
      e.stopPropagation();
      if(_open){
        _btn.classList.remove('si-open','si-sticky');
        _panel.classList.remove('si-open');
        _open = false; _sticky = false;
        if(_isTouch) _revealButton(2000);   // brief visibility after close
      } else {
        _btn.classList.add('si-open','si-sticky');
        _panel.classList.add('si-open');
        _open = true; _sticky = true;
        if(_isTouch) _revealButton(0);      // sticky — cancel fade timer
      }
    });

    return true;
  }

  /* ── Codec sets ─────────────────────────────────────────────────────── */
  const _BAD_ACODEC = new Set(['AC3','EAC3','DTS','DCA','TRUEHD','MLP','PCM']);
  const _BAD_VCODEC = new Set(['HEVC','H265','HEV1','HVC1','X265']);

  /* ── Helpers ────────────────────────────────────────────────────────── */
  function _btnLabel(info){
    const parts = [];
    if(info.res_label) parts.push(info.res_label);
    else if(info.res_str) parts.push(info.res_str);
    if(info.fps) parts.push(info.fps + 'fps');
    const hasWarn = (info.vcodec && _BAD_VCODEC.has(info.vcodec.toUpperCase().replace(/[.\-]/g,'')))
                 || (info.acodec && _BAD_ACODEC.has(info.acodec.toUpperCase().replace('-','')));
    return { text: parts.join(' ') || (info.vcodec || '\u2014'), warn: hasWarn };
  }

  // Format a track label: prefer title, fall back to language code, else "Track N"
  function _trackLabel(t, n){
    if(t.title) return t.title;
    if(t.lang || t.language){
      const l = (t.lang || t.language).toUpperCase();
      return l;
    }
    return 'Track ' + (n+1);
  }

  /* ── Audio track switching ──────────────────────────────────────────── */
  // Two distinct switching paths:
  //   1. Live HLS (useLiveAudio=true, hlsObj alive): hlsObj.audioTrack = id directly.
  //      Zero latency, no stream restart needed.
  //   2. Everything else (transcoded, direct MPEG-TS, direct HLS without multi-audio):
  //      Re-resolve with audio_track=N in POST body.  Server forces audio_only
  //      transcode so ffmpeg applies -map 0:a:<N>.  Stream restarts through hls_proxy.
  //      Works identically for already-transcoded and previously-direct streams.
  function _switchAudio(trackIdx){
    console.log('[probe] _switchAudio called — trackIdx:', trackIdx,
                'curInfo.transcode:', _curInfo && _curInfo.transcode,
                'hlsAudioTracks:', _hlsAudioTracks.length,
                'curStreamUrl:', _curStreamUrl ? _curStreamUrl.substring(0,60)+'…' : '(none)');
    if(!_curInfo){ console.warn('[probe] _switchAudio: no _curInfo, abort'); return; }

    // ── Case 1: live HLS with native multi-audio renditions ──────────────
    // HLS.js tracks the renditions; switch directly with zero restart.
    // SKIP when transcoded: hlsObj feeds off the ffmpeg single-audio output;
    // hlsObj.audioTrack = N there is a complete no-op.
    const _isTranscoded = !!(_curInfo && _curInfo.transcode);
    if(!_isTranscoded && typeof hlsObj !== 'undefined' && hlsObj
       && typeof hlsObj.audioTrack === 'number' && _hlsAudioTracks.length > 0){
      console.log('[probe] Case 1: HLS.js direct switch to track', trackIdx);
      hlsObj.audioTrack = trackIdx;
      _activeAudio = trackIdx;
      _rebuildTracks();
      return;
    }

    // ── Case 2: transcoded or direct MPEG-TS ─────────────────────────────
    // Strategy: rebuild the hls_proxy URL directly from _curStreamUrl (cached
    // by the fetch interceptor when the stream was resolved).  This avoids a
    // full re-resolve round-trip through the Stalker portal and eliminates the
    // brittle dependency on 'it' / filtItems[pIdx].
    //
    // _curStreamUrl is set in the fetch intercept to d.url (the hls_proxy URL).
    // Shape: /api/hls_proxy?transcode=1[&audio_track=N]&url=<encoded-origin>
    // We strip any existing audio_track param and inject the new one.
    const name = _curStreamName || '';
    const isLive = _curStreamLive !== false;

    // Fast-path: only valid when already routed through hls_proxy.
    // Direct-play URLs (raw http://...m3u8) must fall through to re-resolve so
    // the Python route can force-transcode and apply -map 0:a:<N>.
    const _isProxyUrl = _curStreamUrl && _curStreamUrl.includes('/api/hls_proxy');

    if(_isProxyUrl){
      const _trackList = (_curInfo && _curInfo.audio_tracks) || [];
      const _trackMeta = _trackList[trackIdx];
      const _trackLang = _trackMeta
        ? ((_trackMeta.language || _trackMeta.lang || '').toUpperCase() || ('track ' + trackIdx))
        : ('track ' + trackIdx);
      const _displayName = name ? (name + ' [' + _trackLang + ']') : _trackLang;

      let newUrl = _curStreamUrl
        .replace(/[&?]audio_track=\d+/g, '')
        .replace(/[&?]$/, '');
      newUrl += (newUrl.includes('?') ? '&' : '?') + 'audio_track=' + trackIdx;
      console.log('[probe] fast-path: audio_track=' + trackIdx + ' (' + _trackLang + ')',
                  '→', newUrl.substring(0, 80) + '…');
      _activeAudio = trackIdx;
      _rebuildTracks();

      // Snapshot everything needed to fully restore the panel after _siHide fires.
      // _siHide clears _curStreamUrl/_curStreamName too, so we must carry them in
      // the snapshot and restore them — otherwise the NEXT switch has no fast-path URL.
      const _snapUrl  = newUrl;
      const _snapName = name;
      const _infoSnapshot = Object.assign({}, _curInfo, { active_audio_track: trackIdx });
      if(typeof doPlay === 'function'){ doPlay(newUrl, _displayName, {isLive: isLive}); }
      // _siHide has fired. Restore panel + URL state so subsequent switches work.
      if(_snapRestoreTimer){ clearTimeout(_snapRestoreTimer); }
      _snapRestoreTimer = setTimeout(function(){
        _snapRestoreTimer = null;
        _curStreamUrl  = _snapUrl;
        _curStreamName = _snapName;
        _curStreamLive = isLive;
        _siShow(_infoSnapshot);
      }, 120);
      return;
    }

    // ── Case 2 fallback: no cached URL — re-resolve via /api/resolve ─────
    // Reached only if _curStreamUrl wasn't captured (edge-case race).
    // Use filtItems[pIdx] (both are true globals set by playItem).
    console.warn('[probe] Case 2 fallback: no cached URL, falling back to re-resolve');
    const _curIt = (typeof filtItems !== 'undefined' && typeof pIdx !== 'undefined')
                   ? filtItems[pIdx] : null;
    if(!_curIt){
      console.error('[probe] _switchAudio: no current item (filtItems[' + pIdx + '] is null)');
      return;
    }
    _activeAudio = trackIdx;
    _rebuildTracks();
    fetch('/api/resolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        item:        _curIt,
        mode:        (typeof mode   !== 'undefined' ? mode   : 'live'),
        category:    (typeof curCat !== 'undefined' ? curCat : {}),
        audio_track: trackIdx,
      })
    }).then(function(r){ return r.json(); }).then(function(d){
      console.log('[probe] fallback re-resolve response url:', d.url ? d.url.substring(0,80)+'…' : '(none)');
      if(d.url && typeof doPlay === 'function'){
        doPlay(d.url, name, {isLive: isLive});
      }
    }).catch(function(e){
      console.error('[probe] re-resolve for audio track failed:', e);
    });
  }

  /* ── Build panel HTML ───────────────────────────────────────────────── */
  function _buildPanel(info){
    let html = '';
    const isHLS = info.is_hls;

    // ── Video ─────────────────────────────────────────────────────────
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

    html += '<div class="si-divider"></div>';

    // ── Audio ─────────────────────────────────────────────────────────
    // Prefer runtime HLS.js track list (more accurate for HLS multi-audio),
    // fall back to ffprobe data for non-HLS or when HLS hasn't fired yet.
    const useLiveAudio = isHLS && _hlsAudioTracks.length > 0;
    const audioTracks  = useLiveAudio ? _hlsAudioTracks : (info.audio_tracks || []);

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

    // Audio track selector — always shown when track data is present,
    // matching the log which always reports track count and detail.
    if(audioTracks.length > 0){
      html += '<div class="si-section-hdr">Audio tracks</div>';
      html += '<div class="si-tracks" id="si-audio-tracks">';
      audioTracks.forEach(function(t, i){
        const hlsId    = (useLiveAudio ? t.id : i);
        // _activeAudio starts at -1; fall back to server-stamped active_audio_track
        // so the highlight is correct immediately after a switch.
        const _srvActive = (typeof info.active_audio_track === 'number') ? info.active_audio_track : 0;
        const isActive = useLiveAudio
                         ? (_activeAudio === t.id)
                         : (_activeAudio >= 0 ? _activeAudio === i : _srvActive === i);
        const lang     = (t.lang || t.language || '').toUpperCase() || '\u2014';
        const name     = t.title || t.name || '';
        const codec    = (!useLiveAudio && t.codec)     ? t.codec.toUpperCase() : '';
        const ch       = (!useLiveAudio && t.channels)  ? t.channels + 'ch'    : '';
        // Switchable when:
        //   (a) live HLS with HLS.js track list (instant rendition switch)
        //   (b) transcoded stream (re-resolve with track index, ffmpeg -map)
        //   (c) direct MPEG-TS with multiple tracks (re-resolve forces audio_only
        //       transcode so ffmpeg can apply -map 0:a:<N>)
        // Single-track direct streams with no language tag: informational only —
        // clicking would restart the stream identically with no benefit.
        const isMultiOrTagged = audioTracks.length > 1
                             || !!(t.lang || t.language);
        const canSwitch = useLiveAudio || info.transcode || isMultiOrTagged;
        // data-track-idx drives event delegation on _panel — no inline onclick needed.
        html += '<div class="si-track-btn' + (isActive?' si-active':'') + (canSwitch?'':' si-info-only') + '"'
              + (canSwitch ? ' data-track-idx="' + hlsId + '"' : '')
              + '>'
              + '<span class="si-track-dot"></span>'
              + '<span class="si-track-lang">' + lang + '</span>'
              + (name  ? '<span class="si-track-name">'  + name  + '</span>' : '')
              + (codec ? '<span class="si-track-codec">' + codec + '</span>' : '')
              + (ch    ? '<span class="si-track-ch">'    + ch    + '</span>' : '')
              + '</div>';
      });
      html += '</div>';
    }

    // ── Subtitle tracks ───────────────────────────────────────────────
    // subtitleTrackController is fully no-op'd in the HLS config (subtitles_addon
    // owns the subtitle pipeline via addTextTrack + VTT cues for external subs).
    // Embedded subtitle streams from the container are shown as informational only.
    // Image-based subs (PGS/DVD) cannot be rendered in a browser at all.
    const subTracks = info.subtitle_tracks || [];
    if(subTracks.length > 0){
      html += '<div class="si-divider"></div>';
      html += '<div class="si-section-hdr">Subtitle streams</div>';
      html += '<div class="si-tracks">';
      subTracks.forEach(function(t, i){
        const lang  = (t.language || '').toUpperCase() || '—';
        const name  = t.title || '';
        const codec = (t.codec || '').toLowerCase();
        const imgBadge = t.is_image_based ? '<span class="si-img-badge">image</span>' : '';
        // All embedded subs are informational — use subtitles_addon for external subs
        html += '<div class="si-track-btn si-info-only">'
              + '<span class="si-track-dot"></span>'
              + '<span class="si-track-lang">' + lang + '</span>'
              + (name  ? '<span class="si-track-name">' + name  + '</span>' : '')
              + '<span class="si-track-codec">' + codec + '</span>'
              + imgBadge
              + '</div>';
      });
      html += '</div>';
    }

    return html;
  }

  /* ── Rebuild only the track section without full panel redraw ───────── */
  function _rebuildTracks(){
    if(!_panel || !_curInfo) return;
    // Rebuild entire panel — it's lightweight enough
    _panel.innerHTML = _buildPanel(_curInfo);
  }

  /* ── Public: show/update ────────────────────────────────────────────── */
  function _siShow(info){
    if(!_ensureEls()) return;
    if(!info || (!info.res_str && !info.vcodec && !info.acodec)){ _siHide(); return; }
    _curInfo = info;
    _hasData = true;

    const { text, warn } = _btnLabel(info);
    document.getElementById('si-btn-lbl').textContent = text;
    _btn.classList.toggle('si-warn', warn);
    _panel.innerHTML = _buildPanel(info);

    _sticky = false;
    _btn.classList.remove('si-sticky');

    // Show button + panel for 6s then auto-collapse.
    // On touch: _revealButton manages si-touch-visible + the fade timer.
    // On desktop: si-hover is added directly (no timer needed — mouseleave handles it).
    if(_isTouch){
      _revealButton(6000);
    } else {
      _btn.classList.add('si-hover');
    }
    _btn.classList.add('si-open');
    _panel.classList.add('si-open');
    _open = true;

    const snap = info;
    setTimeout(function(){
      if(!_sticky && _open && _curInfo === snap){
        _btn.classList.remove('si-open');
        _panel.classList.remove('si-open');
        _open = false;
        // Desktop: remove si-hover if mouse not over vwrap
        // Touch: _revealButton timer handles si-touch-visible fade independently
        if(!_isTouch){
          const vwrap = document.getElementById('vwrap');
          if(vwrap && !vwrap.matches(':hover')) _btn.classList.remove('si-hover');
        }
      }
    }, 6000);
  }

  /* ── Public: hide (on stop) ─────────────────────────────────────────── */
  function _siHide(){
    // Cancel any pending fast-path panel-restore before wiping state —
    // prevents stale snapshot from a previous channel bleeding into the next.
    if(_snapRestoreTimer){ clearTimeout(_snapRestoreTimer); _snapRestoreTimer = null; }
    _open = false; _sticky = false; _hasData = false; _curInfo = null;
    _hlsAudioTracks = []; _activeAudio = -1;
    _hlsSubTracks   = []; _activeSub   = -1;
    _curStreamUrl = null; _curStreamName = null;
    if(_isTouch && _btn) _cancelReveal();
    if(_btn){ _btn.classList.remove('si-hover','si-open','si-sticky','si-warn'); }
    if(_panel){ _panel.classList.remove('si-open'); }
  }

  /* ── Hook HLS.js AUDIO_TRACKS_UPDATED + AUDIO_TRACK_SWITCHED ────────── */
  // hlsObj is reassigned each time doPlay() runs a new HLS stream.
  // We can't bind events at construction time from here, so we patch the
  // _createHlsAndPlay flow by intercepting the fetch of /api/resolve and
  // then scheduling a one-shot MutationObserver on hlsObj creation, OR
  // more simply: poll for hlsObj to appear and bind once per stream.
  // The simplest reliable approach: bind after the resolve fetch returns
  // (hlsObj is created synchronously in the then() handler of doPlay).
  let _hlsBound = false;
  function _bindHlsEvents(){
    if(_hlsBound) return;
    if(typeof hlsObj === 'undefined' || !hlsObj) return;
    _hlsBound = true;

    hlsObj.on('hlsAudioTracksUpdated', function(evt, data){
      _hlsAudioTracks = (data.audioTracks || []).map(function(t){ return t; });
      // Default active track is whatever hlsObj.audioTrack reports
      _activeAudio = hlsObj.audioTrack;
      if(_curInfo) _rebuildTracks();
    });
    hlsObj.on('hlsAudioTrackSwitched', function(evt, data){
      _activeAudio = data.id;
      if(_curInfo) _rebuildTracks();
    });
  }

  /* ── Intercept /api/resolve ──────────────────────────────────────────── */
  const _origFetch = window.fetch;
  window.fetch = async function(resource, opts){
    const res = await _origFetch.call(this, resource, opts);
    const url = (typeof resource === 'string') ? resource : (resource.url || '');
    if(url.includes('/api/resolve') && !url.includes('/api/resolve_url')){
      // Only restore _activeAudio if this resolve was explicitly an audio-track switch
      // (body contains audio_track). Without this guard, switching channels inherits
      // the previous channel's track highlight — the stale UI tag bug.
      let _reqHadAudioTrack = false;
      try {
        const _body = (opts && opts.body) ? JSON.parse(opts.body) : null;
        _reqHadAudioTrack = (_body && typeof _body.audio_track === 'number');
      } catch(_){}
      const _savedAudio = _reqHadAudioTrack ? _activeAudio : -1;
      _hlsBound = false; _hlsAudioTracks = []; _activeAudio = -1;
      const clone = res.clone();
      clone.json().then(function(d){
        if(d && d.stream_info && (d.stream_info.vcodec || d.stream_info.acodec)){
          // Cache the resolved stream URL and name so _switchAudio can
          // rebuild the hls_proxy URL without a full re-resolve round-trip.
          if(d.url){
            _curStreamUrl  = d.url;
            // Recover name from the POST body if possible; fall back to curInfo later
            try {
              const bodyStr = (opts && opts.body) ? opts.body : null;
              const bodyObj = bodyStr ? JSON.parse(bodyStr) : null;
              const it = bodyObj && bodyObj.item;
              _curStreamName = (it && (it.name || it.o_name || it.fname)) || '';
              _curStreamLive = (bodyObj && bodyObj.mode) ? bodyObj.mode === 'live' : true;
            } catch(_){ _curStreamName = ''; _curStreamLive = true; }
            console.log('[probe] cached stream url:', _curStreamUrl.substring(0,80)+'…');
          }
          _siShow(d.stream_info);
          // Restore active track highlight for audio-switch resolves only.
          if(_savedAudio >= 0){ _activeAudio = _savedAudio; _rebuildTracks(); }
          // Schedule HLS event binding — hlsObj is created shortly after
          // doPlay() receives this response, so defer a few ticks
          setTimeout(_bindHlsEvents, 200);
          setTimeout(_bindHlsEvents, 800);  // retry in case of slow init
        }
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

  /* ── Global exports ─────────────────────────────────────────────────── */
  window._streamInfoShow  = _siShow;
  window._streamInfoHide  = _siHide;
  window._siSwitchAudio   = _switchAudio;

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

            stream_info = {}  # always defined — populated later if ffprobe runs
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
                        "-user_agent", state.stream_ua,
                        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                        "-analyzeduration", "2000000",
                        "-probesize", "500000",
                    ]
                    codecs = probe_stream_codecs(url, pre_input_args=_probe_pre_args,
                                                 timeout=10, ffprobe_path=_FFPROBE_PATH)

                    if codecs:
                        needs_transcode, transcode_reason, detected_codec = _check_codecs(codecs)
                        _is_hls_url = '.m3u8' in url_lower or '.m3u8' in url_lower_full
                        # is_hls=False when transcoding: delivery is fixed-bitrate MPEG-TS
                        # from ffmpeg regardless of source format, so ABR label is wrong
                        stream_info = _build_stream_info(codecs, transcode_reason,
                                                         is_hls=_is_hls_url and not needs_transcode)
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
                                "-user_agent", state.stream_ua,
                                "-protocol_whitelist", "file,http,https,tcp,tls,crypto,hls,applehttp",
                                "-allowed_extensions", "ALL",
                            ]
                            codecs = probe_stream_codecs(url, pre_input_args=_hls_probe_args,
                                                         timeout=12, ffprobe_path=_FFPROBE_PATH)
                            if codecs:
                                state.log("[PROBE] ffprobe HLS retry succeeded")
                                needs_transcode, transcode_reason, detected_codec = _check_codecs(codecs)
                                # is_hls=False when transcoding — delivery is MPEG-TS from ffmpeg
                                stream_info = _build_stream_info(codecs, transcode_reason,
                                                                 is_hls=not needs_transcode)
                                _log_stream_info(state, stream_info, "ffprobe HLS retry")
                            else:
                                # HLS playlist probe failed — try the same URL with a .ts extension.
                                # Some portals expose the actual transport stream at the .ts variant.
                                def _hls_to_ts_variant(hls_url: str) -> str:
                                    base, sep, suffix = hls_url.partition('?')
                                    if '.m3u8' not in base.lower():
                                        return hls_url
                                    lower_base = base.lower()
                                    idx = lower_base.rfind('.m3u8')
                                    if idx < 0:
                                        return hls_url
                                    base = base[:idx] + '.ts' + base[idx + 6:]
                                    return base + (sep + suffix if sep else '')

                                ts_url = _hls_to_ts_variant(url)
                                ts_fallback_used = False
                                if ts_url != url:
                                    state.log("[PROBE] HLS retry failed — trying .ts variant")
                                    codecs = probe_stream_codecs(ts_url, pre_input_args=_probe_pre_args,
                                                                 timeout=10, ffprobe_path=_FFPROBE_PATH)
                                    if codecs:
                                        state.log("[PROBE] .ts probe succeeded — using TS URL")
                                        url = ts_url
                                        ts_fallback_used = True
                                        url_lower_full = url.lower()
                                        url_lower = url_lower_full.split('?')[0]
                                        needs_transcode, transcode_reason, detected_codec = _check_codecs(codecs)
                                        _is_hls_url = False
                                        stream_info = _build_stream_info(codecs, transcode_reason,
                                                                         is_hls=False)
                                        _log_stream_info(state, stream_info, ".ts fallback")
                                    else:
                                        state.log("[PROBE] .ts probe also failed — playing direct")
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
                        ts_info = _probe_ts_streams(url, ua=state.stream_ua)
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
                    and not locals().get('ts_fallback_used', False)
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
                # audio_track: optional zero-based audio stream index from the
                # track selector UI.  Appended to hls_proxy URL so ffmpeg can
                # select the correct stream with -map 0:a:<N>.
                req_audio_track = data.get("audio_track")
                at_param = f"&audio_track={int(req_audio_track)}" if isinstance(req_audio_track, int) else ""

                if req_audio_track is not None:
                    state.log(f"[PROBE] Audio track switch requested: track {req_audio_track} "
                              f"(transcode={needs_transcode}, reason={transcode_reason})")
                    if isinstance(stream_info, dict):
                        stream_info = dict(stream_info, active_audio_track=int(req_audio_track))

                # If a specific audio track was requested on a stream that wouldn't
                # otherwise need transcoding (direct MPEG-TS, direct HLS), force an
                # audio_only transcode so ffmpeg can apply -map 0:a:<N>.
                # Video is copied (not re-encoded) — cheap remux + audio re-encode only.
                if req_audio_track is not None and not needs_transcode:
                    needs_transcode  = True
                    transcode_reason = f"audio track selection (track {req_audio_track})"
                    state.log(f"[PROBE] Forcing audio_only transcode for track selection: track {req_audio_track}")
                    # Rebuild stream_info with transcode=True so JS panel reflects new state
                    stream_info = dict(stream_info, transcode=True, transcode_reason=transcode_reason)

                if needs_transcode:
                    vod_flag = "1" if is_vod else "0"
                    # audio_only when: bad audio codec, OR explicit track selection
                    # (video is browser-compatible so no need for full libx264 re-encode)
                    audio_only_issue = (
                        (transcode_reason or "").startswith("incompatible audio")
                        or (transcode_reason or "").startswith("audio track selection")
                    )
                    if is_multiview:
                        if audio_only_issue:
                            state.log(f"[PROBE] MV audio → hls_proxy: {transcode_reason}")
                            audio_url = f"/api/hls_proxy?audio_only=1&vod={vod_flag}{at_param}&url={quote(url, safe='')}"
                            return jsonify({"url": audio_url, "hevc": False, "stream_info": stream_info})
                        else:
                            return jsonify({"url": url, "hevc": True, "stream_info": stream_info})
                    else:
                        state.log(f"[PROBE] Routing to transcode proxy: {transcode_reason}"
                                  + (f" [audio track {req_audio_track}]" if at_param else ""))
                        if audio_only_issue:
                            # Copy video stream, re-encode audio only — much cheaper
                            # than a full libx264 video re-encode.
                            transcode_url = f"/api/hls_proxy?audio_only=1&vod={vod_flag}{at_param}&url={quote(url, safe='')}"
                        else:
                            transcode_url = f"/api/hls_proxy?transcode=1&vod={vod_flag}{at_param}&url={quote(url, safe='')}"
                        return jsonify({"url": transcode_url, "hevc": True, "stream_info": stream_info})

            return jsonify({"url": url, "stream_info": stream_info})
        except Exception as e:
            state.log(f"[PROBE] Error: {type(e).__name__}: {e}")
            return jsonify({"url": "", "error": str(e)})
