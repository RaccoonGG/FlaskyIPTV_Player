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
proxy_addon.py  —  Image proxy, video proxy, HLS proxy for FlaskyIPTV_Player_byGG.py
======================================================================================
Provides:
  /api/proxy          — General-purpose URL proxy for logos, HLS keys, manifests.
                        Server-side image cache (1500 entries), hotlink-block detection,
                        DNS-fail silencing, per-host rate-limit throttling.
  /api/proxy OPTIONS  — CORS preflight handler.
  /api/video_proxy    — Range-aware video proxy for YouTube VOD / seek support.
  /api/hls_proxy      — ffmpeg remux/transcode stream to MPEG-TS for browser playback.
                        Supports: remux (copy), audio-transcode, full H.264+AAC transcode.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRATION  (three small changes to FlaskyIPTV_Player_byGG.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — add import after the subtitles_addon import block:

    try:
        from proxy_addon import register_proxy_routes, rewrite_m3u8
        _PROXY_AVAILABLE = True
    except ImportError:
        _PROXY_AVAILABLE = False
        def register_proxy_routes(*a, **kw): pass
        def rewrite_m3u8(content, base_url): return content

STEP 2 — register routes (after register_subtitles_routes call):

    register_proxy_routes(flask_app, state)

STEP 3 — replace the direct call to _rewrite_m3u8() in api_resolve() with:

    rewrite_m3u8(content, base_url)

    (The function is re-exported as rewrite_m3u8 to avoid the private _ prefix
     across module boundaries.)

No HTML/JS changes required — all proxy routes are called via existing fetch()
calls in the main template.
"""

import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlparse, quote

import requests as _requests_lib
from flask import request, Response, stream_with_context


# ── ffmpeg detection ───────────────────────────────────────────────────────────
_FFMPEG_WHICH     = shutil.which("ffmpeg")
_FFMPEG_PATH      = _FFMPEG_WHICH or "ffmpeg"
_FFMPEG_AVAILABLE = _FFMPEG_WHICH is not None

# ── Image cache ────────────────────────────────────────────────────────────────
# Server-side in-memory image cache keyed by URL without query string.
# Portals append random ?{number} tokens to logo URLs — stripping them means
# all variants of a URL share one cached entry.
# Cap: _PROXY_IMG_CACHE_MAX entries. When exceeded, oldest half is evicted.
_proxy_img_cache: dict = {}           # norm_url → (content_type, bytes)
_proxy_img_cache_lock = threading.Lock()
_PROXY_IMG_CACHE_MAX = 1500           # ~150 MB at ~100 kB average logo size

# ── Pre-compiled patterns ──────────────────────────────────────────────────────
# /api/proxy is called once per channel logo — potentially hundreds of times
# when a channel list loads — so every allocation avoided here matters.
_RE_IMG_EXT      = re.compile(r'\.(jpe?g|png|gif|webp|svg|ico|bmp)$', re.I)
_RE_DOUBLE_SLASH = re.compile(r'/{2,}')
_RE_M3U8_KEY_URI = re.compile(r'URI="([^"]*)"')
_RE_M3U8_URL     = re.compile(r'\.(m3u8?|m3u)(\?|$)', re.I)

# CORS headers — allocated once, copied per response
_CORS_HEADERS: dict = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}

# 1×1 transparent PNG returned instead of a broken-image icon when a logo
# host blocks hotlinking or fails DNS.
_TRANSPARENT_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
    b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
)

# ── Hotlink-block detection ────────────────────────────────────────────────────
# A host is marked as blocked after _HOTLINK_403_THRESHOLD consecutive 403s.
_HOTLINK_BLOCKED_HOSTS: set = set()
_HOTLINK_BLOCKED_HOSTS_LOCK = threading.Lock()
_HOTLINK_403_COUNTS: dict = {}
_HOTLINK_403_THRESHOLD = 10

# ── 404-silence detection ──────────────────────────────────────────────────────
# Logo hosts that consistently return 404 (wrong path prefix) are silenced after
# _HOST_404_THRESHOLD hits — transparent PNG returned without logging.
_HOST_404_COUNTS: dict = {}
_HOST_404_BLOCKED: set = set()
_HOST_404_LOCK = threading.Lock()
_HOST_404_THRESHOLD = 5


def _record_host_404(host: str) -> bool:
    """Increment 404 counter for host; silence after threshold. Returns True on first cross."""
    with _HOST_404_LOCK:
        _HOST_404_COUNTS[host] = _HOST_404_COUNTS.get(host, 0) + 1
        if _HOST_404_COUNTS[host] >= _HOST_404_THRESHOLD:
            if host not in _HOST_404_BLOCKED:
                _HOST_404_BLOCKED.add(host)
                return True
    return False


def _record_host_403(host: str):
    """Increment 403 counter for host; mark blocked once threshold is reached."""
    with _HOTLINK_BLOCKED_HOSTS_LOCK:
        _HOTLINK_403_COUNTS[host] = _HOTLINK_403_COUNTS.get(host, 0) + 1
        if _HOTLINK_403_COUNTS[host] >= _HOTLINK_403_THRESHOLD:
            if host not in _HOTLINK_BLOCKED_HOSTS:
                _HOTLINK_BLOCKED_HOSTS.add(host)
                return True  # just crossed threshold — caller should log
    return False


# ── DNS-fail silencing ─────────────────────────────────────────────────────────
_DNS_FAIL_BLOCKED_HOSTS: set = set()
_DNS_FAIL_BLOCKED_HOSTS_LOCK = threading.Lock()
_DNS_FAIL_COUNTS: dict = {}
_DNS_FAIL_THRESHOLD = 3   # silence after 3 failures (DNS failures are persistent)


def _record_host_dns_fail(host: str) -> bool:
    """Increment DNS-failure counter; silence host once threshold is reached.
    Returns True the first time the threshold is crossed (caller should log once)."""
    with _DNS_FAIL_BLOCKED_HOSTS_LOCK:
        _DNS_FAIL_COUNTS[host] = _DNS_FAIL_COUNTS.get(host, 0) + 1
        if _DNS_FAIL_COUNTS[host] >= _DNS_FAIL_THRESHOLD:
            if host not in _DNS_FAIL_BLOCKED_HOSTS:
                _DNS_FAIL_BLOCKED_HOSTS.add(host)
                return True
    return False


def _is_dns_fail(exc: Exception) -> bool:
    """Return True if the exception looks like a DNS resolution failure."""
    msg = str(exc).lower()
    return any(k in msg for k in (
        "nameresolutionerror", "getaddrinfo failed", "name or service not known",
        "nodename nor servname", "failed to resolve", "errno 11001", "errno 11002",
    ))


# ── Connect-timeout silencing ─────────────────────────────────────────────────
# Hosts that repeatedly time-out on connection — after _TIMEOUT_THRESHOLD
# consecutive ConnectTimeout errors the host is silenced and we return a
# transparent PNG without logging, same strategy as DNS-fail blocking.
_TIMEOUT_BLOCKED_HOSTS: set = set()
_TIMEOUT_BLOCKED_HOSTS_LOCK = threading.Lock()
_TIMEOUT_COUNTS: dict = {}   # host → timeout count
_TIMEOUT_THRESHOLD = 3       # silence after 3 timeouts (likely unreachable host)


def _record_host_timeout(host: str) -> bool:
    """Increment connect-timeout counter; silence host once threshold is reached.
    Returns True the first time the threshold is crossed (caller should log once)."""
    with _TIMEOUT_BLOCKED_HOSTS_LOCK:
        _TIMEOUT_COUNTS[host] = _TIMEOUT_COUNTS.get(host, 0) + 1
        if _TIMEOUT_COUNTS[host] >= _TIMEOUT_THRESHOLD:
            if host not in _TIMEOUT_BLOCKED_HOSTS:
                _TIMEOUT_BLOCKED_HOSTS.add(host)
                return True  # just crossed threshold
    return False


def _is_connect_timeout(exc: Exception) -> bool:
    """Return True if the exception is a connection-timeout (not a read timeout)."""
    t = type(exc).__name__
    if t in ("ConnectTimeout", "ConnectionError"):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in (
        "connecttimeout", "connect timeout", "connection timed out",
        "timed out", "etimedout",
    ))

# ── Per-host rate-limit throttling ────────────────────────────────────────────
_RATE_LIMITED_HOSTS: set = set()
_RATE_LIMITED_HOSTS_LOCK = threading.Lock()
_RATE_LIMIT_SEMAPHORES: dict = {}
_RATE_LIMIT_SEMAPHORES_LOCK = threading.Lock()
_RATE_LIMIT_CONCURRENCY = 2   # max simultaneous requests to any rate-limited host


def _get_host_semaphore(host: str):
    with _RATE_LIMIT_SEMAPHORES_LOCK:
        if host not in _RATE_LIMIT_SEMAPHORES:
            _RATE_LIMIT_SEMAPHORES[host] = threading.Semaphore(_RATE_LIMIT_CONCURRENCY)
        return _RATE_LIMIT_SEMAPHORES[host]


def _mark_host_rate_limited(host: str):
    with _RATE_LIMITED_HOSTS_LOCK:
        _RATE_LIMITED_HOSTS.add(host)
    _get_host_semaphore(host)


# ── M3U8 manifest rewriter ────────────────────────────────────────────────────

def rewrite_m3u8(content: str, base_url: str) -> str:
    """Rewrite all URLs in an m3u8 manifest to route through /api/proxy."""
    from urllib.parse import urljoin
    lines = content.splitlines()
    result = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith('#'):
            if s.startswith(('http://', 'https://')):
                abs_url = s
            else:
                abs_url = urljoin(base_url, s)
            result.append('/api/proxy?url=' + quote(abs_url, safe=''))
        elif '#EXT-X-KEY' in s and 'URI="' in s:
            def _repl(m):
                uri = m.group(1)
                if not uri.startswith(('http://', 'https://')):
                    uri = urljoin(base_url, uri)
                return 'URI="/api/proxy?url=' + quote(uri, safe='') + '"'
            result.append(_RE_M3U8_KEY_URI.sub(_repl, line))
        else:
            result.append(line)
    return '\n'.join(result)


# Keep the private name as an alias so any internal references still work
_rewrite_m3u8 = rewrite_m3u8


# ===================== REGISTRATION =====================

def register_proxy_routes(flask_app, state):
    """Register all proxy-related Flask routes."""

    # ── /api/proxy ────────────────────────────────────────────────────────────
    @flask_app.route("/api/proxy")
    def api_proxy():
        url = request.args.get("url", "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return Response("Invalid URL", status=400)

        # Normalise double-slashes in the path that some portals embed in logo URLs
        try:
            _p = urlparse(url)
            _clean_path = _RE_DOUBLE_SLASH.sub('/', _p.path)
            if _clean_path != _p.path:
                url = _p._replace(path=_clean_path).geturl()
        except Exception:
            pass

        # Cache key = URL with query string stripped.
        norm_url   = url.split("?")[0]
        is_img_url = bool(_RE_IMG_EXT.search(norm_url))

        cors = _CORS_HEADERS

        # ── Known hotlink-blocked / DNS-failed hosts: return transparent PNG ─
        try:
            _host = urlparse(url).netloc.lower()
        except Exception:
            _host = ""
        with _HOTLINK_BLOCKED_HOSTS_LOCK:
            _is_blocked = _host in _HOTLINK_BLOCKED_HOSTS
        with _DNS_FAIL_BLOCKED_HOSTS_LOCK:
            _is_blocked = _is_blocked or (_host in _DNS_FAIL_BLOCKED_HOSTS)
        with _TIMEOUT_BLOCKED_HOSTS_LOCK:
            _is_blocked = _is_blocked or (_host in _TIMEOUT_BLOCKED_HOSTS)
        with _HOST_404_LOCK:
            _is_blocked = _is_blocked or (_host in _HOST_404_BLOCKED)
        if _is_blocked:
            hdrs = dict(cors)
            hdrs["Content-Type"]  = "image/png"
            hdrs["Cache-Control"] = "public, max-age=86400"
            return Response(_TRANSPARENT_PNG, status=200, headers=hdrs)

        # ── Cache read ────────────────────────────────────────────────────────
        if is_img_url and "Range" not in request.headers:
            with _proxy_img_cache_lock:
                hit = _proxy_img_cache.get(norm_url)
            if hit:
                ct, data = hit
                hdrs = dict(cors)
                hdrs["Content-Type"]   = ct
                hdrs["Content-Length"] = str(len(data))
                hdrs["Cache-Control"]  = "public, max-age=86400"
                hdrs["X-Cache"]        = "HIT"
                return Response(data, status=200, headers=hdrs)

        try:
            if is_img_url:
                parsed_logo = urlparse(url)
                logo_origin = f"{parsed_logo.scheme}://{parsed_logo.netloc}"
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept":          "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer":         logo_origin + "/",
                    "Connection":      "keep-alive",
                }
            else:
                # Build a Referer from the stream URL itself — many CDN/streaming
                # servers (e.g. lx20.net) return 403 when no Referer is present.
                try:
                    _sp = urlparse(url)
                    _ref_path = _sp.path.rsplit("/", 1)[0] + "/"
                    _stream_referer = f"{_sp.scheme}://{_sp.netloc}{_ref_path}"
                except Exception:
                    _stream_referer = url
                headers = {
                    "User-Agent": state.stream_ua,
                    "Accept":     "*/*",
                    "Referer":    _stream_referer,
                    "Connection": "keep-alive",
                }
            if "Range" in request.headers:
                headers["Range"] = request.headers["Range"]

            with _RATE_LIMITED_HOSTS_LOCK:
                _is_rate_limited_host = _host in _RATE_LIMITED_HOSTS
            _sem = _get_host_semaphore(_host) if _is_rate_limited_host else None
            if _sem:
                _sem.acquire()

            _req_timeout = 6 if is_img_url else 20
            try:
                resp    = None
                _backoff = 1.0
                for _attempt in range(3):
                    resp = _requests_lib.get(url, headers=headers, stream=True,
                                             timeout=_req_timeout,
                                             allow_redirects=True, verify=False,
                                             proxies={"http": None, "https": None})
                    if resp.status_code != 429:
                        break
                    _mark_host_rate_limited(_host)
                    resp.close()
                    if _attempt < 2:
                        time.sleep(_backoff)
                        _backoff *= 2
            finally:
                if _sem:
                    _sem.release()

            ct       = resp.headers.get("Content-Type", "application/octet-stream")
            is_img_ct = ct.split(";")[0].strip().startswith("image/")
            is_img    = is_img_url or is_img_ct

            is_m3u8 = (_RE_M3U8_URL.search(url.split('?')[0]) or
                       'mpegurl' in ct.lower() or 'x-mpegurl' in ct.lower())
            if is_m3u8:
                text      = resp.text
                rewritten = rewrite_m3u8(text, resp.url)
                return Response(rewritten, content_type="application/vnd.apple.mpegurl", headers=cors)

            # ── Image: read fully, cache, return ─────────────────────────────
            if is_img and "Range" not in request.headers:
                data = resp.content
                if resp.status_code == 200 and data:
                    with _proxy_img_cache_lock:
                        if norm_url not in _proxy_img_cache:
                            if len(_proxy_img_cache) >= _PROXY_IMG_CACHE_MAX:
                                keys = list(_proxy_img_cache.keys())
                                for k in keys[:len(keys) // 2]:
                                    del _proxy_img_cache[k]
                            _proxy_img_cache[norm_url] = (ct, data)
                    hdrs = dict(cors)
                    hdrs["Content-Type"]   = ct
                    hdrs["Content-Length"] = str(len(data))
                    hdrs["Cache-Control"]  = "public, max-age=86400"
                    hdrs["X-Cache"]        = "MISS"
                    return Response(data, status=200, headers=hdrs)
                elif resp.status_code == 403:
                    if _record_host_403(_host):
                        state.log(f"[Proxy] {_HOTLINK_403_THRESHOLD}x 403 from {_host} — future image requests will skip fetch")
                    hdrs = dict(cors)
                    hdrs["Content-Type"]  = "image/png"
                    hdrs["Cache-Control"] = "public, max-age=3600"
                    return Response(_TRANSPARENT_PNG, status=200, headers=hdrs)
                elif resp.status_code == 429:
                    hdrs = dict(cors)
                    hdrs["Content-Type"]  = "image/png"
                    hdrs["Cache-Control"] = "public, max-age=3600"
                    return Response(_TRANSPARENT_PNG, status=200, headers=hdrs)
                else:
                    if is_img_url and resp.status_code == 404:
                        crossed = _record_host_404(_host)
                        if crossed:
                            state.log(f"[Proxy] 404 x{_HOST_404_THRESHOLD} for {_host} — silencing future logo 404s")
                        elif _HOST_404_COUNTS.get(_host, 0) <= _HOST_404_THRESHOLD:
                            state.log(f"[Proxy] HTTP {resp.status_code} ← {url[:120]}")
                        # return transparent PNG silently once blocked
                        hdrs = dict(cors)
                        hdrs["Content-Type"]  = "image/png"
                        hdrs["Cache-Control"] = "public, max-age=3600"
                        return Response(_TRANSPARENT_PNG, status=200, headers=hdrs)
                    elif (_DNS_FAIL_COUNTS.get(_host, 0) < 2 and
                            _HOTLINK_403_COUNTS.get(_host, 0) < 2):
                        state.log(f"[Proxy] HTTP {resp.status_code} ← {url[:120]}")
                    hdrs = dict(cors)
                    hdrs["Content-Type"]  = "image/png"
                    hdrs["Cache-Control"] = "public, max-age=3600"
                    return Response(_TRANSPARENT_PNG, status=200, headers=hdrs)

            # ── Non-image: stream ─────────────────────────────────────────────
            def _gen():
                try:
                    for chunk in resp.iter_content(chunk_size=16384):
                        yield chunk
                except Exception:
                    return

            h = dict(cors)
            h["Content-Type"] = ct
            if "Content-Length" in resp.headers:
                h["Content-Length"] = resp.headers["Content-Length"]
            if "Content-Range" in resp.headers:
                h["Content-Range"] = resp.headers["Content-Range"]
            if resp.status_code not in (200, 206):
                state.log(f"[Proxy] HTTP {resp.status_code} ← {url[:120]}")
            return Response(stream_with_context(_gen()), status=resp.status_code, headers=h)

        except Exception as e:
            if _is_dns_fail(e):
                crossed = _record_host_dns_fail(_host)
                if crossed:
                    state.log(f"[Proxy] DNS failure {_DNS_FAIL_THRESHOLD}x for {_host} — silencing future logo requests")
                elif _DNS_FAIL_COUNTS.get(_host, 0) < _DNS_FAIL_THRESHOLD:
                    state.log(f"[Proxy] Error: {e} ← {url[:120]}")
            elif _is_connect_timeout(e):
                crossed = _record_host_timeout(_host)
                if crossed:
                    state.log(f"[Proxy] ConnectTimeout {_TIMEOUT_THRESHOLD}x for {_host} — silencing future logo requests")
                elif _TIMEOUT_COUNTS.get(_host, 0) < _TIMEOUT_THRESHOLD:
                    state.log(f"[Proxy] Logo fetch error ({type(e).__name__}) ← {url[:120]}")
            elif is_img_url:
                if _DNS_FAIL_COUNTS.get(_host, 0) < 1 and _HOTLINK_403_COUNTS.get(_host, 0) < 1:
                    state.log(f"[Proxy] Logo fetch error ({type(e).__name__}) ← {url[:120]}")
            else:
                state.log(f"[Proxy] Error: {e} ← {url[:120]}")
            if is_img_url:
                hdrs = dict(cors)
                hdrs["Content-Type"]  = "image/png"
                hdrs["Cache-Control"] = "public, max-age=60"
                return Response(_TRANSPARENT_PNG, status=200, headers=hdrs)
            return Response(f"Proxy error: {e}", status=502)

    # ── /api/proxy OPTIONS ────────────────────────────────────────────────────
    @flask_app.route("/api/proxy", methods=["OPTIONS"])
    def api_proxy_options():
        return Response("", headers={
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        })

    # ── /api/video_proxy ──────────────────────────────────────────────────────
    @flask_app.route("/api/video_proxy")
    def api_video_proxy():
        """Proxy a video URL with Range request support for seeking.
        Used for YouTube VOD and other direct video URLs that need seek support.
        Forwards Range headers so the browser can seek by requesting byte ranges.
        """
        url = request.args.get("url", "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return Response("Invalid URL", status=400)

        range_header = request.headers.get("Range")
        req_headers  = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer":    "https://www.youtube.com/",
        }
        if range_header:
            req_headers["Range"] = range_header

        try:
            import requests as _req
            resp   = _req.get(url, headers=req_headers, stream=True, timeout=30)
            status = resp.status_code

            forward_headers = {
                "Access-Control-Allow-Origin": "*",
                "Accept-Ranges":               "bytes",
            }
            for h in ("Content-Type", "Content-Length", "Content-Range"):
                if h in resp.headers:
                    forward_headers[h] = resp.headers[h]

            def _stream():
                for chunk in resp.iter_content(65536):
                    if chunk:
                        yield chunk

            return Response(_stream(), status=status, headers=forward_headers)
        except Exception as e:
            return Response(f"Video proxy error: {e}", status=502)

    # ── /api/hls_proxy ────────────────────────────────────────────────────────
    @flask_app.route("/api/hls_proxy")
    def api_hls_proxy():
        """Transcode/remux stream for browser compatibility."""
        url = request.args.get("url", "").strip()
        if not url or not url.startswith(("http://", "https://", "rtsp://")):
            return Response("Invalid URL", status=400)
        ffmpeg = _FFMPEG_WHICH
        if not ffmpeg:
            return Response("ffmpeg not available", status=503)

        transcode   = request.args.get("transcode",   "0") == "1"
        audio_only  = request.args.get("audio_only",  "0") == "1" and not transcode
        is_vod      = request.args.get("vod",         "0") == "1"
        # audio_track=N selects a specific audio stream by zero-based index.
        # When absent or invalid, ffmpeg falls back to its default (first/best stream).
        _at = request.args.get("audio_track", "").strip()
        audio_track = int(_at) if _at.isdigit() else None

        cors = _CORS_HEADERS

        base_input = [
            ffmpeg, "-hide_banner", "-nostdin",
            "-user_agent", state.stream_ua,
            "-referer", url.rsplit('/', 1)[0] + "/",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-thread_queue_size", "512",
            "-fflags", "+genpts+igndts+discardcorrupt",
            "-i", url,
        ]

        # Build -map args when a specific audio track is requested.
        # -map 0:v:0        — first video stream
        # -map 0:a:<N>      — audio stream at index N within the audio streams
        # Without explicit maps ffmpeg auto-selects, which is correct for the
        # default (no track preference) case.
        map_args = ["-map", "0:v:0", "-map", f"0:a:{audio_track}"] if audio_track is not None else []
        if audio_track is not None:
            state.log(f"[ffmpeg] Audio track: {audio_track} (-map 0:a:{audio_track})")

        if transcode:
            cmd = base_input + map_args + [
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
                "-f", "mpegts", "-",
            ]
            mode_str = "transcode"
        elif audio_only:
            cmd = base_input + map_args + [
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
                "-f", "mpegts", "-",
            ]
            mode_str = "audio-transcode"
        else:
            cmd = base_input + [
                "-c", "copy",
                "-f", "mpegts", "-",
            ]
            mode_str = "remux"

        state.log(f"[ffmpeg/{mode_str}] Command: {' '.join(cmd[:10])}... [url redacted]")

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            state.log(f"[ffmpeg/{mode_str}] Failed to start: {e}")
            return Response(f"ffmpeg start error: {e}", status=502)

        stderr_lines = []
        _FFMPEG_BENIGN_NOISE = (
            "decode_slice_header error",
            "no frame!",
            "concealing",
            "cabac_init_idc",
            "out of range poc",
            "left block unavailable",
            "error while decoding mb",
        )
        _benign_counts: dict = {}
        _proc_killed = [False]

        def _log_stderr():
            try:
                for raw in proc.stderr:
                    if _proc_killed[0]:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        stderr_lines.append(line)
                        low = line.lower()
                        matched_noise = next((p for p in _FFMPEG_BENIGN_NOISE if p in low), None)
                        if matched_noise:
                            _benign_counts[matched_noise] = _benign_counts.get(matched_noise, 0) + 1
                            if _benign_counts[matched_noise] == 1:
                                state.log(f"[ffmpeg/{mode_str}] (suppressing repeated startup noise: '{matched_noise}')")
                            continue
                        if any(k in low for k in ("error", "invalid", "failed", "unable", "fatal", "unknown")):
                            state.log(f"[ffmpeg/{mode_str}] ERR: {line[:120]}")
                        elif "stream #" in low and "video" in low:
                            state.log(f"[ffmpeg/{mode_str}] INFO: {line[:120]}")
                        elif "conversion failed" in low or "cannot" in low:
                            state.log(f"[ffmpeg/{mode_str}] FAIL: {line[:120]}")
            except Exception as e:
                state.log(f"[ffmpeg/{mode_str}] stderr thread error: {e}")

        threading.Thread(target=_log_stderr, daemon=True).start()
        state.log(f"[ffmpeg/{mode_str}] Started PID {proc.pid}: {url[:60]}...")

        def _gen():
            chunk_count   = 0
            killed_by_us  = False
            try:
                while True:
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        if chunk_count == 0:
                            time.sleep(0.5)
                            if stderr_lines:
                                state.log(f"[ffmpeg/{mode_str}] No output. Last error: {stderr_lines[-1][:100]}")
                        break
                    chunk_count += 1
                    yield chunk
            except GeneratorExit:
                killed_by_us = True
            except Exception as e:
                state.log(f"[ffmpeg/{mode_str}] Generator error: {e}")
            finally:
                _proc_killed[0] = True
                proc.kill()
                proc.wait()
                rc = proc.returncode
                if killed_by_us:
                    state.log(f"[ffmpeg/{mode_str}] Client disconnected after {chunk_count} chunks — stream stopped")
                elif rc == 0:
                    state.log(f"[ffmpeg/{mode_str}] Finished cleanly after {chunk_count} chunks")
                else:
                    state.log(f"[ffmpeg/{mode_str}] Exited with error (exit code {rc}) after {chunk_count} chunks"
                              + (f" — last stderr: {stderr_lines[-1][:120]}" if stderr_lines else ""))

        h = dict(cors)
        h["Content-Type"]  = "video/mp2t"
        h["Cache-Control"] = "no-cache, no-store, must-revalidate"
        h["Pragma"]        = "no-cache"
        return Response(stream_with_context(_gen()), status=200, headers=h)
