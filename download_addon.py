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
import string
import subprocess
import threading
import time
from datetime import datetime
from urllib.parse import quote as _qe

from flask import request, jsonify


# ── Progress-parsing regexes ──────────────────────────────────────────────────
_time_re    = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")
_bitrate_re = re.compile(r"bitrate=\s*([\d.]+)kbits/s")
_size_re    = re.compile(r"size=\s*(\d+)kB")


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
                    break

                if rc == 0:
                    state.log(f"[MKV] ✓ Saved: {out_path}")
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
                        elif err == "stopped":
                            state.log("[MKV]   yt-dlp stopped by user.")
                        else:
                            state.log(f"[MKV]   ✗ yt-dlp failed: {err}")

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
/* Progress panel inside action drawer */
.adr-progress{background:var(--s3);border:1px solid var(--bdr);border-radius:var(--rsm);
  padding:12px 14px;margin-bottom:14px;display:none}
.adr-progress.active{display:block}
.adr-prog-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:5px}
.adr-prog-title{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1px;
  color:var(--acc)}
.adr-prog-stop{background:rgba(255,80,80,.15);border:1px solid rgba(255,80,80,.3);
  color:#f06060;border-radius:6px;height:22px;padding:0 8px;font-size:11px;cursor:pointer;
  flex-shrink:0;transition:background .15s}
.adr-prog-stop:hover{background:rgba(255,80,80,.35)}
.adr-prog-dismiss{background:rgba(120,120,140,.15);border:1px solid rgba(120,120,140,.3);
  color:var(--txt3);border-radius:6px;height:22px;padding:0 8px;font-size:11px;cursor:pointer;
  flex-shrink:0;transition:background .15s}
.adr-prog-dismiss:hover{background:rgba(120,120,140,.35);color:var(--txt)}
.adr-prog-label{font-size:11px;color:var(--txt2);margin-bottom:7px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.adr-prog-bar-wrap{background:rgba(0,0,0,.5);border-radius:8px;height:7px;overflow:hidden;
  margin-bottom:6px;position:relative;border:1px solid rgba(255,255,255,.05)}
.adr-prog-bar{height:100%;border-radius:8px;width:0%;transition:width .35s ease;
  background:linear-gradient(90deg,var(--acc2),var(--acc),var(--cyan));
  box-shadow:0 0 10px rgba(124,58,237,.5);position:relative;overflow:hidden}
.adr-prog-bar::after{content:'';position:absolute;top:0;left:0;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent);
  transform:translateX(-120%);will-change:transform;
  animation:progSweep 1.6s ease infinite}
@keyframes progSweep{to{transform:translateX(120%)}}
@keyframes adr-indeterminate{
  0%{transform:translateX(-110%)}
  100%{transform:translateX(200%)}
}
.adr-prog-footer{display:flex;align-items:center;justify-content:space-between;gap:6px}
.adr-prog-count{font-size:11px;color:var(--txt3);font-weight:600}
.adr-prog-speed{font-size:11px;color:var(--acc2);font-weight:700;text-align:right}
/* Recording section in action drawer */
#adr-rec-section{margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid var(--bdr)}
#adr-rec-btn{width:100%;height:42px;font-size:13px;font-weight:700;border-radius:var(--rsm);
  display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:8px;
  background:rgba(220,50,50,.15);border:1px solid rgba(220,50,50,.35);color:#f06060;cursor:pointer;transition:background .15s}
#adr-rec-btn:hover{background:rgba(220,50,50,.3)}
#adr-rec-btn.rec{background:rgba(220,50,50,.3);border-color:rgba(220,50,50,.7);animation:recpulse 1.2s ease-in-out infinite}
@keyframes recpulse{0%,100%{box-shadow:0 0 0 0 rgba(220,50,50,.4)}50%{box-shadow:0 0 0 6px rgba(220,50,50,0)}}
#adr-rec-info{display:none;flex-direction:column;gap:4px}
#adr-rec-info.vis{display:flex}
#adr-rec-fname{font-size:11px;color:var(--txt2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#adr-rec-timer{font-size:13px;font-weight:700;color:#f06060;letter-spacing:1px}
#adr-rec-open{width:100%;height:34px;font-size:12px;font-weight:600;margin-top:4px}
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
      <button id="adr-rec-btn" onclick="togRec()">⏺ Record</button>
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
        <div id="adr-rec-timer">00:00:00</div>
        <div id="adr-rec-fname"></div>
        <button class="btn-ghost adr-rec-open" onclick="openDrawer();closeDrawer();" style="width:100%;height:34px;font-size:12px;font-weight:600;margin-top:4px" id="adr-rec-open">📂 Open player controls</button>
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
      <div class="adr-progress" id="adr-progress-items">
        <div class="adr-prog-hdr">
          <div class="adr-prog-title" id="adr-prog-items-title">Downloading...</div>
          <div style="display:flex;gap:5px;align-items:center">
            <button class="adr-prog-stop" id="adr-prog-items-stop" onclick="doStop()" title="Stop download">⏹</button>
            <button class="adr-prog-dismiss" id="adr-prog-items-dismiss" onclick="dismissProgress('items')" title="Dismiss" style="display:none">✕</button>
          </div>
        </div>
        <div class="adr-prog-label" id="adr-prog-items-label"></div>
        <div class="adr-prog-bar-wrap"><div class="adr-prog-bar" id="adr-prog-items-bar"></div></div>
        <div class="adr-prog-footer">
          <div class="adr-prog-count" id="adr-prog-items-count"></div>
          <div class="adr-prog-speed" id="adr-prog-items-speed"></div>
        </div>
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
  const rfmob=document.getElementById('rfname-mob'); if(rfmob) rfmob.textContent=d.filename||'';
  const adrFname=document.getElementById('adr-rec-fname');
  if(adrFname) adrFname.textContent=d.filename||'';
  toast('⏺ Recording: '+(d.filename||''),'ok');
  let s=0;
  recTmr=setInterval(()=>{
    s++;
    const h=String(Math.floor(s/3600)).padStart(2,'0');
    const m2=String(Math.floor(s%3600/60)).padStart(2,'0');
    const sc=String(s%60).padStart(2,'0');
    const ts=h+':'+m2+':'+sc;
    document.getElementById('rtimer').textContent=ts;
    const rtmob=document.getElementById('rtimer-mob');
    if(rtmob) rtmob.textContent=ts;
    const adrTimer=document.getElementById('adr-rec-timer');
    if(adrTimer) adrTimer.textContent=ts;
    // Keep button text in sync with elapsed time
    const btn=document.getElementById('rbtn');
    if(btn) btn.textContent=`⏹ Stop Recording ${ts}`;
    const adrBtn=document.getElementById('adr-rec-btn');
    if(adrBtn) adrBtn.textContent=`⏹ Stop Recording ${ts}`;
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
  const rfmob2=document.getElementById('rfname-mob'); if(rfmob2) rfmob2.textContent='';
  const adrFname=document.getElementById('adr-rec-fname');
  if(adrFname) adrFname.textContent='';
  const adrTimer=document.getElementById('adr-rec-timer');
  if(adrTimer) adrTimer.textContent='00:00:00';
  if(recTmr){clearInterval(recTmr);recTmr=null;}
}

function _syncRecBtn(recording){
  const btn=document.getElementById('rbtn');
  const btnMob=document.getElementById('rbtn-mob');
  const timer=document.getElementById('rtimer');
  const timerMob=document.getElementById('rtimer-mob');
  const adrBtn=document.getElementById('adr-rec-btn');
  const adrInfo=document.getElementById('adr-rec-info');
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
      if(timerMob) timerMob.classList.add('vis');
    } else {
      btnMob.textContent='⏺ Record';
      btnMob.classList.remove('rec');
      if(timerMob){timerMob.classList.remove('vis'); timerMob.textContent='00:00:00';}
    }
  }
  if(adrBtn){
    if(recording){
      adrBtn.textContent='⏹ Stop Recording';
      adrBtn.classList.add('rec');
    } else {
      adrBtn.textContent='⏺ Record';
      adrBtn.classList.remove('rec');
    }
  }
  if(adrInfo) adrInfo.classList.toggle('vis', !!recording);
}

// ── DOWNLOADS ──────────────────────────────────────────────
// Show the progress panel immediately (before the server responds)
// so even very fast exports are always visible.
function _showProgressNow(ctx, title, label, total){
  const panel=document.getElementById("adr-progress-"+ctx); if(!panel) return;
  panel.classList.add("active");
  const titleEl=document.getElementById("adr-prog-"+ctx+"-title");
  const labelEl=document.getElementById("adr-prog-"+ctx+"-label");
  const bar=document.getElementById("adr-prog-"+ctx+"-bar");
  const countEl=document.getElementById("adr-prog-"+ctx+"-count");
  const speedEl=document.getElementById("adr-prog-"+ctx+"-speed");
  const stopBtn=document.getElementById("adr-prog-"+ctx+"-stop");
  const dismissBtn=document.getElementById("adr-prog-"+ctx+"-dismiss");
  if(titleEl) titleEl.textContent=title;
  if(labelEl) labelEl.textContent=label;
  if(bar){ bar.style.width="0%"; bar.style.animation="adr-indeterminate 1.2s linear infinite"; bar.style.opacity="0.55"; }
  if(countEl) countEl.textContent=total>0?`0 / ${total} items`:"Starting…";
  if(speedEl) speedEl.textContent="";
  if(stopBtn) stopBtn.style.display="";
  if(dismissBtn) dismissBtn.style.display="none";
  // Always open the drawer to the right context so progress is visible on all screen sizes
  openDrawer(ctx);
}

async function dlM3U(){
  const op=document.getElementById('o-m3u').value.trim();
  if(!op){toast('Set M3U output path first','wrn');return;}
  if(!selSet.size){toast('Select items first','wrn');return;}
  setBusy(true);
  _showProgressNow('items','💾 Saving M3U…', curCat?curCat.title:'', selSet.size);
  const r=await fetch('/api/download/m3u',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({items:[...selSet],category:curCat,mode,out_path:op,total_hint:selSet.size})});
  const d=await r.json();
  d.ok?(toast(d.message,'ok'),pollBusy()):(toast(d.error,'err'),setBusy(false),dismissProgress('items'));
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
  d.ok?(toast(d.message,'ok'),pollBusy()):(toast(d.error,'err'),setBusy(false),dismissProgress('items'));
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
  d.ok?(toast(d.message,'ok'),pollBusy()):(toast(d.error,'err'),setBusy(false),dismissProgress('items'));
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
    // Fetch final authoritative numbers before freezing the panel
    const lastStatus = await fetch('/api/status').then(r=>r.json()).catch(()=>({}));
    const finalDone    = lastStatus.task_done    || 0;
    const finalTotal   = lastStatus.task_total   || 0;
    const finalSkipped = lastStatus.task_skipped || 0;
    ["cats","items"].forEach(ctx=>{
      const panel=document.getElementById("adr-progress-"+ctx);
      if(panel && panel.classList.contains("active")){
        const titleEl=document.getElementById("adr-prog-"+ctx+"-title");
        const bar=document.getElementById("adr-prog-"+ctx+"-bar");
        const speedEl=document.getElementById("adr-prog-"+ctx+"-speed");
        const countEl=document.getElementById("adr-prog-"+ctx+"-count");
        const stopBtn=document.getElementById("adr-prog-"+ctx+"-stop");
        const dismissBtn=document.getElementById("adr-prog-"+ctx+"-dismiss");
        if(titleEl) titleEl.textContent="✓ Done";
        if(bar){ bar.style.width="100%"; bar.style.animation=""; bar.style.opacity="1"; }
        if(speedEl) speedEl.textContent="";
        // Always overwrite count with the real final numbers
        if(countEl){
          const skipTxt = finalSkipped > 0 ? ` · ${finalSkipped} skipped` : "";
          countEl.textContent = finalTotal > 0
            ? `${finalDone} / ${finalTotal} items${skipTxt}`
            : (finalDone > 0 ? `${finalDone} items${skipTxt}` : "Complete");
        }
        if(stopBtn) stopBtn.style.display="none";
        if(dismissBtn) dismissBtn.style.display="";
      }
    });
  }
}
function dismissProgress(ctx){
  const panel=document.getElementById("adr-progress-"+ctx);
  if(!panel) return;
  panel.classList.remove("active");
  // Reset for next run
  const stopBtn=document.getElementById("adr-prog-"+ctx+"-stop");
  const dismissBtn=document.getElementById("adr-prog-"+ctx+"-dismiss");
  if(stopBtn) stopBtn.style.display="";
  if(dismissBtn) dismissBtn.style.display="none";
}
function updateTaskProgress(d){
  const type     = d.task_type       || "";
  const done     = d.task_done       || 0;
  const total    = d.task_total      || 0;
  const label    = d.task_label      || "";
  const filePct  = d.task_file_pct   || 0;
  const elapsed  = d.task_file_elapsed || "";
  const speed    = d.task_speed      || "";
  const active   = type !== "";

  let barPct, countTxt, speedTxt, indeterminate;

  if(type === "mkv"){
    // For MKV: bar = per-file download progress from ffmpeg
    const hasDuration = filePct > 0;
    indeterminate = !hasDuration;
    barPct   = hasDuration ? filePct : 0;
    // Item counter: "File 1 / 3" — shown in count area
    const itemTxt = total > 1 ? `File ${done+1} / ${total}` : (total===1 ? "Downloading…" : "Resolving…");
    // Elapsed time if available
    const elapsedTxt = elapsed ? ` · ${elapsed}` : "";
    countTxt = itemTxt + elapsedTxt;
    speedTxt = speed;
  } else if(type === "m3u"){
    // For M3U: bar = items saved / total
    const skipped = d.task_skipped || 0;
    const hasTot = total > 0;
    indeterminate = !hasTot;
    barPct   = hasTot ? Math.round(done / total * 100) : 0;
    const skipTxt = skipped > 0 ? ` · ${skipped} skipped` : "";
    countTxt = hasTot ? `${done} / ${total} items${skipTxt}` : (done > 0 ? `${done} items saved${skipTxt}` : "Starting…");
    speedTxt = "";
  } else {
    indeterminate = false; barPct = 0; countTxt = ""; speedTxt = "";
  }

  ["cats","items"].forEach(ctx => {
    const panel = document.getElementById("adr-progress-"+ctx);
    if(!panel) return;
    if(active){
      panel.classList.add("active");
      // Reset stop/dismiss to "running" state when a new task starts
      const stopBtn=document.getElementById("adr-prog-"+ctx+"-stop");
      const dismissBtn=document.getElementById("adr-prog-"+ctx+"-dismiss");
      if(stopBtn && stopBtn.style.display==="none"){ stopBtn.style.display=""; }
      if(dismissBtn && dismissBtn.style.display!=="" && d.busy){ dismissBtn.style.display="none"; }
      const title  = type === "mkv" ? "⬇ Downloading MKV…" : "💾 Saving M3U…";
      document.getElementById("adr-prog-"+ctx+"-title").textContent = title;
      document.getElementById("adr-prog-"+ctx+"-label").textContent = label;
      const bar = document.getElementById("adr-prog-"+ctx+"-bar");
      if(indeterminate){
        bar.style.width = "40%";
        bar.style.opacity = "0.55";
        bar.style.animation = "adr-indeterminate 1.2s linear infinite";
      } else {
        bar.style.width   = barPct + "%";
        bar.style.opacity = "1";
        bar.style.animation = "";
      }
      document.getElementById("adr-prog-"+ctx+"-count").textContent = countTxt;
      const speedEl = document.getElementById("adr-prog-"+ctx+"-speed");
      if(speedEl) speedEl.textContent = speedTxt;
    }
    // Never auto-hide here — only pollBusy (Done state) and dismissProgress (✕) hide the panel.
  });
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
        if(adrFname) adrFname.textContent=rs.filename||'';
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
          const adrTimer=document.getElementById('adr-rec-timer');
          if(adrTimer) adrTimer.textContent=ts;
          const btn=document.getElementById('rbtn');
          if(btn) btn.textContent=`⏹ Stop Recording ${ts}`;
          const adrBtn=document.getElementById('adr-rec-btn');
          if(adrBtn) adrBtn.textContent=`⏹ Stop Recording ${ts}`;
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
  document.getElementById({m3u:'o-m3u',dir:'o-dir'}[w]).value=v;
  document.getElementById('sg-'+w).classList.remove('open');
  if(w==='dir') saveFP();
}
document.addEventListener('click',e=>{
  if(!e.target.closest('.prow'))
    document.querySelectorAll('.psug').forEach(el=>el.classList.remove('open'));
});
function saveFP(){
  try{localStorage.setItem('mkv_folder',document.getElementById('o-dir').value);}catch(e){}
  try{localStorage.setItem('m3u_path',document.getElementById('o-m3u').value);}catch(e){}
}
function saveExtPlayer(){
  try{localStorage.setItem('ext_player',document.getElementById('o-extplayer').value);}catch(e){}
}
function saveSubKey(){
  try{localStorage.setItem('opensubtitles_key',document.getElementById('o-subkey').value.trim());}catch(e){}
}
function _getSubKey(){
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
  || (navigator.maxTouchPoints > 1);

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
