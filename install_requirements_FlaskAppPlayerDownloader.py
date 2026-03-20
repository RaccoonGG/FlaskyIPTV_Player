#!/usr/bin/env python3
"""
Installer for MAC/Xtream/M3U Portal Builder — Flask/WebView Edition
Installs all required Python packages and checks system dependencies.
Run with:  python install_requirements_FlaskAppPlayerDownloader.py
"""

import subprocess
import sys
import shutil
import os

# ── Colours ───────────────────────────────────────────────────────────────────
if sys.platform == "win32":
    os.system("color")   # enable ANSI on modern Windows terminals
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):   print(f"{GREEN}  ✓  {msg}{RESET}")
def warn(msg): print(f"{YELLOW}  ⚠  {msg}{RESET}")
def err(msg):  print(f"{RED}  ✗  {msg}{RESET}")
def info(msg): print(f"{CYAN}  →  {msg}{RESET}")
def hdr(msg):  print(f"\n{BOLD}{msg}{RESET}")

SCRIPT_NAME = "FlaskAppPlayerDownloaderMultiView.py"

# ── Termux detection ──────────────────────────────────────────────────────────
def is_termux() -> bool:
    return (
        os.path.isdir("/data/data/com.termux") or
        "com.termux" in os.environ.get("PREFIX", "") or
        "com.termux" in os.environ.get("HOME", "") or
        os.path.isfile("/data/data/com.termux/files/usr/bin/pkg")
    )

IS_TERMUX = is_termux()

# ── Required pip packages ─────────────────────────────────────────────────────
PACKAGES = [
    ("flask",    "flask",   True,  "Web framework — serves the portal UI"),
    ("aiohttp",  "aiohttp", True,  "Async HTTP client — needed for all portal/API calls"),
    ("requests", "requests",True,  "HTTP library — needed for the HLS/TS stream proxy"),
    ("yt-dlp",   "yt_dlp",  False, "yt-dlp — optional: URL resolver for YouTube/Twitch in Multi-View, and HLS fallback downloader"),
]

# ── cast_addon optional packages ──────────────────────────────────────────────
# Each entry: (pip_name, import_name, description, termux_note)
CAST_PACKAGES = [
    (
        "pychromecast",
        "pychromecast",
        "Chromecast / Google TV casting",
        None,
    ),
    (
        "async-upnp-client",
        "async_upnp_client",
        "DLNA / UPnP media renderer casting",
        None,
    ),
    (
        "pyatv",
        "pyatv",
        "AirPlay casting (Apple TV, HomePod, …)",
        "pyatv may not build on Termux — skip if it fails",
    ),
]


def pip_install(pip_name: str) -> bool:
    info(f"Installing {pip_name} …")
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", pip_name]
    if IS_TERMUX:
        cmd.append("--break-system-packages")
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def check_import(import_name: str) -> bool:
    result = subprocess.run(
        [sys.executable, "-c", f"import {import_name}"],
        capture_output=True
    )
    return result.returncode == 0


def pkg_install(pkg_name: str) -> bool:
    info(f"Running: pkg install -y {pkg_name} …")
    result = subprocess.run(["pkg", "install", "-y", pkg_name], text=True)
    return result.returncode == 0


# ── System dependency checks ──────────────────────────────────────────────────
def check_system_deps():
    hdr("Checking system dependencies …")

    if IS_TERMUX:
        info("Termux detected — system packages will be installed via pkg")

    # ffmpeg
    if shutil.which("ffmpeg"):
        ok("ffmpeg found")
    else:
        if IS_TERMUX:
            warn("ffmpeg NOT found — attempting automatic install via pkg …")
            if pkg_install("ffmpeg"):
                if shutil.which("ffmpeg"):
                    ok("ffmpeg installed successfully via pkg")
                else:
                    err("pkg reported success but ffmpeg not found — try manually: pkg install ffmpeg")
            else:
                err("pkg install ffmpeg failed — run manually: pkg install ffmpeg")
        else:
            warn("ffmpeg NOT found — MKV downloading, recording, casting, and Multi-View won't work")
            if sys.platform == "win32":
                print("       Download from: https://ffmpeg.org/download.html")
                print("       Or via winget:       winget install ffmpeg")
                print("       Or via Chocolatey:   choco install ffmpeg")
            elif sys.platform == "darwin":
                print("       Install via Homebrew: brew install ffmpeg")
            else:
                print("       Install via package manager:")
                print("         Ubuntu/Debian:  sudo apt install ffmpeg")
                print("         Fedora:         sudo dnf install ffmpeg")
                print("         Arch:           sudo pacman -S ffmpeg")

    # ffprobe
    if shutil.which("ffprobe"):
        ok("ffprobe found")
    else:
        if IS_TERMUX:
            warn("ffprobe NOT found — re-running pkg install ffmpeg to pull it in …")
            pkg_install("ffmpeg")
            if shutil.which("ffprobe"):
                ok("ffprobe installed")
            else:
                warn("ffprobe still not found — codec probing will be disabled")
        else:
            warn("ffprobe NOT found — codec probing disabled (usually bundled with ffmpeg)")

    # Browser / WebView guidance
    hdr("Browser / WebView notes …")
    if IS_TERMUX:
        info(f"Start the server:     python {SCRIPT_NAME}")
        info("Then open in browser: http://localhost:5000")
        info("Tip: use Firefox or Brave (from F-Droid) for best HLS/TS playback support")
        info("Multi-View requires MSE (Media Source Extensions) — Firefox for Android supports it")
    elif sys.platform == "win32":
        info("Open http://127.0.0.1:5000 in Chrome, Edge, or Firefox")
    elif sys.platform == "darwin":
        info("Open http://127.0.0.1:5000 in Safari, Chrome, or Firefox")
    else:
        info("Open http://127.0.0.1:5000 in your browser")


# ── cast_addon dependency check/install ───────────────────────────────────────
def check_cast_addon():
    hdr("Checking cast_addon.py …")

    cast_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cast_addon.py")
    if not os.path.isfile(cast_file):
        warn("cast_addon.py not found in this directory — cast feature will be disabled")
        info("Place cast_addon.py alongside the Flask app to enable casting")
        return

    ok("cast_addon.py found")
    print()
    print(f"  {BOLD}cast_addon supports three optional cast protocols.{RESET}")
    print(f"  Install any combination — each degrades gracefully if missing.\n")

    for pip_name, import_name, desc, termux_note in CAST_PACKAGES:
        already = check_import(import_name)
        if already:
            ok(f"{pip_name} — already installed  ({desc})")
            continue

        warn(f"{pip_name} — NOT installed  ({desc})")
        if termux_note and IS_TERMUX:
            info(f"Note: {termux_note}")

        try:
            answer = input(f"  Install {pip_name}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"

        if answer == "y":
            if pip_install(pip_name):
                if check_import(import_name):
                    ok(f"{pip_name} — installed OK")
                else:
                    warn(f"{pip_name} — installed but import failed (may need a restart)")
            else:
                warn(f"{pip_name} — install failed (casting for this protocol will be unavailable)")
        else:
            info(f"Skipped {pip_name} — you can install it later with:  pip install {pip_name}")


# ── multiview_addon check ─────────────────────────────────────────────────────
def check_multiview_addon():
    hdr("Checking multiview_addon.py …")

    mv_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "multiview_addon.py")
    if not os.path.isfile(mv_file):
        warn("multiview_addon.py not found in this directory — Multi-View feature will be disabled")
        info("Place multiview_addon.py alongside the Flask app to enable Multi-View")
        return

    ok("multiview_addon.py found")
    print()
    print(f"  {BOLD}Multi-View requirements:{RESET}")
    print(f"  • ffmpeg  — required (streams each channel through ffmpeg → MPEG-TS to the browser)")
    print(f"  • yt-dlp  — optional (needed only to play YouTube/Twitch/Vimeo URLs in a tile)")
    print()

    # ffmpeg is the hard requirement for multiview — already checked above, just remind
    if shutil.which("ffmpeg"):
        ok("ffmpeg available — Multi-View will work")
    else:
        warn("ffmpeg NOT found — Multi-View will not be able to stream channels")
        info("Install ffmpeg (see system dependencies section above)")

    # yt-dlp is optional — only needed for URL-play feature
    if check_import("yt_dlp"):
        ok("yt-dlp available — YouTube/Twitch URL playback in Multi-View tiles is enabled")
    else:
        warn("yt-dlp NOT installed — YouTube/Twitch/Vimeo URLs in Multi-View tiles won't resolve")
        info("Install with:  pip install yt-dlp")
        info("Direct IPTV stream URLs and .m3u8 links still work without yt-dlp")


# ── dvr_addon check ───────────────────────────────────────────────────────────
def check_dvr_addon():
    hdr("Checking dvr_addon.py …")

    dvr_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dvr_addon.py")
    if not os.path.isfile(dvr_file):
        warn("dvr_addon.py not found in this directory — DVR feature will be disabled")
        info("Place dvr_addon.py alongside the Flask app to enable scheduled/manual recordings")
        return

    ok("dvr_addon.py found")
    print()
    print(f"  {BOLD}DVR requirements:{RESET}")
    print(f"  • ffmpeg  — required (stream-copy recording, timeshift transcode)")
    print(f"  • No extra Python packages needed beyond the core requirements")
    print()

    if shutil.which("ffmpeg"):
        ok("ffmpeg available — DVR recording and timeshift playback will work")
    else:
        warn("ffmpeg NOT found — DVR will not be able to record or play back recordings")
        info("Install ffmpeg (see system dependencies section above)")



def check_port(port: int = 5000):
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                warn(f"Port {port} is already in use — something else may be running on it")
                info(f"Change it with:  PORT=5001 python {SCRIPT_NAME}")
            else:
                ok(f"Port {port} is free")
    except Exception:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  MAC/Xtream/M3U Portal Builder (Flask Edition){RESET}")
    print(f"{BOLD}  Dependency Installer{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"  Python: {sys.version.split()[0]}  |  {sys.executable}")
    if IS_TERMUX:
        print(f"  Platform: Android / Termux")

    if sys.version_info < (3, 9):
        err(f"Python 3.9+ required (you have {sys.version.split()[0]})")
        input("Press enter to exit …")
        sys.exit(1)

    hdr("Installing Python packages …")

    all_ok = True
    for pip_name, import_name, required, desc in PACKAGES:
        if check_import(import_name):
            ok(f"{pip_name} — already installed  ({desc})")
        else:
            if pip_install(pip_name):
                if check_import(import_name):
                    ok(f"{pip_name} — installed OK  ({desc})")
                else:
                    if required:
                        err(f"{pip_name} — installed but import failed!  ({desc})")
                        all_ok = False
                    else:
                        warn(f"{pip_name} — installed but import failed (optional)  ({desc})")
            else:
                if required:
                    err(f"{pip_name} — FAILED to install  ({desc})")
                    all_ok = False
                else:
                    warn(f"{pip_name} — failed to install (optional)  ({desc})")

    check_cast_addon()
    check_multiview_addon()
    check_dvr_addon()
    check_system_deps()
    check_port(5000)

    hdr("Summary")
    if all_ok:
        ok("All required packages installed — ready to run!")
        print()
        info(f"Start the server:  python {SCRIPT_NAME}")
        info(f"Then open:         http://{'localhost' if IS_TERMUX else '127.0.0.1'}:5000")
        print()
        input("Press enter to exit …")
    else:
        err("Some required packages failed — see errors above")
        warn("The app may not work correctly until these are resolved")
        print()
        input("Press enter to exit …")
        sys.exit(1)

    print()


if __name__ == "__main__":
    main()
