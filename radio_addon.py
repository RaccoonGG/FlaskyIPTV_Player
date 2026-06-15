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

from flask import Response

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
# NOTE (June 2026): a former "🌍 iptv-org Radio (all — ~20k stations)" entry
# pointing at https://iptv-org.github.io/iptv/index.radio.m3u was removed.
# That path 404s and is NOT part of iptv-org/iptv's published playlist set —
# verified against their PLAYLISTS.md, which lists category/language/country
# playlists but has no "Radio" category and no index.radio.m3u anywhere.
# RadioBrowser (the #1 source above, ~35k stations) already covers this.
M3U_SOURCES: Dict[str, str] = {
    "🎵 iptv-org Music":
        "https://iptv-org.github.io/iptv/categories/music.m3u",
    "📰 iptv-org News":
        "https://iptv-org.github.io/iptv/categories/news.m3u",
    "🇮🇹 Italia (Tundrak — daily)":
        "https://cdn.jsdelivr.net/gh/Tundrak/IPTV-Italia@main/iptvitaplus.m3u",
    "🇮🇹 Italia (Free-TV)":
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_italy.m3u8",
    "🇬🇧 UK (Free-TV)":
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_uk.m3u8",
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

    def trending(self, limit: int = 50) -> List[Dict]:
        """Stations clicked recently — 'what's hot right now'.

        Distinct from top_stations() which reflects all-time click totals.
        Uses RadioBrowser's `stations/lastclick` endpoint (NOT
        `stations/clicked`, which does not exist and always returns
        nothing — see https://docs.radio-browser.info/#stations-by-recent-clicks).
        """
        data = self._request(
            "stations/lastclick",
            {"limit": limit, "hidebroken": "true"},
        )
        return [self._normalize(s) for s in data if self._valid(s)]

    def nearby(
        self,
        lat:         float,
        lng:         float,
        distance_km: int = 200,
        limit:       int = 100,
    ) -> List[Dict]:
        """Stations within `distance_km` km of the given coordinates.

        Uses RadioBrowser's geo_lat / geo_long / geo_distance parameters.
        Note: only stations that have geographic coordinates in the database
        are returned; smaller local stations may lack geo data.
        """
        data = self._request(
            "stations",
            {
                "geo_lat":      str(lat),
                "geo_long":     str(lng),
                "geo_distance": str(distance_km),
                "limit":        limit,
                "hidebroken":   "true",
                "order":        "clickcount",
                "reverse":      "true",
            },
        )
        return [self._normalize(s) for s in data if self._valid(s)]

    def register_click(self, stationuuid: str) -> Optional[str]:
        """Register a play click with RadioBrowser (API citizenship).

        RadioBrowser asks clients to call /json/url/{uuid} when playback
        starts. This records the click in their statistics (improves the
        clickcount ranking we rely on) and returns the freshest resolved
        stream URL — useful when CDN tokens rotate.

        NOT cached — always makes a live request so the URL is genuinely
        fresh. Returns the resolved URL string, or None on failure.
        """
        if not self._session or not stationuuid:
            return None
        for server in RADIO_BROWSER_SERVERS:
            try:
                url  = f"{server}/json/url/{stationuuid}"
                resp = self._session.get(url, timeout=4)
                if resp.ok:
                    data     = resp.json()
                    resolved = (data.get("url") or "").strip()
                    if resolved.startswith(("http://", "https://")):
                        return resolved
            except Exception:
                continue
        return None


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
                timeout=60,
                headers={
                    # Use a browser UA — GitHub Pages (iptv-org.github.io) returns 403
                    # for non-browser User-Agent strings.
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept":          "text/plain, application/x-mpegurl, */*",
                    "Accept-Encoding": "gzip, deflate",
                },
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
        """Remove station whose stationuuid OR url matches identifier."""
        with self._lock:
            before = len(self._items)
            self._items = [
                s for s in self._items
                if self._id(s) != identifier and s.get("url", "") != identifier
            ]
            changed = len(self._items) < before
            if changed:
                self._save()
        return changed

    def contains(self, station: Dict) -> bool:
        sid = self._id(station)
        with self._lock:
            return any(self._id(s) == sid for s in self._items)


# ══════════════════════════════════════════════════════════════════════════════
# RECENTLY PLAYED HISTORY
# Capped ring-buffer (last 50 plays), newest-first, JSON-backed.
# Duplicate URLs are moved to the front rather than duplicated.
# ══════════════════════════════════════════════════════════════════════════════

class RadioHistory:
    """Recently played stations, newest first, capped at MAX_ENTRIES.

    Each entry is a station dict with an added ``_played_at`` ISO-8601 UTC
    timestamp.  Keyed by URL so duplicate plays move the entry to the front
    rather than adding a second copy.
    """

    MAX_ENTRIES = 50

    def __init__(self, path: Optional[Path] = None):
        self._path  = path or (_CACHE_DIR / "radio_history.json")
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

    def push(self, station: Dict) -> None:
        """Prepend station to history; remove any existing entry for same URL."""
        url = (station.get("url") or "").strip()
        if not url:
            return
        entry = dict(station)
        entry["_played_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            self._items = [s for s in self._items if s.get("url") != url]
            self._items.insert(0, entry)
            self._items = self._items[: self.MAX_ENTRIES]
            self._save()

    def list(self) -> List[Dict]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._save()


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
# NOW PLAYING — ICY STREAM METADATA READER
# Reads the dynamic StreamTitle embedded in the audio stream bytes.
# The static ICY headers (icy-name, icy-genre) never change; StreamTitle
# updates with every track change.  Results are cached per URL for
# _NOWPLAYING_TTL seconds so repeated polls don't hammer streams.
# ══════════════════════════════════════════════════════════════════════════════

_NOWPLAYING_TTL  = 20       # seconds between live re-reads per URL
_NOWPLAYING_READ = 300_000  # max stream bytes read before giving up (300 KB)


class RadioNowPlaying:
    """Read ICY StreamTitle (current track) from a live audio stream.

    ICY protocol overview:
      1. Request stream with ``Icy-MetaData: 1`` header.
      2. Server includes ``icy-metaint: N`` in response headers.
      3. Stream layout: [N audio bytes] [1 byte = meta_len / 16]
         [meta_len bytes of metadata] [N audio bytes] …
      4. Metadata: ``StreamTitle='Artist - Track Title';StreamUrl='…';``
      5. If meta_len == 0 the block is empty — track hasn't changed.

    We read until we've seen the first metadata block (or MAX_READ bytes).
    The connection is closed immediately after.  Results are cached for
    _NOWPLAYING_TTL seconds to avoid repeated stream connections.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[float, Dict]] = {}
        self._lock  = threading.Lock()

    def get(self, url: str, timeout: int = 8) -> Dict:
        """Return now-playing info for *url*, using a short per-URL cache."""
        now = time.time()
        with self._lock:
            if url in self._cache:
                ts, data = self._cache[url]
                if now - ts < _NOWPLAYING_TTL:
                    return {**data, "cached": True}

        result = self._fetch(url, timeout)
        with self._lock:
            self._cache[url] = (time.time(), result)
        return {**result, "cached": False}

    def _fetch(self, url: str, timeout: int) -> Dict:
        if not _HAS_REQUESTS:
            return {"ok": False, "error": "requests not installed", "stream_title": ""}
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "invalid URL scheme", "stream_title": ""}
        try:
            t0   = time.time()
            resp = _requests.get(
                url,
                headers={
                    # Winamp UA gets the best ICY compliance from Shoutcast/Icecast
                    "User-Agent":   "WinampMPEG/5.0",
                    "Icy-MetaData": "1",
                    "Connection":   "close",
                },
                stream=True,
                timeout=timeout,
                allow_redirects=True,
            )
            h       = resp.headers
            metaint = int(h.get("icy-metaint", 0))
            base    = {
                "ok":          resp.status_code in (200, 206),
                "status":      resp.status_code,
                "latency_ms":  int((time.time() - t0) * 1000),
                "icy_name":    h.get("icy-name",  ""),
                "icy_genre":   h.get("icy-genre", ""),
                "icy_br":      h.get("icy-br",    ""),
                "content_type":h.get("Content-Type", ""),
                "metaint":     metaint,
                "stream_title": "",
            }

            if not base["ok"] or metaint == 0:
                resp.close()
                return base

            # Read exactly enough bytes to reach the first metadata block
            buf = b""
            for chunk in resp.iter_content(chunk_size=8192):
                buf += chunk
                if len(buf) >= min(metaint + 513, _NOWPLAYING_READ):
                    break
            resp.close()

            base["stream_title"] = self._parse_stream_title(buf, metaint)
            return base

        except Exception as exc:
            return {"ok": False, "error": str(exc), "stream_title": ""}

    @staticmethod
    def _parse_stream_title(buf: bytes, metaint: int) -> str:
        """Extract StreamTitle from a raw ICY buffer at the given metaint offset."""
        if len(buf) <= metaint:
            return ""
        meta_len = buf[metaint] * 16
        if meta_len == 0:
            return ""   # empty block — track unchanged
        end = metaint + 1 + meta_len
        if len(buf) < end:
            return ""   # truncated buffer
        raw = buf[metaint + 1 : end].rstrip(b"\x00").decode("utf-8", errors="replace")
        m   = re.search(r"StreamTitle='([^']*)'", raw)
        return m.group(1).strip() if m else ""


# ══════════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL SINGLETONS (lazy, thread-safe init)
# ══════════════════════════════════════════════════════════════════════════════

_singleton_lock = threading.Lock()
_cache:      Optional[RadioCache]          = None
_rb:         Optional[RadioBrowserClient]  = None
_sc:         Optional[ShoutcastClient]     = None
_m3u:        Optional[RadioM3ULoader]      = None
_favs:       Optional[RadioFavorites]      = None
_verifier:   Optional[RadioStreamVerifier] = None
_history:    Optional[RadioHistory]        = None
_nowplaying: Optional[RadioNowPlaying]     = None


def _instances():
    global _cache, _rb, _sc, _m3u, _favs, _verifier, _history, _nowplaying
    with _singleton_lock:
        if _cache is None:
            _cache      = RadioCache()
            _rb         = RadioBrowserClient(_cache)
            _sc         = ShoutcastClient()
            _m3u        = RadioM3ULoader(_cache)
            _favs       = RadioFavorites()
            _verifier   = RadioStreamVerifier()
            _history    = RadioHistory()
            _nowplaying = RadioNowPlaying()
    return _cache, _rb, _sc, _m3u, _favs, _verifier, _history, _nowplaying


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

    cache, rb, sc, m3u_ldr, favs, verifier, history, nowplaying = _instances()

    # ── /api/radio/status ─────────────────────────────────────────────────────

    @app.route("/api/radio/status")
    def radio_status():
        return jsonify({
            "status":          "ok",
            "builtin_count":   len(BUILTIN_STATIONS),
            "favorites_count": len(favs.list()),
            "history_count":   len(history.list()),
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

    # ── /api/radio/load_source  POST {source: "name", limit?: 500} ───────────
    # Blocks until load completes (max 70 s) — M3U hits disk cache on repeat
    # calls so only the first call per 24 h is slow.
    # `limit` caps the returned list (default 500, max 5000) — large playlists
    # like the Free-TV per-country lists can still be truncated; `total` and
    # `truncated` fields tell the client how many stations exist overall.

    @app.route("/api/radio/load_source", methods=["POST"])
    def radio_load_source():
        body  = request.get_json(force=True, silent=True) or {}
        name  = body.get("source", "").strip()
        limit = _clamp(body.get("limit", 500), 1, 5000)
        if not name or name not in M3U_SOURCES:
            return jsonify({
                "status":  "error",
                "message": f"Unknown source '{name}'. "
                           f"Available: {list(M3U_SOURCES.keys())}",
            }), 400

        holder: Dict = {}

        def _load():
            msgs: List[str] = []
            holder["data"] = m3u_ldr.load(name, progress_cb=msgs.append)
            if msgs:
                holder["last_msg"] = msgs[-1]

        t = threading.Thread(target=_load, daemon=True)
        t.start()
        t.join(timeout=70)   # increased: large playlists on slow connections

        all_stations = holder.get("data") or []
        total        = len(all_stations)
        stations     = all_stations[:limit]

        note = _load_source_note(
            url            = M3U_SOURCES.get(name, ""),
            total          = total,
            limit          = limit,
            stations_shown = len(stations),
            last_msg       = holder.get("last_msg", ""),
            still_running  = t.is_alive(),
        )

        _mark_favorites(stations, favs)
        return jsonify({
            "status":    "ok",
            "source":    name,
            "data":      stations,
            "count":     len(stations),
            "total":     total,
            "truncated": total > limit,
            "note":      note,
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
        # Try URL-decoded first (clients may percent-encode the URL component),
        # fall back to the raw identifier for uuid-keyed entries.
        decoded = urllib.parse.unquote(identifier)
        removed = favs.remove(decoded)
        if not removed:
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

    # ══════════════════════════════════════════════════════════════════════════
    # NEW FEATURES
    # ══════════════════════════════════════════════════════════════════════════

    # ── /api/radio/nowplaying  GET ?url=... ───────────────────────────────────
    # Read the ICY StreamTitle (current track) from a live audio stream.
    # The result is cached per URL for _NOWPLAYING_TTL seconds so repeated
    # polls are instant.  On a cache miss the call may block up to `timeout`
    # seconds while reading stream bytes — the client should call this after
    # playback has already started, not on every page load.
    #
    # Response: {ok, stream_title, icy_name, icy_genre, icy_br,
    #            latency_ms, metaint, cached}
    #
    # Mobile UX: display only `stream_title` in the compact now-playing bar;
    # poll every 15–20 s while a station is active.

    @app.route("/api/radio/nowplaying")
    def radio_nowplaying():
        url = request.args.get("url", "").strip()
        if not url:
            return jsonify({"status": "error", "message": "url parameter required"}), 400
        result = nowplaying.get(url, timeout=8)
        return jsonify({"status": "ok", "result": result})

    # ── /api/radio/click  POST ────────────────────────────────────────────────
    # Called when the user starts playing a station.  Does two things:
    #   1. Pushes the station to the recently played history (local, instant).
    #   2. Registers a click with RadioBrowser (API citizenship, improves
    #      clickcount rankings we rely on for search ordering).
    #      Also returns the freshest resolved stream URL (CDN tokens etc).
    #
    # POST body: full station dict (name + url required; stationuuid optional)
    # Response:  {status, resolved_url}
    #
    # resolved_url is the RadioBrowser-resolved URL when a stationuuid is
    # present and RadioBrowser responds within 2.5 s; otherwise the original
    # url is echoed back unchanged.  The client should switch to resolved_url
    # if it differs.

    @app.route("/api/radio/click", methods=["POST"])
    def radio_click():
        station = request.get_json(force=True, silent=True) or {}
        url     = (station.get("url") or "").strip()
        uuid    = (station.get("stationuuid") or station.get("uuid") or "").strip()

        # 1. Always push to local history (instant, no network)
        if station.get("name") and url:
            history.push(station)

        # 2. Register click with RadioBrowser; try to get a fresh resolved URL
        resolved_url = url
        if uuid:
            holder: Dict = {}

            def _register():
                holder["url"] = rb.register_click(uuid)

            t = threading.Thread(target=_register, daemon=True)
            t.start()
            t.join(timeout=2.5)
            if holder.get("url"):
                resolved_url = holder["url"]

        return jsonify({"status": "ok", "resolved_url": resolved_url})

    # ── /api/radio/trending  GET ?limit=50 ───────────────────────────────────
    # Stations clicked in the last hour — what's popular right now.
    # Distinct from /api/radio/top which reflects all-time click totals.
    # Refreshes with every call (short RadioBrowser cache TTL).

    @app.route("/api/radio/trending")
    def radio_trending():
        limit   = _clamp(request.args.get("limit", 50), 1, 200)
        results = rb.trending(limit)
        _mark_favorites(results, favs)
        return jsonify({"status": "ok", "data": results, "count": len(results)})

    # ── /api/radio/history  GET / DELETE ─────────────────────────────────────
    # GET    → list of last 50 played stations, newest first.
    #          Each entry carries a `_played_at` ISO-8601 UTC timestamp.
    # DELETE → clear the entire history.
    #
    # Entries are added automatically by POST /api/radio/click; no separate
    # write endpoint is needed.
    #
    # Mobile UX: show name + formatted _played_at (e.g. "Today 14:32").

    @app.route("/api/radio/history", methods=["GET"])
    def radio_history_get():
        data = history.list()
        _mark_favorites(data, favs)
        return jsonify({"status": "ok", "data": data, "count": len(data)})

    @app.route("/api/radio/history", methods=["DELETE"])
    def radio_history_clear():
        history.clear()
        return jsonify({"status": "ok", "message": "History cleared"})

    # ── /api/radio/favorites/export.m3u  GET ─────────────────────────────────
    # Download favorites as a standard M3U playlist compatible with VLC,
    # Kodi, MPV, and any other M3U-capable player.
    # Triggers a file-save dialog on both desktop and mobile browsers.

    @app.route("/api/radio/favorites/export.m3u")
    def radio_favorites_export_m3u():
        try:
            from flask import Response
        except ImportError:
            return jsonify({"status": "error", "message": "Flask not available"}), 500

        stations = favs.list()
        lines    = ["#EXTM3U"]
        for s in stations:
            url  = (s.get("url") or "").strip()
            if not url:
                continue
            name  = (s.get("name") or "Unknown").replace(",", " ")
            logo  = s.get("logo",        "")
            tags  = s.get("tags",        "") or s.get("countrycode", "") or "Radio"
            attrs = f'tvg-logo="{logo}" group-title="{tags}"'
            lines.append(f"#EXTINF:-1 {attrs},{name}")
            lines.append(url)

        m3u_body = "\n".join(lines) + "\n"
        return Response(
            m3u_body,
            status=200,
            mimetype="audio/x-mpegurl",
            headers={
                "Content-Disposition": "attachment; filename=radio_favorites.m3u",
                "Content-Type":        "audio/x-mpegurl; charset=utf-8",
            },
        )

    # ── /api/radio/nearby  GET ?lat=...&lng=...&distance=200 ─────────────────
    # Stations within `distance` km of the given coordinates.
    # Intended to be called with coordinates from the browser Geolocation API:
    #
    #   navigator.geolocation.getCurrentPosition(pos => {
    #       fetch(`/api/radio/nearby?lat=${pos.coords.latitude}
    #                               &lng=${pos.coords.longitude}&distance=200`)
    #           .then(r => r.json()).then(d => renderStations(d.data));
    #   });
    #
    # Note: only stations with geo coordinates in the RadioBrowser database
    # are returned; smaller local stations may be absent.

    @app.route("/api/radio/nearby")
    def radio_nearby():
        try:
            lat      = float(request.args["lat"])
            lng      = float(request.args["lng"])
        except (KeyError, ValueError, TypeError):
            return jsonify({"status": "error",
                            "message": "lat and lng (decimal degrees) are required"}), 400
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            return jsonify({"status": "error",
                            "message": "lat must be −90…90, lng must be −180…180"}), 400

        distance = _clamp(request.args.get("distance", 200), 1, 5000)
        limit    = _clamp(request.args.get("limit",   100), 1, 500)
        results  = rb.nearby(lat, lng, distance_km=distance, limit=limit)
        _mark_favorites(results, favs)
        return jsonify({
            "status":      "ok",
            "lat":         lat,
            "lng":         lng,
            "distance_km": distance,
            "data":        results,
            "count":       len(results),
        })

    # ── Frontend: radio modal + visualizer UI ───────────────────────────────
    _register_radio_ui_route(app)

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


def _load_source_note(
    url:            str,
    total:          int,
    limit:          int,
    stations_shown: int,
    last_msg:       str = "",
    still_running:  bool = False,
) -> str:
    """Build the human-readable `note` field for /api/radio/load_source.

    Pure function (no I/O) so the diagnostic messaging can be unit-tested
    without spinning up threads or a Flask app.

    - `total > 0`            → normal "Showing X of Y" (or "" if not
                                truncated).
    - `still_running`        → background load exceeded the 70 s budget;
                                tell the user it may still finish & cache.
    - `last_msg` starts with
      "Error loading"         → the fallback parser captured a real
                                exception (status code, network error,
                                timeout, ...) — surface it verbatim rather
                                than guessing.
    - otherwise               → honest generic message. Does NOT assert a
                                specific HTTP status code we never actually
                                observed (e.g. previously this always
                                claimed "HTTP 403", even for a plain 404 or
                                a network timeout).
    """
    if total:
        return (f"Showing {stations_shown} of {total} stations."
                if total > limit else "")

    if still_running:
        return (
            f"Still loading {url} after 70 s — large playlists can "
            "take a while on a slow connection. Try again in a "
            "moment; once it finishes the result is cached for 24 h."
        )

    if last_msg.startswith("Error loading"):
        detail = last_msg.split(":", 1)[-1].strip()
        return (
            f"No stations parsed from {url}. {detail} "
            "Try again — on success the result is cached for 24 h."
        )

    return (
        f"No stations parsed from {url}. The source returned no "
        "usable entries — it may be temporarily unreachable, the "
        "playlist URL may have changed, or the host may be "
        "rate-limiting automated requests. Try again — on "
        "success the result is cached for 24 h."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Frontend  (served as /api/radio/ui.js)
# ─────────────────────────────────────────────────────────────────────────────

_RADIO_UI_JS_BYTES: bytes = b""   # filled in register_radio_addon


def _register_radio_ui_route(app) -> None:
    """Add the /api/radio/ui.js route and pre-encode the JS once."""
    global _RADIO_UI_JS_BYTES
    _RADIO_UI_JS_BYTES = _RADIO_UI_JS.encode("utf-8")

    @app.route("/api/radio/ui.js")
    def radio_ui_js():
        return Response(
            _RADIO_UI_JS_BYTES,
            content_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )


_RADIO_UI_JS = r"""
// ════════════════════════════════════════════════════════════════════════════
// RADIO MODAL  —  self-contained IIFE, uses global doPlay() and toast()
// ════════════════════════════════════════════════════════════════════════════
(function(){
'use strict';

// ── state ─────────────────────────────────────────────────────────────────
const _FAV_KEY = 'rdio_favs_v1';
let _curTab       = 'search';
let _favs         = [];
let _ctriesLoaded = false;
let _curRadioUrl  = '';      // URL of currently playing radio station
let _rdioNpTimer  = null;    // setInterval handle for now-playing polling
let _rdioLastNp   = '';      // last known StreamTitle from ICY stream

// ── open / close ──────────────────────────────────────────────────────────
window.radioOpen = function(){
  document.getElementById('radio-overlay').classList.add('open');
  document.getElementById('radio-open-btn').classList.add('active');
  _rdioVizSyncBtn();
  _favsLoad();
  if(!_ctriesLoaded) _loadCountryDropdown();
  // activate the current tab (re-entering keeps previous tab selected)
  const activeTab = document.querySelector('.rdio-tab.active');
  const tabName   = activeTab ? activeTab.dataset.tab : 'search';
  _activateTab(tabName, activeTab);
};

window.radioClose = function(){
  document.getElementById('radio-overlay').classList.remove('open');
  document.getElementById('radio-open-btn').classList.remove('active');
};

// ── tab switching ─────────────────────────────────────────────────────────
window.radioTab = function(btn, name){
  _curTab = name;
  document.querySelectorAll('.rdio-tab').forEach(b => b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  _activateTab(name, btn);
};

function _activateTab(name, btn){
  _curTab = name;
  const searchRow = document.getElementById('rdio-search-row');
  searchRow.style.display = (name === 'search') ? '' : 'none';
  switch(name){
    case 'search':    _setBody(_emptyHtml('🔍', 'Type a query and press Search')); break;
    case 'top':       _loadTop();       break;
    case 'builtin':   _loadBuiltin();   break;
    case 'country':   _loadCountryGrid(); break;
    case 'genre':     _loadGenreGrid(); break;
    case 'favorites': _renderFavs();    break;
    case 'sources':   _loadSources();   break;
    case 'trending':  _loadTrending();  break;
    case 'history':   _loadHistory();   break;
    case 'nearby':    _loadNearby();    break;
  }
}

// ── search ────────────────────────────────────────────────────────────────
window.radioSearch = async function(){
  const q  = (document.getElementById('rdio-q').value || '').trim();
  const cc = document.getElementById('rdio-country').value || '';
  if(!q){ if(typeof toast === 'function') toast('Enter a search query','w'); return; }
  _setBody(_loadingHtml());
  try{
    const p = new URLSearchParams({q, limit: 60});
    if(cc) p.set('country', cc);
    const d = await _api('/api/radio/search?' + p);
    _renderList(d.data || [], q);
  }catch(e){ _setBody(_emptyHtml('⚠️', 'Search failed: ' + _esc(e.message))); }
};

// ── top 100 ───────────────────────────────────────────────────────────────
async function _loadTop(){
  _setBody(_loadingHtml());
  try{
    const d = await _api('/api/radio/top?limit=100');
    _renderList(d.data || []);
  }catch(e){ _setBody(_emptyHtml('⚠️', _esc(e.message))); }
}

// ── builtin (instant, no network) ─────────────────────────────────────────
async function _loadBuiltin(){
  _setBody(_loadingHtml());
  try{
    const d = await _api('/api/radio/builtin');
    _renderList(d.data || []);
  }catch(e){ _setBody(_emptyHtml('⚠️', _esc(e.message))); }
}

// ── locale-aware country ordering ─────────────────────────────────────────
// Mirrors sortedCountryTags() in the main IIFE:
//   1. User's own country (ISO-2 from timezone / navigator.language)
//   2. US → CA → GB  (skip if already shown as local)
//   3. Everything else in original stationcount-desc order from RadioBrowser
const _RDIO_LOCAL_CC     = (window._rdioLocalCC || '').toUpperCase();
const _RDIO_PRIORITY_CC  = ['US', 'CA', 'GB'];   // mirrors PRIORITY_AFTER_LOCAL

function _sortCountries(list){
  // list: [{iso_3166_1, name, stationcount}, …] already stationcount-desc from API
  const byCC = {};
  for(const c of list){ byCC[(c.iso_3166_1||'').toUpperCase()] = c; }

  const used   = new Set();
  const result = [];

  // 1 — local
  if(_RDIO_LOCAL_CC && byCC[_RDIO_LOCAL_CC]){
    result.push(byCC[_RDIO_LOCAL_CC]);
    used.add(_RDIO_LOCAL_CC);
  }
  // 2 — US / CA / GB
  for(const cc of _RDIO_PRIORITY_CC){
    if(!used.has(cc) && byCC[cc]){ result.push(byCC[cc]); used.add(cc); }
  }
  // 3 — rest in original API order (stationcount desc)
  for(const c of list){
    const cc = (c.iso_3166_1||'').toUpperCase();
    if(!used.has(cc)){ result.push(c); used.add(cc); }
  }
  return result;
}

// ── country grid → stations ───────────────────────────────────────────────
async function _loadCountryGrid(){
  _setBody(_loadingHtml());
  try{
    const d  = await _api('/api/radio/countries');
    const raw = (d.data || []).filter(c => c.name && (c.stationcount||0) > 0).slice(0, 200);
    if(!raw.length){ _setBody(_emptyHtml('🌍','No country data available')); return; }

    const sorted = _sortCountries(raw);
    let h = '<div class="rdio-tag-grid">';
    for(const c of sorted){
      const cc    = (c.iso_3166_1 || c.name).toUpperCase();
      const label = _esc(c.name);
      const count = c.stationcount
        ? `<span style="font-size:9px;opacity:.45;margin-left:3px">${c.stationcount}</span>`
        : '';
      // Highlight local country
      const isLocal    = cc === _RDIO_LOCAL_CC;
      const isPriority = !isLocal && _RDIO_PRIORITY_CC.includes(cc);
      const extra = isLocal
        ? ' style="background:rgba(124,58,237,.22);color:var(--acc);border-color:rgba(124,58,237,.5);font-weight:700"'
        : isPriority
          ? ' style="border-color:rgba(255,255,255,.18)"'
          : '';
      h += `<button class="rdio-tag" onclick="_rdioByCountry('${_esc(cc)}','${label}')"${extra}>${label}${count}</button>`;
    }
    h += '</div>';
    _setBody(h);
  }catch(e){ _setBody(_emptyHtml('⚠️', _esc(e.message))); }
}

window._rdioByCountry = async function(cc, label){
  _setBody(_loadingHtml('Loading ' + _esc(label) + '…'));
  try{
    const d = await _api(`/api/radio/country/${encodeURIComponent(cc)}?limit=200`);
    _renderList(d.data || [], label, true);
  }catch(e){ _setBody(_emptyHtml('⚠️', _esc(e.message))); }
};

// ── genre grid → stations ─────────────────────────────────────────────────
async function _loadGenreGrid(){
  _setBody(_loadingHtml());
  try{
    const d = await _api('/api/radio/genres?limit=80');
    const ts = (d.data || []).filter(t => t.name && (t.stationcount||0) > 0);
    if(!ts.length){ _setBody(_emptyHtml('🎵','No genre data available')); return; }
    let h = '<div class="rdio-tag-grid">';
    for(const t of ts){
      const name  = _esc(t.name);
      const count = t.stationcount ? `<span style="font-size:9px;opacity:.45;margin-left:3px">${t.stationcount}</span>` : '';
      h += `<button class="rdio-tag" onclick="_rdioByGenre('${name}')">${name}${count}</button>`;
    }
    h += '</div>';
    _setBody(h);
  }catch(e){ _setBody(_emptyHtml('⚠️', _esc(e.message))); }
}

window._rdioByGenre = async function(tag){
  _setBody(_loadingHtml('Loading ' + _esc(tag) + '…'));
  try{
    const d = await _api(`/api/radio/genre/${encodeURIComponent(tag)}?limit=200`);
    _renderList(d.data || [], tag, true);
  }catch(e){ _setBody(_emptyHtml('⚠️', _esc(e.message))); }
};

// ── M3U sources ───────────────────────────────────────────────────────────
async function _loadSources(){
  _setBody(_loadingHtml());
  try{
    const d = await _api('/api/radio/sources');
    const srcs = d.sources || [];
    if(!srcs.length){ _setBody(_emptyHtml('📂','No M3U sources configured')); return; }
    let h = '<ul class="rdio-list">';
    for(const s of srcs){
      h += `<li class="rdio-src-item">
        <span class="rdio-src-name">${_esc(s)}</span>
        <button class="btn-ghost" style="height:26px;padding:0 12px;font-size:11px;flex-shrink:0"
          onclick="_rdioLoadM3U('${_esc(s)}')">Load</button>
      </li>`;
    }
    h += '</ul>';
    _setBody(h);
  }catch(e){ _setBody(_emptyHtml('⚠️', _esc(e.message))); }
}

window._rdioLoadM3U = async function(name){
  _setBody(_loadingHtml('Fetching ' + _esc(name) + ' — may take a moment for large playlists…'));
  try{
    const d = await _api('/api/radio/load_source', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({source: name, limit: 500}),
    }, 80000);
    if(!d.data || !d.data.length){
      const note = d.note || 'No stations found';
      _setBody(_emptyHtml('📂', note));
      return;
    }
    const label = d.truncated
      ? `${_esc(name)} (showing ${d.count} of ${d.total})`
      : _esc(name);
    _renderList(d.data, label, true);
  }catch(e){ _setBody(_emptyHtml('⚠️', _esc(e.message))); }
};

// ── trending (RadioBrowser last-hour clicks) ──────────────────────────────
async function _loadTrending(){
  _setBody(_loadingHtml());
  try{
    const d = await _api('/api/radio/trending?limit=50');
    if(!(d.data||[]).length){
      _setBody(_emptyHtml('📈','No trending data available right now'));
      return;
    }
    _renderList(d.data, 'Trending Now');
  }catch(e){ _setBody(_emptyHtml('⚠️', _esc(e.message))); }
}

// ── recently played history ────────────────────────────────────────────────
async function _loadHistory(){
  _setBody(_loadingHtml());
  try{
    const d = await _api('/api/radio/history');
    const items = d.data || [];
    if(!items.length){
      _setBody(_emptyHtml('🕐','No recently played stations yet'));
      return;
    }
    let h = '<div style="display:flex;justify-content:flex-end;padding:6px 12px;flex-shrink:0">'
          + '<button class="btn-ghost" style="height:26px;padding:0 12px;font-size:11px"'
          + ' onclick="_rdioHistoryClear()">🗑 Clear</button></div>';
    h += '<ul class="rdio-list">';
    for(const s of items){
      const url  = (s.url_resolved || s.url || '').trim();
      if(!url) continue;
      const name = _esc(s.name || 'Unknown Station');
      const cc   = (s.countrycode || '').toUpperCase();
      const rel  = _esc(_rdioRelTime(s._played_at || ''));
      const logo = (s.logo || '').trim();
      const uuid = s.stationuuid || '';
      const fav  = _isFav(url, uuid);
      const logoH = logo
        ? `<img class="rdio-item-logo" loading="lazy" src="${_esc(logo)}"
             onerror="this.outerHTML='<div class=rdio-item-logo style=\\'font-size:18px\\'>📻</div>'">`
        : `<div class="rdio-item-logo" style="font-size:18px">📻</div>`;
      const stEnc  = encodeURIComponent(JSON.stringify({
        name:s.name||'Unknown',url,url_resolved:s.url_resolved||url,
        logo,countrycode:cc,tags:s.tags||'',bitrate:s.bitrate||0,stationuuid:uuid}));
      const urlEnc  = encodeURIComponent(url);
      const uuidEnc = encodeURIComponent(uuid);
      h += `<li class="rdio-item">
        ${logoH}
        <div class="rdio-item-info">
          <div class="rdio-item-name">${name}</div>
          <div class="rdio-item-meta">${rel}${cc ? '  ·  ' + _esc(cc) : ''}</div>
        </div>
        <button class="rdio-item-fav${fav?' active':''}"
          onclick="_rdioToggleFav(this,'${urlEnc}','${uuidEnc}','${stEnc}')">${fav?'★':'☆'}</button>
        <button class="rdio-item-play"
          onclick="radioPlayStation('${urlEnc}','${stEnc}')">▶</button>
      </li>`;
    }
    h += '</ul>';
    _setBody(h);
  }catch(e){ _setBody(_emptyHtml('⚠️', _esc(e.message))); }
}

window._rdioHistoryClear = async function(){
  try{
    await fetch('/api/radio/history', {method:'DELETE'});
    _setBody(_emptyHtml('🕐','No recently played stations yet'));
    if(typeof toast==='function') toast('History cleared','k');
  }catch(e){}
};

function _rdioRelTime(isoStr){
  if(!isoStr) return '';
  try{
    const sec = Math.floor((Date.now() - new Date(isoStr)) / 1000);
    if(sec < 60)    return 'Just now';
    if(sec < 3600)  return Math.floor(sec/60) + ' min ago';
    if(sec < 86400) return Math.floor(sec/3600) + ' hr ago';
    if(sec < 172800)return 'Yesterday';
    return new Date(isoStr).toLocaleDateString();
  }catch(e){ return ''; }
}

// ── nearby (Geolocation → RadioBrowser geo radius) ────────────────────────
async function _loadNearby(){
  if(!('geolocation' in navigator)){
    _setBody(_emptyHtml('📍','Geolocation not supported in this browser'));
    return;
  }
  _setBody(_loadingHtml('Getting your location…'));
  navigator.geolocation.getCurrentPosition(
    async pos => {
      _setBody(_loadingHtml('Finding nearby stations…'));
      try{
        const {latitude:lat, longitude:lng} = pos.coords;
        const d = await _api(
          `/api/radio/nearby?lat=${lat}&lng=${lng}&distance=300&limit=100`);
        if(!(d.data||[]).length){
          _setBody(_emptyHtml('📍',
            'No stations found within 300 km — not all stations have location data'));
        } else {
          _renderList(d.data, 'Nearby Stations', true);
        }
      }catch(e){ _setBody(_emptyHtml('⚠️', _esc(e.message))); }
    },
    err => {
      const msgs = {
        1:'Location access denied — allow location in browser settings',
        2:'Location unavailable',
        3:'Location request timed out',
      };
      _setBody(_emptyHtml('📍', msgs[err.code] || 'Geolocation error'));
    },
    {timeout:10000, maximumAge:300000}
  );
}

// ── M3U export from localStorage favorites ────────────────────────────────
window._rdioExportM3U = function(){
  _favsLoad();
  if(!_favs.length){
    if(typeof toast==='function') toast('No favorites to export','w');
    return;
  }
  const lines = ['#EXTM3U'];
  for(const s of _favs){
    const url = (s.url_resolved||s.url||'').trim();
    if(!url) continue;
    const name  = (s.name||'Unknown').replace(/,/g,' ');
    const logo  = (s.logo||'').replace(/"/g,'');
    const group = (s.tags||s.countrycode||'Radio').replace(/"/g,'');
    lines.push(`#EXTINF:-1 tvg-logo="${logo}" group-title="${group}",${name}`);
    lines.push(url);
  }
  const blob = new Blob([lines.join('\n')+'\n'], {type:'audio/x-mpegurl;charset=utf-8'});
  const a    = document.createElement('a');
  a.href     = URL.createObjectURL(blob);
  a.download = 'radio_favorites.m3u';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
  const n = _favs.filter(s=>s.url).length;
  if(typeof toast==='function') toast(`Exported ${n} station${n!==1?'s':''} 💾`,'k');
};

// ── now-playing bar (ICY StreamTitle polling) ─────────────────────────────
// Polls /api/radio/nowplaying every 20 s after a station starts playing.
// The server caches ICY reads for 20 s — cheap after the first call.
// Bar visible inside the radio modal only.

function _rdioNpStart(url){
  _curRadioUrl = url;
  _rdioLastNp  = '';
  _rdioNpBarShow('');
  if(_rdioNpTimer){ clearInterval(_rdioNpTimer); _rdioNpTimer = null; }
  setTimeout(()  => _rdioNpFetch(url), 4000);           // first fetch after 4 s
  _rdioNpTimer = setInterval(() => _rdioNpFetch(url), 20000);
}

function _rdioNpStop(){
  _curRadioUrl = '';
  _rdioLastNp  = '';
  if(_rdioNpTimer){ clearInterval(_rdioNpTimer); _rdioNpTimer = null; }
  _rdioNpBarShow('');
}

async function _rdioNpFetch(url){
  if(!url || url !== _curRadioUrl) return;
  try{
    const r = await _api(`/api/radio/nowplaying?url=${encodeURIComponent(url)}`,{},12000);
    if(url !== _curRadioUrl) return;
    const title = (r.result && r.result.stream_title) || '';
    _rdioLastNp = title;
    _rdioNpBarShow(title);
  }catch(e){}
}

function _rdioNpBarShow(title){
  const bar  = document.getElementById('rdio-np-bar');
  const text = document.getElementById('rdio-np-text');
  if(!bar||!text) return;
  if(title && _curRadioUrl){
    text.textContent = '♫  ' + title;
    bar.style.display = '';
  } else {
    bar.style.display = 'none';
  }
}

// ── favorites ─────────────────────────────────────────────────────────────
function _favsLoad(){
  try{ _favs = JSON.parse(localStorage.getItem(_FAV_KEY) || '[]'); }
  catch(e){ _favs = []; }
}
function _favsSave(){
  try{ localStorage.setItem(_FAV_KEY, JSON.stringify(_favs)); }
  catch(e){}
}
function _isFav(url, uuid){
  return _favs.some(f => (uuid && f.stationuuid && f.stationuuid === uuid) || f.url === url);
}

window._rdioToggleFav = function(btn, urlEnc, uuidEnc, stJsonEnc){
  const url  = decodeURIComponent(urlEnc);
  const uuid = decodeURIComponent(uuidEnc);
  _favsLoad();
  if(_isFav(url, uuid)){
    _favs = _favs.filter(f => !((uuid && f.stationuuid && f.stationuuid===uuid) || f.url===url));
    btn.classList.remove('active'); btn.textContent = '☆';
    if(typeof toast === 'function') toast('Removed from Radio Favorites','k');
  } else {
    let st;
    try{ st = JSON.parse(decodeURIComponent(stJsonEnc)); }
    catch(e){ st = {name:'Unknown', url}; }
    _favs.push(st);
    btn.classList.add('active'); btn.textContent = '★';
    if(typeof toast === 'function') toast('Added to Radio Favorites ★','k');
  }
  _favsSave();
  // If we're on the favorites tab, refresh it live
  if(_curTab === 'favorites') _renderFavs();
};

function _renderFavs(){
  _favsLoad();
  if(!_favs.length){
    _setBody(_emptyHtml('★', 'No favorites yet — tap ☆ on any station to save it'));
    return;
  }
  _renderList(_favs, '', false);
  // Prepend export button without duplicating list rendering
  const body = document.getElementById('rdio-body');
  if(body){
    const wrap = document.createElement('div');
    wrap.style.cssText = 'display:flex;justify-content:flex-end;padding:6px 12px;flex-shrink:0';
    wrap.innerHTML = '<button class="btn-ghost" style="height:26px;padding:0 12px;font-size:11px"'
                   + ' onclick="_rdioExportM3U()">⬇ Export M3U</button>';
    body.insertBefore(wrap, body.firstChild);
  }
}

// ── render station list ───────────────────────────────────────────────────
function _renderList(stations, queryOrLabel, showBack){
  if(!stations || !stations.length){
    _setBody(_emptyHtml('📭', 'No stations found'));
    return;
  }
  let h = '';
  if(showBack){
    const tabName = _curTab;
    h += `<button class="btn-ghost rdio-back-btn"
      onclick="radioTab(document.querySelector('.rdio-tab[data-tab=&quot;${tabName}&quot;]'),'${tabName}')">
      ← Back
    </button>`;
  }
  h += '<ul class="rdio-list">';
  for(const s of stations){
    const url  = (s.url_resolved || s.url || '').trim();
    if(!url) continue;
    const name = _esc(s.name || 'Unknown Station');
    const cc   = (s.countrycode || '').toUpperCase();
    const tags = (s.tags || '').split(',').slice(0,2).map(t=>t.trim()).filter(Boolean).join(' · ');
    const br   = s.bitrate ? s.bitrate + ' kbps' : '';
    const meta = [cc, tags, br].filter(Boolean).join('  ·  ');
    const logo = (s.logo || '').trim();
    const uuid = s.stationuuid || '';
    const fav  = _isFav(url, uuid);

    // Logo or placeholder emoji
    const logoH = logo
      ? `<img class="rdio-item-logo" loading="lazy" src="${_esc(logo)}"
           onerror="this.outerHTML='<div class=rdio-item-logo style=\\'font-size:18px\\'>📻</div>'">`
      : `<div class="rdio-item-logo" style="font-size:18px">📻</div>`;

    // Encode station data for fav callback without inline JSON
    const stEnc = encodeURIComponent(JSON.stringify({
      name: s.name || 'Unknown', url, url_resolved: s.url_resolved || url,
      logo, countrycode: cc, tags: s.tags || '',
      bitrate: s.bitrate || 0, stationuuid: uuid,
    }));
    const urlEnc  = encodeURIComponent(url);
    const uuidEnc = encodeURIComponent(uuid);

    h += `<li class="rdio-item">
      ${logoH}
      <div class="rdio-item-info">
        <div class="rdio-item-name">${name}</div>
        ${meta ? `<div class="rdio-item-meta">${_esc(meta)}</div>` : ''}
      </div>
      <button class="rdio-item-fav${fav?' active':''}" title="${fav?'Remove from favorites':'Add to favorites'}"
        onclick="_rdioToggleFav(this,'${urlEnc}','${uuidEnc}','${stEnc}')">${fav?'★':'☆'}</button>
      <button class="rdio-item-play" title="Play ${_esc(s.name||'')}"
        onclick="radioPlayStation('${urlEnc}','${stEnc}')">▶</button>
    </li>`;
  }
  h += '</ul>';
  _setBody(h);
}

// ── play ──────────────────────────────────────────────────────────────────
window.radioPlayStation = function(urlEnc, stEnc){
  const url = decodeURIComponent(urlEnc);
  if(!url) return;
  let st = {name: url, url};
  if(stEnc){ try{ st = JSON.parse(decodeURIComponent(stEnc)); }catch(e){} }
  st.url = url;   // always trust the passed URL
  radioClose();
  _rdioVizStart(st);

  // 1. Register click with RadioBrowser (API citizenship) + push to history
  fetch('/api/radio/click', {
    method:  'POST',
    headers: {'Content-Type':'application/json'},
    body:    JSON.stringify(st),
  }).catch(()=>{});   // fire-and-forget

  // 2. Start now-playing bar polling (ICY StreamTitle)
  _rdioNpStart(url);

  if(typeof doPlay === 'function'){
    doPlay(url, st.name || url, {isLive: true});
  } else {
    const v = document.getElementById('vid');
    if(v){ v.src = url; v.play().catch(()=>{}); }
  }
};

// ── country dropdown population ───────────────────────────────────────────
function _loadCountryDropdown(){
  _ctriesLoaded = true;
  const sel = document.getElementById('rdio-country');
  if(!sel) return;
  // Add placeholder option
  sel.innerHTML = '<option value="">🌍 All countries</option>';
  _api('/api/radio/countries').then(d => {
    (d.data || [])
      .filter(c => c.name && (c.stationcount||0) > 5)
      .slice(0, 150)
      .forEach(c => {
        const o  = document.createElement('option');
        o.value  = c.iso_3166_1 || c.name;
        o.textContent = c.name + (c.stationcount ? ` (${c.stationcount})` : '');
        sel.appendChild(o);
      });
  }).catch(()=>{});
}

// ── helpers ───────────────────────────────────────────────────────────────
function _setBody(html){ document.getElementById('rdio-body').innerHTML = html; }

function _loadingHtml(msg){
  return `<div class="rdio-loading">
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none"
         style="animation:spin .8s linear infinite;flex-shrink:0">
      <circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="2" opacity=".25"/>
      <path d="M8 2a6 6 0 0 1 6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    </svg>
    ${_esc(msg || 'Loading…')}
  </div>`;
}

function _emptyHtml(ico, msg){
  return `<div class="rdio-empty"><span>${ico}</span>${_esc(msg)}</div>`;
}

function _esc(s){
  return String(s || '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

async function _api(url, opts, timeout){
  const ctrl = new AbortController();
  const tid  = setTimeout(() => ctrl.abort(), timeout || 15000);
  try{
    const r = await fetch(url, {...(opts||{}), signal: ctrl.signal});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  } finally { clearTimeout(tid); }
}


// ══════════════════════════════════════════════════════════════════
// RADIO VISUALIZER
//
// Canvas: position:fixed on <body>, promoted to its own GPU
//   compositing layer via transform:translateZ(0).
//
// Layout tracking (black-space / cpanel fix):
//   Every _draw() frame checks #vwrap.getBoundingClientRect().
//   If position or size changed, _applyRect() repositions/resizes
//   the canvas immediately. Catches ALL layout shifts including the
//   #cpanel max-height CSS transition (which fires no resize/scroll).
//
// Modal fix:
//   MutationObserver watches .open class on overlay elements and
//   style on inline-style modals. When any modal is visible the
//   canvas is hidden (visibility:hidden, NOT display:none — the
//   animation loop continues so it's instant when modal closes).
//   No GPU layer fights, 100% reliable.
//
// Audio: Web Audio API createMediaElementSource(#vid).
//   Falls back to simulated sine-wave if CORS-blocked or unavailable.
//
// Layers (bottom → top):
//   0  Dark trail (motion blur)
//   1  Drifting nebula gradient (purple/cyan/green, bass-reactive)
//   2  Scrolling perspective grid
//   3  128-bar radial spectrum (rotates, color-coded by freq)
//   4  Inner bass ring (purple glow)
//   5  Outer mid ring (cyan)
//   6  Center circle (station logo or 📻 emoji)
//   7  36 floating particles (bass-reactive)
//   8  Bottom scrim with station name / country / tags / bitrate
//   9  ● RADIO badge top-right
// ══════════════════════════════════════════════════════════════════

class _RdioViz {
  constructor(){
    this._canvas    = null;   this._ctx       = null;
    this._animId    = null;   this._running   = false;
    this._audioCtx  = null;   this._analyser  = null;
    this._source    = null;   this._audioOk   = false;
    this._info      = {};     this._dpr        = 1;
    this._simT      = 0;
    this._particles = this._mkParticles();
    this._logoImg   = null;   this._logoLoaded = false;
    this._logoSrc   = '';
    // Layout tracking
    this._lastRect  = null;
    // Modal observer
    this._modalObs  = null;
    // Window resize (canvas pixel dimensions)
    this._onResize  = null;
  }

  // ── lifecycle ────────────────────────────────────────────────────

  start(info){
    this._info = info || {};
    this._ensureCanvas();
    if(!this._canvas) return;
    this._setLogo(this._info.logo || '');
    this._canvas.style.cssText += ';display:block;visibility:visible';
    this._lastRect = null;          // force initial position update
    this._resize();
    this._setupAudio();
    this._running   = true;
    this._onResize  = () => requestAnimationFrame(() => this._resize());
    window.addEventListener('resize', this._onResize);
    this._startModalObs();
    this._loop();
  }

  stop(){
    this._running = false;
    if(this._animId){ cancelAnimationFrame(this._animId); this._animId = null; }
    if(this._onResize){ window.removeEventListener('resize', this._onResize); this._onResize = null; }
    this._stopModalObs();
    if(this._canvas) this._canvas.style.display = 'none';
  }

  // ── canvas setup ─────────────────────────────────────────────────

  _ensureCanvas(){
    if(this._canvas) return;
    const c = document.createElement('canvas');
    c.id = 'radio-viz';
    // Fixed on <body> so it's in the same top-level stacking context as <video>.
    // transform:translateZ(0) + will-change promote it to its own GPU compositing
    // layer so z-index 98 is respected above the video's hardware layer.
    Object.assign(c.style, {
      position:        'fixed',
      display:         'none',
      visibility:      'visible',
      pointerEvents:   'none',
      zIndex:          '98',
      transform:       'translateZ(0)',
      willChange:      'transform',
      webkitTransform: 'translateZ(0)',
    });
    document.body.appendChild(c);
    this._canvas = c;
    this._ctx    = c.getContext('2d');
  }

  // Apply a pre-computed rect to the canvas CSS position + pixel dimensions.
  // Called from _draw() every frame (position) and _resize() (dimensions).
  _applyRect(r){
    if(!this._canvas || !r || !r.width || !r.height) return;
    const dpr  = window.devicePixelRatio || 1;
    this._dpr  = dpr;
    const c    = this._canvas;
    // CSS position (cheap, no canvas buffer change)
    c.style.left   = r.left   + 'px';
    c.style.top    = r.top    + 'px';
    c.style.width  = r.width  + 'px';
    c.style.height = r.height + 'px';
    // Pixel dimensions (expensive — clears canvas, only if changed)
    const nw = Math.round(r.width  * dpr);
    const nh = Math.round(r.height * dpr);
    if(c.width !== nw || c.height !== nh){ c.width = nw; c.height = nh; }
  }

  _resize(){
    const vwrap = document.getElementById('vwrap');
    if(!vwrap) return;
    const r = vwrap.getBoundingClientRect();
    if(r.width && r.height){
      this._lastRect = {left:r.left, top:r.top, width:r.width, height:r.height};
      this._applyRect(r);
    }
  }

  // Called by _rdioVizStart() after stream has had time to decode
  checkVideoContent(){
    const vid = document.getElementById('vid');
    if(!vid || !this._canvas) return;
    // >=120x60: Chrome audio-player UI can report small non-zero dims
    if(vid.videoWidth >= 120 && vid.videoHeight >= 60)
      this._canvas.style.display = 'none';
  }

  // ── modal observer ───────────────────────────────────────────────
  // Hides canvas (visibility:hidden) when any modal is open so modals
  // always appear above the canvas regardless of GPU layer order.

  _startModalObs(){
    if(this._modalObs) return;
    const update = () => {
      if(!this._canvas) return;
      // Class-based overlays
      const classOpen = ['pl-overlay','vf-overlay','radio-overlay',
                         'vod-expand-overlay','vod-expand-detail']
        .some(id => { const el = document.getElementById(id); return el && el.classList.contains('open'); });
      // Style-based modals
      const styleOpen = ['item-menu','profile-modal']
        .some(id => { const el = document.getElementById(id);
                      return el && el.style.display && el.style.display !== 'none'; });
      const vis = (classOpen || styleOpen) ? 'hidden' : 'visible';
      if(this._canvas.style.visibility !== vis) this._canvas.style.visibility = vis;
    };
    this._modalObs = new MutationObserver(update);
    ['pl-overlay','vf-overlay','radio-overlay','vod-expand-overlay','vod-expand-detail']
      .forEach(id => {
        const el = document.getElementById(id);
        if(el) this._modalObs.observe(el, {attributes:true, attributeFilter:['class']});
      });
    ['item-menu','profile-modal']
      .forEach(id => {
        const el = document.getElementById(id);
        if(el) this._modalObs.observe(el, {attributes:true, attributeFilter:['style']});
      });
  }

  _stopModalObs(){
    if(this._modalObs){ this._modalObs.disconnect(); this._modalObs = null; }
    if(this._canvas) this._canvas.style.visibility = 'visible';
  }

  // ── audio ────────────────────────────────────────────────────────

  _setupAudio(){
    const vid = document.getElementById('vid');
    if(!vid) return;
    try{
      if(!this._audioCtx)
        this._audioCtx = new(window.AudioContext||window.webkitAudioContext)();
      if(this._audioCtx.state === 'suspended') this._audioCtx.resume().catch(()=>{});
      if(!this._analyser){
        this._analyser = this._audioCtx.createAnalyser();
        this._analyser.fftSize               = 512;
        this._analyser.smoothingTimeConstant = 0.80;
        this._analyser.minDecibels           = -88;
        this._analyser.maxDecibels           = -10;
      }
      if(!this._source){
        this._source = this._audioCtx.createMediaElementSource(vid);
        this._source.connect(this._analyser);
        this._analyser.connect(this._audioCtx.destination);
      }
      this._audioOk = true;
    }catch(e){ this._audioOk = false; }
  }

  _setLogo(src){
    src = (src || '').trim();
    if(src === this._logoSrc) return;
    this._logoSrc = src;
    if(!src){
      // This station has no logo — clear any previously-loaded image so
      // the 📻 fallback renders instead of a stale logo from the last
      // station that did have one.
      this._logoImg = null; this._logoLoaded = false;
      return;
    }
    this._logoLoaded = false;
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload  = () => { this._logoImg = img; this._logoLoaded = true; };
    img.onerror = () => { this._logoImg = null; this._logoLoaded = false; };
    img.src = src;
  }

  _mkParticles(){
    const C = [['#7c3aed','#a855f7'],['#06b6d4','#22d3ee'],['#22c55e','#4ade80']];
    return Array.from({length:36}, () => ({
      x: Math.random(), y: Math.random(),
      r: 0.9 + Math.random() * 1.9,
      vx:(Math.random() - 0.5) * 0.00022,
      vy:-(0.00007 + Math.random() * 0.00017),
      a: 0.12 + Math.random() * 0.34,
      clr: C[Math.floor(Math.random()*3)],
    }));
  }

  // ── animation loop ───────────────────────────────────────────────

  _loop(){
    if(!this._running) return;
    this._animId = requestAnimationFrame(() => this._loop());
    this._draw();
  }

  _draw(){
    // ── Per-frame layout tracking ────────────────────────────────────
    // getBoundingClientRect() is ~0.01 ms; doing it every frame is negligible
    // but catches every layout shift: cpanel transitions, sidebar changes, etc.
    const vwrap = document.getElementById('vwrap');
    if(vwrap){
      const r  = vwrap.getBoundingClientRect();
      const lr = this._lastRect;
      if(r.width && r.height){
        if(!lr || r.left!==lr.left || r.top!==lr.top || r.width!==lr.width || r.height!==lr.height){
          this._lastRect = {left:r.left, top:r.top, width:r.width, height:r.height};
          this._applyRect(r);
        }
      }
    }

    const c = this._canvas, ctx = this._ctx;
    if(!c || !ctx || !c.width || !c.height) return;

    const dpr  = this._dpr || 1;
    const W    = c.width / dpr;
    const H    = c.height / dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const t    = Date.now() / 1000;
    const freq = this._getFreq();
    const N    = freq.length;
    const avgAmp  = freq.reduce((s,v) => s+v, 0) / N / 255;
    const bEnd    = Math.floor(N * 0.12);
    const mEnd    = Math.floor(N * 0.45);
    const bassAmp = freq.slice(0, bEnd).reduce((s,v) => s+v, 0) / bEnd / 255;
    const midAmp  = freq.slice(bEnd, mEnd).reduce((s,v) => s+v, 0) / (mEnd-bEnd) / 255;
    const cx = W/2, cy = H/2;
    const innerR = Math.min(W,H) * 0.13;
    const maxBar = Math.min(W,H) * 0.27;

    // L0: dark persistent trail (motion blur)
    ctx.fillStyle = 'rgba(6,6,18,0.32)';
    ctx.fillRect(0, 0, W, H);

    // L1: drifting nebula gradient
    const gx = cx + Math.sin(t*0.19)*W*0.20;
    const gy = cy + Math.cos(t*0.14)*H*0.14;
    const gr = ctx.createRadialGradient(gx,gy,0,cx,cy,Math.max(W,H)*0.72);
    gr.addColorStop(0,    `rgba(124,58,237,${+(0.12+bassAmp*0.15).toFixed(3)})`);
    gr.addColorStop(0.38, `rgba(6,182,212,${+(0.05+midAmp*0.08).toFixed(3)})`);
    gr.addColorStop(0.75, `rgba(34,197,94,${+(0.01+avgAmp*0.03).toFixed(3)})`);
    gr.addColorStop(1,    'rgba(0,0,0,0)');
    ctx.fillStyle = gr;
    ctx.fillRect(0, 0, W, H);

    // L2: scrolling perspective grid
    const gs = 44;
    ctx.strokeStyle = `rgba(124,58,237,${+(0.030+avgAmp*0.038).toFixed(3)})`;
    ctx.lineWidth = 0.5; ctx.beginPath();
    for(let x=(t*6)%gs-gs; x<W+gs; x+=gs){ ctx.moveTo(x,0); ctx.lineTo(x,H); }
    for(let y=(t*3)%gs-gs; y<H+gs; y+=gs){ ctx.moveTo(0,y); ctx.lineTo(W,y); }
    ctx.stroke();

    // L3: 128-bar radial spectrum
    const numBars = 128, rot = t * 0.13;
    ctx.lineCap = 'round';
    for(let i=0; i<numBars; i++){
      const angle  = (i/numBars)*Math.PI*2 - Math.PI/2 + rot;
      const amp    = freq[Math.floor((i/numBars)*N*0.68)] / 255;
      const barLen = innerR*0.08 + amp*maxBar;
      const frac   = i/numBars;
      let r,g,b;
      if(frac<0.34){ const p=frac/0.34; r=Math.round(124+p*(6-124)); g=Math.round(58+p*(182-58));   b=Math.round(237+p*(212-237)); }
      else if(frac<0.67){ const p=(frac-0.34)/0.33; r=Math.round(6+p*(34-6));   g=Math.round(182+p*(197-182)); b=Math.round(212+p*(94-212)); }
      else{ const p=(frac-0.67)/0.33; r=Math.round(34+p*(124-34)); g=Math.round(197+p*(58-197)); b=Math.round(94+p*(237-94)); }
      const alpha = 0.42 + amp*0.58;
      const x1=cx+Math.cos(angle)*innerR,          y1=cy+Math.sin(angle)*innerR;
      const x2=cx+Math.cos(angle)*(innerR+barLen),  y2=cy+Math.sin(angle)*(innerR+barLen);
      const bg = ctx.createLinearGradient(x1,y1,x2,y2);
      bg.addColorStop(0, `rgba(${r},${g},${b},${alpha.toFixed(2)})`);
      bg.addColorStop(1, `rgba(${r},${g},${b},0)`);
      ctx.strokeStyle = bg; ctx.lineWidth = 2.4;
      ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
    }

    // L4: inner bass ring
    ctx.shadowColor = '#7c3aed'; ctx.shadowBlur = 7 + bassAmp*20;
    ctx.strokeStyle = `rgba(124,58,237,${+(0.42+bassAmp*0.52).toFixed(2)})`;
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(cx, cy, innerR*(1+bassAmp*0.38), 0, Math.PI*2); ctx.stroke();
    ctx.shadowBlur = 0;

    // L5: outer mid ring
    ctx.shadowColor = '#06b6d4'; ctx.shadowBlur = 3 + midAmp*9;
    ctx.strokeStyle = `rgba(6,182,212,${+(0.16+midAmp*0.22).toFixed(2)})`;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, innerR*1.72+midAmp*innerR*0.55, 0, Math.PI*2); ctx.stroke();
    ctx.shadowBlur = 0;

    // L6: center circle (black fill erases bars crossing center, then logo/emoji)
    ctx.fillStyle = '#060612';
    ctx.beginPath(); ctx.arc(cx, cy, innerR*0.98, 0, Math.PI*2); ctx.fill();
    if(this._logoLoaded && this._logoImg){
      const lr = innerR * 0.84;
      ctx.save();
      ctx.beginPath(); ctx.arc(cx, cy, lr, 0, Math.PI*2); ctx.clip();
      ctx.drawImage(this._logoImg, cx-lr, cy-lr, lr*2, lr*2);
      ctx.restore();
    } else {
      const es = Math.round(innerR * 0.78);
      ctx.font = es + 'px serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.globalAlpha = 0.70 + Math.sin(t*1.8)*0.14;
      ctx.fillText('\uD83D\uDCFB', cx, cy + es*0.04);
      ctx.globalAlpha = 1;
    }

    // L7: particles
    for(const p of this._particles){
      p.x += p.vx + (Math.random()-0.5)*0.00014;
      p.y += p.vy * (1 + bassAmp*2.4);
      if(p.y < -0.04){ p.y = 1.04; p.x = Math.random(); }
      ctx.globalAlpha = p.a * (0.5 + avgAmp*0.55);
      ctx.shadowColor = p.clr[1]; ctx.shadowBlur = 5;
      ctx.fillStyle   = p.clr[0];
      ctx.beginPath(); ctx.arc(p.x*W, p.y*H, p.r*(1+bassAmp*1.9), 0, Math.PI*2); ctx.fill();
    }
    ctx.globalAlpha = 1; ctx.shadowBlur = 0;

    // L8: bottom station info scrim
    const name = (this._info.name    || '').trim();
    const cc   = (this._info.countrycode || '').toUpperCase();
    const tags = (this._info.tags    || '').split(',').slice(0,3).map(s=>s.trim()).filter(Boolean).join(' \u00B7 ');
    const br   = this._info.bitrate  ? this._info.bitrate + ' kbps' : '';
    const sub  = [cc, tags, br].filter(Boolean).join('  \u00B7  ');
    if(name || sub){
      const sh = Math.max(56, H*0.20);
      const sc = ctx.createLinearGradient(0, H-sh, 0, H);
      sc.addColorStop(0,'rgba(0,0,0,0)'); sc.addColorStop(1,'rgba(0,0,0,0.80)');
      ctx.fillStyle = sc; ctx.fillRect(0, H-sh, W, sh);
      if(name){
        const fz = Math.max(12, Math.min(22, W*0.028));
        ctx.font = `600 ${fz}px "Segoe UI",system-ui,sans-serif`;
        ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
        ctx.fillStyle = 'rgba(228,234,248,0.93)';
        ctx.shadowColor = 'rgba(0,0,0,0.95)'; ctx.shadowBlur = 5;
        ctx.fillText(name.length>46 ? name.slice(0,45)+'\u2026' : name, cx, H-(sub?28:12));
        ctx.shadowBlur = 0;
      }
      if(sub){
        const fz = Math.max(9, Math.min(13, W*0.016));
        ctx.font = `${fz}px "Segoe UI",system-ui,sans-serif`;
        ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
        ctx.fillStyle = 'rgba(125,138,162,0.88)';
        ctx.fillText(sub.length>60 ? sub.slice(0,59)+'\u2026' : sub, cx, H-10);
      }
    }

    // L9: RADIO badge
    const bfz = Math.max(9, Math.min(12, W*0.014));
    ctx.font = `bold ${bfz}px "Segoe UI",sans-serif`;
    ctx.textAlign = 'right'; ctx.textBaseline = 'top';
    ctx.fillStyle = `rgba(124,58,237,${+(0.5+bassAmp*0.4).toFixed(2)})`;
    ctx.fillText('\u25CF RADIO', W-10, 10);
  }

  // ── frequency data ────────────────────────────────────────────────

  _getFreq(){
    if(this._audioOk && this._analyser){
      const buf = new Uint8Array(this._analyser.frequencyBinCount);
      this._analyser.getByteFrequencyData(buf);
      if(buf.some(v => v > 0)) return buf;
    }
    // Simulation: three-layer sine waves producing realistic music energy
    this._simT += 0.022;
    const st=this._simT, N=256, buf=new Uint8Array(N);
    for(let i=0; i<N; i++){
      const f    = i/N;
      const bass = Math.pow(Math.max(0, Math.sin(st*0.88+i*0.11)), 2) * 200 * (1-f*0.78);
      const mid  = Math.abs(Math.sin(st*2.14+i*0.27)) * 125 * (f<0.55?1:0.35);
      const hi   = Math.abs(Math.sin(st*5.40+i*0.74)) * 52 * f;
      buf[i]     = Math.min(255, Math.round(bass + mid + hi + Math.random()*14));
    }
    return buf;
  }
}

// ── toggle state (persisted in localStorage) ──────────────────────────────────
// Default: OFF. If the user turns it ON, that choice is remembered for next run
// (localStorage 'rdio_viz_en' === '1'). Any other value (including absent) = off.
let _rdioVizEnabled = false;
try{ _rdioVizEnabled = localStorage.getItem('rdio_viz_en') === '1'; }catch(e){}

// Exposed on window — called from an inline onclick="" attribute in the HTML,
// which executes in global scope and cannot see names local to this IIFE.
window._rdioVizToggle = function(){
  _rdioVizEnabled = !_rdioVizEnabled;
  try{ localStorage.setItem('rdio_viz_en', _rdioVizEnabled ? '1' : '0'); }catch(e){}
  _rdioVizSyncBtn();
  if(!_rdioVizEnabled && _rdioVizActive){
    _rdioVizStop();
  } else if(_rdioVizEnabled && !_rdioVizActive && _curRadioUrl){
    // User turned viz on while radio is already playing — start it now
    // using the info from the currently-playing station, if available.
    const info = (_favs.find(s => (s.url_resolved||s.url) === _curRadioUrl)) || {url: _curRadioUrl};
    _rdioVizStart(info);
  }
  if(typeof toast === 'function') toast(_rdioVizEnabled ? 'Visualizer on' : 'Visualizer off', 'k');
};

function _rdioVizSyncBtn(){
  const btn = document.getElementById('rdio-viz-btn');
  if(!btn) return;
  btn.classList.toggle('viz-on', _rdioVizEnabled);
  btn.title = _rdioVizEnabled
    ? 'Visualizer ON \u2014 click to disable'
    : 'Visualizer OFF \u2014 click to enable';
}

// ── singleton + start/stop ────────────────────────────────────────────────────
const _rdioViz        = new _RdioViz();
let   _rdioVizActive  = false;
let   _rdioVizStartTs = 0;
const _RDIO_GUARD_MS  = 2000;   // ignore loadstart/emptied for this long after starting

function _rdioVizStart(stInfo){
  if(!_rdioVizEnabled) return;
  _rdioVizActive  = true;
  _rdioVizStartTs = Date.now();
  _rdioViz.start(stInfo || {});
  // After stream decodes first frames, hide canvas if station has real video
  setTimeout(() => { if(_rdioVizActive) _rdioViz.checkVideoContent(); }, 2800);
}

function _rdioVizStop(){
  _rdioVizActive  = false;
  _rdioVizStartTs = 0;
  _rdioViz.stop();
  _rdioNpStop();   // stop now-playing polling when non-radio stream plays
}

// Stop viz when user plays a non-radio stream.
// Timestamp guard: doPlay() fires loadstart TWICE (clear old src + load new URL).
// A one-shot boolean flag would be consumed by the first event, letting the
// second event immediately kill the viz ("1-frame flash" bug). Instead we
// ignore ALL events within _RDIO_GUARD_MS of radio starting.
;(function(){
  const vid = document.getElementById('vid');
  if(!vid) return;
  const _onLoad = () => {
    if(!_rdioVizActive) return;
    if((Date.now() - _rdioVizStartTs) < _RDIO_GUARD_MS) return;
    _rdioVizStop();
  };
  vid.addEventListener('loadstart', _onLoad, {passive:true});
  vid.addEventListener('emptied',   _onLoad, {passive:true});
  // Secondary video-content check for slow HLS streams
  vid.addEventListener('playing', () => {
    if(_rdioVizActive)
      setTimeout(() => { if(_rdioVizActive) _rdioViz.checkVideoContent(); }, 500);
  }, {passive:true});
})();

// ── tab bar drag-to-scroll (desktop mouse) ───────────────────────────────
// Touch devices get native momentum scrolling for free.
// This adds equivalent click-drag for desktop mice.
//
// Key fix vs a naïve approach:
//   • rdio-tabs-dragging class is added ONLY after movement > 5 px — so a
//     normal click never has the class and buttons remain fully clickable.
//   • capture-phase click handler suppresses the click event only when a
//     real drag occurred (dragged flag), so tab buttons fire on clicks.
//   • No pointer-events:none on children — that was the v1 bug that blocked
//     all clicks by disabling hit-testing during mousedown→mouseup.
;(function(){
  const el = document.getElementById('rdio-tabs');
  if(!el) return;
  let down = false, dragged = false, startX = 0, scrollLeft = 0;

  el.addEventListener('mousedown', e => {
    if(e.button !== 0) return;
    down     = true;
    dragged  = false;
    startX   = e.clientX;
    scrollLeft = el.scrollLeft;
    // Do NOT add dragging class yet — only after actual movement
  });

  window.addEventListener('mouseup', () => {
    if(!down) return;
    down = false;
    el.classList.remove('rdio-tabs-dragging');
  });

  window.addEventListener('mousemove', e => {
    if(!down) return;
    const dx = e.clientX - startX;
    if(Math.abs(dx) > 5){
      dragged = true;
      el.classList.add('rdio-tabs-dragging');  // grabbing cursor only during real drag
    }
    if(dragged) el.scrollLeft = scrollLeft - dx;
  });

  // Capture phase: swallow the click if mouse was dragging so the tab
  // button under the release point doesn't activate.
  el.addEventListener('click', e => {
    if(dragged){ e.stopPropagation(); e.preventDefault(); dragged = false; }
  }, true);

  // Vertical wheel → horizontal pan (Shift+scroll on standard mice,
  // native horizontal swipe on trackpads).
  el.addEventListener('wheel', e => {
    e.preventDefault();
    el.scrollLeft += (e.deltaX || e.deltaY) * 0.8;
  }, {passive: false});
})();

})(); // end radio IIFE
"""
