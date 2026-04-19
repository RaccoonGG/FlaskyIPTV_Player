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
download_addon.py  —  MKV/M3U download and quick-record for FlaskyIPTV_Player_byGG.py
========================================================================================
Provides:
  /api/download/m3u   — Save current category or selection as M3U playlist file.
  /api/download/mkv   — Download selected items as MKV (ffmpeg, with yt-dlp fallback).
  /api/record/start   — Start a quick in-player recording to MKV.
  /api/record/stop    — Stop the current quick recording.
  /api/record/status  — Poll recording state and elapsed time.

Also exports as module-level functions:
  safe_filename(name)              — Sanitise a string for use as a filename.
  probe_stream_codecs(url, ...)    — ffprobe a URL and return codec/duration info.
  run_ffmpeg_download(url, ...)    — Run an ffmpeg copy-download with progress callback.
  run_yt_dlp_download(url, ...)    — Run a yt-dlp download with progress callback.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRATION  (three small changes to FlaskyIPTV_Player_byGG.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — add import after the proxy_addon import block:

    try:
        from download_addon import (
            register_download_routes,
            safe_filename, probe_stream_codecs,
            run_ffmpeg_download, run_yt_dlp_download,
        )
        _DOWNLOAD_AVAILABLE = True
    except ImportError:
        _DOWNLOAD_AVAILABLE = False
        def register_download_routes(*a, **kw): pass
        # Stubs so api_resolve and _probe_hevc still compile:
        def safe_filename(name): return name[:200]
        def probe_stream_codecs(*a, **kw): return None
        def run_ffmpeg_download(*a, **kw): return 1
        def run_yt_dlp_download(*a, **kw): return False, "unavailable"

STEP 2 — register routes (after register_proxy_routes call):

    register_download_routes(flask_app, state, run_async, run_worker, _make_client,
                             _FFMPEG_PATH, _FFPROBE_PATH,
                             _FFMPEG_AVAILABLE, YTDLP_AVAILABLE)

STEP 3 — remove the now-duplicate definitions of safe_filename, probe_stream_codecs,
    run_ffmpeg_download, run_yt_dlp_download, and their regex helpers
    (_time_re, _bitrate_re, _size_re) from the MKV / FFMPEG HELPERS section of main,
    since they are now imported from this module.

STEP 4 — add script tag as the FIRST external script after the main inline </script>:

    <script src="/api/dl/ui.js"></script>

    It must load before mv/ui.js (which hooks _switchTab defined in this file).
"""

import contextlib
import json
import os
import re
import shutil
import string
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote as _qe

from flask import request, jsonify


# ── Progress-parsing regexes ──────────────────────────────────────────────────
_time_re    = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")
_bitrate_re = re.compile(r"bitrate=\s*([\d.]+)kbits/s")
_size_re    = re.compile(r"size=\s*(\d+)kB")


# ── Download Manager job persistence ──────────────────────────────────────────
# Mirrors dvr_addon pattern: JSON file on disk, in-memory cache, dirty flag.

DLM_JOBS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dlm_jobs.json")

_dlm_jobs_lock  = threading.Lock()
_dlm_jobs_cache = None   # type: ignore
_dlm_jobs_dirty = True


def _dlm_load_jobs():
    global _dlm_jobs_cache, _dlm_jobs_dirty
    if not _dlm_jobs_dirty and _dlm_jobs_cache is not None:
        return _dlm_jobs_cache
    if not os.path.exists(DLM_JOBS_FILE):
        _dlm_jobs_cache = []
        _dlm_jobs_dirty = False
        return _dlm_jobs_cache
    try:
        with open(DLM_JOBS_FILE, "r", encoding="utf-8") as fh:
            _dlm_jobs_cache = json.load(fh).get("jobs", [])
        _dlm_jobs_dirty = False
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("[DLM] Failed to load jobs: %s", exc)
        _dlm_jobs_cache = []
    return _dlm_jobs_cache


def _dlm_save_jobs(jobs):
    global _dlm_jobs_cache, _dlm_jobs_dirty
    try:
        with open(DLM_JOBS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"jobs": jobs}, fh, indent=2, ensure_ascii=False)
        _dlm_jobs_cache = jobs
        _dlm_jobs_dirty = False
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("[DLM] Failed to save jobs: %s", exc)
        _dlm_jobs_dirty = True


def _dlm_add_job(job_id, job_type, name, file_path, started_at=None):
    """Register a new in-progress job."""
    t = started_at or time.time()
    job = {
        "id":            job_id,
        "type":          job_type,   # "recording" | "mkv"
        "name":          name,
        "status":        "in_progress",
        "filePath":      file_path,
        "filename":      os.path.basename(file_path),
        "fileSizeBytes": 0,
        "startedAt":     datetime.fromtimestamp(t, tz=timezone.utc).isoformat(),
        "completedAt":   "",
        "errorMessage":  "",
    }
    with _dlm_jobs_lock:
        jobs = _dlm_load_jobs()
        jobs.append(job)
        _dlm_save_jobs(jobs)


def _dlm_complete_job(job_id, file_path=None):
    """Mark a job completed, update file size and path."""
    updates = {
        "status":      "completed",
        "completedAt": datetime.now(tz=timezone.utc).isoformat(),
    }
    fp = file_path or ""
    if fp and os.path.exists(fp):
        try:
            updates["fileSizeBytes"] = os.path.getsize(fp)
            updates["filePath"]      = fp
            updates["filename"]      = os.path.basename(fp)
        except Exception:
            pass
    with _dlm_jobs_lock:
        jobs = _dlm_load_jobs()
        for j in jobs:
            if j["id"] == job_id:
                j.update(updates)
                break
        _dlm_save_jobs(jobs)


def _dlm_error_job(job_id, message=""):
    with _dlm_jobs_lock:
        jobs = _dlm_load_jobs()
        for j in jobs:
            if j["id"] == job_id:
                j["status"]       = "error"
                j["completedAt"]  = datetime.now(tz=timezone.utc).isoformat()
                j["errorMessage"] = message
                break
        _dlm_save_jobs(jobs)


# ── Filename sanitiser ────────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    valid   = "-_.() %s%s" % (string.ascii_letters, string.digits)
    cleaned = "".join(c if c in valid else "_" for c in name).strip()
    if not cleaned:
        cleaned = "stream"
    return cleaned[:200]


# ── ffprobe codec inspector ───────────────────────────────────────────────────

def probe_stream_codecs(url: str, pre_input_args=None, timeout=15,
                        ffprobe_path="ffprobe"):
    cmd = [ffprobe_path, "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format"]
    if pre_input_args:
        cmd = [ffprobe_path, "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format"] + pre_input_args + ["-i", url]
    else:
        cmd += ["-i", url]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout)
        if proc.returncode != 0:
            return None
        data    = json.loads(proc.stdout)
        streams = data.get("streams", [])
        result  = {"audio": [], "video": [], "subtitle": [], "duration": None}
        for s in streams:
            typ   = s.get("codec_type")
            codec = s.get("codec_name")
            if typ == "audio" and codec:
                result["audio"].append(codec)
            elif typ == "video" and codec:
                result["video"].append(codec)
            elif typ == "subtitle" and codec:
                result["subtitle"].append(codec)
        dur = data.get("format", {}).get("duration")
        if dur:
            try:
                result["duration"] = float(dur)
            except Exception:
                pass
        return result
    except Exception:
        return None


# ── ffmpeg copy-download ──────────────────────────────────────────────────────

def run_ffmpeg_download(url: str, out_path: str, pre_input_args=None,
                        post_input_args=None, on_progress=None,
                        stop_event: threading.Event = None, set_proc=None,
                        ffmpeg_path="ffmpeg"):
    cmd = [ffmpeg_path, "-hide_banner", "-nostdin", "-y"]
    if pre_input_args:
        cmd += pre_input_args
    cmd += ["-i", url]
    if post_input_args:
        cmd += post_input_args
    cmd += ["-c", "copy", out_path]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    if set_proc:
        try:
            set_proc(proc)
        except Exception:
            pass

    try:
        while True:
            if stop_event and stop_event.is_set():
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            line = proc.stderr.readline()
            if line == "" and proc.poll() is not None:
                break
            if line:
                if on_progress:
                    try:
                        on_progress(line)
                    except Exception:
                        pass
            else:
                time.sleep(0.01)
    except Exception:
        pass
    proc.wait()
    return proc.returncode


# ── yt-dlp fallback download ──────────────────────────────────────────────────

def run_yt_dlp_download(url: str, out_path: str,
                        stop_event: threading.Event = None,
                        on_progress=None):
    try:
        import yt_dlp
    except ImportError:
        return False, "yt-dlp not installed"

    dirn      = os.path.dirname(out_path) or "."
    item_name = os.path.splitext(os.path.basename(out_path))[0]
    work_dir  = os.path.join(dirn, f"{item_name}_ytdlp_tmp")

    def _cleanup():
        with contextlib.suppress(Exception):
            for fname in os.listdir(work_dir):
                with contextlib.suppress(Exception):
                    os.remove(os.path.join(work_dir, fname))
            with contextlib.suppress(Exception):
                os.rmdir(work_dir)

    def _progress_hook(d):
        if stop_event and stop_event.is_set():
            raise Exception("stopped")
        if d.get("status") == "downloading" and on_progress:
            try:
                on_progress(d)
            except Exception:
                pass

    try:
        os.makedirs(work_dir, exist_ok=True)
    except Exception as e:
        return False, f"Could not create temp dir: {e}"

    ydl_opts = {
        "outtmpl":        os.path.join(work_dir, "%(title)s.%(ext)s"),
        "quiet":          True,
        "no_warnings":    True,
        "noplaylist":     True,
        "format":         "best",
        "progress_hooks": [_progress_hook],
    }
    try:
        if stop_event and stop_event.is_set():
            _cleanup()
            return False, "stopped"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        if stop_event and stop_event.is_set():
            time.sleep(1.0)
            _cleanup()
            return False, "stopped"
        candidates = [f for f in os.listdir(work_dir)
                      if not f.endswith(".part") and not f.endswith(".ytdl")
                      and ".part-Frag" not in f]
        if candidates:
            src = os.path.join(work_dir, candidates[0])
            os.replace(src, out_path)
        _cleanup()
        return True, None
    except Exception as e:
        time.sleep(1.0)
        _cleanup()
        if stop_event and stop_event.is_set():
            return False, "stopped"
        return False, str(e)


# ===================== REGISTRATION =====================

def register_download_routes(flask_app, state, run_async, run_worker, _make_client,
                             ffmpeg_path, ffprobe_path,
                             ffmpeg_available, ytdlp_available):
    """Register all download and quick-record Flask routes.

    Parameters passed from main at startup so the addon doesn't need to
    re-detect ffmpeg or duplicate the async helper plumbing.
    """

    # Bind to local names so closures capture them correctly
    _FFMPEG_PATH      = ffmpeg_path
    _FFPROBE_PATH     = ffprobe_path
    _FFMPEG_AVAILABLE = ffmpeg_available
    YTDLP_AVAILABLE   = ytdlp_available

    # ── /api/download/m3u ─────────────────────────────────────────────────────
    @flask_app.route("/api/download/m3u", methods=["POST"])
    def api_download_m3u():
        data       = request.get_json(force=True)
        items      = data.get("items", None)    # None = whole category
        cat        = data.get("category", {})
        mode       = data.get("mode", "live")
        mode       = mode if mode in ("live", "vod", "series") else "live"
        out_path   = data.get("out_path", "").strip()
        total_hint = int(data.get("total_hint", 0) or 0)

        if not out_path:
            return jsonify({"error": "No output path specified"}), 400
        if state.busy:
            return jsonify({"error": "Another operation is in progress"}), 409

        state.stop_flag.clear()
        state.set_status("Downloading M3U…")

        async def worker():
            state.task_type    = "m3u"
            state.task_done    = 0
            state.task_skipped = 0
            state.task_label   = ""
            if items is not None:
                state.task_total = len(items)
            else:
                state.task_total = total_hint

            try:
                os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            except Exception:
                pass

            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    existing = f.read(10)
            except FileNotFoundError:
                existing = ""

            if not existing:
                epg_url = ""
                if state.ext_epg_url:
                    epg_url = state.ext_epg_url
                elif state.conn_type == "xtream" and state.url and state.username and state.password:
                    _base   = state.url.rstrip("/")
                    epg_url = (f"{_base}/xmltv.php"
                               f"?username={_qe(state.username, safe='')}"
                               f"&password={_qe(state.password, safe='')}")
                elif state.conn_type == "mac" and state.url:
                    epg_url = state.url.rstrip("/") + "/xmltv.php"
                elif state.conn_type == "m3u_url":
                    epg_url = getattr(state, "_tvg_url_cache", "") or ""

                if epg_url:
                    state.log(f"[M3U] Writing EPG url-tvg: {epg_url[:80]}")
                    header = f'#EXTM3U url-tvg="{epg_url}"\n'
                else:
                    header = "#EXTM3U\n"

                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(header)

            async with _make_client() as client:
                if items is None:
                    if state.stop_flag.is_set():
                        return

                    def _m3u_pcb(n, label=None):
                        state.task_done = n
                        if label:
                            state.task_label = label

                    await client.dump_category_to_file(
                        mode, cat, out_path,
                        stop_flag=state.stop_flag,
                        progress_cb=_m3u_pcb,
                    )
                else:
                    for item in items:
                        if state.stop_flag.is_set():
                            state.log("Stopped by user.")
                            break
                        name             = item.get("name") or item.get("o_name") or item.get("fname") or "?"
                        state.task_label = name
                        state.log(f"Processing: {name}")
                        await client.dump_single_item_to_file(
                            mode, item, cat, out_path, stop_flag=state.stop_flag
                        )
                        state.task_done += 1

            if state.task_total > 0 and not state.stop_flag.is_set():
                state.task_skipped = max(0, state.task_total - state.task_done)

            state.task_type  = ""
            skipped_msg      = (f" ({state.task_skipped} skipped — no valid URL)"
                                if state.task_skipped > 0 else "")
            state.set_status(f"Done. {state.task_done} items saved{skipped_msg}. Output: {out_path}")
            if state.task_skipped > 0:
                state.log(f"[M3U] ⚠ {state.task_skipped} item(s) skipped (stream URL could not be resolved)")
            state.log("DONE.")

        run_worker(worker())
        return jsonify({"ok": True, "message": f"Download started → {out_path}"})

    # ── /api/download/mkv ─────────────────────────────────────────────────────
    @flask_app.route("/api/download/mkv", methods=["POST"])
    def api_download_mkv():
        data         = request.get_json(force=True)
        items        = data.get("items", [])
        cat          = data.get("category", {})
        mode         = data.get("mode", "live")
        mode         = mode if mode in ("live", "vod", "series") else "live"
        out_dir      = data.get("out_dir", state.mkv_folder).strip()
        use_fallback = data.get("use_fallback", state.mkv_fallback)

        if not items:
            return jsonify({"error": "No items selected"}), 400
        if not out_dir:
            return jsonify({"error": "No output folder specified"}), 400
        if not _FFMPEG_AVAILABLE:
            return jsonify({"error": "ffmpeg not found on PATH"}), 400
        if state.busy:
            return jsonify({"error": "Another operation is in progress"}), 409

        state.mkv_folder   = out_dir
        state.mkv_fallback = use_fallback
        state.stop_flag.clear()
        state.task_item_names = [
            (item.get("name") or item.get("o_name") or item.get("fname") or "")
            for item in items
        ]
        state.set_status(f"Resolving + downloading {len(items)} item(s) as MKV…")

        async def worker():
            total            = len(items)
            state.task_type  = "mkv"
            state.task_total = total
            state.task_done  = 0
            state.task_label = "Resolving URLs…"
            state.log(f"[MKV] Phase 1: resolving {total} item URL(s)…")
            resolved_items = []

            async with _make_client() as client:
                for i, item in enumerate(items, 1):
                    if state.stop_flag.is_set():
                        state.log("[MKV] Stopped during URL resolution.")
                        return
                    name             = item.get("name") or item.get("o_name") or item.get("fname") or f"item_{i}"
                    state.task_label = f"Resolving: {name}"
                    state.task_done  = i - 1
                    state.log(f"[MKV] Resolving ({i}/{total}): {name}")

                    if item.get("_is_series_group"):
                        episodes = item.get("_episodes", [])
                        for ep in episodes:
                            ep_url = ep.get("_url", "")
                            if ep_url:
                                resolved_items.append((ep.get("name", name), ep_url))
                        state.log(f"[MKV]   → expanded to {len(episodes)} episode(s)")
                        continue

                    if item.get("_is_show_item"):
                        cat_title = cat.get("title", "Unknown")
                        expanded  = dict(item)
                        expanded["_cat_id"] = str(cat.get("id", ""))
                        state.log("[MKV]   Show-level item — fetching episode list…")
                        try:
                            episodes = await client.fetch_episodes_for_show(expanded, cat_title)
                        except Exception as e:
                            state.log(f"[MKV]   ✗ Could not fetch episodes: {e}")
                            episodes = []
                        for ep in episodes:
                            if state.stop_flag.is_set():
                                break
                            ep_url = ep.get("_direct_url", "") or await client.resolve_item_url(mode, ep, cat)
                            if ep_url:
                                resolved_items.append((ep.get("name", name), ep_url))
                        continue

                    url = await client.resolve_item_url(mode, item, cat)
                    if url:
                        resolved_items.append((name, url))
                    else:
                        state.log(f"[MKV]   ✗ Could not resolve URL for: {name}")

            if not resolved_items:
                state.log("[MKV] No URLs could be resolved.")
                state.set_status("MKV: no URLs resolved.")
                return

            os.makedirs(out_dir, exist_ok=True)
            state.task_type  = "mkv"
            state.task_total = len(resolved_items)
            state.task_done  = 0
            state.task_label = f"Downloading {len(resolved_items)} file(s)…"
            state.log(f"[MKV] Phase 2: downloading {len(resolved_items)} file(s) to: {out_dir}")

            pre_args = [
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto,rtsp,rtmp",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "10",
                "-fflags", "+genpts+igndts",
            ]
            MAX_RETRIES = 3

            for idx, (name, url) in enumerate(resolved_items, 1):
                if state.stop_flag.is_set():
                    state.log("[MKV] Stopped by user.")
                    break

                safe     = safe_filename(name)
                out_path = os.path.join(out_dir, f"{safe}.mkv")
                state.task_done         = idx - 1
                state.task_label        = name
                state.task_file_pct     = 0.0
                state.task_file_elapsed = ""
                state.task_speed        = ""
                state.log(f"[MKV] ({idx}/{len(resolved_items)}) Downloading: {name}")
                state.set_status(f"MKV {idx}/{len(resolved_items)}: {name}")
                # ── DLM: register this file ────────────────────────────────
                _mkv_jid = str(uuid.uuid4())
                state._dlm_current_mkv_jid = _mkv_jid
                _dlm_add_job(_mkv_jid, "mkv", name, out_path)

                state.log("[MKV]   Probing codecs…")
                codecs = probe_stream_codecs(url, pre_input_args=pre_args,
                                             ffprobe_path=_FFPROBE_PATH)
                state.task_file_duration = (codecs.get("duration") or 0.0) if codecs else 0.0
                post_args = ["-avoid_negative_ts", "make_zero"]
                if codecs and codecs.get("audio"):
                    if any(c.lower() == "aac" for c in codecs["audio"]):
                        post_args += ["-bsf:a", "aac_adtstoasc"]
                        state.log("[MKV]   AAC audio → adding -bsf:a aac_adtstoasc")

                def _set_proc(p):
                    with state.mkv_proc_lock:
                        state.mkv_proc = p

                def _on_ffmpeg_line(line: str):
                    stripped = line.rstrip()
                    if stripped:
                        state.log(stripped)
                    m_t = _time_re.search(line)
                    if m_t:
                        h, mi, s = int(m_t.group(1)), int(m_t.group(2)), float(m_t.group(3))
                        elapsed_s = h * 3600 + mi * 60 + s
                        dur = state.task_file_duration
                        state.task_file_elapsed = f"{int(h):02d}:{int(mi):02d}:{int(s):02d}"
                        if dur and dur > 0:
                            state.task_file_pct = min(100.0, round(elapsed_s / dur * 100, 1))
                    m_b = _bitrate_re.search(line)
                    if m_b:
                        kbits = float(m_b.group(1))
                        kbytes = kbits / 8.0
                        state.task_speed = (f"{kbytes/1024:.1f} MB/s"
                                            if kbytes >= 1024 else f"{kbytes:.0f} KB/s")

                rc = 0
                for attempt in range(1, MAX_RETRIES + 1):
                    if state.stop_flag.is_set():
                        break
                    if attempt > 1:
                        state.task_file_pct     = 0.0
                        state.task_file_elapsed = ""
                        state.task_speed        = ""

                    if attempt == 1:
                        state.log(f"[MKV]   Attempt {attempt}/{MAX_RETRIES} — direct MKV…")
                        rc = run_ffmpeg_download(
                            url, out_path,
                            pre_input_args=pre_args,
                            post_input_args=post_args,
                            on_progress=_on_ffmpeg_line,
                            stop_event=state.stop_flag,
                            set_proc=_set_proc,
                            ffmpeg_path=_FFMPEG_PATH,
                        )
                        with state.mkv_proc_lock:
                            state.mkv_proc = None
                        if rc == 0:
                            break
                        if state.stop_flag.is_set():
                            break
                        state.log(f"[MKV]   ✗ Direct MKV exit {rc} — retrying via TS intermediate…")
                        with contextlib.suppress(Exception):
                            os.remove(out_path)
                    else:
                        state.log(f"[MKV]   Attempt {attempt}/{MAX_RETRIES} — saving as MPEG-TS…")
                        ts_out      = os.path.splitext(out_path)[0] + ".ts"
                        ts_post_args = [a for a in post_args
                                        if a not in ("-avoid_negative_ts", "make_zero",
                                                     "-bsf:a", "aac_adtstoasc")]
                        ts_post_args += ["-f", "mpegts"]

                        rc = run_ffmpeg_download(
                            url, ts_out,
                            pre_input_args=pre_args,
                            post_input_args=ts_post_args,
                            on_progress=_on_ffmpeg_line,
                            stop_event=state.stop_flag,
                            set_proc=_set_proc,
                            ffmpeg_path=_FFMPEG_PATH,
                        )
                        with state.mkv_proc_lock:
                            state.mkv_proc = None

                        if state.stop_flag.is_set():
                            break
                        if rc != 0:
                            state.log(f"[MKV]   ✗ TS download exit {rc} on attempt {attempt}/{MAX_RETRIES}")
                            with contextlib.suppress(Exception):
                                os.remove(ts_out)
                            if attempt < MAX_RETRIES:
                                time.sleep(2)
                            continue

                        state.log(f"[MKV]   Saved as MPEG-TS: {os.path.basename(ts_out)}")
                        out_path = ts_out
                        break

                if state.stop_flag.is_set():
                    _dlm_error_job(_mkv_jid, "stopped by user")
                    state._dlm_current_mkv_jid = None
                    break

                if rc == 0:
                    state.log(f"[MKV] ✓ Saved: {out_path}")
                    _dlm_complete_job(_mkv_jid, out_path)
                    state._dlm_current_mkv_jid = None
                else:
                    state.log(f"[MKV] ✗ Failed after {MAX_RETRIES} attempt(s): {name}")
                    if use_fallback and YTDLP_AVAILABLE and not state.stop_flag.is_set():
                        state.log("[MKV]   Trying yt-dlp fallback…")
                        state.task_file_pct     = 0.0
                        state.task_file_elapsed = ""
                        state.task_speed        = ""

                        def _ytdlp_progress(d):
                            downloaded = d.get("downloaded_bytes") or 0
                            total_b    = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                            speed      = d.get("speed") or 0
                            elapsed    = d.get("elapsed") or 0
                            frag_idx   = d.get("fragment_index") or 0
                            frag_total = d.get("fragment_count") or 0
                            if total_b > 0:
                                state.task_file_pct = min(100.0, round(downloaded / total_b * 100, 1))
                            if speed:
                                kbytes = speed / 1024.0
                                state.task_speed = (f"{kbytes/1024:.1f} MB/s"
                                                    if kbytes >= 1024 else f"{kbytes:.0f} KB/s")
                            if elapsed:
                                h = int(elapsed) // 3600
                                m = (int(elapsed) % 3600) // 60
                                s = int(elapsed) % 60
                                state.task_file_elapsed = f"{h:02d}:{m:02d}:{s:02d}"
                            if frag_idx and frag_idx % 10 == 0:
                                pct_str  = d.get("_percent_str", "").strip() or (
                                    f"{state.task_file_pct:.1f}%" if state.task_file_pct else "?")
                                spd_str  = d.get("_speed_str", "").strip() or state.task_speed
                                eta_str  = d.get("_eta_str", "").strip() or ""
                                frag_str = f" (frag {frag_idx}/{frag_total})" if frag_total else ""
                                eta_part = f" ETA {eta_str}" if eta_str else ""
                                state.log(f"[yt-dlp] {pct_str} at {spd_str}{eta_part}{frag_str}")

                        ok, err = run_yt_dlp_download(url, out_path,
                                                      stop_event=state.stop_flag,
                                                      on_progress=_ytdlp_progress)
                        if ok:
                            state.log(f"[MKV]   ✓ yt-dlp saved: {out_path}")
                            _dlm_complete_job(_mkv_jid, out_path)
                        elif err == "stopped":
                            state.log("[MKV]   yt-dlp stopped by user.")
                            _dlm_error_job(_mkv_jid, "stopped by user")
                        else:
                            state.log(f"[MKV]   ✗ yt-dlp failed: {err}")
                            _dlm_error_job(_mkv_jid, f"yt-dlp: {err}")
                        state._dlm_current_mkv_jid = None
                    else:
                        _dlm_error_job(_mkv_jid, f"ffmpeg exit {rc}")
                        state._dlm_current_mkv_jid = None

            if not state.stop_flag.is_set():
                state.task_done = len(resolved_items)
                state.set_status(f"MKV download complete. Files in: {out_dir}")
                state.log(f"[MKV] All done. Output folder: {out_dir}")
            state.task_type         = ""
            state.task_file_pct     = 0.0
            state.task_file_elapsed = ""
            state.task_speed        = ""
            state.task_item_names   = []
            state.log("DONE.")

        run_worker(worker())
        return jsonify({"ok": True, "message": f"MKV download started → {out_dir}"})

    # ── /api/record/start ─────────────────────────────────────────────────────
    @flask_app.route("/api/record/start", methods=["POST"])
    def api_record_start():
        data        = request.get_json(force=True)
        stream_url  = data.get("url", "").strip()
        out_dir     = data.get("out_dir", state.mkv_folder).strip()
        stream_name = data.get("name", "recording").strip()

        if not stream_url:
            return jsonify({"error": "No stream URL"}), 400
        if not _FFMPEG_AVAILABLE:
            return jsonify({"error": "ffmpeg not found"}), 400
        if state.recording:
            return jsonify({"error": "Already recording"}), 409

        if not out_dir:
            out_dir = os.path.expanduser("~/Downloads")
        os.makedirs(out_dir, exist_ok=True)

        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname    = safe_filename(stream_name) + f"_{ts}.mkv"
        out_path = os.path.join(out_dir, fname)

        cmd = [_FFMPEG_PATH, "-hide_banner", "-nostdin", "-y",
               "-protocol_whitelist", "file,http,https,tcp,tls,crypto,rtsp,rtmp",
               "-i", stream_url, "-c", "copy", out_path]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            return jsonify({"error": f"Failed to start ffmpeg: {e}"}), 500

        with state.record_proc_lock:
            state.record_proc = proc
        state.recording         = True
        state.record_start_time = time.time()
        state.record_file_path  = out_path
        state.log(f"[REC] ⏺ Recording started: {out_path}")
        state.set_status(f"⏺ Recording → {fname}")
        # ── DLM: track this recording ──────────────────────────────────────
        _rec_jid = str(uuid.uuid4())
        state.record_job_id = _rec_jid
        _dlm_add_job(_rec_jid, "recording", stream_name, out_path, state.record_start_time)
        return jsonify({"ok": True, "file": out_path, "filename": fname})

    # ── /api/record/stop ──────────────────────────────────────────────────────
    @flask_app.route("/api/record/stop", methods=["POST"])
    def api_record_stop():
        if not state.recording:
            return jsonify({"error": "Not recording"}), 400
        with state.record_proc_lock:
            p = state.record_proc
        if p:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        with state.record_proc_lock:
            state.record_proc = None
        state.recording = False
        saved = state.record_file_path
        state.log(f"[REC] ⏹ Recording stopped. Saved: {saved}")
        state.set_status(f"Recording stopped. Saved: {os.path.basename(saved)}")
        # ── DLM: mark completed ────────────────────────────────────────────
        _rec_jid = getattr(state, "record_job_id", None)
        if _rec_jid:
            _dlm_complete_job(_rec_jid, saved)
            state.record_job_id = None
        return jsonify({"ok": True, "file": saved})

    # ── /api/record/status ────────────────────────────────────────────────────
    @flask_app.route("/api/record/status", methods=["GET"])
    def api_record_status():
        if not state.recording:
            return jsonify({"recording": False})
        elapsed = int(time.time() - state.record_start_time)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        return jsonify({
            "recording": True,
            "elapsed":   f"{h:02d}:{m:02d}:{s:02d}",
            "file":      state.record_file_path,
            "filename":  os.path.basename(state.record_file_path),
        })

    _register_dl_ui_route(flask_app)
    _register_dlm_routes(flask_app, state)


# ─────────────────────────────────────────────────────────────────────────────
# Download Manager routes + UI  (served as /api/dlm/ui.js)
# ─────────────────────────────────────────────────────────────────────────────

_DLM_UI_JS_BYTES: bytes = b""


def _register_dlm_routes(app, state) -> None:
    """Register all /api/dlm/* routes and the /api/dlm/ui.js frontend."""
    import logging
    LOG = logging.getLogger(__name__)

    # ── Startup recovery: fix jobs stuck in_progress from a previous crash ─────
    with _dlm_jobs_lock:
        jobs    = _dlm_load_jobs()
        changed = False
        for job in jobs:
            if job.get("status") != "in_progress":
                continue
            fp = job.get("filePath", "")
            if fp and os.path.exists(fp):
                job["status"]       = "completed"
                job["completedAt"]  = datetime.now(tz=timezone.utc).isoformat()
                try:
                    job["fileSizeBytes"] = os.path.getsize(fp)
                except Exception:
                    pass
                LOG.info("[DLM] Recovered completed job on startup: %s", job.get("name"))
            else:
                job["status"]       = "error"
                job["completedAt"]  = datetime.now(tz=timezone.utc).isoformat()
                job["errorMessage"] = "Interrupted (app restarted)"
                LOG.warning("[DLM] Ghost job cleared on startup: %s", job.get("name"))
            changed = True
        if changed:
            _dlm_save_jobs(jobs)

    # ── GET /api/dlm/jobs  (in-progress) ──────────────────────────────────────
    @app.route("/api/dlm/jobs")
    def dlm_list_jobs():
        jobs = _dlm_load_jobs()
        return jsonify([j for j in jobs if j["status"] == "in_progress"])

    # ── GET /api/dlm/completed  (finished jobs, newest first) ─────────────────
    @app.route("/api/dlm/completed")
    def dlm_list_completed():
        jobs = _dlm_load_jobs()
        done = [j for j in jobs if j["status"] != "in_progress"]
        done.sort(key=lambda j: j.get("completedAt") or j.get("startedAt") or "", reverse=True)
        return jsonify(done)

    # ── POST /api/dlm/set_folder ───────────────────────────────────────────────
    @app.route("/api/dlm/set_folder", methods=["POST"])
    def dlm_set_folder():
        d      = request.get_json(force=True)
        folder = (d.get("folder") or "").strip()
        if not folder:
            return jsonify({"error": "folder is required"}), 400
        if state:
            state.mkv_folder = folder
        _storage_cache.clear()
        LOG.info("[DLM] Output folder set: %s", folder)
        return jsonify({"ok": True, "folder": folder})

    # ── GET /api/dlm/storage ──────────────────────────────────────────────────
    _storage_cache: dict = {}

    @app.route("/api/dlm/storage")
    def dlm_storage():
        nonlocal _storage_cache
        # Accept explicit path from frontend (avoids stale state.mkv_folder)
        out_dir = request.args.get("path", "").strip()
        if not out_dir and state:
            out_dir = getattr(state, "mkv_folder", "") or getattr(state, "dvr_folder", "")
        if not out_dir:
            out_dir = os.path.expanduser("~/Downloads")
        cached = _storage_cache
        if cached.get("folder") == out_dir and time.time() - cached.get("_ts", 0) < 60:
            return jsonify({k: v for k, v in cached.items() if k != "_ts"})
        try:
            tgt   = out_dir if os.path.exists(out_dir) else os.path.expanduser("~")
            usage = shutil.disk_usage(tgt)
            pct   = round(usage.used / usage.total * 100, 1) if usage.total else 0
            result = {"total": usage.total, "used": usage.used, "free": usage.free,
                      "percentage": pct, "folder": out_dir, "_ts": time.time()}
            _storage_cache = result
            return jsonify({k: v for k, v in result.items() if k != "_ts"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── GET /api/dlm/progress  (live stats for active jobs) ───────────────────
    @app.route("/api/dlm/progress")
    def dlm_progress():
        if not state:
            return jsonify({})
        now    = time.time()
        result = {}
        for job in _dlm_load_jobs():
            if job["status"] != "in_progress":
                continue
            jid  = job["id"]
            fp   = job.get("filePath", "")
            size = 0
            if fp and os.path.exists(fp):
                try:
                    size = os.path.getsize(fp)
                except Exception:
                    pass

            if job["type"] == "recording":
                if (getattr(state, "recording", False) and
                        getattr(state, "record_job_id", None) == jid):
                    elapsed = max(0, int(now - getattr(state, "record_start_time", now)))
                    result[jid] = {"type": "recording",
                                   "fileSizeBytes":  size,
                                   "elapsedSeconds": elapsed}

            elif job["type"] == "mkv":
                if (getattr(state, "task_type", "") == "mkv" and
                        getattr(state, "_dlm_current_mkv_jid", None) == jid):
                    result[jid] = {
                        "type":          "mkv",
                        "fileSizeBytes": size,
                        "pct":           getattr(state, "task_file_pct",     0),
                        "elapsed":       getattr(state, "task_file_elapsed", ""),
                        "speed":         getattr(state, "task_speed",        ""),
                        "label":         getattr(state, "task_label",        ""),
                        "done":          getattr(state, "task_done",         0),
                        "total":         getattr(state, "task_total",        0),
                    }
        return jsonify(result)

    # ── DELETE /api/dlm/completed/all ─────────────────────────────────────────
    @app.route("/api/dlm/completed/all", methods=["DELETE"])
    def dlm_clear_all():
        with_files = request.args.get("files", "false").lower() == "true"
        with _dlm_jobs_lock:
            jobs   = _dlm_load_jobs()
            to_del = [j for j in jobs if j["status"] != "in_progress"]
            if with_files:
                for j in to_del:
                    fp = j.get("filePath", "")
                    if fp and os.path.exists(fp):
                        try:
                            os.remove(fp)
                        except Exception as exc:
                            LOG.warning("[DLM] Could not delete %s: %s", fp, exc)
            _dlm_save_jobs([j for j in jobs if j["status"] == "in_progress"])
        return jsonify({"ok": True})

    # ── DELETE /api/dlm/completed/<id>  (remove entry, keep file) ─────────────
    @app.route("/api/dlm/completed/<job_id>", methods=["DELETE"])
    def dlm_remove_entry(job_id):
        with _dlm_jobs_lock:
            jobs = _dlm_load_jobs()
            _dlm_save_jobs([j for j in jobs if j["id"] != job_id])
        return jsonify({"ok": True})

    # ── DELETE /api/dlm/completed/<id>/file  (remove entry + delete file) ─────
    @app.route("/api/dlm/completed/<job_id>/file", methods=["DELETE"])
    def dlm_delete_file(job_id):
        with _dlm_jobs_lock:
            jobs = _dlm_load_jobs()
            job  = next((j for j in jobs if j["id"] == job_id), None)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            fp = job.get("filePath", "")
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                    LOG.info("[DLM] Deleted file: %s", fp)
                except Exception as exc:
                    LOG.warning("[DLM] Could not delete file %s: %s", fp, exc)
            _dlm_save_jobs([j for j in jobs if j["id"] != job_id])
        return jsonify({"ok": True})

    # ── GET /api/dlm/ui.js ────────────────────────────────────────────────────
    global _DLM_UI_JS_BYTES
    _DLM_UI_JS_BYTES = _DLM_UI_JS.encode("utf-8")

    @app.route("/api/dlm/ui.js")
    def dlm_ui_js():
        from flask import Response
        return Response(
            _DLM_UI_JS_BYTES,
            content_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    LOG.info("[DLM] Routes registered (jobs_file=%s)", DLM_JOBS_FILE)


_DLM_UI_JS = r"""
/* ── Inject CSS ─────────────────────────────────────────────────────── */
(function(){
  const s = document.createElement('style');
  s.textContent = `
/* ─── Download Manager ──────────────────────────────────────────────────────── */
.dlm-card{display:flex;flex-direction:column;gap:3px;padding:9px 11px;
  background:rgba(255,255,255,.02);border:1px solid var(--bdr);border-radius:var(--rsm);
  transition:var(--tr)}
.dlm-card:hover{border-color:rgba(124,58,237,.25);background:rgba(124,58,237,.04)}
.dlm-card-top{display:flex;align-items:center;gap:6px;min-width:0}
.dlm-card-title{flex:1;font-size:12px;font-weight:600;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.dlm-card-meta{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-top:1px}
.dlm-card-time{font-size:10px;color:var(--txt3)}
.dlm-badge{display:inline-block;padding:1px 7px;border-radius:20px;font-size:9px;
  font-weight:800;text-transform:uppercase;letter-spacing:.5px;flex-shrink:0}
.dlm-badge.rec-active{background:rgba(220,38,38,.2);color:#f87171;
  border:1px solid rgba(220,38,38,.4);animation:dlm-pulse 1.4s ease infinite}
.dlm-badge.mkv-active{background:rgba(59,130,246,.2);color:#60a5fa;
  border:1px solid rgba(59,130,246,.4);animation:dlm-pulse 1.4s ease infinite}
@keyframes dlm-pulse{0%,100%{opacity:1}50%{opacity:.55}}
.dlm-badge.done{background:rgba(34,197,94,.12);color:#4ade80;
  border:1px solid rgba(34,197,94,.25)}
.dlm-badge.err{background:rgba(239,68,68,.15);color:#fca5a5;
  border:1px solid rgba(239,68,68,.3)}
.dlm-badge.stopped{background:rgba(107,114,128,.15);color:#9ca3af;
  border:1px solid rgba(107,114,128,.25)}
.dlm-card-btns{display:flex;gap:4px;flex-shrink:0;margin-top:3px;justify-content:flex-end}
.dlm-card-btns button{height:24px;padding:0 8px;font-size:10px;
  font-weight:700;border-radius:var(--rss)}
`;
  document.head.appendChild(s);
})();

/* ── Inject HTML ────────────────────────────────────────────────────── */
(function(){
  /* Modal */
  const d = document.createElement('div');
  d.innerHTML = `
<div id="dlm-overlay" style="display:none;position:fixed;inset:0;z-index:850;
  background:rgba(0,0,0,.6);align-items:center;justify-content:center">
<div style="background:var(--bg);border:1px solid var(--bdr);border-radius:var(--r);
  width:min(440px,96vw);max-height:92vh;display:flex;flex-direction:column;
  overflow:hidden;box-shadow:0 24px 64px rgba(0,0,0,.7)">

  <!-- Header -->
  <div style="display:flex;align-items:center;gap:8px;padding:10px 14px;
    border-bottom:1px solid var(--bdr);flex-shrink:0">
    <span style="font-size:16px">📥</span>
    <h3 style="flex:1;font-size:14px;font-weight:700;margin:0">Downloads</h3>
    <span id="dlm-hdr-badge" style="display:none;background:var(--acc);color:#fff;
      border-radius:20px;font-size:10px;padding:1px 6px;font-weight:800;margin-right:4px"></span>
    <button class="btn-ghost" onclick="dlmClose()"
      style="height:28px;width:28px;padding:0;font-size:15px">✕</button>
  </div>

  <!-- Storage bar -->
  <div style="padding:7px 14px 6px;flex-shrink:0;border-bottom:1px solid var(--bdr)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
      <span style="font-size:10px;font-weight:700;text-transform:uppercase;
        letter-spacing:.5px;color:var(--txt3)">Storage</span>
      <span id="dlm-storage-text" style="font-size:10px;color:var(--txt3)"></span>
    </div>
    <div style="height:5px;background:rgba(255,255,255,.08);border-radius:3px;
      overflow:hidden;margin-bottom:5px">
      <div id="dlm-storage-bar" style="height:100%;width:0%;border-radius:3px;transition:width .4s"></div>
    </div>
  </div>

  <div style="flex:1;overflow-y:auto;padding:10px 12px 12px">

    <!-- In progress -->
    <div style="font-size:11px;font-weight:800;text-transform:uppercase;
      letter-spacing:.7px;color:var(--txt3);margin-bottom:5px">In Progress</div>
    <div id="dlm-active-empty" style="text-align:center;padding:14px;
      color:var(--txt3);font-size:12px;display:none">Nothing active right now</div>
    <div id="dlm-active-list" style="display:flex;flex-direction:column;
      gap:4px;margin-bottom:12px"></div>

    <!-- Completed: tabbed by type -->
    <div style="margin-top:4px;margin-bottom:8px">
      <div style="display:flex;gap:0;border-bottom:1px solid var(--bdr);margin-bottom:8px">
        <button id="dlm-tab-rec" onclick="dlmSwitchDoneTab('rec')"
          style="flex:1;height:30px;font-size:11px;font-weight:700;background:none;
                 border:none;border-bottom:2px solid var(--acc);color:var(--txt);
                 cursor:pointer;padding:0;transition:color .15s">
          ⏺ Recordings <span id="dlm-tab-rec-count" style="opacity:.6;font-weight:400"></span>
        </button>
        <button id="dlm-tab-mkv" onclick="dlmSwitchDoneTab('mkv')"
          style="flex:1;height:30px;font-size:11px;font-weight:700;background:none;
                 border:none;border-bottom:2px solid transparent;color:var(--txt2);
                 cursor:pointer;padding:0;transition:color .15s">
          🎬 MKV <span id="dlm-tab-mkv-count" style="opacity:.6;font-weight:400"></span>
        </button>
      </div>
      <!-- per-tab clear/delete row -->
      <div style="display:flex;justify-content:flex-end;gap:4px;margin-bottom:6px">
        <button class="btn-ghost" onclick="dlmClearAll(false)"
          style="height:22px;padding:0 7px;font-size:10px;opacity:.7">Clear list</button>
        <button class="btn-ghost" onclick="dlmClearAll(true)"
          style="height:22px;padding:0 7px;font-size:10px;opacity:.7;color:#f87171">Delete files</button>
      </div>
      <!-- Recordings tab -->
      <div id="dlm-done-rec-panel">
        <div id="dlm-done-rec-empty" style="text-align:center;padding:14px;
          color:var(--txt3);font-size:12px;display:none">No completed recordings yet</div>
        <div id="dlm-done-rec-list" style="display:flex;flex-direction:column;gap:4px"></div>
      </div>
      <!-- MKV tab -->
      <div id="dlm-done-mkv-panel" style="display:none">
        <div id="dlm-done-mkv-empty" style="text-align:center;padding:14px;
          color:var(--txt3);font-size:12px;display:none">No completed MKV downloads yet</div>
        <div id="dlm-done-mkv-list" style="display:flex;flex-direction:column;gap:4px"></div>
      </div>
    </div>

  </div>
</div>
</div>
`;
  while(d.firstChild) document.body.appendChild(d.firstChild);

  /* ── Inject "Downloads" button into the action drawer after DVR button ── */
  function _injectBtn(){
    const dvrBtn = document.getElementById('adr-dvr-btn');
    if(!dvrBtn) return;
    if(document.getElementById('adr-dlm-btn')) return; // already injected
    const btn = document.createElement('button');
    btn.id = 'adr-dlm-btn';
    btn.setAttribute('onclick','closeDrawer();dlmOpen()');
    btn.style.cssText = [
      'margin-top:8px','width:100%','height:auto','padding:8px 12px',
      'font-size:13px','font-weight:700',
      'background:linear-gradient(135deg,rgba(59,130,246,.2),rgba(37,99,235,.2))',
      'border:1px solid rgba(59,130,246,.35)','border-radius:var(--rsm)',
      'cursor:pointer','color:var(--txt)',
      'display:flex','align-items:center','gap:7px','flex-direction:column'
    ].join(';');
    btn.innerHTML =
      '<span style="display:flex;align-items:center;gap:7px;width:100%;justify-content:center">' +
        '📥 Downloads' +
        '<span id="dlm-badge-adr" style="display:none;background:#3b82f6;color:#fff;' +
          'border-radius:20px;font-size:10px;padding:1px 6px;font-weight:800"></span>' +
      '</span>' +
      '<span id="dlm-adr-status" style="display:none;font-size:10px;font-weight:600;' +
        'color:#60a5fa;width:100%;text-align:center"></span>';
    dvrBtn.insertAdjacentElement('afterend', btn);
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _injectBtn);
  else _injectBtn();
})();

// ── State ──────────────────────────────────────────────────────────────────
const _DLM_OK = (typeof CFG !== 'undefined') && CFG.dlm_ok === true;
let _dlmInited = false;
let _dlmActive = [];
let _dlmDone   = [];

function dlmOpen(){
  if(!_DLM_OK){ toast('Download Manager not available','wrn'); return; }
  document.getElementById('dlm-overlay').style.display = 'flex';
  dlmInit();
}
function dlmClose(){
  document.getElementById('dlm-overlay').style.display = 'none';
}
async function dlmInit(){
  if(!_DLM_OK) return;
  if(_dlmInited){ await dlmRefresh(); return; }
  _dlmInited = true;
  // Sync download output path from settings to backend on first open
  const _dlmPathEl = document.getElementById('o-dir');
  if(_dlmPathEl && _dlmPathEl.value.trim()) await dlmSetFolder(_dlmPathEl.value.trim());
  await dlmRefresh();
}

async function dlmRefresh(){
  try{
    const open = document.getElementById('dlm-overlay')?.style.display === 'flex';
    const [ar, dr, sr] = await Promise.all([
      fetch('/api/dlm/jobs').then(r=>r.json()),
      fetch('/api/dlm/completed').then(r=>r.json()),
      open ? fetch('/api/dlm/storage?path='+encodeURIComponent(document.getElementById('o-dir')?.value||'')).then(r=>r.json()) : Promise.resolve(null),
    ]);
    _dlmActive = Array.isArray(ar) ? ar : [];
    _dlmDone   = Array.isArray(dr) ? dr : [];
    _dlmRenderActive();
    _dlmRenderDone();
    if(sr) _dlmRenderStorage(sr);
    _dlmBadgeUpdate();
  }catch(e){
    if(document.getElementById('dlm-overlay')?.style.display==='flex')
      toast('DLM: could not load data','err');
  }
}

// ── Formatters ─────────────────────────────────────────────────────────────
function _dlmFmtBytes(b){
  if(!b) return '';
  const k=1024, u=['B','KB','MB','GB'];
  const i=Math.floor(Math.log(b)/Math.log(k));
  return (b/Math.pow(k,i)).toFixed(1)+' '+u[i];
}
function _dlmFmtDate(iso){
  if(!iso) return '—';
  return new Date(iso).toLocaleString([],{month:'short',day:'numeric',
    hour:'2-digit',minute:'2-digit'});
}

// ── Render active ──────────────────────────────────────────────────────────
function _dlmRenderActive(){
  const el = document.getElementById('dlm-active-list');
  const em = document.getElementById('dlm-active-empty');
  if(!_dlmActive.length){ el.innerHTML=''; em.style.display=''; return; }
  em.style.display = 'none';
  el.innerHTML = _dlmActive.map(j=>{
    const isRec  = j.type === 'recording';
    const badgeCls = isRec ? 'rec-active' : 'mkv-active';
    const badgeTxt = isRec ? '⏺ REC' : '⬇ MKV';
    const prog = isRec
      ? `<div style="margin-top:5px">
           <div style="display:flex;justify-content:space-between;font-size:9px;
             color:var(--txt3);margin-bottom:2px">
             <span id="dlm-t-${esc(j.id)}">Recording…</span>
             <span id="dlm-sz-${esc(j.id)}"></span>
           </div>
           <div style="height:3px;background:rgba(255,255,255,.1);border-radius:2px;overflow:hidden">
             <div style="height:100%;width:100%;background:#f87171;border-radius:2px"></div>
           </div>
         </div>`
      : `<div style="margin-top:5px">
           <div style="display:flex;justify-content:space-between;font-size:9px;
             color:var(--txt3);margin-bottom:2px">
             <span id="dlm-t-${esc(j.id)}">Downloading…</span>
             <span id="dlm-sp-${esc(j.id)}"></span>
           </div>
           <div style="height:3px;background:rgba(255,255,255,.1);border-radius:2px;overflow:hidden">
             <div id="dlm-bar-${esc(j.id)}" style="height:100%;width:0%;
               background:#60a5fa;border-radius:2px;transition:width .5s"></div>
           </div>
           <div id="dlm-lbl-${esc(j.id)}"
             style="font-size:9px;color:var(--txt3);margin-top:2px"></div>
         </div>`;
    return `<div class="dlm-card">
      <div class="dlm-card-top">
        <span class="dlm-card-title">${esc(j.name||j.filename||'Download')}</span>
        <span class="dlm-badge ${badgeCls}" style="flex-shrink:0">${badgeTxt}</span>
        <button onclick="${isRec?'stopRec()':'doStop()'}"
          title="${isRec?'Stop recording':'Stop download'}"
          style="flex-shrink:0;height:20px;padding:0 7px;font-size:10px;font-weight:700;
                 background:none;border:1px solid ${isRec?'rgba(220,38,38,.5)':'rgba(59,130,246,.5)'};
                 border-radius:4px;cursor:pointer;color:${isRec?'#f87171':'#60a5fa'};
                 line-height:1">⏹</button>
      </div>
      <div class="dlm-card-meta">
        <span class="dlm-card-time">${_dlmFmtDate(j.startedAt)}</span>
        <span id="dlm-sz-meta-${esc(j.id)}" class="dlm-card-time"></span>
      </div>
      ${prog}
    </div>`;
  }).join('');
  _dlmPollProgress();
}

// ── Completed tab state ────────────────────────────────────────────────────
let _dlmDoneTab = 'rec'; // 'rec' | 'mkv'

function dlmSwitchDoneTab(tab){
  _dlmDoneTab = tab;
  const recBtn = document.getElementById('dlm-tab-rec');
  const mkvBtn = document.getElementById('dlm-tab-mkv');
  const recPnl = document.getElementById('dlm-done-rec-panel');
  const mkvPnl = document.getElementById('dlm-done-mkv-panel');
  if(recBtn){ recBtn.style.borderBottomColor = tab==='rec' ? 'var(--acc)' : 'transparent'; recBtn.style.color = tab==='rec' ? 'var(--txt)' : 'var(--txt2)'; }
  if(mkvBtn){ mkvBtn.style.borderBottomColor = tab==='mkv' ? 'var(--acc)' : 'transparent'; mkvBtn.style.color = tab==='mkv' ? 'var(--txt)' : 'var(--txt2)'; }
  if(recPnl) recPnl.style.display = tab==='rec' ? '' : 'none';
  if(mkvPnl) mkvPnl.style.display = tab==='mkv' ? '' : 'none';
}

// ── Render completed ───────────────────────────────────────────────────────
function _dlmRenderDone(){
  const recs = _dlmDone.filter(j=>j.type==='recording');
  const mkvs = _dlmDone.filter(j=>j.type!=='recording');

  // Update tab count badges
  const rc = document.getElementById('dlm-tab-rec-count');
  const mc = document.getElementById('dlm-tab-mkv-count');
  if(rc) rc.textContent = recs.length ? `(${recs.length})` : '';
  if(mc) mc.textContent = mkvs.length ? `(${mkvs.length})` : '';

  _renderDoneGroup('rec', recs);
  _renderDoneGroup('mkv', mkvs);
}

function _renderDoneGroup(key, jobs){
  const el = document.getElementById('dlm-done-'+key+'-list');
  const em = document.getElementById('dlm-done-'+key+'-empty');
  if(!el||!em) return;
  if(!jobs.length){ el.innerHTML=''; em.style.display=''; return; }
  em.style.display = 'none';
  el.innerHTML = jobs.map(j=>{
    const isRec = j.type === 'recording';
    const ico   = isRec ? '⏺' : '🎬';
    const lbl   = isRec ? 'REC' : 'MKV';
    const bc    = j.status==='completed' ? 'done' : j.status==='error' ? 'err' : 'stopped';
    const meta  = [_dlmFmtBytes(j.fileSizeBytes),
                   j.status!=='completed' ? j.status : ''].filter(Boolean).join(' · ');
    const err   = (j.status==='error'&&j.errorMessage)
      ? `<div style="font-size:10px;color:#fca5a5;margin-top:2px">${esc(j.errorMessage)}</div>` : '';
    const canReveal = !!(j.filename || j.fileSizeBytes);
    return `<div class="dlm-card">
      <div class="dlm-card-top">
        <span style="font-size:11px;flex-shrink:0">${ico}</span>
        <span class="dlm-card-title">${esc(j.name||j.filename||'Download')}</span>
        <span class="dlm-badge ${bc}">${lbl}</span>
      </div>
      <div class="dlm-card-meta">
        <span class="dlm-card-time">${_dlmFmtDate(j.completedAt||j.startedAt)}</span>
        ${meta?`<span class="dlm-card-time">· ${esc(meta)}</span>`:''}
      </div>
      ${err}
      <div class="dlm-card-btns">
        ${canReveal?`<button class="btn-ghost" onclick="dlmReveal('${esc(j.id)}')"
          title="Show in folder" style="font-size:11px">📂</button>`:''}
        <button class="btn-ghost" onclick="dlmRemoveEntry('${esc(j.id)}')"
          title="Remove from list">🗑</button>
        ${canReveal?`<button class="btn-ghost" onclick="dlmDeleteFile('${esc(j.id)}')"
          title="Delete file" style="color:#f87171;font-size:10px">🗑 file</button>`:''}
      </div>
    </div>`;
  }).join('');
}
// ── Render storage bar ─────────────────────────────────────────────────────
function _dlmRenderStorage(s){
  if(!s||s.error) return;
  const pct = s.percentage || 0;
  const bar = document.getElementById('dlm-storage-bar');
  if(bar){ bar.style.width=pct+'%';
    bar.style.background=pct>90?'#dc2626':pct>75?'#ca8a04':'var(--acc)'; }
  const txt = document.getElementById('dlm-storage-text');
  if(txt) txt.textContent = `${_dlmFmtBytes(s.used)} of ${_dlmFmtBytes(s.total)} used`;
}

// ── Badge update ───────────────────────────────────────────────────────────
function _dlmBadgeUpdate(){
  const n = _dlmActive.length;
  const hb = document.getElementById('dlm-hdr-badge');
  if(hb){ hb.textContent=n; hb.style.display=n?'':'none'; }
  const ab = document.getElementById('dlm-badge-adr');
  if(ab){ ab.textContent=n; ab.style.display=n?'':'none'; }
  const st = document.getElementById('dlm-adr-status');
  if(st){
    if(n){
      const recs = _dlmActive.filter(j=>j.type==='recording');
      const mkvs = _dlmActive.filter(j=>j.type==='mkv');
      const parts = [];
      if(recs.length) parts.push('⏺ '+recs.map(j=>j.name).join(', ').slice(0,28));
      if(mkvs.length) parts.push('⬇ '+mkvs.map(j=>j.name).join(', ').slice(0,28));
      st.textContent = parts.join(' · ').slice(0,56);
      st.style.display = '';
    } else {
      st.style.display = 'none';
    }
  }
}

// ── Live progress polling ──────────────────────────────────────────────────
let _dlmProgTimer    = null;
let _dlmRefreshPend  = false;

async function _dlmPollProgress(){
  clearTimeout(_dlmProgTimer);
  if(!_dlmActive.length ||
     document.getElementById('dlm-overlay')?.style.display !== 'flex') return;
  try{
    const prog = await fetch('/api/dlm/progress').then(r=>r.json());

    // Job gone from progress → finished, trigger refresh
    const missing = _dlmActive.map(j=>j.id).filter(id=>!(id in prog));
    if(missing.length && !_dlmRefreshPend){
      _dlmRefreshPend = true;
      setTimeout(()=>{ _dlmRefreshPend=false; dlmRefresh(); }, 1200);
    }

    for(const [id, p] of Object.entries(prog)){
      if(p.type === 'recording'){
        const e = p.elapsedSeconds||0;
        const h=Math.floor(e/3600), m=Math.floor((e%3600)/60), s=e%60;
        const ts=(h>0?`${h}h `:'')+`${String(m).padStart(2,'0')}m ${String(s).padStart(2,'0')}s`;
        const t  = document.getElementById(`dlm-t-${id}`);
        const sz = document.getElementById(`dlm-sz-${id}`);
        const sm = document.getElementById(`dlm-sz-meta-${id}`);
        if(t)  t.textContent  = ts;
        if(sz) sz.textContent = p.fileSizeBytes ? _dlmFmtBytes(p.fileSizeBytes) : '';
        if(sm) sm.textContent = p.fileSizeBytes ? '· '+_dlmFmtBytes(p.fileSizeBytes) : '';

      } else if(p.type === 'mkv'){
        const bar = document.getElementById(`dlm-bar-${id}`);
        const t   = document.getElementById(`dlm-t-${id}`);
        const sp  = document.getElementById(`dlm-sp-${id}`);
        const lb  = document.getElementById(`dlm-lbl-${id}`);
        const sm  = document.getElementById(`dlm-sz-meta-${id}`);
        const pct   = p.pct   || 0;
        const total = p.total || 0;
        const done  = p.done  || 0;
        if(bar) bar.style.width = pct + '%';
        if(t){
          const fi = total>1 ? `File ${done+1}/${total}` : 'Downloading…';
          t.textContent = p.elapsed ? `${fi} · ${p.elapsed}` : fi;
        }
        if(sp) sp.textContent = p.speed || '';
        if(lb) lb.textContent = p.label || '';
        if(sm) sm.textContent = p.fileSizeBytes ? '· '+_dlmFmtBytes(p.fileSizeBytes) : '';
      }
    }
  }catch(e){}
  _dlmProgTimer = setTimeout(_dlmPollProgress, 3000);
}

// ── Background badge poll when modal is closed ─────────────────────────────
setInterval(async ()=>{
  if(!_DLM_OK || !_dlmInited) return;
  if(document.getElementById('dlm-overlay')?.style.display === 'flex') return;
  try{
    const j = await fetch('/api/dlm/jobs').then(r=>r.json());
    if(Array.isArray(j)){ _dlmActive=j; _dlmBadgeUpdate(); }
  }catch(e){}
}, 5000);

// ── Folder picker (mirrors DVR exactly) ───────────────────────────────────
async function dlmPickFolder(){
  // Desktop: try tkinter picker; mobile/fallback: shared output file browser
  try{
    const r = await fetch('/api/browse_folder');
    const d = await r.json();
    if(d.path){ await dlmSetFolder(d.path); dlmRefresh(); return; }
    if(d.error) throw new Error(d.error);
  }catch(e){
    // Open shared output browser targeting Download
    if(typeof outFbSetTarget === 'function'){
      _outFbMobileMode = true;
      _outFbOpen = true;
      if(typeof _outFbApplyState === 'function') _outFbApplyState();
      outFbSetTarget('dir');
    }
  }
}
async function dlmSetFolder(path){
  try{
    await fetch('/api/dlm/set_folder',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({folder:path})});
  }catch(e){}
  // Also keep the settings o-dir field in sync
  const el = document.getElementById('o-dir');
  if(el && path) el.value = path;
  if(typeof saveFP === 'function') saveFP();
}

// ── Actions ────────────────────────────────────────────────────────────────
async function dlmRemoveEntry(id){
  await fetch(`/api/dlm/completed/${id}`,{method:'DELETE'});
  toast('Removed from list','ok');
  dlmRefresh();
}
async function dlmDeleteFile(id){
  const j = _dlmDone.find(x=>x.id===id);
  const name = j ? (j.name||j.filename||'file') : 'file';
  _mvConfirm('Delete File',`Permanently delete "${name}"?`, async ()=>{
    await fetch(`/api/dlm/completed/${id}/file`,{method:'DELETE'});
    toast('File deleted','ok');
    dlmRefresh();
  });
}
async function dlmClearAll(withFiles){
  const title = withFiles ? 'Delete All Files' : 'Clear List';
  const msg   = withFiles
    ? 'Permanently delete ALL completed download files?'
    : 'Remove all completed entries from the list? Files will be kept.';
  _mvConfirm(title, msg, async ()=>{
    await fetch('/api/dlm/completed/all'+(withFiles?'?files=true':''),{method:'DELETE'});
    toast(withFiles?'All files deleted':'List cleared','ok');
    dlmRefresh();
  });
}
async function dlmReveal(id){
  const j = _dlmDone.find(x=>x.id===id);
  if(!j||!j.filePath) return;
  if(typeof _isMobile !== 'undefined' && _isMobile){
    toast('Show in folder is a desktop feature','wrn'); return;
  }
  try{
    const r = await fetch('/api/reveal_in_folder',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:j.filePath})});
    const d = await r.json();
    if(d.error) toast(d.error,'err');
  }catch(e){ toast('Could not open folder: '+e,'err'); }
}

// ── Auto-refresh while overlay open ───────────────────────────────────────
let _dlmAutoTimer = null;
function _dlmScheduleAuto(){
  clearTimeout(_dlmAutoTimer);
  _dlmAutoTimer = setTimeout(async ()=>{
    if(_dlmInited && document.getElementById('dlm-overlay')?.style.display==='flex'){
      await dlmRefresh();
    }
    _dlmScheduleAuto();
  }, _dlmActive.length ? 10000 : 60000);
}
const _dlmRefreshOrig = dlmRefresh;
dlmRefresh = async function(){
  await _dlmRefreshOrig();
  _dlmScheduleAuto();
};

// ── Backdrop click ─────────────────────────────────────────────────────────
document.getElementById('dlm-overlay').addEventListener('click', e=>{
  if(e.target === document.getElementById('dlm-overlay')) dlmClose();
});
"""


# ─────────────────────────────────────────────────────────────────────────────
# Frontend  (served as /api/dl/ui.js)
# ─────────────────────────────────────────────────────────────────────────────

_DL_UI_JS_BYTES: bytes = b""   # filled in register_download_routes


def _register_dl_ui_route(app) -> None:
    """Add the /api/dl/ui.js route and pre-encode the JS once."""
    global _DL_UI_JS_BYTES
    _DL_UI_JS_BYTES = _DL_UI_JS.encode("utf-8")

    @app.route("/api/dl/ui.js")
    def dl_ui_js():
        from flask import Response
        return Response(
            _DL_UI_JS_BYTES,
            content_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )


_DL_UI_JS = r"""
/* ── Inject CSS ─────────────────────────────────────────────────────── */
(function(){
  const s = document.createElement('style');
  s.textContent = `
/* ─── action drawer ──────────────────────────────────────────── */
#act-overlay{position:fixed;inset:0;z-index:400;background:rgba(0,0,0,.5);
  display:none;backdrop-filter:none}
#act-overlay.open{display:block}
#act-drawer{position:fixed;top:0;right:0;bottom:0;z-index:401;
  width:min(300px,85vw);background:var(--s2);border-left:1px solid var(--bdr2);
  display:flex;flex-direction:column;box-shadow:-8px 0 40px rgba(0,0,0,.6);
  transform:translateX(100%);transition:transform .3s cubic-bezier(.4,0,.2,1)}
#act-drawer.open{transform:translateX(0)}
.adr-hdr{display:flex;align-items:center;gap:10px;padding:16px;
  border-bottom:1px solid var(--bdr);flex-shrink:0}
.adr-hdr h3{flex:1;font-size:13px;font-weight:800;color:var(--txt)}
.adr-body{flex:1;overflow-y:auto;padding:14px}
.adr-section{margin-bottom:18px}
.adr-section-title{font-size:10px;font-weight:800;text-transform:uppercase;
  letter-spacing:1.2px;color:var(--txt3);margin-bottom:8px;padding-bottom:5px;
  border-bottom:1px solid var(--bdr)}
.adr-btn{width:100%;min-height:46px;font-size:13px;font-weight:600;
  display:flex;align-items:center;gap:10px;padding:8px 16px;
  margin-bottom:7px;border-radius:var(--rsm);text-align:left;justify-content:flex-start;
  box-sizing:border-box}
.adr-btn span.adr-ico{font-size:18px;flex-shrink:0;width:26px;text-align:center;align-self:flex-start;padding-top:1px}
.adr-btn span.adr-lbl{flex:1;min-width:0;white-space:normal;word-break:break-word;line-height:1.3}
.adr-btn span.adr-sub{font-size:11px;color:rgba(255,255,255,.5);font-weight:400;flex-shrink:0;white-space:nowrap;align-self:flex-start;padding-top:2px}
.adr-sel-row{display:flex;gap:4px;margin-bottom:10px}
.adr-sel-row button{flex:1;height:30px;font-size:11px;padding:0 4px}
.adr-count{font-size:12px;color:var(--acc);font-weight:700;
  text-align:center;padding:6px 0 2px}
/* Recording section in action drawer */
#adr-rec-section{margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid var(--bdr)}
#adr-rec-btn{width:100%;height:auto;min-height:42px;font-size:13px;font-weight:700;border-radius:var(--rsm);
  padding:8px 12px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
  background:rgba(220,50,50,.15);border:1px solid rgba(220,50,50,.35);color:#f06060;cursor:pointer;transition:background .15s}
#adr-rec-btn:hover{background:rgba(220,50,50,.3)}
#adr-rec-btn.rec{background:rgba(220,50,50,.3);border-color:rgba(220,50,50,.7);animation:recpulse 1.2s ease-in-out infinite}
@keyframes recpulse{0%,100%{box-shadow:0 0 0 0 rgba(220,50,50,.4)}50%{box-shadow:0 0 0 6px rgba(220,50,50,0)}}
#adr-rec-info{display:none;flex-direction:column;gap:4px}
#adr-rec-info.vis{display:flex}
/* FAB — floating action button to open drawer */
.fab{position:absolute;bottom:70px;right:60px;z-index:50;
  width:48px;height:48px;border-radius:50%;padding:0;font-size:20px;
  background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;
  box-shadow:0 4px 20px var(--glow);border:none;cursor:pointer;
  transition:var(--tr);display:flex;align-items:center;justify-content:center}
.fab:hover{transform:scale(1.1);box-shadow:0 6px 28px var(--glow)}
.fab:active{transform:scale(.93)}
.fab-badge{position:absolute;top:-3px;right:-3px;background:var(--green);
  color:#fff;font-size:9px;font-weight:800;border-radius:10px;
  padding:1px 5px;min-width:16px;text-align:center;display:none;
  border:1.5px solid var(--bg);box-shadow:0 0 6px rgba(34,197,94,.5)}
.fab-badge.vis{display:block}
@media(min-width:900px){.fab{display:none}}

/* Actions tab */
#t-act.act-open{color:var(--orange)}
#t-act.act-open::after{content:'';position:absolute;top:0;left:25%;right:25%;height:2.5px;
  background:linear-gradient(90deg,var(--orange),var(--acc));border-radius:0 0 4px 4px;
  box-shadow:0 0 10px var(--orange),0 0 20px rgba(245,158,11,.35);pointer-events:none}
#t-act.act-open .nt-ico{transform:scale(1.2);filter:drop-shadow(0 0 8px var(--orange))}

@media(min-width:900px){
  .ph h3{display:none}
  .ph{padding:8px 10px;gap:5px;justify-content:space-between}
}

/* M3U export progress panel inside action drawer */
#adr-m3u-progress{display:none;background:var(--s3);border:1px solid var(--bdr);
  border-radius:var(--rsm);padding:10px 12px;margin-top:10px}
#adr-m3u-progress.active{display:block}
#adr-m3u-prog-bar-wrap{background:rgba(0,0,0,.5);border-radius:8px;height:6px;
  overflow:hidden;margin:6px 0;position:relative}
#adr-m3u-prog-bar{height:100%;border-radius:8px;width:0%;transition:width .35s ease;
  background:linear-gradient(90deg,var(--acc2),var(--acc),var(--cyan))}
@keyframes adr-m3u-indeterminate{
  0%{transform:translateX(-110%)} 100%{transform:translateX(200%)}}
/* ─── toasts ──────────────────────────────────────────────────── */
#toasts{position:fixed;bottom:72px;left:50%;transform:translateX(-50%);
  z-index:9999;display:flex;flex-direction:column;gap:5px;pointer-events:none;width:min(90vw,300px)}
@media(min-width:900px){ #toasts{bottom:18px}}
.toast{padding:10px 18px;border-radius:24px;font-size:13px;font-weight:600;text-align:center;
  backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,.12);
  animation:slide-up .3s cubic-bezier(.34,1.56,.64,1);
  box-shadow:0 8px 32px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.1)}
.tok2{background:rgba(16,48,24,.95);color:var(--green);
  border-color:rgba(34,197,94,.4);box-shadow:0 0 20px rgba(34,197,94,.25),0 8px 32px rgba(0,0,0,.6)}
.terr2{background:rgba(48,12,12,.95);color:#ff7070;
  border-color:rgba(239,68,68,.4);box-shadow:0 0 20px rgba(239,68,68,.25),0 8px 32px rgba(0,0,0,.6)}
.tinfo{background:rgba(10,22,48,.95);color:#7ab8ff;
  border-color:rgba(59,130,246,.4);box-shadow:0 0 20px rgba(59,130,246,.2),0 8px 32px rgba(0,0,0,.6)}
.twrn2{background:rgba(40,28,8,.95);color:var(--orange);
  border-color:rgba(245,158,11,.4);box-shadow:0 0 20px rgba(245,158,11,.2),0 8px 32px rgba(0,0,0,.6)}

/* ─── spinner ────────────────────────────────────────────────── */
.spin{display:inline-block;width:16px;height:16px;border:2px solid var(--s5);
  border-top-color:var(--acc);border-radius:50%;animation:spin .7s linear infinite}

/* ─── animations ─────────────────────────────────────────────── */
@keyframes fade-up{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse-dot{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
@keyframes rec-glow{0%,100%{box-shadow:0 0 6px rgba(239,68,68,.4)}
  50%{box-shadow:0 0 22px rgba(239,68,68,.8),0 0 40px rgba(239,68,68,.25)}}
@keyframes pop-in{from{transform:scale(.4);opacity:0}to{transform:scale(1);opacity:1}}
@keyframes slide-up{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:translateY(0)}}

.hidden{display:none!important}
`;
  document.head.appendChild(s);
})();

/* ── Inject HTML ────────────────────────────────────────────────────── */
(function(){
  const d = document.createElement('div');
  d.innerHTML = `
<div id="act-overlay" onclick="closeDrawer()"></div>
<div id="act-drawer">
  <div class="adr-hdr">
    <h3 id="adr-title">⚡ Actions</h3>
    <button class="btn-ghost" onclick="closeDrawer()" style="height:32px;padding:0 12px;font-size:13px">✕</button>
  </div>
  <div class="adr-body">
    <!-- Recording section — always visible -->
    <div id="adr-rec-section">
      <div class="adr-section-title">⏺ Recording</div>
      <button id="adr-rec-btn" onclick="adrRecBtn()"
        style="display:flex;flex-direction:column;align-items:center;gap:3px;width:100%;padding:8px 12px">
        <span id="adr-rec-btn-label">⏺ Record</span>
        <span id="adr-rec-fname" style="font-size:10px;font-weight:600;opacity:.85;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%;display:none"></span>
      </button>
      <button id="adr-dvr-btn" onclick="closeDrawer();dvrOpen()"
        style="margin-top:6px;width:100%;height:auto;padding:8px 12px;font-size:13px;font-weight:700;
        background:linear-gradient(135deg,rgba(124,58,237,.25),rgba(99,46,188,.25));
        border:1px solid rgba(124,58,237,.4);border-radius:var(--rsm);cursor:pointer;
        color:var(--txt);display:flex;align-items:center;gap:7px;flex-direction:column">
        <span style="display:flex;align-items:center;gap:7px;width:100%;justify-content:center">
          📹 DVR
          <span id="dvr-badge-adr" style="display:none;background:var(--acc);
            color:#fff;border-radius:20px;font-size:10px;padding:1px 6px;font-weight:800"></span>
        </span>
        <span id="dvr-adr-status" style="display:none;font-size:10px;font-weight:600;
          color:#f87171;width:100%;text-align:center"></span>
      </button>
      <div id="adr-rec-info">
      </div>
    </div>
    <!-- UNIFIED ACTIONS — categories + items together -->
    <div id="adr-unified-content" class="hidden">
      <div class="adr-section">
        <div style="display:flex;gap:6px">
          <!-- Categories column -->
          <div style="flex:1">
            <div class="adr-section-title">Categories</div>
            <div class="adr-sel-row" style="margin-bottom:4px">
              <button class="btn-ghost" onclick="selAllCats(true)">☑ All</button>
              <button class="btn-ghost" onclick="selAllCats(false)">☐ None</button>
            </div>
            <div class="adr-count" id="adr-cat-count" style="margin-bottom:0">0 selected</div>
          </div>
          <!-- Divider -->
          <div style="width:1px;background:var(--bdr);flex-shrink:0;margin:0 2px"></div>
          <!-- Items column -->
          <div style="flex:1">
            <div class="adr-section-title">Items</div>
            <div class="adr-sel-row" style="margin-bottom:4px">
              <button class="btn-ghost" onclick="selAll(true)">☑ All</button>
              <button class="btn-ghost" onclick="selAll(false)">☐ None</button>
            </div>
            <div class="adr-count" id="adr-item-count" style="margin-bottom:0">0 selected</div>
          </div>
        </div>
      </div>
      <div class="adr-section">
        <div class="adr-section-title">Export Selected</div>
        <button class="adr-btn btn-blue" id="adr-dlm3u" onclick="dlSelectedAll('m3u')" disabled>
          <span class="adr-ico">💾</span>
          <span class="adr-lbl">Export selected → M3U</span>
          <span class="adr-sub" id="adr-m3u-sub"></span>
        </button>
        <button class="adr-btn btn-acc" id="adr-dlmkv" onclick="dlSelectedAll('mkv')" disabled>
          <span class="adr-ico">🎬</span>
          <span class="adr-lbl">Download selected → MKV</span>
          <span class="adr-sub" id="adr-mkv-sub"></span>
        </button>
      </div>
      <div class="adr-section">
        <div class="adr-section-title">Visibility</div>
        <button class="adr-btn btn-ghost" id="adr-hide-sel" onclick="hideSelectedAll()" disabled>
          <span class="adr-ico">🚫</span>
          <span class="adr-lbl">Hide selected</span>
          <span class="adr-sub" id="adr-hide-sub"></span>
        </button>
        <button class="adr-btn btn-ghost" onclick="closeDrawer();openHiddenManager()" style="margin-top:6px">
          <span class="adr-ico">👁</span>
          <span class="adr-lbl">Manage hidden</span>
          <span class="adr-sub" id="adr-hidden-count"></span>
        </button>
      </div>
    </div>
    <!-- M3U export inline progress — only shown during M3U saves -->
    <div id="adr-m3u-progress">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
        <span style="font-size:10px;font-weight:800;text-transform:uppercase;
          letter-spacing:1px;color:var(--acc)">💾 Saving M3U…</span>
        <button onclick="dismissM3uProgress()" title="Dismiss"
          style="background:none;border:none;color:var(--txt3);cursor:pointer;
                 font-size:13px;line-height:1;padding:0">✕</button>
      </div>
      <div id="adr-m3u-prog-label" style="font-size:11px;color:var(--txt2);
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:4px"></div>
      <div id="adr-m3u-prog-bar-wrap">
        <div id="adr-m3u-prog-bar"></div>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span id="adr-m3u-prog-count" style="font-size:11px;color:var(--txt3);font-weight:600"></span>
        <button onclick="doStop()" title="Stop"
          style="height:22px;padding:0 8px;font-size:10px;font-weight:700;
                 background:rgba(255,80,80,.15);border:1px solid rgba(255,80,80,.3);
                 border-radius:4px;cursor:pointer;color:#f06060">⏹ Stop</button>
      </div>
    </div>
  </div>
</div>

<div id="toasts"></div>

<!-- HIDDEN ITEMS MANAGER -->
<div id="hidden-overlay" onclick="if(event.target===this)closeHiddenManager()"
  style="display:none;position:fixed;inset:0;z-index:950;background:rgba(0,0,0,.65);
         align-items:center;justify-content:center">
  <div style="background:var(--s2);border:1px solid var(--bdr);border-radius:var(--r);
              width:min(440px,93vw);max-height:80vh;display:flex;flex-direction:column;overflow:hidden">
    <div style="display:flex;align-items:center;padding:13px 16px;border-bottom:1px solid var(--bdr);flex-shrink:0">
      <span style="flex:1;font-size:14px;font-weight:800;color:var(--txt)">🚫 Hidden</span>
      <button class="btn-ghost" onclick="closeHiddenManager()" style="height:28px;padding:0 10px;font-size:12px">✕</button>
    </div>
    <div style="display:flex;gap:5px;padding:9px 12px 8px;border-bottom:1px solid var(--bdr);flex-shrink:0">
      <button id="hm-tab-live"   onclick="hmSetMode('live')"   style="flex:1;height:28px;font-size:11px;font-weight:500;cursor:pointer;background:transparent;border:1px solid var(--bdr);border-radius:var(--rsm);color:var(--txt);transition:all .15s">Live</button>
      <button id="hm-tab-vod"    onclick="hmSetMode('vod')"    style="flex:1;height:28px;font-size:11px;font-weight:500;cursor:pointer;background:transparent;border:1px solid var(--bdr);border-radius:var(--rsm);color:var(--txt);transition:all .15s">VOD</button>
      <button id="hm-tab-series" onclick="hmSetMode('series')" style="flex:1;height:28px;font-size:11px;font-weight:500;cursor:pointer;background:transparent;border:1px solid var(--bdr);border-radius:var(--rsm);color:var(--txt);transition:all .15s">Series</button>
    </div>
    <div style="display:flex;gap:0;padding:7px 12px;border-bottom:1px solid var(--bdr);flex-shrink:0">
      <button id="hm-sub-items" onclick="hmSetSubView('items')"
        style="flex:1;height:26px;font-size:11px;cursor:pointer;
               border:1px solid var(--bdr);border-radius:var(--rsm) 0 0 var(--rsm);
               background:transparent;color:var(--txt);transition:all .15s">📋 Items</button>
      <button id="hm-sub-cats" onclick="hmSetSubView('cats')"
        style="flex:1;height:26px;font-size:11px;cursor:pointer;
               border:1px solid var(--bdr);border-left:none;border-radius:0 var(--rsm) var(--rsm) 0;
               background:transparent;color:var(--txt);transition:all .15s">📁 Categories</button>
    </div>
    <div id="hm-list" style="flex:1;overflow-y:auto;min-height:60px"></div>
    <div style="display:flex;align-items:center;gap:8px;padding:9px 14px;border-top:1px solid var(--bdr);flex-shrink:0">
      <span id="hm-count" style="flex:1;font-size:11px;color:var(--txt3)"></span>
      <button id="hm-clear-btn" onclick="hmClearAll()"
        style="height:28px;padding:0 12px;font-size:11px;cursor:pointer;background:rgba(248,113,113,.1);
               border:1px solid rgba(248,113,113,.3);border-radius:var(--rsm);color:#f87171">Clear All</button>
    </div>
  </div>
</div>
`;
  while(d.firstChild) document.body.appendChild(d.firstChild);
})();

// ── RECORDING ──────────────────────────────────────────────
async function togRec(){isRec?stopRec():startRec();}

// Record button in the Actions drawer — extends togRec with selected-item support.
// If 1 item is selected and no stream is playing, resolves its URL and records that.
async function adrRecBtn(){
  if(isRec){ stopRec(); return; }
  // If a stream is already playing, record it as normal
  if(pUrl){ startRec(); return; }
  // If exactly 1 item is selected, resolve and record it
  if(typeof selSet !== 'undefined' && selSet.size === 1){
    const it = [...selSet][0];
    if(!it){ startRec(); return; } // fallback — will show the "play first" toast
    toast('Resolving stream…','info');
    try{
      const r = await fetch('/api/resolve_url',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({item:it, mode:typeof mode!=='undefined'?mode:'live',
                             category:typeof curCat!=='undefined'?curCat:{}})});
      const d = await r.json();
      if(d.error){ toast('Could not resolve stream: '+d.error,'err'); return; }
      // Temporarily set pUrl/pName so startRec() picks them up
      pUrl = d.url;
      pName = it.name || it.o_name || '';
      startRec();
    }catch(e){ toast('Resolve error: '+e.message,'err'); }
    return;
  }
  // Nothing playing, nothing selected — fall through to normal error toast
  startRec();
}

async function startRec(){
  if(!pUrl){toast('Play a stream first','wrn');return;}
  const od=document.getElementById('o-dir').value.trim();
  const r=await fetch('/api/record/start',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:pUrl, name:pName, out_dir:od})});
  const d=await r.json();
  if(!d.ok){toast(d.error||'Record failed','err');return;}
  isRec=true;
  _syncRecBtn(true);
  document.getElementById('rfname').textContent=d.filename||'';
  const adrFname=document.getElementById('adr-rec-fname');
  if(adrFname){ adrFname.textContent=d.filename||''; adrFname.style.display=d.filename?'':'none'; }
  toast('⏺ Recording: '+(d.filename||''),'ok');
  // Open Downloads manager so the live recording card is immediately visible
  if(typeof dlmOpen === 'function') dlmOpen();
  // Immediately update the Downloads button badge/status — works even before the overlay
  // has ever been opened (dlmRefresh requires _dlmInited which dlmInit sets on first open)
  fetch('/api/dlm/jobs').then(r=>r.json()).then(j=>{
    if(Array.isArray(j)){ _dlmActive=j; _dlmBadgeUpdate(); }
  }).catch(()=>{});
  let s=0;
  recTmr=setInterval(()=>{
    s++;
    const h=String(Math.floor(s/3600)).padStart(2,'0');
    const m2=String(Math.floor(s%3600/60)).padStart(2,'0');
    const sc=String(s%60).padStart(2,'0');
    const ts=h+':'+m2+':'+sc;
    document.getElementById('rtimer').textContent=ts;
    // Keep button texts in sync — time in label only, no separate timer div
    const btn=document.getElementById('rbtn');
    if(btn) btn.textContent=`⏹ Stop Recording ${ts}`;
    const adrLabel=document.getElementById('adr-rec-btn-label');
    if(adrLabel) adrLabel.textContent=`⏹ Stop Recording ${ts}`;
  },1000);
}

async function stopRec(){
  const r=await fetch('/api/record/stop',{method:'POST',
    headers:{'Content-Type':'application/json'},body:'{}'});
  const d=await r.json();
  if(d.ok) toast('Saved: '+(d.file||''),'ok');
  isRec=false;
  _syncRecBtn(false);
  document.getElementById('rfname').textContent='';
  const adrFname=document.getElementById('adr-rec-fname');
  if(adrFname){ adrFname.textContent=''; adrFname.style.display='none'; }
  if(recTmr){clearInterval(recTmr);recTmr=null;}
  fetch('/api/dlm/jobs').then(r=>r.json()).then(j=>{
    if(Array.isArray(j)){ _dlmActive=j; _dlmBadgeUpdate(); }
  }).catch(()=>{});
}

function _syncRecBtn(recording){
  const btn=document.getElementById('rbtn');
  const btnMob=document.getElementById('rbtn-mob');
  const timer=document.getElementById('rtimer');
  const adrBtn=document.getElementById('adr-rec-btn');
  if(btn){
    if(recording){
      btn.textContent='⏹ Stop Recording';
      btn.classList.add('rec');
      if(timer) timer.classList.add('vis');
    } else {
      btn.textContent='⏺ Record';
      btn.classList.remove('rec');
      if(timer){timer.classList.remove('vis'); timer.textContent='00:00:00';}
    }
  }
  if(btnMob){
    if(recording){
      btnMob.textContent='⏹ Stop';
      btnMob.classList.add('rec');
    } else {
      btnMob.textContent='⏺ Record';
      btnMob.classList.remove('rec');
    }
  }
  if(adrBtn){
    const adrLabel=document.getElementById('adr-rec-btn-label');
    if(recording){
      if(adrLabel) adrLabel.textContent='⏹ Stop Recording';
      adrBtn.classList.add('rec');
    } else {
      if(adrLabel) adrLabel.textContent='⏺ Record';
      adrBtn.classList.remove('rec');
    }
  }
  // adr-rec-info intentionally NOT toggled — div is empty, nothing to show
}

// ── DOWNLOADS ──────────────────────────────────────────────
// Show the progress panel immediately (before the server responds)
// so even very fast exports are always visible.
function _showProgressNow(ctx, title, label, total){
  if(ctx !== 'm3u_inline'){ if(typeof dlmOpen === 'function') dlmOpen(); return; }
  const panel = document.getElementById('adr-m3u-progress');
  if(!panel) return;
  panel.classList.add('active');
  const lbl   = document.getElementById('adr-m3u-prog-label');
  const bar   = document.getElementById('adr-m3u-prog-bar');
  const count = document.getElementById('adr-m3u-prog-count');
  if(lbl)   lbl.textContent = label;
  if(bar){  bar.style.width = '0%'; bar.style.animation = 'adr-m3u-indeterminate 1.2s linear infinite'; bar.style.opacity='0.55'; }
  if(count) count.textContent = total > 0 ? `0 / ${total} items` : 'Starting\u2026';
  // Open actions drawer so the panel is visible
  if(typeof openDrawer === 'function') openDrawer('items');
  else if(typeof openActTab === 'function') openActTab();
}

async function dlM3U(){
  const op=document.getElementById('o-m3u').value.trim();
  if(!op){toast('Set M3U output path first','wrn');return;}
  if(!selSet.size){toast('Select items first','wrn');return;}
  setBusy(true);
  _showProgressNow('m3u_inline','💾 Saving M3U…', curCat?curCat.title:'', selSet.size);
  const r=await fetch('/api/download/m3u',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items:[...selSet],category:curCat,mode,out_path:op,total_hint:selSet.size})});
  const d=await r.json();
  if(d.ok){toast(d.message,'ok');pollBusy();setTimeout(()=>dlmRefresh().catch(()=>{}),1000);}else{toast(d.error,'err');setBusy(false);}
}

// Mobile MKV button — opens Actions drawer if download in progress, else downloads
window._mobMkvClick = function(){
  const stopBtn = document.getElementById('stopbtn');
  if(stopBtn && !stopBtn.disabled) openActTab();  // busy = stopbtn enabled
  else dlNowMKV();
};

async function dlNowMKV(){
  if(!pUrl){toast('No stream playing','wrn');return;}
  const od=document.getElementById('o-dir').value.trim();
  if(!od){toast('Set output folder first','wrn');return;}
  // Build a minimal item from the currently playing stream
  const nowItem = (pIdx>=0 && filtItems[pIdx]) ? filtItems[pIdx] : {name:pName, _direct_url:pUrl};
  setBusy(true);
  _showProgressNow('items','⬇ Downloading MKV…', nowItem.name||pName, 1);
  const r=await fetch('/api/download/mkv',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items:[nowItem],category:curCat,mode,out_dir:od,use_fallback:true})});
  const d=await r.json();
  if(d.ok){toast(d.message,'ok');pollBusy();setTimeout(()=>dlmRefresh().catch(()=>{}),1000);}else{toast(d.error,'err');setBusy(false);}
}

async function dlMKV(){
  const od=document.getElementById('o-dir').value.trim();
  if(!od){toast('Set output folder first','wrn');return;}
  if(!selSet.size){toast('Select items first','wrn');return;}
  setBusy(true);
  _showProgressNow('items','⬇ Downloading MKV…', curCat?curCat.title:'', selSet.size);
  const r=await fetch('/api/download/mkv',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items:[...selSet],category:curCat,mode,out_dir:od,
      use_fallback:true})});
  const d=await r.json();
  if(d.ok){toast(d.message,'ok');pollBusy();setTimeout(()=>dlmRefresh().catch(()=>{}),1000);}else{toast(d.error,'err');setBusy(false);}
}


// ── STOP ───────────────────────────────────────────────────
async function doStop(){
  await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  setBusy(false); toast('Stopped','info');
  _dlActive=false; _dlTaskType=''; _dlItemNames=[];
  _refreshDlButtons();
}

// ── POLLING ────────────────────────────────────────────────
async function pollBusy(){
  const r=await fetch('/api/status').catch(()=>null); if(!r) return;
  const d=await r.json().catch(()=>null); if(!d) return;
  if(d.status){ const _con=document.getElementById('cdot').classList.contains('on'); if(!_con) setStatus(d.status); }
  updateTaskProgress(d);
  _syncDlState(d);
  if(d.busy){
    setTimeout(pollBusy,800);
  } else {
    setBusy(false);
    // Freeze M3U progress panel with final count, then auto-dismiss after 3s
    const _m3uPanel = document.getElementById('adr-m3u-progress');
    if(_m3uPanel && _m3uPanel.classList.contains('active')){
      const _ls = await fetch('/api/status').then(r=>r.json()).catch(()=>({}));
      const _fd=_ls.task_done||0, _ft=_ls.task_total||0, _fs=_ls.task_skipped||0;
      const _bar=document.getElementById('adr-m3u-prog-bar');
      const _cnt=document.getElementById('adr-m3u-prog-count');
      if(_bar){ _bar.style.animation=''; _bar.style.opacity='1'; _bar.style.width='100%'; }
      if(_cnt){ const _sk=_fs>0?` \u00b7 ${_fs} skipped`:'';
        _cnt.textContent=_ft>0?`${_fd} / ${_ft} items${_sk}`:(_fd>0?`${_fd} items${_sk}`:'Complete'); }
      // Panel stays visible — user dismisses via the ✕ button
    }
    // Refresh Downloads manager to show completed jobs
    fetch('/api/dlm/jobs').then(r=>r.json()).then(j=>{
      if(Array.isArray(j)){ _dlmActive=j; _dlmBadgeUpdate(); }
    }).catch(()=>{});
    if(typeof dlmRefresh==='function' && document.getElementById('dlm-overlay')?.style.display==='flex')
      dlmRefresh().catch(()=>{});
  }
}
function dismissM3uProgress(){
  const panel = document.getElementById('adr-m3u-progress');
  if(panel) panel.classList.remove('active');
}
function dismissProgress(ctx){ dismissM3uProgress(); }
function updateTaskProgress(d){
  if(!document.getElementById('adr-m3u-progress')?.classList.contains('active')) return;
  const type    = d.task_type    || '';
  const done    = d.task_done    || 0;
  const total   = d.task_total   || 0;
  const skipped = d.task_skipped || 0;
  if(type !== 'm3u') return;
  const bar   = document.getElementById('adr-m3u-prog-bar');
  const count = document.getElementById('adr-m3u-prog-count');
  const pct   = total > 0 ? Math.round(done / total * 100) : 0;
  if(bar){ bar.style.animation = ''; bar.style.width = pct + '%'; }
  if(count){
    const skipTxt = skipped > 0 ? ` \u00b7 ${skipped} skipped` : '';
    count.textContent = total > 0
      ? `${done} / ${total} items${skipTxt}`
      : (done > 0 ? `${done} items saved${skipTxt}` : 'Starting\u2026');
  }
}
// Adaptive status poll: 4s when busy or recording, 15s when idle.
// Replaces the old fixed 5s setInterval which hammered /api/status
// even when nothing was happening.
let _statusPollTimer = null;
async function _statusPoll(){
  clearTimeout(_statusPollTimer);
  const r=await fetch('/api/status').catch(()=>null); if(!r){ _statusPollTimer=setTimeout(_statusPoll,15000); return; }
  const d=await r.json().catch(()=>null); if(!d){ _statusPollTimer=setTimeout(_statusPoll,15000); return; }
  if(d.status){ const _con=document.getElementById('cdot').classList.contains('on'); if(!_con) setStatus(d.status); }
  if(!d.busy) setBusy(false);
  updateTaskProgress(d);
  _syncDlState(d);
  // Sync recording button if server state differs from JS state (e.g. page reload)
  if(d.recording && !isRec){
    isRec=true; _syncRecBtn(true);
    // Resync elapsed time from server
    fetch('/api/record/status').then(r=>r.json()).then(rs=>{
      if(rs.recording){
        document.getElementById('rfname').textContent=rs.filename||'';
        const adrFname=document.getElementById('adr-rec-fname');
        if(adrFname){ adrFname.textContent=rs.filename||''; adrFname.style.display=rs.filename?'':'none'; }
        // Restart timer from server elapsed
        if(recTmr){clearInterval(recTmr);recTmr=null;}
        const parts=(rs.elapsed||'00:00:00').split(':').map(Number);
        let s=parts[0]*3600+parts[1]*60+parts[2];
        recTmr=setInterval(()=>{
          s++;
          const h=String(Math.floor(s/3600)).padStart(2,'0');
          const m2=String(Math.floor(s%3600/60)).padStart(2,'0');
          const sc=String(s%60).padStart(2,'0');
          const ts=h+':'+m2+':'+sc;
          document.getElementById('rtimer').textContent=ts;
          const btn=document.getElementById('rbtn');
          if(btn) btn.textContent=`⏹ Stop Recording ${ts}`;
          const adrLabel=document.getElementById('adr-rec-btn-label');
          if(adrLabel) adrLabel.textContent=`⏹ Stop Recording ${ts}`;
        },1000);
      }
    }).catch(()=>{});
  } else if(!d.recording && isRec){
    isRec=false; _syncRecBtn(false);
    document.getElementById('rfname').textContent='';
    if(recTmr){clearInterval(recTmr);recTmr=null;}
  }
  // Schedule next poll: fast (4s) when active, slow (15s) when idle
  const _nextPoll = (d.busy || d.recording) ? 4000 : 15000;
  _statusPollTimer = setTimeout(_statusPoll, _nextPoll);
}
_statusPoll(); // kick off immediately on page load

// ── SSE LOGS ───────────────────────────────────────────────
function startLog(){
  if(logEs) logEs.close();
  logEs=new EventSource('/api/logs');
  logEs.onmessage=e=>{
    const msg=e.data;
    if(msg==='Connected to log stream') return;
    let c='';
    if(msg.includes('[STATUS]')){c='s'; setStatus(msg.replace(/\[STATUS\]\s*/,''));}
    else if(/✓|success|saved|Done/i.test(msg)) c='k';
    else if(/✗|error|failed|ERROR/i.test(msg)) c='e';
    else if(/warn|⚠/i.test(msg)) c='w';
    else if(/\[MKV\]|\[SERIES\]|\[REC\]/i.test(msg)) c='m';
    else if(/▶|Playing/i.test(msg)) c='i';
    alog(msg.replace(/\[STATUS\]\s*/,'').trim(),c);
  };
  logEs.onerror=()=>setTimeout(startLog,3000);
}

// ── HELPERS ────────────────────────────────────────────────
// Log entries are buffered and flushed once per animation frame.
// This prevents the forced synchronous reflow (scrollHeight read) from
// blocking the main thread on every incoming SSE message.
let _logBuf = [];
let _logRafPending = false;

function _flushLog(){
  _logRafPending = false;
  if(!_logBuf.length) return;
  const entries = _logBuf.splice(0);
  ['logout','desktop-logout'].forEach(id=>{
    const out = document.getElementById(id); if(!out) return;
    const frag = document.createDocumentFragment();
    entries.forEach(({msg, cls})=>{
      const d = document.createElement('div');
      d.className = 'll' + (cls ? ' l'+cls : '');
      d.textContent = msg;
      frag.appendChild(d);
    });
    out.appendChild(frag);
    // Trim to 600 lines
    while(out.children.length > 600) out.removeChild(out.firstChild);
    // Single scroll — reads scrollHeight only once per frame
    out.scrollTop = out.scrollHeight;
  });
}

function alog(msg, cls){
  _logBuf.push({msg, cls});
  if(!_logRafPending){
    _logRafPending = true;
    requestAnimationFrame(_flushLog);
  }
}
function clearLog(){
  ['logout','desktop-logout'].forEach(id=>{
    const el=document.getElementById(id); if(el) el.innerHTML='';
  });
}
function toggleDesktopLog(){
  const panel = document.getElementById('desktop-log');
  if(!panel) return;
  const expanded = panel.classList.toggle('expanded');
  // After expand, scroll log to bottom
  if(expanded){
    const out = document.getElementById('desktop-logout');
    if(out) setTimeout(()=>{ out.scrollTop = out.scrollHeight; }, 260);
  }
}
let _activityStatusTimer=null;
function setStatus(m){
  const btn=document.getElementById('conn-btn');
  const connected=document.getElementById('cdot').classList.contains('on');
  btn.classList.toggle('connected',connected);
  btn.style.cursor=connected?'pointer':'default';
  const isConnMsg = m.startsWith('Connected') || m.startsWith('Connecting') ||
                    m.startsWith('Error') || !connected;
  // Short label for mobile header (hdr-status-short shown via CSS on <600px screens)
  const shortEl=document.getElementById('hdr-status-short');
  if(shortEl){
    if(m.startsWith('Connected')) shortEl.textContent='Online';
    else if(m.startsWith('Connecting')) shortEl.textContent='Wait…';
    else if(m.startsWith('Error')) shortEl.textContent='Error';
    else shortEl.textContent='Offline';
  }
  if(!isConnMsg){
    const act=document.getElementById('activity-status');
    if(act){
      act.textContent=m; act.style.opacity='1';
      clearTimeout(_activityStatusTimer);
      _activityStatusTimer=setTimeout(()=>{act.style.opacity='0';
        setTimeout(()=>{if(act.style.opacity==='0')act.textContent='';},300);},4000);
    }
    return;
  }
  document.getElementById('hdr-status').textContent=m;
  const act=document.getElementById('activity-status');
  if(act){clearTimeout(_activityStatusTimer);act.textContent='';act.style.opacity='1';}
}
function setBusy(v){
  document.getElementById('busy-sp').classList.toggle('hidden',!v);
  document.getElementById('cbtn').disabled=v;
  document.getElementById('stopbtn').disabled=!v;
}

// ── DOWNLOAD-AWARE BUTTON SYNC ──────────────────────────────
// Called whenever we receive a fresh /api/status payload.
// Updates _dlActive/_dlTaskType/_dlItemNames and refreshes the two
// "Download MKV" buttons that live outside the Action drawer:
//   • dl-now-btn  — in the Player controls bar
//   • imenu-mkv   — in the item context menu
function _syncDlState(d){
  _dlActive    = !!(d.busy && d.task_type);
  _dlTaskType  = d.task_type || '';
  _dlItemNames = Array.isArray(d.task_item_names) ? d.task_item_names : [];
  _refreshDlButtons();
}

function _refreshDlButtons(){
  const mkvRunning = _dlActive && _dlTaskType === 'mkv';

  // ── dl-now-btn (Player controls bar) ─────────────────────
  const dnBtn = document.getElementById('dl-now-btn');
  if(dnBtn){
    if(mkvRunning){
      dnBtn.innerHTML = '⏹ Stop';
      dnBtn.title     = 'Stop current MKV download';
      dnBtn.onclick   = ()=>doStop();
      dnBtn.disabled  = false;
      dnBtn.style.color       = 'var(--acc,#f87171)';
      dnBtn.style.borderColor = 'var(--acc,#f87171)';
    } else {
      dnBtn.innerHTML = '⬇ MKV';
      dnBtn.title     = 'Download currently playing item as MKV';
      dnBtn.onclick   = ()=>dlNowMKV();
      dnBtn.disabled  = !pUrl;
      dnBtn.style.color       = '';
      dnBtn.style.borderColor = '';
    }
  }

  // ── dl-now-btn-mob (mobile Player controls bar) ───────────
  const dnBtnMob = document.getElementById('dl-now-btn-mob');
  if(dnBtnMob){
    if(mkvRunning){
      dnBtnMob.innerHTML = '⏹ Stop';
      dnBtnMob.title     = 'Stop current MKV download';
      dnBtnMob.onclick   = ()=>doStop();
      dnBtnMob.disabled  = false;
      dnBtnMob.style.color       = 'var(--acc,#f87171)';
      dnBtnMob.style.borderColor = 'var(--acc,#f87171)';
    } else {
      dnBtnMob.innerHTML = '⬇ MKV';
      dnBtnMob.title     = 'Download currently playing item as MKV';
      dnBtnMob.onclick   = ()=>dlNowMKV();
      dnBtnMob.disabled  = !pUrl;
      dnBtnMob.style.color       = '';
      dnBtnMob.style.borderColor = '';
    }
  }

  // ── imenu-mkv (item context menu) ────────────────────────
  const imBtn = document.getElementById('imenu-mkv');
  if(!imBtn) return;
  if(mkvRunning){
    imBtn.innerHTML = '<span class="imenu-ico">⏹</span>Stop Download';
    imBtn.onclick   = ()=>{ closeItemMenu(); doStop(); };
    imBtn.style.color = 'var(--acc,#f87171)';
  } else {
    imBtn.innerHTML = '<span class="imenu-ico">⬇</span>Download MKV';
    imBtn.onclick   = iMenuMKV;
    imBtn.style.color = '';
  }
}
// Stub overwritten by orientation manager on mobile
window._orientOnTabSwitch = function(){};
function showT(pid,tid){
  if(window.innerWidth>=900) return;
  _switchTab(pid,tid);
}
function forceTab(pid,tid){
  // always switch on mobile regardless of current state
  if(window.innerWidth>=900) return;
  _switchTab(pid,tid);
}
function _switchTab(pid,tid){
  if(typeof closeCP === 'function') closeCP();
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  const panel=document.getElementById(pid);
  if(panel) panel.classList.add('active');
  document.querySelectorAll('.nt').forEach(b=>b.classList.remove('on'));
  const tab=document.getElementById(tid);
  if(tab) tab.classList.add('on');
  _orientOnTabSwitch(pid);
}
function toast(msg,type){
  const el=document.createElement('div');
  const map={ok:'tok2',err:'terr2',info:'tinfo',wrn:'twrn2'};
  el.className='toast '+(map[type]||'tinfo');
  el.textContent=msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(()=>{el.style.opacity='0';el.style.transform='translateY(8px)';
    el.style.transition='.3s'; setTimeout(()=>el.remove(),300);},2700);
}
function togSug(w){
  const el=document.getElementById('sg-'+w);
  const was=el.classList.contains('open');
  document.querySelectorAll('.psug').forEach(e=>e.classList.remove('open'));
  if(!was) el.classList.add('open');
}
function pickP(w,v){
  document.getElementById({m3u:'o-m3u',dir:'o-dir',dvr:'o-dvr'}[w]).value=v;
  document.getElementById('sg-'+w).classList.remove('open');
  saveFP();
}
document.addEventListener('click',e=>{
  if(!e.target.closest('.prow'))
    document.querySelectorAll('.psug').forEach(el=>el.classList.remove('open'));
});
function saveFP(){
  try{localStorage.setItem('mkv_folder',document.getElementById('o-dir').value);}catch(e){}
  try{localStorage.setItem('m3u_path',document.getElementById('o-m3u').value);}catch(e){}
  try{const dv=document.getElementById('o-dvr');if(dv)localStorage.setItem('dvr_folder',dv.value);}catch(e){}
}
// ── Shared output-path mobile browser ─────────────────────────────────────
let _outFbTarget     = 'm3u';
let _outFbCurPath    = '/sdcard/Download';
let _outFbOpen       = false;
let _outFbMobileMode = false;

// localStorage key map — source of truth for all three paths
const _outFbLsKey = {m3u:'m3u_path', dir:'mkv_folder', dvr:'dvr_folder'};

// ── State synchronisation ──────────────────────────────────────────────────
function _outFbSyncReadouts(){
  // Read from localStorage — never from display:none inputs which can return ''
  const spanMap = {m3u:'out-mob-m3u', dir:'out-mob-dir', dvr:'out-mob-dvr'};
  for(const [k, lsKey] of Object.entries(_outFbLsKey)){
    const span = document.getElementById(spanMap[k]);
    if(span){
      const v = localStorage.getItem(lsKey) || '';
      span.textContent = v || '(not set)';
    }
  }
}

function _outFbApplyState(){
  const desktop = document.getElementById('out-paths-desktop');
  const mobile  = document.getElementById('out-paths-mobile');
  const btn     = document.getElementById('out-fb-toggle');
  if(desktop) desktop.style.display = _outFbOpen ? 'none' : '';
  if(mobile)  mobile.style.display  = _outFbOpen ? 'flex' : 'none';
  const epDesktop = document.getElementById('extplayer-row-desktop');
  const epMobile  = document.getElementById('extplayer-row-mobile');
  if(epDesktop) epDesktop.style.display = _outFbOpen ? 'none' : '';
  if(epMobile)  epMobile.style.display  = _outFbOpen ? 'flex' : 'none';
  if(btn){
    btn.textContent = _outFbOpen
      ? '\uD83D\uDCC1 File browser: On'
      : '\uD83D\uDCC1 File browser: Off';
    btn.style.background  = _outFbOpen ? 'rgba(124,58,237,.25)' : '';
    btn.style.borderColor = _outFbOpen ? 'var(--acc)' : '';
    btn.style.color       = _outFbOpen ? 'var(--txt)' : '';
  }
  if(_outFbOpen) _outFbSyncReadouts();
}

function outFbToggle(){
  _outFbOpen = !_outFbOpen;
  _outFbApplyState();
  if(_outFbOpen) outFbSetTarget(_outFbTarget);
}
function outFbClose(){
  _outFbOpen = false;
  _outFbApplyState();
}

function outFbSetTarget(t){
  _outFbTarget = t;
  document.querySelectorAll('.out-fb-tgt').forEach(b=>b.classList.remove('active'));
  const tb = document.getElementById('out-fb-tgt-'+t);
  if(tb) tb.classList.add('active');
  // Filename row — only for M3U
  const fnRow = document.getElementById('out-fb-fname-row');
  if(fnRow) fnRow.style.display = (t==='m3u') ? 'flex' : 'none';
  // Show only the correct preset group, hide the others
  ['m3u','dir','dvr'].forEach(k=>{
    const el = document.getElementById('out-fb-'+k+'-presets');
    if(el) el.style.display = (k===t) ? 'flex' : 'none';
  });
  if(t==='m3u'){
    // Pre-fill filename from localStorage
    const cur  = localStorage.getItem('m3u_path') || '';
    const base = cur.split('/').pop() || 'playlist.m3u';
    const inp  = document.getElementById('out-fb-fname');
    if(inp) inp.value = base;
    // Start path = directory of saved M3U path
    const dir = cur.includes('/') ? cur.replace(/\/[^/]+$/, '') : _outFbCurPath;
    if(dir && dir !== _outFbCurPath){
      _outFbCurPath = dir;
      const pathEl = document.getElementById('out-fb-path');
      if(pathEl) pathEl.textContent = dir;
    }
  }
  outFbNav(_outFbCurPath);
}

function outFbUp(){
  const cur = document.getElementById('out-fb-path')?.textContent || _outFbCurPath;
  outFbNav(cur.replace(/\/[^/]+\/?$/, '') || '/');
}

async function outFbNav(path){
  path = path.replace(/\/+$/, '') || '/';
  _outFbCurPath = path;
  const listEl = document.getElementById('out-fb-list');
  const pathEl = document.getElementById('out-fb-path');
  const upBtn  = document.getElementById('out-fb-up');
  if(pathEl) pathEl.textContent = path;
  if(listEl) listEl.innerHTML = '<div style="padding:8px;font-size:12px;color:var(--txt3)">Loading\u2026</div>';
  const dirsOnly = (_outFbTarget !== 'm3u');
  try{
    const r = await fetch('/api/browse_dir',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path, dirs_only:dirsOnly})});
    const d = await r.json();
    if(upBtn) upBtn.disabled = !d.parent;
    const rows = [];
    for(const name of (d.dirs||[])){
      const fp = path.replace(/\/+$/,'') + '/' + name;
      rows.push(`<div class="sub-fb-row sub-fb-dir" onclick="outFbNav('${esc(fp)}')">
        <span class="sub-fb-icon">\uD83D\uDCC1</span><span class="sub-fb-name">${esc(name)}</span><span class="sub-fb-arr">\u203a</span>
      </div>`);
    }
    for(const name of (d.files||[])){
      const fp = path.replace(/\/+$/,'') + '/' + name;
      rows.push(`<div class="sub-fb-row sub-fb-file" onclick="outFbPickFile('${esc(fp)}')">
        <span class="sub-fb-icon">\uD83D\uDCC4</span><span class="sub-fb-name">${esc(name)}</span>
      </div>`);
    }
    if(!rows.length) rows.push('<div style="padding:8px;font-size:12px;color:var(--txt3)">No items here.</div>');
    if(listEl) listEl.innerHTML = rows.join('');
    if(d.error && !d.dirs.length && !d.files.length && listEl){
      listEl.innerHTML = `<div style="padding:8px;font-size:12px;color:#f87171">\u26a0 ${esc(d.error)}</div>`;
    }
  }catch(e){
    if(listEl) listEl.innerHTML = `<div style="padding:8px;font-size:12px;color:#f87171">\u26a0 ${esc(String(e))}</div>`;
  }
}

function outFbConfirm(){
  const dir = (document.getElementById('out-fb-path')?.textContent || _outFbCurPath).replace(/\/+$/,'');
  if(_outFbTarget === 'm3u'){
    const inp   = document.getElementById('out-fb-fname');
    const fname = (inp && inp.value.trim()) || 'playlist.m3u';
    outFbApply(dir + '/' + fname);
  } else {
    outFbApply(dir + '/');
  }
}
function outFbPickFile(fp){ outFbApply(fp); }

function outFbApply(val){
  const lsKey = _outFbLsKey[_outFbTarget];
  const idMap  = {m3u:'o-m3u', dir:'o-dir', dvr:'o-dvr'};
  const spanMap= {m3u:'out-mob-m3u', dir:'out-mob-dir', dvr:'out-mob-dvr'};
  // 1. Write to localStorage — primary storage, bypasses all hidden-input issues
  try{ localStorage.setItem(lsKey, val); }catch(e){}
  // 2. Update the hidden desktop input so desktop view and saveFP() stay in sync
  const inp = document.getElementById(idMap[_outFbTarget]);
  if(inp) inp.value = val;
  // 3. Update the visible readout span directly — do NOT re-read inp.value
  const span = document.getElementById(spanMap[_outFbTarget]);
  if(span) span.textContent = val;
  toast('Path set \u2014 ' + (val.split('/').filter(Boolean).pop()||val), 'ok');
}

// Quick-apply a preset path for any target (replaces old outFbApplyM3uPreset)
function outFbQuickApply(fullPath){
  outFbApply(fullPath);
}

// Keep old name as alias in case anything else calls it
function outFbApplyM3uPreset(fullPath){ outFbQuickApply(fullPath); }

// ── Per-row browse button: desktop=tkinter, mobile=shared browser ─────────
async function outBrowseRow(target){
  // Use shared browser if: Mobile toggle is on, or actual mobile device
  const useBrowser = _outFbMobileMode || (typeof _isMobile !== 'undefined' && _isMobile);
  if(useBrowser){
    // Ensure panel is open
    if(!_outFbOpen){
      _outFbOpen = true;
      _outFbApplyState();
    }
    outFbSetTarget(target);
    return;
  }
  // Desktop without mobile mode: try tkinter picker
  const _m3uBase = (document.getElementById('o-m3u')?.value||'').split('/').pop()||'playlist.m3u';
  const apiUrl = (target === 'm3u')
    ? '/api/browse_m3u_file?name='+encodeURIComponent(_m3uBase)
    : '/api/browse_folder';
  try{
    const r = await fetch(apiUrl);
    const d = await r.json();
    if(d.path && d.path.length){
      const idMap = {m3u:'o-m3u', dir:'o-dir', dvr:'o-dvr'};
      const el = document.getElementById(idMap[target]);
      if(el){ el.value = d.path; saveFP(); }
      toast('Path set', 'ok');
      return;
    }
    // d.path empty = user cancelled tkinter dialog — do nothing
    if(!d.error) return;
    // d.error = tkinter unavailable (Android/headless) — open shared browser
  }catch(e){ /* fall through */ }
  // Tkinter unavailable fallback: open shared browser
  _outFbMobileMode = true;
  _outFbOpen = true;
  _outFbApplyState();
  outFbSetTarget(target);
}

function saveExtPlayer(){
  try{localStorage.setItem('ext_player',document.getElementById('o-extplayer').value);}catch(e){}
}
function saveSubKey(){
  // API key field now lives inside the subtitle modal (#sub-apikey)
  try{
    const el=document.getElementById('sub-apikey');
    if(el) localStorage.setItem('opensubtitles_key',el.value.trim());
  }catch(e){}
}
function _getSubKey(){
  // Always read from localStorage; modal input is populated from it on open
  try{return localStorage.getItem('opensubtitles_key')||'';}catch(e){return '';}
}
function saveMobilePlayer(){
  try{localStorage.setItem('mobile_player',document.getElementById('o-mobile-player').value);}catch(e){}
}
async function browseExtPlayer(){
  try{
    const r=await fetch('/api/browse_exe'); const d=await r.json();
    if(d.path){
      document.getElementById('o-extplayer').value=d.path;
      saveExtPlayer();
      toast('External player set: '+d.path.split(/[\\/]/).pop(),'ok');
    }
  }catch(e){toast('Browse failed: '+e,'err');}
}
const _isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
  || ('ontouchstart' in window)
  || (navigator.maxTouchPoints > 1)
  || window.innerWidth <= 900;
// Auto-switch output paths to mobile browser if on mobile — deferred to DOM ready
function _outFbAutoInit(){
  if(_isMobile && !_outFbOpen){
    outFbToggle();
    outFbSetTarget('m3u');
  }
}
if(document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', _outFbAutoInit);
} else {
  _outFbAutoInit();
}

/* ─── Orientation manager (mobile only) ──────────────────────────────────────
   • On player tab  : unlock orientation; auto-fullscreen on landscape rotation.
   • On other tabs  : lock to portrait so rotation is disabled.
   • Exiting fullscreen (back button / swipe down) while landscape → lock portrait
     so the device doesn't immediately re-trigger fullscreen.
────────────────────────────────────────────────────────────────────────────── */
(function(){
  if(!_isMobile) return;                          // desktop — do nothing
  const SO = window.screen && screen.orientation; // ScreenOrientation API
  if(!SO) return;                                 // very old WebView — bail

  let _onPlayerTab = false;

  function _isLandscape(){
    const t = SO.type || '';
    if(t) return t.startsWith('landscape');
    // fallback: compare dimensions
    return window.innerWidth > window.innerHeight;
  }

  function _lockPortrait(){
    try{ SO.lock('portrait').catch(()=>{}); }catch(e){}
  }

  function _unlock(){
    try{ SO.unlock(); }catch(e){}
  }

  function _enterFullscreen(){
    const el = document.getElementById('vid') || document.querySelector('video');
    if(!el) return;
    const req = el.requestFullscreen || el.webkitRequestFullscreen
              || el.mozRequestFullScreen || el.msRequestFullscreen;
    if(req) req.call(el).catch(()=>{});
  }

  function _exitFullscreen(){
    const exit = document.exitFullscreen || document.webkitExitFullscreen
               || document.mozCancelFullScreen || document.msExitFullscreen;
    const inFS  = document.fullscreenElement || document.webkitFullscreenElement;
    if(exit && inFS) exit.call(document).catch(()=>{});
  }

  // Called by _switchTab on every tab change
  window._orientOnTabSwitch = function(pid){
    _onPlayerTab = (pid === 'p-player');
    if(_onPlayerTab){
      _unlock();
      // If already landscape when arriving on player tab → go fullscreen
      if(_isLandscape()) _enterFullscreen();
    } else {
      _exitFullscreen();
      _lockPortrait();
    }
  };

  // Fires whenever the physical device rotates
  SO.addEventListener('change', function(){
    if(!_onPlayerTab){ _lockPortrait(); return; }
    if(_isLandscape()){
      _enterFullscreen();
    } else {
      _exitFullscreen();
    }
  });

  // User manually exits fullscreen (back button / swipe-down) while landscape
  // → lock portrait so it doesn't immediately bounce back into fullscreen
  document.addEventListener('fullscreenchange', function(){
    const inFS = document.fullscreenElement || document.webkitFullscreenElement;
    if(!inFS && _onPlayerTab && _isLandscape()){
      _lockPortrait();
      // Give the OS a moment to settle orientation before we unlock again
      setTimeout(()=>{ if(_onPlayerTab) _unlock(); }, 1200);
    }
  });

  // Lock portrait on startup until the player tab is explicitly opened
  _lockPortrait();
})();

async function openExternal(i){
  const it=filtItems[i]; if(!it) return;
  const name=it.name||it.o_name||'?';

  if(_isMobile){
    toast('Resolving stream…','info');
    try{
      const r=await fetch('/api/resolve_url',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({item:it,mode,category:curCat||{}})});
      const d=await r.json();
      if(d.error){toast('Error: '+d.error,'err');return;}
      const url=d.url;
      const player=localStorage.getItem('mobile_player')||'ask';
      if(player==='copy'){
        try{await navigator.clipboard.writeText(url);toast('Stream URL copied!','ok');}
        catch(e){prompt('Copy stream URL:',url);}
        return;
      }
      if(player==='ask'){
        // No package → Android shows only installed handlers
        // S.browser_fallback_url=about:blank prevents Play Store from opening
        window.location.href=`intent:${url}#Intent;type=video/*;S.browser_fallback_url=about:blank;end`;
      } else {
        // Direct to specific app — S.browser_fallback_url=about:blank prevents Play Store if not installed
        window.location.href=`intent:${url}#Intent;package=${player};type=video/*;S.browser_fallback_url=about:blank;end`;
      }
    }catch(e){toast('Failed: '+e,'err');}
    return;
  }

  // Desktop — original subprocess path
  const exe=(localStorage.getItem('ext_player')||'').trim();
  if(!exe){toast('Set external player path in ⚙ settings first','wrn');return;}
  toast('Opening in external player…','info');
  try{
    const r=await fetch('/api/open_external',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({exe,item:it,mode,category:curCat||{}})});
    const d=await r.json();
    if(d.error) toast('Error: '+d.error,'err');
    else toast('Launched: '+name,'ok');
  }catch(e){toast('Failed: '+e,'err');}
}

function esc(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

"""
