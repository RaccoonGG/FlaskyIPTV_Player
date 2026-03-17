#!/usr/bin/env python3
"""
MAC/Xtream/M3U Portal Builder — Flask/Android WebView Edition
Original CustomTkinter GUI by GG_Raccoon.
Adapted to Flask + HTML5/HLS.js by conversion script.
Run: python app.py  then open http://localhost:5000 in your WebView/browser.
Tested on Windows 10 with python 3.14 and Termux on Android 16.
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
import sys
from datetime import datetime
from urllib.parse import urlparse, quote, quote_plus, unquote, parse_qs
import asyncio
import aiohttp
import requests as _requests_lib

from flask import Flask, request, jsonify, Response, render_template_string, stream_with_context

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


_time_re = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")


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


def run_yt_dlp_download(url: str, out_path: str, stop_event: threading.Event = None):
    if not YTDLP_AVAILABLE:
        return False, "yt-dlp not installed"
    ydl_opts = {
        "outtmpl": out_path + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best",
    }
    try:
        if stop_event and stop_event.is_set():
            return False, "stopped"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        if stop_event and stop_event.is_set():
            return False, "stopped"
        dirn = os.path.dirname(out_path) or "."
        nameprefix = os.path.splitext(os.path.basename(out_path))[0]
        for f in os.listdir(dirn):
            if f.startswith(nameprefix) and f != os.path.basename(out_path):
                try:
                    os.replace(os.path.join(dirn, f), out_path)
                    return True, None
                except Exception:
                    pass
        return True, None
    except Exception as e:
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
        mac = str(js.get("mac", "unknown"))
        phone = str(js.get("phone", "unknown"))
        self.log(f"[MAC] Account: MAC={mac}  expiry={phone}")
        return (mac, phone)

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
        self.log(f"[MAC] {mode.upper()} cat={cat_id} p={page}: {len(items)} items")
        return items

    async def fetch_vod_play_link(self, cmd: str) -> str:
        if not cmd:
            return ""
        try:
            url = f"{self.base}/portal.php?type=vod&action=create_link&cmd={quote(cmd)}"
            self.log(f"[VOD] create_link → {url[:80]}")
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
                        self.log(f"[MAC] create_link resolved → {candidate[:80]}")
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
                        self.log(f"[MAC] create_link retry resolved → {candidate2[:80]}")
                        return candidate2
            except Exception as e:
                self.log(f"[MAC] create_link raw error: {e}")
            return ""
        except Exception as e:
            self.log(f"[create_link] unexpected error: {e}")
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
                        self.log(f"[LOCALHOST FIX] Resolved ch={cid} → {resolved[:80]}")
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
                    f.write(f'#EXTINF:-1 tvg-name="{ep_name}" tvg-type="series" tvg-logo="{ep_logo}" group-title="{ep_cat}",{ep_name}\n{resolved}\n')
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
                        f.write(f'#EXTINF:-1 tvg-name="{full_name}" tvg-type="series" tvg-logo="{series_logo}" group-title="{cat_title}",{full_name}\n{resolved}\n')

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
                    f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-type="{tvg_type}" tvg-logo="{logo}" group-title="{cat_title}",{name}\n{resolved}\n')
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
                    f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-type="{tvg_type}" tvg-logo="{logo}" group-title="{cat_title}",{name}\n{resolved}\n')
                    self.log(f"[LIVE] ✓ Wrote: {name}")

    async def dump_category_to_file(self, mode: str, category: dict, out_path: str, append=True, stop_flag=None):
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
                                f.write(f'#EXTINF:-1 tvg-name="{full_name}" tvg-type="series" tvg-logo="{series_logo}" group-title="{cat_title}",{full_name}\n{resolved}\n')
                                lines_written += 1
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
                        f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-type="{tvg_type}" tvg-logo="{logo}" group-title="{cat_title}",{name}\n{resolved}\n')
                        lines_written += 1
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
                            continue
                        resolved = unquote(resolved)
                        if resolved in seen_urls:
                            continue
                        seen_urls.add(resolved)
                        f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-type="{tvg_type}" tvg-logo="{logo}" group-title="{cat_title}",{name}\n{resolved}\n')
                        lines_written += 1
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
    LOAD_PHP = "/stalker_portal/server/load.php"

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
            self.log(f"[STALKER] Resolved ch={cid} → {resolved[:80]}")
            return resolved
        extracted = _extract_url_from_text(resolved)
        if extracted:
            return extracted
        return stub

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
                f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-type="{tvg_type}" tvg-logo="{logo}" group-title="{cat_title}",{name}\n{resolved}\n')
            self.log(f"[STALKER] ✓ {name}")
        else:
            self.log(f"[STALKER] ✗ Could not resolve: {name}")

    async def dump_category_to_file(self, mode: str, category: dict, out_path: str, append=True, stop_flag=None):
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
        self.log(f"[XTREAM] Auth OK — status: {info.get('status','?')}  expiry: {info.get('exp_date','?')}")
        return info

    async def account_info(self):
        url = f"{self.base}/player_api.php?username={self.username}&password={self.password}"
        async with self.session.get(url) as r:
            data = await safe_json(r)
        if not isinstance(data, dict):
            return (self.username, "unknown")
        info = data.get("user_info", {})
        if not isinstance(info, dict):
            return (self.username, "unknown")
        exp_raw = info.get("exp_date", "")
        exp = "unknown"
        try:
            if exp_raw and str(exp_raw).isdigit():
                exp = datetime.fromtimestamp(int(exp_raw)).strftime("%Y-%m-%d")
            else:
                exp = str(exp_raw)
        except Exception:
            exp = str(exp_raw)
        max_conn = info.get("max_connections", "?")
        active = info.get("active_cons", "?")
        status = info.get("status", "?")
        self.log(f"[XTREAM] Account: user={self.username}  status={status}  expiry={exp}  connections={active}/{max_conn}")
        return (self.username, exp)

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
                f.write(f'#EXTINF:-1 tvg-name="{ep_name}" tvg-type="series" tvg-logo="{ep_logo}" group-title="{ep_cat}",{ep_name}\n{ep_url}\n')
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
                        f.write(f'#EXTINF:-1 tvg-name="{full_name}" tvg-type="series" tvg-logo="{series_logo}" group-title="{cat_title}",{full_name}\n{url}\n')
            self.log(f"[SERIES] ✓ Done: {series_name}")
        else:
            name = self._item_name(item)
            logo = self._item_logo(item)
            url = self._stream_url(mode, item)
            if not url:
                return
            tvg_type = "live" if mode == "live" else "movie"
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-type="{tvg_type}" tvg-logo="{logo}" group-title="{cat_title}",{name}\n{url}\n')
            self.log(f"✓ Wrote: {name}")

    async def dump_category_to_file(self, mode: str, category: dict, out_path: str, append=True, stop_flag=None):
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
                    f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-type="{tvg_type}" tvg-logo="{logo}" group-title="{cat_title}",{name}\n{url}\n')
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
                return await self._xtream_client.account_info()
            except Exception:
                pass
        return ("M3U", "loaded")

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
                    f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-type="series" tvg-logo="{logo}" group-title="{cat_title}",{name}\n{url}\n')
            return
        name = item.get("name", "Unknown")
        logo = item.get("logo", "")
        url = item.get("_url", "")
        tvg_type = item.get("tvg_type") or ("live" if mode == "live" else "movie")
        if not url:
            return
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-type="{tvg_type}" tvg-logo="{logo}" group-title="{cat_title}",{name}\n{url}\n')
        self.log(f"✓ Wrote: {name}")

    async def dump_category_to_file(self, mode: str, category: dict, out_path: str, append=True, stop_flag=None):
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
            episodes = [i for i in raw_items if i.get("tvg_type", "") in type_filter]
            count = 0
            with open(out_path, "a", encoding="utf-8") as f:
                for ep in episodes:
                    if stop_flag and stop_flag.is_set():
                        break
                    name = ep.get("name", "Unknown")
                    logo = ep.get("logo", "")
                    url = ep.get("_url", "")
                    if not url:
                        continue
                    f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-type="series" tvg-logo="{logo}" group-title="{cat_title}",{name}\n{url}\n')
                    count += 1
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
                f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-type="{tvg_type}" tvg-logo="{logo}" group-title="{cat_title}",{name}\n{url}\n')
                count += 1
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
        self.connected = False
        self.is_stalker_portal = False  # True when URL contains 'stalker_portal'
        self.cats_cache: dict = {}
        self.m3u_cache = None
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
        self._epg_cache_ttl = 1800  # seconds
        # Persistent StalkerPortalClient — reused across requests to avoid
        # repeated handshake/profile calls that cause portal rate-limiting
        self._stalker_client: object = None
        self._stalker_client_lock = threading.Lock()

    def log(self, msg: str):
        try:
            self.log_queue.put_nowait(str(msg).rstrip())
        except queue.Full:
            pass

    def set_status(self, msg: str):
        self.status = msg
        self.log(f"[STATUS] {msg}")


state = AppState()


# ===================== ASYNC HELPERS =====================

@contextlib.asynccontextmanager
async def _make_client(do_handshake=True):
    conn = state.conn_type
    if conn == "xtream":
        client = XtreamClient(state.url, state.username, state.password, state.log)
        async with client:
            if do_handshake:
                await client.handshake()
            yield client
    elif conn == "m3u_url":
        if state.m3u_xtream_override:
            creds = state.m3u_xtream_override
            client = XtreamClient(creds["base"], creds["username"], creds["password"], state.log)
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
        async with client:
            if do_handshake:
                await client.handshake()
            yield client


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
        detected = extract_xtream_from_m3u_url(m3u_url)
        if detected:
            state.log(f"[CONNECT] Xtream credentials detected in M3U URL — trying Xtream API first")
            try:
                xt = XtreamClient(detected["base"], detected["username"], detected["password"], state.log)
                async with xt:
                    await xt.handshake()
                    ident, exp = await xt.account_info()
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
                    return {"success": True, "categories": state.cats_cache, "ident": ident, "exp": exp}
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
            ident, exp = await client.account_info()
            state.log(f"[CONNECT] ✓ Connected: {ident} | {exp}")
            for m in ("live", "vod", "series"):
                tmp = M3UClient(m3u_url, state.log, preloaded=state.m3u_cache)
                async with tmp:
                    state.cats_cache[m] = await tmp.fetch_categories(m)
                    state.log(f"[CONNECT] {m.upper()}: {len(state.cats_cache[m])} categories")
        state.connected = True
        state.set_status(f"Connected: {ident} | {exp}")
        return {"success": True, "categories": state.cats_cache, "ident": ident, "exp": exp}

    # MAC / Xtream
    if state.is_stalker_portal:
        state.log("[CONNECT] 🔌 Stalker portal detected — using StalkerPortalClient (/stalker_portal/server/load.php)")
    async with _make_client() as client:
        ident, exp = await client.account_info()
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
    return {"success": True, "categories": state.cats_cache, "ident": ident, "exp": exp}


# ===================== FLASK APP =====================

flask_app = Flask(__name__)
flask_app.config["SECRET_KEY"] = os.urandom(24)


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
        state.is_stalker_portal = (
            state.conn_type == "mac" and
            "stalker_portal" in state.url.lower()
        )
        state.cats_cache = {}
        state.m3u_cache = None
        state.m3u_xtream_override = None
        state._epg_cache = {}
        state.connected = False
        state.stop_flag.clear()

    try:
        result = run_async(_connect_async())
        return jsonify(result)
    except Exception as e:
        state.log(f"[CONNECT] Error: {e}")
        return jsonify({"success": False, "error": str(e), "categories": {}, "ident": "", "exp": ""})


@flask_app.route("/api/categories", methods=["GET"])
def api_categories():
    mode = request.args.get("mode", "live")
    if not state.connected:
        return jsonify({"error": "Not connected", "categories": []})
    cats = state.cats_cache.get(mode, [])
    return jsonify({"categories": cats, "mode": mode})


@flask_app.route("/api/items", methods=["POST"])
def api_items():
    data = request.get_json(force=True)
    mode = data.get("mode", "live")
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
    mode = data.get("mode", "series")
    item = dict(item)
    item["_cat_id"] = cat_id
    item["_mode"] = mode

    try:
        async def fetch():
            async with _make_client() as client:
                return await client.fetch_episodes_for_show(item, cat_title)

        episodes = run_async(fetch())
        return jsonify({"episodes": episodes, "count": len(episodes)})
    except Exception as e:
        state.log(f"[EPISODES] Error: {e}")
        return jsonify({"error": str(e), "episodes": []})


@flask_app.route("/api/resolve", methods=["POST"])
def api_resolve():
    data = request.get_json(force=True)
    item = data.get("item", {})
    mode = data.get("mode", "live")
    cat = data.get("category", {})

    try:
        async def resolve():
            async with _make_client() as client:
                return await client.resolve_item_url(mode, item, cat)

        url = run_async(resolve())
        return jsonify({"url": url})
    except Exception as e:
        state.log(f"[RESOLVE] Error: {type(e).__name__}: {e}")
        return jsonify({"url": "", "error": str(e)})


@flask_app.route("/api/download/m3u", methods=["POST"])
def api_download_m3u():
    data = request.get_json(force=True)
    items = data.get("items", None)    # None = whole category
    cat = data.get("category", {})
    mode = data.get("mode", "live")
    out_path = data.get("out_path", "").strip()

    if not out_path:
        return jsonify({"error": "No output path specified"}), 400
    if state.busy:
        return jsonify({"error": "Another operation is in progress"}), 409

    state.stop_flag.clear()
    state.set_status(f"Downloading M3U…")

    async def worker():
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
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")

        async with _make_client() as client:
            if items is None:
                if state.stop_flag.is_set():
                    return
                await client.dump_category_to_file(mode, cat, out_path, stop_flag=state.stop_flag)
            else:
                for item in items:
                    if state.stop_flag.is_set():
                        state.log("Stopped by user.")
                        break
                    name = item.get("name") or item.get("o_name") or item.get("fname") or "?"
                    state.log(f"Processing: {name}")
                    await client.dump_single_item_to_file(mode, item, cat, out_path, stop_flag=state.stop_flag)

        state.set_status(f"Done. Output: {out_path}")
        state.log("DONE.")

    run_worker(worker())
    return jsonify({"ok": True, "message": f"Download started → {out_path}"})


@flask_app.route("/api/download/mkv", methods=["POST"])
def api_download_mkv():
    data = request.get_json(force=True)
    items = data.get("items", [])
    cat = data.get("category", {})
    mode = data.get("mode", "live")
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
    state.set_status(f"Resolving + downloading {len(items)} item(s) as MKV…")

    async def worker():
        total = len(items)
        state.log(f"[MKV] Phase 1: resolving {total} item URL(s)…")
        resolved_items = []

        async with _make_client() as client:
            for i, item in enumerate(items, 1):
                if state.stop_flag.is_set():
                    state.log("[MKV] Stopped during URL resolution.")
                    return
                name = item.get("name") or item.get("o_name") or item.get("fname") or f"item_{i}"
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
        state.log(f"[MKV] Phase 2: downloading {len(resolved_items)} file(s) to: {out_dir}")
        pre_args = ["-protocol_whitelist", "file,http,https,tcp,tls,crypto,rtsp,rtmp"]

        for idx, (name, url) in enumerate(resolved_items, 1):
            if state.stop_flag.is_set():
                state.log("[MKV] Stopped by user.")
                break

            safe = safe_filename(name)
            out_path = os.path.join(out_dir, f"{safe}.mkv")
            state.log(f"[MKV] ({idx}/{len(resolved_items)}) Downloading: {name}")
            state.set_status(f"MKV {idx}/{len(resolved_items)}: {name}")

            state.log("[MKV]   Probing codecs…")
            codecs = probe_stream_codecs(url, pre_input_args=pre_args)
            post_args = []
            if codecs and codecs.get("audio"):
                if any(c.lower() == "aac" for c in codecs["audio"]):
                    post_args = ["-bsf:a", "aac_adtstoasc"]
                    state.log("[MKV]   AAC audio → adding -bsf:a aac_adtstoasc")

            def _set_proc(p):
                with state.mkv_proc_lock:
                    state.mkv_proc = p

            rc = run_ffmpeg_download(
                url, out_path,
                pre_input_args=pre_args,
                post_input_args=post_args,
                on_progress=lambda line: state.log(line.rstrip()),
                stop_event=state.stop_flag,
                set_proc=_set_proc,
            )
            with state.mkv_proc_lock:
                state.mkv_proc = None

            if state.stop_flag.is_set():
                break

            if rc == 0:
                state.log(f"[MKV] ✓ Saved: {out_path}")
            else:
                state.log(f"[MKV] ✗ ffmpeg exit {rc} for: {name}")
                if use_fallback and YTDLP_AVAILABLE and not state.stop_flag.is_set():
                    state.log("[MKV]   Trying yt-dlp fallback…")
                    ok, err = run_yt_dlp_download(url, out_path, stop_event=state.stop_flag)
                    if ok:
                        state.log(f"[MKV]   ✓ yt-dlp saved: {out_path}")
                    else:
                        state.log(f"[MKV]   ✗ yt-dlp failed: {err}")

        if not state.stop_flag.is_set():
            state.set_status(f"MKV download complete. Files in: {out_dir}")
            state.log(f"[MKV] All done. Output folder: {out_dir}")
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

    async def fetch_epg():
        conn = state.conn_type

        # ── Xtream (direct or M3U override) — use XMLTV feed directly ─────────
        if conn == "xtream" or (conn == "m3u_url" and state.m3u_xtream_override):
            creds = state.m3u_xtream_override if conn == "m3u_url" else None
            base  = creds["base"]      if creds else state.url
            user  = creds["username"]  if creds else state.username
            pwd   = creds["password"]  if creds else state.password
            from urllib.parse import urlparse as _up, quote as _q
            _p = _up(base.rstrip("/"))
            xmltv_url = (f"{_p.scheme}://{_p.netloc}/xmltv.php"
                         f"?username={_q(user, safe='')}&password={_q(pwd, safe='')}")
            tvg_lookup = tvg_id or stream_id
            state.log(f"[EPG] Xtream XMLTV: {xmltv_url} (looking for: {tvg_lookup})")
            return await _fetch_xmltv_epg(xmltv_url, tvg_lookup, state.log)

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
                # MAC/Stalker portals vary — try all known EPG actions in order
                # action=get_epg_info: standard Stalker, returns {js:{ch_id:[{name,time,time_to}]}}
                # action=get_short_epg: some portals, returns {js:{data:[...]}}
                # action=get_content:   older MAC portals with &type=epg
                if not ch_id:
                    return {"current": None, "next": None, "error": "No channel ID for EPG"}
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
            return {"current": None, "next": None, "error": "No EPG data from portal"}

        # ── M3U without Xtream — try tvg-url XMLTV ───────────────────────────
        if conn == "m3u_url" and tvg_id:
            tvg_url = str(item.get("tvg_url") or item.get("_tvg_url") or "").strip()
            if not tvg_url:
                # Try to get from M3U header if cached
                tvg_url = getattr(state, "_tvg_url_cache", "")
            if tvg_url and tvg_url.startswith("http"):
                return await _fetch_xmltv_epg(tvg_url, tvg_id, state.log)

        return {"current": None, "next": None, "error": "EPG not available for this portal/item"}

    try:
        result = run_async(fetch_epg())
        # Cache successful results (even empty ones to avoid hammering unavailable portals)
        if not result.get("error"):
            state._epg_cache[cache_key] = (time.time(), result)
        return jsonify(result)
    except Exception as e:
        state.log(f"[EPG] Error: {type(e).__name__}: {e}")
        return jsonify({"current": None, "next": None, "error": str(e)})


def _parse_xtream_epg(listings: list) -> dict:
    """Parse Xtream get_short_epg response into current/next/schedule."""
    import base64 as _b64
    from datetime import datetime as _dt, timezone as _tz

    def _safe_b64(s):
        """Decode base64 only if result is valid UTF-8 printable text, else return original."""
        if not s:
            return s
        try:
            decoded = _b64.b64decode(s + "==").decode("utf-8")
            # Sanity check: decoded should be printable, not binary garbage
            if decoded.isprintable() and len(decoded) > 1:
                return decoded
        except Exception:
            pass
        return s

    out = {"current": None, "next": None, "schedule": []}
    now = _dt.now(_tz.utc).timestamp()
    parsed = []
    for ep in listings:
        try:
            start = int(ep.get("start_timestamp") or ep.get("start") or 0)
            end   = int(ep.get("stop_timestamp")  or ep.get("end")   or 0)
            title = _safe_b64(ep.get("title") or ep.get("name") or "").strip()
            desc  = _safe_b64(ep.get("description") or ep.get("plot") or "").strip()
            if not title:
                continue
            parsed.append({"title": title, "start": start, "end": end, "desc": desc})
        except Exception:
            continue
    parsed.sort(key=lambda x: x["start"])
    out["schedule"] = parsed
    for ep in parsed:
        if ep["start"] <= now < ep["end"]:
            out["current"] = ep
        elif ep["start"] > now and out["next"] is None:
            out["next"] = ep
    return out


def _parse_stalker_epg(payload: dict, ch_id: str) -> dict:
    """Parse Stalker/MAC get_epg_info / get_short_epg response."""
    from datetime import datetime as _dt, timezone as _tz
    out = {"current": None, "next": None, "schedule": []}
    if not isinstance(payload, dict):
        return out
    now = _dt.now(_tz.utc).timestamp()

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
                return _dt.strptime(s[:19], fmt).replace(tzinfo=_tz.utc).timestamp()
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


async def _fetch_xmltv_epg(xmltv_url: str, tvg_id: str, log_cb=None) -> dict:
    """Fetch XMLTV and find schedule for tvg_id."""
    import xml.etree.ElementTree as ET
    from datetime import datetime as _dt, timezone as _tz
    out = {"current": None, "next": None, "schedule": []}
    if not tvg_id:
        return out
    _log = log_cb or (lambda x: None)
    now = _dt.now(_tz.utc).timestamp()

    def _ts(s):
        s = s.strip()
        try:
            dt = _dt.strptime(s[:14], "%Y%m%d%H%M%S")
            if len(s) > 14:
                tz = s[14:].strip()
                sign = 1 if tz.startswith("+") else -1
                h, m = int(tz[1:3]), int(tz[3:5])
                offset = sign * (h * 3600 + m * 60)
                return dt.replace(tzinfo=_tz.utc).timestamp() - offset
            return dt.replace(tzinfo=_tz.utc).timestamp()
        except Exception:
            return 0

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(xmltv_url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    out["error"] = f"XMLTV HTTP {r.status}"
                    return out
                # Stream and parse iteratively — XMLTV files can be huge
                raw = await r.read()

        root = ET.fromstring(raw)
        tvg_lower = tvg_id.lower()
        schedule = []

        # Build a set of channel identifiers to match against
        # Some XMLTV files have empty id but populated display-name, or vice versa
        chan_map = {}  # id → display-name for logging
        for c in root.findall("channel"):
            cid = (c.get("id") or "").strip()
            dname = (c.findtext("display-name") or "").strip()
            chan_map[cid] = dname

        _log(f"[EPG] XMLTV channels ({len(chan_map)}): {list(chan_map.items())[:8]}")
        _log(f"[EPG] Looking for tvg_id: '{tvg_id}'")

        def _matches(chan_attr: str) -> bool:
            """Check if a programme's channel attribute matches our tvg_id."""
            c = chan_attr.lower().strip()
            t = tvg_lower.strip()
            if not c or not t:
                return False
            if c == t or t in c or c in t:
                return True
            # Also check display-name for this channel id
            dname = chan_map.get(chan_attr, "").lower()
            return dname and (dname == t or t in dname or dname in t)

        for prog in root.findall("programme"):
            chan = prog.get("channel", "")
            if not _matches(chan):
                continue
            start = _ts(prog.get("start", ""))
            end   = _ts(prog.get("stop", ""))
            title = (prog.findtext("title") or "").strip()
            desc  = (prog.findtext("desc") or "").strip()
            if not title or not start:
                continue
            entry = {"title": title, "start": start, "end": end, "desc": desc}
            schedule.append(entry)
            # Only collect up to 10 entries around now
            if len(schedule) >= 10 and all(e["start"] > now + 3600 for e in schedule[-3:]):
                break

        schedule.sort(key=lambda x: x["start"])
        out["schedule"] = schedule
        for ep in schedule:
            if ep["start"] <= now < ep["end"]:
                out["current"] = ep
            elif ep["start"] > now and out["next"] is None:
                out["next"] = ep

    except Exception as e:
        out["error"] = str(e)
    return out


@flask_app.route("/api/proxy")
def api_proxy():
    url = request.args.get("url", "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return Response("Invalid URL", status=400)
    try:
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
        cors = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
        is_m3u8 = (re.search(r'\.(m3u8?|m3u)(\?|$)', url.split('?')[0], re.I) or
                   'mpegurl' in ct.lower() or 'x-mpegurl' in ct.lower())
        if is_m3u8:
            text = resp.text
            rewritten = _rewrite_m3u8(text, url)
            return Response(rewritten, content_type="application/vnd.apple.mpegurl", headers=cors)
        def _gen():
            for chunk in resp.iter_content(chunk_size=16384):
                yield chunk
        h = dict(cors)
        h["Content-Type"] = ct
        if "Content-Length" in resp.headers:
            h["Content-Length"] = resp.headers["Content-Length"]
        if "Content-Range" in resp.headers:
            h["Content-Range"] = resp.headers["Content-Range"]
        return Response(stream_with_context(_gen()), status=resp.status_code, headers=h)
    except Exception as e:
        return Response(f"Proxy error: {e}", status=502)


@flask_app.route("/api/proxy", methods=["OPTIONS"])
def api_proxy_options():
    return Response("", headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    })


@flask_app.route("/api/hls_proxy")
def api_hls_proxy():
    """Remux any MPEG-TS/MPG stream to HLS on-the-fly via ffmpeg.
    Used as fallback when the browser cannot play MPEG-TS natively via MSE.
    Returns a chunked MPEG-TS stream wrapped as HLS-compatible for mpegts.js,
    OR if ?hls=1 is requested, pipes through ffmpeg → fmp4/HLS for native <video>.
    """
    url = request.args.get("url", "").strip()
    if not url or not url.startswith(("http://", "https://", "rtsp://")):
        return Response("Invalid URL", status=400)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return Response("ffmpeg not available", status=503)
    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    # Remux to MPEG-TS via ffmpeg — copy streams, no re-encode
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin",
        "-user_agent", "Mozilla/5.0",
        "-i", url,
        "-c", "copy",
        "-f", "mpegts",
        "pipe:1",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return Response(f"ffmpeg error: {e}", status=502)

    def _gen():
        try:
            while True:
                chunk = proc.stdout.read(16384)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()
            proc.wait()

    h = dict(cors)
    h["Content-Type"] = "video/mp2t"
    return Response(stream_with_context(_gen()), status=200, headers=h)


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
  --glow:rgba(124,58,237,.45);--glow2:rgba(124,58,237,.18);--glow3:rgba(124,58,237,.07);
  --cyan:#06b6d4;--green:#22c55e;--red:#ef4444;--orange:#f59e0b;--blue:#3b82f6;
  --txt:#e4e8f5;--txt2:#7d8a9e;--txt3:#3d4558;
  --r:12px;--rsm:8px;--rss:5px;
  --tr:all .2s cubic-bezier(.4,0,.2,1);
  --sh:0 8px 32px rgba(0,0,0,.7);
}
html,body{height:100dvh;overflow:hidden;background:var(--bg);color:var(--txt);
  font-family:'Segoe UI',-apple-system,system-ui,sans-serif;font-size:14px;line-height:1.5;
  -webkit-font-smoothing:antialiased}
::-webkit-scrollbar{width:3px;height:3px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--s5);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--acc2)}

/* ─── inputs ─────────────────────────────────────────────────── */
input,textarea{background:var(--s3);color:var(--txt);border:1.5px solid var(--bdr);
  border-radius:var(--rsm);padding:9px 12px;font-size:13px;outline:none;width:100%;
  transition:var(--tr);-webkit-appearance:none}
input:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--glow2)}
input::placeholder{color:var(--txt3)}
input[type=range]{background:transparent;border:none;box-shadow:none;padding:0;cursor:pointer;
  -webkit-appearance:auto;appearance:auto}
input[type=checkbox]{width:auto;height:auto;padding:0;accent-color:var(--acc)}

/* ─── buttons ────────────────────────────────────────────────── */
button{cursor:pointer;border:none;border-radius:var(--rsm);padding:9px 16px;font-size:13px;
  font-weight:600;transition:var(--tr);outline:none;white-space:nowrap;
  -webkit-tap-highlight-color:transparent;user-select:none;position:relative;overflow:hidden}
button::after{content:'';position:absolute;inset:0;background:rgba(255,255,255,0);
  transition:background .15s;pointer-events:none;border-radius:inherit}
button:hover:not(:disabled)::after{background:rgba(255,255,255,.06)}
button:active:not(:disabled){transform:scale(.95)}
button:disabled{opacity:.3;cursor:not-allowed;transform:none!important}

.btn-acc{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;
  box-shadow:0 3px 14px var(--glow2)}
.btn-acc:hover:not(:disabled){box-shadow:0 5px 22px var(--glow);filter:brightness(1.1)}
.btn-green{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.22)}
.btn-green:hover:not(:disabled){background:rgba(34,197,94,.2)}
.btn-red{background:rgba(239,68,68,.1);color:var(--red);border:1px solid rgba(239,68,68,.22)}
.btn-red:hover:not(:disabled){background:rgba(239,68,68,.2)}
.btn-blue{background:rgba(59,130,246,.1);color:var(--blue);border:1px solid rgba(59,130,246,.22)}
.btn-blue:hover:not(:disabled){background:rgba(59,130,246,.22)}
.btn-ghost{background:var(--s3);color:var(--txt2);border:1px solid var(--bdr)}
.btn-ghost:hover:not(:disabled){background:var(--s4);color:var(--txt);border-color:var(--bdr2)}
.btn-sm{height:30px;padding:0 10px;font-size:12px;border-radius:var(--rss)}

/* ─── layout ─────────────────────────────────────────────────── */
#app{display:flex;flex-direction:column;height:100dvh}

/* ─── header ─────────────────────────────────────────────────── */
#hdr{flex-shrink:0;z-index:200;
  background:linear-gradient(180deg,rgba(11,11,26,.98) 0%,rgba(10,10,22,.95) 100%);
  border-bottom:1px solid var(--bdr);box-shadow:0 2px 20px rgba(0,0,0,.5)}
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
#cpanel.open{max-height:420px}
#cpi{padding:4px 12px 14px;display:flex;flex-direction:column;gap:8px}
.ct-row{display:flex;gap:5px}
.ct-btn{flex:1;height:32px;font-size:12px;padding:0;border-radius:var(--rsm)}
.cr{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.cr label{font-size:11px;color:var(--txt2);flex-shrink:0;width:28px}
.cr input{flex:1;min-width:120px;height:34px;font-size:12px}
.cr-bot{display:flex;gap:7px;align-items:center}

/* ─── main panels ─────────────────────────────────────────────── */
#main{flex:1;overflow:hidden;display:flex;min-height:0}
.panel{display:none;flex-direction:column;overflow:hidden;min-width:0;min-height:0}
.panel.active{display:flex;flex:1}
@media(min-width:900px){
  #main{display:grid!important;grid-template-columns:260px 1fr 400px}
  .panel{display:flex!important;flex:unset;border-right:1px solid var(--bdr)}
  .panel:last-child{border-right:none}
  #botnav{display:none!important}
  /* On desktop, log panel is hidden — log is shown inline inside player */
  #p-log{display:none!important}
  /* Re-add log area at bottom of player panel on desktop */
  #p-player{overflow-y:auto}
  #desktop-log{display:flex!important}
}

/* ─── panel header ───────────────────────────────────────────── */
.ph{background:linear-gradient(90deg,var(--s1),var(--s2));border-bottom:1px solid var(--bdr);
  padding:10px 14px;display:flex;align-items:center;gap:8px;flex-shrink:0}
.ph h3{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px;
  color:var(--txt2);flex:1;min-width:0}

/* ─── bottom nav ─────────────────────────────────────────────── */
#botnav{display:flex;background:var(--s1);border-top:1px solid var(--bdr);
  flex-shrink:0;z-index:100;padding-bottom:env(safe-area-inset-bottom)}
.nt{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:8px 4px 10px;gap:3px;border:none;background:none;color:var(--txt3);
  font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
  transition:var(--tr);position:relative;border-radius:0;overflow:visible}
.nt.on{color:var(--acc)}
.nt.on::before{content:'';position:absolute;top:0;left:25%;right:25%;height:2.5px;
  background:linear-gradient(90deg,var(--acc),var(--cyan));border-radius:0 0 4px 4px;
  animation:pop-in .2s ease}
.nt-ico{font-size:22px;transition:var(--tr)}
.nt.on .nt-ico{transform:scale(1.12)}
.badge{position:absolute;top:4px;right:calc(50% - 22px);background:var(--acc);
  color:#fff;font-size:9px;font-weight:800;border-radius:10px;padding:1px 5px;
  min-width:16px;text-align:center;display:none;line-height:1.4;animation:pop-in .15s ease}
.badge.vis{display:block}

/* ─── mode tabs ─────────────────────────────────────────────── */
.mtabs{display:flex;gap:4px}
.mt{padding:5px 11px;font-size:12px;font-weight:700;border-radius:20px;
  background:var(--s3);color:var(--txt2);border:1px solid var(--bdr);transition:var(--tr)}
.mt.on{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;
  border-color:transparent;box-shadow:0 2px 12px var(--glow2)}

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
.citem:hover{background:var(--s3);border-color:var(--bdr);transform:translateX(3px)}
.citem:active{transform:scale(.97) translateX(2px)}
.citem::after{content:'';position:absolute;inset:0;opacity:0;transition:opacity .2s;
  background:linear-gradient(90deg,var(--glow3),transparent);pointer-events:none}
.citem:hover::after{opacity:1}
.c-ico{font-size:16px;flex-shrink:0;z-index:1}
.c-name{flex:1;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;z-index:1}
.c-arr{font-size:10px;color:var(--txt3);flex-shrink:0;z-index:1;transition:var(--tr)}
.citem:hover .c-arr{color:var(--acc);transform:translateX(3px)}

/* ─── skeleton ───────────────────────────────────────────────── */
.skel{height:42px;border-radius:var(--rsm);margin-bottom:4px;
  background:linear-gradient(90deg,var(--s2) 25%,var(--s3) 50%,var(--s2) 75%);
  background-size:200% 100%;animation:shimmer 1.5s infinite}

/* ─── item list ──────────────────────────────────────────────── */
.bcrum{font-size:11px;color:var(--txt3);margin-bottom:8px;display:flex;
  align-items:center;gap:4px;flex-wrap:wrap}
.bc-s{color:var(--txt2)}.bc-c{color:var(--acc);font-weight:600}.bc-x{font-size:9px}

.irow{display:flex;align-items:center;gap:7px;padding:8px 10px;border-radius:var(--rsm);
  margin-bottom:3px;background:var(--s2);border:1px solid transparent;
  animation:fade-up var(--d,.25s) ease both;transition:var(--tr)}
.irow:hover{background:var(--s3);border-color:var(--bdr)}
.irow.now{background:linear-gradient(90deg,rgba(124,58,237,.12),var(--s2));
  border-color:rgba(124,58,237,.35);box-shadow:inset 3px 0 0 var(--acc)}
.irow.now .iname{color:var(--acc)}
.ichk{
  width:18px!important;height:18px!important;min-width:18px;flex-shrink:0;
  accent-color:var(--acc);cursor:pointer;
  -webkit-appearance:checkbox!important;appearance:checkbox!important;
  border:none;box-shadow:none;padding:0;background:none}
.ilogo{width:32px;height:21px;object-fit:contain;border-radius:3px;flex-shrink:0;
  background:var(--s4)}
.iname{flex:1;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ibtns{display:flex;gap:3px;flex-shrink:0}
.ibtns button{height:27px;padding:0 9px;font-size:11px;border-radius:var(--rss)}

.ibottom{display:flex;flex-wrap:wrap;gap:5px;padding:8px 0 4px;
  border-top:1px solid var(--bdr);flex-shrink:0}
.ibottom button{flex:1;min-width:68px;height:34px;font-size:12px}
.icount{font-size:11px;color:var(--txt3);padding:3px 0;text-align:center;flex-shrink:0}

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
.ytrow{display:flex;align-items:center;gap:7px;padding:4px 0;font-size:12px;color:var(--txt2)}

/* ─── player ─────────────────────────────────────────────────── */
#p-player{background:#000}
#vwrap{position:relative;background:#000;flex-shrink:0;width:100%}
#vid{width:100%;display:block;aspect-ratio:16/9;background:#000;max-height:58dvh}
@media(min-width:900px){ #vid{max-height:55vh;aspect-ratio:16/9}}
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

.pctrl{background:var(--s2);padding:12px 14px;display:flex;flex-direction:column;
  gap:10px;flex-shrink:0;border-bottom:1px solid var(--bdr)}
.ctrl-r{display:flex;align-items:center;gap:7px}
.ctrl-r.ctr{justify-content:center}
.pbig{width:54px;height:54px;font-size:22px;border-radius:50%;
  background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;
  box-shadow:0 4px 22px var(--glow);flex-shrink:0}
.pbig:hover:not(:disabled){box-shadow:0 6px 30px var(--glow);filter:brightness(1.1);
  transform:scale(1.06)!important}
.pnav{width:42px;height:42px;border-radius:50%;font-size:16px;padding:0;flex-shrink:0}
.vrow{display:flex;align-items:center;gap:9px}
.vrow input[type=range]{flex:1;height:4px;accent-color:var(--acc)}
.vlbl{font-size:11px;color:var(--txt2);width:28px;text-align:right;flex-shrink:0}
.recrow{display:flex;align-items:center;gap:8px}
#rbtn{height:34px;padding:0 14px}
#rbtn.rec{animation:rec-glow 1.5s ease infinite;
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
  backdrop-filter:blur(4px);padding:12px}
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
  margin-bottom:5px;background:var(--s3);border:1px solid var(--bdr);transition:var(--tr);
  animation:fade-up .2s ease both}
.pli:hover{background:var(--s4);border-color:var(--bdr2)}
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
  display:none;backdrop-filter:blur(3px)}
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
  border:1.5px solid var(--bg)}
.fab-badge.vis{display:block}
@media(min-width:900px){.fab{display:none}}

/* ─── toasts ──────────────────────────────────────────────────── */
#toasts{position:fixed;bottom:72px;left:50%;transform:translateX(-50%);
  z-index:9999;display:flex;flex-direction:column;gap:5px;pointer-events:none;width:min(90vw,300px)}
@media(min-width:900px){ #toasts{bottom:18px}}
.toast{padding:10px 18px;border-radius:24px;font-size:13px;font-weight:600;text-align:center;
  box-shadow:var(--sh);border:1px solid rgba(255,255,255,.1);
  animation:slide-up .3s cubic-bezier(.34,1.56,.64,1)}
.tok2{background:rgba(34,197,94,.92);color:#fff}
.terr2{background:rgba(239,68,68,.92);color:#fff}
.tinfo{background:rgba(59,130,246,.92);color:#fff}
.twrn2{background:rgba(245,158,11,.92);color:#fff}

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
      <button class="btn-ghost hdr-ico" onclick="openPL()" title="Saved Playlists">📋</button>
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
      <div id="cr-mac" class="cr">
        <label>URL</label><input id="i-url" type="url" placeholder="http://portal.host:8080">
        <label>MAC</label><input id="i-mac" placeholder="00:1A:79:XX:XX:XX" style="max-width:200px">
      </div>
      <div id="cr-xtream" class="cr hidden">
        <label>URL</label><input id="i-xu" type="url" placeholder="http://server.host:8080">
        <label>User</label><input id="i-us" placeholder="username" style="max-width:150px">
        <label>Pass</label><input id="i-pw" type="password" placeholder="password" style="max-width:150px">
      </div>
      <div id="cr-m3u" class="cr hidden">
        <label>URL</label><input id="i-m3u" type="url" placeholder="http://example.com/list.m3u">
      </div>
      <div class="cr-bot">
        <button class="btn-acc" id="cbtn" onclick="doConnect()" style="height:36px;min-width:120px">🔌 Connect</button>
        <button id="save-profile-chk" onclick="toggleSaveChk(this)"
          style="height:36px;padding:0 12px;font-size:12px;border-radius:var(--rss);
                 border:1px solid var(--bdr2);background:var(--s3);color:var(--txt2);
                 cursor:pointer;white-space:nowrap;transition:var(--tr)"
          >💾 Save</button>
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
        <div class="ytrow"><input type="checkbox" id="ytfb" checked> <span>yt-dlp fallback if ffmpeg fails</span></div>
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
        <button class="mt on" data-m="live" onclick="setMode('live')">📺 Live</button>
        <button class="mt" data-m="vod" onclick="setMode('vod')">🎬 VOD</button>
        <button class="mt" data-m="series" onclick="setMode('series')">📂 Series</button>
      </div>
    </div>
    <div style="padding:8px 10px 0;flex-shrink:0;display:flex;flex-direction:column;gap:6px">
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
    <button class="fab" onclick="openDrawer('cats')" title="Actions">
      ⚡<span class="fab-badge" id="fab-cat-badge"></span>
    </button>
  </div>

  <!-- BROWSE -->
  <div class="panel" id="p-items">
    <div class="ph">
      <h3 id="ittitle">Browse</h3>
      <button class="btn-ghost btn-sm" id="backbtn" onclick="goBack()" disabled>◀ Back</button>
    </div>
    <div style="padding:10px 10px 0;display:flex;flex-direction:column;gap:6px;flex-shrink:0">
      <div class="bcrum" id="bcrum"><span class="bc-s">Categories</span></div>
      <div class="sbar"><span class="sico">🔍</span>
        <input id="isrch" type="search" placeholder="Search items…" oninput="filterItems()">
      </div>
    </div>
    <div style="flex:1;overflow-y:auto;padding:6px 10px 0;min-height:0" id="ilist"></div>
    <div style="padding:0 10px">
      <div class="icount" id="icount"></div>
    </div>
    <button class="fab" onclick="openDrawer('items')" title="Actions">
      ⚡<span class="fab-badge" id="fab-item-badge"></span>
    </button>
  </div>

  <!-- PLAYER -->
  <div class="panel" id="p-player" style="background:#000">
    <div id="vwrap">
      <video id="vid" controls preload="none" playsinline webkit-playsinline></video>
      <div id="vph">
        <div id="vph-ico">▶</div>
        <div>No stream loaded</div>
      </div>
    </div>
    <div class="pinfo">
      <div id="np">No stream loaded</div>
      <div id="pu" onclick="cpyUrl()" title="Tap to copy stream URL">—</div>
    </div>
    <div class="pctrl">
      <div class="ctrl-r ctr">
        <button class="btn-ghost pnav" onclick="playerPrev()" title="Prev">⏮</button>
        <button class="pbig" id="ppbtn" onclick="playerPP()">▶</button>
        <button class="btn-ghost pnav" onclick="playerStop()" title="Stop">⏹</button>
        <button class="btn-ghost pnav" onclick="playerNext()" title="Next">⏭</button>
        <button class="btn-ghost pnav" id="epgbtn" onclick="showEPG()" title="EPG" style="font-size:14px;opacity:0.35">📅</button>
      </div>
      <div style="min-height:16px;padding:0 4px">
        <span id="epg-now" style="font-size:11px;color:var(--txt2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block"></span>
      </div>
      <div class="vrow">
        <span style="font-size:15px">🔉</span>
        <input type="range" id="vol" min="0" max="100" value="80" oninput="setVol(this.value)">
        <span class="vlbl" id="vlbl">80</span>
        <span style="font-size:15px">🔊</span>
      </div>
      <div class="recrow">
        <button class="btn-red" id="rbtn" onclick="togRec()">⏺ Record</button>
        <span class="rtimer" id="rtimer">00:00:00</span>
        <span class="rfname" id="rfname"></span>
      </div>
    </div>
    <!-- Desktop-only inline log (hidden on mobile via CSS) -->
    <div id="desktop-log" style="display:none;flex-direction:column;
      flex:1;overflow:hidden;border-top:1px solid var(--bdr);min-height:100px">
      <div class="ph" style="padding:8px 14px">
        <h3>Activity Log</h3>
        <button class="btn-ghost" onclick="clearLog()"
          style="height:24px;padding:0 8px;font-size:11px;border-radius:var(--rss)">Clear</button>
      </div>
      <div id="desktop-logout" style="flex:1;overflow-y:auto;padding:8px 12px;
        font-family:'Cascadia Code','JetBrains Mono','Courier New',monospace;
        font-size:11px;line-height:1.7;color:#4a556a;background:var(--bg);
        white-space:pre-wrap;word-break:break-word"></div>
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
  <button class="nt" id="t-log" onclick="showT('p-log','t-log')">
    <span class="nt-ico">📜</span><span>Log</span>
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
    <!-- CATS mode -->
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
        <button class="adr-btn btn-blue" id="adr-cat-m3u" onclick="dlSelCats('m3u');closeDrawer()" disabled>
          <span class="adr-ico">💾</span>
          <span class="adr-lbl">Export as M3U</span>
          <span class="adr-sub" id="adr-cat-m3u-sub"></span>
        </button>
        <button class="adr-btn btn-acc" id="adr-cat-mkv" onclick="dlSelCats('mkv');closeDrawer()" disabled>
          <span class="adr-ico">🎬</span>
          <span class="adr-lbl">Download as MKV</span>
          <span class="adr-sub" id="adr-cat-mkv-sub"></span>
        </button>
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
        <button class="adr-btn btn-blue" id="adr-dlm3u" onclick="dlM3U();closeDrawer()" disabled>
          <span class="adr-ico">💾</span>
          <span class="adr-lbl">Export selected → M3U</span>
          <span class="adr-sub" id="adr-m3u-sub"></span>
        </button>
        <button class="adr-btn btn-acc" id="adr-dlmkv" onclick="dlMKV();closeDrawer()" disabled>
          <span class="adr-ico">🎬</span>
          <span class="adr-lbl">Download selected → MKV</span>
          <span class="adr-sub" id="adr-mkv-sub"></span>
        </button>
      </div>
      <div class="adr-section">
        <div class="adr-section-title">Whole Category</div>
        <button class="adr-btn btn-ghost" onclick="dlCat();closeDrawer()">
          <span class="adr-ico">📂</span>
          <span class="adr-lbl">Export entire category → M3U</span>
          <span class="adr-sub" id="adr-cat-all-sub"></span>
        </button>
      </div>
    </div>
  </div>
</div>

<div id="toasts"></div>

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
        <div class="pl-row"><label>Name</label><input id="pl-name" placeholder="My Playlist"></div>
        <div id="plf-mac">
          <div class="pl-row"><label>URL</label><input id="pl-url" type="url" placeholder="http://portal.host:8080"></div>
          <div class="pl-row"><label>MAC</label><input id="pl-mac" placeholder="00:1A:79:XX:XX:XX"></div>
        </div>
        <div id="plf-xtream" class="hidden">
          <div class="pl-row"><label>URL</label><input id="pl-xu" type="url" placeholder="http://server.host:8080"></div>
          <div class="pl-row"><label>User</label><input id="pl-us" placeholder="username"></div>
          <div class="pl-row"><label>Pass</label><input id="pl-pw" type="password" placeholder="password"></div>
        </div>
        <div id="plf-m3u" class="hidden">
          <div class="pl-row"><label>URL</label><input id="pl-m3u" type="url" placeholder="http://example.com/list.m3u"></div>
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
<script>
const CFG = {{ config | safe }};

// ── STATE ──────────────────────────────────────────────────
let CT='mac', mode='live', curCat=null;
let allCats=[], catsCache={}, selCats=new Map(); // selCats: id/title → cat object
let allItems=[], filtItems=[], navStack=[], selSet=new Set();
let pUrl='', pName='', pIdx=-1;
let hlsObj=null, mpegtsObj=null, recTmr=null, isRec=false, logEs=null, cpOpen=false;
const vid = document.getElementById('vid');

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

function setCT(t){
  CT=t;
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
  };
  const saveBtn = document.getElementById('save-profile-chk');
  const saveToProfile = saveBtn._on || false;
  setBusy(true); setStatus('Connecting…'); toggleCP();
  try{
    const r=await fetch('/api/connect',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();
    if(d.success){
      document.getElementById('cdot').classList.add('on');
      setStatus('Connected: '+d.ident+(d.exp&&d.exp!=='unknown'?' · exp '+d.exp:''));
      catsCache=d.categories||{};
      switchMode(mode, catsCache[mode]||[]);
      toast('✓ Connected!','ok');
      // Save to profiles if toggle was active
      if(saveToProfile){
        const arr=plLoadAll();
        // Auto-generate name from URL/ident
        const autoName = d.ident && d.ident!=='unknown' ? d.ident
          : (payload.url||payload.m3u_url||'').replace(/https?:\/\//,'').split('/')[0].split(':')[0];
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
        };
        arr.push(entry);
        plSaveAll(arr);
        renderPLList();
        toast('✓ Connected & saved to profiles!','ok');
        // Reset save button
        saveBtn._on = true; // toggleSaveChk will flip it to false
        toggleSaveChk(saveBtn);
      }
    } else {
      document.getElementById('cdot').classList.remove('on');
      setStatus('Error: '+(d.error||'Unknown'));
      toast(d.error||'Connection failed','err');
      alog('❌ '+(d.error||''),'e');
      toggleCP();
    }
  }catch(e){setStatus('Error: '+e.message);toast(e.message,'err');}
  finally{setBusy(false);}
}

// ── MODES ──────────────────────────────────────────────────
function setMode(m){
  mode=m; navStack=[]; selSet.clear(); selCats.clear(); refreshCatBtns();
  switchMode(m, catsCache[m]||[]);
}

function switchMode(m, cats){
  mode=m;
  document.querySelectorAll('.mt').forEach(b=>b.classList.toggle('on',b.dataset.m===m));
  allCats=cats; filterCats();
  document.getElementById('catlist').scrollTop=0;
}

function filterCats(){
  const q=document.getElementById('csrch').value.toLowerCase();
  renderCats(q?allCats.filter(c=>c.title.toLowerCase().includes(q)):allCats);
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
function onCatChk(cj, checked){
  const c=JSON.parse(cj);
  const key=c.id||c.title;
  if(checked) selCats.set(key,c); else selCats.delete(key);
  refreshCatBtns();
}
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
  // FAB badge
  const b=document.getElementById('fab-cat-badge');
  if(b){b.textContent=n>99?'99+':n; b.classList.toggle('vis',n>0);}
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
function browseC(cj){
  const cat=(typeof cj==='string')?JSON.parse(cj):cj; curCat=cat;
  navStack=[]; setBusy(true); setStatus("Loading '"+cat.title+"'…"); showSkels();
  fetch('/api/items',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode, category:cat, browse:true})})
  .then(r=>r.json()).then(d=>{
    if(d.error){toast(d.error,'err');setStatus('Error: '+d.error);return;}
    allItems=d.items||[];
    setStatus("'"+cat.title+"' — "+allItems.length+' items');
    showItems(cat.title, allItems);
    showT('p-items','t-items');
  }).catch(e=>toast(e.message,'err')).finally(()=>setBusy(false));
}

function showSkels(){
  document.getElementById('ilist').innerHTML=
    Array(8).fill('<div class="skel"></div>').join('');
}

function showItems(label, items){
  allItems=items; filtItems=[...items]; selSet.clear();
  document.getElementById('ilist').scrollTop=0;
  document.getElementById('isrch').value='';
  document.getElementById('backbtn').disabled=false; // always can go back to categories

  mkBcrum(label); renderItems(filtItems); refreshBtns();
  const n=items.length;
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

function renderItems(items){
  const el=document.getElementById('ilist');
  document.getElementById('icount').textContent=items.length+' item'+(items.length!==1?'s':'');
  if(!items.length){
    el.innerHTML='<div style="text-align:center;padding:20px;color:var(--txt3);font-size:12px">No items found</div>';
    refreshBtns(); return;
  }
  const isSeries=mode==='series'||mode==='vod';
  el.innerHTML=items.map((it,i)=>{
    const name=it.name||it.o_name||it.fname||'Unknown';
    const logo=it.logo||it.stream_icon||it.cover||'';
    const grp=!!it._is_series_group;
    const epN=grp?(it._episodes||[]).length:0;
    const show=!!it._is_show_item;
    const playing=i===pIdx;
    return '<div class="irow'+(playing?' now':'')+'" style="--d:'+(Math.min(i,50)*.016)+'s">'
      +'<input class="ichk" type="checkbox" data-i="'+i+'" onchange="onChk('+i+',this.checked)">'
      +(logo?'<img class="ilogo" src="'+esc(logo)+'" onerror="this.style.display=\'none\'">':'<span style="width:32px;height:21px;flex-shrink:0"></span>')
      +'<span class="iname" title="'+esc(name)+'">'+esc(name)+'</span>'
      +'<div class="ibtns">'
        +(grp?'<button class="btn-ghost" onclick="drillGrp('+i+')">'+epN+' eps</button>':'')
        +(show&&isSeries?'<button class="btn-ghost" onclick="drillShow('+i+')">Eps</button>':'')
        +(!grp?'<button class="btn-blue" onclick="playItem('+i+')">▶</button>':'')
      +'</div></div>';
  }).join('');
  refreshBtns();

}

function filterItems(){
  const q=document.getElementById('isrch').value.toLowerCase();
  filtItems=q?allItems.filter(it=>(it.name||it.o_name||it.fname||'').toLowerCase().includes(q)):[...allItems];
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
  // FAB badge
  const b=document.getElementById('fab-item-badge');
  if(b){b.textContent=n>99?'99+':n; b.classList.toggle('vis',n>0);}
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
  setBusy(true); setStatus("Loading eps for '"+it.name+"'…");
  fetch('/api/episodes',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({item:it, mode, cat_id:curCat?.id||'', cat_title:curCat?.title||''})})
  .then(r=>r.json()).then(d=>{
    if(d.error||!d.episodes?.length){toast('No episodes found','warn');return;}
    navStack.push({label:'Browse',items:[...allItems]});
    setStatus(it.name+' — '+d.episodes.length+' episodes');
    showItems(it.name, d.episodes);
    document.getElementById('backbtn').disabled=false;
  }).catch(e=>toast(e.message,'err')).finally(()=>setBusy(false));
}

function goBack(){
  if(!navStack.length){
    // No nav stack — go back to categories panel
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
  // Store item for EPG lookup (live channels only)
  _epgItem = (mode==='live') ? it : null;
  document.getElementById('epg-now').textContent='';
  document.getElementById('epgbtn').style.opacity=(mode==='live')?'1':'0.35';
  const name=it.name||it.o_name||it.fname||'Unknown';
  const direct=it._direct_url||it._url;
  if(direct){doPlay(direct,name);return;}
  setNP('⟳ Resolving: '+name+'…');
  forceTab('p-player','t-player');
  try{
    const r=await fetch('/api/resolve',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({item:it, mode, category:curCat||{}})});
    const d=await r.json();
    if(d.url) doPlay(d.url, name);
    else{setNP('✗ Could not resolve: '+name);toast('Could not resolve URL','err');}
  }catch(e){setNP('✗ '+e.message);}
}

function _destroyPlayers(){
  if(hlsObj){hlsObj.destroy();hlsObj=null;}
  if(mpegtsObj){mpegtsObj.destroy();mpegtsObj=null;}
  vid.pause(); vid.removeAttribute('src'); vid.load();
}

function doPlay(url, name){
  pUrl=url; pName=name||url;
  setNP('▶ '+pName);
  document.getElementById('pu').textContent=url;
  document.getElementById('ppbtn').textContent='⏸';
  document.getElementById('vph').style.opacity='0';
  forceTab('p-player','t-player');

  _destroyPlayers();

  const px='/api/proxy?url='+encodeURIComponent(url);
  const u=url.toLowerCase().split('?')[0];
  const qs=url.toLowerCase();

  const isHls  = u.endsWith('.m3u8') || u.endsWith('.m3u')
               || u.includes('/hls/')
               || qs.includes('extension=m3u8');

  const isMpegTs = u.endsWith('.ts')
               || u.endsWith('.mpg')
               || u.endsWith('/mpegts')
               || u.includes('/mpegts?')
               || qs.includes('extension=ts')
               || qs.includes('output=ts');

  const playerType = isHls?'HLS':isMpegTs?'MPEG-TS':'direct';
  const mpegtsOk = isMpegTs && typeof mpegts!=='undefined' && mpegts.isSupported();
  alog('▶ '+pName+' ['+playerType+(isMpegTs&&!mpegtsOk?' → MSE not supported, trying native':'')+']','k');

  if(isHls && typeof Hls !== 'undefined' && Hls.isSupported()){
    // ── HLS via HLS.js ────────────────────────────────────────
    hlsObj=new Hls({
      enableWorker:false, lowLatencyMode:false,
      maxBufferLength:60, maxMaxBufferLength:180,
      fragLoadingTimeOut:25000, manifestLoadingTimeOut:20000,
      levelLoadingTimeOut:20000,
      xhrSetup(xhr){xhr.withCredentials=false;}
    });
    hlsObj.loadSource(px);
    hlsObj.attachMedia(vid);
    hlsObj.on(Hls.Events.MANIFEST_PARSED,()=>vid.play().catch(()=>{}));
    hlsObj.on(Hls.Events.ERROR,(_,data)=>{
      if(data.fatal){
        alog('[HLS] '+data.type+': '+data.details,'e');
        if(data.type===Hls.ErrorTypes.NETWORK_ERROR)
          setTimeout(()=>{if(hlsObj)hlsObj.startLoad();},2500);
        else if(data.type===Hls.ErrorTypes.MEDIA_ERROR)
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
    // ── Raw MPEG-TS via mpegts.js (desktop Chrome / some Android) ──
    mpegtsObj=mpegts.createPlayer({
      type:'mse',
      isLive:true,
      url:px,
      cors:true,
    },{
      enableWorker:false,
      liveBufferLatencyChasing:true,
      liveBufferLatencyMaxLatency:8,
      liveBufferLatencyMinRemain:2,
    });
    mpegtsObj.attachMediaElement(vid);
    mpegtsObj.load();
    mpegtsObj.on(mpegts.Events.ERROR,(et,ed)=>{
      const msg=(ed?.msg||JSON.stringify(ed));
      alog('[MPEGTS] '+et+': '+msg,'e');
      // On fatal network error try reloading once
      if(et===mpegts.ErrorTypes.NETWORK_ERROR){
        setTimeout(()=>{ if(mpegtsObj){ mpegtsObj.unload(); mpegtsObj.load(); vid.play().catch(()=>{}); }},2000);
      }
    });
    vid.play().catch(()=>{});

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
  _destroyPlayers();
  pUrl=''; setNP('⏹ Stopped'); document.getElementById('pu').textContent='—';
  document.getElementById('ppbtn').textContent='▶';
  document.getElementById('vph').style.opacity='1';
}
function playerPrev(){if(!filtItems.length)return; playItem(pIdx<=0?filtItems.length-1:pIdx-1);}
function playerNext(){if(!filtItems.length)return; playItem(pIdx<0||pIdx>=filtItems.length-1?0:pIdx+1);}
function setVol(v){document.getElementById('vlbl').textContent=v; vid.volume=v/100;}
function setNP(t){document.getElementById('np').textContent=t;}
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
  }catch(e){
    document.getElementById('epg-body').innerHTML=`<div style="color:var(--err);font-size:12px;text-align:center;padding:20px">Failed: ${e.message}</div>`;
  }
}
function closeEPG(){document.getElementById('epg-overlay').style.display='none';}
// Close on backdrop click
document.getElementById('epg-overlay').addEventListener('click',function(e){if(e.target===this)closeEPG();});

async function startRec(){
  if(!pUrl){toast('Play a stream first','wrn');return;}
  const od=document.getElementById('o-dir').value.trim();
  const r=await fetch('/api/record/start',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:pUrl, name:pName, out_dir:od})});
  const d=await r.json();
  if(!d.ok){toast(d.error||'Record failed','err');return;}
  isRec=true;
  const btn=document.getElementById('rbtn');
  btn.textContent='⏹ Stop'; btn.classList.add('rec');
  document.getElementById('rtimer').classList.add('vis');
  document.getElementById('rfname').textContent=d.filename||'';
  toast('⏺ Recording: '+(d.filename||''),'ok');
  let s=0;
  recTmr=setInterval(()=>{
    s++;
    const h=String(Math.floor(s/3600)).padStart(2,'0');
    const m2=String(Math.floor(s%3600/60)).padStart(2,'0');
    const sc=String(s%60).padStart(2,'0');
    document.getElementById('rtimer').textContent=h+':'+m2+':'+sc;
  },1000);
}

async function stopRec(){
  const r=await fetch('/api/record/stop',{method:'POST',
    headers:{'Content-Type':'application/json'},body:'{}'});
  const d=await r.json();
  if(d.ok) toast('Saved: '+(d.file||''),'ok');
  isRec=false;
  const btn=document.getElementById('rbtn');
  btn.textContent='⏺ Record'; btn.classList.remove('rec');
  document.getElementById('rtimer').classList.remove('vis');
  document.getElementById('rfname').textContent='';
  if(recTmr){clearInterval(recTmr);recTmr=null;}
}

// ── DOWNLOADS ──────────────────────────────────────────────
async function dlM3U(){
  const op=document.getElementById('o-m3u').value.trim();
  if(!op){toast('Set M3U output path first','wrn');return;}
  if(!selSet.size){toast('Select items first','wrn');return;}
  setBusy(true);
  const r=await fetch('/api/download/m3u',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items:[...selSet],category:curCat,mode,out_path:op})});
  const d=await r.json();
  d.ok?(toast(d.message,'ok'),pollBusy()):(toast(d.error,'err'),setBusy(false));
}

async function dlMKV(){
  const od=document.getElementById('o-dir').value.trim();
  if(!od){toast('Set output folder first','wrn');return;}
  if(!selSet.size){toast('Select items first','wrn');return;}
  setBusy(true);
  const r=await fetch('/api/download/mkv',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items:[...selSet],category:curCat,mode,out_dir:od,
      use_fallback:document.getElementById('ytfb').checked})});
  const d=await r.json();
  d.ok?(toast(d.message,'ok'),pollBusy()):(toast(d.error,'err'),setBusy(false));
}

async function dlCat(){
  const op=document.getElementById('o-m3u').value.trim();
  if(!op){toast('Set M3U output path first','wrn');return;}
  if(!curCat){toast('Select a category first','wrn');return;}
  setBusy(true);
  const r=await fetch('/api/download/m3u',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items:null,category:curCat,mode,out_path:op})});
  const d=await r.json();
  d.ok?(toast(d.message,'ok'),pollBusy()):(toast(d.error,'err'),setBusy(false));
}

// ── STOP ───────────────────────────────────────────────────
async function doStop(){
  await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  setBusy(false); toast('Stopped','info');
}

// ── POLLING ────────────────────────────────────────────────
async function pollBusy(){
  const r=await fetch('/api/status').catch(()=>null); if(!r) return;
  const d=await r.json().catch(()=>null); if(!d) return;
  if(d.status) setStatus(d.status);
  if(d.busy) setTimeout(pollBusy,1200); else setBusy(false);
}
setInterval(async()=>{
  const r=await fetch('/api/status').catch(()=>null); if(!r) return;
  const d=await r.json().catch(()=>null); if(!d) return;
  if(d.status) setStatus(d.status);
  if(!d.busy) setBusy(false);
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
function alog(msg,cls){
  ['logout','desktop-logout'].forEach(id=>{
    const out=document.getElementById(id); if(!out) return;
    const d=document.createElement('div');
    d.className='ll'+(cls?' l'+cls:'');
    d.textContent=msg; out.appendChild(d);
    out.scrollTop=out.scrollHeight;
    while(out.children.length>600) out.removeChild(out.firstChild);
  });
}
function clearLog(){
  ['logout','desktop-logout'].forEach(id=>{
    const el=document.getElementById(id); if(el) el.innerHTML='';
  });
}
function setStatus(m){document.getElementById('hdr-status').textContent=m;}
function setBusy(v){
  document.getElementById('busy-sp').classList.toggle('hidden',!v);
  document.getElementById('cbtn').disabled=v;
  document.getElementById('stopbtn').disabled=!v;
}
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
function esc(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── ACTION DRAWER ──────────────────────────────────────────
let drawerCtx = 'cats';
function openDrawer(ctx){
  drawerCtx = ctx||'cats';
  document.getElementById('adr-cats-content').classList.toggle('hidden', drawerCtx!=='cats');
  document.getElementById('adr-items-content').classList.toggle('hidden', drawerCtx!=='items');
  document.getElementById('adr-title').textContent = drawerCtx==='cats'
    ? '⚡ Category Actions' : '⚡ Item Actions';
  document.getElementById('act-overlay').classList.add('open');
  document.getElementById('act-drawer').classList.add('open');
}
function closeDrawer(){
  document.getElementById('act-overlay').classList.remove('open');
  document.getElementById('act-drawer').classList.remove('open');
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
  el.innerHTML=arr.map((p,i)=>{
    const ico=icons[p.type]||'📡';
    const sub=p.type==='mac'?p.url+' • '+p.mac
      :p.type==='xtream'?p.url+' • '+p.username
      :p.m3u_url||p.url||'';
    return '<div class="pli" style="--delay:'+(i*.04)+'s">'
      +'<span class="pli-ico">'+ico+'</span>'
      +'<div class="pli-info"><div class="pli-name">'+esc(p.name||'Untitled')+'</div>'
      +'<div class="pli-sub">'+esc(sub)+'</div></div>'
      +'<div class="pli-acts">'
      +'<button class="btn-acc" onclick="plConnect('+i+')" style="height:28px;padding:0 10px;font-size:11px">▶ Load</button>'
      +'<button class="btn-ghost" onclick="plEdit('+i+')">✏</button>'
      +'<button class="btn-red" onclick="plDelete('+i+')">🗑</button>'
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
  // scroll form into view
  document.querySelector('.pl-add').scrollIntoView({behavior:'smooth'});
}

function plDelete(i){
  const arr=plLoadAll(); arr.splice(i,1); plSaveAll(arr); renderPLList();
  toast('Deleted','info');
}

function plClearForm(){
  plEditId=null;
  ['pl-name','pl-url','pl-mac','pl-xu','pl-us','pl-pw','pl-m3u'].forEach(id=>
    document.getElementById(id).value='');
}

async function plConnect(i){
  const arr=plLoadAll(); const p=arr[i]; if(!p) return;
  closePL();
  // Fill in the connection form
  setCT(p.type||'mac');
  toggleCP();
  document.getElementById('i-url').value=p.url||'';
  document.getElementById('i-mac').value=p.mac||'';
  document.getElementById('i-xu').value=p.url_xtream||p.url||'';
  document.getElementById('i-us').value=p.username||'';
  document.getElementById('i-pw').value=p.password||'';
  document.getElementById('i-m3u').value=p.m3u_url||'';
  // Auto-connect
  await doConnect();
}

// ── INIT ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded',()=>{
  setCT('mac'); toggleCP();
  try{const sv=localStorage.getItem('mkv_folder');
    if(sv) document.getElementById('o-dir').value=sv;
    else document.getElementById('o-dir').value='/sdcard/Download/';}catch(e){}
  try{const sm=localStorage.getItem('m3u_path');
    if(sm) document.getElementById('o-m3u').value=sm;
    else document.getElementById('o-m3u').value='/sdcard/Download/playlist.m3u';}catch(e){}
  startLog();
  alog('IPTV Portal Builder ready.','k');
  alog('Tap ⚙ in the header to enter credentials and connect.','i');
});
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
