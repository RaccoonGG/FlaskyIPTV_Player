#!/usr/bin/env python3
"""
MAC/Xtream/M3U Portal Builder — Flask/Android WebView Edition by GG_Raccoon.
Build on the base of Mac2M3UMKV_LiveVodsSeriesGUIPlayer_byGGv5.pyw CustomTkinter by GG_Raccoon.
Adapted to Flask + HTML5/HLS.js by conversion script.

Tested on Windows 10 with Python 3.14 and Termux on Android 16.
First run install_requirements_FlaskAppPlayerDownloader.py to make sure you have everything you need to run this script.
Run: python app.py,  then open http://localhost:5000 in your WebView/browser.

Updates:
Added support for /stalker_portal/ type of MAC portals.
Added support for EPG.
Added support for Stalker Portal CatchUp (where available and supported by the portal).
Added support for external EPG URL to cover channels where the portal does not provide EPG.
Added tag bar above categories.
Added support for Xtream CatchUp (where supported and available by the Xtream portal).
Added Favourites and saving them across sessions in browser memory.
Added Whats on TV Now button, which checks the external EPG URL (you have to set it) for the current time, and lists the programs and channels that are playing it.
Clicking on the Search Icon will check the currently active portal to see if your portal has a channel that you requested. (experimental, needs testing, and good external EPG)
Fixed different channel URL outputs not playing correctly, fixed HEVC channels not going through ffmpeg.
Also, on network error and parsing HLS errors (although this can happen when the channel is offline too), we attempt to play with ffmpeg.
Added progress bar with real kbs speed for downloading MKV, and items/total items for M3U saving.
Fixed EPG out of memory happening in large EPG lists (although now large external EPG lists can use 2000 MB of ram, like 30k channel lists)
Fixed laggy desktop input in the search field for Whats on Now tab and dekstop version of saved logins tab.
Added button that opens external player of your choice (on desktop select exe, on mobile you can pick VLC, MX, MX PRO, Just Player)
Added option to add subtitles from opensubtitles.com via inscript serach (get free apikey from https://www.opensubtitles.com/en/consumers)
Added option to add local subtitles file for subtitles (.srt/.vtt/.ass/.ssa) via Local File tab in the subtitle modal.
Subtitle delay +/- works the same for local files as for OpenSubtitles.
Desktop view optimizations, bigger player, now activity log is hidden by default, can expand, player controls can be hidden to expand player, theater mode button to fully expand player and hiding all tabs.
Added external play button inside Whats on Now tab after search and matching channel, and added also inside catchup tab.
Fixed a major bug with yt-dlp fallback not respecting the stop button.
Fixed ffmpeg mkv download not working on specific HLS VODs/series, by adding mpeg-ts format as a fallback to mkv.
Added logos to channels/vods/series.
Added sub-menu option on channels/vods/series.
Added an open IMDb page for VODs/Series in the sub-menu of VODs/Series; it just does a search for IMDb title.
Added local M3U file parsing in M3U connect options.
Added EPG layout to items tab.
Added cast to Chromecast, DLNA, Airplay feature.
Added Multi-View feature that also supports external URLs like YouTube/Twitch (does not work on age-gated content)
Fixed external EPG decompression blocking threads, added EPG caching per attempted channel while we wait external EPG download to finish.
Improved button highlights and added a glossy style to all buttons.
Fixed some vods and series with mp4/mkv with hevc video or unsuported audio formats not playing in browser.
Varius UI fixes, adjustments and overall script optimizations.
Added logo caching for MAC portal (PortalClient): live channels use get_all_channels fallback (one request, cached); VOD/series use a zero-cost in-memory dict built from already-fetched items.
Extended StalkerPortalClient logo caching to VOD and series modes (was live-only); same zero-cost in-memory dict strategy.
Fixed Xtream double round-trip: handshake() and account_info() previously both issued GET /player_api.php — account_info() now reads from the cached user_info set by handshake(), saving one network call on every connect.
Added Xtream logo cache: stream_id → logo URL dict populated during fetch_items_page, fills missing logos without extra requests.
"""

import base64
import hashlib
import json
import re
import contextlib
import os
import random
import shutil
import string
import subprocess
import tempfile
import threading
import time
import queue
import math
import xml.etree.ElementTree as ET
import gzip as _gzip
from datetime import datetime, timezone
from urllib.parse import urlparse, quote, quote_plus, unquote, parse_qs
import asyncio
import aiohttp
import requests as _requests_lib

from flask import Flask, request, jsonify, Response, render_template_string, stream_with_context
try:
    from cast_addon import register_cast_routes, get_cast_proxy
    _CAST_AVAILABLE = True
except ImportError:
    _CAST_AVAILABLE = False
    def register_cast_routes(*a, **kw): pass
    def get_cast_proxy(): return None

try:
    from multiview_addon import register_multiview_routes
    _MULTIVIEW_AVAILABLE = True
except ImportError:
    _MULTIVIEW_AVAILABLE = False
    def register_multiview_routes(*a, **kw): pass

# ===================== OPTIONAL DEPS =====================

try:
    import yt_dlp  # type: ignore
    YTDLP_AVAILABLE = True
except Exception:
    YTDLP_AVAILABLE = False


# ===================== MKV / FFMPEG HELPERS =====================

def safe_filename(name: str) -> str:
    valid = "-_.() %s%s" % (string.ascii_letters, string.digits)
    cleaned = "".join(c if c in valid else "_" for c in name).strip()
    if not cleaned:
        cleaned = "stream"
    return cleaned[:200]


_time_re    = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")
_bitrate_re = re.compile(r"bitrate=\s*([\d.]+)kbits/s")
_size_re    = re.compile(r"size=\s*(\d+)kB")


def probe_stream_codecs(url: str, pre_input_args=None, timeout=15):
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format"]
    if pre_input_args:
        cmd = [ffprobe, "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format"] + pre_input_args + ["-i", url]
    else:
        cmd += ["-i", url]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        streams = data.get("streams", [])
        result = {"audio": [], "video": [], "subtitle": [], "duration": None}
        for s in streams:
            typ = s.get("codec_type")
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


def run_ffmpeg_download(url: str, out_path: str, pre_input_args=None, post_input_args=None,
                        on_progress=None, stop_event: threading.Event = None, set_proc=None):
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    if pre_input_args:
        cmd += pre_input_args
    cmd += ["-i", url]
    if post_input_args:
        cmd += post_input_args
    cmd += ["-c", "copy", out_path]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    if set_proc:
        try:
            set_proc(proc)
        except Exception:
            pass

    try:
        while True:
            if stop_event and stop_event.is_set():
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            line = proc.stderr.readline()
            if line == "" and proc.poll() is not None:
                break
            if line:
                if on_progress:
                    try:
                        on_progress(line)
                    except Exception:
                        pass
            else:
                time.sleep(0.01)
    except Exception:
        pass
    proc.wait()
    return proc.returncode


def run_yt_dlp_download(url: str, out_path: str, stop_event: threading.Event = None,
                        on_progress=None):
    if not YTDLP_AVAILABLE:
        return False, "yt-dlp not installed"

    # Work inside a dedicated temp subfolder named after the item so all yt-dlp
    # .part / -FragN.part / .ytdl files are isolated there.  On stop or failure
    # we simply rmtree the whole folder — no pattern matching, no risk of
    # accidentally touching other files in the output directory.
    dirn      = os.path.dirname(out_path) or "."
    item_name = os.path.splitext(os.path.basename(out_path))[0]   # e.g. "A Bug_s Life"
    work_dir  = os.path.join(dirn, f"{item_name}_ytdlp_tmp")

    def _cleanup():
        with contextlib.suppress(Exception):
            for fname in os.listdir(work_dir):
                with contextlib.suppress(Exception):
                    os.remove(os.path.join(work_dir, fname))
            with contextlib.suppress(Exception):
                os.rmdir(work_dir)

    def _progress_hook(d):
        if stop_event and stop_event.is_set():
            raise Exception("stopped")
        if d.get("status") == "downloading" and on_progress:
            try:
                on_progress(d)
            except Exception:
                pass

    try:
        os.makedirs(work_dir, exist_ok=True)
    except Exception as e:
        return False, f"Could not create temp dir: {e}"

    ydl_opts = {
        "outtmpl":      os.path.join(work_dir, "%(title)s.%(ext)s"),
        "quiet":        True,
        "no_warnings":  True,
        "noplaylist":   True,
        "format":       "best",
        "progress_hooks": [_progress_hook],
    }
    try:
        if stop_event and stop_event.is_set():
            _cleanup()
            return False, "stopped"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        if stop_event and stop_event.is_set():
            time.sleep(1.0)
            _cleanup()
            return False, "stopped"
        # Find the downloaded file and move it to the final out_path
        candidates = [f for f in os.listdir(work_dir)
                      if not f.endswith(".part") and not f.endswith(".ytdl")
                      and not ".part-Frag" in f]
        if candidates:
            src = os.path.join(work_dir, candidates[0])
            os.replace(src, out_path)
        _cleanup()
        return True, None
    except Exception as e:
        time.sleep(1.0)  # allow yt-dlp to release file handles before cleanup
        _cleanup()
        if stop_event and stop_event.is_set():
            return False, "stopped"
        return False, str(e)


# ===================== SHARED HELPERS =====================

def normalize_base_url(url: str) -> str:
    url = url.strip()
    p = urlparse(url)
    scheme = p.scheme or "http"
    host = p.hostname or ""
    port = p.port or 80
    return f"{scheme}://{host}:{port}"


_URL_RE = re.compile(r'https?://[^\s\'"\\]+')


def _extract_url_from_text(s: str):
    if not s:
        return None
    s2 = s.replace('\\/', '/')
    m = _URL_RE.search(s2)
    if m:
        return m.group(0)
    return None


async def safe_json(resp: aiohttp.ClientResponse):
    try:
        text = await resp.text()
    except Exception:
        return None
    if not text or not text.strip():
        return None
    t = text.lstrip()
    if not (t.startswith("{") or t.startswith("[")):
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def normalize_js(payload):
    if not isinstance(payload, dict):
        return []
    js = payload.get("js")
    if isinstance(js, list):
        return [x for x in js if isinstance(x, dict)]
    if isinstance(js, dict):
        data = js.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return [js]
    return []


# ===================== XTREAM CREDENTIAL DETECTION =====================

def extract_xtream_from_m3u_url(url: str):
    if not url:
        return None
    try:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if "get.php" in parsed.path or "player_api.php" in parsed.path:
            params = parse_qs(parsed.query)
            username = (params.get("username") or params.get("user") or [""])[0]
            password = (params.get("password") or params.get("pass") or [""])[0]
            if username and password:
                return {"base": base, "username": username, "password": password}
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        skip_prefixes = {"live", "movie", "series", "get.php", "player_api.php", "panel_api.php"}
        if len(parts) >= 2 and parts[0].lower() not in skip_prefixes:
            u, p = parts[0], parts[1]
            if (3 <= len(u) <= 64 and 3 <= len(p) <= 64
                    and "." not in u and "." not in p):
                return {"base": base, "username": u, "password": p}
    except Exception:
        pass
    return None


# ===================== M3U LINE HELPER =====================

def _extinf_line(name: str, logo: str, tvg_type: str, group: str, item: dict = None) -> str:
    """Build a single #EXTINF line with all available EPG/matching attributes.

    Writes tvg-id when the portal provides one so EPG players (TiviMate,
    Kodi, IPTV Smarters…) can match channels to programme data without
    relying on fuzzy name matching.

    tvg-id priority:
      1. epg_channel_id  — Xtream live channels
      2. tvg_id          — M3U items parsed from the source file
      3. xmltv_id        — some Stalker portals
      4. (blank)         — no EPG ID available; players fall back to name matching
    """
    tvg_id = ""
    if item:
        tvg_id = str(
            item.get("epg_channel_id") or
            item.get("tvg_id") or
            item.get("xmltv_id") or
            ""
        ).strip()
    id_attr = f' tvg-id="{tvg_id}"' if tvg_id else ""
    logo_attr = f' tvg-logo="{logo}"' if logo else ""
    return (f'#EXTINF:-1{id_attr} tvg-name="{name}" tvg-type="{tvg_type}"'
            f'{logo_attr} group-title="{group}",{name}\n')


# ===================== MAC PORTAL CLIENT =====================

class PortalClient:
    def __init__(self, base_url: str, mac: str, log_cb):
        self.base = normalize_base_url(base_url)
        self.mac = mac.strip().upper()
        self.log = log_cb
        self._extract_url_from_text = _extract_url_from_text
        self.session = None
        self.token = None
        self.headers = {}
        # Logo caches — keyed by item id → logo URL.
        # _ch_logo_cache: populated once via get_all_channels (live fallback).
        # _vod_logo_cache: built lazily from already-fetched VOD/series items
        #   (no extra round-trip; avoids the 2-request pattern stalker uses for live).
        self._ch_logo_cache: dict | None = None
        self._vod_logo_cache: dict = {}

    async def __aenter__(self):
        _timeout = aiohttp.ClientTimeout(total=15, connect=8)
        self.session = aiohttp.ClientSession(cookies={"mac": self.mac}, timeout=_timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.close()

    async def handshake(self) -> str:
        assert self.session is not None
        url = f"{self.base}/portal.php?action=handshake&type=stb&token=&JsHttpRequest=1-xml"
        self.log(f"[MAC] Handshake → {self.base}")
        async with self.session.get(url) as r:
            self.log(f"[MAC] Handshake HTTP {r.status}")
            payload = await safe_json(r)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Handshake failed: empty/non-JSON response (HTTP {r.status})")
        js = payload.get("js")
        if isinstance(js, list) and js:
            js = js[0]
        if not isinstance(js, dict) or not js.get("token"):
            raise RuntimeError(f"Handshake failed: token missing (HTTP {r.status})")
        self.token = js["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.log(f"[MAC] Token acquired: {self.token[:16]}…")
        return self.token

    async def account_info(self):
        assert self.session is not None
        url = f"{self.base}/portal.php?type=account_info&action=get_main_info&JsHttpRequest=1-xml"
        self.log("[MAC] Fetching account info…")
        async with self.session.get(url, headers=self.headers) as r:
            self.log(f"[MAC] Account info HTTP {r.status}")
            payload = await safe_json(r)
        if not isinstance(payload, dict):
            return ("unknown", "unknown")
        js = payload.get("js")
        if isinstance(js, list) and js:
            js = js[0]
        if not isinstance(js, dict):
            return ("unknown", "unknown")
        mac = str(js.get("mac") or js.get("device_mac") or self.mac or "unknown")
        phone = str(js.get("phone") or js.get("end_date") or js.get("expire_date")
                    or js.get("expiry") or js.get("expired") or "unknown")
        self.log(f"[MAC] Account: MAC={mac}  expiry={phone}")
        return (mac, phone)

    async def _fetch_ch_logo_cache(self) -> dict:
        """Fetch live-channel logos once via get_all_channels and cache them.

        Uses the same pattern as StalkerPortalClient._fetch_ch_logo_cache.
        Called lazily only when a page contains channels with missing logos.
        Subsequent pages reuse the cached dict at zero network cost."""
        if self._ch_logo_cache is not None:
            return self._ch_logo_cache
        self._ch_logo_cache = {}

        def _extract(items: list) -> dict:
            out = {}
            for ch in items:
                if not isinstance(ch, dict):
                    continue
                ch_id = str(ch.get("id") or "").strip()
                logo = str(ch.get("logo") or ch.get("screenshot_uri") or
                           ch.get("tv_logo") or ch.get("pic") or "").strip()
                if ch_id and logo:
                    out[ch_id] = logo
            return out

        try:
            url = (f"{self.base}/portal.php?type=itv&action=get_all_channels"
                   f"&force_ch_link_check=&JsHttpRequest=1-xml")
            self.log("[MAC] Logo cache: fetching get_all_channels…")
            async with self.session.get(url, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                self.log(f"[MAC] Logo cache HTTP {r.status}")
                payload = await safe_json(r)
            self._ch_logo_cache = _extract(normalize_js(payload))
            self.log(f"[MAC] Live logo cache: {len(self._ch_logo_cache)} entries")
        except Exception as e:
            self.log(f"[MAC] Logo cache error: {e}")

        return self._ch_logo_cache

    async def fetch_categories(self, mode: str):
        assert self.session is not None
        if mode == "live":
            url = f"{self.base}/portal.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
        elif mode == "vod":
            url = f"{self.base}/portal.php?type=vod&action=get_categories&JsHttpRequest=1-xml"
        else:
            url = f"{self.base}/portal.php?type=series&action=get_categories&JsHttpRequest=1-xml"
        self.log(f"[MAC] Fetching {mode.upper()} categories…")
        async with self.session.get(url, headers=self.headers) as r:
            self.log(f"[MAC] Categories HTTP {r.status} ({mode.upper()})")
            payload = await safe_json(r)
        cats = normalize_js(payload)
        cats = [c for c in cats if isinstance(c, dict) and str(c.get("id", "")).strip()]
        self.log(f"[MAC] {mode.upper()} categories: {len(cats)} found")
        return cats

    async def fetch_series_episodes(self, series_id: str, category_id: str):
        assert self.session is not None
        url = (
            f"{self.base}/portal.php?type=series&action=get_ordered_list"
            f"&movie_id={quote(series_id)}&season_id=0&episode_id=0&row=0"
            f"&JsHttpRequest=1-xml&category={category_id}"
            f"&sortby=added&fav=0&hd=0&not_ended=0"
            f"&abc=*&genre=*&years=*&search=&p=1"
        )
        self.log(f"[MAC] Fetching episodes series_id={series_id}")
        async with self.session.get(url, headers=self.headers) as r:
            payload = await safe_json(r)
        items = normalize_js(payload)
        self.log(f"[MAC] Series episodes: {len(items)} seasons found")
        return items

    async def fetch_items_page(self, mode: str, cat_id: str, page: int):
        assert self.session is not None
        if mode == "live":
            url = (f"{self.base}/portal.php?type=itv&action=get_ordered_list"
                   f"&genre={cat_id}&JsHttpRequest=1-xml&p={page}&sortby=number")
        elif mode == "vod":
            url = (f"{self.base}/portal.php?type=vod&action=get_ordered_list"
                   f"&category={cat_id}&JsHttpRequest=1-xml&p={page}&sortby=added")
        else:
            url = (f"{self.base}/portal.php?type=series&action=get_ordered_list"
                   f"&category={cat_id}&JsHttpRequest=1-xml&p={page}&sortby=added")
        self.log(f"[MAC] Fetching {mode.upper()} items page={page} cat={cat_id}…")
        async with self.session.get(url, headers=self.headers) as r:
            self.log(f"[MAC] Items HTTP {r.status} ({mode.upper()} cat={cat_id} p={page})")
            payload = await safe_json(r)
        items = normalize_js(payload)
        if mode == "series":
            for it in items:
                if isinstance(it, dict):
                    it["_is_show_item"] = True

        # ── Logo caching ─────────────────────────────────────────────────────
        # LIVE: use the get_all_channels cache (one-time network call) to fill
        #       in any channel whose logo field came back empty.
        if mode == "live":
            if any(not it.get("logo") for it in items if isinstance(it, dict)):
                logo_cache = await self._fetch_ch_logo_cache()
                if logo_cache:
                    for it in items:
                        if isinstance(it, dict) and not it.get("logo"):
                            ch_id = str(it.get("id") or "").strip()
                            if ch_id and ch_id in logo_cache:
                                it["logo"] = logo_cache[ch_id]
        else:
            # VOD / SERIES: no extra network call needed.
            # First populate the running in-memory cache from items that DO have
            # a logo, then use it to fill items that don't.
            for it in items:
                if not isinstance(it, dict):
                    continue
                item_id = str(it.get("id") or "").strip()
                logo = (it.get("logo") or it.get("screenshot_uri") or
                        it.get("pic") or "").strip()
                if item_id and logo:
                    self._vod_logo_cache[item_id] = logo
            for it in items:
                if not isinstance(it, dict):
                    continue
                if not (it.get("logo") or it.get("screenshot_uri") or it.get("pic")):
                    item_id = str(it.get("id") or "").strip()
                    cached = self._vod_logo_cache.get(item_id, "")
                    if cached:
                        it["logo"] = cached

        self.log(f"[MAC] {mode.upper()} cat={cat_id} p={page}: {len(items)} items")
        return items

    async def fetch_vod_play_link(self, cmd: str) -> str:
        if not cmd:
            return ""
        try:
            url = f"{self.base}/portal.php?type=vod&action=create_link&cmd={quote(cmd)}"
            self.log(f"[VOD] create_link → {url[:120]}")
            async with self.session.get(url, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                self.log(f"[VOD] create_link HTTP {r.status}")
                if r.status != 200:
                    return ""
                payload = await safe_json(r)
                if not isinstance(payload, dict):
                    return ""
                js = payload.get("js")
                if isinstance(js, list) and js:
                    js = js[0]
                if not isinstance(js, dict):
                    return ""
                cmd_value = js.get("cmd", "")
                if not cmd_value:
                    return ""
                parts = cmd_value.split()
                if len(parts) >= 2:
                    play_link = parts[1].replace("\\/", "/")
                    if play_link.startswith(("http://", "https://", "rtsp://")):
                        return play_link
                extracted = self._extract_url_from_text(cmd_value)
                if extracted:
                    extracted = extracted.replace("\\/", "/")
                    if extracted.startswith(("http://", "https://", "rtsp://")):
                        return extracted
        except Exception as e:
            self.log(f"[VOD] Error fetching play link: {e}")
        return ""

    async def create_episode_link(self, cmd: str, call_mode: str = "series") -> str:
        """Full resolution with encoded + raw retry, localhost fix, multi-key js parsing.
        Matches original GUI script create_episode_link exactly."""
        if not cmd:
            return ""
        try:
            type_map = {"series": "series", "vod": "vod", "live": "itv"}
            ptype = type_map.get(call_mode, "series")

            async def _try_url_and_extract(r):
                try:
                    payload = await safe_json(r)
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    js = payload.get("js")
                    if isinstance(js, list) and js:
                        js = js[0]
                    if isinstance(js, dict):
                        for key in ("cmd", "link", "url", "play_url", "stream"):
                            val = js.get(key)
                            if isinstance(val, str):
                                if val.startswith(("http://", "https://", "rtsp://")):
                                    val = unquote(val)
                                    if "localhost" in val:
                                        resolved = await self.resolve_localhost_url(val)
                                        if resolved != val:
                                            return resolved
                                    return val
                                extracted = self._extract_url_from_text(val)
                                if extracted:
                                    extracted = unquote(extracted)
                                    if "localhost" in extracted:
                                        resolved = await self.resolve_localhost_url(extracted)
                                        if resolved != extracted:
                                            return resolved
                                    return extracted
                try:
                    text = await r.text()
                except Exception:
                    text = ""
                text_stripped = (text or "").strip()
                if text_stripped.startswith(("http://", "https://", "rtsp://")):
                    text_stripped = unquote(text_stripped)
                    if "localhost" in text_stripped:
                        resolved = await self.resolve_localhost_url(text_stripped)
                        if resolved != text_stripped:
                            return resolved
                    return text_stripped
                if text_stripped.startswith("#EXTM3U") or text_stripped.startswith("#EXTINF"):
                    return str(r.url)
                extracted = self._extract_url_from_text(text_stripped)
                if extracted:
                    extracted = unquote(extracted)
                    if "localhost" in extracted:
                        resolved = await self.resolve_localhost_url(extracted)
                        if resolved != extracted:
                            return resolved
                    return extracted
                return ""

            encoded = quote_plus(cmd)
            url = f"{self.base}/portal.php?type={ptype}&action=create_link&cmd={encoded}&JsHttpRequest=1-xml"
            self.log(f"[MAC] create_link ({ptype}) encoded")
            try:
                async with self.session.get(url, headers=self.headers, allow_redirects=True) as r:
                    self.log(f"[MAC] create_link HTTP {r.status} ({ptype})")
                    candidate = await _try_url_and_extract(r)
                    if candidate:
                        self.log(f"[MAC] create_link resolved → {candidate[:120]}")
                        return candidate
            except Exception as e:
                self.log(f"[MAC] create_link encoded error: {e}")
            # Raw retry — some portals reject quote_plus encoding
            try:
                url2 = f"{self.base}/portal.php?type={ptype}&action=create_link&cmd={cmd}&JsHttpRequest=1-xml"
                self.log(f"[MAC] create_link ({ptype}) raw retry")
                async with self.session.get(url2, headers=self.headers, allow_redirects=True) as r2:
                    self.log(f"[MAC] create_link retry HTTP {r2.status} ({ptype})")
                    candidate2 = await _try_url_and_extract(r2)
                    if candidate2:
                        self.log(f"[MAC] create_link retry resolved → {candidate2[:120]}")
                        return candidate2
            except Exception as e:
                self.log(f"[MAC] create_link raw error: {e}")
            return ""
        except Exception as e:
            self.log(f"[create_link] unexpected error: {e}")
            return ""

    async def create_catchup_link(self, cmd: str, start_str: str, duration_min: int,
                                  archive_cmd: str = "") -> str:
        """Resolve a catchup/timeshift link for a past programme via MAC portal.

        If archive_cmd is supplied (e.g. 'auto /media/537163805.mpg' from
        get_simple_data_table), the request is sent as type=tv_archive — exactly
        what SFVip/TiviMate send and what Stalker portals actually honour.
        Without archive_cmd we fall back to type=itv + start/duration.

        start_str: 'YYYY-MM-DD:HH-MM' (local time)
        duration_min: programme duration in minutes
        """
        assert self.session is not None
        from urllib.parse import quote as _q

        effective_cmd = archive_cmd.strip() if archive_cmd.strip() else cmd

        if archive_cmd.strip():
            # SFVip-style: type=tv_archive with the per-entry archive cmd.
            # Use %20 (not +) for spaces — do NOT pre-quote then urlencode (double-encode).
            params_str = (
                f"type=tv_archive&action=create_link"
                f"&cmd={_q(effective_cmd, safe='')}"
                f"&series=&forced_storage=0&disable_ad=0&download=0"
                f"&force_ch_link_check=0&JsHttpRequest=1-xml"
            )
        else:
            # providers.py resolve_catchup exact params: type=itv, series=1, start, duration
            params_str = (
                f"type=itv&action=create_link"
                f"&cmd={_q(effective_cmd, safe='')}"
                f"&JsHttpRequest=1-xml"
                f"&download=0&save=0&series=1&forced_storage=0"
                f"&start={_q(start_str, safe='-:')}&duration={duration_min}"
            )
        url = f"{self.base}/portal.php?{params_str}"
        self.log(f"[MAC] create_catchup_link start={start_str} dur={duration_min}m")
        try:
            async with self.session.get(url, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                self.log(f"[MAC] catchup_link HTTP {r.status}")
                payload = await safe_json(r)
            if not isinstance(payload, dict):
                return ""
            js = payload.get("js", {})
            if isinstance(js, list) and js:
                js = js[0]
            if not isinstance(js, dict):
                return ""
            cmd_value = js.get("cmd") or js.get("url") or ""
            if not cmd_value:
                return ""
            cmd_value = cmd_value.strip().replace("\\/", "/")
            for prefix in ("ffmpeg ", "auto "):
                if cmd_value.lower().startswith(prefix):
                    cmd_value = cmd_value[len(prefix):].strip()
            # Fix hostless URLs: http://:/... or http:///...
            if re.match(r'https?://[:/]', cmd_value):
                path_part = re.sub(r'^https?://[^/]*', '', cmd_value)
                cmd_value = self.base.rstrip('/') + path_part
                self.log(f"[MAC] Fixed hostless URL → {cmd_value[:120]}")
            if cmd_value.startswith(("http://", "https://", "rtsp://")):
                if "localhost" in cmd_value:
                    return await self.resolve_localhost_url(cmd_value)
                return cmd_value
            extracted = self._extract_url_from_text(cmd_value)
            return extracted or ""
        except Exception as e:
            self.log(f"[MAC] create_catchup_link error: {e}")
            return ""

    def _join_path_and_file(self, path, file):
        if not path or not file:
            return None
        path = str(path).strip()
        file = str(file).strip()
        if not path or not file:
            return None
        return f"{path.rstrip('/')}/{file.lstrip('/')}"

    @staticmethod
    def _clean_cmd(cmd: str) -> str:
        """Strip 'ffmpeg ' / 'auto ' prefixes and backslash-escapes from a cmd value."""
        if not cmd:
            return cmd
        cmd = cmd.replace("\\/", "/").strip()
        if cmd.startswith("ffmpeg "):
            cmd = cmd.split(" ", 1)[1].strip()
        if cmd.lower().startswith("auto "):
            cmd = cmd.split(" ", 1)[1].strip()
        return cmd

    async def resolve_localhost_url(self, stub_url: str) -> str:
        """Resolve a localhost stub URL (e.g. http://localhost/ch/10571_) to a real stream URL.
        Matches the original GUI script logic exactly: extract channel id, call create_link."""
        if not stub_url or "localhost" not in stub_url:
            return stub_url
        try:
            if "/ch/" in stub_url:
                cid = stub_url.split("/ch/")[1].split("_")[0]
                cmd = quote(f"ffmpeg http://localhost/ch/{cid}_")
                url = (
                    f"{self.base}/portal.php?type=itv&action=create_link"
                    f"&cmd={cmd}&series=&forced_storage=0"
                    f"&disable_ad=0&download=0&force_ch_link_check=0"
                    f"&JsHttpRequest=1-xml"
                )
                self.log(f"[MAC] Resolving localhost ch={cid}")
                async with self.session.get(url, headers=self.headers) as r:
                    self.log(f"[MAC] Localhost fix HTTP {r.status} (ch={cid})")
                    payload = await safe_json(r)
                if not isinstance(payload, dict):
                    return stub_url
                js = payload.get("js", {})
                if isinstance(js, list) and js:
                    js = js[0]
                resolved = js.get("cmd") or js.get("url") if isinstance(js, dict) else None
                if not resolved and isinstance(js, dict):
                    data = js.get("data", {})
                    if isinstance(data, dict):
                        resolved = data.get("cmd") or data.get("url")
                if resolved and isinstance(resolved, str):
                    # Strip "ffmpeg " or "auto " prefix
                    if resolved.startswith("ffmpeg "):
                        resolved = resolved.split(" ", 1)[1]
                    if resolved.lower().startswith("auto "):
                        resolved = resolved.split(" ", 1)[1]
                    resolved = resolved.replace("\\/", "/").strip()
                    if resolved.startswith(("http://", "https://", "rtsp://")):
                        self.log(f"[LOCALHOST FIX] Resolved ch={cid} → {resolved[:120]}")
                        return resolved
        except Exception as e:
            self.log(f"[LOCALHOST FIX] Failed to resolve {stub_url}: {e}")
        return stub_url

    async def _maybe_resolve_cmd(self, cmd: str) -> str:
        assert self.session is not None
        if not cmd:
            return ""
        cmd = self._clean_cmd(cmd)
        # If cleaning already gave us a plain URL, check localhost and return
        if cmd.startswith(("http://", "https://", "rtsp://")):
            if "localhost" in cmd:
                return await self.resolve_localhost_url(cmd)
            return cmd
        try:
            candidates = []
            url_match = self._extract_url_from_text(cmd)
            if url_match:
                candidates.append(url_match)
            if not candidates:
                encoded = quote_plus(cmd)
                candidates = [
                    f"{self.base}/portal.php?type=vod&action=create_link&cmd={encoded}&JsHttpRequest=1-xml",
                    f"{self.base}/portal.php?type=itv&action=create_link&cmd={encoded}&JsHttpRequest=1-xml",
                ]
            for url in candidates:
                try:
                    async with self.session.get(url, headers=self.headers, allow_redirects=True) as r:
                        final_url = str(r.url)
                        text = await r.text()
                        text_stripped = text.strip()
                        if text_stripped.startswith(("http://", "https://", "rtsp://")):
                            if "localhost" in text_stripped:
                                resolved = await self.resolve_localhost_url(text_stripped)
                                if resolved != text_stripped:
                                    return resolved
                            return text_stripped
                        if final_url.startswith(("http://", "https://", "rtsp://")) and final_url != url:
                            if "localhost" in final_url:
                                resolved = await self.resolve_localhost_url(final_url)
                                if resolved != final_url:
                                    return resolved
                            return final_url
                        if text_stripped.startswith("#EXTM3U") or text_stripped.startswith("#EXTINF"):
                            return final_url
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    async def fetch_episodes_for_show(self, item: dict, cat_title: str):
        series_id = item.get("id")
        if isinstance(series_id, str) and ":" in series_id:
            series_id = series_id.split(":")[0]
        series_name = item.get("name") or item.get("o_name") or item.get("fname") or "Unknown Series"
        series_logo = item.get("logo") or item.get("screenshot_uri") or ""
        cat_id = str(item.get("_cat_id", ""))
        self.log(f"[SERIES] Fetching episodes for: {series_name}")
        episodes_data = await self.fetch_series_episodes(series_id, cat_id)
        if not episodes_data:
            self.log(f"[SERIES] No episodes returned for {series_name}")
            return []
        result = []
        for season in episodes_data:
            if not isinstance(season, dict):
                continue
            season_id = season.get("id", "")
            if isinstance(season_id, str) and ":" in season_id:
                season_num = season_id.split(":")[1]
            else:
                season_num = str(season_id)
            episodes_list = season.get("series", [])
            if not episodes_list:
                continue
            cmd_data = {"series_id": series_id, "season_num": int(season_num), "type": "series"}
            cmd_json = json.dumps(cmd_data, separators=(",", ":")).encode("utf-8")
            cmd_b64 = base64.b64encode(cmd_json).decode("ascii")
            total_eps = len(episodes_list)
            ep_width = len(str(total_eps))
            for episode_num in episodes_list:
                try:
                    ep_num_int = int(episode_num)
                except Exception:
                    ep_num_int = 0
                full_name = f"{series_name} S{season_num.zfill(2)}E{ep_num_int:0{ep_width}d}"
                result.append({
                    "name": full_name,
                    "logo": series_logo,
                    "_mac_resolve": True,
                    "_mac_cmd_b64": cmd_b64,
                    "_mac_episode_num": episode_num,
                    "_mac_series_id": series_id,
                    "_mac_cat_id": cat_id,
                    "_cat_title": cat_title,
                    "tvg_type": "series",
                })
        self.log(f"[SERIES] {series_name}: {len(result)} episodes across {len(episodes_data)} season(s)")
        return result

    def extract_vod_info(self, item: dict):
        name = item.get("name") or item.get("o_name") or item.get("fname") or "Unknown"
        logo = item.get("logo") or item.get("screenshot_uri") or item.get("pic") or ""
        cmd = item.get("cmd") or ""
        return (name, logo, str(cmd))

    def extract_playables_for_item(self, mode: str, item: dict):
        results = []
        parent_name = item.get("name") or item.get("o_name") or item.get("fname") or "Unknown"
        parent_logo = item.get("logo") or item.get("screenshot_uri") or item.get("pic") or ""
        cmd = item.get("cmd") or item.get("rtsp_url") or item.get("file") or ""
        if not cmd:
            cmd = self._join_path_and_file(item.get("path"), item.get("file")) or ""
        if mode == "live" and cmd:
            cmd = cmd.split()[-1]
        if cmd:
            results.append((parent_name, parent_logo, cmd))
        return results

    async def resolve_item_url(self, mode: str, item: dict, category: dict) -> str:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".m3u")
        os.close(tmp_fd)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write("")
            await self.dump_single_item_to_file(mode, item, category, tmp_path)
            with open(tmp_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
            return ""
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    async def dump_single_item_to_file(self, mode: str, item: dict, category: dict, out_path: str, stop_flag=None):
        cat_title = category.get("title", "Unknown")

        if item.get("_mac_resolve"):
            ep_name = item.get("name", "Unknown")
            ep_logo = item.get("logo", "")
            ep_cat = item.get("_cat_title") or cat_title
            cmd_b64 = item.get("_mac_cmd_b64", "")
            ep_num = item.get("_mac_episode_num", "")
            series_id = item.get("_mac_series_id", "")
            url = f"{self.base}/portal.php?type=vod&action=create_link&cmd={quote_plus(cmd_b64)}&series={ep_num}"
            resolved = ""
            try:
                async with self.session.get(url, headers=self.headers, allow_redirects=True) as r:
                    payload = await safe_json(r)
                    if isinstance(payload, dict):
                        js = payload.get("js")
                        if isinstance(js, dict):
                            cmd_value = js.get("cmd", "")
                            if isinstance(cmd_value, str):
                                for part in cmd_value.split():
                                    if part.startswith(("http://", "https://", "rtsp://")):
                                        resolved = part
                                        break
                                if resolved and "localhost" in resolved:
                                    res2 = await self.resolve_localhost_url(resolved)
                                    if res2 != resolved:
                                        resolved = res2
            except Exception as e:
                self.log(f"[SERIES] Error resolving {ep_name}: {e}")
            if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                try:
                    fallback = f"{self.base}/get.php?series={series_id}&episode={ep_num}"
                    if self.token:
                        fallback += f"&token={self.token}"
                    async with self.session.get(fallback, headers=self.headers, allow_redirects=True) as rr:
                        text = (await rr.text()).strip()
                        final_url = str(rr.url)
                        if text.startswith(("http://", "https://", "rtsp://")):
                            resolved = text
                        elif final_url != fallback and final_url.startswith(("http://", "https://", "rtsp://")):
                            resolved = final_url
                except Exception:
                    pass
            if resolved and resolved.startswith(("http://", "https://", "rtsp://")):
                resolved = unquote(resolved)
                with open(out_path, "a", encoding="utf-8") as f:
                    f.write(_extinf_line(ep_name, ep_logo, 'series', ep_cat) + f'{resolved}\n')
                self.log(f"[SERIES] ✓ {ep_name}")
            else:
                self.log(f"[SERIES] ✗ Could not resolve: {ep_name}")
            return

        cat_id = str(category.get("id", ""))
        tvg_type = "live" if mode == "live" else ("movie" if mode == "vod" else "series")
        seen_urls = set()

        async def _try_get_series_episode(series_id, ep_id) -> str:
            if not series_id or not ep_id:
                return ""
            try:
                fallback = f"{self.base}/get.php?series={series_id}&episode={ep_id}"
                if getattr(self, "token", None) and "token=" not in fallback:
                    fallback = fallback + f"&token={self.token}"
                async with self.session.get(fallback, headers=self.headers, allow_redirects=True) as rr:
                    text = (await rr.text()).strip()
                    final_url = str(rr.url)
                    if text.startswith(("http://", "https://", "rtsp://", "#EXTM3U", "#EXTINF")):
                        result = final_url if text.startswith("#EXTM3U") else text
                        if "localhost" in result:
                            resolved = await self.resolve_localhost_url(result)
                            if resolved != result:
                                return resolved
                        return result
                    if final_url.startswith(("http://", "https://", "rtsp://")) and final_url != fallback:
                        if "localhost" in final_url:
                            resolved = await self.resolve_localhost_url(final_url)
                            if resolved != final_url:
                                return resolved
                        return final_url
            except Exception as e:
                self.log(f"[get.php series fallback] error: {e}")
            return ""

        with open(out_path, "a", encoding="utf-8") as f:
            if mode == "series":
                series_id = item.get("id")
                if isinstance(series_id, str) and ":" in series_id:
                    series_id = series_id.split(":")[0]
                series_name = item.get("name") or item.get("o_name") or item.get("fname") or "Unknown Series"
                series_logo = item.get("logo") or item.get("screenshot_uri") or ""
                if not series_id:
                    return
                self.log(f"[SERIES] Fetching episodes for: {series_name}")
                episodes_data = await self.fetch_series_episodes(series_id, cat_id)
                if not episodes_data:
                    return
                for season in episodes_data:
                    if not isinstance(season, dict):
                        continue
                    season_id = season.get("id", "")
                    if isinstance(season_id, str) and ":" in season_id:
                        season_num = season_id.split(":")[1]
                    else:
                        season_num = str(season_id)
                    episodes_list = season.get("series", [])
                    if not episodes_list:
                        continue
                    self.log(f"[SERIES] Season {season_num}: {len(episodes_list)} episodes")
                    cmd_data = {"series_id": series_id, "season_num": int(season_num), "type": "series"}
                    cmd_json = json.dumps(cmd_data, separators=(",", ":")).encode("utf-8")
                    cmd_b64 = base64.b64encode(cmd_json).decode("ascii")
                    for episode_num in episodes_list:
                        if stop_flag and stop_flag.is_set():
                            return
                        url = f"{self.base}/portal.php?type=vod&action=create_link&cmd={quote_plus(cmd_b64)}&series={episode_num}"
                        try:
                            async with self.session.get(url, headers=self.headers, allow_redirects=True) as r:
                                payload = await safe_json(r)
                                resolved = ""
                                if isinstance(payload, dict):
                                    js = payload.get("js")
                                    if isinstance(js, dict):
                                        cmd_value = js.get("cmd", "")
                                        if isinstance(cmd_value, str):
                                            for part in cmd_value.split():
                                                if part.startswith(("http://", "https://", "rtsp://")):
                                                    resolved = part
                                                    break
                                            if resolved and "localhost" in resolved:
                                                res2 = await self.resolve_localhost_url(resolved)
                                                if res2 != resolved:
                                                    resolved = res2
                        except Exception as e:
                            self.log(f"[SERIES] Error fetching episode {episode_num}: {e}")
                            continue
                        if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                            try:
                                fb = await _try_get_series_episode(series_id, episode_num)
                                if fb and fb.startswith(("http://", "https://", "rtsp://")):
                                    resolved = fb
                            except Exception:
                                pass
                        if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                            continue
                        resolved = unquote(resolved)
                        if resolved in seen_urls:
                            continue
                        seen_urls.add(resolved)
                        total_eps = len(episodes_list)
                        ep_width = len(str(total_eps))
                        try:
                            ep_num_int = int(episode_num)
                        except Exception:
                            ep_num_int = 0
                        full_name = f"{series_name} S{season_num} E{ep_num_int:0{ep_width}d}"
                        f.write(_extinf_line(full_name, series_logo, 'series', cat_title, item) + f'{resolved}\n')

            elif mode == "vod":
                name, logo, cmd = self.extract_vod_info(item)
                if not cmd:
                    return
                self.log(f"[VOD] Processing: {name}")
                try:
                    resolved = await self.fetch_vod_play_link(cmd)
                except Exception as e:
                    self.log(f"[VOD] Error resolving {name}: {e}")
                    resolved = ""
                if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                    try:
                        resolved = await self._maybe_resolve_cmd(cmd)
                    except Exception:
                        resolved = ""
                if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                    self.log(f"[VOD] Failed to resolve: {name}")
                    return
                resolved = unquote(resolved)
                if resolved not in seen_urls:
                    seen_urls.add(resolved)
                    f.write(_extinf_line(name, logo, tvg_type, cat_title, item) + f'{resolved}\n')
                    self.log(f"[VOD] ✓ Wrote: {name}")

            else:  # live
                playables = self.extract_playables_for_item(mode, item)
                for name, logo, cmd in playables:
                    if not cmd:
                        continue
                    cmd = cmd.split()[-1]
                    resolved = ""
                    if isinstance(cmd, str) and cmd.startswith(("http://", "https://", "rtsp://")):
                        if "localhost" in cmd:
                            resolved = await self.resolve_localhost_url(cmd)
                        else:
                            resolved = cmd
                    else:
                        try:
                            resolved = await self._maybe_resolve_cmd(cmd)
                        except Exception:
                            resolved = ""
                    if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                        try:
                            resolved = await self.create_episode_link(cmd, "live")
                        except Exception:
                            resolved = ""
                    if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                        self.log(f"Skipping unresolved item: {name}")
                        continue
                    resolved = unquote(resolved)
                    if resolved in seen_urls:
                        continue
                    seen_urls.add(resolved)
                    f.write(_extinf_line(name, logo, tvg_type, cat_title, item) + f'{resolved}\n')
                    self.log(f"[LIVE] ✓ Wrote: {name}")

    async def dump_category_to_file(self, mode: str, category: dict, out_path: str, append=True, stop_flag=None, progress_cb=None):
        cat_title = category.get("title", "Unknown")
        cat_id = str(category.get("id", ""))
        tvg_type = "live" if mode == "live" else ("movie" if mode == "vod" else "series")
        self.log(f"Downloading {mode.upper()} → {cat_title}")
        seen_urls = set()
        lines_written = 0

        async def _try_get_series_episode(series_id, ep_id) -> str:
            if not series_id or not ep_id:
                return ""
            try:
                fallback = f"{self.base}/get.php?series={series_id}&episode={ep_id}"
                if getattr(self, "token", None) and "token=" not in fallback:
                    fallback = fallback + f"&token={self.token}"
                async with self.session.get(fallback, headers=self.headers, allow_redirects=True) as rr:
                    text = (await rr.text()).strip()
                    final_url = str(rr.url)
                    if text.startswith(("http://", "https://", "rtsp://", "#EXTM3U", "#EXTINF")):
                        result = final_url if text.startswith("#EXTM3U") else text
                        if "localhost" in result:
                            resolved = await self.resolve_localhost_url(result)
                            if resolved != result:
                                return resolved
                        return result
                    if final_url.startswith(("http://", "https://", "rtsp://")) and final_url != fallback:
                        return final_url
            except Exception as e:
                self.log(f"[get.php series fallback] error: {e}")
            return ""

        with open(out_path, "a", encoding="utf-8") as f:
            if mode == "series":
                page = 1
                while True:
                    items = await self.fetch_items_page(mode, cat_id, page)
                    if not items:
                        break
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        series_id = it.get("id")
                        if isinstance(series_id, str) and ":" in series_id:
                            series_id = series_id.split(":")[0]
                        series_name = it.get("name") or it.get("o_name") or it.get("fname") or "Unknown Series"
                        series_logo = it.get("logo") or it.get("screenshot_uri") or ""
                        if not series_id:
                            continue
                        self.log(f"[SERIES] Fetching episodes for: {series_name}")
                        episodes_data = await self.fetch_series_episodes(series_id, cat_id)
                        if not episodes_data:
                            continue
                        for season in episodes_data:
                            if not isinstance(season, dict):
                                continue
                            season_id = season.get("id", "")
                            if isinstance(season_id, str) and ":" in season_id:
                                season_num = season_id.split(":")[1]
                            else:
                                season_num = str(season_id)
                            episodes_list = season.get("series", [])
                            if not episodes_list:
                                continue
                            cmd_data = {"series_id": series_id, "season_num": int(season_num), "type": "series"}
                            cmd_json = json.dumps(cmd_data, separators=(",", ":")).encode("utf-8")
                            cmd_b64 = base64.b64encode(cmd_json).decode("ascii")
                            for episode_num in episodes_list:
                                url = f"{self.base}/portal.php?type=vod&action=create_link&cmd={quote_plus(cmd_b64)}&series={episode_num}"
                                try:
                                    async with self.session.get(url, headers=self.headers, allow_redirects=True) as r:
                                        payload = await safe_json(r)
                                        resolved = ""
                                        if isinstance(payload, dict):
                                            js = payload.get("js")
                                            if isinstance(js, dict):
                                                cmd_value = js.get("cmd", "")
                                                if isinstance(cmd_value, str):
                                                    for part in cmd_value.split():
                                                        if part.startswith(("http://", "https://", "rtsp://")):
                                                            resolved = part
                                                            break
                                except Exception as e:
                                    self.log(f"[SERIES] Error: {e}")
                                    continue
                                if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                                    try:
                                        fb = await _try_get_series_episode(series_id, episode_num)
                                        if fb:
                                            resolved = fb
                                    except Exception:
                                        pass
                                if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                                    continue
                                resolved = unquote(resolved)
                                if resolved in seen_urls:
                                    continue
                                seen_urls.add(resolved)
                                total_eps = len(episodes_list)
                                ep_width = len(str(total_eps))
                                try:
                                    ep_num_int = int(episode_num)
                                except Exception:
                                    ep_num_int = 0
                                full_name = f"{series_name} S{season_num} E{ep_num_int:0{ep_width}d}"
                                f.write(_extinf_line(full_name, series_logo, 'series', cat_title, it) + f'{resolved}\n')
                                lines_written += 1
                                if progress_cb: progress_cb(lines_written)
                    page += 1
                    if len(items) < 5:
                        break
                return

            if mode == "vod":
                page = 1
                while True:
                    items = await self.fetch_items_page(mode, cat_id, page)
                    if not items:
                        break
                    new_count = 0
                    for it in items:
                        if stop_flag and stop_flag.is_set():
                            return
                        if not isinstance(it, dict):
                            continue
                        name, logo, cmd = self.extract_vod_info(it)
                        if not cmd:
                            continue
                        try:
                            resolved = await self.fetch_vod_play_link(cmd)
                        except Exception as e:
                            self.log(f"[VOD] Error resolving {name}: {e}")
                            resolved = ""
                        if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                            try:
                                resolved = await self._maybe_resolve_cmd(cmd)
                            except Exception:
                                resolved = ""
                        if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                            continue
                        resolved = unquote(resolved)
                        if resolved in seen_urls:
                            continue
                        seen_urls.add(resolved)
                        f.write(_extinf_line(name, logo, tvg_type, cat_title, it) + f'{resolved}\n')
                        lines_written += 1
                        if progress_cb: progress_cb(lines_written)
                        new_count += 1
                    if new_count == 0:
                        break
                    page += 1
                return

            # live
            page = 1
            while True:
                items = await self.fetch_items_page(mode, cat_id, page)
                if not items:
                    break
                new_count = 0
                for it in items:
                    if stop_flag and stop_flag.is_set():
                        return
                    if not isinstance(it, dict):
                        continue
                    playables = self.extract_playables_for_item(mode, it)
                    for name, logo, cmd in playables:
                        if not cmd:
                            continue
                        cmd = cmd.split()[-1]
                        resolved = ""  # resolve normally""
                        if isinstance(cmd, str) and cmd.startswith(("http://", "https://", "rtsp://")):
                            if "localhost" in cmd:
                                resolved = await self.resolve_localhost_url(cmd)
                            else:
                                resolved = cmd
                        else:
                            try:
                                resolved = await self._maybe_resolve_cmd(cmd)
                            except Exception:
                                resolved = ""
                        if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                            try:
                                resolved = await self.create_episode_link(cmd, "live")
                            except Exception:
                                resolved = ""
                        if not resolved or not resolved.startswith(("http://", "https://", "rtsp://")):
                            continue
                        resolved = unquote(resolved)
                        if resolved in seen_urls:
                            continue
                        seen_urls.add(resolved)
                        f.write(_extinf_line(name, logo, tvg_type, cat_title, it) + f'{resolved}\n')
                        lines_written += 1
                        if progress_cb: progress_cb(lines_written)
                        new_count += 1
                if new_count == 0:
                    break
                page += 1

        self.log(f"Finished {cat_title} (items: {lines_written})")


# ===================== STALKER PORTAL CLIENT =====================
# Mirrors the working stalker.py logic but using aiohttp for async compatibility.
# Key differences from the standard PortalClient:
#   - URL path: /stalker_portal/server/load.php  (not /portal.php)
#   - Requires MAG200 User-Agent, Referer, X-User-Agent, Cookie as header string
#   - 404 handshake: generate token+prehash and retry
#   - get_profile must be called after handshake to confirm/refresh token

class StalkerPortalClient:
    LOAD_PHP     = "/stalker_portal/server/load.php"
    LOAD_PHP_ALT = "/stalker_portal/portal.php"

    def __init__(self, base_url: str, mac: str, log_cb):
        self.base = normalize_base_url(base_url)
        self.mac = mac.strip().upper()
        self.log = log_cb
        self.session = None
        self.token = None
        self.bearer_token = None
        self._random = None
        # Derived IDs — mirroring stalker.py
        self.serial = hashlib.md5(self.mac.encode()).hexdigest()[:13].upper()
        self.device_id = hashlib.sha256(self.mac.encode()).hexdigest().upper()
        # Cache for channel id → logo URL, populated lazily from get_all_channels
        self._ch_logo_cache: dict | None = None
        # Running in-memory logo cache for VOD / series — populated from items
        # that already have a logo so we can fill blanks without extra requests.
        self._vod_logo_cache: dict = {}

    # ── context manager ──────────────────────────────────────────────────────

    async def __aenter__(self):
        _timeout = aiohttp.ClientTimeout(total=60, connect=10)
        # NO session-level cookies — stalker portals require Cookie as a header string
        self.session = aiohttp.ClientSession(timeout=_timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.close()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _fix_logo_url(self, val: str) -> str:
        """Normalise a logo/screenshot URL returned by the stalker portal.

        Stalker portals are notorious for returning image paths in three broken forms
        in addition to well-formed absolute URLs:

          1. Relative path    – ``/stalker_portal/misc/logos/480.png``
          2. Hostless URL     – ``http://:/stalker_portal/...`` or
                                ``http:///stalker_portal/...``  (no host, no port)
          3. Localhost URL    – ``http://localhost/stalker_portal/misc/logos/480.png``
                                The portal embeds 'localhost' in image paths (same as
                                it does in stream cmd fields). The browser would try to
                                load this from the user's own machine instead of the
                                portal server, so we must replace it with self.base.

        In all three cases the path is intact; only the authority is missing or wrong.
        """
        if not val or not isinstance(val, str):
            return val or ""
        val = val.strip()
        if not val:
            return ""
        # Case 2: hostless URL — http://:/... or http:///...
        if re.match(r'https?://[:/]', val):
            path_part = re.sub(r'^https?://[^/]*', '', val)
            return self.base.rstrip("/") + "/" + path_part.lstrip("/")
        # Case 3: localhost URL — replace localhost authority with portal base
        if re.match(r'https?://localhost(?:[:/]|$)', val):
            path_part = re.sub(r'^https?://localhost(?::\d+)?', '', val)
            return self.base.rstrip("/") + "/" + path_part.lstrip("/")
        # Case 1 (already absolute, correct host) — return as-is
        if val.startswith(("http://", "https://")):
            return val
        # Case 1b: relative path
        return self.base.rstrip("/") + "/" + val.lstrip("/")

    def _cookie_str(self, include_token: bool = True) -> str:
        parts = [
            f"mac={quote(self.mac)}",
            "stb_lang=en",
            f"timezone={quote('Europe/Paris')}",
        ]
        if include_token and self.bearer_token:
            parts.append(f"token={quote(self.bearer_token)}")
        return "; ".join(parts)

    def _headers(self, include_auth: bool = False, include_token: bool = True) -> dict:
        h = {
            "Accept": "*/*",
            "User-Agent": (
                "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
                "(KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3"
            ),
            "Referer": f"{self.base}/stalker_portal/c/index.html",
            "Accept-Language": "en-US,en;q=0.5",
            "Pragma": "no-cache",
            "X-User-Agent": "Model: MAG250; Link: WiFi",
            "Cookie": self._cookie_str(include_token=include_token),
            "Connection": "close",
            "Accept-Encoding": "gzip, deflate",
        }
        if include_auth and self.bearer_token:
            h["Authorization"] = f"Bearer {self.bearer_token}"
        return h

    def _load_url(self, **params) -> str:
        from urllib.parse import urlencode
        return f"{self.base}{self.LOAD_PHP}?{urlencode(params)}"

    def _load_url_alt(self, **params) -> str:
        from urllib.parse import urlencode
        return f"{self.base}{self.LOAD_PHP_ALT}?{urlencode(params)}"

    def _generate_token(self) -> str:
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=32))

    def _generate_prehash(self, token: str) -> str:
        return hashlib.sha1(token.encode()).hexdigest()

    def _generate_random(self) -> str:
        return ''.join(random.choices('0123456789abcdef', k=40))

    def _generate_signature(self) -> str:
        data = f"{self.mac}{self.serial}{self.device_id}{self.device_id}"
        return hashlib.sha256(data.encode()).hexdigest().upper()

    def _generate_metrics(self) -> str:
        if not self._random:
            self._random = self._generate_random()
        return json.dumps({
            "mac": self.mac, "sn": self.serial, "type": "STB",
            "model": "MAG250", "uid": "", "random": self._random
        })

    # ── auth ──────────────────────────────────────────────────────────────────

    async def handshake(self) -> str:
        assert self.session is not None
        url = self._load_url(type="stb", action="handshake", token="", JsHttpRequest="1-xml")
        headers = self._headers(include_auth=False, include_token=False)
        self.log(f"[STALKER] Handshake → {self.base}{self.LOAD_PHP}")
        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] Handshake HTTP {r.status}")
            if r.status == 404:
                # Stalker-specific: generate token+prehash and retry
                self.log("[STALKER] 404 on handshake — retrying with token+prehash")
                tok = self._generate_token()
                prehash = self._generate_prehash(tok)
                url2 = self._load_url(type="stb", action="handshake",
                                      token=tok, prehash=prehash, JsHttpRequest="1-xml")
                async with self.session.get(url2, headers=headers) as r2:
                    self.log(f"[STALKER] Retry handshake HTTP {r2.status}")
                    payload = await safe_json(r2)
            else:
                payload = await safe_json(r)

        if not isinstance(payload, dict) or "js" not in payload:
            raise RuntimeError(f"[STALKER] Handshake failed — no valid JSON response")
        js = payload["js"]
        if not isinstance(js, dict):
            raise RuntimeError("[STALKER] Handshake failed — unexpected js structure")
        self.token = js.get("token")
        if not self.token:
            raise RuntimeError("[STALKER] Handshake failed — token missing in response")
        rand = js.get("random")
        self._random = rand.lower() if rand else self._generate_random()
        self.bearer_token = self.token
        self.log(f"[STALKER] Token acquired: {self.token[:16]}…")

        # Call get_profile to confirm/refresh token (required by stalker protocol)
        await self.get_profile()
        return self.token

    async def get_profile(self) -> dict:
        assert self.session is not None
        # Must match stalker.py exactly — ver and metrics are required to activate the token
        from urllib.parse import urlencode
        params = {
            "type": "stb",
            "action": "get_profile",
            "hd": "1",
            "ver": (
                "ImageDescription: 0.2.18-r23-250; ImageDate: Thu Sep 13 11:31:16 EEST 2018; "
                "PORTAL version: 5.6.2; API Version: JS API version: 343; "
                "STB API version: 146; Player Engine version: 0x58c"
            ),
            "num_banks": "2",
            "sn": self.serial,
            "stb_type": "MAG250",
            "client_type": "STB",
            "image_version": "218",
            "video_out": "hdmi",
            "device_id": self.device_id,
            "device_id2": self.device_id,
            "signature": self._generate_signature(),
            "auth_second_step": "1",
            "hw_version": "1.7-BD-00",
            "not_valid_token": "0",
            "metrics": self._generate_metrics(),
            "hw_version_2": hashlib.sha1(self.mac.encode()).hexdigest(),
            "timestamp": int(time.time()),
            "api_signature": "262",
            "prehash": "",
            "JsHttpRequest": "1-xml",
        }
        url = f"{self.base}{self.LOAD_PHP}?{urlencode(params)}"
        headers = self._headers(include_auth=True, include_token=False)
        self.log("[STALKER] Getting profile…")
        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] Profile HTTP {r.status}")
            payload = await safe_json(r)
        if isinstance(payload, dict):
            js = payload.get("js", {})
            if isinstance(js, dict):
                new_token = js.get("token")
                if new_token:
                    self.token = new_token
                    self.bearer_token = new_token
                    self.log(f"[STALKER] Profile token refreshed: {self.token[:16]}…")
                return js
        return {}

    async def account_info(self):
        assert self.session is not None
        url = self._load_url(type="account_info", action="get_main_info", JsHttpRequest="1-xml")
        headers = self._headers(include_auth=True)
        self.log("[STALKER] Fetching account info…")
        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] Account info HTTP {r.status}")
            payload = await safe_json(r)
        if isinstance(payload, dict):
            js = payload.get("js", {})
            if isinstance(js, dict):
                mac = str(js.get("mac") or js.get("device_mac") or self.mac)
                exp = str(js.get("phone") or js.get("expire_billing_date") or "unknown")
                self.log(f"[STALKER] Account: MAC={mac}  expiry={exp}")
                return (mac, exp)
        return (self.mac, "unknown")

    # ── categories ────────────────────────────────────────────────────────────

    @staticmethod
    def _is_series_cat(name: str) -> bool:
        return any(k in name.lower() for k in ('tv', 'series', 'show', 'episode'))

    async def fetch_categories(self, mode: str):
        assert self.session is not None
        if mode == "live":
            url = self._load_url(type="itv", action="get_genres", JsHttpRequest="1-xml")
        else:
            # Both vod and series use the same endpoint — filtered by name below
            url = self._load_url(type="vod", action="get_categories", JsHttpRequest="1-xml")
        headers = self._headers(include_auth=True)
        self.log(f"[STALKER] Fetching {mode.upper()} categories…")
        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] Categories HTTP {r.status} ({mode.upper()})")
            payload = await safe_json(r)
        cats = normalize_js(payload)
        # Fallback: try /stalker_portal/portal.php if server/load.php returned nothing
        if not cats:
            if mode == "live":
                alt_url = self._load_url_alt(type="itv", action="get_genres", JsHttpRequest="1-xml")
            else:
                alt_url = self._load_url_alt(type="vod", action="get_categories", JsHttpRequest="1-xml")
            self.log(f"[STALKER] Categories empty — retrying via portal.php ({mode.upper()})")
            async with self.session.get(alt_url, headers=headers) as r2:
                self.log(f"[STALKER] Categories (alt) HTTP {r2.status} ({mode.upper()})")
                payload = await safe_json(r2)
            cats = normalize_js(payload)
        result = []
        for c in cats:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or c.get("category_id") or "").strip()
            name = str(c.get("title") or c.get("name") or c.get("category_name") or "").strip()
            if not cid or not name:
                continue
            # Filter: series tab gets TV/series/show categories; vod tab gets the rest
            if mode == "series" and not self._is_series_cat(name):
                continue
            if mode == "vod" and self._is_series_cat(name):
                continue
            result.append({"id": cid, "title": name})
        self.log(f"[STALKER] {mode.upper()} categories: {len(result)} found")
        return result

    # ── items ─────────────────────────────────────────────────────────────────

    async def _fetch_ch_logo_cache(self) -> dict:
        """Fetch get_all_channels once and return a dict of {channel_id: logo_url}.
        Tries load.php first, then portal.php as fallback if no logos come back.
        Results are cached on the instance so subsequent pages pay no extra cost."""
        if self._ch_logo_cache is not None:
            return self._ch_logo_cache
        self._ch_logo_cache = {}
        headers = self._headers(include_auth=True)

        def _extract_logos(all_ch: list) -> dict:
            out = {}
            for ch in all_ch:
                if not isinstance(ch, dict):
                    continue
                ch_id = str(ch.get("id") or "").strip()
                logo  = str(ch.get("logo") or ch.get("screenshot_uri") or
                            ch.get("tv_logo") or ch.get("pic") or "").strip()
                if ch_id and logo:
                    out[ch_id] = self._fix_logo_url(logo)
            return out

        # Attempt 1: /stalker_portal/server/load.php
        try:
            url = self._load_url(type="itv", action="get_all_channels",
                                 force_ch_link_check="", JsHttpRequest="1-xml")
            self.log("[STALKER] Logo cache: trying load.php get_all_channels…")
            async with self.session.get(url, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                self.log(f"[STALKER] Logo cache load.php HTTP {r.status}")
                payload = await safe_json(r)
            self._ch_logo_cache = _extract_logos(normalize_js(payload))
            self.log(f"[STALKER] Logo cache (load.php): {len(self._ch_logo_cache)} entries")
        except Exception as e:
            self.log(f"[STALKER] Logo cache load.php error: {e}")

        # Attempt 2: /stalker_portal/portal.php — only if attempt 1 yielded nothing
        if not self._ch_logo_cache:
            try:
                url2 = self._load_url_alt(type="itv", action="get_all_channels",
                                          force_ch_link_check="", JsHttpRequest="1-xml")
                self.log("[STALKER] Logo cache: trying portal.php get_all_channels…")
                async with self.session.get(url2, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=20)) as r2:
                    self.log(f"[STALKER] Logo cache portal.php HTTP {r2.status}")
                    payload2 = await safe_json(r2)
                self._ch_logo_cache = _extract_logos(normalize_js(payload2))
                self.log(f"[STALKER] Logo cache (portal.php): {len(self._ch_logo_cache)} entries")
            except Exception as e2:
                self.log(f"[STALKER] Logo cache portal.php error: {e2}")

        return self._ch_logo_cache

    async def fetch_items_page(self, mode: str, cat_id: str, page: int):
        assert self.session is not None
        if mode == "live":
            url = self._load_url(type="itv", action="get_ordered_list",
                                 genre=cat_id, JsHttpRequest="1-xml", p=page)
        else:
            # Both vod and series use type=vod in the stalker protocol
            url = self._load_url(type="vod", action="get_ordered_list",
                                 category=cat_id, JsHttpRequest="1-xml", p=page)
        headers = self._headers(include_auth=True)
        self.log(f"[STALKER] Fetching {mode.upper()} items page={page} cat={cat_id}…")
        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] Items HTTP {r.status} ({mode.upper()} cat={cat_id} p={page})")
            payload = await safe_json(r)
        items = normalize_js(payload)
        # Fallback: try /stalker_portal/portal.php if server/load.php returned nothing
        if not items and page == 1:
            if mode == "live":
                alt_url = self._load_url_alt(type="itv", action="get_ordered_list",
                                              genre=cat_id, JsHttpRequest="1-xml", p=page)
            else:
                alt_url = self._load_url_alt(type="vod", action="get_ordered_list",
                                              category=cat_id, JsHttpRequest="1-xml", p=page)
            self.log(f"[STALKER] Items empty — retrying via portal.php ({mode.upper()} cat={cat_id})")
            async with self.session.get(alt_url, headers=headers) as r2:
                self.log(f"[STALKER] Items (alt) HTTP {r2.status} ({mode.upper()} cat={cat_id})")
                payload = await safe_json(r2)
            items = normalize_js(payload)
        for it in items:
            if not isinstance(it, dict):
                continue
            # is_series=1 → show with seasons
            if str(it.get("is_series", "0")) == "1":
                it["_is_show_item"] = True
            # is_season present → season container returned inside a show drill
            elif "is_season" in it:
                it["_is_show_item"] = True
            # Fallback: name ends with "Season N" — untagged season containers
            elif re.search(r'\bSeason\s+\d+\b', it.get("name") or it.get("o_name") or "", re.IGNORECASE):
                it["_is_show_item"] = True
            # Rewrite logo/screenshot URLs to absolute (handles relative, hostless,
            # AND localhost URLs that stalker portals embed in item data)
            for logo_field in ("logo", "screenshot_uri", "pic"):
                val = it.get(logo_field)
                if val and isinstance(val, str):
                    fixed = self._fix_logo_url(val)
                    if fixed != val:
                        it[logo_field] = fixed
        # For live channels whose logo field is empty, try get_all_channels as fallback.
        # Only triggered when at least one channel in this page is missing a logo.
        if mode == "live":
            if any(not it.get("logo") for it in items if isinstance(it, dict)):
                logo_cache = await self._fetch_ch_logo_cache()
                if logo_cache:
                    for it in items:
                        if isinstance(it, dict) and not it.get("logo"):
                            ch_id = str(it.get("id") or "").strip()
                            if ch_id and ch_id in logo_cache:
                                it["logo"] = logo_cache[ch_id]
        else:
            # VOD / SERIES: no extra network call.
            # Build the running in-memory cache from items that have a logo,
            # then use it to fill items that don't — handles portals that return
            # logos inconsistently across pages.
            for it in items:
                if not isinstance(it, dict):
                    continue
                item_id = str(it.get("id") or "").strip()
                logo = (it.get("logo") or it.get("screenshot_uri") or
                        it.get("pic") or "").strip()
                if item_id and logo:
                    self._vod_logo_cache[item_id] = logo
            for it in items:
                if not isinstance(it, dict):
                    continue
                if not (it.get("logo") or it.get("screenshot_uri") or it.get("pic")):
                    item_id = str(it.get("id") or "").strip()
                    cached = self._vod_logo_cache.get(item_id, "")
                    if cached:
                        it["logo"] = cached
        self.log(f"[STALKER] {mode.upper()} cat={cat_id} p={page}: {len(items)} items")
        return items

    async def fetch_series_episodes(self, series_id: str, category_id: str):
        assert self.session is not None
        # Stalker portals use type=vod for series episode lists.
        # Pass series_id raw — _load_url/urlencode handles encoding (no pre-quoting).
        url = self._load_url(type="vod", action="get_ordered_list",
                             movie_id=series_id, season_id="0", episode_id="0",
                             row="0", JsHttpRequest="1-xml", category=category_id,
                             sortby="added", fav="0", hd="0", not_ended="0",
                             abc="*", genre="*", years="*", search="", p="1")
        headers = self._headers(include_auth=True)
        self.log(f"[STALKER] Fetching episodes series_id={series_id}")
        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] Episodes HTTP {r.status} (series_id={series_id})")
            payload = await safe_json(r)
        items = normalize_js(payload)
        self.log(f"[STALKER] Series episodes: {len(items)} found")
        # Rewrite logo URLs to absolute (handles relative, hostless and localhost URLs)
        for it in items:
            if not isinstance(it, dict):
                continue
            for logo_field in ("logo", "screenshot_uri", "pic"):
                val = it.get(logo_field)
                if val and isinstance(val, str):
                    fixed = self._fix_logo_url(val)
                    if fixed != val:
                        it[logo_field] = fixed
        return items

    # ── stream link ───────────────────────────────────────────────────────────

    async def _resolve_stub_url(self, stub: str) -> str:
        """Resolve a Stalker stub URL like http:///ch/27063_ or http://localhost/ch/27063_
        by making a second create_link call with the forced_storage/series params."""
        assert self.session is not None
        # Extract channel id from /ch/{id}_ pattern
        m = re.search(r'/ch/(\d+)_?', stub)
        if not m:
            return stub
        cid = m.group(1)
        cmd = f"ffmpeg http://localhost/ch/{cid}_"
        from urllib.parse import urlencode
        params = {
            "type": "itv",
            "action": "create_link",
            "cmd": cmd,
            "series": "",
            "forced_storage": "0",
            "disable_ad": "0",
            "download": "0",
            "force_ch_link_check": "0",
            "JsHttpRequest": "1-xml",
        }
        url = f"{self.base}{self.LOAD_PHP}?{urlencode(params)}"
        headers = self._headers(include_auth=True)
        self.log(f"[STALKER] Resolving stub ch={cid}…")
        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] Stub resolve HTTP {r.status} (ch={cid})")
            payload = await safe_json(r)
        if not isinstance(payload, dict):
            return stub
        js = payload.get("js", {})
        if isinstance(js, list) and js:
            js = js[0]
        if not isinstance(js, dict):
            return stub
        resolved = js.get("cmd") or js.get("url") or ""
        if not resolved:
            return stub
        resolved = resolved.strip()
        if resolved.lower().startswith("ffmpeg "):
            resolved = resolved.split(" ", 1)[1].strip()
        if resolved.lower().startswith("auto "):
            resolved = resolved.split(" ", 1)[1].strip()
        resolved = resolved.replace("\\/", "/")
        if resolved.startswith(("http://", "https://", "rtsp://")):
            self.log(f"[STALKER] Resolved ch={cid} → {resolved[:120]}")
            return resolved
        extracted = _extract_url_from_text(resolved)
        if extracted:
            return extracted
        return stub

    async def create_catchup_link(self, cmd: str, start_str: str, duration_min: int,
                                  archive_cmd: str = "") -> str:
        """Resolve a catchup/timeshift link for a past programme.

        If archive_cmd is supplied (e.g. 'auto /media/537163805.mpg' from
        get_simple_data_table), the request is sent as type=tv_archive — exactly
        what SFVip/TiviMate send and what Stalker portals actually honour.
        Without archive_cmd we fall back to type=itv + start/duration (providers.py
        style) which works on some portals but not all.

        start_str: 'YYYY-MM-DD:HH-MM' (local time)
        duration_min: programme duration in minutes
        """
        assert self.session is not None
        from urllib.parse import quote as _q

        effective_cmd = archive_cmd.strip() if archive_cmd.strip() else cmd

        if archive_cmd.strip():
            # SFVip-style: type=tv_archive with the per-entry archive cmd.
            # Use %20 (not +) for spaces — Stalker portals require it in cmd.
            params_str = (
                f"type=tv_archive&action=create_link"
                f"&cmd={_q(effective_cmd, safe='')}"
                f"&series=&forced_storage=0&disable_ad=0&download=0"
                f"&force_ch_link_check=0&JsHttpRequest=1-xml"
            )
        else:
            # providers.py resolve_catchup exact params: type=itv, series=1, start, duration
            params_str = (
                f"type=itv&action=create_link"
                f"&cmd={_q(effective_cmd, safe='')}"
                f"&JsHttpRequest=1-xml"
                f"&download=0&save=0&series=1&forced_storage=0"
                f"&start={_q(start_str, safe='-:')}&duration={duration_min}"
            )
        url = f"{self.base}{self.LOAD_PHP}?{params_str}"
        headers = self._headers(include_auth=True)
        self.log(f"[STALKER] create_catchup_link cmd={cmd[:40]} start={start_str} dur={duration_min}m")
        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] catchup_link HTTP {r.status}")
            payload = await safe_json(r)
        if not isinstance(payload, dict):
            return ""
        js = payload.get("js", {})
        if isinstance(js, list) and js:
            js = js[0]
        if not isinstance(js, dict):
            return ""
        cmd_value = js.get("cmd") or js.get("url") or ""
        if not cmd_value:
            return ""
        cmd_value = cmd_value.strip()
        if cmd_value.lower().startswith("ffmpeg "):
            cmd_value = cmd_value.split(" ", 1)[1].strip()
        if cmd_value.lower().startswith("auto "):
            cmd_value = cmd_value.split(" ", 1)[1].strip()
        cmd_value = cmd_value.replace("\\/", "/")
        # Fix hostless URLs the portal sometimes returns:
        #   http://:/stalker_portal/...  or  http:///stalker_portal/...
        # Prepend the base host so the URL is valid.
        if re.match(r'https?://[:/]', cmd_value):
            path_part = re.sub(r'^https?://[^/]*', '', cmd_value)
            cmd_value = self.base.rstrip('/') + path_part
            self.log(f"[STALKER] Fixed hostless URL → {cmd_value[:120]}")

        # Detect a null/failed tv_archive storage response.
        # When the portal can't find a recording it returns a storage URL like:
        #   .../storage/get.php?filename=19691231-19.mpg&start=0&duration=0&real_id=
        # (filename date is Unix epoch 0).  Treat this as a failure so the caller
        # can fall back to type=itv + start/duration.
        if ('storage/get.php' in cmd_value and
                ('filename=1969' in cmd_value or
                 'start=0&duration=0' in cmd_value or
                 'real_id=' in cmd_value.split('real_id=')[-1][:1] + ' ')):
            # Check specifically for epoch date or empty real_id
            _is_null = (
                'filename=1969' in cmd_value or
                ('real_id=' in cmd_value and cmd_value.split('real_id=')[1].split('&')[0] == '')
            )
            if _is_null:
                self.log(f"[STALKER] tv_archive returned null storage response — will fallback")
                return ""

        if cmd_value.startswith(("http://", "https://", "rtsp://")):
            return cmd_value
        extracted = _extract_url_from_text(cmd_value)
        return extracted or ""

    async def create_stream_link(self, cmd: str, ptype: str = "itv") -> str:
        assert self.session is not None
        # Pass raw cmd — _load_url uses urlencode() which encodes it correctly once.
        # Do NOT quote_plus() here or the cmd gets double-encoded.
        url = self._load_url(type=ptype, action="create_link",
                             cmd=cmd, JsHttpRequest="1-xml")
        headers = self._headers(include_auth=True)
        self.log(f"[STALKER] create_link ({ptype}) cmd={cmd[:40]}…")
        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] create_link HTTP {r.status}")
            payload = await safe_json(r)
        if not isinstance(payload, dict):
            return ""
        js = payload.get("js", {})
        if isinstance(js, list) and js:
            js = js[0]
        if not isinstance(js, dict):
            return ""
        cmd_value = js.get("cmd") or js.get("url") or ""
        if not cmd_value:
            return ""
        # Strip 'ffmpeg '/'auto ' prefix
        cmd_value = cmd_value.strip()
        if cmd_value.lower().startswith("ffmpeg "):
            cmd_value = cmd_value.split(" ", 1)[1].strip()
        if cmd_value.lower().startswith("auto "):
            cmd_value = cmd_value.split(" ", 1)[1].strip()
        cmd_value = cmd_value.replace("\\/", "/")
        # Detect stub: empty host (http:///ch/...) or localhost/ch/...
        is_stub = (
            re.search(r'https?:///ch/', cmd_value) is not None or
            re.search(r'https?://localhost/ch/', cmd_value) is not None
        )
        if is_stub:
            return await self._resolve_stub_url(cmd_value)
        if cmd_value.startswith(("http://", "https://", "rtsp://")):
            return cmd_value
        # Relative path (e.g. /media/7382.mpg) — build full URL.
        # stalker.py derives stream_base_url as {scheme}://{netloc}/vod4
        if cmd_value.startswith("/"):
            from urllib.parse import urlparse as _up
            p = _up(self.base)
            full = f"{p.scheme}://{p.netloc}/vod4{cmd_value}"
            self.log(f"[STALKER] Relative path → {full}")
            return full
        extracted = _extract_url_from_text(cmd_value)
        return extracted or ""

    # ── expose same interface as PortalClient ─────────────────────────────────

    async def fetch_vod_play_link(self, cmd: str) -> str:
        return await self.create_stream_link(cmd, ptype="vod")

    async def create_episode_link(self, cmd: str, call_mode: str = "series") -> str:
        type_map = {"series": "vod", "vod": "vod", "live": "itv"}
        return await self.create_stream_link(cmd, ptype=type_map.get(call_mode, "vod"))

    async def resolve_item_url(self, mode: str, item: dict, category: dict) -> str:
        if mode == "live":
            cmd = item.get("cmd") or item.get("rtsp_url") or ""
            if not cmd:
                return ""
            return await self.create_stream_link(cmd, ptype="itv")

        # Episode item: has _parent_movie_id and _season_id set during drill
        # stalker.py get_episode_stream_url: get_ordered_list(movie_id, season_id, episode_id)
        parent_movie_id = str(item.get("_parent_movie_id") or "").strip()
        season_id = str(item.get("_season_id") or "").strip()
        episode_id = str(item.get("id") or "").strip()

        if parent_movie_id and season_id and episode_id:
            url = self._load_url(type="vod", action="get_ordered_list",
                                 movie_id=parent_movie_id, season_id=season_id,
                                 episode_id=episode_id, JsHttpRequest="1-xml")
            headers = self._headers(include_auth=True)
            self.log(f"[STALKER] episode lookup movie_id={parent_movie_id} season_id={season_id} episode_id={episode_id}")
            async with self.session.get(url, headers=headers) as r:
                payload = await safe_json(r)
            if isinstance(payload, dict):
                js = payload.get("js", {})
                data = js.get("data", []) if isinstance(js, dict) else []
                if data and isinstance(data, list):
                    stream_id = str(data[0].get("id") or "").strip()
                    if stream_id:
                        cmd = f"/media/file_{stream_id}.mpg"
                        self.log(f"[STALKER] create_link stream_id={stream_id}")
                        return await self.create_stream_link(cmd, ptype="vod")

        # Regular VOD/Series: two-step lookup
        movie_id = str(item.get("movie_id") or item.get("id") or "").strip()
        if movie_id:
            url = self._load_url(type="vod", action="get_ordered_list",
                                 movie_id=movie_id, JsHttpRequest="1-xml")
            headers = self._headers(include_auth=True)
            self.log(f"[STALKER] stream lookup movie_id={movie_id} mode={mode}")
            async with self.session.get(url, headers=headers) as r:
                payload = await safe_json(r)
            if isinstance(payload, dict):
                js = payload.get("js", {})
                data = js.get("data", []) if isinstance(js, dict) else []
                if data and isinstance(data, list):
                    stream_id = str(data[0].get("id") or "").strip()
                    if stream_id:
                        cmd = f"/media/file_{stream_id}.mpg"
                        self.log(f"[STALKER] create_link stream_id={stream_id}")
                        return await self.create_stream_link(cmd, ptype="vod")

        # Fallback: use cmd directly
        cmd = item.get("cmd") or item.get("rtsp_url") or ""
        if not cmd:
            return ""
        cmd = cmd.strip()
        if cmd.lower().startswith("ffmpeg "):
            cmd = cmd.split(" ", 1)[1].strip()
        if cmd.lower().startswith("auto "):
            cmd = cmd.split(" ", 1)[1].strip()
        cmd = cmd.replace("\\/", "/")
        if cmd.startswith(("http://", "https://", "rtsp://")):
            is_stub = (re.search(r'https?:///ch/', cmd) or re.search(r'https?://localhost/ch/', cmd))
            if is_stub:
                return await self._resolve_stub_url(cmd)
            return cmd
        return await self.create_stream_link(cmd, ptype="vod")

    async def fetch_episodes_for_show(self, item: dict, cat_title: str):
        series_name = item.get("name") or item.get("o_name") or item.get("fname") or "Unknown"
        cat_id = str(item.get("_cat_id", ""))

        # Season item: has _parent_movie_id set by previous drill
        # stalker.py: fetch_episode_pages(movie_id, season_id) where season_id = it["id"]
        parent_movie_id = str(item.get("_parent_movie_id") or "").strip()
        if parent_movie_id:
            movie_id = parent_movie_id
            season_id = str(item.get("id") or "").strip()
            self.log(f"[STALKER] Fetching episodes for season: {series_name} (movie_id={movie_id} season_id={season_id})")
        else:
            movie_id = str(item.get("id") or item.get("movie_id") or "").strip()
            season_id = ""
            self.log(f"[STALKER] Fetching episodes for: {series_name} (movie_id={movie_id})")

        if not movie_id:
            return []

        all_items = []
        page = 1
        while True:
            params = dict(type="vod", action="get_ordered_list",
                         movie_id=movie_id, JsHttpRequest="1-xml", p=page)
            if season_id:
                params["season_id"] = season_id
                params["episode_id"] = "0"
            if cat_id:
                params["category"] = cat_id
            url = self._load_url(**params)
            headers = self._headers(include_auth=True)
            async with self.session.get(url, headers=headers) as r:
                payload = await safe_json(r)
            items = normalize_js(payload)
            if not items:
                break
            all_items.extend(items)
            if len(items) < 5:
                break
            page += 1

        # If results are season containers (have is_season), mark them drillable
        # with parent movie_id stored so next drill can fetch actual episodes
        if all_items and all_items[0].get("is_season") is not None:
            for it in all_items:
                if isinstance(it, dict):
                    it["_is_show_item"] = True
                    it["_parent_movie_id"] = movie_id
                    it["_cat_id"] = cat_id
        elif season_id:
            # These are actual episodes — stamp parent ids for resolve_item_url
            for it in all_items:
                if isinstance(it, dict):
                    it["_parent_movie_id"] = movie_id
                    it["_season_id"] = season_id

        self.log(f"[STALKER] {series_name}: {len(all_items)} items found")
        # Rewrite logo/screenshot URLs on every returned item (season containers
        # and actual episode rows both suffer from localhost/hostless paths)
        for it in all_items:
            if not isinstance(it, dict):
                continue
            for logo_field in ("logo", "screenshot_uri", "pic"):
                val = it.get(logo_field)
                if val and isinstance(val, str):
                    fixed = self._fix_logo_url(val)
                    if fixed != val:
                        it[logo_field] = fixed
        return all_items

    async def dump_single_item_to_file(self, mode: str, item: dict, category: dict, out_path: str, stop_flag=None):
        # Reuse PortalClient's dump logic by forwarding — same API shape
        cat_title = category.get("title", "Unknown")
        cmd = item.get("cmd") or item.get("rtsp_url") or ""
        name = item.get("name") or item.get("o_name") or "Unknown"
        logo = item.get("logo") or item.get("screenshot_uri") or ""
        tvg_type = "live" if mode == "live" else "movie" if mode == "vod" else "series"
        if not cmd:
            return
        ptype = "itv" if mode == "live" else "vod"
        resolved = await self.create_stream_link(cmd, ptype=ptype)
        if resolved and resolved.startswith(("http://", "https://", "rtsp://")):
            resolved = unquote(resolved)
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(_extinf_line(name, logo, tvg_type, cat_title, item) + f'{resolved}\n')
            self.log(f"[STALKER] ✓ {name}")
        else:
            self.log(f"[STALKER] ✗ Could not resolve: {name}")

    async def dump_category_to_file(self, mode: str, category: dict, out_path: str, append=True, stop_flag=None, progress_cb=None):
        cat_title = category.get("title", "Unknown")
        cat_id = str(category.get("id", ""))
        page = 1
        lines_written = 0
        while True:
            items = await self.fetch_items_page(mode, cat_id, page)
            if not items:
                break
            for it in items:
                if stop_flag and stop_flag.is_set():
                    return
                if not isinstance(it, dict):
                    continue
                await self.dump_single_item_to_file(mode, it, category, out_path, stop_flag)
                lines_written += 1
                if progress_cb: progress_cb(lines_written)
            if len(items) < 5:
                break
            page += 1
        self.log(f"[STALKER] Finished {cat_title} (items: {lines_written})")


# ===================== XTREAM CODES CLIENT =====================

class XtreamClient:
    def __init__(self, base_url: str, username: str, password: str, log_cb):
        self.base = normalize_base_url(base_url)
        self.username = username.strip()
        self.password = password.strip()
        self.log = log_cb
        self.session = None
        # Cache the user_info dict returned by the player_api.php auth response.
        # Both handshake() and account_info() hit the identical URL — storing the
        # result here lets account_info() skip the second round-trip entirely.
        self._cached_user_info: dict | None = None
        # Running logo cache: stream_id (str) → logo URL.
        # Populated during fetch_items_page so items with missing logos can be
        # filled from the cache without extra network calls.
        self._logo_cache: dict = {}

    async def __aenter__(self):
        _timeout = aiohttp.ClientTimeout(total=30, connect=10)
        self.session = aiohttp.ClientSession(timeout=_timeout)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _api(self, action: str, **params) -> str:
        url = f"{self.base}/player_api.php?username={self.username}&password={self.password}&action={action}"
        for k, v in params.items():
            url += f"&{k}={v}"
        return url

    async def handshake(self):
        url = f"{self.base}/player_api.php?username={self.username}&password={self.password}"
        self.log(f"[XTREAM] Connecting → {self.base}")
        async with self.session.get(url) as r:
            self.log(f"[XTREAM] Auth HTTP {r.status}")
            data = await safe_json(r)
        if not isinstance(data, dict):
            raise RuntimeError(f"Xtream: no JSON response (HTTP {r.status})")
        info = data.get("user_info", {})
        if not isinstance(info, dict):
            raise RuntimeError(f"Xtream: unexpected response format")
        if str(info.get("auth", "0")) == "0":
            raise RuntimeError(f"Xtream: authentication failed — wrong username/password")
        # Cache user_info so account_info() can read it without a second request.
        self._cached_user_info = info
        self.log(f"[XTREAM] Auth OK — status: {info.get('status','?')}  expiry: {info.get('exp_date','?')}")
        return info

    async def account_info(self):
        # Re-use the user_info already fetched by handshake() when available.
        # This eliminates the duplicate GET /player_api.php that previously
        # happened whenever handshake() and account_info() were called in sequence.
        if self._cached_user_info is not None:
            info = self._cached_user_info
        else:
            url = f"{self.base}/player_api.php?username={self.username}&password={self.password}"
            async with self.session.get(url) as r:
                data = await safe_json(r)
            if not isinstance(data, dict):
                return (self.username, "unknown")
            info = data.get("user_info", {})
            if not isinstance(info, dict):
                return (self.username, "unknown")
            self._cached_user_info = info
        exp_raw = info.get("exp_date", "")
        exp = "unknown"
        try:
            if exp_raw and str(exp_raw).isdigit():
                exp = datetime.fromtimestamp(int(exp_raw)).strftime("%Y-%m-%d")
            else:
                exp = str(exp_raw)
        except Exception:
            exp = str(exp_raw)
        max_conn_raw = info.get("max_connections", None)
        max_conn_int = 0
        try:
            if max_conn_raw is not None:
                max_conn_int = int(max_conn_raw)
        except Exception:
            pass
        active = info.get("active_cons", "?")
        status = info.get("status", "?")
        self.log(f"[XTREAM] Account: user={self.username}  status={status}  expiry={exp}  connections={active}/{max_conn_raw}")
        return (self.username, exp, max_conn_int)

    async def fetch_categories(self, mode: str):
        action_map = {"live": "get_live_categories", "vod": "get_vod_categories", "series": "get_series_categories"}
        url = self._api(action_map.get(mode, "get_live_categories"))
        self.log(f"[XTREAM] Fetching {mode.upper()} categories…")
        async with self.session.get(url) as r:
            self.log(f"[XTREAM] Categories HTTP {r.status} ({mode.upper()})")
            data = await safe_json(r)
        if not isinstance(data, list):
            return []
        cats = []
        for c in data:
            if not isinstance(c, dict):
                continue
            cid = c.get("category_id")
            cname = c.get("category_name", "Unknown")
            if cid:
                cats.append({"id": str(cid), "title": cname})
        self.log(f"[XTREAM] {mode.upper()} categories: {len(cats)} found")
        return cats

    async def fetch_items_page(self, mode: str, cat_id: str, page: int):
        if page > 1:
            return []
        action_map = {"live": "get_live_streams", "vod": "get_vod_streams", "series": "get_series"}
        url = self._api(action_map.get(mode, "get_live_streams"), category_id=cat_id)
        self.log(f"[XTREAM] Fetching {mode.upper()} streams cat={cat_id}…")
        async with self.session.get(url) as r:
            data = await safe_json(r)
        if not isinstance(data, list):
            return []
        if mode == "series":
            for it in data:
                if isinstance(it, dict):
                    it["_is_show_item"] = True
        self.log(f"[XTREAM] {mode.upper()} cat={cat_id}: {len(data)} items")

        # ── Logo caching ─────────────────────────────────────────────────────
        # Pass 1: populate cache from items that carry a logo.
        for it in data:
            if not isinstance(it, dict):
                continue
            sid = str(it.get("stream_id") or it.get("series_id") or it.get("id") or "").strip()
            logo = self._item_logo(it)
            if sid and logo:
                self._logo_cache[sid] = logo
        # Pass 2: fill blanks from cache (covers cross-category duplicates).
        for it in data:
            if not isinstance(it, dict):
                continue
            if not self._item_logo(it):
                sid = str(it.get("stream_id") or it.get("series_id") or it.get("id") or "").strip()
                cached = self._logo_cache.get(sid, "")
                if cached:
                    it["stream_icon"] = cached

        return data

    def _stream_url(self, mode: str, item: dict) -> str:
        if mode == "live":
            sid = item.get("stream_id", "")
            return f"{self.base}/live/{self.username}/{self.password}/{sid}.m3u8"
        elif mode == "vod":
            sid = item.get("stream_id", "")
            ext = item.get("container_extension", "mp4")
            return f"{self.base}/movie/{self.username}/{self.password}/{sid}.{ext}"
        return ""

    async def _fetch_series_info(self, series_id) -> dict:
        url = self._api("get_series_info", series_id=series_id)
        self.log(f"[XTREAM] Fetching series info id={series_id}…")
        async with self.session.get(url) as r:
            data = await safe_json(r)
        if not isinstance(data, dict):
            return {}
        ep_count = sum(len(v) for v in data.get("episodes", {}).values())
        self.log(f"[XTREAM] Series id={series_id}: {len(data.get('episodes', {}))} season(s), {ep_count} episodes")
        return data

    def _item_name(self, item: dict) -> str:
        return item.get("name") or item.get("title") or item.get("stream_name") or "Unknown"

    def _item_logo(self, item: dict) -> str:
        return item.get("stream_icon") or item.get("cover") or item.get("logo") or ""

    async def fetch_episodes_for_show(self, item: dict, cat_title: str):
        series_id = item.get("series_id") or item.get("id")
        series_name = self._item_name(item)
        series_logo = self._item_logo(item)
        self.log(f"[SERIES] Fetching info for: {series_name}")
        info = await self._fetch_series_info(series_id)
        if not info:
            return []
        episodes_by_season = info.get("episodes", {})
        result = []
        for season_num_str, ep_list in sorted(episodes_by_season.items(),
                                               key=lambda x: int(x[0]) if x[0].isdigit() else 0):
            for ep in ep_list:
                if not isinstance(ep, dict):
                    continue
                ep_id = ep.get("id")
                ep_num = ep.get("episode_num", "?")
                ext = ep.get("container_extension", "mkv")
                url = f"{self.base}/series/{self.username}/{self.password}/{ep_id}.{ext}"
                sn = season_num_str.zfill(2)
                en = str(ep_num).zfill(2)
                full_name = f"{series_name} S{sn}E{en}"
                ep_title = ep.get("title", "")
                if ep_title:
                    full_name = f"{full_name} — {ep_title}"
                result.append({
                    "name": full_name,
                    "logo": series_logo,
                    "_direct_url": url,
                    "_cat_title": cat_title,
                    "tvg_type": "series",
                })
        self.log(f"[SERIES] {series_name}: {len(result)} episodes")
        return result

    async def resolve_item_url(self, mode: str, item: dict, category: dict) -> str:
        if item.get("_direct_url"):
            return item["_direct_url"]
        if mode in ("live", "vod"):
            return self._stream_url(mode, item)
        return ""

    async def dump_single_item_to_file(self, mode: str, item: dict, category: dict, out_path: str, stop_flag=None):
        cat_title = category.get("title", "Unknown")
        if item.get("_direct_url"):
            ep_name = item.get("name", "Unknown")
            ep_logo = item.get("logo", "")
            ep_cat = item.get("_cat_title") or cat_title
            ep_url = item["_direct_url"]
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(_extinf_line(ep_name, ep_logo, 'series', ep_cat) + f'{ep_url}\n')
            self.log(f"[SERIES] ✓ {ep_name}")
            return
        if mode == "series":
            series_id = item.get("series_id") or item.get("id")
            series_name = self._item_name(item)
            series_logo = self._item_logo(item)
            info = await self._fetch_series_info(series_id)
            if not info:
                return
            episodes = info.get("episodes", {})
            with open(out_path, "a", encoding="utf-8") as f:
                for season_num_str, ep_list in sorted(episodes.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
                    for ep in ep_list:
                        if stop_flag and stop_flag.is_set():
                            return
                        if not isinstance(ep, dict):
                            continue
                        ep_id = ep.get("id")
                        ep_num = ep.get("episode_num", "?")
                        ext = ep.get("container_extension", "mkv")
                        url = f"{self.base}/series/{self.username}/{self.password}/{ep_id}.{ext}"
                        sn = season_num_str.zfill(2)
                        en = str(ep_num).zfill(2)
                        full_name = f"{series_name} S{sn}E{en}"
                        f.write(_extinf_line(full_name, series_logo, 'series', cat_title, item) + f'{url}\n')
            self.log(f"[SERIES] ✓ Done: {series_name}")
        else:
            name = self._item_name(item)
            logo = self._item_logo(item)
            url = self._stream_url(mode, item)
            if not url:
                return
            tvg_type = "live" if mode == "live" else "movie"
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(_extinf_line(name, logo, tvg_type, cat_title, item) + f'{url}\n')
            self.log(f"✓ Wrote: {name}")

    async def dump_category_to_file(self, mode: str, category: dict, out_path: str, append=True, stop_flag=None, progress_cb=None):
        cat_title = category.get("title", "Unknown")
        cat_id = str(category.get("id", ""))
        self.log(f"[XTREAM] Downloading {mode.upper()} → {cat_title}")
        items = await self.fetch_items_page(mode, cat_id, 1)
        count = 0
        if mode == "series":
            for item in items:
                if stop_flag and stop_flag.is_set():
                    break
                await self.dump_single_item_to_file(mode, item, category, out_path, stop_flag)
                count += 1
        else:
            tvg_type = "live" if mode == "live" else "movie"
            with open(out_path, "a", encoding="utf-8") as f:
                for item in items:
                    if stop_flag and stop_flag.is_set():
                        break
                    if not isinstance(item, dict):
                        continue
                    name = self._item_name(item)
                    logo = self._item_logo(item)
                    url = self._stream_url(mode, item)
                    if not url:
                        continue
                    f.write(_extinf_line(name, logo, tvg_type, cat_title, item) + f'{url}\n')
                    count += 1
        self.log(f"[XTREAM] Finished {cat_title} (items: {count})")


# ===================== M3U URL CLIENT =====================

_SERIES_SXEX_RE = re.compile(r'^(.*?)\s+[Ss](\d+)\s*[Ee](\d+)', re.DOTALL)
_SERIES_NxN_RE = re.compile(r'^(.*?)\s+(\d+)[xX](\d+)')
_SERIES_EP_STRIP_RE = re.compile(
    r'\s+(?:[Ss]\d+\s*[Ee]\d+|[Ss]eason\s*\d+|[Ee]pisode\s*\d+|\d+[xX]\d+).*$',
    re.IGNORECASE | re.DOTALL
)


def _extract_series_name(ep_name: str) -> str:
    m = _SERIES_SXEX_RE.match(ep_name)
    if m:
        return m.group(1).strip()
    m = _SERIES_NxN_RE.match(ep_name)
    if m:
        return m.group(1).strip()
    cleaned = _SERIES_EP_STRIP_RE.sub("", ep_name).strip()
    if cleaned and cleaned != ep_name:
        return cleaned
    return ep_name


class M3UClient:
    def __init__(self, m3u_url: str, log_cb, preloaded=None):
        self.m3u_url = m3u_url.strip()
        self.log = log_cb
        self.session = None
        self._all_groups = preloaded or {}
        self._xtream_creds = extract_xtream_from_m3u_url(m3u_url)
        self._xtream_client = None
        self._tvg_url = ""

    async def __aenter__(self):
        _timeout = aiohttp.ClientTimeout(total=300, connect=20, sock_read=None)
        connector = aiohttp.TCPConnector(ssl=False)
        self.session = aiohttp.ClientSession(timeout=_timeout, connector=connector)
        if self._xtream_creds:
            creds = self._xtream_creds
            self._xtream_client = XtreamClient(creds["base"], creds["username"], creds["password"], self.log)
            self._xtream_client.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30, connect=10))
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
        if self._xtream_client and self._xtream_client.session:
            await self._xtream_client.session.close()

    async def handshake(self):
        if self._all_groups:
            if self._xtream_client:
                try:
                    await self._xtream_client.handshake()
                    self.log("[M3U] ✓ Xtream API handshake succeeded")
                except Exception as e:
                    self.log(f"[M3U] Xtream handshake failed: {e}")
                    self._xtream_client = None
            return True

        self.log(f"[M3U] Fetching playlist from: {self.m3u_url}")

        if self._xtream_client:
            try:
                await self._xtream_client.handshake()
                self.log("[M3U] ✓ Xtream API credentials detected and authenticated")
            except Exception as e:
                self.log(f"[M3U] Xtream handshake failed: {e}")
                self._xtream_client = None

        headers = {"User-Agent": "VLC/3.0.0 LibVLC/3.0.0", "Accept": "*/*"}
        MAX_MB = 520

        try:
            async with self.session.get(self.m3u_url, headers=headers,
                                        allow_redirects=True, max_redirects=10) as r:
                self.log(f"[M3U] HTTP {r.status}")
                if r.status != 200:
                    body_preview = await r.text(errors="replace")
                    raise RuntimeError(f"M3U fetch failed: HTTP {r.status}\n{body_preview[:300]}")

                chunks = []
                bytes_received = 0
                last_logged_mb = 0
                async for chunk in r.content.iter_chunked(1024 * 256):
                    chunks.append(chunk)
                    bytes_received += len(chunk)
                    current_mb = bytes_received // (1024 * 1024)
                    if current_mb >= last_logged_mb + 10:
                        last_logged_mb = current_mb
                        self.log(f"[M3U] Downloaded {current_mb} MB…")
                    if current_mb >= MAX_MB:
                        self.log(f"[M3U] ⚠ Reached {MAX_MB} MB limit — truncating")
                        break

                raw = b"".join(chunks).decode("utf-8", errors="replace")
        except Exception as e:
            raise RuntimeError(f"M3U fetch error: {e}")

        self.log(f"[M3U] Parsing {len(raw) // 1024} KB…")
        self._parse_m3u(raw)
        self.log(f"[M3U] Parsed — {len(self._all_groups)} groups")
        return True

    def _parse_m3u(self, raw: str):
        groups: dict = {}
        lines = raw.splitlines()
        i = 0
        # Cache tvg-url from #EXTM3U header for EPG fallback
        if lines and lines[0].startswith("#EXTM3U"):
            m = re.search(r'(?:url-tvg|x-tvg-url)="([^"]*)"', lines[0], re.IGNORECASE)
            if m:
                self._tvg_url = m.group(1).strip()
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXTINF"):
                info_line = line
                url_line = ""
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].strip()
                    if next_line and not next_line.startswith("#"):
                        url_line = next_line
                        i = j
                        break
                    elif next_line.startswith("#EXTINF"):
                        i = j - 1
                        break
                    j += 1

                if not url_line:
                    i += 1
                    continue

                attrs = {}
                m = re.search(r'tvg-name="([^"]*)"', info_line)
                if m:
                    attrs["tvg_name"] = m.group(1)
                m = re.search(r'tvg-id="([^"]*)"', info_line)
                if m:
                    attrs["tvg_id"] = m.group(1)
                m = re.search(r'tvg-logo="([^"]*)"', info_line)
                if m:
                    attrs["tvg_logo"] = m.group(1)
                m = re.search(r'group-title="([^"]*)"', info_line)
                if m:
                    attrs["group_title"] = m.group(1)
                m = re.search(r'tvg-type="([^"]*)"', info_line)
                if m:
                    attrs["tvg_type"] = m.group(1).lower()

                comma_idx = info_line.rfind(",")
                display_name = info_line[comma_idx + 1:].strip() if comma_idx != -1 else ""
                name = attrs.get("tvg_name") or display_name or "Unknown"
                group = attrs.get("group_title") or "Uncategorized"
                logo = attrs.get("tvg_logo") or ""
                tvg_type = attrs.get("tvg_type") or ""
                tvg_id = attrs.get("tvg_id") or ""

                if not tvg_type:
                    url_lower = url_line.lower()
                    if "/series/" in url_lower or "/episode/" in url_lower:
                        tvg_type = "series"
                    elif "/movie/" in url_lower:
                        tvg_type = "movie"
                    else:
                        tvg_type = "live"

                entry = {"name": name, "logo": logo, "_url": url_line, "tvg_type": tvg_type, "tvg_id": tvg_id}
                groups.setdefault(group, []).append(entry)

            i += 1

        # Group series by show name
        processed_groups = {}
        for group_name, items in groups.items():
            series_items = [it for it in items if it.get("tvg_type") in ("series", "episode")]
            other_items = [it for it in items if it not in series_items]

            if series_items:
                shows: dict = {}
                for ep in series_items:
                    ep_name = ep.get("name", "")
                    show_name = _extract_series_name(ep_name)
                    if show_name not in shows:
                        shows[show_name] = {"name": show_name, "logo": ep.get("logo", ""),
                                            "_is_series_group": True, "_episodes": [], "tvg_type": "series"}
                    shows[show_name]["_episodes"].append(ep)
                other_items.extend(shows.values())

            processed_groups[group_name] = other_items

        self._all_groups = processed_groups

    def _type_filter(self, mode: str):
        if mode == "live":
            return {"live", ""}
        elif mode == "vod":
            return {"movie", "vod"}
        else:
            return {"series", "episode"}

    async def account_info(self):
        if self._xtream_client:
            try:
                result = await self._xtream_client.account_info()
                # XtreamClient.account_info() returns (ident, exp, max_conn) — pass through
                return result
            except Exception:
                pass
        return ("M3U", "loaded", 0)

    async def fetch_categories(self, mode: str):
        if self._xtream_client:
            try:
                cats = await self._xtream_client.fetch_categories(mode)
                if cats:
                    for c in cats:
                        c["_xtream_fallback"] = True
                    return cats
            except Exception as e:
                self.log(f"[M3U] Xtream categories fallback failed: {e}")

        type_filter = self._type_filter(mode)
        seen = set()
        cats = []
        for group_name, items in self._all_groups.items():
            has_match = any(it.get("tvg_type", "") in type_filter
                            or (mode == "live" and it.get("tvg_type", "") == "")
                            for it in items)
            if has_match and group_name not in seen:
                seen.add(group_name)
                cats.append({"id": group_name, "title": group_name})

        self.log(f"[M3U] {mode.upper()} categories: {len(cats)} found")
        return cats

    async def fetch_items_page(self, mode: str, cat_id: str, page: int):
        if self._xtream_client:
            try:
                real_cat = {"id": cat_id, "title": cat_id}
                items = await self._xtream_client.fetch_items_page(mode, cat_id, page)
                if items:
                    return items
            except Exception:
                pass

        if page > 1:
            return []
        type_filter = self._type_filter(mode)
        raw_items = self._all_groups.get(cat_id, [])
        if mode == "series":
            return [i for i in raw_items if i.get("tvg_type", "") in type_filter
                    or i.get("_is_series_group")]
        filtered = [i for i in raw_items if i.get("tvg_type", "") in type_filter]
        if not filtered and mode == "live":
            filtered = raw_items
        return filtered

    async def fetch_episodes_for_show(self, item: dict, cat_title: str):
        if self._xtream_client and item.get("_is_show_item"):
            try:
                return await self._xtream_client.fetch_episodes_for_show(item, cat_title)
            except Exception as e:
                self.log(f"[M3U] Xtream episodes fallback failed: {e}")
        if item.get("_is_series_group"):
            return item.get("_episodes", [])
        return []

    async def resolve_item_url(self, mode: str, item: dict, category: dict) -> str:
        if self._xtream_client and (item.get("_is_show_item") or item.get("_direct_url")):
            return await self._xtream_client.resolve_item_url(mode, item, category)
        return item.get("_url") or item.get("_direct_url") or ""

    async def dump_single_item_to_file(self, mode: str, item: dict, category: dict, out_path: str, stop_flag=None):
        cat_title = category.get("title", "Unknown")
        if item.get("_is_series_group"):
            episodes = item.get("_episodes", [])
            with open(out_path, "a", encoding="utf-8") as f:
                for ep in episodes:
                    if stop_flag and stop_flag.is_set():
                        return
                    name = ep.get("name", "Unknown")
                    logo = ep.get("logo", "")
                    url = ep.get("_url", "")
                    if not url:
                        continue
                    f.write(_extinf_line(name, logo, 'series', cat_title, ep) + f'{url}\n')
            return
        name = item.get("name", "Unknown")
        logo = item.get("logo", "")
        url = item.get("_url", "")
        tvg_type = item.get("tvg_type") or ("live" if mode == "live" else "movie")
        if not url:
            return
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(_extinf_line(name, logo, tvg_type, cat_title, item) + f'{url}\n')
        self.log(f"✓ Wrote: {name}")

    async def dump_category_to_file(self, mode: str, category: dict, out_path: str, append=True, stop_flag=None, progress_cb=None):
        cat_title = category.get("title", "Unknown")
        cat_id = str(category.get("id", ""))
        if category.get("_xtream_fallback") and self._xtream_client:
            cat_copy = dict(category)
            cat_copy.pop("_xtream_fallback", None)
            await self._xtream_client.dump_category_to_file(mode, cat_copy, out_path, append, stop_flag)
            return
        type_filter = self._type_filter(mode)
        raw_items = self._all_groups.get(cat_id, [])
        if not raw_items and self._xtream_client:
            await self._xtream_client.dump_category_to_file(mode, category, out_path, append, stop_flag)
            return
        if mode == "series":
            # For series groups, iterate episodes inside each show group
            count = 0
            with open(out_path, "a", encoding="utf-8") as f:
                for item in raw_items:
                    if item.get("_is_series_group"):
                        show_name = item.get("name", "Unknown")
                        for ep in item.get("_episodes", []):
                            if stop_flag and stop_flag.is_set():
                                break
                            name = ep.get("name", show_name)
                            logo = ep.get("logo", "")
                            url = ep.get("_url", "")
                            if not url:
                                continue
                            f.write(_extinf_line(name, logo, 'series', cat_title, ep) + f'{url}\n')
                            count += 1
                            if progress_cb:
                                try: progress_cb(count, name)
                                except TypeError: progress_cb(count)
                    elif item.get("tvg_type", "") in type_filter:
                        if stop_flag and stop_flag.is_set():
                            break
                        name = item.get("name", "Unknown")
                        logo = item.get("logo", "")
                        url = item.get("_url", "")
                        if not url:
                            continue
                        f.write(_extinf_line(name, logo, 'series', cat_title, item) + f'{url}\n')
                        count += 1
                        if progress_cb:
                            try: progress_cb(count, name)
                            except TypeError: progress_cb(count)
            self.log(f"[M3U] Finished {cat_title} (items: {count})")
            return
        filtered = [i for i in raw_items if i.get("tvg_type", "") in type_filter]
        if not filtered and mode == "live":
            filtered = raw_items
        count = 0
        with open(out_path, "a", encoding="utf-8") as f:
            for item in filtered:
                if stop_flag and stop_flag.is_set():
                    break
                name = item.get("name", "Unknown")
                logo = item.get("logo", "")
                url = item.get("_url", "")
                tvg_type = item.get("tvg_type") or ("live" if mode == "live" else "movie")
                if not url:
                    continue
                f.write(_extinf_line(name, logo, tvg_type, cat_title, item) + f'{url}\n')
                count += 1
                if progress_cb:
                    try: progress_cb(count, name)
                    except TypeError: progress_cb(count)
        self.log(f"[M3U] Finished {cat_title} (items: {count})")


# ===================== GLOBAL APP STATE =====================

class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.conn_type = "mac"
        self.url = ""
        self.mac = ""
        self.username = ""
        self.password = ""
        self.m3u_url = ""
        self.ext_epg_url = ""  # User-supplied external XMLTV EPG URL (overrides portal's own)
        self.connected = False
        self.is_stalker_portal = False  # True when URL contains 'stalker_portal'
        self.cats_cache: dict = {}
        self.m3u_cache = None
        self.m3u_is_local = False
        self.m3u_xtream_override = None
        self.stop_flag = threading.Event()
        self.log_queue: queue.Queue = queue.Queue(maxsize=2000)
        self.busy = False
        self.status = "Not connected."
        self.worker_thread = None
        self.active_loop = None
        self.active_task = None
        self.mkv_proc = None
        self.mkv_proc_lock = threading.Lock()
        self.recording = False
        self.record_proc = None
        self.record_proc_lock = threading.Lock()
        self.record_start_time = 0.0
        self.record_file_path = ""
        self.mkv_folder = ""
        self.mkv_fallback = True
        # EPG cache: key → (timestamp, result_dict), TTL = 30 minutes
        self._epg_cache: dict = {}
        self._epg_cache_ttl = 1200  # seconds (20 min)
        # Per-portal flag: set of base_urls where get_short_epg always returns empty.
        # After one confirmed empty response we skip straight to XMLTV for that portal.
        self._short_epg_broken: set = set()
        # XMLTV cache: key=base_norm → (fetched_ts, epg_dict, chan_names)
        # epg_dict: {channel_id_lower: [(title, start, end, desc), ...]}  ← compact tuples
        # TTL = 1 hour, same as reference app
        self._xmltv_cache: dict = {}
        self._xmltv_cache_ttl = 1800  # 30 min — matches the -4h/+20h window; no benefit caching longer
        # Portals whose xmltv.php has channel defs but zero programme entries —
        # marked after first download so we never re-download this session.
        self._xmltv_no_data: set = set()
        # Per-URL download state:
        #   _xmltv_dl_locks: url → threading.Lock()  (one download at a time)
        #   _xmltv_dl_events: url → threading.Event()  (set when download done)
        #   _xmltv_downloading: set of urls currently being downloaded
        # Callers acquire the event (wait with timeout) instead of the lock,
        # so they never block the Flask worker — they retry after the event fires.
        self._xmltv_dl_locks: dict = {}      # url → threading.Lock()
        self._xmltv_dl_events: dict = {}     # url → threading.Event()
        self._xmltv_downloading: set = set() # urls currently in-flight
        self._xmltv_needs: set = set()       # cache_keys confirmed to need XMLTV (no portal data)
        # Persistent StalkerPortalClient — reused across requests to avoid
        # repeated handshake/profile calls that cause portal rate-limiting
        self._stalker_client: object = None
        self._stalker_client_lock = threading.Lock()
        # ── Persistent logo caches ─────────────────────────────────────────
        # These survive across _make_client() calls (client instances are
        # short-lived — created and destroyed per request — so any cache on
        # the client object is useless across requests).
        #
        # _logo_cache_live: {ch_id: logo_url} for live channels.
        #   None  = get_all_channels not yet attempted this session.
        #   dict  = already fetched (may be empty if portal returned nothing).
        # _logo_cache_vod: {item_id: logo_url} for VOD / series / Xtream.
        #   Built lazily from items that arrive with logos; zero extra requests.
        self._logo_cache_live: dict | None = None
        self._logo_cache_vod: dict = {}
        # Cache for all-channels list used by What's on Now → Find Channel
        # (timestamp, [{"name", "cmd"/"stream_id"/"url", "tvg_id", ...}])
        self._won_ch_cache: tuple = (0.0, [])
        self._won_ch_cache_ttl = 1200  # 20 minutes
        # Download/export progress tracking (polled via /api/status)
        self.task_type       = ""   # "m3u" | "mkv" | ""
        self.task_label      = ""   # current item name
        self.task_item_names: list = []   # names of all items in the current download job
        self.task_total   = 0    # total items (item counter)
        self.task_done    = 0    # items completed/written
        self.task_skipped = 0    # items skipped (no URL / failed to resolve)
        # Per-file MKV download progress (from ffmpeg stderr)
        self.task_file_pct      = 0.0   # 0-100 % of current file
        self.task_file_elapsed  = ""    # "00:12:34" elapsed in current file
        self.task_speed         = ""    # "2.4 MB/s" or "512 KB/s"
        self.task_file_duration = 0.0   # probed duration of current file (seconds)

    def log(self, msg: str):
        try:
            self.log_queue.put_nowait(str(msg).rstrip())
        except queue.Full:
            pass

    def set_status(self, msg: str):
        self.status = msg
        self.log(f"[STATUS] {msg}")


state = AppState()
if _CAST_AVAILABLE:
    get_cast_proxy().start()

# ===================== ASYNC HELPERS =====================

@contextlib.asynccontextmanager
async def _make_client(do_handshake=True):
    conn = state.conn_type
    if conn == "xtream":
        client = XtreamClient(state.url, state.username, state.password, state.log)
        # _logo_cache is a plain dict — share the same object so mutations
        # (new entries added during this request) survive after the client exits.
        client._logo_cache = state._logo_cache_vod
        async with client:
            if do_handshake:
                await client.handshake()
            yield client
        # dict is shared by reference; no sync needed
    elif conn == "m3u_url":
        if state.m3u_xtream_override:
            creds = state.m3u_xtream_override
            client = XtreamClient(creds["base"], creds["username"], creds["password"], state.log)
            client._logo_cache = state._logo_cache_vod
            async with client:
                if do_handshake:
                    await client.handshake()
                yield client
        else:
            client = M3UClient(state.m3u_url, state.log, preloaded=state.m3u_cache)
            async with client:
                if do_handshake:
                    await client.handshake()
                    state.m3u_cache = dict(client._all_groups)
                yield client
    else:  # mac
        if state.is_stalker_portal:
            client = StalkerPortalClient(state.url, state.mac, state.log)
        else:
            client = PortalClient(state.url, state.mac, state.log)
        # Inject both caches from AppState so this request can read what
        # previous requests already discovered.
        # _ch_logo_cache may be None (not yet fetched) or a dict — assign directly.
        # _vod_logo_cache is always a dict — share by reference.
        client._ch_logo_cache = state._logo_cache_live
        client._vod_logo_cache = state._logo_cache_vod
        async with client:
            if do_handshake:
                await client.handshake()
            yield client
        # _ch_logo_cache may have been populated (None → dict) this request —
        # sync it back so the next request starts with the filled dict.
        state._logo_cache_live = client._ch_logo_cache
        # _vod_logo_cache is a shared dict object; no re-assignment needed.


def run_async(coro):
    """Run an async coroutine from sync context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def run_worker(coro, on_done=None):
    """Run an async coroutine in a background thread."""
    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        state.active_loop = loop
        task = loop.create_task(coro)
        state.active_task = task
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            state.log("Operation cancelled.")
        except Exception as e:
            state.log(f"ERROR: {e}")
        finally:
            loop.close()
            state.active_loop = None
            state.active_task = None
            state.busy = False
            if on_done:
                on_done()
    t = threading.Thread(target=worker, daemon=True)
    state.worker_thread = t
    state.busy = True
    t.start()


# ===================== CONNECT LOGIC =====================

async def _connect_async():
    conn = state.conn_type

    if conn == "m3u_url":
        m3u_url = state.m3u_url

        # Local file: already parsed — build cats directly, no network
        if state.m3u_is_local and state.m3u_cache:
            type_map = {"live": {"live", ""}, "vod": {"movie", "vod"}, "series": {"series", "episode"}}
            for m in ("live", "vod", "series"):
                tf = type_map[m]
                seen, cats = set(), []
                for gname, items in state.m3u_cache.items():
                    if any(it.get("tvg_type","") in tf or (m=="live" and it.get("tvg_type","")=="") for it in items):
                        if gname not in seen:
                            seen.add(gname)
                            cats.append({"id": gname, "title": gname})
                state.cats_cache[m] = cats
                state.log(f"[CONNECT] {m.upper()}: {len(cats)} categories")
            state.connected = True
            fname = m3u_url or "local file"
            state.set_status(f"Connected (local M3U): {fname}")
            return {"success": True, "categories": state.cats_cache,
                    "ident": "Local M3U", "exp": fname, "is_stalker": False}

        detected = extract_xtream_from_m3u_url(m3u_url)
        if detected:
            state.log(f"[CONNECT] Xtream credentials detected in M3U URL — trying Xtream API first")
            try:
                xt = XtreamClient(detected["base"], detected["username"], detected["password"], state.log)
                async with xt:
                    await xt.handshake()
                    ident, exp, max_conn = await xt.account_info()
                    state.m3u_xtream_override = detected
                    state.log(f"[CONNECT] ✓ Xtream API connected: {ident} | {exp}")
                    for m in ("live", "vod", "series"):
                        try:
                            cats = await xt.fetch_categories(m)
                            state.cats_cache[m] = cats
                            state.log(f"[CONNECT] {m.upper()}: {len(cats)} categories")
                        except Exception as e:
                            state.log(f"[CONNECT] ✗ {m.upper()} categories: {e}")
                            state.cats_cache[m] = []
                    state.connected = True
                    state.set_status(f"Connected (Xtream via M3U): {ident} | {exp}")
                    return {"success": True, "categories": state.cats_cache, "ident": ident, "exp": exp,
                            "max_connections": max_conn, "portal_url": detected["base"],
                            "is_stalker": False}
            except Exception as e:
                state.log(f"[CONNECT] Xtream failed ({e}) — falling back to M3U download…")
                state.m3u_xtream_override = None

        # Pure M3U
        state.m3u_xtream_override = None
        client = M3UClient(m3u_url, state.log)
        async with client:
            await client.handshake()
            state.m3u_cache = dict(client._all_groups)
            state._tvg_url_cache = client._tvg_url
            _ai3 = await client.account_info()
            ident, exp = _ai3[0], _ai3[1]
            max_conn = _ai3[2] if len(_ai3) > 2 else 0
            state.log(f"[CONNECT] ✓ Connected: {ident} | {exp}")
            for m in ("live", "vod", "series"):
                tmp = M3UClient(m3u_url, state.log, preloaded=state.m3u_cache)
                async with tmp:
                    state.cats_cache[m] = await tmp.fetch_categories(m)
                    state.log(f"[CONNECT] {m.upper()}: {len(state.cats_cache[m])} categories")
        state.connected = True
        state.set_status(f"Connected: {ident} | {exp}")
        return {"success": True, "categories": state.cats_cache, "ident": ident, "exp": exp,
                "max_connections": max_conn, "portal_url": state.m3u_url,
                "is_stalker": state.is_stalker_portal}

    # MAC / Xtream
    if state.is_stalker_portal:
        state.log("[CONNECT] 🔌 Stalker portal detected — using StalkerPortalClient (/stalker_portal/server/load.php)")
    async with _make_client() as client:
        _ai4 = await client.account_info()
        ident, exp = _ai4[0], _ai4[1]
        max_conn = _ai4[2] if len(_ai4) > 2 else 0
        state.log(f"[CONNECT] ✓ Connected: {ident} | {exp}")
        for m in ("live", "vod", "series"):
            try:
                extra = await client.fetch_categories(m)
                state.cats_cache[m] = extra
                state.log(f"[CONNECT] {m.upper()}: {len(extra)} categories")
            except Exception as e:
                state.log(f"[CONNECT] ✗ Could not load {m.upper()} categories: {e}")
                state.cats_cache[m] = []
    state.connected = True
    state.set_status(f"Connected: {ident} | {exp}")
    return {"success": True, "categories": state.cats_cache, "ident": ident, "exp": exp,
            "max_connections": max_conn, "portal_url": state.url or state.m3u_url,
            "is_stalker": state.is_stalker_portal}


# ===================== FLASK APP =====================

flask_app = Flask(__name__)
flask_app.config["SECRET_KEY"] = os.urandom(24)
if _CAST_AVAILABLE:
    register_cast_routes(flask_app, state, run_async, _make_client)
if _MULTIVIEW_AVAILABLE:
    register_multiview_routes(flask_app)

@flask_app.route('/api/multiview/available')
def multiview_available():
    """Probe endpoint — returns 200 if multiview_addon is loaded, 404 if not.
    Mirrors the cast_addon pattern: the JS checks this on load and hides the
    multiview buttons if the addon is not present."""
    if _MULTIVIEW_AVAILABLE:
        return '', 200
    return '', 404

# NOTE: Do NOT use a shared requests.Session for /api/proxy.
# HLS.js downloads multiple fragments in parallel — each hits Flask in its own
# thread. A shared Session is not thread-safe for concurrent use and causes
# race conditions on the connection pool. Plain requests.get() (which creates
# a disposable Session per call) is the correct choice here.


@flask_app.route("/")
def index():
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    ffprobe_ok = shutil.which("ffprobe") is not None
    config = json.dumps({
        "ffmpeg_ok": ffmpeg_ok,
        "ffprobe_ok": ffprobe_ok,
        "ytdlp_ok": YTDLP_AVAILABLE,
    })
    tags = []
    tags.append('<span class="tag tag-ok">✓ ffmpeg</span>' if ffmpeg_ok else '<span class="tag tag-err">✗ ffmpeg</span>')
    if not ffprobe_ok:
        tags.append('<span class="tag tag-warn">✗ ffprobe</span>')
    if YTDLP_AVAILABLE:
        tags.append('<span class="tag tag-ok">✓ yt-dlp</span>')
    tags_html = "".join(tags)
    return render_template_string(HTML_TEMPLATE, config=config, tags_html=tags_html)


@flask_app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.get_json(force=True)
    with state.lock:
        state.conn_type = data.get("conn_type", "mac")
        state.url = data.get("url", "").strip()
        state.mac = data.get("mac", "").strip().upper()
        state.username = data.get("username", "").strip()
        state.password = data.get("password", "").strip()
        state.m3u_url = data.get("m3u_url", "").strip()
        state.ext_epg_url = data.get("ext_epg_url", "").strip()
        state.is_stalker_portal = (
            state.conn_type == "mac" and
            "stalker_portal" in state.url.lower()
        )
        state.cats_cache = {}
        state.m3u_cache = None
        state.m3u_is_local = False
        state.m3u_xtream_override = None
        state._epg_cache = {}
        state._xmltv_cache = {}
        state._xmltv_dl_locks = {}
        state._xmltv_dl_events = {}
        state._xmltv_downloading = set()
        state._xmltv_needs = set()
        state._short_epg_broken = set()
        state._xmltv_no_data = set()
        state._won_ch_cache = (0.0, [])
        state.connected = False
        state.stop_flag.clear()
        # Reset logo caches so a new portal starts fresh
        state._logo_cache_live = None
        state._logo_cache_vod = {}
        # Local M3U file: pre-parse content, set flag so _connect_async skips network
        m3u_content = data.get("m3u_content", "").strip()
        if m3u_content and state.conn_type == "m3u_url":
            try:
                _tmp = M3UClient("local_file", state.log)
                _tmp._parse_m3u(m3u_content)
                state.m3u_cache = dict(_tmp._all_groups)
                state.m3u_is_local = True
                state.log(f"[CONNECT] Local M3U parsed — {len(state.m3u_cache)} groups")
            except Exception as _e:
                state.log(f"[CONNECT] Local M3U parse error: {_e}")
                state.m3u_cache = None
                state.m3u_is_local = False

    try:
        result = run_async(_connect_async())
        return jsonify(result)
    except Exception as e:
        state.log(f"[CONNECT] Error: {e}")
        return jsonify({"success": False, "error": str(e), "categories": {}, "ident": "", "exp": ""})


@flask_app.route("/api/categories", methods=["GET"])
def api_categories():
    mode = request.args.get("mode", "live"); mode = mode if mode in ("live","vod","series") else "live"
    if not state.connected:
        return jsonify({"error": "Not connected", "categories": []})
    cats = state.cats_cache.get(mode, [])
    return jsonify({"categories": cats, "mode": mode})


@flask_app.route("/api/clear_cache", methods=["POST"])
def api_clear_cache():
    """Clear server-side caches without disconnecting.
    Called by the Refresh Playlist button: wipes logo cache, item cache hints,
    and the proxy image cache, then the JS side re-runs doConnect() to refetch
    categories and reconnect with fresh data."""
    global _proxy_img_cache
    with _proxy_img_cache_lock:
        _proxy_img_cache = {}
    state._logo_cache_live = None
    state._logo_cache_vod  = {}
    state.cats_cache        = {}
    state.log("[CACHE] Server-side caches cleared — ready for reconnect")
    return jsonify({"ok": True})


@flask_app.route("/api/items", methods=["POST"])
def api_items():
    data = request.get_json(force=True)
    mode = data.get("mode", "live"); mode = mode if mode in ("live","vod","series") else "live"
    cat = data.get("category", {})
    cat_id = str(cat.get("id", ""))
    browse = data.get("browse", True)
    max_pages = 9999  # always fetch all pages

    if not state.connected:
        return jsonify({"error": "Not connected", "items": []})

    try:
        async def fetch():
            all_items = []
            page = 1
            async with _make_client() as client:
                while page <= max_pages:
                    items = await client.fetch_items_page(mode, cat_id, page)
                    if not items:
                        break
                    all_items.extend(items)
                    if browse:
                        state.log(f"[MAC] Loaded {len(all_items)} items from '{cat.get('title','?')}'")
                    else:
                        state.log(f"  Page {page}: {len(items)} items (total: {len(all_items)})")
                    page += 1
                    if len(items) < 5:
                        break
            if browse and page > max_pages and items:
                # There are more pages — let UI know so it can offer "load more"
                return {"items": all_items, "has_more": True}
            return {"items": all_items, "has_more": False}

        result = run_async(fetch())
        items = result["items"] if isinstance(result, dict) else result
        has_more = result.get("has_more", False) if isinstance(result, dict) else False
        return jsonify({"items": items, "count": len(items), "has_more": has_more})
    except Exception as e:
        state.log(f"[ITEMS] Error: {e}")
        return jsonify({"error": str(e), "items": []})


@flask_app.route("/api/episodes", methods=["POST"])
def api_episodes():
    data = request.get_json(force=True)
    item = data.get("item", {})
    cat_title = data.get("cat_title", "Unknown")
    cat_id = str(data.get("cat_id", ""))
    mode = data.get("mode", "series"); mode = mode if mode in ("live","vod","series") else "series"
    # parent_logo: the show's logo URL sent by the JS frontend so the backend can
    # inject it into any episode that carries no thumbnail of its own.
    parent_logo = str(data.get("parent_logo") or "").strip()
    item = dict(item)
    item["_cat_id"] = cat_id
    item["_mode"] = mode

    try:
        async def fetch():
            async with _make_client() as client:
                return await client.fetch_episodes_for_show(item, cat_title)

        episodes = run_async(fetch())

        # Server-side parent-logo injection: fill in any episode that has no
        # thumbnail with the parent show's logo.  This mirrors the client-side
        # propagation in drillShow() and acts as a belt-and-suspenders guarantee.
        if parent_logo:
            for ep in episodes:
                if isinstance(ep, dict):
                    if not (ep.get("logo") or ep.get("stream_icon") or ep.get("cover")
                            or ep.get("screenshot_uri") or ep.get("pic")):
                        ep["logo"] = parent_logo

        return jsonify({"episodes": episodes, "count": len(episodes)})
    except Exception as e:
        state.log(f"[EPISODES] Error: {e}")
        return jsonify({"error": str(e), "episodes": []})


def _probe_hevc(url: str) -> bool:
    """Read first ~1880 bytes of a MPEG-TS stream and return True if video is HEVC (stream_type 0x24).
    Times out quickly — failure is non-fatal, we just skip the transcode."""
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
            if pid == 0 and pmt_pid is None:
                pos = off + 8
                while pos + 3 < 188:
                    pn = (pkt[pos] << 8) | pkt[pos+1]
                    pp = ((pkt[pos+2] & 0x1f) << 8) | pkt[pos+3]
                    pos += 4
                    if pn != 0: pmt_pid = pp; break
            elif pmt_pid and pid == pmt_pid:
                sec = pkt[off:]
                if len(sec) < 12: continue
                pi_len = ((sec[10] & 0x0f) << 8) | sec[11]
                pos = 12 + pi_len
                while pos + 4 < len(sec) - 4:
                    st = sec[pos]
                    ei = ((sec[pos+3] & 0x0f) << 8) | sec[pos+4]
                    if st == 0x24: return True   # HEVC
                    pos += 5 + ei
                return False
        return False
    except Exception:
        return False


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
            needs_transcode = False
            detected_codec = None
            transcode_reason = None
            is_vod = mode in ('vod', 'series')  # VOD needs different handling than live
            
            # Check both path and query string for container extensions.
            # Many portals serve files via get.php?stream=movie.mkv&mac=xxx —
            # splitting on '?' loses the extension, skipping the codec probe entirely.
            url_lower_full = url.lower()
            url_lower      = url_lower_full.split('?')[0]

            # ==== SMART CONTAINER DETECTION ====
            needs_codec_check = (
                url_lower.endswith('.mp4') or
                url_lower.endswith('.mkv') or
                re.search(r'\.mp4[?&]', url_lower_full) is not None or
                re.search(r'\.mkv[?&]', url_lower_full) is not None or
                re.search(r'\.mp4$',    url_lower_full) is not None or
                re.search(r'\.mkv$',    url_lower_full) is not None or
                any(ext in url_lower for ext in ['.hevc', '.265', '.h265'])
            )
            
            # Quick extension-based HEVC hint
            if any(ext in url_lower for ext in ['.hevc', '.265', '.h265']):
                needs_transcode = True
                transcode_reason = "hevc by extension"
                state.log(f"[RESOLVE] HEVC suspected by extension: {url_lower[-20:]}")
            
            # For MP4/MKV containers, check BOTH video AND audio codecs
            elif needs_codec_check:
                codecs = probe_stream_codecs(url, timeout=6)
                
                if codecs:
                    # Check video codec
                    if codecs.get("video"):
                        vcodec = codecs["video"][0].lower() if codecs["video"] else ""
                        detected_codec = vcodec
                        
                        hevc_codecs = ("hevc", "h265", "h.265", "hev1", "hvc1", "x265")
                        if vcodec in hevc_codecs or any(h in vcodec for h in hevc_codecs):
                            needs_transcode = True
                            transcode_reason = f"hevc video ({vcodec})"
                            state.log(f"[RESOLVE] HEVC video detected: {vcodec}")
                    
                    # Check audio codec - browsers only support AAC, MP3, Opus, Vorbis
                    if not needs_transcode and codecs.get("audio"):
                        acodec = codecs["audio"][0].lower() if codecs["audio"] else ""
                        
                        # Browser-supported audio codecs
                        safe_audio = ("aac", "mp3", "mp2", "opus", "vorbis", "flac")
                        
                        # Common problematic codecs in MKV
                        bad_audio = ("ac3", "eac3", "dts", "dca", "truehd", "mlp", "pcm")
                        
                        if acodec not in safe_audio and (acodec in bad_audio or 
                            any(b in acodec for b in bad_audio)):
                            needs_transcode = True
                            transcode_reason = f"incompatible audio ({acodec})"
                            state.log(f"[RESOLVE] Audio codec needs transcode: {acodec}")
                        else:
                            state.log(f"[RESOLVE] Audio codec OK: {acodec}")
                    
                    if not needs_transcode:
                        state.log(f"[RESOLVE] All codecs playable: v={detected_codec}, a={codecs.get('audio', ['?'])[0] if codecs.get('audio') else 'none'}")
                else:
                    state.log(f"[RESOLVE] ffprobe failed, attempting direct play")
            
            # Legacy MPEG-TS probe for live streams
            if not needs_transcode and 'play_token=' in url:
                try:
                    if _probe_hevc(url):
                        needs_transcode = True
                        transcode_reason = "hevc (ts probe)"
                        is_vod = False  # play_token indicates live
                except Exception as pe:
                    state.log(f"[RESOLVE] HEVC TS probe failed: {pe}")
            
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


@flask_app.route("/api/download/m3u", methods=["POST"])
def api_download_m3u():
    data = request.get_json(force=True)
    items = data.get("items", None)    # None = whole category
    cat = data.get("category", {})
    mode = data.get("mode", "live"); mode = mode if mode in ("live","vod","series") else "live"
    out_path = data.get("out_path", "").strip()
    total_hint = int(data.get("total_hint", 0) or 0)  # client-supplied item count

    if not out_path:
        return jsonify({"error": "No output path specified"}), 400
    if state.busy:
        return jsonify({"error": "Another operation is in progress"}), 409

    state.stop_flag.clear()
    state.set_status(f"Downloading M3U…")

    async def worker():
        state.task_type    = "m3u"
        state.task_done    = 0
        state.task_skipped = 0
        state.task_label   = ""
        # Use client-supplied total (already known from the loaded items list).
        # Fall back to len(items) for selected-items mode, or 0 (indeterminate) if unavailable.
        if items is not None:
            state.task_total = len(items)
        else:
            state.task_total = total_hint  # allItems.length sent from JS

        try:
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        except Exception:
            pass
        # Write header if file is empty/new
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                existing = f.read(10)
        except FileNotFoundError:
            existing = ""
        if not existing:
            # Build the best available EPG URL for the url-tvg header attribute.
            # Priority: 1) user-supplied external EPG  2) portal's own xmltv.php
            #           3) M3U source tvg-url  4) no EPG (plain header)
            epg_url = ""
            if state.ext_epg_url:
                epg_url = state.ext_epg_url
            elif state.conn_type == "xtream" and state.url and state.username and state.password:
                from urllib.parse import quote as _qe
                _base = state.url.rstrip("/")
                epg_url = (f"{_base}/xmltv.php"
                           f"?username={_qe(state.username, safe='')}"
                           f"&password={_qe(state.password, safe='')}")
            elif state.conn_type == "mac" and state.url:
                # Standard MAC/Ministra portals serve XMLTV at /xmltv.php
                epg_url = state.url.rstrip("/") + "/xmltv.php"
            elif state.conn_type == "m3u_url":
                # Use the tvg-url scraped from the M3U header at connect time
                epg_url = getattr(state, "_tvg_url_cache", "") or ""

            if epg_url:
                state.log(f"[M3U] Writing EPG url-tvg: {epg_url[:80]}")
                header = f'#EXTM3U url-tvg="{epg_url}"\n'
            else:
                header = "#EXTM3U\n"

            with open(out_path, "w", encoding="utf-8") as f:
                f.write(header)

        async with _make_client() as client:
            if items is None:
                if state.stop_flag.is_set():
                    return
                def _m3u_pcb(n, label=None):
                    state.task_done = n
                    # Don't calculate skipped mid-run — only set it accurately at completion
                    if label:
                        state.task_label = label
                await client.dump_category_to_file(mode, cat, out_path, stop_flag=state.stop_flag, progress_cb=_m3u_pcb)
            else:
                for item in items:
                    if state.stop_flag.is_set():
                        state.log("Stopped by user.")
                        break
                    name = item.get("name") or item.get("o_name") or item.get("fname") or "?"
                    state.task_label = name
                    state.log(f"Processing: {name}")
                    await client.dump_single_item_to_file(mode, item, cat, out_path, stop_flag=state.stop_flag)
                    state.task_done += 1

        # Final skipped count — difference between what the client said was available vs what was written
        if state.task_total > 0 and not state.stop_flag.is_set():
            state.task_skipped = max(0, state.task_total - state.task_done)

        state.task_type = ""
        skipped_msg = f" ({state.task_skipped} skipped — no valid URL)" if state.task_skipped > 0 else ""
        state.set_status(f"Done. {state.task_done} items saved{skipped_msg}. Output: {out_path}")
        if state.task_skipped > 0:
            state.log(f"[M3U] ⚠ {state.task_skipped} item(s) skipped (stream URL could not be resolved)")
        state.log("DONE.")

    run_worker(worker())
    return jsonify({"ok": True, "message": f"Download started → {out_path}"})


@flask_app.route("/api/download/mkv", methods=["POST"])
def api_download_mkv():
    data = request.get_json(force=True)
    items = data.get("items", [])
    cat = data.get("category", {})
    mode = data.get("mode", "live"); mode = mode if mode in ("live","vod","series") else "live"
    out_dir = data.get("out_dir", state.mkv_folder).strip()
    use_fallback = data.get("use_fallback", state.mkv_fallback)

    if not items:
        return jsonify({"error": "No items selected"}), 400
    if not out_dir:
        return jsonify({"error": "No output folder specified"}), 400
    if not shutil.which("ffmpeg"):
        return jsonify({"error": "ffmpeg not found on PATH"}), 400
    if state.busy:
        return jsonify({"error": "Another operation is in progress"}), 409

    state.mkv_folder = out_dir
    state.mkv_fallback = use_fallback
    state.stop_flag.clear()
    state.task_item_names = [
        (item.get("name") or item.get("o_name") or item.get("fname") or "")
        for item in items
    ]
    state.set_status(f"Resolving + downloading {len(items)} item(s) as MKV…")

    async def worker():
        total = len(items)
        # Set task state immediately so progress bar shows from the start (Phase 1)
        state.task_type  = "mkv"
        state.task_total = total
        state.task_done  = 0
        state.task_label = "Resolving URLs…"
        state.log(f"[MKV] Phase 1: resolving {total} item URL(s)…")
        resolved_items = []

        async with _make_client() as client:
            for i, item in enumerate(items, 1):
                if state.stop_flag.is_set():
                    state.log("[MKV] Stopped during URL resolution.")
                    return
                name = item.get("name") or item.get("o_name") or item.get("fname") or f"item_{i}"
                state.task_label = f"Resolving: {name}"
                state.task_done  = i - 1
                state.log(f"[MKV] Resolving ({i}/{total}): {name}")

                if item.get("_is_series_group"):
                    episodes = item.get("_episodes", [])
                    for ep in episodes:
                        ep_name = ep.get("name", name)
                        ep_url = ep.get("_url", "")
                        if ep_url:
                            resolved_items.append((ep_name, ep_url))
                    state.log(f"[MKV]   → expanded to {len(episodes)} episode(s)")
                    continue

                if item.get("_is_show_item"):
                    cat_title = cat.get("title", "Unknown")
                    cat_id = str(cat.get("id", ""))
                    expanded = dict(item)
                    expanded["_cat_id"] = cat_id
                    state.log(f"[MKV]   Show-level item — fetching episode list…")
                    try:
                        episodes = await client.fetch_episodes_for_show(expanded, cat_title)
                    except Exception as e:
                        state.log(f"[MKV]   ✗ Could not fetch episodes: {e}")
                        episodes = []
                    for ep in episodes:
                        if state.stop_flag.is_set():
                            break
                        ep_name = ep.get("name", name)
                        ep_url = ep.get("_direct_url", "")
                        if not ep_url:
                            ep_url = await client.resolve_item_url(mode, ep, cat)
                        if ep_url:
                            resolved_items.append((ep_name, ep_url))
                    continue

                url = await client.resolve_item_url(mode, item, cat)
                if url:
                    resolved_items.append((name, url))
                else:
                    state.log(f"[MKV]   ✗ Could not resolve URL for: {name}")

        if not resolved_items:
            state.log("[MKV] No URLs could be resolved.")
            state.set_status("MKV: no URLs resolved.")
            return

        os.makedirs(out_dir, exist_ok=True)
        state.task_type  = "mkv"
        state.task_total = len(resolved_items)
        state.task_done  = 0
        state.task_label = f"Downloading {len(resolved_items)} file(s)…"
        state.log(f"[MKV] Phase 2: downloading {len(resolved_items)} file(s) to: {out_dir}")
        pre_args = [
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto,rtsp,rtmp",
            # Auto-reconnect on connection drop (works for HTTP/HTTPS streams)
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            # Generate PTS for packets that have unset timestamps (common in HLS
            # streams whose segments start at a large offset such as 600 s).
            # Without this the matroska muxer rejects the first few packets and
            # aborts with "Can't write packet with unknown timestamp".
            "-fflags", "+genpts+igndts",
        ]

        MAX_RETRIES = 3  # max retry attempts on unexpected failure

        for idx, (name, url) in enumerate(resolved_items, 1):
            if state.stop_flag.is_set():
                state.log("[MKV] Stopped by user.")
                break

            safe = safe_filename(name)
            out_path = os.path.join(out_dir, f"{safe}.mkv")
            state.task_done         = idx - 1
            state.task_label        = name
            state.task_file_pct     = 0.0
            state.task_file_elapsed = ""
            state.task_speed        = ""
            state.log(f"[MKV] ({idx}/{len(resolved_items)}) Downloading: {name}")
            state.set_status(f"MKV {idx}/{len(resolved_items)}: {name}")

            state.log("[MKV]   Probing codecs…")
            codecs = probe_stream_codecs(url, pre_input_args=pre_args)
            state.task_file_duration = (codecs.get("duration") or 0.0) if codecs else 0.0
            # -avoid_negative_ts make_zero: shifts output timestamps so they start
            # at 0, which resolves "Can't write packet with unknown timestamp" when
            # the HLS stream has a large start offset (e.g. 600 s) and the raw TS
            # packets arrive with NOPTS DTS — genpts alone can't help in that case.
            post_args = ["-avoid_negative_ts", "make_zero"]
            if codecs and codecs.get("audio"):
                if any(c.lower() == "aac" for c in codecs["audio"]):
                    post_args += ["-bsf:a", "aac_adtstoasc"]
                    state.log("[MKV]   AAC audio → adding -bsf:a aac_adtstoasc")

            def _set_proc(p):
                with state.mkv_proc_lock:
                    state.mkv_proc = p

            def _on_ffmpeg_line(line: str):
                stripped = line.rstrip()
                # Always forward every ffmpeg line to the activity log
                if stripped:
                    state.log(stripped)
                # Also parse for progress bar data
                m_t = _time_re.search(line)
                if m_t:
                    h, mi, s = int(m_t.group(1)), int(m_t.group(2)), float(m_t.group(3))
                    elapsed_s = h * 3600 + mi * 60 + s
                    dur = state.task_file_duration
                    state.task_file_elapsed = f"{int(h):02d}:{int(mi):02d}:{int(s):02d}"
                    if dur and dur > 0:
                        state.task_file_pct = min(100.0, round(elapsed_s / dur * 100, 1))
                m_b = _bitrate_re.search(line)
                if m_b:
                    kbits = float(m_b.group(1))
                    kbytes = kbits / 8.0
                    state.task_speed = f"{kbytes/1024:.1f} MB/s" if kbytes >= 1024 else f"{kbytes:.0f} KB/s"

            rc = 0
            for attempt in range(1, MAX_RETRIES + 1):
                if state.stop_flag.is_set():
                    break
                if attempt > 1:
                    state.task_file_pct     = 0.0
                    state.task_file_elapsed = ""
                    state.task_speed        = ""

                if attempt == 1:
                    # ── Attempt 1: direct HLS → MKV ──────────────────────────
                    state.log(f"[MKV]   Attempt {attempt}/{MAX_RETRIES} — direct MKV…")
                    rc = run_ffmpeg_download(
                        url, out_path,
                        pre_input_args=pre_args,
                        post_input_args=post_args,
                        on_progress=_on_ffmpeg_line,
                        stop_event=state.stop_flag,
                        set_proc=_set_proc,
                    )
                    with state.mkv_proc_lock:
                        state.mkv_proc = None
                    if rc == 0:
                        break
                    if state.stop_flag.is_set():
                        break
                    state.log(f"[MKV]   ✗ Direct MKV exit {rc} — retrying via TS intermediate…")
                    with contextlib.suppress(Exception):
                        os.remove(out_path)

                else:
                    # ── Attempts 2-3: direct HLS → MPEG-TS ───────────────────
                    # MPEG-TS tolerates unset/negative timestamps that MKV rejects.
                    # Save directly as .ts — no remux step needed.
                    state.log(f"[MKV]   Attempt {attempt}/{MAX_RETRIES} — saving as MPEG-TS…")
                    ts_out = os.path.splitext(out_path)[0] + ".ts"
                    ts_post_args = [a for a in post_args
                                    if a not in ("-avoid_negative_ts", "make_zero",
                                                 "-bsf:a", "aac_adtstoasc")]
                    ts_post_args += ["-f", "mpegts"]

                    rc = run_ffmpeg_download(
                        url, ts_out,
                        pre_input_args=pre_args,
                        post_input_args=ts_post_args,
                        on_progress=_on_ffmpeg_line,
                        stop_event=state.stop_flag,
                        set_proc=_set_proc,
                    )
                    with state.mkv_proc_lock:
                        state.mkv_proc = None

                    if state.stop_flag.is_set():
                        break

                    if rc != 0:
                        state.log(f"[MKV]   ✗ TS download exit {rc} on attempt {attempt}/{MAX_RETRIES}")
                        with contextlib.suppress(Exception):
                            os.remove(ts_out)
                        if attempt < MAX_RETRIES:
                            time.sleep(2)
                        continue

                    state.log(f"[MKV]   Saved as MPEG-TS: {os.path.basename(ts_out)}")
                    # Use ts_out as the successful output for logging purposes
                    out_path = ts_out
                    break

            if state.stop_flag.is_set():
                break

            if rc == 0:
                state.log(f"[MKV] ✓ Saved: {out_path}")
            else:
                state.log(f"[MKV] ✗ Failed after {MAX_RETRIES} attempt(s): {name}")
                if use_fallback and YTDLP_AVAILABLE and not state.stop_flag.is_set():
                    state.log("[MKV]   Trying yt-dlp fallback…")
                    state.task_file_pct     = 0.0
                    state.task_file_elapsed = ""
                    state.task_speed        = ""

                    def _ytdlp_progress(d):
                        downloaded = d.get("downloaded_bytes") or 0
                        total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        speed      = d.get("speed") or 0          # bytes/s
                        elapsed    = d.get("elapsed") or 0        # seconds so far
                        frag_idx   = d.get("fragment_index") or 0
                        frag_total = d.get("fragment_count") or 0
                        if total > 0:
                            state.task_file_pct = min(100.0, round(downloaded / total * 100, 1))
                        if speed:
                            kbytes = speed / 1024.0
                            state.task_speed = (f"{kbytes/1024:.1f} MB/s"
                                                if kbytes >= 1024 else f"{kbytes:.0f} KB/s")
                        if elapsed:
                            h = int(elapsed) // 3600
                            m = (int(elapsed) % 3600) // 60
                            s = int(elapsed) % 60
                            state.task_file_elapsed = f"{h:02d}:{m:02d}:{s:02d}"
                        # Log to activity log every 10 fragments (avoids flooding)
                        if frag_idx and frag_idx % 10 == 0:
                            pct_str  = d.get("_percent_str", "").strip() or (
                                f"{state.task_file_pct:.1f}%" if state.task_file_pct else "?")
                            spd_str  = d.get("_speed_str", "").strip() or state.task_speed
                            eta_str  = d.get("_eta_str", "").strip() or ""
                            frag_str = f" (frag {frag_idx}/{frag_total})" if frag_total else ""
                            eta_part = f" ETA {eta_str}" if eta_str else ""
                            state.log(f"[yt-dlp] {pct_str} at {spd_str}{eta_part}{frag_str}")

                    ok, err = run_yt_dlp_download(url, out_path,
                                                  stop_event=state.stop_flag,
                                                  on_progress=_ytdlp_progress)
                    if ok:
                        state.log(f"[MKV]   ✓ yt-dlp saved: {out_path}")
                    elif err == "stopped":
                        state.log("[MKV]   yt-dlp stopped by user.")
                    else:
                        state.log(f"[MKV]   ✗ yt-dlp failed: {err}")

        if not state.stop_flag.is_set():
            state.task_done = len(resolved_items)
            state.set_status(f"MKV download complete. Files in: {out_dir}")
            state.log(f"[MKV] All done. Output folder: {out_dir}")
        state.task_type         = ""
        state.task_file_pct     = 0.0
        state.task_file_elapsed = ""
        state.task_speed        = ""
        state.task_item_names   = []
        state.log("DONE.")

    run_worker(worker())
    return jsonify({"ok": True, "message": f"MKV download started → {out_dir}"})


@flask_app.route("/api/record/start", methods=["POST"])
def api_record_start():
    data = request.get_json(force=True)
    stream_url = data.get("url", "").strip()
    out_dir = data.get("out_dir", state.mkv_folder).strip()
    stream_name = data.get("name", "recording").strip()

    if not stream_url:
        return jsonify({"error": "No stream URL"}), 400
    if not shutil.which("ffmpeg"):
        return jsonify({"error": "ffmpeg not found"}), 400
    if state.recording:
        return jsonify({"error": "Already recording"}), 409

    if not out_dir:
        out_dir = os.path.expanduser("~/Downloads")
    os.makedirs(out_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = safe_filename(stream_name) + f"_{ts}.mkv"
    out_path = os.path.join(out_dir, fname)

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y",
           "-protocol_whitelist", "file,http,https,tcp,tls,crypto,rtsp,rtmp",
           "-i", stream_url, "-c", "copy", out_path]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        return jsonify({"error": f"Failed to start ffmpeg: {e}"}), 500

    with state.record_proc_lock:
        state.record_proc = proc
    state.recording = True
    state.record_start_time = time.time()
    state.record_file_path = out_path
    state.log(f"[REC] ⏺ Recording started: {out_path}")
    state.set_status(f"⏺ Recording → {fname}")
    return jsonify({"ok": True, "file": out_path, "filename": fname})


@flask_app.route("/api/record/stop", methods=["POST"])
def api_record_stop():
    if not state.recording:
        return jsonify({"error": "Not recording"}), 400
    with state.record_proc_lock:
        p = state.record_proc
    if p:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    with state.record_proc_lock:
        state.record_proc = None
    state.recording = False
    saved = state.record_file_path
    state.log(f"[REC] ⏹ Recording stopped. Saved: {saved}")
    state.set_status(f"Recording stopped. Saved: {os.path.basename(saved)}")
    return jsonify({"ok": True, "file": saved})


@flask_app.route("/api/record/status", methods=["GET"])
def api_record_status():
    if not state.recording:
        return jsonify({"recording": False})
    elapsed = int(time.time() - state.record_start_time)
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    return jsonify({
        "recording": True,
        "elapsed": f"{h:02d}:{m:02d}:{s:02d}",
        "file": state.record_file_path,
        "filename": os.path.basename(state.record_file_path),
    })


@flask_app.route("/api/stop", methods=["POST"])
def api_stop():
    state.stop_flag.set()
    with state.mkv_proc_lock:
        p = state.mkv_proc
    if p:
        try:
            p.terminate()
        except Exception:
            pass
    loop = state.active_loop
    task = state.active_task
    if loop and task and not task.done():
        loop.call_soon_threadsafe(task.cancel)
    state.log("⏹ Stopped by user.")
    state.set_status("Stopped.")
    return jsonify({"ok": True})


@flask_app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "connected": state.connected,
        "busy": state.busy,
        "status": state.status,
        "recording": state.recording,
        "conn_type": state.conn_type,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "task_type":       state.task_type,
        "task_label":      state.task_label,
        "task_item_names": list(state.task_item_names),
        "task_total": state.task_total,
        "task_done":  state.task_done,
        "task_skipped": state.task_skipped,
        "task_file_pct":     state.task_file_pct,
        "task_file_elapsed": state.task_file_elapsed,
        "task_speed":        state.task_speed,
        "ytdlp": YTDLP_AVAILABLE,
    })


@flask_app.route("/api/logs")
def api_logs():
    """SSE stream of log messages."""
    def generate():
        # Send initial ping
        yield "data: Connected to log stream\n\n"
        while True:
            try:
                msg = state.log_queue.get(timeout=1.0)
                # Escape newlines for SSE
                safe_msg = msg.replace("\n", " ").replace("\r", "")
                yield f"data: {safe_msg}\n\n"
            except queue.Empty:
                # Heartbeat
                yield ": heartbeat\n\n"

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection": "keep-alive",
                    })


# ===================== HLS PROXY =====================

def _rewrite_m3u8(content: str, base_url: str) -> str:
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
            result.append(re.sub(r'URI="([^"]*)"', _repl, line))
        else:
            result.append(line)
    return '\n'.join(result)


@flask_app.route("/api/epg", methods=["POST"])
def api_epg():
    """Fetch current + next EPG for a live channel.
    Works for: Xtream, Stalker, MAC portal, M3U (via Xtream override or tvg-url XMLTV).
    Returns: {current: {title, start, end, desc}, next: {title, start, end, desc}}
    """
    data = request.get_json(force=True)
    item = data.get("item", {})
    # stream_id for Xtream, ch_id for Stalker/MAC, tvg_id for M3U
    stream_id = str(item.get("stream_id") or item.get("id") or "").strip()
    tvg_id    = str(item.get("tvg_id") or item.get("epg_channel_id") or item.get("name") or "").strip()

    # Cache key: portal type + channel identifier
    cache_key = f"{state.conn_type}:{stream_id or tvg_id}"
    cached = state._epg_cache.get(cache_key)
    if cached:
        ts, result = cached
        if time.time() - ts < state._epg_cache_ttl:
            state.log(f"[EPG] Cache hit for {cache_key}")
            return jsonify(result)
        else:
            del state._epg_cache[cache_key]

    # Short-circuit: if this channel is confirmed to need XMLTV (no portal data)
    # and XMLTV is still downloading, skip the entire portal chain immediately.
    ext_url = state.ext_epg_url
    if cache_key in state._xmltv_needs and ext_url and ext_url in state._xmltv_downloading:
        _loading = {"current": None, "next": None,
                    "error": "EPG loading… please try again in a moment"}
        return jsonify(_loading)

    async def fetch_epg():
        conn = state.conn_type

        # ── Xtream (direct or M3U override) ──────────────────────────────────
        # Method 1: player_api get_short_epg (fast, per-channel)
        # Method 2: portal's /xmltv.php
        # Method 3: user-supplied external XMLTV (fallback for channels portal has no data for)
        if conn == "xtream" or (conn == "m3u_url" and state.m3u_xtream_override):
            creds = state.m3u_xtream_override if conn == "m3u_url" else None
            base  = creds["base"]      if creds else state.url
            user  = creds["username"]  if creds else state.username
            pwd   = creds["password"]  if creds else state.password
            from urllib.parse import urlparse as _up, quote as _q
            _p = _up(base.rstrip("/"))
            base_norm = f"{_p.scheme}://{_p.netloc}"

            # ── Method 1: get_short_epg (per-channel, fast) ──────────────────
            short_epg_skip = base_norm in state._short_epg_broken
            if stream_id and not short_epg_skip:
                epg_api_url = (f"{base_norm}/player_api.php"
                               f"?username={_q(user, safe='')}&password={_q(pwd, safe='')}"
                               f"&action=get_short_epg&stream_id={stream_id}&limit=3")
                state.log(f"[EPG] Xtream get_short_epg stream_id={stream_id}")
                try:
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(epg_api_url,
                                            timeout=aiohttp.ClientTimeout(total=10)) as r:
                            payload = await safe_json(r)
                    listings = (payload.get("epg_listings") or
                                (payload.get("js") or {}).get("data") or
                                (payload.get("js") or {}).get("epg_listings") or []) \
                               if isinstance(payload, dict) else []
                    if listings and isinstance(listings, list):
                        state.log(f"[EPG] get_short_epg first entry: {listings[0]}")
                        result = _parse_xtream_short_epg(payload)
                        if result.get("current") or result.get("next"):
                            state.log(f"[EPG] get_short_epg OK — current={result.get('current',{}).get('title','?')!r}")
                            return result
                        state.log(f"[EPG] get_short_epg has entries but none current/next — falling through")
                    else:
                        state._short_epg_broken.add(base_norm)
                        state.log(f"[EPG] get_short_epg empty — portal flagged, skipping next time")
                except Exception as e:
                    state.log(f"[EPG] get_short_epg error: {e}")
            elif short_epg_skip:
                state.log(f"[EPG] Skipping get_short_epg (portal flagged as broken)")

            # ── Method 2: portal's own XMLTV ─────────────────────────────────
            epg_ch_id = str(item.get("epg_channel_id") or "").strip()
            portal_result = None
            if epg_ch_id and epg_ch_id != item.get("name", "") \
                    and base_norm not in state._xmltv_no_data:
                xmltv_url = (f"{base_norm}/xmltv.php"
                             f"?username={_q(user, safe='')}&password={_q(pwd, safe='')}")
                state.log(f"[EPG] Xtream portal XMLTV (epg_channel_id={epg_ch_id!r})")
                portal_result = await _fetch_xmltv_epg(xmltv_url, epg_ch_id, state.log,
                                                       cache_key=base_norm)
                if portal_result.get("current") or portal_result.get("next"):
                    return portal_result
                state.log(f"[EPG] Portal XMLTV returned no data for this channel")
            elif not epg_ch_id or epg_ch_id == item.get("name", ""):
                state.log(f"[EPG] No epg_channel_id — skipping portal XMLTV")

            # ── Method 3: external XMLTV fallback ────────────────────────────
            # Use epg_channel_id if available, else fall back to tvg_id (channel name).
            # Even if tvg_id is a display name it's worth trying — some EPG sources
            # use display-name matching and it costs nothing once the index is cached.
            if state.ext_epg_url:
                lookup_id = epg_ch_id or tvg_id
                if lookup_id:
                    state.log(f"[EPG] External EPG fallback (lookup={lookup_id!r})")
                    ext_result = await _fetch_xmltv_epg(state.ext_epg_url, lookup_id,
                                                        state.log, cache_key=state.ext_epg_url)
                    if ext_result.get("current") or ext_result.get("next"):
                        return ext_result
                    # If still loading, mark channel and skip portal next time
                    if "loading" in (ext_result.get("error") or "").lower():
                        state._xmltv_needs.add(cache_key)
                        return {"current": None, "next": None,
                                "error": ext_result.get("error")}
                    state.log(f"[EPG] External EPG: no match for {lookup_id!r}")
                    return {"current": None, "next": None,
                            "error": "Channel not found in external EPG.",
                            "_xmltv_checked": True}

            # Nothing worked
            err = "No EPG data found."
            if not state.ext_epg_url:
                err += " Try adding an external EPG URL in settings."
            return {"current": None, "next": None, "error": err}

        # ── Stalker / MAC portal ──────────────────────────────────────────────
        if conn == "mac":
            ch_id = str(item.get("ch_id") or item.get("id") or stream_id or "").strip()
            php = "/stalker_portal/server/load.php" if state.is_stalker_portal else "/portal.php"
            client = StalkerPortalClient(state.url, state.mac, state.log) if state.is_stalker_portal \
                     else PortalClient(state.url, state.mac, state.log)
            async with client:
                await client.handshake()
                headers = client._headers(include_auth=True) if state.is_stalker_portal \
                          else client.headers
                base_url = normalize_base_url(state.url)
                if not ch_id:
                    # No portal ch_id — skip straight to external EPG if available
                    pass
                else:
                    epg_url = (f"{base_url}{php}?type=itv&action=get_short_epg"
                               f"&ch_id={ch_id}&count=10&JsHttpRequest=1-xml")
                    state.log(f"[EPG] Trying: {epg_url}")
                    async with client.session.get(epg_url, headers=headers,
                                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                        state.log(f"[EPG] HTTP {r.status}")
                        payload = await safe_json(r)
                    state.log(f"[EPG] Raw: {str(payload)[:300]}")
                    result = _parse_stalker_epg(payload, ch_id)
                    if result.get("current") or result.get("next") or result.get("schedule"):
                        return result
                    state.log(f"[EPG] Portal returned no EPG data for this channel")
                    # Fallback: try alternate path (portal.php ↔ load.php)
                    if state.is_stalker_portal:
                        alt_php = "/stalker_portal/portal.php"
                    else:
                        alt_php = "/stalker_portal/server/load.php"
                    if alt_php != php:
                        alt_epg_url = (f"{base_url}{alt_php}?type=itv&action=get_short_epg"
                                       f"&ch_id={ch_id}&count=10&JsHttpRequest=1-xml")
                        state.log(f"[EPG] Retrying via alt path: {alt_epg_url}")
                        try:
                            async with client.session.get(alt_epg_url, headers=headers,
                                                          timeout=aiohttp.ClientTimeout(total=10)) as r2:
                                state.log(f"[EPG] Alt HTTP {r2.status}")
                                payload2 = await safe_json(r2)
                            state.log(f"[EPG] Alt raw: {str(payload2)[:300]}")
                            result2 = _parse_stalker_epg(payload2, ch_id)
                            if result2.get("current") or result2.get("next") or result2.get("schedule"):
                                return result2
                            state.log(f"[EPG] Alt path also returned no EPG data")
                        except Exception as _e2:
                            state.log(f"[EPG] Alt path error: {_e2}")

            # External EPG fallback for MAC/Stalker
            if state.ext_epg_url:
                lookup_id = str(item.get("epg_channel_id") or tvg_id or "").strip()
                if lookup_id:
                    state.log(f"[EPG] MAC external EPG fallback (lookup={lookup_id!r})")
                    ext_result = await _fetch_xmltv_epg(state.ext_epg_url, lookup_id,
                                                        state.log, cache_key=state.ext_epg_url)
                    if ext_result.get("current") or ext_result.get("next"):
                        return ext_result
                    # If XMLTV is still loading, mark channel so future requests
                    # skip the portal chain entirely while download is in progress.
                    if "loading" in (ext_result.get("error") or "").lower():
                        state._xmltv_needs.add(cache_key)
                        return {"current": None, "next": None,
                                "error": ext_result.get("error")}
                    # XMLTV was consulted and definitively returned nothing — tag the
                    # result so the retry loop knows there's no point trying again.
                    err = "No EPG data from portal."
                    err += " Channel not found in external EPG either."
                    return {"current": None, "next": None, "error": err, "_xmltv_checked": True}

            err = "No EPG data from portal."
            if state.ext_epg_url:
                err += " Channel not found in external EPG either."
            else:
                err += " Try adding an external EPG URL in settings."
            return {"current": None, "next": None, "error": err}

        # ── M3U without Xtream — try tvg-url XMLTV then external ─────────────
        if conn == "m3u_url" and tvg_id:
            tvg_url = str(item.get("tvg_url") or item.get("_tvg_url") or "").strip()
            if not tvg_url:
                tvg_url = getattr(state, "_tvg_url_cache", "")
            if tvg_url and tvg_url.startswith("http"):
                m3u_result = await _fetch_xmltv_epg(tvg_url, tvg_id, state.log)
                if m3u_result.get("current") or m3u_result.get("next"):
                    return m3u_result
                state.log(f"[EPG] M3U tvg-url returned no data — trying external EPG")
            # External EPG fallback
            if state.ext_epg_url:
                return await _fetch_xmltv_epg(state.ext_epg_url, tvg_id, state.log,
                                              cache_key=state.ext_epg_url)

        return {"current": None, "next": None, "error": "EPG not available for this portal/item"}

    try:
        result = run_async(fetch_epg())
        # Retry up to 2 more times (3 total attempts) before giving up.
        # BUT: if XMLTV was already consulted and confirmed nothing (_xmltv_checked),
        # the outcome is deterministic — skip further retries immediately.
        for _retry in range(2):
            if result.get("current") or result.get("next") or result.get("schedule"):
                break
            if result.get("_xmltv_checked"):
                state.log(f"[EPG] XMLTV confirmed no data — skipping retries")
                break
            # Don't retry if EPG is loading in background — just return the loading msg
            if "loading" in (result.get("error") or "").lower():
                break
            state.log(f"[EPG] Attempt {_retry + 1} returned no data — retrying ({_retry + 2}/3)")
            result = run_async(fetch_epg())

        if result.get("current") or result.get("next") or result.get("schedule"):
            # Full 20-minute cache for successful results
            state._epg_cache[cache_key] = (time.time(), result)
        elif result.get("_xmltv_checked"):
            # Confirmed-empty (XMLTV was tried and found nothing): cache for 5 min
            state.log(f"[EPG] Confirmed no EPG — caching for 5 min")
            _confirmed_empty = {k: v for k, v in result.items() if k != "_xmltv_checked"}
            state._epg_cache[cache_key] = (time.time() - (state._epg_cache_ttl - 300), _confirmed_empty)
        elif "loading" in (result.get("error") or "").lower():
            # XMLTV download in progress — cache for 4s so rapid per-channel retries
            # hit the cache instead of re-running the full portal EPG chain.
            state._epg_cache[cache_key] = (time.time() - (state._epg_cache_ttl - 4), result)
        # Other transient failures not cached so they get a fresh try on next load.
        return jsonify({k: v for k, v in result.items() if k != "_xmltv_checked"})
    except Exception as e:
        state.log(f"[EPG] Error: {type(e).__name__}: {e}")
        err_result = {"current": None, "next": None, "error": str(e)}
        # Cache errors too so a broken channel doesn't retry every single request
        state._epg_cache[cache_key] = (time.time(), err_result)
        return jsonify(err_result)


@flask_app.route("/api/epg_status", methods=["GET"])
def api_epg_status():
    """Returns whether external EPG is currently downloading or ready."""
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"downloading": False, "ready": True})
    downloading = url in state._xmltv_downloading
    ready = url in state._xmltv_cache and url not in state._xmltv_downloading
    return jsonify({"downloading": downloading, "ready": ready})


@flask_app.route("/api/whats_on", methods=["GET"])
def api_whats_on():
    """Return all currently airing programmes from cached XMLTV data.
    If ext_epg_url is set but not yet cached, kicks off a background download
    and returns a loading status — never blocks the request.
    """
    now = time.time()

    # If ext_epg_url is configured but not yet in cache, kick off background download
    ek = state.ext_epg_url
    if ek and ek not in state._xmltv_cache and ek not in state._xmltv_no_data:
        if ek not in state._xmltv_downloading:
            state._xmltv_downloading.add(ek)
            state.log(f"[WHATS_ON] Launching background EPG download from {ek}")

            def _bg():
                try:
                    bg_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(bg_loop)
                    epg_d, ch_n = bg_loop.run_until_complete(
                        _build_xmltv_index(ek, state.log))
                    bg_loop.close()
                    state._xmltv_cache[ek] = (time.time(), epg_d, ch_n)
                    if not epg_d:
                        state._xmltv_no_data.add(ek)
                    state.log(f"[WHATS_ON] Background EPG download complete")
                except Exception as e:
                    state.log(f"[WHATS_ON] EPG load failed: {e}")
                finally:
                    state._xmltv_downloading.discard(ek)
                    state._xmltv_needs.clear()
                    stale = [k for k, v in list(state._epg_cache.items())
                             if "loading" in (v[1].get("error") or "").lower()]
                    for k in stale:
                        state._epg_cache.pop(k, None)

            threading.Thread(target=_bg, daemon=True, name="xmltv-whats-on").start()

        return jsonify({"programs": [], "count": 0, "status": "loading",
                        "message": "EPG loading in background — please try again in a moment"})

    if not state._xmltv_cache:
        msg = ("No EPG data loaded yet. Open any live channel first to trigger EPG load, "
               "then re-open What's on Now.") if state.ext_epg_url else (
               "No external EPG URL configured. Add one in Settings (EPG field) and reconnect.")
        return jsonify({"programs": [], "count": 0, "status": "no_epg", "message": msg})

    results = []
    seen = set()

    for _ck, (ts, epg_dict, chan_names) in list(state._xmltv_cache.items()):
        for channel_id, programmes in epg_dict.items():
            names = chan_names.get(channel_id, [])
            display_name = names[0].title() if names else channel_id
            for prog in programmes:
                # Support both tuple (title,start,end,desc) and legacy dict entries
                if isinstance(prog, tuple):
                    p_title, p_start, p_end, p_desc = prog[0], prog[1], prog[2], prog[3] if len(prog) > 3 else ""
                else:
                    p_title, p_start, p_end, p_desc = prog["title"], prog["start"], prog["end"], prog.get("desc", "")
                if p_start <= now < p_end:
                    key = (p_title.lower(), channel_id)
                    if key not in seen:
                        seen.add(key)
                        # Calculate progress percentage through the show
                        duration = p_end - p_start
                        elapsed = now - p_start
                        progress = int((elapsed / duration * 100)) if duration > 0 else 0
                        results.append({
                            "title": p_title,
                            "channel_id": channel_id,
                            "channel_name": display_name,
                            "start": p_start,
                            "end": p_end,
                            "desc": p_desc,
                            "progress": progress,
                        })

    results.sort(key=lambda x: x["title"].lower())
    return jsonify({"programs": results, "count": len(results), "status": "ok"})


@flask_app.route("/api/find_channel", methods=["POST"])
def api_find_channel():
    """Fuzzy-match an EPG channel name against the currently connected portal's live channels.
    Body: {channel_name: str, channel_id: str}
    Returns: {found: bool, name: str, score: int, cat: str, cmd/stream_id: ...}
    """
    if not state.connected:
        return jsonify({"found": False, "error": "Not connected"})

    data = request.get_json(force=True)
    epg_channel_name = (data.get("channel_name") or "").strip()
    epg_channel_id   = (data.get("channel_id")   or "").strip().lower()

    state.log(f"[FIND_CH] Request: name={epg_channel_name!r} id={epg_channel_id!r} conn={state.conn_type} connected={state.connected}")

    if not epg_channel_name and not epg_channel_id:
        return jsonify({"found": False, "error": "No channel name provided"})

    # ── Return cached channel list if fresh ──────────────────────────────────
    cache_ts, cached_channels = state._won_ch_cache
    if cached_channels and (time.time() - cache_ts) < state._won_ch_cache_ttl:
        channels = cached_channels
    else:
        # ── Fetch all live channels from portal ───────────────────────────────
        async def fetch_all_channels():
            conn = state.conn_type
            chans = []

            if conn == "mac":
                is_stalker = state.is_stalker_portal
                client_cls = StalkerPortalClient if is_stalker else PortalClient
                async with client_cls(state.url, state.mac, state.log) as client:
                    await client.handshake()

                    # ── Attempt 1: get_all_channels — retry same path 2× before fallback ──
                    # Some portals return 0 on first call if the token is fresh/cold.
                    for _try in range(1, 3):
                        try:
                            if is_stalker:
                                url = client._load_url(
                                    type="itv", action="get_all_channels",
                                    force_ch_link_check="", JsHttpRequest="1-xml"
                                )
                                hdrs = client._headers(include_auth=True)
                            else:
                                url = (f"{client.base}/portal.php?type=itv"
                                       f"&action=get_all_channels"
                                       f"&force_ch_link_check=&JsHttpRequest=1-xml")
                                hdrs = client.headers
                            state.log(f"[FIND_CH] Attempt 1.{_try}: {url[:80]}")
                            async with client.session.get(url, headers=hdrs) as r:
                                payload = await safe_json(r)
                            chans = normalize_js(payload)
                            state.log(f"[FIND_CH] Attempt 1.{_try} → {len(chans)} channels")
                            if chans:
                                break
                            if _try < 2:
                                await asyncio.sleep(1.5)
                        except Exception as e:
                            state.log(f"[FIND_CH] Attempt 1.{_try} error: {e}")
                            chans = []
                            if _try < 2:
                                await asyncio.sleep(1.5)

                    # ── Attempt 2: try alternate path (portal.php for stalker, load.php for MAC) ──
                    if not chans:
                        try:
                            alt_base = "/stalker_portal/portal.php" if is_stalker else "/stalker_portal/server/load.php"
                            alt_url = (f"{client.base}{alt_base}?type=itv"
                                       f"&action=get_all_channels"
                                       f"&force_ch_link_check=&JsHttpRequest=1-xml")
                            alt_hdrs = client._headers(include_auth=True) if is_stalker else client.headers
                            state.log(f"[FIND_CH] Attempt 2: {alt_url[:80]}")
                            async with client.session.get(alt_url, headers=alt_hdrs) as r2:
                                payload2 = await safe_json(r2)
                            chans = normalize_js(payload2)
                            state.log(f"[FIND_CH] Attempt 2 → {len(chans)} channels")
                        except Exception as e2:
                            state.log(f"[FIND_CH] Attempt 2 error: {e2}")
                            chans = []

                    # ── Attempt 3: walk all live categories page-by-page (always works) ──
                    if not chans:
                        state.log("[FIND_CH] Falling back to category walk…")
                        cats = await client.fetch_categories("live")
                        for cat in cats:
                            cat_id = str(cat.get("id", ""))
                            if not cat_id:
                                continue
                            page = 1
                            while True:
                                items = await client.fetch_items_page("live", cat_id, page)
                                if not items:
                                    break
                                chans.extend(items)
                                # Most portals return ≤14 items/page; if full page, try next
                                if len(items) < 14:
                                    break
                                page += 1
                        state.log(f"[FIND_CH] Category walk found {len(chans)} channels")

            elif conn == "xtream" or (conn == "m3u_url" and state.m3u_xtream_override):
                creds = state.m3u_xtream_override if conn == "m3u_url" else None
                base  = creds["base"]     if creds else state.url
                user  = creds["username"] if creds else state.username
                pwd   = creds["password"] if creds else state.password
                async with XtreamClient(base, user, pwd, state.log) as client:
                    await client.handshake()
                    url = client._api("get_live_streams")
                    async with client.session.get(url) as r:
                        chans = await safe_json(r) or []

            elif conn == "m3u_url" and state.m3u_cache:
                # Pull all live entries from the in-memory M3U cache
                type_filter = {"live", ""}
                for group_items in state.m3u_cache.values():
                    for it in group_items:
                        if isinstance(it, dict) and it.get("tvg_type", "") in type_filter:
                            chans.append(it)

            return [c for c in chans if isinstance(c, dict)]

        try:
            channels = run_async(fetch_all_channels())
            state._won_ch_cache = (time.time(), channels)
            state.log(f"[FIND_CH] Fetched {len(channels)} live channels from portal")
        except Exception as e:
            state.log(f"[FIND_CH] Fetch error: {e}")
            return jsonify({"found": False, "error": str(e)})

    if not channels:
        return jsonify({"found": False, "error": "No live channels on portal"})

    # ── Fuzzy scoring ─────────────────────────────────────────────────────────
    QUALITY_TAGS = ["hevc", "h265", "h.265", "hvc1", "hvc", "av1",
                    "hd", "sd", "fhd", "uhd", "4k", "h264", "h.264",
                    "avc", "av1", "1080p", "720p", "480p"]
    # Tags that indicate a stream the browser likely can't play (HEVC/H265)
    HEVC_TAGS = {"hevc", "h265", "h.265", "hvc1", "hvc", "h.265"}

    # Country code synonyms — portals use these interchangeably as prefixes/suffixes
    COUNTRY_SYNONYMS = {
        "sr": "rs",   # Serbia: SR (srpski) ↔ RS (ISO 3166)
        "rs": "rs",
        "hr": "hr",   "ba": "ba",  "si": "si",  "mk": "mk",
        "me": "me",   "al": "al",  "bg": "bg",  "ro": "ro",
        "hu": "hu",   "sk": "sk",  "cz": "cz",  "pl": "pl",
        "uk": "uk",   "us": "us",  "de": "de",  "fr": "fr",
        "it": "it",   "es": "es",  "pt": "pt",  "nl": "nl",
        "tr": "tr",   "gr": "gr",  "at": "at",  "ch": "ch",
    }
    _CC_PATTERN = '|'.join(COUNTRY_SYNONYMS.keys())

    def _strip_country_prefix(s):
        m = re.match(r'^([A-Za-z]{2,3})\s*[:\|]\s*', s)
        if m:
            code = m.group(1).lower()
            if code in COUNTRY_SYNONYMS:
                return s[m.end():].strip(), code
        return s.strip(), None

    def _strip_country_suffix(s):
        s = re.sub(rf'\.({_CC_PATTERN})$', '', s, flags=re.I)
        s = re.sub(rf'\s+\(?({_CC_PATTERN})\)?$', '', s, flags=re.I)
        return s.strip()

    def _norm_code(code):
        return COUNTRY_SYNONYMS.get((code or "").lower(), (code or "").lower())

    def _strip_quality(s):
        s = (s or "").lower().strip()
        for tag in QUALITY_TAGS:
            s = s.replace(f" {tag}", "").replace(f"({tag})", "").replace(f"[{tag}]", "")
        return re.sub(r"\s+", " ", s).strip()

    def _core(s):
        """Strip country prefix + suffix + quality tags → pure channel name."""
        stripped, _ = _strip_country_prefix(s)
        stripped = _strip_country_suffix(stripped)
        return _strip_quality(stripped)

    def _core_words(s):
        return set(re.findall(r"[a-z0-9]+", _core(s)))

    def _has_hevc(s):
        sl = (s or "").lower()
        return any(t in sl for t in HEVC_TAGS)

    # Pre-process EPG side
    epg_name_l    = epg_channel_name.lower().strip()
    epg_core      = _core(epg_channel_name)
    epg_cwords    = _core_words(epg_channel_name)
    _, epg_cc_raw = _strip_country_prefix(epg_channel_name)
    if not epg_cc_raw:
        m = re.search(rf'\.({_CC_PATTERN})$', epg_channel_name, re.I)
        if m:
            epg_cc_raw = m.group(1)
    epg_cc = _norm_code(epg_cc_raw)   # canonical country code or ""

    state.log(f"[FIND_CH] EPG core={epg_core!r} country={epg_cc!r} words={epg_cwords}")

    scored = []   # list of (score, ch) — collect all to log top candidates

    for ch in channels:
        ch_name     = (ch.get("name") or ch.get("stream_name") or ch.get("title") or "").strip()
        ch_tvg_id   = (ch.get("epg_channel_id") or ch.get("tvg_id") or "").strip().lower()
        ch_name_l   = ch_name.lower()
        score = 0

        ch_core_str, ch_cc_raw = _strip_country_prefix(ch_name)
        ch_core_str = _strip_country_suffix(ch_core_str)
        ch_core_str = _strip_quality(ch_core_str)
        ch_cc = _norm_code(ch_cc_raw)

        # ── Country conflict check ────────────────────────────────────────────
        # If BOTH sides have explicit country codes and they differ → hard cap at 45
        # This prevents DE: channel from beating RS: channel
        country_conflict = bool(epg_cc and ch_cc and epg_cc != ch_cc)

        # ── tvg-id match ──────────────────────────────────────────────────────
        if epg_channel_id and ch_tvg_id:
            if epg_channel_id == ch_tvg_id:
                score = 100
            elif epg_channel_id in ch_tvg_id or ch_tvg_id in epg_channel_id:
                score = max(score, 80)

        # ── Exact name ───────────────────────────────────────────────────────
        if ch_name_l == epg_name_l:
            score = max(score, 90)

        # ── Core name match (stripped of country + quality tags) ──────────────
        if epg_core and ch_core_str and epg_core == ch_core_str:
            if epg_cc and ch_cc and epg_cc == ch_cc:
                score = max(score, 85)   # same core + same country
            elif not epg_cc or not ch_cc:
                score = max(score, 75)   # same core, one side has no country
            else:
                score = max(score, 45)   # same core but different countries

        # ── Core contains ────────────────────────────────────────────────────
        # Only trigger if the shorter core has ≥2 words — prevents single words
        # like "jazz" (from "PL| JAZZ HD") from matching "NBA - Utah Jazz"
        if epg_core and ch_core_str:
            short, long_ = (ch_core_str, epg_core) if len(ch_core_str) < len(epg_core) else (epg_core, ch_core_str)
            short_words = set(re.findall(r"[a-z0-9]+", short))
            if len(short_words) >= 2 and short in long_:
                score = max(score, 48)

        # ── Word overlap on core words ────────────────────────────────────────
        if epg_cwords and ch_core_str:
            ch_cw = _core_words(ch_name)
            if ch_cw:
                overlap = len(epg_cwords & ch_cw)
                if overlap:
                    # Proportional score based on coverage of the LARGER set
                    total = max(len(epg_cwords), len(ch_cw))
                    word_score = int(60 * overlap / total)
                    score = max(score, word_score)

                    # All-words-match bonus: if ALL EPG words are present in channel
                    # (e.g. 'nba','utah','jazz' all in 'NBA: UTAH JAZZ HD') → big boost
                    if epg_cwords.issubset(ch_cw):
                        score = max(score, 72)
                    # Partial but dominant match (≥2 words AND covers ≥2/3 of EPG words)
                    elif overlap >= 2 and overlap / len(epg_cwords) >= 0.66:
                        score = max(score, 55)

        # ── Apply hard country conflict cap ───────────────────────────────────
        if country_conflict:
            score = min(score, 45)

        # ── HEVC penalty — deprioritize when non-HEVC alternatives likely exist
        if _has_hevc(ch_name):
            score = max(0, score - 10)

        scored.append((score, ch_name, ch))

    # Sort and pick best
    scored.sort(key=lambda x: -x[0])

    # Log top 5 candidates for debugging
    state.log(f"[FIND_CH] Top candidates for {epg_channel_name!r}:")
    for s, n, _ in scored[:5]:
        state.log(f"[FIND_CH]   score={s:3d}  {n!r}")

    best_score, _, best_channel = scored[0] if scored else (0, "", None)

    MIN_SCORE = 30
    if not best_channel or best_score < MIN_SCORE:
        return jsonify({"found": False, "score": best_score,
                        "message": f"No match found (best score: {best_score})"})

    # Build a tidy result dict
    result_name = (best_channel.get("name") or best_channel.get("stream_name")
                   or best_channel.get("title") or "Unknown")
    result_cat  = (best_channel.get("genre_title") or best_channel.get("category_name")
                   or best_channel.get("group_title") or best_channel.get("group") or "")

    state.log(f"[FIND_CH] Best match: {result_name!r} score={best_score}")
    return jsonify({
        "found":    True,
        "score":    best_score,
        "name":     result_name,
        "cat":      result_cat,
        "channel":  best_channel,
    })


@flask_app.route("/api/catchup", methods=["POST"])
def api_catchup():
    """Fetch past archived programmes for a live channel.
    Uses get_simple_data_table (same as catchuptestv9 / SFVIP) which returns
    mark_archive flag and direct cmd per entry — the correct EPG source for
    Stalker portals.  Falls back to Xtream timeshift URL for Xtream portals.
    """

    data      = request.get_json(force=True)
    item      = data.get("item", {})
    start_ts  = int(data.get("start", 0))
    end_ts    = int(data.get("end",   0))
    if not start_ts:
        start_ts = int(datetime.now(timezone.utc).timestamp()) - 86400 * 3
    if not end_ts:
        end_ts = int(datetime.now(timezone.utc).timestamp())
    duration_min = max(1, math.ceil((end_ts - start_ts) / 60))

    conn = state.conn_type

    async def _resolve():
        # ── Xtream timeshift ──────────────────────────────────────────────────
        if conn == "xtream" or (conn == "m3u_url" and state.m3u_xtream_override):
            creds = state.m3u_xtream_override if conn == "m3u_url" else None
            base  = (creds["base"] if creds else state.url).rstrip("/")
            user  = creds["username"] if creds else state.username
            pwd   = creds["password"] if creds else state.password
            sid   = str(item.get("stream_id") or item.get("id") or "").strip()
            if not sid:
                return {"error": "No stream_id for Xtream catch-up"}

            from urllib.parse import urlparse as _up, quote as _q
            _p = _up(base)
            base_norm = f"{_p.scheme}://{_p.netloc}"
            now_ts = datetime.now(timezone.utc).timestamp()

            # ── Step 1: get_epg — full channel schedule including past entries ───
            # get_epg returns the full EPG listing for the channel (past + future),
            # sorted newest-first on most panels.  This is the correct endpoint for
            # catchup because it includes historical programmes the user can rewind to.
            # get_short_epg only returns a handful of current/next entries and never
            # contains past programmes, so it is useless here.
            epg_api_url = (f"{base_norm}/player_api.php"
                           f"?username={_q(user, safe='')}&password={_q(pwd, safe='')}"
                           f"&action=get_epg&stream_id={sid}")
            state.log(f"[CatchUp] Xtream get_epg stream_id={sid}")

            results = []
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(epg_api_url,
                                        timeout=aiohttp.ClientTimeout(total=12)) as r:
                        payload = await safe_json(r)

                parsed = _parse_xtream_short_epg(payload)
                all_entries = parsed.get("schedule", [])
                state.log(f"[CatchUp] Xtream EPG entries: {len(all_entries)} total")

                # Include past entries (start < now) — these are the ones the user
                # can rewind to via timeshift.
                for ep in all_entries:
                    ep_end   = ep.get("end", 0)
                    ep_start = ep.get("start", 0)
                    if ep_start and ep_end and ep_start < now_ts:
                        results.append({
                            "title":        ep.get("title") or "Unknown",
                            "start":        ep_start,
                            "stop":         ep_end,
                            # Store stream_id in cmd/live_cmd so api_catchup/play
                            # can build the timeshift URL without extra state.
                            "cmd":          sid,
                            "live_cmd":     sid,
                            "mark_archive": "1",  # Xtream always supports timeshift
                            "epg_id":       "",
                            "id":           "",
                            "ch_id":        "",
                        })

                # Sort newest first (same as MAC catchup)
                results.sort(key=lambda x: x.get("start", 0), reverse=True)

            except Exception as e:
                state.log(f"[CatchUp] Xtream EPG fetch error: {e}")

            # ── Step 2: fallback to portal XMLTV for past-3-days window ───────────
            # _fetch_xmltv_epg is designed for current/next EPG (±1-3h window).
            # For catchup we access the XMLTV index cache directly and apply a
            # wider 3-day past window ourselves.  We also try channel name as
            # lookup when no dedicated epg_channel_id is available.
            if not results:
                epg_ch_id = str(item.get("epg_channel_id") or "").strip()
                tvg_name  = str(item.get("name") or "").strip()
                lookup_id = epg_ch_id or tvg_name  # fall back to channel name
                if lookup_id and base_norm not in state._xmltv_no_data:
                    xmltv_url = (f"{base_norm}/xmltv.php"
                                 f"?username={_q(user, safe='')}&password={_q(pwd, safe='')}")
                    state.log(f"[CatchUp] Xtream XMLTV fallback (lookup={lookup_id!r})")
                    try:
                        # Build / retrieve the per-portal XMLTV index (shared with EPG cache)
                        ck = base_norm
                        cached_xmltv = state._xmltv_cache.get(ck)
                        if cached_xmltv:
                            _, epg_dict, chan_names = cached_xmltv
                        else:
                            epg_dict, chan_names = await _build_xmltv_index(
                                xmltv_url, state.log)
                            state._xmltv_cache[ck] = (time.time(), epg_dict, chan_names)
                            if not epg_dict:
                                state._xmltv_no_data.add(ck)

                        if epg_dict:
                            lookup_lower = lookup_id.strip().lower()
                            entries = epg_dict.get(lookup_lower)
                            # Display-name fallback: match via <display-name> in XMLTV
                            if not entries:
                                for cid, names in chan_names.items():
                                    if (lookup_lower in names
                                            or any(lookup_lower in n or n in lookup_lower
                                                   for n in names)):
                                        entries = epg_dict.get(cid)
                                        if entries:
                                            state.log(f"[CatchUp] XMLTV name match:"
                                                      f" {lookup_id!r} → {cid!r}")
                                            break

                            if entries:
                                cutoff = now_ts - 86400 * 3   # look back up to 3 days
                                for ep in entries:
                                    # Support both tuple (title,start,end,desc) and legacy dict
                                    if isinstance(ep, tuple):
                                        ep_title, ep_start, ep_end = ep[0], ep[1], ep[2]
                                    else:
                                        ep_title  = ep.get("title") or "Unknown"
                                        ep_start  = ep.get("start", 0)
                                        ep_end    = ep.get("end",   0)
                                    if ep_start and ep_end and cutoff <= ep_start < now_ts:
                                        results.append({
                                            "title":        ep_title or "Unknown",
                                            "start":        ep_start,
                                            "stop":         ep_end,
                                            "cmd":          sid,
                                            "live_cmd":     sid,
                                            "mark_archive": "1",
                                            "epg_id":       "",
                                            "id":           "",
                                            "ch_id":        "",
                                        })
                                results.sort(key=lambda x: x.get("start", 0), reverse=True)
                                state.log(f"[CatchUp] XMLTV gave {len(results)} past entries")
                    except Exception as e:
                        state.log(f"[CatchUp] Xtream XMLTV fallback error: {e}")

            if not results:
                return {"error": "No past EPG data found. Use the manual time picker below."}

            return {"archive_listings": results, "label": item.get("name", "")}

        # ── Stalker / MAC portal — get_simple_data_table ──────────────────────
        # This is the correct API (same as SFVIP/TiviMate). Returns mark_archive
        # flag per entry plus direct archive cmd. get_epg_info only returns today's
        # upcoming schedule and does NOT have mark_archive.
        if conn == "mac":
            cmd_field  = str(item.get("cmd") or "").strip()
            item_ch_id = str(item.get("ch_id") or item.get("id") or "").strip()
            m          = re.search(r'/ch/(\d+)', cmd_field)
            cmd_ch_id  = m.group(1) if m else None
            ch_id      = item_ch_id or cmd_ch_id
            state.log(f"[CatchUp] ch_id={ch_id}")
            if not ch_id:
                return {"error": "No channel ID for catch-up"}

            php        = "/stalker_portal/server/load.php" if state.is_stalker_portal else "/portal.php"
            base_url   = normalize_base_url(state.url)
            client_cls = StalkerPortalClient if state.is_stalker_portal else PortalClient

            def _to_ts(v):
                if not v: return 0
                try: return int(v)
                except: pass
                try: return int(datetime.strptime(str(v), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
                except: return 0

            results = []
            async with client_cls(state.url, state.mac, state.log) as client:
                await client.handshake()
                hdrs = client._headers(include_auth=True) if state.is_stalker_portal else client.headers

                for day_offset in range(4):
                    day_ts   = int(datetime.now(timezone.utc).timestamp()) - day_offset * 86400
                    date_str = datetime.fromtimestamp(day_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    page = 1
                    while True:
                        epg_url = (f"{base_url}{php}?type=epg&action=get_simple_data_table"
                                   f"&ch_id={ch_id}&date={date_str}&p={page}&JsHttpRequest=1-xml")
                        state.log(f"[CatchUp] get_simple_data_table ch={ch_id} date={date_str} p={page}")
                        try:
                            async with client.session.get(epg_url, headers=hdrs,
                                                          timeout=aiohttp.ClientTimeout(total=10)) as r:
                                payload = await safe_json(r)
                        except Exception as e:
                            state.log(f"[CatchUp] fetch error: {e}")
                            break
                        js   = payload.get("js", {}) if isinstance(payload, dict) else {}
                        rows = (js.get("data") or []) if isinstance(js, dict) else (js if isinstance(js, list) else [])
                        if not rows:
                            break
                        state.log(f"[CatchUp] date={date_str} p={page} → {len(rows)} entries")
                        first_logged = False
                        for ep in rows:
                            if not isinstance(ep, dict):
                                continue
                            if not first_logged:
                                state.log(f"[CatchUp] fields: {list(ep.keys())}")
                                first_logged = True
                            mark_archive = str(ep.get("mark_archive", 0))
                            archive_cmd  = str(ep.get("cmd") or "").strip()
                            raw_real_id  = str(ep.get("real_id") or "").strip()
                            # The EPG entry 'id' is the sequential archive recording ID
                            # (e.g. '537163805') — this is what SFVip uses to build
                            # cmd='auto /media/{id}.mpg' for type=tv_archive.
                            # 'real_id' is a portal-internal field and is NOT used.
                            epg_id = str(ep.get("id") or "").strip()
                            valid_epg_id = (
                                epg_id
                                if (re.match(r'^\d+$', epg_id) and epg_id not in ('0', ''))
                                else ""
                            )
                            st = _to_ts(ep.get("start_timestamp") or ep.get("time"))
                            sp = _to_ts(ep.get("stop_timestamp")  or ep.get("time_to"))
                            if not st:
                                continue
                            state.log(
                                f"[CatchUp] '{ep.get('name','?')}' mark_archive={mark_archive}"
                                f" id={epg_id!r} real_id={raw_real_id!r}"
                            )
                            results.append({
                                "title":        ep.get("name") or ep.get("o_name") or "Unknown",
                                "start":        st,
                                "stop":         sp,
                                "cmd":          archive_cmd,
                                "live_cmd":     cmd_field,
                                "mark_archive": mark_archive,
                                "ch_id":        ep.get("ch_id") or "",
                                # epg_id is the sequential archive file ID used by SFVip:
                                # cmd = 'auto /media/{epg_id}.mpg' → type=tv_archive
                                "epg_id":       valid_epg_id,
                                "id":           valid_epg_id,
                            })
                        total = js.get("total_items", 0) if isinstance(js, dict) else 0
                        if not total or page * 14 >= int(total):
                            break
                        page += 1

            if not results:
                return {"error": "No EPG data found for this channel"}
            results.sort(key=lambda x: x["start"], reverse=True)
            return {"archive_listings": results, "label": item.get("name", "")}

        return {"error": "Catch-up not supported for this connection type"}

    try:
        return jsonify(run_async(_resolve()))
    except Exception as e:
        state.log(f"[CatchUp] Error: {e}")
        return jsonify({"error": str(e)})


@flask_app.route("/api/catchup/play", methods=["POST"])
def api_catchup_play():
    """Resolve a Stalker/MAC archive entry to a playable URL.
    Uses create_catchup_link (same params as providers.py resolve_catchup):
      type=itv, action=create_link, cmd=<live_cmd>, start=<local YYYY-MM-DD:HH-MM>,
      duration=<minutes>, series=1, forced_storage=0
    series=1 is REQUIRED — without it the portal returns the live stream.
    cmd must be the original live-channel stub (ffmpeg http:///ch/NNNN_), NOT
    an archive-specific cmd from EPG entries.
    """
    data     = request.get_json(force=True)
    cmd_in   = str(data.get("cmd")      or "").strip()
    live_cmd = str(data.get("live_cmd") or "").strip()
    epg_id   = str(data.get("epg_id")   or data.get("real_id") or "").strip()
    start_ts = int(data.get("start") or 0)
    stop_ts  = int(data.get("stop")  or 0)

    # Two-stage approach matching SFVip + providers.py:
    #
    # Stage 1 (SFVip sniff): type=tv_archive, cmd='auto /media/{epg_id}.mpg'
    #   — epg_id is the EPG entry's 'id' field (sequential archive recording ID).
    #   — series='' (empty), no start/duration params.
    #   — Returns a direct storage URL if the recording exists.
    #
    # Stage 2 (providers.py resolve_catchup): type=itv, cmd=<live_cmd>,
    #   series=1, start=YYYY-MM-DD:HH-MM, duration=<minutes>
    #   — Used when tv_archive fails (no recording / Flussonic-only portal).
    #   — Flussonic portals return a live token URL → rewrite to archive-{ts}-{dur}.m3u8.
    archive_cmd   = f"auto /media/{epg_id}.mpg" if epg_id else ""
    effective_cmd = live_cmd or cmd_in
    if not effective_cmd or not start_ts:
        return jsonify({"error": "Missing cmd or start timestamp"})
    if not stop_ts or stop_ts <= start_ts:
        stop_ts = start_ts + 3600

    async def _play():
        # ── Xtream: build timeshift URL directly — no portal call needed ──────
        _conn = state.conn_type
        if _conn == "xtream" or (_conn == "m3u_url" and state.m3u_xtream_override):
            # cmd_in / live_cmd carries the stream_id (set by api_catchup above)
            sid = live_cmd or cmd_in
            if not sid:
                return {"error": "Missing stream_id for Xtream catch-up"}
            creds = state.m3u_xtream_override if _conn == "m3u_url" else None
            base  = (creds["base"] if creds else state.url).rstrip("/")
            user  = creds["username"] if creds else state.username
            pwd   = creds["password"] if creds else state.password
            _p = urlparse(base)
            dur = max(1, math.ceil((stop_ts - start_ts) / 60))
            start_dt  = datetime.fromtimestamp(start_ts, tz=timezone.utc)
            # Format: YYYY-MM-DD:HH-MM  (date:time separator=colon, time uses dashes)
            start_fmt = start_dt.strftime("%Y-%m-%d:%H-%M")

            # Primary: path-based .ts format — routes through mpegts.js automatically.
            # Do NOT use quote() on credentials — raw values match what the server expects.
            cu_url = (f"{_p.scheme}://{_p.netloc}"
                      f"/timeshift/{user}/{pwd}/{dur}/{start_fmt}/{sid}.ts")

            # Fallback: query-string format for servers that don't support path-based format.
            # timeshift.php returns m3u8 so it routes through HLS.js.
            cu_url_fallback = (f"{_p.scheme}://{_p.netloc}/streaming/timeshift.php"
                               f"?username={user}&password={pwd}"
                               f"&stream={sid}&start={start_fmt}&duration={dur}")

            state.log(f"[CatchUp/Play] Xtream timeshift (path/primary)   -> {cu_url}")
            state.log(f"[CatchUp/Play] Xtream timeshift (query/fallback)  -> {cu_url_fallback}")
            return {"url": cu_url, "fallback_url": cu_url_fallback}

        # ── MAC / Stalker portal ──────────────────────────────────────────────
        start_dt_utc = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        start_local  = start_dt_utc.astimezone()   # local tz, same as utc_to_local()
        start_str    = start_local.strftime("%Y-%m-%d:%H-%M")
        duration_min = max(1, (stop_ts - start_ts) // 60)

        state.log(f"[CatchUp/Play] cmd={effective_cmd[:50]} archive_cmd={archive_cmd[:50] if archive_cmd else '(none)'} start={start_str} dur={duration_min}m")

        client_cls = StalkerPortalClient if state.is_stalker_portal else PortalClient
        async with client_cls(state.url, state.mac, state.log) as client:
            await client.handshake()
            url = await client.create_catchup_link(effective_cmd, start_str, duration_min,
                                                   archive_cmd=archive_cmd)
            # If tv_archive returned nothing (null storage response), fall back to
            # type=itv + start/duration which works on Flussonic-backed portals.
            if not url and archive_cmd:
                state.log(f"[CatchUp/Play] tv_archive failed — retrying with type=itv fallback")
                url = await client.create_catchup_link(effective_cmd, start_str, duration_min,
                                                       archive_cmd="")

        if not url:
            return {"error": "Portal returned no catch-up URL"}

        # Flussonic CDN: portal returns live token URL even for archive requests.
        # Detect /stream/mpegts?token=XYZ and rewrite to /stream/archive-{ts}-{dur}.m3u8?token=XYZ
        _pu   = urlparse(url)
        _qs   = parse_qs(_pu.query)
        _tok  = (_qs.get("token") or [None])[0]
        _path = _pu.path
        # Strip any live-manifest filename to get the stream base path.
        # Flussonic serves live via: mpegts, index.m3u8, mono.m3u8, playlist.m3u8, chunklist*, manifest*
        _live_manifest_re = r'/(mpegts|index\.m3u8|mono\.m3u8|playlist\.m3u8|chunklist[^/]*|manifest[^/]*)$'
        _stream_base = re.sub(_live_manifest_re, '', _path, flags=re.IGNORECASE)
        # A URL is a Flussonic live token URL if:
        #   - it has a token query param
        #   - its path ends with a known live-manifest name (NOT already an archive URL)
        _is_flussonic = (
            _tok and
            re.search(_live_manifest_re, _path, re.IGNORECASE) and
            not re.search(r'archive|timeshift', _path, re.IGNORECASE)
        )
        if _is_flussonic and _stream_base:
            dur_secs    = stop_ts - start_ts
            # Preserve any extra query params beyond 'token' (some CDNs need them)
            _extra_qs = '&'.join(
                f"{k}={v[0]}" for k, v in _pqs(_pu.query).items() if k != 'token'
            )
            archive_url = (f"{_pu.scheme}://{_pu.netloc}"
                           f"{_stream_base}/archive-{start_ts}-{dur_secs}.m3u8"
                           f"?token={_tok}"
                           + (f"&{_extra_qs}" if _extra_qs else ""))
            state.log(f"[CatchUp/Play] Flussonic → {archive_url}")
            return {"url": archive_url}

        state.log(f"[CatchUp/Play] Resolved → {url}")
        return {"url": url}

    try:
        return jsonify(run_async(_play()))
    except Exception as e:
        state.log(f"[CatchUp/Play] Error: {e}")
        return jsonify({"error": str(e)})


def _parse_xtream_short_epg(payload: dict) -> dict:
    """Parse Xtream player_api get_short_epg response.

    Response shape (two known variants):
      {"epg_listings": [{"title": b64, "start": "2024-01-01 20:00:00",
                          "end": "2024-01-01 21:00:00", "description": b64, ...}, ...]}
      {"js": {"data": [...]}}   — some panels wrap it

    title/description fields are base64-encoded on most panels.
    start/end are UTC strings "YYYY-MM-DD HH:MM:SS".
    """
    now = datetime.now(timezone.utc).timestamp()
    out = {"current": None, "next": None, "schedule": []}

    def _safe_b64(s: str) -> str:
        """Decode base64 if it looks encoded, else return as-is."""
        if not s:
            return s
        try:
            decoded = base64.b64decode(s + "==").decode("utf-8", errors="replace")
            if decoded.isprintable() and len(decoded) >= 1:
                return decoded.strip()
        except Exception:
            pass
        return s.strip()

    def _to_ts(val) -> float:
        """Convert Xtream EPG time value to UTC unix timestamp.
        Xtream panels use: start_timestamp (unix int), start (local datetime string),
        or occasionally time (unix int). Prefer unix timestamps over formatted strings.
        """
        if not val:
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip()
        # Integer string (most common for start_timestamp)
        try:
            return float(s)
        except ValueError:
            pass
        # Formatted datetime strings — Xtream sends these in the SERVER'S local time,
        # NOT UTC. Treat them as naive (local) so they round-trip correctly.
        # If the consumer needs UTC epoch, use the start_timestamp field instead.
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                # datetime.fromtimestamp(datetime.strptime(...).timestamp()) keeps local tz
                return datetime.strptime(s[:19], fmt).timestamp()
            except ValueError:
                continue
        return 0.0

    if not isinstance(payload, dict):
        return out

    # Unwrap js/data envelope if present
    listings = payload.get("epg_listings") or []
    if not listings:
        js = payload.get("js", {})
        if isinstance(js, dict):
            listings = js.get("data") or js.get("epg_listings") or []
        elif isinstance(js, list):
            listings = js

    if not isinstance(listings, list):
        return out

    entries = []
    for ep in listings:
        if not isinstance(ep, dict):
            continue
        title = _safe_b64(str(ep.get("title") or ep.get("name") or ""))
        desc  = _safe_b64(str(ep.get("description") or ep.get("desc") or ep.get("plot") or ""))
        # Prefer unix timestamp fields (start_timestamp, stop_timestamp) over formatted strings
        start = _to_ts(ep.get("start_timestamp") or ep.get("time") or ep.get("start"))
        end   = _to_ts(ep.get("stop_timestamp")  or ep.get("time_to") or ep.get("end") or ep.get("stop"))
        if not title or not start:
            continue
        entries.append({"title": title, "start": start, "end": end, "desc": desc})

    entries.sort(key=lambda x: x["start"])
    out["schedule"] = entries

    for ep in entries:
        if ep["start"] <= now < ep["end"]:
            out["current"] = ep
        elif ep["start"] > now and out["next"] is None:
            out["next"] = ep

    # If nothing matched by time window, pick closest past as current and first future as next
    if not out["current"] and entries:
        past = [e for e in entries if e["end"] <= now]
        future = [e for e in entries if e["start"] > now]
        if past:
            out["current"] = past[-1]
        if future:
            out["next"] = future[0]

    return out


def _parse_stalker_epg(payload: dict, ch_id: str) -> dict:
    """Parse Stalker/MAC get_epg_info / get_short_epg response."""
    out = {"current": None, "next": None, "schedule": []}
    if not isinstance(payload, dict):
        return out
    now = datetime.now(timezone.utc).timestamp()

    def _to_ts(val):
        """Convert value to UTC unix timestamp. Handles int or 'YYYY-MM-DD HH:MM:SS' string."""
        if not val:
            return 0
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip()
        try:
            return float(s)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
        return 0

    js = payload.get("js", {})
    listings = []

    if isinstance(js, list):
        for entry in js:
            if isinstance(entry, dict) and str(entry.get("ch_id", "")) == str(ch_id):
                listings.append(entry)
        if not listings:
            listings = js

    elif isinstance(js, dict):
        inner = js.get("data") or js
        if isinstance(inner, dict):
            listings = (inner.get(str(ch_id)) or inner.get(ch_id)
                        or next(iter(inner.values()), []))
        if isinstance(listings, dict):
            listings = list(listings.values())

    if not isinstance(listings, list):
        return out

    for ep in listings:
        if not isinstance(ep, dict):
            continue
        try:
            start = _to_ts(ep.get("time") or ep.get("start_timestamp") or ep.get("start"))
            end   = _to_ts(ep.get("time_to") or ep.get("stop_timestamp") or ep.get("stop") or ep.get("end"))
            title = str(ep.get("name") or ep.get("title") or "").strip()
            desc  = str(ep.get("descr") or ep.get("description") or ep.get("desc") or "").strip()
            if not title or not start:
                continue
            prog = {"title": title, "start": start, "end": end, "desc": desc}
            out["schedule"].append(prog)
            if start <= now < end:
                out["current"] = prog
            elif start > now and out["next"] is None:
                out["next"] = prog
        except Exception:
            continue
    return out


async def _build_xmltv_index(xmltv_url: str, log_cb=None,
                             win_back_h: int = 4, win_fwd_h: int = 20) -> dict:
    """Download XMLTV and build a time-windowed channel→programmes index.

    Memory optimisations vs the naive approach:
      1. Time-window filter at parse time — only programmes that overlap
         [now - win_back_h .. now + win_fwd_h] are kept.  A 7-day feed for
         22k channels produces ~1.36M entries; a 30h window cuts that to
         ~115k — roughly a 12× reduction before any other tricks.
      2. Compact tuple storage — each programme is stored as a plain tuple
         (title, start, end, desc) instead of a dict.  A 4-item tuple costs
         ~88 bytes of overhead vs ~240 bytes for a dict, saving another ~35%.
      3. Description truncation — descs are capped at 200 chars; the full
         text is rarely displayed and can be extremely long in some feeds.

    Net effect: ~1500 MB → ~100 MB for a 185 MB / 22k-channel feed.

    Programme tuples are converted back to dicts by _fetch_xmltv_epg at
    lookup time (one channel at a time, negligible cost).

    Returns: (epg_dict, chan_names)
      epg_dict   = {channel_id_lower: [(title, start, end, desc), ...]}
      chan_names  = {channel_id_lower: [display_name_lower, ...]}
    """
    _log = log_cb or (lambda x: None)

    def _ts(s: str) -> float:
        s = s.strip()
        try:
            dt = datetime.strptime(s[:14], "%Y%m%d%H%M%S")
            offset = 0
            if len(s) > 14:
                tz = s[14:].strip()
                sign = 1 if tz.startswith("+") else -1
                h, m = int(tz[1:3]), int(tz[3:5])
                offset = sign * (h * 3600 + m * 60)
            return dt.replace(tzinfo=timezone.utc).timestamp() - offset
        except Exception:
            return 0.0

    # Time window: only keep programmes that overlap [win_start .. win_end].
    # A programme is included if it ends after win_start AND starts before win_end.
    now_ts    = datetime.now(timezone.utc).timestamp()
    win_start = now_ts - win_back_h * 3600   # e.g. 6 h ago
    win_end   = now_ts + win_fwd_h  * 3600   # e.g. 24 h from now

    _log(f"[EPG] Downloading XMLTV from {xmltv_url}")
    _log(f"[EPG] Time window: -{win_back_h}h / +{win_fwd_h}h (discarding rest at parse time)")

    # Stream the response into a temp file to avoid OOM on large feeds
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xmltv") as tmp:
        tmp_path = tmp.name
        total_bytes = 0
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(xmltv_url, timeout=aiohttp.ClientTimeout(total=120)) as r:
                    if r.status != 200:
                        raise RuntimeError(f"XMLTV HTTP {r.status}")
                    async for chunk in r.content.iter_chunked(1 << 16):  # 64 KB chunks
                        tmp.write(chunk)
                        total_bytes += len(chunk)
        except Exception:
            with contextlib.suppress(Exception):
                os.remove(tmp_path)
            raise

    _log(f"[EPG] XMLTV downloaded {total_bytes // 1024}KB — parsing…")

    # Detect gzip by magic bytes or URL extension
    try:
        with open(tmp_path, "rb") as _f:
            _magic = _f.read(2)
        is_gz = xmltv_url.lower().rstrip("?").endswith(".gz") or _magic == b'\x1f\x8b'
    except Exception:
        is_gz = False

    if is_gz:
        _log(f"[EPG] Detected gzip — decompressing on-the-fly into parser (no second temp file)…")

    # ── Parsing is synchronous CPU-bound work (gzip decompress + iterparse).
    # Running it directly on the asyncio event loop blocks ALL other requests
    # until the parse finishes (can take 10-30s on large feeds).
    # Offload to a thread-pool executor so the event loop stays responsive.
    def _sync_parse() -> tuple:
        fh = _gzip.open(tmp_path, "rb") if is_gz else open(tmp_path, "rb")
        chan_names_: dict = {}
        epg_dict_:   dict = {}
        root_ = None
        total_seen_  = 0
        total_kept_  = 0
        try:
            context = ET.iterparse(fh, events=("start", "end"))
            for event, elem in context:
                if event == "start" and root_ is None:
                    root_ = elem
                    continue
                if event != "end":
                    continue
                tag = elem.tag
                if tag == "channel":
                    cid = (elem.get("id") or "").strip().lower()
                    if cid:
                        names = [dn.text.strip().lower()
                                 for dn in elem.findall("display-name") if dn.text]
                        chan_names_[cid] = names
                elif tag == "programme":
                    total_seen_ += 1
                    cid = (elem.get("channel") or "").strip().lower()
                    if cid:
                        start = _ts(elem.get("start", ""))
                        end   = _ts(elem.get("stop",  ""))
                        if end and end < win_start:
                            if root_ is not None and elem is not root_:
                                with contextlib.suppress(ValueError):
                                    root_.remove(elem)
                            continue
                        if start > win_end:
                            if root_ is not None and elem is not root_:
                                with contextlib.suppress(ValueError):
                                    root_.remove(elem)
                            continue
                        title = (elem.findtext("title") or "").strip()
                        desc  = (elem.findtext("desc")  or "").strip()[:200]
                        if title and start:
                            if cid not in epg_dict_:
                                epg_dict_[cid] = []
                            epg_dict_[cid].append((title, start, end, desc))
                            total_kept_ += 1
                if root_ is not None and elem is not root_:
                    try:
                        root_.remove(elem)
                    except ValueError:
                        pass
        finally:
            fh.close()
        return epg_dict_, chan_names_, total_seen_, total_kept_

    try:
        loop = asyncio.get_event_loop()
        epg_dict, chan_names, total_seen, total_kept = await loop.run_in_executor(
            None, _sync_parse)
    finally:
        # On Windows, os.remove can fail with PermissionError if the OS hasn't
        # fully released the file lock yet (even after fh.close()).
        # contextlib.suppress would silently eat that error and leave the file
        # behind, filling %TEMP% over time.  Retry a few times with short sleeps.
        for _attempt in range(5):
            try:
                os.remove(tmp_path)
                break
            except FileNotFoundError:
                break  # already gone — fine
            except Exception:
                if _attempt < 4:
                    time.sleep(0.1 * (2 ** _attempt))  # 0.1s, 0.2s, 0.4s, 0.8s

    # Estimated RAM: each kept entry ≈ 88 (tuple) + 80 (title) + 200 (desc) + 56 (2×float) bytes
    est_mb = (total_kept * 424) // (1024 * 1024)
    pct_kept = (total_kept / total_seen * 100) if total_seen else 0
    _log(f"[EPG] XMLTV index built: {len(epg_dict)} channels with programmes "
         f"(out of {len(chan_names)} channel defs)")
    _log(f"[EPG] Kept {total_kept:,} / {total_seen:,} programmes ({pct_kept:.0f}%) "
         f"in -{win_back_h}h/+{win_fwd_h}h window — est. RAM ~{est_mb} MB")
    if not epg_dict:
        _log(f"[EPG] XMLTV has channel defs but NO programme data — portal serves stub XMLTV")
    return epg_dict, chan_names


async def _fetch_xmltv_epg(xmltv_url: str, tvg_id: str, log_cb=None,
                           cache_key: str = "") -> dict:
    """Look up EPG for tvg_id using cached XMLTV index.
    cache_key should be base_norm (e.g. 'http://host:port') to share the
    index across all channels on the same portal.
    Never blocks the caller — if a download is in progress, returns immediately
    with a 'loading' error so the caller can retry later.
    """
    out = {"current": None, "next": None, "schedule": []}
    if not tvg_id:
        return out
    _log = log_cb or (lambda x: None)
    now = datetime.now(timezone.utc).timestamp()
    lookup = tvg_id.strip().lower()
    ck = cache_key or xmltv_url

    # Fast-path: portal already confirmed to have no programme data
    if ck in state._xmltv_no_data:
        _log(f"[EPG] Portal XMLTV has no programme data (flagged) — skipping")
        out["error"] = "Provider XMLTV contains no programme data"
        return out

    # ── Cache hit ─────────────────────────────────────────────────────────────
    cached = state._xmltv_cache.get(ck)
    if cached:
        cached_ts, epg_dict, chan_names = cached
        if time.time() - cached_ts < state._xmltv_cache_ttl:
            _log(f"[EPG] XMLTV cache hit for {ck}")
        else:
            _log(f"[EPG] XMLTV cache expired — refreshing")
            cached = None

    if not cached:
        # ── Non-blocking download: kick off background thread if not running ──
        # Do NOT acquire a lock that would block the Flask worker thread.
        # Instead: if a download is already in progress, return immediately with
        # a retryable error. The UI will retry on next EPG click.
        if ck in state._xmltv_downloading:
            _log(f"[EPG] XMLTV download in progress for {ck} — will retry")
            out["error"] = "EPG loading… please try again in a moment"
            return out

        # Mark as downloading and spawn a background thread
        state._xmltv_downloading.add(ck)
        _log(f"[EPG] Launching background XMLTV download for {ck}")

        def _bg_download():
            try:
                bg_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(bg_loop)
                epg_d, ch_n = bg_loop.run_until_complete(
                    _build_xmltv_index(xmltv_url, _log))
                bg_loop.close()
                state._xmltv_cache[ck] = (time.time(), epg_d, ch_n)
                if not epg_d:
                    state._xmltv_no_data.add(ck)
                _log(f"[EPG] Background XMLTV download complete for {ck}")
            except Exception as e:
                _log(f"[EPG] Background XMLTV error for {ck}: {e}")
            finally:
                state._xmltv_downloading.discard(ck)
                # Clear the "needs XMLTV" markers so all waiting channels get
                # a fresh EPG lookup now that the data is available.
                state._xmltv_needs.clear()
                # Also clear the per-channel "loading" cache entries so they
                # don't serve stale loading responses after the download finishes.
                stale = [k for k, v in list(state._epg_cache.items())
                         if "loading" in (v[1].get("error") or "").lower()]
                for k in stale:
                    state._epg_cache.pop(k, None)

        t = threading.Thread(target=_bg_download, daemon=True,
                             name=f"xmltv-dl-{ck[:30]}")
        t.start()
        out["error"] = "EPG loading… please try again in a moment"
        return out

    # ── Resolve channel ID → programme list ───────────────────────────────────
    entries = epg_dict.get(lookup)

    # Fallback: match via display-name if exact ID miss
    if not entries:
        for cid, names in chan_names.items():
            if lookup in names or any(lookup in n or n in lookup for n in names):
                entries = epg_dict.get(cid)
                if entries:
                    _log(f"[EPG] XMLTV display-name fallback: {tvg_id!r} → {cid!r}")
                    break

    if not entries:
        _log(f"[EPG] XMLTV: no programmes found for {tvg_id!r}")
        out["error"] = f"No EPG data in provider for '{tvg_id}'"
        return out

    _log(f"[EPG] XMLTV: {len(entries)} programmes for {tvg_id!r}")

    # Convert compact tuples (title, start, end, desc) back to dicts.
    # This happens once per lookup on one channel — negligible cost.
    def _to_dict(e):
        if isinstance(e, dict):
            return e
        # tuple: (title, start, end, desc)
        return {"title": e[0], "start": e[1], "end": e[2], "desc": e[3] if len(e) > 3 else ""}

    entries = [_to_dict(e) for e in entries]

    # Filter to window around now (keep past 1h and next 3h)
    window = [e for e in entries if e["end"] >= now - 3600 and e["start"] <= now + 10800]
    if not window:
        window = entries  # fallback: no filtering
    window.sort(key=lambda x: x["start"])
    out["schedule"] = window[:12]

    for ep in window:
        if ep["start"] <= now < ep["end"]:
            out["current"] = ep
        elif ep["start"] > now and out["next"] is None:
            out["next"] = ep

    _cur = out["current"]["title"] if out["current"] else None
    _nxt = out["next"]["title"] if out["next"] else None
    _log(f"[EPG] XMLTV result: current={_cur!r} next={_nxt!r}")
    return out


# ── /api/proxy image cache ────────────────────────────────────────────────────
# Logos served through /api/proxy are fetched from the origin on every browser
# request when no cache exists.  Portals also append random ?{number} query
# strings to logo URLs (e.g. 320/12019.jpg?8392 … ?42491) which defeats the
# browser's own cache even when we send Cache-Control headers.
#
# Solution: server-side in-memory image cache keyed by the URL *without* its
# query string.  First fetch stores (content_type, bytes); all subsequent
# requests for the same base path are served from memory instantly, regardless
# of what query string the portal appended.
#
# Cap: _PROXY_IMG_CACHE_MAX entries.  When exceeded we drop the oldest half
# (insertion-order dict) rather than evicting everything at once.
_proxy_img_cache: dict = {}           # norm_url → (content_type, bytes)
_proxy_img_cache_lock = threading.Lock()
_PROXY_IMG_CACHE_MAX = 1500           # ~150 MB at ~100 kB average logo size


# 1×1 transparent PNG returned instead of a 403 when a logo host blocks hotlinking.
# This lets the browser's onerror handler hide the <img> cleanly, avoids broken-image
# icons, and stops the log from being spammed with expected 403s.
_TRANSPARENT_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
    b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
)

# Hosts that are known to use cookie/session-based hotlink protection.
# Browser UA + Referer alone will never work for these — we serve the transparent
# PNG immediately without even attempting a network fetch, saving a round-trip.
_HOTLINK_BLOCKED_HOSTS = frozenset({
    "www.lyngsat.com",
    "lyngsat.com",
    "lyngsat-logo.com",
    "www.lyngsat-logo.com",
})


@flask_app.route("/api/proxy")
def api_proxy():
    url = request.args.get("url", "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return Response("Invalid URL", status=400)

    # Normalise double-slashes in the path that some portals embed in logo URLs
    # (e.g. "https://www.lyngsat.com//logo/tv/…" → single slash after the host).
    try:
        _p = urlparse(url)
        _clean_path = re.sub(r'/{2,}', '/', _p.path)
        if _clean_path != _p.path:
            # ParseResult is a namedtuple — _replace() + geturl() rebuilds the URL
            url = _p._replace(path=_clean_path).geturl()
    except Exception:
        pass

    # Cache key = URL with query string stripped.
    # Portals append random ?{number} version tokens to logo URLs; stripping
    # them means "320/12019.jpg?8392" and "320/12019.jpg?42491" share one entry.
    norm_url = url.split("?")[0]
    is_img_url = bool(re.search(r'\.(jpe?g|png|gif|webp|svg|ico|bmp)$', norm_url, re.I))

    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }

    # ── Known hotlink-blocked hosts: return transparent PNG immediately ───
    if is_img_url:
        try:
            _host = urlparse(url).netloc.lower()
        except Exception:
            _host = ""
        if _host in _HOTLINK_BLOCKED_HOSTS:
            hdrs = dict(cors)
            hdrs["Content-Type"] = "image/png"
            hdrs["Cache-Control"] = "public, max-age=86400"
            return Response(_TRANSPARENT_PNG, status=200, headers=hdrs)

    # ── Cache read ────────────────────────────────────────────────────────
    if is_img_url and "Range" not in request.headers:
        with _proxy_img_cache_lock:
            hit = _proxy_img_cache.get(norm_url)
        if hit:
            ct, data = hit
            hdrs = dict(cors)
            hdrs["Content-Type"] = ct
            hdrs["Content-Length"] = str(len(data))
            hdrs["Cache-Control"] = "public, max-age=86400"
            hdrs["X-Cache"] = "HIT"
            return Response(data, status=200, headers=hdrs)

    try:
        # Image requests need a browser-like User-Agent and a matching Referer.
        # Many logo CDNs (tmdb.org, fanart.tv, etc.) block VLC/curl UAs and
        # requests with no Referer. Stream requests keep VLC/3.0 so portals
        # recognise the player.
        if is_img_url:
            parsed_logo = urlparse(url)
            logo_origin = f"{parsed_logo.scheme}://{parsed_logo.netloc}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": logo_origin + "/",
                "Connection": "keep-alive",
            }
        else:
            headers = {
                "User-Agent": "VLC/3.0.0 LibVLC/3.0.0",
                "Accept": "*/*",
                "Connection": "keep-alive",
            }
        if "Range" in request.headers:
            headers["Range"] = request.headers["Range"]
        # proxies={} bypasses Windows system proxy (fixes ERR_UNEXPECTED_PROXY_AUTH)
        resp = _requests_lib.get(url, headers=headers, stream=True, timeout=20,
                                 allow_redirects=True, verify=False,
                                 proxies={"http": None, "https": None})
        ct = resp.headers.get("Content-Type", "application/octet-stream")
        is_img_ct = ct.split(";")[0].strip().startswith("image/")
        # Treat as image if either the URL extension or the response CT says so
        is_img = is_img_url or is_img_ct

        is_m3u8 = (re.search(r'\.(m3u8?|m3u)(\?|$)', url.split('?')[0], re.I) or
                   'mpegurl' in ct.lower() or 'x-mpegurl' in ct.lower())
        if is_m3u8:
            text = resp.text
            # Use resp.url (final URL after any redirects) as the base for resolving
            # relative segment/chunklist URLs. Using the original `url` would produce
            # wrong absolute URLs if the server redirected the manifest request.
            rewritten = _rewrite_m3u8(text, resp.url)
            return Response(rewritten, content_type="application/vnd.apple.mpegurl", headers=cors)

        # ── Image: read fully, cache, return ─────────────────────────────
        if is_img and "Range" not in request.headers:
            data = resp.content  # reads entire body at once — logos are small
            if resp.status_code == 200 and data:
                with _proxy_img_cache_lock:
                    if norm_url not in _proxy_img_cache:
                        if len(_proxy_img_cache) >= _PROXY_IMG_CACHE_MAX:
                            # Evict oldest half to stay under the cap
                            keys = list(_proxy_img_cache.keys())
                            for k in keys[:len(keys) // 2]:
                                del _proxy_img_cache[k]
                        _proxy_img_cache[norm_url] = (ct, data)
                hdrs = dict(cors)
                hdrs["Content-Type"] = ct
                hdrs["Content-Length"] = str(len(data))
                hdrs["Cache-Control"] = "public, max-age=86400"
                hdrs["X-Cache"] = "MISS"
                return Response(data, status=200, headers=hdrs)
            elif resp.status_code == 403:
                # Hotlink-blocked — serve transparent PNG so the browser's
                # onerror handler hides the <img> cleanly with no log spam.
                hdrs = dict(cors)
                hdrs["Content-Type"] = "image/png"
                hdrs["Cache-Control"] = "public, max-age=3600"
                return Response(_TRANSPARENT_PNG, status=200, headers=hdrs)
            else:
                state.log(f"[Proxy] HTTP {resp.status_code} ← {url[:80]}")
                hdrs = dict(cors)
                hdrs["Content-Type"] = ct
                return Response(data, status=resp.status_code, headers=hdrs)

        # ── Non-image: stream as before ───────────────────────────────────
        def _gen():
            try:
                for chunk in resp.iter_content(chunk_size=16384):
                    yield chunk
            except Exception:
                # Portal dropped connection mid-stream (normal for progressive VOD)
                return
        h = dict(cors)
        h["Content-Type"] = ct
        if "Content-Length" in resp.headers:
            h["Content-Length"] = resp.headers["Content-Length"]
        if "Content-Range" in resp.headers:
            h["Content-Range"] = resp.headers["Content-Range"]
        if resp.status_code not in (200, 206):
            state.log(f"[Proxy] HTTP {resp.status_code} ← {url[:80]}")
        return Response(stream_with_context(_gen()), status=resp.status_code, headers=h)
    except Exception as e:
        state.log(f"[Proxy] Error: {e} ← {url[:80]}")
        return Response(f"Proxy error: {e}", status=502)


@flask_app.route("/api/proxy", methods=["OPTIONS"])
def api_proxy_options():
    return Response("", headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    })


@flask_app.route("/api/browse_exe", methods=["GET"])
def api_browse_exe():
    """Open a native OS file picker and return the selected executable path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Select External Player Executable",
            filetypes=[
                ("Executable files", "*.exe *.bat *.cmd" if os.name == "nt" else "*"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        return jsonify({"path": path or ""})
    except Exception as e:
        return jsonify({"path": "", "error": str(e)})


@flask_app.route("/api/browse_subtitle", methods=["GET"])
def api_browse_subtitle():
    """Desktop only: open a native OS file picker for subtitle files."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Select Subtitle File",
            filetypes=[
                ("Subtitle files", "*.srt *.vtt *.ass *.ssa"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        return jsonify({"path": path or ""})
    except Exception as e:
        return jsonify({"path": "", "error": str(e)})


@flask_app.route("/api/load_subtitle_path", methods=["POST"])
def api_load_subtitle_path():
    """Android/mobile: read a subtitle file from an absolute path on the server filesystem."""
    data = request.get_json(force=True)
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400
    if not os.path.isfile(path):
        return jsonify({"error": f"File not found: {path}"}), 404
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".srt", ".vtt", ".ass", ".ssa", ".txt"):
        return jsonify({"error": f"Unsupported subtitle format: {ext}"}), 400
    try:
        with open(path, "rb") as f:
            raw = f.read()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw.decode("latin-1", errors="replace")
        mime_map = {".srt": "text/srt", ".vtt": "text/vtt",
                    ".ass": "text/x-ssa", ".ssa": "text/x-ssa"}
        mime = mime_map.get(ext, "text/srt")
        fname = os.path.basename(path)
        return jsonify({"content": content, "file_name": fname, "mime": mime})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/browse_dir", methods=["POST"])
def api_browse_dir():
    """List directory contents for the mobile subtitle file browser."""
    data = request.get_json(force=True)
    path = (data.get("path") or "/sdcard/Download").rstrip("/") or "/"
    try:
        entries = os.listdir(path)
    except PermissionError:
        return jsonify({"error": "Permission denied", "path": path, "dirs": [], "files": []}), 403
    except FileNotFoundError:
        return jsonify({"error": "Directory not found", "path": path, "dirs": [], "files": []}), 404
    except Exception as e:
        return jsonify({"error": str(e), "path": path, "dirs": [], "files": []}), 500

    sub_exts = {".srt", ".vtt", ".ass", ".ssa"}
    dirs, files = [], []
    for name in sorted(entries, key=lambda x: x.lower()):
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full):
                dirs.append(name)
            elif os.path.isfile(full) and os.path.splitext(name)[1].lower() in sub_exts:
                files.append(name)
        except Exception:
            pass

    parent = str(os.path.dirname(path)) if path not in ("/", "") else None
    return jsonify({"path": path, "parent": parent, "dirs": dirs, "files": files})


@flask_app.route("/api/get_tmdb_id", methods=["POST"])
def api_get_tmdb_id():
    """Fetch TMDB/IMDB metadata for an Xtream VOD or Series item."""
    data = request.get_json(force=True)
    stream_id = str(data.get("stream_id", "")).strip()
    series_id = str(data.get("series_id", "")).strip()
    if not (stream_id or series_id) or state.conn_type != "xtream":
        return jsonify({"tmdb_id": "", "imdb_id": ""})
    try:
        async def fetch():
            async with _make_client(do_handshake=True) as client:
                if series_id:
                    url = client._api("get_series_info", series_id=series_id)
                    async with client.session.get(url) as r:
                        d = await safe_json(r)
                    state.log(f"[TMDB] get_series_info top keys: {list(d.keys()) if isinstance(d, dict) else type(d)}")
                    info = (d.get("info") or d.get("movie_data") or d) if isinstance(d, dict) else {}
                    tmdb_id = str(info.get("tmdb_id") or info.get("tmdb") or "").strip()
                    imdb_id = str(info.get("imdb") or info.get("imdb_id") or "").strip()
                    state.log(f"[TMDB] get_series_info info keys: {list(info.keys()) if isinstance(info, dict) else type(info)} tmdb={tmdb_id!r} imdb={imdb_id!r}")
                else:
                    url = client._api("get_vod_info", vod_id=stream_id)
                    async with client.session.get(url) as r:
                        d = await safe_json(r)
                    info = (d.get("info") or d.get("movie_data") or d) if isinstance(d, dict) else {}
                    tmdb_id = str(info.get("tmdb_id") or info.get("tmdb") or "").strip()
                    imdb_id = str(info.get("imdb") or info.get("imdb_id") or "").strip()
                    state.log(f"[TMDB] get_vod_info keys: {list(info.keys()) if isinstance(info, dict) else type(info)} tmdb={tmdb_id!r} imdb={imdb_id!r}")
                return {"tmdb_id": tmdb_id, "imdb_id": imdb_id}
        result = run_async(fetch())
        return jsonify(result)
    except Exception as e:
        state.log(f"[TMDB] get_info error: {e}")
        return jsonify({"tmdb_id": "", "imdb_id": ""})


@flask_app.route("/api/browse_m3u", methods=["GET"])
def api_browse_m3u():
    """Desktop only: open a native OS file picker for M3U/M3U8 files."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Select M3U / M3U8 Playlist File",
            filetypes=[("M3U playlist files", "*.m3u *.m3u8"), ("All files", "*.*")],
        )
        root.destroy()
        return jsonify({"path": path or ""})
    except Exception as e:
        return jsonify({"path": "", "error": str(e)})


@flask_app.route("/api/browse_dir_m3u", methods=["POST"])
def api_browse_dir_m3u():
    """List directory contents for the mobile M3U file browser (.m3u/.m3u8 files only)."""
    data = request.get_json(force=True)
    path = (data.get("path") or "/sdcard/Download").rstrip("/") or "/"
    try:
        entries = os.listdir(path)
    except PermissionError:
        return jsonify({"error": "Permission denied", "path": path, "dirs": [], "files": []}), 403
    except FileNotFoundError:
        return jsonify({"error": "Directory not found", "path": path, "dirs": [], "files": []}), 404
    except Exception as e:
        return jsonify({"error": str(e), "path": path, "dirs": [], "files": []}), 500
    m3u_exts = {".m3u", ".m3u8"}
    dirs, files = [], []
    for name in sorted(entries, key=lambda x: x.lower()):
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full):
                dirs.append(name)
            elif os.path.isfile(full) and os.path.splitext(name)[1].lower() in m3u_exts:
                files.append(name)
        except Exception:
            pass
    parent = str(os.path.dirname(path)) if path not in ("/", "") else None
    return jsonify({"path": path, "parent": parent, "dirs": dirs, "files": files})


@flask_app.route("/api/read_m3u_path", methods=["POST"])
def api_read_m3u_path():
    """Read an M3U file from an absolute server-side path and return its text content."""
    data = request.get_json(force=True)
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400
    if not os.path.isfile(path):
        return jsonify({"error": f"File not found: {path}"}), 404
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".m3u", ".m3u8", ".txt"):
        return jsonify({"error": f"Unsupported format: {ext}"}), 400
    try:
        with open(path, "rb") as f:
            raw = f.read()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw.decode("latin-1", errors="replace")
        return jsonify({"content": content, "file_name": os.path.basename(path)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/resolve_url", methods=["POST"])
def api_resolve_url():
    """Resolve item stream URL without launching anything — used by mobile intent flow."""
    data = request.get_json(force=True)
    item = data.get("item", {})
    mode = data.get("mode", "live")
    cat  = data.get("category", {})
    try:
        async def _resolve():
            async with _make_client() as client:
                return await client.resolve_item_url(mode, item, cat)
        url = run_async(_resolve())
        if not url:
            return jsonify({"error": "Could not resolve stream URL"}), 400
        return jsonify({"url": url})
    except Exception as e:
        state.log(f"[EXT] Resolve error: {e}")
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/open_external", methods=["POST"])
def api_open_external():
    """Resolve item URL then launch it in the configured external player."""
    data = request.get_json(force=True)
    exe  = (data.get("exe") or "").strip()
    item = data.get("item", {})
    mode = data.get("mode", "live")
    cat  = data.get("category", {})
    pre_url = (data.get("url") or "").strip()  # pre-resolved URL (catchup / WON)

    if not exe:
        return jsonify({"error": "No external player configured"}), 400
    if not os.path.isfile(exe):
        return jsonify({"error": f"Player not found: {exe}"}), 400

    try:
        if pre_url:
            url = pre_url
        else:
            async def _resolve():
                async with _make_client() as client:
                    return await client.resolve_item_url(mode, item, cat)
            url = run_async(_resolve())
        if not url:
            return jsonify({"error": "Could not resolve stream URL"}), 400
        state.log(f"[EXT] Launching {os.path.basename(exe)} with stream URL")
        subprocess.Popen([exe, url], close_fds=True)
        return jsonify({"ok": True})
    except Exception as e:
        state.log(f"[EXT] Error: {e}")
        return jsonify({"error": str(e)}), 500

@flask_app.route("/api/hls_proxy")
def api_hls_proxy():
    """Transcode/remux stream for browser compatibility."""
    url = request.args.get("url", "").strip()
    if not url or not url.startswith(("http://", "https://", "rtsp://")):
        return Response("Invalid URL", status=400)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return Response("ffmpeg not available", status=503)
    
    transcode   = request.args.get("transcode", "0") == "1"
    audio_only  = request.args.get("audio_only", "0") == "1" and not transcode
    is_vod      = request.args.get("vod", "0") == "1"
    
    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }

    base_input = [
        ffmpeg, "-hide_banner", "-nostdin",
        "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-referer", url.rsplit('/', 1)[0] + "/",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-i", url,
    ]

    # Build ffmpeg command
    if transcode:
        # Full transcode: H.264 video + AAC audio (for HEVC video or combined issues)
        cmd = base_input + [
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
            "-f", "mpegts", "-",
        ]
        mode_str = "transcode"
    elif audio_only:
        # Audio-only transcode: copy video stream unchanged, re-encode audio to AAC.
        # Used when video is already H.264 but audio is AC3/EAC3/DTS/etc.
        # Much cheaper than full libx264 re-encode and avoids re-encoding artifacts.
        cmd = base_input + [
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
            "-f", "mpegts", "-",
        ]
        mode_str = "audio-transcode"
    else:
        # Remux only (copy all streams)
        cmd = [
            ffmpeg, "-hide_banner", "-nostdin",
            "-user_agent", "Mozilla/5.0",
            "-i", url,
            "-c", "copy",
            "-f", "mpegts", "-",
        ]
        mode_str = "remux"

    state.log(f"[ffmpeg/{mode_str}] Command: {' '.join(cmd[:10])}... [url redacted]")
    
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as e:
        state.log(f"[ffmpeg/{mode_str}] Failed to start: {e}")
        return Response(f"ffmpeg start error: {e}", status=502)

    # Capture stderr for debugging
    stderr_lines = []
    def _log_stderr():
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    stderr_lines.append(line)
                    # Log errors and important info
                    low = line.lower()
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
        chunk_count = 0
        killed_by_us = False
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    # Check if process died early
                    if chunk_count == 0:
                        time.sleep(0.5)  # Give stderr time to capture error
                        if stderr_lines:
                            state.log(f"[ffmpeg/{mode_str}] No output. Last error: {stderr_lines[-1][:100]}")
                    break
                chunk_count += 1
                yield chunk
        except GeneratorExit:
            killed_by_us = True   # Client disconnected / player switched — expected
        except Exception as e:
            state.log(f"[ffmpeg/{mode_str}] Generator error: {e}")
        finally:
            proc.kill()
            proc.wait()
            rc = proc.returncode
            if killed_by_us:
                # Normal: browser stopped reading because user switched episode/channel
                state.log(f"[ffmpeg/{mode_str}] Client disconnected after {chunk_count} chunks — stream stopped")
            elif rc == 0:
                state.log(f"[ffmpeg/{mode_str}] Finished cleanly after {chunk_count} chunks")
            else:
                state.log(f"[ffmpeg/{mode_str}] Exited with error (exit code {rc}) after {chunk_count} chunks"
                          + (f" — last stderr: {stderr_lines[-1][:120]}" if stderr_lines else ""))

    h = dict(cors)
    h["Content-Type"] = "video/mp2t"
    # Add cache-busting headers for VOD
    h["Cache-Control"] = "no-cache, no-store, must-revalidate"
    h["Pragma"] = "no-cache"
    
    return Response(stream_with_context(_gen()), status=200, headers=h)

# ===================== OPENSUBTITLES API =====================

OPENSUBTITLES_BASE    = "https://api.opensubtitles.com/api/v1"
OPENSUBTITLES_UA      = "IPTVPortalPlayer v1.0"

def _os_headers(api_key: str = ""):
    return {
        "Api-Key": api_key.strip(),
        "User-Agent": OPENSUBTITLES_UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


@flask_app.route("/api/subtitles/search", methods=["POST"])
def api_subtitles_search():
    data       = request.get_json(force=True)
    query      = (data.get("query") or "").strip()
    lang       = (data.get("lang") or "en").strip()
    season     = data.get("season")
    episode    = data.get("episode")
    sub_type   = (data.get("type") or "").strip()   # "movie" or "episode"
    max_results = int(data.get("max_results") or 20)
    api_key    = (data.get("api_key") or "").strip()

    if not query:
        return jsonify({"error": "No query provided", "results": []}), 400
    if not api_key:
        return jsonify({"error": "No OpenSubtitles API key set — add it in ⚙ Settings.", "results": []}), 400

    params = {"query": query, "languages": lang, "per_page": min(max_results, 40)}
    if sub_type in ("movie", "episode"):
        params["type"] = sub_type
    if season:
        params["season_number"] = int(season)
    if episode:
        params["episode_number"] = int(episode)

    try:
        r = _requests_lib.get(
            f"{OPENSUBTITLES_BASE}/subtitles",
            headers=_os_headers(api_key),
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json().get("data", [])
        # Slim down the payload sent to the browser
        results = []
        for item in raw:
            a    = item.get("attributes", {})
            feat = a.get("feature_details", {})
            files = a.get("files", [])
            if not files:
                continue
            results.append({
                "file_id":      files[0].get("file_id"),
                "file_name":    files[0].get("file_name", "subtitle"),
                "title":        feat.get("movie_name") or feat.get("title", "Unknown"),
                "year":         feat.get("year", ""),
                "season":       feat.get("season_number"),
                "episode":      feat.get("episode_number"),
                "feature_type": feat.get("feature_type", ""),
                "lang":         a.get("language", "?"),
                "rating":       a.get("ratings", "?"),
                "downloads":    a.get("download_count", 0),
                "uploader":     a.get("uploader", {}).get("name", "anonymous"),
                "release":      a.get("release", ""),
            })
        return jsonify({"results": results, "count": len(results)})
    except _requests_lib.HTTPError as e:
        return jsonify({"error": f"OpenSubtitles HTTP error: {e}", "results": []}), 502
    except Exception as e:
        return jsonify({"error": str(e), "results": []}), 500


@flask_app.route("/api/subtitles/download", methods=["POST"])
def api_subtitles_download():
    """Fetch subtitle file from OpenSubtitles and return its content."""
    data    = request.get_json(force=True)
    file_id = data.get("file_id")
    api_key = (data.get("api_key") or "").strip()
    if not file_id:
        return jsonify({"error": "No file_id provided"}), 400
    if not api_key:
        return jsonify({"error": "No OpenSubtitles API key set — add it in ⚙ Settings."}), 400
    try:
        r = _requests_lib.post(
            f"{OPENSUBTITLES_BASE}/download",
            headers=_os_headers(api_key),
            json={"file_id": int(file_id)},
            timeout=15,
        )

        # 406 = daily download quota exhausted — read body for reset time
        if r.status_code == 406:
            try:
                info = r.json()
                remaining  = info.get("remaining", 0)
                reset_time = info.get("reset_time", "")
                reset_str  = f"  Resets: {reset_time}" if reset_time else ""
                requests_  = info.get("requests", "?")
            except Exception:
                remaining, reset_str, requests_ = 0, "", "?"
            return jsonify({
                "error": (
                    f"Daily download quota reached ({requests_} used, {remaining} remaining).{reset_str}  "
                    f"Free accounts get 5 downloads/day — register at opensubtitles.com for 20/day."
                )
            }), 429

        # 401/403 = bad API key
        if r.status_code in (401, 403):
            return jsonify({"error": "Invalid OpenSubtitles API key — check your key in ⚙ Settings."}), 401

        r.raise_for_status()
        info   = r.json()
        dl_url = info.get("link")
        if not dl_url:
            return jsonify({"error": "No download link returned by OpenSubtitles"}), 502

        sub = _requests_lib.get(dl_url, timeout=30)
        sub.raise_for_status()

        # Detect encoding and decode
        content_bytes = sub.content
        try:
            content_text = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content_text = content_bytes.decode("latin-1", errors="replace")

        # Determine MIME type from file extension in URL or content
        fname = info.get("file_name", dl_url.split("?")[0].split("/")[-1])
        if fname.endswith(".ass") or fname.endswith(".ssa"):
            mime = "text/x-ssa"
        elif fname.endswith(".vtt"):
            mime = "text/vtt"
        else:
            mime = "text/srt"

        return jsonify({
            "content":   content_text,
            "file_name": fname,
            "mime":      mime,
            "remaining": info.get("remaining", "?"),
        })
    except _requests_lib.HTTPError as e:
        return jsonify({"error": f"OpenSubtitles HTTP error: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===================== HTML TEMPLATE =====================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#060612">
<title>IPTV Portal</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#060612;--s1:#0b0b1a;--s2:#10101e;--s3:#161628;--s4:#1c1c33;--s5:#23233d;
  --bdr:rgba(255,255,255,.07);--bdr2:rgba(255,255,255,.13);
  --acc:#7c3aed;--acc2:#6d28d9;--acc3:#5b21b6;
  --glow:rgba(124,58,237,.55);--glow2:rgba(124,58,237,.22);--glow3:rgba(124,58,237,.08);
  --cyan:#06b6d4;--green:#22c55e;--red:#ef4444;--orange:#f59e0b;--blue:#3b82f6;
  --txt:#e4e8f5;--txt2:#7d8a9e;--txt3:#3d4558;
  --r:12px;--rsm:8px;--rss:5px;
  --tr:all .2s cubic-bezier(.4,0,.2,1);
  --sh:0 8px 32px rgba(0,0,0,.7);
  /* glow helpers */
  --glow-acc: 0 0 20px rgba(124,58,237,.5), 0 0 50px rgba(124,58,237,.2);
  --glow-cyan: 0 0 18px rgba(6,182,212,.5), 0 0 40px rgba(6,182,212,.2);
  --glow-green: 0 0 18px rgba(34,197,94,.45);
  --glow-red: 0 0 18px rgba(239,68,68,.45);
}
html,body{height:100dvh;overflow:hidden;background:var(--bg);color:var(--txt);
  font-family:'Segoe UI',-apple-system,system-ui,sans-serif;font-size:14px;line-height:1.5;
  -webkit-font-smoothing:antialiased}

/* Scrollbar */
::-webkit-scrollbar{width:3px;height:3px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(124,58,237,.35);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:rgba(124,58,237,.6)}
::selection{background:rgba(124,58,237,.3);color:var(--acc)}

/* ─── inputs ─────────────────────────────────────────────────── */
input,textarea{background:rgba(0,0,0,.55);color:var(--txt);border:1.5px solid rgba(255,255,255,.1);
  border-radius:var(--rsm);padding:9px 12px;font-size:13px;outline:none;width:100%;
  transition:border-color .25s ease,box-shadow .25s ease,transform .2s ease;
  -webkit-appearance:none;box-shadow:inset 0 2px 8px rgba(0,0,0,.35)}
input:focus,textarea:focus{border-color:var(--acc);
  box-shadow:inset 0 2px 10px rgba(0,0,0,.4), 0 0 0 3px var(--glow2), 0 0 20px rgba(124,58,237,.2);
  transform:scale(1.005)}
input::placeholder,textarea::placeholder{color:var(--txt3);font-style:italic}
input[type=range]{background:transparent;border:none;box-shadow:none;padding:0;cursor:pointer;
  -webkit-appearance:auto;appearance:auto;transform:none}
input[type=checkbox]{width:auto;height:auto;padding:0;accent-color:var(--acc);transform:none;box-shadow:none}

/* ─── buttons ────────────────────────────────────────────────── */
button{cursor:pointer;border:none;border-radius:var(--rsm);padding:9px 16px;font-size:13px;
  font-weight:600;transition:var(--tr);outline:none;white-space:nowrap;
  -webkit-tap-highlight-color:transparent;user-select:none;position:relative;overflow:hidden}
/* Shine sweep — animates left→right on hover only, resets instantly on release */
button::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent);
  transition:none;pointer-events:none}
button:hover:not(:disabled)::before{left:100%;transition:left .45s ease}
/* Scale on active for regular buttons only — .nt tabs must not scale (breaks selection visual) */
button:not(.nt):active:not(:disabled){transform:scale(.94)}
button:disabled{opacity:.3;cursor:not-allowed}
/* Nav buttons: isolate stacking context so sweep clips per-button */
.nt{overflow:hidden!important;isolation:isolate}

.btn-acc{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;
  box-shadow:0 3px 14px var(--glow2),inset 0 1px 0 rgba(255,255,255,.15)}
.btn-acc:hover:not(:disabled){box-shadow:var(--glow-acc);filter:brightness(1.12);transform:translateY(-1px)}

.btn-green{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.3);
  box-shadow:0 0 12px rgba(34,197,94,.1)}
.btn-green:hover:not(:disabled){background:rgba(34,197,94,.18);border-color:rgba(34,197,94,.55);
  box-shadow:var(--glow-green);transform:translateY(-1px)}

.btn-red{background:rgba(239,68,68,.1);color:var(--red);border:1px solid rgba(239,68,68,.3);
  box-shadow:0 0 12px rgba(239,68,68,.1)}
.btn-red:hover:not(:disabled){background:rgba(239,68,68,.18);border-color:rgba(239,68,68,.55);
  box-shadow:var(--glow-red);transform:translateY(-1px)}

.btn-blue{background:rgba(59,130,246,.1);color:var(--blue);border:1px solid rgba(59,130,246,.3)}
.btn-blue:hover:not(:disabled){background:rgba(59,130,246,.2);border-color:rgba(59,130,246,.55);
  box-shadow:0 0 18px rgba(59,130,246,.4);transform:translateY(-1px)}

.btn-ghost{background:rgba(255,255,255,.04);color:var(--txt2);border:1px solid var(--bdr);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.btn-ghost:hover:not(:disabled){background:rgba(255,255,255,.09);color:var(--txt);
  border-color:var(--bdr2);box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}

.btn-sm{height:30px;padding:0 10px;font-size:12px;border-radius:var(--rss)}

/* ─── layout ─────────────────────────────────────────────────── */
/* Ambient background glow — subtle, non-distracting */
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse at top left,  rgba(124,58,237,.07) 0%,transparent 50%),
    radial-gradient(ellipse at top right, rgba(6,182,212,.05)  0%,transparent 50%),
    radial-gradient(ellipse at bottom,    rgba(124,58,237,.04) 0%,transparent 55%)}
#app{display:flex;flex-direction:column;height:100dvh;position:relative;z-index:1}

/* ─── header ─────────────────────────────────────────────────── */
#hdr{flex-shrink:0;z-index:200;position:relative;overflow:hidden;
  background:rgba(8,8,20,.94);backdrop-filter:blur(20px);
  border-bottom:1px solid rgba(124,58,237,.25);
  box-shadow:0 2px 20px rgba(0,0,0,.6),0 0 40px rgba(124,58,237,.06),inset 0 1px 0 rgba(255,255,255,.06)}
/* animated gradient scan-line at bottom of header */
#hdr::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--acc),var(--cyan),var(--acc),transparent);
  animation:hdrScan 4s ease-in-out infinite;opacity:.7}
@keyframes hdrScan{0%,100%{opacity:.35;transform:scaleX(.6)}50%{opacity:.9;transform:scaleX(1)}}
#hdr-bar{display:flex;align-items:center;gap:8px;padding:8px 12px;min-height:52px}
#cdot{width:9px;height:9px;border-radius:50%;background:var(--txt3);flex-shrink:0;transition:var(--tr)}
#cdot.on{background:var(--green);box-shadow:0 0 8px var(--green),0 0 20px rgba(34,197,94,.3);
  animation:pulse-dot 2.5s infinite}
#hdr-status{flex:1;font-size:12px;color:var(--txt2);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;min-width:0}
.hdr-r{display:flex;align-items:center;gap:5px;flex-shrink:0}
.tag{display:inline-flex;align-items:center;padding:2px 7px;border-radius:20px;font-size:10px;font-weight:700}
.tok{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.2)}
.terr{background:rgba(239,68,68,.1);color:var(--red);border:1px solid rgba(239,68,68,.2)}
.twrn{background:rgba(245,158,11,.1);color:var(--orange);border:1px solid rgba(245,158,11,.2)}
.hdr-ico{width:34px;height:34px;padding:0;display:inline-flex;align-items:center;
  justify-content:center;font-size:16px;border-radius:var(--rsm)}

/* ─── conn panel ─────────────────────────────────────────────── */
#cpanel{overflow:hidden;max-height:0;transition:max-height .35s cubic-bezier(.4,0,.2,1)}
#cpanel.open{max-height:560px}
#cpi{padding:4px 12px 14px;display:flex;flex-direction:column;gap:8px}
.ct-row{display:flex;gap:5px}
.ct-btn{flex:1;height:32px;font-size:12px;padding:0;border-radius:var(--rsm)}
.cr{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.cr label{font-size:11px;color:var(--txt2);flex-shrink:0;width:28px}
.cr input{flex:1;min-width:120px;height:34px;font-size:12px}
.cr-bot{display:flex;gap:7px;align-items:center;justify-content:space-between}

/* ─── main panels ─────────────────────────────────────────────── */
#main{flex:1;overflow:hidden;display:flex;min-height:0;transition:grid-template-columns .25s ease}
.panel{display:none;flex-direction:column;overflow:hidden;min-width:0;min-height:0}
.panel.active{display:flex!important;flex:1}
/* Mobile pctrl: stack vertically, record/mkv row below controls */
@media(max-width:899px){
  .pctrl{flex-direction:column;align-items:stretch;padding:8px 10px;gap:6px}
  .btn-vol-group{flex:unset;width:100%}
  .pctrl-desktop-only{display:none!important}
  .pctrl-mobile-rec{display:flex!important}
}
@media(min-width:900px){
  .pctrl-mobile-rec{display:none!important}
}
#pctrl-hdr{display:none}
#pctrl-body{max-height:none!important;overflow:visible!important}
@media(min-width:900px){
  #pctrl-hdr{display:flex}
  #pctrl-body{overflow:hidden!important;transition:max-height .25s ease;max-height:0!important}
  #pctrl-panel.expanded #pctrl-body{max-height:300px!important}
}
@media(min-width:900px){
  #main{display:grid!important;grid-template-columns:350px 36px 1fr;height:100%;transition:grid-template-columns .3s ease}
  #main.items-open{grid-template-columns:350px 350px 1fr}
  #main.items-open #p-items > *{opacity:1;transition:opacity .2s ease .15s}
  #main:not(.items-open) #p-items > *{opacity:0;pointer-events:none;transition:opacity .1s ease}
  #main:not(.items-open) #p-items::after{content:'›';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:16px;color:var(--txt3);pointer-events:none}
  #p-items{position:relative}
  .panel{display:flex!important;flex:unset;border-right:1px solid var(--bdr);height:100%}
  #theaterbtn{display:flex!important}
  #main.theater{grid-template-columns:0 0 1fr}
  #main.theater #p-cats,
  #main.theater #p-items{overflow:hidden;opacity:0;pointer-events:none}
  .panel:last-child{border-right:none}
  #botnav{display:none!important}
  /* On desktop, log panel is hidden — log is shown inline inside player */
  #p-log{display:none!important}
  /* Re-add log area at bottom of player panel on desktop */
  #desktop-log{display:flex!important}
  #desktop-log.expanded #desktop-log-body{max-height:200px!important}
  #desktop-log.expanded #desktop-log-arrow{transform:rotate(0deg)}
  #desktop-log #desktop-log-arrow{transform:rotate(180deg)}
  #pctrl-panel.expanded #pctrl-arrow{transform:rotate(0deg)}
  #pctrl-panel #pctrl-arrow{transform:rotate(180deg)}
}

/* ─── panel header ───────────────────────────────────────────── */
.ph{background:linear-gradient(90deg,rgba(11,11,26,.9),rgba(16,16,30,.9));
  border-bottom:1px solid rgba(124,58,237,.15);backdrop-filter:blur(12px);
  padding:10px 14px;display:flex;align-items:center;gap:8px;flex-shrink:0;position:relative}
.ph::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(124,58,237,.3),rgba(6,182,212,.2),transparent)}
.ph h3{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px;
  color:var(--txt2);flex:1;min-width:0}

/* ─── bottom nav ─────────────────────────────────────────────── */
#botnav{display:flex;background:rgba(8,8,20,.97);border-top:1px solid rgba(124,58,237,.2);
  flex-shrink:0;z-index:100;padding-bottom:env(safe-area-inset-bottom);
  backdrop-filter:blur(20px);box-shadow:0 -4px 20px rgba(0,0,0,.5),0 0 30px rgba(124,58,237,.05)}
.nt{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:8px 4px 10px;gap:3px;border:none;background:none;color:var(--txt3);
  font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
  transition:var(--tr);position:relative;border-radius:0;overflow:visible}
.nt.on{color:var(--acc)}
/* Indicator bar uses ::after — ::before belongs to the shine sweep and must not be touched */
.nt.on::after{content:'';position:absolute;top:0;left:25%;right:25%;height:2.5px;
  background:linear-gradient(90deg,var(--acc),var(--cyan));border-radius:0 0 4px 4px;
  box-shadow:0 0 10px var(--acc),0 0 20px rgba(124,58,237,.4);animation:pop-in .2s ease;
  pointer-events:none}
.nt-ico{font-size:22px;transition:var(--tr)}
.nt.on .nt-ico{transform:scale(1.2);filter:drop-shadow(0 0 6px var(--acc))}
.badge{position:absolute;top:4px;right:calc(50% - 22px);background:var(--acc);
  color:#fff;font-size:9px;font-weight:800;border-radius:10px;padding:1px 5px;
  min-width:16px;text-align:center;display:none;line-height:1.4;animation:pop-in .15s ease;
  box-shadow:0 0 8px var(--acc)}
.badge.vis{display:block}

/* ─── mode tabs ─────────────────────────────────────────────── */
.mtabs{display:flex;gap:4px}
.mt{padding:5px 11px;font-size:12px;font-weight:700;border-radius:20px;
  background:rgba(255,255,255,.03);color:var(--txt2);border:1px solid var(--bdr);
  transition:var(--tr);position:relative;overflow:hidden}
.mt::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.1),transparent);
  transition:left .4s ease;pointer-events:none}
.mt:hover::before{left:100%}
.mt:hover:not(.on){border-color:var(--bdr2);color:var(--txt);background:rgba(255,255,255,.06)}
.mt.on{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;
  border-color:transparent;box-shadow:0 2px 14px var(--glow2),0 0 28px rgba(124,58,237,.2),
  inset 0 1px 0 rgba(255,255,255,.2)}
@media(min-width:900px){
  .mtabs{gap:3px}\n  .mt{padding:5px 8px;font-size:11px}\n  .mt[data-m=\"favs\"]{padding:5px 7px}\n}
/* Desktop: show full labels with icons, nice spacing */
@media(min-width:900px){
  .mtabs{gap:6px;flex:1}
  .mt{padding:6px 14px;font-size:12px;letter-spacing:.3px}
  .mt[data-m="favs"]{padding:6px 10px}
  .mt[data-m="live"]{margin-left:auto}
  .mt-txt{display:inline}
  .mt-ico{display:inline;margin-right:4px}
}
.tag-bar{display:flex;flex-direction:column;gap:3px;padding:4px 10px 2px;flex-shrink:0}
.tag-bar::-webkit-scrollbar{display:none}
.tag-row{display:flex;gap:5px;overflow-x:auto;flex-wrap:nowrap;scrollbar-width:none;
  cursor:grab;user-select:none;-webkit-user-select:none}
.tag-row::-webkit-scrollbar{display:none}
.tag-row.dragging{cursor:grabbing}
.tag-row.dragging .tag-pill{pointer-events:none}
.tag-row-lbl{font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;
  color:var(--txt3);padding:0 2px;flex-shrink:0;align-self:center}
.tag-pill{padding:3px 10px;border-radius:20px;font-size:10px;font-weight:700;letter-spacing:.4px;
  cursor:pointer;white-space:nowrap;border:1px solid var(--bdr2);background:rgba(255,255,255,.03);
  color:var(--txt2);transition:all .15s;flex-shrink:0}
.tag-pill:hover{border-color:var(--acc);color:var(--acc);box-shadow:0 0 10px rgba(124,58,237,.2)}
.tag-pill.on{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;
  border-color:transparent;box-shadow:0 0 12px var(--glow2)}

/* ─── search bar ─────────────────────────────────────────────── */
.sbar{position:relative;flex-shrink:0}
.sbar input{padding-left:34px;height:36px;font-size:12px}
.sico{position:absolute;left:11px;top:50%;transform:translateY(-50%);
  font-size:13px;color:var(--txt3);pointer-events:none}

/* ─── category list ──────────────────────────────────────────── */
.cat-chk{
  width:18px!important;height:18px!important;min-width:18px;flex-shrink:0;
  accent-color:var(--acc);cursor:pointer;
  -webkit-appearance:checkbox!important;appearance:checkbox!important;
  border:none;box-shadow:none;padding:0;background:none}
.citem{display:flex;align-items:center;gap:10px;padding:11px 12px;border-radius:var(--rsm);
  cursor:pointer;margin-bottom:3px;transition:var(--tr);border:1px solid transparent;
  animation:fade-up var(--d,.3s) ease both;position:relative;overflow:hidden}
.citem:hover{background:rgba(124,58,237,.07);border-color:rgba(124,58,237,.25);
  transform:translateX(3px);box-shadow:0 0 14px rgba(124,58,237,.1),inset 0 1px 0 rgba(255,255,255,.04)}
.citem:active{transform:scale(.97) translateX(2px)}
/* shine sweep */
.citem::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.06),transparent);
  transition:left .5s ease;pointer-events:none;z-index:0}
.citem:hover::before{left:100%}
.citem::after{content:'';position:absolute;inset:0;opacity:0;transition:opacity .2s;
  background:linear-gradient(90deg,rgba(124,58,237,.06),transparent);pointer-events:none}
.citem:hover::after{opacity:1}
.c-ico{font-size:16px;flex-shrink:0;z-index:1}
.c-name{flex:1;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;z-index:1}
.c-arr{font-size:10px;color:var(--txt3);flex-shrink:0;z-index:1;transition:var(--tr)}
.citem:hover .c-arr{color:var(--acc);transform:translateX(3px)}

/* ─── skeleton ───────────────────────────────────────────────── */
.skel{height:52px;border-radius:var(--rsm);margin-bottom:4px;display:flex;
  align-items:center;gap:10px;padding:0 12px;
  background:var(--s2);border:1px solid var(--bdr)}
.skel::before{content:'';width:32px;height:32px;border-radius:6px;flex-shrink:0;
  background:linear-gradient(90deg,var(--s3) 25%,var(--s4) 50%,var(--s3) 75%);
  background-size:200% 100%;animation:shimmer 1.4s infinite}
.skel::after{content:'';flex:1;height:14px;border-radius:4px;
  background:linear-gradient(90deg,var(--s3) 25%,var(--s4) 50%,var(--s3) 75%);
  background-size:200% 100%;animation:shimmer 1.4s infinite .1s}
.skel-sm{height:38px;border-radius:var(--rsm);margin-bottom:3px;display:flex;
  align-items:center;gap:10px;padding:0 12px;
  background:var(--s2);border:1px solid var(--bdr)}
.skel-sm::before{content:'';width:22px;height:22px;border-radius:4px;flex-shrink:0;
  background:linear-gradient(90deg,var(--s3) 25%,var(--s4) 50%,var(--s3) 75%);
  background-size:200% 100%;animation:shimmer 1.4s infinite}
.skel-sm::after{content:'';flex:1;height:11px;border-radius:3px;
  background:linear-gradient(90deg,var(--s3) 25%,var(--s4) 50%,var(--s3) 75%);
  background-size:200% 100%;animation:shimmer 1.4s infinite .08s}
/* loading label in panel header */
.loading-lbl{font-size:11px;color:var(--acc);display:flex;align-items:center;gap:5px;animation:pulse 1.2s ease infinite}
@keyframes pulse{0%,100%{opacity:.5}50%{opacity:1}}

/* ─── item list ──────────────────────────────────────────────── */
.bcrum{font-size:11px;color:var(--txt3);margin-bottom:8px;display:flex;
  align-items:center;gap:4px;flex-wrap:wrap}
.bc-s{color:var(--txt2)}.bc-c{color:var(--acc);font-weight:600}.bc-x{font-size:9px}

.irow{display:flex;align-items:center;gap:7px;padding:8px 10px;border-radius:var(--rsm);
  margin-bottom:3px;background:rgba(255,255,255,.02);border:1px solid transparent;
  animation:fade-up var(--d,.25s) ease both;transition:var(--tr);position:relative;overflow:hidden}
.irow::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.05),transparent);
  transition:left .45s ease;pointer-events:none}
.irow:hover{background:rgba(124,58,237,.07);border-color:rgba(124,58,237,.22);
  box-shadow:0 0 12px rgba(124,58,237,.08)}
.irow:hover::before{left:100%}
.irow.now{background:linear-gradient(90deg,rgba(124,58,237,.15),rgba(124,58,237,.04));
  border-color:rgba(124,58,237,.4);box-shadow:inset 3px 0 0 var(--acc),0 0 18px rgba(124,58,237,.12)}
.irow.now .iname{color:var(--acc)}
.ichk{
  width:18px!important;height:18px!important;min-width:18px;flex-shrink:0;
  accent-color:var(--acc);cursor:pointer;
  -webkit-appearance:checkbox!important;appearance:checkbox!important;
  border:none;box-shadow:none;padding:0;background:none}
.ilogo{width:36px;height:24px;object-fit:contain;border-radius:3px;flex-shrink:0;
  background:var(--s4)}
.iname{flex:1;font-size:12px;overflow:hidden;white-space:nowrap;position:relative;cursor:default}
.iname-inner{display:inline-block;white-space:nowrap;padding-right:24px}
.iname.scrolling .iname-inner{animation:iname-scroll var(--scroll-dur,6s) linear infinite}
@keyframes iname-scroll{0%{transform:translateX(0)}100%{transform:translateX(var(--scroll-dist,-100%))}}
.ibtns{display:flex;gap:3px;flex-shrink:0}
.ibtns button{height:27px;padding:0 9px;font-size:11px;border-radius:var(--rss)}
/* ── item context menu ── */
#item-menu{position:fixed;z-index:800;background:var(--s3);border:1px solid var(--bdr);
  border-radius:10px;box-shadow:0 8px 32px rgba(0,0,0,.55);min-width:180px;
  overflow:hidden;display:none;flex-direction:column;animation:fade-up .15s ease both}
#item-menu.open{display:flex}
#item-menu-hdr{padding:8px 12px 6px;font-size:10px;font-weight:800;text-transform:uppercase;
  letter-spacing:1.2px;color:var(--txt3);border-bottom:1px solid var(--bdr);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}
.imenu-btn{display:flex;align-items:center;gap:9px;padding:9px 14px;
  font-size:12px;font-weight:600;color:var(--txt);background:none;border:none;
  cursor:pointer;text-align:left;transition:background .12s;width:100%}
.imenu-btn:hover{background:var(--s4)}
.imenu-btn .imenu-ico{font-size:14px;width:18px;text-align:center;flex-shrink:0}
.imenu-sep{height:1px;background:var(--bdr);margin:3px 0}

.ibottom{display:flex;flex-wrap:wrap;gap:5px;padding:8px 0 4px;
  border-top:1px solid var(--bdr);flex-shrink:0}
.ibottom button{flex:1;min-width:68px;height:34px;font-size:12px}
.icount{font-size:11px;color:var(--txt3);padding:3px 0;text-align:center;flex-shrink:0}

/* ─── EPG Grid layout ──────────────────────────────────────────────────────── */
#epg-grid-wrap{display:none;flex:1;flex-direction:column;min-height:0;overflow:hidden}
#epg-grid-wrap.active{display:flex}
/* Two-panel layout: ch-col (fixed, scrolls vertically only) + tl-col (scrolls both) */
#epg-grid-body{display:flex;flex:1;min-height:0;overflow:hidden}
#epg-ch-col{width:110px;min-width:110px;flex-shrink:0;overflow-y:auto;overflow-x:hidden;display:flex;flex-direction:column;
  scrollbar-width:none;border-right:1px solid var(--bdr2)}
#epg-ch-col::-webkit-scrollbar{display:none}
#epg-tl-col{flex:1;overflow:auto;min-width:0}
#epg-grid-scroll{flex:1;overflow:auto;position:relative;min-height:0}
@media(min-width:900px){
  #epg-tl-col{cursor:grab}
  #epg-tl-col:active{cursor:grabbing}
}
.epg-grid{display:table;min-width:100%;border-collapse:collapse}
.epg-time-header{display:flex;position:sticky;top:0;z-index:30;background:var(--s1);
  border-bottom:1px solid var(--bdr2);height:28px;flex-shrink:0}
/* ch-cell lives in #epg-ch-col which is a separate non-scrolling panel — no sticky needed */
.epg-ch-cell{width:110px;min-width:110px;height:62px;min-height:62px;
  background:var(--s1);flex-shrink:0;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:3px;padding:4px 5px;cursor:pointer;transition:var(--tr);overflow:hidden;position:relative}
.epg-ch-cell::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.15),transparent);
  transition:left .45s ease;pointer-events:none}
.epg-ch-cell:hover::before{left:100%}
.epg-ch-cell:hover{background:rgba(124,58,237,.15)}
.epg-ch-logo{width:48px;height:30px;object-fit:contain;border-radius:3px;flex-shrink:0}
.epg-ch-logo-ph{width:48px;height:30px;background:var(--s3);border-radius:3px;
  display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
.epg-ch-name{font-size:9px;font-weight:600;color:var(--txt2);text-align:center;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%;line-height:1.2}
/* epg-row is now only in the timeline column — just a height container */
.epg-row{border-bottom:1px solid var(--bdr);height:62px;min-height:62px;max-height:62px;position:relative;overflow:hidden}
.epg-timeline{position:relative;overflow:hidden;min-width:0;contain:paint;height:62px;min-height:62px;max-height:62px}
.epg-prog{position:absolute;top:2px;bottom:2px;border-radius:5px;
  background:var(--s3);border:1px solid var(--bdr);
  padding:3px 6px;overflow:hidden;cursor:pointer;transition:.12s;
  display:flex;flex-direction:column;justify-content:center;min-width:4px;z-index:1}
.epg-prog:hover{background:var(--s4);border-color:var(--acc);z-index:3;
  box-shadow:inset 0 0 0 1px var(--acc)}
.epg-prog.now{background:rgba(139,92,246,.14);border-color:var(--acc)}
.epg-prog-title{font-size:11px;font-weight:600;color:var(--txt1);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.epg-prog-time{font-size:9px;color:var(--txt3);white-space:nowrap}
.epg-now-line{position:absolute;top:0;bottom:0;width:2px;background:var(--acc);
  z-index:10;pointer-events:none;opacity:.8}
.epg-now-dot{position:absolute;top:-4px;left:-4px;width:10px;height:10px;
  border-radius:50%;background:var(--acc)}
.epg-time-tick{position:absolute;top:0;bottom:0;display:flex;flex-direction:column;
  justify-content:flex-end;padding-bottom:4px}
.epg-time-tick-line{position:absolute;top:0;width:1px;height:100%;
  background:var(--bdr);opacity:.5}
.epg-time-lbl{font-size:9px;color:var(--txt3);white-space:nowrap;font-weight:600}
.epg-grid-hdr-corner{width:110px;min-width:110px;height:28px;flex-shrink:0;position:sticky;
  left:0;z-index:40;background:var(--s1);border-right:1px solid var(--bdr2);
  border-bottom:1px solid var(--bdr2);display:flex;align-items:center;justify-content:center}
.epg-grid-hdr-times{flex:1;position:relative;overflow:hidden;height:28px}
.epg-prog-loading{position:absolute;inset:2px;border-radius:5px;
  background:var(--s3);animation:shimmer 1.4s infinite linear;background-size:200% 100%;
  background-image:linear-gradient(90deg,var(--s3) 25%,var(--s4) 50%,var(--s3) 75%)}
.epg-layout-btn{display:flex;align-items:center;gap:4px;padding:4px 9px;
  font-size:11px;font-weight:700;border-radius:14px;border:1.5px solid var(--bdr2);
  background:var(--s2);color:var(--txt2);cursor:pointer;transition:var(--tr);flex-shrink:0;
  white-space:nowrap}
.epg-layout-btn:hover{border-color:var(--acc);color:var(--acc)}
.epg-layout-btn.active{background:rgba(139,92,246,.15);border-color:var(--acc);color:var(--acc)}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}

/* ─── paths area ─────────────────────────────────────────────── */
#paths{padding:8px 0 4px;border-top:1px solid var(--bdr);flex-shrink:0;display:none}
.prow{display:flex;align-items:center;gap:5px;margin-bottom:5px;position:relative}
.plbl{font-size:11px;color:var(--txt2);white-space:nowrap;width:46px;flex-shrink:0}
.prow input{flex:1;height:30px;font-size:12px;padding:0 8px}
.psug-btn{width:30px;height:30px;padding:0;font-size:13px;flex-shrink:0;border-radius:var(--rss)}
.psug{position:absolute;top:calc(100% + 3px);left:46px;right:30px;z-index:300;
  background:var(--s3);border:1px solid var(--bdr2);border-radius:var(--rsm);
  overflow:hidden;display:none;box-shadow:var(--sh)}
.psug.open{display:block;animation:fade-up .15s ease}
.psopt{padding:9px 12px;font-size:12px;cursor:pointer;color:var(--txt2);
  border-bottom:1px solid var(--bdr);transition:var(--tr)}
.psopt:last-child{border-bottom:none}
.psopt:hover{background:var(--s4);color:var(--txt)}


/* ─── player ─────────────────────────────────────────────────── */
#p-player{background:#000;flex-direction:column;overflow:hidden;display:none}
@media(min-width:900px){ #p-player{display:flex!important}}
#vwrap{position:relative;background:#000;flex:1;min-height:0;display:flex;flex-direction:column}
#vid{flex:1;min-height:0;width:100%;display:block;background:#000;object-fit:contain}
@media(min-width:900px){ #vid{width:100%;object-fit:contain}}
#vph{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:12px;pointer-events:none;
  background:radial-gradient(ellipse at 50% 55%,var(--s2) 0%,#000 70%);
  transition:opacity .35s;color:var(--txt3);font-size:13px}
#vph-ico{font-size:52px;opacity:.18;animation:float 3.5s ease infinite}

.pinfo{background:linear-gradient(180deg,var(--s1),var(--s2));padding:11px 14px;
  border-bottom:1px solid var(--bdr);flex-shrink:0}
#np{font-size:14px;font-weight:600;color:var(--txt);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;margin-bottom:2px}
#pu{font-size:11px;color:var(--acc);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;cursor:pointer;transition:var(--tr)}
#pu:hover{color:var(--cyan)}

.pctrl{background:var(--s2);padding:8px 14px;display:flex;flex-direction:row;
  align-items:flex-start;gap:10px;flex-shrink:0;border-bottom:1px solid var(--bdr)}
.ctrl-r{display:flex;align-items:center;gap:7px}
.ctrl-r.ctr{justify-content:center}
.pbig{width:54px;height:54px;font-size:22px;border-radius:50%;
  background:linear-gradient(135deg,
    #a855f7 0%,#7c3aed 20%,#c084fc 40%,#6d28d9 55%,#a78bfa 70%,#7c3aed 85%,#4c1d95 100%);
  background-size:200% 200%;
  animation:metallicShift 3s ease-in-out infinite;
  box-shadow:0 4px 22px var(--glow),0 0 0 1px rgba(168,85,247,.3),inset 0 1px 0 rgba(255,255,255,.25),inset 0 -2px 4px rgba(0,0,0,.4);
  color:#fff;flex-shrink:0;position:relative}
@keyframes metallicShift{0%,100%{background-position:0% 50%}50%{background-position:100% 50%}}
.pbig::after{content:'';position:absolute;top:6px;left:10px;right:20px;height:8px;
  background:linear-gradient(180deg,rgba(255,255,255,.35),transparent);
  border-radius:50%;pointer-events:none}
.pbig:hover:not(:disabled){box-shadow:0 6px 30px var(--glow),0 0 20px rgba(168,85,247,.5),inset 0 1px 0 rgba(255,255,255,.3);
  filter:brightness(1.15);transform:scale(1.06)!important}
/* Animated divider line between player bottom and activity log */
#pctrl-panel{position:relative}
#pctrl-panel::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--acc),var(--cyan),var(--acc),transparent);
  animation:hdrScan 3.5s ease-in-out infinite;opacity:.6;pointer-events:none}
/* Animated line above player controls (below video area) */
#p-player .panel-divider-line{height:1px;flex-shrink:0;position:relative;overflow:visible}
#p-player .panel-divider-line::after{content:'';position:absolute;inset:0;
  background:linear-gradient(90deg,transparent,var(--cyan),var(--acc),var(--cyan),transparent);
  animation:hdrScan 5s ease-in-out infinite 1s;opacity:.5}
.pnav{width:42px;height:42px;border-radius:50%;font-size:16px;padding:0;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center}
.btn-vol-group{display:flex;flex-direction:column;gap:4px;flex:1;align-items:center;}
.vrow{display:flex;align-items:center;gap:9px}
.vrow input[type=range]{flex:1;min-width:0;height:4px;accent-color:var(--acc)}
.vlbl{font-size:11px;color:var(--txt2);width:28px;text-align:right;flex-shrink:0}
.recrow{display:flex;align-items:center;gap:8px}
#rbtn,#rbtn-mob{height:34px;padding:0 14px}
#rbtn.rec,#rbtn-mob.rec{animation:rec-glow 1.5s ease infinite;
  background:rgba(239,68,68,.18);color:var(--red);border:1px solid rgba(239,68,68,.4)}
.rtimer{font-size:13px;color:var(--red);font-variant-numeric:tabular-nums;font-weight:700;
  display:none;letter-spacing:.5px}
.rtimer.vis{display:block;animation:blink .9s infinite}
.rfname{font-size:11px;color:var(--txt3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}

/* ─── log ─────────────────────────────────────────────────────── */
#p-log #logout{background:var(--bg)}
.ll{animation:fade-up .2s ease both}
.lk{color:var(--green)}.le{color:var(--red)}.lw{color:var(--orange)}
.li{color:var(--blue)}.ls{color:var(--cyan)}.lm{color:#a78bfa}

/* ─── saved playlists modal ─────────────────────────────────────── */
#pl-overlay{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.65);
  display:none;align-items:center;justify-content:center;
  backdrop-filter:none;padding:12px}
#pl-overlay.open{display:flex}
#pl-modal{background:var(--s2);border:1px solid var(--bdr2);border-radius:var(--r);
  width:min(480px,100%);max-height:88dvh;display:flex;flex-direction:column;
  box-shadow:0 24px 64px rgba(0,0,0,.8);animation:slide-up .25s cubic-bezier(.34,1.56,.64,1)}
.plm-hdr{display:flex;align-items:center;gap:8px;padding:14px 16px;
  border-bottom:1px solid var(--bdr);flex-shrink:0}
.plm-hdr h2{flex:1;font-size:14px;font-weight:800;
  background:linear-gradient(90deg,var(--txt),var(--acc));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.pl-list{flex:1;overflow-y:auto;padding:10px;min-height:60px}
.pl-empty{text-align:center;padding:32px 16px;color:var(--txt3);font-size:12px}
.pl-empty span{font-size:40px;display:block;margin-bottom:8px;opacity:.2;animation:float 3s ease infinite}
.pli{display:flex;align-items:center;gap:8px;padding:10px 12px;border-radius:var(--rsm);
  margin-bottom:5px;background:rgba(255,255,255,.025);border:1px solid var(--bdr);transition:var(--tr);
  animation:fade-up .2s ease both;border-left:3px solid var(--pli-accent,var(--bdr));
  position:relative;overflow:hidden;box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
.pli::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.06),transparent);
  transition:left .5s ease;pointer-events:none}
.pli:hover::before{left:100%}
.pli:hover{background:rgba(255,255,255,.05);border-color:var(--bdr2);
  box-shadow:0 0 12px rgba(var(--pli-accent,124,58,237),.08),inset 0 1px 0 rgba(255,255,255,.06)}
.pli-type-badge{font-size:9px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;
  padding:1px 5px;border-radius:3px;flex-shrink:0;opacity:.9}
.pli-type-mac{background:rgba(59,130,246,.15);color:#3b82f6;border:1px solid rgba(59,130,246,.3);
  box-shadow:0 0 8px rgba(59,130,246,.15)}
.pli-type-xtream{background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.3);
  box-shadow:0 0 8px rgba(34,197,94,.15)}
.pli-type-m3u{background:rgba(239,68,68,.15);color:var(--red);border:1px solid rgba(239,68,68,.3);
  box-shadow:0 0 8px rgba(239,68,68,.15)}
.pli-ico{font-size:20px;flex-shrink:0}
.pli-info{flex:1;min-width:0}
.pli-name{font-size:13px;font-weight:600;color:var(--txt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pli-sub{font-size:11px;color:var(--txt2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}
.pli-acts{display:flex;gap:4px;flex-shrink:0}
.pl-add{border-top:1px solid var(--bdr);padding:14px 16px;flex-shrink:0}
.pl-add h3{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;
  color:var(--txt2);margin-bottom:10px}
.pl-form{display:flex;flex-direction:column;gap:7px}
.pl-row{display:flex;gap:6px;align-items:center}
.pl-row label{font-size:11px;color:var(--txt2);width:36px;flex-shrink:0;text-align:right}
.pl-row input{flex:1;height:32px;font-size:12px}
.pl-ct-row{display:flex;gap:5px;margin-bottom:4px}
.pl-ct-btn{flex:1;height:28px;font-size:11px;padding:0;border-radius:var(--rss)}

/* ─── action drawer ──────────────────────────────────────────── */
#act-overlay{position:fixed;inset:0;z-index:400;background:rgba(0,0,0,.5);
  display:none;backdrop-filter:none}
#act-overlay.open{display:block}
#act-drawer{position:fixed;top:0;right:0;bottom:0;z-index:401;
  width:min(300px,85vw);background:var(--s2);border-left:1px solid var(--bdr2);
  display:flex;flex-direction:column;box-shadow:-8px 0 40px rgba(0,0,0,.6);
  transform:translateX(100%);transition:transform .3s cubic-bezier(.4,0,.2,1)}
#act-drawer.open{transform:translateX(0)}
.adr-hdr{display:flex;align-items:center;gap:10px;padding:16px;
  border-bottom:1px solid var(--bdr);flex-shrink:0}
.adr-hdr h3{flex:1;font-size:13px;font-weight:800;color:var(--txt)}
.adr-body{flex:1;overflow-y:auto;padding:14px}
.adr-section{margin-bottom:18px}
.adr-section-title{font-size:10px;font-weight:800;text-transform:uppercase;
  letter-spacing:1.2px;color:var(--txt3);margin-bottom:8px;padding-bottom:5px;
  border-bottom:1px solid var(--bdr)}
.adr-btn{width:100%;height:46px;font-size:13px;font-weight:600;
  display:flex;align-items:center;gap:10px;padding:0 16px;
  margin-bottom:7px;border-radius:var(--rsm);text-align:left;justify-content:flex-start}
.adr-btn span.adr-ico{font-size:18px;flex-shrink:0;width:26px;text-align:center}
.adr-btn span.adr-lbl{flex:1}
.adr-btn span.adr-sub{font-size:11px;color:rgba(255,255,255,.5);font-weight:400}
.adr-sel-row{display:flex;gap:7px;margin-bottom:10px}
.adr-sel-row button{flex:1;height:38px;font-size:12px}
.adr-count{font-size:12px;color:var(--acc);font-weight:700;
  text-align:center;padding:6px 0 2px}
/* Progress panel inside action drawer */
.adr-progress{background:var(--s3);border:1px solid var(--bdr);border-radius:var(--rsm);
  padding:12px 14px;margin-bottom:14px;display:none}
.adr-progress.active{display:block}
.adr-prog-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:5px}
.adr-prog-title{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1px;
  color:var(--acc)}
.adr-prog-stop{background:rgba(255,80,80,.15);border:1px solid rgba(255,80,80,.3);
  color:#f06060;border-radius:6px;height:22px;padding:0 8px;font-size:11px;cursor:pointer;
  flex-shrink:0;transition:background .15s}
.adr-prog-stop:hover{background:rgba(255,80,80,.35)}
.adr-prog-dismiss{background:rgba(120,120,140,.15);border:1px solid rgba(120,120,140,.3);
  color:var(--txt3);border-radius:6px;height:22px;padding:0 8px;font-size:11px;cursor:pointer;
  flex-shrink:0;transition:background .15s}
.adr-prog-dismiss:hover{background:rgba(120,120,140,.35);color:var(--txt)}
.adr-prog-label{font-size:11px;color:var(--txt2);margin-bottom:7px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.adr-prog-bar-wrap{background:rgba(0,0,0,.5);border-radius:8px;height:7px;overflow:hidden;
  margin-bottom:6px;position:relative;border:1px solid rgba(255,255,255,.05)}
.adr-prog-bar{height:100%;border-radius:8px;width:0%;transition:width .35s ease;
  background:linear-gradient(90deg,var(--acc2),var(--acc),var(--cyan));
  box-shadow:0 0 10px rgba(124,58,237,.5);position:relative;overflow:hidden}
.adr-prog-bar::after{content:'';position:absolute;top:0;left:-120%;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent);
  animation:progSweep 1.6s ease infinite}
@keyframes progSweep{to{left:120%}}
@keyframes adr-indeterminate{
  0%{transform:translateX(-110%)}
  100%{transform:translateX(200%)}
}
.adr-prog-footer{display:flex;align-items:center;justify-content:space-between;gap:6px}
.adr-prog-count{font-size:11px;color:var(--txt3);font-weight:600}
.adr-prog-speed{font-size:11px;color:var(--acc2);font-weight:700;text-align:right}
/* Recording section in action drawer */
#adr-rec-section{margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid var(--bdr)}
#adr-rec-btn{width:100%;height:42px;font-size:13px;font-weight:700;border-radius:var(--rsm);
  display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:8px;
  background:rgba(220,50,50,.15);border:1px solid rgba(220,50,50,.35);color:#f06060;cursor:pointer;transition:background .15s}
#adr-rec-btn:hover{background:rgba(220,50,50,.3)}
#adr-rec-btn.rec{background:rgba(220,50,50,.3);border-color:rgba(220,50,50,.7);animation:recpulse 1.2s ease-in-out infinite}
@keyframes recpulse{0%,100%{box-shadow:0 0 0 0 rgba(220,50,50,.4)}50%{box-shadow:0 0 0 6px rgba(220,50,50,0)}}
#adr-rec-info{display:none;flex-direction:column;gap:4px}
#adr-rec-info.vis{display:flex}
#adr-rec-fname{font-size:11px;color:var(--txt2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#adr-rec-timer{font-size:13px;font-weight:700;color:#f06060;letter-spacing:1px}
#adr-rec-open{width:100%;height:34px;font-size:12px;font-weight:600;margin-top:4px}
/* FAB — floating action button to open drawer */
.fab{position:absolute;bottom:70px;right:60px;z-index:50;
  width:48px;height:48px;border-radius:50%;padding:0;font-size:20px;
  background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;
  box-shadow:0 4px 20px var(--glow);border:none;cursor:pointer;
  transition:var(--tr);display:flex;align-items:center;justify-content:center}
.fab:hover{transform:scale(1.1);box-shadow:0 6px 28px var(--glow)}
.fab:active{transform:scale(.93)}
.fab-badge{position:absolute;top:-3px;right:-3px;background:var(--green);
  color:#fff;font-size:9px;font-weight:800;border-radius:10px;
  padding:1px 5px;min-width:16px;text-align:center;display:none;
  border:1.5px solid var(--bg);box-shadow:0 0 6px rgba(34,197,94,.5)}
.fab-badge.vis{display:block}
@media(min-width:900px){.fab{display:none}}

/* Actions tab */
#t-act.act-open{color:var(--orange)}
#t-act.act-open::after{content:'';position:absolute;top:0;left:25%;right:25%;height:2.5px;
  background:linear-gradient(90deg,var(--orange),var(--acc));border-radius:0 0 4px 4px;
  box-shadow:0 0 10px var(--orange),0 0 20px rgba(245,158,11,.35);pointer-events:none}
#t-act.act-open .nt-ico{transform:scale(1.2);filter:drop-shadow(0 0 8px var(--orange))}

/* Desktop Actions button — shown in panel header on desktop, hidden on mobile */
.ph-act-btn{display:none;align-items:center;gap:5px;padding:5px 12px;
  font-size:12px;font-weight:700;border-radius:20px;position:relative;
  background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;
  border:none;cursor:pointer;flex-shrink:0;overflow:hidden;
  box-shadow:0 2px 10px var(--glow2),inset 0 1px 0 rgba(255,255,255,.15);transition:var(--tr)}
.ph-act-btn::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent);
  transition:left .5s ease;pointer-events:none}
.ph-act-btn:hover::before{left:100%}
.ph-act-btn:hover{filter:brightness(1.12);transform:scale(1.03);box-shadow:var(--glow-acc)}
.ph-act-btn:active{transform:scale(.96)}
.ph-act-badge{background:var(--green);color:#fff;font-size:9px;font-weight:800;
  border-radius:10px;padding:1px 5px;min-width:16px;text-align:center;display:none;
  margin-left:3px;border:1.5px solid var(--bg)}
.ph-act-badge.vis{display:inline-block}
@media(min-width:900px){
  .ph-act-btn{display:flex;padding:4px 8px;font-size:11px;gap:3px}
  .ph h3{display:none}
  .ph{padding:8px 10px;gap:5px;justify-content:space-between}
}

/* ─── toasts ──────────────────────────────────────────────────── */
#toasts{position:fixed;bottom:72px;left:50%;transform:translateX(-50%);
  z-index:9999;display:flex;flex-direction:column;gap:5px;pointer-events:none;width:min(90vw,300px)}
@media(min-width:900px){ #toasts{bottom:18px}}
.toast{padding:10px 18px;border-radius:24px;font-size:13px;font-weight:600;text-align:center;
  backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,.12);
  animation:slide-up .3s cubic-bezier(.34,1.56,.64,1);
  box-shadow:0 8px 32px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.1)}
.tok2{background:rgba(16,48,24,.95);color:var(--green);
  border-color:rgba(34,197,94,.4);box-shadow:0 0 20px rgba(34,197,94,.25),0 8px 32px rgba(0,0,0,.6)}
.terr2{background:rgba(48,12,12,.95);color:#ff7070;
  border-color:rgba(239,68,68,.4);box-shadow:0 0 20px rgba(239,68,68,.25),0 8px 32px rgba(0,0,0,.6)}
.tinfo{background:rgba(10,22,48,.95);color:#7ab8ff;
  border-color:rgba(59,130,246,.4);box-shadow:0 0 20px rgba(59,130,246,.2),0 8px 32px rgba(0,0,0,.6)}
.twrn2{background:rgba(40,28,8,.95);color:var(--orange);
  border-color:rgba(245,158,11,.4);box-shadow:0 0 20px rgba(245,158,11,.2),0 8px 32px rgba(0,0,0,.6)}

/* ─── spinner ────────────────────────────────────────────────── */
.spin{display:inline-block;width:16px;height:16px;border:2px solid var(--s5);
  border-top-color:var(--acc);border-radius:50%;animation:spin .7s linear infinite}

/* ─── animations ─────────────────────────────────────────────── */
@keyframes fade-up{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
@keyframes pulse-dot{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
@keyframes rec-glow{0%,100%{box-shadow:0 0 6px rgba(239,68,68,.4)}
  50%{box-shadow:0 0 22px rgba(239,68,68,.8),0 0 40px rgba(239,68,68,.25)}}
@keyframes pop-in{from{transform:scale(.4);opacity:0}to{transform:scale(1);opacity:1}}
@keyframes slide-up{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:translateY(0)}}

.hidden{display:none!important}

/* ─── What's On Now modal ─────────────────────────────────── */
#won-overlay{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.6);
  display:none;align-items:center;justify-content:center}
#won-overlay.open{display:flex}
#won-modal{background:var(--s2);border-radius:14px;width:min(700px,96vw);
  max-height:88vh;display:flex;flex-direction:column;overflow:hidden;
  box-shadow:0 24px 80px rgba(0,0,0,.6);animation:pop-in .2s ease}
.won-hdr{display:flex;align-items:center;gap:10px;padding:14px 16px 10px;
  border-bottom:1px solid var(--s4);flex-shrink:0}
.won-hdr h3{flex:1;margin:0;font-size:15px;font-weight:700}
.won-hdr .won-count{font-size:11px;color:var(--txt3);background:var(--s3);
  padding:2px 8px;border-radius:20px}
.won-search{padding:10px 14px;flex-shrink:0;border-bottom:1px solid var(--s4)}
.won-search input{width:100%;box-sizing:border-box;background:var(--s3);border:1px solid var(--s5);
  color:var(--txt1);border-radius:8px;padding:7px 12px;font-size:13px;outline:none}
.won-search input:focus{border-color:var(--acc)}
.won-list{flex:1;overflow-y:auto;padding:6px 8px}
.won-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;
  cursor:pointer;transition:var(--tr);border:1px solid transparent;
  position:relative;overflow:hidden}
.won-item::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.05),transparent);
  transition:left .45s ease;pointer-events:none}
.won-item:hover{background:rgba(124,58,237,.08);border-color:rgba(124,58,237,.2);
  box-shadow:0 0 10px rgba(124,58,237,.07)}
.won-item:hover::before{left:100%}
.won-item:active{transform:scale(.98)}
.won-item-info{flex:1;min-width:0}
.won-item-title{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.won-item-ch{font-size:11px;color:var(--txt3);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.won-item-times{font-size:10px;color:var(--txt3);margin-top:3px}
.won-progress{width:48px;flex-shrink:0}
.won-progress-bar{height:3px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden}
.won-progress-fill{height:100%;background:linear-gradient(90deg,var(--acc2),var(--acc));
  border-radius:2px;transition:width .3s;box-shadow:0 0 5px rgba(124,58,237,.4)}
.won-progress-pct{font-size:9px;color:var(--txt3);text-align:right;margin-top:2px}
.won-find-btn{flex-shrink:0;width:30px;height:30px;border-radius:7px;border:1px solid var(--s5);
  background:var(--s3);color:var(--txt2);font-size:14px;cursor:pointer;display:flex;
  align-items:center;justify-content:center;transition:background .15s,color .15s}
.won-find-btn:hover{background:var(--acc);color:#fff;border-color:var(--acc)}
.won-find-btn.loading{opacity:.5;pointer-events:none}
.won-find-result{font-size:10px;margin-top:4px;padding:3px 6px;border-radius:4px;display:none}
.won-find-result.ok{background:rgba(34,197,94,.18);color:var(--green);display:block;
  cursor:pointer;transition:background .15s}
.won-find-result.ok:hover{background:rgba(34,197,94,.32)}
.won-find-result.ok:active{background:rgba(34,197,94,.45)}
.won-find-result.fail{background:rgba(239,68,68,.13);color:#f87171;display:block}
.won-find-result.playing{background:rgba(59,130,246,.18);color:#60a5fa;display:block;cursor:default}
.won-ext-btn{display:block;font-size:10px;margin-top:0;padding:3px 6px;border-radius:4px;
  background:rgba(139,92,246,.18);color:#a78bfa;cursor:pointer;transition:background .15s}
.won-ext-btn:hover{background:rgba(139,92,246,.32)}
.won-ext-btn:active{background:rgba(139,92,246,.45)}
.won-empty{text-align:center;padding:48px 20px;color:var(--txt3);font-size:13px}
.won-empty span{font-size:40px;display:block;margin-bottom:10px;opacity:.3}
.won-loading{display:flex;align-items:center;justify-content:center;gap:10px;
  padding:40px 20px;color:var(--txt3);font-size:13px}
.won-ftr{padding:10px 14px;border-top:1px solid var(--s4);display:flex;
  justify-content:flex-end;flex-shrink:0}
@media(max-width:600px){
  #won-modal{width:100vw;max-height:100vh;border-radius:0}
}

/* ─── subtitle modal ─────────────────────────────────────────── */
#sub-overlay{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.75);
  display:none;align-items:center;justify-content:center;padding:16px}
#sub-overlay.open{display:flex}
#sub-modal{background:var(--s1);border:1px solid var(--bdr2);border-radius:var(--r);
  width:100%;max-width:640px;max-height:88vh;display:flex;flex-direction:column;
  box-shadow:0 20px 60px rgba(0,0,0,.8);overflow:hidden}
.sub-hdr{padding:14px 16px;border-bottom:1px solid var(--bdr);
  display:flex;align-items:center;gap:10px;flex-shrink:0;background:var(--s2)}
.sub-hdr h3{flex:1;font-size:13px;font-weight:800;letter-spacing:.5px;color:var(--txt)}
.sub-body{flex:1;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px}
.sub-search-row{display:flex;gap:8px;align-items:center}
.sub-search-row input{flex:1;height:36px;font-size:13px}
.sub-search-row button{height:36px;padding:0 14px;flex-shrink:0}
.sub-filters{display:flex;flex-wrap:wrap;gap:10px;padding:8px 10px;
  background:var(--s3);border-radius:var(--rsm);border:1px solid var(--bdr)}
.sub-filter-group{display:flex;flex-direction:column;gap:5px}
.sub-filter-group label.grp-lbl{font-size:10px;font-weight:800;text-transform:uppercase;
  letter-spacing:1px;color:var(--txt3)}
.sub-lang-grid{display:flex;flex-wrap:wrap;gap:4px}
.sub-lang-chip{display:flex;align-items:center;gap:4px;padding:3px 8px;border-radius:20px;
  font-size:11px;font-weight:600;border:1px solid var(--bdr2);background:var(--s4);
  color:var(--txt2);cursor:pointer;transition:all .15s;user-select:none;white-space:nowrap}
.sub-lang-chip input{width:14px;height:14px;cursor:pointer;flex-shrink:0;accent-color:var(--acc)}
.sub-lang-chip:has(input:checked){background:rgba(124,58,237,.18);
  border-color:var(--acc);color:var(--txt)}
.sub-type-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sub-type-chip{display:flex;align-items:center;gap:4px;padding:3px 10px;border-radius:20px;
  font-size:11px;font-weight:600;border:1px solid var(--bdr2);background:var(--s4);
  color:var(--txt2);cursor:pointer;transition:all .15s;user-select:none}
.sub-type-chip input{width:14px;height:14px;cursor:pointer;flex-shrink:0;accent-color:var(--acc)}
.sub-type-chip:has(input:checked){background:rgba(124,58,237,.18);
  border-color:var(--acc);color:var(--txt)}
.sub-ep-row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.sub-ep-row label{font-size:11px;color:var(--txt2);white-space:nowrap}
.sub-ep-row input{width:60px;height:28px;font-size:12px;text-align:center}
.sub-results{display:flex;flex-direction:column;gap:6px}
.sub-result-item{background:var(--s3);border:1px solid var(--bdr);border-radius:var(--rsm);
  padding:10px 12px;display:flex;gap:10px;align-items:flex-start;transition:border-color .15s}
.sub-result-item:hover{border-color:var(--bdr2)}
.sub-result-info{flex:1;min-width:0}
.sub-result-title{font-size:13px;font-weight:700;color:var(--txt);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub-result-meta{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.sub-meta-badge{padding:2px 7px;border-radius:20px;font-size:10px;font-weight:700}
.sub-meta-lang{background:rgba(6,182,212,.12);color:var(--cyan);border:1px solid rgba(6,182,212,.2)}
.sub-meta-dl{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.15)}
.sub-meta-rat{background:rgba(245,158,11,.1);color:var(--orange);border:1px solid rgba(245,158,11,.15)}
.sub-meta-ep{background:rgba(124,58,237,.12);color:#a78bfa;border:1px solid rgba(124,58,237,.2)}
.sub-result-release{font-size:10px;color:var(--txt3);margin-top:3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub-load-btn{flex-shrink:0;height:34px;padding:0 12px;font-size:12px;align-self:center}
.sub-load-btn.loaded{background:rgba(34,197,94,.15);color:var(--green);
  border:1px solid rgba(34,197,94,.3)}
.sub-empty{text-align:center;padding:36px 20px;color:var(--txt3);font-size:13px}
.sub-empty span{font-size:36px;display:block;margin-bottom:8px;opacity:.3}
.sub-status-bar{padding:8px 12px;border-top:1px solid var(--bdr);flex-shrink:0;
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  background:var(--s2);font-size:11px;color:var(--txt3);flex-wrap:wrap}
#sub-status-msg{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub-status-bar .btn-ghost{flex-shrink:0;white-space:nowrap}
.sub-active-strip{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.2);
  border-radius:var(--rss);padding:4px 10px;font-size:11px;color:var(--green);
  display:flex;align-items:center;gap:6px}
.sub-delay-row{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--txt2)}
.sub-delay-row button{width:26px;height:26px;padding:0;font-size:13px;border-radius:var(--rss);
  border:1px solid var(--bdr2);background:var(--s3);color:var(--txt);cursor:pointer;
  display:flex;align-items:center;justify-content:center;transition:var(--tr);flex-shrink:0}
.sub-delay-row button:hover{background:var(--s4);border-color:var(--acc)}
#sub-delay-val{min-width:52px;text-align:center;font-weight:700;color:var(--acc);font-size:12px;
  font-variant-numeric:tabular-nums}
/* subtitle tab row */
.sub-tab-row{display:flex;gap:6px;flex-shrink:0;border-bottom:1px solid var(--bdr);padding-bottom:8px;margin-bottom:2px}
.sub-tab-btn{height:30px;padding:0 14px;font-size:12px;font-weight:700;border-radius:var(--rss);
  border:1px solid var(--bdr2);background:var(--s3);color:var(--txt2);cursor:pointer;transition:var(--tr)}
.sub-tab-btn.active{background:var(--acc);border-color:var(--acc);color:#fff}
.sub-tab-btn:hover:not(.active){background:var(--s4);color:var(--txt)}
/* mobile subtitle file browser */
.sub-fb-row{display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid var(--bdr);
  cursor:pointer;transition:background .12s;font-size:13px}
.sub-fb-row:last-child{border-bottom:none}
.sub-fb-row:hover,.sub-fb-row:active{background:var(--s4)}
.sub-fb-dir{color:var(--txt)}
.sub-fb-file{color:var(--cyan)}
.sub-fb-icon{flex-shrink:0;font-size:15px}
.sub-fb-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub-fb-arr{flex-shrink:0;color:var(--txt3);font-size:16px}
@media(max-width:600px){
  #sub-modal{max-height:96vh;border-radius:0}
  #sub-overlay{padding:0;align-items:flex-end}
}

/* ═══════════════════════════════════════════════════════════
   MULTIVIEW  — CSS
   mirrors multiview.js widget structure and cast_addon panel
   positioning pattern
════════════════════════════════════════════════════════════ */

/* Full-viewport overlay panel — sits above #main, below header/botnav */
#p-mv{
  position:fixed;
  top:var(--mv-top,44px);   /* updated in JS via _mvUpdateTop() */
  left:0;right:0;
  bottom:0;
  z-index:200;
  background:var(--bg);
  display:none;           /* hidden until activated */
  flex-direction:column;
  overflow:hidden;
  transition:top .35s cubic-bezier(.4,0,.2,1); /* follows cpanel open/close */
}
#p-mv.mv-active{ display:flex; }
/* On mobile leave room for botnav */
@media(max-width:899px){
  #p-mv{ bottom:56px; }
}
/* Desktop multiview button — only shown on desktop (handled by JS) */
#mv-desktop-btn.mv-btn-active{
  background:var(--acc) !important;
  color:#fff !important;
  border-color:var(--acc2) !important;
  box-shadow:0 2px 10px var(--glow2);
}

/* Toolbar — always-visible strip + collapsible body */
#mv-toolbar{
  display:flex;flex-direction:column;
  background:var(--s1);border-bottom:1px solid var(--bdr);flex-shrink:0;
}
/* Always-visible strip: toggle arrow + close button */
#mv-tb-strip{
  display:flex;align-items:center;gap:5px;
  padding:4px 8px;min-height:36px;
}
#mv-tb-toggle{
  display:flex;align-items:center;gap:5px;
  height:28px;padding:0 10px;font-size:12px;font-weight:600;
  background:var(--s3);border:1px solid var(--bdr2);border-radius:var(--rss);
  color:var(--txt2);cursor:pointer;
}
#mv-tb-toggle:hover{background:var(--s4);color:var(--txt)}
#mv-tb-arrow{ font-size:10px;transition:transform .2s; }
#mv-toolbar.tb-open #mv-tb-arrow{ transform:rotate(180deg); }
/* Collapsible body */
#mv-tb-body{
  display:none;flex-wrap:wrap;align-items:center;gap:5px;
  padding:5px 8px 7px;border-top:1px solid var(--bdr);
}
#mv-toolbar.tb-open #mv-tb-body{ display:flex; }
#mv-toolbar button{
  height:30px;padding:0 10px;font-size:12px;font-weight:600;
}
#mv-toolbar select{
  height:30px;padding:0 6px;font-size:12px;background:var(--s3);
  color:var(--txt);border:1px solid var(--bdr2);border-radius:var(--rsm);
  cursor:pointer;
}
.mv-tb-sep{
  width:1px;height:20px;background:var(--bdr2);margin:0 2px;
}
/* Close-multiview button — prominent, uses the same ⊞ icon as the entry button */
#mv-close-btn{
  font-size:13px;font-weight:700;letter-spacing:.5px;
  padding:0 12px;
  color:var(--txt) !important;
  background:var(--s4) !important;
  border:1px solid var(--bdr2) !important;
  border-radius:var(--rss);
  gap:4px;
  transition:background .15s,border-color .15s,color .15s;
}
#mv-close-btn:hover{
  background:var(--acc) !important;
  border-color:var(--acc2) !important;
  color:#fff !important;
  box-shadow:0 2px 8px var(--glow2);
}

/* Gridstack container — updateGridBackground targets this */
#mv-grid-wrap{
  flex:1;overflow:auto;position:relative;
}
/* Mobile resize fix:
   GridStack positions resize handles with small negative insets inside each item.
   overflow:hidden on the wrapper would clip them; overflow:auto lets them render.
   touch-action:none stops the browser treating the handle drag as a scroll
   gesture — without this, touch-resize is silently swallowed on Android/iOS. */
#mv-grid-wrap .ui-resizable-handle,
#mv-grid-wrap .grid-stack-item > .ui-resizable-se,
#mv-grid-wrap .grid-stack-item > .ui-resizable-sw,
#mv-grid-wrap .grid-stack-item > .ui-resizable-ne,
#mv-grid-wrap .grid-stack-item > .ui-resizable-nw,
#mv-grid-wrap .grid-stack-item > .ui-resizable-n,
#mv-grid-wrap .grid-stack-item > .ui-resizable-e,
#mv-grid-wrap .grid-stack-item > .ui-resizable-s,
#mv-grid-wrap .grid-stack-item > .ui-resizable-w {
  touch-action: none !important;
}
/* Make resize handles larger on touch screens so they're easier to grab */
@media(max-width:899px){
  #mv-grid-wrap .ui-resizable-handle { min-width:20px; min-height:20px; }
  #mv-grid-wrap .ui-resizable-se     { width:20px !important; height:20px !important; }
  #mv-grid-wrap .ui-resizable-s,
  #mv-grid-wrap .ui-resizable-e      { width:16px !important; height:16px !important; }
}
#mv-grid-wrap .grid-stack{
  min-height:100%;height:100%;
  background-color:var(--s1);
  background-image:
    linear-gradient(var(--bdr) 1px,transparent 1px),
    linear-gradient(90deg,var(--bdr) 1px,transparent 1px);
  background-size:var(--mv-cell-w,80px) var(--mv-cell-w,80px);
}

/* Widget content wrapper — mirrors .grid-stack-item-content styling */
.mv-widget-content{
  display:flex;flex-direction:column;
  background:var(--s2);border-radius:6px;
  border:1px solid var(--bdr);overflow:hidden;
  height:100%;
}
.mv-widget-content.mv-active-player{
  border-color:var(--acc);
  box-shadow:0 0 0 2px var(--glow2);
}

/* Player header — mirrors .player-header in multiview.js widgetHTML */
.mv-hdr{
  display:flex;align-items:center;justify-content:space-between;
  padding:3px 6px;background:var(--s1);flex-shrink:0;min-height:28px;
  touch-action:none; /* allow GridStack to intercept drag on mobile */
}
.mv-hdr-info{
  flex:1;min-width:0;display:flex;flex-direction:column;gap:1px;overflow:hidden;
}
.mv-hdr-title{
  font-size:11px;font-weight:700;color:var(--txt2);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
/* Portal name + connection count badge shown beneath the channel title */
.mv-hdr-portal{
  font-size:9px;color:var(--red);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;line-height:1.2;
}
.mv-hdr-portal:empty{display:none}
/* Highlight when we know max connections and are approaching the limit */
.mv-hdr-portal.mv-conn-warn{color:#f59e0b}
.mv-hdr-portal.mv-conn-full{color:#ef4444}

/* URL entry bar — shown inline below the header when the 🔗 button is clicked */
.mv-url-bar{
  display:flex;align-items:center;gap:4px;
  padding:4px 6px;background:var(--s1);border-top:1px solid var(--bdr);
  flex-shrink:0;
}
.mv-url-bar.mv-hidden{display:none}
.mv-url-input{
  flex:1;height:24px;font-size:11px;padding:0 6px;
  background:var(--s3);border:1px solid var(--bdr2);border-radius:3px;
  color:var(--txt);
}
.mv-url-input:focus{outline:none;border-color:var(--acc)}
.mv-url-bar button{
  height:24px;padding:0 7px;font-size:11px;flex-shrink:0;
  background:var(--s3);border:1px solid var(--bdr2);border-radius:3px;
  color:var(--txt2);cursor:pointer;
}
.mv-url-bar button:hover{background:var(--s4);color:var(--txt)}
.mv-ctrl{
  display:flex;align-items:center;gap:2px;flex-shrink:0;
  overflow:hidden; /* clips when tile is too narrow */
  min-width:0;
}
.mv-ctrl button{
  height:22px;width:22px;min-width:22px;padding:0;font-size:11px;
  background:none;border:1px solid transparent;border-radius:3px;
  color:var(--txt2);display:flex;align-items:center;justify-content:center;
  flex-shrink:0;
}
.mv-ctrl button:hover{background:var(--s4);border-color:var(--bdr2);color:var(--txt)}
.mv-ctrl input[type=range]{
  width:44px;min-width:0;height:4px;padding:0;cursor:pointer;
  accent-color:var(--acc);flex-shrink:1;
}
/* On very small tiles progressively hide lower-priority controls.
   Priority order (highest→lowest): 📺 sel, 🔗 url, ⏸ pp, 🔊 mute, vol, ⛶ fs, ⏹ stop, ✕ rm */
.mv-widget-content.mv-tiny .mv-vol       { display:none; }
.mv-widget-content.mv-tiny .mv-fs-btn    { display:none; }
.mv-widget-content.mv-xs   .mv-stop-btn  { display:none; }
.mv-widget-content.mv-xs   .mv-pp-btn    { display:none; }
.mv-widget-content.mv-xs   .mv-url-btn   { display:none; }

/* Player body */
.mv-body{flex:1;position:relative;background:#000;overflow:hidden;min-height:0}
.mv-video{width:100%;height:100%;object-fit:contain;display:block}
.mv-video.mv-hidden{display:none}

/* Placeholder — mirrors .player-placeholder */
.mv-placeholder{
  position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:8px;
  color:var(--txt3);font-size:11px;cursor:pointer;
  background:var(--s2);
}
.mv-placeholder:hover{background:var(--s3);color:var(--txt2)}
.mv-placeholder .mv-ph-ico{font-size:28px;opacity:.35}
.mv-placeholder.mv-hidden{display:none}

/* Channel selector modal — mirrors multiviewChannelSelectorModal */
#mv-sel-overlay{
  display:none;position:fixed;inset:0;z-index:1100;
  background:rgba(0,0,0,.75);align-items:center;justify-content:center;
}
#mv-sel-overlay.open{display:flex}
#mv-sel-modal{
  background:var(--s2);border-radius:var(--r);
  border:1px solid var(--bdr2);
  width:min(400px,94vw);max-height:min(80vh,560px);
  display:flex;flex-direction:column;overflow:hidden;
  box-shadow:var(--sh);
}
/* Play-URL row inside multiview selector */
.mv-sel-play-url-row{
  display:flex;align-items:center;gap:6px;
  margin:6px 8px 2px;padding:7px 8px;
  background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.22);
  border-radius:var(--rsm);cursor:pointer;flex-shrink:0;
}
.mv-sel-play-url-row:hover{background:rgba(239,68,68,.14)}
.mv-sel-play-url-inp{
  flex:1;height:26px;font-size:11px;padding:0 6px;border-radius:3px;
  background:var(--s3);border:1px solid var(--bdr2);color:var(--txt);
  outline:none;
}
.mv-sel-play-url-inp:focus{border-color:var(--red)}
/* Seek bar overlaid at bottom of mv-body */
.mv-seek-wrap{
  position:absolute;bottom:0;left:0;right:0;z-index:5;
  padding:0 4px 2px;background:linear-gradient(transparent,rgba(0,0,0,.55));
  display:none;align-items:center;gap:4px;
}
.mv-seek-wrap.mv-seek-visible{display:flex}
.mv-seek{flex:1;height:3px;cursor:pointer;accent-color:var(--acc);min-width:0}
.mv-seek-time{font-size:9px;color:rgba(255,255,255,.75);white-space:nowrap;flex-shrink:0;font-variant-numeric:tabular-nums}
/* Quality selector in ctrl area */
.mv-quality-sel{
  height:22px;font-size:10px;padding:0 2px;background:var(--s3);
  border:1px solid var(--bdr2);border-radius:3px;color:var(--txt2);
  cursor:pointer;flex-shrink:0;max-width:58px;
}
.mv-widget-content.mv-tiny .mv-quality-sel{display:none}
.mv-sel-hdr{
  display:flex;align-items:center;justify-content:space-between;
  padding:12px 14px 10px;border-bottom:1px solid var(--bdr);flex-shrink:0;
}
.mv-sel-hdr h3{font-size:11px;font-weight:800;text-transform:uppercase;
  letter-spacing:1.5px;color:var(--txt2)}
#mv-sel-search{
  margin:8px 10px;height:32px;font-size:12px;
}
#mv-sel-list{
  flex:1;overflow-y:auto;padding:4px 6px;min-height:0;
}
.mv-ch-row{
  display:flex;align-items:center;gap:8px;padding:6px 8px;
  border-radius:var(--rsm);cursor:pointer;transition:background .12s;
}
.mv-ch-row:hover{background:rgba(124,58,237,.08);border-color:rgba(124,58,237,.2) !important;
  box-shadow:0 0 8px rgba(124,58,237,.07)}
.mv-ch-logo{width:32px;height:22px;object-fit:contain;border-radius:3px;
  background:var(--s3);flex-shrink:0}
.mv-ch-name{font-size:12px;font-weight:600;color:var(--txt);
  flex:1;overflow:hidden;white-space:nowrap;position:relative}
.mv-ch-name .iname-inner{display:inline-block;white-space:nowrap;padding-right:20px}
.mv-ch-name.scrolling .iname-inner{animation:iname-scroll var(--scroll-dur,6s) linear infinite}
/* Action buttons always visible in the multiview channel selector */
.mv-ch-row .mv-item-btns{display:flex;gap:3px;flex-shrink:0;align-items:center}
.mv-item-btns .btn-ghost{height:24px;padding:0 7px;font-size:11px;font-weight:700}
/* Small inline context dropdown inside the selector — fixed so it escapes overflow:hidden */
.mv-item-ctx{position:fixed;z-index:2100;background:var(--s2);border:1px solid var(--bdr2);
  border-radius:var(--rsm);box-shadow:0 4px 16px rgba(0,0,0,.45);min-width:170px;padding:4px 0;display:none}
.mv-item-ctx.open{display:block}
.mv-item-ctx button{display:flex;align-items:center;gap:8px;width:100%;padding:7px 14px;
  background:none;border:none;color:var(--txt);font-size:12px;cursor:pointer;text-align:left;white-space:nowrap}
.mv-item-ctx button:hover{background:rgba(124,58,237,.12)}
/* Tabs row — explicit row layout so it never stacks vertically */
#mv-sel-tabs{display:flex;flex-flow:row nowrap;gap:4px;padding:6px 10px 0;flex-shrink:0;width:100%;box-sizing:border-box}
.mv-sel-footer{padding:8px 10px;border-top:1px solid var(--bdr);flex-shrink:0;
  display:flex;justify-content:flex-end}
.mv-sel-tab{flex:1;height:26px;font-size:11px;font-weight:700;border-radius:var(--rss);
  border:1px solid var(--bdr2);background:var(--s3);color:var(--txt2);cursor:pointer;
  transition:var(--tr);white-space:nowrap;
  display:flex;align-items:center;justify-content:center;text-align:center}
.mv-sel-tab.active{background:var(--acc);color:#fff;border-color:var(--acc)}

/* Save layout modal */
#mv-save-overlay{
  display:none;position:fixed;inset:0;z-index:1200;
  background:rgba(0,0,0,.75);align-items:center;justify-content:center;
}
#mv-save-overlay.open{display:flex}
#mv-save-modal{
  background:var(--s2);border-radius:var(--r);border:1px solid var(--bdr2);
  width:min(320px,90vw);padding:16px;box-shadow:var(--sh);
}
#mv-save-modal h3{font-size:11px;font-weight:800;text-transform:uppercase;
  letter-spacing:1.5px;color:var(--txt2);margin-bottom:10px}
#mv-save-name{height:34px;font-size:13px;margin-bottom:10px}
.mv-save-btns{display:flex;gap:7px;justify-content:flex-end}
.mv-save-btns button{height:32px;padding:0 14px;font-size:12px}

/* Confirm overlay for layout operations */
#mv-confirm-overlay{
  display:none;position:fixed;inset:0;z-index:1300;
  background:rgba(0,0,0,.75);align-items:center;justify-content:center;
}
#mv-confirm-overlay.open{display:flex}
#mv-confirm-modal{
  background:var(--s2);border-radius:var(--r);border:1px solid var(--bdr2);
  width:min(340px,90vw);padding:18px;box-shadow:var(--sh);
}
#mv-confirm-title{font-weight:700;color:var(--txt);margin-bottom:6px}
#mv-confirm-msg{font-size:12px;color:var(--txt2);margin-bottom:14px;line-height:1.5}
.mv-confirm-btns{display:flex;gap:7px;justify-content:flex-end}
.mv-confirm-btns button{height:32px;padding:0 14px;font-size:12px}
</style>
</head>
<body>
<div id="app">

<!-- HEADER -->
<header id="hdr">
  <div id="hdr-bar">
    <div id="cdot"></div>
    <span id="hdr-status">Not connected — tap ⚙ to set up</span>
    <div class="hdr-r">
      <span id="busy-sp" class="spin hidden"></span>
      {{ tags_html | safe }}
      <button class="btn-ghost hdr-ico" id="stopbtn" onclick="doStop()" disabled title="Stop">⏹</button>
      <button class="btn-ghost hdr-ico" onclick="openWhatsOn()" title="What's on Now">📺</button>
      <button class="btn-ghost hdr-ico" onclick="refreshPlaylist()" title="Refresh playlist — clear cache &amp; reconnect" id="refresh-btn">🔄</button>
      <button class="btn-ghost hdr-ico" onclick="openPL()" title="Saved Playlists">📋</button>
      <button class="btn-ghost hdr-ico" id="cast-fab" title="Cast to TV / speaker" style="position:relative">
        <svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18"><path d="M1 18v3h3c0-1.66-1.34-3-3-3zm0-4v2c2.76 0 5 2.24 5 5h2c0-3.87-3.13-7-7-7zm18-7H5c-1.1 0-2 .9-2 2v3h2v-3h14v12h-5v2h5c1.1 0 2-.9 2-2V9c0-1.1-.9-2-2-2zm-18 3v2c4.97 0 9 4.03 9 9h2c0-6.08-4.93-11-11-11z"/></svg>
        <span class="cast-badge" id="cast-nav-badge"></span>
      </button>
      <button class="btn-ghost hdr-ico" onclick="toggleCP()" title="Settings">⚙</button>
    </div>
  </div>
  <div id="cpanel">
    <div id="cpi">
      <div class="ct-row">
        <button class="btn-acc ct-btn" data-t="mac" onclick="setCT('mac')">🔌 MAC</button>
        <button class="btn-ghost ct-btn" data-t="xtream" onclick="setCT('xtream')">📡 Xtream</button>
        <button class="btn-ghost ct-btn" data-t="m3u_url" onclick="setCT('m3u_url')">📄 M3U</button>
      </div>
      <div id="cr-mac" class="cr" style="flex-direction:column;align-items:stretch">
        <div style="display:flex;gap:6px;align-items:center">
          <label>URL</label><input id="i-url" type="text" inputmode="url" placeholder="http://portal.host:8080" autocomplete="new-password" autocorrect="off" spellcheck="false">
          <label>MAC</label><input id="i-mac" placeholder="00:1A:79:XX:XX:XX" style="max-width:200px" autocomplete="new-password" autocorrect="off" spellcheck="false">
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <label title="Optional: external XMLTV EPG URL. Leave blank to use portal's own EPG.">EPG</label><input id="i-mac-epg" type="text" inputmode="url" placeholder="https://… xmltv URL (optional)" autocomplete="new-password" autocorrect="off" spellcheck="false">
        </div>
      </div>
      <div id="cr-xtream" class="cr hidden" style="flex-direction:column;align-items:stretch">
        <div style="display:flex;gap:6px;align-items:center">
          <label>URL</label><input id="i-xu" type="text" inputmode="url" placeholder="http://server.host:8080" autocomplete="new-password" autocorrect="off" spellcheck="false">
          <label>User</label><input id="i-us" placeholder="username" style="max-width:150px" autocomplete="new-password" autocorrect="off" spellcheck="false">
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <label title="Optional: external XMLTV EPG URL (e.g. epg.best). Leave blank to use provider's own EPG.">EPG</label><input id="i-epg" type="text" inputmode="url" placeholder="https://epg.best/xmltv.php?… (optional)" style="flex:1" autocomplete="new-password" autocorrect="off" spellcheck="false">
          <label>Pass</label><input id="i-pw" type="password" placeholder="password" style="max-width:150px" autocomplete="new-password">
        </div>
      </div>
      <div id="cr-m3u" class="cr hidden" style="flex-direction:column;align-items:stretch;gap:5px">
        <!-- URL row -->
        <div style="display:flex;gap:6px;align-items:center">
          <label>URL</label>
          <input id="i-m3u" type="text" inputmode="url" placeholder="http://example.com/list.m3u" autocomplete="new-password" autocorrect="off" spellcheck="false">
        </div>
        <!-- EPG row -->
        <div style="display:flex;gap:6px;align-items:center">
          <label title="Optional: external XMLTV EPG URL. Leave blank to use tvg-url from M3U.">EPG</label>
          <input id="i-m3u-epg" type="text" inputmode="url" placeholder="https://epg.best/xmltv.php?… (optional)" style="max-width:300px" autocomplete="new-password" autocorrect="off" spellcheck="false">
        </div>
        <!-- File row — always visible -->
        <div style="display:flex;gap:6px;align-items:center">
          <label style="flex-shrink:0">File</label>
          <span id="m3u-fp-fname" style="flex:1;font-size:12px;color:var(--txt2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">No file chosen</span>
          <button class="btn-ghost" onclick="m3uOpenPicker()" style="height:28px;padding:0 10px;font-size:12px;flex-shrink:0;white-space:nowrap">📂 Browse…</button>
          <button class="btn-ghost" onclick="m3uForceFileBrowser()" title="Force mobile file browser" style="height:28px;padding:0 8px;font-size:12px;flex-shrink:0;white-space:nowrap">📁</button>
          <button class="btn-ghost" id="m3u-clear-btn" onclick="m3uClearLocal()" style="height:28px;padding:0 8px;font-size:11px;flex-shrink:0;display:none">✕</button>
          <input type="file" id="m3u-local-input" accept=".m3u,.m3u8,audio/x-mpegurl,application/x-mpegurl" style="display:none;position:absolute;width:0;height:0;opacity:0" onchange="m3uLoadLocalFile(this)">
        </div>
        <span id="m3u-fp-status" style="font-size:11px;color:var(--txt2);padding-left:2px"></span>
        <!-- Mobile inline file browser (shown when Browse clicked on mobile) -->
        <div id="m3u-fp-mobile" style="display:none;border:1px solid var(--bdr);border-radius:var(--rsm);background:var(--s3);padding:8px;margin-top:2px">
          <div style="display:flex;gap:5px;margin-bottom:6px;align-items:center">
            <button class="btn-ghost" id="m3u-fb-up" style="height:30px;padding:0 10px;font-size:16px;flex-shrink:0" onclick="m3uFbUp()" title="Up">&#x2191;</button>
            <span id="m3u-fb-path" style="font-size:11px;color:var(--txt2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;align-self:center">/sdcard/Download</span>
            <button class="btn-ghost" onclick="document.getElementById('m3u-fp-mobile').style.display='none'" style="height:26px;padding:0 8px;font-size:11px;flex-shrink:0">✕</button>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="m3uFbNav('/sdcard/Download')">📥 Download</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="m3uFbNav('/storage/emulated/0/Download')">📥 /0/Download</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="m3uFbNav('/sdcard')">📱 /sdcard</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="m3uFbNav('/storage/emulated/0')">📱 /storage/0</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="m3uFbNav('/data/data/com.termux/files/home')">🖥 Termux</button>
          </div>
          <div id="m3u-fb-list" style="max-height:180px;overflow-y:auto;border:1px solid var(--bdr);border-radius:var(--rsm);background:var(--s4)">
            <div style="padding:10px;font-size:12px;color:var(--txt3)">Loading…</div>
          </div>
          <div id="m3u-fp-status-mob" style="font-size:11px;color:var(--txt2);margin-top:4px"></div>
        </div>
      </div>
      <div class="cr-bot">
        <span id="portal-name-label" style="font-size:12px;font-weight:700;color:var(--acc);
              white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:55%;
              opacity:0.85">—</span>
        <div style="display:flex;gap:7px;align-items:center;flex-shrink:0">
          <button class="btn-acc" id="cbtn" onclick="doConnect()" style="height:36px;min-width:120px">🔌 Connect</button>
          <button id="save-profile-chk" onclick="toggleSaveChk(this)"
            style="height:36px;padding:0 12px;font-size:12px;border-radius:var(--rss);
                   border:1px solid var(--bdr2);background:var(--s3);color:var(--txt2);
                   cursor:pointer;white-space:nowrap;transition:var(--tr)"
            >💾 Save</button>
        </div>
      </div>
      <!-- Output paths — always accessible from settings panel -->
      <div style="border-top:1px solid var(--bdr);padding-top:8px;display:flex;flex-direction:column;gap:6px">
        <div style="font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1px;color:var(--txt3);padding-bottom:2px">Output Paths</div>
        <div class="prow" style="position:relative">
          <span class="plbl">M3U:</span>
          <input id="o-m3u" type="text" placeholder="/sdcard/Download/playlist.m3u" oninput="saveFP()" style="height:30px;font-size:12px">
          <button class="btn-ghost psug-btn" onclick="togSug('m3u')" title="Suggestions">📁</button>
          <div class="psug" id="sg-m3u" style="top:auto;bottom:calc(100% + 3px)">
            <div class="psopt" onclick="pickP('m3u','/sdcard/Download/playlist.m3u')">/sdcard/Download/playlist.m3u</div>
            <div class="psopt" onclick="pickP('m3u','/storage/emulated/0/Download/playlist.m3u')">/storage/emulated/0/Download/playlist.m3u</div>
            <div class="psopt" onclick="pickP('m3u','/data/data/com.termux/files/home/playlist.m3u')">Termux ~/playlist.m3u</div>
          </div>
        </div>
        <div class="prow" style="position:relative">
          <span class="plbl">Folder:</span>
          <input id="o-dir" type="text" placeholder="/sdcard/Download/" oninput="saveFP()" style="height:30px;font-size:12px">
          <button class="btn-ghost psug-btn" onclick="togSug('dir')" title="Suggestions">📁</button>
          <div class="psug" id="sg-dir" style="top:auto;bottom:calc(100% + 3px)">
            <div class="psopt" onclick="pickP('dir','/sdcard/Download/')">/sdcard/Download/</div>
            <div class="psopt" onclick="pickP('dir','/storage/emulated/0/Download/')">/storage/emulated/0/Download/</div>
            <div class="psopt" onclick="pickP('dir','/data/data/com.termux/files/home/Downloads/')">Termux ~/Downloads/</div>
          </div>
        </div>
        <div class="prow" style="position:relative" id="extplayer-row-desktop">
          <span class="plbl">Player:</span>
          <input id="o-extplayer" type="text" placeholder="C:\\Program Files\\VLC\\vlc.exe"
            autocomplete="new-password" autocorrect="off" spellcheck="false"
            oninput="saveExtPlayer()" style="height:30px;font-size:12px"
            title="Path to external player executable (e.g. VLC, mpv)">
          <button class="btn-ghost psug-btn" onclick="browseExtPlayer()" title="Browse for player exe" style="font-size:13px">📂</button>
        </div>
        <div id="extplayer-row-mobile" style="display:none;gap:6px;align-items:center">
          <span class="plbl">Player:</span>
          <select id="o-mobile-player" onchange="saveMobilePlayer()" style="flex:1;height:30px;font-size:12px;background:var(--s3);color:var(--txt);border:1.5px solid var(--bdr);border-radius:var(--rsm);padding:0 8px">
            <option value="ask">Ask every time</option>
            <option value="org.videolan.vlc">VLC</option>
            <option value="com.mxtech.videoplayer.ad">MX Player</option>
            <option value="com.mxtech.videoplayer.pro">MX Player Pro</option>
            <option value="com.brouken.player">Just Player</option>
            <option value="com.husudosu.mpvremote">mpv</option>
            <option value="copy">Copy URL</option>
          </select>
        </div>
        <div class="prow" style="position:relative">
          <span class="plbl" style="white-space:nowrap;font-size:10px">&#x1F4AC; Sub:</span>
          <input id="o-subkey" type="text"
            placeholder="OpenSubtitles API key &mdash; free at opensubtitles.com/en/consumers"
            autocomplete="new-password" autocorrect="off" spellcheck="false"
            oninput="saveSubKey()" style="height:30px;font-size:12px"
            title="Your OpenSubtitles Consumer API key. Get one free at opensubtitles.com/en/consumers">
          <a href="https://www.opensubtitles.com/en/consumers" target="_blank" rel="noopener"
            class="btn-ghost psug-btn"
            style="display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:var(--rss);text-decoration:none;font-size:13px;flex-shrink:0;border:1px solid var(--bdr);background:var(--s3);color:var(--txt2)"
            title="Get a free API key at opensubtitles.com/en/consumers">&#x1F511;</a>
        </div>

      </div>
    </div>
  </div>
</header>

<!-- MAIN -->
<main id="main">

  <!-- CATEGORIES -->
  <div class="panel active" id="p-cats">
    <div class="ph">
      <h3>Categories</h3>
      <div class="mtabs">
        <button class="mt" data-m="favs" onclick="toggleFavsFilter()">⭐</button>
        <button class="mt on" data-m="live" onclick="setMode('live')"><span class="mt-ico">📺</span><span class="mt-txt">Live</span></button>
        <button class="mt" data-m="vod" onclick="setMode('vod')"><span class="mt-ico">🎬</span><span class="mt-txt">VOD</span></button>
        <button class="mt" data-m="series" onclick="setMode('series')"><span class="mt-ico">📂</span><span class="mt-txt">Series</span></button>
      </div>
      <!-- Category-level actions accessible via FAB on mobile only -->
    </div>
    <div style="padding:8px 10px 0;flex-shrink:0;display:flex;flex-direction:column;gap:6px">
      <div class="tag-bar" id="tag-bar" style="display:none"></div>
      <div class="sbar"><span class="sico">🔍</span>
        <input id="csrch" type="search" placeholder="Search categories…" oninput="filterCats()">
      </div>

    </div>
    <div style="flex:1;overflow-y:auto;padding:6px 10px 10px;position:relative" id="catlist">
      <div style="text-align:center;padding:48px 20px;color:var(--txt3)">
        <div id="cat-ph-ico" style="font-size:52px;opacity:.13;margin-bottom:12px;animation:float 3s ease infinite">📡</div>
        <div style="font-size:13px">Connect to load categories</div>
      </div>
    </div>

  </div>

  <!-- BROWSE -->
  <div class="panel" id="p-items">
    <div class="ph">
      <h3 id="ittitle">Browse</h3>
      <button class="btn-ghost btn-sm" id="backbtn" onclick="goBack()" disabled>◀ Back</button>
    </div>
    <div style="padding:10px 10px 0;display:flex;flex-direction:column;gap:6px;flex-shrink:0">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:6px">
        <div class="bcrum" id="bcrum" style="flex:1;min-width:0"><span class="bc-s">Categories</span></div>
        <button class="epg-layout-btn" id="epg-grid-btn" onclick="toggleEpgGrid()" title="EPG Grid view" style="display:none">📅 EPG</button>
        <button class="ph-act-btn" onclick="openDrawer('items')" title="Download / Actions" id="ph-items-act-btn">
          ⚡ Actions<span class="ph-act-badge" id="ph-item-badge"></span>
        </button>
      </div>
      <div class="sbar" id="items-sbar"><span class="sico">🔍</span>
        <input id="isrch" type="search" placeholder="Search items…" oninput="filterItems()">
      </div>
    </div>
    <div style="flex:1;overflow-y:auto;padding:6px 10px 0;min-height:0" id="ilist"></div>
    <!-- EPG Grid container (replaces ilist when active) -->
    <div id="epg-grid-wrap">
      <div id="epg-grid-body">
        <div id="epg-ch-col">
          <div id="epg-ch-header"></div>
        </div>
        <div id="epg-tl-col"></div>
      </div>
    </div>
    <div style="padding:0 10px">
      <div class="icount" id="icount"></div>
    </div>

  </div>

  <!-- PLAYER -->
  <div class="panel" id="p-player" style="background:#000">
    <div style="flex:1;display:flex;flex-direction:column;overflow:hidden;min-height:0">
    <div style="flex:1;background:#000;min-height:0;display:flex;flex-direction:column" id="vwrap">
      <video id="vid" controls preload="none" playsinline webkit-playsinline style="flex:1;min-height:0;width:100%;object-fit:contain;background:#000"></video>
      <div id="vph">
        <div id="vph-ico">▶</div>
        <div>No stream loaded</div>
      </div>
    </div>
    <!-- Collapsible player controls -->
    <div class="panel-divider-line"></div>
    <div id="pctrl-panel" style="flex-shrink:0;border-top:1px solid var(--bdr)">
      <div id="pctrl-hdr" onclick="togglePlayerControls()" style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:5px 14px;background:var(--s2);user-select:none">
        <div style="display:flex;align-items:center;gap:7px">
          <span id="pctrl-arrow" style="font-size:10px;color:var(--txt3);transition:transform .2s;display:inline-block">▲</span>
          <h3 style="font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px;color:var(--txt2);margin:0">Player Controls</h3>
        </div>
        <div id="pctrl-hdr" onclick="togglePlayerControls()"
             style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;
             padding:5px 14px;background:var(--s2);user-select:none">

          <!-- RIGHT BUTTON GROUP -->
          <div style="display:flex;align-items:center;gap:6px">

            <button class="btn-ghost pnav" id="theaterbtn"
              onclick="toggleTheater()"
              title="Theater mode"
              style="height:26px;width:32px;padding:0;display:flex;
              align-items:center;justify-content:center">
              <svg id="theater-icon" width="16" height="16" viewBox="0 0 16 16"
                   fill="none" stroke="currentColor" stroke-width="1.8">
                <polyline points="4,2 2,2 2,4"/>
                <polyline points="12,2 14,2 14,4"/>
                <polyline points="4,14 2,14 2,12"/>
                <polyline points="12,14 14,14 14,12"/>
              </svg>
            </button>

            <button id="mv-desktop-btn"
              onclick="event.stopPropagation();mvToggle()"
              title="Multi-View"
              style="height:26px;padding:0 10px;font-size:12px;font-weight:700;
              border-radius:var(--rss);background:var(--s4);color:var(--txt2);
              border:1px solid var(--bdr2);letter-spacing:.5px;display:none">
              ⊞ Multi-View
            </button>

          </div>
        </div>
      </div>
      <div id="pctrl-body" style="overflow:hidden;transition:max-height .25s ease;max-height:0">
        <div class="pinfo">
          <div id="np">No stream loaded</div>
          <div id="pu" onclick="cpyUrl()" title="Tap to copy stream URL">—</div>
        </div>
        <div class="pctrl">
          <div style="display:flex;flex-direction:column;gap:4px;align-self:flex-start;flex-shrink:0" class="pctrl-desktop-only">
            <button class="btn-red" id="rbtn" onclick="togRec()" style="height:28px;padding:0 10px;font-size:12px">⏺ Record</button>
            <button class="btn-ghost" id="dl-now-btn" onclick="dlNowMKV()" title="Download currently playing item as MKV" disabled style="flex-shrink:0;height:28px;padding:0 10px;font-size:12px">⬇ MKV</button>
          </div>
          <div class="btn-vol-group">
          <div class="ctrl-r ctr">
            <button class="btn-ghost pnav" onclick="playerPrev()" title="Prev">&#9198;</button>
            <button class="pbig" id="ppbtn" onclick="playerPP()">&#9654;</button>
            <button class="btn-ghost pnav" onclick="playerStop()" title="Stop">&#9209;</button>
            <button class="btn-ghost pnav" onclick="playerNext()" title="Next">&#9197;</button>
            <button class="btn-ghost pnav" id="epgbtn" onclick="showEPG()" title="EPG" style="font-size:14px;opacity:0.35">&#128197;</button>
            <button class="btn-ghost pnav" id="catchupbtn" onclick="showCatchup()" title="Catch-up TV" style="font-size:16px;opacity:0.35">&#8634;</button>
            <button class="btn-ghost pnav" id="subbtn" onclick="openSubSearch()" title="Subtitles" style="font-size:14px;opacity:0.35">&#128172;</button>
          </div>
          <div style="min-height:12px;padding:0 4px">
            <span id="epg-now" style="font-size:11px;color:var(--txt2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block"></span>
          </div>
          <div class="vrow">
            <span style="font-size:15px;cursor:pointer;user-select:none" title="Mute" onclick="setVol(0);document.getElementById('vol').value=0">&#128265;</span>
            <input type="range" id="vol" min="0" max="100" value="80" oninput="setVol(this.value)">
            <span class="vlbl" id="vlbl">80</span>
            <span style="font-size:15px;cursor:pointer;user-select:none" title="Max volume" onclick="setVol(100);document.getElementById('vol').value=100">&#128266;</span>
          </div>
          </div>
          <div style="display:flex;flex-direction:column;gap:4px;align-self:flex-start;flex-shrink:0;min-width:80px" class="pctrl-desktop-only">
            <span class="rtimer" id="rtimer" style="font-size:11px;color:var(--txt3);text-align:center">00:00:00</span>
            <span class="rfname" id="rfname"></span>
          </div>
        </div>
        <!-- Mobile-only: Record and MKV row shown below controls on small screens -->
        <div class="pctrl-mobile-rec recrow" style="display:none;padding:0 0 4px 0">
          <button class="btn-red" onclick="togRec()" id="rbtn-mob" style="height:28px;padding:0 12px;font-size:12px">⏺ Record</button>
          <span class="rtimer" id="rtimer-mob"></span>
          <span class="rfname" id="rfname-mob"></span>
          <button class="btn-ghost" onclick="window._mobMkvClick()" title="Download MKV" disabled id="dl-now-btn-mob" style="height:28px;padding:0 10px;font-size:12px">⬇ MKV</button>
        </div>
      </div>
    </div>
    </div><!-- end flex:1 player content wrapper -->

    <!-- Desktop-only inline log (hidden on mobile via CSS) -->
    <div id="desktop-log" style="display:none;flex-direction:column;flex-shrink:0;border-top:1px solid var(--bdr)">
      <div id="desktop-log-hdr" onclick="toggleDesktopLog()" style="cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:5px 14px;background:var(--s2);user-select:none">
        <div style="display:flex;align-items:center;gap:7px">
          <span id="desktop-log-arrow" style="font-size:10px;color:var(--txt3);transition:transform .2s">▲</span>
          <h3 style="font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px;color:var(--txt2);margin:0">Activity Log</h3>
        </div>
        <div style="display:flex;gap:6px" onclick="event.stopPropagation()">
          <button class="btn-ghost" onclick="clearLog()" style="height:22px;padding:0 8px;font-size:11px;border-radius:var(--rss)">Clear</button>
          <button class="btn-ghost" onclick="toggleDesktopLog()" style="height:22px;padding:0 8px;font-size:11px;border-radius:var(--rss)">✕</button>
        </div>
      </div>
      <div id="desktop-log-body" style="overflow:hidden;transition:max-height .25s ease;max-height:0">
        <div id="desktop-logout" style="height:180px;overflow-y:auto;padding:8px 12px;
          font-family:'Cascadia Code','JetBrains Mono','Courier New',monospace;
          font-size:11px;line-height:1.7;color:#4a556a;background:var(--bg);
          white-space:pre-wrap;word-break:break-word"></div>
      </div>
    </div>
  </div>

  <!-- EPG OVERLAY -->
  <div id="epg-overlay" style="display:none;position:fixed;inset:0;z-index:900;
    background:rgba(0,0,0,.7);align-items:flex-end;justify-content:center">
    <div style="background:var(--s2);border-radius:var(--rs) var(--rs) 0 0;
      width:100%;max-width:600px;padding:16px;box-shadow:var(--sh);
      border-top:1px solid var(--bdr2);max-height:60vh;overflow-y:auto">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <span style="font-size:13px;font-weight:700;color:var(--txt1)" id="epg-ch-name">EPG</span>
        <button class="btn-ghost" onclick="closeEPG()"
          style="height:28px;width:28px;padding:0;font-size:14px;border-radius:var(--rss)">✕</button>
      </div>
      <div id="epg-body">
        <div style="color:var(--txt3);font-size:12px;text-align:center;padding:20px">Loading…</div>
      </div>
    </div>
  </div>

  <!-- CATCHUP OVERLAY -->
  <div id="catchup-overlay" style="display:none;position:fixed;inset:0;z-index:900;
    background:rgba(0,0,0,.7);align-items:flex-end;justify-content:center">
    <div style="background:var(--s2);border-radius:var(--rs) var(--rs) 0 0;
      width:100%;max-width:600px;padding:16px;box-shadow:var(--sh);
      border-top:1px solid var(--bdr2);max-height:70vh;overflow-y:auto">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
        <div>
          <span style="font-size:13px;font-weight:700;color:var(--txt1)" id="catchup-ch-name">↺ Catch-up TV</span>
          <div style="font-size:11px;color:var(--txt3);margin-top:2px">Select a past programme to watch</div>
        </div>
        <button class="btn-ghost" onclick="closeCatchup()"
          style="height:28px;width:28px;padding:0;font-size:14px;border-radius:var(--rss)">✕</button>
      </div>
      <div id="catchup-status" style="font-size:11px;color:var(--txt3);min-height:14px;margin-bottom:4px"></div>
      <div id="catchup-body" style="margin-top:4px">
        <div style="color:var(--txt3);font-size:12px;text-align:center;padding:20px">Loading…</div>
      </div>
    </div>
  </div>

  <!-- LOG (mobile tab) -->
  <div class="panel" id="p-log" style="background:var(--bg)">
    <div class="ph">
      <h3>Activity Log</h3>
      <button class="btn-ghost" onclick="clearLog()"
        style="height:24px;padding:0 8px;font-size:11px;border-radius:var(--rss)">Clear</button>
    </div>
    <div id="logout" style="flex:1;overflow-y:auto;padding:8px 12px;
      font-family:'Cascadia Code','JetBrains Mono','Courier New',monospace;
      font-size:11px;line-height:1.7;color:#4a556a;white-space:pre-wrap;word-break:break-word"></div>
  </div>

</main>

<!-- ═══════════════════════════════════════════════════════
     MULTIVIEW PANEL  (fixed overlay above #main)
     Structure mirrors multiview.js panel/widget layout
════════════════════════════════════════════════════════ -->
<div id="p-mv">

  <!-- Toolbar — always-visible strip + collapsible controls body -->
  <div id="mv-toolbar">
    <!-- Strip: always visible — toggle + close -->
    <div id="mv-tb-strip">
      <button id="mv-tb-toggle" title="Show/hide toolbar controls" onclick="mvTbToggle()">
        <span id="mv-tb-arrow">▾</span> Controls
      </button>
      <div style="flex:1"></div>
      <button class="btn-ghost" id="mv-close-btn" title="Close Multi-View">⊞ Multi-View ✕</button>
    </div>
    <!-- Collapsible body — hidden on mobile after load -->
    <div id="mv-tb-body">
      <button class="btn-ghost" id="mv-add-btn"     title="Add player">＋ Add</button>
      <button class="btn-ghost" id="mv-remove-btn"  title="Remove last player">－ Remove</button>
      <div class="mv-tb-sep"></div>
      <button class="btn-ghost" id="mv-layout-auto" title="Auto layout">⊞ Auto</button>
      <button class="btn-ghost" id="mv-layout-1p1"  title="1+1: two equal players">1＋1</button>
      <button class="btn-ghost" id="mv-layout-1p2"  title="1+2: large left, two stacked right">1＋2</button>
      <div class="mv-tb-sep"></div>
      <button class="btn-ghost" id="mv-save-btn"    title="Save current layout">💾 Save</button>
      <select id="mv-layouts-sel" title="Saved layouts">
        <option value="" disabled selected>Load layout…</option>
      </select>
      <button class="btn-ghost" id="mv-load-btn"    title="Load selected layout">Load</button>
      <button class="btn-ghost" id="mv-delete-btn"  title="Delete selected layout">🗑</button>
    </div>
  </div>

  <!-- Gridstack grid — id mirrors multiview.js GridStack.init('#multiview-grid') -->
  <div id="mv-grid-wrap">
    <div class="grid-stack" id="multiview-grid"></div>
  </div>

</div>

<!-- ── Channel selector modal ──────────────────────────────
     Mirrors multiviewChannelSelectorModal in multiview.js  -->
<div id="mv-sel-overlay">
  <div id="mv-sel-modal">
    <div class="mv-sel-hdr">
      <button id="mv-sel-back" style="display:none;background:none;border:none;
        color:var(--txt2);font-size:13px;font-weight:700;padding:0 8px 0 0;
        cursor:pointer;white-space:nowrap">← Back</button>
      <h3 id="mv-sel-title">Browse Categories</h3>
      <button class="btn-ghost" id="mv-sel-close"
        style="height:26px;width:26px;padding:0;font-size:14px;flex-shrink:0">✕</button>
    </div>
    <!-- Mode tabs — only visible when in category list (cats mode) -->
    <div id="mv-sel-tabs">
      <button class="mv-sel-tab active" data-mode="live"   onclick="_mvSelSetMode('live')"  >📡 Live</button>
      <button class="mv-sel-tab"        data-mode="vod"    onclick="_mvSelSetMode('vod')"   >🎬 VOD</button>
      <button class="mv-sel-tab"        data-mode="series" onclick="_mvSelSetMode('series')">📺 Series</button>
    </div>
    <input id="mv-sel-search" type="search" placeholder="Search…"/>
    <div id="mv-sel-list"></div>
    <!-- Inline context popup for items (submenu ⋮) -->
    <div id="mv-item-ctx" class="mv-item-ctx"></div>
    <div class="mv-sel-footer">
      <button class="btn-ghost" id="mv-sel-cancel"
        style="height:30px;padding:0 14px;font-size:12px">Cancel</button>
    </div>
  </div>
</div>

<!-- ── Save layout modal ───────────────────────────────────
     Mirrors saveLayoutModal in multiview.js              -->
<div id="mv-save-overlay">
  <div id="mv-save-modal">
    <h3>Save Layout</h3>
    <input id="mv-save-name" type="text" placeholder="Layout name…"/>
    <div class="mv-save-btns">
      <button class="btn-ghost" id="mv-save-cancel">Cancel</button>
      <button class="btn-acc"   id="mv-save-ok">💾 Save</button>
    </div>
  </div>
</div>

<!-- ── Confirm modal (layout operations) ─────────────────── -->
<div id="mv-confirm-overlay">
  <div id="mv-confirm-modal">
    <div id="mv-confirm-title">Confirm</div>
    <div id="mv-confirm-msg"></div>
    <div class="mv-confirm-btns">
      <button class="btn-ghost" id="mv-confirm-cancel">Cancel</button>
      <button class="btn-acc"   id="mv-confirm-ok">OK</button>
    </div>
  </div>
</div>

<!-- ITEM CONTEXT MENU -->
<div id="item-menu">
  <div id="item-menu-hdr">Options</div>
  <div class="imenu-sep" id="imenu-sep1"></div>
  <button class="imenu-btn" id="imenu-epg"      onclick="iMenuEPG()">     <span class="imenu-ico">📅</span>EPG / Programme Info</button>
  <button class="imenu-btn" id="imenu-catchup"  onclick="iMenuCatchup()"> <span class="imenu-ico">↺</span>Catch-up TV</button>
  <div class="imenu-sep" id="imenu-sep2"></div>
  <button class="imenu-btn" id="imenu-ext"      onclick="iMenuExternal()"><span class="imenu-ico">🎬</span>External Player</button>
  <button class="imenu-btn" id="imenu-imdb"     onclick="iMenuIMDB()">    <span class="imenu-ico">🔍</span>Open TMDB/IMDB</button>
  <button class="imenu-btn" id="imenu-rec"      onclick="iMenuRec()">     <span class="imenu-ico">⏺</span>Record</button>
  <button class="imenu-btn" id="imenu-mkv"      onclick="iMenuMKV()">     <span class="imenu-ico">⬇</span>Download MKV</button>
</div>
<div id="item-menu-bg" onclick="closeItemMenu()" style="display:none;position:fixed;inset:0;z-index:799"></div>

<!-- BOTTOM NAV -->
<nav id="botnav">
  <button class="nt on" id="t-cats" onclick="showT('p-cats','t-cats')">
    <span class="nt-ico">📁</span><span>Browse</span>
  </button>
  <button class="nt" id="t-items" onclick="showT('p-items','t-items')">
    <span class="nt-ico">📋</span><span>Items</span>
    <span class="badge" id="badge"></span>
  </button>
  <button class="nt" id="t-player" onclick="showT('p-player','t-player')">
    <span class="nt-ico">▶️</span><span>Player</span>
  </button>
  <button class="nt" id="t-mv" onclick="mvToggle()">
    <span class="nt-ico">⊞</span><span>Multi</span>
  </button>
  <button class="nt" id="t-log" onclick="showT('p-log','t-log')">
    <span class="nt-ico">📜</span><span>Log</span>
  </button>
  <button class="nt" id="t-act" onclick="openActTab()">
    <span class="nt-ico">⚡</span><span>Actions</span>
    <span class="fab-badge" id="act-tab-badge"></span>
    <span class="act-ind" id="act-ind"></span>
  </button>

</nav>

<!-- ACTION DRAWER -->
<div id="act-overlay" onclick="closeDrawer()"></div>
<div id="act-drawer">
  <div class="adr-hdr">
    <h3 id="adr-title">⚡ Actions</h3>
    <button class="btn-ghost" onclick="closeDrawer()" style="height:32px;padding:0 12px;font-size:13px">✕</button>
  </div>
  <div class="adr-body">
    <!-- Recording section — always visible -->
    <div id="adr-rec-section">
      <div class="adr-section-title">⏺ Recording</div>
      <button id="adr-rec-btn" onclick="togRec()">⏺ Record</button>
      <div id="adr-rec-info">
        <div id="adr-rec-timer">00:00:00</div>
        <div id="adr-rec-fname"></div>
        <button class="btn-ghost adr-rec-open" onclick="openDrawer();closeDrawer();" style="width:100%;height:34px;font-size:12px;font-weight:600;margin-top:4px" id="adr-rec-open">📂 Open player controls</button>
      </div>
    </div>
    <div id="adr-cats-content" class="hidden">
      <div class="adr-section">
        <div class="adr-section-title">Select Categories</div>
        <div class="adr-sel-row">
          <button class="btn-ghost" onclick="selAllCats(true)">☑ All</button>
          <button class="btn-ghost" onclick="selAllCats(false)">☐ None</button>
        </div>
        <div class="adr-count" id="adr-cat-count">0 selected</div>
      </div>
      <div class="adr-section">
        <div class="adr-section-title">Download Selected</div>
        <button class="adr-btn btn-blue" id="adr-cat-m3u" onclick="dlSelCats('m3u')" disabled>
          <span class="adr-ico">💾</span>
          <span class="adr-lbl">Export as M3U</span>
          <span class="adr-sub" id="adr-cat-m3u-sub"></span>
        </button>
        <button class="adr-btn btn-acc" id="adr-cat-mkv" onclick="dlSelCats('mkv')" disabled>
          <span class="adr-ico">🎬</span>
          <span class="adr-lbl">Download as MKV</span>
          <span class="adr-sub" id="adr-cat-mkv-sub"></span>
        </button>
      </div>
      <div class="adr-progress" id="adr-progress-cats">
        <div class="adr-prog-hdr">
          <div class="adr-prog-title" id="adr-prog-cats-title">Downloading...</div>
          <div style="display:flex;gap:5px;align-items:center">
            <button class="adr-prog-stop" id="adr-prog-cats-stop" onclick="doStop()" title="Stop download">⏹</button>
            <button class="adr-prog-dismiss" id="adr-prog-cats-dismiss" onclick="dismissProgress('cats')" title="Dismiss" style="display:none">✕</button>
          </div>
        </div>
        <div class="adr-prog-label" id="adr-prog-cats-label"></div>
        <div class="adr-prog-bar-wrap"><div class="adr-prog-bar" id="adr-prog-cats-bar"></div></div>
        <div class="adr-prog-footer">
          <div class="adr-prog-count" id="adr-prog-cats-count"></div>
          <div class="adr-prog-speed" id="adr-prog-cats-speed"></div>
        </div>
      </div>
    </div>
    <!-- ITEMS mode -->
    <div id="adr-items-content" class="hidden">
      <div class="adr-section">
        <div class="adr-section-title">Select Items</div>
        <div class="adr-sel-row">
          <button class="btn-ghost" onclick="selAll(true)">☑ All</button>
          <button class="btn-ghost" onclick="selAll(false)">☐ None</button>
        </div>
        <div class="adr-count" id="adr-item-count">0 selected</div>
      </div>
      <div class="adr-section">
        <div class="adr-section-title">Selected Items</div>
        <button class="adr-btn btn-blue" id="adr-dlm3u" onclick="dlM3U()" disabled>
          <span class="adr-ico">💾</span>
          <span class="adr-lbl">Export selected → M3U</span>
          <span class="adr-sub" id="adr-m3u-sub"></span>
        </button>
        <button class="adr-btn btn-acc" id="adr-dlmkv" onclick="dlMKV()" disabled>
          <span class="adr-ico">🎬</span>
          <span class="adr-lbl">Download selected → MKV</span>
          <span class="adr-sub" id="adr-mkv-sub"></span>
        </button>
      </div>
      <div class="adr-section">
        <div class="adr-section-title">Whole Category</div>
        <button class="adr-btn btn-ghost" onclick="dlCat()">
          <span class="adr-ico">📂</span>
          <span class="adr-lbl">Export entire category → M3U</span>
          <span class="adr-sub" id="adr-cat-all-sub"></span>
        </button>
      </div>
      <div class="adr-progress" id="adr-progress-items">
        <div class="adr-prog-hdr">
          <div class="adr-prog-title" id="adr-prog-items-title">Downloading...</div>
          <div style="display:flex;gap:5px;align-items:center">
            <button class="adr-prog-stop" id="adr-prog-items-stop" onclick="doStop()" title="Stop download">⏹</button>
            <button class="adr-prog-dismiss" id="adr-prog-items-dismiss" onclick="dismissProgress('items')" title="Dismiss" style="display:none">✕</button>
          </div>
        </div>
        <div class="adr-prog-label" id="adr-prog-items-label"></div>
        <div class="adr-prog-bar-wrap"><div class="adr-prog-bar" id="adr-prog-items-bar"></div></div>
        <div class="adr-prog-footer">
          <div class="adr-prog-count" id="adr-prog-items-count"></div>
          <div class="adr-prog-speed" id="adr-prog-items-speed"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<div id="toasts"></div>

<!-- WHAT'S ON NOW MODAL -->
<div id="won-overlay" onclick="if(event.target===this)closeWhatsOn()">
  <div id="won-modal">
    <div class="won-hdr">
      <h3>📺 What's on Now</h3>
      <span class="won-count" id="won-count">—</span>
      <button class="btn-ghost" onclick="closeWhatsOn()" style="height:28px;padding:0 10px;font-size:12px">✕</button>
    </div>
    <div class="won-search">
      <input id="won-srch" type="search" placeholder="Filter by title or channel…" oninput="wonFilter()" autocomplete="new-password">
    </div>
    <div class="won-list" id="won-list">
      <div class="won-loading"><span class="spin"></span> Loading EPG data…</div>
    </div>
    <div class="won-ftr">
      <button class="btn-ghost" onclick="closeWhatsOn()" style="height:32px;padding:0 14px;font-size:12px">Close</button>
    </div>
  </div>
</div>


<!-- SUBTITLE SEARCH MODAL -->
<div id="sub-overlay" onclick="if(event.target===this)closeSubSearch()">
  <div id="sub-modal">
    <div class="sub-hdr">
      <h3>&#x1F4AC; Subtitle Search</h3>
      <div id="sub-active-info" style="display:none" class="sub-active-strip">
        <span>&#x2713;</span><span id="sub-active-name" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
        <button onclick="clearSubtitle()" style="background:none;border:none;color:var(--green);cursor:pointer;padding:0;font-size:12px;margin-left:2px" title="Remove subtitle">&#x2715;</button>
      </div>
      <button class="btn-ghost" onclick="closeSubSearch()" style="height:28px;padding:0 10px;font-size:12px;margin-left:6px">&#x2715;</button>
    </div>
    <div class="sub-body">
      <!-- Tab switcher: Online Search vs Local File -->
      <div class="sub-tab-row" id="sub-tab-row">
        <button class="sub-tab-btn active" id="sub-tab-online" onclick="subSwitchTab('online')">&#x1F50D; Online Search</button>
        <button class="sub-tab-btn" id="sub-tab-local" onclick="subSwitchTab('local')">&#x1F4C2; Local File</button>
      </div>

      <!-- ONLINE SEARCH PANEL -->
      <div id="sub-panel-online">
      <div class="sub-search-row">
        <input id="sub-query" type="search" placeholder="Title (auto-filled from player)&hellip;"
          autocomplete="new-password" autocorrect="off" spellcheck="false"
          onkeydown="if(event.key==='Enter')subSearch()">
        <button class="btn-acc" onclick="subSearch()" id="sub-search-btn">&#x1F50D; Search</button>
      </div>
      <div class="sub-filters">
        <div class="sub-filter-group" style="flex:1;min-width:200px">
          <label class="grp-lbl">Language</label>
          <div class="sub-lang-grid" id="sub-lang-grid"></div>
        </div>
        <div class="sub-filter-group" style="min-width:180px">
          <label class="grp-lbl">Type</label>
          <div class="sub-type-row">
            <label class="sub-type-chip"><input type="radio" name="sub-type" value="movie" id="sub-type-movie" checked onchange="subToggleEp()"> &#x1F3AC; Movie</label>
            <label class="sub-type-chip"><input type="radio" name="sub-type" value="series" id="sub-type-series" onchange="subToggleEp()"> &#x1F4FA; Series</label>
          </div>
          <div class="sub-ep-row" id="sub-ep-row" style="display:none;margin-top:6px">
            <label>Season</label>
            <input id="sub-season" type="number" min="1" placeholder="S#" oninput="subSeasonChange()">
            <label>Episode</label>
            <input id="sub-episode" type="number" min="1" placeholder="Ep#">
          </div>
        </div>
        <div class="sub-filter-group" style="min-width:80px">
          <label class="grp-lbl">Max results</label>
          <select id="sub-maxresults" style="height:28px;font-size:12px;background:var(--s4);color:var(--txt);border:1px solid var(--bdr2);border-radius:var(--rss);padding:0 8px">
            <option value="10">10</option>
            <option value="20" selected>20</option>
            <option value="40">40</option>
          </select>
        </div>
      </div>
      <div id="sub-results-wrap">
        <div class="sub-empty" id="sub-placeholder">
          <span>&#x1F4AC;</span>
          Search for subtitles &mdash; title is auto-filled from what&apos;s playing.
        </div>
      </div>
      </div><!-- /sub-panel-online -->

      <!-- LOCAL FILE PANEL -->
      <div id="sub-panel-local" style="display:none;padding:10px 0 4px 0">
        <!-- DESKTOP: native file picker via tkinter -->
        <div id="sub-local-desktop">
          <div style="margin-bottom:8px;font-size:12px;color:var(--txt2);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px">
            <span>Choose a local subtitle file (.srt, .vtt, .ass, .ssa).</span>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 8px;opacity:0.7" onclick="subForceFileBrowser()" title="Switch to file browser (Android)">📁 File browser</button>
          </div>
          <div class="sub-search-row" style="align-items:center;gap:8px">
            <button class="btn-ghost" style="height:32px;padding:0 14px;font-size:12px;display:inline-flex;align-items:center;gap:6px;flex-shrink:0"
              onclick="subBrowseDesktop()">&#x1F4C2; Choose file&hellip;</button>
            <input type="file" id="sub-local-input" accept=".srt,.vtt,.ass,.ssa,text/plain"
              style="display:none;position:absolute;width:0;height:0;opacity:0" onchange="subLoadLocalFile(this)">
            <span id="sub-local-filename" style="font-size:12px;color:var(--txt2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">No file chosen</span>
          </div>
        </div>
        <!-- MOBILE: inline file browser -->
        <div id="sub-local-mobile" style="display:none">
          <div class="sub-search-row" style="gap:5px;margin-bottom:6px">
            <button class="btn-ghost" id="sub-fb-up" style="height:30px;padding:0 10px;font-size:16px;flex-shrink:0" onclick="subFbUp()" title="Up">&#x2191;</button>
            <span id="sub-fb-path" style="font-size:11px;color:var(--txt2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;align-self:center">/sdcard/Download</span>
          </div>
          <!-- Quick-jump roots -->
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="subFbNav('/sdcard/Download')">📥 Download</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="subFbNav('/storage/emulated/0/Download')">📥 /0/Download</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="subFbNav('/sdcard')">📱 /sdcard</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="subFbNav('/storage/emulated/0')">📱 /storage/0</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="subFbNav('/data/data/com.termux/files/home')">🖥 Termux</button>
          </div>
          <div id="sub-fb-list" style="max-height:200px;overflow-y:auto;border:1px solid var(--bdr);border-radius:var(--rsm);background:var(--s3)">
            <div style="padding:10px;font-size:12px;color:var(--txt3)">Loading…</div>
          </div>
        </div>
        <div id="sub-local-status" style="font-size:11px;color:var(--txt2);margin-top:4px"></div>
      </div>
    </div>
    <div class="sub-status-bar">
      <span id="sub-status-msg">Ready</span>
      <div class="sub-delay-row" id="sub-delay-row" style="display:none">
        <span>&#9201; Delay:</span>
        <button onclick="subAdjustDelay(-0.1)" title="-0.1s">&#x2212;</button>
        <span id="sub-delay-val">0.0s</span>
        <button onclick="subAdjustDelay(0.1)" title="+0.1s">&#x2b;</button>
        <button onclick="subAdjustDelay(-subDelayMs/1000)" title="Reset" style="font-size:10px;width:34px">Reset</button>
        <button id="sub-toggle-btn" onclick="subToggleVisible()" title="Hide/show subtitles" style="width:auto;padding:0 7px;font-size:11px;margin-left:2px">&#x1F441; On</button>
      </div>
      <button class="btn-ghost" onclick="closeSubSearch()" style="height:28px;padding:0 12px;font-size:12px">Close</button>
    </div>
  </div>
</div>

<!-- SAVED PLAYLISTS MODAL -->
<div id="pl-overlay" onclick="if(event.target===this)closePL()">
  <div id="pl-modal">
    <div class="plm-hdr">
      <h2>📋 Saved Playlists</h2>
      <button class="btn-ghost" onclick="closePL()"
        style="height:28px;padding:0 10px;font-size:12px">✕ Close</button>
    </div>
    <div class="pl-list" id="pl-list"></div>
    <div class="pl-add">
      <h3>Add / Edit Playlist</h3>
      <div class="pl-form">
        <div class="pl-ct-row">
          <button class="btn-acc pl-ct-btn" data-t="mac" onclick="plSetCT('mac')">🔌 MAC</button>
          <button class="btn-ghost pl-ct-btn" data-t="xtream" onclick="plSetCT('xtream')">📡 Xtream</button>
          <button class="btn-ghost pl-ct-btn" data-t="m3u_url" onclick="plSetCT('m3u_url')">📄 M3U</button>
        </div>
        <div class="pl-row"><label>Name</label><input id="pl-name" placeholder="My Playlist" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
        <div id="plf-mac">
          <div class="pl-row"><label>URL</label><input id="pl-url" type="text" inputmode="url" placeholder="http://portal.host:8080" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
          <div class="pl-row"><label>MAC</label><input id="pl-mac" placeholder="00:1A:79:XX:XX:XX" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
          <div class="pl-row"><label>EPG</label><input id="pl-mac-epg" type="text" inputmode="url" placeholder="External EPG URL (optional)" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
        </div>
        <div id="plf-xtream" class="hidden">
          <div class="pl-row"><label>URL</label><input id="pl-xu" type="text" inputmode="url" placeholder="http://server.host:8080" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
          <div class="pl-row"><label>User</label><input id="pl-us" placeholder="username" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
          <div class="pl-row"><label>Pass</label><input id="pl-pw" type="password" placeholder="password" autocomplete="new-password"></div>
          <div class="pl-row"><label>EPG</label><input id="pl-epg" type="text" inputmode="url" placeholder="External EPG URL (optional)" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
        </div>
        <div id="plf-m3u" class="hidden">
          <div class="pl-row"><label>URL</label><input id="pl-m3u" type="text" inputmode="url" placeholder="http://example.com/list.m3u" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
          <div class="pl-row"><label>EPG</label><input id="pl-m3u-epg" type="text" inputmode="url" placeholder="External EPG URL (optional)" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
        </div>
        <div class="pl-row" style="justify-content:flex-end;gap:7px">
          <button class="btn-ghost" onclick="plClearForm()" style="height:34px;padding:0 12px;font-size:12px">Clear</button>
          <button class="btn-acc" onclick="plSave()" style="height:34px;padding:0 16px;font-size:12px">💾 Save</button>
        </div>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js" crossorigin="anonymous"></script>
<script>if(typeof Hls==='undefined'){document.write('<scr'+'ipt src="https://unpkg.com/hls.js@1.5.7/dist/hls.min.js"><\/scr'+'ipt>');}</script>
<script src="https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack.min.css"/>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack-extra.min.css"/>
<script src="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack-all.min.js"></script>
<script>
const CFG = {{ config | safe }};

// ── STATE ──────────────────────────────────────────────────
let CT='mac', mode='live', curCat=null;
let allCats=[], catsCache={}, selCats=new Map();
let categoryItemsCache = {};   // <-- add this (mode -> { key: items[] })
let allItems=[], filtItems=[], navStack=[], selSet=new Set();
let pUrl='', pName='', pIdx=-1;
let isStalker=false;  // true when connected to a stalker_portal MAC portal
let _dlActive=false, _dlTaskType='', _dlItemNames=[];
let hlsObj=null, mpegtsObj=null, recTmr=null, isRec=false, logEs=null, cpOpen=false;
const vid = document.getElementById('vid');


// ── SUBTITLES ──────────────────────────────────────────────
const SUB_LANGS = [
  {code:'en',label:'English'},{code:'sr',label:'Serbian'},{code:'hr',label:'Croatian'},
  {code:'es',label:'Spanish'},{code:'fr',label:'French'},{code:'de',label:'German'},
  {code:'it',label:'Italian'},{code:'pt',label:'Portuguese'},{code:'ru',label:'Russian'},
  {code:'nl',label:'Dutch'},{code:'pl',label:'Polish'},{code:'tr',label:'Turkish'},
  {code:'sv',label:'Swedish'},{code:'hu',label:'Hungarian'},{code:'cs',label:'Czech'},
  {code:'ro',label:'Romanian'},{code:'bg',label:'Bulgarian'},{code:'uk',label:'Ukrainian'},
  {code:'el',label:'Greek'},{code:'ar',label:'Arabic'},{code:'zh',label:'Chinese'},
  {code:'ja',label:'Japanese'},{code:'ko',label:'Korean'},
];

let _subActiveFile  = null;
let _subCuesBase    = [];     // [{startMs, endMs, text}] — never mutated after parse
let subDelayMs      = 0;
let _subTrackObj    = null;   // single TextTrack added via addTextTrack() — reused, never removed

// ── Native TextTrack helpers ────────────────────────────────
// We use vid.addTextTrack() + VTTCue instead of <track> DOM elements.
// HLS.js's SubtitleTrackController is disabled in the Hls() config, so
// it never reacts to textTrack changes — no stream reloads, no crashes.

function _subGetOrCreateTrack(){
  if(_subTrackObj) return _subTrackObj;
  _subTrackObj = vid.addTextTrack('subtitles', 'Subtitle', 'und');
  return _subTrackObj;
}

function _subClearNativeTrack(){
  if(!_subTrackObj) return;
  const list = _subTrackObj.cues;
  while(list && list.length){ try{ _subTrackObj.removeCue(list[0]); }catch(e){ break; } }
  _subTrackObj.mode = 'disabled';
}

function _subLoadCuesToTrack(cues){
  const track = _subGetOrCreateTrack();
  // Clear previous cues
  const list = track.cues;
  while(list && list.length){ try{ track.removeCue(list[0]); }catch(e){ break; } }
  // Add new cues
  const offsetSec = subDelayMs / 1000;
  for(const c of cues){
    const startSec = Math.max(0, c.startMs/1000 + offsetSec);
    const endSec   = Math.max(startSec + 0.001, c.endMs/1000 + offsetSec);
    try{ track.addCue(new VTTCue(startSec, endSec, c.text)); }catch(e){}
  }
  track.mode = 'showing';
}

// ── Parse any format into cues ──────────────────────────────
function _subParseCues(content, mime, fileName){
  const lower = (fileName||'').toLowerCase();
  if(lower.endsWith('.ass') || lower.endsWith('.ssa')) return _parseAssCues(content);
  if(lower.endsWith('.vtt') || mime === 'text/vtt')    return _parseVttCues(content);
  return _parseSrtCues(content);
}

function _tsToMs(ts){
  ts = ts.trim().replace(',','.');
  const parts = ts.split(':');
  if(parts.length < 3) return 0;
  const [h, m, s] = parts;
  return (parseInt(h)*3600 + parseInt(m)*60 + parseFloat(s)) * 1000;
}

function _parseSrtCues(srt){
  const cues = [];
  const blocks = srt.replace(/\r\n/g,'\n').replace(/\r/g,'\n').split(/\n\s*\n/);
  for(const block of blocks){
    const lines = block.trim().split('\n');
    if(lines.length < 2) continue;
    let tsLine = -1;
    for(let i=0;i<lines.length;i++){ if(lines[i].includes('-->')){tsLine=i;break;} }
    if(tsLine < 0) continue;
    const m = lines[tsLine].match(/(\d[\d:,\.]+)\s*-->\s*(\d[\d:,\.]+)/);
    if(!m) continue;
    const text = lines.slice(tsLine+1).join('\n').replace(/<\/?[^>]+>/g,'').trim();
    if(!text) continue;
    cues.push({startMs: _tsToMs(m[1]), endMs: _tsToMs(m[2]), text});
  }
  return cues;
}

function _parseVttCues(vtt){
  const cues = [];
  const blocks = vtt.replace(/\r\n/g,'\n').replace(/\r/g,'\n').split(/\n\s*\n/);
  for(const block of blocks){
    const lines = block.trim().split('\n');
    let tsLine = -1;
    for(let i=0;i<lines.length;i++){ if(lines[i].includes('-->')){tsLine=i;break;} }
    if(tsLine < 0) continue;
    const m = lines[tsLine].match(/(\d[\d:\.]+)\s*-->\s*(\d[\d:\.]+)/);
    if(!m) continue;
    const text = lines.slice(tsLine+1).join('\n').replace(/<\/?[^>]+>/g,'').trim();
    if(!text) continue;
    cues.push({startMs: _tsToMs(m[1]), endMs: _tsToMs(m[2]), text});
  }
  return cues;
}

function _parseAssCues(ass){
  const cues = [];
  const lines = ass.replace(/\r\n/g,'\n').replace(/\r/g,'\n').split('\n');
  for(const line of lines){
    const m = line.match(/^Dialogue:\s*\d+,(\d+:\d{2}:\d{2}\.\d{2}),(\d+:\d{2}:\d{2}\.\d{2}),[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,(.*)$/);
    if(!m) continue;
    const _assTs = t => { const [h,min,sec]=t.split(':'); const [s,cs]=sec.split('.'); return (parseInt(h)*3600+parseInt(min)*60+parseFloat(s+'.'+cs))*1000; };
    const text = m[3].replace(/\{[^}]*\}/g,'').replace(/\\N/gi,'\n').replace(/\\n/gi,'\n').trim();
    if(!text) continue;
    cues.push({startMs: _assTs(m[1]), endMs: _assTs(m[2]), text});
  }
  return cues;
}

// ── Apply subtitle (called from both online + local paths) ──
function _subApplyToPlayer(content, fileName, mime){
  _subCuesBase = _subParseCues(content, mime, fileName);
  subDelayMs   = 0;
  _subLoadCuesToTrack(_subCuesBase);

  const _dv = document.getElementById('sub-delay-val');
  if(_dv) _dv.textContent = '0.0s';
  const _dr = document.getElementById('sub-delay-row');
  if(_dr) _dr.style.display = 'flex';
  const _tb = document.getElementById('sub-toggle-btn');
  if(_tb) _tb.innerHTML = '&#x1F441; On';
}

function subAdjustDelay(deltaSec){
  if(!_subCuesBase.length){ toast('No subtitle loaded','w'); return; }
  subDelayMs += Math.round(deltaSec * 1000);
  // Re-load all cues with new offset applied directly during load
  _subLoadCuesToTrack(_subCuesBase);
  const dv = document.getElementById('sub-delay-val');
  if(dv) dv.textContent = (subDelayMs>=0?'+':'') + (subDelayMs/1000).toFixed(1) + 's';
}

function subToggleVisible(){
  if(!_subTrackObj) return;
  const nowShowing = _subTrackObj.mode === 'showing';
  _subTrackObj.mode = nowShowing ? 'hidden' : 'showing';
  const btn = document.getElementById('sub-toggle-btn');
  if(btn) btn.innerHTML = !nowShowing ? '&#x1F441; On' : '&#x1F648; Off';
}

function clearSubtitle(){
  _subClearNativeTrack();
  _subCuesBase = []; subDelayMs = 0;
  _subActiveFile = null;
  const info = document.getElementById('sub-active-info');
  if(info) info.style.display='none';
  const subBtn = document.getElementById('subbtn');
  if(subBtn) subBtn.style.opacity='0.35';
  const _dr = document.getElementById('sub-delay-row');
  if(_dr) _dr.style.display = 'none';
  const _tb = document.getElementById('sub-toggle-btn');
  if(_tb) _tb.innerHTML = '&#x1F441; On';
  toast('Subtitle removed','info');
}

// ── SUBTITLE TAB SWITCHER ──────────────────────────────────
function subForceFileBrowser(){
  document.getElementById('sub-local-desktop').style.display = 'none';
  document.getElementById('sub-local-mobile').style.display  = '';
  document.getElementById('sub-local-status').textContent = '';
  subFbNav(_subFbCurrentPath);
}
function subSwitchTab(tab){
  const isOnline = tab === 'online';
  document.getElementById('sub-panel-online').style.display = isOnline ? '' : 'none';
  document.getElementById('sub-panel-local').style.display  = isOnline ? 'none' : '';
  document.getElementById('sub-tab-online').classList.toggle('active', isOnline);
  document.getElementById('sub-tab-local').classList.toggle('active', !isOnline);
  if(!isOnline){
    document.getElementById('sub-local-desktop').style.display = _isMobile ? 'none' : '';
    document.getElementById('sub-local-mobile').style.display  = _isMobile ? ''     : 'none';
    document.getElementById('sub-local-status').textContent = '';
    if(_isMobile){
      // Auto-load the browser starting at Download folder
      subFbNav(_subFbCurrentPath);
    } else {
      const inp = document.getElementById('sub-local-input');
      if(inp) inp.value = '';
      document.getElementById('sub-local-filename').textContent = 'No file chosen';
    }
  }
}

// ── DESKTOP: tkinter file picker ───────────────────────────
async function subBrowseDesktop(){
  const stEl = document.getElementById('sub-local-status');
  stEl.textContent = 'Opening file picker…';
  try{
    const r = await fetch('/api/browse_subtitle');
    const d = await r.json();
    if(d.error || !d.path){ stEl.textContent = d.error ? '⚠ '+d.error : 'No file selected.'; return; }
    stEl.textContent = 'Loading…';
    document.getElementById('sub-local-filename').textContent = d.path.split(/[\\/]/).pop();
    await _subLoadFromServerPath(d.path, stEl);
  } catch(e){
    // tkinter not available (e.g. headless) — fall back to browser file picker
    stEl.textContent = '';
    document.getElementById('sub-local-input').value = '';
    document.getElementById('sub-local-input').click();
  }
}

// ── MOBILE: inline file browser ────────────────────────────
let _subFbCurrentPath = '/sdcard/Download';

function subFbUp(){
  const el = document.getElementById('sub-fb-path');
  const cur = (el && el.textContent) || _subFbCurrentPath;
  const parent = cur.replace(/\/[^/]+$/, '') || '/';
  subFbNav(parent);
}

async function subFbNav(path){
  _subFbCurrentPath = path;
  const listEl  = document.getElementById('sub-fb-list');
  const pathEl  = document.getElementById('sub-fb-path');
  const upBtn   = document.getElementById('sub-fb-up');
  if(pathEl) pathEl.textContent = path;
  listEl.innerHTML = '<div style="padding:10px;font-size:12px;color:var(--txt3)">Loading…</div>';

  try{
    const r = await fetch('/api/browse_dir',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path}),
    });
    const d = await r.json();
    if(upBtn) upBtn.disabled = !d.parent;

    if(d.error && !d.dirs.length && !d.files.length){
      listEl.innerHTML = `<div style="padding:10px;font-size:12px;color:#f87171">⚠ ${esc(d.error)}</div>`;
      return;
    }

    const rows = [];
    // Dirs first
    for(const name of d.dirs){
      const fullPath = path.replace(/\/+$/,'') + '/' + name;
      rows.push(`<div class="sub-fb-row sub-fb-dir" onclick="subFbNav('${esc(fullPath)}')">
        <span class="sub-fb-icon">📁</span><span class="sub-fb-name">${esc(name)}</span><span class="sub-fb-arr">›</span>
      </div>`);
    }
    // Subtitle files
    for(const name of d.files){
      const fullPath = path.replace(/\/+$/,'') + '/' + name;
      rows.push(`<div class="sub-fb-row sub-fb-file" onclick="subFbPickFile('${esc(fullPath)}','${esc(name)}')">
        <span class="sub-fb-icon">💬</span><span class="sub-fb-name">${esc(name)}</span>
      </div>`);
    }
    if(!rows.length){
      rows.push('<div style="padding:10px;font-size:12px;color:var(--txt3)">No subtitle files here. Tap a folder to browse.</div>');
    }
    listEl.innerHTML = rows.join('');
  } catch(e){
    listEl.innerHTML = `<div style="padding:10px;font-size:12px;color:#f87171">⚠ ${esc(String(e))}</div>`;
  }
}

async function subFbPickFile(fullPath, name){
  const stEl = document.getElementById('sub-local-status');
  stEl.textContent = 'Loading ' + name + '…';
  await _subLoadFromServerPath(fullPath, stEl);
}

async function _subLoadFromServerPath(path, stEl){
  try{
    const r = await fetch('/api/load_subtitle_path',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path}),
    });
    const d = await r.json();
    if(d.error){ stEl.textContent = '⚠ '+d.error; toast(d.error,'err'); return; }
    _subApplyToPlayer(d.content, d.file_name, d.mime);
    _subActiveFile = {name: d.file_name};
    document.getElementById('sub-active-name').textContent = d.file_name;
    document.getElementById('sub-active-info').style.display = 'flex';
    const subBtn = document.getElementById('subbtn');
    if(subBtn) subBtn.style.opacity = '1';
    stEl.style.color = 'var(--green)';
    stEl.textContent = '✓ Loaded: ' + d.file_name;
    document.getElementById('sub-status-msg').textContent = 'Ready';
    toast('Subtitle loaded','ok');
  } catch(e){
    stEl.textContent = '⚠ Error: '+e;
    toast('Failed to load subtitle','err');
  }
}

// ── BROWSER FILE INPUT (desktop fallback / direct pick) ────
function subLoadLocalFile(input){
  const file = input.files && input.files[0];
  if(!file){ return; }
  const fnEl = document.getElementById('sub-local-filename');
  const stEl = document.getElementById('sub-local-status');
  fnEl.textContent = file.name;
  stEl.textContent = 'Reading file…';
  const reader = new FileReader();
  reader.onload = function(e){
    const content = e.target.result;
    if(!content){ stEl.textContent = '⚠ File appears empty.'; return; }
    const lower = file.name.toLowerCase();
    let mime = 'text/vtt';
    if(lower.endsWith('.srt')) mime = 'text/srt';
    else if(lower.endsWith('.ass') || lower.endsWith('.ssa')) mime = 'text/x-ssa';
    _subApplyToPlayer(content, file.name, mime);
    _subActiveFile = {name: file.name};
    document.getElementById('sub-active-name').textContent = file.name;
    document.getElementById('sub-active-info').style.display = 'flex';
    const subBtn = document.getElementById('subbtn');
    if(subBtn) subBtn.style.opacity = '1';
    stEl.style.color = 'var(--green)';
    stEl.textContent = '✓ Loaded: ' + file.name;
    document.getElementById('sub-status-msg').textContent = 'Ready';
    toast('Subtitle loaded','ok');
  };
  reader.onerror = function(){ stEl.textContent = '⚠ Failed to read file.'; toast('Failed to read subtitle file','err'); };
  reader.readAsText(file, 'utf-8');
}


function _subInitLangGrid(){
  const grid = document.getElementById('sub-lang-grid');
  if(!grid || grid.children.length) return;
  // Default checked: English + Serbian
  const defaults = new Set(['en','sr']);
  grid.innerHTML = SUB_LANGS.map(l => `
    <label class="sub-lang-chip">
      <input type="checkbox" value="${l.code}" ${defaults.has(l.code)?'checked':''}>
      ${l.label}
    </label>`).join('');
}

function openSubSearch(){
  _subInitLangGrid();
  // Auto-fill title from currently playing stream
  const q = document.getElementById('sub-query');
  if(pName && !q.value){
    // Clean up: strip S01E01 / season-episode patterns, quality tags, etc.
    let cleaned = pName
      .replace(/\bS\d{1,2}E\d{1,2}\b/gi,'')
      .replace(/\b(720p|1080p|4k|hevc|h264|h265|hd|sd|fhd|uhd|bluray|webrip|web-dl|xvid|x264|x265)\b/gi,'')
      .replace(/[._\-\[\]()]+/g,' ')
      .replace(/\s{2,}/g,' ').trim();
    q.value = cleaned;
    // Auto-detect series pattern and pre-select Series radio
    const epMatch = pName.match(/[Ss](\d{1,2})[Ee](\d{1,2})/);
    if(epMatch){
      document.getElementById('sub-type-series').checked = true;
      document.getElementById('sub-season').value  = epMatch[1];
      document.getElementById('sub-episode').value = epMatch[2];
      subToggleEp();
    }
  }
  // Show active subtitle badge
  const info = document.getElementById('sub-active-info');
  if(_subActiveFile){
    document.getElementById('sub-active-name').textContent = _subActiveFile.name;
    info.style.display = 'flex';
  } else {
    info.style.display = 'none';
  }
  document.getElementById('sub-overlay').classList.add('open');
  setTimeout(()=>document.getElementById('sub-query').focus(), 150);
}

function closeSubSearch(){
  document.getElementById('sub-overlay').classList.remove('open');
}

function subToggleEp(){
  const isSeries = document.getElementById('sub-type-series').checked;
  document.getElementById('sub-ep-row').style.display = isSeries ? 'flex' : 'none';
}

function subSeasonChange(){
  // When user types a season, auto-focus episode field
  const s = document.getElementById('sub-season').value;
  if(s) document.getElementById('sub-episode').focus();
}

function _subGetLangs(){
  const checks = document.querySelectorAll('#sub-lang-grid input[type=checkbox]:checked');
  const codes = Array.from(checks).map(c=>c.value);
  return codes.length ? codes.join(',') : 'en';
}

async function subSearch(){
  const query = document.getElementById('sub-query').value.trim();
  if(!query){ toast('Enter a title to search','err'); return; }

  const isSeries = document.getElementById('sub-type-series').checked;
  const season   = isSeries ? (document.getElementById('sub-season').value||null) : null;
  const episode  = isSeries ? (document.getElementById('sub-episode').value||null) : null;
  const lang     = _subGetLangs();
  const maxR     = document.getElementById('sub-maxresults').value;

  const btn  = document.getElementById('sub-search-btn');
  const wrap = document.getElementById('sub-results-wrap');
  const msg  = document.getElementById('sub-status-msg');

  btn.disabled = true;
  btn.textContent = '⏳ Searching…';
  msg.textContent = 'Searching OpenSubtitles…';
  wrap.innerHTML = '<div class="sub-empty"><span class="spin" style="font-size:28px;display:block;margin-bottom:12px"></span>Searching…</div>';

  try{
    const r = await fetch('/api/subtitles/search',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({query, lang, season, episode, type: isSeries ? 'episode' : 'movie', max_results: parseInt(maxR), api_key: _getSubKey()}),
    });
    const d = await r.json();
    if(d.error){ toast('Search error: '+d.error,'err'); wrap.innerHTML=_subEmpty('Search failed: '+esc(d.error)); return; }
    if(!d.results || !d.results.length){
      wrap.innerHTML = _subEmpty('No subtitles found. Try a different title or language.');
      msg.textContent = 'No results.';
      return;
    }
    msg.textContent = d.count + ' result(s) found';
    _subRenderResults(d.results);
  } catch(e){
    wrap.innerHTML = _subEmpty('Network error: '+esc(String(e)));
    msg.textContent = 'Error.';
  } finally {
    btn.disabled = false;
    btn.textContent = '🔍 Search';
  }
}

function _subEmpty(msg){
  return `<div class="sub-empty"><span>&#x1F4AC;</span>${msg}</div>`;
}

function _subRenderResults(results){
  const wrap = document.getElementById('sub-results-wrap');
  const parts = results.map((item, i) => {
    const epStr = (item.season && item.episode)
      ? ` <span class="sub-meta-badge sub-meta-ep">S${String(item.season).padStart(2,'0')}E${String(item.episode).padStart(2,'0')}</span>`
      : '';
    const yearStr = item.year ? ` (${item.year})` : '';
    return `<div class="sub-result-item">
      <div class="sub-result-info">
        <div class="sub-result-title">${esc(item.title)}${yearStr}</div>
        <div class="sub-result-meta">
          <span class="sub-meta-badge sub-meta-lang">${esc(item.lang)}</span>
          ${epStr}
          <span class="sub-meta-badge sub-meta-dl">&#x2B07; ${item.downloads}</span>
          <span class="sub-meta-badge sub-meta-rat">&#x2605; ${item.rating}</span>
        </div>
        <div class="sub-result-release">${esc(item.file_name || '')} &bull; ${esc(item.uploader)}</div>
      </div>
      <button class="btn-ghost sub-load-btn" id="sub-load-${i}"
        onclick="subLoadSubtitle(${item.file_id}, '${esc(item.file_name||'subtitle')}', ${i})"
        title="Load into player">&#x25B6; Load</button>
    </div>`;
  });
  wrap.innerHTML = `<div class="sub-results">${parts.join('')}</div>`;
}

async function subLoadSubtitle(fileId, fileName, btnIdx){
  const btn = document.getElementById('sub-load-'+btnIdx);
  const msg = document.getElementById('sub-status-msg');
  if(btn){ btn.disabled=true; btn.textContent='⏳…'; }
  msg.textContent = 'Downloading subtitle…';

  try{
    const r = await fetch('/api/subtitles/download',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({file_id: fileId, api_key: _getSubKey()}),
    });
    const d = await r.json();
    if(d.error){ toast('Download failed: '+d.error,'err'); if(btn){btn.disabled=false;btn.textContent='▶ Load';} return; }

    _subApplyToPlayer(d.content, d.file_name || fileName, d.mime || 'text/srt');
    _subActiveFile = {name: d.file_name || fileName};

    // Update active badge
    document.getElementById('sub-active-name').textContent = _subActiveFile.name;
    document.getElementById('sub-active-info').style.display = 'flex';
    // Mark button as loaded
    if(btn){ btn.textContent='✓ Loaded'; btn.classList.add('loaded'); }
    msg.textContent = 'Loaded: ' + (d.file_name||fileName) + (d.remaining!==undefined ? ' | Quota left: '+d.remaining : '');
    // Update player subtitle button to show active
    const subBtn = document.getElementById('subbtn');
    if(subBtn) subBtn.style.opacity='1';
    toast('Subtitle loaded','ok');
  } catch(e){
    toast('Error: '+e,'err');
    if(btn){ btn.disabled=false; btn.textContent='▶ Load'; }
  }
}

// ── FAVOURITES ─────────────────────────────────────────────
// Stored per portal + mode: localStorage['favs_live_hardcoremedia.xyz'] = [{...item}]
let _favsFilterActive = false;
let _favsPortalKey = '_';   // set at connect time, never read from DOM mid-session

function _favsKey(m){
  return 'favs_'+(m||mode)+'_'+_favsPortalKey;
}
function loadFavs(m){ try{return JSON.parse(localStorage.getItem(_favsKey(m))||'[]');}catch(e){return[];} }
function saveFavs(arr,m){ try{localStorage.setItem(_favsKey(m),JSON.stringify(arr));}catch(e){} }
function isFav(item){
  const name=item.name||item.o_name||item.fname||'';
  return loadFavs(mode).some(f=>(f.name||f.o_name||f.fname||'')===name);
}
function toggleFav(i){
  const it=filtItems[i]; if(!it) return;
  const name=it.name||it.o_name||it.fname||'';
  let arr=loadFavs(mode);
  const idx=arr.findIndex(f=>(f.name||f.o_name||f.fname||'')===name);
  if(idx>=0){ arr.splice(idx,1); toast('Removed from favourites','info'); }
  else {
    arr.push({...it});
    toast('⭐ Added to favourites','ok');
  }
  saveFavs(arr,mode);
  // If filter is active, re-apply it so removed items disappear immediately
  if(_favsFilterActive) _applyFavsFilter();
  else renderItems(filtItems);
}

// ── HEADER ─────────────────────────────────────────────────
function toggleSaveChk(btn){
  btn._on=!btn._on;
  btn.style.background=btn._on?'var(--acc)':'var(--s3)';
  btn.style.color=btn._on?'#fff':'var(--txt2)';
  btn.style.borderColor=btn._on?'var(--acc)':'var(--bdr2)';
  btn.textContent=btn._on?'💾 Save ✓':'💾 Save';
}

function toggleCP(){
  cpOpen=!cpOpen;
  document.getElementById('cpanel').classList.toggle('open',cpOpen);
}
function closeCP(){
  cpOpen=false;
  document.getElementById('cpanel').classList.remove('open');
}

function setCT(t){
  CT=t;
  if(t !== 'm3u_url' && _m3uLocalContent){
    _m3uLocalContent = '';
    _m3uLocalName    = '';
    document.getElementById('m3u-fp-fname').textContent    = 'No file chosen';
    document.getElementById('m3u-fp-fname').style.color    = 'var(--txt2)';
    document.getElementById('m3u-clear-btn').style.display = 'none';
    document.getElementById('m3u-fp-status').textContent   = '';
    document.getElementById('m3u-fp-mobile').style.display = 'none';
  }
  document.querySelectorAll('.ct-btn').forEach(b=>
    b.className = b.dataset.t===t?'btn-acc ct-btn':'btn-ghost ct-btn');
  ['cr-mac','cr-xtream','cr-m3u'].forEach(id=>
    document.getElementById(id).classList.add('hidden'));
  document.getElementById({mac:'cr-mac',xtream:'cr-xtream',m3u_url:'cr-m3u'}[t])
    .classList.remove('hidden');
}

// ── CONNECT ────────────────────────────────────────────────
async function doConnect(){
  const xurl = document.getElementById('i-xu')?.value.trim()||'';
  const url = CT==='xtream' ? xurl : document.getElementById('i-url').value.trim();
  const payload={
    conn_type:CT, url,
    mac:document.getElementById('i-mac').value.trim(),
    username:document.getElementById('i-us').value.trim(),
    password:document.getElementById('i-pw').value.trim(),
    m3u_url:document.getElementById('i-m3u').value.trim(),
    m3u_content: CT==='m3u_url' && !document.getElementById('i-m3u').value.trim() ? (_m3uLocalContent||'') : '',
    ext_epg_url:(CT==='xtream'
      ? document.getElementById('i-epg').value.trim()
      : CT==='mac'
        ? document.getElementById('i-mac-epg').value.trim()
        : document.getElementById('i-m3u-epg').value.trim()),
  };
  const saveBtn = document.getElementById('save-profile-chk');
  const saveToProfile = saveBtn._on || false;
  setBusy(true); setStatus('Connecting…'); closeCP();
  try{
    const r=await fetch('/api/connect',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();
    if(d.success){
      document.getElementById('cdot').classList.add('on');
      setStatus('Connected: '+d.ident+(d.exp&&d.exp!=='unknown'?' · exp '+d.exp:''));
      isStalker = !!d.is_stalker;
      const _rawUrl = payload.m3u_url || payload.url || '';
      const _portalHost = _rawUrl ? (()=>{try{return new URL(_rawUrl).hostname;}catch(e){return _rawUrl.replace(/https?:\/\//,'').split('/')[0].split(':')[0];}})() : '';
      document.getElementById('portal-name-label').textContent = _portalHost || '—';
      _favsPortalKey = (_portalHost || '—').trim();
      // Populate multiview portal max-connection registry
      // so the badge can show e.g. "myportal.tv  ·  2/4 connections"
      if(d.max_connections && d.max_connections > 0 && (d.portal_url || _rawUrl)){
        try {
          const _pu = new URL(d.portal_url || _rawUrl);
          const _pKey = _pu.hostname + (_pu.port ? ':'+_pu.port : '');
          window._mvPortalMaxConns[_pKey] = d.max_connections;
        } catch(e){}
      }
      catsCache=d.categories||{};
      categoryItemsCache = {}; 
      // Always land on Live categories after any connect
      mode='live';
      switchMode('live', catsCache['live']||[]);
      document.getElementById('main').classList.remove('items-open');
      showT('p-cats','t-cats');
      toast('✓ Connected!','ok');
      // Save to profiles if toggle was active — skip if no portal URL (local file connect)
      const canSave = !!(payload.url || payload.m3u_url);
      if(saveToProfile && canSave){
        const arr=plLoadAll();
        // Use hostname (same as portal-name-label) as auto-generated name
        const autoName = _portalHost
          || (payload.url||payload.m3u_url||'').replace(/https?:\/\//,'').split('/')[0].split(':')[0]
          || 'Profile '+arr.length;
        const entry={
          id: Date.now().toString(36),
          name: autoName || 'Profile '+arr.length,
          type: payload.conn_type,
          url: payload.url,
          mac: payload.mac,
          url_xtream: payload.url,
          username: payload.username,
          password: payload.password,
          m3u_url: payload.m3u_url,
          ext_epg_url: payload.ext_epg_url||'',
        };
        arr.push(entry);
        plSaveAll(arr);
        renderPLList();
        toast('✓ Connected & saved to profiles!','ok');
        // Reset save button
        saveBtn._on = true; // toggleSaveChk will flip it to false
        toggleSaveChk(saveBtn);
      } else if(saveToProfile && !canSave){
        toast('Local file — nothing to save to profiles','wrn');
        saveBtn._on = true;
        toggleSaveChk(saveBtn);
      }
    } else {
      document.getElementById('cdot').classList.remove('on');
      setStatus('Error: '+(d.error||'Unknown'));
      document.getElementById('portal-name-label').textContent = '—';
      toast(d.error||'Connection failed','err');
      alog('❌ '+(d.error||''),'e');
      toggleCP(); // re-open so user can fix credentials
    }
  }catch(e){setStatus('Error: '+e.message);toast(e.message,'err');document.getElementById('portal-name-label').textContent='—';}
  finally{setBusy(false);}
}

// ── REFRESH PLAYLIST ────────────────────────────────────────
// Clears all client-side and server-side caches, then reconnects with the
// same credentials currently in the input fields. This is equivalent to
// pressing Connect again but also wipes the proxy image cache and logo caches
// on the server so logos are re-fetched fresh.
async function refreshPlaylist(){
  if(setBusy && typeof setBusy==='function') setBusy(true);
  const btn = document.getElementById('refresh-btn');
  if(btn){ btn.style.opacity='0.5'; btn.style.pointerEvents='none'; }
  toast('Refreshing playlist…','ok');
  try {
    // 1. Clear server-side caches (logo cache, proxy image cache, cats cache)
    await fetch('/api/clear_cache', {method:'POST'});
    // 2. Clear client-side item + category caches
    categoryItemsCache = {};
    catsCache = {};
    allItems = []; filtItems = []; curCat = null; navStack = [];
    // 3. Reconnect — re-fetches categories and rebuilds everything
    await doConnect();
  } catch(e){
    toast('Refresh failed: ' + e.message, 'err');
  } finally {
    if(btn){ btn.style.opacity=''; btn.style.pointerEvents=''; }
    if(setBusy && typeof setBusy==='function') setBusy(false);
  }
}

// ── PLAY DIRECT URL ────────────────────────────────────────
function playDirectUrl(){
  const url = (document.getElementById('play-url-inp').value||'').trim();
  if(!url){ toast('Enter a URL first','wrn'); return; }
  const name = (()=>{ try{ return new URL(url).hostname; }catch(e){ return url.slice(0,40); } })();
  doPlay(url, name, {isLive:true});
  document.getElementById('play-url-inp').value='';
}
function setMode(m){
  _favsFilterActive=false;
  document.querySelector('.mt[data-m="favs"]').classList.remove('on');
  mode=m; navStack=[]; allItems=[]; filtItems=[]; curCat=null;
  selSet.clear(); selCats.clear(); refreshCatBtns();
  if(_epgGridActive) _closeEpgGrid();
  document.getElementById('epg-grid-btn').style.display='none';
  switchMode(m, catsCache[m]||[]);
  document.getElementById('main').classList.remove('items-open');
  showT('p-cats','t-cats');
}

function toggleFavsFilter(){
  // Only works when a real mode is active and portal is connected
  if(!['live','vod','series'].includes(mode)) return;
  _favsFilterActive=!_favsFilterActive;
  document.querySelector('.mt[data-m="favs"]').classList.toggle('on',_favsFilterActive);
  if(_favsFilterActive){
    _applyFavsFilter();
    document.getElementById('main').classList.add('items-open');
    showT('p-items','t-items');
  } else {
    // Restore: if we have items loaded keep them, otherwise go back to cats
    if(allItems.length){
      filtItems=[...allItems];
      document.getElementById('isrch').value='';
      mkBcrum(curCat?curCat.title:'Browse');
      renderItems(filtItems);
    } else {
      document.getElementById('main').classList.remove('items-open');
      showT('p-cats','t-cats');
    }
  }
}

function _applyFavsFilter(){
  const favs=loadFavs(mode);
  const names=new Set(favs.map(f=>f.name||f.o_name||f.fname||''));
  filtItems=allItems.filter(it=>names.has(it.name||it.o_name||it.fname||''));
  // If allItems is empty (no category browsed yet), show all saved favs for this mode
  if(!allItems.length) filtItems=[...favs];
  document.getElementById('isrch').value='';
  const mLabel={live:'Live',vod:'VOD',series:'Series'}[mode]||mode;
  mkBcrum('⭐ '+mLabel+' Favourites');
  document.getElementById('icount').textContent=filtItems.length+' item'+(filtItems.length!==1?'s':'');
  if(!filtItems.length){
    document.getElementById('ilist').innerHTML=
      '<div style="text-align:center;padding:28px;color:var(--txt3);font-size:12px">No '+mLabel.toLowerCase()+' favourites yet.<br>Tap ★ on any item to add it.</div>';
    refreshBtns(); return;
  }
  renderItems(filtItems);
  refreshBtns();
  const b=document.getElementById('badge');
  const total=loadFavs(mode).length;
  b.textContent=total>99?'99+':total; b.classList.toggle('vis',total>0);
}

function switchMode(m, cats){
  mode=m;
  document.querySelectorAll('.mt').forEach(b=>b.classList.toggle('on',b.dataset.m===m));
  allCats=cats; _activeTag=''; _buildTagBar(cats); filterCats();
  document.getElementById('catlist').scrollTop=0;
}

function filterCats(){
  const q=document.getElementById('csrch').value.toLowerCase();
  const tag=_activeTag;
  let cats=allCats;
  if(tag) cats=cats.filter(c=>_catTag(c.title)===tag);
  if(q)   cats=cats.filter(c=>c.title.toLowerCase().includes(q));
  renderCats(cats);
}

// ── TAG BAR ────────────────────────────────────────────────────
let _activeTag='';

// Known tag prefixes recognised when a category name has NO separator.
// Only tags in this set are extracted from bare-prefix names like "US Sports".
// Tags with an explicit separator (US | ..., SPORTS - ...) are always extracted
// regardless of this list. Add entries here if a portal uses an unlisted prefix.
const _KNOWN_TAG_PREFIXES = new Set([
  // ── ISO 3166-1 alpha-2 country codes ────────────────────────────────────
  'AF','AL','DZ','AD','AO','AG','AR','AM','AU','AT','AZ',
  'BS','BH','BD','BB','BY','BE','BZ','BJ','BT','BO','BA','BW','BR','BN','BG','BF','BI',
  'CV','KH','CM','CA','CF','TD','CL','CN','CO','KM','CG','CD','CR','HR','CU','CY','CZ',
  'DK','DJ','DM','DO',
  'EC','EG','SV','GQ','ER','EE','SZ','ET',
  'FJ','FI','FR',
  'GA','GM','GE','DE','GH','GR','GD','GT','GN','GW','GY',
  'HK','HT','HN','HU',                        // HK = Hong Kong
  'IS','IN','ID','IR','IQ','IE','IL','IT',
  'JM','JP','JO',
  'KZ','KE','KI','KP','KR','KW','KG',
  'LA','LV','LB','LS','LR','LY','LI','LT','LU',
  'MG','MW','MY','MV','ML','MT','MH','MR','MU','MX','FM','MD','MC','MN','ME','MK','MA','MZ','MO',
  'MM','NA','NR','NP','NL','NZ','NI','NE','NG','NO',
  'OM',
  'PK','PW','PS','PA','PG','PY','PE','PH','PL','PT',
  'QA',
  'RO','RU','RW',
  'KN','LC','VC','WS','SM','ST','SA','SN','RS','SC','SL','SG','SK','SI','SB','SO','ZA',
  'SS','ES','LK','SD','SR','SE','CH','SY',
  'TW','TJ','TZ','TH','TL','TG','TO','TT','TN','TR','TM','TV',
  'UG','UA','AE','GB','UK','US','UY','UZ',
  'VI','VU','VE','VN',                         // VI = Virgin Islands
  'YE',
  'ZM','ZW',
  // ── Regional blocs & groupings ───────────────────────────────────────────
  'EU','XK',                                   // Kosovo, European Union
  'EXYU','EXUSSR',                             // Former Yugoslavia / Soviet bloc
  'ASIA',                                      // Asia regional
  'AFR',                                       // Africa regional
  'ARAB','MENA',                               // Arab world / Middle East & North Africa
  'LATAM','LAT',                               // Latin America
  'SCAN','SCA',                                // Scandinavia
  'BALK',                                      // Balkans regional
  'CIS',                                       // Commonwealth of Independent States
  // ── Kurdistan ────────────────────────────────────────────────────────────
  'KU','KURD',                                 // Kurdish channels (very common in IPTV)
  // ── 3–5 letter country/language abbreviations used by IPTV providers ─────
  'USA','GBR','GER','FRA','ITA','ESP','POR','TUR','ARA','RUS',
  'NED','BEL','SUI','AUS','MEX','BRA','ARG','POL','CZE','SVK',
  'HUN','SWE','NOR','DEN','FIN','GRE','PER','COL','CHI','URU',
  'IND','PAK','BAN','SRI','NEP','AFG','KAZ','UZB','AZE','GEO',
  'ARM','ALB','KOS','BOS','MNE','SRB','MKD','CRO','SLO','BUL',
  'ROM','MOL','UKR','BLR','BAL','SCO','IRL','WAL','ENG',
  'JAP','KOR','CHN','VIE','THA','MYS','IDN','PHI','HKG','TWN','MAC',
  'THAI','VIET','INDO','SING','MALAY','PAKI','IRAN','IRAQ',
  'IRN','SAU','UAE','KUW','QAT','BHR','OMN','YEM','JOR',
  'LEB','SYR','PAL','EGY','LIB','MAR','ALG','TUN',
  'NIG','GHA','KEN','ETH','SEN','CMR','CIV','ZAF','NAM','ZIM',
  'ICE','LAT','LIT','EST','CAN','MKD',
  // ── Extra regional/cultural tags seen on IPTV providers ──────────────────
  'DESI','HINDI','URDU','PANJ','PUNJ','BENG','TAMI','TELU','GUJA','MALA','KANA',
  'AMHA','SOMA','HUSA','SWAH',                 // African languages
  'PERS','FARS','PASH','DARI','KURD',          // Middle East / Central Asia
  'PORT','CAST','CATA','GALI','BASK',          // Iberian variants
  'NETH','FLEM','WALL',                        // Low Countries
]);

function _catTag(title){
  if(!title) return '';
  const t = title.trim();

  // Normalise common EX-YU / EXYU variants to a single canonical tag before matching
  const normalised = t
    .replace(/^EX[-_\s]?YU\b/i, 'EXYU')
    .replace(/^EX[-_\s]?USSR\b/i, 'EXUSSR');

  // With hard separator (|  -  :) — e.g. "US | News", "SPORTS - HD", "EXYU: Movies"
  // This is reliable because the portal explicitly structured the name this way.
  let m = normalised.match(/^([A-Z0-9]{2,12})\s*[\|\:]\s*\S/i);
  if(m) return m[1].toUpperCase();

  // Without separator — ONLY recognise the prefix if it is a known country/region tag.
  // This prevents random 2-letter channel name prefixes (RM, RX, SU, TS…) from being
  // treated as tags just because they happen to be followed by a space.
  m = normalised.match(/^([A-Z]{2,6})\s+/i);
  if(m){
    const candidate = m[1].toUpperCase();
    if(_KNOWN_TAG_PREFIXES.has(candidate)) return candidate;
  }
  return '';
}

function _buildTagBar(cats){
  const bar=document.getElementById('tag-bar');
  if(!bar) return;
  const counts={};
  cats.forEach(c=>{ const t=_catTag(c.title); if(t) counts[t]=(counts[t]||0)+1; });
  const tags=Object.keys(counts).sort();
  if(!tags.length){ bar.style.display='none'; _activeTag=''; return; }

  // Country classifier: reuse the module-level _KNOWN_TAG_PREFIXES set as the
  // single source of truth. Quality/format tags override it via NOT_COUNTRY.
  const NOT_COUNTRY = new Set(['4K','8K','UHD','FHD','HD','SD','HQ','4G','VIP','FOR','NEW','TOP','HOT','ALL']);

  function isCountryTag(t){
    if(NOT_COUNTRY.has(t)) return false;
    return _KNOWN_TAG_PREFIXES.has(t);
  }

  const countryTags = tags.filter(t => isCountryTag(t));
  const generalTags = tags.filter(t => !isCountryTag(t));

  function pill(t){
    return `<span class="tag-pill" data-tag="${t}" onclick="setTag(this,'${t}')">${t} <span style="opacity:.55;font-size:9px">${counts[t]}</span></span>`;
  }
  const allPill = '<span class="tag-pill on" data-tag="" onclick="setTag(this,\'\')">All</span>';

  let html = '';
  if(generalTags.length && countryTags.length){
    html  = `<div class="tag-row">${allPill}${generalTags.map(pill).join('')}</div>`;
    html += `<div class="tag-row">${countryTags.map(pill).join('')}</div>`;
  } else {
    // Only one type — single row
    html = `<div class="tag-row">${allPill}${tags.map(pill).join('')}</div>`;
  }

  bar.style.display='flex';
  bar.innerHTML = html;

  // Wire drag-scroll on each row (desktop only — touch handles natively)
  bar.querySelectorAll('.tag-row').forEach(row=>{
    let isDown=false, didDrag=false, startX=0, scrollLeft=0;
    row.addEventListener('mousedown', e=>{
      if(e.button !== 0) return;
      isDown=true; didDrag=false;
      startX=e.pageX - row.offsetLeft; scrollLeft=row.scrollLeft;
    });
    row.addEventListener('mousemove', e=>{
      if(!isDown) return;
      const dx = Math.abs(e.pageX - row.offsetLeft - startX);
      if(!didDrag && dx < 5) return;   // threshold — ignore tiny jitter
      didDrag=true;
      row.classList.add('dragging');
      e.preventDefault();
      const x = e.pageX - row.offsetLeft;
      row.scrollLeft = scrollLeft - (x - startX);
    });
    const stopDrag = ()=>{ isDown=false; row.classList.remove('dragging'); };
    row.addEventListener('mouseup',    stopDrag);
    row.addEventListener('mouseleave', stopDrag);
  });
}

function setTag(el, tag){
  _activeTag=tag;
  document.querySelectorAll('#tag-bar .tag-pill').forEach(p=>p.classList.toggle('on',p.dataset.tag===tag));
  filterCats();
}

// store rendered cats for index lookup
let _renderedCats=[];
function renderCats(cats){
  const el=document.getElementById('catlist');
  if(!cats.length){
    el.innerHTML='<div style="text-align:center;padding:24px;color:var(--txt3);font-size:12px">No categories found</div>';
    return;
  }
  _renderedCats=cats;
  el.innerHTML=cats.map((c,i)=>{
    const sel=selCats.has(c.id||c.title);
    // Use data-idx to avoid any JSON/quote issues inside HTML attributes
    return '<div class="citem" style="--d:'+(Math.min(i,40)*.022)+'s" data-idx="'+i+'">'
      +'<input class="cat-chk" type="checkbox"'+(sel?' checked':'')
        +' data-idx="'+i+'" onchange="onCatChkIdx('+i+',this.checked)"'
        +' onclick="event.stopPropagation()">'
      +'<span class="c-ico" style="cursor:pointer" onclick="browseIdx('+i+')">📁</span>'
      +'<span class="c-name" style="cursor:pointer" onclick="browseIdx('+i+')">'
        +esc(c.title)+'</span>'
      +'<span class="c-arr" style="cursor:pointer" onclick="browseIdx('+i+')">›</span>'
      +'</div>';
  }).join('');
}
function browseIdx(i){
  const c=_renderedCats[i]; if(!c) return;
  browseC(c);  // pass object directly — no JSON encoding needed
}
function onCatChkIdx(i, checked){
  const c=_renderedCats[i]; if(!c) return;
  const key=c.id||c.title;
  if(checked) selCats.set(key,c); else selCats.delete(key);
  refreshCatBtns();
}

// ── CATEGORY SELECTION ─────────────────────────────────────
function selAllCats(v){
  selCats.clear();
  if(v) allCats.forEach(c=>selCats.set(c.id||c.title,c));
  filterCats(); refreshCatBtns();
}
function refreshCatBtns(){
  const n=selCats.size, ff=CFG.ffmpeg_ok;
  // Drawer buttons
  const m3uBtn=document.getElementById('adr-cat-m3u');
  const mkvBtn=document.getElementById('adr-cat-mkv');
  if(m3uBtn) m3uBtn.disabled=n===0;
  if(mkvBtn) mkvBtn.disabled=n===0||!ff;
  const sub=n?n+' categor'+(n===1?'y':'ies'):'';
  const m3uSub=document.getElementById('adr-cat-m3u-sub');
  const mkvSub=document.getElementById('adr-cat-mkv-sub');
  if(m3uSub) m3uSub.textContent=sub;
  if(mkvSub) mkvSub.textContent=sub;
  const cnt=document.getElementById('adr-cat-count');
  if(cnt) cnt.textContent=n+' selected';
  // FAB badge (mobile) + desktop header badge
  const b=document.getElementById('act-tab-badge');
  if(b){b.textContent=n>99?'99+':n; b.classList.toggle('vis',n>0);}
  const pb=document.getElementById('ph-cat-badge');
  if(pb){pb.textContent=n>99?'99+':n; pb.classList.toggle('vis',n>0);}
}
async function dlSelCats(type){
  const cats=[...selCats.values()];
  if(!cats.length){toast('Select categories first','wrn');return;}
  const op=document.getElementById('o-m3u').value.trim();
  const od=document.getElementById('o-dir').value.trim();
  if(type==='m3u'&&!op){toast('Set M3U output path in ⚙ settings','wrn');return;}
  if(type==='mkv'&&!od){toast('Set output folder in ⚙ settings','wrn');return;}
  setBusy(true);
  let done=0;
  for(const cat of cats){
    setStatus('Downloading cat '+(++done)+'/'+cats.length+': '+cat.title+'…');
    const r=await fetch('/api/download/m3u',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({items:null,category:cat,mode,
        out_path:type==='m3u'?op:(od.replace(/\/?$/,'/')+mode+'_'+cat.title.replace(/[^a-z0-9]/gi,'_')+'.m3u')
      })});
    const d=await r.json();
    if(!d.ok) toast('Error on '+cat.title+': '+(d.error||'?'),'err');
  }
  toast('Done! '+done+' categories exported','ok');
  pollBusy();
}

// ── BROWSE ─────────────────────────────────────────────────
// ── BROWSE ─────────────────────────────────────────────────
function _categoryKey(m, cat){
  // normalize category identity: prefer id, then category_id, then title
  const id = (cat && (cat.id || cat.category_id || cat.title || '')).toString();
  return String(m||'') + ':' + id;
}

function browseC(cj){
  const cat=(typeof cj==='string')?JSON.parse(cj):cj; curCat=cat;
  if(_epgGridActive) _closeEpgGrid();
  _favsFilterActive=false;
  document.querySelector('.mt[data-m="favs"]').classList.remove('on');
  navStack=[]; setBusy(true);
  _setLoadingHeader(cat.title);
  setStatus("Loading '"+cat.title+"'…");
  showSkels(12); showT('p-items','t-items');

  const key = _categoryKey(mode, cat);
  // ensure container for this mode exists
  categoryItemsCache[mode] = categoryItemsCache[mode] || {};

  // Serve from cache if present
  if(categoryItemsCache[mode][key]){
    allItems = categoryItemsCache[mode][key];
    _setLoadingHeader(null);
    setStatus("'"+cat.title+"' — "+allItems.length+' items (cached)');
    showItems(cat.title, allItems);
    setBusy(false);
    return;
  }

  // Not cached -> fetch
  fetch('/api/items',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode, category:cat, browse:true})})
  .then(r=>r.json()).then(d=>{
    _setLoadingHeader(null);
    if(d.error){ toast(d.error,'err'); setStatus('Error: '+d.error); return; }
    allItems = d.items || [];
    // store into cache
    categoryItemsCache[mode][key] = allItems;
    setStatus("'"+cat.title+"' — "+allItems.length+' items');
    showItems(cat.title, allItems);
  }).catch(e=>{
    _setLoadingHeader(null);
    toast(e.message,'err');
  }).finally(()=> setBusy(false));
}

function showSkels(count=10, small=false){
  document.getElementById('main').classList.add('items-open');
  const cls=small?'skel-sm':'skel';
  document.getElementById('ilist').innerHTML=
    `<div style="padding:4px 0">`+Array(count).fill(`<div class="${cls}" style="--d:${0}s"></div>`).map((s,i)=>
      `<div class="${cls}" style="animation-delay:${i*0.04}s"></div>`).join('')+`</div>`;
}

function _setLoadingHeader(text){
  const el=document.getElementById('ittitle');
  if(!text){el.innerHTML='Browse';return;}
  el.innerHTML=`<span style="display:flex;align-items:center;gap:6px">`
    +`<span style="width:12px;height:12px;border-radius:50%;border:2px solid var(--acc);`
    +`border-top-color:transparent;animation:spin .7s linear infinite;flex-shrink:0"></span>`
    +esc(text)+`</span>`;
}

function showItems(label, items){
  document.getElementById('main').classList.add('items-open');
  allItems=items; filtItems=[...items]; selSet.clear();
  document.getElementById('ilist').scrollTop=0;
  document.getElementById('isrch').value='';
  document.getElementById('backbtn').disabled=false; // always can go back to categories

  mkBcrum(label); renderItems(filtItems); refreshBtns();
  const n=loadFavs(mode).length;
  const b=document.getElementById('badge');
  b.textContent=n>99?'99+':n; b.classList.toggle('vis',n>0);
}

function mkBcrum(label){
  const el=document.getElementById('bcrum');
  const parts=navStack.length
    ?['Categories', curCat?.title, label].filter(Boolean)
    :['Categories', label].filter(Boolean);
  el.innerHTML=parts.map((p,i)=>{
    const last=i===parts.length-1;
    return (i?'<span class="bc-x">›</span>':'')
      +'<span class="'+(last?'bc-c':'bc-s')+'">'+esc(p)+'</span>';
  }).join('');
}

const _ITEMS_BATCH = 75;
let _renderToken = 0;

function renderItems(items){
  const el=document.getElementById('ilist');
  document.getElementById('icount').textContent=items.length+' item'+(items.length!==1?'s':'');
  if(!items.length){
    el.innerHTML='<div style="text-align:center;padding:20px;color:var(--txt3);font-size:12px">No items found</div>';
    refreshBtns(); return;
  }
  const token = ++_renderToken;
  const isSeries=mode==='series'||mode==='vod';

  function buildRow(it, i){
    const name=it.name||it.o_name||it.fname||'Unknown';
    const grp=!!it._is_series_group;
    const epN=grp?(it._episodes||[]).length:0;
    const show=!!it._is_show_item;
    const playing=i===pIdx;
    const playable=!grp&&!show;
    const eps=grp?(it._episodes||[]):[];
    const ep0=eps.length?eps[0]:{};
    const epLogo=grp&&!it.logo&&!it.stream_icon&&!it.cover
      ?(ep0.logo||ep0.stream_icon||ep0.cover||ep0.screenshot_uri||ep0.pic||''):'';
    const logo=it.logo||it.stream_icon||it.cover||it.screenshot_uri||it.pic||epLogo||'';
    const logoSrc = logo && (logo.startsWith('http://') || logo.startsWith('https://'))
      ? '/api/proxy?url='+encodeURIComponent(logo) : logo;
    return '<div class="irow'+(playing?' now':'')+'" style="--d:'+(Math.min(i,20)*.016)+'s">'
      +'<input class="ichk" type="checkbox" data-i="'+i+'" onchange="onChk('+i+',this.checked)">'
      +(logoSrc?'<img class="ilogo" loading="lazy" src="'+esc(logoSrc)+'" onerror="this.style.display=\'none\'">'+'':'<span style="width:36px;height:24px;flex-shrink:0;display:inline-block"></span>')
      +'<button onclick="toggleFav('+i+')" title="Favourite"'
      +' style="background:none;border:none;cursor:pointer;font-size:15px;padding:0 2px;line-height:1;flex-shrink:0;color:'+(isFav(it)?'#f5c518':'rgba(255,255,255,0.25)')+'" >★</button>'
      +'<span class="iname"><span class="iname-inner">'+esc(name)+'</span></span>'
      +'<div class="ibtns">'
        +(grp?'<button class="btn-ghost" onclick="drillGrp('+i+')">'+epN+' eps</button>':'')
        +(show&&isSeries?'<button class="btn-ghost" onclick="drillShow('+i+')">Eps</button>':'')
        +(playable?'<button class="btn-blue" onclick="playItem('+i+')">▶</button>':'')
        +'<button class="btn-ghost imenu-trigger" onclick="event.stopPropagation();openItemMenu('+i+',this)" title="More options" style="padding:0 6px;font-size:18px;line-height:1;letter-spacing:0">⋮</button>'
      +'</div></div>';
  }

  el.innerHTML = items.slice(0, _ITEMS_BATCH).map(buildRow).join('');
  refreshBtns();
  _updateEpgGridBtn();

  if(items.length <= _ITEMS_BATCH) return;

  let offset = _ITEMS_BATCH;
  function appendBatch(){
    if(_renderToken !== token) return;
    if(offset >= items.length) return;
    const end = Math.min(offset + _ITEMS_BATCH, items.length);
    const tmp = document.createElement('div');
    tmp.innerHTML = items.slice(offset, end).map((it,j) => buildRow(it, offset+j)).join('');
    while(tmp.firstChild) el.appendChild(tmp.firstChild);
    offset = end;
    if(offset < items.length) requestAnimationFrame(appendBatch);
  }
  requestAnimationFrame(appendBatch);
}


// ── ITEM CONTEXT MENU ─────────────────────────────────────
let _iMenuIdx = -1;

function openItemMenu(i, btn){
  _iMenuIdx = i;
  const it = filtItems[i];
  if(!it) return;
  const isLive = mode==='live';
  const grp  = !!it._is_series_group;
  const show = !!it._is_show_item;
  const name = it.name||it.o_name||it.fname||'Unknown';

  // Header
  document.getElementById('item-menu-hdr').textContent = name;

  // Show/hide buttons based on context
  document.getElementById('imenu-sep1').style.display     = isLive&&!grp?'block':'none';
  document.getElementById('imenu-epg').style.display      = isLive&&!grp?'flex':'none';
  document.getElementById('imenu-catchup').style.display  = isLive&&!grp?'flex':'none';
  document.getElementById('imenu-sep2').style.display     = !grp?'block':'none';
  document.getElementById('imenu-ext').style.display      = !grp&&!show?'flex':'none';
  document.getElementById('imenu-imdb').style.display     = (!isLive&&!grp)?'flex':'none';
  document.getElementById('imenu-rec').style.display      = !grp&&!show?'flex':'none';
  document.getElementById('imenu-mkv').style.display      = !grp?'flex':'none';

  // Position menu near button
  const menu = document.getElementById('item-menu');
  menu.classList.add('open');
  const r = btn.getBoundingClientRect();
  const mw = 210, mh = menu.offsetHeight||200;
  let left = r.right - mw;
  let top  = r.bottom + 4;
  if(left < 8) left = 8;
  if(top + mh > window.innerHeight - 8) top = r.top - mh - 4;
  menu.style.left = left + 'px';
  menu.style.top  = top  + 'px';
  document.getElementById('item-menu-bg').style.display = 'block';
  _refreshDlButtons();
}

function closeItemMenu(){
  document.getElementById('item-menu').classList.remove('open');
  document.getElementById('item-menu-bg').style.display = 'none';
}

function iMenuExternal(){
  closeItemMenu();
  openExternal(_iMenuIdx);
}

function iMenuEPG(){
  closeItemMenu();
  const it = filtItems[_iMenuIdx];
  if(!it) return;
  _epgItem = it;
  showEPG();
}

function iMenuCatchup(){
  closeItemMenu();
  const it = filtItems[_iMenuIdx];
  if(!it) return;
  _epgItem = it;
  showCatchup();
}

async function iMenuRec(){
  closeItemMenu();
  await playItem(_iMenuIdx);
  setTimeout(()=>{ if(!isRec) startRec(); }, 800);
}

function iMenuMKV(){
  closeItemMenu();
  const it = filtItems[_iMenuIdx];
  if(!it) return;
  // Select just this item and download
  selSet.clear();
  selSet.add(it);
  // Uncheck all, check this one
  document.querySelectorAll('.ichk').forEach((c,ci)=>{ c.checked = (ci===_iMenuIdx); });
  refreshBtns();
  dlMKV();
}

function iMenuIMDB(){
  closeItemMenu();
  const it = filtItems[_iMenuIdx];
  if(!it) return;
  const idFields = ['tmdb_id','tmdb','imdb_id','imdb','kinopoisk_id','movie_id','series_id','stream_id','id'];
  const found = {};
  idFields.forEach(k=>{ if(it[k]!==undefined && it[k]!==null && it[k]!=='') found[k]=it[k]; });
  console.log('[TMDB] item keys:', Object.keys(it));
  console.log('[TMDB] ID fields:', found);
  alog('🔍 Item ID fields: '+JSON.stringify(found), 'i');
  _iMenuIMDBOpen(it);
}

async function _iMenuIMDBOpen(it, _modeOverride){
  const _effectiveMode = _modeOverride || mode;
  const _tmdbFields = ['kinopoisk_id','external_id','movie_tmdb_id','series_tmdb_id','tmdb_id','tmdb'];
  // Priority 1: scan ALL fields for tt-prefixed IMDB ID
  let imdbId = it.imdb_id || it.imdb || '';
  if(!imdbId){
    for(const v of Object.values(it)){
      if(typeof v === 'string' && /^tt\d+$/i.test(v.trim())){ imdbId = v.trim(); break; }
    }
  }
  // Priority 2: whitelisted numeric TMDB field
  let tmdbId = '';
  for(const f of _tmdbFields){
    const v = String(it[f]||'').trim();
    if(v && /^\d+$/.test(v)){ tmdbId = v; break; }
  }
  // Priority 3: for Xtream VOD/Series, fetch info from portal to get tmdb_id
  const needFetch = !imdbId && !tmdbId && (
    (it.stream_id && _effectiveMode === 'vod') ||
    (it.series_id && _effectiveMode === 'series')
  );
  if(needFetch){
    try{
      alog('🔍 Fetching TMDB ID from portal…', 'i');
      const body = it.series_id ? {series_id: it.series_id} : {stream_id: it.stream_id};
      const r = await fetch('/api/get_tmdb_id', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      const d = await r.json();
      imdbId = d.imdb_id || '';
      tmdbId = d.tmdb_id || '';
      alog('🔍 Fetched — tmdb_id: '+(tmdbId||'none')+' imdb_id: '+(imdbId||'none'), 'i');
    } catch(e){ /* fall through to name search */ }
  }
  if(imdbId){
    window.open('https://www.imdb.com/title/'+imdbId+'/', '_blank');
  } else if(tmdbId){
    const section = _effectiveMode === 'series' ? 'tv' : 'movie';
    window.open('https://www.themoviedb.org/'+section+'/'+tmdbId, '_blank');
  } else {
    const name = it.name||it.o_name||it.fname||'Unknown';
    window.open('https://www.imdb.com/find/?q='+encodeURIComponent(name.trim())+'&s=tt', '_blank');
  }
}

// ── M3U LOCAL FILE PICKER ─────────────────────────────────
let _m3uLocalContent  = '';
let _m3uLocalName     = '';
let _m3uFbCurrentPath = '/sdcard/Download';

// Single entry point: desktop → tkinter/file-input, mobile → inline browser
function m3uOpenPicker(){
  if(_isMobile){
    const mob = document.getElementById('m3u-fp-mobile');
    const opening = mob.style.display === 'none';
    mob.style.display = opening ? '' : 'none';
    if(opening) m3uFbNav(_m3uFbCurrentPath);
  } else {
    m3uBrowseDesktop();
  }
}

function m3uForceFileBrowser(){
  const mob = document.getElementById('m3u-fp-mobile');
  mob.style.display = '';
  m3uFbNav(_m3uFbCurrentPath);
}

function m3uClearLocal(){
  _m3uLocalContent = '';
  _m3uLocalName    = '';
  document.getElementById('m3u-fp-fname').textContent    = 'No file chosen';
  document.getElementById('m3u-fp-fname').style.color    = 'var(--txt2)';
  document.getElementById('m3u-clear-btn').style.display = 'none';
  document.getElementById('m3u-fp-status').textContent   = '';
  document.getElementById('m3u-fp-mobile').style.display = 'none';
  const inp = document.getElementById('m3u-local-input');
  if(inp) inp.value = '';
}

async function m3uBrowseDesktop(){
  const stEl = document.getElementById('m3u-fp-status');
  stEl.style.color = 'var(--txt2)';
  stEl.textContent = 'Opening file picker…';
  try{
    const r = await fetch('/api/browse_m3u');
    const d = await r.json();
    if(d.error || !d.path){ stEl.textContent = d.error ? '⚠ '+d.error : 'No file selected.'; return; }
    stEl.textContent = 'Reading…';
    await _m3uLoadFromServerPath(d.path, stEl);
  } catch(e){
    // tkinter not available — fall back to browser <input type=file>
    stEl.textContent = '';
    document.getElementById('m3u-local-input').click();
  }
}

function m3uFbUp(){
  const el = document.getElementById('m3u-fb-path');
  const cur = (el && el.textContent) || _m3uFbCurrentPath;
  m3uFbNav(cur.replace(/\/[^/]+$/, '') || '/');
}

async function m3uFbNav(path){
  _m3uFbCurrentPath = path;
  const listEl = document.getElementById('m3u-fb-list');
  const pathEl = document.getElementById('m3u-fb-path');
  const upBtn  = document.getElementById('m3u-fb-up');
  if(pathEl) pathEl.textContent = path;
  listEl.innerHTML = '<div style="padding:10px;font-size:12px;color:var(--txt3)">Loading…</div>';
  try{
    const r = await fetch('/api/browse_dir_m3u',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path}),
    });
    const d = await r.json();
    if(upBtn) upBtn.disabled = !d.parent;
    if(d.error && !d.dirs.length && !d.files.length){
      listEl.innerHTML = `<div style="padding:10px;font-size:12px;color:#f87171">⚠ ${esc(d.error)}</div>`;
      return;
    }
    const rows = [];
    for(const name of d.dirs){
      const fp = path.replace(/\/+$/,'') + '/' + name;
      rows.push(`<div class="sub-fb-row sub-fb-dir" onclick="m3uFbNav('${esc(fp)}')">
        <span class="sub-fb-icon">📁</span><span class="sub-fb-name">${esc(name)}</span><span class="sub-fb-arr">›</span>
      </div>`);
    }
    for(const name of d.files){
      const fp = path.replace(/\/+$/,'') + '/' + name;
      rows.push(`<div class="sub-fb-row sub-fb-file" style="color:var(--acc)" onclick="m3uFbPickFile('${esc(fp)}','${esc(name)}')">
        <span class="sub-fb-icon">📄</span><span class="sub-fb-name">${esc(name)}</span>
      </div>`);
    }
    if(!rows.length) rows.push('<div style="padding:10px;font-size:12px;color:var(--txt3)">No M3U files here. Tap a folder to browse.</div>');
    listEl.innerHTML = rows.join('');
  } catch(e){
    listEl.innerHTML = `<div style="padding:10px;font-size:12px;color:#f87171">⚠ ${esc(String(e))}</div>`;
  }
}

async function m3uFbPickFile(fullPath, name){
  const stEl = document.getElementById('m3u-fp-status-mob');
  stEl.textContent = 'Reading ' + name + '…';
  await _m3uLoadFromServerPath(fullPath, stEl);
  document.getElementById('m3u-fp-mobile').style.display = 'none';
}

async function _m3uLoadFromServerPath(path, stEl){
  try{
    const r = await fetch('/api/read_m3u_path',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path}),
    });
    const d = await r.json();
    if(d.error){ stEl.textContent = '⚠ '+d.error; toast(d.error,'err'); return; }
    _m3uLocalContent = d.content;
    _m3uLocalName    = d.file_name;
    _m3uApplySelected(d.file_name, stEl);
  } catch(e){ stEl.textContent = '⚠ Error: '+e; toast('Failed to read M3U file','err'); }
}

function m3uLoadLocalFile(input){
  const file = input.files && input.files[0];
  if(!file) return;
  const stEl = document.getElementById('m3u-fp-status');
  stEl.textContent = 'Reading file…';
  const reader = new FileReader();
  reader.onload = function(e){
    const content = e.target.result;
    if(!content){ stEl.textContent = '⚠ File appears empty.'; return; }
    _m3uLocalContent = content;
    _m3uLocalName    = file.name;
    _m3uApplySelected(file.name, stEl);
  };
  reader.onerror = function(){ stEl.textContent = '⚠ Failed to read file.'; toast('Failed to read M3U file','err'); };
  reader.readAsText(file, 'utf-8');
}

function _m3uApplySelected(fname, stEl){
  const fnEl = document.getElementById('m3u-fp-fname');
  fnEl.textContent  = '📄 ' + fname;
  fnEl.style.color  = 'var(--green)';
  document.getElementById('m3u-clear-btn').style.display = '';
  stEl.style.color  = 'var(--green)';
  stEl.textContent  = '✓ Ready — click Connect';
  toast('M3U file loaded — click Connect', 'ok');
}

let _filterDebounceTimer = null;
function filterItems(){
  clearTimeout(_filterDebounceTimer);
  _filterDebounceTimer = setTimeout(_doFilterItems, 150);
}
function _doFilterItems(){
  const q=document.getElementById('isrch').value.toLowerCase();
  const base=_favsFilterActive
    ? loadFavs(mode).filter(f=>!allItems.length||allItems.some(it=>(it.name||it.o_name||it.fname||'')===(f.name||f.o_name||f.fname||'')))
    : allItems;
  filtItems=q?base.filter(it=>(it.name||it.o_name||it.fname||'').toLowerCase().includes(q)):[...base];
  renderItems(filtItems);
}

function onChk(i,v){
  const it=filtItems[i]; if(!it) return;
  v?selSet.add(it):selSet.delete(it); refreshBtns();
}

function selAll(v){
  document.querySelectorAll('.ichk').forEach(c=>c.checked=v);
  selSet.clear(); if(v) filtItems.forEach(it=>selSet.add(it)); refreshBtns();
}

function refreshBtns(){
  const n=selSet.size, ff=CFG.ffmpeg_ok;
  // Drawer buttons
  const m3uBtn=document.getElementById('adr-dlm3u');
  const mkvBtn=document.getElementById('adr-dlmkv');
  if(m3uBtn) m3uBtn.disabled=n===0;
  if(mkvBtn){mkvBtn.disabled=n===0||!ff; if(!ff) mkvBtn.title='ffmpeg not found';}
  const sub=n?n+' item'+(n===1?'':'s'):'';
  const m3uSub=document.getElementById('adr-m3u-sub');
  const mkvSub=document.getElementById('adr-mkv-sub');
  if(m3uSub) m3uSub.textContent=sub;
  if(mkvSub) mkvSub.textContent=sub;
  const cnt=document.getElementById('adr-item-count');
  if(cnt) cnt.textContent=n+' selected';
  // Show current category name on whole-cat button
  const catSub=document.getElementById('adr-cat-all-sub');
  if(catSub) catSub.textContent=curCat?curCat.title:'';
  // FAB badge (mobile) + desktop header badge
  const b=document.getElementById('act-tab-badge');
  if(b){b.textContent=n>99?'99+':n; b.classList.toggle('vis',n>0);}

  const pb=document.getElementById('ph-item-badge');
  if(pb){pb.textContent=n>99?'99+':n; pb.classList.toggle('vis',n>0);}
}

// ── SERIES DRILL ───────────────────────────────────────────
function drillGrp(i){
  const it=filtItems[i]; if(!it) return;
  navStack.push({label:'Browse',items:[...allItems]});
  showItems(it.name||'Episodes', it._episodes||[]);
  document.getElementById('backbtn').disabled=false;
}

function drillShow(i){
  const it=filtItems[i]; if(!it) return;
  // Capture the parent show's logo — episodes rarely have their own thumbnail.
  // Also fall back to the current category logo (curCat.logo / curCat.screenshot_uri)
  // if the show item itself carries no image, so there is always something to show.
  const parentLogo = it.logo||it.stream_icon||it.cover||it.screenshot_uri||it.pic
    ||curCat?.logo||curCat?.screenshot_uri||curCat?.pic||'';
  setBusy(true);
  _setLoadingHeader(it.name);
  setStatus("Loading eps for '"+it.name+"'…");
  showSkels(8, true);
  fetch('/api/episodes',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({item:it, mode, cat_id:curCat?.id||'', cat_title:curCat?.title||'',
      parent_logo:parentLogo})})
  .then(r=>r.json()).then(d=>{
    _setLoadingHeader(null);
    if(d.error||!d.episodes?.length){toast('No episodes found','warn');showItems(it.name||'',allItems);return;}
    // Propagate parent logo to any episode that has no logo of its own.
    // Stalker portals rarely provide per-episode thumbnails; using the show's
    // poster is far better than showing blank squares.
    if(parentLogo){
      d.episodes.forEach(ep=>{
        if(!ep.logo&&!ep.stream_icon&&!ep.cover&&!ep.screenshot_uri&&!ep.pic)
          ep.logo=parentLogo;
      });
    }
    navStack.push({label:'Browse',items:[...allItems]});
    setStatus(it.name+' — '+d.episodes.length+' episodes');
    showItems(it.name, d.episodes);
    document.getElementById('backbtn').disabled=false;
  }).catch(e=>{_setLoadingHeader(null);toast(e.message,'err');}).finally(()=>setBusy(false));
}

function goBack(){
  if(!navStack.length){
    // No nav stack — go back to categories panel
    _favsFilterActive=false;
    document.querySelector('.mt[data-m="favs"]').classList.remove('on');
    document.getElementById('main').classList.remove('items-open');
    showT('p-cats','t-cats');
    return;
  }
  const prev=navStack.pop();
  allItems=prev.items; filtItems=[...allItems]; selSet.clear();
  document.getElementById('isrch').value='';
  // Still show back btn if stack has more; if empty, still allow back to cats
  document.getElementById('backbtn').disabled=false;
  mkBcrum('Browse'); renderItems(allItems); refreshBtns();
}

// ── PLAY ───────────────────────────────────────────────────
async function playItem(i){
  const it=filtItems[i]; if(!it) return;
  pIdx=i;
  // When playing from favs, use the mode the item was originally saved under
  const itemMode = mode;
  // Store item for EPG lookup (live channels only)
  _epgItem = (itemMode==='live') ? it : null;
  document.getElementById('epg-now').textContent='';
  document.getElementById('epgbtn').style.opacity=(itemMode==='live')?'1':'0.35';
  document.getElementById('catchupbtn').style.opacity=(itemMode==='live')?'1':'0.35';
  const name=it.name||it.o_name||it.fname||'Unknown';
  const direct=it._direct_url||it._url;
  if(direct){doPlay(direct,name,{isLive:itemMode==='live'});return;}
  setNP('⟳ Resolving: '+name+'…');
  forceTab('p-player','t-player');
  try{
    const r=await fetch('/api/resolve',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({item:it, mode:itemMode, category:curCat||{}})});
    const d=await r.json();
    if(d.url) doPlay(d.url, name, {isLive: itemMode==='live'});
    else{setNP('✗ Could not resolve: '+name);toast('Could not resolve URL','err');}
  }catch(e){setNP('✗ '+e.message);}
}

let _playerStopped = false;  // set true when user stops — blocks any pending retries

function _destroyPlayers(){
  // Note: does NOT set _playerStopped — caller (doPlay/playerStop) manages that
  if(hlsObj){hlsObj.destroy();hlsObj=null;}
  if(mpegtsObj){mpegtsObj.destroy();mpegtsObj=null;}
  vid.pause(); vid.removeAttribute('src'); vid.load();
}

function doPlay(url, name, opts={}){
  pUrl=url; pName=name||url;
  const dlb=document.getElementById('dl-now-btn'); if(dlb) dlb.disabled=false;
  const dlbm=document.getElementById('dl-now-btn-mob'); if(dlbm) dlbm.disabled=false;
  _playerStopped = false;                        // new play — clear stop flag
  window._mseTranscodeFired = false;             // reset MSE transcode guard
  if(window._mpegRetries) window._mpegRetries = {}; // reset general retry counter
  window._remuxFired = false;                        // reset remux fallback flag
  window._hlsRemuxFired = false;                     // reset HLS remux fallback flag
  if(window._hlsRetries) window._hlsRetries = {};    // reset HLS retry counter
  setNP('▶ '+pName);
  document.getElementById('pu').textContent=url;
  document.getElementById('ppbtn').textContent='⏸';
  document.getElementById('vph').style.opacity='0';
  forceTab('p-player','t-player');

  _destroyPlayers();

  // Local /api/ URLs (transcode proxy) must never be wrapped in /api/proxy again
  const px = url.startsWith('/api/') ? url : '/api/proxy?url='+encodeURIComponent(url);
  const u=url.toLowerCase().split('?')[0];
  const qs=url.toLowerCase();
  const fallbackUrl=opts.fallbackUrl||null;

  // Stalker storage URLs (stalker_portal/storage/get.php) must NOT go through
  // /api/proxy — the proxy double-encodes their query string (?filename=...&token=...).
  // These are direct video files served by the portal; use them as-is.
  const isStorageUrl = u.includes('storage/get.php') || u.includes('/storage/');

  const isHls  = u.endsWith('.m3u8') || u.endsWith('.m3u')
               || u.includes('/hls/')
               || u.includes('timeshift.php')
               || qs.includes('extension=m3u8');

  // MKV/MP4/AVI etc — browser can play natively, no need for mpegts.js or HLS.js
  const _qsFull = url.toLowerCase(); // full URL including query string
  const isDirect = !isHls && !isStorageUrl && (
               u.endsWith('.mkv') || _qsFull.includes('.mkv&') || _qsFull.includes('.mkv?') || _qsFull.includes('stream=') && _qsFull.match(/stream=[^&]*\.mkv/)
               || u.endsWith('.mp4') || _qsFull.includes('.mp4&') || _qsFull.includes('.mp4?') || _qsFull.includes('stream=') && _qsFull.match(/stream=[^&]*\.mp4/)
               || u.endsWith('.avi') || u.endsWith('.mov') || u.endsWith('.webm')
               || qs.includes('extension=mkv') || qs.includes('extension=mp4'));

  const isMpegTs = !isStorageUrl && !isHls && !isDirect && (
               url.includes('/api/hls_proxy') // server-side transcode proxy
               || qs.includes('play_token=')  // MAC portals: short-lived token = raw MPEG-TS stream
               || u.endsWith('.ts')
               || u.endsWith('.mpg')
               || u.endsWith('/mpegts')
               || u.includes('/mpegts?')
               || qs.includes('extension=ts')
               || qs.includes('output=ts'));

  const playerType = isStorageUrl?'storage':isHls?'HLS':isDirect?'direct':isMpegTs?'MPEG-TS':'direct';
  const mpegtsOk = isMpegTs && typeof mpegts!=='undefined' && mpegts.isSupported();
  alog('▶ '+pName+' ['+playerType+(isMpegTs&&!mpegtsOk?' → MSE not supported, trying native':'')+']','k');

  if(isDirect){
    // ── Direct container (MKV/MP4/AVI) — browser native playback via proxy ──
    alog('[Direct] Playing natively ('+playerType+'): '+pName,'k');
    vid.src=px; vid.play().catch(()=>{});

  } else if(isStorageUrl){
    // ── Stalker storage/get.php — direct to video, no proxy ──────
    // Proxying would double-encode the query string (?filename=...&token=...).
    alog('[Storage] Playing direct (no proxy)','k');
    vid.src=url; vid.play().catch(()=>{});

  } else if(isHls && typeof Hls !== 'undefined' && Hls.isSupported()){
    // ── HLS via HLS.js ────────────────────────────────────────
    hlsObj=new Hls({
      enableWorker:false, lowLatencyMode:false,
      maxBufferLength:60, maxMaxBufferLength:180,
      fragLoadingTimeOut:25000, manifestLoadingTimeOut:20000,
      levelLoadingTimeOut:20000,
      xhrSetup(xhr){xhr.withCredentials=false;},
      // Disable HLS.js subtitle track management with full no-op stubs
      // so our own addTextTrack() cues are never touched by HLS internals
      subtitleStreamController: class { startLoad(){}  stopLoad(){}  destroy(){}  onMediaAttached(){}  onMediaDetaching(){}  onManifestLoading(){}  onManifestLoaded(){}  onManifestParsed(){}  onLevelLoaded(){}  onAudioTrackSwitching(){}  onSubtitleFragProcessed(){}  onBufferFlushing(){}  on(){}  off(){} },
      subtitleTrackController:  class { startLoad(){}  stopLoad(){}  destroy(){}  onMediaAttached(){}  onMediaDetaching(){}  onManifestLoading(){}  onManifestLoaded(){}  onManifestParsed(){}  onLevelLoaded(){}  on(){}  off(){} },
    });
    hlsObj.loadSource(px);
    hlsObj.attachMedia(vid);
    hlsObj.on(Hls.Events.MANIFEST_PARSED,()=>vid.play().catch(()=>{}));
    hlsObj.on(Hls.Events.ERROR,(_,data)=>{
      const _det=(data.details||'').toLowerCase();
      const _isManifest=_det.includes('manifest');
      // Log all fatal errors and manifest errors
      if(data.fatal || _isManifest) alog('[HLS] '+data.type+': '+data.details+(data.fatal?' (fatal)':' (non-fatal)'),'e');
      // 503/403/404 — hard stop immediately
      const hc=data?.response?.code||0;
      if(hc===503||hc===403||hc===404){
        alog('[HLS] Channel unavailable ('+hc+') — stopping','e');
        setNP('✗ Channel unavailable ('+hc+')');
        document.getElementById('ppbtn').textContent='▶';
        if(hlsObj){hlsObj.destroy();hlsObj=null;}
        return;
      }
      // manifestParsingError: retrying same manifest is pointless, go straight to remux
      if(_isManifest && !_playerStopped && !url.includes('hls_proxy') && !window._hlsRemuxFired){
        window._hlsRemuxFired=true;
        alog('[HLS] Manifest unparseable — trying ffmpeg remux…','w');
        if(hlsObj){hlsObj.destroy();hlsObj=null;}
        const remuxUrl='/api/hls_proxy?url='+encodeURIComponent(url);
        setTimeout(()=>{
          if(_playerStopped) return;
          setNP('▶ '+name+' [remux]');
          if(typeof mpegts!=='undefined'&&mpegts.isSupported()){
            _playerStopped=false;
            mpegtsObj=mpegts.createPlayer({type:'mse',isLive:true,url:remuxUrl,cors:true},{
              enableWorker:false,liveBufferLatencyChasing:true,
              liveBufferLatencyMaxLatency:12,liveBufferLatencyMinRemain:3,
            });
            mpegtsObj.attachMediaElement(vid);
            mpegtsObj.load();
            vid.play().catch(()=>{});
            mpegtsObj.on(mpegts.Events.ERROR,(et2,ed2)=>{
              if(!_playerStopped){
                alog('[HLS/remux] '+(ed2?.msg||String(ed2)),'e');
              // MSE/codec error (e.g. HEVC) — escalate to ffmpeg transcode
              const _isMSE2 = (String(et2||'').includes('Media') || String(et2||'')==='MediaError')
                           && (String(ed2||'').includes('MSE')||String(ed2?.msg||'').includes('MSE')
                               ||String(ed2||'').includes('Unsupported')||String(ed2?.msg||'').includes('Unsupported'));
              if(_isMSE2 && !_playerStopped && !window._mseTranscodeFired){
                window._mseTranscodeFired=true;
                alog('[HLS/remux] HEVC codec — escalating to ffmpeg transcode…','w');
                setTimeout(()=>{
                  if(_playerStopped) return;
                  if(mpegtsObj){mpegtsObj.destroy();mpegtsObj=null;}
                  vid.pause(); vid.removeAttribute('src'); vid.load();
                  _playerStopped=false;
                  const transcodeUrl='/api/hls_proxy?transcode=1&url='+encodeURIComponent(url);
                  setNP('▶ '+name+' [transcoding HEVC→H.264]');
                  if(typeof mpegts!=='undefined'&&mpegts.isSupported()){
                    mpegtsObj=mpegts.createPlayer({type:'mse',isLive:true,url:transcodeUrl,cors:true},{enableWorker:false,liveBufferLatencyChasing:true,liveBufferLatencyMaxLatency:12,liveBufferLatencyMinRemain:3});
                    mpegtsObj.attachMediaElement(vid); mpegtsObj.load(); vid.play().catch(()=>{});
                    mpegtsObj.on(mpegts.Events.ERROR,(et3,ed3)=>{
                      if(!_playerStopped){ alog('[HLS/transcode] '+(ed3?.msg||String(ed3)),'e'); setNP('✗ Transcode failed: '+name); document.getElementById('ppbtn').textContent='▶'; }
                    });
                  } else { vid.src=transcodeUrl; vid.play().catch(()=>{}); }
                },0);
                return;
              }
                setNP('✗ Stream unavailable: '+name);
                document.getElementById('ppbtn').textContent='▶';
              }
            });
          } else { vid.src=remuxUrl; vid.play().catch(()=>{}); }
        },0);
        return;
      }
      if(!data.fatal) return;
      // Fatal non-manifest errors
      if(data.type===Hls.ErrorTypes.NETWORK_ERROR){
        if(!window._hlsRetries) window._hlsRetries={};
        const _hk=String(pIdx)+'|'+url.slice(-20);
        window._hlsRetries[_hk]=(window._hlsRetries[_hk]||0)+1;
        if(window._hlsRetries[_hk]<=3&&!_playerStopped){
          setTimeout(()=>{if(hlsObj&&!_playerStopped)hlsObj.startLoad();},2500);
        } else if(!_playerStopped&&!url.includes('hls_proxy')&&!window._hlsRemuxFired){
          window._hlsRemuxFired=true;
          alog('[HLS] Retries exhausted — trying ffmpeg remux…','w');
          const remuxUrl='/api/hls_proxy?url='+encodeURIComponent(url);
          if(hlsObj){hlsObj.destroy();hlsObj=null;}
          setTimeout(()=>{
            if(_playerStopped) return;
            setNP('▶ '+name+' [remux]');
            _playerStopped=false;
            if(typeof mpegts!=='undefined'&&mpegts.isSupported()){
              mpegtsObj=mpegts.createPlayer({type:'mse',isLive:true,url:remuxUrl,cors:true},{
                enableWorker:false,liveBufferLatencyChasing:true,
                liveBufferLatencyMaxLatency:12,liveBufferLatencyMinRemain:3,
              });
              mpegtsObj.attachMediaElement(vid);
              mpegtsObj.load();
              vid.play().catch(()=>{});
              mpegtsObj.on(mpegts.Events.ERROR,(et2,ed2)=>{
                if(!_playerStopped){
                  alog('[HLS/remux] '+(ed2?.msg||String(ed2)),'e');
                // MSE/codec error (e.g. HEVC) — escalate to ffmpeg transcode
                const _isMSE2 = (String(et2||'').includes('Media') || String(et2||'')==='MediaError')
                             && (String(ed2||'').includes('MSE')||String(ed2?.msg||'').includes('MSE')
                                   ||String(ed2||'').includes('Unsupported')||String(ed2?.msg||'').includes('Unsupported'));
                if(_isMSE2 && !_playerStopped && !window._mseTranscodeFired){
                  window._mseTranscodeFired=true;
                  alog('[HLS/remux] HEVC codec — escalating to ffmpeg transcode…','w');
                  setTimeout(()=>{
                    if(_playerStopped) return;
                    if(mpegtsObj){mpegtsObj.destroy();mpegtsObj=null;}
                    vid.pause(); vid.removeAttribute('src'); vid.load();
                    _playerStopped=false;
                    const transcodeUrl='/api/hls_proxy?transcode=1&url='+encodeURIComponent(url);
                    setNP('▶ '+name+' [transcoding HEVC→H.264]');
                    if(typeof mpegts!=='undefined'&&mpegts.isSupported()){
                      mpegtsObj=mpegts.createPlayer({type:'mse',isLive:true,url:transcodeUrl,cors:true},{enableWorker:false,liveBufferLatencyChasing:true,liveBufferLatencyMaxLatency:12,liveBufferLatencyMinRemain:3});
                      mpegtsObj.attachMediaElement(vid); mpegtsObj.load(); vid.play().catch(()=>{});
                      mpegtsObj.on(mpegts.Events.ERROR,(et3,ed3)=>{
                        if(!_playerStopped){ alog('[HLS/transcode] '+(ed3?.msg||String(ed3)),'e'); setNP('✗ Transcode failed: '+name); document.getElementById('ppbtn').textContent='▶'; }
                      });
                    } else { vid.src=transcodeUrl; vid.play().catch(()=>{}); }
                  },0);
                  return;
                }
                  setNP('✗ Stream unavailable: '+name);
                  document.getElementById('ppbtn').textContent='▶';
                }
              });
            } else { vid.src=remuxUrl; vid.play().catch(()=>{}); }
          },0);
        } else if(!_playerStopped){
          alog('[HLS] Stream failed — channel may be offline','e');
          setNP('✗ Stream unavailable: '+name);
          document.getElementById('ppbtn').textContent='▶';
          if(hlsObj){hlsObj.destroy();hlsObj=null;}
        }
      } else if(data.type===Hls.ErrorTypes.MEDIA_ERROR){
        hlsObj.recoverMediaError();
      }
    });
  } else if(isHls && vid.canPlayType('application/vnd.apple.mpegurl')){
    // ── Native HLS (Safari / iOS WebView) ─────────────────────
    vid.src=url; vid.play().catch(()=>{});

  } else if(isHls){
    // ── HLS.js not loaded, try native src as last resort ──────
    alog('[HLS] hls.js unavailable — trying native src','w');
    vid.src=url; vid.play().catch(()=>{});

  } else if(mpegtsOk){
    // ── Raw MPEG-TS via mpegts.js ──────────────────────────────
    const isLiveStream = (opts.isLive !== false);
    mpegtsObj=mpegts.createPlayer({
      type:'mse', isLive: isLiveStream, url:px, cors:true,
    },{
      enableWorker:false,
      liveBufferLatencyChasing: isLiveStream,
      liveBufferLatencyMaxLatency: isLiveStream ? 8 : undefined,
      liveBufferLatencyMinRemain: isLiveStream ? 2 : undefined,
      autoCleanupSourceBuffer: !isLiveStream,
    });
    mpegtsObj.attachMediaElement(vid);
    mpegtsObj.load();
        // For catchup/VOD: seek to start once metadata is ready
    if(!isLiveStream){
      vid.addEventListener('loadedmetadata', function _seekStart(){
        vid.removeEventListener('loadedmetadata', _seekStart);
        if(vid.currentTime > 1) vid.currentTime = 0;
        vid.play().catch(()=>{});
      });
    }
    mpegtsObj.on(mpegts.Events.ERROR,(et,ed,ei)=>{
      // et=error type, ed=error detail (string), ei=error info object (has httpStatusCode)
      const msg=(ei?.msg||ed||'');
      const etStr = String(et||'');
      const edStr = String(ed||'');
      const httpCode = ei?.httpStatusCode||ei?.statusCode||ei?.code||0;
      const _codeTag = httpCode && httpCode>0 ? ' (HTTP '+httpCode+')' : '';
      alog('[MPEGTS] '+etStr+_codeTag+': '+edStr,'e');
      const hasPlayToken = url.toLowerCase().includes('play_token=');
      // MediaMSEError = codec unsupported by browser (e.g. HEVC/H.265)
      // Match both strict type check AND string fallback from the log: "MediaError: MediaMSEError"
      // FormatUnsupported = wrong container (e.g. HLS playlist fed to mpegts.js) → try HLS.js
      // Real MSEError = codec unsupported by browser (e.g. HEVC/H.265) → try ffmpeg transcode
      const isFormatUnsupported = (et===mpegts.ErrorTypes.MEDIA_ERROR || etStr==='MediaError')
                      && (edStr.includes('FormatUnsupported') || msg.includes('FormatUnsupported'));
      const isMSEError = !isFormatUnsupported
                      && (et===mpegts.ErrorTypes.MEDIA_ERROR || etStr==='MediaError')
                      && (edStr.includes('MSE') || edStr.includes('mse') || msg.includes('MSE')
                          || edStr.includes('Unsupported') || msg.includes('Unsupported'));
      // FormatUnsupported: content is not MPEG-TS at all (portal sent HLS/MP4 with play_token URL)
      // → try HLS.js on the original URL first; if that also fails, fall back to ffmpeg remux
      if(isFormatUnsupported){
        if(!_playerStopped && !window._mseTranscodeFired){
          window._mseTranscodeFired = true;
          alog('[MPEGTS] FormatUnsupported — content may be HLS; retrying with HLS.js…','w');
          setTimeout(()=>{
            if(_playerStopped) return;
            if(mpegtsObj){mpegtsObj.destroy();mpegtsObj=null;}
            vid.pause(); vid.removeAttribute('src'); vid.load();
            _playerStopped = false;
            const rawUrl = url; // original unproxied URL
            const pxUrl = rawUrl.startsWith('/api/') ? rawUrl : '/api/proxy?url='+encodeURIComponent(rawUrl);
            if(typeof Hls !== 'undefined' && Hls.isSupported()){
              setNP('▶ '+name+' [HLS fallback]');
              hlsObj = new Hls({
                enableWorker:false, lowLatencyMode:false,
                maxBufferLength:60, maxMaxBufferLength:180,
                fragLoadingTimeOut:25000, manifestLoadingTimeOut:20000,
                levelLoadingTimeOut:20000,
                xhrSetup(xhr){xhr.withCredentials=false;},
                subtitleStreamController: class { startLoad(){}  stopLoad(){}  destroy(){}  onMediaAttached(){}  onMediaDetaching(){}  onManifestLoading(){}  onManifestLoaded(){}  onManifestParsed(){}  onLevelLoaded(){}  onAudioTrackSwitching(){}  onSubtitleFragProcessed(){}  onBufferFlushing(){}  on(){}  off(){} },
                subtitleTrackController:  class { startLoad(){}  stopLoad(){}  destroy(){}  onMediaAttached(){}  onMediaDetaching(){}  onManifestLoading(){}  onManifestLoaded(){}  onManifestParsed(){}  onLevelLoaded(){}  on(){}  off(){} },
              });
              hlsObj.loadSource(pxUrl);
              hlsObj.attachMedia(vid);
              hlsObj.on(Hls.Events.MANIFEST_PARSED,()=>vid.play().catch(()=>{}));
              hlsObj.on(Hls.Events.ERROR,(_,d)=>{
                if(d.fatal && !_playerStopped && !window._remuxFired){
                  window._remuxFired = true;
                  alog('[HLS fallback] Failed — trying ffmpeg remux…','w');
                  if(hlsObj){hlsObj.destroy();hlsObj=null;}
                  const remuxUrl='/api/hls_proxy?url='+encodeURIComponent(rawUrl);
                  setTimeout(()=>{
                    if(_playerStopped) return;
                    setNP('▶ '+name+' [remux]');
                    if(typeof mpegts!=='undefined'&&mpegts.isSupported()){
                      mpegtsObj=mpegts.createPlayer({type:'mse',isLive:true,url:remuxUrl,cors:true},{enableWorker:false});
                      mpegtsObj.attachMediaElement(vid); mpegtsObj.load(); vid.play().catch(()=>{});
                    } else { vid.src=remuxUrl; vid.play().catch(()=>{}); }
                  },0);
                }
              });
            } else {
              // No HLS.js — try native src (Safari/iOS handles m3u8 natively)
              setNP('▶ '+name+' [native HLS fallback]');
              vid.src=rawUrl; vid.play().catch(()=>{});
            }
          }, 0);
        }
        return;
      }
      if(isMSEError){
        if(!_playerStopped && !url.includes('transcode=1') && !window._mseTranscodeFired){
          window._mseTranscodeFired = true; // guard: only fire once per play session
          alog('[MPEGTS] MSE codec error — re-encoding via ffmpeg (H.264)…','w');
          const transcodeUrl='/api/hls_proxy?transcode=1&url='+encodeURIComponent(url);
          // Defer to next tick — cannot safely destroy mpegts from within its own error callback
          setTimeout(()=>{
          if(mpegtsObj){mpegtsObj.destroy();mpegtsObj=null;}
          vid.pause(); vid.removeAttribute('src'); vid.load();
          _playerStopped = false;
          if(typeof mpegts!=='undefined' && mpegts.isSupported()){
            setNP('▶ '+name+' [transcoding HEVC→H.264]');
            mpegtsObj=mpegts.createPlayer({type:'mse',isLive:true,url:transcodeUrl,cors:true},{
              enableWorker:false,
              liveBufferLatencyChasing:true,
              liveBufferLatencyMaxLatency:12,
              liveBufferLatencyMinRemain:3,
            });
            mpegtsObj.attachMediaElement(vid);
            mpegtsObj.load();
            vid.play().catch(()=>{});
            mpegtsObj.on(mpegts.Events.ERROR,(et2,ed2)=>{
              if(!_playerStopped){
                alog('[MPEGTS/transcode] '+et2+': '+(ed2?.msg||JSON.stringify(ed2)),'e');
                setNP('✗ Transcode failed — ffmpeg may not support this codec');
                document.getElementById('ppbtn').textContent='▶';
              }
            });
          } else {
            // mpegts.js unavailable — try native src as last resort
            vid.src=transcodeUrl; vid.play().catch(()=>{});
          }
          }, 0); // end setTimeout defer
        }
        return;
      }
      // 503/403/404 = channel offline — stop immediately, never retry
      if(httpCode===503 || httpCode===403 || httpCode===404){
        alog('[MPEGTS] Channel unavailable ('+httpCode+') — stopping','e');
        setNP('✗ Channel unavailable ('+httpCode+')');
        document.getElementById('ppbtn').textContent='▶';
        return;
      }
      // play_token URLs: re-resolve for fresh token, but cap at 2 retries
      if(isLiveStream && et===mpegts.ErrorTypes.NETWORK_ERROR && hasPlayToken){
        if(!window._ptRetries) window._ptRetries = {};
        const _rk = pIdx+'|'+url.slice(-20);
        window._ptRetries[_rk] = (window._ptRetries[_rk]||0)+1;
        if(window._ptRetries[_rk] <= 2 && !_playerStopped){
          alog('[MPEGTS] play_token failed (attempt '+window._ptRetries[_rk]+'/2) — re-resolving…','w');
          if(pIdx>=0) setTimeout(()=>{ if(!_playerStopped) playItem(pIdx); },1000);
        } else {
          alog('[MPEGTS] play_token failed after 2 retries — channel may be offline','e');
          setNP('✗ Stream unavailable: '+name);
          document.getElementById('ppbtn').textContent='▶';
          window._ptRetries[_rk]=0;
        }
      } else if(isLiveStream && et===mpegts.ErrorTypes.NETWORK_ERROR){
        if(!window._mpegRetries) window._mpegRetries = {};
        const _mk = String(pIdx)+'|'+url.slice(-20);
        window._mpegRetries[_mk] = (window._mpegRetries[_mk]||0)+1;
        if(window._mpegRetries[_mk] <= 3 && !_playerStopped){
          setTimeout(()=>{ if(mpegtsObj && !_playerStopped){ mpegtsObj.unload(); mpegtsObj.load(); vid.play().catch(()=>{}); }},2000);
        } else if(!_playerStopped && !url.includes('hls_proxy') && !window._remuxFired){
          // All normal retries exhausted — try ffmpeg -c copy remux as last resort.
          // Handles container/mux issues that mpegts.js can't parse but ffmpeg can.
          // -c copy = no re-encode, near-zero CPU cost.
          window._remuxFired = true;
          alog('[MPEGTS] Retries exhausted \u2014 trying ffmpeg remux (-c copy)\u2026','w');
          const remuxUrl='/api/hls_proxy?url='+encodeURIComponent(url);
          setTimeout(()=>{
            if(_playerStopped) return;
            if(mpegtsObj){mpegtsObj.destroy();mpegtsObj=null;}
            vid.pause(); vid.removeAttribute('src'); vid.load();
            _playerStopped=false;
            setNP('\u25b6 '+name+' [remux]');
            mpegtsObj=mpegts.createPlayer({type:'mse',isLive:true,url:remuxUrl,cors:true},{
              enableWorker:false,liveBufferLatencyChasing:true,
              liveBufferLatencyMaxLatency:12,liveBufferLatencyMinRemain:3,
            });
            mpegtsObj.attachMediaElement(vid);
            mpegtsObj.load();
            vid.play().catch(()=>{});
            mpegtsObj.on(mpegts.Events.ERROR,(et2,ed2)=>{
              if(!_playerStopped){
                alog('[MPEGTS/remux] '+(ed2?.msg||JSON.stringify(ed2)),'e');
                setNP('\u2717 Stream unavailable: '+name);
                document.getElementById('ppbtn').textContent='\u25b6';
              }
            });
          },0);
        } else if(!_playerStopped){
          alog('[MPEGTS] Stream failed after retries \u2014 channel may be offline','e');
          setNP('\u2717 Stream unavailable: '+name);
          document.getElementById('ppbtn').textContent='\u25b6';
          window._mpegRetries[_mk]=0;
        }
      } else if(!isLiveStream && et===mpegts.ErrorTypes.NETWORK_ERROR){
        alog('[MPEGTS] VOD stream unavailable'+_codeTag+' — '+msg,'e');
        setNP('✗ Stream unavailable'+_codeTag+': '+name);
        document.getElementById('ppbtn').textContent='▶';
      } else if(!isLiveStream && fallbackUrl && et===mpegts.ErrorTypes.NETWORK_ERROR){
        // Catchup path-based .ts failed → try query-string format via HLS.js
        alog('[MPEGTS] Catchup .ts failed — retrying with fallback URL via HLS.js','w');
        _destroyPlayers();
        doPlay(fallbackUrl, name, {isLive:false});
      }
    });
    if(isLiveStream) vid.play().catch(()=>{});

  } else if(isMpegTs){
    // ── MPEG-TS but MSE not supported — try direct native src first,
    // then server-side ffmpeg proxy as fallback ────────────────────
    alog('[MPEGTS] MSE unavailable — trying direct native src…','w');
    vid.src=px;
    vid.play().catch(()=>{
      // Direct failed — try ffmpeg remux proxy
      alog('[MPEGTS] Direct failed — remuxing via ffmpeg proxy…','w');
      const hlsProxyUrl='/api/hls_proxy?url='+encodeURIComponent(url);
      vid.src=hlsProxyUrl;
      vid.play().catch(e=>{
        alog('[MPEGTS/proxy] '+e.message,'e');
        document.getElementById('ppbtn').textContent='▶';
      });
    });

  } else {
    // ── Fallback: direct proxy (MP4, VOD, etc.) ────────────────
    vid.src=px; vid.play().catch(()=>{});
  }
  renderItems(filtItems);
}

vid.addEventListener('play',()=>document.getElementById('ppbtn').textContent='⏸');
vid.addEventListener('pause',()=>document.getElementById('ppbtn').textContent='▶');
vid.addEventListener('ended',()=>document.getElementById('ppbtn').textContent='▶');
vid.addEventListener('canplay',()=>document.getElementById('vph').style.opacity='0');

function playerPP(){vid.paused||vid.ended?vid.play().catch(()=>{}):vid.pause();}
function playerStop(){
  _playerStopped = true;
  _destroyPlayers();
  pUrl=''; setNP('⏹ Stopped'); document.getElementById('pu').textContent='—';
  document.getElementById('ppbtn').textContent='▶';
  document.getElementById('vph').style.opacity='1';
  const dlb=document.getElementById('dl-now-btn'); if(dlb) dlb.disabled=true;
  const dlbm=document.getElementById('dl-now-btn-mob'); if(dlbm) dlbm.disabled=true;
}
function playerPrev(){if(!filtItems.length)return; playItem(pIdx<=0?filtItems.length-1:pIdx-1);}
function playerNext(){if(!filtItems.length)return; playItem(pIdx<0||pIdx>=filtItems.length-1?0:pIdx+1);}
function setVol(v){document.getElementById('vlbl').textContent=v; vid.volume=v/100;}
function setNP(t){document.getElementById('np').textContent=t;}
function togglePlayerControls(){
  const panel = document.getElementById('pctrl-panel');
  if(!panel) return;
  panel.classList.toggle('expanded');
}

function toggleTheater(){
  const main = document.getElementById('main');
  const btn  = document.getElementById('theaterbtn');
  const on   = main.classList.toggle('theater');
  // Also collapse/restore player controls
  const pctrl = document.getElementById('pctrl-panel');
  if(pctrl){
    if(on) pctrl.classList.remove('expanded');
    else pctrl.classList.add('expanded');
  }
  // Close activity log if opening theater mode
  if(on){
    const logPanel = document.getElementById('desktop-log');
    if(logPanel && logPanel.classList.contains('expanded')){
      logPanel.classList.remove('expanded');
    }
  }  
  const icon = document.getElementById('theater-icon');
  if(icon) icon.innerHTML = on
    ? '<polyline points="2,4 2,2 4,2"/><polyline points="12,2 14,2 14,4"/><polyline points="2,12 2,14 4,14"/><polyline points="14,12 14,14 12,14"/>'
    : '<polyline points="4,2 2,2 2,4"/><polyline points="12,2 14,2 14,4"/><polyline points="4,14 2,14 2,12"/><polyline points="12,14 14,14 14,12"/>';
  btn.title = on ? 'Exit theater mode' : 'Theater mode';
}

function cpyUrl(){
  if(!pUrl)return;
  navigator.clipboard?.writeText(pUrl)
    .then(()=>toast('URL copied!','ok')).catch(()=>toast('Copy failed','wrn'));
}

// ── RECORDING ──────────────────────────────────────────────
async function togRec(){isRec?stopRec():startRec();}

// ── EPG ────────────────────────────────────────────────────────────────────
let _epgItem=null;
function _fmtEpgTime(ts){
  if(!ts) return '';
  const d=new Date(ts*1000);
  return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
}
function _epgCard(prog, label){
  if(!prog) return `<div style="color:var(--txt3);font-size:12px;padding:6px 0">${label}: —</div>`;
  const start=_fmtEpgTime(prog.start), end=_fmtEpgTime(prog.end);
  const time=start&&end?`<span style="color:var(--acc);font-size:11px;margin-left:6px">${start}–${end}</span>`:'';
  const desc=prog.desc?`<div style="color:var(--txt3);font-size:11px;margin-top:4px;line-height:1.5">${prog.desc}</div>`:'';
  return `<div style="background:var(--s3);border-radius:var(--rsm);padding:10px 12px;margin-bottom:8px">
    <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--txt3);margin-bottom:4px">${label}</div>
    <div style="display:flex;align-items:baseline;flex-wrap:wrap;gap:4px">
      <span style="font-size:13px;font-weight:600;color:var(--txt1)">${prog.title||'Unknown'}</span>${time}
    </div>${desc}
  </div>`;
}
async function showEPG(){
  if(!_epgItem){toast('No channel loaded','warn');return;}
  const ov=document.getElementById('epg-overlay');
  document.getElementById('epg-ch-name').textContent=_epgItem.name||'EPG';
  document.getElementById('epg-body').innerHTML='<div style="color:var(--txt3);font-size:12px;text-align:center;padding:20px">Loading…</div>';
  ov.style.display='flex';
  try{
    const r=await fetch('/api/epg',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({item:_epgItem})});
    const d=await r.json();
    if(d.error&&!d.current&&!d.next&&!d.schedule?.length){
      document.getElementById('epg-body').innerHTML=`<div style="color:var(--txt3);font-size:12px;text-align:center;padding:20px">${d.error}</div>`;
      return;
    }
    // Build full schedule list, highlighting current
    const schedule = d.schedule||[];
    if(schedule.length===0){
      document.getElementById('epg-body').innerHTML=_epgCard(d.current,'Now')+_epgCard(d.next,'Next');
    } else {
      const now=Date.now()/1000;
      const rows=schedule.map(p=>{
        const isCurrent=p.start<=now&&now<p.end;
        const start=_fmtEpgTime(p.start), end=_fmtEpgTime(p.end);
        const bg=isCurrent?'var(--s3)':'transparent';
        const titleColor=isCurrent?'var(--acc)':'var(--txt1)';
        const dot=isCurrent?'<span style="color:var(--acc);margin-right:5px">▸</span>':'';
        const desc=p.desc?`<div style="color:var(--txt3);font-size:11px;margin-top:3px;line-height:1.4">${p.desc}</div>`:'';
        return `<div style="background:${bg};border-radius:var(--rsm);padding:8px 10px;margin-bottom:4px;border-left:2px solid ${isCurrent?'var(--acc)':'transparent'}">
          <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">
            <span style="font-size:11px;color:var(--acc);white-space:nowrap;min-width:90px">${start}${end?' – '+end:''}</span>
            <span style="font-size:13px;font-weight:${isCurrent?700:400};color:${titleColor}">${dot}${p.title}</span>
          </div>${desc}
        </div>`;
      }).join('');
      document.getElementById('epg-body').innerHTML=rows;
      // Scroll current item into view
      const cur=document.querySelector('#epg-body [style*="var(--acc)"]');
      if(cur) cur.scrollIntoView({block:'nearest'});
    }
    if(d.current) document.getElementById('epg-now').textContent='▸ '+d.current.title;
    else if(d.error) document.getElementById('epg-now').textContent='No EPG';
  }catch(e){
    document.getElementById('epg-body').innerHTML=`<div style="color:var(--err);font-size:12px;text-align:center;padding:20px">Failed: ${e.message}</div>`;
  }
}
function closeEPG(){document.getElementById('epg-overlay').style.display='none';}
// Close on backdrop click
document.getElementById('epg-overlay').addEventListener('click',function(e){if(e.target===this)closeEPG();});

// ══════════════════════════════════════════════════════════════════════════════
// EPG GRID VIEW  — TV Guide-style layout across all channels in items tab
// ══════════════════════════════════════════════════════════════════════════════
const EPG_PX_MIN   = 3;          // pixels per minute
const EPG_WIN_BACK = 60;         // minutes before now to show
const EPG_WIN_FWD  = 5 * 60;     // minutes after now to show
const EPG_CH_W     = 110;        // px — fixed left channel column

let _epgGridActive = false;
let _epgGridObs    = null;       // IntersectionObserver for lazy row loading

function _epgNowX(){
  // X-pixel offset of "now" inside the timeline area (relative to timeline start)
  return EPG_WIN_BACK * EPG_PX_MIN;
}

function _epgTsToX(ts){
  const nowSec = Date.now() / 1000;
  const diffMin = (ts - nowSec) / 60;
  return _epgNowX() + diffMin * EPG_PX_MIN;
}

function _epgTotalW(){
  return (EPG_WIN_BACK + EPG_WIN_FWD) * EPG_PX_MIN;
}

function toggleEpgGrid(){
  if(!_epgGridActive) _openEpgGrid();
  else                _closeEpgGrid();
}

function _openEpgGrid(){
  if(mode !== 'live'){ toast('EPG grid only available for Live channels','wrn'); return; }
  if(!filtItems.length){ toast('No channels to show','wrn'); return; }
  _epgGridActive = true;
  document.getElementById('ilist').style.display         = 'none';
  document.getElementById('epg-grid-wrap').classList.add('active');
  document.getElementById('epg-grid-btn').classList.add('active');
  document.getElementById('epg-grid-btn').textContent    = '✕ List';
  document.getElementById('icount').style.display        = 'none';
  document.getElementById('items-sbar').style.display    = 'none';
  _buildEpgGrid(filtItems);

  // ── Click-drag scroll on desktop (on the timeline column) ────────────────
  const wrap = document.getElementById('epg-tl-col');
  const chCol2 = document.getElementById('epg-ch-col');
  if(wrap && !wrap._dragScrollAttached){
    // Sync vertical scroll between timeline col and ch col
    const onTlScroll = () => { if(chCol2) chCol2.scrollTop = wrap.scrollTop; };
    wrap.addEventListener('scroll', onTlScroll);
    wrap._syncScrollCleanup = () => wrap.removeEventListener('scroll', onTlScroll);

    let _isDown = false, _startX = 0, _startY = 0, _scrollLeft = 0, _scrollTop = 0, _dragged = false;
    const onDown = e => {
      if(e.button !== 0) return;
      _isDown = true;
      _dragged = false;
      _startX = e.pageX - wrap.offsetLeft;
      _startY = e.pageY - wrap.offsetTop;
      _scrollLeft = wrap.scrollLeft;
      _scrollTop  = wrap.scrollTop;
      wrap.style.cursor = 'grabbing';
      wrap.style.userSelect = 'none';
    };
    const onUp = () => {
      _isDown = false;
      wrap.style.cursor = '';
      wrap.style.userSelect = '';
    };
    const onMove = e => {
      if(!_isDown) return;
      e.preventDefault();
      const x = e.pageX - wrap.offsetLeft;
      const y = e.pageY - wrap.offsetTop;
      const dx = x - _startX, dy = y - _startY;
      if(Math.abs(dx) > 3 || Math.abs(dy) > 3) _dragged = true;
      wrap.scrollLeft = _scrollLeft - dx;
      wrap.scrollTop  = _scrollTop  - dy;
    };
    // Suppress click on ch-cell if drag occurred
    const onClickCapture = e => {
      if(_dragged){ e.stopPropagation(); e.preventDefault(); _dragged = false; }
    };
    wrap.addEventListener('mousedown', onDown);
    wrap.addEventListener('mouseup',   onUp);
    wrap.addEventListener('mouseleave',onUp);
    wrap.addEventListener('mousemove', onMove);
    wrap.addEventListener('click', onClickCapture, true);
    wrap._dragScrollAttached = true;
    wrap._dragScrollCleanup = () => {
      wrap.removeEventListener('mousedown', onDown);
      wrap.removeEventListener('mouseup',   onUp);
      wrap.removeEventListener('mouseleave',onUp);
      wrap.removeEventListener('mousemove', onMove);
      wrap.removeEventListener('click', onClickCapture, true);
      if(wrap._syncScrollCleanup) wrap._syncScrollCleanup();
      wrap._dragScrollAttached = false;
    };
  }
}

function _closeEpgGrid(){
  _epgGridActive = false;
  if(_epgGridObs){ _epgGridObs.disconnect(); _epgGridObs = null; }
  // Cancel any pending XMLTV poller and clear waiting list
  if(_epgXmltvPollTimer){ clearTimeout(_epgXmltvPollTimer); _epgXmltvPollTimer = null; }
  _epgXmltvWaiting = [];
  // Remove scroll listener from the grid container
  const wrap = document.getElementById('epg-tl-col');
  if(wrap && wrap._epgScrollHandler){
    wrap.removeEventListener('scroll', wrap._epgScrollHandler);
    wrap._epgScrollHandler = null;
  }
  // Remove drag-scroll listeners
  if(wrap && wrap._dragScrollCleanup){
    wrap._dragScrollCleanup();
    wrap._dragScrollCleanup = null;
  }
  document.getElementById('ilist').style.display         = '';
  document.getElementById('epg-grid-wrap').classList.remove('active');
  document.getElementById('epg-grid-btn').classList.remove('active');
  document.getElementById('epg-grid-btn').textContent    = '📅 EPG';
  document.getElementById('icount').style.display        = '';
  document.getElementById('items-sbar').style.display    = '';
}

function _buildEpgGrid(channels){
  const chCol  = document.getElementById('epg-ch-col');
  const chHdr  = document.getElementById('epg-ch-header');
  const tlCol  = document.getElementById('epg-tl-col');
  const totalW = _epgTotalW();
  const nowX   = _epgNowX();
  const nowSec = Date.now() / 1000;

  // Build time header ticks (every 30 min)
  let ticksHtml = '';
  const stepMin = 30;
  const startSec = nowSec - EPG_WIN_BACK * 60;
  for(let m = 0; m <= EPG_WIN_BACK + EPG_WIN_FWD; m += stepMin){
    const x   = m * EPG_PX_MIN;
    const ts  = startSec + m * 60;
    const lbl = new Date(ts * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    ticksHtml += `<div class="epg-time-tick" style="left:${x}px">
      <div class="epg-time-tick-line"></div>
      <span class="epg-time-lbl" style="padding-left:3px">${lbl}</span>
    </div>`;
  }

  // Corner header (sticky, sits above ch-col)
  chHdr.innerHTML = `<div class="epg-grid-hdr-corner">
    <span style="font-size:9px;color:var(--txt3);font-weight:700;text-transform:uppercase;letter-spacing:.8px">Channels</span>
  </div>`;

  // Channel column cells
  const chCells = channels.map((ch, i) => {
    const name    = ch.name || ch.o_name || ch.fname || 'Unknown';
    const logo    = ch.logo || ch.stream_icon || ch.cover || ch.screenshot_uri || ch.pic || '';
    const logoSrc = logo && (logo.startsWith('http://') || logo.startsWith('https://'))
      ? '/api/proxy?url=' + encodeURIComponent(logo) : logo;
    const logoEl  = logoSrc
      ? `<img class="epg-ch-logo" src="${esc(logoSrc)}" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
         <div class="epg-ch-logo-ph" style="display:none">📺</div>`
      : `<div class="epg-ch-logo-ph">📺</div>`;
    return `<div class="epg-ch-cell" id="epg-ch-${i}" onclick="playItem(${i})" title="Play ${esc(name)}">
      ${logoEl}
      <div class="epg-ch-name">${esc(name)}</div>
    </div>`;
  }).join('');
  const cornerHtml = `<div id="epg-ch-header" class="epg-grid-hdr-corner">
    <span style="font-size:9px;color:var(--txt3);font-weight:700;text-transform:uppercase;letter-spacing:.8px">Channels</span>
  </div>`;
  chCol.innerHTML = cornerHtml + `<div>${chCells}</div>`;

  // Timeline column — header sticky inside, rows below
  const timeHeader = `<div class="epg-grid-hdr-times" style="width:${totalW}px;position:sticky;top:0;z-index:30;background:var(--s1);height:28px;flex-shrink:0">
    ${ticksHtml}
    <div class="epg-now-line" style="left:${nowX}px"><div class="epg-now-dot"></div></div>
  </div>`;

  const rows = channels.map((ch, i) => {
    return `<div class="epg-row" id="epg-row-${i}">
      <div class="epg-timeline" style="width:${totalW}px;min-width:${totalW}px;position:relative" id="epg-tl-${i}" data-ch-idx="${i}">
        <div class="epg-now-line" style="left:${nowX}px"></div>
        <div class="epg-prog-loading" id="epg-loading-${i}"></div>
      </div>
    </div>`;
  }).join('');
  tlCol.innerHTML = `<div style="min-width:${totalW}px">${timeHeader}${rows}</div>`;

  // Scroll timeline to "now - 10 min"
  requestAnimationFrame(() => {
    tlCol.scrollLeft = Math.max(0, nowX - 80);
  });

  // ── Scroll-based batch loader ──────────────────────────────────────────────
  const _epgLoaded = new Set();
  const ROW_H = 62;

  function _epgLoadVisible(){
    const scrollTop  = tlCol.scrollTop;
    const viewH      = tlCol.clientHeight;
    const visTop    = scrollTop;
    const visBottom = scrollTop + viewH;
    const buffer    = ROW_H * 3;
    // rows start at y=28 (after the sticky time header)
    const firstRow = Math.max(0, Math.floor((visTop - 28 - buffer) / ROW_H));
    const lastRow  = Math.min(channels.length - 1,
                              Math.ceil((visBottom - 28 + buffer) / ROW_H));

    for(let i = firstRow; i <= lastRow; i++){
      if(!_epgLoaded.has(i)){
        _epgLoaded.add(i);
        _loadEpgRow(channels[i], i);
      }
    }
  }

  if(_epgGridObs){ _epgGridObs.disconnect(); _epgGridObs = null; }
  if(tlCol._epgScrollHandler) tlCol.removeEventListener('scroll', tlCol._epgScrollHandler);
  tlCol._epgScrollHandler = _epgLoadVisible;
  tlCol.addEventListener('scroll', _epgLoadVisible, {passive: true});

  requestAnimationFrame(() => { requestAnimationFrame(_epgLoadVisible); });
}

// ── Shared XMLTV download poller ──────────────────────────────────────────────
// Instead of each EPG row retrying independently every 5s (hammering the portal),
// all "loading" rows register here. A single poller polls /api/epg_status every 5s.
// When the download is ready, ALL waiting rows reload simultaneously — one pass.
let _epgXmltvWaiting = [];   // [{ch, idx}, ...]
let _epgXmltvPollTimer = null;
let _epgXmltvUrl = '';

function _epgRegisterWaiting(ch, idx){
  // Determine the EPG URL being downloaded (from settings or ext_epg_url)
  if(!_epgXmltvUrl){
    // Try to get it from the connect state; fall back to a flag
    _epgXmltvUrl = '__downloading__';
  }
  // Avoid duplicate registrations
  if(!_epgXmltvWaiting.find(w => w.idx === idx)){
    _epgXmltvWaiting.push({ch, idx});
  }
  if(!_epgXmltvPollTimer){
    _epgXmltvPollTimer = setTimeout(_epgXmltvPoll, 6000);
  }
}

async function _epgXmltvPoll(){
  _epgXmltvPollTimer = null;
  if(!_epgXmltvWaiting.length) return;

  // Check if any channel's EPG now returns real data (XMLTV ready)
  // We use a lightweight probe: re-fetch the first waiting row's EPG.
  // If it no longer returns "loading", the download is done → reload all rows.
  const probe = _epgXmltvWaiting[0];
  try {
    const r = await fetch('/api/epg', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item: probe.ch})
    });
    const d = await r.json();
    if(d.error && d.error.toLowerCase().includes('loading')){
      // Still downloading — keep waiting, poll again
      _epgXmltvPollTimer = setTimeout(_epgXmltvPoll, 6000);
      return;
    }
  } catch(e){
    _epgXmltvPollTimer = setTimeout(_epgXmltvPoll, 6000);
    return;
  }

  // Download complete — reload all waiting rows
  const toReload = _epgXmltvWaiting.slice();
  _epgXmltvWaiting = [];
  _epgXmltvUrl = '';
  for(const {ch, idx} of toReload){
    const el = document.getElementById(`epg-loading-${idx}`);
    if(el) el.textContent = '⏳ Loading EPG…';
    _loadEpgRow(ch, idx);
  }
}

async function _loadEpgRow(ch, idx){
  const tl = document.getElementById(`epg-tl-${idx}`);
  if(!tl) return;
  const ctrl    = new AbortController();
  const timeoutId = setTimeout(() => ctrl.abort(), 180000); // 180 s — covers first-time XMLTV downloads (backend allows 120 s + parse time)
  // After 5 s inject a visible "fetching…" label so the user knows it's working
  const hintId = setTimeout(() => {
    const el = document.getElementById(`epg-loading-${idx}`);
    if(el && !el._hinted){
      el._hinted = true;
      el.style.cssText += ';display:flex;align-items:center;padding-left:8px;font-size:10px;color:var(--t2);animation:none;background:var(--s4)';
      el.textContent = '⏳ Fetching EPG…';
    }
  }, 5000);
  // After 30 s update hint to indicate a large guide file may be downloading
  const slowHintId = setTimeout(() => {
    const el = document.getElementById(`epg-loading-${idx}`);
    if(el && el._hinted){ el.textContent = '⏳ Downloading guide data…'; }
  }, 30000);
  try {
    const r = await fetch('/api/epg', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item: ch}),
      signal: ctrl.signal
    });
    clearTimeout(timeoutId);
    clearTimeout(hintId);
    clearTimeout(slowHintId);
    const d = await r.json();

    // EPG download in progress — register row for batch refresh when XMLTV is ready
    if(d.error && d.error.toLowerCase().includes('loading')){
      const el = document.getElementById(`epg-loading-${idx}`);
      if(el){
        el._hinted = true;
        el.style.cssText += ';display:flex;align-items:center;padding-left:8px;font-size:10px;color:var(--t2);animation:none;background:var(--s4)';
        el.textContent = '⏳ EPG downloading…';
      }
      // Register this row in the shared waiting list — the single _epgXmltvPoller
      // will reload all waiting rows at once when the download completes.
      _epgRegisterWaiting(ch, idx);
      return;
    }
    // Reset retry counter on success
    if(_loadEpgRow._attempts){ const key = ch.stream_id || ch.id || idx; delete _loadEpgRow._attempts[key]; }

    const loadingEl = document.getElementById(`epg-loading-${idx}`);
    if(loadingEl) loadingEl.remove();
    const schedule = d.schedule || [];
    if(!schedule.length && (d.current || d.next)){
      // Only now/next available — show them as blocks
      if(d.current) schedule.push(d.current);
      if(d.next)    schedule.push(d.next);
    }
    if(!schedule.length){
      tl.insertAdjacentHTML('beforeend',
        `<div style="position:absolute;inset:0;display:flex;align-items:center;padding-left:8px">
          <span style="font-size:10px;color:var(--txt3);opacity:.6">No EPG data</span>
        </div>`);
      return;
    }

    const nowSec = Date.now() / 1000;
    const winStart = nowSec - EPG_WIN_BACK * 60;
    const winEnd   = nowSec + EPG_WIN_FWD  * 60;

    schedule.forEach(prog => {
      const pStart = prog.start || 0;
      const pEnd   = prog.end   || (pStart + 3600);
      // Clamp to visible window
      if(pEnd < winStart || pStart > winEnd) return;

      const x1 = Math.max(1, _epgTsToX(pStart));
      const x2 = Math.min(_epgTotalW(), _epgTsToX(pEnd));
      const w  = x2 - x1;
      if(w < 2) return;

      const isCurrent = pStart <= nowSec && nowSec < pEnd;
      const startLbl  = _fmtEpgTime(pStart);
      const endLbl    = _fmtEpgTime(pEnd);
      const progTitle = esc(prog.title || '—');

      const el = document.createElement('div');
      el.className = 'epg-prog' + (isCurrent ? ' now' : '');
      el.style.left  = x1 + 'px';
      el.style.width = w  + 'px';
      el.title = `${prog.title||'—'}\n${startLbl} – ${endLbl}${prog.desc ? '\n'+prog.desc : ''}`;
      el.onclick = (e) => { e.stopPropagation(); playItem(idx); };
      el.innerHTML = w > 30
        ? `<div class="epg-prog-title">${progTitle}</div>`
          + (w > 70 ? `<div class="epg-prog-time">${startLbl}–${endLbl}</div>` : '')
        : '';
      tl.appendChild(el);
    });
  } catch(e){
    clearTimeout(timeoutId);
    clearTimeout(hintId);
    clearTimeout(slowHintId);
    const loadingEl = document.getElementById(`epg-loading-${idx}`);
    if(loadingEl) loadingEl.remove();
    const msg = e.name === 'AbortError' ? 'EPG timeout' : 'EPG error';
    tl.insertAdjacentHTML('beforeend',
      `<div style="position:absolute;inset:0;display:flex;align-items:center;padding-left:8px">
        <span style="font-size:10px;color:var(--txt3);opacity:.5">${msg}</span>
      </div>`);
  }
}

// Show/hide EPG grid button based on mode and whether items are loaded
function _updateEpgGridBtn(){
  const btn = document.getElementById('epg-grid-btn');
  if(!btn) return;
  btn.style.display = (mode === 'live' && filtItems.length > 0) ? '' : 'none';
  // If grid is open but mode changed away from live, close it
  if(_epgGridActive && mode !== 'live') _closeEpgGrid();
}

// ── CATCH-UP TV ─────────────────────────────────────────────────────────────
// Catchup: uses /api/catchup to fetch past programme listings (Xtream: get_epg/XMLTV;
// MAC/Stalker: get_simple_data_table). Clicking a programme calls /api/catchup/play.

function showCatchup(){
  if(!_epgItem){toast('Play a live channel first','wrn');return;}
  document.getElementById('catchup-ch-name').textContent='↺ '+(_epgItem.name||'Catch-up TV');
  document.getElementById('catchup-status').textContent='';
  document.getElementById('catchup-body').innerHTML=
    '<div style="color:var(--txt3);font-size:12px;text-align:center;padding:20px">Loading past programmes…</div>';
  document.getElementById('catchup-overlay').style.display='flex';
  _loadCatchupEPG();
}

function closeCatchup(){document.getElementById('catchup-overlay').style.display='none';}
document.getElementById('catchup-overlay').addEventListener('click',function(e){if(e.target===this)closeCatchup();});

function _cuFmtTime(ts){const d=new Date(ts*1000);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}
function _cuFmtDate(ts){const d=new Date(ts*1000);return d.toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'});}

async function _loadCatchupEPG(){
  document.getElementById('catchup-body').innerHTML=
    '<div style="color:var(--txt3);font-size:12px;text-align:center;padding:20px">Loading past programmes…</div>';
  try{
    const now=Math.floor(Date.now()/1000);
    const r=await fetch('/api/catchup',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({item:_epgItem, start:now-86400*3, end:now})});
    const d=await r.json();

    if(d.archive_listings && d.archive_listings.length){
      _renderArchiveListings(d.archive_listings);
      return;
    }
    // No archive data — show manual time picker
    const errMsg=d.error||'No archived programme data found';
    document.getElementById('catchup-body').innerHTML=
      `<div style="color:var(--txt3);font-size:12px;text-align:center;padding:16px">${errMsg}</div>`
      +'<div style="padding:12px">'+_cuManualForm()+'</div>';
  }catch(e){
    document.getElementById('catchup-body').innerHTML=
      `<div style="color:var(--err);font-size:12px;text-align:center;padding:20px">Failed: ${e.message}</div>`
      +'<div style="padding:12px">'+_cuManualForm()+'</div>';
  }
}

let _cuListings = [];
function _renderArchiveListings(listings){
  _cuListings = listings;
  // Show all programmes; highlight archived ones. Non-archived are dimmed.
  let lastDate='';
  const rows=listings.map(p=>{
    const hasArchive=(p.mark_archive==='1'||p.mark_archive===1);
    const dateStr=p.start?_cuFmtDate(p.start):'';
    let dateHdr='';
    if(dateStr&&dateStr!==lastDate){
      lastDate=dateStr;
      dateHdr=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--acc);padding:8px 0 4px">${dateStr}</div>`;
    }
    const t=p.start&&p.stop?`${_cuFmtTime(p.start)}–${_cuFmtTime(p.stop)}`:(p.start?_cuFmtTime(p.start):'');
    const cmdSafe=encodeURIComponent(p.cmd||'');
    const liveCmdSafe=encodeURIComponent(p.live_cmd||'');
    const realIdSafe=encodeURIComponent(p.epg_id||p.id||'');
    const titleSafe=(p.title||'').replace(/'/g,"\\'");
    const opacity=hasArchive?'1':'0.4';
    const click=hasArchive
      ?`onclick="doPlayArchiveCmd('${cmdSafe}',${p.start||0},${p.stop||0},'${titleSafe}','${liveCmdSafe}','${realIdSafe}')"`
      :'';
    const extBtn=hasArchive
      ?`<button class="btn-ghost" onclick="event.stopPropagation();doExternalArchiveCmd('${cmdSafe}',${p.start||0},${p.stop||0},'${titleSafe}','${liveCmdSafe}','${realIdSafe}')" title="Play in external player" style="padding:0 6px;font-size:13px;flex-shrink:0">🎬</button>`
      :'';
    const cursor=hasArchive?'pointer':'default';
    const archIcon=hasArchive?'<span style="font-size:14px;color:var(--acc)">▶</span>':'';
    return dateHdr+`<div ${click}
      style="display:flex;align-items:center;gap:10px;padding:10px 8px;border-radius:var(--rsm);cursor:${cursor};
             border-left:3px solid var(--s4);margin-bottom:4px;background:var(--s3);
             transition:background .15s;opacity:${opacity}"
      ${hasArchive?'onmouseover="this.style.background=\'var(--s4)\'" onmouseout="this.style.background=\'var(--s3)\'"':''}>
      <span style="font-size:11px;color:var(--txt3);white-space:nowrap;min-width:90px">${t}</span>
      <span style="flex:1;font-size:12px;font-weight:600;color:var(--txt1)">${p.title||'Unknown'}</span>
      ${extBtn}
      ${archIcon}
    </div>`;
  }).join('');
  document.getElementById('catchup-body').innerHTML=
    rows+'<div style="padding-top:8px;border-top:1px solid var(--bdr)">'+_cuManualForm()+'</div>';
}

function doPlayArchiveCmd(encodedCmd, startTs, stopTs, title, encodedLiveCmd, encodedRealId){
  const cmd=decodeURIComponent(encodedCmd||'');
  const liveCmd=decodeURIComponent(encodedLiveCmd||'');
  const realId=decodeURIComponent(encodedRealId||'');
  const status=document.getElementById('catchup-status');
  if(status) status.textContent='Resolving…';
  fetch('/api/catchup/play',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd, live_cmd:liveCmd, epg_id:realId, start:startTs, stop:stopTs})})
  .then(r=>r.json()).then(d=>{
    if(d.url){
      closeCatchup();
      const label=(_epgItem?_epgItem.name:'')+' — '+title+' [↺]';
      // Pass raw URL — doPlay always wraps in /api/proxy itself
      // Catchup is VOD — isLive:false prevents mpegts.js SourceBuffer crash
      // d.fallback_url is the query-string format; used if path-based .ts fails
      doPlay(d.url, label, {isLive:false, fallbackUrl:d.fallback_url||null});
      toast('↺ Playing catch-up: '+title,'ok');
    } else {
      if(status) status.textContent='❌ '+(d.error||'Not available');
    }
  }).catch(e=>{if(status) status.textContent='❌ '+e.message;});
}

async function doExternalArchiveCmd(encodedCmd, startTs, stopTs, title, encodedLiveCmd, encodedRealId){
  const cmd=decodeURIComponent(encodedCmd||'');
  const liveCmd=decodeURIComponent(encodedLiveCmd||'');
  const realId=decodeURIComponent(encodedRealId||'');
  const status=document.getElementById('catchup-status');
  if(status) status.textContent='Resolving for external player…';
  try{
    const r=await fetch('/api/catchup/play',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cmd, live_cmd:liveCmd, epg_id:realId, start:startTs, stop:stopTs})});
    const d=await r.json();
    if(!d.url){if(status) status.textContent='❌ '+(d.error||'Not available');return;}
    const url=d.url;
    if(_isMobile){
      const player=localStorage.getItem('mobile_player')||'ask';
      if(player==='copy'){
        try{await navigator.clipboard.writeText(url);toast('Stream URL copied!','ok');}
        catch(e){prompt('Copy stream URL:',url);}
        if(status) status.textContent='';
        return;
      }
      if(status) status.textContent='';
      window.location.href=player==='ask'
        ?`intent:${url}#Intent;type=video/*;S.browser_fallback_url=about:blank;end`
        :`intent:${url}#Intent;package=${player};type=video/*;S.browser_fallback_url=about:blank;end`;
    } else {
      const exe=(localStorage.getItem('ext_player')||'').trim();
      if(!exe){toast('Set external player path in ⚙ settings first','wrn');return;}
      const r2=await fetch('/api/open_external',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({exe, url})});
      const d2=await r2.json();
      if(d2.error) toast('Error: '+d2.error,'err');
      else{ toast('Launched: '+title,'ok'); if(status) status.textContent=''; }
    }
  }catch(e){if(status) status.textContent='❌ '+e.message;}
}

function _cuManualForm(){
  const now=new Date(), ago=new Date(now-3600000);
  const pad=n=>String(n).padStart(2,'0');
  const fmt=d=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes());
  return `<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--txt3);margin-bottom:6px">Manual time range</div>`
    +`<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">`
    +`<input id="cu-start" type="datetime-local" value="${fmt(ago)}" style="height:32px;font-size:11px;background:var(--s3);border:1px solid var(--bdr2);border-radius:var(--rsm);color:var(--txt1);padding:0 6px;flex:1;min-width:130px">`
    +`<input id="cu-end"   type="datetime-local" value="${fmt(now)}" style="height:32px;font-size:11px;background:var(--s3);border:1px solid var(--bdr2);border-radius:var(--rsm);color:var(--txt1);padding:0 6px;flex:1;min-width:130px">`
    +`<button class="btn-acc" onclick="doWatchCatchupManual()" style="height:32px;padding:0 14px;font-size:12px">▶ Watch</button>`
    +`</div>`;
}

function doWatchCatchupManual(){
  const s=document.getElementById('cu-start')?.value;
  const e=document.getElementById('cu-end')?.value;
  if(!s||!e){toast('Set start and end time','wrn');return;}
  const startTs=Math.floor(new Date(s).getTime()/1000);
  const endTs=Math.floor(new Date(e).getTime()/1000);
  if(endTs<=startTs){toast('End must be after start','wrn');return;}
  // Find the matching programme and delegate to doPlayArchiveCmd — exactly
  // the same call that clicking a programme row makes.
  const match=_cuListings.find(p=>p.start&&p.stop&&p.start<=startTs&&startTs<p.stop)
    ||_cuListings.find(p=>p.start&&Math.abs(p.start-startTs)<300);
  // For Xtream: stream_id is the correct cmd value for timeshift.
  // _epgItem.cmd is the full stream URL (not useful here); prefer stream_id.
  const liveCmd=_epgItem?.stream_id||_epgItem?.cmd||'';
  const cmd=encodeURIComponent(match?.cmd||liveCmd);
  const live_cmd=encodeURIComponent(match?.live_cmd||liveCmd);
  const epg_id=encodeURIComponent(match?.epg_id||match?.id||'');
  const title=match?.title||'';
  const useStop=match?.stop||endTs;
  doPlayArchiveCmd(cmd, startTs, useStop, title, live_cmd, epg_id);
}


async function startRec(){
  if(!pUrl){toast('Play a stream first','wrn');return;}
  const od=document.getElementById('o-dir').value.trim();
  const r=await fetch('/api/record/start',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:pUrl, name:pName, out_dir:od})});
  const d=await r.json();
  if(!d.ok){toast(d.error||'Record failed','err');return;}
  isRec=true;
  _syncRecBtn(true);
  document.getElementById('rfname').textContent=d.filename||'';
  const rfmob=document.getElementById('rfname-mob'); if(rfmob) rfmob.textContent=d.filename||'';
  const adrFname=document.getElementById('adr-rec-fname');
  if(adrFname) adrFname.textContent=d.filename||'';
  toast('⏺ Recording: '+(d.filename||''),'ok');
  let s=0;
  recTmr=setInterval(()=>{
    s++;
    const h=String(Math.floor(s/3600)).padStart(2,'0');
    const m2=String(Math.floor(s%3600/60)).padStart(2,'0');
    const sc=String(s%60).padStart(2,'0');
    const ts=h+':'+m2+':'+sc;
    document.getElementById('rtimer').textContent=ts;
    const rtmob=document.getElementById('rtimer-mob');
    if(rtmob) rtmob.textContent=ts;
    const adrTimer=document.getElementById('adr-rec-timer');
    if(adrTimer) adrTimer.textContent=ts;
    // Keep button text in sync with elapsed time
    const btn=document.getElementById('rbtn');
    if(btn) btn.textContent=`⏹ Stop Recording ${ts}`;
    const adrBtn=document.getElementById('adr-rec-btn');
    if(adrBtn) adrBtn.textContent=`⏹ Stop Recording ${ts}`;
  },1000);
}

async function stopRec(){
  const r=await fetch('/api/record/stop',{method:'POST',
    headers:{'Content-Type':'application/json'},body:'{}'});
  const d=await r.json();
  if(d.ok) toast('Saved: '+(d.file||''),'ok');
  isRec=false;
  _syncRecBtn(false);
  document.getElementById('rfname').textContent='';
  const rfmob2=document.getElementById('rfname-mob'); if(rfmob2) rfmob2.textContent='';
  const adrFname=document.getElementById('adr-rec-fname');
  if(adrFname) adrFname.textContent='';
  const adrTimer=document.getElementById('adr-rec-timer');
  if(adrTimer) adrTimer.textContent='00:00:00';
  if(recTmr){clearInterval(recTmr);recTmr=null;}
}

function _syncRecBtn(recording){
  const btn=document.getElementById('rbtn');
  const btnMob=document.getElementById('rbtn-mob');
  const timer=document.getElementById('rtimer');
  const timerMob=document.getElementById('rtimer-mob');
  const adrBtn=document.getElementById('adr-rec-btn');
  const adrInfo=document.getElementById('adr-rec-info');
  if(btn){
    if(recording){
      btn.textContent='⏹ Stop Recording';
      btn.classList.add('rec');
      if(timer) timer.classList.add('vis');
    } else {
      btn.textContent='⏺ Record';
      btn.classList.remove('rec');
      if(timer){timer.classList.remove('vis'); timer.textContent='00:00:00';}
    }
  }
  if(btnMob){
    if(recording){
      btnMob.textContent='⏹ Stop';
      btnMob.classList.add('rec');
      if(timerMob) timerMob.classList.add('vis');
    } else {
      btnMob.textContent='⏺ Record';
      btnMob.classList.remove('rec');
      if(timerMob){timerMob.classList.remove('vis'); timerMob.textContent='00:00:00';}
    }
  }
  if(adrBtn){
    if(recording){
      adrBtn.textContent='⏹ Stop Recording';
      adrBtn.classList.add('rec');
    } else {
      adrBtn.textContent='⏺ Record';
      adrBtn.classList.remove('rec');
    }
  }
  if(adrInfo) adrInfo.classList.toggle('vis', !!recording);
}

// ── DOWNLOADS ──────────────────────────────────────────────
// Show the progress panel immediately (before the server responds)
// so even very fast exports are always visible.
function _showProgressNow(ctx, title, label, total){
  const panel=document.getElementById("adr-progress-"+ctx); if(!panel) return;
  panel.classList.add("active");
  const titleEl=document.getElementById("adr-prog-"+ctx+"-title");
  const labelEl=document.getElementById("adr-prog-"+ctx+"-label");
  const bar=document.getElementById("adr-prog-"+ctx+"-bar");
  const countEl=document.getElementById("adr-prog-"+ctx+"-count");
  const speedEl=document.getElementById("adr-prog-"+ctx+"-speed");
  const stopBtn=document.getElementById("adr-prog-"+ctx+"-stop");
  const dismissBtn=document.getElementById("adr-prog-"+ctx+"-dismiss");
  if(titleEl) titleEl.textContent=title;
  if(labelEl) labelEl.textContent=label;
  if(bar){ bar.style.width="0%"; bar.style.animation="adr-indeterminate 1.2s linear infinite"; bar.style.opacity="0.55"; }
  if(countEl) countEl.textContent=total>0?`0 / ${total} items`:"Starting…";
  if(speedEl) speedEl.textContent="";
  if(stopBtn) stopBtn.style.display="";
  if(dismissBtn) dismissBtn.style.display="none";
  // Always open the drawer to the right context so progress is visible on all screen sizes
  openDrawer(ctx);
}

async function dlM3U(){
  const op=document.getElementById('o-m3u').value.trim();
  if(!op){toast('Set M3U output path first','wrn');return;}
  if(!selSet.size){toast('Select items first','wrn');return;}
  setBusy(true);
  _showProgressNow('items','💾 Saving M3U…', curCat?curCat.title:'', selSet.size);
  const r=await fetch('/api/download/m3u',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items:[...selSet],category:curCat,mode,out_path:op,total_hint:selSet.size})});
  const d=await r.json();
  d.ok?(toast(d.message,'ok'),pollBusy()):(toast(d.error,'err'),setBusy(false),dismissProgress('items'));
}

// Mobile MKV button — opens Actions drawer if download in progress, else downloads
window._mobMkvClick = function(){
  const stopBtn = document.getElementById('stopbtn');
  if(stopBtn && !stopBtn.disabled) openActTab();  // busy = stopbtn enabled
  else dlNowMKV();
};

async function dlNowMKV(){
  if(!pUrl){toast('No stream playing','wrn');return;}
  const od=document.getElementById('o-dir').value.trim();
  if(!od){toast('Set output folder first','wrn');return;}
  // Build a minimal item from the currently playing stream
  const nowItem = (pIdx>=0 && filtItems[pIdx]) ? filtItems[pIdx] : {name:pName, _direct_url:pUrl};
  setBusy(true);
  _showProgressNow('items','⬇ Downloading MKV…', nowItem.name||pName, 1);
  const r=await fetch('/api/download/mkv',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items:[nowItem],category:curCat,mode,out_dir:od,use_fallback:true})});
  const d=await r.json();
  d.ok?(toast(d.message,'ok'),pollBusy()):(toast(d.error,'err'),setBusy(false),dismissProgress('items'));
}

async function dlMKV(){
  const od=document.getElementById('o-dir').value.trim();
  if(!od){toast('Set output folder first','wrn');return;}
  if(!selSet.size){toast('Select items first','wrn');return;}
  setBusy(true);
  _showProgressNow('items','⬇ Downloading MKV…', curCat?curCat.title:'', selSet.size);
  const r=await fetch('/api/download/mkv',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items:[...selSet],category:curCat,mode,out_dir:od,
      use_fallback:true})});
  const d=await r.json();
  d.ok?(toast(d.message,'ok'),pollBusy()):(toast(d.error,'err'),setBusy(false),dismissProgress('items'));
}

async function dlCat(){
  const op=document.getElementById('o-m3u').value.trim();
  if(!op){toast('Set M3U output path first','wrn');return;}
  if(!curCat){toast('Select a category first','wrn');return;}
  setBusy(true);
  _showProgressNow('items','💾 Saving M3U…', curCat.title, allItems.length);
  const r=await fetch('/api/download/m3u',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items:null,category:curCat,mode,out_path:op,total_hint:allItems.length})});
  const d=await r.json();
  d.ok?(toast(d.message,'ok'),pollBusy()):(toast(d.error,'err'),setBusy(false),dismissProgress('items'));
}

// ── STOP ───────────────────────────────────────────────────
async function doStop(){
  await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  setBusy(false); toast('Stopped','info');
  _dlActive=false; _dlTaskType=''; _dlItemNames=[];
  _refreshDlButtons();
}

// ── POLLING ────────────────────────────────────────────────
async function pollBusy(){
  const r=await fetch('/api/status').catch(()=>null); if(!r) return;
  const d=await r.json().catch(()=>null); if(!d) return;
  if(d.status) setStatus(d.status);
  updateTaskProgress(d);
  _syncDlState(d);
  if(d.busy){
    setTimeout(pollBusy,800);
  } else {
    setBusy(false);
    // Fetch final authoritative numbers before freezing the panel
    const lastStatus = await fetch('/api/status').then(r=>r.json()).catch(()=>({}));
    const finalDone    = lastStatus.task_done    || 0;
    const finalTotal   = lastStatus.task_total   || 0;
    const finalSkipped = lastStatus.task_skipped || 0;
    ["cats","items"].forEach(ctx=>{
      const panel=document.getElementById("adr-progress-"+ctx);
      if(panel && panel.classList.contains("active")){
        const titleEl=document.getElementById("adr-prog-"+ctx+"-title");
        const bar=document.getElementById("adr-prog-"+ctx+"-bar");
        const speedEl=document.getElementById("adr-prog-"+ctx+"-speed");
        const countEl=document.getElementById("adr-prog-"+ctx+"-count");
        const stopBtn=document.getElementById("adr-prog-"+ctx+"-stop");
        const dismissBtn=document.getElementById("adr-prog-"+ctx+"-dismiss");
        if(titleEl) titleEl.textContent="✓ Done";
        if(bar){ bar.style.width="100%"; bar.style.animation=""; bar.style.opacity="1"; }
        if(speedEl) speedEl.textContent="";
        // Always overwrite count with the real final numbers
        if(countEl){
          const skipTxt = finalSkipped > 0 ? ` · ${finalSkipped} skipped` : "";
          countEl.textContent = finalTotal > 0
            ? `${finalDone} / ${finalTotal} items${skipTxt}`
            : (finalDone > 0 ? `${finalDone} items${skipTxt}` : "Complete");
        }
        if(stopBtn) stopBtn.style.display="none";
        if(dismissBtn) dismissBtn.style.display="";
      }
    });
  }
}
function dismissProgress(ctx){
  const panel=document.getElementById("adr-progress-"+ctx);
  if(!panel) return;
  panel.classList.remove("active");
  // Reset for next run
  const stopBtn=document.getElementById("adr-prog-"+ctx+"-stop");
  const dismissBtn=document.getElementById("adr-prog-"+ctx+"-dismiss");
  if(stopBtn) stopBtn.style.display="";
  if(dismissBtn) dismissBtn.style.display="none";
}
function updateTaskProgress(d){
  const type     = d.task_type       || "";
  const done     = d.task_done       || 0;
  const total    = d.task_total      || 0;
  const label    = d.task_label      || "";
  const filePct  = d.task_file_pct   || 0;
  const elapsed  = d.task_file_elapsed || "";
  const speed    = d.task_speed      || "";
  const active   = type !== "";

  let barPct, countTxt, speedTxt, indeterminate;

  if(type === "mkv"){
    // For MKV: bar = per-file download progress from ffmpeg
    const hasDuration = filePct > 0;
    indeterminate = !hasDuration;
    barPct   = hasDuration ? filePct : 0;
    // Item counter: "File 1 / 3" — shown in count area
    const itemTxt = total > 1 ? `File ${done+1} / ${total}` : (total===1 ? "Downloading…" : "Resolving…");
    // Elapsed time if available
    const elapsedTxt = elapsed ? ` · ${elapsed}` : "";
    countTxt = itemTxt + elapsedTxt;
    speedTxt = speed;
  } else if(type === "m3u"){
    // For M3U: bar = items saved / total
    const skipped = d.task_skipped || 0;
    const hasTot = total > 0;
    indeterminate = !hasTot;
    barPct   = hasTot ? Math.round(done / total * 100) : 0;
    const skipTxt = skipped > 0 ? ` · ${skipped} skipped` : "";
    countTxt = hasTot ? `${done} / ${total} items${skipTxt}` : (done > 0 ? `${done} items saved${skipTxt}` : "Starting…");
    speedTxt = "";
  } else {
    indeterminate = false; barPct = 0; countTxt = ""; speedTxt = "";
  }

  ["cats","items"].forEach(ctx => {
    const panel = document.getElementById("adr-progress-"+ctx);
    if(!panel) return;
    if(active){
      panel.classList.add("active");
      // Reset stop/dismiss to "running" state when a new task starts
      const stopBtn=document.getElementById("adr-prog-"+ctx+"-stop");
      const dismissBtn=document.getElementById("adr-prog-"+ctx+"-dismiss");
      if(stopBtn && stopBtn.style.display==="none"){ stopBtn.style.display=""; }
      if(dismissBtn && dismissBtn.style.display!=="" && d.busy){ dismissBtn.style.display="none"; }
      const title  = type === "mkv" ? "⬇ Downloading MKV…" : "💾 Saving M3U…";
      document.getElementById("adr-prog-"+ctx+"-title").textContent = title;
      document.getElementById("adr-prog-"+ctx+"-label").textContent = label;
      const bar = document.getElementById("adr-prog-"+ctx+"-bar");
      if(indeterminate){
        bar.style.width = "40%";
        bar.style.opacity = "0.55";
        bar.style.animation = "adr-indeterminate 1.2s linear infinite";
      } else {
        bar.style.width   = barPct + "%";
        bar.style.opacity = "1";
        bar.style.animation = "";
      }
      document.getElementById("adr-prog-"+ctx+"-count").textContent = countTxt;
      const speedEl = document.getElementById("adr-prog-"+ctx+"-speed");
      if(speedEl) speedEl.textContent = speedTxt;
    }
    // Never auto-hide here — only pollBusy (Done state) and dismissProgress (✕) hide the panel.
  });
}
setInterval(async()=>{
  const r=await fetch('/api/status').catch(()=>null); if(!r) return;
  const d=await r.json().catch(()=>null); if(!d) return;
  if(d.status) setStatus(d.status);
  if(!d.busy) setBusy(false);
  updateTaskProgress(d);
  _syncDlState(d);
  // Sync recording button if server state differs from JS state (e.g. page reload)
  if(d.recording && !isRec){
    isRec=true; _syncRecBtn(true);
    // Resync elapsed time from server
    fetch('/api/record/status').then(r=>r.json()).then(rs=>{
      if(rs.recording){
        document.getElementById('rfname').textContent=rs.filename||'';
        const adrFname=document.getElementById('adr-rec-fname');
        if(adrFname) adrFname.textContent=rs.filename||'';
        // Restart timer from server elapsed
        if(recTmr){clearInterval(recTmr);recTmr=null;}
        const parts=(rs.elapsed||'00:00:00').split(':').map(Number);
        let s=parts[0]*3600+parts[1]*60+parts[2];
        recTmr=setInterval(()=>{
          s++;
          const h=String(Math.floor(s/3600)).padStart(2,'0');
          const m2=String(Math.floor(s%3600/60)).padStart(2,'0');
          const sc=String(s%60).padStart(2,'0');
          const ts=h+':'+m2+':'+sc;
          document.getElementById('rtimer').textContent=ts;
          const adrTimer=document.getElementById('adr-rec-timer');
          if(adrTimer) adrTimer.textContent=ts;
          const btn=document.getElementById('rbtn');
          if(btn) btn.textContent=`⏹ Stop Recording ${ts}`;
          const adrBtn=document.getElementById('adr-rec-btn');
          if(adrBtn) adrBtn.textContent=`⏹ Stop Recording ${ts}`;
        },1000);
      }
    }).catch(()=>{});
  } else if(!d.recording && isRec){
    isRec=false; _syncRecBtn(false);
    document.getElementById('rfname').textContent='';
    if(recTmr){clearInterval(recTmr);recTmr=null;}
  }
},5000);

// ── SSE LOGS ───────────────────────────────────────────────
function startLog(){
  if(logEs) logEs.close();
  logEs=new EventSource('/api/logs');
  logEs.onmessage=e=>{
    const msg=e.data;
    if(msg==='Connected to log stream') return;
    let c='';
    if(msg.includes('[STATUS]')){c='s'; setStatus(msg.replace(/\[STATUS\]\s*/,''));}
    else if(/✓|success|saved|Done/i.test(msg)) c='k';
    else if(/✗|error|failed|ERROR/i.test(msg)) c='e';
    else if(/warn|⚠/i.test(msg)) c='w';
    else if(/\[MKV\]|\[SERIES\]|\[REC\]/i.test(msg)) c='m';
    else if(/▶|Playing/i.test(msg)) c='i';
    alog(msg.replace(/\[STATUS\]\s*/,'').trim(),c);
  };
  logEs.onerror=()=>setTimeout(startLog,3000);
}

// ── HELPERS ────────────────────────────────────────────────
// Log entries are buffered and flushed once per animation frame.
// This prevents the forced synchronous reflow (scrollHeight read) from
// blocking the main thread on every incoming SSE message.
let _logBuf = [];
let _logRafPending = false;

function _flushLog(){
  _logRafPending = false;
  if(!_logBuf.length) return;
  const entries = _logBuf.splice(0);
  ['logout','desktop-logout'].forEach(id=>{
    const out = document.getElementById(id); if(!out) return;
    const frag = document.createDocumentFragment();
    entries.forEach(({msg, cls})=>{
      const d = document.createElement('div');
      d.className = 'll' + (cls ? ' l'+cls : '');
      d.textContent = msg;
      frag.appendChild(d);
    });
    out.appendChild(frag);
    // Trim to 600 lines
    while(out.children.length > 600) out.removeChild(out.firstChild);
    // Single scroll — reads scrollHeight only once per frame
    out.scrollTop = out.scrollHeight;
  });
}

function alog(msg, cls){
  _logBuf.push({msg, cls});
  if(!_logRafPending){
    _logRafPending = true;
    requestAnimationFrame(_flushLog);
  }
}
function clearLog(){
  ['logout','desktop-logout'].forEach(id=>{
    const el=document.getElementById(id); if(el) el.innerHTML='';
  });
}
function toggleDesktopLog(){
  const panel = document.getElementById('desktop-log');
  if(!panel) return;
  const expanded = panel.classList.toggle('expanded');
  // After expand, scroll log to bottom
  if(expanded){
    const out = document.getElementById('desktop-logout');
    if(out) setTimeout(()=>{ out.scrollTop = out.scrollHeight; }, 260);
  }
}
function setStatus(m){document.getElementById('hdr-status').textContent=m;}
function setBusy(v){
  document.getElementById('busy-sp').classList.toggle('hidden',!v);
  document.getElementById('cbtn').disabled=v;
  document.getElementById('stopbtn').disabled=!v;
}

// ── DOWNLOAD-AWARE BUTTON SYNC ──────────────────────────────
// Called whenever we receive a fresh /api/status payload.
// Updates _dlActive/_dlTaskType/_dlItemNames and refreshes the two
// "Download MKV" buttons that live outside the Action drawer:
//   • dl-now-btn  — in the Player controls bar
//   • imenu-mkv   — in the item context menu
function _syncDlState(d){
  _dlActive    = !!(d.busy && d.task_type);
  _dlTaskType  = d.task_type || '';
  _dlItemNames = Array.isArray(d.task_item_names) ? d.task_item_names : [];
  _refreshDlButtons();
}

function _refreshDlButtons(){
  const mkvRunning = _dlActive && _dlTaskType === 'mkv';

  // ── dl-now-btn (Player controls bar) ─────────────────────
  const dnBtn = document.getElementById('dl-now-btn');
  if(dnBtn){
    if(mkvRunning){
      dnBtn.innerHTML = '⏹ Stop';
      dnBtn.title     = 'Stop current MKV download';
      dnBtn.onclick   = ()=>doStop();
      dnBtn.disabled  = false;
      dnBtn.style.color       = 'var(--acc,#f87171)';
      dnBtn.style.borderColor = 'var(--acc,#f87171)';
    } else {
      dnBtn.innerHTML = '⬇ MKV';
      dnBtn.title     = 'Download currently playing item as MKV';
      dnBtn.onclick   = ()=>dlNowMKV();
      dnBtn.disabled  = !pUrl;
      dnBtn.style.color       = '';
      dnBtn.style.borderColor = '';
    }
  }

  // ── dl-now-btn-mob (mobile Player controls bar) ───────────
  const dnBtnMob = document.getElementById('dl-now-btn-mob');
  if(dnBtnMob){
    if(mkvRunning){
      dnBtnMob.innerHTML = '⏹ Stop';
      dnBtnMob.title     = 'Stop current MKV download';
      dnBtnMob.onclick   = ()=>doStop();
      dnBtnMob.disabled  = false;
      dnBtnMob.style.color       = 'var(--acc,#f87171)';
      dnBtnMob.style.borderColor = 'var(--acc,#f87171)';
    } else {
      dnBtnMob.innerHTML = '⬇ MKV';
      dnBtnMob.title     = 'Download currently playing item as MKV';
      dnBtnMob.onclick   = ()=>dlNowMKV();
      dnBtnMob.disabled  = !pUrl;
      dnBtnMob.style.color       = '';
      dnBtnMob.style.borderColor = '';
    }
  }

  // ── imenu-mkv (item context menu) ────────────────────────
  const imBtn = document.getElementById('imenu-mkv');
  if(!imBtn) return;
  if(mkvRunning){
    imBtn.innerHTML = '<span class="imenu-ico">⏹</span>Stop Download';
    imBtn.onclick   = ()=>{ closeItemMenu(); doStop(); };
    imBtn.style.color = 'var(--acc,#f87171)';
  } else {
    imBtn.innerHTML = '<span class="imenu-ico">⬇</span>Download MKV';
    imBtn.onclick   = iMenuMKV;
    imBtn.style.color = '';
  }
}
// Stub overwritten by orientation manager on mobile
window._orientOnTabSwitch = function(){};
function showT(pid,tid){
  if(window.innerWidth>=900) return;
  _switchTab(pid,tid);
}
function forceTab(pid,tid){
  // always switch on mobile regardless of current state
  if(window.innerWidth>=900) return;
  _switchTab(pid,tid);
}
function _switchTab(pid,tid){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  const panel=document.getElementById(pid);
  if(panel) panel.classList.add('active');
  document.querySelectorAll('.nt').forEach(b=>b.classList.remove('on'));
  const tab=document.getElementById(tid);
  if(tab) tab.classList.add('on');
  _orientOnTabSwitch(pid);
}
function toast(msg,type){
  const el=document.createElement('div');
  const map={ok:'tok2',err:'terr2',info:'tinfo',wrn:'twrn2'};
  el.className='toast '+(map[type]||'tinfo');
  el.textContent=msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(()=>{el.style.opacity='0';el.style.transform='translateY(8px)';
    el.style.transition='.3s'; setTimeout(()=>el.remove(),300);},2700);
}
function togSug(w){
  const el=document.getElementById('sg-'+w);
  const was=el.classList.contains('open');
  document.querySelectorAll('.psug').forEach(e=>e.classList.remove('open'));
  if(!was) el.classList.add('open');
}
function pickP(w,v){
  document.getElementById({m3u:'o-m3u',dir:'o-dir'}[w]).value=v;
  document.getElementById('sg-'+w).classList.remove('open');
  if(w==='dir') saveFP();
}
document.addEventListener('click',e=>{
  if(!e.target.closest('.prow'))
    document.querySelectorAll('.psug').forEach(el=>el.classList.remove('open'));
});
function saveFP(){
  try{localStorage.setItem('mkv_folder',document.getElementById('o-dir').value);}catch(e){}
  try{localStorage.setItem('m3u_path',document.getElementById('o-m3u').value);}catch(e){}
}
function saveExtPlayer(){
  try{localStorage.setItem('ext_player',document.getElementById('o-extplayer').value);}catch(e){}
}
function saveSubKey(){
  try{localStorage.setItem('opensubtitles_key',document.getElementById('o-subkey').value.trim());}catch(e){}
}
function _getSubKey(){
  try{return localStorage.getItem('opensubtitles_key')||'';}catch(e){return '';}
}
function saveMobilePlayer(){
  try{localStorage.setItem('mobile_player',document.getElementById('o-mobile-player').value);}catch(e){}
}
async function browseExtPlayer(){
  try{
    const r=await fetch('/api/browse_exe'); const d=await r.json();
    if(d.path){
      document.getElementById('o-extplayer').value=d.path;
      saveExtPlayer();
      toast('External player set: '+d.path.split(/[\\/]/).pop(),'ok');
    }
  }catch(e){toast('Browse failed: '+e,'err');}
}
const _isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
  || ('ontouchstart' in window)
  || (navigator.maxTouchPoints > 1);

/* ─── Orientation manager (mobile only) ──────────────────────────────────────
   • On player tab  : unlock orientation; auto-fullscreen on landscape rotation.
   • On other tabs  : lock to portrait so rotation is disabled.
   • Exiting fullscreen (back button / swipe down) while landscape → lock portrait
     so the device doesn't immediately re-trigger fullscreen.
────────────────────────────────────────────────────────────────────────────── */
(function(){
  if(!_isMobile) return;                          // desktop — do nothing
  const SO = window.screen && screen.orientation; // ScreenOrientation API
  if(!SO) return;                                 // very old WebView — bail

  let _onPlayerTab = false;

  function _isLandscape(){
    const t = SO.type || '';
    if(t) return t.startsWith('landscape');
    // fallback: compare dimensions
    return window.innerWidth > window.innerHeight;
  }

  function _lockPortrait(){
    try{ SO.lock('portrait').catch(()=>{}); }catch(e){}
  }

  function _unlock(){
    try{ SO.unlock(); }catch(e){}
  }

  function _enterFullscreen(){
    const el = document.getElementById('vid') || document.querySelector('video');
    if(!el) return;
    const req = el.requestFullscreen || el.webkitRequestFullscreen
              || el.mozRequestFullScreen || el.msRequestFullscreen;
    if(req) req.call(el).catch(()=>{});
  }

  function _exitFullscreen(){
    const exit = document.exitFullscreen || document.webkitExitFullscreen
               || document.mozCancelFullScreen || document.msExitFullscreen;
    const inFS  = document.fullscreenElement || document.webkitFullscreenElement;
    if(exit && inFS) exit.call(document).catch(()=>{});
  }

  // Called by _switchTab on every tab change
  window._orientOnTabSwitch = function(pid){
    _onPlayerTab = (pid === 'p-player');
    if(_onPlayerTab){
      _unlock();
      // If already landscape when arriving on player tab → go fullscreen
      if(_isLandscape()) _enterFullscreen();
    } else {
      _exitFullscreen();
      _lockPortrait();
    }
  };

  // Fires whenever the physical device rotates
  SO.addEventListener('change', function(){
    if(!_onPlayerTab){ _lockPortrait(); return; }
    if(_isLandscape()){
      _enterFullscreen();
    } else {
      _exitFullscreen();
    }
  });

  // User manually exits fullscreen (back button / swipe-down) while landscape
  // → lock portrait so it doesn't immediately bounce back into fullscreen
  document.addEventListener('fullscreenchange', function(){
    const inFS = document.fullscreenElement || document.webkitFullscreenElement;
    if(!inFS && _onPlayerTab && _isLandscape()){
      _lockPortrait();
      // Give the OS a moment to settle orientation before we unlock again
      setTimeout(()=>{ if(_onPlayerTab) _unlock(); }, 1200);
    }
  });

  // Lock portrait on startup until the player tab is explicitly opened
  _lockPortrait();
})();

async function openExternal(i){
  const it=filtItems[i]; if(!it) return;
  const name=it.name||it.o_name||'?';

  if(_isMobile){
    toast('Resolving stream…','info');
    try{
      const r=await fetch('/api/resolve_url',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({item:it,mode,category:curCat||{}})});
      const d=await r.json();
      if(d.error){toast('Error: '+d.error,'err');return;}
      const url=d.url;
      const player=localStorage.getItem('mobile_player')||'ask';
      if(player==='copy'){
        try{await navigator.clipboard.writeText(url);toast('Stream URL copied!','ok');}
        catch(e){prompt('Copy stream URL:',url);}
        return;
      }
      if(player==='ask'){
        // No package → Android shows only installed handlers
        // S.browser_fallback_url=about:blank prevents Play Store from opening
        window.location.href=`intent:${url}#Intent;type=video/*;S.browser_fallback_url=about:blank;end`;
      } else {
        // Direct to specific app — S.browser_fallback_url=about:blank prevents Play Store if not installed
        window.location.href=`intent:${url}#Intent;package=${player};type=video/*;S.browser_fallback_url=about:blank;end`;
      }
    }catch(e){toast('Failed: '+e,'err');}
    return;
  }

  // Desktop — original subprocess path
  const exe=(localStorage.getItem('ext_player')||'').trim();
  if(!exe){toast('Set external player path in ⚙ settings first','wrn');return;}
  toast('Opening in external player…','info');
  try{
    const r=await fetch('/api/open_external',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({exe,item:it,mode,category:curCat||{}})});
    const d=await r.json();
    if(d.error) toast('Error: '+d.error,'err');
    else toast('Launched: '+name,'ok');
  }catch(e){toast('Failed: '+e,'err');}
}

function esc(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── ACTION DRAWER ──────────────────────────────────────────
let drawerCtx = 'cats';
function openActTab(){
  // Detect context from active panel
  const active = document.querySelector('.panel.active');
  const pid = active ? active.id : 'p-cats';
  const ctx = pid==='p-items'||pid==='p-player' ? 'items' : 'cats';
  openDrawer(ctx);
}
function openDrawer(ctx){
  drawerCtx = ctx||'cats';
  document.getElementById('adr-cats-content').classList.toggle('hidden', drawerCtx!=='cats');
  document.getElementById('adr-items-content').classList.toggle('hidden', drawerCtx!=='items');
  document.getElementById('adr-title').textContent = drawerCtx==='cats'
    ? '⚡ Category Actions' : '⚡ Item Actions';
  document.getElementById('act-overlay').classList.add('open');
  document.getElementById('act-drawer').classList.add('open');
  const tact = document.getElementById('t-act');
  if(tact) tact.classList.add('act-open');
}
function closeDrawer(){
  document.getElementById('act-overlay').classList.remove('open');
  document.getElementById('act-drawer').classList.remove('open');
  const tact = document.getElementById('t-act');
  if(tact) tact.classList.remove('act-open');
}

// ── SAVED PLAYLISTS ────────────────────────────────────────
let plEditId = null, plCT = 'mac';

function openPL(){
  document.getElementById('pl-overlay').classList.add('open');
  renderPLList();
}
function closePL(){
  document.getElementById('pl-overlay').classList.remove('open');
}

function plSetCT(t){
  plCT=t;
  document.querySelectorAll('.pl-ct-btn').forEach(b=>
    b.className=b.dataset.t===t?'btn-acc pl-ct-btn':'btn-ghost pl-ct-btn');
  ['plf-mac','plf-xtream','plf-m3u'].forEach(id=>
    document.getElementById(id).classList.add('hidden'));
  document.getElementById({mac:'plf-mac',xtream:'plf-xtream',m3u_url:'plf-m3u'}[t])
    .classList.remove('hidden');
}

function plLoadAll(){
  try{return JSON.parse(localStorage.getItem('playlists')||'[]');}catch(e){return [];}
}
function plSaveAll(arr){
  try{localStorage.setItem('playlists',JSON.stringify(arr));}catch(e){}
}

function renderPLList(){
  const arr=plLoadAll();
  const el=document.getElementById('pl-list');
  if(!arr.length){
    el.innerHTML='<div class="pl-empty"><span>📋</span>No saved playlists yet.<br>Add one below.</div>';
    return;
  }
  const icons={mac:'🔌',xtream:'📡',m3u_url:'📄'};
  const typeAccent={mac:'#3b82f6',xtream:'#22c55e',m3u_url:'#ef4444'};
  const typeLbl={mac:'MAC',xtream:'XTREAM',m3u_url:'M3U'};
  const typeCls={mac:'pli-type-mac',xtream:'pli-type-xtream',m3u_url:'pli-type-m3u'};
  el.innerHTML=arr.map((p,i)=>{
    const t=p.type||'mac';
    const ico=icons[t]||'📡';
    const accent=typeAccent[t]||'var(--bdr)';
    const sub=t==='mac'?p.url+' • '+p.mac
      :t==='xtream'?p.url+' • '+p.username
      :p.m3u_url||p.url||'';
    return '<div class="pli" style="--delay:'+(i*.04)+'s;--pli-accent:'+accent+'">'
      +'<span class="pli-ico">'+ico+'</span>'
      +'<div class="pli-info"><div class="pli-name" style="display:flex;align-items:center;gap:6px">'
      +'<span>'+esc(p.name||'Untitled')+'</span>'
      +'<span class="pli-type-badge '+(typeCls[t]||'pli-type-mac')+'">'+typeLbl[t]+'</span>'
      +'</div>'
      +'<div class="pli-sub">'+esc(sub)+'</div></div>'
      +'<div class="pli-acts">'
      +'<button class="btn-acc" onclick="plConnect('+i+')" style="height:28px;padding:0 10px;font-size:11px">▶ Load</button>'
      +'<button class="btn-ghost" onclick="plEdit('+i+')" style="height:28px;padding:0 8px;font-size:11px">✎ Edit</button>'
      +'<button class="btn-red" onclick="plDelete('+i+')" style="height:28px;padding:0 8px">🗑</button>'
      +'</div></div>';
  }).join('');
}

function plSave(){
  const name=document.getElementById('pl-name').value.trim();
  if(!name){toast('Enter a playlist name','wrn');return;}
  const arr=plLoadAll();
  const entry={
    id: plEditId||Date.now().toString(36),
    name, type:plCT,
    url:   document.getElementById('pl-url').value.trim(),
    mac:   document.getElementById('pl-mac').value.trim(),
    url_xtream: document.getElementById('pl-xu').value.trim(),
    username: document.getElementById('pl-us').value.trim(),
    password: document.getElementById('pl-pw').value.trim(),
    m3u_url: document.getElementById('pl-m3u').value.trim(),
    ext_epg_url: (plCT==='xtream'
      ? document.getElementById('pl-epg').value.trim()
      : plCT==='mac'
        ? document.getElementById('pl-mac-epg').value.trim()
        : document.getElementById('pl-m3u-epg').value.trim()),
  };
  if(plEditId){
    const idx=arr.findIndex(p=>p.id===plEditId);
    if(idx>=0) arr[idx]=entry; else arr.push(entry);
  } else {
    arr.push(entry);
  }
  plSaveAll(arr);
  plClearForm();
  renderPLList();
  toast('Playlist saved!','ok');
}

function plEdit(i){
  const arr=plLoadAll(); const p=arr[i]; if(!p) return;
  plEditId=p.id;
  plSetCT(p.type||'mac');
  document.getElementById('pl-name').value=p.name||'';
  document.getElementById('pl-url').value=p.url||'';
  document.getElementById('pl-mac').value=p.mac||'';
  document.getElementById('pl-xu').value=p.url_xtream||p.url||'';
  document.getElementById('pl-us').value=p.username||'';
  document.getElementById('pl-pw').value=p.password||'';
  document.getElementById('pl-m3u').value=p.m3u_url||'';
  document.getElementById('pl-epg').value=p.ext_epg_url||'';
  document.getElementById('pl-mac-epg').value=p.ext_epg_url||'';
  document.getElementById('pl-m3u-epg').value=p.ext_epg_url||'';
  // scroll form into view
  document.querySelector('.pl-add').scrollIntoView({behavior:'smooth'});
}

function plDelete(i){
  const arr=plLoadAll(); arr.splice(i,1); plSaveAll(arr); renderPLList();
  toast('Deleted','info');
}

function plClearForm(){
  plEditId=null;
  ['pl-name','pl-url','pl-mac','pl-xu','pl-us','pl-pw','pl-m3u',
   'pl-epg','pl-mac-epg','pl-m3u-epg'].forEach(id=>
    document.getElementById(id).value='');
}

async function plConnect(i){
  const arr=plLoadAll(); const p=arr[i]; if(!p) return;
  closePL();
  // Fill in the connection form
  setCT(p.type||'mac');
  document.getElementById('i-url').value=p.url||'';
  document.getElementById('i-mac').value=p.mac||'';
  document.getElementById('i-xu').value=p.url_xtream||p.url||'';
  document.getElementById('i-us').value=p.username||'';
  document.getElementById('i-pw').value=p.password||'';
  document.getElementById('i-m3u').value=p.m3u_url||'';
  document.getElementById('i-epg').value=p.ext_epg_url||'';
  document.getElementById('i-mac-epg').value=p.ext_epg_url||'';
  document.getElementById('i-m3u-epg').value=p.ext_epg_url||'';
  // Auto-connect
  await doConnect();
}

// ── INIT ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded',()=>{
  setCT('mac'); toggleCP();
  // Player controls expanded by default
  const pc = document.getElementById('pctrl-panel');
  if(pc) pc.classList.add('expanded');
  try{const sv=localStorage.getItem('mkv_folder');
    if(sv) document.getElementById('o-dir').value=sv;
    else document.getElementById('o-dir').value='/sdcard/Download/';}catch(e){}
  try{const sm=localStorage.getItem('m3u_path');
    if(sm) document.getElementById('o-m3u').value=sm;
    else document.getElementById('o-m3u').value='/sdcard/Download/playlist.m3u';}catch(e){}
  try{const se=localStorage.getItem('ext_player');
    if(se) document.getElementById('o-extplayer').value=se;}catch(e){}
  try{const sk=localStorage.getItem('opensubtitles_key');
    if(sk) document.getElementById('o-subkey').value=sk;}catch(e){}
  if(_isMobile){
    document.getElementById('extplayer-row-desktop').style.display='none';
    document.getElementById('extplayer-row-mobile').style.display='flex';
    try{const mp=localStorage.getItem('mobile_player');
      if(mp) document.getElementById('o-mobile-player').value=mp;}catch(e){}
  }

  // ── Item name scroll: hover a row → animate long names left to reveal full text ──
  const ilist = document.getElementById('ilist');
  if(ilist){
    ilist.addEventListener('mouseenter', e=>{
      const row = e.target.closest('.irow');
      if(!row) return;
      const wrap = row.querySelector('.iname');
      const inner = row.querySelector('.iname-inner');
      if(!wrap || !inner) return;
      const overflow = inner.scrollWidth - wrap.clientWidth;
      if(overflow <= 6) return;   // not truncated — skip
      // Speed: ~80px/s, min 2s, max 12s
      const dur = Math.min(12, Math.max(2, overflow / 80));
      wrap.style.setProperty('--scroll-dist', `-${overflow + 8}px`);
      wrap.style.setProperty('--scroll-dur', `${dur}s`);
      wrap.classList.add('scrolling');
    }, true);
    ilist.addEventListener('mouseleave', e=>{
      const row = e.target.closest('.irow');
      if(!row) return;
      const wrap = row.querySelector('.iname');
      if(wrap) wrap.classList.remove('scrolling');
    }, true);
  }

  startLog();
  alog('IPTV Portal Builder ready.','k');
  alog('Tap ⚙ in the header to enter credentials and connect.','i');
});
// ── WHAT'S ON NOW ──────────────────────────────────────────
let _wonPrograms = [];
const _wonMatches = {};   // idx → full channel object from portal

function openWhatsOn(){
  document.getElementById('won-overlay').classList.add('open');
  document.getElementById('won-srch').value = '';
  document.getElementById('won-list').innerHTML =
    '<div class="won-loading"><span class="spin"></span> Loading EPG data…</div>';
  document.getElementById('won-count').textContent = '…';
  Object.keys(_wonMatches).forEach(k => delete _wonMatches[k]);
  _wonFetch(0);
  setTimeout(()=>document.getElementById('won-srch').focus(), 200);
}

function _wonFetch(attempt){
  fetch('/api/whats_on')
    .then(r => r.json())
    .then(data => {
      // EPG download in progress — auto-retry up to ~90s
      if(data.status === 'loading'){
        if(attempt < 18){
          const secs = 5;
          document.getElementById('won-list').innerHTML =
            `<div class="won-loading"><span class="spin"></span> EPG downloading… retrying in ${secs}s</div>`;
          document.getElementById('won-count').textContent = '…';
          _wonRetryTimer = setTimeout(()=>_wonFetch(attempt+1), secs * 1000);
        } else {
          document.getElementById('won-list').innerHTML =
            '<div class="won-empty"><span>⏳</span>EPG is taking a while. Try reopening in a moment.</div>';
          document.getElementById('won-count').textContent = '0';
        }
        return;
      }
      if(data.status === 'no_epg' || data.status === 'error'){
        document.getElementById('won-list').innerHTML =
          `<div class="won-empty"><span>📡</span>${esc(data.message||'No EPG data available.')}</div>`;
        document.getElementById('won-count').textContent = '0';
        return;
      }
      _wonPrograms = data.programs || [];
      wonRender(_wonPrograms);
    })
    .catch(e => {
      document.getElementById('won-list').innerHTML =
        `<div class="won-empty"><span>⚠️</span>Failed to load: ${esc(String(e))}</div>`;
    });
}

let _wonRetryTimer = null;
function closeWhatsOn(){
  if(_wonRetryTimer){ clearTimeout(_wonRetryTimer); _wonRetryTimer = null; }
  document.getElementById('won-overlay').classList.remove('open');
}

let _wonFilterTimer = null;
const WON_PAGE_SIZE = 200;  // max items rendered at once

function wonFilter(){
  clearTimeout(_wonFilterTimer);
  _wonFilterTimer = setTimeout(_wonFilterApply, 180);  // debounce 180ms
}

function _wonFilterApply(){
  const q = document.getElementById('won-srch').value.toLowerCase().trim();
  if(!q){ wonRender(_wonPrograms); return; }
  wonRender(_wonPrograms.filter(p =>
    p.title.toLowerCase().includes(q) || p.channel_name.toLowerCase().includes(q)
  ));
}

function wonRender(list){
  const total = list.length;
  const shown = list.slice(0, WON_PAGE_SIZE);
  document.getElementById('won-count').textContent =
    total > WON_PAGE_SIZE
      ? `${WON_PAGE_SIZE} of ${total} programmes (refine filter to see more)`
      : total + ' programmes';
  const el = document.getElementById('won-list');
  if(!total){
    el.innerHTML = '<div class="won-empty"><span>🔍</span>No programmes match your filter.</div>';
    return;
  }
  // Build HTML as a single string — much faster than appending nodes one-by-one
  const parts = shown.map((p, i) => {
    const start = _wonFmt(p.start);
    const end   = _wonFmt(p.end);
    return `<div class="won-item" title="${esc(p.desc||'')}">
      <div class="won-item-info">
        <div class="won-item-title">${esc(p.title)}</div>
        <div class="won-item-ch">${esc(p.channel_name)}</div>
        <div class="won-item-times">${start} – ${end}</div>
        <div class="won-find-result" id="won-res-${i}"></div>
        <div id="won-ext-${i}" style="display:none;margin-top:3px">
          <span class="won-ext-btn"
            onclick="wonOpenExternal(${i})">🎬 external player</span>
        </div>
      </div>
      <div class="won-progress">
        <div class="won-progress-bar"><div class="won-progress-fill" style="width:${p.progress}%"></div></div>
        <div class="won-progress-pct">${p.progress}%</div>
      </div>
      <button class="won-find-btn" id="won-fbtn-${i}" data-name="${esc(p.channel_name)}" data-cid="${esc(p.channel_id)}" onclick="wonFindChannel(this,${i})" title="Find on portal">🔍</button>
    </div>`;
  });
  el.innerHTML = parts.join('');
}

async function wonPlayFound(idx, resEl, name){
  const ch = _wonMatches[idx];
  if(!ch){ console.warn('[WON] No cached channel for idx', idx); return; }

  resEl.className = 'won-find-result playing';
  resEl.textContent = '⟳ Resolving ' + name + '…';
  resEl.onclick = null;

  console.log('[WON] Resolving channel:', name, ch);

  try {
    const r = await fetch('/api/resolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item: ch, mode: 'live', category: curCat || {}})
    });
    const d = await r.json();
    console.log('[WON] resolve result:', d);

    if(d.url){
      resEl.textContent = '▶ Playing: ' + name;
      closeWhatsOn();
      doPlay(d.url, name);
    } else {
      resEl.className = 'won-find-result fail';
      resEl.textContent = '✗ Could not resolve stream URL';
      resEl.onclick = () => wonPlayFound(idx, resEl, name);
    }
  } catch(e) {
    console.error('[WON] resolve error:', e);
    resEl.className = 'won-find-result fail';
    resEl.textContent = '✗ Error: ' + e;
    resEl.onclick = () => wonPlayFound(idx, resEl, name);
  }
}

async function wonOpenExternal(idx){
  const ch = _wonMatches[idx];
  if(!ch){ toast('Find the channel first','wrn'); return; }
  const name = ch.name || ch.o_name || '?';
  toast('Resolving for external player…','info');
  try{
    const r = await fetch('/api/resolve_url',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({item:ch, mode:'live', category:curCat||{}})});
    const d = await r.json();
    if(!d.url){ toast('Could not resolve stream URL','err'); return; }
    const url = d.url;
    if(_isMobile){
      const player = localStorage.getItem('mobile_player')||'ask';
      if(player==='copy'){
        try{await navigator.clipboard.writeText(url); toast('Stream URL copied!','ok');}
        catch(e){prompt('Copy stream URL:',url);}
        return;
      }
      window.location.href = player==='ask'
        ?`intent:${url}#Intent;type=video/*;S.browser_fallback_url=about:blank;end`
        :`intent:${url}#Intent;package=${player};type=video/*;S.browser_fallback_url=about:blank;end`;
    } else {
      const exe=(localStorage.getItem('ext_player')||'').trim();
      if(!exe){toast('Set external player path in ⚙ settings first','wrn');return;}
      const r2=await fetch('/api/open_external',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({exe, url})});
      const d2=await r2.json();
      if(d2.error) toast('Error: '+d2.error,'err');
      else toast('Launched: '+name,'ok');
    }
  }catch(e){ toast('Failed: '+e,'err'); }
}

function _wonFmt(ts){
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

function wonFindChannel(btn, idx){
  const channelName = btn.dataset.name || '';
  const channelId   = btn.dataset.cid  || '';
  const res = document.getElementById('won-res-'+idx);
  if(!res) return;

  console.log('[WON] Find channel:', channelName, '| id:', channelId);

  btn.classList.add('loading');
  btn.textContent = '⏳';
  res.className = 'won-find-result';
  res.textContent = '';

  fetch('/api/find_channel', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({channel_name: channelName, channel_id: channelId})
  })
  .then(r => {
    console.log('[WON] find_channel HTTP', r.status);
    return r.json();
  })
  .then(data => {
    console.log('[WON] find_channel result:', data);
    btn.classList.remove('loading');
    btn.textContent = '🔍';
    if(data.found){
      const cat = data.cat ? ` · ${data.cat}` : '';
      res.className = 'won-find-result ok';
      res.textContent = `▶ ${data.name}${cat} (${data.score}%) — tap to play`;
      res.title = 'Click to play this channel';
      _wonMatches[idx] = data.channel;
      res.onclick = () => wonPlayFound(idx, res, data.name);
      const extBtn = document.getElementById('won-ext-'+idx);
      if(extBtn) extBtn.style.display = '';
    } else if(data.error === 'Not connected'){
      res.className = 'won-find-result fail';
      res.textContent = '✗ Not connected to portal';
    } else {
      res.className = 'won-find-result fail';
      res.textContent = data.message || '✗ Not found on this portal';
    }
  })
  .catch(e => {
    console.error('[WON] find_channel error:', e);
    btn.classList.remove('loading');
    btn.textContent = '🔍';
    res.className = 'won-find-result fail';
    res.textContent = '✗ Request failed: ' + e;
  });
}
</script>
<script>
  // Hide Cast tab if cast_addon is not available (pychromecast etc. not installed)
  fetch('/api/cast/status').then(r => {
    if (!r.ok) document.getElementById('cast-fab').style.display = 'none';
  }).catch(() => {
    document.getElementById('cast-fab').style.display = 'none';
  });
</script>
<script>
  // Hide all Multi-View entry points if multiview_addon.py is not present.
  // Mirrors the cast_addon pattern: probe returns 404 when addon is not loaded.
  fetch('/api/multiview/available').then(r => {
    if (!r.ok) _mvHideAll();
  }).catch(() => {
    _mvHideAll();
  });
  function _mvHideAll(){
    // Desktop ⊞ Multi-View button in Player Controls bar
    const desktopBtn = document.getElementById('mv-desktop-btn');
    if(desktopBtn) desktopBtn.style.display = 'none';
    // Mobile ⊞ Multi tab in bottom navigation
    const mobileTab = document.getElementById('t-mv');
    if(mobileTab) mobileTab.style.display = 'none';
  }
</script>
<script src="/api/cast/ui.js"></script>
<script>
/* ═══════════════════════════════════════════════════════════════════════
   MULTIVIEW  —  JavaScript (Phases 3–9)

   Design cross-references:
     multiview.js  → playChannelInWidget, stopAndCleanupPlayer,
                     addPlayerWidget, attachWidgetEventListeners,
                     setActivePlayer, pauseAndClearAllPlayers,
                     handleVisibilityChange, saveLayout, loadSelectedLayout,
                     applyPresetLayout, populateChannelSelector
     server.js     → /stream dedup, /api/stream/stop reference guard
     multiview_addon.py → /api/multiview/stream, /api/multiview/stream/stop,
                          /api/multiview/layouts CRUD
   ════════════════════════════════════════════════════════════════════════ */

// ── STATE  (mirrors multiview.js top-level variables exactly) ────────────────
// multiview.js: const players = new Map();
const mvPlayers    = new Map();   // widgetId → mpegts player instance
// multiview.js: const playerUrls = new Map();
const mvUrls       = new Map();   // widgetId → original channel URL
// multiview.js: let activePlayerId = null;
let mvActiveId     = null;
// multiview.js: let channelSelectorCallback = null;
let mvSelCallback  = null;
let _mvSelWidgetCtx = null;   // { wid, cEl } of the widget that opened the selector
// multiview.js: let grid;
let mvGrid         = null;
// multiview.js: const MAX_PLAYERS = 9;
const MV_MAX       = 9;

// Portal tracking — maps widgetId → { portalKey, portalName, maxConn }
// portalKey = hostname:port extracted from the resolved stream URL
const mvPortalMeta  = new Map();
// Track which widgets are playing an external/direct URL (not a portal channel)
// — these don't count toward or display portal connection limits.
const mvExternalUrlWidgets = new Set();

// Optional override: populate window._mvPortalMaxConns[portalKey] = N
// when you connect to a portal that exposes its max-connection limit
// (Xtream API auth response includes user_info.max_connections).
// The multiview UI reads from this object to show "N/M" instead of "N conn".
if (!window._mvPortalMaxConns) window._mvPortalMaxConns = {};

// Client identity — replaces server.js userId for stream key construction.
// Mirrors server.js: const streamKey = `${userId}::${streamUrl}::${profileId}`;
// Stored in localStorage so the same client_id survives page reloads within
// a session (ffmpeg processes keyed by it remain valid).
let mvClientId = (()=>{
  try {
    let id = localStorage.getItem('mv_client_id');
    if(!id){ id = 'mv-' + Date.now() + '-' + Math.random().toString(36).slice(2,8); localStorage.setItem('mv_client_id',id); }
    return id;
  } catch(e){ return 'mv-'+Date.now(); }
})();

// ── HELPERS ───────────────────────────────────────────────────────────────────

// mirrors multiview.js isVODFile() — not used here but kept for completeness
function _mvIsVod(url){ return /\.(mkv|mp4|avi|mov|m4v|flv|wmv|mpg|mpeg|webm)/i.test(url.split('?')[0]); }

// ── Portal tracking helpers ───────────────────────────────────────────────────

// Extract a stable portal key (hostname + port) and friendly display name from
// any stream URL.  Used to group connections by portal for the badge display.
function _mvPortalKeyFromUrl(url){
  try {
    const p = new URL(url);
    const key  = p.hostname + (p.port ? ':'+p.port : '');
    // Display name: just hostname, strip leading 'www.'
    const name = p.hostname.replace(/^www\./,'');
    return { key, name };
  } catch(e) {
    return { key: 'unknown', name: 'unknown' };
  }
}

// Recount active connections per portal and refresh all widget portal badges.
// Called whenever a player starts or stops.
function _mvUpdatePortalBadges(){
  // Tally connections per portalKey — exclude external/custom-URL widgets
  const counts = {};
  for(const [wid, meta] of mvPortalMeta.entries()){
    if(!meta || !meta.portalKey) continue;
    if(mvExternalUrlWidgets.has(wid)) continue;  // external URL — not a portal connection
    counts[meta.portalKey] = (counts[meta.portalKey] || 0) + 1;
  }

  // Update each widget's badge
  for(const [wid, meta] of mvPortalMeta.entries()){
    const cEl = document.getElementById('mwc-' + wid);
    if(!cEl || !meta) continue;
    const badge = cEl.querySelector('.mv-hdr-portal');
    if(!badge) continue;

    // External URL widgets: show just the hostname, no connection count
    if(mvExternalUrlWidgets.has(wid)){
      badge.textContent = meta.portalName || '';
      badge.classList.remove('mv-conn-warn','mv-conn-full');
      continue;
    }

    const count   = counts[meta.portalKey] || 1;
    const maxConn = window._mvPortalMaxConns[meta.portalKey] || 0;
    const connStr = maxConn > 0 ? `${count}/${maxConn}` : `${count}`;
    const label   = count === 1 ? 'connection' : 'connections';
    badge.textContent = `${meta.portalName}  ·  ${connStr} ${label}`;

    // Colour-code badge when approaching / hitting the limit
    badge.classList.remove('mv-conn-warn','mv-conn-full');
    if(maxConn > 0){
      if(count >= maxConn)           badge.classList.add('mv-conn-full');
      else if(count >= maxConn - 1)  badge.classList.add('mv-conn-warn');
    }
  }
}

// ── URL / YouTube play helper ─────────────────────────────────────────────────

// Detect URLs that need yt-dlp resolution before we can stream them.
function _mvNeedsResolve(url){
  return /youtube\.com\/|youtu\.be\/|twitch\.tv\/|dailymotion\.com|vimeo\.com/i.test(url);
}

// Play an arbitrary URL (IPTV direct stream, YouTube, etc.) inside a widget.
// If the URL belongs to a supported site, we call /api/multiview/resolve_url
// first to get a streamable direct URL, then feed it through the normal
// mpegts.js + ffmpeg proxy pipeline.
async function _mvPlayFromUrl(wid, rawUrl, cEl){
  if(!cEl){ console.warn('[MV] _mvPlayFromUrl: cEl is null for wid='+wid); return; }
  rawUrl = (rawUrl||'').trim();
  if(!rawUrl){ toast('Enter a URL first', 'wrn'); return; }

  // Persist raw URL so quality changes can re-resolve with new quality
  cEl._mvRawUrl = rawUrl;

  const titleEl = cEl ? cEl.querySelector('.mv-hdr-title') : null;
  if(titleEl) titleEl.textContent = 'Resolving…';

  // Read quality from the widget's selector (default 'best')
  const qualSel = cEl.querySelector('.mv-quality-sel');
  const quality = (qualSel ? qualSel.value : null) || 'best';

  let finalUrl    = rawUrl;
  let channelName = '';
  let isLive      = true;   // assumed live unless yt-dlp says otherwise

  if(_mvNeedsResolve(rawUrl)){
    // Ask the server to resolve via yt-dlp
    try {
      const r = await fetch('/api/multiview/resolve_url', {
        method:  'POST',
        headers: {'Content-Type':'application/json'},
        body:    JSON.stringify({url: rawUrl, quality})
      });
      const d = await r.json();
      if(d.error){
        toast('Resolve error: ' + d.error, 'err');
        if(titleEl) titleEl.textContent = 'No Channel';
        return;
      }
      finalUrl    = d.url;
      channelName = d.title || '';
      isLive      = d.is_live !== false;   // false = VOD → enable seek bar
    } catch(e){
      toast('Could not resolve URL: ' + e, 'err');
      if(titleEl) titleEl.textContent = 'No Channel';
      return;
    }
  }

  // Build a friendly display name from the URL if yt-dlp didn't give one
  if(!channelName){
    try {
      const p = new URL(rawUrl);
      channelName = p.hostname.replace(/^www\./,'') + (p.pathname !== '/' ? ' · '+p.pathname.split('/').filter(Boolean).pop() : '');
    } catch(e){ channelName = rawUrl.slice(0,40); }
  }

  // Synthesise a channel object for _mvPlayChannel
  const { key: _synthKey, name: _synthName } = _mvPortalKeyFromUrl(finalUrl);
  const synth = {
    name:             channelName,
    _direct_url:      finalUrl,   // skips the /api/resolve call in _mvPlayChannel
    id:               'custom-url-' + Date.now(),
    _portal_override: { key: _synthKey, name: _synthName },
    _is_live:         isLive,     // passed to mpegts isLive flag for VOD seek support
  };

  mvExternalUrlWidgets.add(wid);
  await _mvPlayChannel(wid, synth, cEl);
}

// ── Toolbar collapse ─────────────────────────────────────────────────────────

// On mobile the toolbar body starts collapsed so the grid gets maximum space.
// On desktop it starts expanded since there is plenty of room.
function _mvTbInit(){
  const tb = document.getElementById('mv-toolbar');
  if(!tb) return;
  const isMobile = window.innerWidth < 900;
  // Start collapsed on mobile, expanded on desktop
  tb.classList.toggle('tb-open', !isMobile);
  _mvFitCellHeight();
}

function mvTbToggle(){
  const tb = document.getElementById('mv-toolbar');
  if(!tb) return;
  tb.classList.toggle('tb-open');
  _mvFitCellHeight();
}

// Auto-collapse toolbar after a layout loads (mobile only).
function _mvTbCollapseIfMobile(){
  if(window.innerWidth >= 900) return;
  const tb = document.getElementById('mv-toolbar');
  if(tb) tb.classList.remove('tb-open');
  _mvFitCellHeight();
}

// Simple confirm dialog using our custom modal
// mirrors multiview.js showConfirm() calls
let _mvConfirmOk = null;
function _mvConfirm(title, msg, onOk, onCancel){
  document.getElementById('mv-confirm-title').textContent = title;
  document.getElementById('mv-confirm-msg').textContent   = msg;
  _mvConfirmOk = onOk;
  document.getElementById('mv-confirm-overlay').classList.add('open');
  document.getElementById('mv-confirm-cancel').onclick = ()=>{
    document.getElementById('mv-confirm-overlay').classList.remove('open');
    if(onCancel) onCancel();
  };
}
document.getElementById('mv-confirm-ok').addEventListener('click', ()=>{
  document.getElementById('mv-confirm-overlay').classList.remove('open');
  if(_mvConfirmOk) _mvConfirmOk();
  _mvConfirmOk = null;
});

// ── INIT / OPEN / CLOSE ───────────────────────────────────────────────────────

// ── Top-position tracking ─────────────────────────────────────────────────────
// p-mv must sit directly below the header (which grows when cpanel opens).
// We read the live offsetHeight of #hdr and push it into the CSS variable
// --mv-top.  Called on open, on cpanel toggle, and on window resize.
function _mvUpdateTop(){
  const hdr = document.getElementById('hdr');
  const h   = hdr ? hdr.offsetHeight : 44;
  document.documentElement.style.setProperty('--mv-top', h + 'px');
  // Refit cell height now that the panel height has changed.
  // This is what makes the grid expand to fill the space when the connect
  // panel closes (the most common case after first connect).
  _mvFitCellHeight();
}

// Patch toggleCP so the panel top follows the connect panel animation.
// We poll offsetHeight for the duration of the CSS transition (350 ms).
(function(){
  const _origToggleCP = window.toggleCP;
  const _origCloseCP  = window.closeCP;
  function _trackCpTransition(){
    let t = 0;
    const iv = setInterval(()=>{
      _mvUpdateTop();
      t += 30;
      if(t >= 400) clearInterval(iv);
    }, 30);
  }
  window.toggleCP = function(){
    if(typeof _origToggleCP === 'function') _origToggleCP();
    _trackCpTransition();
  };
  window.closeCP = function(){
    if(typeof _origCloseCP === 'function') _origCloseCP();
    _trackCpTransition();
  };
})();

// Keep top in sync on window resize too
window.addEventListener('resize', ()=>{ _mvUpdateTop(); _mvFitCellHeight(); });

// Show/hide the desktop multiview button (only meaningful on desktop ≥900px)
function _mvSyncDesktopBtn(){
  const btn  = document.getElementById('mv-desktop-btn');
  if(!btn) return;
  const isDesktop = window.innerWidth >= 900;
  btn.style.display = isDesktop ? '' : 'none';
  const isOpen = document.getElementById('p-mv').classList.contains('mv-active');
  btn.classList.toggle('mv-btn-active', isOpen);
}
window.addEventListener('resize', _mvSyncDesktopBtn);

// Toggle: open if closed, close if open.
// Called from both the desktop pctrl-hdr button and the mobile botnav tab.
function mvToggle(){
  const isOpen = document.getElementById('p-mv').classList.contains('mv-active');
  if(isOpen){ mvClose(); } else { mvOpen(); }
}

// Called from ⊞ button in pctrl-hdr (desktop) and botnav t-mv tab (mobile).
function mvOpen(){
  _mvUpdateTop();
  const panel = document.getElementById('p-mv');
  panel.classList.add('mv-active');

  // Highlight the botnav button (mobile)
  document.querySelectorAll('.nt').forEach(b=>b.classList.remove('on'));
  const tb = document.getElementById('t-mv');
  if(tb) tb.classList.add('on');

  _mvSyncDesktopBtn();

  // mirrors multiview.js initMultiView()
  if(mvGrid){ _mvTbInit(); _mvLoadLayouts().then(_mvAutoRestoreLayout); return; }

  // First time — initialise grid
  mvGrid = GridStack.init({
    float: true,
    cellHeight: '8vh',
    margin: 5,
    column: 12,
    alwaysShowResizeHandle: true,  // always show on all platforms, not just mobile
    resizable: { handles: 'e, se, s, sw, w' },
    // Restrict drag to the header bar so touch on the video body scrolls normally.
    // On mobile this prevents the video area from eating drag gestures.
    handle: '.mv-hdr',
    handleClass: 'mv-hdr',
  }, '#multiview-grid');

  _mvUpdateGridBg();
  mvGrid.on('change', _mvUpdateGridBg);

  _mvSetupListeners();
  _mvTbInit();
  _mvLoadLayouts();

  // Default layout on first open: 1+2
  // One large player left (w:8), two stacked right (w:4, h:5 each)
  mvGrid.batchUpdate();
  try {
    _mvAddWidget(null, {x:0, y:0, w:8, h:10});
    _mvAddWidget(null, {x:8, y:0, w:4, h:5});
    _mvAddWidget(null, {x:8, y:5, w:4, h:5});
  } finally {
    mvGrid.commit();
  }

  // Fit cell height AFTER widgets are in DOM so offsetHeight is accurate
  setTimeout(_mvFitCellHeight, 50);

  // ResizeObserver on the grid wrapper fires whenever the wrapper's rendered
  // size changes — after CSS transitions finish, after toolbar collapse,
  // after orientation changes, after the connect panel animates closed.
  // This is more reliable than polling and fires at the right moment.
  if(window.ResizeObserver && !mvGrid._mvWrapRO){
    mvGrid._mvWrapRO = new ResizeObserver(()=> _mvFitCellHeight());
    const wrap = document.getElementById('mv-grid-wrap');
    if(wrap) mvGrid._mvWrapRO.observe(wrap);
  }
}

// mirrors multiview.js cleanupMultiView()
async function mvClose(){
  document.getElementById('p-mv').classList.remove('mv-active');
  document.getElementById('mv-confirm-overlay').classList.remove('open');
  document.getElementById('mv-sel-overlay').classList.remove('open');
  document.getElementById('mv-save-overlay').classList.remove('open');
  mvSelCallback = null; _mvSelWidgetCtx = null; _mvSelMode = 'cats'; _mvSelCat = null; _mvSelItems = [];

  _mvSyncDesktopBtn();

  // Remove botnav highlight — restore previous tab highlight
  document.querySelectorAll('.nt').forEach(b=>b.classList.remove('on'));
  const prevPanel = document.querySelector('#main .panel.active');
  if(prevPanel){
    const tid = prevPanel.id.replace('p-','t-');
    const tb  = document.getElementById(tid);
    if(tb) tb.classList.add('on');
  }

  if(!mvGrid) return;

  // ── Snapshot current grid layout to localStorage before teardown ──────────
  // This allows re-open to restore the exact widget arrangement even if no
  // named layout was ever explicitly loaded.
  try {
    const items = mvGrid.getGridItems();
    if(items.length){
      const snapshot = items.map(item=>{
        const node = item.gridstackNode;
        const ph   = item.querySelector('.mv-placeholder');
        return { x:node.x, y:node.y, w:node.w, h:node.h,
                 id: ph?.id || node.id,
                 channelId: ph?.dataset.channelId || null };
      });
      localStorage.setItem('mv_session_layout', JSON.stringify(snapshot));
    }
  } catch(e){}

  const stops = Array.from(mvPlayers.keys()).map(id => _mvStopCleanup(id, true));
  await Promise.all(stops);
  mvGrid.removeAll();
  mvPlayers.clear();
  mvUrls.clear();
  mvPortalMeta.clear();
  mvActiveId    = null;
  mvSelCallback = null;
}

// ── GRID BACKGROUND + CELL HEIGHT ────────────────────────────────────────────
// mirrors multiview.js updateGridBackground()
// Extended to also recalculate cellHeight so the grid always fills the panel.
//
// Root cause of empty space:
//   cellHeight:'8vh' × 10 rows = 80vh.  Panel height ≈ (100vh - header) = ~95vh.
//   Fixed '8vh' leaves ~15vh of dead space at the bottom.
// Fix: compute cellHeight = availablePanelPx / TARGET_ROWS each time.
const _MV_TARGET_ROWS = 10;   // grid coordinate space matches our default 1+2 layout

function _mvUpdateGridBg(){
  const gs = document.querySelector('#mv-grid-wrap .grid-stack');
  if(!gs || !mvGrid) return;
  const cols   = mvGrid.getColumn ? mvGrid.getColumn() : 12;
  const cellW  = gs.offsetWidth / cols;
  gs.style.setProperty('--mv-cell-w', cellW + 'px');
  // Recompute cell height to fill the available panel height exactly
  _mvFitCellHeight();
}

function _mvFitCellHeight(){
  if(!mvGrid) return;
  // Read the grid wrapper height directly — it is a flex:1 child so the browser
  // has already computed the correct height after any layout pass, including
  // mid-transition states where panel.offsetHeight would be stale.
  const wrap = document.getElementById('mv-grid-wrap');
  if(!wrap) return;
  const available = wrap.offsetHeight;
  if(available <= 0) return;
  const cellH = Math.max(40, Math.floor(available / _MV_TARGET_ROWS));
  mvGrid.cellHeight(cellH, true);
}

// ── WIDGET ───────────────────────────────────────────────────────────────────

// Monotonic counter for widget IDs.
// CRITICAL: Date.now() returns the same value when multiple widgets are
// batch-created in the same millisecond (default layout, preset layouts).
// Duplicate IDs mean document.getElementById('mwc-'+wid) returns the FIRST
// element — all subsequent widgets get the wrong content element and their
// event listeners are attached to the wrong DOM node → they appear dead.
let _mvWidgetSeq = 0;

// mirrors multiview.js addPlayerWidget(channel, layout)
function _mvAddWidget(channel, layout){
  if(mvGrid.getGridItems().length >= MV_MAX){
    toast('Maximum ' + MV_MAX + ' players', 'wrn'); return null;
  }
  layout = layout || {};
  // Use layout.id if restoring a saved layout; otherwise generate a unique id.
  // Combine counter + timestamp so ids are unique across page reloads too.
  const wid = layout.id || ('mv-' + (++_mvWidgetSeq) + '-' + Date.now());

  // mirrors multiview.js widgetHTML — exact same structure/classes
  const html = `
    <div class="mv-widget-content" id="mwc-${wid}">
      <div class="mv-hdr">
        <div class="mv-hdr-info">
          <span class="mv-hdr-title">No Channel</span>
          <span class="mv-hdr-portal"></span>
        </div>
        <div class="mv-ctrl">
          <button class="mv-sel-btn"  title="Select IPTV channel">📺</button>
          <button class="mv-url-btn"  title="Play URL / YouTube">🔗</button>
          <button class="mv-pp-btn"   title="Play/Pause">⏸</button>
          <button class="mv-mute-btn" title="Mute">🔊</button>
          <input  type="range" class="mv-vol" min="0" max="1" step="0.05" value="0.5"/>
          <select class="mv-quality-sel" title="Quality">
            <option value="best">Auto</option>
            <option value="1080">1080p</option>
            <option value="720">720p</option>
            <option value="480">480p</option>
            <option value="360">360p</option>
          </select>
          <button class="mv-fs-btn"   title="Fullscreen">⛶</button>
          <button class="mv-stop-btn" title="Stop">⏹</button>
          <button class="mv-rm-btn"   title="Remove player">✕</button>
        </div>
      </div>
      <div class="mv-url-bar mv-hidden">
        <input type="text" class="mv-url-input" placeholder="Paste URL or YouTube link and press Enter…"/>
        <button class="mv-url-play-btn">▶ Play</button>
        <button class="mv-url-close-btn">✕</button>
      </div>
      <div class="mv-body">
        <div class="mv-placeholder" id="${wid}" data-channel-id="">
          <span class="mv-ph-ico">▶</span>
          <span>📺 Select IPTV channel &nbsp;|&nbsp; 🔗 Play URL</span>
        </div>
        <video class="mv-video mv-hidden" muted playsinline></video>
        <div class="mv-seek-wrap">
          <input type="range" class="mv-seek" min="0" max="100" step="0.1" value="0">
          <span class="mv-seek-time">0:00</span>
        </div>
      </div>
    </div>`;

  const el = mvGrid.addWidget({
    id: wid, content: html,
    w: layout.w || 4, h: layout.h || 4,
    x: layout.x,      y: layout.y
  });

  const contentEl = document.getElementById('mwc-' + wid);
  if(contentEl){
    _mvAttachListeners(contentEl, wid);
    // Watch widget width and add size-hint classes so CSS can hide
    // low-priority controls when the tile is too small to show them all.
    if(window.ResizeObserver){
      const ro = new ResizeObserver(entries=>{
        for(const e of entries){
          const w = e.contentRect.width;
          contentEl.classList.toggle('mv-tiny', w < 220);
          contentEl.classList.toggle('mv-xs',   w < 140);
        }
      });
      ro.observe(contentEl);
    }
  }
  if(channel)   _mvPlayChannel(wid, channel, contentEl);
  return el;
}

// mirrors multiview.js attachWidgetEventListeners()
function _mvAttachListeners(cEl, wid){
  const placeholder  = cEl.querySelector('.mv-placeholder');
  const videoEl      = cEl.querySelector('.mv-video');
  const gsItem       = cEl.closest('.grid-stack-item');

  // Open channel selector — mirrors openSelector in multiview.js
  const openSel = ()=>{
    mvSelCallback = (ch)=> _mvPlayChannel(wid, ch, cEl);
    _mvSelWidgetCtx = { wid, cEl };   // stored so "Play URL" row can fire _mvPlayFromUrl
    _mvPopulateSelector();
    document.getElementById('mv-sel-overlay').classList.add('open');
  };

  cEl.querySelector('.mv-sel-btn').addEventListener('click', openSel);
  if(placeholder) placeholder.addEventListener('click', openSel);

  // ── URL button & URL bar ──────────────────────────────────────────────────
  const urlBar      = cEl.querySelector('.mv-url-bar');
  const urlInput    = cEl.querySelector('.mv-url-input');
  const urlPlayBtn  = cEl.querySelector('.mv-url-play-btn');
  const urlCloseBtn = cEl.querySelector('.mv-url-close-btn');

  // Toggle the URL bar on/off
  cEl.querySelector('.mv-url-btn').addEventListener('click', e=>{
    e.stopPropagation();
    if(urlBar.classList.toggle('mv-hidden')){
      // just hid it — nothing else to do
    } else {
      // just shown it — focus the input
      urlInput.focus();
      urlInput.select();
    }
  });

  // Play from URL bar
  const doPlayUrl = async ()=>{
    urlBar.classList.add('mv-hidden');
    await _mvPlayFromUrl(wid, urlInput.value, cEl);
    urlInput.value = '';
  };
  urlPlayBtn.addEventListener('click',  e=>{ e.stopPropagation(); doPlayUrl(); });
  urlInput.addEventListener('keydown',  e=>{ if(e.key==='Enter'){ e.stopPropagation(); doPlayUrl(); }});
  urlCloseBtn.addEventListener('click', e=>{ e.stopPropagation(); urlBar.classList.add('mv-hidden'); });

  // Stop — mirrors multiview.js .stop-btn listener
  cEl.querySelector('.mv-stop-btn').addEventListener('click', e=>{
    e.stopPropagation();
    _mvStopCleanup(wid, true);
  });

  // Remove widget — mirrors multiview.js .remove-widget-btn listener
  cEl.querySelector('.mv-rm-btn').addEventListener('click', e=>{
    e.stopPropagation();
    _mvStopCleanup(wid, true);
    if(gsItem) mvGrid.removeWidget(gsItem);
  });

  // Mute toggle — mirrors multiview.js muteBtn listener
  const muteBtn = cEl.querySelector('.mv-mute-btn');
  muteBtn.addEventListener('click', e=>{
    e.stopPropagation();
    videoEl.muted = !videoEl.muted;
    muteBtn.textContent = videoEl.muted ? '🔇' : '🔊';
  });

  // Play/Pause — mirrors multiview.js playPauseBtn listener
  const ppBtn = cEl.querySelector('.mv-pp-btn');
  ppBtn.addEventListener('click', e=>{
    e.stopPropagation();
    if(videoEl.paused){ videoEl.play(); ppBtn.textContent='⏸'; }
    else              { videoEl.pause(); ppBtn.textContent='▶'; }
  });
  videoEl.addEventListener('play',  ()=>{ ppBtn.textContent='⏸'; });
  videoEl.addEventListener('pause', ()=>{ ppBtn.textContent='▶'; });

  // Volume slider — mirrors multiview.js volume-slider listener
  cEl.querySelector('.mv-vol').addEventListener('input', e=>{
    e.stopPropagation();
    videoEl.volume = parseFloat(e.target.value);
    if(videoEl.volume > 0){ videoEl.muted = false; muteBtn.textContent='🔊'; }
  });

  // Fullscreen — mirrors multiview.js fullscreen-btn listener
  cEl.querySelector('.mv-fs-btn').addEventListener('click', e=>{
    e.stopPropagation();
    if(videoEl.requestFullscreen) videoEl.requestFullscreen();
    else if(videoEl.webkitRequestFullscreen) videoEl.webkitRequestFullscreen();
  });

  // ── Seek bar ─────────────────────────────────────────────────────────────
  const seekWrap = cEl.querySelector('.mv-seek-wrap');
  const seekBar  = cEl.querySelector('.mv-seek');
  const seekTime = cEl.querySelector('.mv-seek-time');

  const _fmtTime = s => {
    if(!isFinite(s)||s<0) s=0;
    const m=Math.floor(s/60), ss=Math.floor(s%60);
    return m+':'+(ss<10?'0':'')+ss;
  };
  const _syncSeek = () => {
    if(!isFinite(videoEl.duration)||videoEl.duration<=0){
      // Live — show indicator, hide the range (no seekable range)
      seekBar.style.display = 'none';
      seekTime.textContent  = '🔴 LIVE';
      return;
    }
    seekBar.style.display = '';
    seekBar.value = (videoEl.currentTime/videoEl.duration)*100;
    seekTime.textContent = _fmtTime(videoEl.currentTime)+' / '+_fmtTime(videoEl.duration);
  };
  const _tryShowSeek = () => {
    // External URL widgets: always show (LIVE indicator or VOD seek)
    // Portal channels: only show for VOD (finite duration)
    const isExt = mvExternalUrlWidgets.has(wid);
    const hasVod = isFinite(videoEl.duration) && videoEl.duration > 0 && videoEl.duration < 86400;
    if(isExt || hasVod){
      seekWrap.classList.add('mv-seek-visible');
      _syncSeek();
    }
  };

  // Show immediately if already external (e.g. re-play after quality change)
  if(mvExternalUrlWidgets.has(wid)){
    seekWrap.classList.add('mv-seek-visible');
    seekBar.style.display = 'none';
    seekTime.textContent  = '🔴 LIVE';
  }

  videoEl.addEventListener('loadedmetadata', _tryShowSeek);
  videoEl.addEventListener('durationchange', _tryShowSeek);
  videoEl.addEventListener('timeupdate', () => {
    _tryShowSeek();
    if(seekWrap.classList.contains('mv-seek-visible')) _syncSeek();
  });
  videoEl.addEventListener('emptied', () => {
    if(!mvExternalUrlWidgets.has(wid)) seekWrap.classList.remove('mv-seek-visible');
    seekBar.style.display = '';
  });

  seekBar.addEventListener('click', e => e.stopPropagation());
  seekBar.addEventListener('mousedown', e => e.stopPropagation());
  seekBar.addEventListener('touchstart', e => e.stopPropagation(), {passive:true});
  seekBar.addEventListener('input', e => {
    e.stopPropagation();
    if(isFinite(videoEl.duration) && videoEl.duration>0)
      videoEl.currentTime = (parseFloat(e.target.value)/100) * videoEl.duration;
    _syncSeek();
  });

  // ── Quality selector ──────────────────────────────────────────────────────
  const qualSel = cEl.querySelector('.mv-quality-sel');
  if(qualSel){
    qualSel.addEventListener('click',  e => e.stopPropagation());
    qualSel.addEventListener('change', e => {
      e.stopPropagation();
      const rawUrl = cEl._mvRawUrl;
      if(!rawUrl){ toast('Quality only applies to YouTube/external URLs','wrn'); return; }
      // Re-resolve and re-play with the new quality
      _mvPlayFromUrl(wid, rawUrl, cEl);
    });
  }

  // Click anywhere on widget → make it the active player
  cEl.addEventListener('click', ()=> _mvSetActive(wid));
}

// ── PLAY ─────────────────────────────────────────────────────────────────────

// mirrors multiview.js playChannelInWidget(widgetId, channel, gridstackItemContentEl)
async function _mvPlayChannel(wid, channel, cEl){
  if(!cEl) return;

  // mirrors: await stopAndCleanupPlayer(widgetId, false)  ← cleanup without UI reset
  await _mvStopCleanup(wid, false);

  const videoEl      = cEl.querySelector('.mv-video');
  const placeholder  = cEl.querySelector('.mv-placeholder');
  const titleEl      = cEl.querySelector('.mv-hdr-title');

  titleEl.textContent = channel.name || 'Channel';
  if(placeholder) placeholder.dataset.channelId = channel.id || '';

  // Show video, hide placeholder
  videoEl.classList.remove('mv-hidden');
  if(placeholder) placeholder.classList.add('mv-hidden');

  // ── Resolve the actual stream URL ──────────────────────────────────────────
  // The channel object from allItems may not have a direct URL yet —
  // it needs /api/resolve (same path as playItem in the main player).
  // mirrors multiview.js: playerUrls.set(widgetId, channel.url)
  // Strip the HEVC transcode proxy wrapper if the stored URL points at
  // /api/hls_proxy — that proxy is for the main browser player, not multiview.
  // Multiview's own ffmpeg handles HEVC via stream-copy.
  function _mvStripProxy(u){
    if(!u || !u.includes('/api/hls_proxy')) return u;
    try {
      const params = new URLSearchParams(u.split('?')[1] || '');
      return params.get('url') || u;
    } catch(e){ return u; }
  }
  let resolvedUrl = _mvStripProxy(channel._direct_url || channel._url || channel.url || '');

  if(!resolvedUrl && channel.name){
    // Need to resolve — same fetch as playItem()
    try {
      // ?mv=1 tells the server this is a multiview resolve.
      // For HEVC video: server returns raw URL + hevc:true → addon handles via &transcode=1
      // For incompatible audio (AC3/DTS): server returns hls_proxy URL + hevc:false
      //   → played directly via mpegts.js (hls_proxy outputs raw MPEG-TS)
      // Derive mode from the item itself: series/vod items have _is_show_item or _direct_url
      const _mvResolveMode = (channel.tvg_type==='series'||channel._is_show_item||channel._direct_url)
        ? (channel.tvg_type||'live') : 'live';
      const r = await fetch('/api/resolve?mv=1', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({item: channel, mode: _mvResolveMode, category: curCat || {}})
      });
      const d = await r.json();
      resolvedUrl = d.url || '';
      if(d.hevc) channel._mv_transcode = true;
      // When the server routed through hls_proxy for audio transcoding (AC3→AAC),
      // hls_proxy outputs raw MPEG-TS. Flag it so we play via mpegts.js directly,
      // bypassing the multiview_addon stream proxy (which expects a raw portal URL).
      if(resolvedUrl.includes('/api/hls_proxy')) channel._mv_hls_transcode = true;
    } catch(e){ toast('MV: resolve error: ' + e, 'err'); }
  }

  if(!resolvedUrl){
    toast('Could not resolve URL for: ' + (channel.name || '?'), 'err');
    _mvStopCleanup(wid, true); return;
  }

  // Store original URL — mirrors multiview.js: playerUrls.set(widgetId, channel.url)
  mvUrls.set(wid, resolvedUrl);

  // ── Record portal metadata for the connection-count badge ─────────────────
  // Allow caller to override portal info (e.g. when playing a custom URL)
  const portalInfo = channel._portal_override || _mvPortalKeyFromUrl(resolvedUrl);
  mvPortalMeta.set(wid, {
    portalKey:  portalInfo.key,
    portalName: portalInfo.name,
  });
  // Refresh all badges (connection counts change when this widget starts)
  _mvUpdatePortalBadges();

  // ── Audio-transcoded stream: play hls_proxy MPEG-TS directly via mpegts.js ─
  // When /api/resolve returned an /api/hls_proxy URL (e.g. EAC3→AAC transcode),
  // the proxy outputs raw MPEG-TS (-f mpegts). Use mpegts.js directly on that
  // local URL — no need to pass through the multiview_addon stream proxy, which
  // expects a portal URL, not a local proxy URL.
  if(channel._mv_hls_transcode){
    if(typeof mpegts === 'undefined' || !mpegts.isSupported()){
      toast('Browser does not support MSE — cannot play transcoded stream', 'err');
      _mvStopCleanup(wid, true); return;
    }
    // isLive: true for live channels, false for VOD/series (enables seek bar + finite duration)
    const _hlsIsLive = channel._is_live !== false && !channel._direct_url && channel.tvg_type !== 'movie' && channel.tvg_type !== 'series';
    const player = mpegts.createPlayer({
      type:   'mse',
      isLive: _hlsIsLive,
      url:    resolvedUrl,
    }, {
      enableStashBuffer: true,
      stashInitialSize:  4096,
    });
    player.on(mpegts.Events.ERROR, (errType, errDetail)=>{
      console.error('[MV/transcode] mpegts error wid='+wid, errType, errDetail);
      if(document.getElementById('p-mv').classList.contains('mv-active'))
        toast('Stream error: '+(channel.name||wid),'err');
      _mvStopCleanup(wid, true);
    });
    mvPlayers.set(wid, player);
    player.attachMediaElement(videoEl);
    player.load();
    try {
      await player.play();
      const muteBtn = cEl.querySelector('.mv-mute-btn');
      if(mvPlayers.size === 1){ videoEl.muted=false; if(muteBtn) muteBtn.textContent='🔊'; }
      else if(muteBtn) muteBtn.textContent = videoEl.muted?'🔇':'🔊';
    } catch(e){ console.warn('[MV/transcode] play() error', e); }
    _mvUpdateSeekBar(wid);
    return;
  }

  // ── Build the proxy stream URL ─────────────────────────────────────────────
  // mirrors server.js /stream GET handler stream key:
  //   streamKey = `${userId}::${streamUrl}::${profileId}`
  // We route through multiview_addon.py /api/multiview/stream which handles
  // dedup and reference counting server-side.
  const proxyUrl = '/api/multiview/stream?'
    + 'url='        + encodeURIComponent(resolvedUrl)
    + '&client_id=' + encodeURIComponent(mvClientId)
    + (channel._mv_transcode ? '&transcode=1' : '');

  // ── Create mpegts.js player ────────────────────────────────────────────────
  // mirrors multiview.js mpegts.createPlayer block exactly
  if(typeof mpegts === 'undefined' || !mpegts.isSupported()){
    toast('Browser does not support MSE — cannot use Multi-View', 'err');
    _mvStopCleanup(wid, true); return;
  }

  // mirrors multiview.js mpegtsConfig
  // Use isLive=false for VOD content (e.g. YouTube VOD) so mpegts exposes
  // a finite duration and the seek bar works correctly.
  const _mpIsLive = channel._is_live !== false;  // default true for IPTV; false for VOD
  const player = mpegts.createPlayer({
    type:   'mse',
    isLive: _mpIsLive,
    url:    proxyUrl
  }, {
    enableStashBuffer: true,
    stashInitialSize:  4096,
    liveBufferLatency: 2.0,
  });

  // mirrors multiview.js player.on(mpegts.Events.ERROR ...)
  player.on(mpegts.Events.ERROR, (errType, errDetail)=>{
    console.error('[MV] mpegts error wid=' + wid, errType, errDetail);
    // Only toast if the panel is still open (don't spam after mvClose)
    if(document.getElementById('p-mv').classList.contains('mv-active'))
      toast('Stream error: ' + (channel.name||wid), 'err');
    _mvStopCleanup(wid, true);
  });

  // mirrors multiview.js: players.set(widgetId, player)
  mvPlayers.set(wid, player);
  player.attachMediaElement(videoEl);
  player.load();

  try {
    await player.play();
    // Unmute automatically when this is the only/first active player.
    // Browsers require `muted` on the <video> element for autoplay to work,
    // so we unmute here after playback has started. Subsequent widgets stay
    // muted to avoid audio clashing; the user can unmute them manually.
    const muteBtn = cEl.querySelector('.mv-mute-btn');
    if(mvPlayers.size === 1){
      videoEl.muted = false;
      if(muteBtn) muteBtn.textContent = '🔊';
    } else {
      // Make sure btn reflects actual state
      if(muteBtn) muteBtn.textContent = videoEl.muted ? '🔇' : '🔊';
    }
    _mvSetActive(wid);
  } catch(e){
    // mirrors multiview.js: if(err.name !== 'AbortError')
    if(e && e.name !== 'AbortError'){
      console.error('[MV] play() error wid=' + wid, e);
      if(document.getElementById('p-mv').classList.contains('mv-active'))
        toast('Could not play: ' + (channel.name||wid), 'err');
      _mvStopCleanup(wid, true);
    }
  }
}

// ── STOP / CLEANUP ────────────────────────────────────────────────────────────

// mirrors multiview.js stopAndCleanupPlayer(widgetId, resetUI)
async function _mvStopCleanup(wid, resetUI){
  // 1. Tell the server to kill the ffmpeg process for this widget.
  //
  //    RACE CONDITION FIX:
  //    We must AWAIT this request before _mvPlayChannel starts a new ffmpeg
  //    process for the same widget.  If we fire-and-forget, the old ffmpeg is
  //    still connected to the IPTV source when the new one starts — two
  //    simultaneous connections to the same portal account → the provider kills
  //    one of them (the "1-connection limit" symptom).
  //
  //    multiview.js reference (stopAndCleanupPlayer / stopStream in api.js):
  //      stopPromises.push(stopStream(originalUrl));
  //      await Promise.all(stopPromises);   ← server stop IS awaited
  //
  //    mirrors server.js POST /api/stream/stop reference guard:
  //      if (activeStreamInfo.references > 1) → kept alive for other widgets
  if(mvUrls.has(wid)){
    const url = mvUrls.get(wid);
    mvUrls.delete(wid);
    // Await so old ffmpeg is confirmed dead before caller starts a new one.
    await fetch('/api/multiview/stream/stop', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url: url, client_id: mvClientId })
    }).catch(()=>{});
  }

  // 2. Clear portal metadata and refresh connection-count badges.
  // Only remove the external-URL flag when the user explicitly stops/removes
  // the widget (resetUI=true). Internal cleanup (resetUI=false, called before
  // re-playing) must preserve the flag so the badge stays correct.
  if(resetUI) mvExternalUrlWidgets.delete(wid);
  if(mvPortalMeta.has(wid)){
    mvPortalMeta.delete(wid);
    _mvUpdatePortalBadges();
  }

  // 3. Destroy mpegts player — fire-and-forget to prevent blocking.
  //    mirrors multiview.js: Promise.resolve().then(() => { player.destroy() })
  if(mvPlayers.has(wid)){
    const player = mvPlayers.get(wid);
    mvPlayers.delete(wid);  // remove from map immediately
    Promise.resolve().then(()=>{
      try { player.pause(); player.unload(); player.detachMediaElement(); player.destroy(); }
      catch(e){ /* non-critical */ }
    });
  }

  // 4. Reset UI
  if(resetUI){
    const cEl = document.getElementById('mwc-' + wid);
    if(cEl){
      const videoEl     = cEl.querySelector('.mv-video');
      const placeholder = cEl.querySelector('.mv-placeholder');
      const titleEl     = cEl.querySelector('.mv-hdr-title');
      const portalBadge = cEl.querySelector('.mv-hdr-portal');
      if(videoEl){ videoEl.src=''; videoEl.removeAttribute('src'); videoEl.load(); videoEl.classList.add('mv-hidden'); }
      if(placeholder){ placeholder.classList.remove('mv-hidden'); placeholder.dataset.channelId=''; }
      if(titleEl) titleEl.textContent = 'No Channel';
      if(portalBadge){ portalBadge.textContent=''; portalBadge.className='mv-hdr-portal'; }
      cEl.classList.remove('mv-active-player');
    }
    if(mvActiveId === wid) mvActiveId = null;
  }
}

// ── ACTIVE PLAYER ─────────────────────────────────────────────────────────────

// mirrors multiview.js setActivePlayer(widgetId)
// Only updates the visual highlight (active border) — does NOT touch mute state.
// Each player controls its own audio independently via its 🔊/🔇 button.
// Removing auto-mute prevents the jarring behaviour where clicking any control
// on Player B silently kills audio on Player A.
function _mvSetActive(wid){
  if(mvActiveId === wid) return;

  // Remove highlight from old active player (audio untouched)
  if(mvActiveId){
    const oldEl = document.getElementById('mwc-' + mvActiveId);
    if(oldEl) oldEl.classList.remove('mv-active-player');
  }

  // Add highlight to new active player (audio untouched)
  const newEl = document.getElementById('mwc-' + wid);
  if(newEl) newEl.classList.add('mv-active-player');

  mvActiveId = wid;
}

// ── REMOVE LAST PLAYER ────────────────────────────────────────────────────────
// mirrors multiview.js removeLastPlayer()
async function _mvRemoveLast(){
  const items = mvGrid.getGridItems();
  if(!items.length){ toast('No players to remove', 'wrn'); return; }
  // Sort by timestamp embedded in widget id (same sort as multiview.js)
  const sorted = items.slice().sort((a,b)=>{
    const ta = parseInt((a.gridstackNode.id||'0').split('-')[1]||0);
    const tb = parseInt((b.gridstackNode.id||'0').split('-')[1]||0);
    return ta - tb;
  });
  const last = sorted[sorted.length-1];
  if(!last) return;
  const ph  = last.querySelector('.mv-placeholder');
  const wid = ph ? ph.id : last.gridstackNode.id;
  await _mvStopCleanup(wid, false);
  mvGrid.removeWidget(last);
}

// ── VISIBILITY CHANGE ─────────────────────────────────────────────────────────
//
// GOAL: audio and video must keep playing even when the user switches to
// another tab or alt-tabs away from the window.
//
// Strategy:
//   • When hidden  → do NOTHING.  The browser may throttle JS timers but
//     mpegts.js feeds its video element directly from an MSE SourceBuffer
//     which the browser will not suspend mid-stream for an active video
//     element.  We deliberately do NOT mute or pause anything here.
//
//   • When visible → if the browser suspended/paused the active player's
//     <video> (seen on some Chromium builds with aggressive background
//     throttling), we resume it immediately so the user hears sound right away.
//
document.addEventListener('visibilitychange', ()=>{
  if(document.hidden) return;   // ← tab hidden: leave everything alone
  if(!document.getElementById('p-mv').classList.contains('mv-active')) return;

  // Tab became visible again — resume ALL players the browser may have paused
  // or re-muted during background throttling.  Since each player now controls
  // its own audio independently, we restore each one to its pre-hide state:
  // paused players stay paused; playing-but-muted players stay muted.
  for(const [wid, player] of mvPlayers.entries()){
    const cEl = document.getElementById('mwc-' + wid);
    if(!cEl) continue;
    const v  = cEl.querySelector('.mv-video');
    if(!v || v.ended) continue;

    // If the browser silently muted a video that the user had unmuted, restore it.
    // We infer the user's intent from the mute-button label.
    const mb = cEl.querySelector('.mv-mute-btn');
    const userWantsAudio = mb && mb.textContent === '🔊';
    if(userWantsAudio && v.muted){
      v.muted = false;
    }

    // Resume playback if the browser suspended the element while it was playing.
    if(v.paused && !v.ended && !v.muted){
      v.play().catch(()=>{});
    }
  }
});

// mirrors multiview.js applyPresetLayout() exactly — same coordinates, same logic
function _mvApplyPreset(name){
  const numPlayers = mvGrid.getGridItems().length;

  if(name==='auto' && numPlayers===0){ _mvAddWidget(); return; }

  const doApply = async ()=>{
    // mirrors cleanupMultiView() then batch-add
    const stops = Array.from(mvPlayers.keys()).map(id => _mvStopCleanup(id, false));
    await Promise.all(stops);
    mvPlayers.clear(); mvUrls.clear(); mvActiveId = null;
    if(mvGrid) mvGrid.removeAll();

    let layout = [];

    if(name==='auto'){
      let cols, rows;
      if(numPlayers<=1){cols=1;rows=1;}
      else if(numPlayers===2){cols=2;rows=1;}
      else if(numPlayers===3){cols=3;rows=1;}
      else if(numPlayers===4){cols=2;rows=2;}
      else if(numPlayers<=6){cols=3;rows=2;}
      else{cols=3;rows=3;}
      const ww = Math.floor(12/cols);
      const wh = Math.floor(10/rows);  // match 1+1/1+2 which use h:10
      for(let i=0;i<numPlayers;i++){
        layout.push({x:(i%cols)*ww, y:Math.floor(i/cols)*wh, w:ww, h:wh});
      }
    } else if(name==='1+1'){
      if(window.innerWidth < 900){
        layout = [{x:0,y:0,w:12,h:5},{x:0,y:5,w:12,h:5}];
      } else {
        layout = [{x:0,y:0,w:6,h:10},{x:6,y:0,w:6,h:10}];
      }
    } else if(name==='1+2'){
      layout = [{x:0,y:0,w:8,h:10},
                {x:8,y:0,w:4,h:5},{x:8,y:5,w:4,h:5}];
    }

    mvGrid.batchUpdate();
    try { layout.forEach(ld => _mvAddWidget(null, ld)); }
    finally { mvGrid.commit(); setTimeout(_mvFitCellHeight, 50); }
  };

  if(numPlayers > 0){
    _mvConfirm(
      'Apply \'' + name + '\' Layout?',
      'This will stop all current streams and apply the new layout. Are you sure?',
      doApply
    );
  } else {
    doApply();
  }
}

// ── CHANNEL SELECTOR ─────────────────────────────────────────────────────────

// mirrors multiview.js populateChannelSelector()
// ── CHANNEL SELECTOR — FULL CATEGORY BROWSER ─────────────────────────────────
//
// Three-level navigation:
//   cats      → category list (tabs: Live / VOD / Series)
//   items     → item list for a category (channels / VOD titles / show containers)
//   episodes  → episode list for a show item (after clicking Eps)
//
// Each level has a Back button that goes up one level.

let _mvSelNavMode     = 'cats';    // 'cats' | 'items' | 'episodes'
let _mvSelContentMode = 'live';    // 'live' | 'vod' | 'series'
let _mvSelCat         = null;      // current category
let _mvSelItems       = [];        // items for current category
let _mvSelShowItem    = null;      // show item whose episodes are being browsed
let _mvSelEpisodes    = [];        // episodes loaded for _mvSelShowItem

// Backward-compat alias
Object.defineProperty(window, '_mvSelMode', {
  get(){ return _mvSelNavMode; },
  set(v){ _mvSelNavMode = v; }
});

function _mvSelSetMode(mode){
  if(_mvSelContentMode === mode) return;
  _mvSelContentMode = mode;
  _mvSelNavMode = 'cats';
  _mvSelCat = null; _mvSelItems = []; _mvSelShowItem = null; _mvSelEpisodes = [];
  document.getElementById('mv-sel-search').value = '';
  document.querySelectorAll('.mv-sel-tab').forEach(b=>{
    b.classList.toggle('active', b.dataset.mode === mode);
  });
  _mvRenderSel();
}

function _mvPopulateSelector(){
  _mvSelNavMode = 'cats';
  _mvSelCat = null; _mvSelItems = []; _mvSelShowItem = null; _mvSelEpisodes = [];
  document.getElementById('mv-sel-search').value = '';
  document.querySelectorAll('.mv-sel-tab').forEach(b=>{
    b.classList.toggle('active', b.dataset.mode === _mvSelContentMode);
  });
  _mvCloseCtxMenu();
  _mvRenderSel();
  document.getElementById('mv-sel-search').oninput = ()=> _mvRenderSel();
}

// ── tiny inline context menu helpers ─────────────────────────────────────────
function _mvCloseCtxMenu(){
  const m = document.getElementById('mv-item-ctx');
  if(m){ m.classList.remove('open'); m.innerHTML=''; }
}
function _mvOpenCtxMenu(btn, actions){
  _mvCloseCtxMenu();
  const m = document.getElementById('mv-item-ctx');
  if(!m) return;
  m.innerHTML = actions.map(a=>
    `<button onclick="${a.fn}">${a.icon} ${esc(a.label)}</button>`
  ).join('');
  m.classList.add('open');
  // Use fixed viewport coords — the menu is position:fixed so it escapes
  // the modal's overflow:hidden and positions relative to the viewport.
  const r  = btn.getBoundingClientRect();
  const mw = 190;
  const mh = actions.length * 36 + 8;  // estimated height
  // Right-align to button by default; shift left if it would overflow viewport
  let left = r.right - mw;
  let top  = r.bottom + 2;
  if(left < 8) left = 8;
  if(top + mh > window.innerHeight - 8) top = r.top - mh - 2;
  m.style.left = left + 'px';
  m.style.top  = top  + 'px';
  // Close on outside click
  setTimeout(()=> document.addEventListener('click', _mvCtxOutside, {once:true}), 0);
}
function _mvCtxOutside(e){
  const m = document.getElementById('mv-item-ctx');
  if(m && !m.contains(e.target)) _mvCloseCtxMenu();
}

// ── shared row builder ────────────────────────────────────────────────────────
function _mvBuildItemRow(it, i, forEpisodes){
  const name    = it.name || it.o_name || it.title || 'Unknown';
  // Logo: check all fields; fallback to parent show logo for episodes
  const rawLogo = it.logo || it.stream_icon || it.cover || it.screenshot_uri || it.pic || '';
  const logoSrc = rawLogo && rawLogo.startsWith('http')
    ? '/api/proxy?url='+encodeURIComponent(rawLogo) : (rawLogo||'');
  const isShow  = !forEpisodes && (it._is_show_item || it._is_series_group);
  const isGroup = !forEpisodes && !!it._is_series_group;
  const epCount = isGroup ? (it._episodes||[]).length : 0;
  const isSeries = _mvSelContentMode === 'series' || _mvSelContentMode === 'vod';

  const logoHtml = logoSrc
    ? `<img class="mv-ch-logo" src="${esc(logoSrc)}" loading="lazy" onerror="this.style.display='none'">`
    : `<span class="mv-ch-logo" style="background:var(--s4);display:flex;align-items:center;justify-content:center;font-size:13px">${isShow?'📺':'🎬'}</span>`;

  // Action buttons (visible on hover)
  let btns = '';
  if(isGroup){
    btns += `<button class="btn-ghost" onclick="event.stopPropagation();_mvSelDrillGrp(${i})" title="Browse episodes">${epCount} eps</button>`;
  } else if(isShow && isSeries){
    btns += `<button class="btn-ghost" onclick="event.stopPropagation();_mvSelDrillShow(${i})" title="Browse episodes">Eps</button>`;
  }
  if(!isShow && !isGroup){
    // Directly playable — play button
    btns += `<button class="btn-blue" style="height:24px;padding:0 8px;font-size:11px" onclick="event.stopPropagation();_mvSelPickItem(${i})" title="Play in Multi-View">▶</button>`;
  }
  // Submenu ⋮ — always shown
  btns += `<button class="btn-ghost" style="padding:0 5px;font-size:16px;line-height:1" onclick="event.stopPropagation();_mvSelOpenItemMenu(${i},this)" title="More options">⋮</button>`;

  const drillArrow = (isShow||isGroup) ? `<span style="color:var(--txt3);font-size:14px;flex-shrink:0">›</span>` : '';

  return `<div class="mv-ch-row" data-ii="${i}" data-show="${isShow||isGroup?1:0}">
    ${logoHtml}
    <span class="mv-ch-name"><span class="iname-inner">${esc(name)}</span></span>
    <div class="mv-item-btns">${btns}</div>
    ${drillArrow}
  </div>`;
}

// Pick item (play in multiview) — called from play button or clicking a playable row.
// Index i always refers to the currently-displayed (filtered) list at the active level.
function _mvSelPickItem(i){
  let it;
  if(_mvSelNavMode === 'episodes'){
    // _mvSelEpisodesFiltered is the filtered subset actually rendered — index matches display
    it = (_mvSelEpisodesFiltered.length ? _mvSelEpisodesFiltered : _mvSelEpisodes)[i];
  } else {
    it = (_mvSelFilteredItems.length ? _mvSelFilteredItems : _mvSelItems)[i];
  }
  if(!it) return;
  _mvCloseCtxMenu();
  document.getElementById('mv-sel-overlay').classList.remove('open');
  if(mvSelCallback){ mvSelCallback(it); mvSelCallback=null; }
}

// Open context submenu for an item
function _mvSelOpenItemMenu(i, btn){
  let it;
  if(_mvSelNavMode === 'episodes'){
    it = (_mvSelEpisodesFiltered.length ? _mvSelEpisodesFiltered : _mvSelEpisodes)[i];
  } else {
    it = (_mvSelFilteredItems.length ? _mvSelFilteredItems : _mvSelItems)[i];
  }
  if(!it) return;
  const isShow  = it._is_show_item || it._is_series_group;
  const isGroup = !!it._is_series_group;
  const isSeries = _mvSelContentMode === 'series' || _mvSelContentMode === 'vod';
  const actions = [];
  // "Play in Multi-View" is intentionally omitted — the ▶ button in the row
  // already does this; duplicating it in the submenu adds no value.
  if(isGroup){
    actions.push({icon:'📋', label:`Browse ${(it._episodes||[]).length} eps`, fn:`_mvCloseCtxMenu();_mvSelDrillGrp(${i})`});
  } else if(isShow && isSeries){
    actions.push({icon:'📋', label:'Browse episodes', fn:`_mvCloseCtxMenu();_mvSelDrillShow(${i})`});
  }
  // TMDB/IMDb — same lookup logic as main browse (direct link when ID available,
  // falls back to name search only when no ID exists anywhere in the item)
  if(_mvSelContentMode !== 'live'){
    actions.push({icon:'🎬', label:'Open TMDB/IMDb',
      fn:`_mvSelIMDb(${i});_mvCloseCtxMenu()`});
  }
  _mvOpenCtxMenu(btn, actions);
}

// TMDB/IMDb lookup for multiview — resolves item by index then delegates to
// _iMenuIMDBOpen with the current content mode so it behaves identically to
// the main browse: direct IMDB/TMDB link when an ID is found, name search fallback.
function _mvSelIMDb(i){
  let it;
  if(_mvSelNavMode === 'episodes'){
    it = (_mvSelEpisodesFiltered.length ? _mvSelEpisodesFiltered : _mvSelEpisodes)[i];
  } else {
    it = (_mvSelFilteredItems.length ? _mvSelFilteredItems : _mvSelItems)[i];
  }
  if(!it) return;
  _iMenuIMDBOpen(it, _mvSelContentMode);
}

// Drill into a _is_series_group (M3U grouped episodes — no network call needed)
function _mvSelDrillGrp(i){
  const it = (_mvSelFilteredItems.length ? _mvSelFilteredItems : _mvSelItems)[i];
  if(!it) return;
  _mvSelShowItem   = it;
  _mvSelEpisodes   = it._episodes || [];
  _mvSelNavMode    = 'episodes';
  document.getElementById('mv-sel-search').value = '';
  _mvRenderSel();
}

// Drill into a _is_show_item (needs /api/episodes fetch)
async function _mvSelDrillShow(i){
  const it = (_mvSelFilteredItems.length ? _mvSelFilteredItems : _mvSelItems)[i];
  if(!it) return;
  _mvSelShowItem = it;
  _mvSelNavMode  = 'episodes';
  _mvSelEpisodes = [];
  document.getElementById('mv-sel-search').value = '';

  document.getElementById('mv-sel-list').innerHTML =
    '<div style="text-align:center;padding:24px;color:var(--txt3);font-size:12px">Loading episodes…</div>';
  document.getElementById('mv-sel-title').textContent = it.name || 'Episodes';
  document.getElementById('mv-sel-back').style.display = '';

  const parentLogo = it.logo||it.stream_icon||it.cover||it.screenshot_uri||it.pic||
    _mvSelCat?.logo||_mvSelCat?.screenshot_uri||'';

  try {
    const r = await fetch('/api/episodes', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({item:it, mode:_mvSelContentMode,
        cat_id:_mvSelCat?.id||'', cat_title:_mvSelCat?.title||'',
        parent_logo:parentLogo})
    });
    const d = await r.json();
    _mvSelEpisodes = d.episodes || [];
    // Propagate parent logo to episodes that have none
    if(parentLogo){
      _mvSelEpisodes.forEach(ep=>{
        if(!ep.logo&&!ep.stream_icon&&!ep.cover&&!ep.screenshot_uri&&!ep.pic)
          ep.logo = parentLogo;
      });
    }
  } catch(e){
    _mvSelEpisodes = [];
    toast('Could not load episodes: ' + (it.name||'?'), 'err');
  }
  _mvRenderSel();
}

// Mutable refs so pick/submenu handlers can reach the current filtered list
let _mvSelFilteredItems = [];
let _mvSelEpisodesFiltered = [];

function _mvRenderSel(){
  const listEl  = document.getElementById('mv-sel-list');
  const titleEl = document.getElementById('mv-sel-title');
  const backBtn = document.getElementById('mv-sel-back');
  const tabsEl  = document.getElementById('mv-sel-tabs');
  const q       = document.getElementById('mv-sel-search').value.trim().toLowerCase();
  _mvCloseCtxMenu();

  // ── EPISODES level ─────────────────────────────────────────────────────────
  if(_mvSelNavMode === 'episodes'){
    titleEl.textContent   = _mvSelShowItem ? (_mvSelShowItem.name||'Episodes') : 'Episodes';
    backBtn.style.display = '';
    if(tabsEl) tabsEl.style.display = 'none';
    document.getElementById('mv-sel-search').placeholder = 'Search episodes…';
    const _pRow = document.getElementById('mv-sel-play-url-row');
    if(_pRow) _pRow.style.display = 'none';

    const eps = q ? _mvSelEpisodes.filter(ep=>(ep.name||ep.title||'').toLowerCase().includes(q)) : _mvSelEpisodes;
    _mvSelEpisodesFiltered = eps;

    if(!eps.length){
      listEl.innerHTML = '<div style="text-align:center;padding:16px;color:var(--txt3);font-size:12px">'
        + (_mvSelEpisodes.length ? 'No episodes match' : 'Loading…') + '</div>';
      return;
    }
    listEl.innerHTML = eps.map((ep,i)=> _mvBuildItemRow(ep, i, true)).join('');
    listEl.querySelectorAll('.mv-ch-row').forEach(row=>{
      row.addEventListener('click', e=>{
        if(e.target.closest('.mv-item-btns')) return; // buttons handle their own clicks
        _mvSelPickItem(parseInt(row.dataset.ii));
      });
    });
    return;
  }

  // ── ITEMS level ────────────────────────────────────────────────────────────
  if(_mvSelNavMode === 'items'){
    const _pRow = document.getElementById('mv-sel-play-url-row');
    if(_pRow) _pRow.style.display = 'none';
    if(tabsEl) tabsEl.style.display = 'none';
    titleEl.textContent   = _mvSelCat ? (_mvSelCat.title||'Items') : 'Items';
    backBtn.style.display = '';
    document.getElementById('mv-sel-search').placeholder = 'Search…';

    const filtered = q ? _mvSelItems.filter(it=>(it.name||it.o_name||it.title||'').toLowerCase().includes(q)) : _mvSelItems;
    _mvSelFilteredItems = filtered;

    if(!filtered.length){
      listEl.innerHTML = '<div style="text-align:center;padding:16px;color:var(--txt3);font-size:12px">'
        + (_mvSelItems.length ? 'No items match' : 'Loading…') + '</div>';
      return;
    }
    listEl.innerHTML = filtered.map((it,i)=> _mvBuildItemRow(it, i, false)).join('');
    listEl.querySelectorAll('.mv-ch-row').forEach(row=>{
      row.addEventListener('click', e=>{
        if(e.target.closest('.mv-item-btns')) return;
        const it = filtered[parseInt(row.dataset.ii)];
        if(!it) return;
        const isShow  = it._is_show_item || it._is_series_group;
        const isSeries = _mvSelContentMode === 'series' || _mvSelContentMode === 'vod';
        if(it._is_series_group){ _mvSelDrillGrp(parseInt(row.dataset.ii)); return; }
        if(isShow && isSeries){  _mvSelDrillShow(parseInt(row.dataset.ii)); return; }
        _mvSelPickItem(parseInt(row.dataset.ii));
      });
    });
    return;
  }

  // ── CATS level ─────────────────────────────────────────────────────────────
  const modeLabel = {live:'Live',vod:'VOD',series:'Series'}[_mvSelContentMode]||'';
  titleEl.textContent   = 'Browse ' + modeLabel + ' Categories';
  backBtn.style.display = 'none';
  if(tabsEl) tabsEl.style.display = '';
  document.getElementById('mv-sel-search').placeholder = 'Search categories…';

  // ── Play URL row (live only) ──────────────────────────────────────────────
  const playUrlRowId = 'mv-sel-play-url-row';
  let playUrlRow = document.getElementById(playUrlRowId);
  if(!playUrlRow){
    playUrlRow = document.createElement('div');
    playUrlRow.id = playUrlRowId;
    playUrlRow.className = 'mv-sel-play-url-row';
    playUrlRow.innerHTML =
      '<span style="font-size:14px;flex-shrink:0">🔗</span>'
      +'<input id="mv-sel-play-url-inp" class="mv-sel-play-url-inp" type="text" inputmode="url"'
      +' placeholder="Paste URL to play directly…" autocomplete="off" autocorrect="off" spellcheck="false">'
      +'<button id="mv-sel-play-url-btn" style="height:26px;padding:0 9px;font-size:11px;white-space:nowrap;'
      +'flex-shrink:0;background:rgba(239,68,68,.15);color:var(--red);border:1px solid rgba(239,68,68,.35);'
      +'border-radius:3px;cursor:pointer">▶ Play</button>';
    listEl.parentElement.insertBefore(playUrlRow, listEl);
    const inp = playUrlRow.querySelector('#mv-sel-play-url-inp');
    const doMvPlayUrl = async ()=>{
      const url = (inp.value||'').trim();
      if(!url){ toast('Enter a URL','wrn'); return; }
      inp.value='';
      document.getElementById('mv-sel-overlay').classList.remove('open');
      const ctx = _mvSelWidgetCtx;
      mvSelCallback = null; _mvSelWidgetCtx = null;
      if(ctx) await _mvPlayFromUrl(ctx.wid, url, ctx.cEl);
    };
    playUrlRow.querySelector('#mv-sel-play-url-btn').addEventListener('click', e=>{ e.stopPropagation(); doMvPlayUrl(); });
    inp.addEventListener('keydown', e=>{ if(e.key==='Enter'){ e.stopPropagation(); doMvPlayUrl(); }});
    inp.addEventListener('click', e=> e.stopPropagation());
  }
  playUrlRow.style.display = _mvSelContentMode === 'live' ? '' : 'none';

  const cats = (catsCache && catsCache[_mvSelContentMode]) ? catsCache[_mvSelContentMode] : [];
  if(!cats || !cats.length){
    listEl.innerHTML = '<div style="text-align:center;padding:24px;color:var(--txt3);font-size:12px">No categories — connect to a portal first</div>';
    return;
  }
  const filtered = q ? cats.filter(c=>(c.title||'').toLowerCase().includes(q)) : cats;
  if(!filtered.length){
    listEl.innerHTML = '<div style="text-align:center;padding:16px;color:var(--txt3);font-size:12px">No categories match</div>';
    return;
  }
  listEl.innerHTML = filtered.map((c,i)=>`
    <div class="mv-ch-row" data-ci="${i}" style="cursor:pointer">
      <span class="mv-ch-logo" style="font-size:18px;background:none;display:flex;align-items:center;justify-content:center">${
        _mvSelContentMode==='vod'?'🎬':_mvSelContentMode==='series'?'📺':'📁'}</span>
      <span class="mv-ch-name"><span class="iname-inner">${esc(c.title||'?')}</span></span>
      <span style="color:var(--txt3);font-size:14px;flex-shrink:0">›</span>
    </div>`).join('');
  listEl.querySelectorAll('.mv-ch-row').forEach(row=>{
    row.addEventListener('click', ()=>{
      const cat = filtered[parseInt(row.dataset.ci)];
      if(cat) _mvSelOpenCat(cat);
    });
  });
}

async function _mvSelOpenCat(cat){
  _mvSelCat     = cat;
  _mvSelNavMode = 'items';
  _mvSelItems   = [];
  document.getElementById('mv-sel-search').value = '';

  document.getElementById('mv-sel-list').innerHTML =
    '<div style="text-align:center;padding:24px;color:var(--txt3);font-size:12px">Loading…</div>';
  document.getElementById('mv-sel-title').textContent = cat.title || 'Items';
  document.getElementById('mv-sel-back').style.display = '';
  const tabsEl = document.getElementById('mv-sel-tabs');
  if(tabsEl) tabsEl.style.display = 'none';

  const mode = _mvSelContentMode;
  const key  = _categoryKey(mode, cat);
  categoryItemsCache[mode] = categoryItemsCache[mode] || {};

  if(categoryItemsCache[mode][key]){
    _mvSelItems = categoryItemsCache[mode][key];
    _mvRenderSel();
    return;
  }

  try {
    const r = await fetch('/api/items', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({category: cat, mode: mode, browse: true})
    });
    const d = await r.json();
    _mvSelItems = d.items || [];
    categoryItemsCache[mode][key] = _mvSelItems;
  } catch(e){
    _mvSelItems = [];
    toast('Could not load category: ' + (cat.title||'?'), 'err');
  }

  _mvRenderSel();
}

// ── LAYOUT PERSISTENCE ────────────────────────────────────────────────────────

// mirrors multiview.js loadLayouts() → populateLayoutsDropdown()
async function _mvLoadLayouts(){
  try {
    const r = await fetch('/api/multiview/layouts');
    if(!r.ok) return;
    const layouts = await r.json();
    const sel = document.getElementById('mv-layouts-sel');
    sel.innerHTML = '<option value="" disabled selected>Load layout…</option>';
    layouts.forEach(l=>{
      const opt = document.createElement('option');
      opt.value       = l.id;
      opt.textContent = l.name;
      sel.appendChild(opt);
    });
    // Store on window for load callback
    window._mvLayouts = layouts;
  } catch(e){ console.warn('[MV] loadLayouts error', e); }
}

// Auto-restore the last grid layout after re-opening multiview.
// Primary source: session snapshot saved by mvClose() to localStorage.
// Fallback: last explicitly loaded named layout (mv_last_layout_id).
async function _mvAutoRestoreLayout(){
  try {
    // Try session snapshot first — this always reflects the exact layout
    // the user had when they closed, even if they never saved a named layout.
    const raw = localStorage.getItem('mv_session_layout');
    if(raw){
      const snapshot = JSON.parse(raw);
      if(Array.isArray(snapshot) && snapshot.length){
        // Clear any existing widgets before restoring to avoid duplicates
        mvGrid.removeAll();
        const toRestore = snapshot.slice(0, MV_MAX); // cap to max
        mvGrid.batchUpdate();
        try { toRestore.forEach(ld => _mvAddWidget(null, ld)); }
        finally { mvGrid.commit(); setTimeout(_mvFitCellHeight, 50); }
        _mvTbCollapseIfMobile();
        return;
      }
    }
    // Fallback: last manually loaded named layout
    const lastId = parseInt(localStorage.getItem('mv_last_layout_id') || '0');
    if(!lastId) return;
    const layout = (window._mvLayouts||[]).find(l=> l.id === lastId);
    if(!layout) return;
    mvGrid.removeAll();
    const toRestore = (layout.layout_data||[]).slice(0, MV_MAX);
    mvGrid.batchUpdate();
    try { toRestore.forEach(ld => _mvAddWidget(null, ld)); }
    finally { mvGrid.commit(); setTimeout(_mvFitCellHeight, 50); }
    const sel = document.getElementById('mv-layouts-sel');
    if(sel) sel.value = lastId;
    _mvTbCollapseIfMobile();
  } catch(e){ console.warn('[MV] autoRestoreLayout error', e); }
}

// mirrors multiview.js saveLayout()
async function _mvSaveLayout(){
  const name = document.getElementById('mv-save-name').value.trim();
  if(!name){ toast('Layout name required', 'wrn'); return; }

  const items = mvGrid.getGridItems();
  if(!items.length){ toast('No players to save', 'wrn'); return; }

  const layoutData = items.map(item=>{
    const node = item.gridstackNode;
    const ph   = item.querySelector('.mv-placeholder');
    return { x:node.x, y:node.y, w:node.w, h:node.h,
             id: ph?.id || node.id,
             channelId: ph?.dataset.channelId || null };
  });

  try {
    const r = await fetch('/api/multiview/layouts',{
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name, layout_data: layoutData})
    });
    if(r.ok){
      toast('Layout saved: ' + name, 'ok');
      document.getElementById('mv-save-overlay').classList.remove('open');
      document.getElementById('mv-save-name').value = '';
      _mvLoadLayouts();
      _mvTbCollapseIfMobile();
    }
  } catch(e){ toast('Save failed: ' + e, 'err'); }
}

// mirrors multiview.js loadSelectedLayout()
function _mvLoadSelected(){
  const sel = document.getElementById('mv-layouts-sel');
  const id  = parseInt(sel.value);
  if(!id) return;
  const layout = (window._mvLayouts||[]).find(l=> l.id === id);
  if(!layout){ toast('Layout not found', 'err'); return; }

  _mvConfirm(
    'Load \'' + layout.name + '\'?',
    'This will stop all current streams and load the selected layout.',
    async ()=>{
      const stops = Array.from(mvPlayers.keys()).map(wid => _mvStopCleanup(wid, false));
      await Promise.all(stops);
      mvPlayers.clear(); mvUrls.clear(); mvActiveId = null;
      mvGrid.removeAll();

      mvGrid.batchUpdate();
      try { layout.layout_data.forEach(ld => _mvAddWidget(null, ld)); }
      finally { mvGrid.commit(); setTimeout(_mvFitCellHeight, 50); }
      // Remember this layout so it auto-restores on next open
      try { localStorage.setItem('mv_last_layout_id', layout.id); } catch(e){}
      _mvTbCollapseIfMobile();
    }
  );
}

// mirrors multiview.js deleteLayout()
async function _mvDeleteSelected(){
  const sel = document.getElementById('mv-layouts-sel');
  const id  = parseInt(sel.value);
  if(!id){ toast('Select a layout to delete', 'wrn'); return; }

  _mvConfirm('Delete Layout?', 'Are you sure you want to delete this layout?', async ()=>{
    try {
      const r = await fetch('/api/multiview/layouts/' + id, {method:'DELETE'});
      if(r.ok){
        toast('Layout deleted', 'ok');
        // Clear auto-restore pointer if this was the last used layout
        try { if(parseInt(localStorage.getItem('mv_last_layout_id')||'0')===id) localStorage.removeItem('mv_last_layout_id'); } catch(e){}
        _mvLoadLayouts();
      }
    } catch(e){ toast('Delete failed: ' + e, 'err'); }
  });
}

// ── EVENT LISTENER SETUP ─────────────────────────────────────────────────────

// mirrors multiview.js setupMultiViewEventListeners()
function _mvSetupListeners(){
  document.getElementById('mv-add-btn')      .addEventListener('click', ()=> _mvAddWidget());
  document.getElementById('mv-remove-btn')   .addEventListener('click', ()=> _mvRemoveLast());
  document.getElementById('mv-layout-auto')  .addEventListener('click', ()=> _mvApplyPreset('auto'));
  document.getElementById('mv-layout-1p1')   .addEventListener('click', ()=> _mvApplyPreset('1+1'));
  document.getElementById('mv-layout-1p2')   .addEventListener('click', ()=> _mvApplyPreset('1+2'));
  document.getElementById('mv-close-btn')    .addEventListener('click', ()=> mvClose());
  document.getElementById('mv-save-btn')     .addEventListener('click', ()=>{
    document.getElementById('mv-save-overlay').classList.add('open');
  });
  document.getElementById('mv-save-ok')      .addEventListener('click', ()=> _mvSaveLayout());
  document.getElementById('mv-save-cancel')  .addEventListener('click', ()=>{
    document.getElementById('mv-save-overlay').classList.remove('open');
  });
  document.getElementById('mv-load-btn')     .addEventListener('click', ()=> _mvLoadSelected());
  document.getElementById('mv-delete-btn')   .addEventListener('click', ()=> _mvDeleteSelected());

  // Channel selector close/back buttons
  document.getElementById('mv-sel-back').addEventListener('click', ()=>{
    _mvCloseCtxMenu();
    if(_mvSelNavMode === 'episodes'){
      // Episodes → Items
      _mvSelNavMode  = 'items';
      _mvSelShowItem = null;
      _mvSelEpisodes = [];
    } else {
      // Items → Cats
      _mvSelNavMode = 'cats';
      _mvSelCat     = null;
      _mvSelItems   = [];
    }
    document.getElementById('mv-sel-search').value = '';
    const _pRow = document.getElementById('mv-sel-play-url-row');
    if(_pRow) _pRow.style.display = _mvSelNavMode==='cats' && _mvSelContentMode==='live' ? '' : 'none';
    const tabsEl = document.getElementById('mv-sel-tabs');
    if(tabsEl) tabsEl.style.display = _mvSelNavMode==='cats' ? '' : 'none';
    _mvRenderSel();
  });
  document.getElementById('mv-sel-close').addEventListener('click', ()=>{
    document.getElementById('mv-sel-overlay').classList.remove('open');
    mvSelCallback = null;
  });
  document.getElementById('mv-sel-cancel').addEventListener('click', ()=>{
    document.getElementById('mv-sel-overlay').classList.remove('open');
    mvSelCallback = null;
  });
  // Close selector on overlay click
  document.getElementById('mv-sel-overlay').addEventListener('click', e=>{
    if(e.target === document.getElementById('mv-sel-overlay')){
      document.getElementById('mv-sel-overlay').classList.remove('open');
      mvSelCallback = null;
    }
  });

  // mirrors multiview.js: close panel on outside click
  // (not applicable here since we're a full-overlay panel, but
  //  Escape key is a good UX addition that mirrors the Node.js app behaviour)
  document.addEventListener('keydown', e=>{
    if(e.key==='Escape' && document.getElementById('p-mv').classList.contains('mv-active')){
      // Close modals first, then the panel
      if(document.getElementById('mv-confirm-overlay').classList.contains('open')){
        document.getElementById('mv-confirm-overlay').classList.remove('open');
      } else if(document.getElementById('mv-save-overlay').classList.contains('open')){
        document.getElementById('mv-save-overlay').classList.remove('open');
      } else if(document.getElementById('mv-sel-overlay').classList.contains('open')){
        // If browsing items, go back to cats; if at cats level, close modal
        if(_mvSelNavMode === 'items'){
          _mvSelNavMode = 'cats'; _mvSelCat = null; _mvSelItems = [];
          document.getElementById('mv-sel-search').value = '';
          const tabsEl = document.getElementById('mv-sel-tabs');
          if(tabsEl) tabsEl.style.display = '';
          _mvRenderSel();
        } else {
          document.getElementById('mv-sel-overlay').classList.remove('open');
          mvSelCallback = null;
        }
      } else {
        mvClose();
      }
    }
  });

  // ── Item name scroll in selector — same logic as main ilist ──────────────
  const mvList = document.getElementById('mv-sel-list');
  if(mvList){
    mvList.addEventListener('mouseenter', e=>{
      const row = e.target.closest('.mv-ch-row');
      if(!row) return;
      const wrap  = row.querySelector('.mv-ch-name');
      const inner = row.querySelector('.mv-ch-name .iname-inner');
      if(!wrap || !inner) return;
      const overflow = inner.scrollWidth - wrap.clientWidth;
      if(overflow <= 6) return;
      const dur = Math.min(12, Math.max(2, overflow / 80));
      wrap.style.setProperty('--scroll-dist', `-${overflow + 8}px`);
      wrap.style.setProperty('--scroll-dur', `${dur}s`);
      wrap.classList.add('scrolling');
    }, true);
    mvList.addEventListener('mouseleave', e=>{
      const row = e.target.closest('.mv-ch-row');
      if(!row) return;
      const wrap = row.querySelector('.mv-ch-name');
      if(wrap) wrap.classList.remove('scrolling');
    }, true);
  }
}

// ── HOOK INTO EXISTING _switchTab ────────────────────────────────────────────
// On mobile, switching tabs just HIDES the multiview overlay — streams keep
// running and the grid layout is preserved. Coming back restores exactly where
// you left off. Only the explicit ⊞ ✕ button triggers a full teardown.
//
// On desktop the panel is a fixed overlay that coexists with the main UI,
// so we also just hide it (same behaviour, consistent).
function mvHide(){
  const panel = document.getElementById('p-mv');
  if(!panel.classList.contains('mv-active')) return;
  panel.classList.remove('mv-active');
  _mvSyncDesktopBtn();
  // Restore botnav highlight to whichever real tab is active
  document.querySelectorAll('.nt').forEach(b=>b.classList.remove('on'));
  const prevPanel = document.querySelector('#main .panel.active');
  if(prevPanel){
    const tid = prevPanel.id.replace('p-','t-');
    const tb  = document.getElementById(tid);
    if(tb) tb.classList.add('on');
  }
}

(function(){
  const _orig = window._switchTab;
  window._switchTab = function(pid, tid){
    // Just hide — do NOT destroy. Layout and streams survive the tab switch.
    mvHide();
    if(typeof _orig === 'function') _orig(pid, tid);
  };
  // Show the desktop button once DOM is ready
  _mvUpdateTop();
  _mvSyncDesktopBtn();
})();
</script>
</body>
</html>
"""

# ===================== ENTRY POINT =====================

if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀  IPTV Portal Builder starting on http://{host}:{port}")
    print(f"    Open this address in your browser or WebView.")
    print(f"    ffmpeg: {'found ✓' if shutil.which('ffmpeg') else 'NOT FOUND ✗'}")
    print(f"    yt-dlp: {'found ✓' if YTDLP_AVAILABLE else 'not available'}")
    # Use threaded=True for SSE support
    flask_app.run(host=host, port=port, threaded=True, debug=False)
