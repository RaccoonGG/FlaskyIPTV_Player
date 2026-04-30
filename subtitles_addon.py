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
subtitles_addon.py  —  Subtitle support for FlaskyIPTV_Player_byGG.py
=============================================================================
Provides OpenSubtitles.com search/download, local subtitle file loading,
mobile directory browser, and the full subtitle player UI (TextTrack-based,
supports SRT / VTT / ASS / SSA with live delay control).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INTEGRATION  (three small changes to FlaskyIPTV_Player_byGG.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — add import after the dvr_addon import block:

    try:
        from subtitles_addon import register_subtitles_routes
        _SUBTITLES_AVAILABLE = True
    except ImportError:
        _SUBTITLES_AVAILABLE = False
        def register_subtitles_routes(*a, **kw): pass

STEP 2 — register routes after dvr registration:

    register_subtitles_routes(flask_app)

STEP 3 — add one script tag inside HTML_TEMPLATE, just before </body>:

    <script src="/api/subtitles/ui.js"></script>

That's it — no other files required. This file is fully self-contained.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GLOBALS USED FROM MAIN SCRIPT (via window.*)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  vid          — the <video> element
  pName        — currently playing stream name (auto-fills subtitle search)
  mode         — current portal mode (live/vod/series)
  toast()      — UI notification helper
  alog()       — activity log helper
  esc()        — HTML escape helper
  _isMobile    — mobile detection flag
  _getSubKey() — reads opensubtitles API key from localStorage (in settings)
"""

import os

import requests as _requests_lib
from flask import request, jsonify, Response


# ===================== OPENSUBTITLES API =====================

OPENSUBTITLES_BASE = "https://api.opensubtitles.com/api/v1"
OPENSUBTITLES_UA   = "IPTVPortalPlayer v1.0"


def _os_headers(api_key: str = ""):
    return {
        "Api-Key":      api_key.strip(),
        "User-Agent":   OPENSUBTITLES_UA,
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }


# ===================== REGISTRATION =====================

def register_subtitles_routes(flask_app):
    """Register all subtitle-related Flask routes."""

    # ── /api/load_subtitle_path ───────────────────────────────────────────────
    @flask_app.route("/api/load_subtitle_path", methods=["POST"])
    def api_load_subtitle_path():
        """Android/mobile: read a subtitle file from an absolute path on the server filesystem."""
        data = request.get_json(force=True)
        path = (data.get("path") or "").strip()
        if not path:
            return jsonify({"error": "No path provided"}), 400
        if not os.path.isfile(path):
            return jsonify({"error": f"File not found: {path}"}), 404
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".srt", ".vtt", ".ass", ".ssa", ".txt"):
            return jsonify({"error": f"Unsupported subtitle format: {ext}"}), 400
        try:
            with open(path, "rb") as f:
                raw = f.read()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = raw.decode("latin-1", errors="replace")
            mime_map = {".srt": "text/srt", ".vtt": "text/vtt",
                        ".ass": "text/x-ssa", ".ssa": "text/x-ssa"}
            mime  = mime_map.get(ext, "text/srt")
            fname = os.path.basename(path)
            return jsonify({"content": content, "file_name": fname, "mime": mime})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── /api/browse_dir ───────────────────────────────────────────────────────
    @flask_app.route("/api/browse_dir", methods=["POST"])
    def api_browse_dir():
        """List directory contents for the mobile subtitle file browser."""
        data     = request.get_json(force=True)
        path     = (data.get("path") or "/sdcard/Download").rstrip("/") or "/"
        dirs_only = data.get("dirs_only", False)
        try:
            entries = os.listdir(path)
        except PermissionError:
            return jsonify({"error": "Permission denied", "path": path, "dirs": [], "files": []}), 403
        except FileNotFoundError:
            return jsonify({"error": "Directory not found", "path": path, "dirs": [], "files": []}), 404
        except Exception as e:
            return jsonify({"error": str(e), "path": path, "dirs": [], "files": []}), 500

        sub_exts = {".srt", ".vtt", ".ass", ".ssa"}
        dirs, files = [], []
        for name in sorted(entries, key=lambda x: x.lower()):
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    dirs.append(name)
                elif not dirs_only and os.path.isfile(full) and os.path.splitext(name)[1].lower() in sub_exts:
                    files.append(name)
            except Exception:
                pass

        parent = str(os.path.dirname(path)) if path not in ("/", "") else None
        return jsonify({"path": path, "parent": parent, "dirs": dirs, "files": files})

    # ── /api/browse_subtitle ─────────────────────────────────────────────────
    @flask_app.route("/api/browse_subtitle", methods=["GET"])
    def api_browse_subtitle():
        """Desktop only: open a native OS file picker for subtitle files."""
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", True)
            path = filedialog.askopenfilename(
                title="Select Subtitle File",
                filetypes=[
                    ("Subtitle files", "*.srt *.vtt *.ass *.ssa"),
                    ("All files", "*.*"),
                ],
            )
            root.destroy()
            return jsonify({"path": path or ""})
        except Exception as e:
            return jsonify({"path": "", "error": str(e)})

    # ── /api/subtitles/search ─────────────────────────────────────────────────
    @flask_app.route("/api/subtitles/search", methods=["POST"])
    def api_subtitles_search():
        data        = request.get_json(force=True)
        query       = (data.get("query") or "").strip()
        lang        = (data.get("lang") or "en").strip()
        season      = data.get("season")
        episode     = data.get("episode")
        sub_type    = (data.get("type") or "").strip()   # "movie" or "episode"
        max_results = int(data.get("max_results") or 20)
        api_key     = (data.get("api_key") or "").strip()

        if not query:
            return jsonify({"error": "No query provided", "results": []}), 400
        if not api_key:
            return jsonify({"error": "No OpenSubtitles API key set — add it in ⚙ Settings.", "results": []}), 400

        params = {"query": query, "languages": lang, "per_page": min(max_results, 40)}
        if sub_type in ("movie", "episode"):
            params["type"] = sub_type
        if season:
            params["season_number"] = int(season)
        if episode:
            params["episode_number"] = int(episode)

        try:
            r = _requests_lib.get(
                f"{OPENSUBTITLES_BASE}/subtitles",
                headers=_os_headers(api_key),
                params=params,
                timeout=15,
            )
            r.raise_for_status()
            raw = r.json().get("data", [])
            results = []
            for item in raw:
                a    = item.get("attributes", {})
                feat = a.get("feature_details", {})
                files = a.get("files", [])
                if not files:
                    continue
                results.append({
                    "file_id":      files[0].get("file_id"),
                    "file_name":    files[0].get("file_name", "subtitle"),
                    "title":        feat.get("movie_name") or feat.get("title", "Unknown"),
                    "year":         feat.get("year", ""),
                    "season":       feat.get("season_number"),
                    "episode":      feat.get("episode_number"),
                    "feature_type": feat.get("feature_type", ""),
                    "lang":         a.get("language", "?"),
                    "rating":       a.get("ratings", "?"),
                    "downloads":    a.get("download_count", 0),
                    "uploader":     a.get("uploader", {}).get("name", "anonymous"),
                    "release":      a.get("release", ""),
                })
            return jsonify({"results": results, "count": len(results)})
        except _requests_lib.HTTPError as e:
            return jsonify({"error": f"OpenSubtitles HTTP error: {e}", "results": []}), 502
        except Exception as e:
            return jsonify({"error": str(e), "results": []}), 500

    # ── /api/subtitles/download ───────────────────────────────────────────────
    @flask_app.route("/api/subtitles/download", methods=["POST"])
    def api_subtitles_download():
        """Fetch subtitle file from OpenSubtitles and return its content."""
        data    = request.get_json(force=True)
        file_id = data.get("file_id")
        api_key = (data.get("api_key") or "").strip()
        if not file_id:
            return jsonify({"error": "No file_id provided"}), 400
        if not api_key:
            return jsonify({"error": "No OpenSubtitles API key set — add it in ⚙ Settings."}), 400
        try:
            r = _requests_lib.post(
                f"{OPENSUBTITLES_BASE}/download",
                headers=_os_headers(api_key),
                json={"file_id": int(file_id)},
                timeout=15,
            )

            if r.status_code == 406:
                try:
                    info       = r.json()
                    remaining  = info.get("remaining", 0)
                    reset_time = info.get("reset_time", "")
                    reset_str  = f"  Resets: {reset_time}" if reset_time else ""
                    requests_  = info.get("requests", "?")
                except Exception:
                    remaining, reset_str, requests_ = 0, "", "?"
                return jsonify({
                    "error": (
                        f"Daily download quota reached ({requests_} used, {remaining} remaining).{reset_str}  "
                        f"Free accounts get 5 downloads/day — register at opensubtitles.com for 20/day."
                    )
                }), 429

            if r.status_code in (401, 403):
                return jsonify({"error": "Invalid OpenSubtitles API key — check your key in ⚙ Settings."}), 401

            r.raise_for_status()
            info   = r.json()
            dl_url = info.get("link")
            if not dl_url:
                return jsonify({"error": "No download link returned by OpenSubtitles"}), 502

            sub = _requests_lib.get(dl_url, timeout=30)
            sub.raise_for_status()

            content_bytes = sub.content
            try:
                content_text = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                content_text = content_bytes.decode("latin-1", errors="replace")

            fname = info.get("file_name", dl_url.split("?")[0].split("/")[-1])
            if fname.endswith(".ass") or fname.endswith(".ssa"):
                mime = "text/x-ssa"
            elif fname.endswith(".vtt"):
                mime = "text/vtt"
            else:
                mime = "text/srt"

            return jsonify({
                "content":   content_text,
                "file_name": fname,
                "mime":      mime,
                "remaining": info.get("remaining", "?"),
            })
        except _requests_lib.HTTPError as e:
            return jsonify({"error": f"OpenSubtitles HTTP error: {e}"}), 502
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── /api/subtitles/ui.js ─────────────────────────────────────────────────
    # Serves the complete subtitle UI: CSS + HTML modal + JS — injected into
    # the main page via <script src="/api/subtitles/ui.js"></script>.
    # Globals used from main script: vid, pName, toast, alog, esc,
    #   _isMobile, _getSubKey (all available as window.* in the same page).
    _SUBTITLES_UI_JS_BYTES = _SUBTITLES_UI_JS.encode("utf-8")

    @flask_app.route("/api/subtitles/ui.js")
    def api_subtitles_ui_js():
        return Response(
            _SUBTITLES_UI_JS_BYTES,
            content_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )


# ===================== FRONTEND (CSS + HTML + JS) =====================

_SUBTITLES_UI_JS = r"""
/* ── Inject CSS ───────────────────────────────────────────────────── */
(function(){
  const style = document.createElement('style');
  style.textContent = `
/* ─── subtitle modal ─────────────────────────────────────────── */
#sub-overlay{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.75);
  display:none;align-items:center;justify-content:center;padding:16px}
#sub-overlay.open{display:flex}
#sub-modal{background:var(--s1);border:1px solid var(--bdr2);border-radius:var(--r);
  width:100%;max-width:640px;max-height:88vh;display:flex;flex-direction:column;
  box-shadow:0 20px 60px rgba(0,0,0,.8);overflow:hidden}
.sub-hdr{padding:14px 16px;border-bottom:1px solid var(--bdr);
  display:flex;align-items:center;gap:10px;flex-shrink:0;background:var(--s2)}
.sub-hdr h3{flex:1;font-size:13px;font-weight:800;letter-spacing:.5px;color:var(--txt)}
.sub-body{flex:1;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px}
.sub-search-row{display:flex;gap:8px;align-items:center}
.sub-search-row input{flex:1;height:36px;font-size:13px}
.sub-search-row button{height:36px;padding:0 14px;flex-shrink:0}
.sub-filters{display:flex;flex-wrap:wrap;gap:10px;padding:8px 10px;
  background:var(--s3);border-radius:var(--rsm);border:1px solid var(--bdr)}
.sub-filter-group{display:flex;flex-direction:column;gap:5px}
.sub-filter-group label.grp-lbl{font-size:10px;font-weight:800;text-transform:uppercase;
  letter-spacing:1px;color:var(--txt3)}
.sub-lang-grid{display:flex;flex-wrap:wrap;gap:4px}
.sub-lang-chip{display:flex;align-items:center;gap:4px;padding:3px 8px;border-radius:20px;
  font-size:11px;font-weight:600;border:1px solid var(--bdr2);background:var(--s4);
  color:var(--txt2);cursor:pointer;transition:all .15s;user-select:none;white-space:nowrap}
.sub-lang-chip input{width:14px;height:14px;cursor:pointer;flex-shrink:0;accent-color:var(--acc)}
.sub-lang-chip:has(input:checked){background:rgba(124,58,237,.18);
  border-color:var(--acc);color:var(--txt)}
.sub-type-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sub-type-chip{display:flex;align-items:center;gap:4px;padding:3px 10px;border-radius:20px;
  font-size:11px;font-weight:600;border:1px solid var(--bdr2);background:var(--s4);
  color:var(--txt2);cursor:pointer;transition:all .15s;user-select:none}
.sub-type-chip input{width:14px;height:14px;cursor:pointer;flex-shrink:0;accent-color:var(--acc)}
.sub-type-chip:has(input:checked){background:rgba(124,58,237,.18);
  border-color:var(--acc);color:var(--txt)}
.sub-ep-row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.sub-ep-row label{font-size:11px;color:var(--txt2);white-space:nowrap}
.sub-ep-row input{width:60px;height:28px;font-size:12px;text-align:center}
.sub-results{display:flex;flex-direction:column;gap:6px}
.sub-result-item{background:var(--s3);border:1px solid var(--bdr);border-radius:var(--rsm);
  padding:10px 12px;display:flex;gap:10px;align-items:flex-start;transition:border-color .15s}
.sub-result-item:hover{border-color:var(--bdr2)}
.sub-result-info{flex:1;min-width:0}
.sub-result-title{font-size:13px;font-weight:700;color:var(--txt);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub-result-meta{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.sub-meta-badge{padding:2px 7px;border-radius:20px;font-size:10px;font-weight:700}
.sub-meta-lang{background:rgba(6,182,212,.12);color:var(--cyan);border:1px solid rgba(6,182,212,.2)}
.sub-meta-dl{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.15)}
.sub-meta-rat{background:rgba(245,158,11,.1);color:var(--orange);border:1px solid rgba(245,158,11,.15)}
.sub-meta-ep{background:rgba(124,58,237,.12);color:#a78bfa;border:1px solid rgba(124,58,237,.2)}
.sub-result-release{font-size:10px;color:var(--txt3);margin-top:3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub-load-btn{flex-shrink:0;height:34px;padding:0 12px;font-size:12px;align-self:center}
.sub-load-btn.loaded{background:rgba(34,197,94,.15);color:var(--green);
  border:1px solid rgba(34,197,94,.3)}
.sub-empty{text-align:center;padding:36px 20px;color:var(--txt3);font-size:13px}
.sub-empty span{font-size:36px;display:block;margin-bottom:8px;opacity:.3}
.sub-status-bar{padding:8px 12px;border-top:1px solid var(--bdr);flex-shrink:0;
  display:flex;flex-direction:column;gap:5px;
  background:var(--s2);font-size:11px;color:var(--txt3)}
.sub-sbar-r1{display:flex;align-items:center;gap:8px;min-width:0}
#sub-status-msg{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub-sbar-r2{display:none;align-items:center;gap:6px;padding-top:5px;border-top:1px solid var(--bdr)}
#sub-sync-inp{flex:1;min-width:0;height:24px;font-size:12px;padding:0 7px;border-radius:var(--rss);
  border:1px solid var(--bdr);background:var(--s3);color:var(--txt)}
.sub-status-bar .btn-ghost{flex-shrink:0;white-space:nowrap}
.sub-active-strip{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.2);
  border-radius:var(--rss);padding:4px 10px;font-size:11px;color:var(--green);
  display:flex;align-items:center;gap:6px}
.sub-delay-row{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--txt2)}
.sub-delay-row button{width:26px;height:26px;padding:0;font-size:13px;border-radius:var(--rss);
  border:1px solid var(--bdr2);background:var(--s3);color:var(--txt);cursor:pointer;
  display:flex;align-items:center;justify-content:center;transition:var(--tr);flex-shrink:0}
.sub-delay-row button:hover{background:var(--s4);border-color:var(--acc)}
#sub-delay-val{min-width:52px;text-align:center;font-weight:700;color:var(--acc);font-size:12px;
  font-variant-numeric:tabular-nums}
/* subtitle tab row */
.sub-tab-row{display:flex;gap:6px;flex-shrink:0;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--bdr);padding-bottom:8px;margin-bottom:2px}
.sub-api-key-wrap{display:flex;gap:6px;align-items:center;flex:1;min-width:0}
.sub-tab-btn{height:30px;padding:0 14px;font-size:12px;font-weight:700;border-radius:var(--rss);
  border:1px solid var(--bdr2);background:var(--s3);color:var(--txt2);cursor:pointer;transition:var(--tr)}
.sub-tab-btn.active{background:var(--acc);border-color:var(--acc);color:#fff}
.sub-tab-btn:hover:not(.active){background:var(--s4);color:var(--txt)}
/* On mobile: API key + icon take full first row; divider hidden; tabs share second row */
@media(max-width:599px){
  .sub-api-key-wrap{flex:1 1 100%;order:-1;display:flex;gap:6px;align-items:center}
  .sub-tab-divider-v{display:none!important}
  .sub-tab-btn{flex:1}
}
/* mobile subtitle file browser */
.sub-fb-row{display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid var(--bdr);
  cursor:pointer;transition:background .12s;font-size:13px}
.sub-fb-row:last-child{border-bottom:none}
.sub-fb-row:hover,.sub-fb-row:active{background:var(--s4)}
.sub-fb-dir{color:var(--txt)}
.sub-fb-file{color:var(--cyan)}
.sub-fb-icon{flex-shrink:0;font-size:15px}
.sub-fb-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub-fb-arr{flex-shrink:0;color:var(--txt3);font-size:16px}
@media(max-width:600px){
  #sub-modal{max-height:96vh;border-radius:0}
  #sub-overlay{padding:0;align-items:flex-end}
}
`;
  document.head.appendChild(style);
})();

/* ── Inject HTML modal ────────────────────────────────────────────── */
(function(){
  const div = document.createElement('div');
  div.innerHTML = `
<!-- SUBTITLE SEARCH MODAL -->
<div id="sub-overlay" onclick="if(event.target===this)closeSubSearch()">
  <div id="sub-modal">
    <div class="sub-hdr">
      <h3>&#x1F4AC; Subtitle Search</h3>
      <div id="sub-active-info" style="display:none" class="sub-active-strip">
        <span>&#x2713;</span><span id="sub-active-name" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
        <button onclick="clearSubtitle()" style="background:none;border:none;color:var(--green);cursor:pointer;padding:0;font-size:12px;margin-left:2px" title="Remove subtitle">&#x2715;</button>
      </div>
      <button class="btn-ghost" onclick="closeSubSearch()" style="height:28px;padding:0 10px;font-size:12px;margin-left:6px">&#x2715;</button>
    </div>
    <div class="sub-body">
      <div class="sub-tab-row" id="sub-tab-row">
        <div class="sub-api-key-wrap">
          <span style="position:relative;display:inline-flex;align-items:center;flex:1;min-width:0">
            <input id="sub-apikey" type="password"
              placeholder="OpenSubtitles API key &mdash; get one free at opensubtitles.com"
              autocomplete="new-password" autocorrect="off" spellcheck="false"
              oninput="saveSubKey()" title="Your OpenSubtitles Consumer API key"
              style="width:100%;height:30px;font-size:12px;padding-right:28px">
            <button type="button" onclick="(function(b){var i=document.getElementById('sub-apikey');i.type=i.type==='password'?'text':'password';b.textContent=i.type==='password'?'👁':'🙈'})(this)" style="position:absolute;right:4px;background:none;border:none;cursor:pointer;padding:0;font-size:13px;line-height:1;color:var(--txt2)" tabindex="-1">👁</button>
          </span>
          <a href="https://www.opensubtitles.com/en/consumers" target="_blank" rel="noopener"
            class="btn-ghost"
            style="display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;
                   border-radius:var(--rss);text-decoration:none;font-size:13px;flex-shrink:0;
                   border:1px solid var(--bdr);background:var(--s3);color:var(--txt2)"
            title="Get a free API key at opensubtitles.com/en/consumers">&#x1F511;</a>
        </div>
        <div class="sub-tab-divider-v" style="width:1px;background:var(--bdr);align-self:stretch;flex-shrink:0"></div>
        <button class="sub-tab-btn active" id="sub-tab-online" onclick="subSwitchTab('online')">&#x1F50D; Online Search</button>
        <button class="sub-tab-btn" id="sub-tab-local" onclick="subSwitchTab('local')">&#x1F4C2; Local File</button>
      </div>
      <!-- ONLINE SEARCH PANEL -->
      <div id="sub-panel-online">
      <div class="sub-search-row">
        <input id="sub-query" type="search" placeholder="Title (auto-filled from player)&hellip;"
          autocomplete="new-password" autocorrect="off" spellcheck="false"
          onkeydown="if(event.key==='Enter')subSearch()">
        <button class="btn-acc" onclick="subSearch()" id="sub-search-btn">&#x1F50D; Search</button>
      </div>
      <div class="sub-filters">
        <div class="sub-filter-group" style="flex:1;min-width:200px">
          <label class="grp-lbl">Language</label>
          <div class="sub-lang-grid" id="sub-lang-grid"></div>
        </div>
        <div class="sub-filter-group" style="min-width:180px">
          <label class="grp-lbl">Type</label>
          <div class="sub-type-row">
            <label class="sub-type-chip"><input type="radio" name="sub-type" value="movie" id="sub-type-movie" checked onchange="subToggleEp()"> &#x1F3AC; Movie</label>
            <label class="sub-type-chip"><input type="radio" name="sub-type" value="series" id="sub-type-series" onchange="subToggleEp()"> &#x1F4FA; Series</label>
          </div>
          <div class="sub-ep-row" id="sub-ep-row" style="display:none;margin-top:6px">
            <label>Season</label>
            <input id="sub-season" type="number" min="1" placeholder="S#" oninput="subSeasonChange()">
            <label>Episode</label>
            <input id="sub-episode" type="number" min="1" placeholder="Ep#">
          </div>
        </div>
        <div class="sub-filter-group" style="min-width:80px">
          <label class="grp-lbl">Max results</label>
          <select id="sub-maxresults" style="height:28px;font-size:12px;background:var(--s4);color:var(--txt);border:1px solid var(--bdr2);border-radius:var(--rss);padding:0 8px">
            <option value="10">10</option>
            <option value="20" selected>20</option>
            <option value="40">40</option>
          </select>
        </div>
      </div>
      <div id="sub-results-wrap">
        <div class="sub-empty" id="sub-placeholder">
          <span>&#x1F4AC;</span>
          Search for subtitles &mdash; title is auto-filled from what&apos;s playing.
        </div>
      </div>
      </div><!-- /sub-panel-online -->
      <!-- LOCAL FILE PANEL -->
      <div id="sub-panel-local" style="display:none;padding:10px 0 4px 0">
        <div id="sub-local-desktop">
          <div style="margin-bottom:8px;font-size:12px;color:var(--txt2);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px">
            <span>Choose a local subtitle file (.srt, .vtt, .ass, .ssa).</span>
            <button id="sub-fb-toggle-btn" class="btn-ghost" style="font-size:10px;height:22px;padding:0 8px" onclick="subToggleFileBrowser()" title="Switch to inline file browser">&#x1F4C1; File browser: Off</button>
          </div>
          <div class="sub-search-row" style="align-items:center;gap:8px">
            <button class="btn-ghost" style="height:32px;padding:0 14px;font-size:12px;display:inline-flex;align-items:center;gap:6px;flex-shrink:0"
              onclick="subBrowseDesktop()">&#x1F4C2; Choose file&hellip;</button>
            <input type="file" id="sub-local-input" accept=".srt,.vtt,.ass,.ssa,text/plain"
              style="display:none;position:absolute;width:0;height:0;opacity:0" onchange="subLoadLocalFile(this)">
            <span id="sub-local-filename" style="font-size:12px;color:var(--txt2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">No file chosen</span>
          </div>
        </div>
        <div id="sub-local-mobile" style="display:none">
          <div style="display:flex;justify-content:flex-end;margin-bottom:6px">
            <button id="sub-fb-toggle-btn2" class="btn-ghost" style="font-size:10px;height:22px;padding:0 8px;background:rgba(124,58,237,.2);border-color:var(--acc);color:var(--txt)" onclick="subToggleFileBrowser()" title="Switch back to desktop file picker">&#x1F4C1; File browser: On</button>
          </div>
          <div class="sub-search-row" style="gap:5px;margin-bottom:6px">
            <button class="btn-ghost" id="sub-fb-up" style="height:30px;padding:0 10px;font-size:16px;flex-shrink:0" onclick="subFbUp()" title="Up">&#x2191;</button>
            <span id="sub-fb-path" style="font-size:11px;color:var(--txt2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;align-self:center">/sdcard/Download</span>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="subFbNav('/sdcard/Download')">&#x1F4E5; Download</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="subFbNav('/storage/emulated/0/Download')">&#x1F4E5; /0/Download</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="subFbNav('/sdcard')">&#x1F4F1; /sdcard</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="subFbNav('/storage/emulated/0')">&#x1F4F1; /storage/0</button>
            <button class="btn-ghost" style="font-size:10px;height:22px;padding:0 7px" onclick="subFbNav('/data/data/com.termux/files/home')">&#x1F5A5; Termux</button>
          </div>
          <div id="sub-fb-list" style="max-height:200px;overflow-y:auto;border:1px solid var(--bdr);border-radius:var(--rsm);background:var(--s3)">
            <div style="padding:10px;font-size:12px;color:var(--txt3)">Loading&hellip;</div>
          </div>
        </div>
        <div id="sub-local-status" style="font-size:11px;color:var(--txt2);margin-top:4px"></div>
      </div>
    </div>
    <div class="sub-status-bar">
      <div class="sub-sbar-r1">
        <span id="sub-status-msg">Ready</span>
        <div class="sub-delay-row" id="sub-delay-row" style="display:none;flex-shrink:0">
          <span>&#9201; Delay:</span>
          <button onmousedown="subDelayHold(-0.1)" onmouseup="subDelayRelease()" onmouseleave="subDelayRelease()" ontouchstart="subDelayTouch(event,-0.1)" ontouchend="subDelayRelease()" title="-0.1s">&#x2212;</button>
          <span id="sub-delay-val">0.0s</span>
          <button onmousedown="subDelayHold(0.1)" onmouseup="subDelayRelease()" onmouseleave="subDelayRelease()" ontouchstart="subDelayTouch(event,0.1)" ontouchend="subDelayRelease()" title="+0.1s">&#x2b;</button>
          <button onclick="subAdjustDelay(-subDelayMs/1000)" title="Reset" style="font-size:10px;width:34px">Reset</button>
          <button id="sub-toggle-btn" onclick="subToggleVisible()" title="Hide/show subtitles" style="width:auto;padding:0 7px;font-size:11px;margin-left:2px">&#x1F441; On</button>
        </div>
      </div>
      <div class="sub-sbar-r2" id="sub-sync-row">
        <span style="color:var(--txt3);white-space:nowrap">&#127916; Movie time on screen:</span>
        <input id="sub-sync-inp" type="text" placeholder="MM:SS or H:MM:SS"
          onkeydown="if(event.key==='Enter') subSyncToMovieTime()">
        <button onclick="subSyncToMovieTime()"
          style="height:24px;padding:0 10px;font-size:11px;font-weight:700;border-radius:var(--rss);
                 background:var(--acc);color:#fff;border:none;cursor:pointer;flex-shrink:0">Sync</button>
        <span id="sub-sync-status" style="font-size:11px;color:var(--green);display:none;white-space:nowrap"></span>
        <button class="btn-ghost" onclick="closeSubSearch()" style="height:28px;padding:0 12px;font-size:12px;flex-shrink:0">Close</button>
      </div>
    </div>
  </div>
</div>
`;
  document.body.appendChild(div.firstElementChild);
})();

/* ── Subtitle JS ──────────────────────────────────────────────────── */

const SUB_LANGS = [
  {code:'en',label:'English'},{code:'sr',label:'Serbian'},{code:'hr',label:'Croatian'},
  {code:'es',label:'Spanish'},{code:'fr',label:'French'},{code:'de',label:'German'},
  {code:'it',label:'Italian'},{code:'pt',label:'Portuguese'},{code:'ru',label:'Russian'},
  {code:'nl',label:'Dutch'},{code:'pl',label:'Polish'},{code:'tr',label:'Turkish'},
  {code:'sv',label:'Swedish'},{code:'hu',label:'Hungarian'},{code:'cs',label:'Czech'},
  {code:'ro',label:'Romanian'},{code:'bg',label:'Bulgarian'},{code:'uk',label:'Ukrainian'},
  {code:'el',label:'Greek'},{code:'ar',label:'Arabic'},{code:'zh',label:'Chinese'},
  {code:'ja',label:'Japanese'},{code:'ko',label:'Korean'},
];

let _subActiveFile = null;
let _subCuesBase   = [];
let subDelayMs     = 0;
let _subTrackObj   = null;

// ── Native TextTrack helpers ────────────────────────────────
function _subGetOrCreateTrack(){
  if(_subTrackObj) return _subTrackObj;
  _subTrackObj = vid.addTextTrack('subtitles', 'Subtitle', 'und');
  return _subTrackObj;
}

function _subClearNativeTrack(){
  if(!_subTrackObj) return;
  const list = _subTrackObj.cues;
  while(list && list.length){ try{ _subTrackObj.removeCue(list[0]); }catch(e){ break; } }
  _subTrackObj.mode = 'disabled';
}

function _subLoadCuesToTrack(cues){
  const track = _subGetOrCreateTrack();
  const list = track.cues;
  while(list && list.length){ try{ track.removeCue(list[0]); }catch(e){ break; } }
  const offsetSec = subDelayMs / 1000;
  for(const c of cues){
    const startSec = Math.max(0, c.startMs/1000 + offsetSec);
    const endSec   = Math.max(startSec + 0.001, c.endMs/1000 + offsetSec);
    try{ track.addCue(new VTTCue(startSec, endSec, c.text)); }catch(e){}
  }
  track.mode = 'showing';
}

// ── Parse any format into cues ──────────────────────────────
function _subParseCues(content, mime, fileName){
  const lower = (fileName||'').toLowerCase();
  if(lower.endsWith('.ass') || lower.endsWith('.ssa')) return _parseAssCues(content);
  if(lower.endsWith('.vtt') || mime === 'text/vtt')    return _parseVttCues(content);
  return _parseSrtCues(content);
}

function _tsToMs(ts){
  ts = ts.trim().replace(',','.');
  const parts = ts.split(':');
  if(parts.length < 3) return 0;
  const [h, m, s] = parts;
  return (parseInt(h)*3600 + parseInt(m)*60 + parseFloat(s)) * 1000;
}

function _parseSrtCues(srt){
  const cues = [];
  const blocks = srt.replace(/\r\n/g,'\n').replace(/\r/g,'\n').split(/\n\s*\n/);
  for(const block of blocks){
    const lines = block.trim().split('\n');
    if(lines.length < 2) continue;
    let tsLine = -1;
    for(let i=0;i<lines.length;i++){ if(lines[i].includes('-->')){tsLine=i;break;} }
    if(tsLine < 0) continue;
    const m = lines[tsLine].match(/(\d[\d:,\.]+)\s*-->\s*(\d[\d:,\.]+)/);
    if(!m) continue;
    const text = lines.slice(tsLine+1).join('\n').replace(/<\/?[^>]+>/g,'').trim();
    if(!text) continue;
    cues.push({startMs: _tsToMs(m[1]), endMs: _tsToMs(m[2]), text});
  }
  return cues;
}

function _parseVttCues(vtt){
  const cues = [];
  const blocks = vtt.replace(/\r\n/g,'\n').replace(/\r/g,'\n').split(/\n\s*\n/);
  for(const block of blocks){
    const lines = block.trim().split('\n');
    let tsLine = -1;
    for(let i=0;i<lines.length;i++){ if(lines[i].includes('-->')){tsLine=i;break;} }
    if(tsLine < 0) continue;
    const m = lines[tsLine].match(/(\d[\d:\.]+)\s*-->\s*(\d[\d:\.]+)/);
    if(!m) continue;
    const text = lines.slice(tsLine+1).join('\n').replace(/<\/?[^>]+>/g,'').trim();
    if(!text) continue;
    cues.push({startMs: _tsToMs(m[1]), endMs: _tsToMs(m[2]), text});
  }
  return cues;
}

function _parseAssCues(ass){
  const cues = [];
  const lines = ass.replace(/\r\n/g,'\n').replace(/\r/g,'\n').split('\n');
  for(const line of lines){
    const m = line.match(/^Dialogue:\s*\d+,(\d+:\d{2}:\d{2}\.\d{2}),(\d+:\d{2}:\d{2}\.\d{2}),[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,(.*)$/);
    if(!m) continue;
    const _assTs = t => { const [h,min,sec]=t.split(':'); const [s,cs]=sec.split('.'); return (parseInt(h)*3600+parseInt(min)*60+parseFloat(s+'.'+cs))*1000; };
    const text = m[3].replace(/\{[^}]*\}/g,'').replace(/\\N/gi,'\n').replace(/\\n/gi,'\n').trim();
    if(!text) continue;
    cues.push({startMs: _assTs(m[1]), endMs: _assTs(m[2]), text});
  }
  return cues;
}

// ── Apply subtitle (called from both online + local paths) ──
function _subApplyToPlayer(content, fileName, mime){
  _subCuesBase = _subParseCues(content, mime, fileName);
  subDelayMs   = 0;
  _subLoadCuesToTrack(_subCuesBase);
  const _dv = document.getElementById('sub-delay-val');
  if(_dv) _dv.textContent = '0.0s';
  const _dr = document.getElementById('sub-delay-row');
  if(_dr) _dr.style.display = 'flex';
  const _sr = document.getElementById('sub-sync-row');
  if(_sr) _sr.style.display = 'flex';
  const _tb = document.getElementById('sub-toggle-btn');
  if(_tb) _tb.innerHTML = '&#x1F441; On';
}

function subAdjustDelay(deltaSec){
  if(!_subCuesBase.length){ toast('No subtitle loaded','w'); return; }
  subDelayMs += Math.round(deltaSec * 1000);
  _subLoadCuesToTrack(_subCuesBase);
  const dv = document.getElementById('sub-delay-val');
  if(dv) dv.textContent = (subDelayMs>=0?'+':'') + (subDelayMs/1000).toFixed(1) + 's';
}

let _subDelayHoldTimer = null;
let _subDelayHoldIval  = null;
function subDelayHold(deltaSec){
  subAdjustDelay(deltaSec);
  _subDelayHoldTimer = setTimeout(function(){
    _subDelayHoldIval = setInterval(function(){ subAdjustDelay(deltaSec); }, 100);
  }, 500);
}
function subDelayTouch(e, deltaSec){
  e.preventDefault();
  subDelayHold(deltaSec);
}
function subDelayRelease(){
  clearTimeout(_subDelayHoldTimer);
  clearInterval(_subDelayHoldIval);
  _subDelayHoldTimer = _subDelayHoldIval = null;
}

function subSyncToMovieTime(){
  if(!_subCuesBase.length){ toast('Load a subtitle file first','wrn'); return; }
  const inp = document.getElementById('sub-sync-inp');
  const raw = (inp ? inp.value : '').trim();
  if(!raw){ toast('Enter the movie time shown on screen (e.g. 34:31)','wrn'); return; }
  const parts = raw.split(':').map(Number);
  if(parts.some(isNaN) || parts.length < 2){
    toast('Invalid format — use MM:SS or H:MM:SS','err'); return;
  }
  const movieSecs = parts.length === 3
    ? parts[0]*3600 + parts[1]*60 + parts[2]
    : parts[0]*60 + parts[1];
  const vidEl = document.getElementById('vid');
  if(!vidEl || !vidEl.currentTime){ toast('No stream playing','wrn'); return; }
  subDelayMs = Math.round((vidEl.currentTime - movieSecs) * 1000);
  _subLoadCuesToTrack(_subCuesBase);
  const dv = document.getElementById('sub-delay-val');
  if(dv) dv.textContent = (subDelayMs>=0?'+':'') + (subDelayMs/1000).toFixed(1) + 's';
  const ss = document.getElementById('sub-sync-status');
  if(ss){ ss.textContent = '✓ Synced to ' + raw; ss.style.display='inline'; setTimeout(()=>{ ss.style.display='none'; }, 3000); }
  toast('✓ Subtitles synced to ' + raw, 'ok');
  if(inp) inp.value = '';
}

function subToggleVisible(){
  if(!_subTrackObj) return;
  const nowShowing = _subTrackObj.mode === 'showing';
  _subTrackObj.mode = nowShowing ? 'hidden' : 'showing';
  const btn = document.getElementById('sub-toggle-btn');
  if(btn) btn.innerHTML = !nowShowing ? '&#x1F441; On' : '&#x1F648; Off';
}

function clearSubtitle(){
  _subClearNativeTrack();
  _subCuesBase = []; subDelayMs = 0;
  _subActiveFile = null;
  const info = document.getElementById('sub-active-info');
  if(info) info.style.display='none';
  const subBtn = document.getElementById('subbtn');
  if(subBtn) subBtn.style.opacity='0.35';
  const _dr = document.getElementById('sub-delay-row');
  if(_dr) _dr.style.display = 'none';
  const _sr2 = document.getElementById('sub-sync-row');
  if(_sr2) _sr2.style.display = 'none';
  const _tb = document.getElementById('sub-toggle-btn');
  if(_tb) _tb.innerHTML = '&#x1F441; On';
  toast('Subtitle removed','info');
}

// ── SUBTITLE TAB SWITCHER ──────────────────────────────────
// Auto-detect mobile on first use (includes 900px width emulation)
let _subFbMode = (typeof _isMobile !== 'undefined' && _isMobile) || window.innerWidth <= 900;
function subForceFileBrowser(){ _subFbMode=false; subToggleFileBrowser(); }
function subToggleFileBrowser(){
  _subFbMode = !_subFbMode;
  document.getElementById('sub-local-desktop').style.display = _subFbMode ? 'none' : '';
  document.getElementById('sub-local-mobile').style.display  = _subFbMode ? ''     : 'none';
  document.getElementById('sub-local-status').textContent = '';
  // Update both toggle buttons
  const label = _subFbMode ? '\uD83D\uDCC1 File browser: On' : '\uD83D\uDCC1 File browser: Off';
  const b1 = document.getElementById('sub-fb-toggle-btn');
  const b2 = document.getElementById('sub-fb-toggle-btn2');
  if(b1){ b1.textContent=label;
    b1.style.background=_subFbMode?'rgba(124,58,237,.2)':'';
    b1.style.borderColor=_subFbMode?'var(--acc)':'';
    b1.style.color=_subFbMode?'var(--txt)':''; }
  if(b2){ b2.textContent=label;
    b2.style.background=_subFbMode?'rgba(124,58,237,.2)':'';
    b2.style.borderColor=_subFbMode?'var(--acc)':'';
    b2.style.color=_subFbMode?'var(--txt)':''; }
  // On mobile there is no tkinter picker — keep switch buttons hidden
  if(typeof _isMobile !== 'undefined' && _isMobile){
    if(b1) b1.style.display='none';
    if(b2) b2.style.display='none';
  }
  if(_subFbMode) subFbNav(_subFbCurrentPath);
}

function subSwitchTab(tab){
  const isOnline = tab === 'online';
  document.getElementById('sub-panel-online').style.display = isOnline ? '' : 'none';
  document.getElementById('sub-panel-local').style.display  = isOnline ? 'none' : '';
  document.getElementById('sub-tab-online').classList.toggle('active', isOnline);
  document.getElementById('sub-tab-local').classList.toggle('active', !isOnline);
  if(!isOnline){
    // Use _isMobile (includes width<=900) or user's forced browser mode
    const _subUseMobile = _subFbMode || (typeof _isMobile!=='undefined' && _isMobile) || window.innerWidth<=900;
    document.getElementById('sub-local-desktop').style.display = _subUseMobile ? 'none' : '';
    document.getElementById('sub-local-mobile').style.display  = _subUseMobile ? ''     : 'none';
    // Sync button states
    const _subLabel = _subUseMobile ? '\uD83D\uDCC1 File browser: On' : '\uD83D\uDCC1 File browser: Off';
    [document.getElementById('sub-fb-toggle-btn'),document.getElementById('sub-fb-toggle-btn2')].forEach(b=>{
      if(!b) return;
      b.textContent=_subLabel;
      b.style.background=_subUseMobile?'rgba(124,58,237,.2)':'';
      b.style.borderColor=_subUseMobile?'var(--acc)':'';
      b.style.color=_subUseMobile?'var(--txt)':'';
    });
    // On mobile there is no tkinter picker — hide switch buttons entirely
    if(typeof _isMobile !== 'undefined' && _isMobile){
      [document.getElementById('sub-fb-toggle-btn'),document.getElementById('sub-fb-toggle-btn2')].forEach(b=>{
        if(b) b.style.display='none';
      });
    }
    document.getElementById('sub-local-status').textContent = '';
    if(_subUseMobile){
      subFbNav(_subFbCurrentPath);
    } else {
      const inp = document.getElementById('sub-local-input');
      if(inp) inp.value = '';
      document.getElementById('sub-local-filename').textContent = 'No file chosen';
    }
  }
}

// ── DESKTOP: tkinter file picker ───────────────────────────
async function subBrowseDesktop(){
  const stEl = document.getElementById('sub-local-status');
  stEl.textContent = 'Opening file picker\u2026';
  try{
    const r = await fetch('/api/browse_subtitle');
    const d = await r.json();
    if(d.error || !d.path){ stEl.textContent = d.error ? '\u26a0 '+d.error : 'No file selected.'; return; }
    stEl.textContent = 'Loading\u2026';
    document.getElementById('sub-local-filename').textContent = d.path.split(/[\\\/]/).pop();
    await _subLoadFromServerPath(d.path, stEl);
  } catch(e){
    stEl.textContent = '';
    document.getElementById('sub-local-input').value = '';
    document.getElementById('sub-local-input').click();
  }
}

// ── MOBILE: inline file browser ────────────────────────────
let _subFbCurrentPath = '/sdcard/Download';

function subFbUp(){
  const el = document.getElementById('sub-fb-path');
  const cur = (el && el.textContent) || _subFbCurrentPath;
  const parent = cur.replace(/\/[^/]+$/, '') || '/';
  subFbNav(parent);
}

async function subFbNav(path){
  _subFbCurrentPath = path;
  const listEl = document.getElementById('sub-fb-list');
  const pathEl = document.getElementById('sub-fb-path');
  const upBtn  = document.getElementById('sub-fb-up');
  if(pathEl) pathEl.textContent = path;
  listEl.innerHTML = '<div style="padding:10px;font-size:12px;color:var(--txt3)">Loading\u2026</div>';
  try{
    const r = await fetch('/api/browse_dir',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path}),
    });
    const d = await r.json();
    if(upBtn) upBtn.disabled = !d.parent;
    if(d.error && !d.dirs.length && !d.files.length){
      listEl.innerHTML = `<div style="padding:10px;font-size:12px;color:#f87171">\u26a0 ${esc(d.error)}</div>`;
      return;
    }
    const rows = [];
    for(const name of d.dirs){
      const fullPath = path.replace(/\/+$/,'') + '/' + name;
      rows.push(`<div class="sub-fb-row sub-fb-dir" onclick="subFbNav('${esc(fullPath)}')">
        <span class="sub-fb-icon">&#x1F4C1;</span><span class="sub-fb-name">${esc(name)}</span><span class="sub-fb-arr">\u203a</span>
      </div>`);
    }
    for(const name of d.files){
      const fullPath = path.replace(/\/+$/,'') + '/' + name;
      rows.push(`<div class="sub-fb-row sub-fb-file" onclick="subFbPickFile('${esc(fullPath)}','${esc(name)}')">
        <span class="sub-fb-icon">&#x1F4AC;</span><span class="sub-fb-name">${esc(name)}</span>
      </div>`);
    }
    if(!rows.length){
      rows.push('<div style="padding:10px;font-size:12px;color:var(--txt3)">No subtitle files here. Tap a folder to browse.</div>');
    }
    listEl.innerHTML = rows.join('');
  } catch(e){
    listEl.innerHTML = `<div style="padding:10px;font-size:12px;color:#f87171">\u26a0 ${esc(String(e))}</div>`;
  }
}

async function subFbPickFile(fullPath, name){
  const stEl = document.getElementById('sub-local-status');
  stEl.textContent = 'Loading ' + name + '\u2026';
  await _subLoadFromServerPath(fullPath, stEl);
}

async function _subLoadFromServerPath(path, stEl){
  try{
    const r = await fetch('/api/load_subtitle_path',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path}),
    });
    const d = await r.json();
    if(d.error){ stEl.textContent = '\u26a0 '+d.error; toast(d.error,'err'); return; }
    _subApplyToPlayer(d.content, d.file_name, d.mime);
    _subActiveFile = {name: d.file_name};
    document.getElementById('sub-active-name').textContent = d.file_name;
    document.getElementById('sub-active-info').style.display = 'flex';
    const subBtn = document.getElementById('subbtn');
    if(subBtn) subBtn.style.opacity = '1';
    stEl.style.color = 'var(--green)';
    stEl.textContent = '\u2713 Loaded: ' + d.file_name;
    document.getElementById('sub-status-msg').textContent = 'Ready';
    toast('Subtitle loaded','ok');
  } catch(e){
    stEl.textContent = '\u26a0 Error: '+e;
    toast('Failed to load subtitle','err');
  }
}

// ── BROWSER FILE INPUT (desktop fallback / direct pick) ────
function subLoadLocalFile(input){
  const file = input.files && input.files[0];
  if(!file){ return; }
  const fnEl = document.getElementById('sub-local-filename');
  const stEl = document.getElementById('sub-local-status');
  fnEl.textContent = file.name;
  stEl.textContent = 'Reading file\u2026';
  const reader = new FileReader();
  reader.onload = function(e){
    const content = e.target.result;
    if(!content){ stEl.textContent = '\u26a0 File appears empty.'; return; }
    const lower = file.name.toLowerCase();
    let mime = 'text/vtt';
    if(lower.endsWith('.srt')) mime = 'text/srt';
    else if(lower.endsWith('.ass') || lower.endsWith('.ssa')) mime = 'text/x-ssa';
    _subApplyToPlayer(content, file.name, mime);
    _subActiveFile = {name: file.name};
    document.getElementById('sub-active-name').textContent = file.name;
    document.getElementById('sub-active-info').style.display = 'flex';
    const subBtn = document.getElementById('subbtn');
    if(subBtn) subBtn.style.opacity = '1';
    stEl.style.color = 'var(--green)';
    stEl.textContent = '\u2713 Loaded: ' + file.name;
    document.getElementById('sub-status-msg').textContent = 'Ready';
    toast('Subtitle loaded','ok');
  };
  reader.onerror = function(){ stEl.textContent = '\u26a0 Failed to read file.'; toast('Failed to read subtitle file','err'); };
  reader.readAsText(file, 'utf-8');
}

function _subInitLangGrid(){
  const grid = document.getElementById('sub-lang-grid');
  if(!grid || grid.children.length) return;
  const defaults = new Set(['en']);
  grid.innerHTML = SUB_LANGS.map(l => `
    <label class="sub-lang-chip">
      <input type="checkbox" value="${l.code}" ${defaults.has(l.code)?'checked':''}>
      ${l.label}
    </label>`).join('');
}

function openSubSearch(){
  _subInitLangGrid();
  // Populate API key field from localStorage each time modal opens
  try{
    const ak = document.getElementById('sub-apikey');
    if(ak && !ak.value) ak.value = localStorage.getItem('opensubtitles_key')||'';
  }catch(e){}
  const q = document.getElementById('sub-query');
  if(pName && !q.value){
    let cleaned = pName
      .replace(/\bS\d{1,2}E\d{1,2}\b/gi,'')
      .replace(/\b(720p|1080p|4k|hevc|h264|h265|hd|sd|fhd|uhd|bluray|webrip|web-dl|xvid|x264|x265)\b/gi,'')
      .replace(/[._\-\[\]()]+/g,' ')
      .replace(/\s{2,}/g,' ').trim();
    q.value = cleaned;
    const epMatch = pName.match(/[Ss](\d{1,2})[Ee](\d{1,2})/);
    if(epMatch){
      document.getElementById('sub-type-series').checked = true;
      document.getElementById('sub-season').value  = epMatch[1];
      document.getElementById('sub-episode').value = epMatch[2];
      subToggleEp();
    }
  }
  const info = document.getElementById('sub-active-info');
  if(_subActiveFile){
    document.getElementById('sub-active-name').textContent = _subActiveFile.name;
    info.style.display = 'flex';
  } else {
    info.style.display = 'none';
  }
  document.getElementById('sub-overlay').classList.add('open');
  setTimeout(()=>document.getElementById('sub-query').focus(), 150);
}

function closeSubSearch(){
  document.getElementById('sub-overlay').classList.remove('open');
}

function subToggleEp(){
  const isSeries = document.getElementById('sub-type-series').checked;
  document.getElementById('sub-ep-row').style.display = isSeries ? 'flex' : 'none';
}

function subSeasonChange(){
  const s = document.getElementById('sub-season').value;
  if(s) document.getElementById('sub-episode').focus();
}

function _subGetLangs(){
  const checks = document.querySelectorAll('#sub-lang-grid input[type=checkbox]:checked');
  const codes = Array.from(checks).map(c=>c.value);
  return codes.length ? codes.join(',') : 'en';
}

async function subSearch(){
  const query = document.getElementById('sub-query').value.trim();
  if(!query){ toast('Enter a title to search','err'); return; }
  const isSeries = document.getElementById('sub-type-series').checked;
  const season   = isSeries ? (document.getElementById('sub-season').value||null) : null;
  const episode  = isSeries ? (document.getElementById('sub-episode').value||null) : null;
  const lang     = _subGetLangs();
  const maxR     = document.getElementById('sub-maxresults').value;
  const btn  = document.getElementById('sub-search-btn');
  const wrap = document.getElementById('sub-results-wrap');
  const msg  = document.getElementById('sub-status-msg');
  btn.disabled = true;
  btn.textContent = '\u23f3 Searching\u2026';
  msg.textContent = 'Searching OpenSubtitles\u2026';
  wrap.innerHTML = '<div class="sub-empty"><span class="spin" style="font-size:28px;display:block;margin-bottom:12px"></span>Searching\u2026</div>';
  try{
    const r = await fetch('/api/subtitles/search',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({query, lang, season, episode, type: isSeries ? 'episode' : 'movie', max_results: parseInt(maxR), api_key: _getSubKey()}),
    });
    const d = await r.json();
    if(d.error){ toast('Search error: '+d.error,'err'); wrap.innerHTML=_subEmpty('Search failed: '+esc(d.error)); return; }
    if(!d.results || !d.results.length){
      wrap.innerHTML = _subEmpty('No subtitles found. Try a different title or language.');
      msg.textContent = 'No results.';
      return;
    }
    msg.textContent = d.count + ' result(s) found';
    _subRenderResults(d.results);
  } catch(e){
    wrap.innerHTML = _subEmpty('Network error: '+esc(String(e)));
    msg.textContent = 'Error.';
  } finally {
    btn.disabled = false;
    btn.textContent = '\u1F50D Search';
  }
}

function _subEmpty(msg){
  return `<div class="sub-empty"><span>&#x1F4AC;</span>${msg}</div>`;
}

function _subRenderResults(results){
  const wrap = document.getElementById('sub-results-wrap');
  const parts = results.map((item, i) => {
    const epStr = (item.season && item.episode)
      ? ` <span class="sub-meta-badge sub-meta-ep">S${String(item.season).padStart(2,'0')}E${String(item.episode).padStart(2,'0')}</span>`
      : '';
    const yearStr = item.year ? ` (${item.year})` : '';
    return `<div class="sub-result-item">
      <div class="sub-result-info">
        <div class="sub-result-title">${esc(item.title)}${yearStr}</div>
        <div class="sub-result-meta">
          <span class="sub-meta-badge sub-meta-lang">${esc(item.lang)}</span>
          ${epStr}
          <span class="sub-meta-badge sub-meta-dl">&#x2B07; ${item.downloads}</span>
          <span class="sub-meta-badge sub-meta-rat">&#x2605; ${item.rating}</span>
        </div>
        <div class="sub-result-release">${esc(item.file_name || '')} &bull; ${esc(item.uploader)}</div>
      </div>
      <button class="btn-ghost sub-load-btn" id="sub-load-${i}"
        onclick="subLoadSubtitle(${item.file_id}, '${esc(item.file_name||'subtitle')}', ${i})"
        title="Load into player">&#x25B6; Load</button>
    </div>`;
  });
  wrap.innerHTML = `<div class="sub-results">${parts.join('')}</div>`;
}

async function subLoadSubtitle(fileId, fileName, btnIdx){
  const btn = document.getElementById('sub-load-'+btnIdx);
  const msg = document.getElementById('sub-status-msg');
  if(btn){ btn.disabled=true; btn.textContent='\u23f3\u2026'; }
  msg.textContent = 'Downloading subtitle\u2026';
  try{
    const r = await fetch('/api/subtitles/download',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({file_id: fileId, api_key: _getSubKey()}),
    });
    const d = await r.json();
    if(d.error){ toast('Download failed: '+d.error,'err'); if(btn){btn.disabled=false;btn.textContent='\u25b6 Load';} return; }
    _subApplyToPlayer(d.content, d.file_name || fileName, d.mime || 'text/srt');
    _subActiveFile = {name: d.file_name || fileName};
    document.getElementById('sub-active-name').textContent = _subActiveFile.name;
    document.getElementById('sub-active-info').style.display = 'flex';
    if(btn){ btn.textContent='\u2713 Loaded'; btn.classList.add('loaded'); }
    msg.textContent = 'Loaded: ' + (d.file_name||fileName) + (d.remaining!==undefined ? ' | Quota left: '+d.remaining : '');
    const subBtn = document.getElementById('subbtn');
    if(subBtn) subBtn.style.opacity='1';
    toast('Subtitle loaded','ok');
  } catch(e){
    toast('Error: '+e,'err');
    if(btn){ btn.disabled=false; btn.textContent='\u25b6 Load'; }
  }
}

"""
