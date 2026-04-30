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
portal_clients.py  —  Portal client classes for FlaskyIPTV_Player_byGG.py
===========================================================================
Contains all four portal client classes and their shared helpers:

  Helpers
  ─────────────────────────────────────────────────────────────────────
  normalize_base_url(url)          Strip path/query, return base URL.
  _extract_url_from_text(s)        Pull first http(s) URL from a string.
  safe_json(resp)                  Safely decode aiohttp JSON response.
  normalize_js(payload)            Normalise Stalker JS-wrapped JSON.
  extract_xtream_from_m3u_url(url) Detect Xtream credentials in M3U URL.
  _extinf_line(...)                Build #EXTINF line for M3U output.
  _extract_series_name(ep_name)    Strip episode suffix from series name.

  Clients
  ─────────────────────────────────────────────────────────────────────
  PortalClient          MAC portal (JSON API, /portal.php).
  StalkerPortalClient   Stalker/MAG portal (/stalker_portal/server/load.php).
  XtreamClient          Xtream Codes API (/player_api.php).
  M3UClient             Plain M3U URL or local file.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRATION  (one change to FlaskyIPTV_Player_byGG.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Replace the SHARED HELPERS → M3UClient section in main with:

    from portal_clients import (
        normalize_base_url, _extract_url_from_text, safe_json, normalize_js,
        extract_xtream_from_m3u_url, _extinf_line, _extract_series_name,
        PortalClient, StalkerPortalClient, XtreamClient, M3UClient,
    )

No other changes required — all call sites remain identical.
"""

import base64
import hashlib
import json
import os
import random
import re
import string
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, quote, quote_plus, unquote, parse_qs
import asyncio
import aiohttp

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
        # Full raw channel list from get_all_channels — populated once per session.
        # None = not yet attempted. list = already fetched (may be empty on failure).
        self._all_channels_raw: list | None = None

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
        payload = None
        _status = 0
        for _attempt in range(4):  # up to 3 retries on 429
            async with self.session.get(url) as r:
                _status = r.status
                self.log(f"[MAC] Handshake HTTP {r.status}")
                if r.status == 429:
                    _wait = 2 ** _attempt  # 1s, 2s, 4s
                    self.log(f"[MAC] Handshake 429 — backing off {_wait}s (attempt {_attempt+1}/4)")
                    await asyncio.sleep(_wait)
                    continue
                payload = await safe_json(r)
                break
        if not isinstance(payload, dict):
            raise RuntimeError(f"Handshake failed: empty/non-JSON response (HTTP {_status})")
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
        login = str(js.get("login") or js.get("fname") or js.get("username") or "")
        max_conn = 0
        try:
            raw = (js.get("max_connections") or js.get("con_per_device")
                   or js.get("connections_limit") or js.get("max_con")
                   or js.get("playback_limit") or 0)
            max_conn = int(raw) if raw else 0
        except Exception:
            pass
        settings_pwd = str(js.get("settings_password", "") or "")
        adult_pwd    = str(js.get("parent_password", "") or js.get("adult_password", "") or "")
        ident = login or mac
        self.log(f"[MAC] Account: MAC={mac}  login={login}  expiry={phone}")
        return (ident, phone, max_conn, settings_pwd, adult_pwd)

    async def get_all_channels(self, mode: str = "live") -> list:
        """Fetch ALL live channels in one shot via get_all_channels.

        Returns the raw list of channel dicts.  Result is cached for the
        lifetime of the client instance so subsequent calls are free.
        Returns [] on error (caller should fall back to pagination)."""
        if self._all_channels_raw is not None:
            return self._all_channels_raw
        self._all_channels_raw = []
        try:
            url = (f"{self.base}/portal.php?type=itv&action=get_all_channels"
                   f"&force_ch_link_check=&JsHttpRequest=1-xml")
            self.log("[MAC] get_all_channels: fetching full channel list…")
            async with self.session.get(url, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=30)) as r:
                self.log(f"[MAC] get_all_channels HTTP {r.status}")
                payload = await safe_json(r)
            self._all_channels_raw = [ch for ch in normalize_js(payload)
                                      if isinstance(ch, dict)]
            self.log(f"[MAC] get_all_channels: {len(self._all_channels_raw)} channels")
        except Exception as e:
            self.log(f"[MAC] get_all_channels error: {e}")
        return self._all_channels_raw

    async def _fetch_ch_logo_cache(self) -> dict:
        """Return {channel_id: logo_url} dict, derived from get_all_channels.

        Reuses the already-fetched raw channel list — no extra network call.

        Race guard: if _ch_logo_cache is an empty dict (prefetch injected it as
        a shared placeholder), wait for the background prefetch to finish filling
        it in-place rather than firing a concurrent get_all_channels call."""
        if self._ch_logo_cache is not None and self._ch_logo_cache:
            return self._ch_logo_cache   # populated — fast path

        _evt = getattr(self, "_all_channels_ready_event", None)
        if self._ch_logo_cache is not None and not self._ch_logo_cache:
            # Empty dict: prefetch started but not done yet — wait for it.
            if _evt is not None and not _evt.is_set():
                self.log("[MAC] Logo cache: waiting for background prefetch…")
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: _evt.wait(20))
            # Whether the wait succeeded or timed out, return whatever is in the
            # shared dict now.  The prefetch either filled it or failed cleanly.
            return self._ch_logo_cache

        # _ch_logo_cache is None — no prefetch, make our own call.
        self._ch_logo_cache = {}
        channels = await self.get_all_channels()
        for ch in channels:
            ch_id = str(ch.get("id") or "").strip()
            logo = str(ch.get("logo") or ch.get("screenshot_uri") or
                       ch.get("tv_logo") or ch.get("pic") or "").strip()
            if ch_id and logo:
                self._ch_logo_cache[ch_id] = logo
        self.log(f"[MAC] Live logo cache: {len(self._ch_logo_cache)} entries")
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
        if page == 1:
            self.log(f"[MAC] Fetching {mode.upper()} items cat={cat_id}…")
        async with self.session.get(url, headers=self.headers) as r:
            if page == 1:
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

        if page == 1:
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
            for prefix in ("ffmpeg ", "auto ", "ffrt "):
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
        for _pfx in ("ffmpeg ", "auto ", "ffrt "):
            if cmd.lower().startswith(_pfx):
                cmd = cmd.split(" ", 1)[1].strip()
                break
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
                    for _pfx in ("ffmpeg ", "auto ", "ffrt "):
                        if resolved.lower().startswith(_pfx):
                            resolved = resolved.split(" ", 1)[1]
                            break
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

    # ── profile variant table ─────────────────────────────────────────────────
    # Each row: (new_loader, stb_type, image_version, ver_string, prehash, api_sig, hw2, sig_mode)
    # new_loader=True  → device_id2 param = device_id  (portal registered them equal)
    # new_loader=False → device_id2 param = sha256(serialcut)  (old-style distinct IDs)
    # Special prehash sentinels:
    #   "__sha1_mac__"   → sha1(MAC) at request-time (most modern portals)
    #   "__allf__"       → device_id/2 = 64 f's (Go reference default)
    #   "__minimal__"    → minimal params only (no sig/prehash/ver/metrics)
    #   "__mag200__"     → MAG200 diagnostic: auth_second_step=0, empty sig, api_sig=0
    #   "__nodevid__"    → no device_id params (MAC-cookie-only TiviMate style)
    # Special hw2 sentinels:
    #   "__sha1_mac__"   → sha1(MAC) at request-time
    #   "__sn_lower__"   → sn_full.lower() (reference generate_signature style)
    # sig_mode:
    #   "default"        → sha256(sncut + mac)           (current standard)
    #   "plus"           → sha256(sncut + "+" + mac)     (reference generate_signature style)
    #   "sha256_mac"     → sha256(MAC) as device_id1, sha256(sncut) as device_id2
    #   "mag270_static"  → static base64 sig, fixed uid from reference tools
    _V1_VER = ("ImageDescription: 0.2.18-r23-250; ImageDate: Thu Sep 13 11:31:16 EEST 2018; "
               "PORTAL version: 5.6.7; API Version: JS API version: 343; "
               "STB API version: 146; Player Engine version: 0x58c")
    _V0_VER = ("ImageDescription: 0.2.18-r14-pub-250; ImageDate: Fri Jan 15 15:20:44 EET 2016; "
               "PORTAL version: 5.6.1; API Version: JS API version: 328; "
               "STB API version: 134; Player Engine version: 0x561")
    _V2_VER = ("ImageDescription: 0.2.18-r14-pub-254; ImageDate: Fri Jan 15 15:20:44 EET 2016; "
               "PORTAL version: 5.5.0; API Version: JS API version: 328; "
               "STB API version: 134; Player Engine version: 0x550")
    _V3_VER = ("ImageDescription: 2.20.04-pub-520; ImageDate: Thu Jan 06 12:00:00 EET 2022; "
               "PORTAL version: 5.6.7; API Version: JS API version: 332; "
               "STB API version: 136; Player Engine version: 0x568")
    _V4_VER = ("ImageDescription: 2.17.02-pub-254; ImageDate: Fri Jan 15 15:20:44 EET 2016; "
               "PORTAL version: 5.6.1; API Version: JS API version: 330; "
               "STB API version: 135; Player Engine version: 0x550")
    # MAG270 — Dec 2017 firmware
    _V5_VER = ("ImageDescription: 0.2.18-r22-pub-270; ImageDate: Tue Dec 19 11:33:53 EET 2017; "
               "PORTAL version: 5.6.6; API Version: JS API version: 328; "
               "STB API version: 134; Player Engine version: 0x566")
    # MAG254 r23 — Oct 2018, hw_version=2.6-IB-00
    _V6_VER = ("ImageDescription: 0.2.18-r23-254; ImageDate: Wed Oct 31 15:22:54 EEST 2018; "
               "PORTAL version: 5.5.0; API Version: JS API version: 343; "
               "STB API version: 146; Player Engine version: 0x58c")
    # MAG250 r14 older portal 5.5.0 variant (seen in some checker tools)
    _V7_VER = ("ImageDescription: 0.2.18-r14-pub-250; ImageDate: Fri Jan 15 15:20:44 EET 2016; "
               "PORTAL version: 5.5.0; API Version: JS API version: 328; "
               "STB API version: 134; Player Engine version: 0x566")
    _ALL_F = "f" * 64

    _PROFILE_VARIANTS = [
        # (new_loader, stb_type, img_ver, ver_str, prehash, api_sig, hw2, sig_mode)
        #
        # ── TIER 1: sha1(mac) prehash, default sig — most permissive modern portals ───────────
        (True,  "MAG250", "218",      _V1_VER, "__sha1_mac__",                                     "262", "__sha1_mac__",                                    "default"),
        (False, "MAG250", "218",      _V1_VER, "__sha1_mac__",                                     "262", "__sha1_mac__",                                    "default"),
        (False, "MAG254", "0.2.18",   _V2_VER, "__sha1_mac__",                                     "263", "1.7-BD-00",                                       "default"),
        (False, "MAG254", "2.17.02",  _V4_VER, "__sha1_mac__",                                     "263", "7c431b0aec69b2f0194c0680c32fe4e3",                 "default"),
        #
        # ── TIER 2: sha1(mac) prehash, plus-separator sig sha256(sncut+"+"+mac) ────────────────
        # Reference generate_signature() style: dev1=sha256(MAC), dev2=sha256(sncut), hw2=sn.lower()
        (True,  "MAG250", "218",      _V1_VER, "__sha1_mac__",                                     "262", "__sn_lower__",                                     "plus"),
        (False, "MAG250", "218",      _V1_VER, "__sha1_mac__",                                     "262", "__sn_lower__",                                     "plus"),
        (False, "MAG254", "0.2.18",   _V2_VER, "__sha1_mac__",                                     "263", "1.7-BD-00",                                       "plus"),
        (False, "MAG254", "2.17.02",  _V4_VER, "__sha1_mac__",                                     "263", "7c431b0aec69b2f0194c0680c32fe4e3",                 "plus"),
        #
        # ── TIER 2b: sha1(mac) prehash, base64 sig — base64(sha256(sncut + mac.upper())) ───────
        # Some portal software expects a URL-safe base64 signature rather than a hex digest.
        (True,  "MAG250", "218",      _V1_VER, "__sha1_mac__",                                     "262", "__sha1_mac__",                                    "b64"),
        (False, "MAG250", "218",      _V1_VER, "__sha1_mac__",                                     "262", "__sha1_mac__",                                    "b64"),
        (False, "MAG254", "0.2.18",   _V2_VER, "__sha1_mac__",                                     "263", "1.7-BD-00",                                       "b64"),
        #
        # ── TIER 3: MAG200 / diagnostic style ───────────────────────────────────────────────────
        (True,  "MAG200", "",         "",       "__mag200__",                                       "0",   "",                                                  "default"),
        #
        # ── TIER 4: MAG270 — static base64 sig, fixed uid, distinct hw2 ─────────────────────────
        (True,  "MAG270", "0.2.18",   _V5_VER, "efd15c16dc497e0839ff5accfdc6ed99c32c4e2a",        "262", "85a284d980bbfb74dca9bc370a6ad160e968d350",          "mag270_static"),
        (False, "MAG270", "0.2.18",   _V5_VER, "efd15c16dc497e0839ff5accfdc6ed99c32c4e2a",        "262", "85a284d980bbfb74dca9bc370a6ad160e968d350",          "mag270_static"),
        #
        # ── TIER 5: MAG254 r23, hw_version=2.6-IB-00, distinct prehash+hw2 ──────────────────────
        (True,  "MAG254", "0.2.18",   _V6_VER, "4cda0db2375f15f906d2b4df85fc58e05b839d79",        "262", "5ab8c9dceec64b9540bb41bc527e88658aa8c620",          "default"),
        (False, "MAG254", "0.2.18",   _V6_VER, "4cda0db2375f15f906d2b4df85fc58e05b839d79",        "262", "5ab8c9dceec64b9540bb41bc527e88658aa8c620",          "default"),
        (True,  "MAG254", "0.2.18",   _V6_VER, "9036d5f7dc752a23dfc087de916552a2de3e70bb",        "262", "39d95ea1affa08953d5951afeb1fbe57f8ffc23a",          "default"),
        (False, "MAG254", "0.2.18",   _V6_VER, "9036d5f7dc752a23dfc087de916552a2de3e70bb",        "263", "39d95ea1affa08953d5951afeb1fbe57f8ffc23a",          "default"),
        #
        # ── TIER 6: static prehashes from real STB firmware ROMs ─────────────────────────────────
        (True,  "MAG250", "218",      _V1_VER, "53302b3a8bcca197b7366e83d5e2883f99973f09",        "262", "__sha1_mac__",                                    "default"),
        (False, "MAG250", "218",      _V1_VER, "53302b3a8bcca197b7366e83d5e2883f99973f09",        "262", "__sha1_mac__",                                    "default"),
        (False, "MAG254", "0.2.18",   _V2_VER, "efd15c16dc497e0839ff5accfdc6ed99c32c4e2a",        "263", "1.7-BD-00",                                       "default"),
        (False, "MAG250", "0.2.18",   _V0_VER, "efd15c16dc497e0839ff5accfdc6ed99c32c4e2a",        "263", "",                                                  "default"),
        (False, "MAG520", "2.20.04",  _V3_VER, "efd15c16dc497e0839ff5accfdc6ed99c32c4e2a",        "263", "",                                                  "default"),
        (False, "MAG254", "2.17.02",  _V4_VER, "efd15c16dc497e0839ff5accfdc6ed99c32c4e2a",        "263", "7c431b0aec69b2f0194c0680c32fe4e3",                 "default"),
        (True,  "MAG250", "218",      _V1_VER, "6b1e45cc169162c9e876a29707236e54c24631db",        "262", "__sha1_mac__",                                    "default"),
        (False, "MAG250", "218",      _V1_VER, "6b1e45cc169162c9e876a29707236e54c24631db",        "262", "__sha1_mac__",                                    "default"),
        #
        # ── TIER 7: literal prehash strings used by some portal checkers ─────────────────────────
        (True,  "MAG250", "218",      _V1_VER, "false",                                            "262", "__sha1_mac__",                                    "default"),
        (True,  "MAG250", "218",      _V1_VER, "0",                                                "262", "__sha1_mac__",                                    "default"),
        #
        # ── TIER 8: no device_id params — MAC-cookie-only style ──────────────────────────────────
        (True,  "MAG250", "218",      _V7_VER, "__nodevid__",                                      "262", "__sha1_mac__",                                    "default"),
        #
        # ── TIER 9: allf device IDs fallback ─────────────────────────────────────────────────────
        (True,  "MAG254", "0.2.18",   _V2_VER, "__allf__",                                         "263", "1.7-BD-00",                                       "default"),
        #
        # ── TIER 10: minimal params fallback ─────────────────────────────────────────────────────
        (True,  "MAG254", "0.2.18",   "",       "__minimal__",                                      "263", "",                                                  "default"),
    ]

    def __init__(self, base_url: str, mac: str, log_cb,
                 custom_sn: str = "", custom_device_id: str = "",
                 custom_device_id2: str = "", custom_signature: str = ""):
        self.base = normalize_base_url(base_url)
        self.mac = mac.strip().upper()
        self.log = log_cb
        self.session = None
        self.token = None
        self.bearer_token = None
        self._random = None
        self._last_profile_js: dict | None = None  # cached from handshake() variant loop; {} = tried+failed; None = not yet tried
        # Derived IDs — reference algorithm:
        #   SN       = md5(MAC).upper()      (full 32-char hex)
        #   SNCUT    = SN[:13]
        #   deviceid1= sha256(MAC).upper()
        #   deviceid2= sha256(SNCUT).upper()  (old loader)
        #   signature= sha256(SNCUT+MAC).upper()
        self.serial    = hashlib.md5(self.mac.encode("utf-8")).hexdigest().upper()
        self.serialcut = self.serial[:13]
        self.device_id  = (custom_device_id.strip().upper()
                           if custom_device_id.strip()
                           else hashlib.sha256(self.mac.encode("utf-8")).hexdigest().upper())
        self.device_id2 = (custom_device_id2.strip().upper()
                           if custom_device_id2.strip()
                           else hashlib.sha256(self.serialcut.encode("utf-8")).hexdigest().upper())
        if custom_sn.strip():
            self.serial    = custom_sn.strip().upper()
            self.serialcut = self.serial[:13]
        self.sg        = self.serialcut + self.mac
        self.signature = (custom_signature.strip()
                          if custom_signature.strip()
                          else hashlib.sha256(self.sg.encode("utf-8")).hexdigest().upper())
        self.log(f"[STALKER] Computed IDs — SN={self.serial}  SNCUT={self.serialcut}  "
                 f"deviceid1={self.device_id}  deviceid2={self.device_id2}  "
                 f"signature={self.signature}"
                 + (" (custom)" if custom_signature.strip() else ""))
        # Cache for channel id → logo URL, populated lazily from get_all_channels
        self._ch_logo_cache: dict | None = None
        # Running in-memory logo cache for VOD / series — populated from items
        # that already have a logo so we can fill blanks without extra requests.
        self._vod_logo_cache: dict = {}
        # Full raw channel list from get_all_channels — populated once per session.
        self._all_channels_raw: list | None = None

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
            "PHPSESSID=null",
            f"mac={quote(self.mac)}",
            f"sn={quote(self.serialcut)}",   # Go ref: SerialNumber = SNCUT (13-char)
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

    def _generate_metrics(self, stb_type: str = "MAG250", uid_override: str = "",
                          extra: dict | None = None) -> str:
        """Build the metrics JSON string sent in the get_profile request.

        extra — optional dict of additional fields to include (e.g. hw_version_2,
        hw_version, image_version — used by the reference generate_signature style).
        """
        if not self._random:
            self._random = self._generate_random()
        d: dict = {
            "mac": self.mac,
            "sn": self.serialcut,
            "type": "STB",
            "model": stb_type,
            "uid": uid_override or self.device_id,
            "random": self._random,
        }
        if extra:
            d.update(extra)
        return json.dumps(d)

    async def _read_json(self, r: aiohttp.ClientResponse, tag=None):
        """Read response text, log raw content (when tag is not None), then parse JSON."""
        try:
            text = await r.text()
        except Exception as e:
            if tag:
                self.log(f"[STALKER] {tag} read error: {e}")
            return None
        if tag:
            preview = repr(text[:800]) if text else "''"
            self.log(f"[STALKER] {tag} raw: {preview}")
        if not text or not text.strip():
            return None
        t = text.lstrip()
        if not (t.startswith("{") or t.startswith("[")):
            return None
        try:
            return json.loads(text)
        except Exception:
            return None
        except Exception:
            return None

    @staticmethod
    def _is_profile_conflict(js: dict) -> bool:
        """Return True when this variant should be skipped and the next tried.

        Covers two cases:
        - status 1 + conflict/rejection keywords: portal recognises the MAC/device but rejects the
          specific device_id/prehash combination (firmware mismatch, hash error, device conflict,
          old firmware, etc.)
        - status 2: portal is issuing an authentication challenge — the session is not
          authenticated, so all subsequent requests will return 'Authorization failed.'
          We must keep iterating variants rather than accepting this as a valid profile.
        """
        if not isinstance(js, dict):
            return False
        status = js.get("status")
        if status == 2:
            return True
        msg = (str(js.get("msg", "") or "") + " " + str(js.get("block_msg", "") or "")).lower()
        return (status == 1 and any(kw in msg for kw in (
            "conflict", "mismatch", "device", "hash", "not valid", "not supported",
            "firmware", "outdated", "old firmware", "update", "not registered",
        )))

    # ── auth ──────────────────────────────────────────────────────────────────

    async def handshake(self) -> str:
        assert self.session is not None
        url = self._load_url(type="stb", action="handshake", token="", JsHttpRequest="1-xml")
        headers = self._headers(include_auth=False, include_token=False)
        self.log(f"[STALKER] Handshake → {self.base}{self.LOAD_PHP}")
        payload = None
        for _attempt in range(4):  # up to 3 retries on 429
            async with self.session.get(url, headers=headers) as r:
                self.log(f"[STALKER] Handshake HTTP {r.status}")
                if r.status == 429:
                    _wait = 2 ** _attempt
                    self.log(f"[STALKER] Handshake 429 — backing off {_wait}s (attempt {_attempt+1}/4)")
                    await asyncio.sleep(_wait)
                    continue
                if r.status == 404:
                    self.log("[STALKER] 404 on handshake — retrying with token+prehash")
                    tok = self._generate_token()
                    prehash = self._generate_prehash(tok)
                    url2 = self._load_url(type="stb", action="handshake",
                                          token=tok, prehash=prehash, JsHttpRequest="1-xml")
                    async with self.session.get(url2, headers=headers) as r2:
                        self.log(f"[STALKER] Retry handshake HTTP {r2.status}")
                        payload = await self._read_json(r2, "Handshake retry")
                else:
                    payload = await self._read_json(r, "Handshake")
                break

        if not isinstance(payload, dict) or "js" not in payload:
            raise RuntimeError("[STALKER] Handshake failed — no valid JSON response")
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

        # Try each profile variant in order; stop at first success
        self._last_profile_js: dict | None = None
        for idx, (new_loader, stb_type, img_ver, ver_str, prehash, api_sig, hw2, sig_mode) in enumerate(self._PROFILE_VARIANTS):
            label = prehash if prehash.startswith("__") else prehash[:8] + "…"
            self.log(f"[STALKER] Getting profile… (variant {idx+1}/{len(self._PROFILE_VARIANTS)}: "
                     f"new_loader={new_loader} stb={stb_type} prehash={label})")
            js = await self._get_profile_variant(new_loader, stb_type, img_ver, ver_str, prehash, api_sig, hw2, sig_mode)
            if self._is_profile_conflict(js):
                status = js.get("status", "?")
                msg    = js.get("msg") or js.get("block_msg") or ""
                self.log(f"[STALKER] Profile variant {idx+1} rejected (status={status} msg={msg!r}) — trying next")
                continue
            self._last_profile_js = js  # cache for get_profile() callers
            break
        else:
            # All variants exhausted without a successful profile — portal likely has a strict
            # device_id lock or requires credentials we cannot satisfy. Proceed so the caller
            # can still report a partial connection, but log the failure clearly.
            self.log("[STALKER] ⚠ All profile variants exhausted — no authenticated profile. "
                     "Subsequent requests will likely return 'Authorization failed.'")
            self._last_profile_js = {}  # empty dict: signals "tried but failed" without re-triggering

        return self.token

    async def _get_profile_variant(self, new_loader: bool, stb_type: str, image_version: str,
                                    ver_string: str, prehash: str, api_sig: str, hw2: str,
                                    sig_mode: str = "default") -> dict:
        """Send a get_profile request with the given variant parameters. Returns js dict."""
        assert self.session is not None
        from urllib.parse import urlencode
        if not self._random:
            self._random = self._generate_random()

        # ── compute device IDs based on sig_mode ───────────────────────────
        if prehash == "__allf__":
            dev1 = dev2 = self._ALL_F
        elif sig_mode == "plus":
            # Reference generate_signature(): dev1=sha256(MAC), dev2=sha256(sncut)
            dev1 = hashlib.sha256(self.mac.encode("utf-8")).hexdigest().upper()
            dev2 = hashlib.sha256(self.serialcut.encode("utf-8")).hexdigest().upper()
        else:
            dev1 = self.device_id
            dev2 = self.device_id if new_loader else self.device_id2

        # ── compute signature based on sig_mode ────────────────────────────
        if sig_mode == "plus":
            # sha256(sncut + "+" + mac) — reference generate_signature style
            computed_sig = hashlib.sha256(
                (self.serialcut + "+" + self.mac).encode("utf-8")
            ).hexdigest().upper()
        elif sig_mode == "b64":
            # base64(sha256(sncut + MAC.upper())) — some portal software expects base64
            import base64 as _b64
            computed_sig = _b64.b64encode(
                hashlib.sha256((self.serialcut + self.mac.upper()).encode("utf-8")).digest()
            ).decode()
        elif sig_mode == "mag270_static":
            computed_sig = "OaRqL9kBdR5qnMXL+h6b+i8yeRs9/xWXeKPXpI48VVE="
        else:
            computed_sig = self.signature

        # ── resolve hw2 sentinel ───────────────────────────────────────────
        if hw2 == "__sha1_mac__":
            hw2 = hashlib.sha1(self.mac.encode("utf-8")).hexdigest()
        elif hw2 == "__sn_lower__":
            hw2 = self.serial.lower()  # sn_full.lower() per reference generate_signature

        # ── resolve prehash sentinel ───────────────────────────────────────
        if prehash == "__sha1_mac__":
            prehash = hashlib.sha1(self.mac.encode("utf-8")).hexdigest()

        # ── hw_version — 2.6-IB-00 for MAG254-r23, otherwise 1.7-BD-00 ───
        hw_version = "2.6-IB-00" if stb_type == "MAG254" and image_version == "0.2.18" and "r23" in ver_string else "1.7-BD-00"

        # ── MAG200 / diagnostic style ─────────────────────────────────────
        # auth_second_step=0, empty signature, api_signature=0, sha1(MAC) prehash.
        # Uses self.serial (full 32-char SN) not serialcut — matches what diagnostic tools send.
        if prehash == "__mag200__":
            params = {
                "type": "stb",
                "action": "get_profile",
                "hd": "1",
                "not_valid_token": "0",
                "video_out": "hdmi",
                "auth_second_step": "0",
                "num_banks": "2",
                "metrics": self._generate_metrics("MAG200"),
                "sn": self.serial,
                "stb_type": "MAG200",
                "client_type": "STB",
                "device_id": dev1,
                "device_id2": dev2,
                "signature": "",
                "timestamp": int(time.time()),
                "api_signature": "0",
                "prehash": hashlib.sha1(self.mac.encode("utf-8")).hexdigest(),
                "JsHttpRequest": "1-xml",
            }

        # ── minimal-params sentinel — Go authenticateWithDeviceIDs style ───
        elif prehash == "__minimal__":
            params = {
                "type": "stb",
                "action": "get_profile",
                "hd": "1",
                "sn": self.serialcut,
                "stb_type": stb_type,
                "device_id": dev1,
                "device_id2": dev2,
                "auth_second_step": "1",
                "random": self._random,
                "JsHttpRequest": "1-xml",
            }

        # ── no device_id params — TiviMate style, relies on MAC cookie only ──
        elif prehash == "__nodevid__":
            params = {
                "type": "stb",
                "action": "get_profile",
                "hd": "1",
                "ver": ver_string,
                "num_banks": "2",
                "sn": self.serialcut,
                "stb_type": stb_type,
                "client_type": "STB",
                "image_version": image_version,
                "video_out": "hdmi",
                "auth_second_step": "1",
                "hw_version": hw_version,
                "metrics": self._generate_metrics(stb_type),
                "hw_version_2": hw2,
                "timestamp": int(time.time()),
                "api_signature": api_sig,
                "prehash": hashlib.sha1(self.mac.encode("utf-8")).hexdigest(),
                "random": self._random,
                "JsHttpRequest": "1-xml",
            }

        else:
            # For "plus" sig_mode, the reference generate_signature() style embeds additional
            # fields inside the metrics JSON itself (hw_version_2, hw_version, image_version).
            # For mag270_static, use a fixed uid from the reference tool's hardcoded value.
            metrics_extra: dict | None = None
            metrics_uid = ""
            if sig_mode == "plus":
                metrics_extra = {
                    "hw_version_2": hw2,
                    "hw_version": hw_version,
                    "image_version": image_version,
                }
            elif sig_mode == "mag270_static":
                metrics_uid = "BB340DE42B8A3032F84F5CAF137AEBA287CE8D51F44E39527B14B6FC0B81171E"

            params = {
                "type": "stb",
                "action": "get_profile",
                "hd": "1",
                "ver": ver_string,
                "num_banks": "2",
                "sn": self.serialcut,
                "stb_type": stb_type,
                "client_type": "STB",
                "image_version": image_version,
                "video_out": "hdmi",
                "device_id": dev1,
                "device_id2": dev2,
                "signature": computed_sig,
                "auth_second_step": "1",
                "hw_version": hw_version,
                "not_valid_token": "0",
                "metrics": self._generate_metrics(stb_type, uid_override=metrics_uid, extra=metrics_extra),
                "hw_version_2": hw2,
                "timestamp": int(time.time()),
                "api_signature": api_sig,
                "prehash": prehash,
                "random": self._random,
                "JsHttpRequest": "1-xml",
            }

        url = f"{self.base}{self.LOAD_PHP}?{urlencode(params)}"
        self.log(f"[STALKER] Profile URL: {url}")
        headers = self._headers(include_auth=True, include_token=False)
        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] Profile HTTP {r.status}")
            payload = await self._read_json(r, "Profile")
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

    async def get_profile(self) -> dict:
        """Return the profile js cached by handshake(). If called before handshake, run variant 1."""
        if self._last_profile_js is not None:
            return self._last_profile_js
        new_loader, stb_type, img_ver, ver_str, prehash, api_sig, hw2, sig_mode = self._PROFILE_VARIANTS[0]
        js = await self._get_profile_variant(new_loader, stb_type, img_ver, ver_str, prehash, api_sig, hw2, sig_mode)
        self._last_profile_js = js
        return js

    async def account_info(self):
        assert self.session is not None
        url = self._load_url(type="account_info", action="get_main_info", JsHttpRequest="1-xml")
        headers = self._headers(include_auth=True)
        self.log("[STALKER] Fetching account info…")
        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] Account info HTTP {r.status}")
            payload = await self._read_json(r, "Account info")

        def _extract_max_conn(js: dict) -> int:
            try:
                # Top-level fields first
                raw = (js.get("max_online") or js.get("playback_limit") or
                       js.get("max_connections") or js.get("con_per_device") or
                       js.get("max_con") or js.get("connections_limit") or 0)
                if raw:
                    return int(raw)
                # profile.json shows max_online nested inside storages dict
                storages = js.get("storages")
                if isinstance(storages, dict):
                    for store in storages.values():
                        if isinstance(store, dict):
                            v = store.get("max_online")
                            if v:
                                return int(v)
            except Exception:
                pass
            return 0

        if isinstance(payload, dict):
            js = payload.get("js", {})
            if isinstance(js, dict):
                mac = str(js.get("mac") or js.get("device_mac") or self.mac)
                exp = str(js.get("phone") or js.get("end_date") or js.get("expire_billing_date") or "unknown")
                max_conn = _extract_max_conn(js)
                active = js.get("active_cons") or js.get("online_streams") or "?"
                # Fallback: if account_info was blocked, pull from cached profile js
                if not max_conn and self._last_profile_js:
                    max_conn = _extract_max_conn(self._last_profile_js)
                self.log(f"[STALKER] Account: MAC={mac}  expiry={exp}  connections={active}/{max_conn or '?'}")
                return (mac, exp, max_conn)

        # account_info endpoint blocked — pull what we can from cached profile
        mac = self.mac
        exp = "unknown"
        max_conn = 0
        if self._last_profile_js:
            js = self._last_profile_js
            mac = str(js.get("mac") or js.get("device_mac") or self.mac)
            exp = str(js.get("phone") or js.get("end_date") or js.get("expire_billing_date") or "unknown")
            max_conn = _extract_max_conn(js)
        return (mac, exp, max_conn)

    # ── categories ────────────────────────────────────────────────────────────

    @staticmethod
    def _is_series_cat(name: str) -> bool:
        return any(k in name.lower() for k in ('tv', 'series', 'show', 'episode'))

    async def fetch_categories(self, mode: str):
        assert self.session is not None
        headers = self._headers(include_auth=True)
        self.log(f"[STALKER] Fetching {mode.upper()} categories…")

        # For series: try the dedicated type=series endpoint first.
        # Many Stalker portals have it, and it returns the correct full list
        # without needing keyword-based splitting (which drops "Drama", "Action", etc.)
        if mode == "series":
            try:
                ser_url = self._load_url(type="series", action="get_categories", JsHttpRequest="1-xml")
                async with self.session.get(ser_url, headers=headers) as r:
                    self.log(f"[STALKER] Series endpoint HTTP {r.status}")
                    payload = await self._read_json(r, "Categories (SERIES)")
                ser_cats = normalize_js(payload)
                if not ser_cats:
                    ser_url2 = self._load_url_alt(type="series", action="get_categories", JsHttpRequest="1-xml")
                    self.log("[STALKER] Series endpoint empty — retrying via portal.php")
                    async with self.session.get(ser_url2, headers=headers) as r2:
                        self.log(f"[STALKER] Series endpoint (alt) HTTP {r2.status}")
                        payload = await self._read_json(r2, "Categories alt (SERIES)")
                    ser_cats = normalize_js(payload)
                if ser_cats:
                    result = []
                    for c in ser_cats:
                        if not isinstance(c, dict):
                            continue
                        cid = str(c.get("id") or c.get("category_id") or "").strip()
                        name = str(c.get("title") or c.get("name") or c.get("category_name") or "").strip()
                        if cid and name:
                            result.append({"id": cid, "title": name})
                    self.log(f"[STALKER] SERIES categories: {len(result)} found (dedicated endpoint)")
                    return result
            except Exception as e:
                self.log(f"[STALKER] Series dedicated endpoint failed ({e}) — falling back to vod+split")

        # For live: dedicated genre endpoint. For vod/series fallback: shared vod endpoint.
        if mode == "live":
            url = self._load_url(type="itv", action="get_genres", JsHttpRequest="1-xml")
        else:
            url = self._load_url(type="vod", action="get_categories", JsHttpRequest="1-xml")

        async with self.session.get(url, headers=headers) as r:
            self.log(f"[STALKER] Categories HTTP {r.status} ({mode.upper()})")
            payload = await self._read_json(r, f"Categories ({mode.upper()})")
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
                payload = await self._read_json(r2, f"Categories alt ({mode.upper()})")
            cats = normalize_js(payload)
        result = []
        for c in cats:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or c.get("category_id") or "").strip()
            name = str(c.get("title") or c.get("name") or c.get("category_name") or "").strip()
            if not cid or not name:
                continue
            # Keyword split only used as fallback when the dedicated series endpoint failed.
            # This is imperfect — categories like "Drama" or "Action" won't match — but it
            # is better than returning nothing for portals that have no dedicated endpoint.
            if mode == "series" and not self._is_series_cat(name):
                continue
            if mode == "vod" and self._is_series_cat(name):
                continue
            result.append({"id": cid, "title": name})
        self.log(f"[STALKER] {mode.upper()} categories: {len(result)} found (vod+split fallback)")
        return result

    # ── items ─────────────────────────────────────────────────────────────────

    async def get_all_channels(self, mode: str = "live") -> list:
        """Fetch ALL live channels in one shot via get_all_channels.

        Tries load.php first, then portal.php as fallback.
        Result is cached for the lifetime of the client instance.
        Returns [] on complete failure (caller should fall back to pagination)."""
        if self._all_channels_raw is not None:
            return self._all_channels_raw
        self._all_channels_raw = []
        headers = self._headers(include_auth=True)

        # Attempt 1: /stalker_portal/server/load.php
        try:
            url = self._load_url(type="itv", action="get_all_channels",
                                 force_ch_link_check="", JsHttpRequest="1-xml")
            self.log("[STALKER] get_all_channels: trying load.php…")
            async with self.session.get(url, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=30)) as r:
                self.log(f"[STALKER] get_all_channels load.php HTTP {r.status}")
                payload = await self._read_json(r, "get_all_channels (load.php)")
            self._all_channels_raw = [ch for ch in normalize_js(payload)
                                      if isinstance(ch, dict)]
            self.log(f"[STALKER] get_all_channels (load.php): {len(self._all_channels_raw)} channels")
        except Exception as e:
            self.log(f"[STALKER] get_all_channels load.php error: {e}")

        # Attempt 2: /stalker_portal/portal.php — only if attempt 1 returned nothing
        if not self._all_channels_raw:
            try:
                url2 = self._load_url_alt(type="itv", action="get_all_channels",
                                          force_ch_link_check="", JsHttpRequest="1-xml")
                self.log("[STALKER] get_all_channels: trying portal.php…")
                async with self.session.get(url2, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=30)) as r2:
                    self.log(f"[STALKER] get_all_channels portal.php HTTP {r2.status}")
                    payload2 = await self._read_json(r2, "get_all_channels (portal.php)")
                self._all_channels_raw = [ch for ch in normalize_js(payload2)
                                          if isinstance(ch, dict)]
                self.log(f"[STALKER] get_all_channels (portal.php): {len(self._all_channels_raw)} channels")
            except Exception as e2:
                self.log(f"[STALKER] get_all_channels portal.php error: {e2}")

        return self._all_channels_raw

    async def _fetch_ch_logo_cache(self) -> dict:
        """Return {channel_id: logo_url} dict, derived from get_all_channels.

        Reuses the already-fetched raw channel list — no extra network call.

        Race guard: if _ch_logo_cache is an empty dict (prefetch injected it as
        a shared placeholder), wait for the background prefetch to finish filling
        it in-place rather than firing a concurrent get_all_channels call."""
        if self._ch_logo_cache is not None and self._ch_logo_cache:
            return self._ch_logo_cache   # populated — fast path

        _evt = getattr(self, "_all_channels_ready_event", None)
        if self._ch_logo_cache is not None and not self._ch_logo_cache:
            if _evt is not None and not _evt.is_set():
                self.log("[STALKER] Logo cache: waiting for background prefetch…")
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: _evt.wait(20))
            return self._ch_logo_cache

        # _ch_logo_cache is None — no prefetch, make our own call.
        self._ch_logo_cache = {}
        channels = await self.get_all_channels()
        for ch in channels:
            ch_id = str(ch.get("id") or "").strip()
            logo = str(ch.get("logo") or ch.get("screenshot_uri") or
                       ch.get("tv_logo") or ch.get("pic") or "").strip()
            if ch_id and logo:
                self._ch_logo_cache[ch_id] = self._fix_logo_url(logo)
        self.log(f"[STALKER] Live logo cache: {len(self._ch_logo_cache)} entries")
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
        if page == 1:
            self.log(f"[STALKER] Fetching {mode.upper()} items cat={cat_id}…")
        async with self.session.get(url, headers=headers) as r:
            if page == 1:
                self.log(f"[STALKER] Items HTTP {r.status} ({mode.upper()} cat={cat_id} p={page})")
            payload = await self._read_json(r, f"Items ({mode.upper()} cat={cat_id} p={page})" if page == 1 else None)
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
                payload = await self._read_json(r2, f"Items alt ({mode.upper()} cat={cat_id})")
            items = normalize_js(payload)
        # Final fallback for live categories: filter _all_channels_raw by tv_genre_id.
        # Triggered when get_ordered_list returns "Access denied" or similar block — portal
        # allows get_all_channels but not per-category listing (seen on 4k1.new4k.cc).
        if not items and page == 1 and mode == "live" and cat_id not in ("*", "__all__"):
            raw_all = getattr(self, "_all_channels_raw", [])
            if raw_all:
                filtered = [ch for ch in raw_all
                            if isinstance(ch, dict) and str(ch.get("tv_genre_id", "")) == str(cat_id)]
                if filtered:
                    self.log(f"[STALKER] Items fallback: filtered {len(filtered)} channels "
                             f"from _all_channels_raw for cat={cat_id}")
                    items = filtered
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
        if page == 1:
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
        m = re.search(r'/ch/(\d+)_?', stub)
        if not m:
            return stub
        cid = m.group(1)
        cmd = f"ffmpeg http://localhost/ch/{cid}_"
        from urllib.parse import urlencode

        async def _try_resolve(base_url: str) -> str:
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
            url = f"{base_url}?{urlencode(params)}"
            headers = self._headers(include_auth=True)
            self.log(f"[STALKER] Resolving stub ch={cid} via {base_url.split('/')[-1]}")
            async with self.session.get(url, headers=headers) as r:
                self.log(f"[STALKER] Stub resolve HTTP {r.status} (ch={cid})")
                payload = await self._read_json(r, f"Stub resolve ch={cid}")
            if not isinstance(payload, dict):
                return ""
            js = payload.get("js", {})
            if isinstance(js, list) and js:
                js = js[0]
            if not isinstance(js, dict):
                return ""
            resolved = js.get("cmd") or js.get("url") or ""
            if not resolved:
                return ""
            resolved = resolved.strip()
            for _pfx in ("ffmpeg ", "auto ", "ffrt "):
                if resolved.lower().startswith(_pfx):
                    resolved = resolved.split(" ", 1)[1].strip()
                    break
            resolved = resolved.replace("\\/", "/")
            # Fix hostless URL: http://:/path or http:///path → prepend portal base
            if re.match(r'https?://[:/]', resolved):
                path_part = re.sub(r'^https?://[^/]*', '', resolved)
                resolved = self.base.rstrip('/') + '/' + path_part.lstrip('/')
                self.log(f"[STALKER] Fixed hostless stub URL → {resolved[:120]}")
            if resolved.startswith(("http://", "https://", "rtsp://")):
                # Reject if still a localhost/stub
                if "localhost" in resolved or re.search(r'https?:///ch/', resolved):
                    return ""
                self.log(f"[STALKER] Resolved ch={cid} → {resolved[:120]}")
                return resolved
            extracted = _extract_url_from_text(resolved)
            return extracted or ""

        # Try load.php first, then portal.php as fallback
        for endpoint in (f"{self.base}{self.LOAD_PHP}",
                         f"{self.base}{self.LOAD_PHP_ALT}"):
            result = await _try_resolve(endpoint)
            if result:
                return result

        # Final fallback: request http://portal_host/ch/{cid}_ directly with
        # full session headers (Authorization + Cookie). The portal may serve
        # the stream at this path when the request carries a valid session.
        try:
            from urllib.parse import urlparse as _up3
            _p3 = _up3(self.base)
            direct_url = f"{_p3.scheme}://{_p3.netloc}/ch/{cid}_"
            headers = self._headers(include_auth=True)
            self.log(f"[STALKER] Stub direct attempt → {direct_url}")
            async with self.session.get(direct_url, headers=headers,
                                        allow_redirects=True,
                                        timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status in (200, 206):
                    # Portal served the stream directly — return the URL
                    self.log(f"[STALKER] Stub direct succeeded (HTTP {r.status}) → {direct_url}")
                    return direct_url
                final_url = str(r.url)
                if final_url != direct_url and final_url.startswith(("http://", "https://")):
                    self.log(f"[STALKER] Stub direct redirected → {final_url}")
                    return final_url
                self.log(f"[STALKER] Stub direct HTTP {r.status} — no stream at portal host")
        except Exception as e:
            self.log(f"[STALKER] Stub direct error: {e}")

        self.log(f"[STALKER] Could not resolve stub ch={cid} — returning original")
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
        for _pfx in ("ffmpeg ", "auto ", "ffrt "):
            if cmd_value.lower().startswith(_pfx):
                cmd_value = cmd_value.split(" ", 1)[1].strip()
                break
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
        headers = self._headers(include_auth=True)
        self.log(f"[STALKER] create_link ({ptype}) cmd={cmd[:40]}…")

        async def _try_endpoint(base_url: str) -> dict:
            from urllib.parse import urlencode
            params = {"type": ptype, "action": "create_link", "cmd": cmd,
                      "series": "", "forced_storage": "0", "disable_ad": "0",
                      "download": "0", "force_ch_link_check": "0", "JsHttpRequest": "1-xml"}
            url = f"{base_url}?{urlencode(params)}"
            async with self.session.get(url, headers=headers) as r:
                self.log(f"[STALKER] create_link HTTP {r.status} ({base_url.split('/')[-1]})")
                return await self._read_json(r, "create_link") or {}

        # Go reference (channels.go): create_link goes to portal.php (c.Portal.Location),
        # NOT server/load.php. Try portal.php first, fall back to load.php.
        payload = await _try_endpoint(f"{self.base}{self.LOAD_PHP_ALT}")
        js = payload.get("js", {}) if isinstance(payload, dict) else {}
        if isinstance(js, list) and js:
            js = js[0]
        cmd_value = (js.get("cmd") or js.get("url") or "") if isinstance(js, dict) else ""

        if not cmd_value:
            # Fallback to load.php
            payload = await _try_endpoint(f"{self.base}{self.LOAD_PHP}")
            js = payload.get("js", {}) if isinstance(payload, dict) else {}
            if isinstance(js, list) and js:
                js = js[0]
            cmd_value = (js.get("cmd") or js.get("url") or "") if isinstance(js, dict) else ""

        if not cmd_value:
            # Both endpoints failed. Check what the raw responses said to give
            # a useful diagnostic instead of silently returning empty.
            self.log("[STALKER] create_link failed on all endpoints — stream unavailable for this account "
                     "(access denied 126 = account has no streaming permission / subscription restriction / "
                     "concurrent connection limit reached on portal side)")
            return ""

        cmd_value = cmd_value.strip()
        for _pfx in ("ffmpeg ", "auto ", "ffrt "):
            if cmd_value.lower().startswith(_pfx):
                cmd_value = cmd_value.split(" ", 1)[1].strip()
                break
        cmd_value = cmd_value.replace("\\/", "/")
        is_stub = (
            re.search(r'https?:///ch/', cmd_value) is not None or
            re.search(r'https?://localhost/ch/', cmd_value) is not None
        )
        if is_stub:
            return await self._resolve_stub_url(cmd_value)
        if cmd_value.startswith(("http://", "https://", "rtsp://")):
            self.log(f"[STALKER] create_link resolved → {cmd_value[:120]}")
            return cmd_value
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
        for _pfx in ("ffmpeg ", "auto ", "ffrt "):
            if cmd.lower().startswith(_pfx):
                cmd = cmd.split(" ", 1)[1].strip()
                break
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
        # Full live channel list — cached for the session lifetime.
        # None  = not yet fetched.
        # list  = already fetched (may be empty on failure).
        # Pre-seeded from state._items_cache by _make_client so any call to
        # get_all_channels() after a connect-time prefetch is a free list return.
        self._all_channels_raw: list | None = None

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
        # Compute server UTC offset using calendar.timegm (NOT datetime.timestamp).
        # datetime.timestamp() on a naive datetime applies CLIENT local timezone —
        # wrong on any non-UTC client. calendar.timegm always treats timetuple as UTC.
        # Example: server=UTC-4, client=UTC+2
        #   .timestamp() → -21600 (wrong: -6h)  calendar.timegm → -14400 (correct: -4h)
        self._server_utc_offset: int = 0
        try:
            import calendar as _cal
            from datetime import datetime as _dt2
            srv = data.get("server_info", {})
            if isinstance(srv, dict):
                ts_now = srv.get("timestamp_now") or srv.get("time")
                t_str  = srv.get("time_now") or srv.get("server_time") or ""
                tz_nm  = str(srv.get("timezone") or "").strip()
                if ts_now and t_str:
                    _srv_naive = _dt2.strptime(str(t_str)[:19], "%Y-%m-%d %H:%M:%S")
                    _offset    = _cal.timegm(_srv_naive.timetuple()) - int(float(ts_now))
                    if -50400 <= _offset <= 50400:
                        self._server_utc_offset = _offset
                        self.log(f"[XTREAM] Server UTC offset: {_offset:+d}s ({tz_nm or 'derived'})")
                elif tz_nm:
                    try:
                        import zoneinfo as _zi, datetime as _dm
                        _z = _zi.ZoneInfo(tz_nm)
                        self._server_utc_offset = int(_dm.datetime.now(_z).utcoffset().total_seconds())
                        self.log(f"[XTREAM] Server UTC offset: {self._server_utc_offset:+d}s (from {tz_nm!r})")
                    except Exception:
                        self.log(f"[XTREAM] Timezone {tz_nm!r} — zoneinfo unavailable, using UTC")
        except Exception as _tz_e:
            self.log(f"[XTREAM] Timezone detection failed ({_tz_e}), using UTC")
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
        # exp_date=None means the account has no expiry (unlimited)
        exp_raw = info.get("exp_date")
        exp = "unknown"
        try:
            if exp_raw is None:
                exp = "Unlimited"
            elif str(exp_raw) in ("", "0", "None"):
                exp = "Unlimited"
            elif str(exp_raw).isdigit():
                exp = datetime.fromtimestamp(int(exp_raw)).strftime("%Y-%m-%d")
            else:
                exp = str(exp_raw)
        except Exception:
            exp = str(exp_raw) if exp_raw else "unknown"
        max_conn_raw = info.get("max_connections", None)
        max_conn_int = 0
        try:
            if max_conn_raw is not None:
                max_conn_int = int(max_conn_raw)
        except Exception:
            pass
        active = info.get("active_cons", "?")
        status = info.get("status", "?")
        password = str(info.get("password", "") or "")
        self.log(f"[XTREAM] Account: user={self.username}  status={status}  expiry={exp}  connections={active}/{max_conn_raw}")
        return (self.username, exp, max_conn_int, password, str(active))

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

    async def get_all_channels(self, mode: str = "live") -> list:
        """Return all streams for the given mode in one call (no category filter).

        Xtream's get_live_streams / get_vod_streams / get_series with no
        category_id already returns everything — this is just a named wrapper
        so api_items() and api_global_search() can call it uniformly.

        For live mode, results are cached in _all_channels_raw so repeated calls
        (from api_items, api_global_search, download_addon, api_find_channel) are
        free list returns.  _make_client pre-seeds this from state._items_cache
        so a connect-time prefetch means the first call is already a cache hit."""
        if mode == "live" and self._all_channels_raw is not None:
            return self._all_channels_raw
        result = await self.fetch_items_page(mode, "", 1)
        if mode == "live":
            self._all_channels_raw = result
        return result

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
                # XtreamClient.account_info() returns (ident, exp, max_conn, password, active_cons) — pass through
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

    async def get_all_channels(self, mode: str = "live") -> list:
        """Return all items for the given mode without category filtering.

        Tries the wrapped Xtream client first, then flattens all groups from
        the preloaded M3U data."""
        if self._xtream_client:
            try:
                result = await self._xtream_client.get_all_channels(mode)
                if result:
                    return result
            except Exception:
                pass
        type_filter = self._type_filter(mode)
        out = []
        for items in self._all_groups.values():
            for it in items:
                tvg = it.get("tvg_type", "")
                if tvg in type_filter or (mode == "live" and tvg == ""):
                    out.append(it)
        return out

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
