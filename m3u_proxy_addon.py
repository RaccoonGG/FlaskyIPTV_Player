"""
m3u_proxy_addon.py  —  M3U URL proxy addon for FlaskyIPTV_Player_byGG.py
=========================================================================
Converts any portal connection into a **permanent M3U URL** whose stream
links resolve a fresh play token on every player click.

  MAC / Stalker  — cmd stored at generation time; create_link called at
                   play time → brand-new token every click, never expires.
  Xtream         — stable credential-in-path URL built via _stream_url();
                   no portal API call at either generation or play time.
  M3U direct     — URL written straight into the playlist (is_direct=True);
                   proxy entry not used.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRATION  (three small changes to FlaskyIPTV_Player_byGG.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — import block (after the proxy_addon import block, ~line 84):

    try:
        from m3u_proxy_addon import register_m3u_proxy_routes
        _M3U_PROXY_AVAILABLE = True
    except ImportError:
        _M3U_PROXY_AVAILABLE = False
        def register_m3u_proxy_routes(*a, **kw): pass

STEP 2 — registration (after register_epg_routes call, ~line 1599):

    register_m3u_proxy_routes(flask_app, state, run_async, _make_client)

STEP 3 — script tag in HTML_TEMPLATE (after /api/radio/ui.js, ~line 8670):

    <script src="/api/m3u_proxy/ui.js"></script>
"""

import re
import socket
import threading
import time
import uuid as _uuid_mod
from urllib.parse import quote as _qe

from flask import request, Response, redirect, jsonify

from portal_clients import _extinf_line


# ── Configurable limits ────────────────────────────────────────────────────────
_MAX_PLAYLISTS = 50     # Oldest entry evicted when the cap is reached


# ── In-memory store ────────────────────────────────────────────────────────────
# Dict keyed by a 12-char hex UUID.  Lives for the server process lifetime;
# cleared on restart (players get a 503 and a clear human-readable message).
_M3U_PROXY_STORE: dict = {}
_STORE_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# Store helpers
# ══════════════════════════════════════════════════════════════════════════════

def _new_pid() -> str:
    return _uuid_mod.uuid4().hex[:12]


def _store_put(pid: str, data: dict) -> None:
    """Insert playlist *pid*, evicting the oldest entry when the cap is reached."""
    with _STORE_LOCK:
        if len(_M3U_PROXY_STORE) >= _MAX_PLAYLISTS:
            oldest = min(_M3U_PROXY_STORE,
                         key=lambda k: _M3U_PROXY_STORE[k].get("created_at", 0))
            del _M3U_PROXY_STORE[oldest]
        _M3U_PROXY_STORE[pid] = data


def _store_get(pid: str) -> "dict | None":
    with _STORE_LOCK:
        return _M3U_PROXY_STORE.get(pid)


def _store_delete(pid: str) -> bool:
    with _STORE_LOCK:
        return _M3U_PROXY_STORE.pop(pid, None) is not None


def _store_list() -> list:
    with _STORE_LOCK:
        return list(_M3U_PROXY_STORE.values())


# ══════════════════════════════════════════════════════════════════════════════
# Connection helpers
# ══════════════════════════════════════════════════════════════════════════════

def _conn_fingerprint(state) -> dict:
    """Snapshot of the fields that uniquely identify a portal connection."""
    return {
        "conn_type":  state.conn_type or "",
        "url":        state.url or getattr(state, "m3u_url", "") or "",
        "mac":        state.mac or "",
        "username":   state.username or "",
        "is_stalker": getattr(state, "is_stalker_portal", False),
    }


def _conn_matches(playlist: dict, state) -> bool:
    """True when *state* matches the portal the playlist was built against."""
    fp = playlist.get("conn_fp", {})
    return (
        fp.get("conn_type")  == (state.conn_type or "") and
        fp.get("url")        == (state.url or getattr(state, "m3u_url", "") or "") and
        fp.get("mac")        == (state.mac or "") and
        fp.get("username")   == (state.username or "") and
        fp.get("is_stalker") == getattr(state, "is_stalker_portal", False)
    )


def _get_epg_url(state) -> str:
    """Return the best EPG URL for the ``#EXTM3U url-tvg`` header."""
    epg = getattr(state, "ext_epg_url", "") or ""
    if epg:
        return epg
    conn = state.conn_type or ""
    if conn == "xtream" and state.url and state.username and state.password:
        base = state.url.rstrip("/")
        return (f"{base}/xmltv.php"
                f"?username={_qe(state.username, safe='')}"
                f"&password={_qe(state.password, safe='')}")
    if conn == "mac" and state.url:
        return state.url.rstrip("/") + "/xmltv.php"
    if conn == "m3u_url":
        return getattr(state, "_tvg_url_cache", "") or ""
    return ""


_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

def _is_loopback(host: str) -> bool:
    """Return True when *host* (with or without port) is a loopback/any address."""
    # Check bare host first (handles ::1 without port),
    # then strip IPv4 port suffix (handles 127.0.0.1:5000).
    return host in _LOOPBACK or host.split(":")[0] in _LOOPBACK


def _get_lan_ip() -> str:
    """
    Return the machine's primary LAN IP (the interface that would route
    outbound traffic).  Uses a UDP connect trick — no packet is sent.
    Falls back to empty string on failure (e.g. no network interfaces).
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# Channel extraction  (zero portal API calls — reads raw item dict only)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_channel(mode: str, item: dict,
                     cat_title: str, conn_type: str) -> "dict | None":
    """
    Build a storable channel descriptor from a raw portal item dict.

    The full *item* dict is stored so that ``resolve_item_url()`` called at
    play time has exactly the same input it would have during normal playback.

    For MAC / Stalker live channels this means the ``cmd`` field (e.g.
    ``"ffmpeg http://localhost/ch/1234_"``) is preserved for ``create_link``.
    No token resolution happens here — that is deliberately deferred to the
    proxy handler so tokens are always fresh.

    Returns ``None`` when no usable source can be found in *item*.
    """
    name = (item.get("name") or item.get("o_name") or item.get("fname") or
            item.get("stream_name") or item.get("title") or "?")
    logo = (item.get("logo") or item.get("stream_icon") or
            item.get("screenshot_uri") or item.get("pic") or "")
    tvg_type = "live" if mode == "live" else ("movie" if mode == "vod" else "series")

    # Minimal fields used by _extinf_line for tvg-id attribute
    item_meta: dict = {}
    for k in ("epg_channel_id", "tvg_id", "xmltv_id"):
        if item.get(k):
            item_meta[k] = item[k]

    ch: dict = {
        "name":      name,
        "logo":      logo,
        "group":     cat_title,
        "mode":      mode,
        "tvg_type":  tvg_type,
        "item_meta": item_meta,
        "item":      item,              # full dict for resolve_item_url()
        "category":  {"title": cat_title},
        "is_direct": False,
        "direct_url": "",
    }

    if conn_type == "m3u_url":
        # M3U source — URL is stable, write it directly to the playlist
        direct = (item.get("_direct_url") or item.get("_url") or
                  item.get("url") or item.get("cmd") or "")
        if not direct or not direct.startswith(("http://", "https://", "rtsp://")):
            return None
        ch["is_direct"]  = True
        ch["direct_url"] = direct

    elif conn_type == "xtream":
        # Xtream — stream_id is required to build the URL via _stream_url()
        if not item.get("stream_id"):
            direct = item.get("_direct_url") or ""
            if not direct:
                return None
            ch["is_direct"]  = True
            ch["direct_url"] = direct

    else:
        # MAC or Stalker (both report conn_type=="mac", distinguished by is_stalker_portal)
        # Validate that at least one source field is present; actual extraction
        # is delegated to extract_playables_for_item inside resolve_item_url().
        has_src = (item.get("cmd") or item.get("rtsp_url") or item.get("file") or
                   item.get("path") or item.get("_direct_url"))
        if not has_src:
            return None

    return ch


# ══════════════════════════════════════════════════════════════════════════════
# M3U content builder
# ══════════════════════════════════════════════════════════════════════════════

def _build_m3u(playlist: dict, proxy_base: str) -> str:
    """
    Generate M3U playlist text dynamically from the stored channel list.

    Each channel that needs token refresh uses a proxy URL:
        ``http://HOST/api/m3u_proxy/stream/PID/IDX``

    Channels with ``is_direct=True`` (M3U sources, some Xtream edge-cases)
    write their stable URL directly — one fewer hop at play time.
    """
    pid      = playlist["id"]
    channels = playlist.get("channels", [])
    epg_url  = playlist.get("epg_url", "")

    header = f'#EXTM3U url-tvg="{epg_url}"\n' if epg_url else "#EXTM3U\n"
    lines  = [header]

    for idx, ch in enumerate(channels):
        extinf = _extinf_line(
            ch["name"],
            ch.get("logo", ""),
            ch.get("tvg_type", "live"),
            ch.get("group", ""),
            ch.get("item_meta") or None,
        )
        if ch.get("is_direct") and ch.get("direct_url"):
            url = ch["direct_url"]
        else:
            url = f"{proxy_base}/api/m3u_proxy/stream/{pid}/{idx}"
        lines.append(extinf)
        lines.append(f"{url}\n")

    return "".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Route registration
# ══════════════════════════════════════════════════════════════════════════════

def register_m3u_proxy_routes(flask_app, state, run_async, _make_client):
    """Register all M3U proxy Flask routes and ``/api/m3u_proxy/ui.js``."""

    # ── POST /api/download/m3u_url ────────────────────────────────────────────
    @flask_app.route("/api/download/m3u_url", methods=["POST"])
    def api_generate_m3u_url():
        """
        Build a proxy playlist from selected items / category and return
        its permanent M3U URL.

        Generation is fast: for MAC / Stalker live the ``cmd`` is already
        present in every item from the browser-side cache — no ``create_link``
        call is made here.

        Request body (JSON)
        -------------------
        items       list | null   Items to include; null = fetch whole category.
        category    dict          Category metadata (id, title).
        mode        str           "live" | "vod" | "series"  (default: "live")
        total_hint  int           Client-side item count estimate (cosmetic).

        Response (JSON)
        ---------------
        ok           bool
        m3u_url      str    Permanent URL to the generated playlist.
        playlist_id  str    12-char hex ID of the stored playlist.
        count        int    Number of channels stored.
        label        str    Human-readable summary.
        """
        if not state.connected:
            return jsonify({"error": "Not connected to portal"}), 400

        data      = request.get_json(force=True)
        items_raw = data.get("items", None)   # None → fetch whole category
        cat       = data.get("category", {})
        mode      = data.get("mode", "live")
        mode      = mode if mode in ("live", "vod", "series") else "live"
        cat_title = cat.get("title", "Unknown")
        cat_id    = str(cat.get("id", ""))
        conn_type = state.conn_type or "mac"

        channels: list = []

        if items_raw is not None:
            # Fast path — items supplied by JS (already in categoryItemsCache).
            # For MAC/Stalker live: cmd is already in every item dict → no portal call.
            for item in items_raw:
                if not isinstance(item, dict):
                    continue
                ch = _extract_channel(mode, item, cat_title, conn_type)
                if ch:
                    channels.append(ch)

        else:
            # Category-level path — page through the portal API.
            # Mirrors the paging in api_download_m3u but without create_link.
            async def _fetch_category():
                result = []
                async with _make_client() as client:
                    # All-channels shortcut for live mode
                    if (cat_id in ("", "__all__")
                            and mode == "live"
                            and hasattr(client, "get_all_channels")):
                        try:
                            all_ch = await client.get_all_channels("live")
                        except Exception as exc:
                            state.log(f"[M3U_PROXY] get_all_channels error: {exc}")
                            all_ch = []
                        for it in all_ch:
                            if isinstance(it, dict):
                                ch = _extract_channel(mode, it, cat_title, conn_type)
                                if ch:
                                    result.append(ch)
                    else:
                        page = 1
                        while True:
                            try:
                                pg = await client.fetch_items_page(mode, cat_id, page)
                            except Exception as exc:
                                state.log(
                                    f"[M3U_PROXY] fetch_items_page p{page}: {exc}"
                                )
                                break
                            if not pg:
                                break
                            for it in pg:
                                if isinstance(it, dict):
                                    ch = _extract_channel(mode, it, cat_title, conn_type)
                                    if ch:
                                        result.append(ch)
                            if len(pg) < 5:   # last (partial) page
                                break
                            page += 1
                return result

            try:
                channels = run_async(_fetch_category())
            except Exception as exc:
                state.log(f"[M3U_PROXY] Category fetch failed: {exc}")
                return jsonify({"error": f"Failed to fetch category: {exc}"}), 500

        if not channels:
            return jsonify({
                "error": "No playable channels found in selection"
            }), 400

        pid   = _new_pid()
        label = f"{cat_title} ({len(channels)} ch)"
        _store_put(pid, {
            "id":         pid,
            "channels":   channels,
            "conn_fp":    _conn_fingerprint(state),
            "conn_type":  conn_type,
            "created_at": time.time(),
            "label":      label,
            "epg_url":    _get_epg_url(state),
        })

        proxy_base = f"http://{request.host}"
        m3u_url    = f"{proxy_base}/api/m3u_proxy/playlist/{pid}.m3u"

        # When the browser connected via loopback, also offer the LAN URL so
        # the user can paste it into TiviMate / Kodi on another device without
        # having to know their server IP.
        lan_m3u_url = ""
        req_host = request.host                          # e.g. "127.0.0.1:5000"
        req_ip   = req_host.split(":")[0]
        req_port = req_host.split(":")[-1] if ":" in req_host else "5000"
        if _is_loopback(req_host):
            lan_ip = _get_lan_ip()
            if lan_ip and lan_ip != req_ip:
                lan_m3u_url = (
                    f"http://{lan_ip}:{req_port}"
                    f"/api/m3u_proxy/playlist/{pid}.m3u"
                )

        state.log(
            f"[M3U_PROXY] ✓ Created {pid}: {len(channels)} ch — {cat_title}"
        )
        return jsonify({
            "ok":          True,
            "m3u_url":     m3u_url,
            "lan_m3u_url": lan_m3u_url,
            "playlist_id": pid,
            "count":       len(channels),
            "label":       label,
        })

    # ── GET /api/m3u_proxy/playlist/<pid>.m3u ────────────────────────────────
    @flask_app.route("/api/m3u_proxy/playlist/<pid>.m3u")
    def api_serve_playlist(pid):
        """
        Serve the M3U playlist for *pid*.

        Content is generated dynamically on each request so proxy stream URLs
        always carry the correct host and port (the request's ``Host`` header),
        enabling both localhost and LAN access from the same server instance.
        """
        pid = re.sub(r"[^a-f0-9]", "", pid.lower())[:12]
        playlist = _store_get(pid)
        if not playlist:
            body = (
                "#EXTM3U\n"
                "# Proxy playlist not found — server may have restarted.\n"
                "# Open FlaskyIPTV and regenerate the M3U URL.\n"
            )
            return Response(body, status=404,
                            content_type="application/x-mpegurl; charset=utf-8")

        proxy_base = f"http://{request.host}"
        content    = _build_m3u(playlist, proxy_base)
        return Response(
            content,
            status=200,
            content_type="application/x-mpegurl; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="flasky_proxy_{pid}.m3u"'
                ),
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )

    # ── GET /api/m3u_proxy/stream/<pid>/<idx> ────────────────────────────────
    @flask_app.route("/api/m3u_proxy/stream/<pid>/<int:idx>")
    def api_stream_proxy(pid, idx):
        """
        Resolve and redirect to the real stream URL for channel *idx*.

        This is the hot path — called by the external player every time a
        channel is opened.  For MAC / Stalker portals it triggers a fresh
        ``create_link`` call inside ``resolve_item_url`` so the resulting
        CDN URL always carries a valid play token.

        HTTP responses
        --------------
        302  Location: <fresh stream URL>   — normal success
        503  text/plain                     — server restarted / portal changed
        502  text/plain                     — resolution failed / channel offline
        404  text/plain                     — channel index out of range
        """
        pid = re.sub(r"[^a-f0-9]", "", pid.lower())[:12]
        playlist = _store_get(pid)
        if not playlist:
            return Response(
                "Proxy playlist expired — server restarted.\n"
                "Open FlaskyIPTV and regenerate the M3U URL.",
                status=503, content_type="text/plain; charset=utf-8",
            )

        channels = playlist.get("channels", [])
        if idx < 0 or idx >= len(channels):
            return Response(
                f"Channel index {idx} out of range "
                f"(playlist has {len(channels)} channels).",
                status=404, content_type="text/plain; charset=utf-8",
            )

        ch = channels[idx]

        # M3U-direct and some Xtream edge-cases: stable URL, no portal call needed
        if ch.get("is_direct") and ch.get("direct_url"):
            state.log(f"[M3U_PROXY] direct → {ch['name'][:60]}")
            return redirect(ch["direct_url"], 302)

        # Guard: reject stale playlists before making a portal call
        if not _conn_matches(playlist, state):
            return Response(
                "Portal changed since this M3U URL was generated.\n"
                "Reconnect to the original portal and regenerate the M3U URL.",
                status=503, content_type="text/plain; charset=utf-8",
            )

        if not state.connected:
            return Response(
                "Not connected to portal — open FlaskyIPTV and reconnect.",
                status=503, content_type="text/plain; charset=utf-8",
            )

        item     = ch["item"]
        mode     = ch["mode"]
        category = ch.get("category", {})

        async def _resolve():
            async with _make_client() as client:
                return await client.resolve_item_url(mode, item, category)

        try:
            resolved = run_async(_resolve())
        except Exception as exc:
            state.log(f"[M3U_PROXY] ✗ {ch['name']}: {exc}")
            return Response(
                f"Stream resolution failed: {exc}",
                status=502, content_type="text/plain; charset=utf-8",
            )

        if (not resolved
                or not isinstance(resolved, str)
                or not resolved.startswith(("http://", "https://", "rtsp://"))):
            state.log(f"[M3U_PROXY] ✗ empty URL for: {ch['name']}")
            return Response(
                "Could not resolve stream URL — channel may be offline.",
                status=502, content_type="text/plain; charset=utf-8",
            )

        state.log(f"[M3U_PROXY] ✓ {ch['name'][:50]} → {resolved[:80]}")
        return redirect(resolved, 302)

    # ── GET /api/m3u_proxy/list ───────────────────────────────────────────────
    @flask_app.route("/api/m3u_proxy/list")
    def api_list_playlists():
        """Return metadata for all active proxy playlists (newest first)."""
        proxy_base = f"http://{request.host}"
        out = []
        for p in _store_list():
            out.append({
                "id":         p["id"],
                "label":      p.get("label", "?"),
                "count":      len(p.get("channels", [])),
                "conn_type":  p.get("conn_type", "?"),
                "created_at": p.get("created_at", 0),
                "m3u_url": (
                    f"{proxy_base}/api/m3u_proxy/playlist/{p['id']}.m3u"
                ),
                "active":     _conn_matches(p, state),
            })
        out.sort(key=lambda x: x["created_at"], reverse=True)
        return jsonify({"playlists": out})

    # ── DELETE|POST /api/m3u_proxy/delete/<pid> ───────────────────────────────
    @flask_app.route("/api/m3u_proxy/delete/<pid>", methods=["DELETE", "POST"])
    def api_delete_playlist(pid):
        """Remove a proxy playlist from the in-memory store."""
        pid = re.sub(r"[^a-f0-9]", "", pid.lower())[:12]
        if _store_delete(pid):
            state.log(f"[M3U_PROXY] Deleted proxy playlist {pid}")
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Playlist not found"}), 404

    # ── GET /api/m3u_proxy/ui.js ──────────────────────────────────────────────
    _UI_JS_BYTES = _M3U_PROXY_UI_JS.encode("utf-8")

    @flask_app.route("/api/m3u_proxy/ui.js")
    def api_m3u_proxy_ui_js():
        return Response(
            _UI_JS_BYTES,
            content_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    state.log(
        "[M3U_PROXY] Routes registered: /api/download/m3u_url  "
        "/api/m3u_proxy/{playlist,stream,list,delete}  /api/m3u_proxy/ui.js"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Frontend  (served as /api/m3u_proxy/ui.js)
# ══════════════════════════════════════════════════════════════════════════════

_M3U_PROXY_UI_JS = r"""
/* ── CSS injection ──────────────────────────────────────────────────── */
(function(){
const s=document.createElement('style');
s.textContent=`
#m3u-px-ov{display:none;position:fixed;inset:0;z-index:950;
  background:rgba(0,0,0,.65);align-items:center;justify-content:center}
#m3u-px-ov.vis{display:flex}
#m3u-px-modal{background:var(--s2);border:1px solid var(--bdr2,var(--bdr));
  border-radius:var(--r);width:min(480px,94vw);max-height:86vh;
  display:flex;flex-direction:column;overflow:hidden;
  box-shadow:0 24px 80px rgba(0,0,0,.7)}
.m3u-px-hdr{display:flex;align-items:center;padding:14px 16px;
  border-bottom:1px solid var(--bdr);flex-shrink:0;gap:10px}
.m3u-px-hdr h3{flex:1;font-size:14px;font-weight:800;
  color:var(--txt);margin:0}
.m3u-px-body{flex:1;overflow-y:auto;padding:16px;
  display:flex;flex-direction:column;gap:12px}
.m3u-px-url-row{display:flex;gap:6px;align-items:stretch}
#m3u-px-url{flex:1;font-family:monospace;font-size:11px;
  padding:8px 10px;background:var(--s3);border:1px solid var(--bdr);
  border-radius:var(--rsm);color:var(--txt);word-break:break-all;
  resize:none;height:52px;line-height:1.4}
.m3u-px-info{font-size:11px;color:var(--txt2);line-height:1.6;
  padding:9px 11px;background:rgba(20,184,166,.07);
  border:1px solid rgba(20,184,166,.22);border-radius:var(--rsm)}
.m3u-px-ltitle{font-size:10px;font-weight:800;text-transform:uppercase;
  letter-spacing:1.2px;color:var(--txt3);padding-bottom:5px;
  border-bottom:1px solid var(--bdr)}
.m3u-px-row{display:flex;align-items:center;gap:8px;padding:8px 10px;
  border:1px solid var(--bdr);border-radius:var(--rsm);
  background:var(--s3);margin-top:6px}
.m3u-px-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;
  background:var(--green,#4ade80);box-shadow:0 0 5px var(--green,#4ade80)}
.m3u-px-dot.off{background:#4b5563;box-shadow:none}
/* Action drawer button */
#adr-dlm3u-url{background:linear-gradient(135deg,
  rgba(20,184,166,.16),rgba(6,182,212,.16));
  border:1px solid rgba(20,184,166,.32)}
#adr-dlm3u-url:hover:not(:disabled){background:linear-gradient(135deg,
  rgba(20,184,166,.26),rgba(6,182,212,.26))}
`;
document.head.appendChild(s);
})();

/* ── Dialog HTML injection ──────────────────────────────────────────── */
(function(){
const d=document.createElement('div');
d.innerHTML=`
<div id="m3u-px-ov" onclick="if(event.target===this)_pxClose()">
  <div id="m3u-px-modal">
    <div class="m3u-px-hdr">
      <h3>🔗 M3U Proxy URL</h3>
      <button class="btn-ghost" onclick="_pxClose()"
        style="height:30px;padding:0 10px;font-size:12px">✕</button>
    </div>
    <div class="m3u-px-body">
      <div id="m3u-px-lbl" style="font-size:12px;color:var(--txt2)"></div>
      <div>
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;
          letter-spacing:.9px;color:var(--txt3);margin-bottom:4px">
          Same device</div>
        <div class="m3u-px-url-row">
          <textarea id="m3u-px-url" readonly
            onclick="this.select()"
            title="Click to select all, then copy"></textarea>
          <button class="btn-ghost" id="m3u-px-copy" onclick="_pxCopy('m3u-px-url','m3u-px-copy')"
            title="Copy URL"
            style="height:52px;padding:0 14px;font-size:16px;
                   flex-shrink:0">📋</button>
        </div>
      </div>
      <div id="m3u-px-lan-block" style="display:none">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;
          letter-spacing:.9px;color:var(--txt3);margin-bottom:4px">
          Over local network (LAN) 📡</div>
        <div class="m3u-px-url-row">
          <textarea id="m3u-px-lan-url" readonly
            onclick="this.select()"
            title="Use this URL on phones/TVs on your WiFi"
            style="border-color:rgba(20,184,166,.45)"></textarea>
          <button class="btn-ghost" id="m3u-px-lan-copy"
            onclick="_pxCopy('m3u-px-lan-url','m3u-px-lan-copy')"
            title="Copy LAN URL"
            style="height:52px;padding:0 14px;font-size:16px;
                   flex-shrink:0">📋</button>
        </div>
      </div>
      <div class="m3u-px-info">
        📱 <strong>TiviMate:</strong> Add Playlist → M3U URL → paste above<br>
        📺 <strong>VLC / Kodi:</strong> Open Network Stream → paste above<br>
        🔄 Token renewed automatically on every channel click<br>
        🖥 URL is permanent while FlaskyIPTV is running on this device
      </div>
      <div>
        <div class="m3u-px-ltitle" style="margin-bottom:4px">
          Active Proxy Playlists
        </div>
        <div id="m3u-px-list">
          <div style="font-size:11px;color:var(--txt3);
                      padding:6px 0">Loading…</div>
        </div>
      </div>
    </div>
  </div>
</div>`;
while(d.firstChild) document.body.appendChild(d.firstChild);
})();

/* ── Dialog functions ───────────────────────────────────────────────── */
function _pxOpen(url, label, count, lanUrl){
  const ta=document.getElementById('m3u-px-url');
  const lb=document.getElementById('m3u-px-lbl');
  const cb=document.getElementById('m3u-px-copy');
  const lb2=document.getElementById('m3u-px-lan-block');
  const ta2=document.getElementById('m3u-px-lan-url');
  if(ta) ta.value=url;
  if(lb) lb.innerHTML=
    '<strong>'+count+'</strong> channel'+(count!==1?'s':'')+
    (label?' · '+label:'');
  if(cb) cb.textContent='📋';
  if(lb2 && ta2){
    if(lanUrl){
      ta2.value=lanUrl;
      lb2.style.display='';
    } else {
      lb2.style.display='none';
    }
  }
  document.getElementById('m3u-px-ov').classList.add('vis');
  _pxLoadList();
}

function _pxClose(){
  document.getElementById('m3u-px-ov').classList.remove('vis');
}

async function _pxCopy(taId, btnId){
  taId  = taId  || 'm3u-px-url';
  btnId = btnId || 'm3u-px-copy';
  const ta=document.getElementById(taId);
  const btn=document.getElementById(btnId);
  const url=ta?ta.value:'';
  try{
    await navigator.clipboard.writeText(url);
    if(btn){
      btn.textContent='✅';
      setTimeout(function(){btn.textContent='📋';},2000);
    }
  }catch(e){
    if(ta) ta.select();
    if(typeof toast==='function') toast('Long-press or Ctrl+C to copy','wrn');
  }
}

async function _pxLoadList(){
  const el=document.getElementById('m3u-px-list');
  if(!el) return;
  try{
    const d=await fetch('/api/m3u_proxy/list').then(function(r){return r.json();});
    const pl=d.playlists||[];
    if(!pl.length){
      el.innerHTML=
        '<div style="font-size:11px;color:var(--txt3);'+
        'padding:6px 0">No active playlists</div>';
      return;
    }
    el.innerHTML=pl.map(function(p){
      const mins=Math.round((Date.now()/1000-p.created_at)/60);
      const age=mins<60?mins+'m ago':Math.round(mins/60)+'h ago';
      const dotCls=p.active?'':'off';
      const tip=p.active?'Active (same portal)':'Stale (portal changed)';
      return '<div class="m3u-px-row">'+
        '<div class="m3u-px-dot '+dotCls+'" title="'+tip+'"></div>'+
        '<div style="flex:1;min-width:0">'+
          '<div style="font-size:12px;color:var(--txt);overflow:hidden;'+
            'text-overflow:ellipsis;white-space:nowrap" title="'+p.m3u_url+'">'+
            p.label+'</div>'+
          '<div style="font-size:10px;color:var(--txt3)">'+
            p.conn_type.toUpperCase()+' · '+age+'</div>'+
        '</div>'+
        '<button class="btn-ghost" title="Copy URL"'+
          ' onclick="_pxCopyOne(\''+p.m3u_url+'\')"'+
          ' style="height:26px;padding:0 8px;font-size:11px;flex-shrink:0">📋</button>'+
        '<button class="btn-ghost" title="Delete"'+
          ' onclick="_pxDel(\''+p.id+'\',this)"'+
          ' style="height:26px;padding:0 8px;font-size:11px;'+
            'flex-shrink:0;color:#f87171">🗑</button>'+
      '</div>';
    }).join('');
  }catch(e){
    el.innerHTML=
      '<div style="font-size:11px;color:#f87171">Error loading list</div>';
  }
}

function _pxCopyOne(url){
  navigator.clipboard.writeText(url).catch(function(){});
  if(typeof toast==='function') toast('URL copied','ok');
}

async function _pxDel(pid, btn){
  if(btn) btn.disabled=true;
  try{
    await fetch('/api/m3u_proxy/delete/'+pid,{method:'POST'});
    await _pxLoadList();
  }catch(e){ if(btn) btn.disabled=false; }
}

/* ── Export functions (called from button) ──────────────────────────── */
async function dlSelectedAllM3UUrl(){
  var nc=typeof selCats!=='undefined'?selCats.size:0;
  var ni=typeof selSet!=='undefined'?selSet.size:0;
  if(!nc&&!ni){
    if(typeof toast==='function') toast('Select categories or items first','wrn');
    return;
  }
  if(nc) await _dlSelCatsM3UUrl();
  if(ni&&!nc) await _dlItemsM3UUrl();
}

async function _dlSelCatsM3UUrl(){
  var cats=[].concat(Array.from(selCats.values()));
  for(var i=0;i<cats.length;i++){
    var cat=cats[i];
    var catKey=_categoryKey(mode,cat);
    var cached=(categoryItemsCache[mode]||{})[catKey];
    var items=null;
    if(cached&&cached.length){
      var _h=loadHidden(mode);
      items=cached.filter(function(it){
        return !_h.has(it.name||it.o_name||it.fname||'');
      });
    }
    try{
      var r=await fetch('/api/download/m3u_url',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          items:items,category:cat,mode:mode,
          total_hint:items?items.length:0
        })
      });
      var d=await r.json();
      if(d.ok) _pxOpen(d.m3u_url,d.label,d.count,d.lan_m3u_url||'');
      else if(typeof toast==='function') toast('M3U URL: '+(d.error||'?'),'err');
    }catch(e){
      if(typeof toast==='function') toast('Network error: '+e.message,'err');
    }
  }
}

async function _dlItemsM3UUrl(){
  if(typeof selSet==='undefined'||!selSet.size){
    if(typeof toast==='function') toast('Select items first','wrn');
    return;
  }
  try{
    var r=await fetch('/api/download/m3u_url',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        items:Array.from(selSet),
        category:curCat,mode:mode,
        total_hint:selSet.size
      })
    });
    var d=await r.json();
    if(d.ok) _pxOpen(d.m3u_url,d.label,d.count,d.lan_m3u_url||'');
    else if(typeof toast==='function') toast('M3U URL: '+(d.error||'?'),'err');
  }catch(e){
    if(typeof toast==='function') toast('Network error: '+e.message,'err');
  }
}

/* ── Button injection & refresh hook ───────────────────────────────── */
(function(){
  function _injectPxBtn(){
    var ref=document.getElementById('adr-dlm3u');
    if(!ref) return;
    if(document.getElementById('adr-dlm3u-url')) return;
    var btn=document.createElement('button');
    btn.id='adr-dlm3u-url';
    btn.className='adr-btn';
    btn.disabled=true;
    btn.onclick=dlSelectedAllM3UUrl;
    btn.innerHTML=
      '<span class="adr-ico">🔗</span>'+
      '<span class="adr-lbl">Get M3U URL'+
        '<span style="font-size:10px;opacity:.55;font-weight:400;'+
          'margin-left:4px">(live proxy)</span>'+
      '</span>'+
      '<span class="adr-sub" id="adr-m3u-url-sub"></span>';
    ref.insertAdjacentElement('afterend',btn);
  }

  if(document.readyState==='loading')
    document.addEventListener('DOMContentLoaded',_injectPxBtn);
  else _injectPxBtn();

  /* Wrap _refreshExportBtn so the new button tracks selection state */
  var _origREB=window._refreshExportBtn;
  window._refreshExportBtn=function(){
    if(typeof _origREB==='function') _origREB.apply(this,arguments);
    var n=typeof selSet!=='undefined'?selSet.size:0;
    var nc=typeof selCats!=='undefined'?selCats.size:0;
    var btn=document.getElementById('adr-dlm3u-url');
    var sub=document.getElementById('adr-m3u-url-sub');
    if(btn) btn.disabled=(n+nc)===0;
    if(sub){
      var parts=[];
      if(nc) parts.push(nc+' cat'+(nc===1?'':'s'));
      if(n)  parts.push(n+' item'+(n===1?'':'s'));
      sub.textContent=parts.join(' + ');
    }
  };
})();
""".lstrip()
