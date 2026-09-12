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

# -*- coding: utf-8 -*-
"""
restream_addon.py
==================
LAN restreaming for FlaskyIPTV Player.

Problem this solves: most IPTV accounts cap simultaneous connections. If two
devices on the home network both want the same live channel, that's two
provider connections. This addon lets the user pick a channel (the one
currently playing, or any other) and share ONE upstream connection to every
device on the LAN — one ffmpeg process per channel, fanned out to as many
local clients as attach.

Design notes (why this isn't a straight port of a reference restreamer app)
-----------------------------------------------------------------------
A reference standalone implementation (raw-socket HTTP server, its own
portal login code, its own VPN-proxy detection, firewall automation, tkinter
GUI) was analyzed for this feature. Only the actually-novel part — one
upstream ffmpeg shared across many downstream consumers, with idle-timeout
teardown, restart-with-backoff, and backpressure-based slow-client eviction
— is carried over, reimplemented against Flask instead of raw sockets:

  * URL resolution reuses the app's own `_make_client()` / `resolve_item_url()`
    (portal login, backup-URL failover, SSRF hardening all already live
    there) instead of duplicating source/portal client code.
  * No VPN-proxy auto-detection: the app already successfully resolves and
    plays every channel today, so the broadcaster just reuses that same
    resolution — there is nothing new to route through a VPN.
  * No firewall automation, no Xtream-login emulation surface: out of scope
    for "restream the channel I pick to my LAN", and the app is already
    LAN-reachable the same way remote_addon.py's remote control is
    (flask_app.run(host="0.0.0.0", ...)).
  * Delivery to each LAN client is a Flask `Response(stream_with_context(...))`
    generator pulling from a per-client `queue.Queue`, matching the
    generator/`GeneratorExit` cleanup pattern proxy_addon.py already uses for
    its own live ffmpeg-piped responses — not a second bespoke HTTP server.

Integration points in FlaskyIPTV_Player_byGG.py (see accompanying diffs):
  * `register_restream_routes(flask_app, state, run_async, _make_client,
    _FFMPEG_PATH, _FFPROBE_PATH)` — called once at startup, same convention
    as every other `register_*_routes()` addon.
  * One toolbar button + one `<script src="/api/restream/ui.js">` tag.
  * A 3-line addition inside `playItem()` so "restream the channel I'm
    currently watching" still refers to the right channel after the user
    has browsed elsewhere while it keeps playing in the background — see
    `_LASTLIVE_JS_HOOK` below and the docstring on why this is necessary.

Async dispatch — read this before changing anything that calls resolve_item_url
---------------------------------------------------------------------------
Two different call sites need a fresh resolved URL, and they are NOT
interchangeable:

  1. Request-triggered (first "start" click, or a LAN client's first GET to
     /api/restream/live/<key>.ts): runs on a genuine Flask request thread.
     Safe to use the app's own shared `run_async` here — this is exactly
     the context probe_addon.py's /api/resolve already uses it from.

  2. Background-triggered (ffmpeg died, this handle's own reader thread is
     respawning it): NOT a Flask request thread. The app's `run_async`'s
     M3U-only fallback path assigns `state._connect_loop` /
     `state._connect_task` — fields reserved for cancelling an in-flight
     *portal connect*, not safe for concurrent unrelated callers to share.
     `run_worker` is also the wrong tool: it claims the single app-wide
     `state.busy` / `state.worker_thread` slot that MKV downloads and DVR
     recording depend on — a restream restart stealing that slot mid-download
     would misreport progress and could stomp the download's own on_done
     callback. See `RestreamManager._run_async_background()` for the
     dedicated dispatcher used instead. When a PortalSessionManager is
     active (the common case), it still dispatches to the *same* persistent
     event loop `run_async` uses (via `mgr.submit()`, which is designed for
     concurrent callers) — only the M3U-fallback path is actually
     different.

Lifecycle — deliberately NOT wired into state.addon_active_checks /
state.addon_abort_hooks
---------------------------------------------------------------------------
AppState declares these two lists for addons to participate in the
heartbeat-driven watchdog that aborts active downloads/recordings when the
browser tab closes (`last_client_heartbeat`, updated every 5s by the
frontend). Restreaming is deliberately exempt from that: the entire point
is serving OTHER devices (a TV, a phone) independently of the FlaskyIPTV
browser tab staying open — closing the tab that started a restream must not
kill it. Whatever consumes those two lists isn't in this addon's view of the
codebase (it isn't in FlaskyIPTV_Player_byGG.py itself), so this also avoids
guessing at a contract this addon can't see. Lifecycle here is fully
self-contained instead: an idle-timeout watchdog thread (mirrors the
reference implementation's own design) plus `atexit` cleanup on interpreter
shutdown.
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import hashlib
import html
import ipaddress
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Optional

from flask import request, jsonify, Response, stream_with_context


# ============================================================================
# Tunables
# ============================================================================
IDLE_TIMEOUT_SECS      = 20     # kill ffmpeg this many seconds after the last LAN client leaves
START_GRACE_SECS       = 90     # ...but the FIRST time (nobody has attached yet), give the user
                                 # time to actually go open their TV app before we reap it
MAX_RESTARTS           = 6      # ffmpeg restart attempts before giving up on a channel
MAX_CONCURRENT_STREAMS = 8      # distinct channels restreaming at once (distinct upstream conns)
CLIENT_QUEUE_MAXSIZE   = 300    # ~64KB chunks -> up to ~19MB backlog before a client is "too slow"
READ_CHUNK             = 64 * 1024
RESOLVE_TIMEOUT_SECS   = 20     # bound on resolve_item_url() calls made from background threads
WATCHDOG_INTERVAL_SECS = 2
CLIENT_Q_GET_TIMEOUT   = 30     # generator poll interval — see _restream_live()'s _gen()
START_POLL_SECS        = 0.6    # how long start_foreground() waits for an early ffmpeg exit
RESTART_BACKOFF_STEP   = 1.5    # seconds added per restart attempt (linear, capped below)
RESTART_BACKOFF_CAP    = 6.0    # max seconds between restart attempts
RATE_SAMPLE_MIN_SECS   = 0.5    # ignore rate updates spaced closer than this (avoids a
                                 # divide-by-a-near-zero-interval spike right after spawn)

_SENTINEL = object()  # pushed into a client's queue to mean "no more data, stream is over"


# ============================================================================
# Process-death safety
# ============================================================================
# Two gaps daemon threads + atexit can't close on their own:
#
#  1. Python's *default* handler for SIGTERM does not run atexit callbacks —
#     only a normal return from main(), sys.exit(), or an uncaught
#     KeyboardInterrupt (Ctrl+C) does. A plain SIGTERM (a process manager
#     asking the app to stop, `kill` without -9, however Termux/Android may
#     signal an app to quit) would otherwise skip stop_all() entirely and
#     leave every active ffmpeg running. Installing a handler that raises
#     SystemExit turns SIGTERM into the same shutdown path Ctrl+C already
#     takes.
#
#  2. A signal a handler *can't* intercept (SIGKILL, the OOM killer,
#     Android force-stopping the app under memory pressure) gives the
#     process no chance to run ANY Python code, handler or atexit alike.
#     The only thing that reliably stops the ffmpeg child in that case is
#     the OS itself — on Linux, PR_SET_PDEATHSIG asks the kernel to send the
#     child a signal automatically the moment its parent thread dies, no
#     cooperation from that parent required. This is Linux-only (no
#     equivalent on Windows, and none on macOS either, but macOS isn't one
#     of this app's targets); it's applied to every ffmpeg spawn on POSIX
#     and is a no-op everywhere else.
#
# Neither of these closes the gap completely — see _spawn()'s docstring and
# the module-level note further down for what's still unprotected on
# Windows.
_SIGTERM_HANDLER_INSTALLED = False


def _install_sigterm_handler() -> None:
    global _SIGTERM_HANDLER_INSTALLED
    if _SIGTERM_HANDLER_INSTALLED:
        return
    try:
        def _on_sigterm(signum, frame):
            sys.exit(0)  # goes through normal interpreter shutdown -> atexit runs
        signal.signal(signal.SIGTERM, _on_sigterm)
        _SIGTERM_HANDLER_INSTALLED = True
    except (ValueError, OSError, AttributeError):
        # ValueError: not called from the main thread (signal handlers can only
        # be installed there) — if some future caller registers this addon
        # from a worker thread, skip rather than crash; atexit still covers
        # the normal-shutdown path on its own.
        pass


def _posix_pdeathsig_preexec():
    """preexec_fn target for subprocess.Popen — Linux only. Deliberately the
    only thing this does is one ctypes call: Python's docs warn preexec_fn
    can deadlock a multi-threaded parent at fork() if it does anything that
    touches a lock also held by another thread, and this app is heavily
    threaded (Flask threaded=True plus this addon's own reader/watchdog
    threads). A single, lock-free prctl() call is about as safe as a
    preexec_fn gets, but it's not zero-risk — noted here rather than left
    implicit."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    except Exception:
        pass  # best-effort only — never let this be why ffmpeg fails to start


# ============================================================================
# LAN / private-network helpers
# ============================================================================
# Networks that a "what's my LAN IP" guess should never advertise to other
# devices: loopback, unspecified, multicast, and 198.18.0.0/15 — the range
# Clash/v2ray-style VPN clients commonly use for TUN-mode synthetic
# interfaces. Those addresses resolve fine *on this machine* but are
# unreachable from a TV or phone elsewhere on the LAN.
_NEVER_ADVERTISE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "127.0.0.0/8", "198.18.0.0/15", "224.0.0.0/4",
))


def detect_lan_ip(default: str = "127.0.0.1") -> str:
    """Best-effort guess at this machine's LAN-reachable IPv4 address.

    Deliberately simpler than the reference implementation's version (which
    parses `ipconfig`/`ifconfig`/`ip addr` output and ranks interfaces by
    name). That complexity mainly helps pick the *best* answer when there
    are several candidate interfaces; the failure mode this app actually
    needs to avoid is returning an obviously-wrong one (loopback, a TUN
    artifact). The user can always override via the Restream panel's IP
    field — see /api/restream/set_ip — so auto-detection only needs to be a
    reasonable default, not perfect.
    """
    candidates = []
    try:
        # UDP "connect" sends no packets — it just asks the OS to pick the
        # outbound interface for that destination, which is what we want.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.3)
            s.connect(("8.8.8.8", 80))
            candidates.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in candidates:
                candidates.append(ip)
    except socket.gaierror:
        pass

    for ip in candidates:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if any(addr in net for net in _NEVER_ADVERTISE_NETS):
            continue
        if addr.is_private or addr.is_global:
            return str(addr)
    return default


def is_private_addr(ip: str) -> bool:
    """True for RFC1918 / loopback / link-local addresses.

    Used to gate every restream endpoint to LAN-only callers. The app
    already binds 0.0.0.0 (remote_addon.py's remote control needs that too),
    so this is the belt to that suspenders: an unauthenticated stream
    endpoint that also pulls real bandwidth from the provider connection is
    a worse thing to have reachable off-LAN than the control UI is, on the
    off chance this machine is ever on a network it doesn't fully trust.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def _require_lan(fn):
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        addr = request.remote_addr or ""
        if not is_private_addr(addr):
            return jsonify({"ok": False, "error": "restream endpoints are LAN-only"}), 403
        return fn(*args, **kwargs)
    return _wrapped


# ============================================================================
# Target / key derivation
# ============================================================================
def _make_target_key(connect_epoch: int, mode: str, item: dict, name: str) -> str:
    """Stable id for one restreamable channel, embedded in LAN-facing URLs.

    Scoped to `connect_epoch` (AppState._connect_epoch, incremented on every
    api_connect()) rather than just the item's own id. Portal-assigned ids
    (`item['id']` / `stream_id` / `cmd`) are only guaranteed unique *within*
    that portal — without this, reconnecting to a different portal whose
    numbering happens to overlap the previous one could make a brand-new
    restream request resolve to a stale target still pointing at the old
    portal's credentials. Folding the epoch in means every reconnect gets a
    fresh key namespace by construction: old keys just go unreferenced and
    idle-timeout away normally. The trade-off — a LAN playlist URL doesn't
    survive reconnecting to a different playlist — is the safe direction to
    fail in.
    """
    raw_id = str(item.get("id") or item.get("stream_id") or item.get("cmd")
                 or item.get("url") or name or "")
    raw = f"{connect_epoch}|{mode}|{raw_id}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]


def _m3u_attr(value: str) -> str:
    return html.escape(value or "", quote=True)


class RestreamTarget:
    """Everything needed to (re)resolve one item's stream URL, plus the
    display metadata used in the LAN playlist. Registered once when the user
    starts restreaming something (via the modal); the underlying StreamHandle
    (the actual shared ffmpeg process) is created/destroyed independently as
    LAN clients come and go — this class only holds what's needed to
    (re)resolve it.

    All three modes (live/vod/series) go through the same StreamHandle
    broadcaster — see StreamHandle._handle_exit() for the one place they
    behave differently: a live stream retries on exit, a vod/series stream
    just ends (it reached the end of the file, not a failure)."""

    __slots__ = ("key", "name", "logo", "group", "item", "mode", "category",
                 "connect_epoch", "added_at")

    def __init__(self, key, name, logo, group, item, mode, category, connect_epoch):
        self.key = key
        self.name = name
        self.logo = logo
        self.group = group
        self.item = item
        self.mode = mode
        self.category = category
        self.connect_epoch = connect_epoch
        self.added_at = time.time()


# ============================================================================
# Per-LAN-client delivery queue
# ============================================================================
class _LiveClient:
    __slots__ = ("q", "addr", "joined_at", "bytes_sent")

    def __init__(self, addr: str):
        self.q: "queue.Queue" = queue.Queue(maxsize=CLIENT_QUEUE_MAXSIZE)
        self.addr = addr
        self.joined_at = time.time()
        self.bytes_sent = 0


def _client_end(client: "_LiveClient") -> None:
    """Signal a client's generator to stop, without blocking on a full queue."""
    try:
        client.q.put_nowait(_SENTINEL)
        return
    except queue.Full:
        pass
    try:
        while True:
            client.q.get_nowait()
    except queue.Empty:
        pass
    try:
        client.q.put_nowait(_SENTINEL)
    except queue.Full:
        pass  # generator will notice via its own poll timeout instead


# ============================================================================
# StreamHandle — one upstream ffmpeg process, fanned out to N LAN clients
# ============================================================================
class StreamHandle:
    _BENIGN_STDERR_NOISE = (
        "decode_slice_header error", "no frame!", "concealing",
        "cabac_init_idc", "out of range poc", "left block unavailable",
        "error while decoding mb",
    )

    def __init__(self, mgr: "RestreamManager", target: RestreamTarget):
        self.mgr = mgr
        self.target = target
        self.lock = threading.RLock()
        self.clients: dict = {}          # int -> _LiveClient
        self._client_seq = 0
        self.main_screen_client: Optional["_LiveClient"] = None  # see add_client()/leave_main_screen()
        self.proc: Optional[subprocess.Popen] = None
        self.state = "idle"              # idle | starting | running | error | stopped
        self.error = ""
        self.started_at = 0.0
        self.last_client_left = 0.0      # 0.0 == nobody has left yet
        self.ever_had_client = False
        self.restarts = 0
        self.bytes_out = 0
        self.resolved_url = ""
        self.transcode_mode = "copy"
        self._codec_checked = False
        self.codec_info = ""             # e.g. "1920x1080 h264" — from the up-front probe, best-effort
        self.kbps = 0.0                  # current throughput estimate (kilobits/sec), updated by the
                                          # manager's watchdog tick — see RestreamManager._watch()
        self._rate_sample_time = 0.0
        self._rate_sample_bytes = 0

    # -- state -------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self.state in ("starting", "running")

    def update_rate(self, now: float) -> None:
        """Called once per watchdog tick for every handle, running or not —
        recomputes a smoothed throughput estimate from the delta since the
        last tick. Deliberately separate from the idle-reap check below it
        in _watch(): that check `continue`s early for handles WITH clients
        (nothing to reap), but rate tracking needs to run for exactly those
        handles — the ones actually moving bytes."""
        with self.lock:
            if self.state != "running":
                self.kbps = 0.0
                self._rate_sample_time = now
                self._rate_sample_bytes = self.bytes_out
                return
            elapsed = now - self._rate_sample_time
            if self._rate_sample_time and elapsed >= RATE_SAMPLE_MIN_SECS:
                delta_bytes = self.bytes_out - self._rate_sample_bytes
                self.kbps = round((delta_bytes * 8) / 1024 / elapsed, 1)
            if not self._rate_sample_time or elapsed >= RATE_SAMPLE_MIN_SECS:
                self._rate_sample_time = now
                self._rate_sample_bytes = self.bytes_out

    def snapshot(self) -> dict:
        with self.lock:
            clients = len(self.clients)
            return {
                "key": self.target.key,
                "name": self.target.name,
                "group": self.target.group,
                "logo": self.target.logo,
                "mode": self.target.mode,
                "state": self.state,
                "error": self.error,
                "clients": clients,
                "client_hosts": sorted({c.addr for c in self.clients.values()}),
                "uptime": int(time.time() - self.started_at) if self.started_at else 0,
                "mb_sent": round(self.bytes_out / 1_048_576, 1),
                "restarts": self.restarts,
                "transcode_mode": self.transcode_mode,
                "codec_info": self.codec_info,
                "kbps": self.kbps,                       # upstream: what this channel costs the one
                                                              # provider connection (same for every client)
                "lan_kbps": round(self.kbps * clients, 1),  # downstream: kbps repeated once per attached
                                                              # LAN client — the number that actually
                                                              # competes with the rest of the home network
            }

    # -- foreground path: MUST be called from a genuine Flask request thread
    def start_foreground(self, poll_secs: float = None) -> dict:
        if poll_secs is None:
            poll_secs = START_POLL_SECS
        with self.lock:
            if self.alive:
                return {"ok": True, **self.snapshot()}
            self.state = "starting"
            self.error = ""
            self.started_at = time.time()
        ok = self._spawn(background=False)
        if ok:
            self._poll_for_early_exit(poll_secs)
        with self.lock:
            ok_now = self.state != "error"
        return {"ok": ok_now, **self.snapshot()}

    def _poll_for_early_exit(self, poll_secs: float) -> None:
        """Give a doomed ffmpeg (bad URL, geo-block, auth failure) a brief
        window to exit before we tell the caller "started" — mirrors
        proxy_addon.py's api_hls_proxy pre-flight check, same rationale:
        report the real error instead of silently failing on the LAN side."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < poll_secs:
            with self.lock:
                proc = self.proc
            if proc is None:
                return
            if proc.poll() is not None:
                time.sleep(0.15)  # let the stderr-drain thread log the reason
                return
            time.sleep(0.05)

    # -- resolve -------------------------------------------------------------
    def _resolve(self, background: bool) -> str:
        target = self.target
        cur_epoch = getattr(self.mgr.state, "_connect_epoch", 0)
        if target.connect_epoch != cur_epoch:
            raise RuntimeError(
                "the portal connection changed since this restream was started "
                "(reconnected/disconnected) — open the Restream panel and start "
                "this channel again from the currently-connected portal"
            )

        async def _do():
            async with self.mgr.make_client() as client:
                return await client.resolve_item_url(target.mode, target.item, target.category)

        if background:
            url = self.mgr.run_async_background(_do(), timeout=RESOLVE_TIMEOUT_SECS)
        else:
            url = self.mgr.run_async(_do())

        if not url or not isinstance(url, str):
            raise RuntimeError("portal did not return a stream URL for this channel")
        return url

    def _decide_transcode_mode(self, url: str) -> None:
        """One up-front codec check (reusing the app's own ffprobe detection —
        the same helpers /api/resolve already calls) so a channel that's
        known to need transcoding doesn't have to fail in copy mode first.
        Best-effort: any probe failure just leaves copy mode, same as the
        rest of the app falls back to direct play when ffprobe can't tell."""
        try:
            from probe_addon import probe_stream_codecs, _check_codecs
            pre_args = [
                "-user_agent", self.mgr.state.stream_ua,
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                "-analyzeduration", "2000000", "-probesize", "500000",
            ]
            codecs = probe_stream_codecs(url, pre_input_args=pre_args, timeout=8,
                                          ffprobe_path=self.mgr.ffprobe_path)
            if codecs:
                needs_transcode, reason, codec_name = _check_codecs(codecs)
                if codec_name:
                    w, h = codecs.get("width"), codecs.get("height")
                    self.codec_info = f"{w}x{h} {codec_name}" if w and h else codec_name
                if needs_transcode:
                    self.transcode_mode = "transcode"
                    self.mgr.log(f"[restream] '{self.target.name}': transcode mode ({reason})")
        except Exception as exc:
            self.mgr.log(f"[restream] '{self.target.name}': codec probe skipped ({exc})")
        finally:
            self._codec_checked = True

    # -- ffmpeg --------------------------------------------------------------
    def _build_cmd(self, url: str) -> list:
        cmd = [self.mgr.ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "warning"]
        if url.split(":", 1)[0].lower() in ("http", "https"):
            cmd += [
                "-user_agent", self.mgr.state.stream_ua,
                "-referer", url.rsplit("/", 1)[0] + "/",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "10",
                "-rw_timeout", "20000000",
                "-thread_queue_size", "512",
            ]
        cmd += ["-fflags", "+genpts+igndts+discardcorrupt", "-err_detect", "ignore_err"]
        cmd += ["-i", url]

        if self.transcode_mode == "transcode":
            cmd += [
                "-map", "0:v:0?", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                "-crf", "23", "-maxrate", "8M", "-bufsize", "16M",
                "-pix_fmt", "yuv420p", "-g", "50",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000", "-sn",
            ]
        else:
            cmd += ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-c", "copy"]

        cmd += ["-avoid_negative_ts", "make_zero", "-flush_packets", "1",
                "-f", "mpegts", "pipe:1"]
        return cmd

    def _spawn(self, background: bool) -> bool:
        try:
            url = self._resolve(background=background)
        except Exception as exc:
            self._fail(str(exc))
            return False

        if not self._codec_checked:
            self._decide_transcode_mode(url)
        elif self.restarts >= 2 and self.transcode_mode == "copy":
            # Copy mode has now failed repeatedly for a reason the up-front
            # probe didn't catch (e.g. a mid-stream codec change on the
            # provider's end) — fall back to transcode, same "auto" idea the
            # reference implementation uses.
            self.transcode_mode = "transcode"
            self.mgr.log(f"[restream] '{self.target.name}': switching to transcode "
                          f"after repeated copy-mode failures")

        cmd = self._build_cmd(url)
        self.resolved_url = url

        popen_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, bufsize=0)
        if os.name == "nt":
            # Long-lived background subprocess — worth the belt-and-suspenders
            # console-window suppression even though the existing hls_proxy
            # ffmpeg calls elsewhere in this app don't bother (those are
            # short-lived and tied to a browser tab the user is watching).
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            # See _posix_pdeathsig_preexec()'s docstring for what this does
            # and its trade-off. Windows has no equivalent — a hard kill of
            # the Flask process there can still orphan ffmpeg.exe; see the
            # module docstring's "Process-death safety" note.
            popen_kwargs["preexec_fn"] = _posix_pdeathsig_preexec

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError:
            self._fail("ffmpeg not found")
            return False
        except Exception as exc:
            self._fail(f"could not start ffmpeg: {exc}")
            return False

        with self.lock:
            if self.state == "stopped":
                try:
                    proc.kill()
                except Exception:
                    pass
                return False
            self.proc = proc

        self.mgr.log(f"[restream] '{self.target.name}' ffmpeg PID {proc.pid} started "
                      f"({self.transcode_mode}, attempt {self.restarts + 1})")
        threading.Thread(target=self._reader, args=(proc,), daemon=True,
                          name=f"restream-rd-{self.target.key}").start()
        threading.Thread(target=self._stderr_drain, args=(proc,), daemon=True,
                          name=f"restream-err-{self.target.key}").start()
        return True

    def _fail(self, message: str) -> None:
        with self.lock:
            self.state = "error"
            self.error = message
            self.proc = None
            clients = list(self.clients.values())
            self.clients.clear()
        self.mgr.log(f"[restream] \u2717 '{self.target.name}': {message}")
        for c in clients:
            _client_end(c)

    # -- reader / fan-out ------------------------------------------------
    def _reader(self, proc: subprocess.Popen) -> None:
        try:
            with self.lock:
                if self.proc is proc:
                    self.state = "running"
            while True:
                chunk = proc.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                self.bytes_out += len(chunk)
                self._deliver(chunk)
        except Exception as exc:
            self.mgr.log(f"[restream] '{self.target.name}' reader error: {exc!r}")
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            self._handle_exit(proc)

    def _deliver(self, chunk: bytes) -> None:
        with self.lock:
            items = list(self.clients.items())
        dead_ids = []
        for cid, client in items:
            try:
                client.q.put_nowait(chunk)
                client.bytes_sent += len(chunk)
            except queue.Full:
                dead_ids.append(cid)
        if dead_ids:
            removed = []
            with self.lock:
                for cid in dead_ids:
                    c = self.clients.pop(cid, None)
                    if c is not None:
                        removed.append(c)
                if not self.clients:
                    self.last_client_left = time.time()
            for c in removed:
                self.mgr.log(f"[restream] {c.addr} dropped from '{self.target.name}' (too slow)")
                _client_end(c)

    def _handle_exit(self, proc: subprocess.Popen) -> None:
        try:
            code = proc.wait(timeout=10)
        except Exception:
            code = -1

        with self.lock:
            if self.proc is not proc or self.state == "stopped":
                return  # a newer proc replaced this one, or stop() already ran

        if self.target.mode in ("vod", "series"):
            # A movie/episode plays forward once and ends — ffmpeg reaching
            # EOF is normal completion, not a failure to recover from.
            # Auto-restarting here (live's behavior below) would silently
            # jump everyone still attached back to the beginning, which is
            # worse than just ending the stream. Wanting to watch it again
            # is a fresh "restream this" click from the modal, which spawns
            # a new ffmpeg from the start via the normal start_foreground()
            # path — same as attaching to a since-reaped key does already.
            with self.lock:
                clients = list(self.clients.values())
                self.clients.clear()
                self.state = "idle"
                self.proc = None
            for c in clients:
                _client_end(c)
            self.mgr.log(f"[restream] '{self.target.name}' finished (exit code {code})")
            return

        with self.lock:
            has_clients = bool(self.clients)
            still_in_grace = (not self.ever_had_client
                               and time.time() - self.started_at < START_GRACE_SECS)
        if not has_clients and not still_in_grace:
            with self.lock:
                self.state = "idle"
                self.proc = None
            return

        with self.lock:
            if self.restarts >= MAX_RESTARTS:
                give_up = True
            else:
                give_up = False
                self.restarts += 1
                self.state = "starting"
                self.proc = None

        if give_up:
            self._fail(f"upstream kept failing (exit code {code}, {self.restarts} attempts)")
            return

        backoff = min(RESTART_BACKOFF_STEP * self.restarts, RESTART_BACKOFF_CAP)
        self.mgr.log(f"[restream] '{self.target.name}' ffmpeg exited ({code}), "
                      f"retrying in {backoff:.1f}s (attempt {self.restarts + 1})")
        time.sleep(backoff)
        with self.lock:
            if self.state == "stopped":
                return
        self._spawn(background=True)

    def _stderr_drain(self, proc: subprocess.Popen) -> None:
        limit, count = 60, 0
        seen_noise = set()
        try:
            for raw in iter(proc.stderr.readline, b""):
                if count >= limit:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                low = line.lower()
                noise = next((p for p in self._BENIGN_STDERR_NOISE if p in low), None)
                if noise:
                    seen_noise.add(noise)
                    continue
                count += 1
                self.mgr.log(f"[restream/ffmpeg] '{self.target.name}': {line[:200]}")
        except Exception:
            pass

    # -- clients -----------------------------------------------------------
    def add_client(self, addr: str, is_main: bool = False) -> "_LiveClient":
        client = _LiveClient(addr)
        with self.lock:
            self._client_seq += 1
            self.clients[self._client_seq] = client
            self.ever_had_client = True
            self.last_client_left = 0.0
            if is_main:
                self.main_screen_client = client
        self.mgr.log(f"[restream] {addr} joined '{self.target.name}' "
                      f"({len(self.clients)} watching)")
        return client

    def remove_client(self, client: "_LiveClient") -> None:
        with self.lock:
            for cid, c in list(self.clients.items()):
                if c is client:
                    del self.clients[cid]
                    break
            if self.main_screen_client is client:
                self.main_screen_client = None
            if not self.clients:
                self.last_client_left = time.time()

    def leave_main_screen(self) -> bool:
        """Explicitly detach the main screen's own client, if it's currently
        attached here. Called when the app's own playback is about to show
        something else (see the /api/restream/leave_main route and the
        playItem() hook in the main file) — deliberately not left to the
        underlying HTTP connection closing on its own. A <video> element
        switching sources doesn't reliably (or promptly) abort the old
        fetch/connection from the server's point of view, which would
        otherwise leave a stale client registered indefinitely: still
        counted as "watching" with the idle-timeout never triggering, since
        from this handle's perspective clients is never empty. Returns
        True if a main-screen client was actually found and removed."""
        with self.lock:
            client = self.main_screen_client
            if client is None:
                return False
            for cid, c in list(self.clients.items()):
                if c is client:
                    del self.clients[cid]
                    break
            self.main_screen_client = None
            if not self.clients:
                self.last_client_left = time.time()
        _client_end(client)
        self.mgr.log(f"[restream] main screen left '{self.target.name}'")
        return True

    def stop(self, reason: str = "") -> None:
        with self.lock:
            if self.state == "stopped":
                return
            self.state = "stopped"
            proc, self.proc = self.proc, None
            clients = list(self.clients.values())
            self.clients.clear()
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        for c in clients:
            _client_end(c)
        self.mgr.log(f"[restream] stopped '{self.target.name}'"
                      + (f" ({reason})" if reason else ""))


# ============================================================================
# RestreamManager — owns all targets/handles, the idle watchdog, playlist
# ============================================================================
class RestreamManager:
    def __init__(self, state, run_async, make_client, ffmpeg_path, ffprobe_path, log_fn=None):
        self.state = state
        self.run_async = run_async
        self.make_client = make_client
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.log = log_fn or (lambda msg: None)
        self.targets: dict = {}   # key -> RestreamTarget
        self.streams: dict = {}   # key -> StreamHandle
        self.lock = threading.RLock()
        self._shutdown = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, daemon=True,
                                           name="restream-watchdog")
        self._watchdog.start()
        atexit.register(self.stop_all)
        _install_sigterm_handler()

    # -- background-thread-safe async dispatch — see module docstring ------
    def run_async_background(self, coro, timeout=None):
        if timeout is None:
            timeout = RESOLVE_TIMEOUT_SECS
        mgr = getattr(self.state, "portal_mgr", None)
        if mgr is not None and not mgr.loop.is_closed():
            return mgr.submit(coro, timeout=timeout)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
        finally:
            loop.close()

    # -- start / attach (both MUST run on a Flask request thread) ----------
    def start(self, *, item: dict, mode: str, category: dict, name: str,
              logo: str = "", group: str = "") -> dict:
        if mode not in ("live", "vod", "series"):
            return {"ok": False, "error": "unsupported mode"}
        if not item:
            return {"ok": False, "error": "missing item"}

        epoch = getattr(self.state, "_connect_epoch", 0)
        key = _make_target_key(epoch, mode, item, name)
        with self.lock:
            target = self.targets.get(key)
            if target is None:
                target = RestreamTarget(key, name or "Channel", logo, group,
                                         item, mode, category, epoch)
                self.targets[key] = target
            handle = self.streams.get(key)
            if handle is None:
                alive_count = sum(1 for h in self.streams.values() if h.alive)
                if alive_count >= MAX_CONCURRENT_STREAMS:
                    return {"ok": False, "error": f"too many simultaneous restreams "
                                                    f"(limit {MAX_CONCURRENT_STREAMS}) — "
                                                    f"stop one first"}
                handle = StreamHandle(self, target)
                self.streams[key] = handle

        result = handle.start_foreground()
        result["playlist_url"] = self.playlist_url()
        result["stream_url"] = self.stream_url(key)
        result["key"] = key
        return result

    def attach(self, key: str, addr: str, is_main: bool = False):
        """A LAN client's GET to /live/<key>.ts — any mode. is_main marks
        this specific connection as the app's own main-screen playback (see
        StreamHandle.leave_main_screen()) rather than an external device.
        Returns (handle, client); client is None if the key is unknown or
        the handle failed to start."""
        with self.lock:
            target = self.targets.get(key)
            if target is None:
                return None, None
            handle = self.streams.get(key)
            if handle is None:
                handle = StreamHandle(self, target)
                self.streams[key] = handle

        result = handle.start_foreground()
        if not result.get("ok"):
            return handle, None
        client = handle.add_client(addr, is_main=is_main)
        return handle, client

    def leave_main_screen(self, key: str) -> bool:
        with self.lock:
            handle = self.streams.get(key)
        if handle is None:
            return False
        return handle.leave_main_screen()

    def stop_target(self, key: str) -> bool:
        with self.lock:
            handle = self.streams.pop(key, None)
            target_removed = self.targets.pop(key, None) is not None
        if handle is not None:
            handle.stop("stopped from restream panel")
        return target_removed or handle is not None

    def list_active(self) -> list:
        with self.lock:
            handles = list(self.streams.values())
        return [h.snapshot() for h in handles]

    # -- URLs ----------------------------------------------------------------
    def base_url(self) -> str:
        """Built from the live request's Host header (for the port — the
        app's actual bound port isn't known until deep inside
        `if __name__ == "__main__":`, well after addon registration, so it
        can't be threaded through at register_restream_routes() time) plus
        the detected/overridden LAN IP (for the host — request.host's own
        hostname is whatever the CALLER dialed, e.g. 127.0.0.1 if this
        request came from the same machine's own browser, which is useless
        to hand to a different device)."""
        host_hdr = request.host or "127.0.0.1:5000"
        port = host_hdr.rsplit(":", 1)[-1] if ":" in host_hdr else "80"
        ip = (getattr(self.state, "restream_advertise_ip", "") or "").strip() or detect_lan_ip()
        return f"http://{ip}:{port}"

    def playlist_url(self) -> str:
        return f"{self.base_url()}/api/restream/playlist.m3u"

    def stream_url(self, key: str) -> str:
        return f"{self.base_url()}/api/restream/live/{key}.ts"

    def build_playlist_m3u(self) -> str:
        with self.lock:
            targets = list(self.targets.values())
        base = self.base_url()
        lines = ["#EXTM3U"]
        for t in sorted(targets, key=lambda x: (x.group.lower(), x.name.lower())):
            attrs = [f'tvg-name="{_m3u_attr(t.name)}"']
            if t.logo:
                attrs.append(f'tvg-logo="{_m3u_attr(t.logo)}"')
            if t.group:
                attrs.append(f'group-title="{_m3u_attr(t.group)}"')
            lines.append("#EXTINF:-1 " + " ".join(attrs) + "," + t.name)
            lines.append(f"{base}/api/restream/live/{t.key}.ts")
        return "\n".join(lines) + "\n"

    # -- lifecycle -----------------------------------------------------------
    def _watch(self) -> None:
        while not self._shutdown.is_set():
            time.sleep(WATCHDOG_INTERVAL_SECS)
            now = time.time()
            with self.lock:
                items = list(self.streams.items())
            for key, handle in items:
                handle.update_rate(now)
                with handle.lock:
                    if handle.clients:
                        continue
                    if handle.state not in ("running", "starting", "error"):
                        continue
                    grace = IDLE_TIMEOUT_SECS if handle.ever_had_client else START_GRACE_SECS
                    deadline = handle.last_client_left or handle.started_at or now
                    timed_out = (now - deadline) > grace
                if not timed_out:
                    continue
                with self.lock:
                    if self.streams.get(key) is not handle:
                        continue
                    del self.streams[key]
                handle.stop(f"idle {grace}s")

    def stop_all(self) -> None:
        self._shutdown.set()
        with self.lock:
            handles = list(self.streams.values())
            self.streams.clear()
            self.targets.clear()
        for h in handles:
            h.stop("app shutting down")


# ============================================================================
# Frontend: toolbar button + modal, injected at runtime (matches the
# radio_addon.py pattern — UI JS extracted into the addon, not the main
# file's HTML template)
# ============================================================================
_RESTREAM_UI_JS = r"""
(function(){
  'use strict';
  if (window.__restreamAddonLoaded) return;
  window.__restreamAddonLoaded = true;

  // ---- styles -----------------------------------------------------------
  const css = `
#restream-btn.active{background:rgba(124,58,237,.18)!important;color:var(--acc)!important;
  border-color:rgba(124,58,237,.5)!important;box-shadow:0 0 10px rgba(124,58,237,.25)}
.restream-badge{position:absolute;top:2px;right:2px;min-width:14px;height:14px;padding:0 3px;
  border-radius:8px;background:var(--acc);color:#fff;font-size:9px;font-weight:800;
  line-height:14px;text-align:center;display:none;box-shadow:0 0 0 2px rgba(8,8,20,.97)}
.restream-badge.show{display:block}
#rst-overlay{position:fixed;inset:0;z-index:700;background:rgba(0,0,0,.72);
  display:none;align-items:center;justify-content:center;padding:12px}
#rst-overlay.open{display:flex}
#rst-modal{background:var(--s2);border:1px solid var(--bdr2);border-radius:var(--r);
  width:min(640px,100%);max-height:90dvh;display:flex;flex-direction:column;
  box-shadow:0 24px 72px rgba(0,0,0,.9),0 0 0 1px rgba(124,58,237,.12);
  transform:translateZ(0);animation:rst-up .22s cubic-bezier(.34,1.3,.64,1)}
@keyframes rst-up{from{opacity:0;transform:translateY(18px) scale(.97)}to{opacity:1;transform:none}}
.rst-hdr{display:flex;align-items:center;gap:10px;padding:14px 16px 12px;
  border-bottom:1px solid var(--bdr);flex-shrink:0;
  background:linear-gradient(135deg,rgba(124,58,237,.08) 0%,transparent 60%)}
.rst-hdr h2{flex:1;font-size:15px;font-weight:800;color:var(--txt);margin:0;letter-spacing:.3px}
.rst-hdr button{background:transparent;border:none;color:var(--txt2);font-size:18px;
  cursor:pointer;padding:2px 6px;line-height:1}
.rst-hdr button:hover{color:var(--txt)}
.rst-tabs{display:flex;gap:3px;padding:8px 10px;border-bottom:1px solid var(--bdr);
  flex-shrink:0;overflow-x:auto}
.rst-tab{height:26px;padding:0 11px;font-size:11px;font-weight:600;border-radius:var(--rss);
  background:transparent;color:var(--txt2);border:1px solid transparent;cursor:pointer;
  white-space:nowrap;transition:all .15s;flex-shrink:0}
.rst-tab.active{background:rgba(124,58,237,.18);color:var(--acc);border-color:rgba(124,58,237,.35)}
.rst-tab:hover:not(.active){background:var(--s4);color:var(--txt)}
.rst-mode-tabs{padding:8px 12px 0;border-bottom:none}
.rst-mode-tabs .rst-tab{height:22px;font-size:10px;background:var(--s4)}
.rst-mode-tabs .rst-tab.active{background:rgba(124,58,237,.22)}
.rst-pane{display:none;flex:1;overflow-y:auto;min-height:120px}
.rst-pane.active{display:flex;flex-direction:column}
.rst-sec{padding:12px}
.rst-cur-card{display:flex;align-items:center;gap:10px;padding:12px;border-radius:var(--rsm);
  background:rgba(124,58,237,.07);border:1px solid rgba(124,58,237,.25);margin-bottom:10px}
.rst-cur-name{flex:1;font-size:13px;font-weight:700;color:var(--txt);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.rst-cur-empty{font-size:12px;color:var(--txt3);padding:10px 2px}
.rst-search-row{display:flex;gap:6px;padding:9px 12px;flex-shrink:0;border-bottom:1px solid var(--bdr)}
.rst-search-row input,.rst-search-row select{flex:1;height:32px;font-size:12px;padding:0 10px;
  border-radius:var(--rss)}
.rst-list{list-style:none;flex:1;overflow-y:auto}
.rst-item{display:flex;align-items:center;gap:8px;padding:9px 12px;cursor:pointer;
  transition:background .12s;border-bottom:1px solid rgba(255,255,255,.03)}
.rst-item:hover{background:rgba(124,58,237,.07)}
.rst-item-logo{width:30px;height:30px;border-radius:5px;object-fit:contain;background:var(--s4);
  flex-shrink:0}
.rst-item-name{flex:1;font-size:12px;font-weight:600;color:var(--txt);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.rst-item button{height:24px;padding:0 9px;font-size:11px;flex-shrink:0}
.rst-note{font-size:11px;color:var(--txt3);padding:8px 12px;line-height:1.5}
.rst-stream-row{display:flex;align-items:center;gap:8px;padding:10px 12px;border-radius:var(--rsm);
  background:rgba(255,255,255,.025);border:1px solid var(--bdr);margin:0 12px 8px}
.rst-stream-info{flex:1;min-width:0}
.rst-stream-name{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700;
  color:var(--txt);overflow:hidden}
.rst-stream-name span.nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rst-stream-meta{font-size:10px;color:var(--txt3);margin-top:2px;line-height:1.5}
.rst-stream-meta.err{color:var(--red,#f66)}
.rst-stream-row button{height:24px;padding:0 9px;font-size:11px;flex-shrink:0}
.rst-xc-badge{flex-shrink:0;font-size:9px;font-weight:800;padding:1px 6px;border-radius:8px;
  text-transform:uppercase;letter-spacing:.3px}
.rst-xc-badge.copy{background:rgba(34,197,94,.12);color:var(--green,#4ade80)}
.rst-xc-badge.transcode{background:rgba(245,158,11,.12);color:var(--orange,#f59e0b)}
.rst-mode-badge{flex-shrink:0;font-size:9px;font-weight:800;padding:1px 6px;border-radius:8px;
  text-transform:uppercase;letter-spacing:.3px;background:rgba(96,165,250,.12);color:#60a5fa}
.rst-summary{display:flex;gap:14px;padding:10px 12px;margin:8px 12px 4px;border-radius:var(--rsm);
  background:rgba(124,58,237,.06);border:1px solid rgba(124,58,237,.2);font-size:11px}
.rst-summary-item{display:flex;flex-direction:column;gap:1px}
.rst-summary-item b{font-size:13px;color:var(--txt);font-weight:800}
.rst-summary-item span{color:var(--txt3);font-size:9px;text-transform:uppercase;letter-spacing:.3px}
.rst-url-row{display:flex;gap:6px;align-items:center;padding:8px 12px}
.rst-url-row input{flex:1;height:30px;font-size:11px;padding:0 8px;border-radius:var(--rss);
  color:var(--txt2);font-family:monospace}
.rst-url-row button{height:30px;padding:0 10px;font-size:11px;flex-shrink:0}
#rst-onair{position:fixed;left:50%;top:10px;transform:translateX(-50%);z-index:650;
  display:none;align-items:center;gap:8px;padding:6px 10px 6px 12px;border-radius:999px;
  background:rgba(20,10,35,.94);border:1px solid rgba(124,58,237,.5);
  box-shadow:0 8px 24px rgba(0,0,0,.5);font-size:11px;color:var(--txt);
  animation:rst-up .22s cubic-bezier(.34,1.3,.64,1)}
#rst-onair.show{display:flex}
#rst-onair .dot{width:7px;height:7px;border-radius:50%;background:#f43f5e;flex-shrink:0;
  animation:rst-pulse 1.6s ease-in-out infinite}
@keyframes rst-pulse{0%,100%{opacity:1}50%{opacity:.35}}
#rst-onair b{font-weight:700}
#rst-onair button{height:22px;padding:0 8px;font-size:10px;flex-shrink:0}
`;
  const styleEl = document.createElement('style');
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  // ---- small helpers ------------------------------------------------------
  function esc(s){ const d=document.createElement('div'); d.textContent = (s==null?'':String(s)); return d.innerHTML; }
  function el(tag, attrs, children){
    const e = document.createElement(tag);
    if(attrs) for(const k in attrs){
      if(k==='class') e.className = attrs[k];
      else if(k==='text') e.textContent = attrs[k];
      else e.setAttribute(k, attrs[k]);
    }
    (children||[]).forEach(c=>{ if(c) e.appendChild(c); });
    return e;
  }
  function toastSafe(msg, kind){
    try{ if(typeof toast==='function') toast(msg, kind); }catch(e){}
  }

  // ---- current-item capture ------------------------------------------------
  // window._lastRestreamTarget is set by a small hook in playItem() (main
  // file) every time ANY item (live/vod/series) actually starts playing,
  // and survives the user browsing to a different category/mode afterward —
  // unlike filtItems/pIdx, which are reset the moment the user navigates.
  // Fall back to reading filtItems[pIdx] directly for the case where
  // something is playing but was started before this addon's hook existed
  // in a given page load (harmless — just won't survive navigation until a
  // fresh play happens).
  //
  // The three checks up front make this reflect what's ACTUALLY playing
  // right now, not merely "the last item played via playItem()" — each of
  // _playerStopped/_curIsRadio/_curIsDirectPlay can become true without
  // playItem() ever running again (pressing Stop, playing a radio station,
  // pasting a direct URL), which would otherwise leave
  // window._lastRestreamTarget silently pointing at a stale, previously-
  // played live/vod/series item. Radio and direct-URL playback are also
  // both genuinely unrestreamable as things stand — RestreamManager.start()
  // rejects any mode outside ("live","vod","series"), and neither a radio
  // station nor a pasted URL is shaped like a catalog item (no id/
  // category/etc.) the way _make_target_key() and the target/category
  // display elsewhere in this modal expect — so null is correct here, not
  // just a stopgap. Falling through to window._lastRestreamTarget (or the
  // filtItems[pIdx] fallback below) is only reached once none of the three
  // hold, which is exactly when it's guaranteed fresh: it's set in the same
  // playItem() call that also sets pIdx, and none of the three gates above
  // can be true without a doPlay() having run since.
  function currentItem(){
    try{
      if(typeof _playerStopped !== 'undefined' && _playerStopped) return null;
      if(typeof _curIsRadio !== 'undefined' && _curIsRadio) return null;
      if(typeof _curIsDirectPlay !== 'undefined' && _curIsDirectPlay) return null;
    }catch(e){}
    try{
      if(window._lastRestreamTarget) return window._lastRestreamTarget;
    }catch(e){}
    try{
      if(typeof mode!=='undefined' &&
         typeof filtItems!=='undefined' && typeof pIdx!=='undefined' &&
         filtItems[pIdx]){
        return {item: filtItems[pIdx], mode:mode, category:(typeof curCat!=='undefined'?curCat:null)||{}};
      }
    }catch(e){}
    return null;
  }
  function modeLabel(m){
    return m==='vod' ? 'movie' : (m==='series' ? 'episode' : 'channel');
  }

  // ---- modal DOM ------------------------------------------------------------
  const overlay = el('div', {id:'rst-overlay'});
  overlay.addEventListener('click', function(e){ if(e.target===overlay) closeRestreamModal(); });
  const modal = el('div', {id:'rst-modal'});
  overlay.appendChild(modal);

  // ---- on-air indicator ------------------------------------------------------
  // Shown whenever THIS browser tab's own player is currently a client of a
  // restream — i.e. once you click Restream, the main screen switches over
  // to watching through the shared broadcast instead of its own separate
  // connection (see startRestream() below for why). Tracks which restream
  // key/URL the main screen is on, so it can tell "stopped elsewhere" and
  // "navigated away to something else" apart and clear itself correctly.
  const onair = el('div', {id:'rst-onair'});
  const onairDot = el('span', {class:'dot'});
  const onairText = el('span');
  const onairCopyBtn = el('button', {text:'Copy URL'});
  const onairStopBtn = el('button', {text:'Stop'});
  onair.appendChild(onairDot);
  onair.appendChild(onairText);
  onair.appendChild(onairCopyBtn);
  onair.appendChild(onairStopBtn);
  let onAirKey = null;
  let onAirUrl = null;
  let onAirName = null;
  function setOnAirText(name, clients){
    onairText.innerHTML = '';
    onairText.appendChild(document.createTextNode('Restreaming '));
    onairText.appendChild(el('b', {text: name}));
    if(typeof clients === 'number' && clients > 0){
      onairText.appendChild(document.createTextNode(
        ' \u00b7 ' + clients + (clients===1 ? ' device' : ' devices')));
    }
  }
  function showOnAir(key, url, name){
    onAirKey = key; onAirUrl = url; onAirName = name;
    setOnAirText(name);
    onair.classList.add('show');
  }
  function clearOnAir(){
    onAirKey = null; onAirUrl = null; onAirName = null;
    onair.classList.remove('show');
  }
  onairCopyBtn.addEventListener('click', function(){ if(onAirUrl) copyToClipboard(onAirUrl); });
  onairStopBtn.addEventListener('click', function(){ if(onAirKey) stopRestream(onAirKey); });
  // Shared by both pollers (the modal's 5s refresh and the always-on 15s
  // background check) so the indicator stays correct whether the modal is
  // open or not. Clears itself if either: the tracked restream is no longer
  // active (stopped, by this tab or from elsewhere), or this tab's own
  // player has moved on to something else (pUrl — the main file's own
  // "what's currently loaded" global — no longer matches what we switched
  // it to; read the same bare-identifier way currentItem() reads
  // filtItems/pIdx, since it's a separate <script> tag, not window.pUrl).
  function syncOnAir(streams){
    if(!onAirKey) return;
    const mine = streams.find(function(s){ return s.key === onAirKey; });
    let navigatedAway = false;
    try{
      navigatedAway = (typeof pUrl !== 'undefined' && !!pUrl && pUrl.split('?')[0] !== onAirUrl);
    }catch(e){}
    if(!mine || navigatedAway){ clearOnAir(); return; }
    onAirName = mine.name;
    setOnAirText(mine.name, mine.clients);
  }
  // Explicit, immediate counterpart to syncOnAir()'s passive pUrl check
  // above (which only runs on the next 5s/15s poll). Exposed so the main
  // file's playItem() can call it the instant the user picks something via
  // NORMAL playback — playItem() is never used for restream-triggered
  // playback (startRestream() calls doPlay() directly), so it firing at all
  // means the main screen is showing something else now, unconditionally.
  window._restreamNotifyNavigated = function(){
    if(!onAirKey) return;
    const leavingKey = onAirKey;
    clearOnAir();
    fetch('/api/restream/leave_main', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({key: leavingKey})}).catch(function(){});
  };

  const hdr = el('div', {class:'rst-hdr'}, [
    el('h2', {text:'\uD83D\uDCE1 Restream to LAN'}),
    el('button', {text:'\u2715', title:'Close'}),
  ]);
  hdr.lastChild.addEventListener('click', function(){ closeRestreamModal(); });
  modal.appendChild(hdr);

  const tabs = el('div', {class:'rst-tabs'});
  const tabNow = el('button', {class:'rst-tab active', text:'Now Playing'});
  const tabBrowse = el('button', {class:'rst-tab', text:'Browse'});
  const tabActive = el('button', {class:'rst-tab', text:'Active Restreams'});
  const tabSettings = el('button', {class:'rst-tab', text:'Settings'});
  [tabNow, tabBrowse, tabActive, tabSettings].forEach(t=>tabs.appendChild(t));
  modal.appendChild(tabs);

  const paneNow = el('div', {class:'rst-pane active'});
  const paneBrowse = el('div', {class:'rst-pane'});
  const paneActive = el('div', {class:'rst-pane'});
  const paneSettings = el('div', {class:'rst-pane'});
  [paneNow, paneBrowse, paneActive, paneSettings].forEach(p=>modal.appendChild(p));

  function selectTab(tab, pane){
    [tabNow, tabBrowse, tabActive, tabSettings].forEach(t=>t.classList.remove('active'));
    [paneNow, paneBrowse, paneActive, paneSettings].forEach(p=>p.classList.remove('active'));
    tab.classList.add('active'); pane.classList.add('active');
  }
  tabNow.addEventListener('click', function(){ selectTab(tabNow, paneNow); renderNowPlaying(); });
  tabBrowse.addEventListener('click', function(){ selectTab(tabBrowse, paneBrowse); if(!browseLoadedOnce) loadCategories(); });
  tabActive.addEventListener('click', function(){ selectTab(tabActive, paneActive); refreshStatus(); });
  tabSettings.addEventListener('click', function(){ selectTab(tabSettings, paneSettings); loadSettings(); });

  // -- "Now Playing" pane -------------------------------------------------
  function renderNowPlaying(){
    paneNow.innerHTML = '';
    const sec = el('div', {class:'rst-sec'});
    const cur = currentItem();
    if(!cur){
      sec.appendChild(el('div', {class:'rst-cur-empty',
        text:'Nothing is currently playing. Start watching something, or use Browse to pick an item directly.'}));
    } else {
      const it = cur.item || {};
      const name = it.name || it.o_name || it.fname || 'Current item';
      const card = el('div', {class:'rst-cur-card'});
      card.appendChild(el('div', {class:'rst-cur-name', text:name}));
      const btn = el('button', {text:'Restream this ' + modeLabel(cur.mode)});
      btn.addEventListener('click', function(){ startRestream(it, cur.mode, cur.category||{}, name); });
      card.appendChild(btn);
      sec.appendChild(card);
      const note = cur.mode === 'live'
        ? 'Starts one upstream connection for this channel and makes it available to every device on your LAN — they will not use separate provider connections.'
        : 'Starts one upstream connection for this ' + modeLabel(cur.mode) + ' and makes it available to every device on your LAN, the same way live channels work: one shared playback, moving forward from the start. It plays independently of what\u2019s on this screen, and there\u2019s no seeking on the restreamed devices \u2014 same as a live channel.';
      sec.appendChild(el('div', {class:'rst-note', text: note}));
    }
    paneNow.appendChild(sec);
  }

  // -- "Browse" pane --------------------------------------------------------
  let browseLoadedOnce = false;
  let browseMode = 'live';
  const browseModeRow = el('div', {class:'rst-tabs rst-mode-tabs'});
  const modeBtnLive = el('button', {class:'rst-tab active', text:'Live'});
  const modeBtnVod = el('button', {class:'rst-tab', text:'VOD'});
  const modeBtnSeries = el('button', {class:'rst-tab', text:'Series'});
  [modeBtnLive, modeBtnVod, modeBtnSeries].forEach(b=>browseModeRow.appendChild(b));
  paneBrowse.appendChild(browseModeRow);
  function selectBrowseMode(btn, m){
    [modeBtnLive, modeBtnVod, modeBtnSeries].forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    browseMode = m;
    catSelect.innerHTML = '';
    catSelect.appendChild(el('option', {value:'__all__', text:'All ' + (m==='live'?'Channels':(m==='vod'?'VOD':'Series'))}));
    loadCategories();
  }
  modeBtnLive.addEventListener('click', function(){ selectBrowseMode(modeBtnLive, 'live'); });
  modeBtnVod.addEventListener('click', function(){ selectBrowseMode(modeBtnVod, 'vod'); });
  modeBtnSeries.addEventListener('click', function(){ selectBrowseMode(modeBtnSeries, 'series'); });

  const browseTop = el('div', {class:'rst-search-row'});
  const catSelect = el('select');
  catSelect.appendChild(el('option', {value:'__all__', text:'All Channels'}));
  const searchInput = el('input', {type:'text', placeholder:'Search\u2026'});
  browseTop.appendChild(catSelect);
  browseTop.appendChild(searchInput);
  paneBrowse.appendChild(browseTop);
  const browseList = el('ul', {class:'rst-list'});
  paneBrowse.appendChild(browseList);

  let allBrowseItems = [];
  function renderBrowseList(){
    const q = (searchInput.value||'').trim().toLowerCase();
    browseList.innerHTML = '';
    let shown = 0;
    for(const it of allBrowseItems){
      const name = it.name || it.o_name || it.fname || '';
      if(q && name.toLowerCase().indexOf(q) === -1) continue;
      shown++;
      if(shown > 400) break; // keep the modal responsive on very large lists
      const row = el('li', {class:'rst-item'});
      const logoUrl = it.logo || it.stream_icon || '';
      if(logoUrl){
        const img = el('img', {class:'rst-item-logo', src:logoUrl, alt:''});
        img.onerror = function(){ this.style.visibility='hidden'; };
        row.appendChild(img);
      } else {
        row.appendChild(el('div', {class:'rst-item-logo'}));
      }
      row.appendChild(el('div', {class:'rst-item-name', text:name || 'Item'}));
      const btn = el('button', {text:'Restream'});
      btn.addEventListener('click', function(ev){
        ev.stopPropagation();
        startRestream(it, browseMode, currentBrowseCategory(), name);
      });
      row.appendChild(btn);
      browseList.appendChild(row);
    }
    if(shown === 0){
      browseList.appendChild(el('li', {class:'rst-note', text:'Nothing matches.'}));
    }
  }
  function currentBrowseCategory(){
    const id = catSelect.value;
    const title = catSelect.options[catSelect.selectedIndex] ? catSelect.options[catSelect.selectedIndex].text : '';
    return {id: id, title: title};
  }
  searchInput.addEventListener('input', renderBrowseList);

  async function loadCategories(){
    browseLoadedOnce = true;
    browseList.innerHTML = '';
    browseList.appendChild(el('li', {class:'rst-note', text:'Loading categories\u2026'}));
    try{
      const r = await fetch('/api/categories?mode=' + encodeURIComponent(browseMode));
      const d = await r.json();
      if(d.error){ browseList.innerHTML=''; browseList.appendChild(el('li',{class:'rst-note', text:d.error})); return; }
      (d.categories||[]).forEach(function(c){
        catSelect.appendChild(el('option', {value:String(c.id), text:c.title||c.name||String(c.id)}));
      });
      catSelect.value = '__all__';
      await loadItemsForCategory('__all__', catSelect.options[0].text);
    }catch(e){
      browseList.innerHTML = '';
      browseList.appendChild(el('li', {class:'rst-note', text:'Could not load categories: '+e.message}));
    }
  }
  catSelect.addEventListener('change', function(){
    loadItemsForCategory(catSelect.value, currentBrowseCategory().title);
  });
  async function loadItemsForCategory(catId, title){
    browseList.innerHTML = '';
    browseList.appendChild(el('li', {class:'rst-note', text:'Loading\u2026'}));
    try{
      const r = await fetch('/api/items', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({mode:browseMode, category:{id:catId, title:title}, browse:true})});
      const d = await r.json();
      allBrowseItems = d.items || [];
      renderBrowseList();
    }catch(e){
      browseList.innerHTML = '';
      browseList.appendChild(el('li', {class:'rst-note', text:'Could not load: '+e.message}));
    }
  }

  // -- "Active Restreams" pane ---------------------------------------------
  let statusTimer = null;
  async function refreshStatus(){
    try{
      const r = await fetch('/api/restream/status');
      const d = await r.json();
      renderActive(d);
      syncOnAir(d.streams || []);
    }catch(e){ /* transient — next poll will retry */ }
  }
  function fmtRate(kbps){
    if(!kbps || kbps <= 0) return '0 kbps';
    if(kbps >= 1000) return (kbps/1000).toFixed(2) + ' Mbps';
    return Math.round(kbps) + ' kbps';
  }
  function fmtUptime(sec){
    sec = Math.max(0, sec||0);
    if(sec < 60) return sec + 's';
    const m = Math.floor(sec/60), s = sec%60;
    if(m < 60) return m + 'm ' + s + 's';
    const h = Math.floor(m/60), rm = m%60;
    return h + 'h ' + rm + 'm';
  }
  function updateRestreamBadge(count){
    const btn = document.getElementById('restream-btn');
    const badge = document.getElementById('restream-nav-badge');
    if(btn) btn.classList.toggle('active', count > 0);
    if(badge){
      badge.textContent = count > 0 ? String(count) : '';
      badge.classList.toggle('show', count > 0);
    }
  }
  function renderActive(d){
    paneActive.innerHTML = '';
    const streams = (d && d.streams) || [];
    updateRestreamBadge(streams.length);
    if(streams.length === 0){
      paneActive.appendChild(el('div', {class:'rst-note',
        text:'Nothing is currently restreaming. Start one from Now Playing or Browse.'}));
    } else {
      // Aggregate summary across every active stream, any mode — answers
      // "how much upload do I need right now": upstream is what the
      // provider connection costs (unaffected by client count, that's the
      // whole point of restreaming); LAN upload is that figure repeated
      // once per attached device, since each one gets its own copy of the
      // bytes. VOD/series streams are real ffmpeg processes too now, so
      // they count here exactly like live ones do.
      let totalUp = 0, totalLan = 0, totalMb = 0;
      streams.forEach(function(s){
        totalUp += s.kbps || 0;
        totalLan += s.lan_kbps || 0;
        totalMb += s.mb_sent || 0;
      });
      const summary = el('div', {class:'rst-summary'});
      function stat(label, value){
        const it = el('div', {class:'rst-summary-item'});
        it.appendChild(el('b', {text:value}));
        it.appendChild(el('span', {text:label}));
        return it;
      }
      summary.appendChild(stat('Streams', String(streams.length)));
      summary.appendChild(stat('Upstream (provider)', fmtRate(totalUp)));
      summary.appendChild(stat('LAN upload', fmtRate(totalLan)));
      summary.appendChild(stat('Total sent', totalMb.toFixed(0)+' MB'));
      paneActive.appendChild(summary);

      streams.forEach(function(s){
        const row = el('div', {class:'rst-stream-row'});
        const info = el('div', {class:'rst-stream-info'});
        const nameLine = el('div', {class:'rst-stream-name'});
        nameLine.appendChild(el('span', {class:'nm', text:s.name}));
        if(s.mode && s.mode !== 'live'){
          nameLine.appendChild(el('span', {class:'rst-mode-badge', text: s.mode}));
        }
        if(s.state !== 'error'){
          nameLine.appendChild(el('span', {
            class: 'rst-xc-badge ' + (s.transcode_mode==='transcode' ? 'transcode' : 'copy'),
            text: s.transcode_mode==='transcode' ? 'CPU' : 'copy'
          }));
        }
        info.appendChild(nameLine);

        if(s.state === 'error'){
          info.appendChild(el('div', {class:'rst-stream-meta err', text:'Error: ' + (s.error||'unknown')}));
        } else {
          const line1 = s.state + ' \u00b7 ' + s.clients + ' watching \u00b7 up ' + fmtUptime(s.uptime)
            + (s.restarts ? ' \u00b7 ' + s.restarts + ' restart(s)' : '');
          info.appendChild(el('div', {class:'rst-stream-meta', text: line1}));
          let line2 = fmtRate(s.kbps) + ' upstream';
          if(s.clients > 0) line2 += ' \u00b7 ' + fmtRate(s.lan_kbps) + ' to LAN (\u00d7' + s.clients + ')';
          line2 += ' \u00b7 ' + s.mb_sent + ' MB sent';
          if(s.codec_info) line2 += ' \u00b7 ' + s.codec_info;
          info.appendChild(el('div', {class:'rst-stream-meta', text: line2}));
        }
        row.appendChild(info);
        const stopBtn = el('button', {text:'Stop'});
        stopBtn.addEventListener('click', function(){ stopRestream(s.key); });
        row.appendChild(stopBtn);
        paneActive.appendChild(row);
      });
    }
    if(d && d.playlist_url){
      const urlRow = el('div', {class:'rst-url-row'});
      const inp = el('input', {type:'text', readonly:'readonly'});
      inp.value = d.playlist_url;
      const copyBtn = el('button', {text:'Copy playlist URL'});
      copyBtn.addEventListener('click', function(){ copyToClipboard(d.playlist_url); });
      urlRow.appendChild(inp); urlRow.appendChild(copyBtn);
      paneActive.appendChild(urlRow);
      paneActive.appendChild(el('div', {class:'rst-note',
        text:'Add this playlist URL in TiviMate / VLC / Kodi / any IPTV app on a device on this network.'}));
    }
  }
  function copyToClipboard(text){
    try{
      navigator.clipboard.writeText(text).then(function(){ toastSafe('Copied', 'ok'); });
    }catch(e){
      try{
        const ta = document.createElement('textarea'); ta.value = text;
        document.body.appendChild(ta); ta.select(); document.execCommand('copy');
        document.body.removeChild(ta); toastSafe('Copied', 'ok');
      }catch(e2){ toastSafe('Could not copy — select and copy manually', 'wrn'); }
    }
  }

  // -- "Settings" pane -------------------------------------------------------
  async function loadSettings(){
    paneSettings.innerHTML = '';
    const sec = el('div', {class:'rst-sec'});
    sec.appendChild(el('div', {class:'rst-note',
      text:'LAN IP shown in the playlist/stream URLs. Leave on Auto unless you run a VPN client for your provider connection and the detected address looks wrong (e.g. starts with 198.18.).'}));
    const row = el('div', {style:'display:flex;gap:6px'});
    const inp = el('input', {type:'text', placeholder:'auto'});
    const saveBtn = el('button', {text:'Save'});
    row.appendChild(inp); row.appendChild(saveBtn);
    sec.appendChild(row);
    paneSettings.appendChild(sec);
    try{
      const r = await fetch('/api/restream/status');
      const d = await r.json();
      inp.value = d.advertise_ip || '';
    }catch(e){}
    saveBtn.addEventListener('click', async function(){
      try{
        const r = await fetch('/api/restream/set_ip', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ip: inp.value.trim()})});
        const d = await r.json();
        if(d.ok){ toastSafe('Saved', 'ok'); inp.value = d.advertise_ip || ''; }
        else toastSafe(d.error||'Could not save', 'err');
      }catch(e){ toastSafe('Could not save: '+e.message, 'err'); }
    });
  }

  // -- start / stop -----------------------------------------------------------
  function withMainViewerParam(url){
    return url + (url.indexOf('?') === -1 ? '?' : '&') + 'viewer=main';
  }
  async function startRestream(item, mode, category, name){
    toastSafe('Starting restream\u2026', 'info');
    // Explicitly detach from whatever THIS screen was previously watching
    // through, if anything — same reasoning as leave_main_screen()'s
    // docstring: switching a <video> element's source doesn't reliably (or
    // promptly) tell the server the old connection is done.
    if(onAirKey){
      try{
        await fetch('/api/restream/leave_main', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({key: onAirKey})});
      }catch(e){}
    }
    try{
      const r = await fetch('/api/restream/start', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({item:item, mode:mode, category:category||{}, name:name})});
      const d = await r.json();
      if(d.ok){
        toastSafe('Restreaming "'+name+'"', 'ok');
        // The whole point: this app's own playback becomes just another
        // client of the SAME broadcast every LAN device attaches to,
        // instead of holding a second, separate connection alongside it.
        // doPlay() is the same function playItem() itself calls for any
        // resolved URL (see e.g. line ~7343/7533 in the main file) — this
        // is not a special restream-only playback path, just the normal
        // one pointed at the restream's own URL, tagged ?viewer=main so
        // the backend can track and later explicitly detach it.
        // isLive:true because the restream output is always a continuous,
        // non-seekable pipe regardless of the original content's mode —
        // that's what turns off seeking UI the same way it already does
        // for a live channel, with no new player-side logic needed.
        if(typeof doPlay === 'function' && d.stream_url){
          doPlay(withMainViewerParam(d.stream_url), name, {isLive:true});
          showOnAir(d.key, d.stream_url, name);
        }
        closeRestreamModal();
        refreshStatus();
      } else {
        toastSafe(d.error || 'Could not start restream', 'err');
      }
    }catch(e){ toastSafe('Could not start restream: '+e.message, 'err'); }
  }
  async function stopRestream(key){
    try{
      const r = await fetch('/api/restream/stop', {method:'POST',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify({key:key})});
      const d = await r.json();
      if(d.ok){
        if(key === onAirKey) clearOnAir();
        refreshStatus();
      }
      else toastSafe(d.error||'Could not stop', 'err');
    }catch(e){ toastSafe('Could not stop: '+e.message, 'err'); }
  }

  // ---- external hooks for other addons (currently: remote_addon.py) --------
  // Exposes the exact same start/stop/current-item functions this modal's
  // own buttons already call, so a remote-relayed command can trigger a
  // restream of "whatever's playing" identically to tapping Restream here
  // — no duplicate start/stop logic, no second on-air tracker to keep in
  // sync. _restreamStopOnAir() mirrors the on-air indicator's own Stop
  // button (only ever stops the one THIS screen is attached to — a remote
  // command has no other way to know a restream's key anyway).
  window._restreamCurrentItem = currentItem;
  window.startRestream = startRestream;
  window._restreamStopOnAir = function(){
    if(!onAirKey) return null;
    return stopRestream(onAirKey);
  };
  window._restreamOnAirSnapshot = function(){
    return onAirKey ? {key: onAirKey, url: onAirUrl, name: onAirName} : null;
  };

  // ---- open / close ------------------------------------------------------------
  window.openRestreamModal = function(){
    overlay.classList.add('open');
    selectTab(tabNow, paneNow);
    renderNowPlaying();
    refreshStatus();
    if(statusTimer) clearInterval(statusTimer);
    statusTimer = setInterval(refreshStatus, 5000);
  };
  window.closeRestreamModal = function(){
    overlay.classList.remove('open');
    if(statusTimer){ clearInterval(statusTimer); statusTimer = null; }
  };

  document.body.appendChild(overlay);
  document.body.appendChild(onair);
  // Lightly refresh the header button's badge/active state — and the
  // on-air indicator — even when the modal isn't open, since the modal
  // closes automatically the moment a restream starts (see startRestream()).
  // The button itself lives as static HTML in the header (next to Cast) —
  // this addon only owns the modal, the indicator, and these updates.
  setInterval(function(){
    fetch('/api/restream/status').then(r=>r.json())
      .then(function(d){
        const streams = d.streams || [];
        updateRestreamBadge(streams.length);
        syncOnAir(streams);
      })
      .catch(function(){});
  }, 15000);
})();
"""


# ============================================================================
# Flask route registration
# ============================================================================
def register_restream_routes(flask_app, state, run_async, make_client,
                              ffmpeg_path, ffprobe_path=None):
    """
    Parameters
    ----------
    flask_app    : Flask application instance
    state        : shared AppState object
    run_async    : helper that runs a coroutine on the worker event loop
                    (used only from genuine Flask request threads — see the
                    module docstring's "Async dispatch" section)
    make_client  : async context-manager factory returning a portal client
                   (i.e. `_make_client` from the main file)
    ffmpeg_path  : absolute path (or bare name) of the ffmpeg binary
    ffprobe_path : absolute path (or bare name) of the ffprobe binary —
                   optional; codec pre-check is skipped (copy mode only,
                   with the same restart-triggered fallback to transcode)
                   if not given
    """
    if not hasattr(state, "restream_advertise_ip"):
        state.restream_advertise_ip = ""

    manager = RestreamManager(state, run_async, make_client, ffmpeg_path,
                               ffprobe_path or "ffprobe", log_fn=state.log)

    ui_js_bytes = _RESTREAM_UI_JS.encode("utf-8")

    @flask_app.route("/api/restream/ui.js")
    def _restream_ui_js():
        return Response(ui_js_bytes, content_type="application/javascript; charset=utf-8",
                         headers={"Cache-Control": "public, max-age=3600"})

    @flask_app.route("/api/restream/start", methods=["POST"])
    @_require_lan
    def _restream_start():
        data = request.get_json(force=True) or {}
        item = data.get("item") or {}
        mode = data.get("mode") or "live"
        category = data.get("category") or {}
        name = (data.get("name") or item.get("name") or item.get("o_name")
                or item.get("fname") or "Channel")
        logo = data.get("logo") or item.get("logo") or item.get("stream_icon") or ""
        group = (data.get("group") or (category.get("title") if isinstance(category, dict) else "")
                 or "")
        if not state.connected:
            return jsonify({"ok": False, "error": "not connected to a portal"}), 400
        result = manager.start(item=item, mode=mode, category=category,
                                name=name, logo=logo, group=group)
        return jsonify(result), (200 if result.get("ok") else 400)

    @flask_app.route("/api/restream/stop", methods=["POST"])
    @_require_lan
    def _restream_stop():
        data = request.get_json(force=True) or {}
        key = (data.get("key") or "").strip()
        if not key:
            return jsonify({"ok": False, "error": "missing key"}), 400
        return jsonify({"ok": manager.stop_target(key)})

    @flask_app.route("/api/restream/leave_main", methods=["POST"])
    @_require_lan
    def _restream_leave_main():
        """Explicit signal that the app's own playback is switching away
        from this restream (a new restream, or normal playback elsewhere) —
        see StreamHandle.leave_main_screen()'s docstring for why this can't
        just be left to the old HTTP connection closing on its own. Not
        finding one to remove isn't an error — the main screen may not have
        been attached here at all (e.g. it was started from Browse without
        ever being watched on this screen), so this always reports ok."""
        data = request.get_json(force=True) or {}
        key = (data.get("key") or "").strip()
        if not key:
            return jsonify({"ok": False, "error": "missing key"}), 400
        left = manager.leave_main_screen(key)
        return jsonify({"ok": True, "left": left})

    @flask_app.route("/api/restream/status", methods=["GET"])
    @_require_lan
    def _restream_status():
        return jsonify({
            "ok": True,
            "streams": manager.list_active(),
            "playlist_url": manager.playlist_url(),
            "advertise_ip": (getattr(state, "restream_advertise_ip", "") or detect_lan_ip()),
            "max_concurrent": MAX_CONCURRENT_STREAMS,
        })

    @flask_app.route("/api/restream/set_ip", methods=["POST"])
    @_require_lan
    def _restream_set_ip():
        data = request.get_json(force=True) or {}
        ip = (data.get("ip") or "").strip()
        if ip and ip.lower() not in ("auto", "automatic"):
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                return jsonify({"ok": False, "error": "not a valid IP address"}), 400
            state.restream_advertise_ip = ip
        else:
            state.restream_advertise_ip = ""
        return jsonify({"ok": True,
                         "advertise_ip": state.restream_advertise_ip or detect_lan_ip()})

    @flask_app.route("/api/restream/playlist.m3u", methods=["GET"])
    @_require_lan
    def _restream_playlist():
        body = manager.build_playlist_m3u()
        return Response(body, content_type="audio/x-mpegurl; charset=utf-8",
                         headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                                  "Content-Disposition": 'inline; filename="flaskyiptv-restream.m3u"'})

    @flask_app.route("/api/restream/live/<key>.ts", methods=["GET"])
    @_require_lan
    def _restream_live(key):
        addr = request.remote_addr or "?"
        is_main = request.args.get("viewer") == "main"
        handle, client = manager.attach(key, addr, is_main=is_main)
        if handle is None:
            return Response("Unknown or expired restream channel — open the Restream "
                             "panel in FlaskyIPTV and start it again.", status=404)
        if client is None:
            return Response(handle.error or "Upstream failed to start", status=502)

        def _gen():
            reason = "client disconnected"
            try:
                while True:
                    try:
                        chunk = client.q.get(timeout=CLIENT_Q_GET_TIMEOUT)
                    except queue.Empty:
                        # No bytes in 30s is already abnormal while a restream
                        # is actively running (live or vod/series alike).
                        # Keep waiting only while the handle still claims this
                        # client; otherwise stop rather than block this Flask
                        # worker thread forever on a queue nothing will ever
                        # signal again.
                        with handle.lock:
                            still_registered = any(c is client for c in handle.clients.values())
                        if not still_registered or handle.state == "stopped":
                            reason = "stream unavailable"
                            break
                        continue
                    if chunk is _SENTINEL:
                        reason = "stream stopped"
                        break
                    yield chunk
            except GeneratorExit:
                pass
            finally:
                handle.remove_client(client)
                manager.log(f"[restream] {addr} left '{handle.target.name}' ({reason})")

        h = {
            "Content-Type": "video/mp2t",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Connection": "close",
            "Access-Control-Allow-Origin": "*",
        }
        return Response(stream_with_context(_gen()), status=200, headers=h)

    state.log("[restream] LAN restreaming addon ready")
    return manager
