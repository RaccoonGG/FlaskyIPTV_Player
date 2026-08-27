"""
remote_addon.py — LAN remote-control addon for FlaskyIPTV Player by GG
========================================================================

Lets a phone (or any browser) on the same network see what's playing and
send transport commands (play/pause/resume/stop, next/previous, volume,
EPG toggle, fullscreen) to whichever machine has FlaskyIPTV open in a
browser tab. Pairs with remote_control.html, which this addon also serves
at /remote.

WHY A RELAY, NOT DIRECT CONTROL
--------------------------------
FlaskyIPTV's actual playback state (current channel, paused/playing,
volume, fullscreen) lives entirely in the browser tab that has the app
open — in `vid` (the <video> element) and the top-level `pIdx` /
`filtItems` / `mode` variables — NOT in the Python AppState. AppState only
tracks the portal session (connected/categories/cache), which is why
/api/status has no "now playing" field. So this addon can't just flip a
flag in Python; it has to relay through the browser tab that's actually
playing:

    phone  --POST /api/remote/command-->  addon  --SSE-->  browser tab
    browser tab --POST /api/remote/report--> addon --SSE--> phone

The addon holds two small pub/sub broadcasters (mirroring the pattern
AppState already uses for its /api/logs queue, just fanned out to
multiple subscribers) and otherwise keeps no state that matters across
restarts.

INTEGRATION (3 lines in FlaskyIPTV_Player_byGG.py, same pattern as every
other *_addon.py already wired into this app)
--------------------------------------------------------------------------
1. Near the other `from X_addon import ...` lines (~line 49-126):

       try:
           from remote_addon import register_remote_routes
       except ImportError:
           def register_remote_routes(*a, **kw): pass

2. Near the other unconditional register_*_routes(flask_app, state) calls
   (~line 1392-1394, right after register_proxy_routes):

       register_remote_routes(flask_app, state)

3. In HTML_TEMPLATE, next to the other <script src="/api/*/ui.js"> tags
   (~line 9877, right before </body>):

       <script src="/api/remote/ui.js"></script>

remote_control.html must sit in the same folder as this file — it's
read from disk (not baked in), so editing it doesn't require restarting
FlaskyIPTV or touching this file.

OPTIONAL PIN
------------
Unset by default, matching the rest of the app (no route in FlaskyIPTV
requires auth today, so an addon-only login wouldn't add much real
protection — anyone on the LAN can already reach the unauthenticated
main API). If your network is shared/untrusted, set FLASKY_REMOTE_PIN
before launching FlaskyIPTV and bookmark the controller as
http://<pc-ip>:<port>/remote?pin=<pin> on your phone — the PIN then
travels with the bookmark, no login screen needed. The PIN gates the
three phone-facing endpoints (status/events/command) only; the two
browser-facing endpoints (report/commands) are the app reporting on
itself locally and are left open, and category/item browsing goes
straight to FlaskyIPTV's own (already-unauthenticated) /api/categories
and /api/items.

KNOWN LIMITATIONS (see remote_control.html for how these surface in the UI)
-----------------------------------------------------------------------------
* Fullscreen-by-remote can be blocked by the browser: the Fullscreen API
  requires a *direct* user gesture on the page requesting it, and a tap
  on the phone can't carry that across the network — this is a browser
  security boundary, not something a relay can route around. We try the
  real Fullscreen API first and fall back to a CSS full-viewport overlay
  (ui.js) if it's rejected, which gets the same visual result.
* Same gesture rule occasionally affects resume/next/prev/play-from-
  browse on a channel that's never played in that tab before. In
  practice this is rare (Chrome/Firefox treat *any* earlier click on the
  page as enough), but when it happens the tab needs one manual tap;
  `playBlocked` in the reported state flags it so the controller can say
  so instead of just looking unresponsive.
* "EPG open/closed" is tracked by mirroring our own toggle presses, not
  by reading the panel's real DOM state (epg_addon.py's markup isn't
  available to introspect from here) — it can drift out of sync if EPG
  is opened/closed from the main screen instead of the remote.
"""

import json
import os
import queue
import socket
import threading
import time

from flask import request, jsonify, Response, stream_with_context, Blueprint

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
_REMOTE_PIN = os.environ.get("FLASKY_REMOTE_PIN", "").strip()  # empty = disabled
_STALE_AFTER_SECONDS = 10.0     # no report in this long => controller shows "not open"
_SSE_KEEPALIVE_SECONDS = 15     # comment-line heartbeat so proxies/browsers don't time out
_VOLUME_STEP_DEFAULT = 5
_HTML_FILENAME = "remote_control.html"

VALID_ACTIONS = {
    "pause", "resume", "stop", "toggle_play",
    "next", "previous",
    "volume_up", "volume_down", "set_volume", "mute_toggle",
    "toggle_epg",
    "fullscreen_on", "fullscreen_off",
    "play_item",
    "play_station",
}

_MAX_STATION_LIST = 500  # generous vs. the addon's own /top,/genre,/search caps (300-500)


# --------------------------------------------------------------------------
# Pure helpers — no Flask/request involved, unit-tested directly
# --------------------------------------------------------------------------
def clamp_volume(v):
    """Clamp to an int 0-100. Non-numeric input clamps to 0 rather than raising —
    callers always get a usable value back."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0
    if v != v:  # NaN
        return 0
    return int(max(0, min(100, round(v))))


def pin_matches(supplied, configured):
    """True if no PIN is configured (feature disabled) or supplied matches exactly."""
    if not configured:
        return True
    if supplied is None:
        return False
    return str(supplied) == configured


def is_fresh(server_ts, now=None, threshold=_STALE_AFTER_SECONDS):
    """Whether a state report timestamp is recent enough to trust."""
    if server_ts is None:
        return False
    if now is None:
        now = time.time()
    return (now - server_ts) <= threshold


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def is_loopback_host(host):
    """True when `host` (bare, or 'host:port') is a loopback/any address.

    Same check m3u_proxy_addon.py already uses for its own LAN-URL
    substitution — copied rather than imported so this addon has no
    dependency on another optional addon being present.
    """
    if not host:
        return False
    return host in _LOOPBACK_HOSTS or host.split(":")[0] in _LOOPBACK_HOSTS


def get_lan_ip():
    """Best-effort LAN IP of this machine (the interface that would route
    outbound traffic), via the standard UDP-connect trick — no packet is
    actually sent, this just asks the OS routing table which local address
    it would use. Returns '' on any failure (e.g. no network interface)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


def compute_lan_remote_url(req_host):
    """Given the Host header a request arrived on (e.g. '127.0.0.1:5000'),
    return the LAN-reachable http://.../remote URL if that request came in
    via loopback and a LAN IP could be detected — otherwise None.

    Mirrors m3u_proxy_addon.py's own "offer the LAN URL when connected via
    loopback" behavior (see its playlist-creation route): someone browsing
    FlaskyIPTV locally on the PC itself can still see the address a phone
    would use, without needing to check Windows/network settings for it.
    """
    if not is_loopback_host(req_host):
        return None
    ip = get_lan_ip()
    if not ip:
        return None
    port = req_host.split(":")[-1] if ":" in req_host else "80"
    return "http://{}:{}/remote".format(ip, port)


# Exact-match paths (no sub-paths) the standalone controller calls directly,
# plus prefix paths (this addon's own routes, and every /api/radio/* route —
# favorites/search/top/builtin/genres/genre/<tag>/click all live under it).
_CORS_EXACT_PATHS = frozenset({"/api/categories", "/api/items", "/api/episodes"})
_CORS_PREFIX_PATHS = ("/api/remote/", "/api/radio/")


def needs_cors(path):
    """Whether `path` should get permissive CORS headers — the explicit
    allowlist behind the app-wide before/after_request hooks below. Kept as
    a standalone function (rather than inlined in the hooks) so the
    allowlist itself is unit-testable independent of a running Flask app."""
    if path in _CORS_EXACT_PATHS:
        return True
    return any(path.startswith(p) for p in _CORS_PREFIX_PATHS)


def validate_command(payload):
    """Validate + normalize an incoming /api/remote/command body.

    Returns (True, cleaned_dict) or (False, error_message). Never raises —
    malformed input always comes back as a clean (False, "...") pair so the
    route handler can turn it straight into a 400.
    """
    if not isinstance(payload, dict):
        return False, "invalid json body"
    action = payload.get("action")
    if action not in VALID_ACTIONS:
        return False, "unknown action: {!r}".format(action)

    out = {"action": action}

    if action == "set_volume":
        if "value" not in payload:
            return False, "set_volume requires 'value'"
        try:
            float(payload["value"])
        except (TypeError, ValueError):
            return False, "set_volume 'value' must be numeric"
        out["value"] = clamp_volume(payload["value"])

    elif action in ("volume_up", "volume_down"):
        step = payload.get("step", _VOLUME_STEP_DEFAULT)
        try:
            step = int(step)
        except (TypeError, ValueError):
            step = _VOLUME_STEP_DEFAULT
        out["step"] = max(1, min(50, step))

    elif action == "play_item":
        if "index" not in payload:
            return False, "play_item requires 'index'"
        try:
            out["index"] = int(payload["index"])
        except (TypeError, ValueError):
            return False, "play_item 'index' must be an integer"
        if out["index"] < 0:
            return False, "play_item 'index' must be >= 0"
        req_mode = payload.get("mode") or "live"
        out["mode"] = req_mode if req_mode in ("live", "vod", "series") else "live"
        cat = payload.get("category")
        out["category"] = cat if isinstance(cat, dict) else {}
        if payload.get("name"):
            out["name"] = str(payload["name"])[:200]

    elif action == "play_station":
        # `stations` is the list the phone was browsing (Favorites/Top/Search/
        # a Genre drilldown/Built-in) at the moment a station was tapped.
        # radio_addon.py's own list/index tracking (_currentList, _rdioNavList,
        # _rdioNavIndex) lives inside a script-wide IIFE and isn't reachable
        # from ui.js at all, so the full list travels with the command and
        # ui.js tracks its own index for prev/next — see remotePlayStation()
        # and remoteRadioRelative() below. Same reason play_item passes a
        # full item list rather than just an id, different underlying cause.
        if "index" not in payload:
            return False, "play_station requires 'index'"
        try:
            out["index"] = int(payload["index"])
        except (TypeError, ValueError):
            return False, "play_station 'index' must be an integer"
        stations = payload.get("stations")
        if not isinstance(stations, list) or not stations:
            return False, "play_station requires a non-empty 'stations' list"
        if len(stations) > _MAX_STATION_LIST:
            return False, "play_station 'stations' list too large (max {})".format(_MAX_STATION_LIST)
        if not (0 <= out["index"] < len(stations)):
            return False, "play_station 'index' out of range for 'stations'"
        out["stations"] = stations
        if payload.get("name"):
            out["name"] = str(payload["name"])[:200]

    return True, out


# --------------------------------------------------------------------------
# Broadcaster — thread-safe pub/sub fan-out for SSE
# --------------------------------------------------------------------------
class _Broadcaster:
    """Mirrors AppState.log_queue's queue.Queue approach, extended to fan a
    message out to *every* connected subscriber (one Queue per SSE client)
    instead of a single shared queue. A slow/stalled subscriber never blocks
    publish() or the other subscribers — it just drops its own oldest queued
    item to make room for the newest one, since for a remote control the
    latest state is always more useful than a backlog of stale ones."""

    def __init__(self, maxsize=50):
        self._subscribers = set()
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def subscribe(self):
        q = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass  # extremely unlucky race with another publisher; drop silently

    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)


_state_broadcaster = _Broadcaster()      # browser tab  -> addon -> phones
_command_broadcaster = _Broadcaster()    # phone        -> addon -> browser tab

_state_lock = threading.Lock()
_last_state = {"report": None, "server_ts": None}


def _record_report(payload):
    now = time.time()
    with _state_lock:
        _last_state["report"] = payload
        _last_state["server_ts"] = now
    _state_broadcaster.publish({"state": payload, "server_ts": now})
    return now


def _status_snapshot():
    with _state_lock:
        report = _last_state.get("report")
        server_ts = _last_state.get("server_ts")
    if report is None:
        return {"ok": True, "fresh": False, "age_seconds": None, "state": None}
    age = time.time() - server_ts
    return {
        "ok": True,
        "fresh": is_fresh(server_ts),
        "age_seconds": round(age, 1),
        "state": report,
    }


def _load_controller_html():
    """Read remote_control.html fresh on every request (it's tiny — unlike the
    424 KB main template there's no reason to cache it) so editing the file's
    styling/copy takes effect on refresh, no FlaskyIPTV restart needed."""
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _HTML_FILENAME)
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return (
            "<!DOCTYPE html><html><body style=\"background:#060612;color:#e4e8f5;"
            "font-family:system-ui,sans-serif;padding:2rem;line-height:1.5\">"
            "<h1 style=\"color:#7c3aed\">remote_control.html not found</h1>"
            "<p>Place remote_control.html in the same folder as remote_addon.py "
            "and reload this page.</p></body></html>"
        )


def _pin_ok(req):
    if not _REMOTE_PIN:
        return True
    supplied = req.args.get("pin")
    if supplied is None:
        supplied = req.headers.get("X-Remote-Pin")
    if supplied is None and req.method == "POST":
        body = req.get_json(silent=True)
        if isinstance(body, dict):
            supplied = body.get("pin")
    return pin_matches(supplied, _REMOTE_PIN)


def _sse_response(broadcaster, prime=None):
    """Build an SSE Response subscribed to `broadcaster`. If `prime` is given
    (a zero-arg callable returning a dict or None), its result is sent as the
    very first event so a newly-connected client doesn't wait for the next
    change to learn the current state.

    The generator's very first yield is ALWAYS immediate and non-blocking
    (a ": connected" comment, ahead of anything `prime` produces) regardless
    of whether `prime` is given. Flask's stream_with_context resolves a
    streamed Response's first chunk synchronously as part of finishing the
    request (verified directly against this app's Werkzeug version, not
    assumed) — without this, /api/remote/commands (which has no `prime`,
    since there's no "current command") would sit inside the very first
    `subq.get(timeout=...)` and hold the whole HTTP response open for up to
    _SSE_KEEPALIVE_SECONDS before a single byte reached the browser.
    """
    subq = broadcaster.subscribe()

    def gen():
        try:
            yield ": connected\n\n"
            if prime is not None:
                first = prime()
                if first is not None:
                    yield "data: " + json.dumps(first) + "\n\n"
            while True:
                try:
                    item = subq.get(timeout=_SSE_KEEPALIVE_SECONDS)
                    yield "data: " + json.dumps(item) + "\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            broadcaster.unsubscribe(subq)

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # nginx: disable proxy buffering if ever fronted by one
            "Connection": "keep-alive",
        },
    )


# --------------------------------------------------------------------------
# Injected browser-side script — served at /api/remote/ui.js, loaded by the
# main page exactly like every other addon's ui.js (see HTML_TEMPLATE).
# Runs as a classic (non-module) script, placed after the main inline
# <script> block, so it shares that block's top-level `let`/`const` scope —
# vid, pIdx, filtItems, mode, pUrl, _curIsRadio etc. are reachable as plain
# identifiers, and playerPP/playerStop/playerPrev/playerNext/setVol/doPlay/
# playItem — all plain top-level `function` declarations — are additionally
# reachable via window.<name>.
# --------------------------------------------------------------------------
_REMOTE_UI_JS = r"""
(function(){
  'use strict';
  if (window.__flaskyRemoteLoaded) return;
  window.__flaskyRemoteLoaded = true;

  var REPORT_INTERVAL_MS = 3000;
  var PLAY_BLOCKED_CHECK_MS = 700;
  var VOLUME_STEP_DEFAULT = 5;

  function byId(id){ return document.getElementById(id); }
  function textOf(id){ var el = byId(id); return el ? el.textContent : ''; }

  function callFn(name){
    try {
      if (typeof window[name] === 'function') { window[name](); return true; }
    } catch(e){ console.warn('[FlaskyRemote] ' + name + '() threw', e); }
    return false;
  }

  // My own tracking of the radio list/index being browsed via THIS remote —
  // needed because radio_addon.py wraps its entire script in one IIFE
  // (confirmed by reading it directly), so _currentList/_rdioNavList/
  // _rdioNavIndex are not reachable from a separately-loaded script at
  // all, not even as bare identifiers the way the main file's pIdx/
  // filtItems/mode are. window._rdioPlayIdx()/_rdioFavIdx()/etc. are
  // explicitly exposed and callable, but they read _currentList from
  // their own closure — setting a same-named bare identifier from out
  // here does not reach it (in non-strict code it would silently create
  // an unrelated global; in strict code, which this script uses, it
  // throws). window.radioPlayStation(urlEnc, stEnc) plays a specific
  // station directly without touching any of that, so it's used instead
  // of _rdioPlayIdx() — the tradeoff is this remote has to maintain its
  // own list/index for next/previous rather than relying on radio_addon.py's.
  var _remoteRadioList = null;
  var _remoteRadioIndex = -1;

  function currentItem(){
    try {
      if (typeof _curIsRadio !== 'undefined' && _curIsRadio) {
        if (_remoteRadioList && _remoteRadioIndex >= 0 && _remoteRadioIndex < _remoteRadioList.length) {
          return _remoteRadioList[_remoteRadioIndex];
        }
        // Defensive fallback only — _rdioNavList/_rdioNavIndex live inside
        // radio_addon.py's IIFE and are not actually reachable here (see
        // above), so typeof on them is always 'undefined' and this branch
        // never runs today. Left in case a future radio_addon.py exposes
        // them differently; typeof keeps this from ever throwing either way.
        if (typeof _rdioNavList !== 'undefined' && _rdioNavList &&
            typeof _rdioNavIndex !== 'undefined' && _rdioNavIndex >= 0 &&
            _rdioNavIndex < _rdioNavList.length) {
          return _rdioNavList[_rdioNavIndex];
        }
      }
      if (typeof filtItems !== 'undefined' && filtItems &&
          typeof pIdx !== 'undefined' && pIdx >= 0 && pIdx < filtItems.length) {
        return filtItems[pIdx];
      }
    } catch(e){}
    return null;
  }
  function itemName(it){
    if (!it) return '';
    return it.name || it.title || it.stream_name || it.o_name || it.fname || '';
  }
  function itemLogo(it){
    if (!it) return '';
    return it.logo || it.icon_fallback || it.stream_icon || it.cover || it.screenshot_uri || it.pic || '';
  }

  var _lastPlayBlocked = false;

  function snapshot(){
    var v = (typeof vid !== 'undefined') ? vid : null;
    var it = currentItem();
    var dur = v ? v.duration : NaN;
    var isRadioNow = (typeof _curIsRadio !== 'undefined') && !!_curIsRadio;
    var radioListOk = isRadioNow && _remoteRadioList && _remoteRadioIndex >= 0;
    return {
      title: textOf('np') || itemName(it) || '',
      track: textOf('np-track') || '',
      playing: !!(v && !v.paused && !v.ended),
      paused: !!(v && v.paused),
      ended: !!(v && v.ended),
      stopped: (typeof _playerStopped !== 'undefined') ? !!_playerStopped : !(v && v.currentSrc),
      volume: v ? Math.round(v.volume * 100) : null,
      muted: v ? !!v.muted : null,
      currentTime: v ? v.currentTime : null,
      duration: (isFinite(dur) && dur > 0) ? dur : null,
      isRadio: isRadioNow,
      mode: (typeof mode !== 'undefined') ? mode : null,
      itemIndex: radioListOk ? _remoteRadioIndex : ((typeof pIdx !== 'undefined') ? pIdx : null),
      itemCount: radioListOk ? _remoteRadioList.length : ((typeof filtItems !== 'undefined' && filtItems) ? filtItems.length : null),
      logo: itemLogo(it),
      fullscreen: !!(document.fullscreenElement || (fsTarget() && fsTarget().classList.contains('_remoteFsActive'))),
      playBlocked: _lastPlayBlocked,
      epgOpenGuess: !!window.__flaskyRemoteEpgGuess
    };
  }

  function report(){
    try {
      fetch('/api/remote/report', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(snapshot())
      })['catch'](function(){});
    } catch(e){}
  }

  function checkPlayBlockedSoon(){
    setTimeout(function(){
      try {
        var v = (typeof vid !== 'undefined') ? vid : null;
        var hasSrc = v && (v.currentSrc || (typeof pUrl !== 'undefined' && pUrl));
        _lastPlayBlocked = !!(v && v.paused && !v.ended && hasSrc);
      } catch(e){ _lastPlayBlocked = false; }
      report();
    }, PLAY_BLOCKED_CHECK_MS);
  }

  // ---- fullscreen: real Fullscreen API first, CSS overlay if it's refused ----
  // The Fullscreen API requires a direct user gesture on *this* page; a tap
  // relayed from a phone across the network can never carry that, so on a
  // strict browser this will reject and we fall back automatically.
  function ensureFsStyle(){
    if (byId('_remoteFsStyle')) return;
    var s = document.createElement('style');
    s.id = '_remoteFsStyle';
    s.textContent = '._remoteFsActive{position:fixed !important;top:0 !important;' +
      'left:0 !important;width:100vw !important;height:100vh !important;' +
      'z-index:2147483647 !important;background:#000;}';
    document.head.appendChild(s);
  }
  function fsTarget(){
    return (typeof vid !== 'undefined' && vid) ? vid : document.documentElement;
  }
  function fullscreenOn(){
    var t = fsTarget();
    var req = t.requestFullscreen || t.webkitRequestFullscreen || t.msRequestFullscreen;
    var result = req ? req.call(t) : null;
    if (result && typeof result.then === 'function') {
      result['catch'](function(){ ensureFsStyle(); t.classList.add('_remoteFsActive'); });
    } else if (!document.fullscreenElement) {
      ensureFsStyle();
      t.classList.add('_remoteFsActive');
    }
    setTimeout(report, 300);
  }
  function fullscreenOff(){
    if (document.fullscreenElement) {
      var exit = document.exitFullscreen || document.webkitExitFullscreen || document.msExitFullscreen;
      if (exit) { try { exit.call(document); } catch(e){} }
    }
    fsTarget().classList.remove('_remoteFsActive');
    setTimeout(report, 150);
  }

  // ---- volume ----
  function setVolumeAbs(v){
    v = Math.max(0, Math.min(100, Math.round(v)));
    if (typeof setVol === 'function') { setVol(v); }
    else if (typeof vid !== 'undefined' && vid) { vid.volume = v / 100; }
    report();
  }
  function volumeStep(delta){
    var v = (typeof vid !== 'undefined' && vid) ? Math.round(vid.volume * 100) : 50;
    setVolumeAbs(v + delta);
  }

  // ---- play a specific browsed item ----
  // Re-fetches /api/items server-side (a cache hit — AppState._items_cache
  // already holds it from the phone's own browse fetch) rather than relaying
  // the whole list through the command channel, then reuses playItem()'s own
  // resolve+play logic by pointing filtItems/pIdx/mode at it directly. This
  // also means next/previous on the physical screen (or the remote) correctly
  // continue through the same category afterward.
  function remotePlayItem(cmd){
    var wantMode = cmd.mode || (typeof mode !== 'undefined' ? mode : 'live');
    var category = cmd.category || {};
    fetch('/api/items', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: wantMode, category: category, browse: true})
    }).then(function(r){ return r.json(); }).then(function(data){
      var items = (data && data.items) || [];
      if (!items.length) { report(); return; }
      try { if (typeof mode !== 'undefined') mode = wantMode; } catch(e){}
      try { if (typeof curCat !== 'undefined') curCat = category; } catch(e){}
      try { filtItems = items; } catch(e){ console.warn('[FlaskyRemote] cannot set filtItems', e); return; }
      var idx = (typeof cmd.index === 'number' && cmd.index >= 0 && cmd.index < items.length) ? cmd.index : 0;
      try { pIdx = idx; } catch(e){}
      if (typeof playItem === 'function') {
        _lastPlayBlocked = false;
        playItem(idx);
        checkPlayBlockedSoon();
      }
      report();
    })['catch'](function(e){
      console.warn('[FlaskyRemote] play_item failed', e);
      report();
    });
  }

  // ---- play a specific browsed radio station ----
  // Unlike remotePlayItem, no server re-fetch is needed here — the phone
  // already has the full station list it was browsing (Favorites/Top/
  // Search/a Genre drilldown/Built-in all return small lists, unlike
  // live-channel categories which can run into the thousands), so it's
  // relayed as-is. Calls window.radioPlayStation() directly rather than
  // window._rdioPlayIdx() — the latter reads _currentList from inside
  // radio_addon.py's own IIFE, which isn't reachable from here (see the
  // _remoteRadioList comment above), so this remote maintains its own
  // list/index instead of relying on radio_addon.py's internal one.
  function _playStationDirect(station){
    var url = station && (station.url_resolved || station.url);
    if (!url) { console.warn('[FlaskyRemote] station has no url/url_resolved', station); return false; }
    if (typeof window.radioPlayStation !== 'function') {
      console.warn('[FlaskyRemote] radioPlayStation not found — is radio_addon.py loaded?');
      return false;
    }
    _lastPlayBlocked = false;
    try {
      window.radioPlayStation(encodeURIComponent(url), encodeURIComponent(JSON.stringify(station)));
    } catch(e){
      console.warn('[FlaskyRemote] radioPlayStation threw', e);
      return false;
    }
    checkPlayBlockedSoon();
    return true;
  }

  function remotePlayStation(cmd){
    var stations = cmd.stations || [];
    var idx = (typeof cmd.index === 'number' && cmd.index >= 0 && cmd.index < stations.length) ? cmd.index : -1;
    if (idx === -1 || !stations.length) { report(); return; }
    _remoteRadioList = stations;
    _remoteRadioIndex = idx;
    _playStationDirect(stations[idx]);
    report();
  }

  // next/previous while radio is playing: prefer stepping through the list
  // THIS remote was browsing (only tracking reachable from here — see the
  // _remoteRadioList comment above); returns false if there's nothing of
  // ours to step through, so the caller can fall back to the normal
  // playerNext/playerPrev path (which still works fine if radio was
  // started from the on-screen UI instead of the remote).
  function remoteRadioRelative(direction){
    if (!_remoteRadioList || !_remoteRadioList.length || _remoteRadioIndex < 0) return false;
    var n = _remoteRadioList.length;
    for (var tries = 0; tries < n; tries++){
      _remoteRadioIndex = (_remoteRadioIndex + direction + n) % n;
      if (_playStationDirect(_remoteRadioList[_remoteRadioIndex])) return true;
    }
    return false;
  }

  // ---- command dispatch ----
  function handleCommand(cmd){
    if (!cmd || !cmd.action) return;
    switch (cmd.action) {
      case 'pause':
        if (typeof vid !== 'undefined' && vid && !vid.paused && !vid.ended) callFn('playerPP');
        break;
      case 'resume':
        if (typeof vid !== 'undefined' && vid && (vid.paused || vid.ended)) {
          callFn('playerPP'); checkPlayBlockedSoon();
        }
        break;
      case 'toggle_play':
        callFn('playerPP'); checkPlayBlockedSoon();
        break;
      case 'stop':
        callFn('playerStop');
        break;
      case 'next':
        if (!((typeof _curIsRadio !== 'undefined') && _curIsRadio && remoteRadioRelative(1))) {
          callFn('playerNext'); checkPlayBlockedSoon();
        }
        break;
      case 'previous':
        if (!((typeof _curIsRadio !== 'undefined') && _curIsRadio && remoteRadioRelative(-1))) {
          callFn('playerPrev'); checkPlayBlockedSoon();
        }
        break;
      case 'volume_up':
        volumeStep(cmd.step || VOLUME_STEP_DEFAULT);
        return; // volumeStep() already reports
      case 'volume_down':
        volumeStep(-(cmd.step || VOLUME_STEP_DEFAULT));
        return;
      case 'set_volume':
        if (typeof cmd.value === 'number') setVolumeAbs(cmd.value);
        return;
      case 'mute_toggle':
        if (typeof vid !== 'undefined' && vid) vid.muted = !vid.muted;
        break;
      case 'toggle_epg': {
        var btn = byId('epgbtn');
        if (btn) { btn.click(); window.__flaskyRemoteEpgGuess = !window.__flaskyRemoteEpgGuess; }
        break;
      }
      case 'fullscreen_on':
        fullscreenOn();
        return;
      case 'fullscreen_off':
        fullscreenOff();
        return;
      case 'play_item':
        remotePlayItem(cmd);
        return; // remotePlayItem() reports when it resolves
      case 'play_station':
        remotePlayStation(cmd);
        return; // remotePlayStation() reports when it resolves
      default:
        console.warn('[FlaskyRemote] unknown action', cmd.action);
        return;
    }
    report();
  }

  // ---- wire up state reporting ----
  function attachVideoListeners(){
    if (typeof vid === 'undefined' || !vid || vid.__flaskyRemoteBound) return;
    vid.__flaskyRemoteBound = true;
    ['play', 'pause', 'ended', 'volumechange', 'loadedmetadata'].forEach(function(ev){
      vid.addEventListener(ev, report);
    });
  }
  attachVideoListeners();
  document.addEventListener('DOMContentLoaded', attachVideoListeners);
  setInterval(function(){ attachVideoListeners(); report(); }, REPORT_INTERVAL_MS);
  report();

  // ---- receive commands over SSE ----
  function connectCommandStream(){
    try {
      var es = new EventSource('/api/remote/commands');
      es.onmessage = function(ev){
        var cmd;
        try { cmd = JSON.parse(ev.data); } catch(e){ return; }
        handleCommand(cmd);
      };
      es.onerror = function(){ /* EventSource retries on its own */ };
    } catch(e){
      console.warn('[FlaskyRemote] EventSource unavailable — commands will not arrive', e);
    }
  }
  connectCommandStream();
})();
"""


# --------------------------------------------------------------------------
# Registration — call register_remote_routes(flask_app, state) once, same
# as every other addon in this app.
# --------------------------------------------------------------------------
def register_remote_routes(app, state):
    # A Blueprint (rather than routes on `app` directly) so the CORS headers
    # below apply only to this addon's own routes via bp.after_request, not
    # every other endpoint in FlaskyIPTV. CORS matters here specifically
    # because remote_control.html can run as a genuinely different origin —
    # opened as a standalone file (file://) rather than fetched from this
    # server (same-origin, which never needed CORS at all — that's why
    # /remote worked from the start while the standalone file's fetch()/
    # EventSource calls were silently blocked by the browser).
    bp = Blueprint("flasky_remote", __name__)

    # CORS has to be app-wide, not blueprint-scoped, because the standalone-
    # file controller also calls straight into /api/categories, /api/items,
    # /api/episodes, and /api/radio/* — none of which are this addon's own
    # routes, so a blueprint-only after_request (the original fix) never
    # covered them. Deliberately allowlisted by path rather than a blanket
    # policy on the whole app: opening every FlaskyIPTV endpoint to
    # cross-origin reads would also remove a real defense against a
    # malicious page on the same LAN probing things like /api/profile or
    # /api/connect from inside someone's browser. Only the specific
    # read-mostly browse endpoints this controller actually calls are
    # allowlisted; anything else keeps its current (no-CORS) behavior.
    @app.before_request
    def _remote_cors_preflight():
        if request.method == "OPTIONS" and needs_cors(request.path):
            return Response(status=204)

    @app.after_request
    def _remote_cors_headers(resp):
        if needs_cors(request.path):
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Remote-Pin"
            resp.headers["Access-Control-Max-Age"] = "600"
        return resp

    @bp.route("/api/remote/ui.js")
    def remote_ui_js():
        return Response(_REMOTE_UI_JS, mimetype="text/javascript")

    @bp.route("/remote")
    def remote_page():
        return Response(_load_controller_html(), mimetype="text/html")

    @bp.route("/api/remote/available")
    def remote_available():
        """Mirrors the cast_addon/multiview_addon probe-endpoint convention,
        plus (when reachable) the machine's own LAN address for /remote —
        so the controller can display it and nobody needs to look up
        ipconfig/ip-addr by hand to fill in the standalone-file connect
        prompt."""
        payload = {"available": True}
        lan_url = compute_lan_remote_url(request.host)
        if lan_url:
            payload["lan_url"] = lan_url
        return jsonify(payload)

    # -- browser tab -> addon (no PIN: this is the app reporting on itself) --
    @bp.route("/api/remote/report", methods=["POST", "OPTIONS"])
    def remote_report():
        if request.method == "OPTIONS":
            return Response(status=204)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "invalid json body"}), 400
        _record_report(payload)
        return jsonify({"ok": True})

    @bp.route("/api/remote/commands")
    def remote_commands_stream():
        return _sse_response(_command_broadcaster)

    # -- phone -> addon (PIN-gated if FLASKY_REMOTE_PIN is set) --
    @bp.route("/api/remote/status")
    def remote_status():
        if not _pin_ok(request):
            return jsonify({"ok": False, "error": "invalid pin"}), 401
        return jsonify(_status_snapshot())

    @bp.route("/api/remote/events")
    def remote_events():
        if not _pin_ok(request):
            return jsonify({"ok": False, "error": "invalid pin"}), 401
        return _sse_response(_state_broadcaster, prime=lambda: (
            {"state": _last_state["report"], "server_ts": _last_state["server_ts"]}
            if _last_state["report"] is not None else None
        ))

    @bp.route("/api/remote/command", methods=["POST", "OPTIONS"])
    def remote_command():
        if request.method == "OPTIONS":
            return Response(status=204)
        if not _pin_ok(request):
            return jsonify({"ok": False, "error": "invalid pin"}), 401
        payload = request.get_json(silent=True)
        ok, result = validate_command(payload)
        if not ok:
            return jsonify({"ok": False, "error": result}), 400
        _command_broadcaster.publish(result)
        try:
            state.log("[REMOTE] command: {}".format(result.get("action")))
        except Exception:
            pass
        return jsonify({"ok": True})

    app.register_blueprint(bp)

    try:
        state.log("[REMOTE] Remote-control addon registered ({} PIN)".format(
            "with" if _REMOTE_PIN else "without"))
    except Exception:
        pass
