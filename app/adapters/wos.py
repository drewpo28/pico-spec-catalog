"""World of Spectrum (worldofspectrum.org/archive) adapter via the ZXInfo API v3.

Browses ZXDB games by first letter and emits a direct download link per playable
file — the catalog stores name + link (link mode); the device downloads/unzips.

API:  GET https://api.zxinfo.dk/v3/games/byletter/{A..Z}?size=&offset=&mode=full
      → { hits: { total:{value}, hits: [ { _source: { title,
          releases: [ { files: [ {path,type,format,size}, ... ] } ] } } ] } }
The PLAYABLE files are in releases[].files[] (type "Tape image"/"Snapshot"/…);
additionalDownloads is only inlays/screens/instructions/music — NOT the game.
Download `path` is a relative WoS-archive path ("/pub/sinclair/games/a/AceLow.tap.zip")
→ prepend https://www.worldofspectrum.org. We keep files whose path carries a
playable format token.

Tree:  Games/<letter>/ → playable files.  (Demos: TODO via /v3/search.)
"""

from __future__ import annotations

import time

import httpx

from .base import Adapter, Entry

API = "https://api.zxinfo.dk/v3"
FILE_BASE = "https://worldofspectrum.net"          # serves /pub/sinclair/… directly (200);
                                                   # www.worldofspectrum.org 301-redirects those
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 1800
PAGE = 500                                          # entries per API request
# A playable file's path contains one of these format tokens (often as
# "NAME.tap.zip" / "NAME.z80"). Matching the TOKEN (not just ".zip") skips the AY
# music, inlays and screenshots that also live in additionalDownloads as .zip.
PLAY_TOKENS = (".tap", ".tzx", ".z80", ".sna", ".trd", ".scl",
               ".dsk", ".szx", ".udi", ".fdi")
LETTERS = ["0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
SECTIONS = ["Games"]


class WosAdapter(Adapter):
    id = "wos"
    name = "World of Spectrum"

    def __init__(self):
        self._client = httpx.Client(
            timeout=30.0, follow_redirects=True,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        self._index_cache: tuple[float, dict[str, list[Entry]]] | None = None

    @staticmethod
    def _clean(s: str) -> str:
        return (s or "").replace("\t", " ").replace("\r", " ").replace("\n", " ") \
                        .replace("/", "_").strip()

    @staticmethod
    def _bucket(title: str) -> str:
        c = title[:1].upper()
        return c if ("A" <= c <= "Z") else "0-9"

    def _index(self) -> "dict[str, list[Entry]]":
        """All playable games bucketed A-Z, via /v3/search (the only endpoint that
        returns releases[].files[]). byletter lacks releases; the search ES window
        caps at 10000, so we page the first ~10000 SOFTWARE/GAMES — partial but huge
        coverage in ~20 requests instead of one detail fetch per game. Cached."""
        if self._index_cache and self._index_cache[0] > time.time():
            return self._index_cache[1]
        buckets: dict[str, list[Entry]] = {l: [] for l in LETTERS}
        seen: dict[str, set[str]] = {l: set() for l in LETTERS}
        files = 0
        offset, total = 0, 10000
        while offset < total and offset < 10000:
            size = min(PAGE, 10000 - offset)         # ES window: offset+size <= 10000
            url = (f"{API}/search?contenttype=SOFTWARE&genretype=GAMES"
                   f"&mode=full&size={size}&offset={offset}")
            # The API is flaky (intermittent 503) — retry a page instead of aborting
            # the whole crawl on the first hiccup (that left us with only page 1).
            data = None
            for attempt in range(5):
                try:
                    r = self._client.get(url)
                    if r.status_code >= 500:
                        time.sleep(1.0 + attempt)
                        continue
                    r.raise_for_status()
                    data = r.json()
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(1.0 + attempt)
            if data is None:
                print(f"  wos: giving up at offset {offset} after retries")
                break
            time.sleep(0.2)  # be polite between pages
            hh = data.get("hits", {})
            tot = hh.get("total", {})
            total = tot.get("value", 0) if isinstance(tot, dict) else (tot or 0)
            hits = hh.get("hits", []) or []
            if not hits:
                break
            for hit in hits:
                src = hit.get("_source", hit)
                title = self._clean(src.get("title", "")) or str(hit.get("_id", ""))
                letter = self._bucket(title)
                for rel in (src.get("releases") or []):
                    for f in (rel.get("files") or []):
                        path = f.get("path", "")
                        low = path.lower()
                        # Only files actually hosted under /pub/ are downloadable;
                        # /denied/ (rights-removed) etc. are skipped.
                        if not low.startswith("/pub/") or not any(t in low for t in PLAY_TOKENS):
                            continue
                        base = path.rsplit("/", 1)[-1]
                        name = title
                        if name in seen[letter]:  # multiple files for one game
                            stem = base.rsplit(".", 1)[0]
                            name = f"{title} ({stem})"
                            i = 2
                            while name in seen[letter]:
                                name = f"{title} ({stem}) {i}"
                                i += 1
                        seen[letter].add(name)
                        buckets[letter].append(Entry(False, name, f.get("size", 0) or 0,
                                                     url=FILE_BASE + path))
                        files += 1
            offset += len(hits)
            if offset >= total:
                break
        print(f"  wos: {files} playable files across {sum(1 for b in buckets.values() if b)} letters "
              f"(of {total} games, ES window 10000)")
        self._index_cache = (time.time() + CACHE_TTL, buckets)
        return buckets

    # ── RemoteFs surface ──────────────────────────────────────────────────────--
    def list(self, path: str) -> list[Entry]:
        if not path:
            return [Entry(True, s, 0) for s in SECTIONS]
        seg = path.split("/")
        if seg[0] == "Games":
            if len(seg) == 1:
                return [Entry(True, l, 0) for l in LETTERS]
            return self._index().get(seg[1], [])
        return []

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """Dynamic /v1 server only: download the entry's URL as-is."""
        url = next((e.url for e in self.list(path)
                    if not e.is_dir and e.name == name and e.url), "")
        if not url:
            raise FileNotFoundError(name)
        return self._client.get(url).content, url.rsplit("/", 1)[-1]
