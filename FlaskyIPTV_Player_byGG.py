#!/usr/bin/env python3

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
MAC/Xtream/M3U Portal Builder — Flask/Android WebView Edition by GG_Raccoon.
Build on the base of Mac2M3UMKV_LiveVodsSeriesGUIPlayer_byGGv5.pyw CustomTkinter by GG_Raccoon.
Adapted to Flask + HTML5/HLS.js by conversion script.
Renamed from FlaskAppPlayerDownloader to FlaskyIPTV_Player.

Tested on Windows 10 with Python 3.14 and Termux on Android 16.
First run install_requirements_FlaskyIPTV_Player.py to make sure you have everything you need to run this script.
Run: python FlaskIPTV_Player_byGG.py,  then open http://localhost:5000 in your WebView/browser.
"""

import json
import re
import contextlib
import os
import random
import shutil
import string
import subprocess
import threading
import time
import queue
import warnings
import gzip as _gzip
from datetime import datetime, timezone
from urllib.parse import urlparse, quote, quote_plus, unquote, parse_qs
import asyncio
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

try:
    from dvr_addon import register_dvr_routes
    _DVR_AVAILABLE = True
except ImportError:
    _DVR_AVAILABLE = False
    def register_dvr_routes(*a, **kw): pass

try:
    from subtitles_addon import register_subtitles_routes
    _SUBTITLES_AVAILABLE = True
except ImportError:
    _SUBTITLES_AVAILABLE = False
    def register_subtitles_routes(*a, **kw): pass

try:
    from proxy_addon import register_proxy_routes, rewrite_m3u8
    _PROXY_AVAILABLE = True
except ImportError:
    _PROXY_AVAILABLE = False
    def register_proxy_routes(*a, **kw): pass
    def rewrite_m3u8(content, base_url): return content

try:
    from probe_addon import register_probe_routes
    _PROBE_AVAILABLE = True
except ImportError:
    _PROBE_AVAILABLE = False
    def register_probe_routes(*a, **kw): pass

try:
    from download_addon import (
        register_download_routes,
        safe_filename,
        run_ffmpeg_download, run_yt_dlp_download,
    )
    _DOWNLOAD_AVAILABLE = True
except ImportError:
    _DOWNLOAD_AVAILABLE = False
    def register_download_routes(*a, **kw): pass
    def safe_filename(name): return name[:200]
    def run_ffmpeg_download(*a, **kw): return 1
    def run_yt_dlp_download(*a, **kw): return False, "unavailable"

try:
    from epg_addon import register_epg_routes, start_epg_prefetch as _epg_prefetch
    _EPG_AVAILABLE = True
except ImportError:
    _EPG_AVAILABLE = False
    def register_epg_routes(*a, **kw): pass
    def _epg_prefetch(*a, **kw): pass

# ===================== OPTIONAL DEPS =====================

try:
    import yt_dlp  # type: ignore
    YTDLP_AVAILABLE = True
except Exception:
    YTDLP_AVAILABLE = False

from portal_clients import (
    normalize_base_url, _extract_url_from_text, safe_json, normalize_js,
    extract_xtream_from_m3u_url, _extinf_line, _extract_series_name,
    PortalClient, StalkerPortalClient, XtreamClient, M3UClient,
)


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
        # Optional user-supplied Stalker device IDs (override computed values when non-empty)
        self.stalker_device_id:  str = ""
        self.stalker_device_id2: str = ""
        self.stalker_sn:         str = ""
        self.stalker_signature:  str = ""
        self.cats_cache: dict = {}
        self._items_cache: dict = {}  # (mode, cat_id) → list of items, session-wide
        self._prefetch_running: bool = False  # True while bg channel prefetch is in-flight
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
        self._xmltv_cache_ttl = 5400  # 90 min — matches the -2h/+14h window
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
        self._xmltv_match_cache: dict = {}   # (lookup, feed_ck) → resolved cid | None
        # Plain MAC portal EPG token — cached after first handshake and reused
        # for all subsequent EPG requests (avoids per-channel handshake → 429s).
        # Stalker portals do NOT use this cache — their tokens are short-lived
        # and invalidate under concurrent load, so they always do a fresh handshake.
        self._mac_epg_token: str = ""
        self._mac_epg_headers: dict = {}
        # Guards the MAC token check+set as one atomic unit so only one thread
        # ever handshakes; all others wait and return the cached result.
        self._mac_epg_token_lock = threading.Lock()
        self.profile_data: dict = {}   # raw profile/account info for display
        # UTC offset in seconds of the Xtream portal server clock (0 = UTC).
        # Populated by XtreamClient.handshake() via calendar.timegm arithmetic.
        self._portal_utc_offset: int = 0
        # Separate XMLTV cache for catchup — built with a wide past window
        # (tv_archive_duration days back) so all archived programmes are available.
        # Distinct from _xmltv_cache which only keeps ±4-20h for live EPG.
        self._xmltv_catchup_cache: dict = {}   # base_norm → (ts, epg_dict, chan_names, win_back_h)
        self._xmltv_catchup_downloading: set = set()
        # ── Persistent logo caches ─────────────────────────────────────────
        # These survive across _make_client() calls (client instances are
        # short-lived — created and destroyed per request — so any cache on
        # the client object is useless across requests).
        #
        # _logo_cache_live: {ch_id: logo_url} for live channels.
        #   None  = get_all_channels not yet attempted this session.
        #   {}    = prefetch in progress (shared dict being filled in-place).
        #   dict  = already fetched (may be empty if portal returned nothing).
        # _logo_cache_vod: {item_id: logo_url} for VOD / series / Xtream.
        #   Built lazily from items that arrive with logos; zero extra requests.
        self._logo_cache_live: dict | None = None
        self._logo_cache_vod: dict = {}
        # Event set by the background prefetch once _logo_cache_live is fully
        # populated.  Any concurrent _fetch_ch_logo_cache() call that finds the
        # shared dict empty (prefetch in progress) waits on this instead of
        # issuing a second get_all_channels HTTP request.
        self._all_channels_ready: threading.Event = threading.Event()
        # Cache for all-channels list used by What's on Now → Find Channel.
        # Keyed by portal base URL so the walk only happens ONCE per connected portal
        # for the entire session — not on a TTL. Cleared on disconnect/reconnect.
        # {portal_key: [{"name", "cmd"/"stream_id"/"url", "tvg_id", ...}]}
        self._won_ch_cache: dict = {}
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
        # Pre-seed live channel pool so get_all_channels() returns from cache
        # immediately if the connect-time prefetch already populated it.
        if client._all_channels_raw is None:
            _xt_pool = state._items_cache.get(("live", "__all__"))
            if _xt_pool:
                client._all_channels_raw = _xt_pool
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
            # Pre-seed live channel pool from connect-time prefetch if available.
            if client._all_channels_raw is None:
                _xt_pool = state._items_cache.get(("live", "__all__"))
                if _xt_pool:
                    client._all_channels_raw = _xt_pool
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
            client = StalkerPortalClient(
                state.url, state.mac, state.log,
                custom_sn=state.stalker_sn,
                custom_device_id=state.stalker_device_id,
                custom_device_id2=state.stalker_device_id2,
                custom_signature=state.stalker_signature,
            )
        else:
            client = PortalClient(state.url, state.mac, state.log)
        # Inject both caches from AppState so this request can read what
        # previous requests already discovered.
        # _ch_logo_cache may be None (not yet fetched) or a dict — assign directly.
        # _vod_logo_cache is always a dict — share by reference.
        client._ch_logo_cache = state._logo_cache_live
        client._vod_logo_cache = state._logo_cache_vod
        # Inject the prefetch ready-event so _fetch_ch_logo_cache() can wait
        # on it instead of firing a concurrent get_all_channels HTTP call when
        # state._logo_cache_live is an empty dict (prefetch in progress).
        client._all_channels_ready_event = state._all_channels_ready
        # Pre-seed _all_channels_raw from the __all__ pool if already fetched.
        # Prevents _fetch_ch_logo_cache() from issuing a redundant get_all_channels
        # call on any code path (api_items, api_global_search, etc.).
        if hasattr(client, "_all_channels_raw") and client._all_channels_raw is None:
            cached_pool = state._items_cache.get(("live", "__all__"))
            if cached_pool:
                client._all_channels_raw = cached_pool
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
                    ident, exp, max_conn, _pw, active_cons = await xt.account_info()
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
                    state.profile_data = {'type':'xtream','user':ident,'exp':exp,'max_conn':str(max_conn) if max_conn else '','active_cons':active_cons,'portal_url':detected["base"]}
                    if hasattr(xt, "_server_utc_offset"):
                        state._portal_utc_offset = xt._server_utc_offset
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
        state.profile_data = {'type': 'm3u', 'user': ident, 'exp': exp, 'max_conn': str(max_conn) if max_conn else '', 'portal_url': state.m3u_url}
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
        # For Stalker: also pull get_profile for richer display data
        if state.is_stalker_portal and hasattr(client, 'get_profile'):
            try:
                prof = await client.get_profile()
                login = prof.get('login') or prof.get('fname') or prof.get('username') or ''
                if login:
                    ident = login
                # Use tariff_expired_date from profile only if account_info didn't get a real expiry.
                # expire_billing_date is a billing timestamp, NOT subscription end — reference code
                # only uses it as absolute last resort when phone/end_date are also empty.
                exp_label = 'expiry'
                if exp == 'unknown':
                    exp_prof = prof.get('tariff_expired_date') or prof.get('end_date') or prof.get('phone') or ''
                    if exp_prof and exp_prof != 'unknown':
                        exp = str(exp_prof)
                    elif prof.get('expire_billing_date'):
                        exp = str(prof.get('expire_billing_date'))
                        exp_label = 'last_billing'
                # max_conn from get_main_info, but also check profile for it
                if not max_conn:
                    try:
                        raw = (prof.get('max_online') or prof.get('playback_limit') or prof.get('max_connections')
                               or prof.get('con_per_device') or prof.get('connections_limit') or 0)
                        if not raw:
                            storages = prof.get('storages')
                            if isinstance(storages, dict):
                                for store in storages.values():
                                    if isinstance(store, dict) and store.get('max_online'):
                                        raw = store['max_online']
                                        break
                        max_conn = int(raw) if raw else 0
                    except Exception:
                        pass
                state.profile_data = {
                    'type': 'stalker',
                    'mac': client.mac,
                    'login': login or ident,
                    'password': str(prof.get('password', '') or ''),
                    'exp': exp,
                    'exp_label': exp_label,
                    'status': prof.get('status', ''),
                    'max_conn': str(max_conn) if max_conn else '–',
                    'active_cons': str(prof.get('active_cons') or prof.get('online_streams') or ''),
                    'settings_password': str(prof.get('settings_password', '') or ''),
                    'adult_password': str(prof.get('parent_password', '') or prof.get('adult_password', '') or ''),
                    'portal_url': state.url or state.m3u_url,
                }
            except Exception as e:
                state.log(f"[CONNECT] Could not fetch Stalker profile details: {e}")
                state.profile_data = {'type': 'stalker', 'mac': client.mac, 'exp': exp,
                                      'max_conn': str(max_conn) if max_conn else '',
                                      'portal_url': state.url or state.m3u_url}
        elif not state.is_stalker_portal:
            _is_xtream = (state.conn_type == 'xtream' or
                          (state.conn_type == 'm3u_url' and state.m3u_xtream_override))
            state.profile_data = {
                'type': 'xtream' if _is_xtream else 'mac',
                'user': ident,
                'mac': client.mac if hasattr(client, 'mac') else '',
                'exp': exp,
                'max_conn': str(max_conn) if max_conn else '',
                # Xtream returns 5-tuple: (user, exp, max_conn, password, active_cons)
                # PortalClient returns 5-tuple: (ident, phone, max_conn, settings_pwd, adult_pwd)
                'active_cons':       _ai4[4] if _is_xtream and len(_ai4) > 4 else '',
                'password':          _ai4[3] if _is_xtream and len(_ai4) > 3 else '',
                'settings_password': _ai4[3] if not _is_xtream and len(_ai4) > 3 else '',
                'adult_password':    _ai4[4] if not _is_xtream and len(_ai4) > 4 else '',
                'portal_url': state.url or state.m3u_url,
            }
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
    if state.conn_type == "xtream" and hasattr(client, "_server_utc_offset"):
        state._portal_utc_offset = client._server_utc_offset
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
if _DVR_AVAILABLE:
    register_dvr_routes(flask_app, state)

    # Resolver callback: re-resolve a fresh CDN URL right before ffmpeg spawns.
    # Stalker / MAC portals return short-lived CDN tokens via create_link; if we
    # record the URL at schedule-time the token may be expired by start-time.
    def _dvr_resolve_url(job: dict):
        channel_item = job.get("channelItem") or {}
        if not channel_item:
            return None
        try:
            async def _r():
                async with _make_client() as client:
                    return await client.resolve_item_url("live", channel_item, {})
            url = run_async(_r())
            if url and isinstance(url, str):
                # Strip hls_proxy wrapper — ffmpeg must hit the raw stream
                if "/api/hls_proxy" in url:
                    from urllib.parse import urlparse as _up2, parse_qs as _pqs
                    _qs = _pqs(_up2(url).query)
                    url = (_qs.get("url") or [url])[0]
                return url
        except Exception as _e:
            state.log(f"[DVR] URL re-resolve error: {_e}")
        return None

    state.dvr_url_resolver = _dvr_resolve_url

register_subtitles_routes(flask_app)
register_proxy_routes(flask_app, state)

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
    # Serve pre-rendered, pre-gzip-compressed HTML cached at startup.
    # Avoids 40ms Jinja2 recompile on every page load.
    from flask import make_response
    if "gzip" in request.headers.get("Accept-Encoding", "").lower():
        resp = make_response(_HTML_BYTES_GZ)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Type"]     = "text/html; charset=utf-8"
        resp.headers["Content-Length"]   = len(_HTML_BYTES_GZ)
        resp.headers["Vary"]             = "Accept-Encoding"
        return resp
    # Fallback for clients that don't accept gzip (rare)
    resp = make_response(_HTML_BYTES)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


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
        state.stalker_device_id  = data.get("stalker_device_id",  "").strip()
        state.stalker_device_id2 = data.get("stalker_device_id2", "").strip()
        state.stalker_sn         = data.get("stalker_sn",         "").strip()
        state.stalker_signature  = data.get("stalker_signature",  "").strip()
        state.cats_cache = {}
        state._items_cache = {}
        state.m3u_cache = None
        state.m3u_is_local = False
        state.m3u_xtream_override = None
        state._epg_cache = {}
        state._xmltv_cache = {}
        state._xmltv_dl_locks = {}
        state._xmltv_dl_events = {}
        state._xmltv_downloading = set()
        state._xmltv_needs = set()
        state._xmltv_match_cache = {}
        state._mac_epg_token = ""
        state._mac_epg_headers = {}
        state._short_epg_broken = set()
        state._xmltv_no_data = set()
        state._won_ch_cache = {}
        state.connected = False
        state.stop_flag.clear()
        # Reset logo caches so a new portal starts fresh
        state._logo_cache_live = None
        state._all_channels_ready.clear()
        state._logo_cache_vod = {}
        state._portal_utc_offset = 0
        state._xmltv_catchup_cache = {}
        state._xmltv_catchup_downloading = set()
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
        # Ensure "All Channels" appears at the top of the live category list
        # with id="__all__" so api_items() always routes it through the fast path.
        #
        # Strategy: if the portal already returned its own "all genres" entry
        # (MAC portals use id="*", title="All"; others vary), replace its id
        # with "__all__" in-place rather than prepending a duplicate entry.
        # If no such entry exists, prepend one.
        _ALL_TITLES = {"all", "all channels", "all streams", "all live", "all genres"}
        _ALL_IDS    = {"*", "0", "all", ""}   # portal-specific "all" sentinel ids
        if result.get("success"):
            live = state.cats_cache.get("live", [])
            if live:
                # Check if there's already a native "all" category from the portal
                native_all_idx = next(
                    (i for i, c in enumerate(live)
                     if str(c.get("id","")).strip() in _ALL_IDS
                     or str(c.get("title","")).strip().lower() in _ALL_TITLES),
                    None
                )
                if native_all_idx is not None:
                    # Re-use the portal's entry but fix its id so the fast path works
                    entry = dict(live[native_all_idx])
                    old_id = entry.get("id")
                    entry["id"] = "__all__"
                    entry["title"] = "All Channels"
                    # Move it to the front
                    live = [entry] + [c for i,c in enumerate(live) if i != native_all_idx]
                    state.cats_cache["live"] = live
                    state.log(f"[CONNECT] Mapped portal 'All' (id={old_id!r}) → '__all__' ({len(live)-1} other categories)")
                elif live[0].get("id") != "__all__":
                    # No native "all" entry — inject our own at the top
                    state.cats_cache["live"] = [{"id": "__all__", "title": "All Channels"}] + live
                    state.log(f"[CONNECT] Injected 'All Channels' category ({len(live)} real categories)")
                result["categories"] = state.cats_cache

        # ── Background: prefetch full live channel list for logo cache + EPG ──
        # Only for MAC/Stalker portals — get_all_channels is a dedicated endpoint
        # that fetches every channel in one call and is expensive on first use.
        # By doing this now in a daemon thread, the logo cache (state._logo_cache_live)
        # and channel pool (state._items_cache[("live","__all__")]) are warm before
        # the user browses any live category, so _fetch_ch_logo_cache() hits the
        # instance cache immediately instead of triggering a blocking HTTP call.
        # The same pool is reused by api_find_channel (EPG matching) and api_whats_on.
        #
        # Race handling: we set state._logo_cache_live = {} (empty dict) right here
        # before the thread starts.  _make_client injects this same dict object into
        # every concurrent client.  _fetch_ch_logo_cache() sees non-None but empty →
        # waits on state._all_channels_ready (max 20s) instead of firing its own
        # get_all_channels call.  The prefetch fills the dict in-place then sets the
        # event so all waiters unblock and return the now-populated dict.
        _is_mac     = result.get("success") and state.conn_type == "mac"
        _is_xtream  = result.get("success") and (
            state.conn_type == "xtream" or
            (state.conn_type == "m3u_url" and state.m3u_xtream_override))

        if _is_mac or _is_xtream:
            _pf_portal_key = (f"{state.conn_type}:{state.url}"
                              f":{getattr(state, 'mac', '')}:{getattr(state, 'username', '')}")

            # MAC only: pre-initialise the shared logo dict to {} (not None) so
            # any concurrent _make_client call gets the empty dict injected and
            # knows to wait on _all_channels_ready rather than fire its own
            # get_all_channels HTTP request.
            if _is_mac:
                state._logo_cache_live = {}
                state._all_channels_ready.clear()

            def _bg_prefetch_channels():
                # Capture conn_type at thread-creation time; reconnect may change it.
                _pf_is_mac = _is_mac
                try:
                    _pf_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(_pf_loop)

                    async def _prefetch():
                        if not state.connected:
                            return
                        _ck = (f"{state.conn_type}:{state.url}"
                               f":{getattr(state, 'mac', '')}:{getattr(state, 'username', '')}")
                        if _ck != _pf_portal_key:
                            return   # portal changed since connect
                        if ("live", "__all__") in state._items_cache:
                            state.log("[PREFETCH] Pool already populated — skipping")
                            return
                        state.log("[PREFETCH] Background: fetching full live channel list…")
                        state._prefetch_running = True
                        async with _make_client() as client:
                            channels = await client.get_all_channels()
                            if channels:
                                state._items_cache[("live", "__all__")] = channels
                                state._won_ch_cache[_pf_portal_key] = channels
                                # Invalidate any empty-result Stalker category caches that
                                # were populated before this prefetch completed — they will
                                # re-fetch and use the _all_channels_raw fallback next time.
                                if _pf_is_mac:
                                    stale = [k for k, v in state._items_cache.items()
                                             if k[0] == "live" and k[1] != "__all__"
                                             and isinstance(v, list) and len(v) == 0]
                                    for k in stale:
                                        del state._items_cache[k]
                                    if stale:
                                        state.log(f"[PREFETCH] Cleared {len(stale)} empty category cache(s) — will re-fetch with full channel pool")
                                if _pf_is_mac:
                                    # Fill the shared logo dict in-place — same object
                                    # already injected into every concurrent MAC client
                                    # via client._ch_logo_cache = state._logo_cache_live.
                                    logo_count = 0
                                    for _ch in channels:
                                        _cid = str(_ch.get("id") or "").strip()
                                        _logo = str(_ch.get("logo") or _ch.get("screenshot_uri") or
                                                    _ch.get("tv_logo") or _ch.get("pic") or "").strip()
                                        if _cid and _logo:
                                            state._logo_cache_live[_cid] = _logo
                                            logo_count += 1
                                    state.log(f"[PREFETCH] {len(channels)} channels cached; "
                                              f"{logo_count} logos ready")
                                else:
                                    state.log(f"[PREFETCH] {len(channels)} channels cached")
                            else:
                                state.log("[PREFETCH] get_all_channels returned empty")

                    _pf_loop.run_until_complete(_prefetch())
                    _pf_loop.close()
                except Exception as _pfe:
                    state.log(f"[PREFETCH] Background channel prefetch error: {_pfe}")
                finally:
                    state._prefetch_running = False
                    if _pf_is_mac:
                        # Always unblock MAC logo-cache waiters, even on failure.
                        state._all_channels_ready.set()

            threading.Thread(target=_bg_prefetch_channels,
                             daemon=True, name="ch-prefetch").start()

        # ── Background: prefetch external EPG (if configured) ──────────────────
        # Runs parallel to the channel prefetch. Builds ek_combined the same way
        # api_whats_on does, registers all relevant keys in _xmltv_downloading so
        # every EPG consumer sees "loading" rather than spawning a parallel HTTP
        # request, then downloads and indexes the feed in a daemon thread.
        # The call is a no-op if no EPG URL is configured or if the data is
        # already cached / in flight.
        if result.get("success") and _EPG_AVAILABLE:
            _epg_prefetch(state)

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
    import proxy_addon as _px
    with _px._proxy_img_cache_lock:
        _px._proxy_img_cache.clear()
    with _px._HOTLINK_BLOCKED_HOSTS_LOCK:
        _px._HOTLINK_BLOCKED_HOSTS.clear()
        _px._HOTLINK_403_COUNTS.clear()
    with _px._DNS_FAIL_BLOCKED_HOSTS_LOCK:
        _px._DNS_FAIL_BLOCKED_HOSTS.clear()
        _px._DNS_FAIL_COUNTS.clear()
    with _px._TIMEOUT_BLOCKED_HOSTS_LOCK:
        _px._TIMEOUT_BLOCKED_HOSTS.clear()
        _px._TIMEOUT_COUNTS.clear()
    with _px._HOST_404_LOCK:
        _px._HOST_404_BLOCKED.clear()
        _px._HOST_404_COUNTS.clear()
    state._logo_cache_live = None
    state._logo_cache_vod  = {}
    state.cats_cache        = {}
    state._items_cache      = {}
    state._won_ch_cache     = {}
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

    # Safety-net alias: if the __all__ pool is cached and this cat_id is a
    # known portal-native "all channels" sentinel, treat it as __all__ so it
    # serves from cache instantly instead of paginating tens of thousands of items.
    _ALL_SENTINELS = {"*", "0", "all", ""}
    if (mode == "live"
            and cat_id in _ALL_SENTINELS
            and ("live", "__all__") in state._items_cache):
        state.log(f"[ITEMS] Aliasing cat_id={cat_id!r} → '__all__' (pool cached)")
        cat_id = "__all__"

    # ── Session-wide items cache — keyed by (mode, cat_id) ───────────────────
    # Cleared on disconnect/reconnect and on Clear Cache. Avoids re-paginating
    # the same category every time the user returns to it.
    _cache_key = (mode, cat_id)
    if _cache_key in state._items_cache:
        cached_items = state._items_cache[_cache_key]
        state.log(f"[ITEMS] Cache hit: {mode} cat={cat_id} ({len(cached_items)} items)")
        return jsonify({"items": cached_items, "count": len(cached_items), "has_more": False})

    # If the __all__ pool is already cached (e.g. from global search), reuse it
    # directly for the __all__ category — no client needed at all.
    _all_key = (mode, "__all__")
    if cat_id == "__all__" and _all_key in state._items_cache:
        items = state._items_cache[_all_key]
        state.log(f"[ITEMS] All Channels: cache hit from global search ({len(items)} items)")
        state._items_cache[_cache_key] = items
        return jsonify({"items": items, "count": len(items), "has_more": False})

    try:
        async def fetch():
            async with _make_client() as client:
                # Pre-seed client's _all_channels_raw from the state-level pool if
                # it already exists — prevents _fetch_ch_logo_cache() from making
                # a redundant get_all_channels call mid-pagination.
                if _all_key in state._items_cache and hasattr(client, "_all_channels_raw"):
                    if client._all_channels_raw is None:
                        client._all_channels_raw = state._items_cache[_all_key]
                        state.log(f"[ITEMS] Pre-seeded client logo cache from __all__ pool ({len(client._all_channels_raw)} entries)")

                # ── "All Channels" fast path via get_all_channels ─────────────
                if cat_id == "__all__" and mode == "live":
                    if hasattr(client, "get_all_channels"):
                        try:
                            channels = await client.get_all_channels("live")
                            if channels:
                                state.log(f"[ITEMS] All Channels via get_all_channels: {len(channels)} items")
                                return {"items": channels, "has_more": False}
                            state.log("[ITEMS] get_all_channels returned empty — falling back to full pagination")
                        except Exception as e:
                            state.log(f"[ITEMS] get_all_channels failed ({e}) — falling back to full pagination")
                    # Fallback: paginate every real category
                    real_cats = [c for c in state.cats_cache.get("live", [])
                                 if c.get("id") != "__all__"]
                    all_items = []
                    for cat_obj in real_cats:
                        page = 1
                        while True:
                            pg_items = await client.fetch_items_page(
                                "live", str(cat_obj.get("id", "")), page)
                            if not pg_items:
                                break
                            all_items.extend(pg_items)
                            page += 1
                            if len(pg_items) < 5:
                                break
                    state.log(f"[ITEMS] All Channels via full pagination: {len(all_items)} items")
                    return {"items": all_items, "has_more": False}

                # ── Normal single-category pagination ─────────────────────────
                all_items = []
                page = 1
                items = []
                while page <= max_pages:
                    items = await client.fetch_items_page(mode, cat_id, page)
                    if not items:
                        break
                    all_items.extend(items)
                    if not browse:
                        state.log(f"  Page {page}: {len(items)} items (total: {len(all_items)})")
                    page += 1
                    if len(items) < 5:
                        break
                if browse:
                    state.log(f"[ITEMS] '{cat.get('title','?')}': {len(all_items)} items loaded")
                if browse and page > max_pages and items:
                    return {"items": all_items, "has_more": True}
                return {"items": all_items, "has_more": False}

        result = run_async(fetch())
        items = result["items"] if isinstance(result, dict) else result
        has_more = result.get("has_more", False) if isinstance(result, dict) else False
        # For Stalker portals: don't cache empty category results while the background
        # prefetch is still running — the channel pool isn't ready yet. Return a
        # "pending" signal so the frontend retries after a short delay.
        if (not items and not has_more
                and state.is_stalker_portal
                and state._prefetch_running
                and mode == "live" and cat_id != "__all__"):
            state.log(f"[ITEMS] '{cat.get('title','?')}': 0 items — prefetch still running, returning pending")
            return jsonify({"items": [], "count": 0, "has_more": False, "pending": True})
        if not has_more:
            state._items_cache[_cache_key] = items
        return jsonify({"items": items, "count": len(items), "has_more": has_more})
    except Exception as e:
        state.log(f"[ITEMS] Error: {e}")
        return jsonify({"error": str(e), "items": []})


@flask_app.route("/api/global_search", methods=["POST"])
def api_global_search():
    """Search all live channels by name.

    Sending pool_only=true (or empty query) warms the channel pool and
    returns its size — used by the JS modal to prefetch on open.
    Shares the same __all__ cache used by the All Channels category."""
    data      = request.get_json(force=True)
    query     = (data.get("query") or "").strip().lower()
    pool_only = bool(data.get("pool_only", False))
    mode      = data.get("mode", "live"); mode = mode if mode in ("live","vod","series") else "live"
    if not state.connected:
        return jsonify({"error": "Not connected", "items": [], "pool_size": 0})

    # Build / reuse channel pool
    _cache_key = (mode, "__all__")
    if _cache_key not in state._items_cache:
        state.log(f"[SEARCH] Building channel pool (mode={mode})...")
        try:
            async def _fetch_pool():
                async with _make_client() as client:
                    # Fast path: single get_all_channels call
                    if hasattr(client, "get_all_channels"):
                        try:
                            channels = await client.get_all_channels(mode)
                            if channels:
                                state.log(f"[SEARCH] ✓ get_all_channels: {len(channels)} channels")
                                return channels
                            state.log("[SEARCH] get_all_channels empty — falling back to pagination")
                        except Exception as e:
                            state.log(f"[SEARCH] get_all_channels failed: {e} — falling back to pagination")
                    # Slow fallback: paginate every real category.
                    # Log per-category only (not per-page) so log stays readable.
                    real_cats = [c for c in state.cats_cache.get(mode, [])
                                 if c.get("id") != "__all__"]
                    state.log(f"[SEARCH] Paginating {len(real_cats)} categories...")
                    all_items = []
                    for cat_obj in real_cats:
                        cat_name = cat_obj.get("title") or cat_obj.get("id") or "?"
                        before   = len(all_items)
                        page = 1
                        while True:
                            pg_items = await client.fetch_items_page(
                                mode, str(cat_obj.get("id", "")), page)
                            if not pg_items:
                                break
                            all_items.extend(pg_items)
                            page += 1
                            if len(pg_items) < 5:
                                break
                        added = len(all_items) - before
                        if added:
                            state.log(f"[SEARCH]   {cat_name}: {added} items (total {len(all_items)})")
                    state.log(f"[SEARCH] ✓ Pagination complete: {len(all_items)} channels total")
                    return all_items

            pool = run_async(_fetch_pool())
            state._items_cache[_cache_key] = pool
        except Exception as e:
            state.log(f"[SEARCH] Error building channel pool: {e}")
            return jsonify({"error": str(e), "items": [], "pool_size": 0})

    pool = state._items_cache[_cache_key]

    # Prefetch / pool-only request
    if pool_only or not query:
        return jsonify({"items": [], "count": 0, "pool_size": len(pool)})

    # Filter by query
    def _name(it):
        return (it.get("name") or it.get("o_name") or it.get("title") or
                it.get("stream_name") or it.get("fname") or "").lower()

    results = [it for it in pool if query in _name(it)]
    state.log(f"[SEARCH] '{query}' → {len(results)} results from {len(pool)} channels")
    return jsonify({"items": results, "count": len(results), "pool_size": len(pool)})


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


# Resolve ffmpeg/ffprobe once at startup — shutil.which() does a filesystem
# PATH search and was previously called on every /api/status poll and on every
# resolve/record/probe request. Caching to module-level variables means the
# search runs exactly once per process lifetime.
_FFMPEG_WHICH      = shutil.which("ffmpeg")
_FFPROBE_WHICH     = shutil.which("ffprobe")
_FFMPEG_PATH       = _FFMPEG_WHICH or "ffmpeg"   # ready-to-use binary path
_FFPROBE_PATH      = _FFPROBE_WHICH or "ffprobe"
_FFMPEG_AVAILABLE  = _FFMPEG_WHICH is not None
_FFPROBE_AVAILABLE = _FFPROBE_WHICH is not None

# Push the resolved ffmpeg path into dvr_addon now that it's known
if _DVR_AVAILABLE:
    import dvr_addon as _dvr_mod
    _dvr_mod._FFMPEG = _FFMPEG_PATH

register_probe_routes(flask_app, state, run_async, _make_client, _FFPROBE_PATH)
register_download_routes(flask_app, state, run_async, run_worker, _make_client,
                         _FFMPEG_PATH, _FFPROBE_PATH,
                         _FFMPEG_AVAILABLE, YTDLP_AVAILABLE)
register_epg_routes(flask_app, state, run_async, _make_client)

@flask_app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "connected": state.connected,
        "busy": state.busy,
        "status": state.status,
        "recording": state.recording,
        "conn_type": state.conn_type,
        "ffmpeg": _FFMPEG_AVAILABLE,
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


@flask_app.route("/api/profile", methods=["GET"])
def api_profile():
    return jsonify(state.profile_data if state.connected else {})


@flask_app.route("/api/logs")
def api_logs():
    """SSE stream of log messages."""
    def generate():
        # Send initial ping
        yield "data: Connected to log stream\n\n"
        while True:
            try:
                # 30-second timeout: sends a heartbeat every 30s instead of
                # every 1s. Keeps the connection alive without waking up a
                # server thread 60 times per minute when the app is idle.
                msg = state.log_queue.get(timeout=30.0)
                # Escape newlines for SSE
                safe_msg = msg.replace("\n", " ").replace("\r", "")
                yield f"data: {safe_msg}\n\n"
            except queue.Empty:
                # Heartbeat — only fires every 30s now
                yield ": heartbeat\n\n"

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection": "keep-alive",
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


@flask_app.route("/api/browse_folder", methods=["GET"])
def api_browse_folder():
    """Desktop only: open a native OS folder picker (askdirectory)."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askdirectory(title="Select DVR Output Folder")
        root.destroy()
        return jsonify({"path": path or ""})
    except Exception as e:
        return jsonify({"path": "", "error": str(e)})

@flask_app.route("/api/browse_m3u_file", methods=["GET"])
def api_browse_m3u_file():
    """Desktop only: save-as dialog to set the M3U output path with a predefined filename."""
    default_name = request.args.get("name", "playlist.m3u")
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.asksaveasfilename(
            title="Set M3U Output Path",
            initialfile=default_name,
            defaultextension=".m3u",
            filetypes=[("M3U playlist", "*.m3u *.m3u8"), ("All files", "*.*")],
        )
        root.destroy()
        return jsonify({"path": path or ""})
    except Exception as e:
        return jsonify({"path": "", "error": str(e)})


@flask_app.route("/api/reveal_in_folder", methods=["POST"])
def api_reveal_in_folder():
    """Open the folder containing a file and highlight it (Windows Explorer / macOS Finder / Linux)."""
    data = request.get_json(force=True)
    path = (data.get("path") or "").strip()
    if not path or not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    try:
        if os.name == "nt":
            # Windows: open Explorer with the file selected
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif sys.platform == "darwin":
            # macOS: Finder reveal
            subprocess.Popen(["open", "-R", path])
        else:
            # Linux: open the folder (file managers vary — xdg-open the directory)
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
  display:inline-flex;align-items:center;justify-content:center;gap:5px;
  -webkit-tap-highlight-color:transparent;user-select:none;position:relative;overflow:hidden}
/* Shine sweep — animates left→right on hover only, resets instantly on release */
button::before{content:'';position:absolute;top:0;left:0;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent);
  transform:translateX(-100%);transition:none;pointer-events:none}
button:hover:not(:disabled)::before{transform:translateX(100%);transition:transform .45s ease}
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
  background:rgba(8,8,20,.97);
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
.hdr-r{display:flex;align-items:center;gap:5px;flex-shrink:0;margin-left:auto}
.tag{display:inline-flex;align-items:center;padding:2px 7px;border-radius:20px;font-size:10px;font-weight:700}
.tok{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.2)}
.terr{background:rgba(239,68,68,.1);color:var(--red);border:1px solid rgba(239,68,68,.2)}
.twrn{background:rgba(245,158,11,.1);color:var(--orange);border:1px solid rgba(245,158,11,.2)}
.hdr-ico{width:34px;height:34px;padding:0;display:inline-flex;align-items:center;
  justify-content:center;font-size:16px;border-radius:var(--rsm)}
#conn-btn.connected{background:rgba(34,197,94,.1);border-color:rgba(34,197,94,.3);
  color:var(--txt);cursor:pointer}
#conn-btn.connected:hover{background:rgba(34,197,94,.18);border-color:rgba(34,197,94,.5)}
@media(max-width:899px){#hdr-tags{display:none}}
/* Short status text — hidden by default, shown on mobile only */
#hdr-status-short{display:none}
/* Mobile: compress header so cast + settings are always visible */
@media(max-width:599px){
  #hdr-bar{gap:3px!important;padding:5px 8px!important}
  #activity-status{display:none!important}
  #conn-btn{max-width:82px!important;padding:0 8px!important}
  .hdr-r{gap:1px!important}
  .hdr-ico{width:28px!important;height:28px!important;font-size:13px!important}
  #hdr-status{display:none!important}
  #hdr-status-short{display:inline!important}
}

/* ─── conn panel ─────────────────────────────────────────────── */
#cpanel{overflow:hidden;max-height:0;transition:max-height .35s cubic-bezier(.4,0,.2,1)}
#cpanel.open{max-height:820px;overflow-y:auto}
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
/* items-collapse-btn is desktop-only; hide globally so it never creates layout gap on mobile */
#items-collapse-btn{display:none}
@media(min-width:900px){
  #main{display:grid!important;grid-template-columns:350px 28px 1fr;height:100%;transition:grid-template-columns .3s ease}
  #main.items-open{grid-template-columns:350px 380px 1fr}
  #main.items-open #p-items > *{opacity:1;transition:opacity .2s ease .15s}
  #main:not(.items-open) #p-items > *{opacity:0;pointer-events:none;transition:opacity .1s ease}
  #main:not(.items-open) #p-items::after{content:'›';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:16px;color:var(--txt3);pointer-events:none}
  #main:not(.items-open) #p-items{cursor:pointer;background:var(--s1);border-left:1px solid var(--bdr);overflow:hidden}
  #main:not(.items-open) #p-items::before{content:'';position:absolute;top:0;left:0;width:100%;height:100%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent);transform:translateX(-100%);transition:none;pointer-events:none}
  #main:not(.items-open) #p-items:hover::before{transform:translateX(100%);transition:transform .45s ease}
  #p-items{position:relative}
  #items-collapse-btn{display:none!important}
  #main.items-open #items-collapse-btn{display:flex!important;position:absolute;right:0;top:0;bottom:0;transform:none;z-index:20;width:28px;height:100%;padding:0;font-size:16px;background:var(--s3);border:1px solid var(--bdr);border-radius:0;color:var(--txt2);align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;border-right:none;border-top:none;border-bottom:none}
  #main.items-open #p-items{padding-right:28px;box-sizing:border-box}
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
  border-bottom:1px solid rgba(124,58,237,.15);
  padding:10px 14px;display:flex;align-items:center;gap:8px;flex-shrink:0;position:relative}
.ph::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(124,58,237,.3),rgba(6,182,212,.2),transparent)}
.ph h3{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px;
  color:var(--txt2);flex:1;min-width:0}

/* ─── bottom nav ─────────────────────────────────────────────── */
#botnav{display:flex;background:rgba(8,8,20,.97);border-top:1px solid rgba(124,58,237,.2);
  flex-shrink:0;z-index:100;padding-bottom:env(safe-area-inset-bottom);
  box-shadow:0 -4px 20px rgba(0,0,0,.5),0 0 30px rgba(124,58,237,.05)}
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
.mt::before{content:'';position:absolute;top:0;left:0;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.1),transparent);
  transform:translateX(-100%);transition:transform .4s ease;pointer-events:none}
.mt:hover::before{transform:translateX(100%)}
.mt:hover:not(.on){border-color:var(--bdr2);color:var(--txt);background:rgba(255,255,255,.06)}
.mt.on{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;
  border-color:transparent;box-shadow:0 2px 14px var(--glow2),0 0 28px rgba(124,58,237,.2),
  inset 0 1px 0 rgba(255,255,255,.2)}
/* Mobile: compact buttons — tighter padding, no icon, text only */
@media(max-width:899px){
  .mt{padding:5px 10px;font-size:11px}
  .mt[data-m="favs"]{padding:4px 7px}
  .mt-txt{display:inline}
}
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
.citem::before{content:'';position:absolute;top:0;left:0;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.06),transparent);
  transform:translateX(-100%);transition:transform .5s ease;pointer-events:none;z-index:0}
.citem:hover::before{transform:translateX(100%)}
.citem::after{content:'';position:absolute;inset:0;opacity:0;transition:opacity .2s;
  background:linear-gradient(90deg,rgba(124,58,237,.06),transparent);pointer-events:none}
.citem:hover::after{opacity:1}
.c-ico{font-size:16px;flex-shrink:0;z-index:1}
.c-name{flex:1;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;z-index:1}
.c-arr{font-size:10px;color:var(--txt3);flex-shrink:0;z-index:1;transition:var(--tr)}
.citem:hover .c-arr{color:var(--acc);transform:translateX(3px)}

/* ─── drag-to-reorder ────────────────────────────────────────── */
.drag-src{opacity:.45!important;outline:2px dashed var(--acc);outline-offset:-2px;
  cursor:grabbing!important;z-index:10;transition:none!important}
.drag-src::before,.drag-src::after{display:none}
.drag-src:active{transform:none!important}
.citem.drag-src{animation:none}
.irow.drag-src{animation:none}
.drag-dropline{height:2px;background:var(--acc);border-radius:2px;
  margin:1px 4px;pointer-events:none;flex-shrink:0;box-shadow:0 0 6px var(--acc)}
.drag-ind{position:absolute;right:38px;top:50%;transform:translateY(-50%);
  font-size:15px;color:var(--acc);font-weight:900;pointer-events:none;
  text-shadow:0 0 8px rgba(0,0,0,.9);letter-spacing:0;line-height:1;z-index:20}

/* ─── skeleton ───────────────────────────────────────────────── */
/* ::before = icon placeholder, ::after = text placeholder — both STATIC.
   The shimmer sweep is a single .skel-wave child div animated via
   transform:translateX() which is fully GPU-composited (zero CPU repaint).
   Previous approach: background-position animation on 2 pseudo-elements per
   row × 12 rows = 24 simultaneous CPU repaints at 60 fps. */
.skel,.skel-sm{position:relative;overflow:hidden;display:flex;
  align-items:center;gap:10px;padding:0 12px;
  background:var(--s2);border:1px solid var(--bdr)}
.skel{height:52px;border-radius:var(--rsm);margin-bottom:4px}
.skel-sm{height:38px;border-radius:var(--rsm);margin-bottom:3px}
.skel::before{content:'';width:32px;height:32px;border-radius:6px;flex-shrink:0;background:var(--s3)}
.skel::after{content:'';flex:1;height:14px;border-radius:4px;background:var(--s3)}
.skel-sm::before{content:'';width:22px;height:22px;border-radius:4px;flex-shrink:0;background:var(--s3)}
.skel-sm::after{content:'';flex:1;height:11px;border-radius:3px;background:var(--s3)}
/* Single GPU-composited sweep per row */
.skel-wave{position:absolute;inset:0;
  background:linear-gradient(90deg,transparent 0%,rgba(255,255,255,.06) 50%,transparent 100%);
  transform:translateX(-100%);will-change:transform;pointer-events:none;
  animation:skel-sweep 1.4s ease-in-out infinite}
@keyframes skel-sweep{to{transform:translateX(200%)}}
@keyframes spin{to{transform:rotate(360deg)}}
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
.irow::before{content:'';position:absolute;top:0;left:0;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.05),transparent);
  transform:translateX(-100%);transition:transform .45s ease;pointer-events:none}
.irow:hover{background:rgba(124,58,237,.07);border-color:rgba(124,58,237,.22);
  box-shadow:0 0 12px rgba(124,58,237,.08)}
.irow:hover::before{transform:translateX(100%)}
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
.imenu-btn{display:flex;align-items:center;justify-content:flex-start;gap:9px;padding:9px 14px;
  font-size:12px;font-weight:600;color:var(--txt);background:none;border:none;
  cursor:pointer;text-align:left;transition:background .12s;width:100%}
.imenu-btn:hover{background:var(--s4)}
.imenu-btn .imenu-ico{font-size:14px;width:18px;text-align:center;flex-shrink:0}
.imenu-sep{height:1px;background:var(--bdr);margin:3px 0}

.ibottom{display:flex;flex-wrap:wrap;gap:5px;padding:8px 0 4px;
  border-top:1px solid var(--bdr);flex-shrink:0}
.ibottom button{flex:1;min-width:68px;height:34px;font-size:12px}
.icount{font-size:11px;color:var(--txt3);padding:3px 0;text-align:center;flex-shrink:0}


/* ─── paths area ─────────────────────────────────────────────── */
#paths{padding:8px 0 4px;border-top:1px solid var(--bdr);flex-shrink:0;display:none}
.prow{display:flex;align-items:center;gap:5px;margin-bottom:5px;position:relative}
.plbl{font-size:11px;color:var(--txt2);white-space:nowrap;min-width:54px;flex-shrink:0}
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
.out-fb-tgt.active{background:rgba(124,58,237,.2);border-color:var(--acc);color:var(--txt)}


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
#vph-ico{font-size:52px;opacity:.18}

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
  background:linear-gradient(135deg,#a855f7 0%,#7c3aed 30%,#c084fc 60%,#6d28d9 100%);
  box-shadow:0 4px 22px var(--glow),0 0 0 1px rgba(168,85,247,.3),inset 0 1px 0 rgba(255,255,255,.25),inset 0 -2px 4px rgba(0,0,0,.4);
  color:#fff;flex-shrink:0;position:relative;overflow:hidden}
/* GPU-composited shine sweep — replaces the old background-position metallicShift.
   Uses transform:translateX so the compositor handles it with zero CPU repaint. */
.pbig::before{content:'';position:absolute;inset:0;border-radius:50%;
  background:linear-gradient(105deg,transparent 20%,rgba(255,255,255,.28) 50%,transparent 80%);
  transform:translateX(-150%) skewX(-15deg);will-change:transform;pointer-events:none;
  animation:pbig-shine 3s ease-in-out infinite}
@keyframes pbig-shine{
  0%,55%,100%{transform:translateX(-150%) skewX(-15deg)}
  30%        {transform:translateX(150%)  skewX(-15deg)}
}
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
  box-shadow:0 24px 64px rgba(0,0,0,.8);animation:slide-up .25s cubic-bezier(.34,1.56,.64,1);
  transform:translateZ(0);-webkit-transform:translateZ(0);backface-visibility:hidden;-webkit-backface-visibility:hidden}
.plm-hdr{display:flex;align-items:center;gap:8px;padding:14px 16px;
  border-bottom:1px solid var(--bdr);flex-shrink:0}
.plm-hdr h2{flex:1;font-size:14px;font-weight:800;color:var(--txt)}
.pl-list{flex:1;overflow-y:auto;padding:10px;min-height:60px}
.pl-empty{text-align:center;padding:32px 16px;color:var(--txt3);font-size:12px}
.pl-empty span{font-size:40px;display:block;margin-bottom:8px;opacity:.2}
.pli{display:flex;align-items:center;gap:8px;padding:10px 12px;border-radius:var(--rsm);
  margin-bottom:5px;background:rgba(255,255,255,.025);border:1px solid var(--bdr);transition:var(--tr);
  animation:fade-up .2s ease both;border-left:3px solid var(--pli-accent,var(--bdr));
  position:relative;overflow:hidden;box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
.pli::before{content:'';position:absolute;top:0;left:0;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.06),transparent);
  transform:translateX(-100%);transition:transform .5s ease;pointer-events:none}
.pli:hover::before{transform:translateX(100%)}
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
.pli-type-stalker{background:rgba(168,85,247,.15);color:#a855f7;border:1px solid rgba(168,85,247,.3);
  box-shadow:0 0 8px rgba(168,85,247,.15)}
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


.mv-confirm-btns button{height:32px;padding:0 14px;font-size:12px}
</style>
</head>
<body>
<div id="app">

<!-- HEADER -->
<header id="hdr">
  <div id="hdr-bar">
    <button id="conn-btn" onclick="openProfileModal()" style="
      display:inline-flex;align-items:center;gap:6px;
      height:30px;padding:0 12px;border-radius:20px;
      background:rgba(255,255,255,.06);border:1px solid var(--bdr);
      font-size:12px;font-weight:600;color:var(--txt3);
      cursor:default;flex-shrink:0;transition:var(--tr);outline:none;
      max-width:200px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">
      <span id="cdot"></span>
      <span id="hdr-status" style="overflow:hidden;text-overflow:ellipsis">Not connected</span><span id="hdr-status-short">Offline</span>
    </button>
    <span id="activity-status" style="
      font-size:11px;color:var(--txt3);white-space:nowrap;overflow:hidden;
      text-overflow:ellipsis;max-width:260px;flex-shrink:1;
      transition:opacity .3s;opacity:1"></span>
    <div class="hdr-r">
      <span id="busy-sp" class="spin hidden"></span>
      <span id="hdr-tags">{{ tags_html | safe }}</span>
      <button class="btn-ghost hdr-ico" id="stopbtn" onclick="doStop()" disabled title="Stop">⏹</button>
      <button class="btn-ghost hdr-ico" id="gsrch-btn" onclick="openGlobalSearch()" title="Global channel search">🔍</button>
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
          <label>MAC</label><span style="position:relative;display:inline-flex;align-items:center;max-width:200px;width:200px"><input id="i-mac" type="password" placeholder="00:1A:79:XX:XX:XX" style="width:100%;padding-right:28px" autocomplete="new-password" autocorrect="off" spellcheck="false"><button type="button" onclick="(function(b){var i=document.getElementById('i-mac');var shown=i.getAttribute('data-shown')==='1';if(shown){i.setAttribute('type','password');i.setAttribute('data-shown','0');b.textContent='👁';}else{i.setAttribute('type','text');i.setAttribute('data-shown','1');b.textContent='🙈';};})(this)" style="position:absolute;right:4px;background:none;border:none;cursor:pointer;padding:0;font-size:13px;line-height:1;color:var(--txt2)" tabindex="-1">👁</button></span>
        </div>
        <details style="margin:4px 0 2px"><summary style="font-size:11px;color:var(--txt3);cursor:pointer;user-select:none;padding:3px 0;list-style:none;display:flex;align-items:center;gap:4px"><span style="font-size:9px;opacity:.6">▶</span>Stalker overrides (optional)</summary>
          <div style="display:flex;flex-direction:column;gap:4px;margin-top:6px;padding:8px;background:rgba(255,255,255,.03);border-radius:6px;border:1px solid var(--bdr)">
            <div style="display:flex;gap:6px;align-items:center"><label style="min-width:68px;font-size:11px;color:var(--txt3)">SN</label><input id="i-sn" placeholder="leave blank — auto-computed" autocomplete="off" autocorrect="off" spellcheck="false" style="font-family:monospace;font-size:11px;flex:1"></div>
            <div style="display:flex;gap:6px;align-items:center"><label style="min-width:68px;font-size:11px;color:var(--txt3)">Device ID</label><input id="i-devid" placeholder="leave blank — auto-computed" autocomplete="off" autocorrect="off" spellcheck="false" style="font-family:monospace;font-size:11px;flex:1"></div>
            <div style="display:flex;gap:6px;align-items:center"><label style="min-width:68px;font-size:11px;color:var(--txt3)">Device ID2</label><input id="i-devid2" placeholder="leave blank — auto-computed" autocomplete="off" autocorrect="off" spellcheck="false" style="font-family:monospace;font-size:11px;flex:1"></div>
            <div style="display:flex;gap:6px;align-items:center"><label style="min-width:68px;font-size:11px;color:var(--txt3)">Signature</label><input id="i-sig" placeholder="leave blank — auto-computed" autocomplete="off" autocorrect="off" spellcheck="false" style="font-family:monospace;font-size:11px;flex:1"></div>
          </div>
        </details>
        <div style="display:flex;gap:6px;align-items:center">
          <label title="Optional: external XMLTV EPG URL(s). One URL per line. Leave blank to use portal's own EPG.">EPG</label><textarea id="i-mac-epg" rows="2" placeholder="https://… xmltv URL (optional, one per line)" autocomplete="new-password" autocorrect="off" spellcheck="false" style="resize:vertical;width:100%"></textarea>
        </div>
      </div>
      <div id="cr-xtream" class="cr hidden" style="flex-direction:column;align-items:stretch">
        <div style="display:flex;gap:6px;align-items:center">
          <label>URL</label><input id="i-xu" type="text" inputmode="url" placeholder="http://server.host:8080" autocomplete="new-password" autocorrect="off" spellcheck="false">
          <label>User</label><input id="i-us" placeholder="username" style="max-width:150px" autocomplete="new-password" autocorrect="off" spellcheck="false">
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <label title="Optional: external XMLTV EPG URL(s). One URL per line. Leave blank to use provider's own EPG.">EPG</label><textarea id="i-epg" rows="2" placeholder="https://epg.best/xmltv.php?… (optional, one per line)" style="flex:1;resize:vertical" autocomplete="new-password" autocorrect="off" spellcheck="false"></textarea>
          <label>Pass</label><span style="position:relative;display:inline-flex;align-items:center;max-width:150px;width:150px"><input id="i-pw" type="password" placeholder="password" style="width:100%;padding-right:28px" autocomplete="new-password"><button type="button" onclick="(function(b){var i=document.getElementById('i-pw');i.type=i.type==='password'?'text':'password';b.textContent=i.type==='password'?'👁':'🙈'})(this)" style="position:absolute;right:4px;background:none;border:none;cursor:pointer;padding:0;font-size:13px;line-height:1;color:var(--txt2)" tabindex="-1">👁</button></span>
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
          <textarea id="i-m3u-epg" rows="2" placeholder="https://epg.best/xmltv.php?… (optional, one per line)" style="max-width:300px;resize:vertical" autocomplete="new-password" autocorrect="off" spellcheck="false"></textarea>
        </div>
        <!-- File row — always visible -->
        <div style="display:flex;gap:6px;align-items:center">
          <label style="flex-shrink:0">File</label>
          <span id="m3u-fp-fname" style="flex:1;font-size:12px;color:var(--txt2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">No file chosen</span>
          <button class="btn-ghost" onclick="m3uOpenPicker()" style="height:28px;padding:0 10px;font-size:12px;flex-shrink:0;white-space:nowrap">📂 Browse…</button>
          <button id="m3u-force-fb-btn" class="btn-ghost" onclick="m3uForceFileBrowser()" title="Force mobile file browser" style="height:28px;padding:0 8px;font-size:12px;flex-shrink:0;white-space:nowrap">📁</button>
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
        <div style="display:flex;align-items:center;justify-content:space-between;padding-bottom:2px">
          <div style="font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1px;color:var(--txt3)">Output Paths</div>
          <button class="btn-ghost" id="out-fb-toggle" onclick="outFbToggle()"
            title="Toggle between desktop picker and mobile file browser"
            style="height:22px;padding:0 8px;font-size:10px;font-weight:700;display:flex;align-items:center;gap:4px;flex-shrink:0
            ">&#x1F4C1; File browser: Off</button>
        </div>
        <!-- DESKTOP: path inputs + tkinter browse buttons -->
        <div id="out-paths-desktop">
          <div class="prow" style="position:relative">
            <span class="plbl">M3U:</span>
            <input id="o-m3u" type="text" placeholder="/sdcard/Download/playlist.m3u" oninput="saveFP()" style="height:30px;font-size:12px">
            <button class="btn-ghost psug-btn" onclick="outBrowseRow('m3u')" title="Browse">&#x1F4C2;</button>
          </div>
          <div class="prow" style="position:relative">
            <span class="plbl">Download:</span>
            <input id="o-dir" type="text" placeholder="/sdcard/Download/" oninput="saveFP()" style="height:30px;font-size:12px">
            <button class="btn-ghost psug-btn" onclick="outBrowseRow('dir')" title="Browse">&#x1F4C2;</button>
          </div>
          <div class="prow" style="position:relative">
            <span class="plbl">DVR:</span>
            <input id="o-dvr" type="text" placeholder="/sdcard/Download/DVR/" oninput="saveFP()" style="height:30px;font-size:12px"
              title="Output folder for DVR scheduled recordings">
            <button class="btn-ghost psug-btn" onclick="outBrowseRow('dvr')" title="Browse">&#x1F4C2;</button>
          </div>
        </div>
        <!-- MOBILE: inline browser — inputs stay in desktop div (hidden), IDs still accessible -->
        <div id="out-paths-mobile" style="display:none;flex-direction:column;gap:0">
        <!-- Path value readouts (read-only display, actual inputs live in desktop div) -->
        <div style="display:flex;flex-direction:column;gap:4px;margin-bottom:6px">
          <div style="display:flex;align-items:center;gap:6px">
            <span class="plbl">M3U:</span>
            <span id="out-mob-m3u" style="font-size:11px;color:var(--txt2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            <span class="plbl">Download:</span>
            <span id="out-mob-dir" style="font-size:11px;color:var(--txt2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            <span class="plbl">DVR:</span>
            <span id="out-mob-dvr" style="font-size:11px;color:var(--txt2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
          </div>
        </div>
        <div id="out-fb-wrap">
          <!-- Target selector -->
          <div style="display:flex;gap:4px;margin-bottom:6px;align-items:center">
            <span style="font-size:10px;color:var(--txt3);white-space:nowrap">Set path for:</span>
            <button id="out-fb-tgt-m3u" class="btn-ghost out-fb-tgt active" onclick="outFbSetTarget('m3u')"
              style="height:22px;padding:0 8px;font-size:10px;font-weight:700">M3U</button>
            <button id="out-fb-tgt-dir" class="btn-ghost out-fb-tgt" onclick="outFbSetTarget('dir')"
              style="height:22px;padding:0 8px;font-size:10px;font-weight:700">Download</button>
            <button id="out-fb-tgt-dvr" class="btn-ghost out-fb-tgt" onclick="outFbSetTarget('dvr')"
              style="height:22px;padding:0 8px;font-size:10px;font-weight:700">DVR</button>
          </div>
          <!-- Nav bar -->
          <div style="display:flex;gap:5px;margin-bottom:5px;align-items:center">
            <button class="btn-ghost" id="out-fb-up" onclick="outFbUp()" style="height:26px;padding:0 8px;font-size:14px;flex-shrink:0">&#x2191;</button>
            <span id="out-fb-path" style="font-size:10px;color:var(--txt2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">/sdcard/Download</span>
            <button class="btn-ghost" id="out-fb-select" onclick="outFbConfirm()"
              style="height:26px;padding:0 8px;font-size:10px;color:#4ade80;border-color:rgba(34,197,94,.3);flex-shrink:0">&#x2713; Select</button>
            <button class="btn-ghost" onclick="outFbClose()" style="height:26px;padding:0 8px;font-size:11px;flex-shrink:0">&#x2715;</button>
          </div>
          <!-- Quick paths (nav shortcuts — always visible) -->
          <div style="display:flex;flex-wrap:wrap;gap:3px;margin-bottom:5px">
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px" onclick="outFbNav('/sdcard/Download')">&#x1F4E5; Download</button>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px" onclick="outFbNav('/storage/emulated/0/Download')">&#x1F4E5; /0/DL</button>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px" onclick="outFbNav('/sdcard/Download/DVR')">&#x1F4FC; DVR</button>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px" onclick="outFbNav('/sdcard')">&#x1F4F1; /sdcard</button>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px" onclick="outFbNav('/storage/emulated/0')">&#x1F4F1; /storage/0</button>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px" onclick="outFbNav('/data/data/com.termux/files/home')">&#x1F5A5; Termux</button>
          </div>
          <!-- M3U: filename input + quick presets -->
          <div id="out-fb-fname-row" style="display:none;align-items:center;gap:6px;margin-bottom:5px">
            <span style="font-size:10px;color:var(--txt3);white-space:nowrap">Filename:</span>
            <input id="out-fb-fname" type="text" placeholder="playlist.m3u"
              style="flex:1;height:24px;font-size:11px;padding:0 7px;border-radius:var(--rss);
                     border:1px solid var(--bdr2);background:var(--s3);color:var(--txt)"
              autocomplete="off" autocorrect="off" spellcheck="false">
          </div>
          <div id="out-fb-m3u-presets" style="display:none;flex-wrap:wrap;gap:3px;margin-bottom:5px">
            <span style="font-size:10px;color:var(--txt3);width:100%;margin-bottom:2px">&#x26A1; Quick set M3U path:</span>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px"
              onclick="outFbQuickApply('/sdcard/Download/playlist.m3u')">/sdcard/DL/playlist.m3u</button>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px"
              onclick="outFbQuickApply('/storage/emulated/0/Download/playlist.m3u')">/0/DL/playlist.m3u</button>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px"
              onclick="outFbQuickApply('/data/data/com.termux/files/home/playlist.m3u')">Termux ~/playlist.m3u</button>
          </div>
          <!-- Download dir: quick presets -->
          <div id="out-fb-dir-presets" style="display:none;flex-wrap:wrap;gap:3px;margin-bottom:5px">
            <span style="font-size:10px;color:var(--txt3);width:100%;margin-bottom:2px">&#x26A1; Quick set Download folder:</span>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px"
              onclick="outFbQuickApply('/sdcard/Download/')">/sdcard/Download/</button>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px"
              onclick="outFbQuickApply('/storage/emulated/0/Download/')">/0/Download/</button>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px"
              onclick="outFbQuickApply('/data/data/com.termux/files/home/')">Termux ~/</button>
          </div>
          <!-- DVR dir: quick presets -->
          <div id="out-fb-dvr-presets" style="display:none;flex-wrap:wrap;gap:3px;margin-bottom:5px">
            <span style="font-size:10px;color:var(--txt3);width:100%;margin-bottom:2px">&#x26A1; Quick set DVR folder:</span>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px"
              onclick="outFbQuickApply('/sdcard/Download/DVR/')">/sdcard/Download/DVR/</button>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px"
              onclick="outFbQuickApply('/storage/emulated/0/Download/DVR/')">/0/Download/DVR/</button>
            <button class="btn-ghost" style="font-size:10px;height:20px;padding:0 6px"
              onclick="outFbQuickApply('/data/data/com.termux/files/home/DVR/')">Termux ~/DVR/</button>
          </div>
          <!-- File/folder list -->
          <div id="out-fb-list" style="max-height:90px;overflow-y:auto;border:1px solid var(--bdr);border-radius:var(--rss);background:var(--s4)">
            <div style="padding:8px;font-size:12px;color:var(--txt3)">Loading&hellip;</div>
          </div>
        </div><!-- /out-fb-wrap -->
        </div><!-- /out-paths-mobile -->
        <div class="prow" style="position:relative" id="extplayer-row-desktop">
          <span class="plbl">Player:</span>
          <input id="o-extplayer" type="text" placeholder="C:\\Program Files\\VLC\\vlc.exe"
            autocomplete="new-password" autocorrect="off" spellcheck="false"
            oninput="saveExtPlayer()" style="height:30px;font-size:12px"
            title="Path to external player executable (e.g. VLC, mpv)">
          <button class="btn-ghost psug-btn" onclick="browseExtPlayer()" title="Browse for player exe" style="font-size:13px">&#x1F4C2;</button>
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
        <div id="cat-ph-ico" style="font-size:52px;opacity:.13;margin-bottom:12px">📡</div>
        <div style="font-size:13px">Connect to load categories</div>
      </div>
    </div>

  </div>

  <!-- BROWSE -->
  <div class="panel" id="p-items">
    <button id="items-collapse-btn" onclick="event.stopPropagation();document.getElementById('main').classList.remove('items-open')" title="Collapse">‹</button>
    <div class="ph">
      <h3 id="ittitle">Browse</h3>
      <button class="btn-ghost btn-sm" id="backbtn" onclick="goBack()" disabled>◀ Back</button>
    </div>
    <div style="padding:10px 10px 0;display:flex;flex-direction:column;gap:6px;flex-shrink:0">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:6px">
        <div class="bcrum" id="bcrum" style="flex:1;min-width:0"><span class="bc-s">Categories</span></div>
        <button class="epg-layout-btn" id="epg-grid-btn" onclick="toggleEpgGrid()" title="EPG Grid view" style="display:none">📅 EPG</button>
        <button class="epg-layout-btn" id="epg-expand-btn" onclick="openEpgExpandOverlay()" title="Expand EPG" style="display:none">⤢</button>
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

            <button id="pctrl-act-btn"
              onclick="event.stopPropagation();openActTab()"
              title="Actions"
              style="height:26px;padding:0 10px;font-size:12px;font-weight:700;
              border-radius:var(--rss);background:var(--s4);color:var(--txt2);
              border:1px solid var(--bdr2);letter-spacing:.5px;position:relative;overflow:hidden">
              ⚡ Actions<span id="pctrl-act-badge" style="display:none;position:absolute;
                top:-4px;right:-4px;background:var(--green);color:#fff;font-size:9px;
                font-weight:800;min-width:14px;height:14px;border-radius:20px;
                text-align:center;line-height:14px;padding:0 3px;pointer-events:none"></span>
            </button>

            <button id="mv-desktop-btn"
              onclick="event.stopPropagation();mvToggle()"
              title="Multi-View"
              style="height:26px;padding:0 10px;font-size:12px;font-weight:700;
              border-radius:var(--rss);background:var(--s4);color:var(--txt2);
              border:1px solid var(--bdr2);letter-spacing:.5px;display:none">
              ⊞ Multi-View
            </button>

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
            <button class="btn-ghost" id="dvr-desktop-btn" onclick="dvrOpen()" title="Open DVR scheduler" style="height:28px;padding:0 10px;font-size:12px">📹 DVR</button>
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
          <button class="btn-ghost" onclick="copyLog()" style="height:22px;padding:0 8px;font-size:11px;border-radius:var(--rss)">Copy</button>
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

  <!-- PROFILE MODAL -->
  <div id="profile-modal" style="display:none;position:fixed;inset:0;z-index:950;background:rgba(0,0,0,.7);align-items:center;justify-content:center" onclick="if(event.target===this)closeProfileModal()">
    <div style="background:var(--s2);border-radius:var(--rs);width:min(420px,92vw);padding:20px;box-shadow:var(--sh);border:1px solid var(--bdr2)">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
        <h3 style="font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px;color:var(--txt2);margin:0">Connection Profile</h3>
        <button class="btn-ghost" onclick="closeProfileModal()" style="height:26px;width:26px;padding:0;font-size:14px">✕</button>
      </div>
      <div id="profile-modal-body" style="display:flex;flex-direction:column;gap:8px;font-size:12px"></div>
    </div>
  </div>

  <!-- EPG OVERLAY -->
  <!-- DVR EPG OVERLAY — programme picker for scheduling -->

  <!-- CATCHUP OVERLAY -->
  <!-- LOG (mobile tab) -->
  <div class="panel" id="p-log" style="background:var(--bg)">
    <div class="ph">
      <h3>Activity Log</h3>
      <button class="btn-ghost" onclick="copyLog()"
        style="height:24px;padding:0 8px;font-size:11px;border-radius:var(--rss)">Copy</button>
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

<!-- ITEM CONTEXT MENU -->
<div id="item-menu">
  <div id="item-menu-hdr">Options</div>
  <div class="imenu-sep" id="imenu-sep1"></div>
  <button class="imenu-btn" id="imenu-epg"      onclick="iMenuEPG()">     <span class="imenu-ico">📅</span>EPG / Programme Info</button>
  <div class="imenu-sep" id="imenu-sep2"></div>
  <button class="imenu-btn" id="imenu-ext"      onclick="iMenuExternal()"><span class="imenu-ico">🎬</span>External Player</button>
  <button class="imenu-btn" id="imenu-imdb"     onclick="iMenuIMDB()">    <span class="imenu-ico">🔍</span>Open TMDB/IMDB</button>
  <button class="imenu-btn" id="imenu-rec"      onclick="iMenuRec()">     <span class="imenu-ico">⏺</span>Record</button>
  <button class="imenu-btn" id="imenu-mkv"      onclick="iMenuMKV()">     <span class="imenu-ico">⬇</span>Download MKV</button>
  <div class="imenu-sep" id="imenu-sep3"></div>
  <button class="imenu-btn" id="imenu-hide"     onclick="iMenuHide()">    <span class="imenu-ico">🚫</span>Hide this item</button>
</div>
<div id="item-menu-bg" onclick="closeItemMenu()" style="display:none;position:fixed;inset:0;z-index:799"></div>

<div id="item-menu-bg" onclick="closeItemMenu()" style="display:none;position:fixed;inset:0;z-index:799"></div>

<!-- DVR OVERLAY -->

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
          <div class="pl-row"><label>MAC</label><span style="position:relative;display:inline-flex;align-items:center;flex:1"><input id="pl-mac" type="password" placeholder="00:1A:79:XX:XX:XX" autocomplete="new-password" autocorrect="off" spellcheck="false" style="flex:1;padding-right:28px"><button type="button" onclick="(function(b){var i=document.getElementById('pl-mac');var shown=i.getAttribute('data-shown')==='1';if(shown){i.setAttribute('type','password');i.setAttribute('data-shown','0');b.textContent='👁';}else{i.setAttribute('type','text');i.setAttribute('data-shown','1');b.textContent='🙈';};})(this)" style="position:absolute;right:4px;background:none;border:none;cursor:pointer;padding:0;font-size:13px;line-height:1;color:var(--txt2)" tabindex="-1">👁</button></span></div>
          <details style="margin:4px 0 2px"><summary style="font-size:11px;color:var(--txt3);cursor:pointer;user-select:none;padding:2px 0">Stalker overrides (optional)</summary>
            <div style="display:flex;flex-direction:column;gap:4px;margin-top:6px">
              <div class="pl-row"><label style="min-width:80px;font-size:11px">SN</label><input id="pl-sn" placeholder="leave blank — auto-computed" autocomplete="off" autocorrect="off" spellcheck="false" style="font-family:monospace;font-size:11px"></div>
              <div class="pl-row"><label style="min-width:80px;font-size:11px">Device ID</label><input id="pl-devid" placeholder="leave blank — auto-computed" autocomplete="off" autocorrect="off" spellcheck="false" style="font-family:monospace;font-size:11px"></div>
              <div class="pl-row"><label style="min-width:80px;font-size:11px">Device ID2</label><input id="pl-devid2" placeholder="leave blank — auto-computed" autocomplete="off" autocorrect="off" spellcheck="false" style="font-family:monospace;font-size:11px"></div>
              <div class="pl-row"><label style="min-width:80px;font-size:11px">Signature</label><input id="pl-sig" placeholder="leave blank — auto-computed" autocomplete="off" autocorrect="off" spellcheck="false" style="font-family:monospace;font-size:11px"></div>
            </div>
          </details>
          <div class="pl-row"><label>EPG</label><textarea id="pl-mac-epg" rows="2" placeholder="External EPG URL(s), one per line (optional)" autocomplete="new-password" autocorrect="off" spellcheck="false" style="resize:vertical;width:100%"></textarea></div>
        </div>
        <div id="plf-xtream" class="hidden">
          <div class="pl-row"><label>URL</label><input id="pl-xu" type="text" inputmode="url" placeholder="http://server.host:8080" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
          <div class="pl-row"><label>User</label><input id="pl-us" placeholder="username" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
          <div class="pl-row"><label>Pass</label><span style="position:relative;display:inline-flex;align-items:center;flex:1"><input id="pl-pw" type="password" placeholder="password" autocomplete="new-password" style="flex:1;padding-right:28px"><button type="button" onclick="(function(b){var i=document.getElementById('pl-pw');i.type=i.type==='password'?'text':'password';b.textContent=i.type==='password'?'👁':'🙈'})(this)" style="position:absolute;right:4px;background:none;border:none;cursor:pointer;padding:0;font-size:13px;line-height:1;color:var(--txt2)" tabindex="-1">👁</button></span></div>
          <div class="pl-row"><label>EPG</label><textarea id="pl-epg" rows="2" placeholder="External EPG URL(s), one per line (optional)" autocomplete="new-password" autocorrect="off" spellcheck="false" style="resize:vertical;width:100%"></textarea></div>
        </div>
        <div id="plf-m3u" class="hidden">
          <div class="pl-row"><label>URL</label><input id="pl-m3u" type="text" inputmode="url" placeholder="http://example.com/list.m3u" autocomplete="new-password" autocorrect="off" spellcheck="false"></div>
          <div class="pl-row"><label>EPG</label><textarea id="pl-m3u-epg" rows="2" placeholder="External EPG URL(s), one per line (optional)" autocomplete="new-password" autocorrect="off" spellcheck="false" style="resize:vertical;width:100%"></textarea></div>
        </div>
        <div class="pl-row" style="justify-content:flex-end;gap:7px">
          <button class="btn-ghost" onclick="plClearForm()" style="height:34px;padding:0 12px;font-size:12px">Clear</button>
          <button class="btn-acc" onclick="plSave()" style="height:34px;padding:0 16px;font-size:12px">💾 Save</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- defer: downloads in parallel, executes after HTML parsed — never blocks rendering.
     These libs are only needed on user interaction (play/multiview), not at load time. -->
<script defer src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js" crossorigin="anonymous"></script>
<script defer src="https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack.min.css"/>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack-extra.min.css"/>
<script defer src="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack-all.min.js"></script>
<script>
const CFG = {{ config | safe }};
const _DVR_OK = CFG.dvr_ok === true;

// Hide DVR buttons if addon is not installed
if(!_DVR_OK){
  document.addEventListener('DOMContentLoaded', ()=>{
    const btn = document.getElementById('adr-dvr-btn');
    if(btn) btn.style.display = 'none';
    const dBtn = document.getElementById('dvr-desktop-btn');
    if(dBtn) dBtn.style.display = 'none';
  });
}

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
  if(_favsFilterActive){
    _applyFavsFilter();
  } else {
    // Update only the star button in-place to preserve scroll position
    const rows = document.getElementById('ilist').querySelectorAll('.irow');
    if(rows[i]){
      const starBtn = rows[i].querySelector('button[title="Favourite"]');
      if(starBtn) starBtn.style.color = isFav(it) ? '#f5c518' : 'rgba(255,255,255,0.25)';
    }
    // Update the favourites badge count
    const b = document.getElementById('badge');
    const total = loadFavs(mode).length;
    b.textContent = total>99?'99+':total;
    b.classList.toggle('vis', total>0);
  }
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
    stalker_sn:         CT==='mac' ? (document.getElementById('i-sn')?.value.trim()||'')     : '',
    stalker_device_id:  CT==='mac' ? (document.getElementById('i-devid')?.value.trim()||'')  : '',
    stalker_device_id2: CT==='mac' ? (document.getElementById('i-devid2')?.value.trim()||'') : '',
    stalker_signature:  CT==='mac' ? (document.getElementById('i-sig')?.value.trim()||'')    : '',
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
      document.getElementById('conn-btn').classList.add('connected');
      setStatus('Connected — click here');
      isStalker = !!d.is_stalker;
      const _rawUrl = payload.m3u_url || payload.url || '';
      const _portalHost = _rawUrl ? (()=>{try{return new URL(_rawUrl).hostname;}catch(e){return _rawUrl.replace(/https?:\/\//,'').split('/')[0].split(':')[0];}})() : '';
      document.getElementById('portal-name-label').textContent = _portalHost || '—';
      // Include the credential (username for Xtream/M3U, MAC for MAC/Stalker) so
      // two different logins on the same portal host get completely separate fav stores.
      const _credSlug = (payload.username || payload.mac || '').trim().toLowerCase()
                          .replace(/[^a-z0-9]/g,'').slice(0,24);
      _favsPortalKey = ((_portalHost || '—').trim()) + (_credSlug ? '_'+_credSlug : '');
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
          is_stalker: d.is_stalker || false,
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
      document.getElementById('conn-btn').classList.remove('connected');
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
  // Always show ALL saved favourites for this mode/portal — never restrict to current category
  filtItems=[...favs];
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

// ── GLOBAL SEARCH ─────────────────────────────────────────────────────────────────────────────
// Opens a modal that immediately prefetches all channels on open.
// Shares the server-side __all__ cache with the All Channels category.
let _gsModal = null;
let _gsReady = false;

function _gsEsc(s){
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function openGlobalSearch(){
  if(_gsModal){ _gsModal.remove(); _gsModal=null; }
  const mo = document.createElement('div');
  mo.id = 'gs-modal';
  mo.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:9100;display:flex;align-items:center;justify-content:center';

  const box = document.createElement('div');
  box.style.cssText = 'background:var(--s2);border:1px solid var(--bdr);border-radius:10px;padding:18px 16px 14px;width:min(500px,94vw);display:flex;flex-direction:column;gap:10px;box-shadow:0 8px 32px rgba(0,0,0,.5)';

  const hdr = document.createElement('div');
  hdr.style.cssText = 'display:flex;justify-content:space-between;align-items:center;gap:8px';
  const titleEl = document.createElement('span');
  titleEl.style.cssText = 'font-size:14px;font-weight:600;color:var(--txt)';
  titleEl.textContent = '🔍 Global Channel Search';
  const closeBtn = document.createElement('button');
  closeBtn.style.cssText = 'background:none;border:none;color:var(--txt3);font-size:19px;cursor:pointer;padding:0 4px;line-height:1';
  closeBtn.textContent = '✕';
  closeBtn.onclick = closeGlobalSearch;
  hdr.appendChild(titleEl); hdr.appendChild(closeBtn);

  const inp = document.createElement('input');
  inp.id = 'gs-input';
  inp.type = 'search';
  inp.placeholder = 'Loading channels…';
  inp.disabled = true;
  inp.autocomplete = 'off'; inp.spellcheck = false;
  inp.style.cssText = 'padding:9px 11px;border-radius:7px;border:1px solid var(--bdr);background:var(--s3);color:var(--txt);font-size:13px;outline:none;width:100%;box-sizing:border-box;opacity:0.6';
  inp.addEventListener('input', _gsFilter);
  inp.addEventListener('keydown', function(e){ if(e.key==='Escape') closeGlobalSearch(); });

  const st = document.createElement('div');
  st.id = 'gs-status';
  st.style.cssText = 'font-size:11px;color:var(--txt3);min-height:14px;padding:0 2px';

  const rl = document.createElement('div');
  rl.id = 'gs-results';
  rl.style.cssText = 'max-height:54vh;overflow-y:auto;display:flex;flex-direction:column;gap:3px';

  box.appendChild(hdr); box.appendChild(inp); box.appendChild(st); box.appendChild(rl);
  mo.appendChild(box);
  mo.addEventListener('click', function(e){ if(e.target===mo) closeGlobalSearch(); });
  document.body.appendChild(mo);
  _gsModal = mo;

  // Kick off prefetch immediately so channels are ready when user types
  _gsPrefetch(st, rl, inp);
}

function closeGlobalSearch(){
  if(_gsModal){ _gsModal.remove(); _gsModal=null; }
}

function _gsPrefetch(st, rl, inp){
  // If already loaded this session, ready instantly
  if(_gsReady){
    st.textContent = '✓ Channels ready — start typing to search';
    inp.placeholder = 'Search channels…';
    inp.style.opacity = '1';
    inp.disabled = false;
    setTimeout(function(){ inp.focus(); }, 40);
    return;
  }
  // Show spinner in status
  st.innerHTML = '';
  const sp = document.createElement('span');
  sp.style.cssText = 'width:11px;height:11px;border-radius:50%;border:2px solid var(--acc);border-top-color:transparent;animation:spin .7s linear infinite;display:inline-block;vertical-align:middle;margin-right:6px';
  st.appendChild(sp);
  st.appendChild(document.createTextNode('Fetching all channels…'));

  fetch('/api/global_search',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pool_only:true, mode:'live'})})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(!_gsModal) return;
    if(d.error){ st.textContent = '⚠ ' + d.error; return; }
    _gsReady = true;
    const n = d.pool_size || 0;
    st.textContent = '✓ ' + n + ' channels ready — start typing to search';
    inp.placeholder = 'Search ' + n + ' channels…';
    inp.style.opacity = '1';
    inp.disabled = false;
    inp.focus();
  })
  .catch(function(e){
    if(!_gsModal) return;
    st.textContent = '⚠ Error: ' + e.message;
    inp.disabled = false;
    inp.style.opacity = '1';
  });
}

let _gsTimer = null;
function _gsFilter(){
  clearTimeout(_gsTimer);
  _gsTimer = setTimeout(_gsDoSearch, 200);
}

function _gsDoSearch(){
  if(!_gsReady) return;
  const q = (document.getElementById('gs-input')?.value || '').trim();
  const st = document.getElementById('gs-status');
  const rl = document.getElementById('gs-results');
  if(!q){
    if(rl) rl.innerHTML = '';
    return;
  }
  fetch('/api/global_search',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({query:q, mode:'live'})})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(!_gsModal) return;
    if(d.error){ if(st) st.textContent='Error: '+d.error; return; }
    const items = d.items || [];
    const pool  = d.pool_size || 0;
    if(st) st.textContent = items.length
      ? items.length+' result'+(items.length!==1?'s':'')+' from '+pool+' channels'+(items.length>=200?' (first 200 shown)':'')
      : 'No results for "'+_gsEsc(q)+'"';
    if(!rl) return;
    rl._gsItems = items;
    rl.innerHTML = '';
    items.slice(0,200).forEach(function(it,i){
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:9px;padding:7px 9px;border-radius:7px;cursor:pointer;background:var(--s3);transition:background .15s';
      row.onmouseenter = function(){ this.style.background='var(--s4)'; };
      row.onmouseleave = function(){ this.style.background='var(--s3)'; };
      row.onclick = function(){ _gsPickItem(i); };
      const logo = it.logo||it.stream_icon||it.screenshot_uri||it.pic||'';
      if(logo){
        const img = document.createElement('img');
        img.src = logo;
        img.style.cssText = 'width:30px;height:30px;object-fit:contain;border-radius:5px;flex-shrink:0;background:var(--s4)';
        img.onerror = function(){ this.style.display='none'; };
        row.appendChild(img);
      } else {
        const ico = document.createElement('div');
        ico.style.cssText = 'width:30px;height:30px;border-radius:5px;background:var(--s4);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:13px';
        ico.textContent = '📺';
        row.appendChild(ico);
      }
      const nm = document.createElement('span');
      nm.style.cssText = 'font-size:12px;color:var(--txt);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
      nm.textContent = it.name||it.o_name||it.title||it.stream_name||it.fname||'';
      row.appendChild(nm);
      // External player button (only shown if ext player is configured)
      if((localStorage.getItem('ext_player')||'').trim()){
        const extBtn = document.createElement('span');
        extBtn.title = 'Open in external player';
        extBtn.style.cssText = 'font-size:11px;color:var(--txt2);flex-shrink:0;padding:2px 7px;border-radius:4px;background:var(--s4);cursor:pointer;margin-right:3px';
        extBtn.textContent = '\uD83C\uDFAC';
        extBtn.onclick = function(e){ e.stopPropagation(); _gsPlayExt(i); };
        row.appendChild(extBtn);
      }
      const arr = document.createElement('span');
      arr.style.cssText = 'font-size:11px;color:var(--acc);flex-shrink:0;padding:2px 7px;border-radius:4px;background:rgba(59,130,246,.15)';
      arr.textContent = '\u25b6';
      row.appendChild(arr);
      rl.appendChild(row);
    });
  })
  .catch(function(e){
    if(!_gsModal) return;
    if(st) st.textContent='Error: '+e.message;
  });
}

function _gsPickItem(i){
  const rl = document.getElementById('gs-results');
  const it = rl?._gsItems?.[i];
  if(!it) return;
  closeGlobalSearch();
  if(mode !== 'live') switchMode('live', catsCache['live']||[]);
  curCat = {id:'__all__', title:'All Channels'};
  // Set both allItems and filtItems to the full search pool so playItem(i)
  // resolves correctly and the items panel shows all results behind the player.
  allItems = rl._gsItems;
  filtItems = rl._gsItems;
  pIdx = -1;
  document.getElementById('main').classList.add('items-open');
  showT('p-items','t-items');
  mkBcrum('All Channels');
  renderItems(filtItems);
  refreshBtns();
  playItem(i);
}

async function _gsPlayExt(i){
  const rl = document.getElementById('gs-results');
  const it = rl?._gsItems?.[i];
  if(!it) return;
  // Set context so openExternal (defined in download_addon) works correctly —
  // it reads filtItems[i] and curCat, same as normal item row usage.
  curCat = {id:'__all__', title:'All Channels'};
  filtItems = rl._gsItems;
  openExternal(allItems.indexOf(it) >= 0 ? allItems.indexOf(it) : i);
}

function switchMode(m, cats){
  mode=m;
  document.querySelectorAll('.mt').forEach(b=>b.classList.toggle('on',b.dataset.m===m));
  allCats=cats; _activeTag=''; _buildTagBar(cats); filterCats();
  document.getElementById('catlist').scrollTop=0;
}


// ── HIDDEN ITEMS ──────────────────────────────────────────────────────────────
function _hiddenKey(m){ return 'hidden_'+(m||mode)+'_'+_favsPortalKey; }
function loadHidden(m){ try{return new Set(JSON.parse(localStorage.getItem(_hiddenKey(m))||'[]'));}catch(e){return new Set();} }
function saveHidden(s,m){ try{localStorage.setItem(_hiddenKey(m),JSON.stringify([...s]));}catch(e){} }
function _hideItems(items, m){
  const s=loadHidden(m||mode);
  items.forEach(it=>{ const n=it.name||it.o_name||it.fname||it.title||''; if(n) s.add(n); });
  saveHidden(s,m||mode);
}
function _unhideItem(name, m){ const s=loadHidden(m||mode); s.delete(name); saveHidden(s,m||mode); }

// ── HIDDEN CATEGORIES ─────────────────────────────────────────────────────────
function _hiddenCatKey(m){ return 'hidden_cats_'+(m||mode)+'_'+_favsPortalKey; }
function loadHiddenCats(m){
  try{ return new Map(JSON.parse(localStorage.getItem(_hiddenCatKey(m))||'[]')); }
  catch(e){ return new Map(); }
}
function saveHiddenCats(map,m){ try{localStorage.setItem(_hiddenCatKey(m),JSON.stringify([...map]));}catch(e){} }
function _hideCat(cat, m){
  const map=loadHiddenCats(m||mode);
  map.set(String(cat.id||cat.title), cat.title||String(cat.id)||'?');
  saveHiddenCats(map,m||mode);
}
function _unhideCatKey(key, m){ const map=loadHiddenCats(m||mode); map.delete(key); saveHiddenCats(map,m||mode); }

// ── DRAG-TO-REORDER ───────────────────────────────────────────────────────────
function _catOrderKey(m){ return 'cat_order_'+(m||mode)+'_'+_favsPortalKey; }
function loadCatOrder(m){ try{return JSON.parse(localStorage.getItem(_catOrderKey(m))||'null');}catch(e){return null;} }
function saveCatOrder(arr,m){ try{localStorage.setItem(_catOrderKey(m),JSON.stringify(arr));}catch(e){} }

function _itemOrderKey(m,catKey){ return 'item_order_'+(m||mode)+'_'+_favsPortalKey+'_'+(catKey||''); }
function loadItemOrder(m,catKey){ try{return JSON.parse(localStorage.getItem(_itemOrderKey(m,catKey))||'null');}catch(e){return null;} }
function saveItemOrder(arr,m,catKey){ try{localStorage.setItem(_itemOrderKey(m,catKey),JSON.stringify(arr));}catch(e){} }
function _curCatKey(){ return String(curCat?.id||curCat?.title||''); }

function _applyOrder(arr, savedKeys, keyFn){
  if(!savedKeys||!savedKeys.length) return arr;
  const pos=new Map(savedKeys.map((k,i)=>[k,i]));
  const n=savedKeys.length;
  return [...arr].sort((a,b)=>{
    const ai=pos.has(keyFn(a))?pos.get(keyFn(a)):n;
    const bi=pos.has(keyFn(b))?pos.get(keyFn(b)):n;
    return ai-bi;
  });
}

function _reorderByDom(arr, rows, keyFn){
  const pos=new Map(rows.map((r,i)=>[r.dataset.key,i]));
  return [...arr].sort((a,b)=>{
    const ak=keyFn(a), bk=keyFn(b);
    const ai=pos.has(ak)?pos.get(ak):arr.length;
    const bi=pos.has(bk)?pos.get(bk):arr.length;
    return ai-bi;
  });
}

function _initDragSort(container, rowSel, searchEl, onCommit){
  if(container._ds){ container._ds.destroy(); }

  let holdTimer=null, srcRow=null, dropLine=null, lastBefore=undefined, edgeScrollRaf=null;
  let dragActive=false;
  const HOLD_MS=400;
  const EDGE_ZONE=60;
  const EDGE_SPEED=8;

  function liveRows(){ return [...container.querySelectorAll(rowSel)]; }
  function stopEdgeScroll(){ if(edgeScrollRaf){ cancelAnimationFrame(edgeScrollRaf); edgeScrollRaf=null; } }

  // Attached to document at POINTERDOWN time (before browser claims scroll).
  // Only blocks scroll and moves rows once dragActive=true.
  function onTouchMove(e){
    if(!dragActive) return;
    e.preventDefault(); // blocks browser scroll — works because listener was added before gesture started
    const t = e.touches[0];
    if(!t) return;
    moveToY(t.clientY);
  }

  function onPointerMove(e){
    if(!dragActive) return;
    moveToY(e.clientY);
  }

  function moveToY(y){
    stopEdgeScroll();
    const rect = container.getBoundingClientRect();
    const distTop = y - rect.top, distBot = rect.bottom - y;
    if(distTop < EDGE_ZONE || distBot < EDGE_ZONE){
      const dir = distTop < EDGE_ZONE ? -1 : 1;
      const speed = (EDGE_ZONE - (dir===-1 ? distTop : distBot)) / EDGE_ZONE * EDGE_SPEED;
      (function doScroll(){
        container.scrollTop += dir * speed;
        edgeScrollRaf = requestAnimationFrame(doScroll);
      })();
    }

    const candidates = liveRows().filter(r => r !== srcRow && r !== dropLine);
    let before = null;
    for(const r of candidates){
      const rr = r.getBoundingClientRect();
      if(y < rr.top + rr.height * 0.5){ before = r; break; }
    }
    if(before === lastBefore) return;
    lastBefore = before;
    if(before){
      container.insertBefore(dropLine, before);
      container.insertBefore(srcRow, dropLine);
    } else {
      container.appendChild(dropLine);
      container.appendChild(srcRow);
    }
  }

  function removeDragListeners(){
    document.removeEventListener('touchmove',    onTouchMove);
    document.removeEventListener('touchend',     onEnd);
    document.removeEventListener('touchcancel',  onEnd);
    document.removeEventListener('pointermove',  onPointerMove);
    document.removeEventListener('pointerup',    onEnd);
    document.removeEventListener('pointercancel',onEnd);
  }

  function onEnd(){
    clearTimeout(holdTimer); holdTimer = null;
    stopEdgeScroll();
    removeDragListeners();
    dragActive = false;
    container.style.userSelect = '';
    if(!srcRow) return;
    srcRow.querySelector('.drag-ind')?.remove();
    srcRow.classList.remove('drag-src');
    if(dropLine?.parentNode) dropLine.parentNode.removeChild(dropLine);
    dropLine = null; lastBefore = undefined;
    onCommit(liveRows());
    srcRow = null;
  }

  function onDown(e){
    if(e.button && e.button !== 0) return;
    if(e.target.closest('button,input,a,label,select')) return;
    if(searchEl && searchEl.value.trim()) return;
    const row = e.target.closest(rowSel);
    if(!row || !row.dataset.key) return;

    // Attach listeners to document NOW — before browser decides this is a scroll.
    // touchmove is {passive:false} so we can call preventDefault once drag is confirmed.
    document.addEventListener('touchmove',    onTouchMove,  {passive:false});
    document.addEventListener('touchend',     onEnd,        {passive:true});
    document.addEventListener('touchcancel',  onEnd,        {passive:true});
    document.addEventListener('pointermove',  onPointerMove,{passive:true});
    document.addEventListener('pointerup',    onEnd,        {passive:true});
    document.addEventListener('pointercancel',onEnd,        {passive:true});

    holdTimer = setTimeout(()=>{
      holdTimer = null;
      srcRow = row;
      dragActive = true;
      row.classList.add('drag-src');
      if(!row.querySelector('.drag-ind')){
        const ind = document.createElement('span');
        ind.className = 'drag-ind'; ind.textContent = '↕';
        row.appendChild(ind);
      }
      dropLine = document.createElement('div');
      dropLine.className = 'drag-dropline';
      container.style.userSelect = 'none';
    }, HOLD_MS);
  }

  container.addEventListener('pointerdown', onDown);

  container._ds={
    destroy(){
      clearTimeout(holdTimer); stopEdgeScroll(); removeDragListeners();
      container.removeEventListener('pointerdown', onDown);
      container.style.userSelect = '';
      container._ds = null;
    }
  };
}

// ── HIDE SELECTED ─────────────────────────────────────────────────────────────
function hideSelectedAll(){
  const nItems=selSet.size, nCats=selCats.size;
  if(!nItems && !nCats) return;
  if(nCats) selCats.forEach(cat=>_hideCat(cat, mode));
  if(nItems) _hideItems([...selSet], mode);
  selCats.clear();
  selSet.clear();
  document.querySelectorAll('.cat-chk').forEach(c=>c.checked=false);
  document.querySelectorAll('.ichk').forEach(c=>c.checked=false);
  filterCats();
  _doFilterItems();
  refreshBtns();
  const parts=[];
  if(nCats) parts.push(nCats+' categor'+(nCats===1?'y':'ies'));
  if(nItems) parts.push(nItems+' item'+(nItems===1?'':'s'));
  toast('🚫 Hidden '+parts.join(' + '),'info');
}

function _refreshExportBtn(){
  const n=selSet.size, nc=selCats.size, total=n+nc, ff=CFG.ffmpeg_ok;
  const m3uBtn=document.getElementById('adr-dlm3u');
  const mkvBtn=document.getElementById('adr-dlmkv');
  if(m3uBtn) m3uBtn.disabled=total===0;
  if(mkvBtn){mkvBtn.disabled=total===0||!ff; if(!ff) mkvBtn.title='ffmpeg not found';}
  const sub=document.getElementById('adr-m3u-sub');
  const mkvSub=document.getElementById('adr-mkv-sub');
  const parts=[];
  if(nc) parts.push(nc+' cat'+(nc===1?'':'s'));
  if(n)  parts.push(n+' item'+(n===1?'':'s'));
  const label=parts.join(' + ');
  if(sub) sub.textContent=label;
  if(mkvSub) mkvSub.textContent=label;
}

function _refreshHideBtn(){
  const n=selSet.size, nc=selCats.size, total=n+nc;
  const hideBtn=document.getElementById('adr-hide-sel');
  if(hideBtn) hideBtn.disabled=total===0;
  const hideSub=document.getElementById('adr-hide-sub');
  if(!hideSub) return;
  const parts=[];
  if(nc) parts.push(nc+' cat'+(nc===1?'':'s'));
  if(n)  parts.push(n+' item'+(n===1?'':'s'));
  hideSub.textContent=parts.join(' + ');
}

function _updateHiddenCount(){
  const iCnt=loadHidden(mode).size;
  const cCnt=loadHiddenCats(mode).size;
  const total=iCnt+cCnt;
  const txt=total?(iCnt?' '+iCnt+' item'+(iCnt===1?'':'s'):'')+(cCnt?' '+cCnt+' cat'+(cCnt===1?'':'s'):''):'';
  const el=document.getElementById('adr-hidden-count');
  if(el) el.textContent=total?txt.trim()+' hidden':'';
}

// ── HIDDEN ITEMS MANAGER ──────────────────────────────────────────────────────
let _hmMode='live';
let _hmSubView='items';

function openHiddenManager(){
  _hmMode=mode;
  _hmSubView=document.getElementById('main').classList.contains('items-open')?'items':'cats';
  _hmRender();
  document.getElementById('hidden-overlay').style.display='flex';
}
function closeHiddenManager(){
  document.getElementById('hidden-overlay').style.display='none';
}
function hmSetMode(m){ _hmMode=m; _hmRender(); }
function hmSetSubView(v){ _hmSubView=v; _hmRender(); }

function _hmRender(){
  ['live','vod','series'].forEach(t=>{
    const btn=document.getElementById('hm-tab-'+t); if(!btn) return;
    const on=t===_hmMode;
    btn.style.fontWeight=on?'800':'500';
    btn.style.background=on?'rgba(255,255,255,.08)':'transparent';
    btn.style.borderColor=on?'var(--acc)':'';
    btn.style.color=on?'var(--txt)':'var(--txt2)';
  });
  ['items','cats'].forEach(v=>{
    const btn=document.getElementById('hm-sub-'+v); if(!btn) return;
    const on=v===_hmSubView;
    btn.style.fontWeight=on?'700':'400';
    btn.style.background=on?'rgba(255,255,255,.08)':'transparent';
    btn.style.borderColor=on?'var(--acc)':'var(--bdr)';
    btn.style.color=on?'var(--txt)':'var(--txt2)';
  });
  const list=document.getElementById('hm-list');
  const cntEl=document.getElementById('hm-count');
  const clearBtn=document.getElementById('hm-clear-btn');
  if(_hmSubView==='cats'){
    const map=loadHiddenCats(_hmMode);
    const entries=[...map.entries()].sort((a,b)=>a[1].localeCompare(b[1]));
    if(cntEl) cntEl.textContent=entries.length?entries.length+' categor'+(entries.length===1?'y':'ies')+' hidden':'No hidden categories';
    if(clearBtn) clearBtn.style.display=entries.length?'':'none';
    if(!entries.length){ list.innerHTML='<div style="text-align:center;padding:28px 16px;color:var(--txt3);font-size:12px">No hidden categories for this mode</div>'; list._hmData=[]; return; }
    list._hmData=entries;
    list.innerHTML=entries.map(([key,title],i)=>
      `<div style="display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.04)">
        <span style="font-size:13px;flex-shrink:0">\uD83D\uDCC1</span>
        <span style="flex:1;font-size:12px;color:var(--txt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(title)}">${esc(title)}</span>
        <button class="btn-ghost" style="flex-shrink:0;height:26px;padding:0 10px;font-size:11px" onclick="hmUnhideCat(${i})">Unhide</button>
      </div>`
    ).join('');
    return;
  }
  const s=loadHidden(_hmMode);
  const names=[...s].sort((a,b)=>a.localeCompare(b));
  if(cntEl) cntEl.textContent=names.length?names.length+' item'+(names.length===1?'':'s')+' hidden':'No hidden items';
  if(clearBtn) clearBtn.style.display=names.length?'':'none';
  if(!names.length){ list.innerHTML='<div style="text-align:center;padding:28px 16px;color:var(--txt3);font-size:12px">No hidden items for this mode</div>'; list._hmData=[]; return; }
  list._hmData=names;
  list.innerHTML=names.map((n,i)=>
    `<div style="display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.04)">
      <span style="flex:1;font-size:12px;color:var(--txt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(n)}">${esc(n)}</span>
      <button class="btn-ghost" style="flex-shrink:0;height:26px;padding:0 10px;font-size:11px" onclick="hmUnhide(${i})">Unhide</button>
    </div>`
  ).join('');
}

function hmUnhide(i){
  const list=document.getElementById('hm-list');
  const names=list._hmData||[]; if(!names[i]) return;
  _unhideItem(names[i],_hmMode); _hmRender(); _updateHiddenCount();
  if(_hmMode===mode){ _doFilterItems(); refreshBtns(); }
  toast('\u2713 Unhidden: '+names[i],'ok');
}
function hmUnhideCat(i){
  const list=document.getElementById('hm-list');
  const entries=list._hmData||[]; if(!entries[i]) return;
  const [key,title]=entries[i];
  _unhideCatKey(key,_hmMode); _hmRender(); _updateHiddenCount();
  if(_hmMode===mode) filterCats();
  toast('\u2713 Unhidden category: '+title,'ok');
}
function hmClearAll(){
  if(_hmSubView==='cats'){
    saveHiddenCats(new Map(),_hmMode);
    if(_hmMode===mode) filterCats();
    toast('\u2713 All hidden categories cleared','ok');
  } else {
    saveHidden(new Set(),_hmMode);
    if(_hmMode===mode){ _doFilterItems(); refreshBtns(); }
    toast('\u2713 All hidden items cleared','ok');
  }
  _hmRender(); _updateHiddenCount();
}

// ── Tag groups ────────────────────────────────────────────────────────────────
// When a country tag is selected, also show categories tagged with regional blocs
// that the country belongs to. Keys are every alias for that country — value is a
// Set of all tags whose categories should appear when that key is active.
const _EXYU_EXTRA  = new Set(['EXYU','EX-YU','EXUSSR','BALK','BALKAN']);
const _TAG_GROUPS  = (function(){
  const g = {};
  // Ex-Yugoslavia countries: RS SR SRB / HR CRO / BA BOS / SI SLO / ME MNE / MK MKD
  for(const t of ['RS','SR','SRB']) g[t] = new Set([...['RS','SR','SRB'], ..._EXYU_EXTRA]);
  for(const t of ['HR','CRO'])      g[t] = new Set([...['HR','CRO'],      ..._EXYU_EXTRA]);
  for(const t of ['BA','BOS'])      g[t] = new Set([...['BA','BOS'],      ..._EXYU_EXTRA]);
  for(const t of ['SI','SLO'])      g[t] = new Set([...['SI','SLO'],      ..._EXYU_EXTRA]);
  for(const t of ['ME','MNE'])      g[t] = new Set([...['ME','MNE'],      ..._EXYU_EXTRA]);
  for(const t of ['MK','MKD'])      g[t] = new Set([...['MK','MKD'],      ..._EXYU_EXTRA]);
  // UK aliases all match each other
  for(const t of ['GB','UK','GBR','ENG','SCO','WAL','IRL'])
    g[t] = new Set(['GB','UK','GBR','ENG','SCO','WAL','IRL']);
  return g;
})();

function filterCats(){
  const q=document.getElementById('csrch').value.toLowerCase();
  const tag=_activeTag;
  const _hiddenCats=loadHiddenCats(mode);
  let cats=allCats.filter(c=>!_hiddenCats.has(String(c.id||c.title)));
  // Pull out the All Channels entry — it will always be pinned first
  const allEntry = cats.find(c=>c.id==='__all__');
  let rest = cats.filter(c=>c.id!=='__all__');
  if(tag){
    // Expand tag to its group — e.g. RS/SR/SRB also shows EXYU/BALK categories
    const matchTags = _TAG_GROUPS[tag] || new Set([tag]);
    rest=rest.filter(c=>matchTags.has(_catTag(c.title)));
  }
  if(q)   rest=rest.filter(c=>c.title.toLowerCase().includes(q));
  // Apply custom order only when not actively searching/tag-filtering
  if(!q&&!tag){
    const order=loadCatOrder(mode);
    if(order) rest=_applyOrder(rest, order, c=>String(c.id||c.title));
  }
  // Always prepend All Channels at the top
  renderCats(allEntry ? [allEntry,...rest] : rest);
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
  'EU',                                        // European Union
  'EXYU','EXUSSR',                             // Former Yugoslavia / Soviet bloc
  'ASIA',                                      // Asia regional
  'AFR',                                       // Africa regional
  'ARB','ARAB','MENA',                         // Arab world / Middle East & North Africa
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
  'ARM','ALB','BOS','MNE','SRB','MKD','CRO','SLO','BUL',
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

  // Pipe/colon separator — catches all variants:
  // |US| FREE TO AIR, US| Sports, US | News, US: Movies
  // Optional leading | handles the |TAG| style; \s* allows zero or more spaces before separator.
  let m = normalised.match(/^[|]?([A-Z0-9/]{2,12})\s*[|:]\s*\S/i);
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

// ── Locale → country tag mapping ─────────────────────────────────────────────
// Derives the user's local country tag from browser locale + timezone.
// Returns a list of candidate tag codes in order of specificity (most specific first).
// We return multiple candidates because IPTV portals may use ISO-2 (RS), local abbrev
// (SR, SRB), or regional bloc (EXYU, BALK) for the same country.
const _LOCALE_TAG_CANDIDATES = (function(){
  // Step 1: get ISO-2 from navigator.language  ("sr-RS" → "RS", "en-US" → "US")
  const lang = (navigator.language || '').toUpperCase();
  const fromLang = lang.includes('-') ? lang.split('-').pop() : '';

  // Step 2: derive from IANA timezone  ("Europe/Belgrade" → "RS")
  const TZ_MAP = {
    // ── Europe ───────────────────────────────────────────────────────────────
    'europe/belgrade':'RS','europe/sarajevo':'BA','europe/zagreb':'HR',
    'europe/ljubljana':'SI','europe/skopje':'MK','europe/podgorica':'ME',
    'europe/tirane':'AL',
    'europe/london':'GB','europe/dublin':'IE','europe/isle_of_man':'GB','europe/jersey':'GB','europe/guernsey':'GB',
    'europe/paris':'FR','europe/berlin':'DE','europe/rome':'IT',
    'europe/madrid':'ES','europe/lisbon':'PT',
    'europe/amsterdam':'NL','europe/brussels':'BE',
    'europe/warsaw':'PL','europe/prague':'CZ','europe/bratislava':'SK',
    'europe/budapest':'HU','europe/bucharest':'RO','europe/sofia':'BG',
    'europe/athens':'GR','europe/nicosia':'CY',
    'europe/vienna':'AT','europe/zurich':'CH',
    'europe/stockholm':'SE','europe/oslo':'NO','europe/copenhagen':'DK',
    'europe/helsinki':'FI','atlantic/reykjavik':'IS',
    'europe/moscow':'RU','europe/kyiv':'UA','europe/kiev':'UA','europe/minsk':'BY',
    'europe/chisinau':'MD','europe/tiraspol':'MD',
    'europe/riga':'LV','europe/tallinn':'EE','europe/vilnius':'LT',
    'europe/istanbul':'TR','europe/ankara':'TR',
    'europe/kaliningrad':'RU','europe/samara':'RU','europe/volgograd':'RU',
    'europe/saratov':'RU','europe/ulyanovsk':'RU','europe/astrakhan':'RU',
    'europe/luxembourg':'LU','europe/monaco':'MC','europe/andorra':'AD',
    'europe/valletta':'MT','europe/san_marino':'IT','europe/vatican':'IT',
    'europe/tallinn':'EE','europe/mariehamn':'FI',
    'atlantic/azores':'PT','atlantic/madeira':'PT','atlantic/canary':'ES',
    'atlantic/faroe':'DK',
    // ── Asia ─────────────────────────────────────────────────────────────────
    'asia/dubai':'AE','asia/abu_dhabi':'AE',
    'asia/riyadh':'SA','asia/jeddah':'SA',
    'asia/kuwait':'KW','asia/qatar':'QA','asia/bahrain':'BH','asia/muscat':'OM',
    'asia/baghdad':'IQ','asia/tehran':'IR',
    'asia/jerusalem':'IL','asia/tel_aviv':'IL',
    'asia/beirut':'LB','asia/damascus':'SY',
    'asia/amman':'JO','asia/nicosia':'CY',
    'asia/karachi':'PK','asia/lahore':'PK',
    'asia/kolkata':'IN','asia/calcutta':'IN','asia/mumbai':'IN',
    'asia/dhaka':'BD','asia/colombo':'LK','asia/kathmandu':'NP',
    'asia/kabul':'AF',
    'asia/tashkent':'UZ','asia/samarkand':'UZ',
    'asia/almaty':'KZ','asia/qyzylorda':'KZ','asia/aqtau':'KZ','asia/aqtobe':'KZ','asia/oral':'KZ',
    'asia/ashgabat':'TM','asia/dushanbe':'TJ','asia/bishkek':'KG',
    'asia/tbilisi':'GE','asia/yerevan':'AM','asia/baku':'AZ',
    'asia/tokyo':'JP','asia/seoul':'KR','asia/pyongyang':'KP',
    'asia/shanghai':'CN','asia/chongqing':'CN','asia/harbin':'CN','asia/urumqi':'CN',
    'asia/hong_kong':'HK','asia/taipei':'TW','asia/singapore':'SG',
    'asia/kuala_lumpur':'MY','asia/kuching':'MY',
    'asia/jakarta':'ID','asia/makassar':'ID','asia/jayapura':'ID',
    'asia/manila':'PH','asia/bangkok':'TH','asia/vientiane':'LA',
    'asia/ho_chi_minh':'VN','asia/hanoi':'VN',
    'asia/yangon':'MM','asia/phnom_penh':'KH','asia/ulaanbaatar':'MN',
    'asia/brunei':'BN','asia/dili':'TL',
    'indian/maldives':'MV','indian/mauritius':'MU',
    // ── Pacific / Oceania ─────────────────────────────────────────────────────
    'pacific/auckland':'NZ','pacific/chatham':'NZ',
    'australia/sydney':'AU','australia/melbourne':'AU','australia/brisbane':'AU',
    'australia/adelaide':'AU','australia/darwin':'AU','australia/perth':'AU',
    'australia/hobart':'AU','australia/lord_howe':'AU',
    'pacific/honolulu':'US','pacific/johnston':'US',
    'pacific/guam':'US','pacific/saipan':'US',
    'pacific/port_moresby':'PG','pacific/fiji':'FJ',
    // ── Americas ─────────────────────────────────────────────────────────────
    'america/new_york':'US','america/los_angeles':'US','america/chicago':'US',
    'america/denver':'US','america/phoenix':'US','america/anchorage':'US',
    'america/adak':'US','america/juneau':'US','america/sitka':'US','america/nome':'US',
    'america/boise':'US','america/detroit':'US','america/kentucky/louisville':'US',
    'america/kentucky/monticello':'US','america/indiana/indianapolis':'US',
    'america/indiana/vincennes':'US','america/indiana/winamac':'US',
    'america/indiana/marengo':'US','america/indiana/tell_city':'US',
    'america/indiana/vevay':'US','america/north_dakota/center':'US',
    'america/north_dakota/new_salem':'US','america/north_dakota/beulah':'US',
    'america/puerto_rico':'US','america/virgin':'US',
    'america/toronto':'CA','america/vancouver':'CA','america/montreal':'CA',
    'america/winnipeg':'CA','america/edmonton':'CA','america/halifax':'CA',
    'america/st_johns':'CA','america/regina':'CA','america/whitehorse':'CA',
    'america/yellowknife':'CA','america/dawson':'CA','america/iqaluit':'CA',
    'america/mexico_city':'MX','america/tijuana':'MX','america/monterrey':'MX',
    'america/merida':'MX','america/chihuahua':'MX','america/hermosillo':'MX',
    'america/mazatlan':'MX','america/cancun':'MX','america/ojinaga':'MX',
    'america/bogota':'CO','america/lima':'PE',
    'america/santiago':'CL','america/buenos_aires':'AR','america/argentina/buenos_aires':'AR',
    'america/argentina/cordoba':'AR','america/argentina/salta':'AR',
    'america/sao_paulo':'BR','america/manaus':'BR','america/belem':'BR',
    'america/fortaleza':'BR','america/recife':'BR','america/noronha':'BR',
    'america/cuiaba':'BR','america/porto_velho':'BR','america/boa_vista':'BR',
    'america/caracas':'VE','america/havana':'CU',
    'america/lima':'PE','america/la_paz':'BO','america/asuncion':'PY',
    'america/montevideo':'UY','america/guayaquil':'EC','america/guyana':'GY',
    'america/paramaribo':'SR','america/cayenne':'GF',
    'america/panama':'PA','america/costa_rica':'CR','america/managua':'NI',
    'america/tegucigalpa':'HN','america/el_salvador':'SV','america/guatemala':'GT',
    'america/belize':'BZ','america/nassau':'BS','america/kingston':'JM',
    'america/port-au-prince':'HT','america/santo_domingo':'DO',
    'america/port_of_spain':'TT','america/barbados':'BB','america/curacao':'CW',
    // ── Africa ─────────────────────────────────────────────────────────────
    'africa/cairo':'EG','africa/johannesburg':'ZA','africa/lagos':'NG',
    'africa/nairobi':'KE','africa/casablanca':'MA','africa/tunis':'TN',
    'africa/algiers':'DZ','africa/accra':'GH','africa/addis_ababa':'ET',
    'africa/dakar':'SN','africa/abidjan':'CI','africa/douala':'CM',
    'africa/kinshasa':'CD','africa/brazzaville':'CG','africa/luanda':'AO',
    'africa/maputo':'MZ','africa/harare':'ZW','africa/lusaka':'ZM',
    'africa/dar_es_salaam':'TZ','africa/kampala':'UG','africa/kigali':'RW',
    'africa/bujumbura':'BI','africa/lilongwe':'MW','africa/windhoek':'NA',
    'africa/gaborone':'BW','africa/mbabane':'SZ','africa/maseru':'LS',
    'africa/tripoli':'LY','africa/khartoum':'SD','africa/juba':'SS',
    'africa/ndjamena':'TD','africa/niamey':'NE','africa/bamako':'ML',
    'africa/ouagadougou':'BF','africa/conakry':'GN','africa/freetown':'SL',
    'africa/monrovia':'LR','africa/bissau':'GW','africa/banjul':'GM',
    'africa/nouakchott':'MR','africa/el_aaiun':'EH','africa/lome':'TG',
    'africa/porto-novo':'BJ','africa/libreville':'GA','africa/malabo':'GQ',
    'africa/sao_tome':'ST','africa/djibouti':'DJ','africa/asmara':'ER',
    'africa/mogadishu':'SO','africa/antananarivo':'MG',
    'indian/reunion':'RE','indian/comoro':'KM',
  };
  const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone || '').toLowerCase();
  const fromTZ = TZ_MAP[tz] || '';

  // Step 3: build candidate list — ISO code + common IPTV aliases
  // Ex-YU countries include EXYU so their local tag detection also tries EXYU
  const ALIASES = {
    'RS':['RS','SR','SRB','EXYU','BALK'],'BA':['BA','BOS','EXYU','BALK'],
    'HR':['HR','CRO','EXYU','BALK'],'SI':['SI','SLO','EXYU','BALK'],
    'ME':['ME','MNE','EXYU','BALK'],'MK':['MK','MKD','EXYU','BALK'],
    'AL':['AL','ALB','BALK'],
    'GB':['GB','UK','GBR','ENG','SCO','WAL','IRL'],
    'DE':['DE','GER'],'FR':['FR','FRA'],'IT':['IT','ITA'],
    'ES':['ES','ESP'],'PT':['PT','POR'],'NL':['NL','NED'],
    'BE':['BE','BEL'],'PL':['PL','POL'],'CZ':['CZ','CZE'],
    'SK':['SK','SVK'],'HU':['HU','HUN'],'RO':['RO','ROM'],
    'BG':['BG','BUL'],'GR':['GR','GRE'],'TR':['TR','TUR'],
    'RU':['RU','RUS'],'UA':['UA','UKR'],'BY':['BY','BLR'],
    'MD':['MD','MOL'],'LV':['LV','LAT'],'LT':['LT','LIT'],'EE':['EE','EST'],
    'SE':['SE','SWE'],'NO':['NO','NOR'],'DK':['DK','DEN'],
    'FI':['FI','FIN'],'IS':['IS','ICE'],
    'AT':['AT','AUT'],'CH':['CH','SUI'],
    'US':['US','USA'],'CA':['CA','CAN'],'AU':['AU','AUS'],
    'NZ':['NZ'],'IE':['IE','IRL'],
    'BR':['BR','BRA'],'AR':['AR','ARG'],'MX':['MX','MEX'],
    'CO':['CO','COL'],'PE':['PE','PER'],'CL':['CL','CHI'],
    'UY':['UY','URU'],'VE':['VE'],'BO':['BO'],'PY':['PY'],
    'IN':['IN','IND'],'PK':['PK','PAK'],'BD':['BD','BAN'],
    'LK':['LK','SRI'],'NP':['NP','NEP'],'AF':['AF','AFG'],
    'JP':['JP','JAP'],'KR':['KR','KOR'],'CN':['CN','CHN'],
    'TH':['TH','THAI'],'VN':['VN','VIET'],'ID':['ID','INDO'],
    'MY':['MY','MALAY'],'PH':['PH','PHI'],'SG':['SG','SING'],
    'HK':['HK','HKG'],'TW':['TW','TWN'],'MN':['MN'],
    'KZ':['KZ','KAZ'],'UZ':['UZ','UZB'],'GE':['GE','GEO'],
    'AM':['AM','ARM'],'AZ':['AZ','AZE'],
    'IR':['IR','IRN','IRAN'],'IQ':['IQ','IRAQ'],
    'SA':['SA','SAU'],'AE':['AE','UAE'],'EG':['EG','EGY'],
    'KW':['KW','KUW'],'QA':['QA','QAT'],'BH':['BH','BAH'],
    'OM':['OM','OMN'],'YE':['YE','YEM'],
    'JO':['JO','JOR'],'LB':['LB','LEB'],'SY':['SY','SYR'],
    'IL':['IL'],'PS':['PS','PAL'],
    'MA':['MA','MAR'],'DZ':['DZ','ALG'],'TN':['TN','TUN'],
    'LY':['LY','LIB'],'SD':['SD'],'ET':['ET','ETH'],
    'NG':['NG','NIG'],'GH':['GH'],'KE':['KE','KEN'],
    'ZA':['ZA','ZAF'],'TZ':['TZ'],'SN':['SN','SEN'],
    'CM':['CM','CMR'],'CI':['CI','CIV'],
  };

  const iso = fromTZ || fromLang;
  return iso ? (ALIASES[iso] || [iso]) : [];
})();

function _buildTagBar(cats){
  const bar=document.getElementById('tag-bar');
  if(!bar) return;
  // Raw counts per exact tag
  const rawCounts={};
  cats.forEach(c=>{ const t=_catTag(c.title); if(t) rawCounts[t]=(rawCounts[t]||0)+1; });
  const tags=Object.keys(rawCounts).sort();
  if(!tags.length){ bar.style.display='none'; _activeTag=''; return; }

  // Group-aware counts: for each tag, sum counts of all tags in its group
  // so SR pill shows "SR 15 + EXYU 8 = 23" rather than just "SR 15"
  const counts={};
  for(const t of tags){
    const grp = _TAG_GROUPS[t];
    if(grp){
      counts[t] = tags.filter(x=>grp.has(x)).reduce((s,x)=>s+(rawCounts[x]||0), 0);
    } else {
      counts[t] = rawCounts[t];
    }
  }
  if(!tags.length){ bar.style.display='none'; _activeTag=''; return; }

  const NOT_COUNTRY = new Set(['4K','8K','UHD','FHD','HD','SD','HQ','4G','VIP','FOR','NEW','TOP','HOT','ALL']);
  function isCountryTag(t){
    if(NOT_COUNTRY.has(t)) return false;
    return _KNOWN_TAG_PREFIXES.has(t);
  }

  // ── Priority ordering for country tags ───────────────────────────────────
  // 1. Local tag (first candidate from locale detection that appears in this portal)
  // 2. US, CA, UK/GB (in that order), skipping any already used as local
  // 3. Rest alphabetical
  const PRIORITY_AFTER_LOCAL = ['US','USA','CA','CAN','UK','GB','GBR'];

  const localTag = _LOCALE_TAG_CANDIDATES.find(c => tags.includes(c)) || '';

  function sortedCountryTags(tagList){
    const used = new Set();
    const result = [];
    // Slot 1: local tag
    if(localTag && tagList.includes(localTag)){ result.push(localTag); used.add(localTag); }
    // Slot 2-N: priority tags (skip if same as local)
    for(const p of PRIORITY_AFTER_LOCAL){
      if(tagList.includes(p) && !used.has(p)){ result.push(p); used.add(p); }
    }
    // Remaining: alphabetical
    for(const t of tagList){
      if(!used.has(t)) result.push(t);
    }
    return result;
  }

  const countryTags = sortedCountryTags(tags.filter(t => isCountryTag(t)));
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
    html = `<div class="tag-row">${allPill}${(countryTags.length ? countryTags : generalTags).map(pill).join('')}</div>`;
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
    const key=esc(String(c.id||c.title));
    return '<div class="citem" style="--d:'+(Math.min(i,40)*.022)+'s" data-idx="'+i+'" data-key="'+key+'">'
      +'<input class="cat-chk" type="checkbox"'+(sel?' checked':'')
        +' data-idx="'+i+'" onchange="onCatChkIdx('+i+',this.checked)"'
        +' onclick="event.stopPropagation()">'
      +'<span class="c-ico" style="cursor:pointer" onclick="browseIdx('+i+')">📁</span>'
      +'<span class="c-name" style="cursor:pointer" onclick="browseIdx('+i+')">'
        +esc(c.title)+'</span>'
      +'<span class="c-arr" style="cursor:pointer" onclick="browseIdx('+i+')">›</span>'
      +'</div>';
  }).join('');

  // Init drag-sort — disabled while search is active
  _initDragSort(el, '.citem', document.getElementById('csrch'), (orderedRows)=>{
    const newKeys=orderedRows.map(r=>r.dataset.key);
    allCats=_reorderByDom(allCats, orderedRows, c=>String(c.id||c.title));
    saveCatOrder(newKeys, mode);
    // Re-render so data-idx values match new _renderedCats order
    renderCats(allCats.filter(c=>!loadHiddenCats(mode).has(String(c.id||c.title))));
  });
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
function refreshBtns(){
  const n=selSet.size, nc=selCats.size;
  const icnt=document.getElementById('adr-item-count');
  if(icnt) icnt.textContent=n+' selected';
  const ccnt=document.getElementById('adr-cat-count');
  if(ccnt) ccnt.textContent=nc+' selected';
  _refreshExportBtn();
  _refreshHideBtn();
  const total=n+nc;
  const b=document.getElementById('act-tab-badge');
  if(b){b.textContent=total>99?'99+':total; b.classList.toggle('vis',total>0);}
  const pcb=document.getElementById('pctrl-act-badge');
  if(pcb){pcb.textContent=total>99?'99+':total; pcb.style.display=total>0?'':'none';}
  _updateHiddenCount();
}
const refreshCatBtns = refreshBtns;

async function dlSelectedAll(type){
  const nCats=selCats.size, nItems=selSet.size;
  if(!nCats && !nItems){toast('Select categories or items first','wrn');return;}
  if(nCats) await dlSelCats(type);
  if(nItems){
    if(type==='m3u') await dlM3U();
    else await dlMKV();
  }
}
async function dlSelCats(type){
  const cats=[...selCats.values()];
  if(!cats.length){toast('Select categories first','wrn');return;}
  const op=document.getElementById('o-m3u').value.trim();
  const od=document.getElementById('o-dir').value.trim();
  if(type==='m3u'&&!op){toast('Set M3U output path in ⚙ settings','wrn');return;}
  if(type==='mkv'&&!od){toast('Set output folder in ⚙ settings','wrn');return;}
  setBusy(true);
  if(type==='m3u') _showProgressNow('m3u_inline','💾 Saving M3U\u2026',cats.map(c=>c.title).join(', '),0);
  let done=0;
  const _hidden=loadHidden(mode);
  for(const cat of cats){
    setStatus('Downloading cat '+(++done)+'/'+cats.length+': '+cat.title+'…');
    const catKey=_categoryKey(mode, cat);
    const cached=(categoryItemsCache[mode]||{})[catKey];
    let items=null;
    if(cached && cached.length){
      const order=loadItemOrder(mode, String(cat.id||cat.title));
      let ordered=cached;
      if(order) ordered=_applyOrder(cached, order, it=>it.name||it.o_name||it.fname||'');
      items=ordered.filter(it=>!_hidden.has(it.name||it.o_name||it.fname||''));
    }
    const outPath=type==='m3u'?op:(od.replace(/\/?$/,'/')+mode+'_'+cat.title.replace(/[^a-z0-9]/gi,'_')+'.m3u');
    const r=await fetch('/api/download/m3u',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({items,category:cat,mode,out_path:outPath,total_hint:items?items.length:0})});
    const d=await r.json();
    if(!d.ok) toast('Error on '+cat.title+': '+(d.error||'?'),'err');
  }
  toast('Done! '+done+' categories exported','ok');
  pollBusy();
}

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
  function _doFetchItems(attempt){
    fetch('/api/items',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode, category:cat, browse:true})})
    .then(r=>r.json()).then(d=>{
      if(d.error){ _setLoadingHeader(null); setBusy(false); toast(d.error,'err'); setStatus('Error: '+d.error); return; }
      // pending: prefetch still running — show spinner and retry in 2s
      if(d.pending){
        const retryIn = Math.min(2000 * attempt, 8000);
        _setLoadingHeader(`Loading ${esc(cat.title)} — waiting for channel list…`);
        setStatus(`Waiting for channel list (${cat.title})…`);
        setTimeout(()=>{ if(state.connected) _doFetchItems(attempt+1); else{_setLoadingHeader(null);setBusy(false);} }, retryIn);
        return;
      }
      _setLoadingHeader(null);
      allItems = d.items || [];
      // store into cache
      categoryItemsCache[mode][key] = allItems;
      setStatus("'"+cat.title+"' — "+allItems.length+' items');
      showItems(cat.title, allItems);
      setBusy(false);
    }).catch(e=>{
      _setLoadingHeader(null);
      setBusy(false);
      toast(e.message,'err');
    });
  }
  _doFetchItems(1);
}

function showSkels(count=10, small=false){
  document.getElementById('main').classList.add('items-open');
  const cls=small?'skel-sm':'skel';
  // .skel-wave is the GPU-composited shimmer sweep (transform:translateX).
  // Stagger delay applied directly to the wave div — parent animation-delay
  // does not propagate to ::before/::after pseudo-elements.
  const rows = Array.from({length:count},(_,i)=>
    `<div class="${cls}"><div class="skel-wave" style="animation-delay:${(i*0.07).toFixed(2)}s"></div></div>`
  ).join('');
  document.getElementById('ilist').innerHTML=`<div style="padding:4px 0">${rows}</div>`;
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
    const hasCatchup = mode==='live' && playable && _channelSupportsCatchup(it);
    return '<div class="irow'+(playing?' now':'')+'" style="--d:'+(Math.min(i,20)*.016)+'s" data-key="'+esc(name)+'">'
      +'<input class="ichk" type="checkbox" data-i="'+i+'" onchange="onChk('+i+',this.checked)">'
      +(logoSrc?'<img class="ilogo" loading="lazy" src="'+esc(logoSrc)+'" onerror="this.style.display=\'none\'">'+'':'<span style="width:36px;height:24px;flex-shrink:0;display:inline-block"></span>')
      +'<button onclick="toggleFav('+i+')" title="Favourite"'
      +' style="background:none;border:none;cursor:pointer;font-size:15px;padding:0 2px;line-height:1;flex-shrink:0;color:'+(isFav(it)?'#f5c518':'rgba(255,255,255,0.25)')+'" >★</button>'
      +'<span class="iname"><span class="iname-inner">'+esc(name)+'</span></span>'
      +'<div class="ibtns">'
        +(grp?'<button class="btn-ghost" onclick="drillGrp('+i+')">'+epN+' eps</button>':'')
        +(show&&isSeries?'<button class="btn-ghost" onclick="drillShow('+i+')">Eps</button>':'')
        +(hasCatchup?'<button class="btn-ghost" onclick="event.stopPropagation();openCatchupInRow('+i+')" title="Catch-up TV" style="padding:0 7px;font-size:14px">↺</button>':'')
        +(playable?'<button class="btn-blue" onclick="playItem('+i+')">▶</button>':'')
        +'<button class="btn-ghost imenu-trigger" onclick="event.stopPropagation();openItemMenu('+i+',this)" title="More options" style="padding:0 6px;font-size:18px;line-height:1;letter-spacing:0">⋮</button>'
      +'</div></div>';
  }

  el.innerHTML = items.slice(0, _ITEMS_BATCH).map(buildRow).join('');
  refreshBtns();
  _updateEpgGridBtn();

  // Init drag-sort on the container (event-delegated, handles lazy-appended rows too)
  _initDragSort(el, '.irow', document.getElementById('isrch'), (orderedRows)=>{
    const catKey=_curCatKey();
    const newKeys=orderedRows.map(r=>r.dataset.key);
    const keyFn=it=>it.name||it.o_name||it.fname||'';
    filtItems=_reorderByDom(filtItems, orderedRows, keyFn);
    allItems =_reorderByDom(allItems,  orderedRows, keyFn);
    saveItemOrder(newKeys, mode, catKey);
    // Re-render so all onclick indices match new filtItems order
    renderItems(filtItems);
  });

  if(items.length <= _ITEMS_BATCH) return;

  // Scroll-triggered lazy loading via IntersectionObserver.
  // A sentinel div sits at the bottom; only appends next batch when user scrolls near it.
  // This keeps DOM node count low regardless of how many items are in the array.
  let offset = _ITEMS_BATCH;
  const sentinel = document.createElement('div');
  sentinel.style.cssText = 'height:1px;flex-shrink:0';
  el.appendChild(sentinel);

  const obs = new IntersectionObserver(function(entries){
    if(_renderToken !== token){ obs.disconnect(); sentinel.remove(); return; }
    if(!entries[0].isIntersecting) return;
    if(offset >= items.length){ obs.disconnect(); sentinel.remove(); return; }
    const end = Math.min(offset + _ITEMS_BATCH, items.length);
    const tmp = document.createElement('div');
    tmp.innerHTML = items.slice(offset, end).map((it,j) => buildRow(it, offset+j)).join('');
    while(tmp.firstChild) el.insertBefore(tmp.firstChild, sentinel);
    offset = end;
    if(offset >= items.length){ obs.disconnect(); sentinel.remove(); }
  }, {root: el, rootMargin: '200px'});

  obs.observe(sentinel);
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
  document.getElementById('imenu-sep2').style.display     = !grp?'block':'none';
  document.getElementById('imenu-ext').style.display      = !grp&&!show?'flex':'none';
  document.getElementById('imenu-imdb').style.display     = (!isLive&&!grp)?'flex':'none';
  document.getElementById('imenu-rec').style.display      = isLive&&!grp&&!show?'flex':'none';   // live only
  document.getElementById('imenu-mkv').style.display      = !isLive&&!grp&&!show?'flex':'none';   // vod/series playable items only

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
  if(!_channelSupportsCatchup(it)){toast('This channel does not support Catch-up TV','wrn');return;}
  _epgItem = it;
  showCatchup();
}

function openCatchupInRow(i){
  const it = filtItems[i];
  if(!it) return;
  if(!_channelSupportsCatchup(it)){toast('This channel does not support Catch-up TV','wrn');return;}
  _iMenuIdx = i;
  _epgItem = it;
  showCatchup();
}

async function iMenuRec(){
  closeItemMenu();
  const it = filtItems[_iMenuIdx];
  if(!it) return;

  // If DVR addon is available, route through it so the recording is tracked,
  // gets a proper filename, and appears in the DVR completed recordings list.
  if(_DVR_OK){
    const name = it.name || it.o_name || it.fname || 'Recording';
    toast('Resolving stream…', 'info');
    try{
      const r = await fetch('/api/resolve', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({item: it, mode, category: curCat || {}})
      });
      const d = await r.json();
      if(!d.url){ toast(d.error || 'Could not resolve stream URL', 'err'); return; }
      // Strip hls_proxy wrapper — DVR ffmpeg records the raw portal stream.
      // Proxy URLs like /api/hls_proxy?url=... can't be opened by an external ffmpeg process.
      let recUrl = d.url;
      if(recUrl.includes('/api/hls_proxy')){
        try{
          const params = new URLSearchParams(recUrl.split('?')[1]||'');
          recUrl = params.get('url') || recUrl;
        }catch(e){}
      }
      // Use end time from form if available, else default 2h
      const dvrStart = document.getElementById('dvr-start')?.value;
      const dvrEnd   = document.getElementById('dvr-end')?.value;
      let durationMinutes = 120;
      if(dvrStart && dvrEnd){
        const diff = (new Date(dvrEnd) - new Date(dvrStart)) / 60000;
        if(diff > 0) durationMinutes = Math.round(diff);
      }
      const rb = await fetch('/api/dvr/record_now', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          channelId:   it.id || it.stream_id || '',
          channelName: name,
          streamUrl:   recUrl,
          title:       name,
          durationMinutes,
          channelItem: it,
        })
      });
      const rd = await rb.json();
      if(rb.ok){
        toast('⏺ DVR recording started: ' + name, 'ok');
        // Update DVR jobs + badge immediately so the Actions drawer reflects the new recording.
        // Don't wait for the user to open the DVR modal.
        try{
          const j = await fetch('/api/dvr/jobs').then(r=>r.json());
          if(Array.isArray(j)){
            _dvrJobs = j;
            _dvrInited = true;   // mark as initialised so future badge polls work
            _dvrBadgeUpdate();
          }
        }catch(e){}
        if(_dvrInited) dvrRefresh();
      } else {
        toast(rd.error || 'DVR record failed', 'err');
      }
    } catch(e){ toast('Record error: ' + e, 'err'); }
    return;
  }

  // Fallback: quick record (no DVR addon)
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
function iMenuHide(){
  closeItemMenu();
  const it = filtItems[_iMenuIdx];
  if(!it) return;
  const name = it.name||it.o_name||it.fname||'';
  _hideItems([it], mode);
  _doFilterItems();
  refreshBtns();
  _updateHiddenCount();
  toast('🚫 Hidden: '+name,'info');
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
  const _hidden=loadHidden(mode);
  const base=_favsFilterActive
    ? loadFavs(mode).filter(f=>!allItems.length||allItems.some(it=>(it.name||it.o_name||it.fname||'')===(f.name||f.o_name||f.fname||'')))
    : allItems;
  const visible=base.filter(it=>!_hidden.has(it.name||it.o_name||it.fname||''));
  filtItems=q?visible.filter(it=>(it.name||it.o_name||it.fname||'').toLowerCase().includes(q)):[...visible];
  // Apply custom order only when not searching
  if(!q){
    const order=loadItemOrder(mode, _curCatKey());
    if(order) filtItems=_applyOrder(filtItems, order, it=>it.name||it.o_name||it.fname||'');
  }
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
  const _cuSupported=(itemMode==='live')&&_channelSupportsCatchup(it);
  const _cuBtn=document.getElementById('catchupbtn');
  _cuBtn.style.opacity=_cuSupported?'1':'0.35';
  _cuBtn.style.pointerEvents=_cuSupported?'':'none';
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
  if(window._stallWatchdog){ clearInterval(window._stallWatchdog); window._stallWatchdog=null; }
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
  window._hlsManifestRetried = false;                // reset manifest retry flag
  if(window._hlsRetries) window._hlsRetries = {};    // reset HLS retry counter
  window._vodRemuxFired = false;                     // reset VOD remux guard
  window._hlsMediaRecoveries = 0;                    // reset HLS media recovery counter
  window._hlsFreshRestarted = false;                  // reset HLS fresh-restart guard
  window._hlsRecoverySuccessHandler = null;           // reset FRAG_BUFFERED success handler
  // ── Stall watchdog ────────────────────────────────────────────────────────
  // Neither HLS.js nor mpegts.js fires an error when the stream silently
  // freezes mid-playback (currentTime stops advancing but no fatal event).
  // This is the most common symptom on slow portals. The watchdog polls every
  // 6 s: if currentTime hasn't moved and the video isn't intentionally paused,
  // it attempts an unload/load recovery cycle. After 3 failed cycles it gives
  // up so it doesn't loop endlessly on a genuinely offline channel.
  if(window._stallWatchdog) clearInterval(window._stallWatchdog);
  window._stallWatchdog = null;
  window._stallLastTime = -1;
  window._stallHits = 0;
  window._stallWatchdog = setInterval(()=>{
    if(_playerStopped){ clearInterval(window._stallWatchdog); window._stallWatchdog=null; return; }
    if(vid.paused || vid.ended || vid.readyState < 2) return; // intentionally paused or not loaded yet
    const ct = vid.currentTime;
    if(ct === window._stallLastTime && ct > 0){
      window._stallHits++;
      alog('[Watchdog] Stall detected ('+window._stallHits+'/3) — currentTime frozen at '+ct.toFixed(2)+'s','w');
      if(window._stallHits >= 3){
        // Three consecutive 6 s checks frozen — give up, don't loop endlessly
        clearInterval(window._stallWatchdog); window._stallWatchdog=null;
        alog('[Watchdog] Stream appears offline after 3 stall cycles — stopping','e');
        setNP('✗ Stream stalled — channel may be offline');
        document.getElementById('ppbtn').textContent='▶';
        return;
      }
      // Attempt a silent recovery: unload then reload without destroying the player.
      // This re-opens the HTTP connection to the portal without a full page-reload.
      setNP('⟳ Buffering… '+pName);
      if(mpegtsObj){ try{ mpegtsObj.unload(); mpegtsObj.load(); vid.play().catch(()=>{}); }catch(e){} }
      else if(hlsObj){
        try{
          hlsObj.stopLoad();
          // startLoad(-1) jumps to the live edge rather than resuming from the stale position
          hlsObj.startLoad(-1);
          const _lp = hlsObj.liveSyncPosition;
          if(typeof _lp === 'number' && Number.isFinite(_lp) && vid.currentTime < _lp - 2){
            try{ vid.currentTime = Math.max(0, _lp - 1); }catch(e){}
          }
        }catch(e){}
        vid.play().catch(()=>{});
      }
    } else {
      // Time is advancing — reset hit counter
      window._stallHits = 0;
    }
    window._stallLastTime = ct;
  }, 6000);
  // ─────────────────────────────────────────────────────────────────────────
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
               url.includes('/api/hls_proxy')       // server-side transcode proxy
               || url.includes('/api/dvr/timeshift') // DVR timeshift — raw MPEG-TS live stream
               || url.includes('/api/dvr/serve')     // DVR completed recording — MPEG-TS file
               || url.includes('/api/dvr/transcode') // DVR completed recording — transcoded MPEG-TS
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
    // Pre-check response status so we can surface 456/458 in the log the same
    // way HLS and MPEGTS paths do — the video element cannot expose HTTP codes.
    const _dAbrt = new AbortController();
    fetch(px, {signal: _dAbrt.signal}).then(r=>{
      _dAbrt.abort(); // stop body download — we only needed the status
      if(r.status===456){
        alog('[Direct] Wrong location — use a VPN (456)','w');
        setNP('✗ Wrong location — use a VPN');
        document.getElementById('ppbtn').textContent='▶';
        return;
      }
      if(r.status===458){
        alog('[Direct] Max connections already in use (458)','w');
        setNP('✗ Max connections in use');
        document.getElementById('ppbtn').textContent='▶';
        return;
      }
      if(!_playerStopped){ vid.src=px; vid.play().catch(()=>{}); }
    }).catch(()=>{
      // AbortError (from _dAbrt.abort above) should not reach here since we're
      // already inside .then(); any real network error → fall back to direct load.
      if(!_playerStopped){ vid.src=px; vid.play().catch(()=>{}); }
    });

  } else if(isStorageUrl){
    // ── Stalker storage/get.php — direct to video, no proxy ──────
    // Proxying would double-encode the query string (?filename=...&token=...).
    alog('[Storage] Playing direct (no proxy)','k');
    vid.src=url; vid.play().catch(()=>{});

  } else if(isHls && typeof Hls !== 'undefined' && Hls.isSupported()){
    // ── HLS via HLS.js ────────────────────────────────────────
    // Shared HLS config — used in every new Hls() call below.
    // enableWorker:true  → parsing/remux runs off the main thread (less UI jank)
    // maxBufferLength:20 → keeps live stream ≤20s behind edge; 45 caused visible lag/drift
    // backBufferLength:10 → evict old segments quickly; frees memory, reduces MSE pressure
    // liveSyncDurationCount:3 / liveMaxLatencyDurationCount:5 → stay 3-5 segments from edge
    // Shorter retry delays: 6×1500ms = 9s of stalls before escalation; now 4×800ms = 3.2s
    const _HLS_CFG = {
      enableWorker:true, lowLatencyMode:false,
      maxBufferLength:20, maxMaxBufferLength:30,
      backBufferLength:10,
      liveSyncDurationCount:3, liveMaxLatencyDurationCount:5,
      fragLoadingTimeOut:20000, manifestLoadingTimeOut:15000,
      levelLoadingTimeOut:15000,
      fragLoadingMaxRetry:4, fragLoadingRetryDelay:800,
      levelLoadingMaxRetry:3, levelLoadingRetryDelay:800,
      manifestLoadingMaxRetry:3, manifestLoadingRetryDelay:1500,
      xhrSetup(xhr){xhr.withCredentials=false;},
      // Disable HLS.js subtitle track management with full no-op stubs
      // so our own addTextTrack() cues are never touched by HLS internals
      subtitleStreamController: class { startLoad(){}  stopLoad(){}  destroy(){}  onMediaAttached(){}  onMediaDetaching(){}  onManifestLoading(){}  onManifestLoaded(){}  onManifestParsed(){}  onLevelLoaded(){}  onAudioTrackSwitching(){}  onSubtitleFragProcessed(){}  onBufferFlushing(){}  on(){}  off(){} },
      subtitleTrackController:  class { startLoad(){}  stopLoad(){}  destroy(){}  onMediaAttached(){}  onMediaDetaching(){}  onManifestLoading(){}  onManifestLoaded(){}  onManifestParsed(){}  onLevelLoaded(){}  on(){}  off(){} },
    };
    hlsObj=new Hls(_HLS_CFG);
    hlsObj.loadSource(px);
    hlsObj.attachMedia(vid);
    hlsObj.on(Hls.Events.MANIFEST_PARSED,()=>vid.play().catch(()=>{}));
    let _hlsRetryFired = false;
    const hlsErrorHandler = (_,data)=>{
      const _det=(data.details||'').toLowerCase();
      const _isManifest=_det.includes('manifest');
      // Log all fatal errors and manifest errors
      if(data.fatal || _isManifest) alog('[HLS] '+data.type+': '+data.details+(data.fatal?' (fatal)':' (non-fatal)'),'e');
      // Non-fatal errors (e.g. manifestParsingError, network hiccups) — HLS.js
      // handles these internally; do NOT destroy or retry from here.
      if(!data.fatal) return;
      // 503/403/404 — hard stop immediately, no retry
      const hc=data?.response?.code||0;
      if(hc===456){
        alog('[HLS] Wrong location — use a VPN (456)','w');
        setNP('✗ Wrong location — use a VPN');
        document.getElementById('ppbtn').textContent='▶';
        if(hlsObj){hlsObj.destroy();hlsObj=null;}
        return;
      }
      if(hc===458){
        alog('[HLS] Max connections already in use (458)','w');
        setNP('✗ Max connections in use');
        document.getElementById('ppbtn').textContent='▶';
        if(hlsObj){hlsObj.destroy();hlsObj=null;}
        return;
      }
      if(hc===503||hc===403||hc===404){
        alog('[HLS] Channel unavailable ('+hc+') — stopping','e');
        setNP('✗ Channel unavailable ('+hc+')');
        document.getElementById('ppbtn').textContent='▶';
        if(hlsObj){hlsObj.destroy();hlsObj=null;}
        return;
      }
      // Fatal error — first try: recreate HLS instance (portal may have hiccuped)
      if(!_hlsRetryFired && !_playerStopped){
        _hlsRetryFired = true;
        alog('[HLS] Fatal error — retrying with fresh HLS instance…','w');
        setNP('⟳ Stream hiccup — retrying: '+name+'…');
        if(hlsObj){hlsObj.destroy();hlsObj=null;}
        vid.pause(); vid.removeAttribute('src'); vid.load();
        setTimeout(()=>{
          if(_playerStopped) return;
          _playerStopped = false;
          const _retryHls = new Hls(_HLS_CFG);
          hlsObj = _retryHls;
          _retryHls.loadSource(px);
          _retryHls.attachMedia(vid);
          _retryHls.on(Hls.Events.MANIFEST_PARSED,()=>vid.play().catch(()=>{}));
          _retryHls.on(Hls.Events.ERROR,(_,d2)=>{
            if(!d2.fatal) return;
            if(!_playerStopped && !window._remuxFired){
              window._remuxFired = true;
              if(hlsObj){hlsObj.destroy();hlsObj=null;}
              // Catchup/VOD: HLS failed (portal doesn't support timeshift.php) —
              // fall back to the .ts path which plays without a seek bar rather
              // than handing off to ffmpeg remux (which would play as live/no-seek anyway).
              if(!isLiveStream && fallbackUrl){
                alog('[HLS] Retry also failed — falling back to .ts (catchup fallback)…','w');
                setTimeout(()=>{
                  if(_playerStopped) return;
                  doPlay(fallbackUrl, name, {isLive:false, durationSecs:opts.durationSecs||0});
                },1000);
                return;
              }
              alog('[HLS] Retry also failed — falling back to ffmpeg remux…','w');
              const remuxUrl='/api/hls_proxy?url='+encodeURIComponent(url);
              setTimeout(()=>{
                if(_playerStopped) return;
                _playerStopped = false;
                setNP('▶ '+name+' [remux]');
                if(typeof mpegts!=='undefined'&&mpegts.isSupported()){
                  mpegtsObj=mpegts.createPlayer({type:'mse',isLive:true,url:remuxUrl,cors:true},{
                    enableWorker:false,liveBufferLatencyChasing:true,
                    liveBufferLatencyMaxLatency:12,liveBufferLatencyMinRemain:3,
                  });
                  mpegtsObj.attachMediaElement(vid); mpegtsObj.load(); vid.play().catch(()=>{});
                } else { vid.src=remuxUrl; vid.play().catch(()=>{}); }
              },3000);
            }
          });
        },2000);
        return;
      }
    };
    hlsObj.on(Hls.Events.ERROR, hlsErrorHandler);
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
    // For catchup streams we know the exact duration from start/stop timestamps.
    // Passing it in the mediaDataSource lets mpegts.js set vid.duration via
    // MediaSource so the seek bar shows a real progress range instead of Infinity.
    const _knownDur = (!isLiveStream && opts.durationSecs > 0) ? opts.durationSecs : undefined;
    mpegtsObj=mpegts.createPlayer({
      type:'mse', isLive: isLiveStream, url:px, cors:true,
      duration: _knownDur,
    },{
      enableWorker:false,
      liveBufferLatencyChasing: isLiveStream,
      // 12 s window matches all remux/fallback paths — avoids the latency-chaser
      // speeding up playback too aggressively when a slow portal delivers bursts.
      liveBufferLatencyMaxLatency: isLiveStream ? 12 : undefined,
      liveBufferLatencyMinRemain: isLiveStream ? 3 : undefined,
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
              // Reuse the shared _HLS_CFG from the outer doPlay scope if available,
              // otherwise define equivalent settings inline for this fallback path.
              const _fbCfg = (typeof _HLS_CFG !== 'undefined') ? _HLS_CFG : {
                enableWorker:true, lowLatencyMode:false,
                maxBufferLength:20, maxMaxBufferLength:30, backBufferLength:10,
                liveSyncDurationCount:3, liveMaxLatencyDurationCount:5,
                fragLoadingTimeOut:20000, manifestLoadingTimeOut:15000, levelLoadingTimeOut:15000,
                fragLoadingMaxRetry:4, fragLoadingRetryDelay:800,
                levelLoadingMaxRetry:3, levelLoadingRetryDelay:800,
                manifestLoadingMaxRetry:3, manifestLoadingRetryDelay:1500,
                xhrSetup(xhr){xhr.withCredentials=false;},
                subtitleStreamController: class { startLoad(){}  stopLoad(){}  destroy(){}  onMediaAttached(){}  onMediaDetaching(){}  onManifestLoading(){}  onManifestLoaded(){}  onManifestParsed(){}  onLevelLoaded(){}  onAudioTrackSwitching(){}  onSubtitleFragProcessed(){}  onBufferFlushing(){}  on(){}  off(){} },
                subtitleTrackController:  class { startLoad(){}  stopLoad(){}  destroy(){}  onMediaAttached(){}  onMediaDetaching(){}  onManifestLoading(){}  onManifestLoaded(){}  onManifestParsed(){}  onLevelLoaded(){}  on(){}  off(){} },
              };
              hlsObj = new Hls(_fbCfg);
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
                  },3000);  // 3s grace period before ffmpeg connects
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
      if(httpCode===456){
        alog('[MPEGTS] Wrong location — use a VPN (456)','w');
        setNP('✗ Wrong location — use a VPN');
        document.getElementById('ppbtn').textContent='▶';
        return;
      }
      if(httpCode===458){
        alog('[MPEGTS] Max connections already in use (458)','w');
        setNP('✗ Max connections in use');
        document.getElementById('ppbtn').textContent='▶';
        return;
      }
      if(httpCode===503 || httpCode===403 || httpCode===404){
        if(!isLiveStream && fallbackUrl && httpCode===404){
          alog('[MPEGTS] Catchup .ts → 404 — retrying via query-string fallback (HLS.js)','w');
          _destroyPlayers(); doPlay(fallbackUrl, name, {isLive:false}); return;
        }
        alog('[MPEGTS] Channel unavailable ('+httpCode+') — stopping','e');
        setNP('✗ Channel unavailable ('+httpCode+')');
        document.getElementById('ppbtn').textContent='▶';
        return;
      }
      // play_token URLs: re-resolve for fresh token, but cap at 2 retries
      if(isLiveStream && et===mpegts.ErrorTypes.NETWORK_ERROR && hasPlayToken){
        if(!window._ptRetries) window._ptRetries = {};
        const _rk = String(pIdx);
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
          // Reconnect attempts — unload/load on same instance
          setTimeout(()=>{ if(mpegtsObj && !_playerStopped){ mpegtsObj.unload(); mpegtsObj.load(); vid.play().catch(()=>{}); }},2000);
        } else if(window._mpegRetries[_mk] === 4 && !_playerStopped && !window._remuxFired){
          // 3 reconnects failed — do one full fresh-instance restart (same as HLS hiccup retry)
          // before giving up and going to ffmpeg remux. Portal may have dropped the session;
          // a brand-new mpegtsObj gets a clean TCP connection with no stale state.
          alog('[MPEGTS] Reconnects failed — doing fresh restart before remux…','w');
          setNP('⟳ Stream hiccup — retrying: '+name+'…');
          if(mpegtsObj){mpegtsObj.destroy();mpegtsObj=null;}
          vid.pause(); vid.removeAttribute('src'); vid.load();
          setTimeout(()=>{
            if(_playerStopped) return;
            alog('[MPEGTS] Fresh restart attempt…','w');
            _playerStopped = false;
            mpegtsObj=mpegts.createPlayer({
              type:'mse', isLive:true, url:px, cors:true,
            },{
              enableWorker:false,
              liveBufferLatencyChasing:true,
              liveBufferLatencyMaxLatency:12,
              liveBufferLatencyMinRemain:3,
            });
            mpegtsObj.attachMediaElement(vid);
            mpegtsObj.load();
            vid.play().catch(()=>{});
            // If this also errors, _mpegRetries[_mk] will be 5 → falls to remux below
            mpegtsObj.on(mpegts.Events.ERROR,(et2,ed2,ei2)=>{
              if(!_playerStopped){
                alog('[MPEGTS] Fresh restart failed — escalating to remux…','w');
                window._mpegRetries[_mk] = 99; // force past retry threshold on next tick
                // Trigger the same handler logic by synthesising a network error event
                if(mpegtsObj){mpegtsObj.destroy();mpegtsObj=null;}
                vid.pause(); vid.removeAttribute('src'); vid.load();
                if(!window._remuxFired){
                  window._remuxFired = true;
                  const remuxUrl='/api/hls_proxy?url='+encodeURIComponent(url);
                  setTimeout(()=>{
                    if(_playerStopped) return;
                    _playerStopped=false;
                    setNP('▶ '+name+' [remux]');
                    mpegtsObj=mpegts.createPlayer({type:'mse',isLive:true,url:remuxUrl,cors:true},{
                      enableWorker:false,liveBufferLatencyChasing:true,
                      liveBufferLatencyMaxLatency:12,liveBufferLatencyMinRemain:3,
                    });
                    mpegtsObj.attachMediaElement(vid);
                    mpegtsObj.load();
                    vid.play().catch(()=>{});
                    mpegtsObj.on(mpegts.Events.ERROR,(et3,ed3)=>{
                      if(!_playerStopped){
                        alog('[MPEGTS/remux] '+(ed3?.msg||JSON.stringify(ed3)),'e');
                        setNP('✗ Stream unavailable: '+name);
                        document.getElementById('ppbtn').textContent='▶';
                      }
                    });
                  }, 3000);  // 3s grace period before ffmpeg connects
                }
              }
            });
          }, 3000);  // 3s for portal to recover before fresh reconnect
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
          },3000);  // 3s grace period — lets portal release the connection slot before ffmpeg connects
        } else if(!_playerStopped){
          alog('[MPEGTS] Stream failed after retries \u2014 channel may be offline','e');
          setNP('\u2717 Stream unavailable: '+name);
          document.getElementById('ppbtn').textContent='\u25b6';
          window._mpegRetries[_mk]=0;
        }
      } else if(!isLiveStream && fallbackUrl && et===mpegts.ErrorTypes.NETWORK_ERROR){
        // Catchup path-based .ts failed → try query-string format via HLS.js.
        // NOTE: this branch MUST come before the plain !isLiveStream branch below,
        // otherwise the broader condition swallows it and fallbackUrl is never tried.
        alog('[MPEGTS] Catchup .ts failed — retrying with fallback URL via HLS.js','w');
        _destroyPlayers();
        doPlay(fallbackUrl, name, {isLive:false});
      } else if(!isLiveStream && et===mpegts.ErrorTypes.NETWORK_ERROR){
        // VOD network error — try a single ffmpeg remux pass before giving up.
        // This handles container/mux issues that mpegts.js can't recover from but
        // ffmpeg -c copy can (e.g. partial PES, missing keyframe at start).
        if(!url.includes('hls_proxy') && !window._vodRemuxFired){
          window._vodRemuxFired = true;
          alog('[MPEGTS] VOD network error — trying ffmpeg remux…','w');
          const remuxUrl='/api/hls_proxy?url='+encodeURIComponent(url);
          setTimeout(()=>{
            if(_playerStopped) return;
            if(mpegtsObj){mpegtsObj.destroy();mpegtsObj=null;}
            vid.pause(); vid.removeAttribute('src'); vid.load();
            _playerStopped=false;
            setNP('▶ '+name+' [remux]');
            if(typeof mpegts!=='undefined'&&mpegts.isSupported()){
              mpegtsObj=mpegts.createPlayer({type:'mse',isLive:false,url:remuxUrl,cors:true},{
                enableWorker:false,autoCleanupSourceBuffer:true,
              });
              mpegtsObj.attachMediaElement(vid);
              mpegtsObj.load();
              vid.play().catch(()=>{});
              mpegtsObj.on(mpegts.Events.ERROR,(et2,ed2)=>{
                if(!_playerStopped){
                  alog('[MPEGTS/VOD remux] '+(ed2?.msg||JSON.stringify(ed2)),'e');
                  setNP('✗ Stream unavailable: '+name);
                  document.getElementById('ppbtn').textContent='▶';
                }
              });
            } else {
              vid.src=remuxUrl; vid.play().catch(()=>{});
            }
          }, 1000);
        } else {
          alog('[MPEGTS] VOD stream unavailable'+_codeTag+' — '+msg,'e');
          setNP('✗ Stream unavailable'+_codeTag+': '+name);
          document.getElementById('ppbtn').textContent='▶';
        }
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
  // Update the "now playing" highlight without re-rendering the full list.
  // renderItems() replaces innerHTML entirely and resets scrollTop — the user
  // loses their scroll position every time they play a channel.
  // Instead just toggle the .now class in-place on the existing rows.
  (()=>{
    const rows = document.getElementById('ilist').querySelectorAll('.irow');
    rows.forEach((r, i) => r.classList.toggle('now', i === pIdx));
  })();
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

// ── ACTION DRAWER ──────────────────────────────────────────
let drawerCtx = 'cats';
function openActTab(){
  openDrawer('both');
}
function openDrawer(ctx){
  drawerCtx = ctx||'both';
  document.getElementById('adr-unified-content').classList.remove('hidden');
  document.getElementById('adr-title').textContent = '⚡ Actions';
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

async function openProfileModal(){
  if(!document.getElementById('cdot').classList.contains('on')) return;
  const modal = document.getElementById('profile-modal');
  const body  = document.getElementById('profile-modal-body');
  body.innerHTML = '<span style="color:var(--txt3)">Loading…</span>';
  modal.style.display = 'flex';
  try{
    const r = await fetch('/api/profile');
    const d = await r.json();
    const row=(label,val,extra='')=>val?`<div style="display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--bdr);padding-bottom:6px"><span style="color:var(--txt3);min-width:120px;flex-shrink:0;font-size:11px">${label}</span><span style="color:var(--txt);word-break:break-all;font-size:12px"${extra}>${val}</span></div>`:'';
    const typeBadge={stalker:'background:rgba(168,85,247,.15);color:#a855f7;border:1px solid rgba(168,85,247,.3)',mac:'background:rgba(59,130,246,.15);color:#3b82f6;border:1px solid rgba(59,130,246,.3)',xtream:'background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.3)',m3u:'background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3)'};
    const typeLabel={stalker:'STALKER / MAG',mac:'MAC PORTAL',xtream:'XTREAM API',m3u:'M3U'};
    const tk=d.type==='stalker'?'stalker':d.type==='xtream'?'xtream':d.type==='m3u'?'m3u':'mac';
    const badgeStyle=typeBadge[tk]||typeBadge.mac;
    const statusBadge=s=>{
      const sl=String(s).trim();
      if(sl==='1'||sl.toLowerCase()==='active') return `<span style="background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.3);padding:1px 7px;border-radius:20px;font-size:10px;font-weight:700">ACTIVE</span>`;
      if(sl==='0') return `<span style="background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.3);padding:1px 7px;border-radius:20px;font-size:10px;font-weight:700">INACTIVE</span>`;
      if(sl==='2') return `<span style="background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3);padding:1px 7px;border-radius:20px;font-size:10px;font-weight:700">BLOCKED</span>`;
      return s?`<span style="background:rgba(107,114,128,.15);color:#9ca3af;border:1px solid rgba(107,114,128,.3);padding:1px 7px;border-radius:20px;font-size:10px;font-weight:700">${s}</span>`:'';
    };
    const isMacAddr=v=>v&&/^([0-9a-f]{2}[:\-]){5}[0-9a-f]{2}$/i.test(v.trim());
    const pwdStyle=' style="font-family:monospace;letter-spacing:2px;color:var(--acc)"';
    let html=`<div style="margin-bottom:10px"><span style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;${badgeStyle}">${typeLabel[tk]||tk.toUpperCase()}</span></div>`;
    html+=row('Portal',d.portal_url);
    if(d.type==='stalker'){
      html+=row('MAC',d.mac);
      html+=row('Login',d.login);
      html+=row('Password',d.password,pwdStyle);
      html+=row(d.exp_label==='last_billing'?'Last Billing':'Expiry',d.exp);
      html+=d.status?`<div style="display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--bdr);padding-bottom:6px"><span style="color:var(--txt3);min-width:120px;flex-shrink:0;font-size:11px">Status</span>${statusBadge(d.status)}</div>`:'';
      html+=row('Active connections', d.active_cons||'');
      html+=row('Max connections', d.max_conn && d.max_conn!=='–' ? d.max_conn : '');
      html+=row('Settings password',d.settings_password,pwdStyle);
      html+=row('Adult password',d.adult_password,pwdStyle);
    } else {
      html+=row('MAC',d.mac);
      if(!isMacAddr(d.user)) html+=row('Username',d.user);
      html+=row('Expiry',d.exp);
      html+=row('Connections', d.active_cons!==undefined&&d.active_cons!==''&&d.max_conn ? d.active_cons+' / '+d.max_conn : d.max_conn||d.active_cons);
      if(d.type==='xtream'){
        // password intentionally omitted from portal info display
      } else {
        html+=row('Settings password',d.settings_password,pwdStyle);
        html+=row('Adult password',d.adult_password,pwdStyle);
      }
    }
    body.innerHTML = html || '<span style="color:var(--txt3)">No profile data available.</span>';
  }catch(e){
    body.innerHTML = `<span style="color:var(--red)">Failed to load profile: ${e.message}</span>`;
  }
}
function closeProfileModal(){
  document.getElementById('profile-modal').style.display='none';
}

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
  const icons={mac:'🔌',xtream:'📡',m3u_url:'📄',stalker:'📺'};
  const typeAccent={mac:'#3b82f6',xtream:'#22c55e',m3u_url:'#ef4444',stalker:'#a855f7'};
  const typeLbl={mac:'MAC',xtream:'XTREAM',m3u_url:'M3U',stalker:'STALKER'};
  const typeCls={mac:'pli-type-mac',xtream:'pli-type-xtream',m3u_url:'pli-type-m3u',stalker:'pli-type-stalker'};
  el.innerHTML=arr.map((p,i)=>{
    const raw=p.type||'mac';
    const t=raw==='mac'&&p.is_stalker?'stalker':raw;
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
    is_stalker: plCT==='mac' && document.getElementById('pl-url').value.trim().toLowerCase().includes('stalker_portal'),
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
    stalker_sn:         plCT==='mac' ? (document.getElementById('pl-sn')?.value.trim()||'')    : '',
    stalker_device_id:  plCT==='mac' ? (document.getElementById('pl-devid')?.value.trim()||'') : '',
    stalker_device_id2: plCT==='mac' ? (document.getElementById('pl-devid2')?.value.trim()||''): '',
    stalker_signature:  plCT==='mac' ? (document.getElementById('pl-sig')?.value.trim()||'')   : '',
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
  if(document.getElementById('pl-sn'))     document.getElementById('pl-sn').value=p.stalker_sn||'';
  if(document.getElementById('pl-devid'))  document.getElementById('pl-devid').value=p.stalker_device_id||'';
  if(document.getElementById('pl-devid2')) document.getElementById('pl-devid2').value=p.stalker_device_id2||'';
  if(document.getElementById('pl-sig'))    document.getElementById('pl-sig').value=p.stalker_signature||'';
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
   'pl-epg','pl-mac-epg','pl-m3u-epg','pl-sn','pl-devid','pl-devid2'].forEach(id=>{
    const el=document.getElementById(id); if(el) el.value='';
  });
}

async function plConnect(i){
  const arr=plLoadAll(); const p=arr[i]; if(!p) return;
  closePL();
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
  // Stalker override hidden inputs
  const sn=document.getElementById('i-sn'); if(sn) sn.value=p.stalker_sn||'';
  const di=document.getElementById('i-devid'); if(di) di.value=p.stalker_device_id||'';
  const di2=document.getElementById('i-devid2'); if(di2) di2.value=p.stalker_device_id2||'';
  const sig=document.getElementById('i-sig'); if(sig) sig.value=p.stalker_signature||'';
  await doConnect();
}

// ── INIT ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded',()=>{
  setCT('mac'); toggleCP();
  // Click on collapsed #p-items bar expands it
  const pItems = document.getElementById('p-items');
  if(pItems) pItems.addEventListener('click', ()=>{
    const main = document.getElementById('main');
    if(main && !main.classList.contains('items-open'))
      main.classList.add('items-open');
  });
  // Player controls expanded by default
  const pc = document.getElementById('pctrl-panel');
  if(pc) pc.classList.add('expanded');
  try{const sv=localStorage.getItem('mkv_folder');
    if(sv) document.getElementById('o-dir').value=sv;
    else document.getElementById('o-dir').value='/sdcard/Download/';}catch(e){}
  try{const sm=localStorage.getItem('m3u_path');
    if(sm) document.getElementById('o-m3u').value=sm;
    else document.getElementById('o-m3u').value='/sdcard/Download/playlist.m3u';}catch(e){}
  try{const sdv=localStorage.getItem('dvr_folder');
    if(sdv) document.getElementById('o-dvr').value=sdv;
    else document.getElementById('o-dvr').value='/sdcard/Download/DVR/';}catch(e){}
  try{const se=localStorage.getItem('ext_player');
    if(se) document.getElementById('o-extplayer').value=se;}catch(e){}
  if(_isMobile){
    document.getElementById('extplayer-row-desktop').style.display='none';
    document.getElementById('extplayer-row-mobile').style.display='flex';
    try{const mp=localStorage.getItem('mobile_player');
      if(mp) document.getElementById('o-mobile-player').value=mp;}catch(e){}
    // Hide desktop-only file browser switch buttons — mobile always uses the
    // inline file browser and has no access to the tkinter desktop picker.
    const _fbForceBtn = document.getElementById('m3u-force-fb-btn');
    if(_fbForceBtn) _fbForceBtn.style.display='none';
    const _outFbToggleBtn = document.getElementById('out-fb-toggle');
    if(_outFbToggleBtn) _outFbToggleBtn.style.display='none';
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

  // ── Back button — wired globally so it works from DVR channel picker
  // (which opens the selector without ever going through mvOpen/_mvSetupListeners)
  const _mvsBackBtn = document.getElementById('mv-sel-back');
  if(_mvsBackBtn){
    _mvsBackBtn.addEventListener('click', ()=>{
      if(typeof _mvCloseCtxMenu === 'function') _mvCloseCtxMenu();
      if(typeof _mvSelNavMode === 'undefined') return;
      if(_mvSelNavMode === 'episodes'){
        _mvSelNavMode  = 'items';
        _mvSelShowItem = null;
        _mvSelEpisodes = [];
      } else {
        _mvSelNavMode = 'cats';
        _mvSelCat     = null;
        _mvSelItems   = [];
      }
      const srch = document.getElementById('mv-sel-search');
      if(srch) srch.value = '';
      const _pRow = document.getElementById('mv-sel-play-url-row');
      if(_pRow){
        const showRow = _mvSelNavMode==='cats'
          && (typeof _mvSelContentMode!=='undefined' && _mvSelContentMode==='live')
          && (typeof _mvSelWidgetCtx!=='undefined' && _mvSelWidgetCtx);
        _pRow.style.display = showRow ? '' : 'none';
      }
      const tabsEl = document.getElementById('mv-sel-tabs');
      // In forced-mode (e.g. DVR live-only) keep tabs hidden at all levels
      if(tabsEl) tabsEl.style.display = (_mvSelNavMode==='cats' && !_mvSelForcedMode) ? '' : 'none';
      if(typeof _mvRenderSel === 'function') _mvRenderSel();
    });
  }

  });
// Public API: open the MV channel selector with a custom callback.
// Used by DVR channel picker and any other feature needing channel selection.
let _mvSelForcedMode = null;  // set when selector is opened in forced mode (e.g. DVR live-only)
function _mvSelOpen(callback, forcedMode){
  if(typeof _mvPopulateSelector !== 'function'){
    toast('Connect to a portal first','wrn'); return;
  }
  mvSelCallback = (ch)=>{ callback(ch); mvSelCallback=null; };
  _mvSelWidgetCtx  = null;
  _mvSelForcedMode = forcedMode || null;
  if(forcedMode){
    _mvSelContentMode = forcedMode;
    // Force the mode state without the early-return guard
    _mvSelNavMode = 'cats';
    _mvSelCat = null; _mvSelItems = []; _mvSelShowItem = null; _mvSelEpisodes = [];
    document.getElementById('mv-sel-search').value = '';
    document.querySelectorAll('.mv-sel-tab').forEach(b=>{
      b.classList.toggle('active', b.dataset.mode === forcedMode);
    });
  }
  _mvPopulateSelector();
  document.getElementById('mv-sel-overlay').classList.add('open');
  // Hide tabs AFTER populate so they aren't restored by _mvPopulateSelector
  const tabsEl2 = document.getElementById('mv-sel-tabs');
  if(tabsEl2) tabsEl2.style.display = forcedMode ? 'none' : '';
}
</script>
<script src="/api/dl/ui.js"></script>
<script src="/api/mv/ui.js"></script>
<script src="/api/cast/ui.js"></script>
<script src="/api/subtitles/ui.js"></script>
<script src="/api/epg/ui.js"></script>
<script src="/api/dvr/ui.js"></script>
<script src="/api/dlm/ui.js"></script>
<script src="/api/probe/ui.js"></script>
</body>
</html>
"""

# ── Pre-render the page once at startup ──────────────────────────────────────
# render_template_string() recompiles the entire 424 KB Jinja2 template on
# EVERY request (≈40 ms CPU per page load). The two substitutions
# ({{ config }} and {{ tags_html }}) are derived purely from module-level
# constants that never change after startup — so the rendered output is
# identical on every call. Pre-render once and cache the bytes.
# We also pre-compress with gzip (105 KB vs 438 KB uncompressed) so the
# browser receives 76% less data and has proportionally less JS/CSS to parse.
import gzip as _gzip_mod
_pre_config_json = json.dumps({
    "ffmpeg_ok":  _FFMPEG_AVAILABLE,
    "ffprobe_ok": _FFPROBE_AVAILABLE,
    "ytdlp_ok":   YTDLP_AVAILABLE,
    "dvr_ok":     _DVR_AVAILABLE,
    "dlm_ok":     _DOWNLOAD_AVAILABLE,
    "probe_ok":   _PROBE_AVAILABLE,
})
_pre_tags: list = []
_pre_tags.append(
    '<span class="tag tag-ok">\u2713 ffmpeg</span>' if _FFMPEG_AVAILABLE
    else '<span class="tag tag-err">\u2717 ffmpeg</span>'
)
if not _FFPROBE_AVAILABLE:
    _pre_tags.append('<span class="tag tag-warn">\u2717 ffprobe</span>')
if YTDLP_AVAILABLE:
    _pre_tags.append('<span class="tag tag-ok">\u2713 yt-dlp</span>')
_pre_tags_html = "".join(_pre_tags)

# Plain Python string replace — avoids Jinja2 parse+compile entirely
_HTML_BYTES: bytes = (
    HTML_TEMPLATE
    .replace("{{ tags_html | safe }}", _pre_tags_html)
    .replace("{{ config | safe }}", _pre_config_json)
).encode("utf-8")
_HTML_BYTES_GZ: bytes = _gzip_mod.compress(_HTML_BYTES, compresslevel=6)

# ===================== ENTRY POINT =====================

if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀  IPTV Portal Builder starting on http://{host}:{port}")
    print(f"    Open this address in your browser or WebView.")
    print(f"    ffmpeg: {'found ✓' if _FFMPEG_AVAILABLE else 'NOT FOUND ✗'}")
    print(f"    yt-dlp: {'found ✓' if YTDLP_AVAILABLE else 'not available'}")
    # Silence urllib3 InsecureRequestWarning — we use verify=False intentionally
    # for IPTV portals that have self-signed certs.
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    # Use threaded=True for SSE support
    flask_app.run(host=host, port=port, threaded=True, debug=False)
