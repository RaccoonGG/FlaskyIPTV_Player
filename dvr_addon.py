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
dvr_addon.py  —  DVR (scheduled recording) addon for FlaskyIPTV_Player_byGG.py
======================================================================================

Adds a full DVR tab to the Flask IPTV portal:
  • Scheduled recordings (future start time)
  • Manual recordings (channel + time range)
  • In-progress recordings with timeshift playback
  • Completed recordings library with playback + delete
  • Storage usage bar
  • Per-job state: scheduled → recording → completed | error | cancelled

All job state persists to dvr_jobs.json next to the script.
Completed recording files are .ts files written to the configured DVR folder.

INTEGRATION  (two lines in FlaskyIPTV_Player_byGG.py)
─────────────────────────────────────────────────────────────
STEP 1 — add import after the multiview_addon import block:

    try:
        from dvr_addon import register_dvr_routes
        _DVR_AVAILABLE = True
    except ImportError:
        _DVR_AVAILABLE = False
        def register_dvr_routes(*a, **kw): pass

STEP 2 — register routes after multiview registration:

    register_dvr_routes(flask_app, state)

STEP 3 — add script tag after /api/epg/ui.js (DVR uses _fmtEpgTime from EPG):

    <script src="/api/dvr/ui.js"></script>

That's it.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from flask import jsonify, request, Response, send_from_directory

LOG = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DVR_JOBS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dvr_jobs.json")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg resolution (mirrors multiview_addon pattern)
# ─────────────────────────────────────────────────────────────────────────────




# ─────────────────────────────────────────────────────────────────────────────
# Job persistence
# ─────────────────────────────────────────────────────────────────────────────

_jobs_lock  = threading.Lock()
_jobs_cache: Optional[List[dict]] = None   # in-memory mirror of dvr_jobs.json
_jobs_dirty: bool = True                    # True = cache invalid, must read disk


def _load_jobs() -> List[dict]:
    """Return the job list. Reads from disk only when the cache is stale.
    All callers hold _jobs_lock, so no extra locking needed here."""
    global _jobs_cache, _jobs_dirty
    if not _jobs_dirty and _jobs_cache is not None:
        return _jobs_cache          # fast path — no disk I/O
    if not os.path.exists(DVR_JOBS_FILE):
        _jobs_cache = []
        _jobs_dirty = False
        return _jobs_cache
    try:
        with open(DVR_JOBS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _jobs_cache = data.get("jobs", [])
        _jobs_dirty = False
        return _jobs_cache
    except Exception as exc:
        LOG.error("[DVR] Failed to load jobs file: %s", exc)
        _jobs_cache = []
        return _jobs_cache


def _save_jobs(jobs: List[dict]) -> None:
    global _jobs_cache, _jobs_dirty
    try:
        with open(DVR_JOBS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"jobs": jobs}, fh, indent=2, ensure_ascii=False)
        _jobs_cache = jobs
        _jobs_dirty = False
    except Exception as exc:
        LOG.error("[DVR] Failed to save jobs file: %s", exc)
        _jobs_dirty = True          # force re-read next time


def _get_job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        jobs = _load_jobs()
    return next((j for j in jobs if j["id"] == job_id), None)


def _update_job(job_id: str, updates: dict) -> bool:
    with _jobs_lock:
        jobs = _load_jobs()
        for j in jobs:
            if j["id"] == job_id:
                j.update(updates)
                _save_jobs(jobs)
                return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler thread — wakes every 15 s, fires recordings that are due
# ─────────────────────────────────────────────────────────────────────────────

_scheduler_thread: Optional[threading.Thread] = None
_scheduler_stop = threading.Event()
_FFMPEG: str = shutil.which("ffmpeg") or "ffmpeg"  # overridden by register_dvr_routes
_active_recordings: Dict[str, subprocess.Popen] = {}  # job_id → ffmpeg Popen
_active_lock = threading.Lock()

_app_state = None  # set at register time


def _scheduler_loop():
    LOG.info("[DVR] Scheduler started")
    while not _scheduler_stop.wait(5):
        try:
            _tick()
        except Exception as exc:
            LOG.error("[DVR] Scheduler tick error: %s", exc)
    LOG.info("[DVR] Scheduler stopped")


def _tick():
    now = datetime.now(timezone.utc)

    # ── Phase 1: check completed recordings (no lock held during poll) ────────
    finished = []  # (job_id, returncode)
    with _active_lock:
        for job_id, proc in list(_active_recordings.items()):
            if proc.poll() is not None:
                finished.append((job_id, proc.returncode))
                del _active_recordings[job_id]

    # ── Phase 2: update job state (brief lock, no I/O inside) ─────────────────
    with _jobs_lock:
        jobs = _load_jobs()
        changed = False

        # Mark finished recordings
        for job_id, rc in finished:
            for job in jobs:
                if job["id"] == job_id and job["status"] == "recording":
                    if rc == 0 or rc == -15 or rc == 1:
                        job["status"] = "completed"
                        fp = job.get("filePath", "")
                        if fp and os.path.exists(fp):
                            job["fileSizeBytes"] = os.path.getsize(fp)
                            start_t = datetime.fromisoformat(job["startTime"].replace("Z", "+00:00"))
                            end_t   = datetime.fromisoformat(job["endTime"].replace("Z", "+00:00"))
                            job["durationSeconds"] = int((end_t - start_t).total_seconds())
                        LOG.info("[DVR] Completed: %s (rc=%d)", job.get("programTitle"), rc)
                    else:
                        job["status"] = "error"
                        job["errorMessage"] = f"ffmpeg exited with code {rc}"
                        LOG.error("[DVR] Error recording %s (rc=%d)", job.get("programTitle"), rc)
                    changed = True

        # Collect jobs that need to start (don't spawn inside lock)
        to_start = []
        for job in jobs:
            if job["status"] == "recording":
                # Belt-and-suspenders: if a recording is still marked "recording"
                # but its scheduled end time has passed by >30s AND it is no longer
                # in _active_recordings (proc was never detected or was missed),
                # promote it to completed if the output file exists, else error.
                try:
                    end_dt_chk = datetime.fromisoformat(job["endTime"].replace("Z", "+00:00"))
                    if now > end_dt_chk + timedelta(seconds=30):
                        with _active_lock:
                            still_active = job["id"] in _active_recordings
                        if not still_active:
                            fp = job.get("filePath", "")
                            if fp and os.path.exists(fp):
                                job["status"] = "completed"
                                job["fileSizeBytes"] = os.path.getsize(fp)
                                LOG.info("[DVR] Rescued completed recording: %s", job.get("programTitle"))
                            else:
                                job["status"] = "error"
                                job["errorMessage"] = "Recording ended without output file."
                                LOG.warning("[DVR] Rescued error recording: %s", job.get("programTitle"))
                            changed = True
                except Exception:
                    pass
                continue

            if job["status"] != "scheduled":
                continue
            start_dt = datetime.fromisoformat(job["startTime"].replace("Z", "+00:00"))
            end_dt   = datetime.fromisoformat(job["endTime"].replace("Z", "+00:00"))
            if now > end_dt:
                job["status"] = "error"
                job["errorMessage"] = "Recording missed — start time passed before it could begin."
                changed = True
                LOG.warning("[DVR] Missed recording: %s", job.get("programTitle"))
            elif now >= start_dt:
                to_start.append(job)

        if changed or to_start:
            _save_jobs(jobs)

    # ── Phase 3: spawn ffmpeg outside the lock ────────────────────────────────
    for job in to_start:
        _start_recording_unlocked(job)
        # Re-save after spawn so filePath/status are persisted
        with _jobs_lock:
            jobs2 = _load_jobs()
            for j2 in jobs2:
                if j2["id"] == job["id"]:
                    j2.update({k: job[k] for k in ("status", "filePath", "filename") if k in job})
            _save_jobs(jobs2)


def _start_recording_unlocked(job: dict):
    """Spawn ffmpeg for this job. Must be called with _jobs_lock held."""
    ffmpeg = _FFMPEG
    if not os.path.exists(ffmpeg) and not shutil.which("ffmpeg"):
        job["status"] = "error"
        job["errorMessage"] = "ffmpeg not found"
        return

    # Re-resolve the stream URL right before spawning ffmpeg so that
    # short-lived CDN tokens (Stalker/MAC portals) are always fresh.
    # Falls back to the URL stored at schedule time if the resolver is
    # unavailable or fails.
    stream_url = job.get("streamUrl", "")
    if _app_state and callable(getattr(_app_state, "dvr_url_resolver", None)):
        try:
            fresh = _app_state.dvr_url_resolver(job)
            if fresh:
                LOG.info("[DVR] Refreshed stream URL for %s", job.get("programTitle"))
                stream_url = fresh
                job["streamUrl"] = fresh   # persist so stop/restart also uses fresh URL
        except Exception as _re:
            LOG.warning("[DVR] URL refresh failed, using stored URL: %s", _re)

    if not stream_url:
        job["status"] = "error"
        job["errorMessage"] = "No stream URL stored for this job"
        return

    # Output folder — use state.mkv_folder or ~/Downloads
    out_dir = ""
    if _app_state:
        out_dir = getattr(_app_state, "mkv_folder", "") or getattr(_app_state, "dvr_folder", "")
    if not out_dir:
        out_dir = os.path.join(os.path.expanduser("~"), "Downloads", "DVR")
    os.makedirs(out_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _safe_fname(job.get("programTitle", "recording"))
    fname = f"{safe}_{ts}.ts"
    out_path = os.path.join(out_dir, fname)

    # Duration in seconds
    start_dt = datetime.fromisoformat(job["startTime"].replace("Z", "+00:00"))
    end_dt   = datetime.fromisoformat(job["endTime"].replace("Z", "+00:00"))
    duration = max(10, int((end_dt - start_dt).total_seconds()))

    _ua = getattr(_app_state, "stream_ua", "VLC/3.0.0 LibVLC/3.0.0") if _app_state else "VLC/3.0.0 LibVLC/3.0.0"
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin",
        "-user_agent", _ua,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
        "-i", stream_url,
        "-t", str(duration),
        "-c", "copy",
        "-f", "mpegts",
        out_path,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:
        job["status"] = "error"
        job["errorMessage"] = f"Failed to spawn ffmpeg: {exc}"
        LOG.error("[DVR] Spawn failed for %s: %s", job.get("programTitle"), exc)
        return

    # CRITICAL: drain stderr in a background thread.
    # ffmpeg writes continuous progress output (frame counts, bitrate, speed)
    # to stderr. Without a reader the OS pipe buffer fills up (~4 KB on
    # Windows, ~64 KB on Linux) and ffmpeg BLOCKS — proc.poll() returns None
    # forever so _tick() never marks the job completed.
    threading.Thread(
        target=lambda: [line for line in proc.stderr],
        daemon=True,
        name=f"dvr-stderr-{job.get('id','')[:8]}",
    ).start()

    with _active_lock:
        _active_recordings[job["id"]] = proc

    job["status"] = "recording"
    job["filePath"] = out_path
    job["filename"] = fname
    LOG.info("[DVR] ⏺ Started recording PID %d → %s", proc.pid, fname)
    if _app_state:
        _app_state.log(f"[DVR] ⏺ Recording started: {fname}")


def _safe_fname(name: str) -> str:
    import re
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:80].strip("._") or "recording"


# ─────────────────────────────────────────────────────────────────────────────
# Route registration
# ─────────────────────────────────────────────────────────────────────────────

def register_dvr_routes(app, state=None) -> None:
    global _app_state, _scheduler_thread
    _app_state = state

    # ── Clean up ghost jobs from previous crashed sessions ────────────────────
    # Any job stuck in 'recording' status on startup means ffmpeg died without
    # us catching it (crash, restart). Mark them as error so the UI doesn't
    # show stale Watch buttons pointing to files that may no longer exist.
    with _jobs_lock:
        jobs = _load_jobs()
        changed = False
        for job in jobs:
            if job.get("status") == "recording":
                fp = job.get("filePath", "")
                # If the file exists, mark completed; otherwise mark error
                if fp and os.path.exists(fp):
                    job["status"] = "completed"
                    try:
                        job["fileSizeBytes"] = os.path.getsize(fp)
                    except Exception:
                        pass
                    LOG.info("[DVR] Recovered completed recording on startup: %s", job.get("programTitle"))
                else:
                    job["status"] = "error"
                    job["errorMessage"] = "Recording interrupted (app restarted)"
                    LOG.warning("[DVR] Ghost recording cleared on startup: %s", job.get("programTitle"))
                changed = True
        if changed:
            _save_jobs(jobs)

    # Start scheduler
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="dvr-scheduler")
    _scheduler_thread.start()

    # ── POST /api/dvr/set_folder  (persist DVR output folder) ─────────────────
    @app.route("/api/dvr/set_folder", methods=["POST"])
    def dvr_set_folder():
        d = request.get_json(force=True)
        folder = (d.get("folder") or "").strip()
        if not folder:
            return jsonify({"error": "folder is required"}), 400
        if state:
            state.dvr_folder = folder
            # Also update mkv_folder as fallback so existing logic picks it up
            if not getattr(state, "mkv_folder", ""):
                state.mkv_folder = folder
        _storage_cache.clear()
        LOG.info("[DVR] Output folder set: %s", folder)
        return jsonify({"ok": True, "folder": folder})

    # ── GET /api/dvr/jobs ─────────────────────────────────────────────────────
    @app.route("/api/dvr/jobs")
    def dvr_list_jobs():
        jobs = _load_jobs()
        # Separate scheduled/recording/error from completed (completed go to recordings endpoint)
        active = [j for j in jobs if j["status"] != "completed"]
        return jsonify(active)

    # ── POST /api/dvr/schedule  (from EPG) ────────────────────────────────────
    @app.route("/api/dvr/schedule", methods=["POST"])
    def dvr_schedule():
        d = request.get_json(force=True)
        channel_id   = (d.get("channelId") or "").strip()
        channel_name = (d.get("channelName") or "Unknown").strip()
        title        = (d.get("programTitle") or "Recording").strip()
        start_iso    = d.get("programStart") or d.get("startTime") or ""
        stop_iso     = d.get("programStop")  or d.get("endTime")   or ""
        stream_url   = (d.get("streamUrl") or "").strip()
        channel_item = d.get("channelItem") or {}

        if not start_iso or not stop_iso:
            return jsonify({"error": "startTime and endTime are required"}), 400

        job = {
            "id":            str(uuid.uuid4()),
            "channelId":     channel_id,
            "channelName":   channel_name,
            "programTitle":  title,
            "startTime":     start_iso,
            "endTime":       stop_iso,
            "streamUrl":     stream_url,
            "channelItem":   channel_item,
            "status":        "scheduled",
            "filePath":      "",
            "filename":      "",
            "fileSizeBytes": 0,
            "durationSeconds": 0,
            "errorMessage":  "",
            "createdAt":     datetime.now(timezone.utc).isoformat(),
        }

        with _jobs_lock:
            jobs = _load_jobs()
            jobs.append(job)
            _save_jobs(jobs)

        LOG.info("[DVR] Scheduled: %s  %s → %s", title, start_iso, stop_iso)
        if state:
            state.log(f"[DVR] Scheduled: {title}")
        return jsonify(job), 201

    # ── POST /api/dvr/schedule/manual ─────────────────────────────────────────
    @app.route("/api/dvr/schedule/manual", methods=["POST"])
    def dvr_schedule_manual():
        d = request.get_json(force=True)
        channel_id   = (d.get("channelId") or "").strip()
        channel_name = (d.get("channelName") or "Unknown").strip()
        start_iso    = d.get("startTime") or ""
        end_iso      = d.get("endTime")   or ""
        stream_url   = (d.get("streamUrl") or "").strip()
        title        = (d.get("programTitle") or f"Scheduled – {channel_name}").strip()
        channel_item = d.get("channelItem") or {}

        if not start_iso or not end_iso:
            return jsonify({"error": "startTime and endTime are required"}), 400

        try:
            s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            if e <= s:
                return jsonify({"error": "endTime must be after startTime"}), 400
        except ValueError as exc:
            return jsonify({"error": f"Invalid datetime: {exc}"}), 400

        job = {
            "id":            str(uuid.uuid4()),
            "channelId":     channel_id,
            "channelName":   channel_name,
            "programTitle":  title,
            "startTime":     start_iso,
            "endTime":       end_iso,
            "streamUrl":     stream_url,
            "channelItem":   channel_item,
            "status":        "scheduled",
            "filePath":      "",
            "filename":      "",
            "fileSizeBytes": 0,
            "durationSeconds": 0,
            "errorMessage":  "",
            "createdAt":     datetime.now(timezone.utc).isoformat(),
        }

        with _jobs_lock:
            jobs = _load_jobs()
            jobs.append(job)
            _save_jobs(jobs)

        LOG.info("[DVR] Manual scheduled: %s", title)
        if state:
            state.log(f"[DVR] Manual scheduled: {title}")
        return jsonify(job), 201

    # ── PUT /api/dvr/jobs/<id>  (edit time) ───────────────────────────────────
    @app.route("/api/dvr/jobs/<job_id>", methods=["PUT"])
    def dvr_edit_job(job_id):
        d = request.get_json(force=True)
        updates = {}
        if "startTime" in d:
            updates["startTime"] = d["startTime"]
        if "endTime" in d:
            updates["endTime"] = d["endTime"]
        if not updates:
            return jsonify({"error": "Nothing to update"}), 400
        if _update_job(job_id, updates):
            return jsonify({"ok": True})
        return jsonify({"error": "Job not found"}), 404

    # ── DELETE /api/dvr/jobs/<id>  (cancel or remove from history) ────────────
    @app.route("/api/dvr/jobs/<job_id>", methods=["DELETE"])
    def dvr_cancel_job(job_id):
        with _jobs_lock:
            jobs = _load_jobs()
            job = next((j for j in jobs if j["id"] == job_id), None)
            if not job:
                return jsonify({"error": "Job not found"}), 404

            if job["status"] == "recording":
                # Kill the ffmpeg process
                with _active_lock:
                    proc = _active_recordings.pop(job_id, None)
                if proc:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

            jobs = [j for j in jobs if j["id"] != job_id]
            _save_jobs(jobs)

        LOG.info("[DVR] Cancelled/deleted job %s", job_id)
        return jsonify({"ok": True})

    # ── POST /api/dvr/jobs/<id>/stop  (stop active recording) ─────────────────
    @app.route("/api/dvr/jobs/<job_id>/stop", methods=["POST"])
    def dvr_stop_job(job_id):
        with _active_lock:
            proc = _active_recordings.pop(job_id, None)
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        with _jobs_lock:
            jobs = _load_jobs()
            for j in jobs:
                if j["id"] == job_id:
                    j["status"] = "completed"
                    fp = j.get("filePath", "")
                    if fp and os.path.exists(fp):
                        j["fileSizeBytes"] = os.path.getsize(fp)
                    break
            _save_jobs(jobs)

        return jsonify({"ok": True})

    # ── DELETE /api/dvr/jobs/<id>/history  (remove from history, keep file) ───
    @app.route("/api/dvr/jobs/<job_id>/history", methods=["DELETE"])
    def dvr_remove_history(job_id):
        with _jobs_lock:
            jobs = _load_jobs()
            jobs = [j for j in jobs if j["id"] != job_id]
            _save_jobs(jobs)
        return jsonify({"ok": True})

    # ── DELETE /api/dvr/jobs/all  (clear all non-recording jobs) ──────────────
    @app.route("/api/dvr/jobs/all", methods=["DELETE"])
    def dvr_clear_jobs():
        with _jobs_lock:
            jobs = _load_jobs()
            # Keep only actively recording jobs
            jobs = [j for j in jobs if j["status"] == "recording"]
            _save_jobs(jobs)
        return jsonify({"ok": True})

    # ── GET /api/dvr/recordings  (completed recordings) ───────────────────────
    @app.route("/api/dvr/recordings")
    def dvr_list_recordings():
        jobs = _load_jobs()
        completed = [j for j in jobs if j["status"] == "completed"]
        return jsonify(completed)

    # ── DELETE /api/dvr/recordings/<id>  (delete file + job) ──────────────────
    @app.route("/api/dvr/recordings/<job_id>", methods=["DELETE"])
    def dvr_delete_recording(job_id):
        with _jobs_lock:
            jobs = _load_jobs()
            job = next((j for j in jobs if j["id"] == job_id), None)
            if not job:
                return jsonify({"error": "Recording not found"}), 404
            fp = job.get("filePath", "")
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception as exc:
                    LOG.warning("[DVR] Could not delete file %s: %s", fp, exc)
            jobs = [j for j in jobs if j["id"] != job_id]
            _save_jobs(jobs)
        return jsonify({"ok": True})

    # ── DELETE /api/dvr/recordings/all  (delete all completed recordings + files)
    @app.route("/api/dvr/recordings/all", methods=["DELETE"])
    def dvr_clear_recordings():
        with _jobs_lock:
            jobs = _load_jobs()
            to_delete = [j for j in jobs if j["status"] == "completed"]
            for j in to_delete:
                fp = j.get("filePath", "")
                if fp and os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except Exception as exc:
                        LOG.warning("[DVR] Could not delete file %s: %s", fp, exc)
            jobs = [j for j in jobs if j["status"] != "completed"]
            _save_jobs(jobs)
        return jsonify({"ok": True})

    # ── GET /api/dvr/storage  (disk usage for DVR folder) ─────────────────────
    # Cache storage result for 60 s — disk_usage is a syscall and can be
    # slow on Windows/network drives; the value changes slowly anyway.
    _storage_cache: dict = {}

    @app.route("/api/dvr/storage")
    def dvr_storage():
        nonlocal _storage_cache
        # Accept explicit path from frontend (avoids stale state.dvr_folder)
        out_dir = request.args.get("path", "").strip()
        if not out_dir and state:
            out_dir = getattr(state, "dvr_folder", "") or getattr(state, "mkv_folder", "")
        if not out_dir:
            out_dir = os.path.join(os.path.expanduser("~"), "Downloads", "DVR")

        # Return cached result if fresh enough
        cached = _storage_cache
        if cached.get("folder") == out_dir and time.time() - cached.get("_ts", 0) < 60:
            return jsonify({k: v for k, v in cached.items() if k != "_ts"})

        try:
            usage = shutil.disk_usage(out_dir if os.path.exists(out_dir) else os.path.expanduser("~"))
            total = usage.total
            used  = usage.used
            pct   = round(used / total * 100, 1) if total else 0
            result = {"total": total, "used": used, "free": usage.free,
                      "percentage": pct, "folder": out_dir, "_ts": time.time()}
            _storage_cache = result
            return jsonify({k: v for k, v in result.items() if k != "_ts"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── GET /api/dvr/timeshift/<job_id>  (transcode in-progress recording) ─────
    @app.route("/api/dvr/timeshift/<job_id>", methods=["GET", "HEAD"])
    def dvr_timeshift(job_id):
        """
        Pipe the partially-written recording .ts file through ffmpeg transcode,
        tail-following the file as ffmpeg writes more data.

        This gives the browser:
          - Full seeking into already-recorded content
          - Only 1 portal connection (recording ffmpeg is already capturing)
          - HEVC/AC3 → H.264/AAC transcode so any browser can play it
          - True timeshift: pause, rewind to start, seek to any recorded point

        Uses ffmpeg -re -stream_loop -1 on the growing file. The key flags:
          -re         : read at real-time speed (prevents over-reading past EOF)
          -fflags     : +genpts to fix timestamps
        """
        job = _get_job(job_id)
        if not job:
            return Response("Job not found", status=404)
        fp = job.get("filePath", "")
        if not fp or not os.path.exists(fp):
            return Response("Recording file not available yet", status=404)

        # HEAD probe — just confirm the file exists, don't start ffmpeg
        if request.method == "HEAD":
            return Response(status=200)

        ffmpeg = _FFMPEG
        cmd = [
            ffmpeg, "-hide_banner", "-nostdin",
            "-fflags", "+genpts+igndts+discardcorrupt",
            "-i", "pipe:0",   # read from stdin — Python feeds the growing file
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
            "-f", "mpegts", "-",
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW,
            )
        except Exception as exc:
            LOG.error("[DVR] Timeshift spawn failed: %s", exc)
            return Response(f"ffmpeg error: {exc}", status=500)

        threading.Thread(
            target=lambda: [LOG.debug("[DVR/ts] %s", l.decode("utf-8","replace").rstrip())
                            for l in proc.stderr],
            daemon=True,
        ).start()

        def _feed_stdin():
            """Tail-follow the growing .ts file and pipe chunks into ffmpeg stdin.
            Python controls the pacing — ffmpeg never sees EOF while recording."""
            try:
                with open(fp, "rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if chunk:
                            try:
                                proc.stdin.write(chunk)
                            except (BrokenPipeError, OSError):
                                break  # client disconnected
                        else:
                            # No new data yet — check if recording is still active
                            current = _get_job(job_id)
                            if current and current.get("status") == "recording":
                                time.sleep(0.3)   # wait for more data to be written
                            else:
                                break  # recording finished — let ffmpeg drain & exit
            except Exception as exc:
                LOG.debug("[DVR] Timeshift feed error: %s", exc)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        threading.Thread(target=_feed_stdin, daemon=True, name=f"dvr-ts-feed-{job_id[:8]}").start()

        def _gen():
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            except GeneratorExit:
                pass
            finally:
                proc.kill()
                proc.wait()
                LOG.info("[DVR] Timeshift stream ended  job=%s", job_id)

        return Response(
            _gen(),
            mimetype="video/mp2t",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # ── GET /api/dvr/progress  (live stats for active recordings) ─────────────
    @app.route("/api/dvr/progress")
    def dvr_progress():
        """Return live file size and elapsed time for all active recordings."""
        now = time.time()
        result = {}
        jobs = _load_jobs()
        for job in jobs:
            if job.get("status") != "recording":
                continue
            job_id = job["id"]
            fp = job.get("filePath", "")
            size = 0
            if fp and os.path.exists(fp):
                try:
                    size = os.path.getsize(fp)
                except Exception:
                    pass
            # Scheduled total duration — compute first so we can cap elapsed
            try:
                start_dt = datetime.fromisoformat(job["startTime"].replace("Z", "+00:00"))
                end_dt   = datetime.fromisoformat(job["endTime"].replace("Z", "+00:00"))
                total    = max(0, int((end_dt - start_dt).total_seconds()))
            except Exception:
                start_dt = None
                total    = 0
            # Elapsed from startTime — capped at total so the counter never
            # runs past the scheduled end while the scheduler hasn't had its
            # 15-second tick yet to mark the job completed.
            try:
                elapsed = int((datetime.now(timezone.utc) - start_dt).total_seconds()) \
                          if start_dt else 0
                if total:
                    elapsed = min(elapsed, total)
            except Exception:
                elapsed = 0
            result[job_id] = {
                "fileSizeBytes":  size,
                "elapsedSeconds": max(0, elapsed),
                "totalSeconds":   max(0, total),
                "openEnded":      bool(job.get("openEnded", False)),
            }
        return jsonify(result)

    # ── GET /api/dvr/transcode/<job_id>  (transcode .ts file via ffmpeg) ────────
    # Used when a completed recording contains HEVC — serves H.264+AAC MPEG-TS
    # so the browser can play it. Reads the file directly from disk (not over HTTP)
    # so ffmpeg can seek/copy it efficiently without a local loopback.
    @app.route("/api/dvr/transcode/<job_id>")
    def dvr_transcode_file(job_id):
        job = _get_job(job_id)
        if not job:
            return Response("Job not found", status=404)
        fp = job.get("filePath", "")
        if not fp or not os.path.exists(fp):
            return Response("Recording file not found", status=404)

        duration_secs = job.get("durationSeconds", 0)

        ffmpeg = _FFMPEG
        cmd = [
            ffmpeg, "-hide_banner", "-nostdin",
            "-fflags", "+genpts+igndts",
            "-i", fp,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
            "-f", "mpegts", "-",
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW,
            )
        except Exception as exc:
            LOG.error("[DVR] Transcode spawn failed: %s", exc)
            return Response(f"ffmpeg error: {exc}", status=500)

        threading.Thread(target=lambda: [line for line in proc.stderr], daemon=True).start()

        def _gen():
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            except GeneratorExit:
                pass
            finally:
                proc.kill()
                proc.wait()

        return Response(
            _gen(),
            mimetype="video/mp2t",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # ── GET /api/dvr/serve/<filename>  (serve completed .ts file) ─────────────
    @app.route("/api/dvr/serve/<path:filename>")
    def dvr_serve_file(filename):
        """Serve a completed recording file for playback/download."""
        out_dir = ""
        if state:
            out_dir = getattr(state, "dvr_folder", "") or getattr(state, "mkv_folder", "")
        if not out_dir:
            out_dir = os.path.join(os.path.expanduser("~"), "Downloads", "DVR")

        safe = os.path.basename(filename)  # prevent path traversal
        return send_from_directory(out_dir, safe, as_attachment=False)

    # ── POST /api/dvr/record_now  (start recording immediately) ───────────────
    @app.route("/api/dvr/record_now", methods=["POST"])
    def dvr_record_now():
        """Schedule a recording that starts immediately."""
        d = request.get_json(force=True)
        channel_id   = (d.get("channelId") or "").strip()
        channel_name = (d.get("channelName") or "Unknown").strip()
        stream_url   = (d.get("streamUrl") or "").strip()
        duration_min = int(d.get("durationMinutes", 60))
        title        = (d.get("title") or f"Recording – {channel_name}").strip()
        channel_item = d.get("channelItem") or {}
        open_ended   = bool(d.get("openEnded", False))

        if not stream_url:
            return jsonify({"error": "streamUrl is required"}), 400

        now = datetime.now(timezone.utc)
        from datetime import timedelta
        end = now + timedelta(minutes=duration_min)

        job = {
            "id":            str(uuid.uuid4()),
            "channelId":     channel_id,
            "channelName":   channel_name,
            "programTitle":  title,
            "startTime":     now.isoformat(),
            "endTime":       end.isoformat(),
            "streamUrl":     stream_url,
            "channelItem":   channel_item,
            "openEnded":     open_ended,
            "status":        "scheduled",
            "filePath":      "",
            "filename":      "",
            "fileSizeBytes": 0,
            "durationSeconds": 0,
            "errorMessage":  "",
            "createdAt":     now.isoformat(),
        }

        with _jobs_lock:
            jobs = _load_jobs()
            jobs.append(job)
            # Start immediately
            _start_recording_unlocked(job)
            _save_jobs(jobs)

        return jsonify(job), 201

    _register_dvr_ui_route(app)
    LOG.info("[DVR] Routes registered  (jobs_file=%s, ui.js=yes)", DVR_JOBS_FILE)


# ─────────────────────────────────────────────────────────────────────────────
# Frontend  (served as /api/dvr/ui.js)
# ─────────────────────────────────────────────────────────────────────────────

_DVR_UI_JS_BYTES: bytes = b""   # filled in register_dvr_routes


def _register_dvr_ui_route(app) -> None:
    """Add the /api/dvr/ui.js route and pre-encode the JS once."""
    global _DVR_UI_JS_BYTES
    _DVR_UI_JS_BYTES = _DVR_UI_JS.encode("utf-8")

    @app.route("/api/dvr/ui.js")
    def dvr_ui_js():
        return Response(
            _DVR_UI_JS_BYTES,
            content_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )


_DVR_UI_JS = r"""
/* ── Inject CSS ─────────────────────────────────────────────────────── */
(function(){
  const s = document.createElement('style');
  s.textContent = `
/* ─── DVR panel ────────────────────────────────────────────────────────────── */
.dvr-card{display:flex;flex-direction:column;gap:3px;padding:9px 11px;
  background:rgba(255,255,255,.02);border:1px solid var(--bdr);border-radius:var(--rsm);
  transition:var(--tr);position:relative;overflow:hidden}
.dvr-card:hover{border-color:rgba(124,58,237,.25);background:rgba(124,58,237,.04)}
.dvr-card-top{display:flex;align-items:center;gap:6px;min-width:0}
.dvr-card-title{flex:1;font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dvr-card-ch{font-size:10px;color:var(--txt3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dvr-card-meta{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-top:1px}
.dvr-card-time{font-size:10px;color:var(--txt3)}
.dvr-badge{display:inline-block;padding:1px 7px;border-radius:20px;font-size:9px;
  font-weight:800;text-transform:uppercase;letter-spacing:.5px;flex-shrink:0}
.dvr-badge.scheduled{background:rgba(59,130,246,.15);color:#60a5fa;border:1px solid rgba(59,130,246,.3)}
.dvr-badge.recording{background:rgba(220,38,38,.2);color:#f87171;border:1px solid rgba(220,38,38,.4);
  animation:dvr-pulse 1.4s ease infinite}
@keyframes dvr-pulse{0%,100%{opacity:1}50%{opacity:.55}}
.dvr-badge.completed{background:rgba(34,197,94,.12);color:#4ade80;border:1px solid rgba(34,197,94,.25)}
.dvr-badge.error{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.3)}
.dvr-badge.cancelled{background:rgba(107,114,128,.15);color:#9ca3af;border:1px solid rgba(107,114,128,.25)}
.dvr-card-btns{display:flex;gap:4px;flex-shrink:0;margin-top:2px;justify-content:flex-end}
.dvr-card-btns button{height:24px;padding:0 8px;font-size:10px;font-weight:700;border-radius:var(--rss)}
.dvr-rec-size{font-size:10px;color:var(--txt3)}
.dvr-rec-dur{font-size:10px;color:var(--txt3)}
`;
  document.head.appendChild(s);
})();

/* ── Inject HTML ────────────────────────────────────────────────────── */
(function(){
  const d = document.createElement('div');
  d.innerHTML = `
  <div id="dvr-epg-overlay" style="display:none;position:fixed;inset:0;z-index:1400;
    background:rgba(0,0,0,.75);align-items:flex-end;justify-content:center">
    <div style="background:var(--s2);border-radius:var(--rs) var(--rs) 0 0;
      width:100%;max-width:600px;padding:16px;box-shadow:var(--sh);
      border-top:1px solid var(--bdr2);max-height:65vh;display:flex;flex-direction:column">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;flex-shrink:0">
        <div>
          <span style="font-size:13px;font-weight:700;color:var(--txt1)" id="dvr-epg-ch-name">EPG</span>
          <div style="font-size:10px;color:var(--acc);margin-top:2px;font-weight:600;letter-spacing:.3px">TAP A PROGRAMME TO SET DVR TIMES</div>
        </div>
        <button class="btn-ghost" onclick="clearTimeout(_dvrEpgRetryTimer);document.getElementById('dvr-epg-overlay').style.display='none'"
          style="height:28px;width:28px;padding:0;font-size:14px;border-radius:var(--rss)">✕</button>
      </div>
      <div id="dvr-epg-body" style="overflow-y:auto;flex:1">
        <div style="color:var(--txt3);font-size:12px;text-align:center;padding:20px">Loading…</div>
      </div>
    </div>
  </div>

<div id="dvr-overlay" style="display:none;position:fixed;inset:0;z-index:850;background:rgba(0,0,0,.6);
  align-items:center;justify-content:center">
<div id="dvr-modal" style="background:var(--bg);border:1px solid var(--bdr);border-radius:var(--r);
  width:min(440px,96vw);max-height:92vh;display:flex;flex-direction:column;overflow:hidden;
  box-shadow:0 24px 64px rgba(0,0,0,.7)">

  <!-- Header -->
  <div style="display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--bdr);flex-shrink:0">
    <span style="font-size:16px">📹</span>
    <h3 style="flex:1;font-size:14px;font-weight:700;margin:0">DVR</h3>
    <span class="badge" id="dvr-badge" style="display:none;margin-right:4px"></span>
    <button class="btn-ghost" onclick="dvrClose()" style="height:28px;width:28px;padding:0;font-size:15px">✕</button>
  </div>

  <!-- Storage bar -->
  <div id="dvr-storage-bar-wrap" style="padding:7px 14px 6px;flex-shrink:0;border-bottom:1px solid var(--bdr)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
      <span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--txt3)">Storage</span>
      <span id="dvr-storage-text" style="font-size:10px;color:var(--txt3)"></span>
    </div>
    <div style="height:5px;background:rgba(255,255,255,.08);border-radius:3px;overflow:hidden;margin-bottom:5px">
      <div id="dvr-storage-bar" style="height:100%;width:0%;border-radius:3px;transition:width .4s"></div>
    </div>
  </div>

  <div style="flex:1;overflow-y:auto;padding:10px 12px 12px">

    <!-- Manual recording form -->
    <div style="background:var(--s2);border:1px solid var(--bdr);border-radius:var(--rsm);padding:10px 12px;margin-bottom:10px">
      <div style="font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.7px;color:var(--acc);margin-bottom:8px">⏱ Schedule Recording</div>
      <div style="display:flex;gap:6px;align-items:center;margin-bottom:7px">
        <button class="btn-ghost" id="dvr-ch-btn" onclick="dvrPickChannel()" style="height:28px;padding:0 10px;font-size:12px;flex-shrink:0">📺 Channel</button>
        <span id="dvr-ch-name" style="font-size:12px;color:var(--txt2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">No channel selected</span>
        <button class="btn-ghost" id="dvr-epg-btn" onclick="dvrOpenEpg()" disabled title="Select a channel first to browse EPG"
          style="height:28px;padding:0 10px;font-size:12px;flex-shrink:0;opacity:.35;cursor:not-allowed;transition:opacity .2s">📅 EPG</button>
      </div>
      <input type="hidden" id="dvr-ch-id">
      <input type="hidden" id="dvr-stream-url">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:7px">
        <div>
          <label style="font-size:10px;color:var(--txt3);display:block;margin-bottom:2px">Start</label>
          <input type="datetime-local" id="dvr-start" style="width:100%;height:30px;font-size:11px;padding:0 6px;background:var(--s3);border:1px solid var(--bdr2);border-radius:var(--rss);color:var(--txt)">
        </div>
        <div>
          <label style="font-size:10px;color:var(--txt3);display:block;margin-bottom:2px">End</label>
          <input type="datetime-local" id="dvr-end" style="width:100%;height:30px;font-size:11px;padding:0 6px;background:var(--s3);border:1px solid var(--bdr2);border-radius:var(--rss);color:var(--txt)">
        </div>
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn-blue" onclick="dvrScheduleManual()" style="flex:1;height:30px;font-size:12px">📅 Schedule</button>
        <button class="btn-blue" onclick="dvrRecordNow()" style="flex:1;height:30px;font-size:12px;background:linear-gradient(135deg,#dc2626,#991b1b)">⏺ Record Now</button>
      </div>
    </div>

    <!-- Scheduled / in-progress jobs -->
    <div style="font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.7px;color:var(--txt3);margin-bottom:5px;display:flex;justify-content:space-between;align-items:center">
      <span>Scheduled &amp; Recording</span>
      <button class="btn-ghost" onclick="dvrClearJobs()" style="height:22px;padding:0 7px;font-size:10px;opacity:.7">Clear All</button>
    </div>
    <div id="dvr-jobs-empty" style="text-align:center;padding:14px;color:var(--txt3);font-size:12px;display:none">No scheduled recordings</div>
    <div id="dvr-jobs-list" style="display:flex;flex-direction:column;gap:4px;margin-bottom:12px"></div>

    <!-- Completed recordings -->
    <div style="font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.7px;color:var(--txt3);margin-bottom:5px;display:flex;justify-content:space-between;align-items:center">
      <span>Completed Recordings</span>
      <button class="btn-ghost" onclick="dvrClearRecordings()" style="height:22px;padding:0 7px;font-size:10px;opacity:.7">Clear All</button>
    </div>
    <div id="dvr-recs-empty" style="text-align:center;padding:14px;color:var(--txt3);font-size:12px;display:none">No completed recordings</div>
    <div id="dvr-recs-list" style="display:flex;flex-direction:column;gap:4px"></div>

  </div>
</div>
</div><!-- /dvr-overlay -->
`;
  while(d.firstChild) document.body.appendChild(d.firstChild);
})();

// ── DVR ────────────────────────────────────────────────────────────────────

let _dvrInited = false;
let _dvrJobs = [];
let _dvrRecs = [];

function dvrOpen(){
  if(!_DVR_OK){ toast('DVR addon (dvr_addon.py) is not installed','wrn'); return; }
  document.getElementById('dvr-overlay').style.display = 'flex';
  dvrInit();
}

function dvrClose(){
  document.getElementById('dvr-overlay').style.display = 'none';
}

async function dvrInit(){
  if(!_DVR_OK) return;
  if(_dvrInited){ dvrRefresh(); return; }
  _dvrInited = true;
  // Sync DVR output path from settings to backend on first open
  const _dvrPathEl = document.getElementById('o-dvr');
  if(_dvrPathEl && _dvrPathEl.value.trim()) await dvrSetFolder(_dvrPathEl.value.trim());
  await dvrRefresh();
  // Pre-fill start to now + 1 min rounded, end to +1 hour
  const pad = n => String(n).padStart(2,'0');
  const toLocal = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const now = new Date(); now.setSeconds(0,0); now.setMinutes(now.getMinutes()+1);
  const end = new Date(now.getTime() + 60*60*1000);
  document.getElementById('dvr-start').value = toLocal(now);
  document.getElementById('dvr-end').value   = toLocal(end);
}

async function dvrRefresh(){
  try{
    const overlayOpen = document.getElementById('dvr-overlay')?.style.display==='flex';
    // Only fetch storage when the modal is open — it's a disk syscall and the
    // result is cached 60s on the backend anyway, so no need to hit it from
    // background badge polls where the storage bar isn't even visible.
    const fetches = [
      fetch('/api/dvr/jobs').then(r=>r.json()),
      fetch('/api/dvr/recordings').then(r=>r.json()),
      overlayOpen ? fetch('/api/dvr/storage?path='+encodeURIComponent(document.getElementById('o-dvr')?.value||'')).then(r=>r.json()) : Promise.resolve(null),
    ];
    const [jr, rr, sr] = await Promise.all(fetches);
    _dvrJobs = Array.isArray(jr) ? jr : [];
    _dvrRecs = Array.isArray(rr) ? rr : [];
    _dvrRenderJobs();
    _dvrRenderRecs();
    if(sr) _dvrRenderStorage(sr);
    _dvrBadgeUpdate();
  }catch(e){ toast('DVR: could not load data','err'); }
}

function _dvrFmt(iso){
  if(!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
}
function _dvrFmtBytes(b){
  if(!b||b===0) return '';
  const k=1024, sizes=['B','KB','MB','GB'];
  const i=Math.floor(Math.log(b)/Math.log(k));
  return (b/Math.pow(k,i)).toFixed(1)+' '+sizes[i];
}
function _dvrFmtDur(s){
  if(!s) return '';
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60);
  return h>0?`${h}h ${m}m`:`${m}m`;
}

function _dvrRenderJobs(){
  const el = document.getElementById('dvr-jobs-list');
  const em = document.getElementById('dvr-jobs-empty');
  if(!_dvrJobs.length){ el.innerHTML=''; em.style.display=''; return; }
  em.style.display='none';
  el.innerHTML = _dvrJobs.map(j=>{
    const isRec    = j.status==='recording';
    const isSched  = j.status==='scheduled';
    const canStop  = isRec;
    const canEdit  = j.status==='scheduled';
    const canCancel= j.status==='scheduled';
    const canRemove= ['error','cancelled','completed'].includes(j.status);
    // Progress bar for recording jobs — filled by _dvrPollProgress
    // Open-ended (Record Now) jobs show elapsed time only, no total or percentage.
    const isOpenEnded = !!j.openEnded;
    const progressHtml = isRec ? `
      <div style="margin-top:5px">
        <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--txt3);margin-bottom:2px">
          <span id="dvr-prog-time-${esc(j.id)}">${isOpenEnded ? 'Recording…' : 'Recording…'}</span>
          <span id="dvr-prog-size-${esc(j.id)}"></span>
        </div>
        <div style="height:3px;background:rgba(255,255,255,.1);border-radius:2px;overflow:hidden">
          <div id="dvr-prog-bar-${esc(j.id)}" style="height:100%;width:${isOpenEnded?'100':'0'}%;background:${isOpenEnded?'var(--acc)':'#f87171'};border-radius:2px;${isOpenEnded?'':'transition:width .5s'}"></div>
        </div>
      </div>` : '';
    // Countdown for scheduled jobs — ticked by _dvrTickCountdowns every second
    const countdownHtml = isSched ? `
      <div style="margin-top:4px;display:flex;align-items:center;gap:6px">
        <div style="height:3px;flex:1;background:rgba(255,255,255,.08);border-radius:2px;overflow:hidden">
          <div id="dvr-cd-bar-${esc(j.id)}" style="height:100%;width:100%;background:var(--acc);border-radius:2px;transition:width 1s linear"></div>
        </div>
        <span id="dvr-cd-${esc(j.id)}" data-start="${esc(j.startTime)}"
          style="font-size:10px;color:var(--acc);font-weight:700;white-space:nowrap;min-width:52px;text-align:right">…</span>
      </div>` : '';
    return `<div class="dvr-card" data-job="${esc(j.id)}">
      <div class="dvr-card-top">
        <span class="dvr-card-title">${esc(j.programTitle||'Recording')}</span>
        <span class="dvr-badge ${j.status}">${j.status}</span>
      </div>
      <div class="dvr-card-ch">${esc(j.channelName||'')}</div>
      <div class="dvr-card-meta">
        <span class="dvr-card-time">${_dvrFmt(j.startTime)} – ${_dvrFmt(j.endTime)}</span>
      </div>
      ${progressHtml}${countdownHtml}
      ${j.status==='error'&&j.errorMessage?`<div style="font-size:10px;color:#fca5a5;margin-top:2px">${esc(j.errorMessage)}</div>`:''}
      <div class="dvr-card-btns">
        ${isRec && j.filePath?`<button class="btn-blue" onclick="dvrTimeshift('${esc(j.id)}')" style="background:rgba(59,130,246,.25);color:#60a5fa">▶ Watch</button>`:
          isRec?`<button class="btn-ghost" disabled style="opacity:.4;font-size:10px">Starting…</button>`:''}
        ${canStop?`<button class="btn-ghost" style="color:#f87171" onclick="dvrStopJob('${esc(j.id)}')">⏹ Stop</button>`:''}
        ${canEdit?`<button class="btn-ghost" onclick="dvrEditJob('${esc(j.id)}')">✏</button>`:''}
        ${canCancel?`<button class="btn-ghost" style="color:#f87171" onclick="dvrCancelJob('${esc(j.id)}')">✕</button>`:''}
        ${canRemove?`<button class="btn-ghost" onclick="dvrRemoveHistory('${esc(j.id)}')">🗑</button>`:''}
      </div>
    </div>`;
  }).join('');
  // Start ticking progress for recording jobs
  _dvrPollProgress();
  // Start countdown ticker for scheduled jobs
  _dvrTickCountdowns();
}

// Poll /api/dvr/progress every 3s while DVR overlay is open and a recording is active
let _dvrProgressTimer    = null;
let _dvrRefreshPending   = false;
const _dvrEndFired = new Set(); // jobs with a one-shot end-refresh already scheduled
async function _dvrPollProgress(){
  clearTimeout(_dvrProgressTimer);
  const activeJobs = _dvrJobs.filter(j=>j.status==='recording');
  if(!activeJobs.length || document.getElementById('dvr-overlay')?.style.display!=='flex') return;
  try{
    const prog = await fetch('/api/dvr/progress').then(r=>r.json());

    // PRIMARY signal: job gone from progress → backend already marked completed.
    // Trigger a refresh after a short settle delay.
    const missingIds = activeJobs.map(j=>j.id).filter(id=>!(id in prog));
    if(missingIds.length && !_dvrRefreshPending){
      _dvrRefreshPending = true;
      setTimeout(()=>{ _dvrRefreshPending=false; dvrRefresh(); }, 1500);
    }

    for(const [id, p] of Object.entries(prog)){
      const elapsed   = p.elapsedSeconds||0;
      const total     = p.totalSeconds||0;
      const size      = p.fileSizeBytes||0;
      const isOpenEnd = !!p.openEnded;

      const h=Math.floor(elapsed/3600), m=Math.floor((elapsed%3600)/60), s=elapsed%60;
      const elapsedStr = (h>0?`${h}h `:'') + `${String(m).padStart(2,'0')}m ${String(s).padStart(2,'0')}s`;

      const t   = document.getElementById(`dvr-prog-time-${id}`);
      const sz  = document.getElementById(`dvr-prog-size-${id}`);
      const bar = document.getElementById(`dvr-prog-bar-${id}`);

      if(isOpenEnd){
        // Open-ended: show elapsed only, bar stays solid accent
        if(t)  t.textContent  = elapsedStr;
        if(sz) sz.textContent = size ? _dvrFmtBytes(size) : '';
      } else {
        // Scheduled: cap at total, show percentage
        const dispElapsed = total>0 ? Math.min(elapsed, total) : elapsed;
        const dh=Math.floor(dispElapsed/3600), dm=Math.floor((dispElapsed%3600)/60), ds=dispElapsed%60;
        const timeStr = (dh>0?`${dh}h `:'') + `${String(dm).padStart(2,'0')}m ${String(ds).padStart(2,'0')}s`;
        const pct = total>0 ? Math.min(100, Math.round(dispElapsed/total*100)) : 0;
        if(t)  t.textContent  = `${timeStr} / ${_dvrFmtDur(total)} (${pct}%)`;
        if(sz) sz.textContent = size ? _dvrFmtBytes(size) : '';
        if(bar)bar.style.width= pct+'%';

        // FALLBACK signal: schedule exactly ONE refresh per job at 100%.
        // No loop — fires once at 8s (covers 5s backend tick + buffer).
        // The primary missingIds check handles it faster when backend updates.
        if(pct>=100 && !_dvrEndFired.has(id)){
          _dvrEndFired.add(id);
          setTimeout(()=>{
            _dvrEndFired.delete(id);
            if(!_dvrRefreshPending){
              _dvrRefreshPending = true;
              dvrRefresh().finally(()=>{ _dvrRefreshPending=false; });
            }
          }, 8000);
        }
      }
    }
  }catch(e){}
  // 5s poll — light on CPU, still responsive enough for elapsed display
  _dvrProgressTimer = setTimeout(_dvrPollProgress, 5000);
}

// ── Countdown ticker for scheduled jobs ───────────────────────────────────
let _dvrCdTimer = null;
function _dvrTickCountdowns(){
  clearTimeout(_dvrCdTimer);
  const spans = document.querySelectorAll('[id^="dvr-cd-"]');
  if(!spans.length) return;
  const now = Date.now();
  let minDiffSec = Infinity;
  spans.forEach(el => {
    const startIso = el.dataset.start;
    if(!startIso) return;
    const startMs = new Date(startIso).getTime();
    const diffMs  = startMs - now;
    const bar = document.getElementById('dvr-cd-bar-' + el.id.slice(7));
    if(diffMs <= 0){
      el.textContent = 'Starting…';
      el.style.color = '#4ade80';
      if(bar) bar.style.width = '0%';
      return;
    }
    const totalSec = diffMs / 1000;
    if(totalSec < minDiffSec) minDiffSec = totalSec;
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = Math.floor(totalSec % 60);
    if(h > 0)       el.textContent = `${h}h ${String(m).padStart(2,'0')}m`;
    else if(m > 0)  el.textContent = `${String(m).padStart(2,'0')}m ${String(s).padStart(2,'0')}s`;
    else            el.textContent = `${s}s`;
    // urgency colour
    el.style.color = totalSec < 300 ? '#f87171' : totalSec < 3600 ? '#fbbf24' : 'var(--acc)';
    // shrink bar: show proportion of time elapsed toward 24h cap
    if(bar){
      const capSec = Math.min(86400, totalSec + 1);
      bar.style.width = Math.max(0, (totalSec / capSec * 100)).toFixed(1) + '%';
    }
  });
  // Tick every 1s when any job is starting in <5 min, else every 10s.
  // Avoids 60 DOM reads/writes per minute when recordings are hours away.
  const nextTick = minDiffSec < 300 ? 1000 : 10000;
  _dvrCdTimer = setTimeout(_dvrTickCountdowns, nextTick);
}

function _dvrRenderRecs(){
  const el = document.getElementById('dvr-recs-list');
  const em = document.getElementById('dvr-recs-empty');
  if(!_dvrRecs.length){ el.innerHTML=''; em.style.display=''; return; }
  em.style.display='none';
  el.innerHTML = _dvrRecs.map(r=>{
    const meta = [_dvrFmtDur(r.durationSeconds), _dvrFmtBytes(r.fileSizeBytes)].filter(Boolean).join(' · ');
    return `<div class="dvr-card" data-rec="${esc(r.id)}">
      <div class="dvr-card-top">
        <span class="dvr-card-title">${esc(r.programTitle||r.filename||'Recording')}</span>
        <span class="dvr-badge completed">saved</span>
      </div>
      <div class="dvr-card-ch">${esc(r.channelName||'')}</div>
      <div class="dvr-card-meta">
        <span class="dvr-card-time">${_dvrFmt(r.startTime)}</span>
        ${meta?`<span class="dvr-card-time">· ${esc(meta)}</span>`:''}
      </div>
      <div class="dvr-card-btns">
        <button class="btn-blue" onclick="dvrPlayRec('${esc(r.id)}')" style="font-size:10px">▶ Play</button>
        <button class="btn-ghost" onclick="dvrRevealRec('${esc(r.id)}')" title="Show in folder" style="font-size:11px">📂</button>
        <button class="btn-ghost" style="color:#f87171" onclick="dvrDeleteRec('${esc(r.id)}')">🗑</button>
      </div>
    </div>`;
  }).join('');
}

function _dvrRenderStorage(s){
  if(!s||s.error) return;
  const pct = s.percentage||0;
  const bar = document.getElementById('dvr-storage-bar');
  bar.style.width = pct+'%';
  bar.style.background = pct>90?'#dc2626':pct>75?'#ca8a04':'var(--acc)';
  document.getElementById('dvr-storage-text').textContent =
    `${_dvrFmtBytes(s.used)} of ${_dvrFmtBytes(s.total)} used`;
}

function _dvrBadgeUpdate(){
  const recording  = _dvrJobs.filter(j=>j.status==='recording');
  const scheduled  = _dvrJobs.filter(j=>j.status==='scheduled');
  const active     = recording.length;
  // Modal header badge
  const badge = document.getElementById('dvr-badge');
  if(badge){ badge.textContent=active; badge.style.display=active?'':'none'; }
  // Actions drawer badge
  const badgeAdr = document.getElementById('dvr-badge-adr');
  if(badgeAdr){ badgeAdr.textContent=active; badgeAdr.style.display=active?'':'none'; }
  // Actions drawer status line
  const statusEl = document.getElementById('dvr-adr-status');
  if(statusEl){
    if(recording.length){
      statusEl.textContent = `⏺ Recording: ${recording.map(j=>j.programTitle||j.channelName).join(', ')}`.slice(0,55);
      statusEl.style.display='';
    } else if(scheduled.length){
      statusEl.textContent = `${scheduled.length} recording${scheduled.length>1?'s':''} scheduled`;
      statusEl.style.color = 'var(--txt3)';
      statusEl.style.display='';
    } else {
      statusEl.style.display='none';
    }
  }
}

// Poll DVR badge every 20s when modal is closed — but only when there are
// active or scheduled jobs worth tracking. When idle, skip the fetch entirely.
setInterval(async ()=>{
  if(!_DVR_OK || !_dvrInited) return;
  if(document.getElementById('dvr-overlay')?.style.display==='flex') return;
  // No active/scheduled jobs → nothing to update, skip the round-trip
  if(!_dvrJobs.some(j=>j.status==='recording'||j.status==='scheduled')) return;
  try{
    const j = await fetch('/api/dvr/jobs').then(r=>r.json());
    if(Array.isArray(j)){ _dvrJobs=j; _dvrBadgeUpdate(); }
  }catch(e){}
}, 20000);

// ── Storage folder picker ──────────────────────────────────────────────────
async function dvrPickFolder(){
  // Desktop: try tkinter picker; mobile/fallback: shared output file browser
  try{
    const r = await fetch('/api/browse_folder');
    const d = await r.json();
    if(d.path){ await dvrSetFolder(d.path); dvrRefresh(); return; }
    if(d.error) throw new Error(d.error);
  } catch(e){
    if(typeof outFbSetTarget === 'function'){
      _outFbMobileMode = true;
      _outFbOpen = true;
      if(typeof _outFbApplyState === 'function') _outFbApplyState();
      outFbSetTarget('dvr');
    }
  }
}

async function dvrSetFolder(path){
  try{
    await fetch('/api/dvr/set_folder',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({folder: path}),
    });
  } catch(e){}
  // Also keep the settings o-dvr field in sync
  const el = document.getElementById('o-dvr');
  if(el && path) el.value = path;
  if(typeof saveFP === 'function') saveFP();
}

let _dvrPickedItem = null;  // full item object from channel selector
let _dvrEpgTitle   = '';    // programme title selected from DVR EPG

function dvrPickChannel(){
  if(typeof _mvSelOpen === 'function'){
    _mvSelOpen(ch => {
      _dvrPickedItem = ch;
      _dvrEpgTitle   = '';     // reset any previously selected EPG title
      document.getElementById('dvr-ch-name').textContent = ch.name||'?';
      document.getElementById('dvr-ch-id').value = ch.id||ch.stream_id||'';
      document.getElementById('dvr-stream-url').value = '';
      // Enable the EPG button now that a channel is selected
      const btn = document.getElementById('dvr-epg-btn');
      if(btn){ btn.disabled=false; btn.style.opacity='1'; btn.style.cursor='pointer'; btn.title='Browse EPG to auto-fill schedule times'; }
    }, 'live');  // DVR only needs live channels
  } else {
    toast('Connect to a portal first to pick a channel','wrn');
  }
}

// ── DVR EPG browser ───────────────────────────────────────────────────────
let _dvrEpgRetryTimer = null;
async function dvrOpenEpg(isRetry){
  if(!_dvrPickedItem){ toast('Pick a channel first','wrn'); return; }
  const ov = document.getElementById('dvr-epg-overlay');
  document.getElementById('dvr-epg-ch-name').textContent = _dvrPickedItem.name || 'EPG';
  // On first open show the loading spinner and make overlay visible.
  // On auto-retries keep the overlay open and just update the body.
  if(!isRetry){
    clearTimeout(_dvrEpgRetryTimer);
    document.getElementById('dvr-epg-body').innerHTML =
      '<div style="color:var(--txt3);font-size:12px;text-align:center;padding:20px">Loading\u2026</div>';
    ov.style.display = 'flex';
  }
  try{
    const r = await fetch('/api/epg', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({item: _dvrPickedItem})});
    const d = await r.json();
    const schedule = d.schedule || [];
    // Normalise to a flat programme array (schedule > current+next fallback)
    let programs = schedule;
    if(!programs.length){
      programs = [d.current, d.next].filter(Boolean).map(p =>
        ({title:p.title, start:p.start, end:p.end, desc:p.desc||''}));
    }
    if(!programs.length){
      const errMsg = (d.error || '').toLowerCase();
      const isLoading = errMsg.includes('loading') || errMsg.includes('please try again');
      if(isLoading){
        // External EPG is still downloading/decompressing — show feedback and auto-retry
        document.getElementById('dvr-epg-body').innerHTML =
          '<div style="color:var(--txt3);font-size:12px;text-align:center;padding:20px">' +
          '\u23f3 External EPG is loading, please wait\u2026' +
          '<br><span style="font-size:11px;opacity:.7">Retrying automatically in 5 seconds\u2026</span></div>';
        _dvrEpgRetryTimer = setTimeout(()=>dvrOpenEpg(true), 5000);
      } else {
        document.getElementById('dvr-epg-body').innerHTML =
          '<div style="color:var(--txt3);font-size:12px;text-align:center;padding:20px">' +
          (d.error || 'No EPG data available for this channel') + '</div>';
      }
      return;
    }
    const now = Date.now() / 1000;
    const rows = programs.map(p => {
      if(!p) return '';
      const isCurrent = p.start <= now && now < p.end;
      const startStr  = _fmtEpgTime(p.start);
      const endStr    = _fmtEpgTime(p.end);
      const startIso  = new Date(p.start * 1000).toISOString();
      const endIso    = new Date(p.end   * 1000).toISOString();
      const safeTitle = esc(p.title || 'Recording');
      const rawTitle  = (p.title || 'Recording').replace(/\\/g,'\\\\').replace(/'/g,"\\'");
      const descHtml  = p.desc
        ? `<div style="font-size:11px;color:var(--txt3);margin-top:3px;line-height:1.4">${esc(p.desc).slice(0,140)}${p.desc.length>140?'\u2026':''}</div>`
        : '';
      const dot = isCurrent ? '<span style="color:var(--acc);margin-right:4px">\u25b8</span>' : '';
      return `<div onclick="dvrSelectEpgProgram('${startIso}','${endIso}','${rawTitle}')"
        style="background:${isCurrent?'var(--s3)':'transparent'};border-radius:var(--rsm);
               padding:8px 10px;margin-bottom:4px;cursor:pointer;
               border-left:2px solid ${isCurrent?'var(--acc)':'transparent'};transition:background .1s"
        onmouseover="this.style.background='var(--s3)'"
        onmouseout="this.style.background='${isCurrent?'var(--s3)':'transparent'}'">
        <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">
          <span style="font-size:11px;color:var(--acc);white-space:nowrap;min-width:90px">${startStr}${endStr?' \u2013 '+endStr:''}</span>
          <span style="font-size:13px;font-weight:${isCurrent?700:400};color:var(--txt1)">${dot}${safeTitle}</span>
        </div>${descHtml}
      </div>`;
    }).join('');
    document.getElementById('dvr-epg-body').innerHTML = rows ||
      '<div style="color:var(--txt3);font-size:12px;text-align:center;padding:20px">No programmes found</div>';
    // Scroll current programme into view
    const cur = document.querySelector('#dvr-epg-body [style*="var(--s3)"]');
    if(cur) setTimeout(()=>cur.scrollIntoView({block:'center'}), 50);
  }catch(e){
    document.getElementById('dvr-epg-body').innerHTML =
      `<div style="color:var(--err);font-size:12px;text-align:center;padding:20px">Failed: ${esc(String(e))}</div>`;
  }
}

function dvrSelectEpgProgram(startIso, endIso, title){
  const pad = n => String(n).padStart(2,'0');
  const toLocal = iso => {
    const d = new Date(iso);
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  document.getElementById('dvr-start').value = toLocal(startIso);
  document.getElementById('dvr-end').value   = toLocal(endIso);
  _dvrEpgTitle = title;   // saved for use as programTitle when scheduling
  document.getElementById('dvr-epg-overlay').style.display = 'none';
  toast(`"\u201c${title.slice(0,40)}\u201d \u2014 times set \u2713`,'ok');
}

// ── Schedule manual ────────────────────────────────────────────────────────
async function dvrScheduleManual(){
  const chId     = document.getElementById('dvr-ch-id').value.trim();
  const chName   = document.getElementById('dvr-ch-name').textContent.trim();
  const startVal = document.getElementById('dvr-start').value;
  const endVal   = document.getElementById('dvr-end').value;

  if(!chId)    { toast('Pick a channel first','wrn'); return; }
  if(!startVal || !endVal){ toast('Set start and end time','wrn'); return; }
  if(new Date(endVal) <= new Date(startVal)){ toast('End must be after start','wrn'); return; }

  // Resolve stream URL — use full picked item so portal client can look it up
  let url = '';
  try{
    const item = _dvrPickedItem || {id:chId, name:chName};
    const cat  = (typeof curCat !== 'undefined' && curCat) ? curCat : {};
    const r = await fetch('/api/resolve', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({item, mode:'live', category: cat})
    });
    const d = await r.json();
    url = d.url||'';
    if(url.includes('/api/hls_proxy')){
      try{ const p=new URLSearchParams(url.split('?')[1]||''); url=p.get('url')||url; }catch(e){}
    }
  }catch(e){ toast('Resolve error: '+e,'err'); return; }

  const body = {
    channelId: chId, channelName: chName, streamUrl: url,
    startTime: new Date(startVal).toISOString(),
    endTime:   new Date(endVal).toISOString(),
    ...(_dvrEpgTitle ? {programTitle: _dvrEpgTitle} : {}),
    channelItem: _dvrPickedItem || {},
  };
  try{
    const r = await fetch('/api/dvr/schedule/manual',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if(r.ok){
      toast('Recording scheduled ✓','ok');
      dvrRefresh();
    } else {
      const d = await r.json();
      toast(d.error||'Schedule failed','err');
    }
  }catch(e){ toast('Schedule error: '+e,'err'); }
}

// ── Record Now ────────────────────────────────────────────────────────────
async function dvrRecordNow(){
  const chId  = document.getElementById('dvr-ch-id').value.trim();
  const chName= document.getElementById('dvr-ch-name').textContent.trim();

  if(!chId){ toast('Pick a channel first','wrn'); return; }

  toast('Resolving stream URL…','info');
  let url = '';
  try{
    // Use the full picked item object so the portal client can look it up correctly
    const item = _dvrPickedItem || {id:chId, name:chName};
    const cat  = (typeof curCat !== 'undefined' && curCat) ? curCat : {};
    const r = await fetch('/api/resolve', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({item, mode:'live', category: cat})
    });
    const d = await r.json();
    url = d.url||'';
    // Strip hls_proxy wrapper — DVR ffmpeg records the raw portal stream directly.
    if(url.includes('/api/hls_proxy')){
      try{
        const params = new URLSearchParams(url.split('?')[1]||'');
        url = params.get('url') || url;
      }catch(e){}
    }
  }catch(e){ toast('Resolve error: '+e,'err'); return; }

  if(!url){ toast('Could not resolve stream URL','err'); return; }

  // Record Now is open-ended: runs until the user hits Stop.
  // Never read the schedule form fields — they belong to the manual scheduler,
  // not to Record Now. Use a 12-hour ceiling so ffmpeg has a hard stop in case
  // the app crashes, but the UI treats the job as having no fixed end time.
  const body = { channelId:chId, channelName:chName, streamUrl:url, title:chName,
                 durationMinutes: 720, openEnded: true, channelItem: _dvrPickedItem || {} };
  try{
    const r = await fetch('/api/dvr/record_now',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if(r.ok){
      toast('⏺ Recording started!','ok');
      dvrRefresh();
    } else {
      const d = await r.json();
      toast(d.error||'Failed to start recording','err');
    }
  }catch(e){ toast('Record error: '+e,'err'); }
}

// ── Job actions ────────────────────────────────────────────────────────────
async function dvrCancelJob(id){
  _mvConfirm('Cancel Recording', 'Cancel this scheduled recording?', async ()=>{
    await fetch(`/api/dvr/jobs/${id}`,{method:'DELETE'});
    toast('Recording cancelled','ok');
    dvrRefresh();
  });
}

async function dvrStopJob(id){
  const job = _dvrJobs.find(j=>j.id===id);
  const name = job ? (job.programTitle||job.channelName||'recording') : 'recording';
  _mvConfirm('Stop Recording', `Stop recording "${name}" now?`, async ()=>{
    await fetch(`/api/dvr/jobs/${id}/stop`,{method:'POST'});
    toast('Recording stopped','ok');
    dvrRefresh();
  });
}

async function dvrRemoveHistory(id){
  await fetch(`/api/dvr/jobs/${id}/history`,{method:'DELETE'});
  toast('Removed from history','ok');
  dvrRefresh();
}

function dvrEditJob(id){
  const job = _dvrJobs.find(j=>j.id===id);
  if(!job) return;
  const pad = n=>String(n).padStart(2,'0');
  const toLocal = iso => {
    const d = new Date(iso);
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  document.getElementById('dvr-start').value = toLocal(job.startTime);
  document.getElementById('dvr-end').value   = toLocal(job.endTime);
  document.getElementById('dvr-ch-name').textContent = job.channelName||'';
  document.getElementById('dvr-ch-id').value = job.channelId||'';
  toast('Times loaded — adjust and click Schedule to save','info');
  _dvrEditingId = id;
}

let _dvrEditingId = null;

async function dvrClearJobs(){
  _mvConfirm('Clear All Jobs', 'Remove all scheduled jobs from history? This will not delete recorded files.', async ()=>{
    await fetch('/api/dvr/jobs/all',{method:'DELETE'});
    toast('Jobs cleared','ok');
    dvrRefresh();
  });
}

// ── Recording actions ──────────────────────────────────────────────────────
async function dvrDeleteRec(id){
  const rec = _dvrRecs.find(r=>r.id===id);
  const name = rec ? (rec.programTitle||rec.filename||'recording') : 'recording';
  _mvConfirm('Delete Recording', `Permanently delete "${name}"?`, async ()=>{
    await fetch(`/api/dvr/recordings/${id}`,{method:'DELETE'});
    toast('Recording deleted','ok');
    dvrRefresh();
  });
}

async function dvrClearRecordings(){
  _mvConfirm('Delete All Recordings', 'Permanently delete ALL completed recording files?', async ()=>{
    await fetch('/api/dvr/recordings/all',{method:'DELETE'});
    toast('All recordings deleted','ok');
    dvrRefresh();
  });
}

async function dvrRevealRec(id){
  const rec = _dvrRecs.find(r=>r.id===id);
  if(!rec||!rec.filePath) return;
  if(_isMobile){ toast('Show in folder is a desktop feature','wrn'); return; }
  try{
    const r = await fetch('/api/reveal_in_folder',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: rec.filePath})});
    const d = await r.json();
    if(d.error) toast(d.error,'err');
  }catch(e){ toast('Could not open folder: '+e,'err'); }
}

// ── Playback ───────────────────────────────────────────────────────────────
async function dvrPlayRec(id){
  const rec = _dvrRecs.find(r=>r.id===id);
  if(!rec) return;
  const name = rec.programTitle||rec.filename||'Recording';
  dvrClose();

  // Try playing the served .ts file first. If the browser hits a MSE/codec error
  // (HEVC) the normal doPlay escalation path will try hls_proxy with the same
  // local URL — but ffmpeg can't open a local Flask URL. So we pre-check: if the
  // file path ends with .ts we use the dedicated transcode endpoint which reads
  // the file directly from disk via ffmpeg. This gives us H.264+AAC output that
  // the browser can always play.
  // For non-HEVC recordings the transcode is slightly wasteful but the only
  // reliable way to handle HEVC transparently without a separate probe round-trip.
  const transcodeUrl = `/api/dvr/transcode/${encodeURIComponent(id)}`;
  const serveUrl     = `/api/dvr/serve/${encodeURIComponent(rec.filename)}`;

  // Use serve (direct) if content is likely not HEVC, transcode otherwise.
  // Heuristic: recordings from MAC/Xtream portals that were HEVC will have
  // been recorded as-is (no re-encoding during recording). We check if the
  // job's original stream URL is known to be HEVC by checking the filename
  // suffix — all our recordings are .ts so we always use the transcode path
  // to be safe. Future improvement: store a hevc flag in the job.
  alog('[DVR] Playing recording via transcode: '+name,'k');
  doPlay(transcodeUrl, name, {isLive: false});
}

async function dvrTimeshift(id){
  const job = _dvrJobs.find(j=>j.id===id);
  if(!job) return;
  const name = (job.programTitle||job.channelName||'Recording')+' (Recording…)';
  dvrClose();

  const url = `/api/dvr/timeshift/${encodeURIComponent(id)}`;

  // Poll until the recording file exists on disk (ffmpeg may take a few seconds
  // to create it after the job starts). Check every second for up to 15s.
  let ready = false;
  for(let i = 0; i < 15; i++){
    try{
      const probe = await fetch(url, {method:'HEAD'});
      if(probe.ok){ ready = true; break; }
    }catch(e){}
    if(i === 0) toast('Waiting for recording to start…','info');
    await new Promise(r=>setTimeout(r, 1000));
  }

  if(!ready){ toast('Recording file not ready — try again in a moment','err'); return; }

  alog('[DVR] Timeshifting via recording file: '+name,'k');
  doPlay(url, name, {isLive: false});
}

// ── Auto-refresh DVR while overlay is open ────────────────────────────────
// 15s when a recording is active (need live size/status updates),
// 60s when idle (scheduled jobs or empty — no urgency).
let _dvrAutoRefreshTimer = null;
function _dvrScheduleAutoRefresh(){
  clearTimeout(_dvrAutoRefreshTimer);
  const interval = _dvrJobs.some(j=>j.status==='recording') ? 15000 : 60000;
  _dvrAutoRefreshTimer = setTimeout(async ()=>{
    if(_dvrInited && document.getElementById('dvr-overlay')?.style.display==='flex'){
      await dvrRefresh();
    }
    _dvrScheduleAutoRefresh();
  }, interval);
}
// Kick off after first successful init
const _origDvrRefresh = dvrRefresh;
dvrRefresh = async function(){
  await _origDvrRefresh();
  _dvrScheduleAutoRefresh();
};

// ── DVR overlay backdrop// ── DVR overlay backdrop click ────────────────────────────────────────────
  const _dvrOvl = document.getElementById('dvr-overlay');
  if(_dvrOvl){
    _dvrOvl.addEventListener('click', e=>{
      if(e.target === _dvrOvl) dvrClose();
    });
  }

"""
