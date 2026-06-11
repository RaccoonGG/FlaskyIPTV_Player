"""
radio_addon.py — FlaskyIPTV Radio Addon
=======================================
Unified radio station discovery for FlaskyIPTV Suite.

Sources (in priority order):
  1. RadioBrowser API  (radio-browser.info) — ~35 000 live-checked stations
  2. Shoutcast CSV directory              — genre / keyword search
  3. Named M3U playlists                  — iptv-org radio index, Free-TV, Tundrak …
  4. Hardcoded verified streams           — always-available fallback

Architecture:
  RadioCache          – two-tier (10 min memory + 24 h disk, thread-safe)
  RadioBrowserClient  – multi-server failover, cached, normalised
  ShoutcastClient     – CSV-scraped directory, no API key required
  RadioM3ULoader      – M3U sources, delegates to core.m3u_parser when available
  RadioFavorites      – JSON-backed persistence, thread-safe
  RadioStreamVerifier – HEAD check + ICY metadata extraction

Flask entry-point:
  register_radio_addon(app)  →  mounts /api/radio/* onto any Flask app
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Optional imports
# ──────────────────────────────────────────────────────────────────────────────
try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:          # pragma: no cover
    _requests = None         # type: ignore
    _HAS_REQUESTS = False


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

_CACHE_DIR    = Path.home() / ".flasky_radio_cache"
_CACHE_TTL    = 86_400   # disk cache: 24 h
_MEM_TTL      = 600      # memory cache: 10 min
_REQ_TIMEOUT  = 10       # HTTP timeout in seconds
_UA           = "FlaskyIPTV/1.0 (radio_addon; +https://github.com/your/repo)"

# RadioBrowser servers — tried in order, first successful wins
RADIO_BROWSER_SERVERS: List[str] = [
    "https://at1.api.radio-browser.info",
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://fr1.api.radio-browser.info",
    "https://us1.api.radio-browser.info",
]

# Named M3U playlist sources (visible to the UI via /api/radio/sources)
M3U_SOURCES: Dict[str, str] = {
    "🌍 iptv-org Radio (all — ~20k stations)":
        "https://iptv-org.github.io/iptv/index.radio.m3u",
    "🎵 iptv-org Music":
        "https://iptv-org.github.io/iptv/categories/music.m3u",
    "📰 iptv-org News":
        "https://iptv-org.github.io/iptv/categories/news.m3u",
    "🇮🇹 Italia (Tundrak — daily)":
        "https://cdn.jsdelivr.net/gh/Tundrak/IPTV-Italia@main/iptvitaplus.m3u",
    "🇮🇹 Italia (Free-TV)":
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_italy.m3u8",
    "🇬🇧 UK (Free-TV)":
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_unitedkingdom.m3u8",
    "🇺🇸 USA (Free-TV)":
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_usa.m3u8",
    "🇫🇷 France (Free-TV)":
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_france.m3u8",
    "🇩🇪 Germany (Free-TV)":
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_germany.m3u8",
    "🇪🇸 Spain (Free-TV)":
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_spain.m3u8",
    "🇵🇱 Poland (Free-TV)":
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_poland.m3u8",
    "🇳🇱 Netherlands (Free-TV)":
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_netherlands.m3u8",
}

# Hardcoded verified streams (always available; used as instant fallback before API loads)
BUILTIN_STATIONS: List[Dict[str, Any]] = [
    # ── Italian commercial ───────────────────────────────────────────────────
    {"name": "RTL 102.5",          "url": "https://streamingv2.shoutcast.com/rtl-1025",
     "countrycode": "IT", "tags": "pop,italian",    "bitrate": 128, "source": "builtin"},
    {"name": "Radio DeeJay",       "url": "https://streamingv2.shoutcast.com/radiodeejay",
     "countrycode": "IT", "tags": "pop,dance",       "bitrate": 128, "source": "builtin"},
    {"name": "RDS",                "url": "https://streamingv2.shoutcast.com/rds",
     "countrycode": "IT", "tags": "pop",             "bitrate": 128, "source": "builtin"},
    {"name": "Virgin Radio Italy", "url": "https://streamingv2.shoutcast.com/virginradio",
     "countrycode": "IT", "tags": "rock,pop",        "bitrate": 128, "source": "builtin"},
    {"name": "Radio Monte Carlo",  "url": "https://streamingv2.shoutcast.com/rmc1",
     "countrycode": "IT", "tags": "classic hits",    "bitrate": 128, "source": "builtin"},
    {"name": "Kiss Kiss",          "url": "https://streamingv2.shoutcast.com/kisskiss",
     "countrycode": "IT", "tags": "pop,dance",       "bitrate": 128, "source": "builtin"},
    {"name": "Radio Capital",      "url": "https://streamingv2.shoutcast.com/capitalradio",
     "countrycode": "IT", "tags": "pop",             "bitrate": 128, "source": "builtin"},
    {"name": "m2o",                "url": "https://streamingv2.shoutcast.com/m2o",
     "countrycode": "IT", "tags": "dance,electronic","bitrate": 128, "source": "builtin"},
    {"name": "R101",               "url": "https://streamingv2.shoutcast.com/r101",
     "countrycode": "IT", "tags": "pop",             "bitrate": 128, "source": "builtin"},
    # ── BBC ──────────────────────────────────────────────────────────────────
    {"name": "BBC World Service",  "url": "https://stream.live.vc.bbcmedia.co.uk/bbc_world_service",
     "countrycode": "GB", "tags": "news,talk",       "bitrate": 128, "source": "builtin"},
    {"name": "BBC Radio 1",        "url": "https://stream.live.vc.bbcmedia.co.uk/bbc_radio_one",
     "countrycode": "GB", "tags": "pop,chart",       "bitrate": 128, "source": "builtin"},
    {"name": "BBC Radio 2",        "url": "https://stream.live.vc.bbcmedia.co.uk/bbc_radio_two",
     "countrycode": "GB", "tags": "pop,adult",       "bitrate": 128, "source": "builtin"},
    {"name": "BBC Radio 6 Music",  "url": "https://stream.live.vc.bbcmedia.co.uk/bbc_6music",
     "countrycode": "GB", "tags": "alternative,indie","bitrate": 128, "source": "builtin"},
    {"name": "BBC Radio 4",        "url": "https://stream.live.vc.bbcmedia.co.uk/bbc_radio_fourfm",
     "countrycode": "GB", "tags": "talk,culture",    "bitrate": 128, "source": "builtin"},
    # ── USA ──────────────────────────────────────────────────────────────────
    {"name": "NPR News",           "url": "https://npr-ice.streamguys1.com/live.mp3",
     "countrycode": "US", "tags": "news,talk",       "bitrate": 128, "source": "builtin"},
    # ── France ───────────────────────────────────────────────────────────────
    {"name": "France Inter",       "url": "https://icecast.radiofrance.fr/franceinter-midfi.mp3",
     "countrycode": "FR", "tags": "talk,culture",    "bitrate": 128, "source": "builtin"},
    {"name": "France Info",        "url": "https://icecast.radiofrance.fr/franceinfo-midfi.mp3",
     "countrycode": "FR", "tags": "news",            "bitrate": 128, "source": "builtin"},
    {"name": "France Musique",     "url": "https://icecast.radiofrance.fr/francemusique-midfi.mp3",
     "countrycode": "FR", "tags": "classical",       "bitrate": 128, "source": "builtin"},
    {"name": "France Culture",     "url": "https://icecast.radiofrance.fr/franceculture-midfi.mp3",
     "countrycode": "FR", "tags": "culture,talk",    "bitrate": 128, "source": "builtin"},
    {"name": "NRJ France",         "url": "https://scdn.nrjaudio.fm/fr/30001/mp3_128.mp3",
     "countrycode": "FR", "tags": "pop,dance",       "bitrate": 128, "source": "builtin"},
    # ── Germany ───────────────────────────────────────────────────────────────
    {"name": "Deutschlandfunk",    "url": "https://st01.sslstream.dlf.de/dlf/01/128/mp3/stream.mp3",
     "countrycode": "DE", "tags": "news,talk",       "bitrate": 128, "source": "builtin"},
    # ── Spain ────────────────────────────────────────────────────────────────
    {"name": "Cadena SER",         "url": "https://playerservices.streamtheworld.com/api/livestream-redirect/CADENASER.mp3",
     "countrycode": "ES", "tags": "news,talk",       "bitrate": 128, "source": "builtin"},
    # ── SomaFM (US ambient/indie — very stable) ───────────────────────────────
    {"name": "SomaFM Groove Salad","url": "https://ice4.somafm.com/groovesalad-128-mp3",
     "countrycode": "US", "tags": "ambient,chill",   "bitrate": 128, "source": "builtin"},
    {"name": "SomaFM Drone Zone",  "url": "https://ice4.somafm.com/dronezone-128-mp3",
     "countrycode": "US", "tags": "ambient,drone",   "bitrate": 128, "source": "builtin"},
    {"name": "SomaFM Indie Pop",   "url": "https://ice4.somafm.com/indiepop-128-mp3",
     "countrycode": "US", "tags": "indie,pop",       "bitrate": 128, "source": "builtin"},
    {"name": "SomaFM Space Station","url": "https://ice4.somafm.com/spacestation-128-mp3",
     "countrycode": "US", "tags": "electronic,space","bitrate": 128, "source": "builtin"},
    {"name": "SomaFM Secret Agent","url": "https://ice4.somafm.com/secretagent-128-mp3",
     "countrycode": "US", "tags": "lounge,jazz",     "bitrate": 128, "source": "builtin"},
    {"name": "SomaFM Metal Detector","url": "https://ice4.somafm.com/metal-128-mp3",
     "countrycode": "US", "tags": "metal",           "bitrate": 128, "source": "builtin"},
]


# ══════════════════════════════════════════════════════════════════════════════
# SEARCH HELPER
# Combines normalisation from RADIO_FINDER_4_1 with an improved scorer that
# weighs keyword coverage + difflib similarity + prefix match.
# ══════════════════════════════════════════════════════════════════════════════

class SearchHelper:
    """Pure static helpers — no I/O, safe to unit-test."""

    _STOP = frozenset({
        "radio", "fm", "am", "online", "live", "stream",
        "the", "and", "or", "de", "la", "le", "les",
    })

    @staticmethod
    def normalize(text: str) -> str:
        text = re.sub(r"[^\w\s]", " ", text.lower().strip())
        words = [w for w in text.split()
                 if w not in SearchHelper._STOP and len(w) > 1]
        return " ".join(words) or text.lower().strip()

    @staticmethod
    def keywords(text: str) -> List[str]:
        words = re.findall(r"\w+", text.lower())
        return list({w for w in words
                     if len(w) > 2 and w not in SearchHelper._STOP})

    @staticmethod
    def score(station_name: str, query: str) -> float:
        """Return [0.0 – 1.0] relevance score."""
        s = station_name.lower()
        q = query.lower()
        sn = SearchHelper.normalize(s)
        qn = SearchHelper.normalize(q)

        # Exact / prefix / substring on normalised strings
        if qn == sn:     return 1.00
        if sn.startswith(qn): return 0.92
        if qn in sn:     return 0.88

        # Keyword coverage
        kw = SearchHelper.keywords(q)
        kw_score = 0.0
        if kw:
            hits = sum(1 for k in kw if k in s)
            kw_score = (hits / len(kw)) * 0.78

        # Difflib similarity on normalised strings
        sim = SequenceMatcher(None, qn, sn).ratio() * 0.68

        return max(0.0, kw_score, sim)

    @staticmethod
    def rank(stations: List[Dict], query: str) -> List[Dict]:
        """Sort in-place by relevance score DESC, then votes + clickcount DESC."""
        for s in stations:
            if "_relevance" not in s or s["_relevance"] == 0.0:
                s["_relevance"] = SearchHelper.score(s.get("name", ""), query)
        stations.sort(
            key=lambda x: (
                x["_relevance"],
                x.get("votes", 0) * 0.5 + x.get("clickcount", 0) * 0.05,
            ),
            reverse=True,
        )
        return stations


# ══════════════════════════════════════════════════════════════════════════════
# TWO-TIER CACHE
# Memory (dict, 10 min TTL) → Disk (JSON, 24 h TTL, MD5-keyed filenames)
# Thread-safe via a single RLock.
# ══════════════════════════════════════════════════════════════════════════════

class RadioCache:
    def __init__(
        self,
        cache_dir: Path = _CACHE_DIR,
        disk_ttl: int = _CACHE_TTL,
        mem_ttl: int = _MEM_TTL,
    ):
        self._dir      = cache_dir
        self._disk_ttl = disk_ttl
        self._mem_ttl  = mem_ttl
        self._mem: Dict[str, Tuple[float, Any]] = {}
        self._lock     = threading.RLock()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # read-only FS — disk cache silently disabled

    # ── internal ────────────────────────────────────────────────────────────

    def _path(self, key: str) -> Path:
        h = hashlib.md5(key.encode()).hexdigest()
        return self._dir / f"{h}.json"

    # ── public API ───────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            if key in self._mem:
                ts, data = self._mem[key]
                if now - ts < self._mem_ttl:
                    return data
                del self._mem[key]

        p = self._path(key)
        try:
            if p.exists() and (now - p.stat().st_mtime < self._disk_ttl):
                with open(p, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                with self._lock:
                    self._mem[key] = (now, data)
                return data
        except Exception:
            pass
        return None

    def set(self, key: str, data: Any) -> None:
        now = time.time()
        with self._lock:
            self._mem[key] = (now, data)
        p = self._path(key)
        try:
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
        except Exception:
            pass  # disk unavailable — memory cache still works

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._mem.pop(key, None)
        p = self._path(key)
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass

    def clear_all(self) -> None:
        """Wipe entire cache (memory + disk). Used in tests / admin."""
        with self._lock:
            self._mem.clear()
        try:
            for f in self._dir.glob("*.json"):
                f.unlink(missing_ok=True)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# RADIO BROWSER CLIENT
# Implements the full RadioBrowser REST API surface needed by the addon.
# Multi-server failover from Radio-Reveil + multi-strategy search from
# RADIO_FINDER_4_1 + two-tier caching.
# ══════════════════════════════════════════════════════════════════════════════

class RadioBrowserClient:
    """Queries radio-browser.info with multi-server failover and two-tier caching."""

    def __init__(self, cache: RadioCache):
        self._cache   = cache
        self._lock    = threading.Lock()
        self._session = self._make_session()

    # ── setup ────────────────────────────────────────────────────────────────

    @staticmethod
    def _make_session() -> Optional[Any]:
        if not _HAS_REQUESTS:
            return None
        s = _requests.Session()
        s.headers.update({"User-Agent": _UA, "Accept": "application/json"})
        return s

    # ── raw request with server failover ────────────────────────────────────

    def _request(self, endpoint: str, params: Dict = None) -> List[Dict]:
        if not self._session:
            return []
        params = params or {}
        cache_key = f"rb:{endpoint}:{json.dumps(params, sort_keys=True)}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        for server in RADIO_BROWSER_SERVERS:
            try:
                url  = f"{server}/json/{endpoint}"
                resp = self._session.get(url, params=params, timeout=_REQ_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and data:
                    self._cache.set(cache_key, data)
                    return data
            except Exception:
                continue
        return []

    # ── normalisation ────────────────────────────────────────────────────────

    @staticmethod
    def _normalize(raw: Dict) -> Dict:
        url = (raw.get("url_resolved") or raw.get("url") or "").strip()
        return {
            "name":        raw.get("name", "").strip(),
            "url":         url,
            "url_resolved":raw.get("url_resolved", "").strip() or url,
            "logo":        raw.get("favicon", "").strip(),
            "bitrate":     raw.get("bitrate", 0),
            "codec":       raw.get("codec", ""),
            "countrycode": raw.get("countrycode", "").upper(),
            "country":     raw.get("country", ""),
            "language":    raw.get("language", ""),
            "tags":        raw.get("tags", ""),
            "votes":       raw.get("votes", 0),
            "clickcount":  raw.get("clickcount", 0),
            "stationuuid": raw.get("stationuuid", ""),
            "lastcheckok": raw.get("lastcheckok", 0),
            "source":      "radiobrowser",
            "_relevance":  0.0,
        }

    @staticmethod
    def _valid(st: Dict) -> bool:
        url = st.get("url_resolved") or st.get("url", "")
        return bool(url.startswith(("http://", "https://"))) and st.get("lastcheckok") == 1

    # ── public API ───────────────────────────────────────────────────────────

    def search(
        self,
        query:    str,
        country:  Optional[str] = None,
        language: Optional[str] = None,
        tag:      Optional[str] = None,
        limit:    int = 60,
    ) -> List[Dict]:
        """
        Multi-strategy search:
          byname   on the normalised query
          search   (full-text) on the normalised query
          byname   on each extracted keyword (up to 2 extra terms)
          bytag    on each extracted keyword (music genre discovery)
          bytag    on explicit tag param when provided
        Deduplicates by stationuuid (or URL if no uuid), relevance-ranks,
        then trims to `limit`.
        """
        q_norm = SearchHelper.normalize(query)
        kws    = SearchHelper.keywords(query)

        base: Dict[str, Any] = {
            "hidebroken": "true",
            "limit":      min(limit * 2, 120),
            "order":      "clickcount",
            "reverse":    "true",
        }
        if country:
            base["countrycode"] = country.upper()
        if language:
            base["language"] = language.lower()

        # (endpoint_path, extra_params)
        strategies = [
            ("stations/byname", {"name": q_norm}),
            ("stations/search", {"name": q_norm}),
        ]
        for kw in kws[:2]:
            strategies.append(("stations/byname", {"name": kw}))
            strategies.append(("stations/bytag",  {"tag":  kw}))
        if tag:
            strategies.append(("stations/bytag", {"tag": tag}))

        seen: set  = set()
        raw_results: List[Dict] = []

        for path, extra in strategies:
            for st in self._request(path, {**base, **extra}):
                if not self._valid(st):
                    continue
                key = st.get("stationuuid") or (st.get("url_resolved") or st.get("url", ""))
                if key in seen:
                    continue
                seen.add(key)
                raw_results.append(self._normalize(st))

        # Relevance-rank, then deduplicate by case-folded name
        ranked     = SearchHelper.rank(raw_results, query)
        seen_names: set = set()
        unique: List[Dict] = []
        for s in ranked:
            k = s["name"].lower().strip()
            if k not in seen_names:
                seen_names.add(k)
                unique.append(s)
            if len(unique) >= limit:
                break
        return unique

    def by_country(self, country_code: str, limit: int = 200) -> List[Dict]:
        data = self._request(
            f"stations/bycountrycodeexact/{country_code.upper()}",
            {"limit": limit, "hidebroken": "true", "order": "clickcount", "reverse": "true"},
        )
        return [self._normalize(s) for s in data if self._valid(s)]

    def by_tag(self, tag: str, limit: int = 200) -> List[Dict]:
        data = self._request(
            f"stations/bytag/{urllib.parse.quote(tag)}",
            {"limit": limit, "hidebroken": "true", "order": "votes", "reverse": "true"},
        )
        return [self._normalize(s) for s in data if self._valid(s)]

    def top_stations(self, limit: int = 100) -> List[Dict]:
        data = self._request(
            "stations",
            {"limit": limit, "hidebroken": "true", "order": "clickcount", "reverse": "true"},
        )
        return [self._normalize(s) for s in data if self._valid(s)]

    def countries(self) -> List[Dict]:
        """Country list with station counts; cached 24 h."""
        return self._request(
            "countries",
            {"order": "stationcount", "reverse": "true"},
        )

    def genres(self, limit: int = 80) -> List[Dict]:
        """Popular genre tags with station counts."""
        return self._request(
            "tags",
            {"limit": limit, "order": "stationcount", "reverse": "true", "hidebroken": "true"},
        )


# ══════════════════════════════════════════════════════════════════════════════
# SHOUTCAST CLIENT
# Scrapes the public Shoutcast CSV directory (k=radiobrowser is a public key
# granted to the radio-browser.info project). Used as a secondary source when
# RadioBrowser returns no results, or directly via /api/radio/shoutcast.
# ══════════════════════════════════════════════════════════════════════════════

class ShoutcastClient:
    _URL = "https://directory.shoutcast.com/Search/CSVSearch"

    def search(self, query: str = "music", limit: int = 100) -> List[Dict]:
        if not _HAS_REQUESTS:
            return []
        try:
            resp = _requests.get(
                self._URL,
                params={"query": query or "music", "k": "radiobrowser"},
                headers={"User-Agent": _UA, "Referer": "https://directory.shoutcast.com/"},
                timeout=_REQ_TIMEOUT,
            )
            if resp.status_code != 200:
                return []
            stations: List[Dict] = []
            # CSV: ID,Name,Genre,Listeners,Bitrate,Type,StreamURL
            for line in resp.text.strip().splitlines()[1:]:
                parts = line.split(",", 6)
                if len(parts) < 7:
                    continue
                _, name, genre, _, bitrate, _, stream_url = parts
                name       = name.strip().strip('"')
                stream_url = stream_url.strip().strip('"')
                genre      = genre.strip().strip('"') or query or "uncategorized"
                try:
                    br = int(bitrate.strip()) if bitrate.strip() else 0
                except ValueError:
                    br = 0
                if name and stream_url.startswith(("http://", "https://")):
                    stations.append({
                        "name":        name,
                        "url":         stream_url,
                        "url_resolved":stream_url,
                        "logo":        "",
                        "bitrate":     br,
                        "countrycode": "",
                        "tags":        genre,
                        "votes":       0,
                        "clickcount":  0,
                        "source":      "shoutcast",
                        "_relevance":  0.0,
                    })
                if len(stations) >= limit:
                    break
            return stations
        except Exception:
            return []


# ══════════════════════════════════════════════════════════════════════════════
# M3U LOADER
# Delegates to core.m3u_parser when available; falls back to a minimal
# built-in parser that handles standard M3U / M3U8 #EXTINF format.
# ══════════════════════════════════════════════════════════════════════════════

_EXTINF_RE = re.compile(r"#EXTINF\s*:\s*-?\d+\s*(.*?),\s*(.*)", re.DOTALL)
# Matches: attr-name="value with spaces" or attr-name='value' or attr-name=bare
_ATTR_RE   = re.compile(r'([\w-]+?)\s*=\s*(?:"([^"]*)"' + r"|'([^']*)'|([^\s,>]+))")


class RadioM3ULoader:
    def __init__(self, cache: RadioCache):
        self._cache = cache

    def sources(self) -> Dict[str, str]:
        return dict(M3U_SOURCES)

    def load(
        self,
        name:        str,
        progress_cb  = None,
        proxy_dict   = None,
    ) -> List[Dict]:
        url = M3U_SOURCES.get(name)
        if not url:
            return []

        cache_key = f"m3u:{name}"
        cached    = self._cache.get(cache_key)
        if cached is not None:
            if progress_cb:
                progress_cb(f"Cache: {len(cached)} stations from {name}")
            return cached

        # Try the Flasky core parser first (preserves group, logo, tvg-id)
        try:
            from core.m3u_parser import M3UParser
            if progress_cb:
                progress_cb(f"Downloading {name}…")
            channels  = M3UParser().parse_url(url, timeout=40, proxy_dict=proxy_dict)
            stations  = [_channel_to_dict(ch, name) for ch in channels]
            if stations:
                self._cache.set(cache_key, stations)
            if progress_cb:
                progress_cb(f"{len(stations)} stations from {name}")
            return stations
        except ImportError:
            pass

        # Fallback: built-in minimal M3U parser
        return self._fetch_and_parse(url, name, progress_cb, proxy_dict)

    def _fetch_and_parse(
        self,
        url:         str,
        source_name: str,
        progress_cb  = None,
        proxy_dict   = None,
    ) -> List[Dict]:
        if not _HAS_REQUESTS:
            return []
        try:
            if progress_cb:
                progress_cb(f"Downloading {source_name}…")
            resp = _requests.get(
                url,
                timeout=40,
                headers={"User-Agent": _UA},
                proxies=proxy_dict,
            )
            resp.raise_for_status()
            stations = _parse_m3u_text(resp.text, source_name)
            if stations:
                self._cache.set(f"m3u:{source_name}", stations)
            if progress_cb:
                progress_cb(f"{len(stations)} stations from {source_name}")
            return stations
        except Exception as exc:
            if progress_cb:
                progress_cb(f"Error loading {source_name}: {exc}")
            return []


# ── M3U helpers (module-level so tests can call them directly) ───────────────

def _parse_m3u_text(text: str, source: str) -> List[Dict]:
    """Parse raw M3U / M3U8 text into station dicts."""
    stations: List[Dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            m    = _EXTINF_RE.match(line)
            attrs_str    = m.group(1) if m else ""
            display_name = (m.group(2) or "").strip() if m else ""

            # Build attrs dict: take first non-empty group (double-quote, single-quote, bare)
            attrs: Dict[str, str] = {}
            for match in _ATTR_RE.finditer(attrs_str):
                key = match.group(1)
                val = match.group(2) if match.group(2) is not None else \
                      match.group(3) if match.group(3) is not None else \
                      match.group(4) or ""
                attrs[key] = val

            # Find next non-comment, non-empty line as URL
            url = ""
            j   = i + 1
            while j < len(lines):
                candidate = lines[j].strip()
                if candidate and not candidate.startswith("#"):
                    url = candidate
                    i   = j
                    break
                j += 1

            if url.startswith(("http://", "https://")):
                name = (
                    attrs.get("tvg-name")
                    or attrs.get("tvg_name")
                    or display_name
                    or "Unknown"
                ).strip()
                stations.append({
                    "name":        name,
                    "url":         url,
                    "url_resolved":url,
                    "logo":        attrs.get("tvg-logo", attrs.get("tvg_logo", "")),
                    "countrycode": attrs.get("tvg-country", attrs.get("tvg_country", "")),
                    "tags":        attrs.get("group-title", attrs.get("group_title", "")),
                    "bitrate":     0,
                    "votes":       0,
                    "clickcount":  0,
                    "source":      source,
                    "_relevance":  0.0,
                })
        i += 1
    return stations


def _channel_to_dict(ch: Any, source: str) -> Dict:
    """Convert a core.m3u_parser.Channel to our station dict."""
    url = getattr(ch, "url", "") or ""
    return {
        "name":        getattr(ch, "name", "") or "",
        "url":         url,
        "url_resolved":url,
        "logo":        getattr(ch, "logo", "") or "",
        "countrycode": "",
        "tags":        getattr(ch, "group", "") or "",
        "bitrate":     0,
        "votes":       0,
        "clickcount":  0,
        "source":      source,
        "_relevance":  0.0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# FAVORITES MANAGER
# JSON-backed file, thread-safe. Keyed by stationuuid when present, else URL.
# ══════════════════════════════════════════════════════════════════════════════

class RadioFavorites:
    def __init__(self, path: Optional[Path] = None):
        self._path  = path or (_CACHE_DIR / "radio_favorites.json")
        self._lock  = threading.Lock()
        self._items: List[Dict] = self._load()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _load(self) -> List[Dict]:
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                pass
        return []

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(self._items, fh, indent=2, ensure_ascii=False)
        except Exception:
            pass

    @staticmethod
    def _id(station: Dict) -> str:
        return station.get("stationuuid") or station.get("url", "")

    def list(self) -> List[Dict]:
        with self._lock:
            return list(self._items)

    def add(self, station: Dict) -> bool:
        sid = self._id(station)
        if not sid:
            return False
        with self._lock:
            if any(self._id(s) == sid for s in self._items):
                return False  # already present
            self._items.append(station)
            self._save()
        return True

    def remove(self, identifier: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [s for s in self._items if self._id(s) != identifier]
            changed = len(self._items) < before
            if changed:
                self._save()
        return changed

    def contains(self, station: Dict) -> bool:
        sid = self._id(station)
        with self._lock:
            return any(self._id(s) == sid for s in self._items)


# ══════════════════════════════════════════════════════════════════════════════
# STREAM VERIFIER
# Quick HEAD (or minimal GET) to check whether a stream URL is alive.
# Also reads ICY metadata headers to fill in name / genre / bitrate.
# ══════════════════════════════════════════════════════════════════════════════

class RadioStreamVerifier:
    def verify(self, url: str, timeout: int = 6) -> Dict:
        if not _HAS_REQUESTS:
            return {"ok": False, "error": "requests library not installed"}
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "invalid URL scheme"}
        try:
            t0   = time.time()
            resp = _requests.head(
                url,
                timeout=timeout,
                allow_redirects=True,
                headers={"User-Agent": _UA, "Icy-MetaData": "1"},
                stream=True,
            )
            latency = int((time.time() - t0) * 1000)
            ok      = resp.status_code in (200, 206)
            # Some ICY servers reply 200 to HEAD, others refuse HEAD → do a
            # minimal GET to at least get the headers
            if not ok and resp.status_code in (400, 405):
                resp = _requests.get(
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                    headers={"User-Agent": _UA, "Icy-MetaData": "1"},
                    stream=True,
                )
                ok = resp.status_code in (200, 206)
            h = resp.headers
            return {
                "ok":          ok,
                "status":      resp.status_code,
                "latency_ms":  latency,
                "content_type":h.get("Content-Type", ""),
                "icy_name":    h.get("icy-name", ""),
                "icy_genre":   h.get("icy-genre", ""),
                "icy_br":      h.get("icy-br", ""),
                "icy_url":     h.get("icy-url", ""),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL SINGLETONS (lazy, thread-safe init)
# ══════════════════════════════════════════════════════════════════════════════

_singleton_lock = threading.Lock()
_cache:    Optional[RadioCache]          = None
_rb:       Optional[RadioBrowserClient]  = None
_sc:       Optional[ShoutcastClient]     = None
_m3u:      Optional[RadioM3ULoader]      = None
_favs:     Optional[RadioFavorites]      = None
_verifier: Optional[RadioStreamVerifier] = None


def _instances():
    global _cache, _rb, _sc, _m3u, _favs, _verifier
    with _singleton_lock:
        if _cache is None:
            _cache    = RadioCache()
            _rb       = RadioBrowserClient(_cache)
            _sc       = ShoutcastClient()
            _m3u      = RadioM3ULoader(_cache)
            _favs     = RadioFavorites()
            _verifier = RadioStreamVerifier()
    return _cache, _rb, _sc, _m3u, _favs, _verifier


# ══════════════════════════════════════════════════════════════════════════════
# FLASK BLUEPRINT
# ══════════════════════════════════════════════════════════════════════════════

def register_radio_addon(app: Any) -> Any:
    """
    Mount all /api/radio/* routes onto the provided Flask app.
    Call once at app startup:
        from radio_addon import register_radio_addon
        register_radio_addon(app)
    """
    try:
        from flask import request, jsonify
    except ImportError as exc:
        raise RuntimeError("Flask is required to register radio_addon routes") from exc

    cache, rb, sc, m3u_ldr, favs, verifier = _instances()

    # ── /api/radio/status ─────────────────────────────────────────────────────

    @app.route("/api/radio/status")
    def radio_status():
        return jsonify({
            "status":          "ok",
            "builtin_count":   len(BUILTIN_STATIONS),
            "favorites_count": len(favs.list()),
            "sources":         list(M3U_SOURCES.keys()),
            "rb_servers":      RADIO_BROWSER_SERVERS,
        })

    # ── /api/radio/builtin ───────────────────────────────────────────────────
    # Always instant (no network). Optional ?country=IT&tag=pop filters.

    @app.route("/api/radio/builtin")
    def radio_builtin():
        country = request.args.get("country", "").upper()
        tag_f   = request.args.get("tag",     "").lower()
        data    = list(BUILTIN_STATIONS)
        if country:
            data = [s for s in data if s.get("countrycode", "").upper() == country]
        if tag_f:
            data = [s for s in data if tag_f in s.get("tags", "").lower()]
        return jsonify({"status": "ok", "data": data, "count": len(data)})

    # ── /api/radio/search ─────────────────────────────────────────────────────
    # ?q=query  [&country=XX] [&language=english] [&genre=jazz] [&limit=50]
    # RadioBrowser primary; Shoutcast fallback when RB returns nothing.

    @app.route("/api/radio/search")
    def radio_search():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify({"status": "error", "message": "q parameter required"}), 400
        country  = request.args.get("country",  None)
        language = request.args.get("language", None)
        genre    = request.args.get("genre",    None)
        limit    = _clamp(request.args.get("limit", 50), 1, 300)

        results = rb.search(q, country=country, language=language, tag=genre, limit=limit)

        if not results:
            # Fallback: Shoutcast directory with relevance-ranked results
            results = sc.search(q, limit=100)
            results = SearchHelper.rank(results, q)

        _mark_favorites(results, favs)
        return jsonify({
            "status": "ok",
            "query":  q,
            "data":   results[:limit],
            "count":  len(results[:limit]),
        })

    # ── /api/radio/top ────────────────────────────────────────────────────────
    # ?limit=100

    @app.route("/api/radio/top")
    def radio_top():
        limit   = _clamp(request.args.get("limit", 100), 1, 500)
        results = rb.top_stations(limit)
        _mark_favorites(results, favs)
        return jsonify({"status": "ok", "data": results, "count": len(results)})

    # ── /api/radio/country/<cc> ───────────────────────────────────────────────
    # ?limit=200

    @app.route("/api/radio/country/<cc>")
    def radio_country(cc: str):
        limit   = _clamp(request.args.get("limit", 200), 1, 500)
        results = rb.by_country(cc, limit=limit)
        _mark_favorites(results, favs)
        return jsonify({
            "status":      "ok",
            "countrycode": cc.upper(),
            "data":        results,
            "count":       len(results),
        })

    # ── /api/radio/genre/<tag> ────────────────────────────────────────────────

    @app.route("/api/radio/genre/<tag>")
    def radio_genre(tag: str):
        limit   = _clamp(request.args.get("limit", 200), 1, 500)
        results = rb.by_tag(tag, limit=limit)
        _mark_favorites(results, favs)
        return jsonify({"status": "ok", "tag": tag, "data": results, "count": len(results)})

    # ── /api/radio/countries ──────────────────────────────────────────────────

    @app.route("/api/radio/countries")
    def radio_countries():
        data = rb.countries()
        return jsonify({"status": "ok", "data": data, "count": len(data)})

    # ── /api/radio/genres ─────────────────────────────────────────────────────

    @app.route("/api/radio/genres")
    def radio_genres():
        limit = _clamp(request.args.get("limit", 80), 1, 300)
        data  = rb.genres(limit=limit)
        return jsonify({"status": "ok", "data": data, "count": len(data)})

    # ── /api/radio/shoutcast ──────────────────────────────────────────────────
    # Explicit Shoutcast directory search (distinct from RB search fallback).
    # ?q=jazz  [&limit=100]

    @app.route("/api/radio/shoutcast")
    def radio_shoutcast():
        q       = request.args.get("q", "music").strip()
        limit   = _clamp(request.args.get("limit", 100), 1, 300)
        results = sc.search(q, limit=limit)
        results = SearchHelper.rank(results, q)
        return jsonify({"status": "ok", "query": q, "data": results[:limit], "count": len(results[:limit])})

    # ── /api/radio/sources ────────────────────────────────────────────────────

    @app.route("/api/radio/sources")
    def radio_sources():
        return jsonify({"status": "ok", "sources": list(M3U_SOURCES.keys())})

    # ── /api/radio/load_source  POST {source: "name"} ─────────────────────────
    # Blocks until load completes (max 45 s) — M3U hits disk cache on repeat
    # calls so only the first call per 24 h is slow.

    @app.route("/api/radio/load_source", methods=["POST"])
    def radio_load_source():
        body = request.get_json(force=True, silent=True) or {}
        name = body.get("source", "").strip()
        if not name or name not in M3U_SOURCES:
            return jsonify({
                "status":  "error",
                "message": f"Unknown source '{name}'. "
                           f"Available: {list(M3U_SOURCES.keys())}",
            }), 400

        holder: Dict = {}

        def _load():
            holder["data"] = m3u_ldr.load(name)

        t = threading.Thread(target=_load, daemon=True)
        t.start()
        t.join(timeout=45)

        stations = holder.get("data", [])
        return jsonify({
            "status": "ok",
            "source": name,
            "data":   stations,
            "count":  len(stations),
        })

    # ── /api/radio/favorites  GET / POST ─────────────────────────────────────
    # POST body: any station dict (needs at least name + url)

    @app.route("/api/radio/favorites", methods=["GET"])
    def radio_favorites_get():
        data = favs.list()
        return jsonify({"status": "ok", "data": data, "count": len(data)})

    @app.route("/api/radio/favorites", methods=["POST"])
    def radio_favorites_add():
        station = request.get_json(force=True, silent=True) or {}
        if not station.get("name") or not station.get("url"):
            return jsonify({"status": "error", "message": "name and url are required"}), 400
        added = favs.add(station)
        return jsonify({"status": "ok", "added": added})

    # ── /api/radio/favorites/<id>  DELETE ────────────────────────────────────
    # <id> is stationuuid or URL-encoded stream URL

    @app.route("/api/radio/favorites/<path:identifier>", methods=["DELETE"])
    def radio_favorites_remove(identifier: str):
        removed = favs.remove(identifier)
        return jsonify({"status": "ok", "removed": removed})

    # ── /api/radio/verify  GET ?url=... ──────────────────────────────────────

    @app.route("/api/radio/verify")
    def radio_verify():
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"status": "error", "message": "url parameter required"}), 400
        result = verifier.verify(url)
        return jsonify({"status": "ok", "result": result})

    # ── /api/radio/cache/clear  POST ─────────────────────────────────────────
    # Admin: purge all cached radio data (forces fresh API calls)

    @app.route("/api/radio/cache/clear", methods=["POST"])
    def radio_cache_clear():
        cache.clear_all()
        return jsonify({"status": "ok", "message": "Radio cache cleared"})

    return app


# ══════════════════════════════════════════════════════════════════════════════
# PRIVATE UTILS
# ══════════════════════════════════════════════════════════════════════════════

def _clamp(value: Any, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


def _mark_favorites(stations: List[Dict], favs: RadioFavorites) -> None:
    """Annotate each station dict with _is_favorite (in-place, best-effort)."""
    for s in stations:
        try:
            s["_is_favorite"] = favs.contains(s)
        except Exception:
            s["_is_favorite"] = False
