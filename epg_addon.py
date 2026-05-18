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
epg_addon.py  —  EPG, Catch-up TV, What's On Now for FlaskyIPTV_Player_byGG.py
=================================================================================
Provides:
  /api/epg                — Fetch current/next/schedule EPG for a channel.
                            Priority chain: portal EPG → external XMLTV.
  /api/epg_status         — Poll XMLTV download progress.
  /api/whats_on           — List all programmes airing right now from XMLTV.
  /api/find_channel       — Fuzzy-match a programme title to a portal channel.
  /api/catchup            — Fetch catch-up/timeshift archive listings.
  /api/catchup/play       — Resolve a catch-up stream URL for playback.

Also contains:
  _parse_xtream_short_epg / _parse_stalker_epg — portal EPG parsers
  _epg_* helpers — fuzzy channel matching, XMLTV index builder and fetcher
  _fch_* helpers — find-channel scoring functions

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRATION  (three changes to FlaskyIPTV_Player_byGG.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — add import after the download_addon import block:

    try:
        from epg_addon import register_epg_routes
        _EPG_AVAILABLE = True
    except ImportError:
        _EPG_AVAILABLE = False
        def register_epg_routes(*a, **kw): pass

STEP 2 — register routes after register_download_routes call:

    register_epg_routes(flask_app, state, run_async, _make_client)

STEP 3 — add script tag just before </body> (after subtitles ui.js):

    <script src="/api/epg/ui.js"></script>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GLOBALS USED FROM MAIN SCRIPT (all available as window.*)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  doPlay()           — main player
  doConnect()        — main connect handler
  closeItemMenu()    — close context menu
  playItem()         — play an item from browse list
  setCT() / toggleCP() — connect panel helpers
  dvrClose()         — DVR addon
  esc() / toast() / alog() — UI helpers
  _isMobile          — mobile detection
  mode / pUrl / pName / pIdx / curCat / allItems / selSet  — playback state
  _enterFullscreen / _exitFullscreen / _isLandscape        — orientation
  _lockPortrait / _unlock                                  — orientation
  _mvCloseCtxMenu / _mvHideAll / _mvPlayChannel            — multiview
  _mvRenderSel / _mvsClose                                 — multiview
"""

import asyncio
import base64
import contextlib
import gzip as _gzip
import json
import math
import os
import re
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse, quote, unquote, parse_qs

import aiohttp
from flask import request, jsonify, Response

from portal_clients import (
    PortalClient, StalkerPortalClient, XtreamClient,
    normalize_base_url, safe_json, normalize_js,
)


# ===================== REGISTRATION =====================

# Set by register_epg_routes once _build_xmltv_index is defined as a closure.
# start_epg_prefetch() uses this to run the download without duplicating the
# function or restructuring the module.
_build_xmltv_index_ref = None


def register_epg_routes(flask_app, state, run_async, _make_client):
    """Register all EPG/catchup/WON Flask routes and serve the UI JS."""

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
        ext_url = state.ext_epg_url
        if cached:
            ts, result = cached
            if time.time() - ts < state._epg_cache_ttl:
                _is_empty = not result.get("current") and not result.get("next") and not result.get("schedule")
                if _is_empty and ext_url and ext_url in state._xmltv_downloading:
                    # Portal already failed for this channel; XMLTV now downloading.
                    # Do NOT re-run the portal chain — just return loading immediately.
                    state._xmltv_needs.add(cache_key)
                    del state._epg_cache[cache_key]
                    _loading = {"current": None, "next": None,
                                "error": "EPG loading… please try again in a moment"}
                    return jsonify(_loading)
                state.log(f"[EPG] Cache hit for {cache_key}")
                return jsonify(result)
            else:
                del state._epg_cache[cache_key]

        # Short-circuit: if this channel is confirmed to need XMLTV (no portal data)
        # and XMLTV is still downloading, skip the entire portal chain immediately.
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
                _short_epg_fallback = None   # holds next-only result if current was absent
                if stream_id and not short_epg_skip:
                    epg_api_url = (f"{base_norm}/player_api.php"
                                   f"?username={_q(user, safe='')}&password={_q(pwd, safe='')}"
                                   f"&action=get_short_epg&stream_id={stream_id}&limit=30")
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
                            if result.get("current"):
                                state.log(f"[EPG] get_short_epg OK — current={(result.get('current') or {}).get('title','?')!r}")
                                return result
                            elif result.get("next"):
                                # Has upcoming data but no current — programme boundary
                                # edge case (fetch happened just before start time).
                                # Continue to XMLTV which may have the correct current;
                                # keep this as a fallback if XMLTV also has no current.
                                state.log(f"[EPG] get_short_epg next-only — trying XMLTV for current")
                                _short_epg_fallback = result
                            else:
                                state.log(f"[EPG] get_short_epg has entries but none current/next — falling through")
                        else:
                            state._short_epg_broken.add(base_norm)
                            state.log(f"[EPG] get_short_epg empty — portal flagged, skipping next time")
                    except Exception as e:
                        state.log(f"[EPG] ✗ get_short_epg error: {e}")
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
                    # Propagate loading signal so client retries rather than caching empty
                    if "loading" in (portal_result.get("error") or "").lower():
                        state._xmltv_needs.add(cache_key)
                        return {"current": None, "next": None,
                                "error": portal_result.get("error")}
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

                # Nothing worked — if get_short_epg had next-only data, return that
                # rather than an error so the client at least sees upcoming info.
                if _short_epg_fallback:
                    state.log(f"[EPG] XMLTV had no current — using get_short_epg fallback (next only)")
                    return _short_epg_fallback
                err = "No EPG data found."
                if not state.ext_epg_url:
                    err += " Try adding an external EPG URL in settings."
                return {"current": None, "next": None, "error": err}

            # ── Stalker / MAC portal ──────────────────────────────────────────────
            if conn == "mac":
                ch_id = str(item.get("ch_id") or item.get("id") or stream_id or "").strip()
                php = "/stalker_portal/server/load.php" if state.is_stalker_portal else "/portal.php"
                base_url = normalize_base_url(state.url)

                async def _mac_epg_request(ch_id, php):
                    """Fetch EPG for one channel.
                    Plain MAC: reuses cached token, re-handshakes on 401/auth-failure.
                    Stalker: always does a fresh handshake (tokens are too short-lived to cache).
                    """
                    _timeout = aiohttp.ClientTimeout(total=30, connect=10)

                    async def _ensure_token():
                        """Return cached headers, acquiring token if needed.
                        For stalker portals: tokens are short-lived and invalidated
                        under concurrent load — skip the cache entirely and always
                        do a fresh handshake per request to avoid Authorization failures.
                        For plain MAC portals: token is stable, so cache and reuse.
                        """
                        loop = asyncio.get_event_loop()
                        def _sync_ensure():
                            if state.is_stalker_portal:
                                # Always fresh handshake for stalker — no caching
                                _cl = StalkerPortalClient(state.url, state.mac, state.log)
                                _loop2 = asyncio.new_event_loop()
                                try:
                                    async def _do_hs():
                                        async with _cl:
                                            await _cl.handshake()
                                            return _cl._headers(include_auth=True)
                                    return _loop2.run_until_complete(_do_hs())
                                finally:
                                    _loop2.close()
                            else:
                                with state._mac_epg_token_lock:
                                    if state._mac_epg_token:
                                        return dict(state._mac_epg_headers)
                                    _cl = PortalClient(state.url, state.mac, state.log)
                                    _loop2 = asyncio.new_event_loop()
                                    try:
                                        async def _do_hs():
                                            async with _cl:
                                                await _cl.handshake()
                                                return _cl.token, dict(_cl.headers)
                                        _tok, _hdrs = _loop2.run_until_complete(_do_hs())
                                    finally:
                                        _loop2.close()
                                    state._mac_epg_token = _tok
                                    state._mac_epg_headers = _hdrs
                                    state.log("[EPG] MAC token acquired and cached for reuse")
                                    return dict(_hdrs)
                        return await loop.run_in_executor(None, _sync_ensure)

                    headers = await _ensure_token()
                    epg_url = (f"{base_url}{php}?type=itv&action=get_short_epg"
                               f"&ch_id={ch_id}&count=10&JsHttpRequest=1-xml")
                    state.log(f"[EPG] Trying: {epg_url}")
                    async with aiohttp.ClientSession(timeout=_timeout) as sess:
                        async with sess.get(epg_url, headers=headers) as r:
                            state.log(f"[EPG] HTTP {r.status}")
                            payload = await safe_json(r)
                            # Detect auth failure by status OR by "Authorization failed"
                            # in the response body (stalker portals return HTTP 200 with
                            # this text when the token has expired/been invalidated).
                            _auth_failed = (
                                r.status == 401
                                or ("authorization failed" in
                                    str(payload.get("text", "") if isinstance(payload, dict) else "").lower())
                            )
                            if _auth_failed:
                                state.log("[EPG] ⚠ Portal token rejected (auth failed) — re-handshaking")
                                # For plain MAC portals, clear the cached token so
                                # _ensure_token() acquires a fresh one.
                                # Stalker portals never cache a token, so this is a no-op for them.
                                if not state.is_stalker_portal:
                                    with state._mac_epg_token_lock:
                                        state._mac_epg_token = ""
                                        state._mac_epg_headers = {}
                                headers = await _ensure_token()
                            else:
                                return r.status, payload
                        # Retry with fresh token
                        async with sess.get(epg_url, headers=headers) as r2:
                            state.log(f"[EPG] ⚠ HTTP {r2.status} (retry)")
                            return r2.status, await safe_json(r2)

                if not ch_id:
                    pass  # No portal ch_id — skip straight to external EPG
                else:
                    _status, payload = await _mac_epg_request(ch_id, php)
                    state.log(f"[EPG] Raw: {str(payload)[:300]}")
                    result = _parse_stalker_epg(payload, ch_id)
                    if result.get("current") or result.get("next") or result.get("schedule"):
                        return result
                    state.log(f"[EPG] Portal returned no EPG data for this channel")
                    # Stalker portals alternate between their two endpoints.
                    # Plain MAC portals use portal.php as primary; also try
                    # /server/load.php (without the stalker_portal prefix) as fallback.
                    if state.is_stalker_portal:
                        alt_php = "/stalker_portal/portal.php" \
                                  if php == "/stalker_portal/server/load.php" \
                                  else "/stalker_portal/server/load.php"
                    else:
                        alt_php = "/server/load.php"
                    state.log(f"[EPG] ⚠ Retrying via alt path ({alt_php})")
                    try:
                        _status2, payload2 = await _mac_epg_request(ch_id, alt_php)
                        state.log(f"[EPG] Alt HTTP {_status2}")
                        state.log(f"[EPG] Alt raw: {str(payload2)[:300]}")
                        result2 = _parse_stalker_epg(payload2, ch_id)
                        if result2.get("current") or result2.get("next") or result2.get("schedule"):
                            return result2
                        state.log(f"[EPG] Alt path also returned no EPG data")
                    except Exception as _e2:
                        state.log(f"[EPG] ✗ Alt path error: {_e2}")

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
                state.log(f"[EPG] ⚠ Attempt {_retry + 1} returned no data — retrying ({_retry + 2}/3)")
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
            state.log(f"[EPG] ✗ Error: {type(e).__name__}: {e}")
            # If ext EPG is configured, this portal failure (429, 502, handshake error)
            # is recoverable — the channel may well be in the XMLTV feed.
            # Trigger XMLTV download if not already running/cached, mark channel as
            # needing XMLTV, and return "loading" so the client retries via the poller.
            # Never cache the portal exception as a permanent "No EPG data" result.
            _ext = state.ext_epg_url
            if _ext and _ext not in state._xmltv_cache and _ext not in state._xmltv_no_data:
                state._xmltv_needs.add(cache_key)
                if _ext not in state._xmltv_downloading:
                    state._xmltv_downloading.add(_ext)
                    state.log(f"[EPG] ⚠ Portal error — launching XMLTV download for {_ext}")
                    # Capture portal identity and object references before thread starts —
                    # same pattern as start_epg_prefetch and _bg_download.
                    _exc_pf_key      = (f"{state.conn_type}:{state.url}"
                                        f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
                    _exc_cache_ref   = state._xmltv_cache
                    _exc_no_data_ref = state._xmltv_no_data
                    _exc_dl_ref      = state._xmltv_downloading
                    _exc_needs_ref   = state._xmltv_needs
                    _exc_epg_ref     = state._epg_cache
                    def _exc_bg(_url=_ext):
                        try:
                            _loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(_loop)
                            # Pre-download portal key check
                            _cur = (f"{state.conn_type}:{state.url}"
                                    f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
                            if _cur != _exc_pf_key:
                                state.log(f"[EPG] Portal changed before exc-bg XMLTV download — aborting")
                                return
                            _ed, _cn = _loop.run_until_complete(_build_xmltv_index(_url, state.log))
                            _loop.close()
                            # Post-download portal key check
                            _cur2 = (f"{state.conn_type}:{state.url}"
                                     f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
                            if _cur2 != _exc_pf_key:
                                state.log(f"[EPG] Portal changed during exc-bg XMLTV download — discarding")
                                return
                            _exc_cache_ref[_url] = (time.time(), _ed, _cn)
                            if not _ed:
                                _exc_no_data_ref.add(_url)
                        except Exception as _e2:
                            state.log(f"[EPG] ✗ Background XMLTV error: {_e2}")
                        finally:
                            _exc_dl_ref.discard(_url)
                            _exc_needs_ref.clear()
                            stale = [k for k, v in list(_exc_epg_ref.items())
                                     if not v[1].get("current") and not v[1].get("next")
                                     and not v[1].get("schedule")]
                            for k in stale:
                                _exc_epg_ref.pop(k, None)
                    threading.Thread(target=_exc_bg, daemon=True, name="xmltv-exc-bg").start()
                _loading_err = {"current": None, "next": None,
                                "error": "EPG loading… please try again in a moment"}
                state._epg_cache[cache_key] = (time.time() - (state._epg_cache_ttl - 4), _loading_err)
                return jsonify(_loading_err)
            if _ext and _ext in state._xmltv_downloading:
                state._xmltv_needs.add(cache_key)
                _loading_err = {"current": None, "next": None,
                                "error": "EPG loading… please try again in a moment"}
                state._epg_cache[cache_key] = (time.time() - (state._epg_cache_ttl - 4), _loading_err)
                return jsonify(_loading_err)
            # No ext EPG configured — cache error briefly (30s) so a dead channel
            # does not hammer the portal on every open.
            err_result = {"current": None, "next": None, "error": str(e)}
            state._epg_cache[cache_key] = (time.time() - (state._epg_cache_ttl - 30), err_result)
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

        # Build the combined EPG URL string: external URLs + portal's own xmltv.php
        # (Xtream and M3U-with-Xtream only — MAC/Stalker portals don't expose xmltv.php)
        ek = state.ext_epg_url
        _portal_xmltv = ""
        _conn = state.conn_type
        if _conn == "xtream" or (_conn == "m3u_url" and state.m3u_xtream_override):
            try:
                from urllib.parse import quote as _q2, urlparse as _up2
                _creds = state.m3u_xtream_override if _conn == "m3u_url" else None
                _base  = (_creds["base"] if _creds else state.url).rstrip("/")
                _user  = _creds["username"] if _creds else state.username
                _pwd   = _creds["password"] if _creds else state.password
                if _base and _user and _pwd:
                    _pn = _up2(_base)
                    _base_norm = f"{_pn.scheme}://{_pn.netloc}"
                    _portal_xmltv = (f"{_base_norm}/xmltv.php"
                                     f"?username={_q2(_user, safe='')}&password={_q2(_pwd, safe='')}")
            except Exception:
                _portal_xmltv = ""
        # Combine: external URLs first, portal xmltv.php last (skip if already in ext list)
        _all_urls = [u.strip() for u in (ek or "").splitlines() if u.strip()]
        if _portal_xmltv and _portal_xmltv not in _all_urls:
            _all_urls.append(_portal_xmltv)
        # ek_combined is the newline-joined string passed to _build_xmltv_index
        # (multi-URL logic inside the function handles splitting + parallel fetch)
        ek_combined = "\n".join(_all_urls)

        # If any URL is not yet cached, kick off background download
        if ek_combined and ek_combined not in state._xmltv_cache and ek_combined not in state._xmltv_no_data:
            if ek_combined not in state._xmltv_downloading:
                state._xmltv_downloading.add(ek_combined)
                if len(_all_urls) > 1:
                    for _i, _u in enumerate(_all_urls, 1):
                        state.log(f"[WHATS_ON] Launching background EPG download [{_i}/{len(_all_urls)}]: {_u}")
                else:
                    state.log(f"[WHATS_ON] Launching background EPG download from {ek_combined}")

                # Capture portal identity and object references before thread starts —
                # same pattern as start_epg_prefetch and the other EPG threads.
                _won_pf_key      = (f"{state.conn_type}:{state.url}"
                                    f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
                _won_cache_ref   = state._xmltv_cache
                _won_no_data_ref = state._xmltv_no_data
                _won_dl_ref      = state._xmltv_downloading
                _won_needs_ref   = state._xmltv_needs
                _won_epg_ref     = state._epg_cache

                def _bg():
                    try:
                        bg_loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(bg_loop)
                        # Pre-download portal key check
                        _cur = (f"{state.conn_type}:{state.url}"
                                f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
                        if _cur != _won_pf_key:
                            state.log(f"[WHATS_ON] Portal changed before EPG download — aborting")
                            return
                        epg_d, ch_n = bg_loop.run_until_complete(
                            _build_xmltv_index(ek_combined, state.log))
                        bg_loop.close()
                        # Post-download portal key check
                        _cur2 = (f"{state.conn_type}:{state.url}"
                                 f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
                        if _cur2 != _won_pf_key:
                            state.log(f"[WHATS_ON] Portal changed during EPG download — discarding")
                            return
                        _won_cache_ref[ek_combined] = (time.time(), epg_d, ch_n)
                        if not epg_d:
                            _won_no_data_ref.add(ek_combined)
                        state.log(f"[WHATS_ON] ✓ Background EPG download complete")
                    except Exception as e:
                        state.log(f"[WHATS_ON] ✗ EPG load failed: {e}")
                    finally:
                        _won_dl_ref.discard(ek_combined)
                        _won_needs_ref.clear()
                        stale = [k for k, v in list(_won_epg_ref.items())
                                 if not v[1].get("current") and not v[1].get("next")
                                 and not v[1].get("schedule")]
                        for k in stale:
                            _won_epg_ref.pop(k, None)

                threading.Thread(target=_bg, daemon=True, name="xmltv-whats-on").start()

            return jsonify({"programs": [], "count": 0, "status": "loading",
                            "message": "EPG loading in background — please try again in a moment"})

        if not state._xmltv_cache:
            msg = ("No EPG data loaded yet. Open any live channel first to trigger EPG load, "
                   "then re-open What's on Now.") if (ek or _portal_xmltv) else (
                   "No external EPG URL configured. Add one in Settings (EPG field) and reconnect.")
            return jsonify({"programs": [], "count": 0, "status": "no_epg", "message": msg})

        # ── Build channel_id→logo lookup from pre-fetched channel pool ──────────
        # Keyed by epg_channel_id / tvg_id (lowercased) — these are the XMLTV
        # channel IDs that epg_dict is already keyed on, so the match is exact
        # rather than a fragile display-name comparison.
        # Secondary key: stripped lowercase portal name, as a fallback for
        # portals that don't populate epg_channel_id.
        _won_pk = (f"{state.conn_type}:{state.url}"
                   f":{getattr(state, 'mac', '')}:{getattr(state, 'username', '')}")
        _won_chans = (state._won_ch_cache.get(_won_pk)
                      or state._items_cache.get(("live", "__all__"))
                      or [])
        _won_logo_by_id:   dict = {}   # epg_channel_id / tvg_id → logo
        _won_logo_by_name: dict = {}   # portal channel name (stripped) → logo
        for _wc in _won_chans:
            _wl = (_wc.get("logo") or _wc.get("stream_icon") or
                   _wc.get("screenshot_uri") or _wc.get("tv_logo") or
                   _wc.get("pic") or "").strip()
            if not _wl:
                continue
            # Primary: epg_channel_id / tvg_id — exact match against XMLTV channel id
            _eid = (_wc.get("epg_channel_id") or _wc.get("tvg_id") or "").strip().lower()
            if _eid:
                _won_logo_by_id[_eid] = _wl
            # Secondary: portal channel name stripped of quality/country prefixes
            _wn = (_wc.get("name") or _wc.get("stream_name") or
                   _wc.get("title") or "").strip()
            # Strip common "XX| " prefixes (e.g. "IT| RAI UNO" → "rai uno")
            _wn = re.sub(r'^[A-Z]{2,4}[|:]\s*', '', _wn).strip().lower()
            if _wn:
                _won_logo_by_name[_wn] = _wl

        results = []
        seen = set()

        for _ck, (ts, epg_dict, chan_names) in list(state._xmltv_cache.items()):
            for channel_id, programmes in epg_dict.items():
                names = chan_names.get(channel_id, [])
                display_name = names[0][2] if names else channel_id
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
                                "logo": (_won_logo_by_id.get(channel_id)
                                         or _won_logo_by_name.get(display_name.lower())
                                         or ""),
                                "start": p_start,
                                "end": p_end,
                                "desc": p_desc,
                                "progress": progress,
                            })

        results.sort(key=lambda x: x["title"].lower())
        return jsonify({"programs": results, "count": len(results), "status": "ok"})


    # ── Channel fuzzy-match constants — moved out of api_find_channel so they are
    # built once at startup instead of on every call to that route.
    _FCH_QUALITY_TAGS = ["hevc", "h265", "h.265", "hvc1", "hvc", "av1",
                         "hd", "sd", "fhd", "uhd", "4k", "h264", "h.264",
                         "avc", "av1", "1080p", "720p", "480p"]
    _FCH_HEVC_TAGS    = {"hevc", "h265", "h.265", "hvc1", "hvc", "h.265"}
    _FCH_COUNTRY_SYNONYMS = {
        "sr": "rs", "rs": "rs", "hr": "hr", "ba": "ba", "si": "si", "mk": "mk",
        "me": "me", "al": "al", "bg": "bg", "ro": "ro", "hu": "hu", "sk": "sk",
        "cz": "cz", "pl": "pl", "uk": "uk", "us": "us", "de": "de", "fr": "fr",
        "it": "it", "es": "es", "pt": "pt", "nl": "nl", "tr": "tr", "gr": "gr",
        "at": "at", "ch": "ch",
    }
    _FCH_CC_PAT = "|".join(_FCH_COUNTRY_SYNONYMS.keys())
    _FCH_RE_CC_PREFIX  = re.compile(r"^([A-Za-z]{2,3})\s*[:|]\s*")
    _FCH_RE_CC_SUFFIX1 = re.compile(rf"\.({_FCH_CC_PAT})$", re.I)
    _FCH_RE_CC_SUFFIX2 = re.compile(rf"\s+\(?({_FCH_CC_PAT})\)?$", re.I)
    _FCH_RE_WORDS      = re.compile(r"[a-z0-9]+")
    _FCH_RE_SPACES     = re.compile(r"\s+")

    def _fch_strip_prefix(s):
        m = _FCH_RE_CC_PREFIX.match(s)
        if m and m.group(1).lower() in _FCH_COUNTRY_SYNONYMS:
            return s[m.end():].strip(), m.group(1).lower()
        return s.strip(), None

    def _fch_strip_suffix(s):
        s = _FCH_RE_CC_SUFFIX1.sub("", s)
        s = _FCH_RE_CC_SUFFIX2.sub("", s)
        return s.strip()

    def _fch_strip_quality(s):
        s = (s or "").lower().strip()
        for tag in _FCH_QUALITY_TAGS:
            s = s.replace(f" {tag}", "").replace(f"({tag})", "").replace(f"[{tag}]", "")
        return _FCH_RE_SPACES.sub(" ", s).strip()

    def _fch_core(s):
        stripped, _ = _fch_strip_prefix(s)
        stripped = _fch_strip_suffix(stripped)
        return _fch_strip_quality(stripped)

    def _fch_core_words(s):
        return set(_FCH_RE_WORDS.findall(_fch_core(s)))

    def _fch_norm_code(code):
        return _FCH_COUNTRY_SYNONYMS.get((code or "").lower(), (code or "").lower())

    def _fch_has_hevc(s):
        sl = (s or "").lower()
        return any(t in sl for t in _FCH_HEVC_TAGS)

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

        # ── Return cached channel list if available for this portal ─────────────
        _portal_key = f"{state.conn_type}:{state.url}:{getattr(state, 'mac', '')}:{getattr(state, 'username', '')}"
        _items_all = state._items_cache.get(("live", "__all__"))
        if _items_all:
            channels = _items_all
            state.log(f"[FIND_CH] Using All Channels cache ({len(channels)} channels)")
        elif _portal_key in state._won_ch_cache:
            channels = state._won_ch_cache[_portal_key]
            state.log(f"[FIND_CH] Using session-cached {len(channels)} channels for {_portal_key[:60]}")
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
                                # Log the raw response structure to help debug portals that return 0
                                if isinstance(payload, dict):
                                    js = payload.get("js", {})
                                    total = js.get("total_items", "?") if isinstance(js, dict) else "?"
                                    data_len = len(js.get("data", [])) if isinstance(js, dict) else "?"
                                    state.log(f"[FIND_CH] get_all_channels raw: total_items={total} data={data_len} keys={list(payload.keys())}")
                                else:
                                    state.log(f"[FIND_CH] get_all_channels raw: {str(payload)[:120]}")
                                chans = normalize_js(payload)
                                state.log(f"[FIND_CH] Attempt 1.{_try} → {len(chans)} channels")
                                if chans:
                                    break
                                if _try < 2:
                                    await asyncio.sleep(1.5)
                            except Exception as e:
                                state.log(f"[FIND_CH] ⚠ Attempt 1.{_try} error: {e}")
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
                                if isinstance(payload2, dict):
                                    js2 = payload2.get("js", {})
                                    total2 = js2.get("total_items", "?") if isinstance(js2, dict) else "?"
                                    data_len2 = len(js2.get("data", [])) if isinstance(js2, dict) else "?"
                                    state.log(f"[FIND_CH] get_all_channels alt raw: total_items={total2} data={data_len2}")
                                chans = normalize_js(payload2)
                                state.log(f"[FIND_CH] Attempt 2 → {len(chans)} channels")
                            except Exception as e2:
                                state.log(f"[FIND_CH] ⚠ Attempt 2 error: {e2}")
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
                state._won_ch_cache[_portal_key] = channels
                if channels:
                    state._items_cache[("live", "__all__")] = channels
                state.log(f"[FIND_CH] Fetched {len(channels)} live channels — cached for session")
            except Exception as e:
                state.log(f"[FIND_CH] ✗ Fetch error: {e}")
                return jsonify({"found": False, "error": str(e)})

        if not channels:
            return jsonify({"found": False, "error": "No live channels on portal"})

        # ── Fuzzy scoring — uses module-level pre-compiled constants (_fch_*) ──────

        # Pre-process EPG side
        epg_name_l    = epg_channel_name.lower().strip()
        epg_core      = _fch_core(epg_channel_name)
        epg_cwords    = _fch_core_words(epg_channel_name)
        _, epg_cc_raw = _fch_strip_prefix(epg_channel_name)
        if not epg_cc_raw:
            m = re.search(rf'\.({_FCH_CC_PAT})$', epg_channel_name, re.I)
            if m:
                epg_cc_raw = m.group(1)
        epg_cc = _fch_norm_code(epg_cc_raw)   # canonical country code or ""

        state.log(f"[FIND_CH] EPG core={epg_core!r} country={epg_cc!r} words={epg_cwords}")

        scored = []   # list of (score, ch) — collect all to log top candidates

        for ch in channels:
            ch_name     = (ch.get("name") or ch.get("stream_name") or ch.get("title") or "").strip()
            ch_tvg_id   = (ch.get("epg_channel_id") or ch.get("tvg_id") or "").strip().lower()
            ch_name_l   = ch_name.lower()
            score = 0

            ch_core_str, ch_cc_raw = _fch_strip_prefix(ch_name)
            ch_core_str = _fch_strip_suffix(ch_core_str)
            ch_core_str = _fch_strip_quality(ch_core_str)
            ch_cc = _fch_norm_code(ch_cc_raw)

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
                ch_cw = _fch_core_words(ch_name)
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
            if _fch_has_hevc(ch_name):
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

                # Archive window — how far back the portal retains recordings.
                # tv_archive_duration comes from get_live_streams (days as int).
                # We use this to: (a) compute mark_archive=0/1 per entry,
                # (b) build a wide-window XMLTV index that covers the full window.
                try:
                    _arch_days = max(1, int(item.get("tv_archive_duration") or 0))
                except Exception:
                    _arch_days = 1
                if _arch_days == 0:
                    _arch_days = 7  # safe default when not reported
                _arch_cutoff = now_ts - _arch_days * 86400  # programmes older = greyed out

                def _match_xmltv(epg_dict, chan_names, lookup_id, tag="XMLTV"):
                    """Match lookup_id against a parsed XMLTV index and return catchup result dicts.
                    Closes over: sid, _arch_cutoff, now_ts (from _resolve scope).
                    Tries: exact channel-id, display-name substring, noise-stripped normalization.
                    Returns a list sorted newest-first; empty list when no match found.
                    """
                    if not epg_dict or not lookup_id:
                        return []
                    lookup_lower = lookup_id.strip().lower()
                    entries = epg_dict.get(lookup_lower)

                    # Tier 1: display-name substring match
                    if not entries:
                        for cid, names in chan_names.items():
                            name_strs = [t[0] for t in names]
                            if (lookup_lower in name_strs or
                                    any(lookup_lower in n or n in lookup_lower
                                        for n in name_strs)):
                                entries = epg_dict.get(cid)
                                if entries:
                                    state.log(f"[CATCHUP] {tag} name match: "
                                              f"{lookup_id!r} → {cid!r}")
                                    break

                    # Tier 2: noise-stripped normalised name
                    if not entries:
                        lookup_norm = _normalize_ch_name(lookup_id)
                        if lookup_norm and lookup_norm != lookup_lower:
                            entries = epg_dict.get(lookup_norm)
                            if not entries:
                                for cid, names in chan_names.items():
                                    cid_norm   = _normalize_ch_name(cid)
                                    names_norm = [_normalize_ch_name(t[0]) for t in names]
                                    if (lookup_norm == cid_norm
                                            or lookup_norm in names_norm
                                            or any(lookup_norm in nn or nn in lookup_norm
                                                   for nn in names_norm if nn)):
                                        entries = epg_dict.get(cid)
                                        if entries:
                                            state.log(
                                                f"[CATCHUP] {tag} normalized match: "
                                                f"{lookup_id!r} → {cid!r}")
                                            break

                    if not entries:
                        return []

                    matched = []
                    for ep in entries:
                        if isinstance(ep, tuple):
                            ep_title, ep_start, ep_end = ep[0], ep[1], ep[2]
                        else:
                            ep_title = ep.get("title") or "Unknown"
                            ep_start = ep.get("start", 0)
                            ep_end   = ep.get("end",   0)
                        # Include entries back to archive window (+1 day tolerance);
                        # mark entries older than cutoff greyed (mark_archive=0).
                        if ep_start and ep_end and (_arch_cutoff - 86400) <= ep_start < now_ts:
                            _ma = "1" if ep_start >= _arch_cutoff else "0"
                            matched.append({
                                "title":        ep_title or "Unknown",
                                "start":        ep_start,
                                "stop":         ep_end,
                                "cmd":          sid,
                                "live_cmd":     sid,
                                "mark_archive": _ma,
                                "epg_id":       "",
                                "id":           "",
                                "ch_id":        "",
                            })
                    matched.sort(key=lambda x: x.get("start", 0), reverse=True)
                    return matched

                # ── Step 1: get_epg ──────────────────────────────────────────────
                epg_api_url = (f"{base_norm}/player_api.php"
                               f"?username={_q(user, safe='')}&password={_q(pwd, safe='')}"
                               f"&action=get_epg&stream_id={sid}")
                state.log(f"[CATCHUP] Xtream get_epg stream_id={sid}")
                results = []
                try:
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(epg_api_url,
                                            timeout=aiohttp.ClientTimeout(total=12)) as r:
                            payload = await safe_json(r)

                    parsed = _parse_xtream_short_epg(payload)
                    all_entries = parsed.get("schedule", [])
                    state.log(f"[CATCHUP] Xtream EPG entries: {len(all_entries)} total")

                    if len(all_entries) == 0 and isinstance(payload, dict):
                        raw = (payload.get("epg_listings") or
                               (payload.get("js") or {}).get("data") or
                               (payload.get("js") or {}).get("epg_listings") or [])
                        if isinstance(raw, list) and len(raw) > 0:
                            s = raw[0]
                            state.log(f"[CATCHUP] get_epg raw={len(raw)} parsed to 0 "
                                      f"— sample keys: {list(s.keys()) if isinstance(s, dict) else s}")
                        else:
                            state.log("[CATCHUP] get_epg returned empty — portal may not support this endpoint")

                    for ep in all_entries:
                        ep_end   = ep.get("end", 0)
                        ep_start = ep.get("start", 0)
                        # Don't gate on ep_end — entries missing stop time would be
                        # silently dropped, falling through to slower fallbacks.
                        # Include them with stop=0; frontend shows start-only.
                        if ep_start and ep_start < now_ts:
                            _ma = "1" if ep_start >= _arch_cutoff else "0"
                            results.append({
                                "title":        ep.get("title") or "Unknown",
                                "start":        ep_start,
                                "stop":         ep_end,
                                "cmd":          sid,
                                "live_cmd":     sid,
                                "mark_archive": _ma,
                                "epg_id":       "",
                                "id":           "",
                                "ch_id":        "",
                            })

                    results.sort(key=lambda x: x.get("start", 0), reverse=True)

                except Exception as e:
                    state.log(f"[CATCHUP] ✗ Xtream EPG fetch error: {e}")

                # ── Step 1.5: get_simple_data_table fallback ─────────────────────
                # Same endpoint used by playlist.py / MainWindow reference impl.
                # Returns epg_listings with has_archive flag and base64-encoded titles.
                if not results:
                    simple_url = (f"{base_norm}/player_api.php"
                                  f"?username={_q(user, safe='')}&password={_q(pwd, safe='')}"
                                  f"&action=get_simple_data_table&stream_id={sid}")
                    state.log(f"[CATCHUP] Trying get_simple_data_table stream_id={sid}")
                    try:
                        async with aiohttp.ClientSession() as sess:
                            async with sess.get(simple_url,
                                                timeout=aiohttp.ClientTimeout(total=12)) as r2:
                                payload2 = await safe_json(r2)
                        if isinstance(payload2, dict):
                            listings2 = payload2.get("epg_listings") or []
                            state.log(f"[CATCHUP] get_simple_data_table entries: {len(listings2)} total")
                            for ep in listings2:
                                if not isinstance(ep, dict):
                                    continue
                                ep_s = int(ep.get("start_timestamp") or 0)
                                ep_e = int(ep.get("stop_timestamp")  or 0)
                                # Some Xtream panels omit stop_timestamp; fall back to
                                # start + duration (seconds) — same logic as playlist.py
                                # which computes duration from (stop_ts - start_ts).
                                if not ep_e and ep_s:
                                    try:
                                        ep_e = ep_s + int(ep.get("duration") or 0)
                                    except Exception:
                                        pass
                                if not ep_s or ep_s >= now_ts:
                                    continue
                                # Title is base64-encoded (same as playlist.py reference)
                                raw_title = ep.get("title") or ""
                                try:
                                    import base64 as _b64
                                    ep_title = _b64.b64decode(raw_title.encode()).decode("utf-8")
                                except Exception:
                                    ep_title = raw_title
                                ep_title = ep_title.strip() or "Unknown"
                                _ma2 = "1" if ep_s >= _arch_cutoff else "0"
                                # Preserve the portal's own server-local datetime string.
                                # start_timestamp is a UTC epoch, but many portals compute it
                                # using a pre-DST timezone offset (e.g. CET instead of CEST),
                                # making it 1 h off after a DST change.  The 'start' string
                                # field is always the portal's stored local time and is
                                # timezone-neutral — we use it directly for the timeshift URL
                                # to avoid the DST mismatch entirely.
                                _raw_start_str = (ep.get("start") or ep.get("time") or "").strip()
                                ep_start_str = _raw_start_str[:16] if len(_raw_start_str) >= 13 else ""
                                results.append({
                                    "title":        ep_title,
                                    "start":        ep_s,
                                    "stop":         ep_e,
                                    "start_str":    ep_start_str,   # server-local "YYYY-MM-DD HH:MM"
                                    "cmd":          sid,
                                    "live_cmd":     sid,
                                    "mark_archive": _ma2,
                                    "epg_id":       "",
                                    "id":           "",
                                    "ch_id":        "",
                                })
                            if results:
                                results.sort(key=lambda x: x.get("start", 0), reverse=True)
                                state.log(f"[CATCHUP] get_simple_data_table gave {len(results)} past entries")
                            else:
                                state.log("[CATCHUP] get_simple_data_table returned no past entries")
                    except Exception as e:
                        state.log(f"[CATCHUP] ✗ get_simple_data_table error: {e}")

                # ── Step 2: XMLTV fallback — wide-window index ────────────────────
                # KEY FIX: the live EPG index only keeps ±4-20h of data.  For catchup
                # we need up to tv_archive_duration days of history.  We maintain a
                # separate catchup XMLTV cache built with win_back_h = archive_days*24
                # so all archived programmes are present and can be greyed/active.
                if not results:
                    epg_ch_id = str(item.get("epg_channel_id") or "").strip()
                    tvg_name  = str(item.get("name") or "").strip()
                    lookup_id = epg_ch_id or tvg_name
                    if lookup_id and base_norm not in state._xmltv_no_data:
                        xmltv_url = (f"{base_norm}/xmltv.php"
                                     f"?username={_q(user, safe='')}&password={_q(pwd, safe='')}")
                        state.log(f"[CATCHUP] Xtream XMLTV fallback (lookup={lookup_id!r}, window={_arch_days}d)")
                        try:
                            ck = base_norm
                            # Use the wide catchup cache; rebuild if window is wider than cached
                            cached_cu = state._xmltv_catchup_cache.get(ck)
                            if cached_cu:
                                _cu_ts, epg_dict, chan_names, _cu_win = cached_cu
                                # Rebuild if cache expired (30 min) or window is too narrow
                                if (time.time() - _cu_ts > state._xmltv_cache_ttl
                                        or _cu_win < _arch_days):
                                    cached_cu = None
                            epg_dict = chan_names = None
                            if cached_cu:
                                _, epg_dict, chan_names, _ = cached_cu
                            elif ck in state._xmltv_catchup_downloading:
                                state.log(f"[CATCHUP] Wide XMLTV download already running for {ck} — waiting…")
                                for _w in range(120):
                                    await asyncio.sleep(1)
                                    if ck not in state._xmltv_catchup_downloading:
                                        break
                                cached_cu = state._xmltv_catchup_cache.get(ck)
                                if cached_cu:
                                    _, epg_dict, chan_names, _ = cached_cu
                                else:
                                    state.log(f"[CATCHUP] Wide XMLTV wait timed out for {ck}")
                                    epg_dict, chan_names = {}, {}
                            else:
                                # Download with full archive window (e.g. 7d back, 1d forward)
                                state._xmltv_catchup_downloading.add(ck)
                                state.log(f"[CATCHUP] Downloading wide XMLTV (win_back={_arch_days * 24}h)")
                                try:
                                    epg_dict, chan_names = await _build_xmltv_index(
                                        xmltv_url, state.log,
                                        win_back_h=_arch_days * 24, win_fwd_h=2)
                                    state._xmltv_catchup_cache[ck] = (time.time(), epg_dict, chan_names, _arch_days)
                                    if not epg_dict:
                                        state._xmltv_no_data.add(ck)
                                finally:
                                    state._xmltv_catchup_downloading.discard(ck)
                            if epg_dict:
                                results = _match_xmltv(epg_dict, chan_names,
                                                       lookup_id, tag="PortalXMLTV")
                                if results:
                                    state.log(
                                        f"[CATCHUP] Portal XMLTV gave {len(results)} past entries "
                                        f"({sum(1 for r in results if r['mark_archive']=='1')} active, "
                                        f"{sum(1 for r in results if r['mark_archive']=='0')} greyed)"
                                    )
                        except Exception as e:
                            state.log(f"[CATCHUP] ✗ Xtream XMLTV fallback error: {e}")

                # ── Step 3: External EPG URLs (−18h window) ──────────────────────
                # If the user supplied external XMLTV URL(s) in the connection panel,
                # try them with a separate 18-hour-back fetch dedicated to catchup.
                # The live-EPG cache keeps only ±2-14h and is intentionally left
                # untouched; this is a separate download (win_back_h=18, win_fwd_h=0)
                # cached under "ext_cu:<url_blob>" so the two windows never collide.
                if not results and (state.ext_epg_url or "").strip():
                    ext_url_blob = state.ext_epg_url.strip()
                    ck_ext = f"ext_cu:{ext_url_blob}"
                    if not lookup_id:
                        _epg_ch_id2 = str(item.get("epg_channel_id") or "").strip()
                        _tvg_name2  = str(item.get("name") or "").strip()
                        lookup_id   = _epg_ch_id2 or _tvg_name2
                    state.log(f"[CATCHUP] Trying external EPG (lookup={lookup_id!r})")
                    try:
                        cached_ext = state._xmltv_catchup_cache.get(ck_ext)
                        if cached_ext:
                            _cu_ts, _ext_d, _ext_n, _cu_win = cached_ext
                            if time.time() - _cu_ts > state._xmltv_cache_ttl:
                                cached_ext = None
                        ext_dict = ext_names = None
                        if cached_ext:
                            _, ext_dict, ext_names, _ = cached_ext
                        elif ck_ext in state._xmltv_catchup_downloading:
                            state.log("[CATCHUP] External EPG download already running — waiting…")
                            for _w in range(120):
                                await asyncio.sleep(1)
                                if ck_ext not in state._xmltv_catchup_downloading:
                                    break
                            cached_ext = state._xmltv_catchup_cache.get(ck_ext)
                            if cached_ext:
                                _, ext_dict, ext_names, _ = cached_ext
                            else:
                                state.log("[CATCHUP] External EPG wait timed out")
                                ext_dict, ext_names = {}, {}
                        else:
                            state._xmltv_catchup_downloading.add(ck_ext)
                            state.log(
                                "[CATCHUP] Downloading external EPG (−18h window for catchup fallback)")
                            try:
                                ext_dict, ext_names = await _build_xmltv_index(
                                    ext_url_blob, state.log,
                                    win_back_h=18, win_fwd_h=0)
                                state._xmltv_catchup_cache[ck_ext] = (
                                    time.time(), ext_dict, ext_names, _arch_days)
                            finally:
                                state._xmltv_catchup_downloading.discard(ck_ext)

                        if ext_dict:
                            matched_ext = _match_xmltv(ext_dict, ext_names,
                                                       lookup_id, tag="ExtEPG")
                            if matched_ext:
                                results = matched_ext
                                state.log(
                                    f"[CATCHUP] External EPG gave {len(results)} past entries "
                                    f"({sum(1 for r in results if r['mark_archive']=='1')} active)")
                    except Exception as e:
                        state.log(f"[CATCHUP] ✗ External EPG error: {e}")

                # ── Step 4: Synthetic time-slot grid ─────────────────────────────
                # Last resort: when ALL EPG sources fail but tv_archive=1 confirms
                # the portal does serve catch-up, generate hourly time blocks over
                # the full archive window.  The user gets clickable slots rather
                # than having to type times manually.
                # Each entry carries synthetic=True so the frontend can display
                # a "no programme data" notice instead of showing "Unknown" titles.
                if not results and int(item.get("tv_archive") or 0) == 1:
                    state.log(
                        f"[CATCHUP] Generating synthetic time grid "
                        f"({_arch_days}d × 60-min slots — no EPG data available)")
                    _slot_sec = 3600  # 60-minute blocks
                    _now_slot  = (int(now_ts) // _slot_sec) * _slot_sec
                    # Exclude the currently-airing (incomplete) slot
                    _t = _now_slot - _slot_sec
                    _cut_slot  = (int(_arch_cutoff) // _slot_sec) * _slot_sec
                    while _t >= _cut_slot:
                        results.append({
                            "title":        "",        # no programme title
                            "start":        _t,
                            "stop":         _t + _slot_sec,
                            "cmd":          sid,
                            "live_cmd":     sid,
                            "mark_archive": "1",       # portal has catchup; all slots active
                            "synthetic":    True,      # frontend notice flag
                            "epg_id":       "",
                            "id":           "",
                            "ch_id":        "",
                        })
                        _t -= _slot_sec
                    # Already sorted newest-first by construction
                    state.log(f"[CATCHUP] Synthetic grid: {len(results)} slots")

                if not results:
                    return {"error": "No catchup data found — this channel may not support catch-up."}

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
                state.log(f"[CATCHUP] ch_id={ch_id}")
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
                            state.log(f"[CATCHUP] get_simple_data_table ch={ch_id} date={date_str} p={page}")
                            try:
                                async with client.session.get(epg_url, headers=hdrs,
                                                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                                    payload = await safe_json(r)
                            except Exception as e:
                                state.log(f"[CATCHUP] ✗ fetch error: {e}")
                                break
                            js   = payload.get("js", {}) if isinstance(payload, dict) else {}
                            rows = (js.get("data") or []) if isinstance(js, dict) else (js if isinstance(js, list) else [])
                            if not rows:
                                break
                            state.log(f"[CATCHUP] date={date_str} p={page} → {len(rows)} entries")
                            first_logged = False
                            for ep in rows:
                                if not isinstance(ep, dict):
                                    continue
                                if not first_logged:
                                    state.log(f"[CATCHUP] fields: {list(ep.keys())}")
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
                                    f"[CATCHUP] '{ep.get('name','?')}' mark_archive={mark_archive}"
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
                    return {"error": "No catchup data found — this channel likely does not support catch-up."}
                results.sort(key=lambda x: x["start"], reverse=True)
                return {"archive_listings": results, "label": item.get("name", "")}

            return {"error": "Catch-up not supported for this connection type"}

        try:
            return jsonify(run_async(_resolve()))
        except Exception as e:
            state.log(f"[CATCHUP] ✗ Error: {e}")
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
                _offset_secs = getattr(state, "_portal_utc_offset", 0)

                # Prefer the portal's own server-local datetime string over the
                # epoch+offset calculation.  start_timestamp is a UTC epoch that
                # many portals computed using a *pre-DST* timezone (e.g. CET),
                # so adding our correctly-updated post-DST offset (+7200 CEST)
                # pushes the URL 1 hour past the actual content start.
                # The 'start' string field is the portal's stored local schedule
                # time — it never changes with DST and matches what the timeshift
                # engine expects directly.
                _start_str_raw = str(data.get("start_str") or "").strip()[:16]
                if len(_start_str_raw) >= 13:
                    try:
                        _dt_local = datetime.strptime(_start_str_raw, "%Y-%m-%d %H:%M")
                        start_fmt = _dt_local.strftime("%Y-%m-%d:%H-%M")
                    except ValueError:
                        _start_str_raw = ""  # fall through to epoch+offset below

                if len(_start_str_raw) < 13:
                    # Fallback: epoch + server-UTC-offset (used when start_str absent,
                    # e.g. entries coming from get_epg or XMLTV path instead of
                    # get_simple_data_table).
                    _srv_local_ts = start_ts + _offset_secs
                    start_fmt = datetime.utcfromtimestamp(_srv_local_ts).strftime("%Y-%m-%d:%H-%M")

                state.log(f"[CATCHUP/Play] Server offset={_offset_secs:+d}s  "
                          f"start_utc={datetime.utcfromtimestamp(start_ts).strftime('%Y-%m-%d %H:%M')}  "
                          f"start_server={start_fmt}"
                          + (f"  [from start_str]" if len(_start_str_raw) >= 13 else ""))

                # Primary: path-based .ts format — routes through mpegts.js automatically.
                # Do NOT use quote() on credentials — raw values match what the server expects.
                cu_url = (f"{_p.scheme}://{_p.netloc}"
                          f"/timeshift/{user}/{pwd}/{dur}/{start_fmt}/{sid}.ts")

                # Fallback: query-string format (timeshift.php) — HLS.js handles this.
                cu_url_fallback = (f"{_p.scheme}://{_p.netloc}/streaming/timeshift.php"
                                   f"?username={user}&password={pwd}"
                                   f"&stream={sid}&start={start_fmt}&duration={dur}")

                state.log(f"[CATCHUP/Play] Xtream timeshift (path/primary)   -> {cu_url}")
                state.log(f"[CATCHUP/Play] Xtream timeshift (query/fallback)  -> {cu_url_fallback}")
                return {"url": cu_url, "fallback_url": cu_url_fallback, "duration_secs": int(stop_ts - start_ts)}

            # ── MAC / Stalker portal ──────────────────────────────────────────────
            # Use the same server-UTC-offset approach as the Xtream path above.
            # astimezone() would use the CLIENT's local timezone (DST-contaminated):
            # after Belgrade's clock moves UTC+1→UTC+2, start_str shifts +1h,
            # causing catchup to play one slot ahead on 60-min programmes.
            _mac_offset_secs = getattr(state, "_portal_utc_offset", 0)
            _mac_srv_local_ts = start_ts + _mac_offset_secs
            start_str    = datetime.utcfromtimestamp(_mac_srv_local_ts).strftime("%Y-%m-%d:%H-%M")
            duration_min = max(1, (stop_ts - start_ts) // 60)

            state.log(f"[CATCHUP/Play] cmd={effective_cmd[:50]} archive_cmd={archive_cmd[:50] if archive_cmd else '(none)'} start={start_str} dur={duration_min}m")

            client_cls = StalkerPortalClient if state.is_stalker_portal else PortalClient
            async with client_cls(state.url, state.mac, state.log) as client:
                await client.handshake()
                url = await client.create_catchup_link(effective_cmd, start_str, duration_min,
                                                       archive_cmd=archive_cmd)
                # If tv_archive returned nothing (null storage response), fall back to
                # type=itv + start/duration which works on Flussonic-backed portals.
                if not url and archive_cmd:
                    state.log(f"[CATCHUP/Play] ⚠ tv_archive failed — retrying with type=itv fallback")
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
                state.log(f"[CATCHUP/Play] Flussonic → {archive_url}")
                return {"url": archive_url}

            state.log(f"[CATCHUP/Play] Resolved → {url}")
            return {"url": url}

        try:
            return jsonify(run_async(_play()))
        except Exception as e:
            state.log(f"[CATCHUP/Play] ✗ Error: {e}")
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
                # Use replacement-char ratio rather than isprintable() — the latter rejects
                # newlines (U+000A, Cc) which are valid in EPG descriptions (e.g. "S02 E19\n...").
                if decoded and decoded.count('\ufffd') / max(len(decoded), 1) < 0.1:
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
            # Formatted datetime strings — Xtream sends these in UTC ("YYYY-MM-DD HH:MM:SS").
            # Must use replace(tzinfo=UTC) so the epoch is UTC-based; .timestamp() alone
            # uses the CLIENT'S local timezone and is contaminated by DST changes.
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc).timestamp()
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
            # Many Xtream panels omit stop_timestamp and only send duration (seconds).
            # Compute end from duration so the catchup end-time column is populated.
            if not end and start:
                try:
                    end = start + float(ep.get("duration") or ep.get("duration_secs") or 0)
                except Exception:
                    pass
            if not title or not start:
                continue
            entries.append({"title": title, "start": start, "end": end, "desc": desc})

        entries.sort(key=lambda x: x["start"])
        out["schedule"] = entries

        # ── Detect and correct "local-as-UTC" timestamp bug ──────────────────────
        # Many Xtream panels compute start_timestamp by converting the server's
        # LOCAL datetime to a Unix epoch WITHOUT applying the UTC offset (i.e.
        # they call mktime on the local struct_time without TZ conversion).
        # This makes every entry appear to start `portal_utc_offset` seconds in
        # the future relative to true UTC — causing the currently-airing show to
        # be classified as "next" instead of "current".
        #
        # Detection: if NO entry spans `now` as-is, but at least one entry WOULD
        # span `now` after subtracting portal_utc_offset, the portal is using
        # local-as-UTC timestamps.  We only correct when we have positive
        # evidence (a current-after-correction entry exists), so correctly-
        # configured UTC portals — or genuine dead-air gaps — are unaffected.
        _utc_offset = getattr(state, "_portal_utc_offset", 0)
        if _utc_offset and entries:
            _spans_now = any(e["start"] <= now < e["end"] for e in entries if e["end"])
            if not _spans_now:
                _corrected_spans = any(
                    (e["start"] - _utc_offset) <= now < (e["end"] - _utc_offset)
                    for e in entries if e["end"]
                )
                if _corrected_spans:
                    state.log(
                        f"[EPG] Detected local-as-UTC timestamps "
                        f"(offset={_utc_offset:+d}s) — correcting {len(entries)} entries"
                    )
                    for e in entries:
                        e["start"] -= _utc_offset
                        if e["end"]:
                            e["end"] -= _utc_offset

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


    # ── Channel-name normalizer for fuzzy XMLTV matching ─────────────────────────
    # Mirrors reference player normalize_channel_name() — strips punctuation and
    # noise words (HD/SD/FHD/UHD/4K/channel/tv/plus…) then collapses whitespace.
    _RE_NOISE = re.compile(r'\b(hd|sd|fhd|uhd|4k|hevc|h265|channel|tv|plus|entertainment)\b', re.I)
    _RE_NONWD = re.compile(r'[^\w\s]')
    _RE_MSPC  = re.compile(r'\s+')

    # ── EPG channel-matching helpers (region/subregion clusters + scoring) ────────
    # Adapted from script (8) — provides country-aware, proximity-ranked XMLTV
    # channel matching used by _fetch_xmltv_epg.

    _EPG_ALIASES = {"gb": "uk", "sr": "rs"}

    # Sub-regions: tightly coupled by language/history/culture.
    # _epg_proximity uses SMALLEST matching group — so tighter clusters always win.
    _EPG_SUBREGIONS = [
        {"uk","gb","ie"},                               # British Isles (3)
        {"us","ca"},                                    # N.America EN (2)
        {"au","nz"},                                    # Oceania EN (2)
        {"gr","cy"},                                    # Greek (2)
        {"ro","md"},                                    # Romanian (2)
        {"jp","kr"},                                    # East Asia (2)
        {"il","ps"},                                    # Levant (2)
        {"pl","cz","sk"},                               # West Slavic (3)
        {"de","at","ch","li"},                          # DACH (4)
        {"nl","be","lu","aw"},                          # Dutch/Benelux (4)
        {"it","sm","va","mt"},                          # Italian (4)
        {"rs","sr","hr","ba","me"},                     # BCS core (5)
        {"no","se","dk","fi","is"},                     # Nordic (5)
        {"in","pk","bd"},                               # South Asia core (3)
        {"al","mk","bg","rs","gr","me"},                # Balkan (6)
        {"tr","az","tm","uz","kg","kz"},                # Turkic (6)
        {"ru","ua","by","bg","rs","mk"},                # East/South Slavic (6)
        {"fr","be","lu","mc","ch"},                     # Francophone Europe (5)
        {"pt","br","ao","mz","cv","gw","st","tl"},      # Lusophone (8)
        {"cn","hk","tw","sg","my"},                     # Chinese-speaking (5)
        {"za","ng","ke","gh","et","tz"},                # Sub-Saharan Africa (6)
        {"rs","sr","hr","ba","si","me","mk"},           # ex-YU (7)
        {"hu","ro","bg","at","sk"},                     # Carpathian (5)
        {"es","pt","mx","ar","co","cl","pe","uy","bo","ec","py","ve"},
        {"ar","sa","ae","eg","iq","jo","kw","lb","ly","ma","sd","sy","tn","ye"},
        {"de","at","pl","cz","sk","hu","ro","bg","rs","sr",
         "hr","ba","si","me","mk","al","lv","lt","ee"},
        {"uk","gb","ie","us","ca","au","nz","za"},
        {"gr","cy","mt","it","es","pt","fr","al","tr"},
        {"no","se","dk","fi","is","de","at","nl","be","lu"},
        {"us","ca","mx","br","ar","co","cl","pe"},
        {"cn","hk","tw","jp","kr","sg","my","id","th"},
        {"sa","ae","eg","iq","ir","jo","kw","lb","qa","tr"},
        {"za","ng","ke","gh","et","tz","ma","eg","tn","dz"},
    ]

    _EPG_REGIONS = [
        {"rs","sr","hr","ba","si","me","mk","al","bg","ro","hu","at","de","ch","li",
         "cz","sk","pl","ua","by","ru","lv","lt","ee","fi","no","se","dk","is",
         "uk","gb","ie","fr","be","lu","nl","es","pt","it","sm","va","mt","gr","cy",
         "tr","md","am","ge","az","xk"},
        {"us","ca","mx","gt","bz","hn","sv","ni","cr","pa","cu","jm","ht","do",
         "tt","bb","lc","vc","gd","ag","dm","kn","bs","ky","vi","pr"},
        {"br","ar","co","cl","pe","uy","bo","ec","py","ve","gy","gf"},
        {"cn","hk","tw","jp","kr","mn","kp"},
        {"in","pk","bd","lk","np","bt","mv"},
        {"sg","my","id","ph","th","vn","kh","la","mm","bn","tl"},
        {"au","nz","pg","fj","sb","vu","ws","to","ki","fm","mh","pw","nr","tv"},
        {"sa","ae","eg","iq","ir","jo","kw","lb","om","qa","sy","ye","il","ps",
         "tr","az","tm","uz","kg","kz","tj","af"},
        {"za","ng","ke","gh","et","tz","ug","rw","mz","zm","zw","mw","ao","cd",
         "cm","ci","sn","ml","bf","ne","td","sd","so","er","dj","ma","tn","ly",
         "dz","mu","mg","re","sc"},
    ]

    _EPG_NOISE_SUFFIXES = re.compile(
        r'[\s_]*(\bfhd\b|\bhd\b|\b4k\b|\buhd\b|\bvip\b|\braw\b|\bplus\b|\bsd\b)$',
        re.IGNORECASE
    )
    _EPG_COUNTRY_PREFIX = re.compile(r'^[a-z]{2,4}\s*[|:]\s*', re.IGNORECASE)


    def _epg_strip_noise(s: str) -> str:
        """Remove IPTV country prefix and quality suffix from a channel name.
        Also normalise + and & to spaces so 'crime + investigation' core-strips
        to 'crime investigation' and can match XMLTV IDs like crimeinvestigation.rs.
        """
        s = _EPG_COUNTRY_PREFIX.sub('', s.lower())
        s = _EPG_NOISE_SUFFIXES.sub('', s)
        # Treat + and & as word separators (e.g. "crime + investigation" → "crime investigation")
        s = re.sub(r'\s*[+&]\s*', ' ', s)
        return s.strip()


    def _epg_cid_suffix(cid: str) -> str:
        """Extract country suffix from a XMLTV channel ID (e.g. hbo.2.hd.hr → hr)."""
        for p in reversed(cid.split(".")):
            if 2 <= len(p) <= 3 and p.isalpha() and p not in ("hd","sd","4k","uhd","fhd"):
                return p
        return ""


    def _epg_proximity(cid: str, lookup_country: str) -> int:
        """Geographic proximity rank (lower = closer).
        0   = same country
        2-N = same sub-region; smaller group = tighter linguistic fit
        100 = same broad continent region
        200 = different continent
        """
        sfx = _epg_cid_suffix(cid)
        if not sfx or not lookup_country:
            return 999
        lc = _EPG_ALIASES.get(lookup_country, lookup_country)
        sc = _EPG_ALIASES.get(sfx, sfx)
        if lc == sc:
            return 0
        best_subregion = None
        for grp in _EPG_SUBREGIONS:
            if lc in grp and sc in grp:
                if best_subregion is None or len(grp) < best_subregion:
                    best_subregion = len(grp)
        if best_subregion is not None:
            return best_subregion
        for grp in _EPG_REGIONS:
            if lc in grp and sc in grp:
                return 100
        return 200


    def _epg_levenshtein(a: str, b: str) -> int:
        """Simple Levenshtein edit distance for short channel name strings."""
        if a == b: return 0
        if not a: return len(b)
        if not b: return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a):
            curr = [i + 1]
            for j, cb in enumerate(b):
                curr.append(min(prev[j + 1] + 1, curr[j] + 1,
                                prev[j] + (0 if ca == cb else 1)))
            prev = curr
        return prev[-1]


    def _normalize_ch_name(name: str) -> str:
        s = name.lower().strip()
        s = _RE_NONWD.sub('', s)
        s = _RE_NOISE.sub('', s)
        s = _RE_MSPC.sub(' ', s).strip()
        return s


    async def _build_xmltv_index(xmltv_url: str, log_cb=None,
                                 win_back_h: int = 2, win_fwd_h: int = 14) -> dict:
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
          chan_names  = {channel_id_lower: [(display_name_lower, lang_lower, display_name_original), ...]}
        """
        _log = log_cb or (lambda x: None)

        # Multi-URL support: if xmltv_url contains newlines, split and fetch concurrently.
        # Maximum 4 URLs accepted; extras are silently ignored.
        # Each URL is fetched in parallel; results are merged into one index.
        # Single-URL path is unchanged.
        _urls = [u.strip() for u in xmltv_url.splitlines() if u.strip()][:4]
        if len(_urls) > 1:
            merged_epg: dict = {}
            merged_names: dict = {}
            async def _fetch_one(_url):
                try:
                    _log(f"[EPG] Multi-URL: fetching {_url}")
                    return await _build_xmltv_index(_url, log_cb, win_back_h, win_fwd_h)
                except Exception as _e:
                    _log(f"[EPG] Multi-URL: failed for {_url}: {_e}")
                    return {}, {}
            _results = await asyncio.gather(*[_fetch_one(u) for u in _urls])
            for _ed, _cn in _results:
                for _ch, _progs in _ed.items():
                    merged_epg.setdefault(_ch, []).extend(_progs)
                merged_names.update(_cn)
            return merged_epg, merged_names

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
                            # Store as (name_lower, lang_lower) tuples so matching
                            # can prefer the right language variant over same-named foreign channels.
                            names = [(dn.text.strip().lower(), (dn.get("lang") or "").strip().lower(),
                                      dn.text.strip())
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

            # Capture portal identity and live-object references NOW, before the
            # thread starts — mirrors the proven pattern in start_epg_prefetch.
            # If api_connect() fires while the download is in-flight:
            #   _portal_key_bg:  used to detect the switch (pre+post check)
            #   _cache_ref_bg:   api_connect() replaces state._xmltv_cache with a
            #                    new dict, so writing to the old ref is harmless
            #   _downloading_ref_bg / _needs_ref_bg / _epg_ref_bg: same pattern —
            #                    finally block operates on the OLD objects, not
            #                    Portal 2's fresh state.
            _portal_key_bg   = (f"{state.conn_type}:{state.url}"
                                f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
            _cache_ref_bg    = state._xmltv_cache
            _no_data_ref_bg  = state._xmltv_no_data
            _dl_ref_bg       = state._xmltv_downloading
            _needs_ref_bg    = state._xmltv_needs
            _epg_ref_bg      = state._epg_cache

            def _bg_download():
                try:
                    bg_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(bg_loop)
                    # Pre-download portal key check
                    _cur = (f"{state.conn_type}:{state.url}"
                            f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
                    if _cur != _portal_key_bg:
                        _log(f"[EPG] Portal changed before XMLTV download started — aborting")
                        return
                    epg_d, ch_n = bg_loop.run_until_complete(
                        _build_xmltv_index(xmltv_url, _log))
                    bg_loop.close()
                    # Post-download portal key check — download may have taken minutes
                    _cur2 = (f"{state.conn_type}:{state.url}"
                             f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
                    if _cur2 != _portal_key_bg:
                        _log(f"[EPG] Portal changed during XMLTV download — discarding")
                        return
                    _cache_ref_bg[ck] = (time.time(), epg_d, ch_n)
                    if not epg_d:
                        _no_data_ref_bg.add(ck)
                    _log(f"[EPG] Background XMLTV download complete for {ck}")
                except Exception as e:
                    _log(f"[EPG] Background XMLTV error for {ck}: {e}")
                finally:
                    _dl_ref_bg.discard(ck)
                    # Clear the "needs XMLTV" markers so all waiting channels get
                    # a fresh EPG lookup now that the data is available.
                    # Uses captured ref so this targets Portal 1's set, not Portal 2's.
                    _needs_ref_bg.clear()
                    # Evict stale empty per-channel EPG cache entries.
                    # Uses captured ref so Portal 2's fresh cache is never touched.
                    stale = [k for k, v in list(_epg_ref_bg.items())
                             if not v[1].get("current") and not v[1].get("next")
                             and not v[1].get("schedule")]
                    for k in stale:
                        _epg_ref_bg.pop(k, None)

            t = threading.Thread(target=_bg_download, daemon=True,
                                 name=f"xmltv-dl-{ck[:30]}")
            t.start()
            out["error"] = "EPG loading… please try again in a moment"
            return out

        # ── Resolve channel ID → programme list ───────────────────────────────────
        entries = None  # always initialised — avoids UnboundLocalError
        # ── 1. Match cache: if we already resolved this lookup → jump to the CID ──
        _match_key = (lookup, ck)
        _cached_match_cid = state._xmltv_match_cache.get(_match_key)
        if _cached_match_cid is not None:
            entries = epg_dict.get(_cached_match_cid) if _cached_match_cid else None
            if entries:
                _log(f"[EPG] XMLTV match-cache hit: {tvg_id!r} → {_cached_match_cid!r}")
            else:
                # CID was cached but no longer in index (feed refresh) — fall through
                del state._xmltv_match_cache[_match_key]
        if not entries:
            # ── 2. tvg-id direct lookup ────────────────────────────────────────────
            _tvg_as_id = tvg_id.strip()
            for _try_id in (_tvg_as_id, _tvg_as_id.lower(), _tvg_as_id.upper()):
                _candidate = epg_dict.get(_try_id)
                if _candidate:
                    entries = _candidate
                    _log(f"[EPG] XMLTV tvg-id direct hit: {tvg_id!r} → {_try_id!r}")
                    state._xmltv_match_cache[_match_key] = _try_id
                    break

        if not entries:
            # ── 3. Exact channel-ID lookup (lookup = tvg_id lowercased) ───────────
            entries = epg_dict.get(lookup)
            if entries:
                _log(f"[EPG] XMLTV exact-ID hit: {tvg_id!r} → {lookup!r} ({len(entries)} progs)")
                state._xmltv_match_cache[_match_key] = lookup

        # Fallback: scored display-name / proximity matching using region clusters.
        # Extract country code and strip noise — use module-level helpers.
        _cc_m = re.match(r'^' + r'([a-z]{2,4})' + r'\s*[|:]\s*', lookup)
        lookup_country = _cc_m.group(1) if _cc_m else ""
        lookup_core = _epg_strip_noise(lookup)

        def _name_matches(names, lc, cid):
            """Check if any (name, lang) pair in names matches lookup core lc.
            Returns (matched, lang_matches_country) tuple.
            Substring (n_core in lc) only accepted when channel has a country signal.
            """
            has_country_signal = bool(
                lookup_country and (
                    cid.endswith("." + lookup_country) or
                    any(lang and (lang == lookup_country or lang.startswith(lookup_country))
                        for _, lang, *_ in names)
                )
            )
            for n, lang, *_ in names:
                n_core = _epg_strip_noise(n)
                if not n_core:
                    continue
                lang_hit = lang == lookup_country or lang.startswith(lookup_country)
                if lc == n_core:
                    return True, lang_hit
                if len(n_core) >= 3 and n_core in lc:
                    if not lookup_country or has_country_signal:
                        return True, lang_hit
            return False, False

        if not entries and lookup_core:
            _log(f"[EPG] XMLTV fallback: lookup={lookup!r} core={lookup_core!r} country={lookup_country!r}")
            # ── Tier 0: construct XMLTV channel ID directly ───────────────────────
            if lookup_country:
                _core_nodots = lookup_core.replace(" ", "")
                _core_dots   = lookup_core.replace(" ", ".")
                for _cid_guess in [
                    _core_nodots + "." + lookup_country,
                    _core_nodots + "-" + lookup_country,
                    _core_nodots + lookup_country,
                    _core_dots   + "." + lookup_country,
                ]:
                    _log(f"[EPG] XMLTV trying constructed-ID: {_cid_guess!r}")
                    _candidate = epg_dict.get(_cid_guess)
                    if _candidate:
                        _log(f"[EPG] XMLTV constructed-ID match: {tvg_id!r} → {_cid_guess!r}")
                        entries = _candidate
                        state._xmltv_match_cache[_match_key] = _cid_guess
                        break

            # ── Tier 1: scored display-name matching ─────────────────────────────
            # Score 2 = name matches AND channel has country signal
            # Score 1 = name matches, no country signal (international)
            if not entries:
                best_cid = None
                best_score = -1
                score1_candidates = []
                for cid, names in chan_names.items():
                    cid_country_match = lookup_country and cid.endswith("." + lookup_country)
                    name_hit, lang_hit = _name_matches(names, lookup_core, cid)
                    if not name_hit:
                        continue
                    score = 2 if (lang_hit or cid_country_match) else 1
                    if score == 2:
                        candidate = epg_dict.get(cid)
                        if candidate:
                            best_score = 2
                            best_cid = cid
                            entries = candidate
                            break
                    else:
                        candidate = epg_dict.get(cid)
                        if candidate:
                            score1_candidates.append((cid, candidate))

                if best_score < 2 and score1_candidates:
                    if not lookup_country:
                        best_cid, entries = score1_candidates[0]
                        best_score = 1
                    elif len(score1_candidates) == 1:
                        best_cid, entries = score1_candidates[0]
                        best_score = 1
                        _log(f"[EPG] XMLTV: sole match {best_cid!r} for {tvg_id!r} (international)")
                    else:
                        # Multiple candidates — rank by geographic proximity
                        score1_candidates.sort(key=lambda x: _epg_proximity(x[0], lookup_country))
                        all_ranked = [(c, _epg_proximity(c, lookup_country)) for c, _ in score1_candidates]
                        _log(f"[EPG] XMLTV: {len(score1_candidates)} candidates for {tvg_id!r} (top 5):")
                        for _c, _r in all_ranked[:5]:
                            _log(f"[EPG]   rank={_r:3d}  {_c}")
                        best_cid, entries = score1_candidates[0]
                        best_score = 1
                        # If the closest geographic match is rank >= 6, it's a language
                        # the user probably can't read (e.g. Greek, Bulgarian for a Serbian
                        # channel). Prefer an English-language version (.us/.uk/.gb/.au)
                        # if one exists among the candidates — English is more universally
                        # readable than a geographically "close" but foreign-language match.
                        _best_rank = all_ranked[0][1] if all_ranked else 0
                        if _best_rank >= 6:
                            _EN_SUFFIXES = {"us", "uk", "gb", "au", "nz", "ca", "ie"}
                            for _cid, _cand in score1_candidates:
                                if _epg_cid_suffix(_cid) in _EN_SUFFIXES and _cand:
                                    _log(f"[EPG] XMLTV: best rank={_best_rank} ≥ 6 — preferring English: {_cid!r}")
                                    best_cid, entries = _cid, _cand
                                    break

                if best_score >= 0 and best_cid:
                    _log(f"[EPG] XMLTV best candidate: score={best_score} cid={best_cid!r} for {tvg_id!r}")
                if entries:
                    _log(f"[EPG] XMLTV display-name fallback (score={best_score}): {tvg_id!r} → {best_cid!r}")
                    state._xmltv_match_cache[_match_key] = best_cid

        # ── Tier 2: fuzzy name match (Levenshtein) ────────────────────────────────
        # Only for names >= 8 chars. Threshold scales with name length.
        _fuzz_best_cid = None
        _fuzz_best_dist = 3
        _fuzz_min_len = 8
        _fuzz_threshold = max(1, len(lookup_core) // 8)
        if not entries and lookup_core and len(lookup_core) >= _fuzz_min_len:
            _fuzz_best_rank = 999
            for cid, names in chan_names.items():
                for n, lang, *_ in names:
                    n_core = _epg_strip_noise(n)
                    if not n_core or len(n_core) < _fuzz_min_len:
                        continue
                    if abs(len(n_core) - len(lookup_core)) > _fuzz_threshold + 1:
                        continue
                    dist = _epg_levenshtein(lookup_core, n_core)
                    if dist > _fuzz_threshold:
                        continue
                    prox = _epg_proximity(cid, lookup_country)
                    if dist < _fuzz_best_dist or (dist == _fuzz_best_dist and prox < _fuzz_best_rank):
                        candidate = epg_dict.get(cid)
                        if candidate:
                            _fuzz_best_dist = dist
                            _fuzz_best_rank = prox
                            _fuzz_best_cid = cid
                            entries = candidate
            if entries:
                _log(f"[EPG] XMLTV fuzzy match (dist={_fuzz_best_dist}): {tvg_id!r} → {_fuzz_best_cid!r}")
                state._xmltv_match_cache[_match_key] = _fuzz_best_cid

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

        # Filter to window around now (keep past 1h and next 24h)
        window = [e for e in entries if e["end"] >= now - 3600 and e["start"] <= now + 86400]
        if not window:
            window = entries  # fallback: no filtering
        window.sort(key=lambda x: x["start"])
        out["schedule"] = window[:48]

        for ep in window:
            if ep["start"] <= now < ep["end"]:
                out["current"] = ep
            elif ep["start"] > now and out["next"] is None:
                out["next"] = ep

        _cur = out["current"]["title"] if out["current"] else None
        _nxt = out["next"]["title"] if out["next"] else None
        _log(f"[EPG] XMLTV result: current={_cur!r} next={_nxt!r}")
        return out


    # ── /api/epg/ui.js ────────────────────────────────────────────────────────
    _EPG_UI_JS_BYTES = _EPG_UI_JS.encode("utf-8")

    @flask_app.route("/api/epg/ui.js")
    def api_epg_ui_js():
        return Response(
            _EPG_UI_JS_BYTES,
            content_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # Expose _build_xmltv_index at module level so start_epg_prefetch() can
    # call it without being inside this closure.
    import epg_addon as _self
    _self._build_xmltv_index_ref = _build_xmltv_index
    state.log("[EPG] Routes registered: /api/epg  /api/epg_status  /api/whats_on"
              "  /api/find_channel  /api/catchup  /api/catchup/play")


# ===================== EPG CONNECT-TIME PREFETCH =====================

def start_epg_prefetch(state):
    """Called from api_connect after a successful portal connect.

    Builds the same ek_combined URL that api_whats_on uses, registers it
    (and each constituent URL) in state._xmltv_downloading, then downloads
    and indexes the EPG in a daemon thread.

    All EPG consumers already guard against their target URL being in
    _xmltv_downloading before spawning their own download — so they get an
    immediate "loading" response and retry rather than issuing a parallel
    HTTP request to the same source.

    After the download completes the result is stored under ek_combined AND
    under each individual URL, so every consumer's cache lookup hits:
      • api_whats_on            → keyed on ek_combined
      • _fetch_xmltv_epg        → keyed on state.ext_epg_url  (method 3)
      • _fetch_xmltv_epg        → keyed on base_norm          (Xtream method 2)
      • api_epg exception path  → checks state.ext_epg_url
    """
    if _build_xmltv_index_ref is None:
        return  # register_epg_routes not yet called

    from urllib.parse import quote as _q2, urlparse as _up2

    ek = state.ext_epg_url
    _portal_xmltv = ""
    _base_norm = ""
    _conn = state.conn_type

    if _conn == "xtream" or (_conn == "m3u_url" and state.m3u_xtream_override):
        try:
            _creds = state.m3u_xtream_override if _conn == "m3u_url" else None
            _base  = (_creds["base"] if _creds else state.url).rstrip("/")
            _user  = _creds["username"] if _creds else state.username
            _pwd   = _creds["password"] if _creds else state.password
            if _base and _user and _pwd:
                _pn = _up2(_base)
                _base_norm = f"{_pn.scheme}://{_pn.netloc}"
                _portal_xmltv = (f"{_base_norm}/xmltv.php"
                                 f"?username={_q2(_user, safe='')}"
                                 f"&password={_q2(_pwd, safe='')}")
        except Exception:
            _portal_xmltv = ""
            _base_norm = ""

    _all_urls = [u.strip() for u in (ek or "").splitlines() if u.strip()]
    if _portal_xmltv and _portal_xmltv not in _all_urls:
        _all_urls.append(_portal_xmltv)

    if not _all_urls:
        return  # no EPG URLs configured for this portal

    ek_combined = "\n".join(_all_urls)

    # All the distinct cache keys that EPG consumers will look up.
    # Registering every key in _xmltv_downloading before the thread starts
    # ensures no consumer spawns a parallel download for any of them.
    _keys: set = {ek_combined}
    if ek:
        _keys.add(ek)
    if _base_norm:
        _keys.add(_base_norm)        # Xtream method-2 cache key
    if _portal_xmltv:
        _keys.add(_portal_xmltv)
    _keys_list = list(_keys)

    # Skip if already cached or in flight for every key
    if all(k in state._xmltv_cache or k in state._xmltv_no_data
           for k in _keys_list):
        return
    if any(k in state._xmltv_downloading for k in _keys_list):
        return   # prefetch (or another consumer) already started

    # Register all keys atomically before the thread starts
    for _k in _keys_list:
        state._xmltv_downloading.add(_k)

    if len(_all_urls) > 1:
        for _i, _u in enumerate(_all_urls, 1):
            state.log(f"[EPG] Connect-time prefetch [{_i}/{len(_all_urls)}]: {_u}")
    else:
        state.log(f"[EPG] Connect-time prefetch: {ek_combined}")

    # Capture portal identity and mutable-object references NOW, before the
    # thread starts.  If the user reconnects while the download is in flight:
    # - The portal-key check aborts the write phase silently.
    # - The finally block uses the CAPTURED set/dict references, not
    #   state._xmltv_downloading (which api_connect replaces with a new set),
    #   so it cannot contaminate the new portal's fresh state.
    _pf_key          = (f"{state.conn_type}:{state.url}"
                        f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
    _downloading_ref = state._xmltv_downloading
    _cache_ref       = state._xmltv_cache
    _no_data_ref     = state._xmltv_no_data
    _epg_cache_ref   = state._epg_cache
    _needs_ref       = state._xmltv_needs

    def _bg_epg():
        try:
            bg_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(bg_loop)

            # Abort if portal changed since connect (user reconnected quickly)
            _cur_key = (f"{state.conn_type}:{state.url}"
                        f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
            if _cur_key != _pf_key:
                state.log("[EPG] Prefetch: portal changed — aborting")
                return

            epg_d, ch_n = bg_loop.run_until_complete(
                _build_xmltv_index_ref(ek_combined, state.log))

            # Verify portal still matches after the (potentially long) download
            _cur_key2 = (f"{state.conn_type}:{state.url}"
                         f":{getattr(state,'mac','')}:{getattr(state,'username','')}")
            if _cur_key2 != _pf_key:
                state.log("[EPG] Prefetch: portal changed during download — discarding")
                return

            _ts_now = time.time()
            # Store under every key so all consumers get a cache hit.
            # Use the captured dict reference so a concurrent reconnect
            # (which replaces state._xmltv_cache) does not get stale data.
            for _k in _keys_list:
                if _k not in _cache_ref:
                    _cache_ref[_k] = (_ts_now, epg_d, ch_n)
            if not epg_d:
                for _k in _keys_list:
                    _no_data_ref.add(_k)
                state.log("[EPG] Prefetch: XMLTV has no programme data")
            else:
                state.log(f"[EPG] ✓ Prefetch complete — "
                          f"{len(epg_d)} channels indexed across "
                          f"{len(_keys_list)} cache key(s)")

        except Exception as _e:
            state.log(f"[EPG] ✗ Prefetch error: {_e}")
        finally:
            bg_loop.close()
            # Always unblock all registered keys — even on failure — so no
            # consumer hangs waiting for a download that died.
            for _k in _keys_list:
                _downloading_ref.discard(_k)
            _needs_ref.clear()
            # Evict stale empty per-channel EPG cache entries so every
            # channel gets a fresh lookup now that the index is available.
            stale = [k for k, v in list(_epg_cache_ref.items())
                     if not v[1].get("current") and not v[1].get("next")
                     and not v[1].get("schedule")]
            for k in stale:
                _epg_cache_ref.pop(k, None)

    threading.Thread(target=_bg_epg, daemon=True, name="epg-prefetch").start()


# ===================== FRONTEND (CSS + HTML + JS) =====================

_EPG_UI_JS = r"""
/* ── Inject CSS ─────────────────────────────────────────────────────── */
(function(){
  const s = document.createElement('style');
  s.textContent = `
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
.epg-ch-cell::before{content:'';position:absolute;top:0;left:0;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.15),transparent);
  transform:translateX(-100%);transition:transform .45s ease;pointer-events:none}
.epg-ch-cell:hover::before{transform:translateX(100%)}
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
  background:var(--s3);overflow:hidden}
.epg-prog-loading::after{content:'';position:absolute;inset:0;
  background:linear-gradient(90deg,transparent 0%,rgba(255,255,255,.06) 50%,transparent 100%);
  transform:translateX(-100%);will-change:transform;
  animation:skel-sweep 1.4s ease-in-out infinite}
.epg-layout-btn{display:flex;align-items:center;gap:4px;padding:4px 9px;
  font-size:11px;font-weight:700;border-radius:14px;border:1.5px solid var(--bdr2);
  background:var(--s2);color:var(--txt2);cursor:pointer;transition:var(--tr);flex-shrink:0;
  white-space:nowrap}
.epg-layout-btn:hover{border-color:var(--acc);color:var(--acc)}
.epg-layout-btn.active{background:rgba(139,92,246,.15);border-color:var(--acc);color:var(--acc)}

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
  position:relative}
.won-item::before{content:'';position:absolute;top:0;left:0;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.05),transparent);
  transform:translateX(-100%);transition:transform .45s ease;pointer-events:none;overflow:hidden}
.won-item:hover{background:rgba(124,58,237,.08);border-color:rgba(124,58,237,.2);
  box-shadow:0 0 10px rgba(124,58,237,.07)}
.won-item:hover::before{transform:translateX(100%)}
.won-item:active{transform:scale(.98)}
.won-item-logo{flex-shrink:0;width:36px;height:36px;display:flex;align-items:center;justify-content:center}
.won-ch-logo{width:36px;height:36px;object-fit:contain;border-radius:4px}
.won-ch-logo-placeholder{width:36px;height:36px;border-radius:4px;background:rgba(255,255,255,.04)}
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
.won-find-result{font-size:10px;margin-top:4px;padding:3px 9px;border-radius:6px;border:1px solid transparent;display:none}
.won-find-result.ok{background:rgba(34,197,94,.18);color:var(--green);display:block;
  border-color:rgba(34,197,94,.3);cursor:pointer;transition:background .15s}
.won-find-result.ok:hover{background:rgba(34,197,94,.32)}
.won-find-result.ok:active{background:rgba(34,197,94,.45)}
.won-find-result.fail{background:rgba(239,68,68,.13);color:#f87171;display:block}
.won-find-result.playing{background:rgba(59,130,246,.18);color:#60a5fa;display:block;cursor:default}
.won-ext-wrap{display:flex;flex-direction:row;gap:6px;align-items:center;margin-top:3px;width:100%}
.won-ext-wrap>.won-ext-btn{flex:2;text-align:center}
.won-ext-wrap>.cast-ext-btn{flex:1;justify-content:center}
.won-ext-btn{display:inline-block;font-size:10px;padding:3px 9px;border-radius:6px;
  border:1px solid rgba(139,92,246,.3);background:rgba(139,92,246,.18);color:#a78bfa;cursor:pointer;transition:background .15s}
.won-ext-btn:hover{background:rgba(139,92,246,.32)}
.won-ext-btn:active{background:rgba(139,92,246,.45)}
.won-empty{text-align:center;padding:48px 20px;color:var(--txt3);font-size:13px}
/* ── EPG expand overlay ─────────────────────────────────────────────── */
#epg-expand-overlay{position:fixed;inset:0;z-index:600;background:rgba(0,0,0,.65);
  display:none;align-items:center;justify-content:center}
#epg-expand-overlay.open{display:flex}
#epg-expand-modal{background:var(--s2);border-radius:14px;
  width:min(1400px,96vw);height:90vh;
  display:flex;flex-direction:column;overflow:hidden;
  box-shadow:0 24px 80px rgba(0,0,0,.7);animation:pop-in .2s ease}
#epg-expand-hdr{display:flex;align-items:center;gap:10px;padding:10px 16px;
  border-bottom:1px solid var(--s4);flex-shrink:0}
#epg-expand-hdr h3{flex:1;margin:0;font-size:14px;font-weight:700}
#epg-expand-body{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.won-empty span{font-size:40px;display:block;margin-bottom:10px;opacity:.3}
.won-loading{display:flex;align-items:center;justify-content:center;gap:10px;
  padding:40px 20px;color:var(--txt3);font-size:13px}
.won-ftr{padding:10px 14px;border-top:1px solid var(--s4);display:flex;
  justify-content:flex-end;flex-shrink:0}
@media(max-width:600px){
  #won-modal{width:100vw;max-height:100vh;border-radius:0}
}
`;
  document.head.appendChild(s);
})();

/* ── Inject HTML overlays ───────────────────────────────────────────── */
(function(){
  const d = document.createElement('div');
  d.innerHTML = `
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

<div id="epg-expand-overlay" onclick="if(event.target===this)closeEpgExpandOverlay()">
  <div id="epg-expand-modal">
    <div id="epg-expand-hdr">
      <h3>📅 EPG Grid</h3>
      <button class="btn-ghost" onclick="closeEpgExpandOverlay()" style="height:28px;padding:0 10px;font-size:12px">✕ Restore</button>
    </div>
    <div id="epg-expand-body"></div>
  </div>
</div>
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
`;
  while(d.firstChild) document.body.appendChild(d.firstChild);
})();

/* ── EPG JS ─────────────────────────────────────────────────────────── */
// ── EPG ────────────────────────────────────────────────────────────────────
let _epgItem=null;

// Xtream: tv_archive=1 → catchup supported, 0 → not supported.
// MAC/Stalker/M3U: field absent → allow (API handles gracefully).
function _channelSupportsCatchup(it){
  if(!it) return false;
  if('tv_archive' in it) return it.tv_archive===1 || it.tv_archive==='1';
  return true;
}
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
    if(d.error&&!d.current&&!d.next&&!(d.schedule&&d.schedule.length)){
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
  // Show expand button on desktop only
  if(!_isMobile){
    const _eb = document.getElementById('epg-expand-btn');
    if(_eb) _eb.style.display = '';
  }
  _buildEpgGrid(filtItems);

  // ── Click-drag scroll on desktop (on the timeline column) ────────────────
  const wrap = document.getElementById('epg-tl-col');
  const chCol2 = document.getElementById('epg-ch-col');
  if(wrap && !wrap._dragScrollAttached){
    // Sync vertical scroll between timeline col and ch col — bidirectional.
    // Guard flag prevents mutual updates from triggering an infinite loop.
    let _syncingScroll = false;
    const onTlScroll = () => {
      if(_syncingScroll) return;
      _syncingScroll = true;
      if(chCol2) chCol2.scrollTop = wrap.scrollTop;
      _syncingScroll = false;
    };
    const onChScroll = () => {
      if(_syncingScroll) return;
      _syncingScroll = true;
      wrap.scrollTop = chCol2.scrollTop;
      _syncingScroll = false;
    };
    wrap.addEventListener('scroll', onTlScroll, {passive: true});
    chCol2.addEventListener('scroll', onChScroll, {passive: true});
    wrap._syncScrollCleanup = () => {
      wrap.removeEventListener('scroll', onTlScroll);
      chCol2.removeEventListener('scroll', onChScroll);
    };

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
  // Hide expand button and close overlay silently if open
  const _eb3 = document.getElementById('epg-expand-btn');
  if(_eb3) _eb3.style.display = 'none';
  _closeEpgExpandOverlaySilent();
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
    return `<div class="epg-ch-cell" id="epg-ch-${i}" onclick="epgExpandCloseForPlay();playItem(${i})" title="Play ${esc(name)}">
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
      el.onclick = (e) => { e.stopPropagation(); epgExpandCloseForPlay(); playItem(idx); };
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

// ── EPG expand overlay (desktop only) ────────────────────────────────────────
// var (not let/const) so inline onclick="..." attribute handlers can reach it
// through window scope — let/const at script top-level are NOT window properties.
var _epgExpandOpen = false;

// Public helper — open the expanded overlay.
function epgExpandOpen(){
  if(_isMobile || !_epgGridActive) return;
  const overlay = document.getElementById('epg-expand-overlay');
  const body    = document.getElementById('epg-expand-body');
  const wrap    = document.getElementById('epg-grid-wrap');
  if(!overlay || !body || !wrap) return;
  body.appendChild(wrap);
  wrap.classList.add('active');
  overlay.classList.add('open');
  document.body.style.overflow = 'hidden';
  _epgExpandOpen = true;
  _buildEpgGrid(filtItems);
}

// Public helper — close and restore, with grid rebuild.
// Use for the Restore button; caller stays in EPG view so rebuild is needed.
function epgExpandClose(){
  if(!_epgExpandOpen) return;
  _restoreEpgGridWrap();
  const overlay = document.getElementById('epg-expand-overlay');
  if(overlay) overlay.classList.remove('open');
  document.body.style.overflow = '';
  _epgExpandOpen = false;
  if(_epgGridActive) _buildEpgGrid(filtItems);
}

// Public helper — close silently, NO grid rebuild.
// Use when caller is about to navigate away (play a channel).
function epgExpandCloseForPlay(){
  if(!_epgExpandOpen) return;
  _restoreEpgGridWrap();
  const overlay = document.getElementById('epg-expand-overlay');
  if(overlay) overlay.classList.remove('open');
  document.body.style.overflow = '';
  _epgExpandOpen = false;
}

// Backward-compat aliases for any internal callers using the old names
function openEpgExpandOverlay()     { epgExpandOpen(); }
function closeEpgExpandOverlay()    { epgExpandClose(); }
function _closeEpgExpandOverlaySilent() { epgExpandCloseForPlay(); }

function _restoreEpgGridWrap(){
  const wrap   = document.getElementById('epg-grid-wrap');
  const pItems = document.getElementById('p-items');
  if(!wrap || !pItems) return;
  // Move epg-grid-wrap back into p-items (after ilist div)
  const ilist = document.getElementById('ilist');
  if(ilist && ilist.nextSibling){
    pItems.insertBefore(wrap, ilist.nextSibling);
  } else {
    pItems.appendChild(wrap);
  }
}

// ── Copy activity log ─────────────────────────────────────────────────────────
function copyLog(){
  const d = document.getElementById('desktop-logout');
  const m = document.getElementById('logout');
  const text = [d,m].map(el=>el?el.innerText.trim():'').filter(Boolean).join('\n');
  if(!text){ toast('Log is empty','wrn'); return; }
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text)
      .then(()=>toast('Log copied','ok'))
      .catch(()=>_copyLogFallback(text));
  } else {
    _copyLogFallback(text);
  }
}
function _copyLogFallback(text){
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px';
  document.body.appendChild(ta);
  ta.select();
  try{ document.execCommand('copy'); toast('Log copied','ok'); }
  catch(e){ toast('Copy failed: '+e,'err'); }
  document.body.removeChild(ta);
}

// Show/hide EPG grid button based on mode and whether items are loaded
function _updateEpgGridBtn(){
  const btn = document.getElementById('epg-grid-btn');
  if(!btn) return;
  btn.style.display = (mode === 'live' && filtItems.length > 0) ? '' : 'none';
  // If grid is open but mode changed away from live, close it
  if(_epgGridActive && mode !== 'live') _closeEpgGrid();
}


/* ── CATCH-UP TV JS ─────────────────────────────────────────────────── */
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

function closeCatchup(){document.getElementById('catchup-overlay').style.display='none';_mvCatchupCtx=null;}
document.getElementById('catchup-overlay').addEventListener('click',function(e){if(e.target===this)closeCatchup();});

function _cuFmtTime(ts){const d=new Date(ts*1000);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}
function _cuFmtDate(ts){const d=new Date(ts*1000);return d.toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'});}
// Display-only helpers — read directly from start_str ("YYYY-MM-DD HH:MM" server-local)
// to avoid Unix-timestamp timezone ambiguity. Catchup request values are untouched.
function _cuDispTime(str){ return str ? str.slice(-5) : ''; }
function _cuDispDate(str){
  if(!str) return '';
  const parts = str.split(' ')[0].split('-').map(Number);
  if(parts.length < 3) return '';
  const dt = new Date(Date.UTC(parts[0], parts[1]-1, parts[2]));
  return dt.toLocaleDateString([], {weekday:'short',month:'short',day:'numeric',timeZone:'UTC'});
}
function _cuDispEndTime(startStr, startTs, stopTs){
  if(!startStr || !startTs || !stopTs) return '';
  const dur = Math.round((stopTs - startTs) / 60);
  const hm = startStr.slice(-5).split(':');
  if(hm.length < 2) return '';
  const totalMins = parseInt(hm[0]) * 60 + parseInt(hm[1]) + dur;
  const hEnd = Math.floor(totalMins / 60) % 24;
  const mEnd = totalMins % 60;
  return String(hEnd).padStart(2,'0') + ':' + String(mEnd).padStart(2,'0');
}

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
  const isSynthetic = listings.length > 0 && listings.every(p => p.synthetic);
  // Show all programmes; highlight archived ones. Non-archived are dimmed.
  let lastDate='';
  const rows=listings.map(p=>{
    const hasArchive=(p.mark_archive==='1'||p.mark_archive===1);
    const dateStr=p.start_str?_cuDispDate(p.start_str):(p.start?_cuFmtDate(p.start):'');
    let dateHdr='';
    if(dateStr&&dateStr!==lastDate){
      lastDate=dateStr;
      dateHdr=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--acc);padding:8px 0 4px">${dateStr}</div>`;
    }
    const t=p.start_str
      ?`${_cuDispTime(p.start_str)}–${_cuDispEndTime(p.start_str,p.start,p.stop)}`
      :(p.start&&p.stop?`${_cuFmtTime(p.start)}–${_cuFmtTime(p.stop)}`:(p.start?_cuFmtTime(p.start):''));
    const cmdSafe=encodeURIComponent(p.cmd||'');
    const liveCmdSafe=encodeURIComponent(p.live_cmd||'');
    const realIdSafe=encodeURIComponent(p.epg_id||p.id||'');
    const titleDisplay=p.title||(p.synthetic?'—':'Unknown');
    const titleSafe=titleDisplay.replace(/'/g,"\\'");
    // start_str is the portal's own server-local datetime string ("YYYY-MM-DD HH:MM").
    // Passing it bypasses the DST-sensitive epoch+offset conversion in api_catchup_play
    // so the timeshift URL always uses the portal's stored time directly.
    const startStrSafe=encodeURIComponent(p.start_str||'');
    const opacity=hasArchive?'1':'0.4';
    const click=hasArchive
      ?`onclick="doPlayArchiveCmd('${cmdSafe}',${p.start||0},${p.stop||0},'${titleSafe}','${liveCmdSafe}','${realIdSafe}','${startStrSafe}')"`
      :'';
    const extBtn=hasArchive
      ?`<button class="btn-ghost" onclick="event.stopPropagation();doExternalArchiveCmd('${cmdSafe}',${p.start||0},${p.stop||0},'${titleSafe}','${liveCmdSafe}','${realIdSafe}','${startStrSafe}')" title="Play in external player" style="padding:0 6px;font-size:13px;flex-shrink:0">🎬</button>`
      :'';
    const cursor=hasArchive?'pointer':'default';
    const archIcon=hasArchive?'<span style="font-size:14px;color:var(--acc)">▶</span>':'';
    const titleColor=p.synthetic?'var(--txt3)':'var(--txt1)';
    return dateHdr+`<div ${click}
      style="display:flex;align-items:center;gap:10px;padding:10px 8px;border-radius:var(--rsm);cursor:${cursor};
             border-left:3px solid var(--s4);margin-bottom:4px;background:var(--s3);
             transition:background .15s;opacity:${opacity}"
      ${hasArchive?'onmouseover="this.style.background=\'var(--s4)\'" onmouseout="this.style.background=\'var(--s3)\'"':''}>
      <span style="font-size:11px;color:var(--txt3);white-space:nowrap;min-width:90px">${t}</span>
      <span style="flex:1;font-size:12px;font-weight:600;color:${titleColor}">${titleDisplay}</span>
      ${extBtn}
      ${archIcon}
    </div>`;
  }).join('');
  const syntheticNotice = isSynthetic
    ? `<div style="font-size:11px;color:var(--txt3);background:var(--s3);border-radius:var(--rsm);
                  padding:8px 10px;margin-bottom:10px;text-align:center;border-left:3px solid var(--bdr2)">
        ⚠ No programme data — showing 1-hour time slots. Click any slot to watch from that time.
       </div>`
    : '';
  document.getElementById('catchup-body').innerHTML=
    syntheticNotice+rows+'<div style="padding-top:8px;border-top:1px solid var(--bdr)">'+_cuManualForm()+'</div>';
}

function doPlayArchiveCmd(encodedCmd, startTs, stopTs, title, encodedLiveCmd, encodedRealId, encodedStartStr){
  const cmd=decodeURIComponent(encodedCmd||'');
  const liveCmd=decodeURIComponent(encodedLiveCmd||'');
  const realId=decodeURIComponent(encodedRealId||'');
  const startStr=decodeURIComponent(encodedStartStr||'');
  const status=document.getElementById('catchup-status');
  if(status) status.textContent='Resolving…';
  fetch('/api/catchup/play',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd, live_cmd:liveCmd, epg_id:realId, start:startTs, stop:stopTs, start_str:startStr})})
  .then(r=>r.json()).then(d=>{
    if(d.url){
      const label=(_epgItem?_epgItem.name:'')+' — '+title+' [↺]';
      // If catchup was opened from a multiview widget, route back to that slot
      const _cuMvCtx = _mvCatchupCtx;
      closeCatchup(); // also clears _mvCatchupCtx
      if(_cuMvCtx && _cuMvCtx.wid && _cuMvCtx.cEl){
        const synth = {
          name:        label,
          _direct_url: d.url,
          id:          'catchup-'+Date.now(),
          _is_live:    false,
        };
        _mvPlayChannel(_cuMvCtx.wid, synth, _cuMvCtx.cEl);
        toast('↺ Playing catch-up in Multi-View: '+title,'ok');
      } else {
        // Pass raw URL — doPlay always wraps in /api/proxy itself
        // Catchup is VOD — isLive:false prevents mpegts.js SourceBuffer crash
        // d.fallback_url is the query-string format; used if path-based .ts fails
        // d.duration_secs lets mpegts.js set a real duration so the seek bar works
        doPlay(d.url, label, {isLive:false, fallbackUrl:d.fallback_url||null, durationSecs:d.duration_secs||0});
        toast('↺ Playing catch-up: '+title,'ok');
      }
    } else {
      if(status) status.textContent='❌ '+(d.error||'Not available');
    }
  }).catch(e=>{if(status) status.textContent='❌ '+e.message;});
}

async function doExternalArchiveCmd(encodedCmd, startTs, stopTs, title, encodedLiveCmd, encodedRealId, encodedStartStr){
  const cmd=decodeURIComponent(encodedCmd||'');
  const liveCmd=decodeURIComponent(encodedLiveCmd||'');
  const realId=decodeURIComponent(encodedRealId||'');
  const startStr=decodeURIComponent(encodedStartStr||'');
  const status=document.getElementById('catchup-status');
  if(status) status.textContent='Resolving for external player…';
  try{
    const r=await fetch('/api/catchup/play',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cmd, live_cmd:liveCmd, epg_id:realId, start:startTs, stop:stopTs, start_str:startStr})});
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
  const s=(document.getElementById('cu-start')||{}).value;
  const e=(document.getElementById('cu-end')||{}).value;
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
  const liveCmd=(_epgItem&&(_epgItem.stream_id||_epgItem.cmd))||'';
  const cmd=encodeURIComponent((match&&match.cmd)||liveCmd);
  const live_cmd=encodeURIComponent((match&&match.live_cmd)||liveCmd);
  const epg_id=encodeURIComponent((match&&(match.epg_id||match.id))||'');
  const title=(match&&match.title)||'';
  const useStop=(match&&match.stop)||endTs;
  doPlayArchiveCmd(cmd, startTs, useStop, title, live_cmd, epg_id);
}



/* ── WHAT'S ON NOW JS ───────────────────────────────────────────────── */
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
    const logoHtml = p.logo
      ? `<img src="${esc(p.logo)}" class="won-ch-logo" alt="" loading="lazy"
              onerror="this.style.display='none'">`
      : `<div class="won-ch-logo-placeholder"></div>`;
    return `<div class="won-item" title="${esc(p.desc||'')}">
      <div class="won-item-logo">${logoHtml}</div>
      <div class="won-item-info">
        <div class="won-item-title">${esc(p.title)}</div>
        <div class="won-item-ch">${esc(p.channel_name)}</div>
        <div class="won-item-times">${start} – ${end}</div>
        <div class="won-find-result" id="won-res-${i}"></div>
        <div id="won-ext-${i}" class="won-ext-wrap" style="display:none">
          <span class="won-ext-btn"
            onclick="wonOpenExternal(${i})">🎬 External Player</span>
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
  if(!ch){ return; }

  resEl.className = 'won-find-result playing';
  resEl.textContent = '⟳ Resolving ' + name + '…';
  resEl.onclick = null;

  try {
    const r = await fetch('/api/resolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item: ch, mode: 'live', category: curCat || {}})
    });
    const d = await r.json();
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

  btn.classList.add('loading');
  btn.textContent = '⏳';
  res.className = 'won-find-result';
  res.textContent = '';

  fetch('/api/find_channel', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({channel_name: channelName, channel_id: channelId})
  })
  .then(r => r.json())
  .then(data => {
    btn.classList.remove('loading');
    btn.textContent = '🔍';
    if(data.found){
      const cat = data.cat ? ` · ${data.cat}` : '';
      res.className = 'won-find-result ok';
      res.textContent = `▶ ${data.name}${cat} (${data.score}%) — Tap to Play`;
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
    btn.classList.remove('loading');
    btn.textContent = '🔍';
    res.className = 'won-find-result fail';
    res.textContent = '✗ Request failed: ' + e;
  });
}
	
/* ── EPG Now-Playing overlay ─────────────────────────────────────────────────
   Mirrors the probe stats overlay architecture exactly:
   - Same IIFE, same vwrap-appended button+panel, same hover/touch/sticky logic
   - Intercepts /api/resolve to detect channel switches (live only)
   - Fetches /api/epg once per channel, refreshes at programme boundary
   - Position: top-left (probe owns top-right)
   ─────────────────────────────────────────────────────────────────────────── */
(function(){
  /* ── CSS ─────────────────────────────────────────────────────────────── */
  (function(){
    const s = document.createElement('style');
    s.textContent = `
#epg-now-btn{
  position:absolute;bottom:94px;right:8px;
  display:flex;align-items:center;gap:5px;
  background:rgba(10,12,20,.68);
  border:1px solid rgba(255,255,255,.13);
  border-radius:4px;
  padding:3px 8px 3px 7px;
  cursor:pointer;z-index:31;
  font-size:11px;font-weight:600;
  color:#c4b5fd;
  user-select:none;white-space:nowrap;
  backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);
  max-width:240px;overflow:hidden;text-overflow:ellipsis;
  opacity:0;pointer-events:none;
  transition:opacity .2s ease,background .15s,border-color .15s;
}
#epg-now-btn.en-hover  { opacity:1;pointer-events:auto; }
#epg-now-btn.en-sticky { opacity:1;pointer-events:auto; }
#epg-now-btn:hover     { background:rgba(30,35,55,.82);border-color:rgba(255,255,255,.25); }
#epg-now-btn.en-open   { background:rgba(30,35,55,.88);border-color:rgba(196,181,253,.35); }
#epg-now-panel{
  position:absolute;bottom:120px;right:8px;
  display:none;flex-direction:column;gap:0;
  background:rgba(10,12,20,.85);
  border:1px solid rgba(255,255,255,.10);
  border-radius:5px;
  padding:9px 12px 8px;
  z-index:30;
  max-width:320px;
  font-size:11.5px;line-height:1.55;
  color:#dde4f0;
  backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);
  pointer-events:auto;
  animation:en-rise .15s ease;
}
#epg-now-panel.en-open{ display:flex; }
@keyframes en-rise{
  from{opacity:0;transform:translateY(4px);}
  to{opacity:1;transform:translateY(0);}
}
@media(hover:none){
  #epg-now-btn.en-touch-visible{ opacity:1;pointer-events:auto; }
  #epg-now-btn  { bottom:61px; }
  #epg-now-panel{ bottom:87px; }
}
.en-time { color:#6ee7b7;font-size:10px;font-weight:700;letter-spacing:.4px;margin-bottom:3px; }
.en-title{ color:#f1f5f9;font-size:12.5px;font-weight:700;margin-bottom:4px;line-height:1.35; }
.en-desc { color:#94a3b8;font-size:10.5px;line-height:1.5;max-width:300px; }
.en-divider{ height:1px;background:rgba(255,255,255,.07);margin:6px 0; }
.en-next-lbl{ color:#4a5a78;font-size:9px;text-transform:uppercase;letter-spacing:.7px;margin-bottom:2px; }
.en-next-title{ color:#7c8fa8;font-size:10.5px; }
.en-next-time { color:#4a5a78;font-size:10px;margin-right:5px; }
`;
    document.head.appendChild(s);
  })();
  /* ── State ──────────────────────────────────────────────────────────── */
  let _btn        = null;
  let _panel      = null;
  let _open       = false;
  let _sticky     = false;
  let _hasData    = false;
  let _curItem    = null;   // last resolved live item
  let _curData    = null;   // last EPG response {current, next}
  let _refreshTmr = null;   // setTimeout to refresh at programme boundary
  let _revealTmr  = null;
  const _isTouch  = window.matchMedia('(hover:none)').matches;
  /* ── Touch reveal ───────────────────────────────────────────────────── */
  function _reveal(ms){
    if(!_btn || !_hasData) return;
    if(_revealTmr){ clearTimeout(_revealTmr); _revealTmr = null; }
    _btn.classList.add('en-touch-visible');
    if(_sticky) return;
    _revealTmr = setTimeout(function(){
      _revealTmr = null;
      if(!_sticky) _btn.classList.remove('en-touch-visible');
    }, ms);
  }
  function _cancelReveal(){
    if(_revealTmr){ clearTimeout(_revealTmr); _revealTmr = null; }
    if(_btn) _btn.classList.remove('en-touch-visible');
  }
  /* ── DOM setup ──────────────────────────────────────────────────────── */
  function _ensureEls(){
    if(_btn) return true;
    const vwrap = document.getElementById('vwrap');
    if(!vwrap) return false;
    _btn = document.createElement('div');
    _btn.id = 'epg-now-btn';
    _btn.textContent = '📺';
    vwrap.appendChild(_btn);
    _panel = document.createElement('div');
    _panel.id = 'epg-now-panel';
    vwrap.appendChild(_panel);
    /* Desktop: hover shows/hides button */
    if(!_isTouch){
      vwrap.addEventListener('mouseenter', function(){
        if(!_hasData) return;
        _btn.classList.add('en-hover');
      });
      vwrap.addEventListener('mouseleave', function(){
        _btn.classList.remove('en-hover');
        if(!_sticky && _open){
          _btn.classList.remove('en-open');
          _panel.classList.remove('en-open');
          _open = false;
        }
      });
    }
    /* Mobile: tap on player area reveals button */
    if(_isTouch){
      vwrap.addEventListener('touchstart', function(e){
        if(!_hasData) return;
        if(!_btn.contains(e.target) && !_panel.contains(e.target)) _reveal(4000);
      }, {passive:true});
      document.addEventListener('touchstart', function(e){
        if(_open && _panel && !_panel.contains(e.target) && !_btn.contains(e.target)){
          _btn.classList.remove('en-open','en-sticky');
          _panel.classList.remove('en-open');
          _open = false; _sticky = false;
          _reveal(2000);
        }
      }, {passive:true});
    }
    /* Button click: toggle panel */
    _btn.addEventListener('click', function(e){
      e.stopPropagation();
      if(_open){
        _btn.classList.remove('en-open','en-sticky');
        _panel.classList.remove('en-open');
        _open = false; _sticky = false;
        if(_isTouch) _reveal(2000);
      } else {
        _btn.classList.add('en-open','en-sticky');
        _panel.classList.add('en-open');
        _open = true; _sticky = true;
        if(_isTouch) _reveal(0);
        // Silently refresh if data is stale (>10 min old)
        if(_curItem && _fetchedAt && Date.now() - _fetchedAt > 10 * 60 * 1000){
          _fetchAndShow(_curItem);
        }
      }
    });
    return true;
  }
  /* ── Helpers ────────────────────────────────────────────────────────── */
  function _fmtTime(iso){
    if(!iso) return '';
    try{
      const d = typeof iso === 'number' ? new Date(iso * 1000) : new Date(iso);
      return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',hour12:false});
    }catch(e){ return ''; }
  }
  function _truncate(s, n){
    if(!s) return '';
    return s.length > n ? s.slice(0, n-1) + '\u2026' : s;
  }
  function _secsUntil(iso){
    if(!iso) return null;
    try{
      const ms = (typeof iso === 'number' ? iso * 1000 : new Date(iso).getTime()) - Date.now();
      return ms > 0 ? Math.ceil(ms/1000) : null;
    }catch(e){ return null; }
  }
  /* ── Schedule auto-refresh at programme boundary ─────────────────────── */
  function _scheduleRefresh(endIso, nextStartIso){
    if(_refreshTmr){ clearTimeout(_refreshTmr); _refreshTmr = null; }
    // Priority: 5s after current ends → 5s after next starts → 15-min poll
    let secs = null;
    if(endIso){
      const s = _secsUntil(endIso);
      if(s !== null && s > 0 && s <= 7200) secs = s + 5;
    }
    if(secs === null && nextStartIso){
      const s = _secsUntil(nextStartIso);
      if(s !== null && s > 0 && s <= 7200) secs = s + 5;
    }
    if(secs === null) secs = 15 * 60;   // fallback: 15-min poll
    _refreshTmr = setTimeout(function(){
      _refreshTmr = null;
      if(_curItem) _fetchAndShow(_curItem);
    }, secs * 1000);
  }
  /* ── Render panel ────────────────────────────────────────────────────── */
  let _fetchedAt = 0;
  function _render(data){
    if(!_ensureEls()) return;
    const cur  = data.current  || {};
    const next = data.next     || {};
    if(!cur.title && !next.title){ _hide(); return; }
    /* Button shows current title ONLY — never next.title, which would make
       an upcoming programme look like it's already playing.              */
    _btn.textContent = cur.title ? _truncate(cur.title, 26) : '\u{1F4FA}';
    /* Panel HTML */
    let html = '';
    if(cur.title){
      const t1 = _fmtTime(cur.start), t2 = _fmtTime(cur.end);
      const timeStr = (t1 && t2) ? (t1 + ' \u2013 ' + t2) : (t1 || '');
      if(timeStr) html += '<div class="en-time">' + timeStr + '</div>';
      html += '<div class="en-title">' + _esc(cur.title) + '</div>';
      if(cur.desc) html += '<div class="en-desc">' + _esc(cur.desc) + '</div>';
    } else {
      html += '<div class="en-desc" style="opacity:.5;font-style:italic">No programme info right now</div>';
    }
    if(next.title){
      html += '<div class="en-divider"></div>';
      html += '<div class="en-next-lbl">Up next</div>';
      const nt = _fmtTime(next.start);
      html += '<div class="en-next-title">'
            + (nt ? '<span class="en-next-time">' + nt + '</span>' : '')
            + _esc(next.title) + '</div>';
    }
    _panel.innerHTML = html;
    _hasData = true;
    _curData = data;
    _fetchedAt = Date.now();
    /* Show button + auto-open panel for 8s then collapse */
    if(_isTouch){
      _reveal(8000);
    } else {
      _btn.classList.add('en-hover');
    }
    _btn.classList.add('en-open');
    _panel.classList.add('en-open');
    _open = true;
    const snap = data;
    setTimeout(function(){
      if(!_sticky && _open && _curData === snap){
        _btn.classList.remove('en-open');
        _panel.classList.remove('en-open');
        _open = false;
        if(!_isTouch){
          const vwrap = document.getElementById('vwrap');
          if(vwrap && !vwrap.matches(':hover')) _btn.classList.remove('en-hover');
        }
      }
    }, 8000);
    /* Schedule next refresh — at current end, at next start, or 15-min poll */
    _scheduleRefresh(cur.end, next.start);
  }
  function _esc(s){
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  /* ── Fetch EPG and render ────────────────────────────────────────────── */
  let _loadingRetryTmr = null;
  function _fetchAndShow(item){
    if(_loadingRetryTmr){ clearTimeout(_loadingRetryTmr); _loadingRetryTmr = null; }
    fetch('/api/epg', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({item: item})
    }).then(function(r){ return r.json(); }).then(function(d){
      if(d && (d.current || d.next)){
        _render(d);
      } else if(d && d.error && d.error.toLowerCase().includes('loading')){
        // XMLTV still downloading — show spinner and retry in 5s
        if(_ensureEls()){
          _btn.textContent = '\u23F3 Loading EPG\u2026';
          _hasData = true;
          if(_isTouch) _reveal(10000);
          else _btn.classList.add('en-hover');
        }
        const capturedItem = item;
        _loadingRetryTmr = setTimeout(function(){
          _loadingRetryTmr = null;
          if(_curItem === capturedItem) _fetchAndShow(capturedItem);
        }, 5000);
      }
    }).catch(function(){});
  }
  /* ── Hide (on stop / channel switch) ────────────────────────────────── */
  function _hide(){
    if(_refreshTmr){ clearTimeout(_refreshTmr); _refreshTmr = null; }
    if(_loadingRetryTmr){ clearTimeout(_loadingRetryTmr); _loadingRetryTmr = null; }
    _open = false; _sticky = false; _hasData = false;
    _curItem = null; _curData = null;
    if(_isTouch && _btn) _cancelReveal();
    if(_btn){ _btn.classList.remove('en-hover','en-open','en-sticky'); _btn.textContent = '📺'; }
    if(_panel){ _panel.classList.remove('en-open'); _panel.innerHTML = ''; }
  }
  /* ── Intercept /api/resolve — same pattern as probe overlay ─────────── */
  // We chain onto the existing window.fetch which may already be wrapped by
  // probe_addon. We do NOT re-assign window.fetch here to avoid overwriting
  // probe's wrapper; instead we hook into window._streamInfoShow being called
  // (probe exports it) and use it as a trigger. This is the cleanest option
  // when both overlays coexist. Additionally, we check the resolve response
  // independently so EPG overlay works even without probe_addon.
  const _origFetch2 = window.fetch;
  window.fetch = async function(resource, opts){
    const res = await _origFetch2.apply(this, arguments);
    const url = (typeof resource === 'string') ? resource : (resource && resource.url || '');
    if(url.includes('/api/resolve') && !url.includes('/api/resolve_url')){
      const clone = res.clone();
      clone.json().then(function(d){
        if(!d || !d.url) return;
        // Only show EPG overlay for live streams
        try{
          const body = opts && opts.body ? JSON.parse(opts.body) : null;
          const isLive = body && (body.mode === 'live' || !body.mode);
          if(!isLive) { _hide(); return; }
          const item = body && body.item;
          if(!item) return;
          _hide();         // clear previous channel immediately
          _curItem = item; // set AFTER _hide() so refresh timer reference survives
          _fetchAndShow(item);
        } catch(e){}
      }).catch(function(){});
    }
    return res;
  };
  /* ── Hook playerStop only ────────────────────────────────────────────
     We intentionally do NOT patch _destroyPlayers here. doPlay() calls
     _destroyPlayers() synchronously before the new player is ready, which
     races with the incoming /api/epg response and wipes the overlay on the
     first channel play. The resolve interceptor's own _hide() call already
     handles channel-switch clearing correctly.                           */
  function _patchStop(name){
    const orig = window[name];
    if(typeof orig !== 'function') return;
    window[name] = function(){ _hide(); return orig.apply(this, arguments); };
  }
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', function(){
      _patchStop('playerStop');
    });
  } else {
    _patchStop('playerStop');
  }
})();
""" # end _EPG_UI_JS
 # end _EPG_UI_JS
