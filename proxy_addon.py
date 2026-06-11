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

import math
import os
import re
import shutil
import subprocess
import tempfile
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


def _adjust_xtream_url(raw_url: str, offset_secs: int):
    """
    For Xtream Codes timeshift path-format URLs, advance the programme start
    time by offset_secs and return (adjusted_url, remaining_seconds).

    URL: /timeshift/{user}/{pass}/{dur_min}/{YYYY-MM-DD}:{HH}-{MM}/{stream}.ts

    remaining_seconds (0-59) is the within-minute offset still to be handled
    by output-side -ss after the adjusted URL is opened, giving ~second-level
    seek precision without requiring HTTP Range support from the server.

    Falls back to (raw_url, offset_secs) for non-Xtream URLs so the caller
    can pass offset_secs straight through as -ss (slower for large offsets on
    real-time-delivery servers, but always correct).
    """
    import re as _re
    m = _re.match(
        r'^(.*?/timeshift/[^/]+/[^/]+/)(\d+)/(\d{4}-\d{2}-\d{2}):(\d{2})-(\d{2})/(.+\.ts)$',
        raw_url
    )
    if not m:
        return raw_url, offset_secs

    base, dur_min, date_str, hh, mm, stream = (
        m.group(1), int(m.group(2)), m.group(3),
        int(m.group(4)), int(m.group(5)), m.group(6)
    )
    extra_min   = offset_secs // 60
    extra_sec   = offset_secs % 60
    total_min   = hh * 60 + mm + extra_min
    new_hh      = (total_min // 60) % 24
    new_mm      = total_min % 60
    new_dur_min = max(1, dur_min - extra_min)
    new_url = f'{base}{new_dur_min}/{date_str}:{new_hh:02d}-{new_mm:02d}/{stream}'
    return new_url, extra_sec


def _adjust_mac_timeshift_url(raw_url: str, offset_secs: int):
    """
    For MAC portal timeshift.php query-string-format URLs, advance the start=
    parameter by offset_secs and reduce duration= accordingly.

    URL: .../timeshift.php?mac=XX&stream=N&extension=ts&duration=D&start=YYYY-MM-DD:HH-MM&play_token=T

    Same semantics as _adjust_xtream_url: returns (adjusted_url, remaining_ss_secs)
    where remaining_ss_secs (0-59) is the within-minute offset for output-side -ss,
    giving ~second-level seek precision without requiring HTTP Range support.

    play_token is preserved unchanged — Stalker middleware validates tokens against
    MAC+stream identity, not against the specific start time, so they remain valid
    across seek-adjusted requests.  This mirrors how native MAG devices seek: they
    reuse the same play_token while updating start= and duration= for each seek.

    Falls back to (raw_url, offset_secs) if the URL doesn't match the expected
    format, allowing the caller to pass offset_secs as output-side -ss (always
    correct but slow: ffmpeg must read and discard offset_secs of real-time
    MPEG-TS delivery at 1× speed before producing the first output frame).
    """
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    from datetime import datetime, timedelta
    import re as _re

    if 'timeshift.php' not in raw_url:
        return raw_url, offset_secs

    try:
        parsed = urlparse(raw_url)
        qs     = parse_qs(parsed.query, keep_blank_values=True)

        start_vals = qs.get('start',    [''])
        dur_vals   = qs.get('duration', [''])
        if not start_vals[0] or not dur_vals[0]:
            return raw_url, offset_secs

        start_str = start_vals[0]                   # e.g. "2026-05-30:02-10"
        m = _re.match(r'^(\d{4}-\d{2}-\d{2}):(\d{2})-(\d{2})$', start_str)
        if not m:
            return raw_url, offset_secs

        date_str = m.group(1)                       # "2026-05-30"
        hh       = int(m.group(2))                  # 2
        mm       = int(m.group(3))                  # 10
        dur_min  = int(dur_vals[0])                 # 140

        extra_min   = offset_secs // 60             # whole minutes to advance start=
        extra_sec   = offset_secs %  60             # sub-minute remainder for -ss

        total_min   = hh * 60 + mm + extra_min
        new_hh      = (total_min // 60) % 24
        new_mm      = total_min % 60
        new_dur_min = max(1, dur_min - extra_min)

        # Handle date rollover when seek crosses midnight
        if total_min >= 24 * 60:
            days_over = total_min // (24 * 60)
            date_obj  = datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=days_over)
            date_str  = date_obj.strftime('%Y-%m-%d')

        new_start = f'{date_str}:{new_hh:02d}-{new_mm:02d}'

        # Rebuild query string preserving all original params (mac, stream, extension,
        # play_token, sn2, etc.) — only start= and duration= are updated.
        new_qs             = {k: v[0] for k, v in qs.items()}
        new_qs['start']    = new_start
        new_qs['duration'] = str(new_dur_min)
        new_url = urlunparse(parsed._replace(query=urlencode(new_qs)))

        return new_url, extra_sec

    except Exception:
        return raw_url, offset_secs


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
                        state.log(f"[PROXY] ⚠ {_HOTLINK_403_THRESHOLD}x 403 from {_host} — future image requests will skip fetch")
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
                            state.log(f"[PROXY] ⚠ 404 x{_HOST_404_THRESHOLD} for {_host} — silencing future logo 404s")
                        elif _HOST_404_COUNTS.get(_host, 0) <= _HOST_404_THRESHOLD:
                            state.log(f"[PROXY] HTTP {resp.status_code} ← {url[:120]}")
                        # return transparent PNG silently once blocked
                        hdrs = dict(cors)
                        hdrs["Content-Type"]  = "image/png"
                        hdrs["Cache-Control"] = "public, max-age=3600"
                        return Response(_TRANSPARENT_PNG, status=200, headers=hdrs)
                    elif (_DNS_FAIL_COUNTS.get(_host, 0) < 2 and
                            _HOTLINK_403_COUNTS.get(_host, 0) < 2):
                        state.log(f"[PROXY] HTTP {resp.status_code} ← {url[:120]}")
                    hdrs = dict(cors)
                    hdrs["Content-Type"]  = "image/png"
                    hdrs["Cache-Control"] = "public, max-age=3600"
                    return Response(_TRANSPARENT_PNG, status=200, headers=hdrs)

            # ── Non-image: stream ─────────────────────────────────────────────
            def _gen():
                try:
                    for chunk in resp.iter_content(chunk_size=16384):
                        yield chunk
                except GeneratorExit:
                    # Client disconnected — explicitly close the upstream connection
                    # so the requests socket is returned to the pool immediately
                    # rather than waiting for GC.
                    resp.close()
                    return
                except Exception:
                    resp.close()
                    return

            h = dict(cors)
            h["Content-Type"] = ct
            if "Content-Length" in resp.headers:
                h["Content-Length"] = resp.headers["Content-Length"]
            if "Content-Range" in resp.headers:
                h["Content-Range"] = resp.headers["Content-Range"]
            if resp.status_code not in (200, 206):
                state.log(f"[PROXY] HTTP {resp.status_code} ← {url[:120]}")
            return Response(stream_with_context(_gen()), status=resp.status_code, headers=h)

        except Exception as e:
            if _is_dns_fail(e):
                crossed = _record_host_dns_fail(_host)
                if crossed:
                    state.log(f"[PROXY] ⚠ DNS failure {_DNS_FAIL_THRESHOLD}x for {_host} — silencing future logo requests")
                elif _DNS_FAIL_COUNTS.get(_host, 0) < _DNS_FAIL_THRESHOLD:
                    state.log(f"[PROXY] ✗ Error: {e} ← {url[:120]}")
            elif _is_connect_timeout(e):
                crossed = _record_host_timeout(_host)
                if crossed:
                    state.log(f"[PROXY] ⚠ ConnectTimeout {_TIMEOUT_THRESHOLD}x for {_host} — silencing future logo requests")
                elif _TIMEOUT_COUNTS.get(_host, 0) < _TIMEOUT_THRESHOLD:
                    state.log(f"[PROXY] ✗ Logo fetch error ({type(e).__name__}) ← {url[:120]}")
            elif is_img_url:
                if _DNS_FAIL_COUNTS.get(_host, 0) < 1 and _HOTLINK_403_COUNTS.get(_host, 0) < 1:
                    state.log(f"[PROXY] ✗ Logo fetch error ({type(e).__name__}) ← {url[:120]}")
            else:
                state.log(f"[PROXY] ✗ Error: {e} ← {url[:120]}")
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
        # deinterlace=1: apply yadif deinterlace filter before libx264 re-encode.
        # Set by api_resolve() when ffprobe detects a non-progressive field_order
        # (e.g. "tt" = top-first interlaced H264).  Browser MSE rejects interlaced
        # H264; yadif deint=1 only processes frames flagged as interlaced, so
        # progressive content passes through with negligible extra cost.
        # Only meaningful when transcode=1; ignored for audio_only/remux paths.
        deinterlace = request.args.get("deinterlace", "0") == "1" and transcode
        # err_recover=1 + seek_secs=N: catchup recovery path — the watchdog
        # detected a bad TS region (sync_byte≠0x47) that mpegts.js cannot skip.
        # ffmpeg is more resilient: -err_detect ignore_err silently discards
        # corrupt packets and -ss N starts output from approximately the last
        # known good playback position, so playback resumes mid-stream rather
        # than from the beginning.
        err_recover = request.args.get("err_recover",  "0") == "1"
        _ss_raw     = request.args.get("seek_secs", "").strip()
        seek_secs   = int(_ss_raw) if _ss_raw.isdigit() else None
        # audio_track=N selects a specific audio stream by zero-based index.
        # When absent or invalid, ffmpeg falls back to its default (first/best stream).
        _at = request.args.get("audio_track", "").strip()
        audio_track = int(_at) if _at.isdigit() else None

        # sub_track=N burns subtitle stream N into the video via -filter_complex overlay.
        # Supports bitmap codecs (DVB, DVD, PGS) that cannot be extracted as text.
        # Only valid with transcode=1 (libx264 re-encode); api_resolve() ensures this.
        _st = request.args.get("sub_track", "").strip()
        try:
            _st_int   = int(_st) if _st else None
            sub_track = _st_int if (_st_int is not None and _st_int >= 0) else None
        except (ValueError, TypeError):
            sub_track = None

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
            # Silently discard corrupted TS packets (bad sync bytes, incomplete PES,
            # splice-point glitches in catchup recordings) instead of aborting.
            # This is the input-side complement to +discardcorrupt above.
            "-err_detect", "ignore_err",
        ]
        # seek_secs: catchup recovery path — watchdog detected an unrecoverable
        # bad TS region and requests ffmpeg to resume from a known-good position.
        # -ss before -i performs a fast input seek (keyframe-accurate, no decode
        # cost) rather than the slow output seek that would come after -i.
        if seek_secs is not None:
            base_input += ["-ss", str(seek_secs)]
            state.log(f"[ffmpeg/hls_proxy] err_recover seek to {seek_secs}s")
        base_input += ["-i", url]

        # ── Stream selection ──────────────────────────────────────────────────
        # sub_track set  → -filter_complex "[0:v:0][0:s:N]overlay[vout]"
        #                    burns bitmap subtitle frames into the video signal;
        #                    requires libx264 re-encode (incompatible with -c:v copy).
        #                    audio_map explicitly selects audio track or defaults to 0:a:0.
        # audio_track only → -map 0:v:0 -map 0:a:N  (video copy + audio select)
        # neither          → no explicit -map (ffmpeg auto-selects best streams)
        #
        # When deinterlace=1, yadif=mode=0:parity=-1:deint=1 is prepended to the
        # video filter chain.  deint=1 means "only process frames flagged as
        # interlaced" — progressive frames pass through untouched.
        _yadif = "yadif=mode=0:parity=-1:deint=1" if deinterlace else None
        if sub_track is not None:
            audio_map    = f"0:a:{audio_track}" if audio_track is not None else "0:a:0"
            if _yadif:
                # Chain yadif before subtitle overlay in filter_complex
                _fc = f"[0:v:0]{_yadif}[v_di];[v_di][0:s:{sub_track}]overlay[vout]"
            else:
                _fc = f"[0:v:0][0:s:{sub_track}]overlay[vout]"
            stream_select = [
                "-filter_complex", _fc,
                "-map", "[vout]",
                "-map", audio_map,
            ]
            state.log(f"[ffmpeg] Subtitle burn-in: track {sub_track}"
                      + (f"  audio track {audio_track}" if audio_track is not None else "")
                      + ("  deinterlace" if _yadif else ""))
        elif audio_track is not None:
            if _yadif:
                stream_select = ["-vf", _yadif, "-map", "0:v:0", "-map", f"0:a:{audio_track}"]
            else:
                stream_select = ["-map", "0:v:0", "-map", f"0:a:{audio_track}"]
            state.log(f"[ffmpeg] Audio track: {audio_track} (-map 0:a:{audio_track})"
                      + ("  deinterlace" if _yadif else ""))
        else:
            if _yadif:
                stream_select = ["-vf", _yadif]
            else:
                stream_select = []

        if transcode:
            cmd = base_input + stream_select + [
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
                "-f", "mpegts", "-",
            ]
            mode_str = ("transcode"
                        + ("/burnin" if sub_track is not None else "")
                        + ("/deinterlace" if deinterlace else ""))
        elif audio_only:
            # audio_only: copy video, re-encode audio.  sub_track is not applied here —
            # overlay requires -c:v libx264, incompatible with -c:v copy.
            # api_resolve() upgrades audio_only → full transcode when sub_track is set.
            ao_select = ["-map", "0:v:0", "-map", f"0:a:{audio_track}"] if audio_track is not None else []
            cmd = base_input + ao_select + [
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
            state.log(f"[ffmpeg/{mode_str}] ✗ Failed to start: {e}")
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
                state.log(f"[ffmpeg/{mode_str}] ✗ stderr thread error: {e}")

        threading.Thread(target=_log_stderr, daemon=True).start()
        state.log(f"[ffmpeg/{mode_str}] ✓ Started PID {proc.pid}: {url[:60]}...")

        # ── Pre-flight: detect immediate 456/458 geo-block before streaming ──────
        # A geo-blocked portal returns 456/458 to ffmpeg within one HTTP round-trip
        # (typically < 200 ms). A healthy stream keeps running. Poll every 50 ms for
        # up to 400 ms: if ffmpeg exits in that window, inspect stderr for the error
        # code and return it as the HTTP status so the frontend mpegts.js error handler
        # can show the correct "use a VPN" / "max connections" tip.
        # For working streams proc.poll() stays None → we exit the loop early and
        # proceed to the streaming response with negligible added latency.
        _t0 = time.monotonic()
        while time.monotonic() - _t0 < 0.4:
            if proc.poll() is not None:
                break
            time.sleep(0.05)

        if proc.poll() is not None:
            # ffmpeg already exited — let the stderr reader thread catch up
            time.sleep(0.15)
            _early = " ".join(stderr_lines).lower()
            for _sc in (456, 458):
                if f"http error {_sc}" in _early:
                    state.log(
                        f"[ffmpeg/{mode_str}] ✗ Pre-flight: HTTP {_sc} detected"
                        f" — returning {_sc} to client"
                    )
                    return Response(f"HTTP {_sc}", status=_sc)
            # Exited for a different reason — fall through to _gen() which will
            # log the 0-chunk error; no need for a special status here.
        # ─────────────────────────────────────────────────────────────────────────

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
                                state.log(f"[ffmpeg/{mode_str}] ✗ No output. Last error: {stderr_lines[-1][:100]}")
                        break
                    chunk_count += 1
                    yield chunk
            except GeneratorExit:
                killed_by_us = True
            except Exception as e:
                state.log(f"[ffmpeg/{mode_str}] ✗ Generator error: {e}")
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
                    state.log(f"[ffmpeg/{mode_str}] ✗ Exited with error (exit code {rc}) after {chunk_count} chunks"
                              + (f" — last stderr: {stderr_lines[-1][:120]}" if stderr_lines else ""))

        h = dict(cors)
        h["Content-Type"]  = "video/mp2t"
        h["Cache-Control"] = "no-cache, no-store, must-revalidate"
        h["Pragma"]        = "no-cache"
        return Response(stream_with_context(_gen()), status=200, headers=h)

    # ── /api/catchup/stream  +  /api/catchup/seg/<sid>/<seg> ─────────────────
    # HLS-VOD proxy for Xtream timeshift/.ts catchup streams.
    #
    # DESIGN (evolved through extensive real-server testing):
    #
    # 1. SINGLE PROC PER SESSION — one ffmpeg for the whole programme.
    #    Per-segment processes re-pay the probe overhead every 6 seconds;
    #    a single proc pays it once and then segments flow continuously.
    #
    # 2. PROBESIZE 500 KB — cuts startup from up to 63 s (default 5 MB @
    #    637 kbps) to ~1–6 s.  -f mpegts is intentionally NOT used: it caused
    #    immediate ffmpeg exits on Windows builds when applied to HTTP inputs.
    #
    # 3. 3-SEGMENT BUFFER WAIT — the original code's 63-second probe
    #    accidentally gave HLS.js a 10-segment head start before it asked for
    #    any data.  With the probesize fix, that head start is gone, and
    #    segments arrive at exactly the same rate the player consumes them
    #    (1× real-time delivery) → 1-2 s stall at every 6-second boundary.
    #    Waiting for 3 segments before returning the playlist restores the
    #    buffer HLS.js needs to absorb production timing variance.
    #
    # 4. SEEKING via XTREAM URL TIME-MANIPULATION — for path-format URLs,
    #    advance :HH-MM to the target minute (fast, server-side seek) then
    #    apply output-side -ss for the remaining seconds.  -start_number N
    #    makes the HLS muxer write seg_0000N.ts directly, keeping filenames
    #    consistent with the pre-declared playlist.
    #
    # 5. GAP-FILL for in-flight pre-seek prefetch requests — when a seek
    #    restarts the proc at segment N, segments 0…N-1 not yet on disk are
    #    permanently unavailable.  HLS.js often sends the "next sequential"
    #    segment concurrently with the seek target.  503 on the old segment
    #    exhausts fragLoadingMaxRetry → fatal → all future segment requests
    #    stop.  Fix: serve the last segment that WAS written instead of 503;
    #    HLS.js decodes valid MPEG-TS, recognises it as before the seek point,
    #    and continues requesting the new segments normally.

    _catchup_sessions: dict = {}   # sid → {proc, dir, ts, url, origin_seg,
                                   #        proc_start, duration, n_segs,
                                   #        seek_in_progress}
    _catchup_lock = threading.Lock()
    _CATCHUP_SEG_SECS            = 6    # HLS target segment duration (s)
    _CATCHUP_SEEK_THRESHOLD_SECS = 45   # seek instead of wait when est. wait > this
    # Backward-seek detection: gap-fill requests with delta > this threshold
    # (and seek_in_progress=False) are genuine user backward seeks, not HLS.js
    # in-flight prefetch.  After seek settles, HLS.js retries at most 1-2 segs
    # behind current position; anything further is a real backward scrub.
    _CATCHUP_BACKWARD_SEEK_MIN_DELTA = 3
    # Initial start buffer: wait for this many segments before returning
    # the manifest.  Prevents micro-stalling on higher-bitrate streams
    # (2 Mbps → only 3-seg burst from 5 MB probe, not 10 as at 637 kbps).
    # 5 segs = 30 s look-ahead.  Fits inside 90 s fragLoadingTimeOut.
    # 0: serve playlist immediately — no server-side initial wait.
    # The client-side FRAG_BUFFERED pre-roll guard in the player delays
    # vid.play() until _PRE_ROLL_SECS (30 s) is buffered, making the
    # server-side wait redundant.  Serving immediately lets HLS.js begin
    # pre-fetching during the ffmpeg probe phase and grow the seek-bar
    # buffer indicator visually before playback starts.
    #
    # Timing (no temp_file, no init wait):
    #   637 kbps: probe ~63 s → burst of ~10 segs → 60 s pre-rolled → PLAY
    #   2 Mbps:   probe ~20 s → burst of ~3 segs (18 s) → 2 more at
    #             real-time (~12 s) → 30 s pre-rolled → PLAY  (~32 s total)
    _CATCHUP_INIT_BUF_SEGS = 0

    def _catchup_cleanup():
        """Daemon: evict temp dirs and kill ffmpeg for sessions idle > 2 h."""
        while True:
            time.sleep(300)
            _now = time.time()
            with _catchup_lock:
                _dead = [s for s, i in _catchup_sessions.items()
                         if _now - i.get('ts', 0) > 7200]
            for s in _dead:
                with _catchup_lock:
                    info = _catchup_sessions.pop(s, {})
                proc = info.get('proc')
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                d = info.get('dir')
                if d and os.path.isdir(d):
                    try:
                        shutil.rmtree(d, ignore_errors=True)
                    except Exception:
                        pass

    threading.Thread(target=_catchup_cleanup, daemon=True,
                     name='catchup-cleanup').start()

    def _catchup_start_proc(url, tmp_dir, duration_secs, seg_secs, stream_ua,
                            origin_seg=0, ss_secs=0):
        """
        Launch one ffmpeg HLS-muxer process — used for both initial session
        start and seek-restarts.

        Probe strategy (MPEG-TS specific):
          The MPEG-TS demuxer in ffmpeg uses -probesize bytes as its probe
          limit and IGNORES -analyzeduration (format-specific override).
          So probe duration = probesize / stream_bitrate.

          initial start (origin_seg == 0):
            No probesize override → ffmpeg default (~5 MB).
            At 637 kbps: probe ≈ 63 s.  ffmpeg buffers 63 s of content
            internally, then flushes it to ≈10 HLS segments almost
            instantaneously (the "burst").  HLS.js loads all 10 at once
            → 60 s head-start.  Delivery rate == playback rate thereafter
            but with 60 s of cushion, so no stalls.
            This is exactly what the original code did and why it didn't stall.

          seek restart (origin_seg > 0):
            probesize=1.5 MB → probe ≈ 18 s at 637 kbps.
            Keeps seek-to-play latency to 18 + ss_secs (0-59) + 6 ≤ 83 s,
            safely within the 90 s fragLoadingTimeOut budget.
            Post-seek burst gives ~3 segments; combined with the
            post-seek buffer-wait (buf_count=3) that's 18 s of head-start.

        -start_number origin_seg: the HLS muxer writes seg_0000N.ts directly
        so seek-restart output names match the pre-declared VOD playlist.

        ss_secs (output-side -ss after -i): within-minute offset after Xtream
        URL time-adjustment; 0 for exact minute-boundary seeks.
        """
        _referer = url.rsplit('/', 1)[0] + '/'
        seg_pat  = os.path.join(tmp_dir, 'seg_%05d.ts')
        playlist = os.path.join(tmp_dir, f'stream_{origin_seg}.m3u8')
        t_limit  = max(seg_secs, int(duration_secs))

        cmd = [_FFMPEG_PATH, '-y']

        # For seek restarts only: reduce probesize to limit seek latency.
        # For initial start: use ffmpeg default (~5 MB) to get the full
        # natural burst that fills HLS.js's 60-second pre-declared buffer.
        # NOTE: do NOT add -analyzeduration here — the MPEG-TS demuxer
        # ignores it; only probesize matters for TS streams.
        if origin_seg > 0:
            cmd += ['-probesize', '1500000']  # 1.5 MB → ~18 s probe at 637 kbps

        cmd += [
            '-user_agent',          stream_ua,
            '-referer',             _referer,
            '-reconnect',           '1',
            '-reconnect_streamed',  '1',
            '-reconnect_delay_max', '10',
            '-fflags',              '+genpts+igndts+discardcorrupt',
            '-err_detect',          'ignore_err',
            '-t',                   str(t_limit),
            '-i',                   url,
        ]
        if ss_secs > 0:
            cmd += ['-ss', str(ss_secs)]
        # When seeking to a non-zero origin, shift all output PTS by the seek
        # offset so segments carry timestamps that match the pre-declared VOD
        # playlist (seg N expected at N*seg_secs seconds).
        #
        # Without this, -fflags +genpts resets PTS to ~0 on every ffmpeg
        # restart.  The VOD playlist says seg_50 starts at 300s, but the file
        # has PTS≈0.  HLS.js appends the segment, tries video.seek(300), finds
        # nothing in that range of the SourceBuffer, stalls, and its stall-
        # handler fires a new seek forward — producing the 50→100→150 cascade.
        if origin_seg > 0:
            cmd += ['-output_ts_offset', str(origin_seg * seg_secs)]
        cmd += [
            '-c', 'copy',
            '-f', 'hls',
            '-hls_time',              str(seg_secs),
            '-hls_list_size',         '0',
            # independent_segments: each segment starts with a keyframe and can
            # be decoded independently.  temp_file removed — it delayed segment
            # detection until the full rename, killing buffer feedback visuals
            # and adding unnecessary latency before first playback.
            '-hls_flags',             'independent_segments',
            '-hls_segment_type',      'mpegts',
            # Resend PAT/PMT at the start of every segment so each is
            # self-contained.  Prevents SourceBuffer decode stalls when
            # HLS.js appends a segment whose decoder context is missing.
            '-hls_segment_options',   'mpegts_flags=resend_headers',
            '-hls_segment_filename',  seg_pat,
            '-start_number',          str(origin_seg),
            playlist,
        ]
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    def _catchup_do_seek(sid, info, target_seg):
        """
        Kill the current ffmpeg proc and restart from target_seg using Xtream
        URL time-adjustment + output-side -ss for within-minute precision.
        """
        proc = info.get('proc')
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait()
            except Exception:
                pass

        offset_secs = target_seg * _CATCHUP_SEG_SECS
        # Route to the correct URL time-adjuster based on URL format.
        # MAC timeshift.php uses query-string format (start=, duration=);
        # Xtream uses path format (/timeshift/user/pass/dur/date:HH-MM/stream.ts).
        # Both adjusters return (adjusted_url, sub-minute-ss_secs).
        # Without URL-level adjustment the fallback is output-side -ss N, which
        # requires ffmpeg to read and discard N seconds of real-time MPEG-TS
        # delivery before producing output — O(seek_offset) latency, not O(1).
        if 'timeshift.php' in info['url']:
            adj_url, ss_secs = _adjust_mac_timeshift_url(info['url'], offset_secs)
            url_note = 'MAC-start-adjusted' if adj_url != info['url'] else 'original (-ss fallback)'
        else:
            adj_url, ss_secs = _adjust_xtream_url(info['url'], offset_secs)
            url_note = 'Xtream-time-adjusted' if adj_url != info['url'] else 'original (-ss fallback)'
        state.log(f'[CATCHUP] Seek → seg {target_seg} @{offset_secs}s  '
                  f'URL {url_note}  -ss {ss_secs}s')

        new_proc = _catchup_start_proc(
            adj_url, info['dir'], info['duration'], _CATCHUP_SEG_SECS,
            state.stream_ua, origin_seg=target_seg, ss_secs=ss_secs,
        )
        with _catchup_lock:
            if sid in _catchup_sessions:
                _catchup_sessions[sid].update({
                    'proc':             new_proc,
                    'origin_seg':       target_seg,
                    'proc_start':       time.time(),
                    'seek_in_progress': True,   # cleared once origin seg is served
                })

    @flask_app.route('/api/catchup/stream')
    def api_catchup_stream():
        """
        Start an ffmpeg HLS-VOD remux session and return a pre-declared
        #EXT-X-PLAYLIST-TYPE:VOD m3u8 playlist.

        Waits for min(3, n_segs) segments before returning the playlist (up to
        30 s).  Without this wait the probesize reduction eliminates the
        accidental buffer the original code had (the 63-second probe @ 637 kbps
        pre-wrote ~10 segments before HLS.js first asked); without it segments
        arrive at exactly the consumption rate with no margin.
        """
        cors = _CORS_HEADERS

        raw_url = request.args.get('url', '').strip()
        sid     = request.args.get('sid', '').strip()
        try:
            duration = int(request.args.get('duration', 0))
        except (ValueError, TypeError):
            duration = 0

        if not raw_url or not sid or duration <= 0:
            return Response('url, sid, and duration > 0 are required',
                            status=400, headers=cors)
        sid = re.sub(r'[^a-zA-Z0-9_-]', '', sid)[:32]
        if not sid:
            return Response('Invalid sid', status=400, headers=cors)
        if not _FFMPEG_AVAILABLE:
            return Response('ffmpeg not available', status=503, headers=cors)

        tmp_dir = os.path.join(tempfile.gettempdir(), f'catchup_{sid}')

        # n_segs is needed both inside (buffer wait) and outside (playlist).
        n_segs   = math.ceil(duration / _CATCHUP_SEG_SECS)
        last_dur = duration - (_CATCHUP_SEG_SECS * (n_segs - 1))

        with _catchup_lock:
            _already = sid in _catchup_sessions

        if not _already:
            # ── Stale temp-dir cleanup ──────────────────────────────────────
            # Previous sessions can leave catchup_* dirs in the system temp
            # folder when the process is killed or the browser is closed
            # before the 2-hour idle-cleanup thread fires.  Clean them up
            # now so disk space doesn't accumulate across many catchup plays.
            try:
                tmp_root   = tempfile.gettempdir()
                active_dirs = {
                    i.get('dir') for i in _catchup_sessions.values()
                    if i.get('dir')
                }
                active_dirs.add(tmp_dir)          # exclude the one we're about to create
                for entry in os.listdir(tmp_root):
                    if not entry.startswith('catchup_'):
                        continue
                    stale = os.path.join(tmp_root, entry)
                    if stale not in active_dirs and os.path.isdir(stale):
                        shutil.rmtree(stale, ignore_errors=True)
                        state.log(f'[CATCHUP] Cleaned stale temp dir: {entry}')
            except Exception as _ce:
                state.log(f'[CATCHUP] Temp cleanup error (non-fatal): {_ce}')
            # ────────────────────────────────────────────────────────────────

            os.makedirs(tmp_dir, exist_ok=True)
            try:
                proc = _catchup_start_proc(
                    raw_url, tmp_dir, duration, _CATCHUP_SEG_SECS, state.stream_ua
                )
            except Exception as exc:
                state.log(f'[CATCHUP/HLS] ffmpeg launch failed: {exc}')
                return Response(f'ffmpeg launch failed: {exc}', status=500,
                                headers=cors)

            with _catchup_lock:
                _catchup_sessions[sid] = {
                    'proc':       proc,
                    'dir':        tmp_dir,
                    'ts':         time.time(),
                    'url':        raw_url,
                    'origin_seg': 0,
                    'proc_start': time.time(),
                    'duration':   duration,
                    'n_segs':     n_segs,
                }
            state.log(f'[CATCHUP/HLS] Started PID {proc.pid} — '
                      f'{duration}s → {tmp_dir}  ({raw_url[:60]}…)')

            # ── Initial segment buffer wait ─────────────────────────────────
            # ffmpeg uses the default 5 MB probesize for initial starts.
            # At 637 kbps that probe takes ~63 s → burst of ~10 segments.
            # At 2 Mbps it takes only ~20 s → burst of only ~3 segments.
            #
            # Without a wait, HLS.js requests seg_00000.ts immediately.
            # The playlist is a pre-declared VOD with all segments listed;
            # HLS.js will prefetch ahead.  But if only 3 segments exist on
            # disk and delivery is at parity (1 new seg every 6 s ≈ real
            # time), HLS.js exhausts the initial buffer in 18 s and begins
            # micro-stalling at every segment boundary — the original bug.
            #
            # Fix: wait for _CATCHUP_INIT_BUF_SEGS segments before returning
            # the playlist.  This mirrors the post-seek buffer wait exactly.
            # fragLoadingTimeOut (90 s in the catchup VOD config) provides
            # the outer guard: if ffmpeg takes longer than 90 s to write
            # the first segment, HLS.js will error out gracefully.
            #
            # Sizing: 5 segments = 30 s of look-ahead.  Sufficient for 2 Mbps
            # streams (20 s probe → segments 0-2 appear, wait adds 0-4).  For
            # 637 kbps streams the 63 s probe delivers all 5 before the wait
            # loop even starts.  30 s budget fits inside the 90 s HLS.js
            # timeout even when added to the probe time at any bitrate.
            _init_buf_segs = min(_CATCHUP_INIT_BUF_SEGS, max(0, n_segs - 1))
            if _init_buf_segs > 0:
                # Only reached when _CATCHUP_INIT_BUF_SEGS > 0 (currently 0).
                # Kept for future use: raises init segs to pre-load before
                # serving the playlist for environments where the client-side
                # FRAG_BUFFERED pre-roll guard is not available.
                state.log(f'[CATCHUP] Initial buffer: waiting for '
                          f'{_init_buf_segs} seg(s) before serving playlist')
                _init_deadline = time.time() + 90
                while time.time() < _init_deadline:
                    have = sum(
                        1 for i in range(_init_buf_segs)
                        if os.path.exists(
                            os.path.join(tmp_dir, f'seg_{i:05d}.ts')
                        )
                    )
                    if have >= _init_buf_segs:
                        break
                    time.sleep(0.1)
                state.log(f'[CATCHUP] Initial buffer ready — serving playlist')

        # Build the pre-declared VOD playlist from the known duration.
        base_url = f'/api/catchup/seg/{sid}'
        lines = [
            '#EXTM3U',
            '#EXT-X-VERSION:3',
            f'#EXT-X-TARGETDURATION:{_CATCHUP_SEG_SECS + 1}',
            '#EXT-X-PLAYLIST-TYPE:VOD',
            '#EXT-X-MEDIA-SEQUENCE:0',
        ]
        for i in range(n_segs):
            seg_dur = last_dur if i == n_segs - 1 else _CATCHUP_SEG_SECS
            lines.append(f'#EXTINF:{float(seg_dur):.3f},')
            lines.append(f'{base_url}/seg_{i:05d}.ts')
        lines.append('#EXT-X-ENDLIST')

        h = dict(cors)
        h['Content-Type'] = 'application/vnd.apple.mpegurl'
        h['Cache-Control'] = 'no-cache, no-store'
        return Response('\n'.join(lines) + '\n', status=200, headers=h)

    @flask_app.route('/api/catchup/seg/<sid>/<seg>')
    def api_catchup_segment(sid, seg):
        """
        Serve a single HLS segment.  Three code paths:

        NORMAL — segment is within the current proc's production range.
                 Poll up to 60 s (original mechanism, unchanged).

        SEEK   — segment is far ahead (est. wait > _CATCHUP_SEEK_THRESHOLD_SECS).
                 Kill proc, restart from target via URL time-adjustment.

        GAP-FILL — segment is below origin_seg (proc has moved past it via a
                 seek) AND not on disk.  503 would exhaust HLS.js retries →
                 fatal → all subsequent requests (including seg N+1) stop.
                 Instead: serve the last available segment's content.  HLS.js
                 decodes valid MPEG-TS, recognises it as behind the seek point,
                 and continues requesting post-seek segments normally.
        """
        cors = _CORS_HEADERS

        sid = re.sub(r'[^a-zA-Z0-9_-]', '', sid)[:32]
        seg = re.sub(r'[^a-zA-Z0-9_.]', '', seg)
        if not sid or not seg or not seg.endswith('.ts'):
            return Response('Bad request', status=400, headers=cors)

        with _catchup_lock:
            info = _catchup_sessions.get(sid)
        if info is None:
            return Response('Session not found', status=404, headers=cors)

        seg_path = os.path.join(info['dir'], seg)
        # seeking_origin is computed inside the poll block (line ~1142) but
        # consumed outside it (post-seek buffer wait).  Initialise here so
        # the variable is always bound even when the file already exists on
        # disk (normal serving path that skips the poll block entirely).
        seeking_origin = False

        # Parse segment index — needed for gap-fill and seek detection.
        try:
            seg_idx = int(seg[4:-3])
        except (ValueError, IndexError):
            return Response('Bad segment name', status=400, headers=cors)

        if not os.path.exists(seg_path):

            origin_seg = info.get('origin_seg', 0)

            # ── GAP-FILL / BACKWARD-SEEK ────────────────────────────────────
            if seg_idx < origin_seg:
                backward_delta = origin_seg - seg_idx
                # Distinguish a genuine user backward seek from HLS.js in-flight
                # prefetch that arrived just behind the forward-seek target.
                #
                # Rule:
                #   seek_in_progress=True  → always stub: old in-flight requests
                #     from pre-seek prefetch; the new proc hasn't settled yet.
                #   seek_in_progress=False AND delta <= _CATCHUP_BACKWARD_SEEK_MIN_DELTA
                #     → stub: HLS.js retry of a near-miss request (1-2 segs back).
                #   seek_in_progress=False AND delta > threshold
                #     → genuine backward seek: restart proc from target segment.
                #       Fall through to normal poll (do NOT return here).
                #
                # Why gap-fill stubs caused backward-seek failure:
                #   Stub returns seg_00003 content (PTS≈0-6s) for seg_00120
                #   (expected PTS≈720-726s).  HLS.js appends to SourceBuffer at
                #   wrong position; video.currentTime can't reach 720s; player
                #   resets to last valid buffered point (the forward-seek position).
                is_genuine_backward = (
                    not info.get('seek_in_progress', False)
                    and backward_delta > _CATCHUP_BACKWARD_SEEK_MIN_DELTA
                )

                if is_genuine_backward:
                    state.log(f'[CATCHUP] Backward seek: seg {seg_idx} '
                              f'(from origin={origin_seg}, '
                              f'Δ={backward_delta} segs = '
                              f'{backward_delta * _CATCHUP_SEG_SECS}s back)')
                    _catchup_do_seek(sid, info, seg_idx)
                    with _catchup_lock:
                        info = _catchup_sessions.get(sid, info)
                    origin_seg = info.get('origin_seg', 0)
                    # DO NOT return — fall through to the normal poll below.
                    # seeking_origin will be True (seek_in_progress=True and
                    # seg_idx==origin_seg), giving a 120s deadline and the
                    # post-seek buffer wait.

                else:
                    # ── STUB (in-flight prefetch or tiny backward delta) ─────
                    # Find the most-recently written segment to use as a stand-in.
                    stub_path = None
                    stub_idx  = -1
                    for ci in range(min(seg_idx, origin_seg - 1), -1, -1):
                        candidate = os.path.join(info['dir'], f'seg_{ci:05d}.ts')
                        if os.path.exists(candidate):
                            stub_path = candidate
                            stub_idx  = ci
                            break

                    if stub_path is None:
                        # Seek just happened; proc has not written its first segment
                        # yet.  Wait briefly for the new-proc's first output.
                        first = os.path.join(info['dir'], f'seg_{origin_seg:05d}.ts')
                        brief = time.time() + 5
                        while time.time() < brief and not os.path.exists(first):
                            time.sleep(0.25)
                        if os.path.exists(first):
                            stub_path = first
                            stub_idx  = origin_seg

                    if stub_path is not None:
                        state.log(f'[CATCHUP] Gap-fill seg {seg_idx} → '
                                  f'seg {stub_idx} (origin={origin_seg})')
                        with _catchup_lock:
                            if sid in _catchup_sessions:
                                _catchup_sessions[sid]['ts'] = time.time()
                        def _gen_stub(p=stub_path):
                            with open(p, 'rb') as f:
                                while True:
                                    chunk = f.read(65536)
                                    if not chunk:
                                        break
                                    yield chunk
                        h = dict(cors)
                        h['Content-Type']  = 'video/mp2t'
                        h['Cache-Control'] = 'no-store'
                        return Response(stream_with_context(_gen_stub()),
                                        status=200, headers=h)

                    # Fallback: nothing on disk and brief wait timed out.
                    hh = dict(cors); hh['Retry-After'] = '3'
                    return Response('Segment not ready', status=503, headers=hh)

            # ── SEEK DETECTION ─────────────────────────────────────────────
            # Estimate how many segments the current proc has written.
            # 1 s is a conservative probe estimate stable across bitrates.
            proc_start  = info.get('proc_start', time.time())
            elapsed     = max(0.0, time.time() - proc_start - 1.0)
            current_est = origin_seg + int(elapsed / _CATCHUP_SEG_SECS)
            est_wait    = max(0, seg_idx - current_est) * _CATCHUP_SEG_SECS

            if est_wait > _CATCHUP_SEEK_THRESHOLD_SECS and seg_idx > origin_seg:
                if info.get('seek_in_progress', False):
                    # ── CASCADE GUARD ───────────────────────────────────────
                    # A seek is already settling (new proc has not yet written
                    # its first segment).  Triggering another seek now would
                    # kill the proc before it produces anything, creating an
                    # infinite cascade (50→100→150 seen in testing).
                    #
                    # Strategy:
                    #   • Segments within 60 s reach: fall through to the
                    #     normal poll — they'll arrive once the proc settles.
                    #   • Segments beyond 60 s reach: fast-fail 503 so HLS.js
                    #     retries sooner (avoids a 60-second dead wait).
                    #     After the origin seg is served the flag clears and
                    #     the retry will trigger a legitimate fresh seek.
                    reach_in_deadline = origin_seg + int((2 * _CATCHUP_SEG_SECS) / _CATCHUP_SEG_SECS)  # 2 segs ahead
                    if seg_idx > reach_in_deadline:
                        state.log(f'[CATCHUP] Cascade blocked: seg {seg_idx} '
                                  f'(settling after seek to {origin_seg}) — fast-fail')
                        hh = dict(cors); hh['Retry-After'] = '5'
                        return Response('Seek settling', status=503, headers=hh)
                    # else: within reach — fall through to normal poll below
                else:
                    state.log(f'[CATCHUP] Seek: seg {seg_idx} est {est_wait:.0f}s '
                              f'away (threshold {_CATCHUP_SEEK_THRESHOLD_SECS}s)')
                    _catchup_do_seek(sid, info, seg_idx)

            # ── NORMAL POLL (original 60-second mechanism, unchanged) ───────
            with _catchup_lock:
                proc = info.get('proc')

            # Adaptive deadline:
            #   • Normal segments: 60 s is ample for real-time delivery.
            #   • Origin segment after a seek: the proc must first probe the
            #     stream, then discard ss_secs seconds of input (output-side
            #     -ss), then write the first 6-s segment.  At 637 kbps with
            #     ss_secs=59 that totals ≈71 s; at higher bitrates ≈12 s.
            #     Using 120 s avoids the two consecutive 503s seen in testing
            #     (each burning one of the two fragLoadingMaxRetry slots) and
            #     lets the single in-flight request stay alive until the seg
            #     actually appears.
            seeking_origin = (
                info.get('seek_in_progress', False)
                and seg_idx == info.get('origin_seg', -1)
            )
            deadline = time.time() + (120 if seeking_origin else 60)  # 60s = 10×6s segments; within fragLoadingTimeOut
            while not os.path.exists(seg_path):
                with _catchup_lock:
                    proc          = info.get('proc')
                    cur_origin    = info.get('origin_seg', 0)
                # A concurrent seek moved origin_seg past this segment — it
                # will never be written by the new proc.  Fast-fail with 503
                # so HLS.js retries after 1 s.  On retry, the gap-fill check
                # at the top of this block reads the updated origin_seg and
                # serves the correct stub immediately.
                #
                # Do NOT break (which falls through to the 404 below): HLS.js
                # treats 404 as a permanent "resource gone" failure and fires
                # fragLoadError/fatal, burning the fragLoadingMaxRetry budget
                # and triggering a full player restart that breaks seeking.
                if seg_idx < cur_origin:
                    hh = dict(cors); hh['Retry-After'] = '1'
                    return Response('Segment skipped by seek', status=503, headers=hh)
                if proc and proc.poll() is not None:
                    break
                if time.time() >= deadline:
                    hh = dict(cors); hh['Retry-After'] = '3'
                    return Response('Segment not ready', status=503, headers=hh)
                time.sleep(0.015)  # 15 ms — reduces avg boundary stall from ~50 ms to ~7 ms

            if not os.path.exists(seg_path):
                return Response('Segment not found', status=404, headers=cors)

        # ── Post-seek buffer wait ───────────────────────────────────────────
        # After a seek the proc writes segments at real-time speed (1×) and
        # HLS.js consumes at real-time speed → tight pipeline → stalls at
        # every segment boundary (watchdog fires at 883, 888, 901, 906, …).
        #
        # Fix: once the origin segment exists on disk, wait for 2 more
        # segments before serving it.  That gives HLS.js 2 × 6 s = 12 s of
        # look-ahead to absorb delivery jitter without stalling.
        #
        # Timing budget (worst case observed: ss_secs=36, 637 kbps):
        #   probe(6 s) + ss(36 s) + first-seg(6 s) = 48 s  → within 120 s ✓
        #   buffer wait for 2 more segs: +12 s              → total 60 s  ✓
        #   fragLoadingTimeOut = 90 s                        → no timeout  ✓
        if seeking_origin:
            remaining_segs = info.get('n_segs', 0) - seg_idx
            buf_count  = min(1, max(0, remaining_segs - 1))   # 1 seg × 6 s = 6 s post-seek buffer; HLS.js manages the rest
            if buf_count > 0:
                state.log(f'[CATCHUP] Post-seek buffer: waiting for '
                          f'{buf_count} seg(s) after {seg_idx} before serving')
                buf_deadline = time.time() + 10   # 1 seg × 6 s + margin; burst delivers almost instantly
                while time.time() < buf_deadline:
                    have = sum(
                        1 for i in range(1, buf_count + 1)
                        if os.path.exists(
                            os.path.join(info['dir'], f'seg_{seg_idx + i:05d}.ts')
                        )
                    )
                    if have >= buf_count:
                        break
                    time.sleep(0.1)  # 100 ms poll for buffer accumulation
                state.log(f'[CATCHUP] Post-seek buffer ready — serving seg {seg_idx}')

        # ── Serve ──────────────────────────────────────────────────────────
        with _catchup_lock:
            if sid in _catchup_sessions:
                sess = _catchup_sessions[sid]
                sess['ts'] = time.time()
                # Clear the cascade-guard flag once the seek-target segment (or
                # any segment at or beyond origin) is successfully on disk and
                # being served.  After this point, a fresh user-initiated seek
                # will be allowed to trigger a new _catchup_do_seek normally.
                if sess.get('seek_in_progress', False) and seg_idx >= sess.get('origin_seg', 0):
                    sess['seek_in_progress'] = False
                    state.log(f'[CATCHUP] Seek settled — seg {seg_idx} ready, cascade guard cleared')

        def _gen():
            with open(seg_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        h = dict(cors)
        h['Content-Type']  = 'video/mp2t'
        h['Cache-Control'] = 'max-age=3600'
        return Response(stream_with_context(_gen()), status=200, headers=h)
    # -- End catchup HLS-VOD proxy --

    state.log("[PROXY] Routes registered: /api/proxy  /api/video_proxy  /api/hls_proxy"
              "  /api/catchup/stream  /api/catchup/seg")
