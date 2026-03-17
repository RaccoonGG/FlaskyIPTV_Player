"""
cast_addon.py  —  Casting integration for FlaskAppPlayerDownloaderv29_byGG.py
=============================================================================
Integrates Chromecast, DLNA/UPnP and AirPlay casting into the Flask IPTV portal.

Drop this file in the same directory as your Flask app, then follow the four
integration steps below.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRATION  (four small changes to FlaskAppPlayerDownloaderv29_byGG.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — add import at the top of the Flask app (after all other imports):

    from cast_addon import register_cast_routes, get_cast_proxy

STEP 2 — start the stream proxy right after  `state = AppState()`:

    get_cast_proxy().start()

STEP 3 — register cast routes right after  `flask_app = Flask(__name__)`:

    register_cast_routes(flask_app, state, run_async, _make_client)

STEP 4 — add one script tag inside HTML_TEMPLATE, just before </body>:

    <script src="/api/cast/ui.js"></script>

That's it — no other files required.  This file is fully self-contained.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OPTIONAL DEPENDENCIES  (install any subset — each protocol degrades gracefully)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    pip install pychromecast          # Chromecast / Google TV
    pip install async-upnp-client     # DLNA / UPnP media renderers
    pip install pyatv                 # AirPlay (Apple TV, HomePod, …)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUGS FIXED vs. original casting.py / stream_proxy.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  • ConnectionError renamed → CastConnectionError (was shadowing the Python built-in)
  • DLNA video path now calls get_transcoded_url() instead of the erroneous
    get_audio_url() — fixes silent video-stream failure on DLNA TVs
  • AirPlay play_direct route sends the raw portal URL directly (pyatv does not
    support custom HTTP headers, so the stream proxy is bypassed for AirPlay;
    auth is already embedded in the resolved URL query-string)
"""

# ─────────────────────────────────────────────────────────────────────────────
# Standard library
# ─────────────────────────────────────────────────────────────────────────────
import asyncio
import base64
import collections
import hashlib
import http.server
import json
import logging
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

from flask import jsonify, request, Response

LOG = logging.getLogger(__name__)
_app_state_ref = None  # set by register_cast_routes; used by _HLSConverter._log_stderr


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — HELPERS  (MIME detection, HTTP header builder)
# ═════════════════════════════════════════════════════════════════════════════

def _detect_mime_type(url: str, default: str = "video/mp2t") -> str:
    """Detect MIME type from URL with heuristics for IPTV and audio streams.

    Tries URL-extension matching, radio-station token matching, a best-effort
    HEAD request, and finally falls back to *default*.
    Directly ported from casting.py _detect_mime_type().
    """
    u = url.lower()

    # High-priority radio/audio tokens — must appear before extension checks
    if any(tok in u for tok in ("radio.", "streamon.fm", "/listen/", "icecast", "shoutcast")):
        if ".m3u8" not in u:
            return "audio/mpeg"

    if ".m3u8" in u:
        return "application/x-mpegURL"
    if ".ts" in u:
        return "video/mp2t"
    if ".mp4" in u:
        return "video/mp4"
    if ".mkv" in u:
        return "video/x-matroska"
    if ".avi" in u:
        return "video/x-msvideo"
    if u.endswith((".mp3", ".m3u", ".pls")):
        return "audio/mpeg"
    if u.endswith((".aac", ".m4a")):
        return "audio/aac"
    if u.endswith((".ogg", ".oga", ".opus")):
        return "audio/ogg"
    if u.endswith(".flac"):
        return "audio/flac"
    if u.endswith((".wav", ".wave")):
        return "audio/wav"

    # Best-effort HEAD request — non-fatal, times out in 3 s
    try:
        if url.startswith("http"):
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=3) as resp:
                ctype = resp.headers.get("Content-Type", "")
                if ctype:
                    ctype = ctype.split(";")[0].strip().lower()
                    if ctype in ("application/octet-stream",
                                 "binary/octet-stream",
                                 "application/octetstream"):
                        return default
                    # IPTV servers sometimes return audio/mpeg for MPEG-TS video —
                    # don't trust audio/* MIME for URLs that look like video streams
                    if ctype.startswith("audio/") and any(
                            tok in u for tok in ("live.php", "play.php",
                                                 "/play/", "stream=", "exten=")):
                        return default
                    return ctype
    except Exception:
        pass

    # Generic stream-path tokens — be very conservative, only obvious audio paths
    # NOTE: do NOT match "/stream" here — IPTV URLs like "play/live.php?stream=123"
    # contain the word "stream" but are video MPEG-TS, not audio.
    if any(tok in u for tok in ("radio/", "/radio", "icecast/", "/shoutcast")):
        return "audio/mpeg"

    # IPTV live.php / play.php streams are always MPEG-TS video
    if "live.php" in u or "play.php" in u or "/play/live" in u:
        return "video/mp2t"

    return default


def _channel_http_headers(channel: Optional[Dict]) -> Dict:
    """Build per-channel HTTP header dict from item metadata.

    Mirrors http_headers.py channel_http_headers() exactly.
    Keys recognised: http-user-agent, http-referrer/referer, http-origin,
    http-cookie, http-authorization, http-accept, http-headers (list).
    """
    headers: Dict = {}
    if not channel:
        return headers

    def _copy(keys, target):
        for key in keys:
            val = channel.get(key)
            if val:
                headers[target] = val
                return

    _copy(["http-user-agent"],               "user-agent")
    _copy(["http-referrer", "http-referer"], "referer")
    _copy(["http-origin"],                   "origin")
    _copy(["http-cookie"],                   "cookie")
    _copy(["http-authorization"],            "authorization")
    _copy(["http-accept"],                   "accept")

    extra = channel.get("http-headers")
    if isinstance(extra, list):
        headers["_extra"] = [str(h) for h in extra if h]

    return headers


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — STREAM PROXY
# Self-contained TCP proxy server.  Cast devices (Chromecast, DLNA TV, etc.)
# cannot use Bearer tokens or custom headers — they just fetch a plain URL.
# This proxy relays the portal stream to a plain http://LAN_IP:PORT/… URL that
# any device on the local network can reach.
#
# Two proxy paths:
#   /stream?url=…&mode=audio   → buffered MP3 (FFmpeg re-encode if needed)
#   /transcode/<id>/stream.m3u8→ live HLS segments from FFmpeg
#
# Directly ported from stream_proxy.py with minor cleanup.
# ═════════════════════════════════════════════════════════════════════════════

def _get_ffmpeg() -> str:
    """Resolve ffmpeg, preferring a PyInstaller-bundled copy over PATH."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = os.path.join(sys._MEIPASS, "ffmpeg.exe")
        if os.path.exists(bundled):
            return bundled
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        for candidate in (
            os.path.join(base, "_internal", "ffmpeg.exe"),
            os.path.join(base, "ffmpeg.exe"),
        ):
            if os.path.exists(candidate):
                return candidate
    if os.path.exists("ffmpeg.exe"):
        return os.path.abspath("ffmpeg.exe")
    return shutil.which("ffmpeg") or "ffmpeg"


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _StreamBuffer:
    """Thread-safe ring buffer that decouples the download producer from
    the HTTP-serve consumer.  Fills to *initial_fill* bytes before unblocking
    reads, absorbing FFmpeg startup latency (~3 s at 320 kbps)."""

    def __init__(self, max_size: int = 16 * 1024 * 1024,
                 initial_fill: int = 128 * 1024):
        self.max_size     = max_size
        self.initial_fill = initial_fill
        self._buf         = collections.deque()
        self._size        = 0
        self._lock        = threading.Lock()
        self._not_empty   = threading.Condition(self._lock)
        self._not_full    = threading.Condition(self._lock)
        self.closed       = False
        self.error        = None
        self._filled      = False

    def write(self, chunk: bytes) -> None:
        with self._lock:
            while self._size + len(chunk) > self.max_size:
                if self.closed:
                    return
                self._not_full.wait()
            self._buf.append(chunk)
            self._size += len(chunk)
            if not self._filled:
                if self._size >= self.initial_fill:
                    self._filled = True
                    self._not_empty.notify_all()
            else:
                self._not_empty.notify()

    def read(self) -> Optional[bytes]:
        with self._lock:
            while not self._buf or (not self._filled and not self.closed):
                if self.closed:
                    if self._buf:
                        break
                    if self.error:
                        raise self.error
                    return None
                if not self._filled and self._size >= self.initial_fill:
                    self._filled = True
                    break
                self._not_empty.wait()
            chunk = self._buf.popleft()
            self._size -= len(chunk)
            self._not_full.notify()
            return chunk

    def close(self, error=None) -> None:
        with self._lock:
            self.closed = True
            self.error  = error
            self._not_empty.notify_all()
            self._not_full.notify_all()


class _HLSConverter:
    """Wraps an FFmpeg subprocess that converts any input stream to a live HLS
    playlist (2-second segments, 5-segment rolling window).

    The source is fed via stdin from a download thread so auth headers can be
    applied before FFmpeg ever sees the data.
    """

    def __init__(self, source_url: str, headers: Optional[Dict] = None,
                 profile: str = "auto"):
        self.source_url  = source_url
        self.headers     = headers or {}
        self.profile     = profile
        self.user_agent  = (self.headers.get("User-Agent")
                            or self.headers.get("user-agent")
                            or "Mozilla/5.0")
        self.temp_dir    = tempfile.mkdtemp(prefix="iptv_cast_")
        self.playlist    = os.path.join(self.temp_dir, "stream.m3u8")
        self.process: Optional[subprocess.Popen] = None
        self.last_access = time.time()
        self._start()

    @staticmethod
    def _is_hls(url: str) -> bool:
        path = url.lower().split("?")[0]
        if path.endswith(".m3u8") or path.endswith(".m3u"):
            return True
        # MAC/Stalker portals encode the stream format in the query string.
        # e.g. play/live.php?mac=...&extension=m3u8
        # The path never ends in .m3u8, but the server returns an M3U8 manifest.
        # Detect this so we use FFmpeg direct-URL mode instead of pipe mode —
        # piping M3U8 text to FFmpeg stdin causes "Invalid data" errors.
        qs = url.lower()
        if "extension=m3u8" in qs or "extension=m3u" in qs:
            return True
        return False

    def _start(self) -> None:
        ffmpeg    = _get_ffmpeg()
        is_hls    = self._is_hls(self.source_url)
        ua        = self.user_agent

        # Build header string for FFmpeg's -headers option
        hdr_lines = f"User-Agent: {ua}\r\n"
        for k, v in self.headers.items():
            kl = k.lower()
            if kl not in ("user-agent",):
                hdr_lines += f"{k}: {v}\r\n"

        hls_out_flags = [
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "5",
            "-hls_flags",
            "delete_segments+split_by_time+independent_segments+append_list+discont_start",
            "-hls_segment_type", "mpegts",
            # -hls_version removed: deprecated/no-op in modern FFmpeg builds
            "-hls_init_time", "0",
            "-flush_packets", "1",
            "-start_number", "1",
            "-hls_segment_filename", os.path.join(self.temp_dir, "seg_%d.ts"),
            "-mpegts_flags", "pat_pmt_at_beginning",
            self.playlist,
        ]

        # Video codec by profile:
        #   "chromecast" -> H.264 re-encode (Chromecast does not support HEVC;
        #                   -c:v copy passes HEVC through and device plays black/silent)
        #   "auto" / else -> copy (fast path for DLNA / browser)
        if self.profile == "chromecast":
            video_codec = [
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-profile:v", "high",
                "-level:v", "4.1",
                "-pix_fmt", "yuv420p",
            ]
            LOG.info("[CAST] HLSConverter: chromecast profile -> H.264 re-encode")
        else:
            video_codec = ["-c:v", "copy"]

        audio_codec = [
            "-c:a", "aac", "-profile:a", "aac_low",
            "-b:a", "320k", "-ac", "2", "-ar", "44100",
        ]

        if is_hls:
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "warning",
                "-headers", hdr_lines,
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-analyzeduration", "3000000", "-probesize", "3000000",
                "-fflags", "nobuffer+genpts+igndts",
                "-flags", "low_delay",
                "-i", self.source_url,
                "-map", "0:v?", "-map", "0:a?",
            ] + video_codec + audio_codec + hls_out_flags
            LOG.info("[CAST] HLSConverter: direct URL mode, profile=%s", self.profile)
            try:
                self.process = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=_NO_WINDOW,
                )
                self._pump_thread = None
                threading.Thread(target=self._log_stderr, daemon=True).start()
            except Exception as exc:
                LOG.error("HLSConverter FFmpeg start failed: %s", exc)
        else:
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-analyzeduration", "5000000", "-probesize", "5000000",
                "-fflags", "nobuffer+genpts+igndts",
                "-flags", "low_delay",
                "-i", "pipe:0",
                "-map", "0:v?", "-map", "0:a?",
            ] + video_codec + audio_codec + hls_out_flags
            LOG.info("[CAST] HLSConverter: pipe mode, profile=%s", self.profile)
            try:
                self.process = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=_NO_WINDOW,
                )
                self._pump_thread = threading.Thread(
                    target=self._pump, daemon=True,
                )
                self._pump_thread.start()
                threading.Thread(target=self._log_stderr, daemon=True).start()
            except Exception as exc:
                LOG.error("HLSConverter FFmpeg start failed: %s", exc)

    def _log_stderr(self) -> None:
        """Stream FFmpeg stderr to the cast log so errors are visible."""
        try:
            for line in self.process.stderr:
                line = line.decode("utf-8", errors="replace").rstrip()
                if line:
                    LOG.info("[CAST][ffmpeg] %s", line)
                    if _app_state_ref:
                        try:
                            _app_state_ref.log(f"[CAST][ffmpeg] {line}")
                        except Exception:
                            pass
        except Exception:
            pass

    def _pump(self) -> None:
        """Download source → feed FFmpeg stdin (non-HLS only)."""
        try:
            req = urllib.request.Request(self.source_url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                while self.process and self.process.poll() is None:
                    chunk = resp.read(32768)
                    if not chunk:
                        break
                    try:
                        self.process.stdin.write(chunk)
                        self.process.stdin.flush()
                    except Exception:
                        break
        except Exception as exc:
            LOG.error("HLSConverter pump error: %s", exc)
        finally:
            if self.process and self.process.stdin:
                try:
                    self.process.stdin.close()
                except Exception:
                    pass

    def stop(self) -> None:
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
            self.process = None
        if os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
            except Exception:
                pass

    def is_alive(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def touch(self) -> None:
        self.last_access = time.time()

    def wait_for_playlist(self, timeout: float = 10.0) -> bool:
        """Block until FFmpeg has written at least one .ts segment into the playlist.

        A size check alone is not sufficient: FFmpeg writes the playlist header
        (~115 bytes with -hls_flags independent_segments etc.) before any segment
        lines appear, so >100 bytes can be True on an empty-segment manifest.
        We require at least one line that ends with .ts.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.playlist):
                try:
                    with open(self.playlist, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    if any(line.strip().endswith(".ts")
                           for line in text.splitlines()):
                        return True
                except Exception:
                    pass
            if not self.is_alive():
                return False
            time.sleep(0.2)
        return False


class _CastProxyHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for the cast stream proxy.

    Routes:
      GET /stream?url=…&mode=audio[&headers=<b64json>]
           Buffered audio relay (MP3, transcoded via FFmpeg if not plain MP3)
      GET /transcode/<session>/stream.m3u8
           Live HLS playlist (rewritten URLs)
      GET /transcode/<session>/seg_N.ts
           Individual HLS segments
      GET /bootstrap.ts
           1-second black-frame segment served while real HLS warms up
      OPTIONS *   CORS pre-flight
    """

    def log_message(self, fmt, *args):
        LOG.info("[CAST][proxy] %s", (fmt % args).strip())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        proxy  = get_cast_proxy()

        # ── /stream  (audio proxy) ──────────────────────────────────────────
        if parsed.path in ("/stream", "/audio", "/proxy"):
            qs         = urllib.parse.parse_qs(parsed.query)
            target_url = (qs.get("url") or [None])[0]
            if not target_url:
                return self.send_error(400)
            mode = ((qs.get("mode") or [""])[0]).lower()

            # Decode optional base64-JSON headers
            req_headers: Dict = {}
            raw_h = (qs.get("headers") or [None])[0]
            if raw_h:
                try:
                    req_headers = json.loads(
                        base64.b64decode(raw_h).decode()
                    )
                except Exception:
                    pass
            req_headers.setdefault(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36",
            )

            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Icy-MetaData",  "1")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            # Determine whether FFmpeg transcoding is needed
            is_mp3       = target_url.lower().endswith(".mp3")
            needs_transcode = not is_mp3
            buf = _StreamBuffer(max_size=16 * 1024 * 1024,
                                initial_fill=128 * 1024)

            def _producer():
                try:
                    if needs_transcode:
                        cmd = [
                            _get_ffmpeg(), "-hide_banner", "-loglevel", "error",
                            "-probesize", "32k", "-analyzeduration", "500000",
                            "-i", "pipe:0", "-vn",
                            "-c:a", "libmp3lame", "-b:a", "320k",
                            "-ar", "44100", "-f", "mp3", "pipe:1",
                        ]
                        proc = subprocess.Popen(
                            cmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            creationflags=_NO_WINDOW,
                        )

                        def _feed():
                            try:
                                req = urllib.request.Request(
                                    target_url, headers=req_headers)
                                with urllib.request.urlopen(req, timeout=15) as r:
                                    while proc and proc.poll() is None:
                                        chunk = r.read(8192)
                                        if not chunk:
                                            break
                                        try:
                                            proc.stdin.write(chunk)
                                            proc.stdin.flush()
                                        except Exception:
                                            break
                            except Exception:
                                pass
                            finally:
                                if proc and proc.stdin:
                                    try:
                                        proc.stdin.close()
                                    except Exception:
                                        pass

                        threading.Thread(target=_feed, daemon=True).start()

                        while True:
                            chunk = proc.stdout.read(8192)
                            if not chunk:
                                break
                            buf.write(chunk)
                        proc.wait()
                    else:
                        req = urllib.request.Request(
                            target_url, headers=req_headers)
                        with urllib.request.urlopen(req, timeout=15) as r:
                            while True:
                                chunk = r.read(8192)
                                if not chunk:
                                    break
                                buf.write(chunk)

                    buf.close()
                except Exception as exc:
                    LOG.error("Cast proxy producer error: %s", exc)
                    buf.close(error=exc)

            threading.Thread(target=_producer, daemon=True).start()

            try:
                while True:
                    chunk = buf.read()
                    if chunk is None:
                        break
                    self.wfile.write(chunk)
            except Exception:
                buf.close()
            return

        # ── /transcode/<session>/... (HLS) ──────────────────────────────────
        if parsed.path.startswith("/transcode/"):
            parts = parsed.path.strip("/").split("/")
            # parts = ["transcode", session_id, filename]
            if len(parts) < 3:
                return self.send_error(404)
            session_id, filename = parts[1], parts[2]
            converter = proxy.get_converter(session_id)
            if not converter:
                return self.send_error(404)
            converter.touch()

            if filename == "stream.m3u8":
                # Block until FFmpeg has written at least one segment (up to 15s).
                # This is better than returning a bootstrap placeholder — VLC handles
                # a slow HTTP response fine, but gets confused by placeholder playlists.
                ready = converter.wait_for_playlist(timeout=15)
                if not ready:
                    # FFmpeg failed or took too long — log and return empty live playlist
                    # so the client keeps retrying rather than giving up entirely
                    LOG.warning("[CAST] FFmpeg not ready after 15s for session %s (alive=%s)",
                                session_id, converter.is_alive())
                    if _app_state_ref:
                        _app_state_ref.log(f"[CAST] ⚠ FFmpeg slow/failed — retrying for session {session_id[:8]}")
                    data = (
                        "#EXTM3U\n#EXT-X-VERSION:3\n"
                        "#EXT-X-TARGETDURATION:2\n"
                        "#EXT-X-MEDIA-SEQUENCE:0\n"
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return

                # Rewrite segment URLs to point back to this proxy
                LOG.info("[CAST] Serving playlist for session %s", session_id[:8])
                try:
                    with open(converter.playlist, "r", encoding="utf-8") as fh:
                        lines = fh.readlines()
                    base = (f"http://{proxy.host}:{proxy.port}"
                            f"/transcode/{session_id}/")
                    out  = [
                        "#EXTM3U", "#EXT-X-VERSION:3",
                        "#EXT-X-TARGETDURATION:2",
                        "#EXT-X-DISCONTINUITY",
                    ]
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("#EXTM3U") or \
                                line.startswith("#EXT-X-VERSION"):
                            continue
                        if not line.startswith("#"):
                            out.append(base + line)
                        else:
                            out.append(line)
                    data = "\n".join(out).encode()
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/vnd.apple.mpegurl")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    self.send_error(500)
                return

            # Serve individual .ts segments
            seg_path = os.path.join(converter.temp_dir, filename)
            if not os.path.exists(seg_path):
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                with open(seg_path, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile)
            except Exception:
                pass
            return

        # ── /bootstrap.ts (1-second black frame, instant play start) ────────
        if parsed.path == "/bootstrap.ts":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            cmd = [
                _get_ffmpeg(), "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i",
                "color=c=black:s=640x360:r=10:d=1",
                "-f", "lavfi", "-i",
                "anullsrc=r=44100:cl=stereo",
                "-t", "1",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-b:v", "1M",
                "-c:a", "aac", "-b:a", "64k",
                "-f", "mpegts", "-muxrate", "2M", "pipe:1",
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
            try:
                data = proc.stdout.read()
                if data:
                    self.wfile.write(data)
            except Exception:
                pass
            finally:
                try:
                    proc.terminate()
                except Exception:
                    pass
            return

        # ── /relay?url=…  (transparent byte relay, no transcode) ──────────────
        # Used for DLNA cast: phone fetches the stream from this PC,
        # PC is the only connection to the IPTV server (avoids 1-connection limit).
        # For HLS (.m3u8) it also rewrites segment URLs to go through this relay.
        if parsed.path == "/relay":
            qs         = urllib.parse.parse_qs(parsed.query)
            target_url = (qs.get("url") or [None])[0]
            if not target_url:
                return self.send_error(400)

            req_headers: Dict = {}
            raw_h = (qs.get("headers") or [None])[0]
            if raw_h:
                try:
                    req_headers = json.loads(base64.b64decode(raw_h).decode())
                except Exception:
                    pass
            req_headers.setdefault("User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36")

            try:
                req = urllib.request.Request(target_url, headers=req_headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    ctype = resp.headers.get("Content-Type", "application/octet-stream")
                    data  = resp.read()

                is_hls = (".m3u8" in target_url.lower()
                          or "mpegurl" in ctype.lower()
                          or "x-mpegurl" in ctype.lower())

                if is_hls:
                    # Rewrite segment/sub-manifest URLs so they also go through relay
                    proxy_base = (f"http://{get_cast_proxy().host}"
                                  f":{get_cast_proxy().port}/relay?url=")
                    base_url   = target_url.rsplit("/", 1)[0] + "/"
                    enc_h      = raw_h or ""
                    lines = data.decode("utf-8", errors="replace").splitlines()
                    out   = []
                    for line in lines:
                        if line.startswith("#") or not line.strip():
                            out.append(line)
                        else:
                            seg = (line if line.startswith("http")
                                   else base_url + line)
                            relay_seg = (proxy_base
                                         + urllib.parse.quote(seg, safe="")
                                         + (f"&headers={enc_h}" if enc_h else ""))
                            out.append(relay_seg)
                    data = "\n".join(out).encode()

                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                LOG.error("[CAST] relay error for %s: %s", target_url, exc)
                self.send_error(502)
            return

        self.send_error(404)


class CastStreamProxy:
    """Manages the cast proxy TCP server and HLS converter sessions."""

    def __init__(self):
        self._server   = None
        self._thread   = None
        self.port      = 0
        self.host      = self._detect_lan_ip()
        self._convs:    Dict[str, _HLSConverter] = {}
        self._lock      = threading.Lock()
        self._running   = False

    @staticmethod
    def _detect_lan_ip() -> str:
        """Return the LAN IP that cast devices on the same network can reach.

        Three-method cascade: connected socket (most reliable) → hostname
        lookup → interface scan.  Falls back to 127.0.0.1.
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                ip = info[4][0]
                if "." in ip and not ip.startswith("127."):
                    return ip
        except Exception:
            pass
        return "127.0.0.1"

    def start(self) -> None:
        if self._server:
            return
        # Bind to port 0 → OS picks a free port
        self._server = socketserver.ThreadingTCPServer(
            (self.host, 0), _CastProxyHandler
        )
        self.port     = self._server.server_address[1]
        self._running = True
        self._open_firewall()
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
            name="CastProxyServer",
        )
        self._thread.start()
        threading.Thread(
            target=self._cleanup_loop, daemon=True,
            name="CastProxyCleanup",
        ).start()
        LOG.info("Cast proxy started at http://%s:%s", self.host, self.port)

    def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.shutdown()
        with self._lock:
            for conv in self._convs.values():
                conv.stop()
            self._convs.clear()

    # ── URL builders ──────────────────────────────────────────────────────────

    def _encode_headers(self, headers: Optional[Dict]) -> str:
        if not headers:
            return ""
        clean = {k: str(v) for k, v in headers.items()
                 if v is not None and k != "_extra"}
        return base64.b64encode(json.dumps(clean).encode()).decode()

    def get_audio_url(self, target_url: str,
                      headers: Optional[Dict] = None) -> str:
        """Return a proxy URL that streams *target_url* as buffered MP3."""
        params: Dict = {"url": target_url, "mode": "audio"}
        enc = self._encode_headers(headers)
        if enc:
            params["headers"] = enc
        return (f"http://{self.host}:{self.port}"
                f"/stream?{urllib.parse.urlencode(params)}")

    def stop_all_sessions(self) -> None:
        """Kill all active HLS converter sessions. Call before starting a new cast."""
        with self._lock:
            for c in self._convs.values():
                c.stop()
            self._convs.clear()

    def get_transcoded_url(self, target_url: str,
                           headers: Optional[Dict] = None,
                           profile: str = "auto") -> str:
        """Return a proxy URL that streams *target_url* as live HLS."""
        tag        = profile
        session_id = hashlib.md5(
            f"{target_url}|{tag}".encode()
        ).hexdigest()
        with self._lock:
            if session_id not in self._convs:
                self._convs[session_id] = _HLSConverter(
                    target_url, headers, profile
                )
            else:
                self._convs[session_id].touch()
        return (f"http://{self.host}:{self.port}"
                f"/transcode/{session_id}/stream.m3u8")

    def get_relay_url(self, target_url: str,
                     headers: Optional[Dict] = None) -> str:
        """Return a proxy URL that relays *target_url* byte-for-byte (no transcode).
        For HLS manifests the proxy rewrites segment URLs to also go through relay,
        so the cast device never opens a direct connection to the IPTV server.
        """
        params: Dict = {"url": target_url}
        enc = self._encode_headers(headers)
        if enc:
            params["headers"] = enc
        return (f"http://{self.host}:{self.port}"
                f"/relay?{urllib.parse.urlencode(params)}")

    def get_converter(self, session_id: str) -> Optional[_HLSConverter]:
        with self._lock:
            return self._convs.get(session_id)

    # ── House-keeping ────────────────────────────────────────────────────────

    def _cleanup_loop(self) -> None:
        while self._running:
            time.sleep(15)
            now = time.time()
            with self._lock:
                stale = [sid for sid, c in self._convs.items()
                         if now - c.last_access > 90]
                for sid in stale:
                    self._convs[sid].stop()
                    del self._convs[sid]
                    LOG.debug("Cast proxy: cleaned up session %s", sid)

    def _open_firewall(self) -> None:
        """On Windows, open a transient inbound firewall rule for the proxy
        port so Chromecast/DLNA devices on the same network can reach it."""
        if os.name != "nt" or not self.port:
            return
        rule = f"IPTV Cast Proxy ({self.port})"
        try:
            flags = _NO_WINDOW
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule",
                 f"name={rule}"],
                capture_output=True, creationflags=flags,
            )
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "add", "rule",
                 f"name={rule}", "dir=in", "action=allow",
                 "protocol=TCP", f"localport={self.port}",
                 "profile=any"],   # must cover Public — home WiFi is often classified Public
                capture_output=True, creationflags=flags,
            )
        except Exception:
            pass


# Module-level singleton — shared by all casters
_CAST_PROXY = CastStreamProxy()


def get_cast_proxy() -> CastStreamProxy:
    return _CAST_PROXY


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — CASTING LAYER
# Ported from casting.py with the following fixes applied:
#   • ConnectionError renamed CastConnectionError (built-in collision fixed)
#   • DLNA video path now calls get_transcoded_url() (was erroneous get_audio_url)
#   • ChromecastCaster / DLNACaster use get_cast_proxy() (local singleton)
#   • AirPlay uses the raw portal URL directly (pyatv has no header support)
# ═════════════════════════════════════════════════════════════════════════════

class CastProtocol(Enum):
    CHROMECAST = "Chromecast"
    DLNA       = "DLNA"
    UPNP       = "UPnP"
    AIRPLAY    = "AirPlay"


@dataclass
class CastDevice:
    name:       str
    protocol:   CastProtocol
    identifier: str
    host:       str
    port:       int
    metadata:   Dict = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return f"{self.name} [{self.protocol.value}]"

    @property
    def unique_id(self) -> str:
        return f"{self.protocol.value}:{self.identifier}"

    def to_dict(self) -> Dict:
        # Serialize metadata but strip non-JSON-serializable values (e.g. pyatv conf objects)
        safe_meta = {k: v for k, v in self.metadata.items()
                     if isinstance(v, (str, int, float, bool, type(None)))}
        return {
            "name":         self.name,
            "display_name": self.display_name,
            "protocol":     self.protocol.value,
            "identifier":   self.identifier,
            "host":         self.host,
            "port":         self.port,
            "unique_id":    self.unique_id,
            "metadata":     safe_meta,   # DLNA connect needs location URL from here
        }


class CastError(Exception):
    pass

class CastConnectionError(CastError):  # renamed: was ConnectionError (shadowed built-in)
    pass

class PlaybackError(CastError):
    pass


class _BaseCaster(ABC):
    @abstractmethod
    async def discover(self, timeout: float = 5.0) -> List[CastDevice]:
        pass
    @abstractmethod
    async def connect(self, device: CastDevice) -> None:
        pass
    @abstractmethod
    async def play(self, url: str, title: str = "IPTV Stream",
                   content_type: str = "video/mp2t",
                   headers: Optional[Dict] = None) -> None:
        pass
    @abstractmethod
    async def stop(self) -> None:
        pass
    @abstractmethod
    async def pause(self) -> None:
        pass
    @abstractmethod
    async def resume(self) -> None:
        pass
    @abstractmethod
    async def set_volume(self, level: float) -> None:
        pass
    @abstractmethod
    async def disconnect(self) -> None:
        pass
    @abstractmethod
    def is_connected(self) -> bool:
        pass


# ── Chromecast ────────────────────────────────────────────────────────────────

try:
    import pychromecast as _pychromecast
    _HAS_CHROMECAST = True
except ImportError:
    _HAS_CHROMECAST = False
    _pychromecast = None


class ChromecastCaster(_BaseCaster):
    def __init__(self):
        if not _HAS_CHROMECAST:
            raise CastError(
                "pychromecast not installed — run: pip install pychromecast"
            )
        self._cast    = None
        self._browser = None

    async def discover(self, timeout: float = 5.0) -> List[CastDevice]:
        loop = asyncio.get_running_loop()

        def _do():
            try:
                casts, browser = _pychromecast.get_chromecasts(timeout=timeout)
                browser.stop_discovery()
                return casts
            except Exception as exc:
                LOG.warning("Chromecast discovery error: %s", exc)
                return []

        found = await loop.run_in_executor(None, _do)
        devices = []
        for cast in found:
            try:
                host = (getattr(cast.cast_info, "host", None)
                        or getattr(cast, "host", None))
                port = (getattr(cast.cast_info, "port", None)
                        or getattr(cast, "port", 8009))
                uuid = str(cast.uuid)
                devices.append(CastDevice(
                    name=(cast.name
                          or getattr(cast.cast_info, "friendly_name", None)
                          or f"Chromecast {uuid[:8]}"),
                    protocol=CastProtocol.CHROMECAST,
                    identifier=uuid,
                    host=host,
                    port=port,
                    metadata={
                        "uuid":       uuid,
                        "model_name": cast.model_name,
                        "cast_type":  cast.cast_type,
                    },
                ))
            except Exception as exc:
                LOG.debug("Skipping Chromecast device: %s", exc)
        return devices

    async def connect(self, device: CastDevice) -> None:
        loop = asyncio.get_running_loop()

        def _do():
            browser1 = None
            try:
                chromecasts, browser1 = _pychromecast.get_listed_chromecasts(
                    uuids=[device.identifier]
                )
                if not chromecasts:
                    # Stop the failed discovery browser before trying again
                    try:
                        browser1.stop_discovery()
                    except Exception:
                        pass
                    browser1 = None
                    # known_hosts= was added in pychromecast 9.x; guard against
                    # TypeError on older installs so we get a clear error, not a crash.
                    try:
                        chromecasts, browser1 = _pychromecast.get_chromecasts(
                            known_hosts=[device.host]
                        )
                    except TypeError:
                        # Older pychromecast — fall back to full scan
                        LOG.debug(
                            "[CAST][CC] known_hosts not supported, falling back to full scan"
                        )
                        chromecasts, browser1 = _pychromecast.get_chromecasts()
                    if not chromecasts:
                        raise CastConnectionError(
                            f"Cannot find Chromecast {device.name} "
                            f"at {device.host}"
                        )
                    # Prefer UUID match; fall back to first result
                    cast = next(
                        (c for c in chromecasts
                         if str(c.uuid) == device.identifier),
                        chromecasts[0],
                    )
                else:
                    cast = chromecasts[0]
                cast.wait()
                return cast, browser1
            except CastConnectionError:
                if browser1:
                    try:
                        browser1.stop_discovery()
                    except Exception:
                        pass
                raise
            except Exception as exc:
                if browser1:
                    try:
                        browser1.stop_discovery()
                    except Exception:
                        pass
                raise CastConnectionError(
                    f"Failed to connect to {device.name}: {exc}"
                ) from exc

        self._cast, self._browser = await loop.run_in_executor(None, _do)

    async def play(self, url: str, title: str = "IPTV Stream",
                   content_type: str = "video/mp2t",
                   headers: Optional[Dict] = None) -> None:
        """Play a stream on Chromecast.

        The URL received here has already been proxied/transcoded by the Flask
        route into a cast-reachable HLS URL — do NOT call get_transcoded_url()
        again or you'll double-transcode (session wrapping session).
        """
        if not self._cast:
            raise CastConnectionError("Not connected to a Chromecast")
        loop = asyncio.get_running_loop()

        def _do():
            # Reference casting.py hardcodes "video/mp2t" regardless of URL.
            # Using application/x-mpegURL triggers Chromecast's strict HLS client
            # which rejects our stream silently. video/mp2t is more permissive —
            # the Cast receiver sniffs the actual format from the content.
            mime = "video/mp2t"
            LOG.info("[CAST][CC] playing mime=%s url=%s", mime, url[:80])

            mc = self._cast.media_controller
            mc.play_media(url, mime, title=title, stream_type="LIVE")
            # block_until_active can raise if the Chromecast is slow to ack
            # (buffering, cold start).  A timeout here does NOT mean playback
            # failed — the cast was already sent.  Log and continue.
            try:
                mc.block_until_active(timeout=10)
            except Exception as _bua_exc:
                LOG.warning(
                    "[CAST][CC] block_until_active timed out (%s) — "
                    "cast was sent, device may still start playing", _bua_exc
                )

        await loop.run_in_executor(None, _do)

    async def stop(self) -> None:
        if self._cast:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._cast.media_controller.stop)
                LOG.info("[CAST][CC] Stop sent OK")
            except Exception as exc:
                LOG.warning("[CAST][CC] Stop failed: %s", exc)

    async def pause(self) -> None:
        if self._cast:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._cast.media_controller.pause)
                LOG.info("[CAST][CC] Pause sent OK")
            except Exception as exc:
                LOG.warning("[CAST][CC] Pause failed: %s", exc)

    async def resume(self) -> None:
        if self._cast:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._cast.media_controller.play)
                LOG.info("[CAST][CC] Resume sent OK")
            except Exception as exc:
                LOG.warning("[CAST][CC] Resume failed: %s", exc)

    async def set_volume(self, level: float) -> None:
        if self._cast:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None, self._cast.set_volume, max(0.0, min(1.0, level)))
            except Exception as exc:
                LOG.debug("[CAST][CC] set_volume error: %s", exc)

    async def disconnect(self) -> None:
        if self._cast:
            try:
                self._cast.disconnect()
            except Exception:
                pass
            self._cast = None
        if self._browser:
            try:
                self._browser.stop_discovery()
            except Exception:
                pass
            self._browser = None

    def is_connected(self) -> bool:
        return self._cast is not None


# ── DLNA / UPnP ───────────────────────────────────────────────────────────────

_HAS_UPNP = False
_UPNP_IMPORT_ERROR = None
try:
    from async_upnp_client.client_factory import UpnpFactory
    from async_upnp_client.profiles.dlna import DmrDevice
    # async_search no longer used — replaced with raw SSDP socket in DLNACaster.discover()
    # SSDP_TARGET_V1 was removed in async-upnp-client 0.40; it was the "ssdp:all" search type
    _SSDP_TARGET_V1 = "ssdp:all"
    # AiohttpRequester moved to aiohttp_requester in async-upnp-client >= 0.33
    try:
        from async_upnp_client.aiohttp_requester import AiohttpRequester
    except ImportError:
        from async_upnp_client.aiohttp import AiohttpRequester
    _HAS_UPNP = True
except Exception as _upnp_exc:
    _UPNP_IMPORT_ERROR = str(_upnp_exc)
    LOG.warning(
        "[CAST] async-upnp-client import failed — DLNA/UPnP disabled. "
        "Error: %s  |  Try: pip install --upgrade async-upnp-client",
        _upnp_exc,
    )


class DLNACaster(_BaseCaster):
    _RENDERER_TYPES = [
        "urn:schemas-upnp-org:device:MediaRenderer:1",
        "urn:schemas-upnp-org:device:MediaRenderer:2",
    ]

    def __init__(self):
        if not _HAS_UPNP:
            raise CastError(
                "async-upnp-client not installed — "
                "run: pip install async-upnp-client"
            )
        self._device    = None
        self._factory   = None
        self._requester = None
        self._session   = None
        self._ctrl_url  = None
        self._svc_type  = "urn:schemas-upnp-org:service:AVTransport:1"

    async def discover(self, timeout: float = 5.0) -> List[CastDevice]:
        """SSDP discovery via raw sockets — avoids async-upnp-client API churn."""
        import socket as _socket
        import select as _select

        devices:   List[CastDevice] = []
        seen_locs: set = set()
        lan_ip     = get_cast_proxy().host

        SSDP_ADDR  = "239.255.255.250"
        SSDP_PORT  = 1900

        loop = asyncio.get_running_loop()

        def _raw_ssdp_search() -> List[dict]:
            results: List[dict] = []
            seen_inner: set = set()
            try:
                sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM,
                                      _socket.IPPROTO_UDP)
                sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                sock.setsockopt(_socket.IPPROTO_IP, _socket.IP_MULTICAST_TTL, 4)
                sock.setsockopt(
                    _socket.IPPROTO_IP, _socket.IP_MULTICAST_IF,
                    _socket.inet_aton(lan_ip),
                )
                sock.bind((lan_ip, 0))
                sock.setblocking(False)
            except Exception as exc:
                LOG.warning("[CAST][DLNA] Failed to create SSDP socket: %s", exc)
                return results

            for st in ("urn:schemas-upnp-org:device:MediaRenderer:1", "ssdp:all"):
                msg = (
                    "M-SEARCH * HTTP/1.1\r\n"
                    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                    'MAN: "ssdp:discover"\r\n'
                    "MX: 3\r\n"
                    f"ST: {st}\r\n"
                    "\r\n"
                ).encode()
                try:
                    sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
                    LOG.debug("[CAST][DLNA] M-SEARCH sent: ST=%r", st)
                except Exception as exc:
                    LOG.warning("[CAST][DLNA] M-SEARCH send error for ST=%r: %s", st, exc)

            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                readable, _, _ = _select.select([sock], [], [], min(remaining, 0.5))
                if not readable:
                    continue
                try:
                    data, addr = sock.recvfrom(4096)
                    text = data.decode("utf-8", errors="ignore")
                    hdrs: dict = {}
                    for line in text.splitlines()[1:]:
                        if ":" in line:
                            k, _, v = line.partition(":")
                            hdrs[k.strip().lower()] = v.strip()
                    location = hdrs.get("location", "")
                    if not location or location in seen_inner:
                        continue
                    seen_inner.add(location)
                    hdrs["_host"] = addr[0]
                    LOG.debug("[CAST][DLNA] Response from %s: location=%s st=%s",
                              addr[0], location, hdrs.get("st", ""))
                    results.append(hdrs)
                except Exception as exc:
                    LOG.debug("[CAST][DLNA] recvfrom error: %s", exc)

            sock.close()
            return results

        responses = await loop.run_in_executor(None, _raw_ssdp_search)
        LOG.debug("[CAST][DLNA] Raw SSDP done — %d unique response(s)", len(responses))

        for hdrs in responses:
            location = hdrs.get("location", "")
            if not location or location in seen_locs:
                continue
            seen_locs.add(location)
            usn    = hdrs.get("usn", "")
            st     = hdrs.get("st", "")
            host   = hdrs.get("_host", "")
            parsed = urllib.parse.urlparse(location)
            name   = usn.split("::")[0] if "::" in usn else usn
            if name.startswith("uuid:"):
                name = f"DLNA Device {name[5:13]}"
            elif not name:
                name = host or parsed.hostname or "UPnP Device"
            protocol = (CastProtocol.DLNA
                        if "DLNA" in st.upper() or "dlna" in location.lower()
                        else CastProtocol.UPNP)
            devices.append(CastDevice(
                name=name,
                protocol=protocol,
                identifier=usn or location,
                host=parsed.hostname or host,
                port=parsed.port or 80,
                metadata={"location": location, "st": st, "usn": usn},
            ))

        # Enrich with friendly names from device XML descriptions
        for device in devices:
            try:
                location = device.metadata.get("location", "")
                if not location:
                    continue
                LOG.debug("[CAST][DLNA] fetching device description: %s", location)
                req = AiohttpRequester()
                fac = UpnpFactory(req)
                ud  = await fac.async_create_device(location)
                if ud.friendly_name:
                    device.name = ud.friendly_name
                    LOG.debug("[CAST][DLNA] friendly_name=%r", ud.friendly_name)
                if ud.manufacturer:
                    device.metadata["manufacturer"] = ud.manufacturer
                if ud.model_name:
                    device.metadata["model_name"] = ud.model_name
                if "dlna" in (ud.model_name or "").lower() or \
                   "dlna" in (ud.manufacturer or "").lower():
                    device.protocol = CastProtocol.DLNA
                await req.async_close()
            except Exception as exc:
                LOG.debug("[CAST][DLNA] enrichment failed for %s: %s",
                          device.metadata.get("location", "?"), exc)

        LOG.info("[CAST][DLNA] discover complete — %d device(s): %s",
                 len(devices), [d.name for d in devices])
        return devices

    # ── Raw AVTransport helpers ──────────────────────────────────────────────

    @staticmethod
    async def _find_avtransport(location: str, requester) -> Optional[str]:
        """Fetch device XML and return the AVTransport control URL, or None."""
        try:
            fac = UpnpFactory(requester)
            ud  = await fac.async_create_device(location)
            for svc in ud.services.values():
                if "AVTransport" in svc.service_type:
                    return svc.control_url
        except Exception as exc:
            LOG.debug("[CAST][DLNA] _find_avtransport error: %s", exc)
        return None

    @staticmethod
    @staticmethod
    async def _soap(ctrl_url: str, action: str,
                    service_type: str, body: str) -> str:
        """Send a UPnP SOAP action and return the response text.
        Uses a fresh aiohttp session per call — avoids stale-session errors
        when the session from connect() has been idle for minutes.
        """
        import aiohttp as _aiohttp
        soap_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            f'<u:{action} xmlns:u="{service_type}">'
            f"{body}"
            f"</u:{action}>"
            "</s:Body>"
            "</s:Envelope>"
        )
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction":   f'"{service_type}#{action}"',
        }
        timeout = _aiohttp.ClientTimeout(total=10)
        async with _aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(ctrl_url, data=soap_body,
                                 headers=headers) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise PlaybackError(
                        f"SOAP {action} → HTTP {resp.status}: {text[:200]}"
                    )
                return text

    # ── connect / play / stop / pause / resume / volume / disconnect ──────────

    async def connect(self, device: CastDevice) -> None:
        location = device.metadata.get("location", "")
        if not location:
            raise CastConnectionError(f"No location URL for {device.name}")
        try:
            import aiohttp as _aiohttp
            self._requester = AiohttpRequester()
            self._session   = _aiohttp.ClientSession()

            # Find the AVTransport control URL (works for any renderer, not just
            # strict MediaRenderer devices like DmrDevice requires)
            ctrl_url = await self._find_avtransport(location, self._requester)
            if not ctrl_url:
                raise CastConnectionError(
                    f"{device.name} has no AVTransport service — "
                    "make sure BubbleUPnP Local Renderer is enabled, "
                    "not the Media Server"
                )
            # Make URL absolute if needed
            parsed = urllib.parse.urlparse(location)
            base   = f"{parsed.scheme}://{parsed.netloc}"
            self._ctrl_url = (ctrl_url if ctrl_url.startswith("http")
                              else base + ctrl_url)
            self._svc_type = "urn:schemas-upnp-org:service:AVTransport:1"
            self._device   = device   # just used as an "is connected" flag
            LOG.info("[CAST][DLNA] Connected to %s — AVTransport at %s",
                     device.name, self._ctrl_url)
        except CastConnectionError:
            await self.disconnect()
            raise
        except Exception as exc:
            await self.disconnect()
            raise CastConnectionError(
                f"Failed to connect to {device.name}: {exc}"
            ) from exc

    async def play(self, url: str, title: str = "IPTV Stream",
                   content_type: str = "video/mp2t",
                   headers: Optional[Dict] = None) -> None:
        if not self._device:
            raise CastConnectionError("Not connected to a DLNA device")

        # The URL has already been proxied by the Flask route — use it as-is.
        # Do NOT call get_transcoded_url() again here; that would create a second
        # FFmpeg session trying to transcode the first one (double-transcode bug).
        proxied    = url
        mime       = _detect_mime_type(url, content_type)
        upnp_class = ("object.item.audioItem.musicTrack"
                      if mime.startswith("audio/")
                      else "object.item.videoItem.videoBroadcast")
        dlna_flags = ("DLNA.ORG_OP=01;DLNA.ORG_CI=0;"
                      "DLNA.ORG_FLAGS=01700000000000000000000000000000")
        safe_title = xml_escape(title)
        safe_url   = xml_escape(proxied)
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="1" parentID="0" restricted="1">'
            f"<dc:title>{safe_title}</dc:title>"
            f"<upnp:class>{upnp_class}</upnp:class>"
            f'<res protocolInfo="http-get:*:{mime}:{dlna_flags}">{safe_url}</res>'
            "</item></DIDL-Lite>"
        )

        try:
            await self._soap(
                self._ctrl_url,
                "SetAVTransportURI", self._svc_type,
                f"<InstanceID>0</InstanceID>"
                f"<CurrentURI>{safe_url}</CurrentURI>"
                f"<CurrentURIMetaData>{xml_escape(didl)}</CurrentURIMetaData>",
            )
            await self._soap(
                self._ctrl_url,
                "Play", self._svc_type,
                "<InstanceID>0</InstanceID><Speed>1</Speed>",
            )
            LOG.info("[CAST][DLNA] Playing %r on %s", title,
                     self._device.name if self._device else "?")
        except Exception as exc:
            raise PlaybackError(f"DLNA playback failed: {exc}") from exc

    async def stop(self) -> None:
        if self._device:
            await self._soap(self._ctrl_url, "Stop",
                             self._svc_type,
                             "<InstanceID>0</InstanceID>")
            LOG.info("[CAST][DLNA] Stop sent OK")

    async def pause(self) -> None:
        if self._device:
            await self._soap(self._ctrl_url, "Pause",
                             self._svc_type,
                             "<InstanceID>0</InstanceID>")
            LOG.info("[CAST][DLNA] Pause sent OK")

    async def resume(self) -> None:
        if self._device:
            await self._soap(self._ctrl_url, "Play",
                             self._svc_type,
                             "<InstanceID>0</InstanceID><Speed>1</Speed>")
            LOG.info("[CAST][DLNA] Resume sent OK")

    async def set_volume(self, level: float) -> None:
        if self._device:
            try:
                vol = int(max(0.0, min(1.0, level)) * 100)
                svc = "urn:schemas-upnp-org:service:RenderingControl:1"
                # Find RenderingControl URL from device XML
                parsed  = urllib.parse.urlparse(
                    self._device.metadata.get("location", ""))
                base    = f"{parsed.scheme}://{parsed.netloc}"
                req     = AiohttpRequester()
                fac     = UpnpFactory(req)
                ud      = await fac.async_create_device(
                    self._device.metadata.get("location", ""))
                rc_url  = None
                for s in ud.services.values():
                    if "RenderingControl" in s.service_type:
                        rc_url = s.control_url
                        break
                await req.async_close()
                if rc_url:
                    rc_url = rc_url if rc_url.startswith("http") else base + rc_url
                    await self._soap(
                        rc_url, "SetVolume", svc,
                        f"<InstanceID>0</InstanceID>"
                        f"<Channel>Master</Channel>"
                        f"<DesiredVolume>{vol}</DesiredVolume>",
                    )
            except Exception as exc:
                LOG.debug("[CAST][DLNA] set_volume error: %s", exc)

    async def disconnect(self) -> None:
        self._device   = None
        self._ctrl_url = None
        self._factory  = None
        if hasattr(self, "_session") and self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        if self._requester:
            try:
                await self._requester.async_close()
            except Exception:
                pass
            self._requester = None

    def is_connected(self) -> bool:
        return self._device is not None


# ── AirPlay ───────────────────────────────────────────────────────────────────

try:
    import pyatv as _pyatv
    from pyatv import conf as _pyatv_conf
    _HAS_AIRPLAY = True
except ImportError:
    _HAS_AIRPLAY = False
    _pyatv       = None
    _pyatv_conf  = None


class AirPlayCaster(_BaseCaster):
    def __init__(self):
        if not _HAS_AIRPLAY:
            raise CastError(
                "pyatv not installed — run: pip install pyatv"
            )
        self._atv = None

    async def discover(self, timeout: float = 5.0) -> List[CastDevice]:
        devices = []
        try:
            # loop= param deprecated and ignored in pyatv 0.14+
            atvs = await _pyatv.scan(timeout=int(timeout))
            for atv in atvs:
                svc = atv.get_service(_pyatv_conf.Protocol.AirPlay)
                if not svc:
                    continue
                devices.append(CastDevice(
                    name=atv.name,
                    protocol=CastProtocol.AIRPLAY,
                    identifier=atv.identifier,
                    host=str(atv.address) if atv.address else "",
                    port=svc.port,
                    metadata={"conf": atv},
                ))
        except Exception as exc:
            LOG.warning("AirPlay discovery error: %s", exc)
        return devices

    async def connect(self, device: CastDevice,
                      credentials: Optional[str] = None) -> None:
        config = device.metadata.get("conf")

        async def _try(cfg):
            if credentials:
                cfg.set_credentials(_pyatv_conf.Protocol.AirPlay, credentials)
            return await _pyatv.connect(cfg)

        # Try with cached config first — only if we actually have it.
        # config will be None after a JSON round-trip (to_dict strips the pyatv
        # conf object), so we go straight to the re-scan in that case.
        if config:
            try:
                self._atv = await _try(config)
                return
            except CastConnectionError:
                raise
            except Exception as exc:
                LOG.debug("[CAST][AP] Initial connect failed (%s), re-scanning…", exc)

        # conf was None (JSON round-trip) or initial connect failed → re-scan.
        # This is the normal path when connecting from the UI after discovery.
        try:
            LOG.info("[CAST][AP] Re-scanning for %s…", device.identifier)
            atvs = await _pyatv.scan(identifier=device.identifier, timeout=3)
            if atvs:
                config = atvs[0]
                device.metadata["conf"] = config
            if not config:
                raise CastConnectionError(
                    f"AirPlay device {device.name!r} not found during re-scan"
                )
            self._atv = await _try(config)
        except CastConnectionError:
            raise
        except Exception as exc:
            raise CastConnectionError(
                f"Failed to connect to {device.name}: {exc}"
            ) from exc

    async def start_pairing(self, device: CastDevice):
        config = device.metadata.get("conf")
        try:
            atvs = await _pyatv.scan(identifier=device.identifier, timeout=3)
            if atvs:
                config = atvs[0]
                device.metadata["conf"] = config
        except Exception as exc:
            LOG.warning("AirPlay re-scan for pairing failed: %s", exc)
        if not config:
            raise CastError("Missing AirPlay configuration")
        config.set_credentials(_pyatv_conf.Protocol.AirPlay, None)
        return await _pyatv.pair(config, _pyatv_conf.Protocol.AirPlay)

    async def play(self, url: str, title: str = "IPTV Stream",
                   content_type: str = "video/mp2t",
                   headers: Optional[Dict] = None) -> None:
        """AirPlay: pyatv does not support custom HTTP headers.

        The proxy is intentionally skipped — the resolved portal URL already
        has authentication embedded (token in query-string for MAC/Xtream).
        """
        if not self._atv:
            raise CastConnectionError("Not connected to an AirPlay device")
        try:
            LOG.info("[CAST][AP] play_url: %s", url[:80])
            await self._atv.stream.play_url(url, position=0)
        except Exception as exc:
            if (_HAS_AIRPLAY and
                    isinstance(exc, _pyatv.exceptions.NotSupportedError)):
                raise PlaybackError(
                    "AirPlay device does not support play_url "
                    "(audio-only device or limited protocol)"
                )
            raise PlaybackError(f"AirPlay playback failed: {exc}") from exc

    async def stop(self) -> None:
        if self._atv:
            try:
                await self._atv.remote_control.stop()
                LOG.info("[CAST][AP] Stop sent OK")
            except Exception as exc:
                LOG.warning("[CAST][AP] Stop failed: %s", exc)

    async def pause(self) -> None:
        if self._atv:
            try:
                await self._atv.remote_control.pause()
                LOG.info("[CAST][AP] Pause sent OK")
            except Exception as exc:
                LOG.warning("[CAST][AP] Pause failed: %s", exc)

    async def resume(self) -> None:
        if self._atv:
            try:
                await self._atv.remote_control.play()
                LOG.info("[CAST][AP] Resume sent OK")
            except Exception as exc:
                LOG.warning("[CAST][AP] Resume failed: %s", exc)

    async def set_volume(self, level: float) -> None:
        if self._atv:
            try:
                await self._atv.audio.set_volume(level * 100)
            except Exception as exc:
                LOG.debug("[CAST][AP] set_volume error: %s", exc)

    async def disconnect(self) -> None:
        if self._atv:
            try:
                # close() is sync in pyatv but may be awaitable in future versions
                result = self._atv.close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
            self._atv = None

    def is_connected(self) -> bool:
        return self._atv is not None


# ── CastingManager ────────────────────────────────────────────────────────────

class CastingManager:
    """Manages casters and an active cast session on a private asyncio loop.

    Runs its own daemon thread + event loop so it never blocks or shares a
    loop with Flask's worker threads or the portal async helpers.
    """

    def __init__(self):
        self.casters: Dict[CastProtocol, _BaseCaster] = {}
        self.active_caster: Optional[_BaseCaster]     = None
        self.active_device: Optional[CastDevice]      = None
        self._loop:   Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread]          = None
        self._running = False

        # Instantiate whichever protocol backends are available
        if _HAS_CHROMECAST:
            self.casters[CastProtocol.CHROMECAST] = ChromecastCaster()
        if _HAS_UPNP:
            dlna = DLNACaster()
            self.casters[CastProtocol.DLNA] = dlna
            self.casters[CastProtocol.UPNP] = dlna   # share instance
        if _HAS_AIRPLAY:
            self.casters[CastProtocol.AIRPLAY] = AirPlayCaster()

    @property
    def available_protocols(self) -> List[str]:
        return [p.value for p in self.casters]

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop    = asyncio.new_event_loop()
        self._thread  = threading.Thread(
            target=self._run_loop, daemon=True,
            name="CastingManagerLoop",
        )
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3.0)

    def dispatch(self, coro):
        """Schedule *coro* on the background loop and block until it finishes."""
        if not self._running or not self._loop:
            raise CastError("CastingManager not running — call start() first")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)

    # ── High-level API called from Flask routes ───────────────────────────────

    def discover_all(self, timeout: float = 6.0) -> List[CastDevice]:
        return self.dispatch(self._discover_all_async(timeout))

    async def _discover_all_async(self, timeout: float) -> List[CastDevice]:
        unique = list({id(c): c for c in self.casters.values()}.values())
        results = await asyncio.gather(
            *[c.discover(timeout) for c in unique],
            return_exceptions=True,
        )
        devices = []
        for res in results:
            if isinstance(res, list):
                devices.extend(res)
        return sorted(devices, key=lambda d: d.name)

    def connect(self, device: CastDevice,
                credentials: Optional[str] = None) -> None:
        self.dispatch(self._connect_async(device, credentials))

    async def _connect_async(self, device: CastDevice,
                             credentials: Optional[str]) -> None:
        if self.active_caster:
            await self.active_caster.disconnect()
            self.active_caster = None
            self.active_device = None

        caster = (self.casters.get(device.protocol)
                  or self.casters.get(CastProtocol.DLNA))  # DLNA/UPnP alias
        if not caster:
            raise CastError(f"No caster for {device.protocol}")

        if isinstance(caster, AirPlayCaster):
            await caster.connect(device, credentials)
        else:
            await caster.connect(device)

        self.active_caster = caster
        self.active_device = device

    def play(self, url: str, title: str = "IPTV Stream",
             headers: Optional[Dict] = None) -> None:
        self.dispatch(self._play_async(url, title, headers))

    async def _play_async(self, url: str, title: str,
                          headers: Optional[Dict]) -> None:
        if not self.active_caster:
            raise CastConnectionError("No active cast device")
        await self.active_caster.play(url, title, headers=headers)

    def stop_playback(self) -> None:
        if self.active_caster:
            self.dispatch(self.active_caster.stop())

    def disconnect(self) -> None:
        self.dispatch(self._disconnect_async())

    async def _disconnect_async(self) -> None:
        if self.active_caster:
            await self.active_caster.disconnect()
        self.active_caster = None
        self.active_device = None

    def control(self, action: str, value: Any = None) -> None:
        """Dispatch a playback control action: pause/resume/stop/volume."""
        if not self.active_caster:
            return
        if action == "volume" and value is not None:
            self.dispatch(self.active_caster.set_volume(float(value)))
            return
        # Build coroutine lazily — eager dict would create all 3 at once,
        # leaving 2 unawaited → RuntimeWarning: coroutine never awaited.
        if action == "pause":
            self.dispatch(self.active_caster.pause())
        elif action == "resume":
            self.dispatch(self.active_caster.resume())
        elif action == "stop":
            self.dispatch(self.active_caster.stop())

    def is_connected(self) -> bool:
        return (self.active_caster is not None
                and self.active_caster.is_connected())


# Module-level singleton
_CAST_MANAGER: Optional[CastingManager] = None
_CAST_MANAGER_LOCK = threading.Lock()


def get_cast_manager() -> Optional[CastingManager]:
    return _CAST_MANAGER


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — FLASK ROUTES
# ═════════════════════════════════════════════════════════════════════════════

def register_cast_routes(flask_app, app_state, run_async_fn, make_client_fn):
    global _app_state_ref
    _app_state_ref = app_state
    """Register all /api/cast/* routes on *flask_app*.

    Parameters
    ----------
    flask_app       : the Flask application instance
    app_state       : the AppState singleton (for logging and state checks)
    run_async_fn    : run_async() helper from the Flask app
    make_client_fn  : _make_client context-manager factory from the Flask app
    """
    global _CAST_MANAGER

    with _CAST_MANAGER_LOCK:
        if _CAST_MANAGER is None:
            _CAST_MANAGER = CastingManager()
            _CAST_MANAGER.start()
            protos = _CAST_MANAGER.available_protocols or ['none installed']
            app_state.log(f"[CAST] Manager started — protocols: {protos}")
            if not _HAS_UPNP and _UPNP_IMPORT_ERROR:
                app_state.log(
                    f"[CAST] ⚠ DLNA/UPnP unavailable — import error: "
                    f"{_UPNP_IMPORT_ERROR}  "
                    f"(run: pip install --upgrade async-upnp-client)"
                )
            if not _HAS_CHROMECAST:
                app_state.log("[CAST] ℹ Chromecast disabled (pip install pychromecast)")
            if not _HAS_AIRPLAY:
                app_state.log("[CAST] ℹ AirPlay disabled (pip install pyatv)")

    manager = _CAST_MANAGER
    proxy   = get_cast_proxy()

    # ── /api/cast/status ──────────────────────────────────────────────────────

    @flask_app.route("/api/cast/status", methods=["GET"])
    def api_cast_status():
        connected = manager.is_connected()
        device    = manager.active_device
        return jsonify({
            "available":  bool(manager.casters),
            "protocols":  manager.available_protocols,
            "connected":  connected,
            "device":     device.to_dict() if device else None,
            "proxy_host": proxy.host,
            "proxy_port": proxy.port,
        })

    # ── /api/cast/discover ────────────────────────────────────────────────────

    @flask_app.route("/api/cast/discover", methods=["POST"])
    def api_cast_discover():
        if not manager.casters:
            return jsonify({"error": "No casting libraries installed",
                            "devices": []}), 200
        data    = request.get_json(force=True) or {}
        timeout = float(data.get("timeout", 6.0))
        app_state.log(f"[CAST] Discovering devices (timeout={timeout}s)…")
        try:
            devices = manager.discover_all(timeout=timeout)
            app_state.log(f"[CAST] Found {len(devices)} device(s)")
            return jsonify({"devices": [d.to_dict() for d in devices]})
        except Exception as exc:
            app_state.log(f"[CAST] Discovery error: {exc}")
            return jsonify({"error": str(exc), "devices": []}), 500

    # ── /api/cast/connect ─────────────────────────────────────────────────────

    @flask_app.route("/api/cast/connect", methods=["POST"])
    def api_cast_connect():
        data = request.get_json(force=True) or {}
        dev  = data.get("device", {})
        if not dev:
            return jsonify({"error": "No device provided"}), 400
        creds = data.get("credentials")

        try:
            protocol = CastProtocol(dev["protocol"])
        except (KeyError, ValueError) as exc:
            return jsonify({"error": f"Unknown protocol: {exc}"}), 400

        device = CastDevice(
            name=dev.get("name", "Unknown"),
            protocol=protocol,
            identifier=dev.get("identifier", ""),
            host=dev.get("host", ""),
            port=int(dev.get("port", 0)),
            metadata=dev.get("metadata", {}),
        )
        app_state.log(f"[CAST] Connecting to {device.display_name}…")
        try:
            manager.connect(device, credentials=creds)
            app_state.log(f"[CAST] ✓ Connected to {device.display_name}")
            return jsonify({"ok": True, "device": device.to_dict()})
        except Exception as exc:
            app_state.log(f"[CAST] Connect failed: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ── /api/cast/play ────────────────────────────────────────────────────────
    # Resolve portal item → proxy URL → cast device.
    # Called with full item metadata so the portal client can build the URL.

    @flask_app.route("/api/cast/play", methods=["POST"])
    def api_cast_play():
        if not manager.is_connected():
            return jsonify({"error": "No cast device connected"}), 400

        data  = request.get_json(force=True) or {}
        item  = data.get("item", {})
        mode  = data.get("mode", "live")
        cat   = data.get("category", {})
        title = data.get("title", item.get("name", item.get("o_name", "Stream")))

        if mode not in ("live", "vod", "series"):
            mode = "live"

        # 1. Resolve raw portal URL
        try:
            async def _resolve():
                async with make_client_fn() as client:
                    return await client.resolve_item_url(mode, item, cat)
            raw_url = run_async_fn(_resolve())
        except Exception as exc:
            app_state.log(f"[CAST] Resolve error: {exc}")
            return jsonify({"error": f"Could not resolve stream URL: {exc}"}), 500

        if not raw_url:
            return jsonify({"error": "Portal returned empty URL"}), 400

        # 2. Build channel headers (User-Agent, Referer, etc.)
        headers = _channel_http_headers(item)

        # 3. Route to proxy or send directly
        import re as _re
        lan_ip = proxy.host
        raw_url = _re.sub(r"(https?://)(?:127\.0\.0\.1|localhost)", lambda m: m.group(1) + lan_ip, raw_url, count=1)

        mime = _detect_mime_type(raw_url)
        if mime.startswith("audio/"):
            proxy_url = proxy.get_audio_url(raw_url, headers)
        elif isinstance(manager.active_caster, DLNACaster):
            proxy_url = proxy.get_transcoded_url(raw_url, headers)
            LOG.info("[CAST][DLNA] transcoded HLS URL for phone: %s", proxy_url[:80])
        elif isinstance(manager.active_caster, (ChromecastCaster, AirPlayCaster)):
            # Both Chromecast and AirPlay need:
            #   • H.264 re-encode (HEVC sources play black/silent on both)
            #   • Flask port 5000 URL (random cast-proxy port is blocked by Windows Firewall)
            #   • Wait for FFmpeg before telling device to fetch
            _raw_proxy = proxy.get_transcoded_url(raw_url, headers, profile="chromecast")
            import re as _re_play
            _pm = _re_play.search(r"/transcode/([a-f0-9]+)/stream\.m3u8", _raw_proxy)
            _ps = _pm.group(1) if _pm else None
            if _ps:
                _pc = proxy.get_converter(_ps)
                if _pc:
                    app_state.log(f"[CAST] ⏳ Waiting for FFmpeg…")
                    _pr = _pc.wait_for_playlist(timeout=15)
                    if _pr:
                        app_state.log(f"[CAST] ✓ FFmpeg ready")
                    else:
                        app_state.log(f"[CAST] ⚠ FFmpeg slow — sending anyway")
                _fp  = request.environ.get("SERVER_PORT", 5000)
                proxy_url = f"http://{proxy.host}:{_fp}/cast/hls/{_ps}/stream.m3u8"
                LOG.info("[CAST] Flask HLS URL for %s: %s", type(manager.active_caster).__name__, proxy_url)
            else:
                proxy_url = _raw_proxy
                LOG.warning("[CAST] Could not extract session from %s", _raw_proxy[:80])
        else:
            proxy_url = proxy.get_transcoded_url(raw_url, headers)

        app_state.log(
            f"[CAST] Playing '{title}' on {manager.active_device.display_name} → {proxy_url[:80]}…"
        )
        try:
            manager.play(proxy_url, title, headers=None)  # headers already in proxy URL
            app_state.log(f"[CAST] ✓ Cast sent — device should start fetching from proxy")
            return jsonify({"ok": True, "proxy_url": proxy_url, "title": title})
        except Exception as exc:
            app_state.log(f"[CAST] Play error: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ── /api/cast/play_direct ─────────────────────────────────────────────────
    # Play a pre-resolved URL (e.g. from doPlay / external player flow).
    # No portal round-trip needed.

    @flask_app.route("/api/cast/play_direct", methods=["POST"])
    def api_cast_play_direct():
        if not manager.is_connected():
            return jsonify({"error": "No cast device connected"}), 400

        data  = request.get_json(force=True) or {}
        url   = (data.get("url") or "").strip()
        title = (data.get("title") or "Stream").strip()

        if not url or not url.startswith(("http://", "https://", "rtsp://")):
            # Most common cause: the Flask app called doPlay() with a relative
            # proxy path (e.g. /api/hls_proxy?...) which is not reachable by
            # cast devices.  The JS-side guard in castPlayDirect should catch
            # this first, but log it server-side too for diagnosability.
            LOG.warning("[CAST] play_direct rejected non-absolute URL: %r", url[:120])
            return jsonify({
                "error": f"Invalid URL (got: {url[:80]!r}) — "
                         "cast devices need an absolute http:// URL. "
                         "If the app routes HEVC through a local proxy, "
                         "cast via the /api/cast/play route instead."
            }), 400

        # If the URL points to localhost/127.0.0.1 (e.g. the Flask /api/proxy endpoint),
        # rewrite it to the LAN IP so cast devices on the network can actually reach it.
        lan_ip = proxy.host
        import re as _re
        url = _re.sub(r"(https?://)(?:127\.0\.0\.1|localhost)", lambda m: m.group(1) + lan_ip, url, count=1)

        LOG.info("[CAST] play_direct url=%s title=%r", url, title)
        app_state.log(f"[CAST] Direct play '{title}' → {url[:80]}…")

        mime    = _detect_mime_type(url)
        headers = data.get("headers")  # optional {key: val} from caller

        # Kill any lingering HLS converter sessions from previous casts.
        proxy.stop_all_sessions()

        # Source URL routing:
        #
        # DLNA: the DLNA renderer cannot set custom HTTP headers, so we route
        #   through Flask's /api/proxy which adds User-Agent: VLC/3.0.0.
        #   The device connects to Flask proxy lazily (only when it starts
        #   playing), so there is no eager IPTV connection race.
        #
        # Chromecast: FFmpeg CAN set custom headers via _HLSConverter.headers,
        #   so we connect DIRECTLY to the IPTV URL — no Flask proxy hop.
        #   Routing Chromecast through Flask proxy causes 458 "already streaming"
        #   errors because Flask proxy's streaming requests.get() (from a previous
        #   channel) stays alive until garbage-collected, and when FFmpeg eagerly
        #   opens a new connection via that same proxy the IPTV server sees 2
        #   simultaneous connections from the same MAC address.
        #
        # DLNA also needs Flask proxy URL to be LAN-IP reachable (127.0.0.1 is
        #   not accessible from the phone); Chromecast's FFmpeg runs locally so
        #   this rewrite is not needed.
        flask_port = request.environ.get("SERVER_PORT", 5000)
        flask_ts_base = f"http://127.0.0.1:{flask_port}"
        is_already_flask_proxied = url.startswith(flask_ts_base)

        if isinstance(manager.active_caster, DLNACaster) and mime == "video/mp2t" and not is_already_flask_proxied:
            # DLNA: route through Flask proxy (device can't set headers)
            source_url = f"{flask_ts_base}/api/proxy?url={urllib.parse.quote(url, safe='')}"
            app_state.log(f"[CAST] Routing via local proxy → {source_url[:60]}…")
        else:
            # Chromecast / AirPlay / HLS: use raw URL, headers injected below
            source_url = url

        # VLC User-Agent for Chromecast: inject into FFmpeg's urllib pump so
        # it reaches the IPTV server exactly as Flask's /api/proxy would send it.
        _vlc_ua = {"User-Agent": "VLC/3.0.0 LibVLC/3.0.0", "Accept": "*/*"}

        # AirPlay: transcode to HLS and serve via Flask /cast/hls/ on port 5000.
        # Sending the raw IPTV URL to the Apple device causes two problems:
        #   1. The Apple device connects to the IPTV server from its own IP with
        #      no custom headers — server sees it as a second connection and blocks.
        #   2. HEVC sources play black/silent on older Apple TV models that only
        #      support H.264 (same issue Chromecast had).
        # Using the same Flask-proxied HLS approach as Chromecast fixes both.
        if isinstance(manager.active_caster, AirPlayCaster):
            proxy_url_raw = proxy.get_transcoded_url(source_url, _vlc_ua, profile="chromecast")
            browser_url   = None

            import re as _re_ap
            _ap_match = _re_ap.search(r"/transcode/([a-f0-9]+)/stream\.m3u8", proxy_url_raw)
            ap_session = _ap_match.group(1) if _ap_match else None

            if ap_session:
                ap_converter = proxy.get_converter(ap_session)
                if ap_converter:
                    app_state.log("[CAST][AP] ⏳ Waiting for FFmpeg to produce first segment…")
                    ap_ready = ap_converter.wait_for_playlist(timeout=15)
                    if ap_ready:
                        app_state.log("[CAST][AP] ✓ FFmpeg ready — sending to AirPlay device")
                    else:
                        app_state.log("[CAST][AP] ⚠ FFmpeg took too long — sending anyway")
                lan_ip_ap     = proxy.host
                flask_port_ap = request.environ.get("SERVER_PORT", 5000)
                proxy_url     = f"http://{lan_ip_ap}:{flask_port_ap}/cast/hls/{ap_session}/stream.m3u8"
                LOG.info("[CAST][AP] Flask-served HLS URL: %s", proxy_url)
            else:
                proxy_url = proxy_url_raw
                LOG.warning("[CAST][AP] Could not extract session from %s, using raw proxy URL", proxy_url_raw[:80])
        elif mime.startswith("audio/"):
            proxy_url   = proxy.get_audio_url(source_url, headers)
            browser_url = None  # audio proxy is a streaming pipe, can't share
        elif isinstance(manager.active_caster, DLNACaster):
            # BubbleUPnP (and most DLNA renderers) can play raw MPEG-TS natively —
            # no HLS transcoding needed. Send the direct TS proxy URL with the LAN IP
            # rewritten so the phone can reach it.
            # LAN-IP rewrite: replace 127.0.0.1 with the PC's LAN IP.
            import re as _re2
            direct_url = _re2.sub(
                r"(https?://)127\.0\.0\.1",
                lambda m: m.group(1) + proxy.host,
                source_url, count=1
            )
            proxy_url      = direct_url
            browser_url    = None  # browser must NOT attach — would create 2nd IPTV connection
            LOG.info("[CAST][DLNA] direct TS proxy URL for phone: %s", proxy_url[:80])
        else:
            # Chromecast: FFmpeg transcodes source → HLS.
            # Pass _vlc_ua so _HLSConverter._pump sends VLC UA to the IPTV server.
            # Without this the server returns an HTML error page (not a TS stream)
            # and FFmpeg fails with "Invalid data found when processing input".
            # browser_url=None — browser must NOT reconnect while cast is active.
            proxy_url   = proxy.get_transcoded_url(source_url, _vlc_ua, profile="chromecast")
            browser_url = None

            # ── Extract session ID, wait for FFmpeg, then rewrite URL to Flask ──
            # The cast proxy runs on a random ephemeral port that Windows Firewall
            # blocks on Public (home WiFi) networks. Flask port 5000 is already
            # open. We serve the same HLS through /cast/hls/<session>/... on Flask.
            import re as _re_cc
            _cc_match = _re_cc.search(r"/transcode/([a-f0-9]+)/stream\.m3u8", proxy_url)
            cc_session = _cc_match.group(1) if _cc_match else None

            # Wait for FFmpeg NOW — before rewriting the URL — while cc_session
            # is still extractable from the original /transcode/... URL.
            # Bug fixed: waiting AFTER the URL rewrite meant proxy_url no longer
            # contained /transcode/ so get_converter("") returned None and the
            # wait was silently skipped entirely.
            if cc_session:
                converter = proxy.get_converter(cc_session)
                if converter:
                    app_state.log("[CAST] ⏳ Waiting for FFmpeg to produce first segment…")
                    ready = converter.wait_for_playlist(timeout=15)
                    if ready:
                        app_state.log("[CAST] ✓ FFmpeg ready — sending to Chromecast")
                    else:
                        app_state.log("[CAST] ⚠ FFmpeg took too long — sending anyway")
                        LOG.warning("[CAST][CC] FFmpeg not ready in 15s, sending anyway")
                else:
                    LOG.warning("[CAST][CC] No converter found for session %s", cc_session[:8])

                # Rewrite to Flask URL only after waiting
                lan_ip        = proxy.host
                flask_port_cc = request.environ.get("SERVER_PORT", 5000)
                proxy_url     = f"http://{lan_ip}:{flask_port_cc}/cast/hls/{cc_session}/stream.m3u8"
                LOG.info("[CAST][CC] Flask-served HLS URL: %s", proxy_url)

        app_state.log(
            f"[CAST] Sending to {manager.active_device.display_name}: {proxy_url[:80]}…"
        )
        LOG.info("[CAST] play_direct proxy_url=%s proxy_host=%s proxy_port=%s",
                 proxy_url, proxy.host, proxy.port)
        try:
            manager.play(proxy_url, title, headers=None)
            app_state.log(f"[CAST] ✓ SOAP sent — phone should start fetching from proxy")
            return jsonify({"ok": True, "proxy_url": browser_url})
        except Exception as exc:
            app_state.log(f"[CAST] Direct play error: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ── /api/cast/control ─────────────────────────────────────────────────────

    @flask_app.route("/api/cast/control", methods=["POST"])
    def api_cast_control():
        if not manager.is_connected():
            return jsonify({"error": "No cast device connected"}), 400
        data   = request.get_json(force=True) or {}
        action = data.get("action", "")
        value  = data.get("value")
        if action not in ("pause", "resume", "stop", "volume", "play"):
            return jsonify({"error": f"Unknown action: {action}"}), 400
        try:
            if action == "stop":
                manager.control("stop", value)
                proxy.stop_all_sessions()   # kill FFmpeg / release IPTV connection
                app_state.log("[CAST] ⏹ Stopped cast playback")
            elif action == "play":
                # Re-cast: caller passes {action:'play', url:'...', title:'...'}
                url   = (data.get("url")   or "").strip()
                title = (data.get("title") or "Stream").strip()
                if not url:
                    return jsonify({"error": "No URL provided for play"}), 400
                manager.play(url, title, headers=None)
                app_state.log(f"[CAST] ▶ Re-playing '{title}'")
            else:
                manager.control(action, value)
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── /api/cast/disconnect ──────────────────────────────────────────────────

    @flask_app.route("/api/cast/disconnect", methods=["POST"])
    def api_cast_disconnect():
        try:
            manager.disconnect()
            app_state.log("[CAST] Disconnected from cast device")
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── /cast/hls/<session>/<file>  ───────────────────────────────────────────
    # Serve HLS manifests and segments through Flask (port 5000) so Chromecast
    # can reach them without needing a separate firewall rule for the cast proxy
    # random port.  DLNA works because it uses port 5000 (/api/proxy); Chromecast
    # was failing because the cast proxy runs on an ephemeral port that Windows
    # Firewall blocks even on home (Public) networks.
    # Flask is already reachable on LAN (user accesses the web UI on port 5000).

    @flask_app.route("/cast/hls/<session_id>/stream.m3u8")
    def cast_hls_manifest(session_id):
        converter = proxy.get_converter(session_id)
        if not converter:
            return ("Session not found", 404)
        converter.touch()

        # Wait up to 15 s for FFmpeg to produce the first segment (same as cast proxy)
        ready = converter.wait_for_playlist(timeout=15)
        if not ready:
            LOG.warning("[CAST][Flask-HLS] FFmpeg not ready for session %s", session_id[:8])
            # Return a minimal live playlist so Chromecast retries rather than giving up
            data = (
                "#EXTM3U\n#EXT-X-VERSION:3\n"
                "#EXT-X-TARGETDURATION:2\n"
                "#EXT-X-MEDIA-SEQUENCE:0\n"
            )
            from flask import Response
            return Response(data, mimetype="application/vnd.apple.mpegurl")

        try:
            with open(converter.playlist, "r", encoding="utf-8") as fh:
                raw_playlist = fh.read()
        except Exception as exc:
            LOG.error("[CAST][Flask-HLS] Cannot read playlist: %s", exc)
            return ("Playlist read error", 500)

        # Log raw FFmpeg playlist so we can verify segment filenames
        LOG.info("[CAST][Flask-HLS] raw playlist for %s:\n%s", session_id[:8], raw_playlist)

        # Determine this request's LAN-reachable base URL so segments also come
        # through Flask on port 5000.
        lan_ip = proxy.host
        flask_port = request.environ.get("SERVER_PORT", 5000)
        base = f"http://{lan_ip}:{flask_port}/cast/hls/{session_id}/"

        out = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for line in raw_playlist.splitlines():
            line = line.strip()
            if not line:
                continue
            # Skip duplicate header lines — we already wrote them above
            if line.startswith("#EXTM3U") or line.startswith("#EXT-X-VERSION"):
                continue
            if not line.startswith("#"):
                # Segment filename: use basename only — some FFmpeg builds write
                # absolute paths e.g. /tmp/iptv_cast_xxx/seg_1.ts
                seg_name = os.path.basename(line)
                out.append(base + seg_name)
            else:
                # Pass through all other tags (#EXT-X-TARGETDURATION, #EXTINF,
                # #EXT-X-MEDIA-SEQUENCE, etc.) unchanged.
                # NOTE: #EXT-X-DISCONTINUITY must NOT appear at the top of the
                # manifest — only immediately before a specific segment entry.
                # We no longer inject it globally.
                out.append(line)

        manifest_body = "\n".join(out) + "\n"
        LOG.info("[CAST][Flask-HLS] rewritten manifest sent to phone:\n%s", manifest_body)
        from flask import Response
        return Response(manifest_body, mimetype="application/vnd.apple.mpegurl",
                        headers={"Access-Control-Allow-Origin": "*",
                                 "Cache-Control": "no-cache"})

    @flask_app.route("/cast/hls/<session_id>/<filename>")
    def cast_hls_segment(session_id, filename):
        converter = proxy.get_converter(session_id)
        if not converter:
            return ("Session not found", 404)
        converter.touch()

        # Sanitise filename — only allow seg_N.ts patterns
        import re as _re_seg
        if not _re_seg.match(r'^seg_\d+\.ts$', filename):
            return ("Invalid segment name", 400)

        seg_path = os.path.join(converter.temp_dir, filename)

        # Wait briefly for the segment file to appear (FFmpeg writes it async)
        deadline = time.time() + 4.0
        while not os.path.exists(seg_path) and time.time() < deadline:
            time.sleep(0.05)

        if not os.path.exists(seg_path):
            return ("Segment not found", 404)

        from flask import Response
        try:
            with open(seg_path, "rb") as fh:
                data = fh.read()
            return Response(data, mimetype="video/mp2t",
                            headers={"Access-Control-Allow-Origin": "*",
                                     "Cache-Control": "no-cache"})
        except Exception as exc:
            LOG.error("[CAST][Flask-HLS] Segment read error %s: %s", filename, exc)
            return ("Segment read error", 500)

    # ── /api/cast/ui.js ───────────────────────────────────────────────────────

    @flask_app.route("/api/cast/ui.js")
    def api_cast_ui_js():
        # Inject the proxy port so JS can detect proxy URLs and avoid re-casting them
        js = _CAST_UI_JS.replace(
            "window._castProxyPort = 0;",
            f"window._castProxyPort = {proxy.port};"
        )
        return Response(js, content_type="application/javascript; charset=utf-8")

    # Global error handler — ensures cast routes always return JSON, never HTML
    @flask_app.errorhandler(Exception)
    def _cast_error_handler(exc):
        from flask import request as _req
        from werkzeug.exceptions import HTTPException
        # Let normal HTTP errors (404, 405 etc) pass through untouched everywhere
        if isinstance(exc, HTTPException):
            return exc
        # Only intercept unexpected exceptions on /api/cast/* routes
        if _req.path.startswith("/api/cast/"):
            LOG.exception("[CAST] Unhandled exception in %s", _req.path)
            return jsonify({"error": str(exc)}), 500
        raise exc

    app_state.log("[CAST] Routes registered: /api/cast/*")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — FRONTEND JAVASCRIPT
# Self-contained, no external dependencies.
# Injects:
#   • Floating 📺 button (bottom-right)
#   • Sliding cast panel (Discover → device list → Connect → Controls)
#   • Auto-cast hook on window.doPlay — when cast mode is active, every
#     channel play is also sent to the cast device
#   • MutationObserver adds 📺 buttons next to existing 🎬 (external player)
#     buttons in the WON tab and anywhere class="won-ext-btn" appears
# ═════════════════════════════════════════════════════════════════════════════

_CAST_UI_JS = r"""
/* cast_addon UI — injected by /api/cast/ui.js */
(function () {
  'use strict';

  /* ── State ───────────────────────────────────────────────────────────── */
  let _devices     = [];      // [{name, display_name, protocol, identifier, host, port, unique_id, metadata}]
  let _connected   = false;
  let _device      = null;    // active device dict
  let _autoCast    = false;   // when true, every doPlay also casts
  let _castTitle   = '';
  let _discovering = false;
  window._castProxyPort = 0;  // replaced at serve-time with actual proxy port

  /* ── Helpers ─────────────────────────────────────────────────────────── */
  function _api(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {})
    }).then(r => {
      const ct = r.headers.get('content-type') || '';
      if (!ct.includes('application/json')) {
        // Server returned HTML (likely a 500 error page) — extract a useful message
        return r.text().then(txt => {
          const preview = txt.replace(/<[^>]+>/g, ' ').replace(/[\s]+/g, ' ').trim().slice(0, 120);
          console.error('[CAST] Non-JSON response from', path, ':', preview);
          return {error: 'Server error: ' + preview};
        });
      }
      return r.json();
    });
  }

  function _toast(msg, type) {
    if (typeof window.toast === 'function') {
      window.toast(msg, type || 'info');
    } else {
      console.log('[CAST]', msg);
    }
  }

  /* ── Inject CSS ──────────────────────────────────────────────────────── */
  const _css = `
    /* ── Cast button sits in header bar — minimal extra styling needed ── */
    #cast-fab{ position:relative; }
    #cast-fab svg{ display:block; }
    #cast-fab.connected svg{ color:var(--acc,#1a73e8); }
    #cast-fab .cast-badge{
      position:absolute;top:3px;right:3px;
      width:7px;height:7px;border-radius:50%;
      background:#00897b;border:1.5px solid var(--s1,#13131a);
      display:none;
    }
    #cast-fab.connected .cast-badge{ display:block; }

    /* ── Cast panel — drops down from header top-right ── */
    #cast-panel{
      position:fixed;top:44px;right:8px;
      width:320px;
      z-index:9999;background:#1e1e2e;color:#cdd6f4;
      border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,.6);
      font-family:inherit;font-size:14px;
      transform:translateY(-8px) scale(.97);transform-origin:top right;
      opacity:0;pointer-events:none;
      transition:opacity .18s,transform .18s;
    }
    #cast-panel.open{opacity:1;transform:none;pointer-events:all;}

    .cast-header{
      display:flex;align-items:center;justify-content:space-between;
      padding:14px 16px 10px;border-bottom:1px solid rgba(255,255,255,.08);
      font-weight:600;font-size:15px;
    }
    .cast-header span{display:flex;align-items:center;gap:8px;}
    .cast-close-btn{
      background:none;border:none;color:#7f849c;cursor:pointer;
      font-size:18px;line-height:1;padding:2px 4px;border-radius:4px;
    }
    .cast-close-btn:hover{color:#cdd6f4;background:rgba(255,255,255,.08);}

    .cast-body{padding:14px 16px;}

    .cast-section-label{
      font-size:11px;font-weight:600;letter-spacing:.06em;
      color:#7f849c;text-transform:uppercase;margin-bottom:8px;
    }

    .cast-device-list{
      max-height:160px;overflow-y:auto;margin-bottom:12px;
      border:1px solid rgba(255,255,255,.08);border-radius:8px;
    }
    .cast-device-item{
      display:flex;align-items:center;gap:10px;
      padding:9px 12px;cursor:pointer;
      transition:background .12s;
    }
    .cast-device-item:not(:last-child){
      border-bottom:1px solid rgba(255,255,255,.06);
    }
    .cast-device-item:hover{background:rgba(255,255,255,.05);}
    .cast-device-item.selected{background:rgba(26,115,232,.18);}
    .cast-device-item .dicon{font-size:18px;}
    .cast-device-info{flex:1;min-width:0;}
    .cast-device-name{
      font-weight:500;white-space:nowrap;overflow:hidden;
      text-overflow:ellipsis;
    }
    .cast-device-proto{font-size:11px;color:#7f849c;}

    .cast-empty{
      padding:18px;text-align:center;color:#7f849c;font-size:13px;
    }

    .cast-btn{
      width:100%;padding:9px;border:none;border-radius:8px;
      font-size:13px;font-weight:600;cursor:pointer;
      transition:background .14s,opacity .14s;
    }
    .cast-btn:disabled{opacity:.45;cursor:default;}
    .cast-btn-primary{background:#1a73e8;color:#fff;}
    .cast-btn-primary:not(:disabled):hover{background:#1558b0;}
    .cast-btn-secondary{
      background:rgba(255,255,255,.07);color:#cdd6f4;
      border:1px solid rgba(255,255,255,.1);
    }
    .cast-btn-secondary:not(:disabled):hover{background:rgba(255,255,255,.12);}
    .cast-btn-danger{background:#cf6679;color:#fff;}
    .cast-btn-danger:not(:disabled):hover{background:#b5495b;}

    .cast-status-bar{
      background:rgba(0,137,123,.15);border:1px solid rgba(0,137,123,.3);
      border-radius:8px;padding:10px 12px;margin-bottom:12px;
      font-size:13px;
    }
    .cast-status-bar .cs-device{font-weight:600;color:#80cbc4;}
    .cast-status-bar .cs-title{
      color:#7f849c;margin-top:2px;font-size:12px;
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    }

    .cast-controls{
      display:flex;gap:8px;margin-bottom:12px;
    }
    .cast-controls .cast-btn{flex:1;padding:8px 4px;font-size:12px;}

    .cast-vol-row{
      display:flex;align-items:center;gap:10px;margin-bottom:12px;
    }
    .cast-vol-label{font-size:12px;color:#7f849c;white-space:nowrap;}
    .cast-vol-slider{flex:1;accent-color:#1a73e8;cursor:pointer;}

    .cast-autocast-row{
      display:flex;align-items:center;justify-content:space-between;
      margin-bottom:12px;font-size:13px;
    }
    .cast-autocast-toggle{
      position:relative;width:36px;height:20px;
    }
    .cast-autocast-toggle input{opacity:0;width:0;height:0;}
    .cast-autocast-slider{
      position:absolute;inset:0;border-radius:20px;
      background:#313244;cursor:pointer;transition:background .2s;
    }
    .cast-autocast-slider::before{
      content:'';position:absolute;
      width:14px;height:14px;left:3px;bottom:3px;
      background:#fff;border-radius:50%;transition:transform .2s;
    }
    input:checked + .cast-autocast-slider{background:#1a73e8;}
    input:checked + .cast-autocast-slider::before{transform:translateX(16px);}

    .cast-btn-row{display:flex;gap:8px;}
    .cast-btn-row .cast-btn{flex:1;}

    /* status toast — appears below header when casting starts */
    #cast-fab-label{
      position:fixed;top:50px;right:8px;
      z-index:10000;background:#1e1e2e;color:#80cbc4;
      padding:6px 14px;border-radius:20px;font-size:12px;font-weight:600;
      box-shadow:0 2px 8px rgba(0,0,0,.4);pointer-events:none;
      opacity:0;transition:opacity .3s;white-space:nowrap;
    }
    #cast-fab-label.show{opacity:1;}

    /* Cast button injected next to external player buttons */
    .cast-ext-btn{
      display:inline-flex;align-items:center;gap:5px;
      background:rgba(26,115,232,.18);color:#82aaff;
      border:1px solid rgba(26,115,232,.3);
      border-radius:6px;padding:3px 9px;font-size:12px;cursor:pointer;
      transition:background .12s;margin-left:6px;
    }
    .cast-ext-btn:hover{background:rgba(26,115,232,.32);}
  `;
  const _styleEl = document.createElement('style');
  _styleEl.textContent = _css;
  document.head.appendChild(_styleEl);

  /* ── Build DOM ───────────────────────────────────────────────────────── */
  // The Cast tab button (#cast-fab) lives in #botnav in the Flask app HTML.
  // We only inject the panel and toast label here.
  document.body.insertAdjacentHTML('beforeend', `
    <div id="cast-fab-label"></div>
    <div id="cast-panel">
      <div class="cast-header">
        <span>
          <svg viewBox="0 0 24 24" fill="currentColor" style="width:18px;height:18px;flex-shrink:0"><path d="M1 18v3h3c0-1.66-1.34-3-3-3zm0-4v2c2.76 0 5 2.24 5 5h2c0-3.87-3.13-7-7-7zm18-7H5c-1.1 0-2 .9-2 2v3h2v-3h14v12h-5v2h5c1.1 0 2-.9 2-2V9c0-1.1-.9-2-2-2zm-18 3v2c4.97 0 9 4.03 9 9h2c0-6.08-4.93-11-11-11z"/></svg>
          Cast to device
        </span>
        <button class="cast-close-btn" id="cast-panel-close">✕</button>
      </div>
      <div class="cast-body" id="cast-body"></div>
    </div>
  `);

  const _fab       = document.getElementById('cast-fab');
  const _panel     = document.getElementById('cast-panel');
  const _body      = document.getElementById('cast-body');
  const _closeBtn  = document.getElementById('cast-panel-close');
  const _fabLabel  = document.getElementById('cast-fab-label');

  /* ── Render panel ────────────────────────────────────────────────────── */
  let _selectedIdx = -1;

  function _protoIcon(proto) {
    const map = {
      Chromecast: '📡', DLNA: '📺', UPnP: '🖥', AirPlay: '🍎'
    };
    return map[proto] || '📻';
  }

  function _renderDiscover() {
    _body.innerHTML = `
      <div class="cast-section-label">Network devices</div>
      <div id="cast-devlist" class="cast-device-list">
        <div class="cast-empty">Press "Discover" to find devices.</div>
      </div>
      <div class="cast-btn-row" style="margin-bottom:10px">
        <button class="cast-btn cast-btn-secondary" id="cast-disc-btn">
          🔍 Discover
        </button>
        <button class="cast-btn cast-btn-primary" id="cast-conn-btn" disabled>
          ✔ Connect
        </button>
      </div>
    `;
    document.getElementById('cast-disc-btn').onclick = _discover;
    document.getElementById('cast-conn-btn').onclick = _connect;
    _renderDeviceList();
    // Re-enable connect if we already have a selection from a previous scan
    const connBtn = document.getElementById('cast-conn-btn');
    if (connBtn) connBtn.disabled = _selectedIdx < 0;
  }

  function _renderDeviceList() {
    const el = document.getElementById('cast-devlist');
    if (!el) return;
    if (!_devices.length) {
      el.innerHTML = '<div class="cast-empty">No devices found.</div>';
      return;
    }
    el.innerHTML = _devices.map((d, i) => `
      <div class="cast-device-item${i === _selectedIdx ? ' selected' : ''}"
           data-idx="${i}">
        <span class="dicon">${_protoIcon(d.protocol)}</span>
        <div class="cast-device-info">
          <div class="cast-device-name">${_esc(d.name)}</div>
          <div class="cast-device-proto">${_esc(d.protocol)}</div>
        </div>
      </div>
    `).join('');
    el.querySelectorAll('.cast-device-item').forEach(el => {
      el.onclick = () => {
        _selectedIdx = parseInt(el.dataset.idx);
        _renderDeviceList();
        const connBtn = document.getElementById('cast-conn-btn');
        if (connBtn) connBtn.disabled = false;
      };
    });
  }

  function _renderConnected() {
    // Sync with the main app's currently-playing URL/title if we don't have one yet.
    // pUrl / pName are set by doPlay() in the main app on every channel play.
    if (!_castUrl && typeof pUrl !== 'undefined' && pUrl) {
      _castUrl   = pUrl;
      _castTitle = (typeof pName !== 'undefined' && pName) ? pName : _castUrl;
    }
    _body.innerHTML = `
      <div class="cast-status-bar">
        <div class="cs-device">📡 ${_esc(_device ? _device.display_name : '?')}</div>
        <div class="cs-title" id="cast-now-title">
          ${_castTitle ? '▶ ' + _esc(_castTitle) : 'Idle'}
        </div>
      </div>
      <div class="cast-autocast-row">
        <span>Auto-cast when I play a channel</span>
        <label class="cast-autocast-toggle">
          <input type="checkbox" id="cast-autocast-chk"
                 ${_autoCast ? 'checked' : ''}>
          <span class="cast-autocast-slider"></span>
        </label>
      </div>
      <div class="cast-vol-row">
        <span class="cast-vol-label">🔊 Vol</span>
        <input type="range" min="0" max="100" value="80"
               class="cast-vol-slider" id="cast-vol">
      </div>
      <div class="cast-controls">
        <button class="cast-btn cast-btn-secondary" id="cast-play-btn"
                title="Re-cast current stream">
          ▶ Play
        </button>
        <button class="cast-btn cast-btn-secondary" id="cast-pause-btn">
          ⏸ Pause
        </button>
        <button class="cast-btn cast-btn-secondary" id="cast-resume-btn">
          ▶ Resume
        </button>
        <button class="cast-btn cast-btn-danger" id="cast-stop-btn">
          ⏹ Stop
        </button>
      </div>
      <div class="cast-btn-row">
        <button class="cast-btn cast-btn-danger" id="cast-disc-device-btn">
          ⏏ Disconnect
        </button>
        <button class="cast-btn cast-btn-secondary" id="cast-back-btn">
          ← Devices
        </button>
      </div>
    `;
    document.getElementById('cast-autocast-chk').onchange = e => {
      _autoCast = e.target.checked;
    };
    document.getElementById('cast-vol').oninput = e => {
      _api('/api/cast/control', {action:'volume', value: e.target.value / 100});
    };

    function _ctrlBtn(id, action, label, workingLabel) {
      const btn = document.getElementById(id);
      if (!btn) return;
      btn.onclick = () => {
        btn.disabled = true;
        btn.textContent = workingLabel || '⏳';
        _api('/api/cast/control', {action})
          .then(d => {
            if (d.error) {
              _toast('Error: ' + d.error, 'err');
              btn.textContent = label;
              btn.disabled = false;
            } else {
              btn.textContent = label;
              btn.disabled = false;
              if (action === 'stop') {
                _castTitle = '';
                _castUrl   = '';
                const el = document.getElementById('cast-now-title');
                if (el) el.textContent = 'Idle';
                _showFabLabel('📡 ' + (_device ? _device.name : ''));
                // Re-render so play/pause/stop disable correctly
                _renderConnected();
              }
            }
          })
          .catch(e => {
            _toast('Error: ' + e, 'err');
            btn.textContent = label;
            btn.disabled = false;
          });
      };
    }

    // Play button — re-casts the last URL through castPlayDirect
    const playBtn = document.getElementById('cast-play-btn');
    if (playBtn) {
      playBtn.onclick = () => {
        if (!_castUrl) {
          _toast('Play a channel first to cast it', 'wrn');
          return;
        }
        window.castPlayDirect(_castUrl, _castTitle);
      };
    }

    _ctrlBtn('cast-pause-btn',  'pause',  '⏸ Pause',  '⏳ Pausing…');
    _ctrlBtn('cast-resume-btn', 'resume', '▶ Resume', '⏳ Resuming…');
    _ctrlBtn('cast-stop-btn',   'stop',   '⏹ Stop',   '⏳ Stopping…');

    document.getElementById('cast-disc-device-btn').onclick = () => {
      _api('/api/cast/disconnect', {}).then(() => {
        _connected = false; _device = null; _castTitle = ''; _castUrl = '';
        _fab.classList.remove('connected');
        _renderDiscover();
        _toast('Cast device disconnected', 'info');
      });
    };
    document.getElementById('cast-back-btn').onclick = () => {
      _renderDiscover();
    };
  }

  function _refreshPanel() {
    if (_connected) _renderConnected();
    else            _renderDiscover();
  }

  /* ── Actions ─────────────────────────────────────────────────────────── */
  function _discover() {
    if (_discovering) return;
    _discovering = true;
    const btn = document.getElementById('cast-disc-btn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Scanning…'; }
    _toast('Scanning for cast devices…', 'info');
    _api('/api/cast/discover', {timeout: 6})
      .then(d => {
        _devices     = d.devices || [];
        _selectedIdx = _devices.length > 0 ? 0 : -1;
        _renderDeviceList();
        const connBtn = document.getElementById('cast-conn-btn');
        if (connBtn) connBtn.disabled = _selectedIdx < 0;
        _toast(`Found ${_devices.length} device(s)`, _devices.length ? 'ok' : 'wrn');
      })
      .catch(e => _toast('Discovery error: ' + e, 'err'))
      .finally(() => {
        _discovering = false;
        const b = document.getElementById('cast-disc-btn');
        if (b) { b.disabled = false; b.textContent = '🔍 Discover'; }
      });
  }

  function _connect() {
    if (_selectedIdx < 0 || _selectedIdx >= _devices.length) return;
    const dev = _devices[_selectedIdx];
    const btn = document.getElementById('cast-conn-btn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Connecting…'; }
    _toast('Connecting to ' + dev.name + '…', 'info');
    _api('/api/cast/connect', {device: dev})
      .then(d => {
        if (d.error) {
          _toast('Connect failed: ' + d.error, 'err');
          if (btn) { btn.disabled = false; btn.textContent = '✔ Connect'; }
          return;
        }
        _connected = true;
        _device    = dev;
        _fab.classList.add('connected');
        _showFabLabel('📡 ' + dev.name);
        _renderConnected();
        _toast('Connected to ' + dev.display_name, 'ok');
      })
      .catch(e => {
        _toast('Error: ' + e, 'err');
        if (btn) { btn.disabled = false; btn.textContent = '✔ Connect'; }
      });
  }

  /* ── Cast a pre-resolved URL (called from doPlay hook or cast buttons) ─ */
  let _castInProgress = false;  // prevents re-entrant casts (e.g. from 429 retry loops)
  let _castUrl        = '';     // last cast URL — used by Play button to re-cast
  window.castPlayDirect = function(url, title) {
    if (!_connected) {
      _toast('No cast device connected — open 📺 to connect', 'wrn');
      return;
    }
    // Block re-entrant calls — the browser's error handler may re-resolve and
    // re-call castPlayDirect while a cast is already in flight, causing 429s.
    if (_castInProgress) {
      console.log('[CAST] castPlayDirect blocked — cast already in progress');
      return;
    }
    // Reject relative / local-proxy URLs before they reach the server.
    // These come from the app's own HLS proxy (e.g. /api/hls_proxy?...) and
    // are not reachable by cast devices on the network.
    if (!url || !url.match(/^https?:\/\//i)) {
      _toast('Cannot cast: URL is a local proxy path. Play the channel first, then use the ▶ button.', 'wrn');
      console.warn('[CAST] castPlayDirect blocked — non-absolute URL:', url);
      return;
    }
    _castInProgress = true;
    _castTitle = title || 'Stream';
    _castUrl   = url   || '';
    _toast('Casting: ' + _castTitle, 'info');

    // Stop the browser's mpegts/hls player NOW — it's using the same IPTV connection.
    // FFmpeg will take over that connection. Browser will switch to FFmpeg's HLS output.
    //
    // _playerStopped is `let`-scoped in the main app — inaccessible from this IIFE.
    // Set all window.* guard flags the error handlers check so they can't restart
    // the player (MSE transcode, HLS remux, play_token retry, general remux).
    try {
      window._mseTranscodeFired = true;   // blocks MSE → /api/hls_proxy?transcode=1
      window._hlsRemuxFired     = true;   // blocks HLS → /api/hls_proxy remux
      window._remuxFired        = true;   // blocks general remux restart
      if (window._mpegRetries)  window._mpegRetries  = {};
      if (window._hlsRetries)   window._hlsRetries   = {};
      if (window._ptRetries)    window._ptRetries    = {};
      if (typeof hlsObj    !== 'undefined' && hlsObj)    { hlsObj.destroy();    hlsObj    = null; }
      if (typeof mpegtsObj !== 'undefined' && mpegtsObj) { mpegtsObj.destroy(); mpegtsObj = null; }
      const _vid = document.querySelector('video');
      if (_vid) { _vid.pause(); _vid.removeAttribute('src'); _vid.load(); }
    } catch(_e) {}

    _api('/api/cast/play_direct', {url, title: _castTitle})
      .then(d => {
        _castInProgress = false;
        if (d.error) {
          _toast('Cast error: ' + d.error, 'err');
          return;
        }
        const el = document.getElementById('cast-now-title');
        if (el) el.textContent = '▶ ' + _esc(_castTitle);
        _showFabLabel('▶ ' + _castTitle);
        // proxy_url is null for DLNA direct TS — browser must NOT connect to the same
        // endpoint as the phone (two clients = two IPTV connections = server kills one).
        // proxy_url is set for HLS — FFmpeg serves both phone and browser from one session.
        if (!d.proxy_url) {
          const _np = document.getElementById('np');
          if (_np) _np.textContent = '📺 Casting: ' + _esc(_castTitle);
          return;
        }
        // Switch the browser to the proxy output.
        // For HLS (transcode) URLs: poll until FFmpeg generates the manifest.
        // For HLS (transcode) URLs: poll until FFmpeg generates the manifest.
        if (d.proxy_url) {
          console.log('[CAST] switching browser to HLS proxy:', d.proxy_url);
          const _proxyUrl = d.proxy_url;
          let _attempts = 0;
          const _pollManifest = () => {
            fetch(_proxyUrl).then(r => {
              if (r.ok) {
                try {
                  const _vid = document.querySelector('video');
                  if (_vid && typeof Hls !== 'undefined' && Hls.isSupported()) {
                    if (typeof hlsObj !== 'undefined' && hlsObj) { hlsObj.destroy(); hlsObj = null; }
                    hlsObj = new Hls({enableWorker: false});
                    hlsObj.loadSource(_proxyUrl);
                    hlsObj.attachMedia(_vid);
                    hlsObj.on(Hls.Events.MANIFEST_PARSED, () => {
                      _vid.play().catch(() => {});
                    });
                    console.log('[CAST] browser attached to HLS proxy after', _attempts, 'polls');
                  } else if (_vid) {
                    _vid.src = _proxyUrl; _vid.play().catch(() => {});
                  }
                } catch(_e2) { console.warn('[CAST] attach error', _e2); }
              } else if (_attempts++ < 15) {
                setTimeout(_pollManifest, 1000);
              } else {
                console.warn('[CAST] proxy manifest never became ready');
              }
            }).catch(() => { if (_attempts++ < 15) setTimeout(_pollManifest, 1000); });
          };
          setTimeout(_pollManifest, 2000);
        }
      })
      .catch(e => {
        _castInProgress = false;
        _toast('Cast error: ' + e, 'err');
      });
  };

  /* ── Cast from a portal item object (calls /api/cast/play) ───────────── */
  window.castPlayItem = function(item, mode, category, title) {
    if (!_connected) {
      _toast('No cast device — open 📺 to connect', 'wrn');
      return;
    }
    _castTitle = title || item.name || item.o_name || 'Stream';
    _toast('Casting: ' + _castTitle, 'info');
    _api('/api/cast/play', {item, mode: mode || 'live',
                            category: category || {}, title: _castTitle})
      .then(d => {
        if (d.error) { _toast('Cast error: ' + d.error, 'err'); return; }
        const el = document.getElementById('cast-now-title');
        if (el) el.textContent = '▶ ' + _esc(_castTitle);
        _showFabLabel('▶ ' + _castTitle);
        if (d.proxy_url) {
          try {
            if (typeof hlsObj !== 'undefined' && hlsObj) { hlsObj.destroy(); hlsObj = null; }
            if (typeof mpegtsObj !== 'undefined' && mpegtsObj) { mpegtsObj.destroy(); mpegtsObj = null; }
          } catch(_e) {}
          const _proxyUrl = d.proxy_url;
          let _attempts = 0;
          const _poll = () => {
            fetch(_proxyUrl).then(r => {
              if (r.ok) {
                try {
                  const _vid = document.querySelector('video');
                  if (_vid && typeof Hls !== 'undefined' && Hls.isSupported()) {
                    if (typeof hlsObj !== 'undefined' && hlsObj) { hlsObj.destroy(); hlsObj = null; }
                    hlsObj = new Hls({enableWorker: false});
                    hlsObj.loadSource(_proxyUrl);
                    hlsObj.attachMedia(_vid);
                    hlsObj.on(Hls.Events.MANIFEST_PARSED, () => _vid.play().catch(() => {}));
                  } else if (_vid) { _vid.src = _proxyUrl; _vid.play().catch(() => {}); }
                } catch(_e2) {}
              } else if (_attempts++ < 15) { setTimeout(_poll, 1000); }
            }).catch(() => { if (_attempts++ < 15) setTimeout(_poll, 1000); });
          };
          setTimeout(_poll, 2000);
        }
      })
      .catch(e => _toast('Cast error: ' + e, 'err'));
  };

  /* ── Intercept doPlay ────────────────────────────────────────────────── */
  // When auto-cast is on, intercept channel plays and cast instead of playing locally.
  const _origDoPlay = window.doPlay;
  window.doPlay = function(url, title) {
    // Determine whether this URL is a local proxy URL that cast devices can't reach.
    // Catches:
    //   • relative paths          e.g.  /api/hls_proxy?...  /api/proxy?...
    //   • cast-addon transcode    e.g.  http://192.168.x.x:PORT/transcode/...
    //   • cast-addon stream/audio e.g.  http://192.168.x.x:PORT/stream?...
    //   • any URL on the cast proxy port
    const _isAbsolute = url && url.match(/^https?:\/\//i);
    const _isCastPort = window._castProxyPort && url && url.includes(':' + window._castProxyPort + '/');
    const isProxyUrl  = !_isAbsolute               // relative path → local proxy
                     || _isCastPort                // cast-addon proxy port
                     || (url && url.includes('/transcode/'))   // HLS session path
                     || (url && url.includes('/stream?'))      // audio proxy path
                     || (url && url.includes('/relay?'));       // relay proxy path

    // Only track a URL as castable if it's an absolute castable URL
    if (!isProxyUrl && url) {
      _castUrl   = url;
      _castTitle = title || url;
    }

    if (_autoCast && _connected && url && !isProxyUrl && !_castInProgress) {
      // Do NOT call _origDoPlay when auto-casting.
      // Calling it would open a browser MPEG-TS connection to the IPTV server
      // at the same moment FFmpeg opens its own connection → 2 connections →
      // the server kills one (403 / single-connection token expiry).
      // Instead: reset the app's internal play-guard flags so the next non-cast
      // play works correctly, then cast directly.
      try {
        window._mseTranscodeFired = false;
        window._hlsRemuxFired     = false;
        window._remuxFired        = false;
      } catch(_e) {}
      setTimeout(() => window.castPlayDirect(url, title), 0);
      return;
    }

    // Normal (non-cast) play
    if (typeof _origDoPlay === 'function') {
      _origDoPlay.apply(this, arguments);
    }
  };

  /* ── Inject cast buttons next to external-player buttons ────────────── */
  function _injectCastButtons(root) {
    root = root || document;
    root.querySelectorAll('.won-ext-btn').forEach(extBtn => {
      // Skip if we already added a cast button right after this one
      if (extBtn.nextElementSibling &&
          extBtn.nextElementSibling.classList.contains('cast-ext-btn')) return;

      const castBtn = document.createElement('span');
      castBtn.className  = 'cast-ext-btn';
      castBtn.textContent = '📺 Cast';
      castBtn.title       = 'Cast to connected device';

      // Mirror the click logic of the adjacent external-player button.
      // The won-ext-btn has an onclick that resolves the URL; we piggy-back
      // on the same item by finding the parent won-item and its data-idx.
      castBtn.onclick = async () => {
        if (!_connected) {
          _toast('No cast device — open 📺 to connect', 'wrn'); return;
        }
        // Find the item index from a sibling button (data-name / data-cid)
        const parent  = extBtn.closest('.won-item');
        const findBtn = parent && parent.querySelector('.won-find-btn');
        const name    = findBtn ? findBtn.dataset.name : '';
        // Resolve via /api/resolve_url using the cached match
        // _wonMatches is defined in the Flask app JS
        const matches = window._wonMatches;
        if (!matches) { _toast('Resolve source unavailable', 'wrn'); return; }
        // Find idx from the won-ext-N id
        const extId = extBtn.id || '';
        const m = extId.match(/won-ext-(\d+)/);
        const idx = m ? parseInt(m[1]) : -1;
        const ch = idx >= 0 ? matches[idx] : null;
        if (!ch) { _toast('Channel not yet resolved (press 🔍 first)', 'wrn'); return; }
        const title = ch.name || ch.o_name || name;
        castBtn.textContent = '⏳';
        try {
          const r = await fetch('/api/resolve_url', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({item:ch, mode:'live',
                                  category: window.curCat || {}})
          });
          const d = await r.json();
          if (!d.url) {
            _toast('Could not resolve stream URL', 'err');
            castBtn.textContent = '📺 Cast'; return;
          }
          window.castPlayDirect(d.url, title);
        } catch (e) {
          _toast('Error: ' + e, 'err');
        } finally {
          castBtn.textContent = '📺 Cast';
        }
      };

      extBtn.parentNode.insertBefore(castBtn, extBtn.nextSibling);
    });
  }

  // MutationObserver: watch for dynamically added .won-ext-btn elements
  const _obs = new MutationObserver(mutations => {
    mutations.forEach(m => {
      m.addedNodes.forEach(n => {
        if (n.nodeType !== 1) return;
        _injectCastButtons(n);
      });
    });
  });
  _obs.observe(document.body, {childList: true, subtree: true});

  /* ── FAB label helpers ───────────────────────────────────────────────── */
  let _labelTimer = null;
  function _showFabLabel(text) {
    _fabLabel.textContent = text;
    _fabLabel.classList.add('show');
    clearTimeout(_labelTimer);
    _labelTimer = setTimeout(() => _fabLabel.classList.remove('show'), 3000);
  }

  /* ── FAB / Panel toggle ──────────────────────────────────────────────── */
  _fab.onclick = () => {
    const open = _panel.classList.toggle('open');
    if (open) _refreshPanel();
  };
  _closeBtn.onclick = () => _panel.classList.remove('open');

  // Close panel on outside click
  // Use .closest() so clicks on child elements (SVG, badge span) inside
  // #cast-fab don't immediately re-close the panel they just opened.
  document.addEventListener('click', e => {
    if (!_panel.contains(e.target) && !e.target.closest('#cast-fab'))
      _panel.classList.remove('open');
  });

  /* ── Escape helper ───────────────────────────────────────────────────── */
  function _esc(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  /* ── Restore state on load ───────────────────────────────────────────── */
  fetch('/api/cast/status')
    .then(r => r.json())
    .then(d => {
      if (d.connected && d.device) {
        _connected = true;
        _device    = d.device;
        _fab.classList.add('connected');
      }
    })
    .catch(() => {});

  console.log('[cast_addon] UI loaded ✓');
})();
"""
