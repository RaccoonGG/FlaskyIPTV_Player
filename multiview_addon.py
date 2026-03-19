"""
multiview_addon.py  —  Multi-View stream management for FlaskAppPlayerDownloader_byGG.py
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
INTEGRATION  (two small changes to FlaskAppPlayerDownloader_byGG.py)
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
        """
        Resolve ffmpeg binary path.
        Direct copy of cast_addon._get_ffmpeg() — kept in sync manually.
        The Flask app itself uses shutil.which("ffmpeg") everywhere; we mirror
        that but also handle the PyInstaller frozen-bundle case from cast_addon.
        """
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            bundled = os.path.join(sys._MEIPASS, 'ffmpeg.exe')
            if os.path.exists(bundled):
                return bundled
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
            for candidate in (
                os.path.join(base, '_internal', 'ffmpeg.exe'),
                os.path.join(base, 'ffmpeg.exe'),
            ):
                if os.path.exists(candidate):
                    return candidate
        if os.path.exists('ffmpeg.exe'):
            return os.path.abspath('ffmpeg.exe')
        # Same fallback used throughout the Flask app
        return shutil.which('ffmpeg') or 'ffmpeg'


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
                 audio_only: bool = False) -> None:
        self.stream_key:  str   = stream_key
        self.channel_url: str   = channel_url
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
            # HEVC → H.264 transcode: re-encode video + audio for full compatibility.
            # mirrors /api/hls_proxy?transcode=1 logic
            codec_args = [
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ac', '2',
                '-ar', '48000',
            ]
            LOG.info('[MV] Spawning ffmpeg with HEVC→H.264 transcode  key=%s', self.stream_key)
        elif self.audio_only:
            # Stream-copy video, re-encode audio only (AC3/EAC3/DTS → AAC).
            # Used when the video codec is already browser-compatible (H.264)
            # but the audio codec is not (e.g. EAC3 / AC3 / DTS).
            codec_args = [
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ac', '2',
                '-ar', '48000',
            ]
            LOG.info('[MV] Spawning ffmpeg with audio-only transcode  key=%s', self.stream_key)
        else:
            codec_args = ['-c', 'copy']

        cmd = [
            ffmpeg,
            '-hide_banner',
            '-loglevel', 'error',
            '-user_agent', self.user_agent,
            '-reconnect', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
            '-i', self.channel_url,
        ] + codec_args + [
            '-f', 'mpegts',
            'pipe:1',
        ]
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
                                audio_only: bool = False) -> Optional[StreamBroadcaster]:
    """
    Return existing broadcaster for stream_key (if alive) or create a new one.

    Node.js equivalent (server.js /stream GET handler):
        const activeStreamInfo = activeStreamProcesses.get(streamKey);
        if (activeStreamInfo) {
            activeStreamInfo.references++;
            activeStreamInfo.lastAccess = Date.now();
            activeStreamInfo.process.stdout.pipe(res);
            ...
            return;
        }
        // ── NEW stream ──
        const ffmpeg = spawn('ffmpeg', args);
        activeStreamProcesses.set(streamKey, newStreamInfo);
    """
    with _mv_streams_lock:
        existing = _mv_streams.get(stream_key)

        if existing:
            if existing.is_alive():
                # Reuse — same as the server.js early-return branch
                return existing
            # Dead process left in map — clean it up before creating a fresh one
            LOG.warning('[MV] Dead broadcaster found in registry  key=%s  — replacing',
                        stream_key)
            existing.stop()
            del _mv_streams[stream_key]

        # No existing broadcaster — spawn a new ffmpeg process
        broadcaster = StreamBroadcaster(stream_key, channel_url, user_agent,
                                        transcode=transcode, audio_only=audio_only)
        if broadcaster.process:
            _mv_streams[stream_key] = broadcaster
            return broadcaster

        # Spawn failed
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

def register_multiview_routes(app) -> None:
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

        broadcaster = _get_or_create_broadcaster(stream_key, channel_url, user_agent,
                                                 transcode=transcode,
                                                 audio_only=audio_only)
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
            """Translate a simple quality label to a yt-dlp format string."""
            if q in ('best', '', None):
                return 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio/best'
            # Numeric height — pick the closest available without going over
            try:
                h = int(q)
            except ValueError:
                return 'best[ext=mp4]/best'
            # e.g. "best[height<=720][ext=mp4]/best[height<=720]/best"
            return (
                f'best[height<={h}][ext=mp4]'
                f'/best[height<={h}]'
                f'/bestvideo[height<={h}]+bestaudio'
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
            resolved = None
            formats  = info.get('formats') or []

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
                # For VOD: prefer the resolved single-file URL yt-dlp chose
                # (already filtered by format selector), then fall back.
                resolved = info.get('url') or (formats[-1].get('url') if formats else None)
                actual_h = info.get('height') or (formats[-1].get('height') if formats else None)

            if not resolved:
                return jsonify({'error': 'yt-dlp could not extract a stream URL'}), 502

            LOG.info('[MV][resolve_url] resolved  title=%r  live=%s  quality=%s  height=%s  via=yt-dlp',
                     title, is_live, quality, actual_h)
            return jsonify({
                'url':     resolved,
                'title':   title,
                'is_live': is_live,
                'quality': quality,
                'height':  actual_h,
                'via':     'yt-dlp',
            })

        except Exception as exc:
            LOG.error('[MV][resolve_url] yt-dlp error: %s', exc)
            return jsonify({'error': str(exc)}), 502

    LOG.info('[MV] Multiview routes registered  '
             '(layouts_file=%s)', LAYOUTS_FILE)
