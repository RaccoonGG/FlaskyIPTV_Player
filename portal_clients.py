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
  parse_expiry_to_epoch(exp_str)   Best-effort portal expiry string → epoch.
  extract_http_status_from_message(msg)  Pull embedded "(HTTP nnn)" from a message.
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
import concurrent.futures
import hashlib
import json
import os
import random
import re
import string
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, quote, quote_plus, unquote, parse_qs
import asyncio
import aiohttp

# ── Inlined IPTV device / app UA profiles ────────────────────────────────────
# Sourced from real-world IPTV client fingerprints.  Used by all three portal
# clients (Stalker/MAC, Xtream, M3U) to send appropriate headers.
#
# For Stalker portals the full profile dict is merged into every request (so
# X-User-Agent, stb_type, image_version etc. are set).  For Xtream/M3U only
# the User-Agent string is injected into the session / fetch headers.

_UA_PROFILES: dict = {
    # ── Original Stalker/MAC default — preserves pre-update hardcoded headers ──
    "MAG250": {
        "User-Agent": (
            "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
            "AppleWebKit/533.3 (KHTML, like Gecko) "
            "MAG200 stbapp ver: 2 rev: 250 Safari/533.3"
        ),
        "X-User-Agent":    "Model: MAG250; Link: WiFi",
        "Accept":          "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "stb_type":        "MAG250",
        "image_version":   "218",
    },
    "MAG254": {
        "User-Agent": (
            "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
            "AppleWebKit/533.3 (KHTML, like Gecko) "
            "MAG200 stbapp ver: 2 rev: 250 Safari/533.3"
        ),
        "X-User-Agent":    "Model: MAG254; Link: WiFi",
        "Accept":          "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "stb_type":        "MAG254",
        "image_version":   "218",
    },
    "MAG322": {
        "User-Agent": (
            "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
            "AppleWebKit/538.1 (KHTML, like Gecko) "
            "MAG200 stbapp ver: 4 rev: 1812 Safari/538.1"
        ),
        "X-User-Agent":    "Model: MAG322; Link: WiFi",
        "Accept":          "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "stb_type":        "MAG322",
        "image_version":   "312",
    },
    "TiviMate": {
        "User-Agent":       "TiviMate/4.7.0 (Linux; Android 12; sdk_gphone_x86)",
        "Accept":           "application/json, text/plain, */*",
        "Accept-Language":  "en-US,en;q=0.8",
        "Accept-Encoding":  "gzip, deflate",
        "X-Requested-With": "ar.tvplayer.tv",
    },
    "GSE_IPTV": {
        "User-Agent":      "GSE IPTV/7.6 CFNetwork/1410.0.3 Darwin/22.6.0",
        "Accept":          "*/*",
        "Accept-Language": "en-us",
        "Accept-Encoding": "gzip, deflate",
    },
    "OTTPlayer": {
        "User-Agent":      "OTTPlayer/2.3 CFNetwork/1209 Darwin/20.2.0",
        "Accept":          "*/*",
        "Accept-Encoding": "gzip, deflate",
    },
    "IPTVSmarters": {
        "User-Agent": (
            "IPTV Smarters Pro Mozilla/5.0 "
            "(Linux; Android 10; K) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36"
        ),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Origin":          "file://",
    },
    "VLC": {
        # Version kept at 3.0.0 to preserve pre-update default stream behaviour.
        "User-Agent":      "VLC/3.0.0 LibVLC/3.0.0",
        "Accept":          "*/*",
        "Accept-Encoding": "gzip, deflate",
    },
    "Chrome": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
    },
}

# Lead profile name per portal type (used as auto-default when preset is "")
# "mac" maps to MAG250 to exactly reproduce the pre-update hardcoded headers.
_UA_DEFAULT: dict = {
    "mac":     "MAG250",
    "xtream":  "TiviMate",
    "m3u_url": "VLC",
}

def get_effective_ua(preset: str, custom: str, conn_type: str = "mac") -> tuple:
    """
    Resolve the active User-Agent and auxiliary headers for a portal session.

    Parameters
    ----------
    preset    : key from _UA_PROFILES (e.g. "MAG254", "TiviMate"), or
                "custom" to use the ``custom`` string verbatim, or
                "" for the auto-default for ``conn_type``.
    custom    : free-form UA string; only used when preset == "custom".
    conn_type : "mac" | "xtream" | "m3u_url" — selects the auto-default.

    Returns
    -------
    (ua_string: str, headers_dict: dict)

    ``headers_dict`` always contains at least {"User-Agent": ua_string}.
    STB presets (MAG254, MAG322) also return X-User-Agent / stb_type /
    image_version which StalkerPortalClient._headers() merges in.

    Custom UA: uses the conn_type default profile as the header base so
    Xtream/M3U still get correct Accept, Accept-Language etc. — only
    User-Agent is replaced with the custom string.
    """
    p = (preset or "").strip()

    if p == "custom":
        ua = (custom or "").strip()
        if ua:
            # Use the conn_type default profile as the surrounding header base
            # so the request looks complete (Accept, Accept-Language etc.)
            # while only the User-Agent string is replaced with the custom value.
            # STB-specific keys (X-User-Agent, stb_type, image_version) are
            # excluded so a custom UA on a Stalker portal doesn't accidentally
            # claim to be a MAG box.
            _STB_KEYS = frozenset({"X-User-Agent", "stb_type", "image_version"})
            default_key = _UA_DEFAULT.get(conn_type, "MAG250")
            profile = {k: v for k, v in _UA_PROFILES[default_key].items()
                       if k not in _STB_KEYS}
            profile["User-Agent"] = ua
            return ua, profile
        # custom selected but field left blank → fall through to auto-default

    if p and p in _UA_PROFILES:
        profile = dict(_UA_PROFILES[p])
        return profile["User-Agent"], profile

    # Auto-default
    default_key = _UA_DEFAULT.get(conn_type, "MAG250")
    profile = dict(_UA_PROFILES[default_key])
    return profile["User-Agent"], profile

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


# Sentinel expiry values seen across MAC/Stalker/Xtream portals that mean
# "no expiry" rather than an actual date — must NOT be parsed as "expired".
_NO_EXPIRY_SENTINELS = {
    "", "unknown", "none", "null", "never", "unlimited", "0", "n/a", "-",
}

# Formats seen in the wild across MAC/Stalker "phone"/"end_date"/
# "expire_billing_date" fields and Xtream's formatted display string.
# Tried in order; first successful parse wins.
_EXPIRY_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y",
    "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
)


def parse_expiry_to_epoch(exp_str) -> "int | None":
    """Best-effort parse of a portal's expiry field into a unix epoch.

    Deliberately conservative: returns None (i.e. "unknown, do not treat as
    expired") for anything that isn't confidently parseable, rather than
    risking a false "this account is expired" verdict from a misread date.
    Field naming/format is not standardized across MAC/Stalker panels
    (phone / end_date / expire_billing_date, epoch or one of several date
    formats) or even guaranteed consistent for a single panel vendor.
    """
    if exp_str is None:
        return None
    s = str(exp_str).strip()
    if s.lower() in _NO_EXPIRY_SENTINELS:
        return None
    # Pure-digit epoch (seconds). Xtream's raw exp_date is commonly this
    # before formatting; some MAC panels also expose a raw epoch string.
    if s.isdigit():
        try:
            val = int(s)
            # Guard against obviously-wrong values (e.g. a plan-id or a
            # count that happens to be numeric) — a unix epoch for any
            # plausible subscription expiry falls comfortably in this range.
            if 0 < val < 4102444800:  # < year 2100
                return val
        except (ValueError, OverflowError):
            return None
        return None
    for fmt in _EXPIRY_DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


_HTTP_STATUS_IN_MSG_RE = re.compile(r'HTTP[:\s]+(\d{3})\b')


def extract_http_status_from_message(msg) -> "int | None":
    """Pull an HTTP status code out of an exception message, where present.
    Several client error messages already embed '(HTTP 456)'-style text —
    this lets a caller classify on it without needing a new exception type
    per status code. Returns None when no status is embedded (e.g. a plain
    connector/timeout exception, or a Stalker message that predates this)."""
    if not msg:
        return None
    m = _HTTP_STATUS_IN_MSG_RE.search(str(msg))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
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
    def __init__(self, base_url: str, mac: str, log_cb,
                 ua_preset: str = "", custom_ua: str = ""):
        self.base = normalize_base_url(base_url)
        self.mac = mac.strip().upper()
        self.log = log_cb
        self._extract_url_from_text = _extract_url_from_text
        self.session = None
        self.token = None
        self.headers = {}
        self.ua_preset: str = (ua_preset or "").strip()
        self.custom_ua: str = (custom_ua or "").strip()
        # Logo caches — keyed by item id → logo URL.
        # _ch_logo_cache: populated once via get_all_channels (live fallback).
        # _vod_logo_cache: built lazily from already-fetched VOD/series items
        #   (no extra round-trip; avoids the 2-request pattern stalker uses for live).
        self._ch_logo_cache: dict | None = None
        self._vod_logo_cache: dict = {}
        # Full raw channel list from get_all_channels — populated once per session.
        # None = not yet attempted. list = already fetched (may be empty on failure).
        self._all_channels_raw: list | None = None
        # Full VOD / Series lists — same None/list semantics.
        # Populated via parse_xtream_info() + player_api.php calls when the
        # MAC portal's stream URLs contain embedded Xtream credentials.
        self._all_vod_raw: list | None = None
        self._all_series_raw: list | None = None
        # Xtream credentials extracted from a VOD stream cmd URL.
        # None = not yet attempted.  {} = tried and failed (no Xtream API).
        # dict with keys "base", "username", "password" = success.
        self._xtream_creds: dict | None = None
        # Full JS dict from get_main_info — cached so FlaskyIPTV can read
        # timezone, comment, ip, storages without an extra round-trip.
        self._last_account_js: dict | None = None
        # Full JS dict from get_profile — may carry extra fields absent in
        # get_main_info: default_timezone, parent_password, settings_password,
        # ip, storages, comment.  None = not yet attempted.  {} = tried+failed.
        self._last_profile_js: dict | None = None

    async def __aenter__(self):
        _timeout = aiohttp.ClientTimeout(total=15, connect=8)
        self.session = aiohttp.ClientSession(cookies={"mac": self.mac}, timeout=_timeout)
        # Resolve the effective UA profile for this session.
        _ua, _profile = get_effective_ua(
            "custom" if self.custom_ua else self.ua_preset,
            self.custom_ua,
            "mac",
        )
        # MAG-family presets (MAG250/254/322) carry stb_type, X-User-Agent,
        # image_version — generic MAC portals run the same Infomir portal stack
        # as Stalker portals and recognise these headers, so let them through.
        # Non-MAG presets (TiviMate, VLC, Chrome, custom…) don't emit STB
        # identity headers — they'd be incongruent alongside a non-MAG UA.
        _is_mag_profile = "stb_type" in _profile
        _ALWAYS_SKIP = frozenset({"Connection", "Upgrade-Insecure-Requests"})
        _NON_MAG_SKIP = frozenset({"stb_type", "image_version", "X-User-Agent"})
        session_headers = {
            k: v for k, v in _profile.items()
            if k not in _ALWAYS_SKIP
            and (k not in _NON_MAG_SKIP or _is_mag_profile)
        }
        session_headers["User-Agent"] = _ua
        self.session.headers.update(session_headers)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # When managed by PortalSessionManager, the session lifetime is controlled
        # externally — do NOT close it here.
        if not getattr(self, "_externally_managed", False):
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
                payload = await self._read_json(r, "handshake")
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

    async def _read_json(self, r: aiohttp.ClientResponse, tag: str = "") -> "dict | list | None":
        """Read response text, log a raw preview, then parse and return JSON.

        Mirrors StalkerPortalClient._read_json so the Activity Log shows the
        same raw-response lines for MAC portal calls as for Stalker calls.
        tag  — label used in the log line, e.g. "account_info", "get_profile".
              Pass "" to skip logging (silent parse-only path).
        """
        try:
            text = await r.text()
        except Exception as e:
            if tag:
                self.log(f"[MAC] {tag} read error: {e}")
            return None
        if tag:
            preview = repr(text[:800]) if text else "''"
            self.log(f"[MAC] {tag} raw: {preview}")
        if not text or not text.strip():
            return None
        t = text.lstrip()
        if not (t.startswith("{") or t.startswith("[")):
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    async def account_info(self):
        assert self.session is not None
        url = f"{self.base}/portal.php?type=account_info&action=get_main_info&JsHttpRequest=1-xml"
        self.log("[MAC] Fetching account info…")
        payload = None
        for _attempt in range(4):
            async with self.session.get(url, headers=self.headers) as r:
                self.log(f"[MAC] Account info HTTP {r.status}")
                if r.status == 429:
                    _wait = 2 ** _attempt
                    self.log(f"[MAC] Account info 429 — backing off {_wait}s (attempt {_attempt+1}/4)")
                    await asyncio.sleep(_wait)
                    continue
                payload = await self._read_json(r, "account_info")
                break
        if not isinstance(payload, dict):
            return ("unknown", "unknown")
        js = payload.get("js")
        if isinstance(js, list) and js:
            js = js[0]
        if not isinstance(js, dict):
            return ("unknown", "unknown")
        self._last_account_js = js  # set immediately so caller always has full dict
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

    async def get_profile(self) -> dict:
        """Fetch portal.php?type=stb&action=get_profile with 429 backoff retry.

        Returns the raw js dict.  Caches in _last_profile_js.  Returns {} on
        failure so callers can always safely do .get().

        This endpoint carries fields that get_main_info typically omits:
        default_timezone, parent_password, settings_password, ip, storages.
        The connect handler merges these into _last_account_js so the rest of
        the profile-display logic needs no changes.
        """
        if self._last_profile_js is not None:
            return self._last_profile_js
        assert self.session is not None
        url = (f"{self.base}/portal.php?type=stb&action=get_profile"
               f"&hd=1&not_valid_token=0&video_out=hdmi"
               f"&auth_second_step=0&num_banks=2&JsHttpRequest=1-xml")
        self.log("[MAC] Fetching get_profile…")
        payload = None
        for _attempt in range(4):
            async with self.session.get(url, headers=self.headers) as r:
                self.log(f"[MAC] get_profile HTTP {r.status}")
                if r.status == 429:
                    _wait = 2 ** _attempt
                    self.log(f"[MAC] get_profile 429 — backing off {_wait}s (attempt {_attempt+1}/4)")
                    await asyncio.sleep(_wait)
                    continue
                payload = await self._read_json(r, "get_profile")
                break
        js: dict = {}
        if isinstance(payload, dict):
            raw = payload.get("js", {})
            if isinstance(raw, list) and raw:
                raw = raw[0]
            if isinstance(raw, dict):
                js = raw
        self._last_profile_js = js
        if js:
            tz = js.get("default_timezone") or js.get("timezone") or ""
            self.log(f"[MAC] get_profile: timezone={tz!r}  ip={js.get('ip', '')!r}")
        return js

    async def get_all_channels(self, mode: str = "live") -> list:
        """Fetch ALL live channels in one shot via type=itv&action=get_all_channels.

        This action is live-only in the MAC/Stalker portal protocol.
        Returns [] for non-live modes so api_items() falls back to per-category
        pagination (which is the correct path for VOD/Series on these portals).

        Returns the raw list of channel dicts.  Result is cached for the
        lifetime of the client instance so subsequent calls are free.
        Returns [] on error (caller should fall back to pagination)."""
        if mode != "live":
            return []
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
        # Normalize archive flag → tv_archive (unified field checked by UI)
        for ch in self._all_channels_raw:
            if isinstance(ch, dict):
                ch.setdefault("tv_archive", 1 if ch.get("enable_tv_archive", 0) else 0)
        return self._all_channels_raw

    async def parse_xtream_info(self) -> dict | None:
        """Comprehensive Xtream credential extraction.

        Order:
          1) Probe Player API shapes (/c/ variants and player_api.php?mac=...) for direct credentials.
          2) Check profile-embedded credentials (get_profile cached).
          3) Fallback: get_ordered_list -> create_link, parse JSON/escaped cmd, handle:
               - Xtream query-string (get.php?username=...&password=...)
               - Path-style /USER/PASS/ (strip tokens)
               - ffmpeg play URLs (return typed play_url)
               - id-based resolve endpoints
          4) Final fallback: attempt original brittle split/text parsing on raw responses.
        Returns:
          - {"type":"xtream","base":..., "username":..., "password":...}
          - {"type":"play_url","base":..., "stream_url":..., "play_token":..., "stream":..., "mac":...}
          - None
        Caches {} in self._xtream_creds to mark tried+failed.
        """
        if self._xtream_creds is not None:
            return self._xtream_creds if self._xtream_creds else None

        self._xtream_creds = {}  # mark as tried
        assert self.session is not None

        _SAFE_CRED_CHARS = frozenset("?=&/ \t\n\\:")

        def _is_safe_cred(s: str) -> bool:
            return isinstance(s, str) and (3 <= len(s) <= 64) and not any(c in s for c in _SAFE_CRED_CHARS)

        def _try_extract_xtream_from_url(candidate: str):
            if not candidate:
                return None
            cand = candidate.replace("\\/", "/").strip()
            # direct query/path extraction helper from module
            xt = extract_xtream_from_m3u_url(cand)
            if xt:
                xt["base"] = normalize_base_url(xt["base"])
                return {"type": "xtream", "base": xt["base"],
                        "username": xt["username"], "password": xt["password"]}
            # path-style: /movie/USER/PASS/id.ext, /live/USER/PASS/id.ts, etc.
            try:
                p = urlparse(cand)
                parts = [seg for seg in p.path.strip("/").split("/") if seg]
                if parts and parts[0].lower() in {"movie", "series", "live", "hls", "ts"}:
                    parts = parts[1:]
                if len(parts) >= 2:
                    u = parts[0]
                    pw = parts[1].split("?")[0].split(";")[0]
                    if "=" in pw:
                        pw = pw.split("=", 1)[0]
                    if _is_safe_cred(u) and _is_safe_cred(pw) and "." not in u and "." not in pw:
                        base = (f"{p.scheme}://{p.netloc}"
                                if p.scheme and p.netloc
                                else self.base)
                        return {"type": "xtream", "base": base, "username": u, "password": pw}
            except Exception:
                pass
            return None

        # ── Step 1: Player API probe — try multiple endpoint shapes ─────────
        try:
            probe_variants = [
                f"{self.base}/player_api.php",              # clean Xtream endpoint — try first
                f"{self.base}/c/",
                f"{self.base}/c/{self.token}" if self.token else None,
                f"{self.base}/c/{self.token}?mac={self.mac}" if self.token else None,
                f"{self.base}/player_api.php?mac={self.mac}",
                f"{self.base}/portal.php?type=account_info&action=get_main_info&JsHttpRequest=1-xml",
                f"{self.base}/portal.php?type=stb&action=get_profile&JsHttpRequest=1-xml",
            ]
            for probe in [p for p in probe_variants if p]:
                try:
                    async with self.session.get(probe, headers=self.headers,
                                                timeout=aiohttp.ClientTimeout(total=8)) as rp:
                        self.log(f"[MAC] Probe {probe.split('?')[0]} HTTP {rp.status}")
                        txt = await rp.text()
                        self.log(f"[MAC] Probe raw: {repr(txt[:200])}")
                except Exception:
                    continue
                try:
                    parsed = json.loads(txt)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    # user_info / server_info shape (native Xtream player_api.php)
                    ui = parsed.get("user_info") or parsed.get("user") or parsed.get("auth")
                    si = parsed.get("server_info") or parsed.get("server")
                    if isinstance(ui, dict):
                        user = ui.get("username") or ui.get("user") or ui.get("login")
                        pwd  = ui.get("password") or ui.get("pass")
                        host = None
                        port = None
                        if isinstance(si, dict):
                            host = si.get("url") or si.get("host") or si.get("server")
                            port = si.get("port") or si.get("https_port")
                        if not host:
                            host = parsed.get("server") or parsed.get("url")
                        if user and pwd:
                            host = str(host).strip().replace("\\/", "/") if host else None
                            if host and port:
                                base = f"http://{host}:{port}"
                            elif host:
                                try:
                                    p = urlparse(host)
                                    base = f"{p.scheme}://{p.netloc}" if p.netloc else f"http://{host}"
                                except Exception:
                                    base = f"http://{host}"
                            else:
                                base = self.base
                            self.log(f"[MAC] Xtream creds via player_api probe (user_info): user={user} pass={pwd}")
                            self._xtream_creds = {"type": "xtream", "base": base,
                                                  "username": str(user), "password": str(pwd)}
                            return self._xtream_creds
                    # MAC / Stalker js-wrapper shape
                    js = parsed.get("js")
                    if isinstance(js, list) and js:
                        js = js[0]
                    if isinstance(js, dict):
                        user = js.get("username") or js.get("login") or js.get("user") or js.get("fname")
                        pwd  = js.get("password") or js.get("pass") or js.get("parent_password")
                        host = js.get("url") or js.get("host") or js.get("server")
                        port = js.get("port")
                        if user and pwd and host:
                            host = str(host).strip().replace("\\/", "/")
                            if port:
                                base = f"http://{host}:{port}"
                            else:
                                try:
                                    p = urlparse(host)
                                    base = f"{p.scheme}://{p.netloc}" if p.netloc else f"http://{host}"
                                except Exception:
                                    base = f"http://{host}"
                            self.log(f"[MAC] Xtream creds via player_api probe (js): user={user} pass={pwd}")
                            self._xtream_creds = {"type": "xtream", "base": base,
                                                  "username": str(user), "password": str(pwd)}
                            return self._xtream_creds
                # text fallback: brittle split when JSON is unparseable
                if '"username"' in txt and '"password"' in txt:
                    try:
                        u = txt.split('"username"')[1].split(':', 1)[1].split(',')[0].strip().strip('" ')
                        p = txt.split('"password"')[1].split(':', 1)[1].split(',')[0].strip().strip('" ')
                        if u and p:
                            base = normalize_base_url(self._extract_url_from_text(txt) or self.base)
                            self.log(f"[MAC] Xtream creds via player_api probe (text): user={u} pass={p}")
                            self._xtream_creds = {"type": "xtream", "base": base,
                                                  "username": u, "password": p}
                            return self._xtream_creds
                    except Exception:
                        pass
        except Exception:
            pass

        # ── Step 0: profile-embedded credentials (cached, zero network cost) ─
        try:
            _profile = self._last_profile_js or {}
            _prof_login = str(_profile.get("login") or "").strip()
            _prof_pass  = str(_profile.get("password") or "").strip()
            if _is_safe_cred(_prof_login) and _is_safe_cred(_prof_pass):
                self.log(f"[MAC] Xtream creds from profile: user={_prof_login} pass={_prof_pass}")
                self._xtream_creds = {"type":"xtream", "base": self.base, "username": _prof_login, "password": _prof_pass}
                return self._xtream_creds
        except Exception:
            pass

        # ── Step 2: get_ordered_list → create_link ─────────────────────────
        try:
            url1 = (f"{self.base}/portal.php?type=vod&action=get_ordered_list"
                    f"&category=*&JsHttpRequest=1-xml&p=1&sortby=added")
            async with self.session.get(url1, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=15)) as r1:
                payload1 = await safe_json(r1)
            items = normalize_js(payload1)
            raw_cmd = ""
            item_id = None
            content_type = "vod"
            for item in items:
                if isinstance(item, dict):
                    if item.get("cmd"):
                        raw_cmd = str(item["cmd"]).replace("\\/", "/").strip()
                        item_id = str(item.get("id") or item.get("vod_id") or "")
                        if raw_cmd:
                            break
            if not raw_cmd:
                self.log("[MAC] parse_xtream_info: no VOD items — trying live channels")
                # Fallback: live channels (type=itv) often share the same Xtream backend
                content_type = "itv"
                try:
                    url_itv = (f"{self.base}/portal.php?type=itv&action=get_all_channels"
                               f"&force_ch_link_check=0&JsHttpRequest=1-xml")
                    async with self.session.get(url_itv, headers=self.headers,
                                                timeout=aiohttp.ClientTimeout(total=15)) as r_itv:
                        payload_itv = await safe_json(r_itv)
                    for item in (normalize_js(payload_itv) or []):
                        if isinstance(item, dict) and item.get("cmd"):
                            raw_cmd = str(item["cmd"]).replace("\\/", "/").strip()
                            item_id = str(item.get("id") or "")
                            if raw_cmd:
                                break
                except Exception as e:
                    self.log(f"[MAC] parse_xtream_info: live-channel fallback error: {e}")
            if not raw_cmd:
                self.log("[MAC] parse_xtream_info: no cmd field in VOD or live items")
                return None

            url2 = (f"{self.base}/portal.php?type={content_type}&action=create_link"
                    f"&cmd={quote(raw_cmd)}&series=&forced_storage=&disable_ad=0&download=0&force_ch_link_check=0&JsHttpRequest=1-xml")
            async with self.session.get(url2, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=15)) as r2:
                raw2 = await r2.text()

            self.log(f"[MAC] create_link raw: {repr(raw2[:200])}")

            # extract link from JSON or raw text
            link = ""
            try:
                parsed = json.loads(raw2)
                js = parsed.get("js")
                if isinstance(js, list) and js:
                    js = js[0]
                if isinstance(js, dict):
                    link = str(js.get("cmd") or js.get("link") or js.get("url") or "").replace("\\/", "/").strip()
                    if not link and js.get("id"):
                        item_id = item_id or str(js.get("id"))
            except Exception:
                if 'cmd":"' in raw2:
                    link = raw2.split('cmd":"', 1)[1].split('"', 1)[0].replace("\\/", "/").strip()
                elif 'cmd":' in raw2:
                    link = raw2.split('cmd":', 1)[1].split('\n', 1)[0].strip().strip('",')

            # If link contains ffmpeg play URL, resolve it first, then try
            # Xtream path extraction -- /movie/USER/PASS/id.ext carries creds.
            if link and "ffmpeg" in link.lower():
                if " " in link:
                    link = link.split(" ", 1)[-1].strip()
                play_url = link
                parsed = urlparse(play_url)
                qs = parse_qs(parsed.query)
                play_token = (qs.get("play_token") or qs.get("token") or [""])[0]
                stream = (qs.get("stream") or [""])[0]
                mac_q = (qs.get("mac") or [""])[0]
                base = (f"{parsed.scheme}://{parsed.netloc}"
                        if parsed.scheme and parsed.netloc
                        else normalize_base_url(play_url))
                self.log(f"[MAC] create_link resolved → {play_url}")
                # Prefer Xtream path creds (/movie/USER/PASS/...) when present
                # so get_vod_streams/get_series can call player_api.php directly.
                _xt = _try_extract_xtream_from_url(play_url)
                if _xt and _xt.get("type") == "xtream":
                    self.log(f"[MAC] create_link ffmpeg → Xtream creds: "
                             f"user={_xt.get('username')!r} "
                             f"pass={_xt.get('password')!r} "
                             f"base={_xt.get('base')!r}")
                    self._xtream_creds = _xt
                    return self._xtream_creds
                # No path creds -- fall back to play_url type
                self.log(f"[MAC] create_link ffmpeg: no Xtream path creds; "
                         f"play_token={play_token!r}")
                self._xtream_creds = {"type": "play_url", "base": base, "stream_url": play_url,
                                      "play_token": play_token, "stream": stream, "mac": mac_q}
                return self._xtream_creds

            # Try direct Xtream extraction from resolved link
            if link:
                res = _try_extract_xtream_from_url(link)
                if res:
                    self._xtream_creds = res
                    self.log(f"[MAC] Xtream creds via create_link: user={res.get('username')} pass={res.get('password')}")
                    return self._xtream_creds
                # link contains a .php endpoint — treat as play_url
                if ".php" in link:
                    parsed = urlparse(link)
                    base = (f"{parsed.scheme}://{parsed.netloc}"
                            if parsed.scheme and parsed.netloc
                            else normalize_base_url(link))
                    qs = parse_qs(parsed.query)
                    play_token = (qs.get("play_token") or qs.get("token") or [""])[0]
                    stream = (qs.get("stream") or [""])[0]
                    mac_q = (qs.get("mac") or [""])[0]
                    self._xtream_creds = {"type": "play_url", "base": base, "stream_url": link,
                                          "play_token": play_token, "stream": stream, "mac": mac_q}
                    return self._xtream_creds

            # ── id-resolve fallback: try alternate endpoints with item id ──
            if item_id:
                candidate_endpoints = [
                    f"{self.base}/portal.php?type=vod&action=get_streams&id={quote(item_id)}&JsHttpRequest=1-xml",
                    f"{self.base}/portal.php?type=vod&action=get_item&item_id={quote(item_id)}&JsHttpRequest=1-xml",
                    f"{self.base}/portal.php?type=stream&action=get_streams&id={quote(item_id)}&JsHttpRequest=1-xml",
                    f"{self.base}/portal.php?type=vod&action=get_ordered_list&JsHttpRequest=1-xml&p=1&id={quote(item_id)}",
                ]
                for ep in candidate_endpoints:
                    try:
                        async with self.session.get(ep, headers=self.headers,
                                                    timeout=aiohttp.ClientTimeout(total=15)) as r3:
                            txt = await r3.text()
                    except Exception:
                        continue
                    self.log(f"[MAC] id-resolve HTTP — checking {ep.split('?')[0]}")
                    cand = self._extract_url_from_text(txt) or txt
                    res = _try_extract_xtream_from_url(cand)
                    if res:
                        self._xtream_creds = res
                        self.log(f"[MAC] Xtream creds from id-resolve: user={res.get('username')} pass={res.get('password')}")
                        return self._xtream_creds
                    if ".php" in cand:
                        try:
                            p = urlparse(cand)
                            base = (f"{p.scheme}://{p.netloc}"
                                    if p.scheme and p.netloc
                                    else normalize_base_url(cand))
                            qs = parse_qs(p.query)
                            play_token = (qs.get("play_token") or qs.get("token") or [""])[0]
                            stream = (qs.get("stream") or [""])[0]
                            mac_q = (qs.get("mac") or [""])[0]
                            self._xtream_creds = {"type": "play_url", "base": base, "stream_url": cand,
                                                  "play_token": play_token, "stream": stream, "mac": mac_q}
                            self.log(f"[MAC] Xtream play_url from id-resolve: {cand}")
                            return self._xtream_creds
                        except Exception:
                            pass

            # ── legacy text-parse fallback ────────────────────────────────
            try:
                txt_try = link if link else raw2
                if 'username=' in txt_try and 'password=' in txt_try:
                    parsed_q = urlparse(txt_try)
                    qs = parse_qs(parsed_q.query)
                    u = (qs.get("username") or qs.get("user") or [""])[0]
                    p = (qs.get("password") or qs.get("pass") or [""])[0]
                    if u and p:
                        base = (f"{parsed_q.scheme}://{parsed_q.netloc}"
                                if parsed_q.scheme and parsed_q.netloc
                                else normalize_base_url(txt_try))
                        self.log(f"[MAC] Xtream creds via legacy query parse: user={u} pass={p}")
                        self._xtream_creds = {"type": "xtream", "base": base, "username": u, "password": p}
                        return self._xtream_creds
                if 'username":"' in raw2 and 'password":"' in raw2:
                    try:
                        u = raw2.split('username":"', 1)[1].split('"', 1)[0]
                        p = raw2.split('password":"', 1)[1].split('"', 1)[0]
                        if u and p:
                            base = normalize_base_url(self._extract_url_from_text(raw2) or self.base)
                            self.log(f"[MAC] Xtream creds via legacy split parse: user={u} pass={p}")
                            self._xtream_creds = {"type": "xtream", "base": base, "username": u, "password": p}
                            return self._xtream_creds
                    except Exception:
                        pass
            except Exception:
                pass

            self.log("[MAC] parse_xtream_info: create_link did not yield usable Xtream creds")
            return None

        except Exception as e:
            self.log(f"[MAC] parse_xtream_info error: {e}")
            return None

    async def get_all_vod_streams(self) -> list:
        """Fetch ALL VOD streams via player_api.php?action=get_vod_streams.

        MAC portals built on Xtream Codes software expose player_api.php
        alongside the MAC portal API.  We extract the Xtream credentials
        from a VOD item cmd URL and call get_vod_streams with no category_id
        — returning all VOD items in one shot, analogous to get_live_streams.

        Items are normalised to MAC portal format (id, name, logo, cmd) so
        the frontend renders and plays them identically to items fetched via
        the portal API.

        Returns [] on failure (no Xtream creds, HTTP error, non-list response)
        so api_items() falls back to per-category pagination automatically.
        Result cached in _all_vod_raw for the session lifetime."""
        if self._all_vod_raw is not None:
            return self._all_vod_raw
        self._all_vod_raw = []
        creds = await self.parse_xtream_info()
        if not creds or creds.get("type") != "xtream":
            _ct = creds.get("type") if creds else None
            _cu = creds.get("username", "") if creds else ""
            _cp = creds.get("password", "") if creds else ""
            self.log(f"[MAC] get_vod_streams: creds type={_ct!r} user={_cu!r} pass={_cp!r} → skipping")
            return self._all_vod_raw
        base = creds["base"]; user = creds["username"]; pas = creds["password"]
        try:
            url = (f"{base}/player_api.php"
                   f"?username={user}&password={pas}&action=get_vod_streams")
            self.log("[MAC] get_vod_streams: fetching all VOD streams…")
            async with self.session.get(url, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=60)) as r:
                self.log(f"[MAC] get_vod_streams HTTP {r.status}")
                data = await r.json(content_type=None)
            if isinstance(data, list):
                out = []
                for it in data:
                    if not isinstance(it, dict):
                        continue
                    sid = str(it.get("stream_id", "")).strip()
                    if not sid:
                        continue
                    ext = it.get("container_extension") or "mp4"
                    out.append({
                        "id": sid,
                        "name": it.get("name", ""),
                        "logo": it.get("stream_icon", ""),
                        "screenshot_uri": it.get("stream_icon", ""),
                        # Construct cmd URL matching the MAC portal stream format
                        "cmd": f"{base}/movie/{user}/{pas}/{sid}.{ext}",
                        "category_id": str(it.get("category_id", "")),
                        "rating": str(it.get("rating", "")),
                        "added":  str(it.get("added", "")),
                    })
                self._all_vod_raw = out
                self.log(f"[MAC] get_vod_streams: {len(out)} items")
            else:
                self.log(f"[MAC] get_vod_streams: unexpected response type {type(data).__name__}")
        except Exception as e:
            self.log(f"[MAC] get_vod_streams error: {e}")
        return self._all_vod_raw

    async def get_all_series_streams(self) -> list:
        """Fetch ALL series via player_api.php?action=get_series.

        Same credential extraction and single-shot approach as
        get_all_vod_streams.  Items normalised to MAC portal format:
        id = series_id, logo = cover, _is_show_item = True so the frontend
        treats them as expandable series rather than playable streams.

        Returns [] on failure so api_items() falls back to per-category
        pagination.  Result cached in _all_series_raw."""
        if self._all_series_raw is not None:
            return self._all_series_raw
        self._all_series_raw = []
        creds = await self.parse_xtream_info()
        if not creds or creds.get("type") != "xtream":
            _ct = creds.get("type") if creds else None
            _cu = creds.get("username", "") if creds else ""
            _cp = creds.get("password", "") if creds else ""
            self.log(f"[MAC] get_series: creds type={_ct!r} user={_cu!r} pass={_cp!r} → skipping")
            return self._all_series_raw
        base = creds["base"]; user = creds["username"]; pas = creds["password"]
        try:
            url = (f"{base}/player_api.php"
                   f"?username={user}&password={pas}&action=get_series")
            self.log("[MAC] get_series: fetching all series…")
            async with self.session.get(url, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=60)) as r:
                self.log(f"[MAC] get_series HTTP {r.status}")
                data = await r.json(content_type=None)
            if isinstance(data, list):
                out = []
                for it in data:
                    if not isinstance(it, dict):
                        continue
                    sid = str(it.get("series_id", "")).strip()
                    if not sid:
                        continue
                    out.append({
                        "id": sid,
                        "name": it.get("name", ""),
                        "logo": it.get("cover", ""),
                        "screenshot_uri": it.get("cover", ""),
                        "category_id": str(it.get("category_id", "")),
                        "rating": str(it.get("rating", "")),
                        "plot": it.get("plot", ""),
                        "_is_show_item": True,
                        "series_id": sid,
                    })
                self._all_series_raw = out
                self.log(f"[MAC] get_series: {len(out)} items")
            else:
                self.log(f"[MAC] get_series: unexpected response type {type(data).__name__}")
        except Exception as e:
            self.log(f"[MAC] get_series error: {e}")
        return self._all_series_raw

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
        # Fast path: serve live category items from the get_all_channels pool.
        # Avoids redundant per-category HTTP calls when the pool is already cached.
        # For page > 1: only short-circuit if this cat_id has matches in the pool —
        # meaning page 1 was already served from the pool in one shot. If page 1
        # fell through to HTTP (pool had no matches for this cat_id), page 2+ must
        # also use HTTP to avoid truncating the paginated results.
        if mode == "live" and cat_id not in ("*", "__all__"):
            raw_all = getattr(self, "_all_channels_raw", None)
            if raw_all:
                if page == 1:
                    filtered = [ch for ch in raw_all
                                if isinstance(ch, dict)
                                and str(ch.get("tv_genre_id", "")) == str(cat_id)]
                    if filtered:
                        self.log(f"[MAC] {mode.upper()} cat={cat_id}: {len(filtered)} items (pool)")
                        return filtered
                    # Pool populated but no match for this cat_id — fall through to HTTP
                else:
                    # page > 1: return [] only if pool has channels for this category
                    # (confirming page 1 used the pool and returned everything at once)
                    if any(isinstance(ch, dict) and str(ch.get("tv_genre_id", "")) == str(cat_id)
                           for ch in raw_all):
                        return []
                    # No pool matches → page 1 used HTTP, continue paginating via HTTP

        # Fast path: serve VOD category items from the get_all_vod_streams cache.
        # When parse_xtream_info succeeded and get_all_vod_streams populated
        # _all_vod_raw, every VOD category can be served from RAM without extra
        # HTTP calls — same principle as the live channel pool above.
        if mode == "vod":
            raw_all = getattr(self, "_all_vod_raw", None)
            if raw_all is not None:
                if cat_id in ("*", "__all__"):
                    if page == 1:
                        self.log(f"[MAC] {mode.upper()} cat={cat_id}: {len(raw_all)} items (all_vod cache)")
                        return list(raw_all)
                    return []
                elif page == 1:
                    filtered = [it for it in raw_all
                                if isinstance(it, dict)
                                and str(it.get("category_id", "")) == str(cat_id)]
                    if filtered:
                        self.log(f"[MAC] {mode.upper()} cat={cat_id}: {len(filtered)} items (all_vod cache)")
                        return filtered
                    # Cache populated but no match for this cat_id — fall through to HTTP
                else:
                    # page > 1: return [] only if cache has items for this category
                    if any(isinstance(it, dict) and str(it.get("category_id", "")) == str(cat_id)
                           for it in raw_all):
                        return []
                    # No cache matches → page 1 used HTTP, continue paginating via HTTP

        # Fast path: serve Series category items from the get_all_series_streams cache.
        # Same logic as VOD above — when the Xtream API series pool is cached,
        # category browsing becomes a pure RAM filter with zero network calls.
        if mode == "series":
            raw_all = getattr(self, "_all_series_raw", None)
            if raw_all is not None:
                if cat_id in ("*", "__all__"):
                    if page == 1:
                        self.log(f"[MAC] {mode.upper()} cat={cat_id}: {len(raw_all)} items (all_series cache)")
                        return list(raw_all)
                    return []
                elif page == 1:
                    filtered = [it for it in raw_all
                                if isinstance(it, dict)
                                and str(it.get("category_id", "")) == str(cat_id)]
                    if filtered:
                        self.log(f"[MAC] {mode.upper()} cat={cat_id}: {len(filtered)} items (all_series cache)")
                        return filtered
                    # Cache populated but no match for this cat_id — fall through to HTTP
                else:
                    # page > 1: return [] only if cache has items for this category
                    if any(isinstance(it, dict) and str(it.get("category_id", "")) == str(cat_id)
                           for it in raw_all):
                        return []
                    # No cache matches → page 1 used HTTP, continue paginating via HTTP

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
        # Large VOD/Series categories can be slow; give them more time than live.
        _req_timeout = aiohttp.ClientTimeout(total=180 if mode in ("vod", "series") else 120)
        async with self.session.get(url, headers=self.headers, timeout=_req_timeout) as r:
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
        # Enrich live items with tv_archive from get_all_channels pool (authoritative source).
        # get_ordered_list may omit archive flags on some portal versions.
        # Copy tv_archive_duration too — the JS check requires both flags.
        if mode == "live" and items:
            raw_all = getattr(self, "_all_channels_raw", None)
            if raw_all:
                _amap = {str(ch.get("id", "")): ch for ch in raw_all
                         if isinstance(ch, dict) and ch.get("id")}
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    src = _amap.get(str(it.get("id", "")).strip())
                    if src:
                        it.setdefault("tv_archive", src.get("tv_archive", 0))
                        it.setdefault("tv_archive_duration", src.get("tv_archive_duration", 0))
            # Fallback: normalize enable_tv_archive directly if pool not yet ready
            for it in items:
                if isinstance(it, dict) and "tv_archive" not in it:
                    it["tv_archive"] = 1 if it.get("enable_tv_archive", 0) else 0
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
                 custom_device_id2: str = "", custom_signature: str = "",
                 ua_preset: str = "", custom_ua: str = ""):
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
        self.signature = hashlib.sha256(self.sg.encode("utf-8")).hexdigest().upper()
        # Allow caller to supply a pre-computed signature verbatim (e.g. copied
        # from a real MAG device or a known-working value for this portal).
        if custom_signature.strip():
            self.signature = custom_signature.strip()
        self.log(f"[STALKER] Computed IDs — SN={self.serial}  SNCUT={self.serialcut}  "
                 f"deviceid1={self.device_id}  deviceid2={self.device_id2}  "
                 f"signature={self.signature}"
                 + (" (custom)" if custom_signature.strip() else ""))
        # UA spoofing — preset name (e.g. "MAG254", "TiviMate") or "" for default,
        # or "custom" which uses custom_ua string verbatim.
        self.ua_preset: str = (ua_preset or "").strip()
        self.custom_ua: str = (custom_ua or "").strip()
        # Cache for channel id → logo URL, populated lazily from get_all_channels
        self._ch_logo_cache: dict | None = None
        # Running in-memory logo cache for VOD / series — populated from items
        # that already have a logo so we can fill blanks without extra requests.
        self._vod_logo_cache: dict = {}
        # Full raw channel list from get_all_channels — populated once per session.
        self._all_channels_raw: list | None = None
        # Full VOD / Series lists — same None/list semantics.
        # Stalker portals may expose player_api.php if running on Xtream Codes;
        # parse_xtream_info() probes for credentials via a VOD item cmd URL.
        # Falls back to [] (→ per-category pagination) if no Xtream API found.
        self._all_vod_raw: list | None = None
        self._all_series_raw: list | None = None
        # Xtream credentials extracted from a VOD stream cmd URL.
        # None = not yet attempted.  {} = tried and failed.  dict = success.
        self._xtream_creds: dict | None = None

    # ── context manager ──────────────────────────────────────────────────────

    async def __aenter__(self):
        _timeout = aiohttp.ClientTimeout(total=60, connect=10)
        # NO session-level cookies — stalker portals require Cookie as a header string
        self.session = aiohttp.ClientSession(timeout=_timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if not getattr(self, "_externally_managed", False):
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
        # Read PHPSESSID from the session jar if the server set a real one via
        # Set-Cookie.  Fall back to "null" for portals that use bearer-token
        # auth only and never establish a PHP session.
        _phpsessid = "null"
        if self.session is not None:
            _jar = {c.key: c.value for c in self.session.cookie_jar}
            _phpsessid = _jar.get("PHPSESSID") or "null"
        parts = [
            f"PHPSESSID={_phpsessid}",
            f"mac={quote(self.mac)}",
            f"sn={quote(self.serialcut)}",   # Go ref: SerialNumber = SNCUT (13-char)
            "stb_lang=en",
            f"timezone={quote('Europe/Paris')}",
        ]
        if include_token and self.bearer_token:
            parts.append(f"token={quote(self.bearer_token)}")
        return "; ".join(parts)

    def _headers(self, include_auth: bool = False, include_token: bool = True) -> dict:
        # Resolve the effective UA and any extra profile headers for this session.
        # get_effective_ua returns a (ua_str, profile_dict) tuple where profile_dict
        # may contain X-User-Agent, stb_type, image_version etc. for STB presets.
        _ua, _profile = get_effective_ua(self.ua_preset, self.custom_ua, "mac")
        h = {
            "Accept":          "*/*",
            "User-Agent":      _ua,
            "Referer":         f"{self.base}/stalker_portal/c/index.html",
            "Accept-Language": "en-US,en;q=0.5",
            "Pragma":          "no-cache",
            "Cookie":          self._cookie_str(include_token=include_token),
            "Accept-Encoding": "gzip, deflate",
        }
        # Merge profile headers — allow Accept, Accept-Language, Accept-Encoding
        # to be overridden by the preset so the full profile is applied correctly.
        # Only protect headers that are Stalker-protocol-critical and must not
        # be overwritten by any preset:
        #   Referer    — portal session anchor, must point at stalker_portal
        #   Cookie     — auth token, computed per-request
        #   Pragma     — always "no-cache" for Stalker cache-busting
        #   Connection — protected so profiles cannot inject "Connection: close"
        #                and defeat the keepalive connector.  We do NOT hardcode
        #                a value here ourselves; the TCPConnector(keepalive_timeout=30)
        #                on the persistent session handles connection reuse, and
        #                aiohttp will send "Connection: keep-alive" at transport level.
        _STALK_PRESERVE = frozenset({"Referer", "Cookie", "Pragma", "Connection"})
        # Also skip internal profile metadata keys that are not HTTP headers
        _NOT_HTTP = frozenset({"stb_type", "image_version"})
        for k, v in _profile.items():
            if k in _STALK_PRESERVE or k in _NOT_HTTP or k == "User-Agent":
                continue
            h[k] = v
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
        _last_status = None
        for _attempt in range(4):  # up to 3 retries on 429
            async with self.session.get(url, headers=headers) as r:
                _last_status = r.status
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
                        _last_status = r2.status
                        self.log(f"[STALKER] Retry handshake HTTP {r2.status}")
                        payload = await self._read_json(r2, "handshake retry")
                else:
                    payload = await self._read_json(r, "handshake")
                break

        if not isinstance(payload, dict) or "js" not in payload:
            raise RuntimeError(f"[STALKER] Handshake failed — no valid JSON response (HTTP {_last_status})")
        js = payload["js"]
        if not isinstance(js, dict):
            raise RuntimeError(f"[STALKER] Handshake failed — unexpected js structure (HTTP {_last_status})")
        self.token = js.get("token")
        if not self.token:
            raise RuntimeError(f"[STALKER] Handshake failed — token missing in response (HTTP {_last_status})")
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
            self.log(f"[STALKER] ✓ Profile accepted (variant {idx+1}: {stb_type})")
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
        payload = None
        for _attempt in range(4):
            async with self.session.get(url, headers=headers) as r:
                self.log(f"[STALKER] Profile HTTP {r.status}")
                if r.status == 429:
                    _wait = 2 ** _attempt
                    self.log(f"[STALKER] Profile 429 — backing off {_wait}s (attempt {_attempt+1}/4)")
                    await asyncio.sleep(_wait)
                    continue
                payload = await self._read_json(r, "get_profile")
                break
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
        """Fetch ALL live channels in one shot via type=itv&action=get_all_channels.

        This action is live-only in the Stalker portal protocol.
        Returns [] for non-live modes so api_items() falls back to per-category
        pagination (which is the correct path for VOD/Series on these portals).

        Tries load.php first, then portal.php as fallback.
        Result is cached for the lifetime of the client instance.
        Returns [] on complete failure (caller should fall back to pagination)."""
        if mode != "live":
            return []
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

        # Normalize archive flag → tv_archive (unified field checked by UI)
        for ch in self._all_channels_raw:
            if isinstance(ch, dict):
                ch.setdefault("tv_archive", 1 if ch.get("enable_tv_archive", 0) else 0)
        return self._all_channels_raw

    async def parse_xtream_info(self) -> dict | None:
        """Comprehensive Xtream credential extraction with retries and quieter logging.

        Return values:
          - {"type":"xtream","base":..., "username":..., "password":...}
          - {"type":"play_url","base":..., "stream_url":..., "play_token":..., "stream":..., "mac":...}
          - None

        Caches {} in self._xtream_creds to mark tried+failed.
        """
        if self._xtream_creds is not None:
            return self._xtream_creds if self._xtream_creds else None

        self._xtream_creds = {}  # mark as tried
        assert self.session is not None

        _SAFE_CRED_CHARS = frozenset("?=&/ \t\n\\:")

        def _is_safe_cred(s: str) -> bool:
            return isinstance(s, str) and (3 <= len(s) <= 64) and not any(c in s for c in _SAFE_CRED_CHARS)

        def _try_extract_xtream_from_url(candidate: str):
            if not candidate:
                return None
            cand = candidate.replace("\\/", "/").strip()
            xt = extract_xtream_from_m3u_url(cand)
            if xt:
                xt["base"] = normalize_base_url(xt["base"])
                return {"type": "xtream", "base": xt["base"],
                        "username": xt["username"], "password": xt["password"]}
            # path-style: /movie/USER/PASS/id.ext, /live/USER/PASS/id.ts, etc.
            try:
                p = urlparse(cand)
                parts = [seg for seg in p.path.strip("/").split("/") if seg]
                if parts and parts[0].lower() in {"movie", "series", "live", "hls", "ts"}:
                    parts = parts[1:]
                if len(parts) >= 2:
                    u = parts[0]
                    pw = parts[1].split("?")[0].split(";")[0]
                    if "=" in pw:
                        pw = pw.split("=", 1)[0]
                    if _is_safe_cred(u) and _is_safe_cred(pw) and "." not in u and "." not in pw:
                        base = (f"{p.scheme}://{p.netloc}"
                                if p.scheme and p.netloc
                                else self.base)
                        return {"type": "xtream", "base": base, "username": u, "password": pw}
            except Exception:
                pass
            return None

        # ── Step 1: Player API probe — try multiple endpoint shapes ─────────
        try:
            _headers = self._headers(include_auth=True)
            # Derive the stalker portal prefix from LOAD_PHP:
            # "/stalker_portal/server/load.php" → "/stalker_portal"
            _lp_parts = [p for p in self.LOAD_PHP.strip("/").split("/") if p]
            _stalker_pfx = ("/" + "/".join(_lp_parts[:-2])) if len(_lp_parts) > 2 else ""
            probe_variants = [
                f"{self.base}/player_api.php",                          # clean Xtream root — try first
                f"{self.base}{_stalker_pfx}/player_api.php" if _stalker_pfx else None,  # Xtream colocated with Stalker panel
                f"{self.base}/c/",
                f"{self.base}/c/{self.token}" if self.token else None,
                f"{self.base}/c/{self.token}?mac={self.mac}" if self.token else None,
                f"{self.base}/player_api.php?mac={self.mac}",
                f"{self.base}/portal.php?type=account_info&action=get_main_info&JsHttpRequest=1-xml",
                f"{self.base}/portal.php?type=stb&action=get_profile&JsHttpRequest=1-xml",
            ]
            for probe in [p for p in probe_variants if p]:
                try:
                    async with self.session.get(probe, headers=_headers,
                                                timeout=aiohttp.ClientTimeout(total=8)) as rp:
                        self.log(f"[STALKER] Probe {probe.split('?')[0]} HTTP {rp.status}")
                        txt = await rp.text()
                        self.log(f"[STALKER] Probe raw: {repr(txt[:200])}")
                except Exception:
                    continue
                try:
                    parsed = json.loads(txt)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    # user_info / server_info shape (native Xtream player_api.php)
                    ui = parsed.get("user_info") or parsed.get("user") or parsed.get("auth")
                    si = parsed.get("server_info") or parsed.get("server")
                    if isinstance(ui, dict):
                        user = ui.get("username") or ui.get("user") or ui.get("login")
                        pwd  = ui.get("password") or ui.get("pass")
                        host = None
                        port = None
                        if isinstance(si, dict):
                            host = si.get("url") or si.get("host") or si.get("server")
                            port = si.get("port") or si.get("https_port")
                        if not host:
                            host = parsed.get("server") or parsed.get("url")
                        if user and pwd:
                            host = str(host).strip().replace("\\/", "/") if host else None
                            if host and port:
                                base = f"http://{host}:{port}"
                            elif host:
                                try:
                                    p = urlparse(host)
                                    base = f"{p.scheme}://{p.netloc}" if p.netloc else f"http://{host}"
                                except Exception:
                                    base = f"http://{host}"
                            else:
                                base = self.base
                            self.log(f"[STALKER] Xtream creds via player_api probe (user_info): user={user} pass={pwd}")
                            self._xtream_creds = {"type": "xtream", "base": base,
                                                  "username": str(user), "password": str(pwd)}
                            return self._xtream_creds
                    # MAC / Stalker js-wrapper shape
                    js = parsed.get("js")
                    if isinstance(js, list) and js:
                        js = js[0]
                    if isinstance(js, dict):
                        user = js.get("username") or js.get("login") or js.get("user") or js.get("fname")
                        pwd  = js.get("password") or js.get("pass") or js.get("parent_password")
                        host = js.get("url") or js.get("host") or js.get("server")
                        port = js.get("port")
                        if user and pwd and host:
                            host = str(host).strip().replace("\\/", "/")
                            if port:
                                base = f"http://{host}:{port}"
                            else:
                                try:
                                    p = urlparse(host)
                                    base = f"{p.scheme}://{p.netloc}" if p.netloc else f"http://{host}"
                                except Exception:
                                    base = f"http://{host}"
                            self.log(f"[STALKER] Xtream creds via player_api probe (js): user={user} pass={pwd}")
                            self._xtream_creds = {"type": "xtream", "base": base,
                                                  "username": str(user), "password": str(pwd)}
                            return self._xtream_creds
                # text fallback: brittle split when JSON is unparseable
                if '"username"' in txt and '"password"' in txt:
                    try:
                        u = txt.split('"username"')[1].split(':', 1)[1].split(',')[0].strip().strip('" ')
                        p = txt.split('"password"')[1].split(':', 1)[1].split(',')[0].strip().strip('" ')
                        if u and p:
                            base = normalize_base_url(self._extract_url_from_text(txt) or self.base)
                            self.log(f"[STALKER] Xtream creds via player_api probe (text): user={u} pass={p}")
                            self._xtream_creds = {"type": "xtream", "base": base,
                                                  "username": u, "password": p}
                            return self._xtream_creds
                    except Exception:
                        pass
        except Exception as e:
            self.log(f"[STALKER] parse_xtream_info probe error: {e}")

        # ── Step 0: profile-embedded credentials (cached, zero network cost) ─
        try:
            _profile = self._last_profile_js or {}
            _prof_login = str(_profile.get("login") or "").strip()
            _prof_pass  = str(_profile.get("password") or "").strip()
            if _is_safe_cred(_prof_login) and _is_safe_cred(_prof_pass):
                self.log(f"[STALKER] Xtream creds from profile: user={_prof_login} pass={_prof_pass}")
                self._xtream_creds = {"type": "xtream", "base": self.base,
                                      "username": _prof_login, "password": _prof_pass}
                return self._xtream_creds
        except Exception:
            pass

        # ── Step 2: get_ordered_list → create_link ─────────────────────────
        try:
            _headers = self._headers(include_auth=True)
            url1 = (f"{self.base}/portal.php?type=vod&action=get_ordered_list"
                    f"&category=*&JsHttpRequest=1-xml&p=1&sortby=added")
            payload1 = None
            for _attempt in range(4):
                try:
                    async with self.session.get(url1, headers=_headers,
                                                timeout=aiohttp.ClientTimeout(total=15)) as r1:
                        if r1.status == 429:
                            _wait = 2 ** _attempt
                            self.log(f"[STALKER] get_ordered_list 429 — backing off {_wait}s "
                                     f"(attempt {_attempt+1}/4)")
                            await asyncio.sleep(_wait)
                            continue
                        payload1 = await safe_json(r1)
                    break
                except Exception as e:
                    self.log(f"[STALKER] get_ordered_list error: {e}")
                    break
            items = normalize_js(payload1)
            content_type = "vod"
            raw_cmd = ""
            item_id = None
            for item in items:
                if isinstance(item, dict) and item.get("cmd"):
                    raw_cmd = str(item["cmd"]).replace("\\/", "/").strip()
                    item_id = str(item.get("id") or item.get("vod_id") or "")
                    if raw_cmd:
                        break
            if not raw_cmd:
                self.log("[STALKER] parse_xtream_info: no VOD items — trying live channels")
                content_type = "itv"
                try:
                    url_itv = (f"{self.base}/portal.php?type=itv&action=get_all_channels"
                               f"&force_ch_link_check=0&JsHttpRequest=1-xml")
                    async with self.session.get(url_itv, headers=_headers,
                                                timeout=aiohttp.ClientTimeout(total=15)) as r_itv:
                        payload_itv = await safe_json(r_itv)
                    for item in (normalize_js(payload_itv) or []):
                        if isinstance(item, dict) and item.get("cmd"):
                            raw_cmd = str(item["cmd"]).replace("\\/", "/").strip()
                            item_id = str(item.get("id") or "")
                            if raw_cmd:
                                break
                except Exception as e:
                    self.log(f"[STALKER] parse_xtream_info: live-channel fallback error: {e}")
            if not raw_cmd:
                self.log("[STALKER] parse_xtream_info: no cmd field in VOD or live items")
                return None

            url2 = (f"{self.base}/portal.php?type={content_type}&action=create_link"
                    f"&cmd={quote(raw_cmd)}&series=&forced_storage=&disable_ad=0&download=0&force_ch_link_check=0&JsHttpRequest=1-xml")
            raw2 = None
            for _attempt in range(4):
                try:
                    async with self.session.get(url2, headers=_headers,
                                                timeout=aiohttp.ClientTimeout(total=15)) as r2:
                        if r2.status == 429:
                            _wait = 2 ** _attempt
                            self.log(f"[STALKER] create_link 429 — backing off {_wait}s "
                                     f"(attempt {_attempt+1}/4)")
                            await asyncio.sleep(_wait)
                            continue
                        raw2 = await r2.text()
                    break
                except Exception as e:
                    self.log(f"[STALKER] create_link error: {e}")
                    break
            if not raw2:
                self.log("[STALKER] parse_xtream_info: create_link request failed")
                return None

            self.log(f"[STALKER] create_link raw: {repr(raw2[:200])}")

            # extract link from JSON or raw text
            link = ""
            try:
                parsed = json.loads(raw2)
                js = parsed.get("js")
                if isinstance(js, list) and js:
                    js = js[0]
                if isinstance(js, dict):
                    link = str(js.get("cmd") or js.get("link") or js.get("url") or "").replace("\\/", "/").strip()
                    if not link and js.get("id"):
                        item_id = item_id or str(js.get("id"))
            except Exception:
                if 'cmd":"' in raw2:
                    link = raw2.split('cmd":"', 1)[1].split('"', 1)[0].replace("\\/", "/").strip()
                elif 'cmd":' in raw2:
                    link = raw2.split('cmd":', 1)[1].split('\n', 1)[0].strip().strip('",')

            # If link contains ffmpeg play URL, return play_url typed result
            if link and "ffmpeg" in link.lower():
                if " " in link:
                    link = link.split(" ", 1)[-1].strip()
                play_url = link
                parsed = urlparse(play_url)
                qs = parse_qs(parsed.query)
                play_token = (qs.get("play_token") or qs.get("token") or [""])[0]
                stream = (qs.get("stream") or [""])[0]
                mac_q = (qs.get("mac") or [""])[0]
                base = (f"{parsed.scheme}://{parsed.netloc}"
                        if parsed.scheme and parsed.netloc
                        else normalize_base_url(play_url))
                self.log(f"[STALKER] create_link resolved → {play_url}")
                self._xtream_creds = {"type": "play_url", "base": base, "stream_url": play_url,
                                      "play_token": play_token, "stream": stream, "mac": mac_q}
                return self._xtream_creds

            # Try direct Xtream extraction from resolved link
            if link:
                res = _try_extract_xtream_from_url(link)
                if res:
                    self._xtream_creds = res
                    self.log(f"[STALKER] Xtream creds via create_link: user={res.get('username')} pass={res.get('password')}")
                    return self._xtream_creds
                # link contains a .php endpoint — treat as play_url
                if ".php" in link:
                    parsed = urlparse(link)
                    base = (f"{parsed.scheme}://{parsed.netloc}"
                            if parsed.scheme and parsed.netloc
                            else normalize_base_url(link))
                    qs = parse_qs(parsed.query)
                    play_token = (qs.get("play_token") or qs.get("token") or [""])[0]
                    stream = (qs.get("stream") or [""])[0]
                    mac_q = (qs.get("mac") or [""])[0]
                    self._xtream_creds = {"type": "play_url", "base": base, "stream_url": link,
                                          "play_token": play_token, "stream": stream, "mac": mac_q}
                    return self._xtream_creds

            # ── id-resolve fallback: try alternate endpoints with item id ──
            if item_id:
                candidate_endpoints = [
                    f"{self.base}/portal.php?type=vod&action=get_streams&id={quote(item_id)}&JsHttpRequest=1-xml",
                    f"{self.base}/portal.php?type=vod&action=get_item&item_id={quote(item_id)}&JsHttpRequest=1-xml",
                    f"{self.base}/portal.php?type=stream&action=get_streams&id={quote(item_id)}&JsHttpRequest=1-xml",
                    f"{self.base}/portal.php?type=vod&action=get_ordered_list&JsHttpRequest=1-xml&p=1&id={quote(item_id)}",
                ]
                for ep in candidate_endpoints:
                    try:
                        async with self.session.get(ep, headers=_headers,
                                                    timeout=aiohttp.ClientTimeout(total=15)) as r3:
                            txt = await r3.text()
                    except Exception:
                        continue
                    self.log(f"[STALKER] id-resolve HTTP — checking {ep.split('?')[0]}")
                    cand = self._extract_url_from_text(txt) or txt
                    res = _try_extract_xtream_from_url(cand)
                    if res:
                        self._xtream_creds = res
                        self.log(f"[STALKER] Xtream creds from id-resolve: user={res.get('username')} pass={res.get('password')}")
                        return self._xtream_creds
                    if ".php" in cand:
                        try:
                            p = urlparse(cand)
                            base = (f"{p.scheme}://{p.netloc}"
                                    if p.scheme and p.netloc
                                    else normalize_base_url(cand))
                            qs = parse_qs(p.query)
                            play_token = (qs.get("play_token") or qs.get("token") or [""])[0]
                            stream = (qs.get("stream") or [""])[0]
                            mac_q = (qs.get("mac") or [""])[0]
                            self._xtream_creds = {"type": "play_url", "base": base, "stream_url": cand,
                                                  "play_token": play_token, "stream": stream, "mac": mac_q}
                            self.log(f"[STALKER] Xtream play_url from id-resolve: {cand}")
                            return self._xtream_creds
                        except Exception:
                            pass

            # ── legacy text-parse fallback ────────────────────────────────
            try:
                txt_try = link or raw2
                if txt_try and 'username=' in txt_try and 'password=' in txt_try:
                    parsed_q = urlparse(txt_try)
                    qs = parse_qs(parsed_q.query)
                    u = (qs.get("username") or qs.get("user") or [""])[0]
                    p = (qs.get("password") or qs.get("pass") or [""])[0]
                    if u and p:
                        base = (f"{parsed_q.scheme}://{parsed_q.netloc}"
                                if parsed_q.scheme and parsed_q.netloc
                                else normalize_base_url(txt_try))
                        self.log(f"[STALKER] Xtream creds via legacy query parse: user={u} pass={p}")
                        self._xtream_creds = {"type": "xtream", "base": base, "username": u, "password": p}
                        return self._xtream_creds
                if raw2 and 'username":"' in raw2 and 'password":"' in raw2:
                    try:
                        u = raw2.split('username":"', 1)[1].split('"', 1)[0]
                        p = raw2.split('password":"', 1)[1].split('"', 1)[0]
                        if u and p:
                            base = normalize_base_url(self._extract_url_from_text(raw2) or self.base)
                            self.log(f"[STALKER] Xtream creds via legacy split parse: user={u} pass={p}")
                            self._xtream_creds = {"type": "xtream", "base": base, "username": u, "password": p}
                            return self._xtream_creds
                    except Exception:
                        pass
            except Exception:
                pass

            self.log("[STALKER] parse_xtream_info: create_link did not yield usable Xtream creds")
            return None

        except Exception as e:
            self.log(f"[STALKER] parse_xtream_info error: {e}")
            return None


    async def get_all_vod_streams(self) -> list:
        """Fetch ALL VOD streams via player_api.php?action=get_vod_streams.

        Stalker portals running on Xtream Codes expose player_api.php.
        Credentials are extracted from a VOD item cmd URL via
        parse_xtream_info().  Items normalised to MAC portal format.
        Returns [] on failure so api_items() falls back to per-category
        pagination.  Result cached in _all_vod_raw."""
        if self._all_vod_raw is not None:
            return self._all_vod_raw
        self._all_vod_raw = []
        creds = await self.parse_xtream_info()
        if not creds or creds.get("type") != "xtream":
            _ct = creds.get("type") if creds else None
            _cu = creds.get("username", "") if creds else ""
            _cp = creds.get("password", "") if creds else ""
            self.log(f"[MAC] get_vod_streams: creds type={_ct!r} user={_cu!r} pass={_cp!r} → skipping")
            return self._all_vod_raw
        base = creds["base"]; user = creds["username"]; pas = creds["password"]
        headers = self._headers(include_auth=True)
        # Derive the stalker portal prefix from LOAD_PHP so we can try both
        # /player_api.php (standard Xtream root) and /stalker_portal/player_api.php
        # (Xtream API colocated with the Stalker panel).
        _lp_parts = [p for p in self.LOAD_PHP.strip("/").split("/") if p]
        _stalker_pfx = ("/" + "/".join(_lp_parts[:-2])) if len(_lp_parts) > 2 else ""
        _api_urls = [f"{base}/player_api.php"]
        if _stalker_pfx:
            _api_urls.append(f"{base}{_stalker_pfx}/player_api.php")
        try:
            data = None
            for _api_base in _api_urls:
                url = f"{_api_base}?username={user}&password={pas}&action=get_vod_streams"
                self.log(f"[STALKER] get_vod_streams: fetching all VOD streams… ({_api_base})")
                async with self.session.get(url, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=60)) as r:
                    self.log(f"[STALKER] get_vod_streams HTTP {r.status}")
                    if r.status >= 400:
                        self.log(f"[STALKER] get_vod_streams: HTTP {r.status} — trying next URL variant")
                        continue
                    data = await r.json(content_type=None)
                break
            if data is None:
                self.log("[STALKER] get_vod_streams: all URL variants exhausted — portal does not support Xtream VOD API")
                return self._all_vod_raw
            if isinstance(data, list):
                out = []
                for it in data:
                    if not isinstance(it, dict): continue
                    sid = str(it.get("stream_id", "")).strip()
                    if not sid: continue
                    ext = it.get("container_extension") or "mp4"
                    out.append({
                        "id": sid, "name": it.get("name", ""),
                        "logo": it.get("stream_icon", ""),
                        "screenshot_uri": it.get("stream_icon", ""),
                        "cmd": f"{base}/movie/{user}/{pas}/{sid}.{ext}",
                        "category_id": str(it.get("category_id", "")),
                        "rating": str(it.get("rating", "")),
                        "added": str(it.get("added", "")),
                    })
                self._all_vod_raw = out
                self.log(f"[STALKER] get_vod_streams: {len(out)} items")
            else:
                self.log(f"[STALKER] get_vod_streams: unexpected response type {type(data).__name__}")
        except Exception as e:
            self.log(f"[STALKER] get_vod_streams error: {e}")
        return self._all_vod_raw

    async def get_all_series_streams(self) -> list:
        """Fetch ALL series via player_api.php?action=get_series.

        Same credential extraction approach as get_all_vod_streams.
        Items normalised to MAC portal format with _is_show_item=True.
        Returns [] on failure so api_items() falls back to per-category
        pagination.  Result cached in _all_series_raw."""
        if self._all_series_raw is not None:
            return self._all_series_raw
        self._all_series_raw = []
        creds = await self.parse_xtream_info()
        if not creds or creds.get("type") != "xtream":
            _ct = creds.get("type") if creds else None
            _cu = creds.get("username", "") if creds else ""
            _cp = creds.get("password", "") if creds else ""
            self.log(f"[MAC] get_series: creds type={_ct!r} user={_cu!r} pass={_cp!r} → skipping")
            return self._all_series_raw
        base = creds["base"]; user = creds["username"]; pas = creds["password"]
        headers = self._headers(include_auth=True)
        _lp_parts = [p for p in self.LOAD_PHP.strip("/").split("/") if p]
        _stalker_pfx = ("/" + "/".join(_lp_parts[:-2])) if len(_lp_parts) > 2 else ""
        _api_urls = [f"{base}/player_api.php"]
        if _stalker_pfx:
            _api_urls.append(f"{base}{_stalker_pfx}/player_api.php")
        try:
            data = None
            for _api_base in _api_urls:
                url = f"{_api_base}?username={user}&password={pas}&action=get_series"
                self.log(f"[STALKER] get_series: fetching all series… ({_api_base})")
                async with self.session.get(url, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=60)) as r:
                    self.log(f"[STALKER] get_series HTTP {r.status}")
                    if r.status >= 400:
                        self.log(f"[STALKER] get_series: HTTP {r.status} — trying next URL variant")
                        continue
                    data = await r.json(content_type=None)
                break
            if data is None:
                self.log("[STALKER] get_series: all URL variants exhausted — portal does not support Xtream series API")
                return self._all_series_raw
            if isinstance(data, list):
                out = []
                for it in data:
                    if not isinstance(it, dict): continue
                    sid = str(it.get("series_id", "")).strip()
                    if not sid: continue
                    out.append({
                        "id": sid, "name": it.get("name", ""),
                        "logo": it.get("cover", ""),
                        "screenshot_uri": it.get("cover", ""),
                        "category_id": str(it.get("category_id", "")),
                        "rating": str(it.get("rating", "")),
                        "plot": it.get("plot", ""),
                        "_is_show_item": True,
                        "series_id": sid,
                    })
                self._all_series_raw = out
                self.log(f"[STALKER] get_series: {len(out)} items")
            else:
                self.log(f"[STALKER] get_series: unexpected response type {type(data).__name__}")
        except Exception as e:
            self.log(f"[STALKER] get_series error: {e}")
        return self._all_series_raw

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
        # Fast path: serve live category items from the get_all_channels pool.
        # Avoids redundant per-category HTTP calls when the pool is already cached.
        # For page > 1: only short-circuit if this cat_id has matches in the pool —
        # meaning page 1 was already served from the pool in one shot. If page 1
        # fell through to HTTP (pool had no matches for this cat_id), page 2+ must
        # also use HTTP to avoid truncating the paginated results.
        if mode == "live" and cat_id not in ("*", "__all__"):
            raw_all = getattr(self, "_all_channels_raw", None)
            if raw_all:
                if page == 1:
                    filtered = [ch for ch in raw_all
                                if isinstance(ch, dict)
                                and str(ch.get("tv_genre_id", "")) == str(cat_id)]
                    if filtered:
                        self.log(f"[STALKER] {mode.upper()} cat={cat_id}: {len(filtered)} items (pool)")
                        return filtered
                    # Pool populated but no match for this cat_id — fall through to HTTP
                else:
                    # page > 1: return [] only if pool has channels for this category
                    # (confirming page 1 used the pool and returned everything at once)
                    if any(isinstance(ch, dict) and str(ch.get("tv_genre_id", "")) == str(cat_id)
                           for ch in raw_all):
                        return []
                    # No pool matches → page 1 used HTTP, continue paginating via HTTP

        # Fast path: serve VOD category items from the get_all_vod_streams cache.
        # When parse_xtream_info succeeded and get_all_vod_streams populated
        # _all_vod_raw, every VOD category can be served from RAM without extra
        # HTTP calls — same principle as the live channel pool above.
        if mode == "vod":
            raw_all = getattr(self, "_all_vod_raw", None)
            if raw_all is not None:
                if cat_id in ("*", "__all__"):
                    if page == 1:
                        self.log(f"[STALKER] {mode.upper()} cat={cat_id}: {len(raw_all)} items (all_vod cache)")
                        return list(raw_all)
                    return []
                elif page == 1:
                    filtered = [it for it in raw_all
                                if isinstance(it, dict)
                                and str(it.get("category_id", "")) == str(cat_id)]
                    if filtered:
                        self.log(f"[STALKER] {mode.upper()} cat={cat_id}: {len(filtered)} items (all_vod cache)")
                        return filtered
                    # Cache populated but no match for this cat_id — fall through to HTTP
                else:
                    # page > 1: return [] only if cache has items for this category
                    if any(isinstance(it, dict) and str(it.get("category_id", "")) == str(cat_id)
                           for it in raw_all):
                        return []
                    # No cache matches → page 1 used HTTP, continue paginating via HTTP

        # Fast path: serve Series category items from the get_all_series_streams cache.
        # Same logic as VOD above — when the Xtream API series pool is cached,
        # category browsing becomes a pure RAM filter with zero network calls.
        if mode == "series":
            raw_all = getattr(self, "_all_series_raw", None)
            if raw_all is not None:
                if cat_id in ("*", "__all__"):
                    if page == 1:
                        self.log(f"[STALKER] {mode.upper()} cat={cat_id}: {len(raw_all)} items (all_series cache)")
                        return list(raw_all)
                    return []
                elif page == 1:
                    filtered = [it for it in raw_all
                                if isinstance(it, dict)
                                and str(it.get("category_id", "")) == str(cat_id)]
                    if filtered:
                        self.log(f"[STALKER] {mode.upper()} cat={cat_id}: {len(filtered)} items (all_series cache)")
                        return filtered
                    # Cache populated but no match for this cat_id — fall through to HTTP
                else:
                    # page > 1: return [] only if cache has items for this category
                    if any(isinstance(it, dict) and str(it.get("category_id", "")) == str(cat_id)
                           for it in raw_all):
                        return []
                    # No cache matches → page 1 used HTTP, continue paginating via HTTP

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
        # Large VOD/Series categories can be slow; give them more time than live.
        _req_timeout = aiohttp.ClientTimeout(total=120 if mode in ("vod", "series") else 60)
        async with self.session.get(url, headers=headers, timeout=_req_timeout) as r:
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
            async with self.session.get(alt_url, headers=headers, timeout=_req_timeout) as r2:
                self.log(f"[STALKER] Items (alt) HTTP {r2.status} ({mode.upper()} cat={cat_id})")
                payload = await self._read_json(r2, f"Items alt ({mode.upper()} cat={cat_id})")
            items = normalize_js(payload)
        # Last-resort fallback for live categories: filter _all_channels_raw by tv_genre_id.
        # Triggered when get_ordered_list returns "Access denied" or similar block — portal
        # allows get_all_channels but not per-category listing (seen on 4k1.new4k.cc).
        # Pool was also not ready at the top of this function (fast-path was skipped),
        # so wait for the prefetch event now before filtering.
        if not items and page == 1 and mode == "live" and cat_id not in ("*", "__all__"):
            raw_all = getattr(self, "_all_channels_raw", None)
            # If _all_channels_raw is None the background prefetch is still in-flight.
            # Wait on the ready-event (already injected by _make_client) rather than
            # returning 0 items immediately and poisoning the cache.
            if raw_all is None:
                _evt = getattr(self, "_all_channels_ready_event", None)
                if _evt is not None:
                    self.log(f"[STALKER] Items fallback waiting for prefetch (cat={cat_id})…")
                    _evt.wait(timeout=25)
                # The prefetch ran on a DIFFERENT client instance (its own _make_client
                # context), so self._all_channels_raw is still None even after the event
                # fires.  Seed it from the shared items-cache that _make_client injected
                # as self._shared_items_cache, then fall back to an empty list.
                if self._all_channels_raw is None:
                    _shared = getattr(self, "_shared_items_cache", None)
                    if _shared is not None:
                        _pool = _shared.get(("live", "__all__"))
                        if _pool:
                            self._all_channels_raw = _pool
                raw_all = self._all_channels_raw or []
            if raw_all:
                filtered = [ch for ch in raw_all
                            if isinstance(ch, dict) and str(ch.get("tv_genre_id", "")) == str(cat_id)]
                if filtered:
                    self.log(f"[STALKER] Items fallback: {len(filtered)} channels from pool for cat={cat_id}")
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
        # Enrich live items with tv_archive from get_all_channels pool (authoritative source).
        # get_ordered_list may omit archive flags on some portal versions.
        # Copy tv_archive_duration too — the JS check requires both flags.
        if mode == "live" and items:
            raw_all = getattr(self, "_all_channels_raw", None)
            if raw_all:
                _amap = {str(ch.get("id", "")): ch for ch in raw_all
                         if isinstance(ch, dict) and ch.get("id")}
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    src = _amap.get(str(it.get("id", "")).strip())
                    if src:
                        it.setdefault("tv_archive", src.get("tv_archive", 0))
                        it.setdefault("tv_archive_duration", src.get("tv_archive_duration", 0))
            # Fallback: normalize enable_tv_archive directly if pool not yet ready
            for it in items:
                if isinstance(it, dict) and "tv_archive" not in it:
                    it["tv_archive"] = 1 if it.get("enable_tv_archive", 0) else 0
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
    def __init__(self, base_url: str, username: str, password: str, log_cb,
                 custom_ua: str = ""):
        self.base = normalize_base_url(base_url)
        self.username = username.strip()
        self.password = password.strip()
        self.log = log_cb
        self.session = None
        # UA spoofing — empty = auto-default (TiviMate), "custom" preset handled
        # by FlaskyIPTV which passes the resolved string directly.
        self.custom_ua: str = (custom_ua or "").strip()
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
        # Full VOD / Series lists — same None/list semantics as _all_channels_raw.
        # Xtream's get_vod_streams / get_series with empty category_id returns
        # every item in one call — exactly analogous to get_live_streams for live.
        # Pre-seeded from state._items_cache by _make_client after prefetch.
        self._all_vod_raw: list | None = None
        self._all_series_raw: list | None = None

    async def __aenter__(self):
        _timeout = aiohttp.ClientTimeout(total=30, connect=10)
        self.session = aiohttp.ClientSession(timeout=_timeout)
        # Apply the full UA profile to all session requests so Xtream servers
        # see the correct Accept, Accept-Language, X-Requested-With etc.
        # Skip keys that are STB-specific (meaningless to Xtream), connection-level
        # (managed by aiohttp), or internal profile metadata (not HTTP headers).
        _XTREAM_SKIP = frozenset({
            "stb_type", "image_version", "X-User-Agent",  # STB/Stalker-specific
            "Connection",                                   # managed by aiohttp
            "Upgrade-Insecure-Requests",                   # browser-only
        })
        _ua, _profile = get_effective_ua(
            "custom" if self.custom_ua else "",
            self.custom_ua,
            "xtream",
        )
        session_headers = {k: v for k, v in _profile.items()
                          if k not in _XTREAM_SKIP}
        session_headers["User-Agent"] = _ua
        self.session.headers.update(session_headers)
        return self

    async def __aexit__(self, *args):
        if not getattr(self, "_externally_managed", False):
            if self.session:
                await self.session.close()

    def _api(self, action: str, **params) -> str:
        url = f"{self.base}/player_api.php?username={self.username}&password={self.password}&action={action}"
        for k, v in params.items():
            url += f"&{k}={v}"
        return url

    async def _read_json(self, r: aiohttp.ClientResponse, tag: str = "") -> "dict | list | None":
        """Read response text, log a raw preview, then parse and return JSON.

        Mirrors StalkerPortalClient._read_json / PortalClient._read_json so the
        Activity Log shows consistent raw-response lines across all portal types.
        tag  — label used in the log line, e.g. "auth", "account_info".
              Pass "" to skip logging (silent parse-only path).
        """
        try:
            text = await r.text()
        except Exception as e:
            if tag:
                self.log(f"[XTREAM] {tag} read error: {e}")
            return None
        if tag:
            preview = repr(text[:800]) if text else "''"
            self.log(f"[XTREAM] {tag} raw: {preview}")
        if not text or not text.strip():
            return None
        t = text.lstrip()
        if not (t.startswith("{") or t.startswith("[")):
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    async def handshake(self):
        url = f"{self.base}/player_api.php?username={self.username}&password={self.password}"
        self.log(f"[XTREAM] Connecting → {self.base}")
        async with self.session.get(url) as r:
            self.log(f"[XTREAM] Auth HTTP {r.status}")
            data = await self._read_json(r, "handshake")
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
        self._server_timezone: str = ""   # IANA tz name from server_info, e.g. "Europe/London"
        try:
            import calendar as _cal
            from datetime import datetime as _dt2
            srv = data.get("server_info", {})
            if isinstance(srv, dict):
                ts_now = srv.get("timestamp_now") or srv.get("time")
                t_str  = srv.get("time_now") or srv.get("server_time") or ""
                tz_nm  = str(srv.get("timezone") or "").strip()
                if tz_nm:
                    self._server_timezone = tz_nm
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
                        self.log(f"[XTREAM] ⚠ Timezone {tz_nm!r} — zoneinfo unavailable, using UTC")
        except Exception as _tz_e:
            self.log(f"[XTREAM] ⚠ Timezone detection unavailable ({_tz_e}), using UTC")
        self.log(f"[XTREAM] Auth OK — status: {info.get('status','?')}  expiry: {info.get('exp_date','?')}")
        return info

    async def account_info(self):
        # Re-use the user_info already fetched by handshake() when available.
        # This eliminates the duplicate GET /player_api.php that previously
        # happened whenever handshake() and account_info() were called in sequence.
        if self._cached_user_info is not None:
            info = self._cached_user_info
            preview = repr(str(info)[:600]) if info else "''"
            self.log(f"[XTREAM] account_info raw (cached from handshake): {preview}")
        else:
            url = f"{self.base}/player_api.php?username={self.username}&password={self.password}"
            self.log("[XTREAM] Fetching account info (no cached user_info)…")
            async with self.session.get(url) as r:
                self.log(f"[XTREAM] Account info HTTP {r.status}")
                data = await self._read_json(r, "account_info")
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
        # Index 5 (status) is additive — existing callers only read ai[0..2].
        return (self.username, exp, max_conn_int, password, str(active), status)

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
        """Fetch ALL live channels in one call via get_live_streams (no category filter).

        Delegates VOD/Series to their dedicated methods so the correct Xtream API
        action and cache are used for each mode.

        For live: calls get_live_streams with empty category_id, caches in
        _all_channels_raw.  _make_client pre-seeds this from state._items_cache
        so a connect-time prefetch means the first call is already a cache hit."""
        if mode == "vod":
            return await self.get_all_vod_streams()
        if mode == "series":
            return await self.get_all_series_streams()
        # mode == "live"
        if self._all_channels_raw is not None:
            return self._all_channels_raw
        result = await self.fetch_items_page("live", "", 1)
        self._all_channels_raw = result
        return result

    async def get_all_vod_streams(self) -> list:
        """Fetch ALL VOD streams in one call via get_vod_streams (no category filter).

        Xtream's get_vod_streams with empty category_id returns every VOD item on
        the server without pagination — analogous to get_live_streams for live.

        Result is cached in _all_vod_raw for the session lifetime so repeated
        calls (browsing 'All VOD' multiple times) are free list returns.
        _make_client pre-seeds this from state._items_cache after prefetch.
        Returns [] on failure — caller should fall back to per-category pagination."""
        if self._all_vod_raw is not None:
            return self._all_vod_raw
        self._all_vod_raw = []
        try:
            self.log("[XTREAM] get_vod_streams: fetching all VOD streams…")
            result = await self.fetch_items_page("vod", "", 1)
            self._all_vod_raw = result
            self.log(f"[XTREAM] get_vod_streams: {len(result)} items")
        except Exception as e:
            self.log(f"[XTREAM] get_vod_streams error: {e}")
        return self._all_vod_raw

    async def get_all_series_streams(self) -> list:
        """Fetch ALL series in one call via get_series (no category filter).

        Xtream's get_series with empty category_id returns every series on the
        server without pagination — analogous to get_live_streams for live.

        Result is cached in _all_series_raw for the session lifetime.
        _make_client pre-seeds this from state._items_cache after prefetch.
        Returns [] on failure — caller should fall back to per-category pagination."""
        if self._all_series_raw is not None:
            return self._all_series_raw
        self._all_series_raw = []
        try:
            self.log("[XTREAM] get_series: fetching all series…")
            result = await self.fetch_items_page("series", "", 1)
            self._all_series_raw = result
            self.log(f"[XTREAM] get_series: {len(result)} items")
        except Exception as e:
            self.log(f"[XTREAM] get_series error: {e}")
        return self._all_series_raw

    def _stream_url(self, mode: str, item: dict) -> str:
        if mode == "live":
            sid = item.get("stream_id", "")
            ext = item.get("container_extension", "m3u8")
            return f"{self.base}/live/{self.username}/{self.password}/{sid}.{ext}"
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
    def __init__(self, m3u_url: str, log_cb, preloaded=None, custom_ua: str = "",
                 timeout_total: float = 300, timeout_connect: float = 20,
                 xtream_timeout_total: float = 30, xtream_timeout_connect: float = 10):
        self.m3u_url = m3u_url.strip()
        self.log = log_cb
        self.session = None
        self._all_groups = preloaded or {}
        self._xtream_creds = extract_xtream_from_m3u_url(m3u_url)
        self._xtream_client = None
        self._tvg_url = ""
        # UA spoofing — empty = auto-default (VLC), or resolved custom string.
        self.custom_ua: str = (custom_ua or "").strip()
        # Timeout budgets — defaults match the historical hardcoded values.
        # A shorter pair is passed in by the connect-failover path when this
        # client is being tried as a *backup* URL, so a dead/hanging mirror
        # can't stall the whole failover sequence for minutes at a time.
        self._timeout_total = timeout_total
        self._timeout_connect = timeout_connect
        self._xtream_timeout_total = xtream_timeout_total
        self._xtream_timeout_connect = xtream_timeout_connect

    async def __aenter__(self):
        _timeout = aiohttp.ClientTimeout(total=self._timeout_total, connect=self._timeout_connect, sock_read=None)
        connector = aiohttp.TCPConnector(ssl=False)
        self.session = aiohttp.ClientSession(timeout=_timeout, connector=connector)
        if self._xtream_creds:
            creds = self._xtream_creds
            self._xtream_client = XtreamClient(creds["base"], creds["username"], creds["password"], self.log)
            self._xtream_client.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._xtream_timeout_total, connect=self._xtream_timeout_connect))
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

        # Apply the full UA profile to the M3U fetch so servers see the correct
        # Accept, Accept-Language etc. for the chosen preset.
        # Skip keys that are STB-specific, connection-level, or browser-only.
        _M3U_SKIP = frozenset({
            "stb_type", "image_version", "X-User-Agent",  # STB/Stalker-specific
            "Connection",                                   # managed by aiohttp session
            "Upgrade-Insecure-Requests",                   # browser-only
            "X-Requested-With",                            # app-internal, not needed for plain fetch
        })
        _ua, _profile = get_effective_ua(
            "custom" if self.custom_ua else "",
            self.custom_ua,
            "m3u_url",
        )
        headers = {k: v for k, v in _profile.items() if k not in _M3U_SKIP}
        headers["User-Agent"] = _ua
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
                self.log(f"[M3U] account_info delegated to Xtream client: user={result[0]}  expiry={result[1]}")
                return result
            except Exception as e:
                self.log(f"[M3U] account_info Xtream delegate failed: {e} — falling back to static")
        self.log("[M3U] account_info: no Xtream credentials detected — returning static profile (no server data)")
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
        """Return all live channels without category filtering.

        Delegates VOD/Series to their dedicated methods (get_all_vod_streams /
        get_all_series_streams) so each mode uses the correct Xtream API action
        and cache.  Tries the wrapped Xtream client first, then flattens all
        groups from the preloaded M3U data for live mode."""
        if mode == "vod":
            return await self.get_all_vod_streams()
        if mode == "series":
            return await self.get_all_series_streams()
        # mode == "live"
        if self._xtream_client:
            try:
                result = await self._xtream_client.get_all_channels("live")
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

    async def get_all_vod_streams(self) -> list:
        """Return all VOD items without category filtering.

        Tries the wrapped Xtream client's get_all_vod_streams first
        (get_vod_streams with empty category_id — single call, all items).
        Falls back to flattening preloaded M3U groups filtered by VOD type."""
        if self._xtream_client:
            try:
                result = await self._xtream_client.get_all_vod_streams()
                if result:
                    return result
            except Exception:
                pass
        type_filter = self._type_filter("vod")
        out = []
        for items in self._all_groups.values():
            for it in items:
                if it.get("tvg_type", "") in type_filter:
                    out.append(it)
        return out

    async def get_all_series_streams(self) -> list:
        """Return all series items without category filtering.

        Tries the wrapped Xtream client's get_all_series_streams first
        (get_series with empty category_id — single call, all items).
        Falls back to flattening preloaded M3U groups filtered by series type."""
        if self._xtream_client:
            try:
                result = await self._xtream_client.get_all_series_streams()
                if result:
                    return result
            except Exception:
                pass
        type_filter = self._type_filter("series")
        out = []
        for items in self._all_groups.values():
            for it in items:
                if it.get("tvg_type", "") in type_filter:
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


# ===================== PORTAL SESSION MANAGER =====================

class PortalSessionManager:
    """Persistent aiohttp session manager for MAC/Stalker/Xtream portals.

    Owns a single asyncio event loop running forever in a daemon thread.
    All portal I/O dispatches to this loop via ``submit()`` so that:

    * The aiohttp ClientSession (and its TCPConnector keepalive pool) remains
      open between requests — no reconnection overhead between operations.
    * ``handshake()`` is called at most ONCE per portal connection (or zero
      times if a valid token is found in the portal's session cache).
    * Concurrent 401/403 responses trigger only ONE re-handshake, protected
      by an asyncio.Lock created inside the persistent loop.

    Usage (called from FlaskyIPTV_Player_byGG.py)::

        state.portal_mgr = PortalSessionManager()
        result = state.portal_mgr.connect_sync(
            conn_type, url, mac, username, password,
            portal_key, log_cb, **client_kwargs,
        )
        # All subsequent async with _make_client() as client: calls
        # dispatch to portal_mgr.loop transparently.

    Thread-safety: ``submit()`` is the only public entry point for coroutines;
    ``connect_sync()`` / ``disconnect()`` / ``stop()`` are synchronous and
    safe to call from any thread.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._session: Optional[aiohttp.ClientSession] = None
        self._client = None          # PortalClient | StalkerPortalClient | XtreamClient
        self._auth_lock: Optional[asyncio.Lock] = None  # created inside _loop
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="portal-io-loop"
        )
        self._thread.start()
        # Create asyncio.Lock in the persistent loop (it is loop-bound in ≤3.9).
        asyncio.run_coroutine_threadsafe(
            self._init_internals(), self._loop
        ).result(timeout=5)

    # ── loop thread ──────────────────────────────────────────────────────────
    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _init_internals(self) -> None:
        self._auth_lock = asyncio.Lock()

    # ── properties ───────────────────────────────────────────────────────────
    @property
    def client(self):
        return self._client

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    # ── public API ───────────────────────────────────────────────────────────
    def submit(self, coro, timeout: float = 300):
        """Dispatch *coro* to the persistent loop and block until it completes.

        Exceptions from the coroutine are re-raised in the calling thread.
        Raises ``TimeoutError`` if *timeout* seconds elapse.
        """
        if self._loop.is_closed():
            raise RuntimeError("PortalSessionManager loop is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"Portal operation timed out after {timeout}s")

    def connect_sync(
        self,
        conn_type: str,
        url: str,
        mac: str,
        username: str,
        password: str,
        portal_key: str,
        log_cb,
        *,
        ua_preset: str = "",
        custom_ua: str = "",
        stalker_sn: str = "",
        stalker_device_id: str = "",
        stalker_device_id2: str = "",
        stalker_signature: str = "",
        is_stalker: bool = False,
        connect_epoch: int = 0,
        get_epoch_fn=None,
        submit_timeout: float = 90,
    ) -> dict:
        """Create session + client, handshake once (or reuse cached token).

        Runs entirely in the persistent loop.  Returns the same dict shape
        that ``_connect_async()`` currently returns from its MAC/Xtream block.

        ``get_epoch_fn`` is a zero-arg callable that returns the current
        ``state._connect_epoch`` — used to abort if a newer connect fired.
        """
        async def _do() -> dict:
            # ── build client ─────────────────────────────────────────────
            # Construct the client first so __aenter__ can initialise its
            # session with the correct UA profile, Accept headers, and
            # (for PortalClient) the MAC cookie jar entry that portal.php
            # uses to associate the TCP connection with this device.
            if conn_type == "xtream":
                client = XtreamClient(url, username, password, log_cb,
                                      custom_ua=custom_ua)
            elif is_stalker:
                client = StalkerPortalClient(
                    url, mac, log_cb,
                    custom_sn=stalker_sn,
                    custom_device_id=stalker_device_id,
                    custom_device_id2=stalker_device_id2,
                    custom_signature=stalker_signature,
                    ua_preset=ua_preset,
                    custom_ua=custom_ua,
                )
            else:
                client = PortalClient(url, mac, log_cb,
                                      ua_preset=ua_preset,
                                      custom_ua=custom_ua)

            # ── set up persistent session via __aenter__ ──────────────────
            # Calling __aenter__ lets each client class initialise its own
            # aiohttp.ClientSession with the correct headers, cookies, and
            # timeout — identical to the non-persistent path.  We then replace
            # the connector with a keep-alive one and mark the session as
            # externally managed so __aexit__ does not close it.
            #
            # For XtreamClient: resolve the effective UA from preset + custom_ua
            # before entering __aenter__, which uses self.custom_ua to apply
            # session-level headers.  Old _make_client() did this via
            # _resolve_custom_ua(); we must replicate it here so that a user-
            # selected UA preset (e.g. "MAG254") is correctly applied to Xtream.
            if conn_type == "xtream" and ua_preset and not custom_ua:
                _resolved_ua, _ = get_effective_ua(ua_preset, "", "xtream")
                client.custom_ua = _resolved_ua  # inject resolved UA into instance

            await client.__aenter__()

            # Upgrade the connector to keep-alive (15 s → 30 s idle timeout,
            # max 10 concurrent connections, clean up half-closed sockets).
            # We can't swap the connector on an existing session, so we close
            # the just-created session and open a new one copying its headers
            # and cookies but using our persistent TCPConnector.
            import aiohttp as _aio
            _old_hdrs    = dict(client.session.headers)
            _old_cookies = {c.key: c.value
                            for c in client.session.cookie_jar}

            # For Stalker: seed the MAC address into the session cookie jar,
            # mirroring PortalClient's cookies={"mac": self.mac} approach.
            # stb_lang and timezone are already carried per-request via _cookie_str()
            # and are redundant in the jar.  Only the MAC address is seeded here so
            # the portal can associate the persistent TCP connection with this device
            # even on requests where the Cookie header is absent or minimal.
            if is_stalker:
                _old_cookies.setdefault("mac", mac)

            await client.session.close()

            _connector = _aio.TCPConnector(
                limit=10,
                keepalive_timeout=30,
                enable_cleanup_closed=True,
            )
            # Use each client type's original timeout to avoid regressions:
            #   StalkerPortalClient.__aenter__: total=60, connect=10
            #   XtreamClient.__aenter__:        total=30, connect=10
            #   PortalClient.__aenter__:         total=30, connect=10
            if is_stalker:
                _tot, _conn_t = 60, 10
            elif conn_type == "xtream":
                _tot, _conn_t = 30, 10
            else:
                _tot, _conn_t = 30, 10
            client.session = _aio.ClientSession(
                connector=_connector,
                timeout=_aio.ClientTimeout(total=_tot, connect=_conn_t),
                cookies=_old_cookies,
            )
            client.session.headers.update(_old_hdrs)

            # Hand session ownership to the manager and mark externally managed.
            self._session = client.session
            client._externally_managed = True
            self._client = client

            # ── authentication: always fresh handshake ───────────────────
            # All portal types perform a fresh handshake on every connect.
            # Token caching across app restarts (SessionStore) is not used:
            #   Xtream:  stateless URL credentials — no session token exists.
            #   Stalker: server-side session state (STB variant, active session
            #            binding) is initialised during handshake(); injecting a
            #            cached token bypasses that and causes account_info() to
            #            fail with "Authorization failed. 75".
            #   MAC:     same principle — always a clean session start on reconnect.
            # Within a single running session the token lives on client.token /
            # client.bearer_token and is reused for all requests via the persistent
            # aiohttp session — no per-request re-handshake.
            await client.handshake()

            # Epoch guard post-handshake
            if get_epoch_fn and get_epoch_fn() != connect_epoch:
                return {"success": False, "error": "superseded"}

            # ── account info ─────────────────────────────────────────────
            ai = await client.account_info()
            ident, exp = ai[0], ai[1]
            max_conn = ai[2] if len(ai) > 2 else 0
            # ai[5] (raw status string) is only present for XtreamClient today —
            # MAC/Stalker have no equivalent field on their panels. exp_epoch is
            # a best-effort parse of the *display* expiry string, computed the
            # same way regardless of portal type.
            account_status_raw = ai[5] if len(ai) > 5 else None
            exp_epoch = parse_expiry_to_epoch(exp)
            # Epoch guard: bail immediately if a newer api_connect() arrived while we
            # were suspended in account_info().  Do this before any state writes so
            # the new portal's clean state is never overwritten by this stale result.
            if get_epoch_fn and get_epoch_fn() != connect_epoch:
                log_cb("[CONNECT] Superseded during account_info — discarding result.")
                return {"success": False, "error": "superseded"}

            # ── profile (Stalker / plain MAC) ─────────────────────────────
            # For Stalker: also pull get_profile for richer display data
            profile_data: dict = {}
            if is_stalker and hasattr(client, "get_profile"):
                try:
                    prof = await client.get_profile()
                    login = prof.get("login") or prof.get("fname") or prof.get("username") or ""
                    if login:
                        ident = login
                    # Use tariff_expired_date from profile only if account_info didn't get a real expiry.
                    # expire_billing_date is a billing timestamp, NOT subscription end — reference code
                    # only uses it as absolute last resort when phone/end_date are also empty.
                    exp_label = "expiry"
                    if exp == "unknown":
                        exp_prof = (prof.get("tariff_expired_date") or prof.get("end_date")
                                    or prof.get("phone") or "")
                        if exp_prof and exp_prof != "unknown":
                            exp = str(exp_prof)
                        elif prof.get("expire_billing_date"):
                            exp = str(prof.get("expire_billing_date"))
                            exp_label = "last_billing"
                    if not max_conn:
                        try:
                            raw = (prof.get("max_online") or prof.get("playback_limit") or
                                   prof.get("max_connections") or prof.get("con_per_device") or
                                   prof.get("connections_limit") or 0)
                            if not raw:
                                storages = prof.get("storages")
                                if isinstance(storages, dict):
                                    for store in storages.values():
                                        if isinstance(store, dict) and store.get("max_online"):
                                            raw = store["max_online"]
                                            break
                            max_conn = int(raw) if raw else 0
                        except Exception:
                            pass
                    _storage_ips = []
                    _storages_raw = prof.get("storages")
                    if isinstance(_storages_raw, dict):
                        for _sname, _st in _storages_raw.items():
                            if isinstance(_st, dict) and _st.get("storage_ip"):
                                _storage_ips.append(str(_st["storage_ip"]))
                    profile_data = {
                        "type": "stalker", "mac": client.mac, "login": login or ident,
                        "password": str(prof.get("password", "") or ""), "exp": exp,
                        "exp_label": exp_label, "status": prof.get("status", ""),
                        "max_conn": str(max_conn) if max_conn else "–",
                        "active_cons": str(prof.get("active_cons") or prof.get("online_streams") or ""),
                        "settings_password": str(prof.get("settings_password", "") or ""),
                        "adult_password": str(prof.get("parent_password", "") or prof.get("adult_password", "") or ""),
                        "portal_url": url, "timezone": str(prof.get("default_timezone") or
                                                           prof.get("timezone") or prof.get("time_zone") or ""),
                        "storage_ips": _storage_ips,
                        "client_ip": str(prof.get("ip") or ""),
                        "comment": str(prof.get("comment") or ""),
                    }
                except Exception as e:
                    log_cb(f"[CONNECT] ✗ Could not fetch Stalker profile: {e}")
                    profile_data = {"type": "stalker", "mac": client.mac, "exp": exp,
                                    "max_conn": str(max_conn) if max_conn else "", "portal_url": url}

            elif conn_type != "xtream" and not is_stalker:
                # Plain MAC: _last_account_js holds the full get_main_info JS dict,
                # set immediately after the dict is confirmed valid in account_info().
                # We read ALL display fields from it directly — the raw portal response
                # has every key needed (settings_password, parent_password,
                # default_timezone, ip, comment, storages) at the top level of js.
                # get_main_info often lacks default_timezone, parent_password,
                # settings_password, ip, and storages — these are present in
                # portal.php?type=stb&action=get_profile.  Fetch and merge any
                # missing fields so the display logic below picks them up.
                _mac_js = getattr(client, "_last_account_js", None) or {}
                if hasattr(client, "get_profile"):
                    try:
                        _prof_js = await client.get_profile()
                        if isinstance(_prof_js, dict):
                            for _pk in ("default_timezone", "timezone", "parent_password",
                                        "settings_password", "ip", "storages", "comment"):
                                if _pk in _prof_js and not _mac_js.get(_pk):
                                    _mac_js[_pk] = _prof_js[_pk]
                    except Exception as _pe:
                        log_cb(f"[CONNECT] MAC get_profile merge failed: {_pe}")
                _mac_storage_ips = []
                _mac_storages = _mac_js.get("storages")
                if isinstance(_mac_storages, dict):
                    for _st in _mac_storages.values():
                        if isinstance(_st, dict) and _st.get("storage_ip"):
                            _mac_storage_ips.append(str(_st["storage_ip"]))
                _str = lambda v: str(v) if v is not None else ""
                profile_data = {
                    "type": "mac", "user": ident,
                    "mac": client.mac if hasattr(client, "mac") else "",
                    "exp": exp, "max_conn": str(max_conn) if max_conn else "",
                    "settings_password": _str(_mac_js.get("settings_password") or
                                             (ai[3] if len(ai) > 3 else "")),
                    "adult_password": _str(_mac_js.get("parent_password") or
                                          _mac_js.get("adult_password") or
                                          (ai[4] if len(ai) > 4 else "")),
                    "portal_url": url,
                    "timezone": _str(_mac_js.get("default_timezone") or _mac_js.get("timezone") or ""),
                    "storage_ips": _mac_storage_ips,
                    "client_ip": _str(_mac_js.get("ip") or ""),
                    "comment": _str(_mac_js.get("comment") or ""),
                    "active_cons": "",
                }

            elif conn_type == "xtream":
                _server_tz = getattr(client, "_server_timezone", "") or ""
                _utc_off   = getattr(client, "_server_utc_offset", 0)
                profile_data = {
                    "type": "xtream", "user": ident, "mac": "",
                    "exp": exp, "max_conn": str(max_conn) if max_conn else "",
                    "active_cons": ai[4] if len(ai) > 4 else "",
                    "password": ai[3] if len(ai) > 3 else "",
                    "portal_url": url, "timezone": _server_tz,
                    "storage_ips": [], "client_ip": "", "comment": "",
                    "_utc_offset": _utc_off,
                }

            log_cb(f"[CONNECT] ✓ Connected: {ident} | {exp}")

            # ── fetch categories ──────────────────────────────────────────
            # Each category fetch is a real network call (live/VOD/series API
            # requests can be slow on large portals); a new api_connect() may
            # have arrived during this window — guard checked before and after.
            if get_epoch_fn and get_epoch_fn() != connect_epoch:
                log_cb("[CONNECT] Superseded before category fetch — discarding.")
                return {"success": False, "error": "superseded"}

            cats: dict = {}
            for m in ("live", "vod", "series"):
                try:
                    cats[m] = await client.fetch_categories(m)
                    log_cb(f"[CONNECT] {m.upper()}: {len(cats[m])} categories")
                except Exception as e:
                    log_cb(f"[CONNECT] ✗ {m.upper()} categories: {e}")
                    cats[m] = []

            # Final epoch guard after category fetches
            if get_epoch_fn and get_epoch_fn() != connect_epoch:
                log_cb("[CONNECT] Superseded during category fetch — discarding.")
                return {"success": False, "error": "superseded"}

            return {
                "success": True,
                "categories": cats,
                "ident": ident,
                "exp": exp,
                "exp_epoch": exp_epoch,
                "account_status": account_status_raw,
                "max_connections": max_conn,
                "portal_url": url,
                "is_stalker": is_stalker,
                "profile_data": profile_data,
            }

        return self.submit(_do(), timeout=submit_timeout)

    def disconnect(self) -> None:
        """Close the persistent session.  Safe to call from any thread."""
        async def _do() -> None:
            self._client = None
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None
        if not self._loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(_do(), self._loop).result(timeout=10)
            except Exception:
                pass

    def stop(self) -> None:
        """Graceful shutdown — call on app exit."""
        self.disconnect()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        # Loop is stopped after thread exits; close it now so is_closed() → True.
        if not self._loop.is_closed():
            self._loop.close()
