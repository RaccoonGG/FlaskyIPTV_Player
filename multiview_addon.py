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
multiview_addon.py  —  Multi-View stream management for FlaskyIPTV_Player_byGG.py
=========================================================================================

Adds multi-view (picture-in-picture grid) streaming to the Flask IPTV portal.

Design mirrors the Node.js server.js stream management exactly:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  server.js concept              →  this file equivalent             │
  ├─────────────────────────────────────────────────────────────────────┤
  │  activeStreamProcesses (Map)    →  _mv_streams (dict)               │
  │  streamKey format               →  "{client_id}::{channel_url}"     │
  │  references counter             →  StreamBroadcaster.references     │
  │  lastAccess timestamp           →  StreamBroadcaster.last_access    │
  │  STREAM_INACTIVITY_TIMEOUT      →  STREAM_INACTIVITY_TIMEOUT = 30   │
  │  cleanupInactiveStreams()        →  _janitor() thread                │
  │  /stream GET — dedup+ref count  →  GET /api/multiview/stream        │
  │  /api/stream/stop POST          →  POST /api/multiview/stream/stop  │
  │  multiview_layouts SQLite table →  multiview_layouts.json file      │
  └─────────────────────────────────────────────────────────────────────┘

Key design difference vs Node.js:
  Node.js uses readable.pipe(writable) which fans out to multiple writables
  in flowing mode.  Python subprocess.stdout is a single-consumer file object,
  so we use a reader thread that broadcasts chunks to per-client queues.
  Each Flask response generator consumes its own queue.
  Late-joining clients receive only bytes produced after they connected —
  acceptable for live TV (same behaviour as the Node.js app).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRATION  (two small changes to FlaskyIPTV_Player_byGG.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — add import after the cast_addon import block:

    try:
        from multiview_addon import register_multiview_routes
        _MULTIVIEW_AVAILABLE = True
    except ImportError:
        _MULTIVIEW_AVAILABLE = False
        def register_multiview_routes(*a, **kw): pass

STEP 2 — register routes right after the cast_routes registration:

    register_multiview_routes(flask_app)

STEP 3 — add script tag before </body> (loads after the main <script> block):

    <script src="/api/mv/ui.js"></script>

That's it — no other files required.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

# ─────────────────────────────────────────────────────────────────────────────
# Standard library
# ─────────────────────────────────────────────────────────────────────────────
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

from flask import jsonify, request, Response

LOG = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONSTANTS
# All values mirror server.js unless noted.
# ═════════════════════════════════════════════════════════════════════════════

# server.js: const STREAM_INACTIVITY_TIMEOUT = 30000;  (ms → s here)
STREAM_INACTIVITY_TIMEOUT: int = 30

# multiview.js: const MAX_PLAYERS = 9;
MAX_PLAYERS: int = 9

# Internal tuning — not in server.js, chosen for MPEG-TS chunk alignment
_FFMPEG_CHUNK_BYTES: int = 65536       # 64 KB read size from ffmpeg stdout
_CLIENT_QUEUE_MAXSIZE: int = 64        # chunks buffered per client before drop

# Layout persistence — JSON file alongside the Flask app script
LAYOUTS_FILE: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'multiview_layouts.json'
)

# Suppress a new console window on Windows (same flag used in cast_addon.py)
_NO_WINDOW: int = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — FFMPEG RESOLUTION
# Try to import from cast_addon first (avoids duplication).
# Falls back to a local copy of the same logic if cast_addon is not present.
# ═════════════════════════════════════════════════════════════════════════════

try:
    from cast_addon import _get_ffmpeg  # type: ignore
except ImportError:
    def _get_ffmpeg() -> str:
        """Resolve ffmpeg binary path (fallback when cast_addon is absent).
        Result is cached after the first call — shutil.which() runs once only."""
        if _get_ffmpeg._cached is not None:
            return _get_ffmpeg._cached
        res = ''
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            bundled = os.path.join(sys._MEIPASS, 'ffmpeg.exe')
            if os.path.exists(bundled):
                res = bundled
        if not res and getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
            for candidate in (
                os.path.join(base, '_internal', 'ffmpeg.exe'),
                os.path.join(base, 'ffmpeg.exe'),
            ):
                if os.path.exists(candidate):
                    res = candidate
                    break
        if not res and os.path.exists('ffmpeg.exe'):
            res = os.path.abspath('ffmpeg.exe')
        if not res:
            res = shutil.which('ffmpeg') or 'ffmpeg'
        _get_ffmpeg._cached = res
        return res
    _get_ffmpeg._cached = None  # type: ignore[attr-defined]


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — STREAM BROADCASTER
#
# Wraps ONE ffmpeg process and fans its stdout to N per-client queues.
#
# Node.js analogue (server.js):
#   activeStreamInfo.process.stdout.pipe(res)   ← first client
#   activeStreamInfo.references++               ← subsequent clients
#   activeStreamInfo.process.stdout.pipe(res2)  ← pipes same readable again
#
# Python cannot pipe the same stdout to multiple consumers, so a dedicated
# reader thread reads chunks and puts them into every registered client queue.
# ═════════════════════════════════════════════════════════════════════════════

class StreamBroadcaster:
    """
    One ffmpeg process → N HTTP streaming clients via per-client queues.

    Attribute mapping to server.js activeStreamProcesses entry:
        references  → activeStreamInfo.references
        last_access → activeStreamInfo.lastAccess  (epoch seconds, not ms)
        stream_key  → streamKey
        process     → activeStreamInfo.process
    """

    def __init__(self, stream_key: str, channel_url: str,
                 user_agent: str = 'Mozilla/5.0',
                 transcode: bool = False,
                 audio_only: bool = False,
                 audio_url: str = '') -> None:
        self.stream_key:  str   = stream_key
        self.channel_url: str   = channel_url
        self.audio_url:   str   = audio_url   # separate audio stream (e.g. YouTube 720p+)
        self.user_agent:  str   = user_agent
        self.transcode:   bool  = transcode
        self.audio_only:  bool  = audio_only  # True = copy video, re-encode audio only
        self.references:  int   = 0
        self.last_access: float = time.time()
        self._stopped:    bool  = False

        self._lock:          threading.Lock      = threading.Lock()
        self._client_queues: List[queue.Queue]   = []

        self.process: Optional[subprocess.Popen] = self._spawn()

        if self.process:
            # Drain stderr in background so the pipe never blocks ffmpeg
            threading.Thread(
                target=self._drain_stderr,
                daemon=True,
                name=f'mv-stderr-{stream_key[:20]}',
            ).start()
            # Reader thread fans stdout to all client queues
            threading.Thread(
                target=self._read_loop,
                daemon=True,
                name=f'mv-reader-{stream_key[:20]}',
            ).start()
            LOG.info('[MV] Broadcaster started  key=%s  pid=%s',
                     stream_key, self.process.pid)
            if _mv_state:
                _mv_state.log(
                    f'[MV] ✓ Broadcaster started  key={stream_key[:20]}'
                    f'  pid={self.process.pid}')
        else:
            LOG.error('[MV] Broadcaster failed to spawn ffmpeg  key=%s', stream_key)

    # ── ffmpeg process ────────────────────────────────────────────────────────

    def _spawn(self) -> Optional[subprocess.Popen]:
        """
        Spawn ffmpeg with reconnect flags, outputting raw MPEG-TS to stdout.

        When self.transcode is True (HEVC streams), re-encode video to H.264
        so the browser's MSE can decode it via mpegts.js. Audio is kept as
        AAC. Uses ultrafast + zerolatency presets for minimal latency.

        When self.transcode is False, stream-copy at zero cost.
        """
        ffmpeg = _get_ffmpeg()

        if self.transcode:
            codec_args = [
                '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
                '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '48000',
            ]
            LOG.info('[MV] Spawning ffmpeg with HEVC→H.264 transcode  key=%s', self.stream_key)
        elif self.audio_only:
            codec_args = [
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '48000',
            ]
            LOG.info('[MV] Spawning ffmpeg with audio-only transcode  key=%s', self.stream_key)
        else:
            codec_args = ['-c', 'copy']

        ua_args = ['-user_agent', self.user_agent, '-reconnect', '1',
                   '-reconnect_streamed', '1', '-reconnect_delay_max', '5']

        if self.audio_url:
            # Two separate streams (e.g. YouTube 720p+): video + audio inputs
            # Always encode video to H.264 — YouTube separate streams may be VP9/AV1
            LOG.info('[MV] Spawning ffmpeg with merged video+audio (H.264 encode)  key=%s', self.stream_key)
            cmd = [
                ffmpeg, '-hide_banner', '-loglevel', 'error',
            ] + ua_args + ['-i', self.channel_url,
            ] + ua_args + ['-i', self.audio_url,
                '-map', '0:v:0', '-map', '1:a:0',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
                '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '48000',
                '-f', 'mpegts', 'pipe:1',
            ]
        else:
            cmd = [
                ffmpeg, '-hide_banner', '-loglevel', 'error',
            ] + ua_args + ['-i', self.channel_url,
            ] + codec_args + ['-f', 'mpegts', 'pipe:1']
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
            return proc
        except Exception as exc:
            LOG.error('[MV] ffmpeg spawn failed  key=%s  error=%s', self.stream_key, exc)
            return None

    def _drain_stderr(self) -> None:
        """
        Consume and log ffmpeg stderr so the pipe buffer never fills and
        blocks the ffmpeg process.
        Mirrors cast_addon._HLSConverter._log_stderr().
        """
        try:
            for raw_line in self.process.stderr:
                line = raw_line.decode('utf-8', errors='replace').rstrip()
                if line:
                    LOG.debug('[MV][ffmpeg] %s', line)
        except Exception:
            pass

    def _read_loop(self) -> None:
        """
        Read chunks from ffmpeg stdout and distribute to all registered client
        queues.

        Node.js equivalent:
            activeStreamInfo.process.stdout.pipe(res)
            // Node.js Readable emits 'data' to all piped Writables

        Here we explicitly copy each chunk into every client's queue.
        Slow clients whose queue is full have chunks silently dropped —
        the same behaviour as TCP backpressure in the Node.js pipe model.
        """
        try:
            while not self._stopped:
                chunk = self.process.stdout.read(_FFMPEG_CHUNK_BYTES)
                if not chunk:
                    # ffmpeg exited or pipe closed
                    break
                with self._lock:
                    for q in list(self._client_queues):
                        try:
                            q.put_nowait(chunk)
                        except queue.Full:
                            # Drop for this client only — matches Node.js backpressure
                            pass
        except Exception as exc:
            LOG.error('[MV] _read_loop error  key=%s  %s', self.stream_key, exc)
        finally:
            # Signal every waiting client generator that the stream has ended
            with self._lock:
                for q in self._client_queues:
                    try:
                        q.put(None)
                    except Exception:
                        pass
            LOG.info('[MV] _read_loop ended  key=%s', self.stream_key)

    # ── Client queue management ───────────────────────────────────────────────

    def add_client(self) -> queue.Queue:
        """
        Register a new HTTP client and increment the reference counter.

        Node.js equivalent (server.js /stream handler):
            activeStreamInfo.references++;
            activeStreamInfo.lastAccess = Date.now();
            activeStreamInfo.process.stdout.pipe(res);
        """
        q: queue.Queue = queue.Queue(maxsize=_CLIENT_QUEUE_MAXSIZE)
        with self._lock:
            self._client_queues.append(q)
            self.references += 1
            self.last_access = time.time()
        LOG.info('[MV] Client added  key=%s  refs=%d', self.stream_key, self.references)
        return q

    def remove_client(self, q: queue.Queue) -> None:
        """
        Unregister a client and decrement the reference counter.

        Node.js equivalent (server.js req.on('close') handler):
            console.log('[STREAM] Client closed connection...');
            activeStreamInfo.references--;
            activeStreamInfo.lastAccess = Date.now();
            if (activeStreamInfo.references <= 0) {
                console.log('[STREAM] Last client disconnected...');
            }
        """
        with self._lock:
            if q in self._client_queues:
                self._client_queues.remove(q)
            self.references = max(0, self.references - 1)
            self.last_access = time.time()
        LOG.info('[MV] Client removed  key=%s  refs=%d', self.stream_key, self.references)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def stop(self, wait_timeout: float = 3.0) -> None:
        """
        Kill the ffmpeg process, wait for it to fully exit, then signal clients.

        CRITICAL — why we wait:
        process.kill() sends SIGKILL but returns immediately before the OS has
        cleaned up the process's file descriptors and TCP sockets.  If the caller
        (the HTTP stop endpoint) returns *before* the process is dead, the JS
        `await fetch('/stop')` resolves while the IPTV server still sees the old
        TCP connection as active.  The new ffmpeg (started immediately after)
        then creates a second simultaneous connection → provider enforces its
        1-connection limit and kills one of them.

        Calling process.wait(timeout) blocks until the kernel has reaped the
        child, guaranteeing the TCP socket to the IPTV server is fully closed
        before the HTTP response is sent and before the next stream starts.

        Node.js equivalent (server.js cleanupInactiveStreams):
            streamInfo.process.kill('SIGKILL');
            activeStreamProcesses.delete(streamKey);
        Node.js's SIGKILL is also synchronous from the OS perspective — the
        difference is that Node's libuv event loop reaps child processes quickly,
        whereas Python needs an explicit .wait() call.
        """
        self._stopped = True
        pid = self.process.pid if self.process else None
        if self.process:
            try:
                self.process.kill()
                # Block until the OS has fully reaped the child process.
                # timeout=3s guards against the (extremely rare) unkillable process.
                try:
                    self.process.wait(timeout=wait_timeout)
                    LOG.info('[MV] ffmpeg exited  pid=%s  key=%s', pid, self.stream_key)
                except subprocess.TimeoutExpired:
                    LOG.warning('[MV] ffmpeg did not exit within %.1fs  pid=%s  key=%s',
                                wait_timeout, pid, self.stream_key)
            except Exception as exc:
                LOG.warning('[MV] Kill error  key=%s  %s', self.stream_key, exc)
        # Wake up any generator threads still waiting on their queues
        with self._lock:
            for q in self._client_queues:
                try:
                    q.put(None)
                except Exception:
                    pass

    def is_alive(self) -> bool:
        """True if the underlying ffmpeg process is still running."""
        return bool(self.process and self.process.poll() is None)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — STREAM REGISTRY
#
# Mirrors server.js:
#   const activeStreamProcesses = new Map();
#
# Thread-safety note: every read AND write of _mv_streams must hold
# _mv_streams_lock.  The lock is always released before any I/O (Flask
# response streaming runs outside the lock).
# ═════════════════════════════════════════════════════════════════════════════

_mv_streams: Dict[str, StreamBroadcaster] = {}
_mv_streams_lock = threading.Lock()


def _build_stream_key(client_id: str, channel_url: str) -> str:
    """
    Build the deduplication key for the registry.

    Node.js: `${userId}::${streamUrl}::${profileId}`
    Here we omit profileId because multiview always uses stream-copy.
    client_id replaces userId (Flask app has no auth system).
    """
    return f'{client_id}::{channel_url}'


def _get_or_create_broadcaster(stream_key: str, channel_url: str,
                                user_agent: str,
                                transcode: bool = False,
                                audio_only: bool = False,
                                audio_url: str = '') -> Optional[StreamBroadcaster]:
    """
    Return existing broadcaster for stream_key (if alive) or create a new one.
    """
    with _mv_streams_lock:
        existing = _mv_streams.get(stream_key)

        if existing:
            if existing.is_alive():
                LOG.info('[MV] Reusing existing broadcaster  key=%s  refs=%d', stream_key, existing.references)
                return existing
            LOG.warning('[MV] Dead broadcaster found in registry  key=%s  — replacing',
                        stream_key)
            existing.stop()
            del _mv_streams[stream_key]

        broadcaster = StreamBroadcaster(stream_key, channel_url, user_agent,
                                        transcode=transcode, audio_only=audio_only,
                                        audio_url=audio_url)
        if broadcaster.process:
            _mv_streams[stream_key] = broadcaster
            return broadcaster

        return None


def _stop_broadcaster(stream_key: str, force: bool = False) -> str:
    """
    Stop (or keep-alive) a broadcaster, respecting the reference count.

    IMPORTANT: We must release _mv_streams_lock BEFORE calling broadcaster.stop()
    because stop() now calls process.wait() (blocks up to 3 s).  Holding the
    lock during wait() would block every other stream operation for 3 s.

    Node.js equivalent (server.js POST /api/stream/stop):
        if (activeStreamInfo.references > 1) {
            return res.json({ success: true,
                              message: 'Stream kept alive for other active clients.' });
        }
        activeStreamInfo.process.kill('SIGKILL');
        activeStreamProcesses.delete(streamKey);

    Returns one of: 'no_active_stream' | 'kept_alive' | 'stopped'
    """
    # Phase 1: check state and remove from registry — all under lock
    broadcaster_to_stop = None
    with _mv_streams_lock:
        broadcaster = _mv_streams.get(stream_key)

        if not broadcaster:
            return 'no_active_stream'

        if not force and broadcaster.references > 1:
            LOG.info('[MV] Stop requested  key=%s  refs=%d — keeping alive',
                     stream_key, broadcaster.references)
            return 'kept_alive'

        # Remove from registry immediately so new streams for this key can start
        # as soon as stop() unblocks — no double-registration possible.
        del _mv_streams[stream_key]
        broadcaster_to_stop = broadcaster
        LOG.info('[MV] Broadcaster removed from registry  key=%s', stream_key)

    # Phase 2: kill ffmpeg and wait for it to fully exit — outside the lock
    # so other threads are not blocked during process.wait()
    broadcaster_to_stop.stop()
    return 'stopped'


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — JANITOR THREAD
#
# Mirrors server.js:
#   function cleanupInactiveStreams() { ... }
#   setInterval(cleanupInactiveStreams, 60000);
#
# We run every 30 s because STREAM_INACTIVITY_TIMEOUT is also 30 s —
# no point waiting 60 s to catch a 30 s timeout.
# ═════════════════════════════════════════════════════════════════════════════

def _janitor() -> None:
    """
    Background thread that removes stale or dead broadcasters from the registry.

    Node.js equivalent (server.js cleanupInactiveStreams):
        activeStreamProcesses.forEach((streamInfo, streamKey) => {
            if (streamInfo.references <= 0 &&
                (now - streamInfo.lastAccess > STREAM_INACTIVITY_TIMEOUT)) {
                streamInfo.process.kill('SIGKILL');
                activeStreamProcesses.delete(streamKey);
            }
        });
    """
    LOG.info('[MV][JANITOR] Inactive stream cleanup thread started '
             '(timeout=%ds, interval=30s)', STREAM_INACTIVITY_TIMEOUT)
    while True:
        time.sleep(30)
        now = time.time()
        to_stop: List[StreamBroadcaster] = []

        with _mv_streams_lock:
            for key, broadcaster in list(_mv_streams.items()):
                idle_secs = now - broadcaster.last_access

                if broadcaster.references <= 0 and idle_secs > STREAM_INACTIVITY_TIMEOUT:
                    LOG.info('[MV][JANITOR] Stale stream  key=%s  idle=%.1fs  refs=%d',
                             key, idle_secs, broadcaster.references)
                    del _mv_streams[key]
                    to_stop.append(broadcaster)
                elif not broadcaster.is_alive():
                    LOG.info('[MV][JANITOR] Dead ffmpeg process  key=%s  — removing', key)
                    del _mv_streams[key]
                    to_stop.append(broadcaster)

        # Stop outside the lock — stop() blocks during process.wait()
        for broadcaster in to_stop:
            broadcaster.stop()

        if to_stop:
            LOG.info('[MV][JANITOR] Removed %d stale broadcaster(s)', len(to_stop))


# Start the janitor as a daemon thread so it dies when the Flask process exits
_janitor_thread = threading.Thread(
    target=_janitor, daemon=True, name='mv-janitor'
)
_janitor_thread.start()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — LAYOUT PERSISTENCE
#
# Node.js stores layouts in SQLite (multiview_layouts table).
# The Flask app has no database, so we use a JSON file in the same directory.
#
# File schema:
#   { "layouts": [ { "id": <int>, "name": <str>, "layout_data": [...] } ] }
#
# layout_data item schema (mirrors multiview.js saveLayout() exactly):
#   { "x": int, "y": int, "w": int, "h": int,
#     "id": str,           ← widget/placeholder DOM id
#     "channelId": str|null }
# ═════════════════════════════════════════════════════════════════════════════

def _load_layouts() -> List[dict]:
    """
    Load saved layouts from JSON file.
    Mirrors server.js GET /api/multiview/layouts — returns the array directly.
    """
    if not os.path.exists(LAYOUTS_FILE):
        return []
    try:
        with open(LAYOUTS_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data.get('layouts', [])
    except Exception as exc:
        LOG.error('[MV] Failed to load layouts file: %s', exc)
        return []


def _save_layouts(layouts: List[dict]) -> None:
    """Persist the full layouts list back to JSON."""
    try:
        with open(LAYOUTS_FILE, 'w', encoding='utf-8') as fh:
            json.dump({'layouts': layouts}, fh, indent=2, ensure_ascii=False)
    except Exception as exc:
        LOG.error('[MV] Failed to save layouts file: %s', exc)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — ROUTE REGISTRATION
# ═════════════════════════════════════════════════════════════════════════════

# Portal state reference — set by register_multiview_routes so MvBroadcaster
# can write to the activity log without needing state in its constructor.
_mv_state = None


def register_multiview_routes(app, state=None) -> None:
    global _mv_state
    _mv_state = state
    """
    Register all multiview API routes on the Flask app instance.

    Routes added:
        GET  /api/multiview/stream           — stream proxy with dedup
        POST /api/multiview/stream/stop      — reference-aware stop
        GET  /api/multiview/layouts          — list saved layouts
        POST /api/multiview/layouts          — save a layout
        DELETE /api/multiview/layouts/<id>   — delete a layout
        GET  /api/multiview/status           — debug: active stream info
    """

    # ── GET /api/multiview/stream ─────────────────────────────────────────────
    #
    # Core endpoint.  Mirrors server.js GET /stream handler in full:
    #   1. Build stream key from client_id + url
    #   2. If broadcaster exists → reuse (increment refs, pipe to new response)
    #   3. If not → spawn new ffmpeg, store in registry
    #   4. On client disconnect → decrement refs (janitor handles eventual kill)
    #
    # Query params:
    #   url        — the raw IPTV stream URL  (required)
    #   client_id  — UUID from browser localStorage  (required for dedup)
    #   ua         — User-Agent string to pass to ffmpeg  (optional)
    #   transcode  — '1' to re-encode HEVC→H.264 (for HEVC-only channels)
    #   audio_only — '1' to copy video, re-encode audio only (AC3/EAC3/DTS→AAC)
    #
    @app.route('/api/multiview/stream')
    def multiview_stream():
        channel_url = request.args.get('url', '').strip()
        client_id   = request.args.get('client_id', '').strip()
        user_agent  = request.args.get('ua', 'Mozilla/5.0').strip()
        transcode   = request.args.get('transcode', '0') == '1'
        audio_only  = request.args.get('audio_only', '0') == '1' and not transcode
        audio_url   = request.args.get('audio_url', '').strip()

        if not channel_url:
            return 'url parameter is required', 400
        if not client_id:
            return 'client_id parameter is required', 400

        # Include mode in stream key so copy/audio-only/full-transcode streams
        # of the same URL are treated as distinct broadcasters.
        stream_key = _build_stream_key(client_id, channel_url)
        if transcode:
            stream_key += '::transcode'
        elif audio_only:
            stream_key += '::audio_only'
        elif audio_url:
            stream_key += '::merged'

        broadcaster = _get_or_create_broadcaster(stream_key, channel_url, user_agent,
                                                 transcode=transcode,
                                                 audio_only=audio_only,
                                                 audio_url=audio_url)
        if not broadcaster:
            return 'Failed to start ffmpeg stream process', 500

        # Add this HTTP client to the broadcaster's fan-out list.
        # Mirrors server.js: activeStreamInfo.references++;
        client_queue = broadcaster.add_client()

        def generate():
            """
            Generator that yields chunks from this client's queue.

            The try/finally ensures remove_client() is always called when
            the HTTP connection closes, mirroring server.js:
                req.on('close', () => {
                    activeStreamInfo.references--;
                    activeStreamInfo.lastAccess = Date.now();
                    if (activeStreamInfo.references <= 0) {
                        console.log('[STREAM] Last client disconnected...');
                    }
                });
            """
            try:
                while True:
                    try:
                        chunk = client_queue.get(timeout=30)
                    except queue.Empty:
                        # Stream stalled for 30 s — give up
                        LOG.warning('[MV] Client queue timeout  key=%s', stream_key)
                        break
                    if chunk is None:
                        # Broadcaster signalled end-of-stream
                        break
                    yield chunk
            finally:
                # Decrement ref count — mirrors server.js req.on('close')
                broadcaster.remove_client(client_queue)

        return Response(
            generate(),
            mimetype='video/mp2t',
            headers={
                'Cache-Control':      'no-cache, no-store',
                'X-Accel-Buffering':  'no',    # disable nginx read-ahead buffering
                'Access-Control-Allow-Origin': '*',
            },
        )

    # ── POST /api/multiview/stream/stop ──────────────────────────────────────
    #
    # Mirrors server.js POST /api/stream/stop — the critical reference-count
    # check that keeps shared streams alive when multiple widgets use same URL.
    #
    # Body JSON: { "url": str, "client_id": str }
    #
    @app.route('/api/multiview/stream/stop', methods=['POST'])
    def multiview_stream_stop():
        data        = request.get_json(silent=True) or {}
        channel_url = (data.get('url') or '').strip()
        client_id   = (data.get('client_id') or '').strip()

        if not channel_url:
            return jsonify({'error': 'url is required'}), 400

        stream_key = _build_stream_key(client_id, channel_url)
        result     = _stop_broadcaster(stream_key)

        # Response messages mirror server.js POST /api/stream/stop exactly
        if result == 'no_active_stream':
            return jsonify({
                'success': True,
                'message': 'No active stream to stop.',
            })
        if result == 'kept_alive':
            return jsonify({
                'success': True,
                'message': 'Stream kept alive for other active clients.',
            })
        # result == 'stopped'
        return jsonify({
            'success': True,
            'message': f'Stream process terminated for {stream_key}.',
        })

    # ── GET /api/multiview/layouts ────────────────────────────────────────────
    #
    # Mirrors server.js GET /api/multiview/layouts.
    # Returns the flat array of layout objects.
    #
    @app.route('/api/multiview/layouts')
    def multiview_get_layouts():
        return jsonify(_load_layouts())

    # ── POST /api/multiview/layouts ───────────────────────────────────────────
    #
    # Mirrors server.js POST /api/multiview/layouts.
    # Body JSON: { "name": str, "layout_data": list }
    # Returns:   { "success": true, "id": int, "name": str, "layout_data": list }
    #
    @app.route('/api/multiview/layouts', methods=['POST'])
    def multiview_save_layout():
        data        = request.get_json(silent=True) or {}
        name        = (data.get('name') or '').strip()
        layout_data = data.get('layout_data')

        if not name:
            return jsonify({'error': 'name is required'}), 400
        if not layout_data or not isinstance(layout_data, list):
            return jsonify({'error': 'layout_data must be a non-empty list'}), 400

        layouts = _load_layouts()

        # Use millisecond timestamp as ID, matching server.js behaviour where
        # SQLite AUTOINCREMENT lastID is used — timestamp is unique enough here
        new_layout: dict = {
            'id':          int(time.time() * 1000),
            'name':        name,
            'layout_data': layout_data,
        }
        layouts.append(new_layout)
        _save_layouts(layouts)

        LOG.info('[MV] Layout saved  name=%r  id=%s', name, new_layout['id'])

        # Mirror server.js response: res.status(201).json({ success: true, id, name, layout_data })
        return jsonify({'success': True, **new_layout}), 201

    # ── DELETE /api/multiview/layouts/<id> ────────────────────────────────────
    #
    # Mirrors server.js DELETE /api/multiview/layouts/:id.
    #
    @app.route('/api/multiview/layouts/<int:layout_id>', methods=['DELETE'])
    def multiview_delete_layout(layout_id: int):
        layouts     = _load_layouts()
        new_layouts = [lay for lay in layouts if lay.get('id') != layout_id]

        if len(new_layouts) == len(layouts):
            # server.js: res.status(404).json({ error: 'Layout not found or...' })
            return jsonify({'error': 'Layout not found'}), 404

        _save_layouts(new_layouts)
        LOG.info('[MV] Layout deleted  id=%s', layout_id)

        # server.js: res.json({ success: true })
        return jsonify({'success': True})

    # ── GET /api/multiview/status ─────────────────────────────────────────────
    #
    # Debug/introspection endpoint — no direct server.js equivalent, but useful
    # for the Flask app's activity log panel and debugging stale streams.
    #
    @app.route('/api/multiview/status')
    def multiview_status():
        with _mv_streams_lock:
            streams = [
                {
                    'key':        k,
                    'references': b.references,
                    'alive':      b.is_alive(),
                    'pid':        b.process.pid if b.process else None,
                    'idle_secs':  round(time.time() - b.last_access, 1),
                }
                for k, b in _mv_streams.items()
            ]
        return jsonify({
            'active_streams': streams,
            'count':          len(streams),
        })

    # ── POST /api/multiview/resolve_url ───────────────────────────────────────
    #
    # Resolves a user-supplied URL (YouTube, Twitch, Dailymotion, Vimeo, or any
    # generic web video URL) to a direct streamable URL using yt-dlp.
    # Falls back gracefully when yt-dlp is unavailable: returns the original URL
    # so mpegts.js / ffmpeg can attempt direct playback (works for plain .m3u8 /
    # .ts / direct-stream URLs without needing yt-dlp at all).
    #
    # Body JSON:  { "url": str }
    # Response:   { "url": str, "title": str, "is_live": bool, "via": str }
    #             or { "error": str } on failure
    #
    @app.route('/api/multiview/resolve_url', methods=['POST'])
    def multiview_resolve_url():
        data    = request.get_json(silent=True) or {}
        raw_url = (data.get('url') or '').strip()
        # quality: 'best' | '1080' | '720' | '480' | '360'
        quality = (data.get('quality') or 'best').strip()

        if not raw_url:
            return jsonify({'error': 'url is required'}), 400

        # ── Build yt-dlp format selector from quality hint ────────────────────
        def _fmt_selector(q: str) -> str:
            """Translate a simple quality label to a yt-dlp format string.
            Force H.264 video (vcodec:h264) so the browser can decode it.
            YouTube's higher qualities often use VP9/AV1 which mpegts.js can't play.
            """
            if q in ('best', '', None):
                return (
                    'bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]'
                    '/bestvideo[vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]'
                    '/best[ext=mp4]/best'
                )
            try:
                h = int(q)
            except ValueError:
                return 'bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
            return (
                f'bestvideo[height<={h}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]'
                f'/bestvideo[height<={h}][vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]'
                f'/best[height<={h}][ext=mp4]'
                f'/best[height<={h}]'
                f'/best'
            )

        # ── Attempt yt-dlp resolution ─────────────────────────────────────────
        try:
            import yt_dlp  # type: ignore
        except ImportError:
            # yt-dlp not installed — return the URL as-is for direct playback
            LOG.info('[MV][resolve_url] yt-dlp not available, returning raw URL')
            return jsonify({
                'url':     raw_url,
                'title':   '',
                'is_live': False,
                'via':     'direct',
            })

        try:
            ydl_opts = {
                'quiet':            True,
                'no_warnings':      True,
                'skip_download':    True,
                'no_cache_dir':     True,   # force fresh URLs, not cached signed URLs
                # Format selector respects user's quality choice.
                # For live streams the HLS lookup below takes precedence anyway.
                'format':           _fmt_selector(quality),
                # Hard timeout so the endpoint never hangs indefinitely
                'socket_timeout':   15,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(raw_url, download=False)

            if not info:
                return jsonify({'error': 'yt-dlp returned no info'}), 502

            title   = info.get('title') or info.get('id') or ''
            is_live = bool(info.get('is_live'))

            # Prefer an HLS manifest for live streams (mpegts.js handles it natively)
            # then fall back to the best direct URL.
            resolved       = None
            resolved_audio = None
            formats        = info.get('formats') or []

            if is_live:
                # For live streams, select an HLS manifest that matches the
                # requested height. yt-dlp exposes per-quality HLS URLs in
                # the formats list — iterate highest-first and pick the best
                # one that fits within the requested height cap.
                try:
                    h_cap = int(quality) if quality not in ('best', '', None) else 99999
                except (ValueError, TypeError):
                    h_cap = 99999

                def _hls_formats(fmts):
                    """All HLS formats sorted best (highest height) first."""
                    return sorted(
                        [f for f in fmts
                         if f.get('protocol') in ('m3u8', 'm3u8_native') and f.get('url')],
                        key=lambda f: f.get('height') or 0,
                        reverse=True,
                    )

                hls_fmts = _hls_formats(formats)
                # Pick the best HLS that fits within h_cap
                hls_picked = next(
                    (f for f in hls_fmts if (f.get('height') or 99999) <= h_cap),
                    hls_fmts[0] if hls_fmts else None,   # fallback: best available
                )
                hls = hls_picked.get('url') if hls_picked else None

                # Last resort: info.get('url') may itself be an HLS manifest
                resolved = hls or info.get('url') or (formats[-1].get('url') if formats else None)
                actual_h = hls_picked.get('height') if hls_picked else None
            else:
                # For VOD: extract video URL and optional separate audio URL
                video_url = None
                audio_url_out = None
                actual_h = None

                # requested_formats: separate video+audio (merged format selection)
                req_fmts = info.get('requested_formats') or []
                if len(req_fmts) >= 2:
                    for rf in req_fmts:
                        vcodec = rf.get('vcodec', 'none')
                        acodec = rf.get('acodec', 'none')
                        if vcodec != 'none' and not video_url:
                            video_url = rf.get('url')
                            actual_h  = rf.get('height')
                        elif acodec != 'none' and not audio_url_out:
                            audio_url_out = rf.get('url')
                elif req_fmts:
                    video_url = req_fmts[0].get('url')
                    actual_h  = req_fmts[0].get('height')

                # requested_downloads: pre-muxed single file
                if not video_url:
                    req_dl = info.get('requested_downloads') or []
                    if req_dl and req_dl[0].get('url'):
                        video_url = req_dl[0]['url']
                        actual_h  = req_dl[0].get('height') or info.get('height')

                # Direct url field (pre-muxed)
                if not video_url:
                    video_url = info.get('url')
                    actual_h  = info.get('height')

                # Last resort: best format from the formats list
                if not video_url and formats:
                    by_height = sorted(
                        [f for f in formats if f.get('url') and f.get('vcodec','none')!='none'],
                        key=lambda f: f.get('height') or 0, reverse=True
                    )
                    if by_height:
                        video_url = by_height[0]['url']
                        actual_h  = by_height[0].get('height')

                resolved      = video_url
                resolved_audio = audio_url_out

            if not resolved:
                return jsonify({'error': 'yt-dlp could not extract a stream URL'}), 502

            duration_secs = None if is_live else info.get('duration')

            LOG.info('[MV][resolve_url] resolved  title=%r  live=%s  quality=%s  height=%s  merged=%s  via=yt-dlp',
                     title, is_live, quality, actual_h, 'yes' if (not is_live and resolved_audio) else 'no')
            resp = {
                'url':      resolved,
                'title':    title,
                'is_live':  is_live,
                'quality':  quality,
                'height':   actual_h,
                'duration': duration_secs,
                'via':      'yt-dlp',
            }
            # Include separate audio URL for merged formats (e.g. YouTube 720p+)
            if not is_live and resolved_audio:
                resp['audio_url'] = resolved_audio
            return jsonify(resp)

        except Exception as exc:
            LOG.error('[MV][resolve_url] yt-dlp error: %s', exc)
            return jsonify({'error': str(exc)}), 502

    _register_mv_ui_route(app)
    LOG.info('[MV] Multiview routes registered  '
             '(layouts_file=%s, ui.js=yes)', LAYOUTS_FILE)
    if state:
        state.log('[MV] Routes registered: /api/multiview/stream  '
                  '/api/multiview/stream/stop  /api/multiview/layouts  '
                  '/api/multiview/status  /api/mv/ui.js')


# ─────────────────────────────────────────────────────────────────────────────
# Frontend  (served as /api/mv/ui.js)
# ─────────────────────────────────────────────────────────────────────────────

_MV_UI_JS_BYTES: bytes = b""   # filled in register_multiview_routes


def _register_mv_ui_route(app) -> None:
    """Add the /api/mv/ui.js route and pre-encode the JS once."""
    global _MV_UI_JS_BYTES
    _MV_UI_JS_BYTES = _MV_UI_JS.encode("utf-8")

    @app.route("/api/mv/ui.js")
    def mv_ui_js():
        return Response(
            _MV_UI_JS_BYTES,
            content_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )


_MV_UI_JS = r"""
/* ── Inject CSS ─────────────────────────────────────────────────────── */
(function(){
  const s = document.createElement('style');
  s.textContent = `
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
/* Seek track — three-zone gradient (played / buffered / empty) via CSS vars.
   --mv-played and --mv-buffered are set in JS so the gradient lives on the
   track pseudo-element itself — nothing can cover it. */
.mv-seek-track{flex:1;min-width:0;display:flex;align-items:center;height:14px}
.mv-seek{
  -webkit-appearance:none;appearance:none;
  width:100%;height:14px;margin:0;cursor:pointer;min-width:0;
  background:transparent;
  --mv-played:0%;--mv-buffered:0%;
}
/* Three-zone gradient: accent=played · semi-white=buffered · dim=unbuffered */
.mv-seek::-webkit-slider-runnable-track{
  height:3px;border-radius:2px;
  background:linear-gradient(to right,
    var(--acc) 0%,               var(--acc) var(--mv-played),
    rgba(255,255,255,.4) var(--mv-played), rgba(255,255,255,.4) var(--mv-buffered),
    rgba(255,255,255,.13) var(--mv-buffered), rgba(255,255,255,.13) 100%);
}
.mv-seek::-moz-range-track{
  height:3px;border-radius:2px;
  background:linear-gradient(to right,
    var(--acc) 0%,               var(--acc) var(--mv-played),
    rgba(255,255,255,.4) var(--mv-played), rgba(255,255,255,.4) var(--mv-buffered),
    rgba(255,255,255,.13) var(--mv-buffered), rgba(255,255,255,.13) 100%);
}
.mv-seek::-webkit-slider-thumb{
  -webkit-appearance:none;width:10px;height:10px;border-radius:50%;
  background:var(--acc);margin-top:-3.5px;box-shadow:0 0 3px rgba(0,0,0,.6);cursor:pointer}
.mv-seek::-moz-range-thumb{width:10px;height:10px;border-radius:50%;background:var(--acc);border:none;cursor:pointer}
.mv-seek-time{font-size:9px;color:rgba(255,255,255,.75);white-space:nowrap;flex-shrink:0;font-variant-numeric:tabular-nums}
/* MV bottom action bar — sits just above the seek bar */
.mv-bottom-bar{
  position:absolute;bottom:22px;left:0;right:0;z-index:6;
  display:none;align-items:center;gap:4px;padding:3px 5px;
  background:linear-gradient(transparent,rgba(0,0,0,.5));
}
.mv-widget-content:hover .mv-bottom-bar.mv-bb-visible{display:flex}
.mv-bb-btn{
  height:20px;padding:0 7px;font-size:9px;font-weight:700;
  border-radius:3px;border:1px solid rgba(255,255,255,.2);
  background:rgba(0,0,0,.55);color:rgba(255,255,255,.9);
  cursor:pointer;white-space:nowrap;flex-shrink:0;
}
.mv-bb-btn:hover{background:rgba(255,255,255,.12)}
.mv-rec-btn{color:#f87171;border-color:rgba(248,113,113,.4)}
.mv-rec-btn.mv-recording{color:#f87171;animation:dvr-pulse 1.4s ease infinite}
.mv-widget-content.mv-tiny .mv-bottom-bar{display:none!important}
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
.mv-item-ctx button{display:flex;align-items:center;justify-content:flex-start;gap:8px;width:100%;padding:7px 14px;
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
`;
  document.head.appendChild(s);
})();

/* ── Inject HTML ────────────────────────────────────────────────────── */
(function(){
  const d = document.createElement('div');
  d.innerHTML = `
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
`;
  while(d.firstChild) document.body.appendChild(d.firstChild);
})();

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
  if(!cEl){ toast('Multiview: slot not found (wid='+wid+')', 'err'); return; }
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
      isLive      = d.is_live !== false;
      if(d.duration) cEl._mvKnownDuration = d.duration;
      // video_proxy only works for pre-muxed formats (single URL has both video+audio).
      // Merged formats (separate video+audio) need the multiview ffmpeg proxy to merge them.
      cEl._mvUseVideoProxy = (!isLive && d.via === 'yt-dlp' && !d.audio_url);
      // For merged formats, pass audio_url to the multiview proxy
      cEl._mvAudioUrl = d.audio_url || '';
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
          <div class="mv-seek-track">
            <input type="range" class="mv-seek" min="0" max="100" step="0.1" value="0">
          </div>
          <span class="mv-seek-time">0:00</span>
        </div>
        <div class="mv-bottom-bar">
          <button class="mv-rec-btn mv-bb-btn" title="Quick Record">⏺ Record</button>
          <button class="mv-mkv-btn mv-bb-btn" title="Download MKV">⬇ MKV</button>
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

  // Update the three-zone gradient on the track pseudoelement via CSS variables.
  // --mv-played  → where accent colour ends (current position)
  // --mv-buffered → where the lighter "buffered" zone ends
  // Both live on seekBar itself so they cascade into ::-webkit-slider-runnable-track.
  const _syncBuf = (playedPct, dur) => {
    seekBar.style.setProperty('--mv-played', playedPct.toFixed(1) + '%');
    const buf = videoEl.buffered;
    if(!buf || !buf.length || !(dur > 0)){
      seekBar.style.setProperty('--mv-buffered', playedPct.toFixed(1) + '%');
      return;
    }
    let bufEnd = videoEl.currentTime;
    for(let i = 0; i < buf.length; i++){
      if(buf.start(i) <= videoEl.currentTime + 0.5)
        bufEnd = Math.max(bufEnd, buf.end(i));
    }
    const bufPct = Math.min(100, (bufEnd / dur) * 100);
    seekBar.style.setProperty('--mv-buffered', bufPct.toFixed(1) + '%');
  };

  const _syncSeek = () => {
    // Use video element duration if finite, or fall back to known duration from yt-dlp
    const dur = (isFinite(videoEl.duration) && videoEl.duration > 0)
      ? videoEl.duration
      : (cEl._mvKnownDuration || 0);

    if(dur <= 0){
      if(cEl._mvIsLive === false){
        seekBar.style.display = 'none';
        seekTime.textContent  = videoEl.currentTime > 0
          ? _fmtTime(videoEl.currentTime) : '▶ VOD';
        seekTime.style.color  = '';
      } else {
        seekBar.style.display = 'none';
        seekTime.textContent  = '● LIVE';
        seekTime.style.color  = '#f87171';
      }
      return;
    }
    // We have a duration — show full seek bar
    seekBar.style.display = '';
    seekTime.style.color  = '';
    const playedPct = (videoEl.currentTime / dur) * 100;
    seekBar.value = playedPct;
    seekTime.textContent = _fmtTime(videoEl.currentTime) + ' / ' + _fmtTime(dur);
    _syncBuf(playedPct, dur);
  };
  const _tryShowSeek = () => {
    const isExt  = mvExternalUrlWidgets.has(wid);
    const hasVod = isFinite(videoEl.duration) && videoEl.duration > 0 && videoEl.duration < 86400;
    const isKnownVod = cEl._mvIsLive === false;
    if(isExt || hasVod || isKnownVod){
      seekWrap.classList.add('mv-seek-visible');
      _syncSeek();
    }
  };

  // Show immediately if already external or known VOD
  if(mvExternalUrlWidgets.has(wid) || cEl._mvIsLive === false){
    seekWrap.classList.add('mv-seek-visible');
    seekBar.style.display = 'none';
    if(cEl._mvIsLive === false){
      seekTime.textContent = '▶ VOD';
      seekTime.style.color = '';
    } else {
      seekTime.textContent = '● LIVE';
      seekTime.style.color = '#f87171';
    }
  }

  videoEl.addEventListener('loadedmetadata', _tryShowSeek);
  videoEl.addEventListener('durationchange', _tryShowSeek);
  videoEl.addEventListener('timeupdate', () => {
    _tryShowSeek();
    if(seekWrap.classList.contains('mv-seek-visible')) _syncSeek();
  });
  // 'progress' fires as the MSE/browser buffer grows — refresh the buffered zone
  videoEl.addEventListener('progress', () => {
    const dur = (isFinite(videoEl.duration) && videoEl.duration > 0)
      ? videoEl.duration : (cEl._mvKnownDuration || 0);
    if(dur > 0 && seekWrap.classList.contains('mv-seek-visible')){
      const pp = (videoEl.currentTime / dur) * 100;
      _syncBuf(pp, dur);
    }
  });
  videoEl.addEventListener('emptied', () => {
    if(!mvExternalUrlWidgets.has(wid)) seekWrap.classList.remove('mv-seek-visible');
    seekBar.style.display = '';
    seekBar.style.setProperty('--mv-played',   '0%');
    seekBar.style.setProperty('--mv-buffered', '0%');
    // If this cell is known to be VOD, restore the seek bar
    if(cEl._mvIsLive === false){
      seekWrap.classList.add('mv-seek-visible');
    }
  });

  seekBar.addEventListener('click', e => e.stopPropagation());
  seekBar.addEventListener('mousedown', e => e.stopPropagation());
  seekBar.addEventListener('touchstart', e => e.stopPropagation(), {passive:true});
  seekBar.addEventListener('input', e => {
    e.stopPropagation();
    const dur = (isFinite(videoEl.duration) && videoEl.duration > 0)
      ? videoEl.duration : (cEl._mvKnownDuration || 0);
    if(dur > 0)
      videoEl.currentTime = (parseFloat(e.target.value)/100) * dur;
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
      _mvPlayFromUrl(wid, rawUrl, cEl);
    });
    // Start hidden — _mvPlayChannel shows it for external/YouTube URLs
    qualSel.style.display = 'none';
  }

  // Click anywhere on widget → make it the active player
  cEl.addEventListener('click', ()=> _mvSetActive(wid));

  // ── Bottom bar: Record + MKV ──────────────────────────────────────────────
  const recBtn  = cEl.querySelector('.mv-rec-btn');
  const mkvBtn  = cEl.querySelector('.mv-mkv-btn');
  const bottomBar = cEl.querySelector('.mv-bottom-bar');

  if(recBtn) recBtn.addEventListener('click', async e=>{
    e.stopPropagation();
    const ch = cEl._mvChannel;
    const name = ch ? (ch.name||ch.o_name||'Recording') : 'Recording';
    const rawUrl = mvUrls.get(wid) || '';
    if(!rawUrl){ toast('No stream loaded','wrn'); return; }
    // Strip proxy wrapper so ffmpeg gets the real portal URL
    let url = rawUrl;
    if(url.includes('/api/hls_proxy')){
      try{ const p=new URLSearchParams(url.split('?')[1]||''); url=p.get('url')||url; }catch(e_){}
    }
    if(recBtn.classList.contains('mv-recording')){
      // Stop
      await fetch('/api/record/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      recBtn.classList.remove('mv-recording');
      recBtn.textContent='⏺ Record';
      toast('Recording stopped','ok');
    } else {
      // Start
      const od = document.getElementById('o-dir')?.value?.trim()||'';
      const r2 = await fetch('/api/record/start',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url, name, out_dir:od})});
      const d2 = await r2.json();
      if(d2.ok){
        recBtn.classList.add('mv-recording');
        recBtn.textContent='⏹ Stop';
        toast('⏺ Recording: '+name,'ok');
      } else toast(d2.error||'Record failed','err');
    }
  });

  if(mkvBtn) mkvBtn.addEventListener('click', async e=>{
    e.stopPropagation();
    const ch = cEl._mvChannel;
    if(!ch){ toast('No stream loaded','wrn'); return; }
    const od = document.getElementById('o-dir')?.value?.trim();
    if(!od){ toast('Set output folder in ⚙ settings first','wrn'); return; }
    toast('Starting MKV download…','info');
    try{
      const r = await fetch('/api/download/mkv',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({items:[ch], mode:'vod', category:{}, out_dir:od})});
      const d = await r.json();
      if(d.error) toast(d.error,'err');
      else toast('MKV download started','ok');
    }catch(e_){ toast('Download error: '+e_,'err'); }
  });
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
      // Use the mode the item was picked from (tagged in _mvSelPickItem).
      // Fall back to heuristics only if the tag is missing (e.g. saved layouts).
      const _mvResolveMode = channel._mvMode
        || ((channel.tvg_type==='series'||channel._is_show_item) ? (channel.tvg_type||'series')
            : channel._direct_url ? 'vod' : 'live');
      const _mvResolveCat  = channel._mvCat || curCat || {};
      const r = await fetch('/api/resolve?mv=1', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({item: channel, mode: _mvResolveMode, category: _mvResolveCat})
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

  // Store channel + live flag on the element so bottom-bar buttons can access them
  cEl._mvChannel = channel;
  // Determine live/VOD: use the mode tag set at pick time (most reliable),
  // then channel._is_live (set by yt-dlp for external URLs),
  // then fallback to live for portal channels without a mode tag.
  const _modeTag = channel._mvMode || '';
  cEl._mvIsLive  = _modeTag === 'vod' || _modeTag === 'series'
    ? false   // VOD/Series are always non-live
    : (channel._is_live !== false) && !mvExternalUrlWidgets.has(wid)
        ? true  // portal live channel
        : (channel._is_live !== false);  // external URL: respect yt-dlp flag

  // Update bottom bar visibility: Record=live only, MKV=VOD/non-live only
  const _bb     = cEl.querySelector('.mv-bottom-bar');
  const _recBtn = cEl.querySelector('.mv-rec-btn');
  const _mkvBtn = cEl.querySelector('.mv-mkv-btn');
  const _isLive = cEl._mvIsLive;
  if(_recBtn) _recBtn.style.display = _isLive ? '' : 'none';
  if(_mkvBtn) _mkvBtn.style.display = !_isLive ? '' : 'none';
  if(_bb){
    _bb.classList.toggle('mv-bb-visible', true);
    if(_recBtn){ _recBtn.classList.remove('mv-recording'); _recBtn.textContent='⏺ Record'; }
  }

  // Show/hide quality selector — only relevant for YouTube/external URLs
  const _qs = cEl.querySelector('.mv-quality-sel');
  if(_qs) _qs.style.display = mvExternalUrlWidgets.has(wid) ? '' : 'none';

  // Store the RAW channel URL (not the proxy URL) so _mvStopCleanup can
  // send the right URL to /api/multiview/stream/stop for key matching.
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

  // ── YouTube/yt-dlp VOD: play via video_proxy for range-based seeking ────────
  // The multiview ffmpeg proxy streams linearly — seek jumps are impossible.
  // For yt-dlp VOD content, use /api/video_proxy which forwards Range headers,
  // enabling the browser to seek by requesting byte ranges from YouTube directly.
  // The video proxy also fixes quality — each resolve gets a fresh URL at the
  // requested quality, and the browser plays it natively.
  if(cEl._mvUseVideoProxy && !channel._is_live){
    const proxyVideoUrl = '/api/video_proxy?url=' + encodeURIComponent(resolvedUrl);
    const dur = cEl._mvKnownDuration || 0;

    if(typeof Hls !== 'undefined' && (resolvedUrl.includes('.m3u8') || resolvedUrl.includes('m3u8'))){
      // HLS manifest (YouTube live at specific quality)
      const hlsInst = new Hls({ enableWorker:true, maxBufferLength:30, maxMaxBufferLength:40, backBufferLength:10,
        liveSyncDurationCount:3, liveMaxLatencyDurationCount:5 });
      hlsInst.loadSource(proxyVideoUrl);
      hlsInst.attachMedia(videoEl);
      hlsInst.on(Hls.Events.MANIFEST_PARSED, ()=>{ videoEl.play().catch(()=>{}); });
      hlsInst.on(Hls.Events.ERROR, (_,d)=>{ if(d.fatal){ toast('Stream error','err'); _mvStopCleanup(wid,true); }});
      mvPlayers.set(wid, {pause:()=>videoEl.pause(), unload:()=>hlsInst.stopLoad(),
        detachMediaElement:()=>hlsInst.detachMedia(), destroy:()=>hlsInst.destroy()});
    } else {
      // Direct mp4 — use native <video> element with the proxy URL
      // Native video supports range-based seeking natively
      videoEl.src = proxyVideoUrl;
      videoEl.play().catch(()=>{});
      mvPlayers.set(wid, {pause:()=>videoEl.pause(), unload:()=>{},
        detachMediaElement:()=>{ videoEl.pause(); videoEl.removeAttribute('src'); },
        destroy:()=>{ videoEl.pause(); videoEl.removeAttribute('src'); }});
    }
    const muteBtn3 = cEl.querySelector('.mv-mute-btn');
    if(mvPlayers.size <= 1){ videoEl.muted=false; if(muteBtn3) muteBtn3.textContent='🔊'; }
    // Show seek bar
    const seekW3 = cEl.querySelector('.mv-seek-wrap');
    if(seekW3) seekW3.classList.add('mv-seek-visible');
    if(dur) cEl._mvKnownDuration = dur;
    return;
  }

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
    // Determine if this is live or VOD using the mode tag (set at pick time)
    const _modeTag2 = channel._mvMode || '';
    const _hlsIsLive = _modeTag2 === 'vod' || _modeTag2 === 'series'
      ? false
      : channel._is_live !== false
          && !channel._direct_url
          && !mvExternalUrlWidgets.has(wid)
          && channel.tvg_type !== 'movie'
          && channel.tvg_type !== 'series'
          && !(resolvedUrl.includes('vod=1'));

    const player = mpegts.createPlayer({
      type:   'mse',
      isLive: _hlsIsLive,
      url:    resolvedUrl,
    }, {
      enableStashBuffer:      true,
      stashInitialSize:       _hlsIsLive ? 4096 : 128 * 1024 * 1024,
      autoCleanupSourceBuffer: !_hlsIsLive,
      lazyLoad:               false,
      seekType:               _hlsIsLive ? 'range' : 'range',
      liveBufferLatencyChasing:   _hlsIsLive,
      liveBufferLatencyMaxLatency: _hlsIsLive ? 8 : undefined,
      liveBufferLatencyMinRemain:  _hlsIsLive ? 2 : undefined,
    });
    player.on(mpegts.Events.ERROR, (errType, errDetail)=>{
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
    } catch(e){ if(e && e.name !== 'AbortError') toast('Playback error: '+(e.message||e), 'err'); }
    // For VOD: show seek bar once duration is known
    if(!_hlsIsLive){
      const seekWrap2 = cEl.querySelector('.mv-seek-wrap');
      if(seekWrap2) seekWrap2.classList.add('mv-seek-visible');
    }
    return;
  }

  // ── Build the proxy stream URL ─────────────────────────────────────────────
  // mirrors server.js /stream GET handler stream key:
  //   streamKey = `${userId}::${streamUrl}::${profileId}`
  // We route through multiview_addon.py /api/multiview/stream which handles
  // dedup and reference counting server-side.
  // Pass the session's effective User-Agent so ffmpeg identifies to the IPTV
  // server with the same UA that all other session requests use.
  const _mvUa = (window._mvEffectiveUa || 'VLC/3.0.0 LibVLC/3.0.0');
  const proxyUrl = '/api/multiview/stream?'
    + 'url='        + encodeURIComponent(resolvedUrl)
    + '&client_id=' + encodeURIComponent(mvClientId)
    + '&ua='        + encodeURIComponent(_mvUa)
    + (channel._mv_transcode ? '&transcode=1' : '')
    + (cEl._mvAudioUrl ? '&audio_url=' + encodeURIComponent(cEl._mvAudioUrl) : '');

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
    // Only toast if the panel is still open (don't spam after mvClose)
    if(document.getElementById('p-mv').classList.contains('mv-active'))
      toast('Stream error: ' + (channel.name||wid), 'err');
    _mvStopCleanup(wid, true);
  });

  // mirrors multiview.js: players.set(widgetId, player)
  mvPlayers.set(wid, player);
  player.attachMediaElement(videoEl);
  player.load();
  // Re-add seek bar for VOD after load (the emptied event fired during load removes it)
  if(!cEl._mvIsLive){
    const _sw = cEl.querySelector('.mv-seek-wrap');
    if(_sw) _sw.classList.add('mv-seek-visible');
  }

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
    `<button onclick="${a.fn}"><span style="width:18px;text-align:center;flex-shrink:0;font-size:13px">${a.icon}</span>${esc(a.label)}</button>`
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
function _mvSelHideItem(i){
  let it;
  if(_mvSelNavMode==='episodes'){
    it=(_mvSelEpisodesFiltered.length?_mvSelEpisodesFiltered:_mvSelEpisodes)[i];
  } else {
    it=(_mvSelFilteredItems.length?_mvSelFilteredItems:_mvSelItems)[i];
  }
  if(!it) return;
  const name=it.name||it.o_name||it.title||'';
  _hideItems([it],_mvSelContentMode);
  // Remove from in-memory list so re-render is instant without a network trip
  if(_mvSelNavMode==='episodes'){
    _mvSelEpisodes=_mvSelEpisodes.filter(e=>(e.name||e.title||'')!==name);
  } else {
    _mvSelItems=_mvSelItems.filter(e=>(e.name||e.o_name||e.title||'')!==name);
  }
  _mvRenderSel();
  _updateHiddenCount();
  toast('🚫 Hidden: '+name,'info');
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
    // Directly playable — catchup (live only, where supported) then play button
    const _mvIsCatchup = _mvSelContentMode === 'live' && _channelSupportsCatchup(it);
    if(_mvIsCatchup){
      btns += `<button class="btn-ghost" style="height:24px;padding:0 6px;font-size:13px" onclick="event.stopPropagation();_mvOpenCatchupForItem(${i})" title="Catch-up TV">↺</button>`;
    }
    btns += `<button class="btn-blue" style="height:24px;padding:0 8px;font-size:11px" onclick="event.stopPropagation();_mvSelPickItem(${i})" title="Play in Multi-View">▶</button>`;
  }
  // Submenu ⋮ — only for multiview widget context (not DVR channel picker)
  if(_mvSelWidgetCtx){
    btns += `<button class="btn-ghost" style="padding:0 5px;font-size:16px;line-height:1" onclick="event.stopPropagation();_mvSelOpenItemMenu(${i},this)" title="More options">⋮</button>`;
  }

  const drillArrow = (isShow||isGroup) ? `<span style="color:var(--txt3);font-size:14px;flex-shrink:0">›</span>` : '';

  return `<div class="mv-ch-row" data-ii="${i}" data-show="${isShow||isGroup?1:0}">
    ${logoHtml}
    <span class="mv-ch-name"><span class="iname-inner">${esc(name)}</span></span>
    <div class="mv-item-btns">${btns}</div>
    ${drillArrow}
  </div>`;
}

// Open catchup overlay for an MV selector item (live channels only)
// Stores the widget context so doPlayArchiveCmd can route back to MV instead of main player.
let _mvCatchupCtx = null; // { wid, cEl } set when catchup opened from MV; null for main player
function _mvOpenCatchupForItem(i){
  let it;
  if(_mvSelNavMode === 'episodes'){
    it = (_mvSelEpisodesFiltered.length ? _mvSelEpisodesFiltered : _mvSelEpisodes)[i];
  } else {
    it = (_mvSelFilteredItems.length ? _mvSelFilteredItems : _mvSelItems)[i];
  }
  if(!it) return;
  if(!_channelSupportsCatchup(it)){toast('This channel does not support Catch-up TV','wrn');return;}
  _mvCloseCtxMenu();
  document.getElementById('mv-sel-overlay').classList.remove('open');
  // Save the widget context so the play resolution routes back to this MV slot
  _mvCatchupCtx = _mvSelWidgetCtx ? { ..._mvSelWidgetCtx } : null;
  _epgItem = it;
  showCatchup();
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
  // Tag the item with the current selector mode so _mvPlayChannel resolves it correctly
  it._mvMode = _mvSelContentMode;
  if(_mvSelCat) it._mvCat = _mvSelCat;
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
  const isLive   = _mvSelContentMode === 'live';
  const actions = [];

  // Browse episodes (series/group drill)
  if(isGroup){
    actions.push({icon:'📋', label:`Browse ${(it._episodes||[]).length} eps`, fn:`_mvCloseCtxMenu();_mvSelDrillGrp(${i})`});
  } else if(isShow && isSeries){
    actions.push({icon:'📋', label:'Browse episodes', fn:`_mvCloseCtxMenu();_mvSelDrillShow(${i})`});
  }

  // External Player — available for directly playable items
  if(!isShow && !isGroup){
    actions.push({icon:'🎬', label:'External Player', fn:`_mvCloseCtxMenu();_mvSelExternalPlay(${i})`});
  }

  // Record (via DVR) — live only, directly playable
  if(isLive && !isShow && !isGroup && typeof _DVR_OK !== 'undefined' && _DVR_OK){
    actions.push({icon:'⏺', label:'Record', fn:`_mvCloseCtxMenu();_mvSelRecord(${i})`});
  }

  // Download MKV — VOD/Series episodes (not show headers)
  if(isSeries && !isShow && !isGroup){
    actions.push({icon:'⬇', label:'Download MKV', fn:`_mvCloseCtxMenu();_mvSelMKV(${i})`});
  }

  // TMDB/IMDb — VOD and Series only
  if(_mvSelContentMode !== 'live'){
    actions.push({icon:'🔍', label:'Open TMDB/IMDb',
      fn:`_mvSelIMDb(${i});_mvCloseCtxMenu()`});
  }

  // Hide — always available
  actions.push({icon:'🚫', label:'Hide this item', fn:`_mvCloseCtxMenu();_mvSelHideItem(${i})`});

  if(!actions.length){
    // Fallback so the menu isn't empty
    actions.push({icon:'ℹ', label:'No actions available', fn:`_mvCloseCtxMenu()`});
  }

  _mvOpenCtxMenu(btn, actions);
}

// ── MV submenu action helpers ──────────────────────────────────────────────

async function _mvSelExternalPlay(i){
  let it;
  if(_mvSelNavMode === 'episodes'){
    it = (_mvSelEpisodesFiltered.length ? _mvSelEpisodesFiltered : _mvSelEpisodes)[i];
  } else {
    it = (_mvSelFilteredItems.length ? _mvSelFilteredItems : _mvSelItems)[i];
  }
  if(!it) return;
  const name = it.name||it.o_name||'?';
  const cat  = _mvSelCat || {};
  const m    = _mvSelContentMode;

  if(_isMobile){
    toast('Resolving stream…','info');
    try{
      const r = await fetch('/api/resolve_url',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({item:it, mode:m, category:cat})});
      const d = await r.json();
      if(d.error){ toast('Error: '+d.error,'err'); return; }
      const player = localStorage.getItem('mobile_player')||'ask';
      if(player==='copy'){
        try{ await navigator.clipboard.writeText(d.url); toast('URL copied!','ok'); }
        catch(e){ prompt('Copy URL:',d.url); }
        return;
      }
      window.location.href = `intent:${d.url}#Intent;type=video/*;S.browser_fallback_url=about:blank;end`;
    }catch(e){ toast('Failed: '+e,'err'); }
    return;
  }
  const exe = (localStorage.getItem('ext_player')||'').trim();
  if(!exe){ toast('Set external player path in ⚙ settings first','wrn'); return; }
  toast('Opening in external player…','info');
  try{
    const r = await fetch('/api/open_external',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({exe, item:it, mode:m, category:cat})});
    const d = await r.json();
    if(d.error) toast('Error: '+d.error,'err');
    else toast('Launched: '+name,'ok');
  }catch(e){ toast('Failed: '+e,'err'); }
}

async function _mvSelRecord(i){
  const it = (_mvSelFilteredItems.length ? _mvSelFilteredItems : _mvSelItems)[i];
  if(!it) return;
  const name = it.name||it.o_name||'Recording';
  const cat  = _mvSelCat || {};
  toast('Resolving stream…','info');
  try{
    const r = await fetch('/api/resolve',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({item:it, mode:'live', category:cat})});
    const d = await r.json();
    if(!d.url){ toast(d.error||'Could not resolve URL','err'); return; }
    let url = d.url;
    if(url.includes('/api/hls_proxy')){
      try{ const p=new URLSearchParams(url.split('?')[1]||''); url=p.get('url')||url; }catch(e){}
    }
    const rb = await fetch('/api/dvr/record_now',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({channelId:it.id||it.stream_id||'', channelName:name,
        streamUrl:url, title:name, durationMinutes:120})});
    const rd = await rb.json();
    if(rb.ok){
      toast('⏺ DVR recording started: '+name,'ok');
      try{ const j=await fetch('/api/dvr/jobs').then(r=>r.json());
        if(Array.isArray(j)){ _dvrJobs=j; _dvrInited=true; _dvrBadgeUpdate(); } }catch(e){}
    } else { toast(rd.error||'DVR record failed','err'); }
  }catch(e){ toast('Record error: '+e,'err'); }
}

async function _mvSelMKV(i){
  let it;
  if(_mvSelNavMode === 'episodes'){
    it = (_mvSelEpisodesFiltered.length ? _mvSelEpisodesFiltered : _mvSelEpisodes)[i];
  } else {
    it = (_mvSelFilteredItems.length ? _mvSelFilteredItems : _mvSelItems)[i];
  }
  if(!it) return;
  const od = document.getElementById('o-dir')?.value?.trim();
  if(!od){ toast('Set output folder in ⚙ settings first','wrn'); return; }
  const cat = _mvSelCat || {};
  const m   = _mvSelContentMode;
  toast('Starting MKV download…','info');
  try{
    const r = await fetch('/api/download/mkv',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({items:[it], mode:m, category:cat, out_dir:od})});
    const d = await r.json();
    if(d.error) toast(d.error,'err');
    else toast('MKV download started','ok');
  }catch(e){ toast('Download error: '+e,'err'); }
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
  // Keep tabs hidden in forced-mode (e.g. DVR live-only picker) even when searching at cats level
  if(tabsEl) tabsEl.style.display = _mvSelForcedMode ? 'none' : '';
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
  // Play URL row: only show for multiview (needs a widget target) and only in live mode
  playUrlRow.style.display = (_mvSelContentMode === 'live' && _mvSelWidgetCtx) ? '' : 'none';

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
  } catch(e){ setStatus('Could not load layouts: ' + (e.message||e)); }
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
  } catch(e){ setStatus('Could not restore multiview layout'); }
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

  // Channel selector back/close/cancel buttons are wired globally in the
  // DOMContentLoaded block so they work from DVR channel picker without
  // needing multiview to have been opened first. Nothing to add here.

  // mirrors multiview.js: close panel on outside click
  // (not applicable here since we're a full-overlay panel, but
  //  Escape key is a good UX addition that mirrors the Node.js app behaviour)
  document.addEventListener('keydown', e=>{
    if(e.key !== 'Escape') return;

    // DVR overlay — highest priority, close it first
    const dvrOvl = document.getElementById('dvr-overlay');
    if(dvrOvl && dvrOvl.style.display === 'flex'){ dvrClose(); return; }

    // mv-sel-overlay — may be open from DVR channel picker OR from multiview
    if(document.getElementById('mv-sel-overlay').classList.contains('open')){
      if(typeof _mvSelNavMode !== 'undefined' && _mvSelNavMode === 'items'){
        _mvSelNavMode = 'cats'; _mvSelCat = null; _mvSelItems = [];
        document.getElementById('mv-sel-search').value = '';
        const tabsEl = document.getElementById('mv-sel-tabs');
        if(tabsEl) tabsEl.style.display = '';
        if(typeof _mvRenderSel === 'function') _mvRenderSel();
      } else {
        document.getElementById('mv-sel-overlay').classList.remove('open');
        if(typeof mvSelCallback !== 'undefined') mvSelCallback = null;
      }
      return;
    }

    // Multiview-only modals — only when multiview panel is active
    if(document.getElementById('p-mv').classList.contains('mv-active')){
      if(document.getElementById('mv-confirm-overlay').classList.contains('open')){
        document.getElementById('mv-confirm-overlay').classList.remove('open');
      } else if(document.getElementById('mv-save-overlay').classList.contains('open')){
        document.getElementById('mv-save-overlay').classList.remove('open');
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

// ── Global close wiring for mv-sel-overlay ───────────────────────────────
  // These must be wired globally (not just inside _mvSetupListeners) so the
  // close/cancel/✕ buttons work when the selector is opened from outside
  // the multiview panel — e.g. DVR channel picker.
  const _mvsOverlay = document.getElementById('mv-sel-overlay');
  function _mvsClose(){
    if(_mvsOverlay) _mvsOverlay.classList.remove('open');
    if(typeof mvSelCallback !== 'undefined') mvSelCallback = null;
    _mvSelForcedMode = null;
    // Restore mode tabs visibility
    const tabsEl = document.getElementById('mv-sel-tabs');
    if(tabsEl) tabsEl.style.display = '';
  }
  const _mvsCloseBtn   = document.getElementById('mv-sel-close');
  const _mvsCancelBtn  = document.getElementById('mv-sel-cancel');
  if(_mvsCloseBtn)  _mvsCloseBtn.addEventListener('click',  _mvsClose);
  if(_mvsCancelBtn) _mvsCancelBtn.addEventListener('click', _mvsClose);
  if(_mvsOverlay){
    _mvsOverlay.addEventListener('click', e=>{
      if(e.target === _mvsOverlay) _mvsClose();
    });
  }

"""
