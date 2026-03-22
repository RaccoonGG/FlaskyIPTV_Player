# IPTV Portal Player — Flask/WebView Edition
**by GG_Raccoon** · `FlaskyIPTV_Player_byGG.py`

A self-contained Flask web app for browsing and playing MAC/Xtream/M3U IPTV portals. Runs locally on Windows, Linux, or Android (Termux) and is accessed through any browser or WebView. No cloud, no ads, no external dependencies beyond what you install yourself.

---


## Screenshots

| Desktop | Mobile |
|---------|--------|
| **Main player** | |
| <a href="https://github.com/user-attachments/assets/bfe3b0f4-247e-4cb6-b42a-5f14ae4b8b9f"><img width="300" src="https://github.com/user-attachments/assets/bfe3b0f4-247e-4cb6-b42a-5f14ae4b8b9f" alt="Main player desktop"></a> | <a href="https://github.com/user-attachments/assets/9c35f0f8-6642-4789-b8f1-854e88909f65"><img width="100" src="https://github.com/user-attachments/assets/9c35f0f8-6642-4789-b8f1-854e88909f65" alt="Main player mobile"></a> |
| **Multi-View grid** | |
| <a href="https://github.com/user-attachments/assets/f0540bf2-102d-4714-a0e5-6c6ae38f356b"><img width="300" src="https://github.com/user-attachments/assets/f0540bf2-102d-4714-a0e5-6c6ae38f356b" alt="Multi-View desktop"></a> | <a href="https://github.com/user-attachments/assets/9199509b-9d52-4e03-8bd0-b2ce56099b4f"><img width="100" src="https://github.com/user-attachments/assets/9199509b-9d52-4e03-8bd0-b2ce56099b4f" alt="Multi-View mobile"></a> |
| **EPG / TV Guide** | |
| <a href="https://github.com/user-attachments/assets/5b7f8771-a247-4009-a309-c4e7137311b9"><img width="300" src="https://github.com/user-attachments/assets/5b7f8771-a247-4009-a309-c4e7137311b9" alt="EPG Whats on Now desktop"></a> <a href="https://github.com/user-attachments/assets/2be30c07-af3a-442e-a288-9efc7c7eeee0"><img width="300" src="https://github.com/user-attachments/assets/2be30c07-af3a-442e-a288-9efc7c7eeee0" alt="EPG per channel desktop"></a> <a href="https://github.com/user-attachments/assets/ebd72dd2-fecd-4c8a-acf7-bde8b06b4d8a"><img width="300" src="https://github.com/user-attachments/assets/ebd72dd2-fecd-4c8a-acf7-bde8b06b4d8a" alt="EPG layout desktop"></a> | <a href="https://github.com/user-attachments/assets/23f84816-b817-450f-b22d-431fb909ecff"><img width="100" src="https://github.com/user-attachments/assets/23f84816-b817-450f-b22d-431fb909ecff" alt="EPG Whats on Now mobile"></a> <a href="https://github.com/user-attachments/assets/ad8eeaa4-61b3-4250-9b4d-d521842d48aa"><img width="100" src="https://github.com/user-attachments/assets/ad8eeaa4-61b3-4250-9b4d-d521842d48aa" alt="EPG per channel mobile"></a> <a href="https://github.com/user-attachments/assets/b00921a6-47b2-4031-99ab-861e7332cc93"><img width="100" src="https://github.com/user-attachments/assets/b00921a6-47b2-4031-99ab-861e7332cc93" alt="EPG layout mobile"></a> |
| **DVR — scheduled recordings** | |
| <a href="https://github.com/user-attachments/assets/e4563bd5-3266-485e-b8ce-471b36d6122a"><img width="300" src="https://github.com/user-attachments/assets/e4563bd5-3266-485e-b8ce-471b36d6122a" alt="DVR desktop"></a> | <a href="https://github.com/user-attachments/assets/671ce34b-01f5-428f-84a8-e331f8d536a2"><img width="100" src="https://github.com/user-attachments/assets/671ce34b-01f5-428f-84a8-e331f8d536a2" alt="DVR mobile"></a> |

---

## ⚠️ Legal Disclaimer

**This tool does not provide, host, stream, or distribute any media content whatsoever.**

It is a local player interface — a front-end that connects to IPTV portals and services that you supply yourself. It is your sole responsibility to ensure that any portal, stream, or content you access through this tool is one you have a legal right to use.

- Do not use this tool to access content you do not have rights or a valid subscription to.
- Recording and downloading features are intended strictly for content you are legally permitted to record or save — for example, for personal backup or time-shifting where permitted by your subscription and applicable law.
- The author provides this software as a technical tool only and accepts no responsibility for any misuse, copyright infringement, or illegal activity carried out by users of this software.
- By using this tool you agree that you are solely responsible for ensuring your use complies with all applicable laws and terms of service.

---

## Requirements

- Python 3.9+ (tested on 3.14)
- `ffmpeg` and `ffprobe` in PATH (required for recording, downloading, HEVC proxy, casting, DVR, and Multi-View)
- `yt-dlp` in PATH (optional — used as HLS fallback and for YouTube/Twitch URL resolution in Multi-View tiles)
- Python packages: `flask`, `aiohttp`, `requests`, `yt-dlp`
- `cast_addon.py` in the same directory (optional — enables casting to TV/speakers)
- `multiview_addon.py` in the same directory (optional — enables Multi-View grid player)
- `dvr_addon.py` in the same directory (optional — enables DVR scheduled recording tab)

---

## Files

| File | Required | Purpose |
|---|---|---|
| `FlaskyIPTV_Player_byGG.py` | ✓ | Main app |
| `install_requirements_FlaskyIPTV_Player.py` | — | Run once to install dependencies |
| `cast_addon.py` | optional | Casting to Chromecast / DLNA / AirPlay |
| `multiview_addon.py` | optional | Multi-View grid player |
| `multiview_layouts.json` | auto-created | Saved Multi-View layouts (created on first save) |
| `dvr_addon.py` | optional | DVR — scheduled and manual recordings |
| `dvr_jobs.json` | auto-created | Saved DVR job list (created on first scheduled recording) |

---

## Setup — Run the Installer First

Before starting the app for the first time, run the bundled installer:

```bash
python install_requirements_FlaskyIPTV_Player.py
```

The installer will:

- Check your Python version (3.9+ required)
- Install all required pip packages (`flask`, `aiohttp`, `requests`)
- Install `yt-dlp` as an optional package
- Check if `ffmpeg` and `ffprobe` are available in PATH
- **Detect `cast_addon.py`** and interactively offer to install each cast protocol package
- **Detect `multiview_addon.py`** and verify its dependencies (ffmpeg + yt-dlp)
- **Detect `dvr_addon.py`** and verify its dependencies (ffmpeg required)
- On **Android/Termux** — automatically installs `ffmpeg` via `pkg install ffmpeg` if missing
- On **Windows/macOS/Linux** — prints install instructions for your platform if ffmpeg is missing
- Check that port 5000 is free

**Platform-specific ffmpeg install (if not already installed):**

| Platform | Command |
|---|---|
| Windows | `winget install ffmpeg` or `choco install ffmpeg` or download from ffmpeg.org |
| macOS | `brew install ffmpeg` |
| Ubuntu/Debian | `sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |
| Arch | `sudo pacman -S ffmpeg` |
| Android/Termux | handled automatically by the installer (`pkg install ffmpeg`) |

Once the installer finishes with no errors, proceed to running the app.

---

## Running the App

```bash
python FlaskyIPTV_Player_byGG.py
```

Then open **http://localhost:5000** in your browser or WebView.

On Android (Termux), the server binds to all interfaces so you can also reach it from other devices on the same network via `http://<device-ip>:5000`.

---

## Portal Types Supported

| Type | How to connect |
|---|---|
| **MAC / Stalker Portal** | Enter portal URL + MAC address |
| **Xtream Codes** | Enter URL + username + password |
| **M3U** | Enter direct M3U URL or local file path |

---

## Features

### Browsing & Playback
- Browse Live TV, VOD, and Series categories with search and tag filtering
- HLS.js in-browser playback with automatic ffmpeg fallback for problematic streams
- HEVC/H.265 streams are routed through an ffmpeg HLS proxy automatically
- Favourites — saved per portal in browser storage, survive page reloads
- Playlist manager — save and quickly reconnect to multiple portals

### Multi-View (`multiview_addon.py`)

Watch up to 9 streams simultaneously in a resizable, draggable grid — live channels, VODs, or any URL side by side.

**Requires:** `multiview_addon.py` in the same directory + `ffmpeg` installed.

#### Opening Multi-View
- **Desktop:** click the **⊞ Multi-View** button in the Player Controls bar
- **Mobile:** tap the **⊞ Multi** tab in the bottom navigation
- Close with the **⊞ ✕** button in the top-right corner of the toolbar

#### Grid Layout
- Up to **9 players** simultaneously, each in its own resizable and draggable tile
- **Desktop:** drag tiles by their header bar; resize by dragging any edge or corner handle
- **Mobile:** drag tiles by their header bar; resize handles are enlarged for touch use
- Preset layouts: **Auto** (fits current player count), **2×2**, **1+3** (large left + three stacked right)
- Save and load named layouts — layouts persist across sessions in `multiview_layouts.json`

#### Playing Content
Each tile has two ways to load content:

- **📺 Select IPTV channel** — opens a category browser to pick any live channel from your connected portal. The same portal and categories as the main browse panel are available.
- **🔗 Play URL** — opens an inline URL bar. Paste any of the following and press Enter or ▶ Play:
  - Direct stream URLs (`.m3u8`, `.ts`, Xtream stream URLs, etc.) — played directly, no yt-dlp needed
  - YouTube, Twitch, Dailymotion, Vimeo URLs — resolved to a streamable URL via yt-dlp (yt-dlp must be installed)

#### Audio & Volume
- Each tile has its own independent **🔊/🔇 mute button** and **volume slider**
- Clicking or interacting with a tile gives it the active highlight border — this does **not** mute any other tile
- Audio from all unmuted tiles plays simultaneously
- If the browser throttles or mutes a tile when you switch tabs, it is automatically restored when you return

#### Portal Connection Tracking
When playing IPTV channels, each tile's header shows:
- The **channel name**
- The **portal hostname** and a live **connection count** — e.g. `myportal.tv  ·  2 connections`
- If your portal exposes a max-connection limit (Xtream portals typically do), the count shows as a fraction — e.g. `2/4 connections`
- The count turns **amber** when one slot away from the limit and **red** when at the limit
- The count updates live as tiles start and stop

#### Stream Deduplication
- Two tiles playing the same channel URL from the same portal share a single ffmpeg process server-side — only one connection is made to the IPTV source regardless of how many tiles show the same channel
- Streams are automatically cleaned up 30 seconds after the last viewer disconnects

#### Requirements & Browser Support
- Requires a browser with **MSE (Media Source Extensions)** support: Chrome, Edge, Firefox, Brave
- On Android: Firefox for Android has full MSE support; some WebView-based browsers may not
- ffmpeg must be installed — each tile runs one ffmpeg process to transcode the stream to MPEG-TS for the browser

### DVR — Scheduled Recording (`dvr_addon.py`)

A full DVR tab for scheduling, managing, and playing back recordings of live channels.

**Requires:** `dvr_addon.py` in the same directory + `ffmpeg` installed.

#### Scheduling Recordings
- **From EPG** — open the EPG panel for any live channel, find a programme, and click **⏺ Record** to schedule it. Start and end times are filled in automatically from the EPG data.
- **Manual** — use the **+ Manual** button in the DVR tab to schedule any channel for a custom time range. Pick the channel from your connected portal's live list, set start and end time, and confirm.
- **Record Now** — start an immediate recording of the currently playing channel from the Player tab using the **⏺ Record** button. Duration is set manually.

#### Job States
Each recording job passes through the following states:

| State | Meaning |
|---|---|
| `scheduled` | Waiting for the start time — ffmpeg has not started yet |
| `recording` | ffmpeg is actively writing the `.ts` file |
| `completed` | Recording finished successfully |
| `error` | ffmpeg exited with an error (error message shown in the job card) |
| `cancelled` | Job was cancelled before it started |

The scheduler wakes every 5 seconds to fire any jobs that are due. Missed recordings (start time passed while the app was not running) are marked as missed on next startup.

#### Editing & Cancelling Jobs
- Scheduled jobs can have their start and end times edited before recording begins
- Any scheduled job can be cancelled; in-progress recordings can be stopped early
- Completed jobs and their history entries can be deleted individually or all at once

#### Timeshift — Watch While Recording
While a recording is in progress you can watch it from the beginning (or any point already recorded) without making a second connection to the portal:

- Click **▶ Watch** on an active recording job
- Playback starts from the beginning of the recording file as ffmpeg writes it in real time
- Full seeking within the already-recorded portion is supported
- HEVC and AC3 audio are transcoded on the fly to H.264/AAC so any browser can play it

#### Completed Recordings Library
- All completed `.ts` files are listed in the **Recordings** section of the DVR tab
- Each entry shows filename, file size, and recording duration
- **▶ Play** — plays the recording in the built-in player (HEVC is transcoded automatically)
- **🗑 Delete** — removes the recording file from disk
- **📂 Show in folder** — opens the containing folder in Windows Explorer / macOS Finder (desktop only)

#### Storage
- A storage usage bar shows how much disk space the DVR folder is using and what is available
- The DVR output folder can be changed from within the DVR tab:
  - **Desktop:** opens a native OS folder picker dialog
  - **Android/Termux:** opens an inline folder browser to navigate the device filesystem
- Recordings are saved as `.ts` files — compatible with VLC, MPV, and most media players

### Cast to TV / Speakers (`cast_addon.py`)
- Cast any live channel, VOD, or series stream to a device on your local network
- **📺 Cast button** in the header opens the cast panel — discover devices, connect, and control playback
- **Auto-cast mode** — when enabled, channel clicks cast directly instead of playing in the browser (prevents double-connection issues with single-connection IPTV tokens)
- **Inline cast buttons** appear next to each external-player button in the channel list for one-tap casting
- Supports three cast protocols — install any combination, each degrades gracefully if its package is missing:

  | Protocol | Package | Devices |
  |---|---|---|
  | **Chromecast** | `pychromecast` | Chromecast, Google TV, Chromecast-enabled TVs |
  | **DLNA / UPnP** | `async-upnp-client` | Smart TVs, media renderers, DLNA-compatible speakers |
  | **AirPlay** | `pyatv` | Apple TV, HomePod, AirPlay 2 speakers |

- Stream proxy built-in — cast devices receive a plain LAN URL so auth headers and tokens are handled transparently by the proxy; no extra setup needed
- HLS transcoding via FFmpeg for Chromecast profile (H.264 re-encode ensures HEVC streams play correctly on Chromecast which does not support HEVC passthrough)

**Install cast packages** (or let the installer prompt you):
```bash
pip install pychromecast          # Chromecast / Google TV
pip install async-upnp-client     # DLNA / UPnP
pip install pyatv                 # AirPlay
```

### EPG (TV Guide)
- Shows current and upcoming programme info per channel
- Supports portal-native EPG (Stalker and Xtream)
- Supports external XMLTV EPG URLs as a fallback or override
- **What's On Now** tab — searches your EPG for what's currently airing across all channels, with a Find Channel button to check if your portal carries it

### Catch-Up TV
- Stalker Portal catch-up (where supported by the portal)
- Xtream Codes catch-up (where supported by the portal)
- Browse archived listings by date and time

### Downloading
- **Save M3U** — export selected categories or all channels as a .m3u playlist file
- **Record MKV** — record a live stream to MKV using ffmpeg with real-time progress (KB/s speed, file size)
- **Download MKV** — download VOD/series items to MKV

### Subtitles
- Search and download subtitles from OpenSubtitles.com (free API key required from opensubtitles.com/en/consumers)
- Load local subtitle files directly from device storage (.srt / .vtt / .ass / .ssa)
- Subtitle delay adjustment (+ / −) works for both OpenSubtitles and local files

### VOD / Series Metadata & Links
- Opens the metadata page for VOD and series items via a priority lookup chain:
  1. **TMDB by ID** — if the item carries a `tmdb_id`, opens `themoviedb.org` directly
  2. **IMDb by ID** — if the item carries an `imdb_id`, opens `imdb.com/title/<id>` directly
  3. **IMDb title search** — fallback only when no ID is available; performs a title-name search on IMDb
- Direct ID lookups are instant and always land on the correct page; the title-search fallback is used only as a last resort

### External Player
- Send any stream to an external player instead of the built-in one
- **Desktop:** set path to any player executable (VLC, MPV, MPC-HC, etc.)
- **Mobile:** choose from VLC, MX Player, MX Player Pro, Just Player, or "Ask each time"
- External player buttons appear in the items list, catch-up archive, and What's On Now results

---

## Desktop Layout (browser ≥ 900px wide)

The interface uses a three-column grid:

```
[ Categories 350px ] [ Items 350px ] [ Player — fills rest ]
```

- **Player Controls bar** — collapsible (click the bar to hide/show). Expanded by default. Contains the ⊞ Multi-View button.
- **Activity Log bar** — collapsible inline log at the bottom of the player panel.
- **Theater mode** — hides both category and items columns so the player fills the full width. The controls bar auto-collapses when entering theater mode and restores on exit.

---

## Mobile Layout (browser < 900px)

Four-tab bottom navigation: Browse · Items · Player · Log · **Multi** (⊞)

- Player controls are always visible (no collapsible bar on mobile)
- Tap category icons (📺 Live / 🎬 VOD / 📂 Series / ⭐ Favs) to switch mode and navigate to categories
- FAB (⚡) button gives access to bulk actions in each panel
- Multi-View is accessible via the **⊞ Multi** tab; resize tiles by dragging the edge handles (enlarged for touch)

---

## Settings

All settings are saved in browser localStorage and persist across sessions.

| Setting | Where | Description |
|---|---|---|
| MKV save folder | ⚙ Settings | Default output path for MKV recordings and downloads |
| M3U save path | ⚙ Settings | Default output path for exported M3U files |
| External player (desktop) | ⚙ Settings | Full path to player executable |
| External player (mobile) | ⚙ Settings | Choose VLC / MX Player / MX Pro / Just Player / Ask |
| OpenSubtitles API key | ⚙ Settings | Free key from opensubtitles.com/en/consumers |
| External EPG URL | Connect panel | XMLTV URL used for EPG and What's On Now |
| DVR output folder | DVR tab | Folder where `.ts` recording files are saved |

---

## Android / Termux Notes

- Run the script inside Termux — it starts a Flask server accessible from the phone's own browser or any WebView app
- File browser for local subtitle and DVR folder selection works via `os.listdir()` on the device filesystem
- The `_isMobile` detection uses both User-Agent and touch API checks, so it works correctly inside WebView apps that report a desktop UA
- HEVC streams pass directly to external players without transcoding (external players handle it natively)
- `pyatv` (AirPlay) may not build on Termux — skip it during install if it fails; Chromecast and DLNA will still work
- **Multi-View on Android:** Firefox for Android has full MSE support and works well. Most Chromium-based WebViews also support MSE. Use Firefox or Brave (F-Droid) for the best experience.
- **DVR on Android:** recordings are saved as `.ts` files to the configured folder (defaults to `~/Downloads/DVR`). Timeshift playback works on Android — the HEVC transcode runs server-side in ffmpeg so the browser receives H.264/AAC regardless of the source codec.

---

## Architecture Notes

- Single-file app — everything (Python backend + full HTML/CSS/JS frontend) is in one `.py` file
- `cast_addon.py`, `multiview_addon.py`, and `dvr_addon.py` are fully self-contained drop-in modules; the main app degrades gracefully if any of them are absent
- The HTML is a Jinja2 template rendered by Flask's `render_template_string`
- HLS playback uses **HLS.js** loaded from CDN
- Multi-View playback uses **mpegts.js** loaded from CDN — each tile decodes an MPEG-TS stream via MSE
- Multi-View grid layout uses **GridStack** loaded from CDN
- Subtitle rendering uses **WebVTT** natively or via a cue-injection bridge for .srt/.ass files
- The HLS proxy endpoint (`/api/hls_proxy`) rewrites segment URLs so the browser can play streams that don't support CORS
- Live activity log uses **Server-Sent Events** (`/api/logs`) streamed from a thread-safe queue
- EPG data is cached in memory; large XMLTV files (30k+ channels) may use up to ~2 GB RAM
- Cast stream proxy runs on a separate port and serves plain LAN URLs; cast devices never see auth headers directly
- Multi-View stream proxy (`/api/multiview/stream`) runs one ffmpeg process per unique stream URL, shared across all tiles showing the same channel. A background janitor thread cleans up idle streams after 30 seconds.
- DVR scheduler runs as a background daemon thread, waking every 5 seconds to fire any due jobs. Job state persists to `dvr_jobs.json`. On startup, any job stuck in `recording` state (ffmpeg died while the app was stopped) is recovered — completed if the file exists and has size, otherwise marked as error.

---

## Known Limitations

- MKV download resume (`-ss` seek) can be unreliable on live IPTV streams — ffmpeg may not seek accurately on some stream types
- Large external EPG files load into RAM in full; very large lists (30k+ channels) will use significant memory
- Recording and downloading run as background threads — closing the browser tab does not stop them; use the Stop button first
- AirPlay via `pyatv` bypasses the stream proxy (pyatv does not support custom HTTP headers); auth must be embedded in the resolved stream URL for AirPlay to work
- Multi-View requires MSE support in the browser — older or restricted WebViews may not support it
- Each Multi-View tile runs one ffmpeg process; 9 simultaneous tiles = 9 ffmpeg processes. On low-end hardware (e.g. older Android phones) this may be demanding. Use fewer tiles or lower-bitrate channels if you experience performance issues.
- YouTube/Twitch URL playback in Multi-View tiles requires yt-dlp installed and working. yt-dlp resolves a direct stream URL; the stream is then proxied through ffmpeg the same way as IPTV channels. Live streams work; age-restricted or DRM-protected content will not.
- DVR recordings are saved as raw `.ts` files — they are not remuxed to MKV or MP4. Most players (VLC, MPV, Kodi) handle `.ts` natively; Windows Media Player may not.
- DVR timeshift (watch while recording) transcodes on the fly via ffmpeg — on low-end hardware this may be slow to start or choppy. Timeshift works best on the same machine running the Flask server.
- If the app is stopped while a recording is in progress, ffmpeg is killed and the partial `.ts` file is kept. On next startup the job is recovered as completed (if the file has content) or error (if the file is empty/missing).
