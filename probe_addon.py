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


# ── ffprobe codec inspector ───────────────────────────────────────────────────

def probe_stream_codecs(url: str, pre_input_args=None, timeout=15,
                        ffprobe_path="ffprobe"):
    """Run ffprobe on *url* and return a codec/duration dict, or None on failure.

    Return value on success:
        {
          "audio":    [str, ...],   # codec names for each audio stream
          "video":    [str, ...],   # codec names for each video stream
          "subtitle": [str, ...],   # codec names for each subtitle stream
          "duration": float | None, # container duration in seconds
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
        result  = {"audio": [], "video": [], "subtitle": [], "duration": None}
        for s in streams:
            typ   = s.get("codec_type")
            codec = s.get("codec_name")
            if typ == "audio" and codec:
                result["audio"].append(codec)
            elif typ == "video" and codec:
                result["video"].append(codec)
            elif typ == "subtitle" and codec:
                result["subtitle"].append(codec)
        dur = data.get("format", {}).get("duration")
        if dur:
            try:
                result["duration"] = float(dur)
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


# ── Flask route registration ──────────────────────────────────────────────────

def register_probe_routes(flask_app, state, run_async, _make_client, ffprobe_path):
    """Register /api/resolve.

    Parameters
    ----------
    flask_app    : Flask application instance
    state        : shared AppState object
    run_async    : helper that runs a coroutine on the worker event loop
    _make_client : async context-manager factory returning a portal client
    ffprobe_path : absolute path (or bare name) of the ffprobe binary
    """

    # Capture ffprobe_path in closure so the route handler doesn't need a global.
    _FFPROBE_PATH = ffprobe_path

    @flask_app.route("/api/resolve", methods=["POST"])
    def api_resolve():
        data = request.get_json(force=True)
        item = data.get("item", {})
        mode = data.get("mode", "live")
        # Validate and sanitize
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
                is_vod = mode in ('vod', 'series')  # VOD needs different handling than live

                # Normalise URL for quick extension checks.
                url_lower_full = url.lower()
                url_lower      = url_lower_full.split('?')[0]

                # ==== UNIVERSAL CODEC PROBE ====
                # We probe every URL regardless of extension — HEVC and AC3/DTS/EAC3
                # audio appear in plain live streams, Xtream URLs, stalker-portal links,
                # and anything else with no recognisable container hint in the path.
                # Skipping the probe for "unknown" URLs was the source of missed transcodes.
                #
                # Fast path: URL contains an explicit HEVC extension — skip ffprobe entirely.
                codecs = None   # populated below when ffprobe runs
                if any(ext in url_lower for ext in ['.hevc', '.265', '.h265']):
                    needs_transcode  = True
                    transcode_reason = "hevc by extension"
                    state.log(f"[RESOLVE] HEVC suspected by extension: {url_lower[-20:]}")

                else:
                    # Always run ffprobe — timeout kept short (8 s) so channel-switch
                    # latency stays acceptable even for slow live streams.
                    # Pass User-Agent and protocol_whitelist — many MAC/Xtream portals
                    # reject ffprobe's default Lavf user-agent and ffprobe needs the
                    # whitelist to open http/https/tcp streams without a config file.
                    _probe_pre_args = [
                        "-user_agent", "VLC/3.0.0 LibVLC/3.0.0",
                        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                    ]
                    codecs = probe_stream_codecs(url, pre_input_args=_probe_pre_args, timeout=8,
                                                 ffprobe_path=_FFPROBE_PATH)

                    if codecs:
                        needs_transcode, transcode_reason, detected_codec = _check_codecs(codecs)
                        if needs_transcode:
                            if transcode_reason.startswith("hevc"):
                                state.log(f"[RESOLVE] HEVC video detected: {detected_codec}")
                            else:
                                acodec = codecs["audio"][0].lower()
                                state.log(f"[RESOLVE] Audio codec needs transcode: {acodec}")
                        else:
                            if codecs.get("audio"):
                                acodec = codecs["audio"][0].lower()
                                state.log(f"[RESOLVE] Audio codec OK: {acodec}")
                            a_str = codecs.get('audio', ['?'])[0] if codecs.get('audio') else 'none'
                            state.log(f"[RESOLVE] All codecs playable: v={detected_codec}, a={a_str}")

                    else:
                        # ffprobe timed-out or couldn't open the stream.
                        # For HLS (m3u8) URLs the TS byte-scan cannot read HLS playlists —
                        # it always returns all-False, which is misleading.  Instead, retry
                        # ffprobe once with HLS-specific protocol whitelist args.
                        _is_hls_url = '.m3u8' in url_lower or '.m3u8' in url_lower_full
                        if _is_hls_url:
                            state.log(f"[RESOLVE] ffprobe failed on HLS URL — retrying with HLS protocol args")
                            _hls_probe_args = [
                                "-user_agent", "VLC/3.0.0 LibVLC/3.0.0",
                                "-protocol_whitelist", "file,http,https,tcp,tls,crypto,hls,applehttp",
                                "-allowed_extensions", "ALL",
                            ]
                            codecs = probe_stream_codecs(url, pre_input_args=_hls_probe_args, timeout=12,
                                                         ffprobe_path=_FFPROBE_PATH)
                            if codecs:
                                state.log(f"[RESOLVE] ffprobe HLS retry succeeded")
                                needs_transcode, transcode_reason, detected_codec = _check_codecs(codecs)
                                if needs_transcode:
                                    if transcode_reason.startswith("hevc"):
                                        state.log(f"[RESOLVE] HEVC video detected: {detected_codec}")
                                    else:
                                        acodec = codecs["audio"][0].lower()
                                        state.log(f"[RESOLVE] Audio codec needs transcode: {acodec}")
                                else:
                                    if codecs.get("audio"):
                                        acodec = codecs["audio"][0].lower()
                                        state.log(f"[RESOLVE] Audio codec OK: {acodec}")
                                    a_str = codecs.get('audio', ['?'])[0] if codecs.get('audio') else 'none'
                                    state.log(f"[RESOLVE] HLS retry: all codecs playable: v={detected_codec}, a={a_str}")
                            else:
                                state.log(f"[RESOLVE] ffprobe HLS retry also failed — playing direct")
                        else:
                            # Non-HLS URL: fall through to TS byte-scan below
                            state.log(f"[RESOLVE] ffprobe failed, attempting TS probe then direct play")

                # ── TS byte-scan fallback ────────────────────────────────────────────
                # When ffprobe failed (codecs is None) we fall back to reading the
                # raw MPEG-TS packet headers directly.  Previously this only ran for
                # play_token= URLs, leaving every other URL type uncovered when
                # ffprobe timed out.  Now it runs for ALL URLs — if the stream is not
                # MPEG-TS the parser just returns all-False within 1880 bytes and we
                # fall through to direct play as before.
                # Guard: skip for HLS (m3u8) URLs — the TS parser cannot read HLS
                # manifests and always returns all-False.  ffprobe retry above is the
                # correct fallback for HLS; if that also failed, play direct.
                _is_hls_url = '.m3u8' in url_lower or '.m3u8' in url_lower_full
                if not needs_transcode and codecs is None and not _is_hls_url:
                    try:
                        ts_info = _probe_ts_streams(url)
                        if ts_info["hevc"]:
                            needs_transcode  = True
                            transcode_reason = "hevc (ts probe)"
                            if 'play_token=' in url:
                                is_vod = False
                            state.log(f"[RESOLVE] TS probe: HEVC video detected")
                        elif ts_info["bad_audio"]:
                            needs_transcode  = True
                            transcode_reason = f"incompatible audio ({ts_info['audio_codec']}) (ts probe)"
                            if 'play_token=' in url:
                                is_vod = False
                            state.log(f"[RESOLVE] TS probe: bad audio detected: {ts_info['audio_codec']}")
                        else:
                            state.log(f"[RESOLVE] TS probe: no transcode needed (or not MPEG-TS)")
                    except Exception as pe:
                        state.log(f"[RESOLVE] TS probe failed: {pe}")

                # ── Fresh token for short-lived Stalker CDN URLs ─────────────────────
                # CDNs like lx20.net issue single-use or connection-bound tokens.
                # ffprobe above opened a connection and consumed/bound the token.
                # Re-call resolve_item_url now to get a fresh token URL for actual
                # playback — skip all probing this time since we have codec info.
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
                            state.log(f"[RESOLVE] Fresh token URL obtained for playback (probe used previous token)")
                            url = fresh_url
                    except Exception as _rfe:
                        state.log(f"[RESOLVE] Fresh token re-fetch failed ({_rfe}) — using probe URL")
                # ─────────────────────────────────────────────────────────────────────

                # Apply transcode if needed
                if needs_transcode:
                    vod_flag = "1" if is_vod else "0"
                    audio_only_issue = (transcode_reason or "").startswith("incompatible audio")
                    if is_multiview:
                        if audio_only_issue:
                            state.log(f"[RESOLVE] MV audio transcode → hls_proxy: {transcode_reason}")
                            audio_url = f"/api/hls_proxy?audio_only=1&vod={vod_flag}&url={quote(url, safe='')}"
                            return jsonify({"url": audio_url, "hevc": False})
                        else:
                            # HEVC video: let multiview_addon handle it natively
                            return jsonify({"url": url, "hevc": True})
                    else:
                        state.log(f"[RESOLVE] Routing to transcode proxy: {transcode_reason}")
                        if audio_only_issue:
                            # Copy video, re-encode audio only — much cheaper than full libx264 re-encode
                            transcode_url = f"/api/hls_proxy?audio_only=1&vod={vod_flag}&url={quote(url, safe='')}"
                        else:
                            transcode_url = f"/api/hls_proxy?transcode=1&vod={vod_flag}&url={quote(url, safe='')}"
                        return jsonify({"url": transcode_url, "hevc": True})

            return jsonify({"url": url})
        except Exception as e:
            state.log(f"[RESOLVE] Error: {type(e).__name__}: {e}")
            return jsonify({"url": "", "error": str(e)})
