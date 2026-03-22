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

That's it.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
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

def _get_ffmpeg() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = os.path.join(sys._MEIPASS, "ffmpeg.exe")
        if os.path.exists(bundled):
            return bundled
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        for candidate in (
            os.path.join(base, "_internal", "ffmpeg.exe"),
            os.path.join(base, "ffmpeg.exe"),
        ):
            if os.path.exists(candidate):
                return candidate
    if os.path.exists("ffmpeg.exe"):
        return os.path.abspath("ffmpeg.exe")
    return shutil.which("ffmpeg") or "ffmpeg"


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
    ffmpeg = _get_ffmpeg()
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

    cmd = [
        ffmpeg, "-hide_banner", "-nostdin",
        "-user_agent", "VLC/3.0.0 LibVLC/3.0.0",
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
        out_dir = ""
        if state:
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

        ffmpeg = _get_ffmpeg()
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

        ffmpeg = _get_ffmpeg()
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

    LOG.info("[DVR] Routes registered  (jobs_file=%s)", DVR_JOBS_FILE)
