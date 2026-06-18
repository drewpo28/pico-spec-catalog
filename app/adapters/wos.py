"""World of Spectrum (worldofspectrum.org/archive) adapter via the ZXInfo API v3.

Browses ZXDB games by first letter and emits a direct download link per playable
file — the catalog stores name + link (link mode); the device downloads/unzips.

API:  GET https://api.zxinfo.dk/v3/games/byletter/{A..Z}?size=&offset=&mode=compact
      → { hits: { total:{value}, hits: [ { _source: { title,
          additionalDownloads: [ {path,type,format,size}, ... ] } } ] } }
Download `path` is a relative WoS-archive path ("/pub/sinclair/games/.../X.tap.zip"),
served by the worldofspectrum.net mirror → prepend https://worldofspectrum.net.
additionalDownloads also lists inlays/screens/instructions, so we keep only items
whose path ends in a playable/archive extension.

Tree:  Games/<letter>/ → playable files.  (Demos: TODO via /v3/search.)
"""

from __future__ import annotations

import time

import httpx

from .base import Adapter, Entry

API = "https://api.zxinfo.dk/v3"
FILE_BASE = "https://worldofspectrum.net"          # mirror that serves /pub/sinclair/…
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
        self._cache: dict[str, tuple[float, list[Entry]]] = {}  # letter -> (expires, entries)

    @staticmethod
    def _clean(s: str) -> str:
        return (s or "").replace("\t", " ").replace("\r", " ").replace("\n", " ") \
                        .replace("/", "_").strip()

    def _games(self, letter: str) -> list[Entry]:
        c = self._cache.get(letter)
        if c and c[0] > time.time():
            return c[1]
        entries: list[Entry] = []
        seen: set[str] = set()
        seen_ids: set[str] = set()
        offset, total, guard = 0, 1, 0
        while offset < total and guard < 60:
            guard += 1
            url = f"{API}/games/byletter/{letter}?mode=compact&size={PAGE}&offset={offset}"
            try:
                r = self._client.get(url)
                r.raise_for_status()
                data = r.json()
            except Exception:  # noqa: BLE001 — degrade to whatever we have
                break
            hh = data.get("hits", {})
            tot = hh.get("total", {})
            total = tot.get("value", 0) if isinstance(tot, dict) else (tot or 0)
            hits = hh.get("hits", []) or []
            if not hits:
                break
            new_ids = 0
            for hit in hits:
                hid = str(hit.get("_id", ""))
                if hid and hid in seen_ids:  # offset ignored / overlap → skip dup entry
                    continue
                if hid:
                    seen_ids.add(hid)
                    new_ids += 1
                src = hit.get("_source", hit)
                title = self._clean(src.get("title", "")) or str(hit.get("_id", ""))
                for dl in (src.get("additionalDownloads") or []):
                    path = dl.get("path", "")
                    low = path.lower()
                    if not path or not any(tok in low for tok in PLAY_TOKENS):
                        continue
                    absurl = FILE_BASE + (path if path.startswith("/") else "/" + path)
                    base = absurl.rsplit("/", 1)[-1]
                    name = title
                    if name in seen:  # several files for one game → disambiguate
                        stem = base.rsplit(".", 1)[0]
                        name = f"{title} ({stem})"
                        i = 2
                        while name in seen:
                            name = f"{title} ({stem}) {i}"
                            i += 1
                    seen.add(name)
                    entries.append(Entry(False, name, dl.get("size", 0) or 0, url=absurl))
            if new_ids == 0:  # page added nothing new → pagination not advancing
                break
            offset += len(hits)
        print(f"  wos {letter}: {len(entries)} files (of {total} entries)")
        self._cache[letter] = (time.time() + CACHE_TTL, entries)
        return entries

    # ── RemoteFs surface ──────────────────────────────────────────────────────--
    def list(self, path: str) -> list[Entry]:
        if not path:
            return [Entry(True, s, 0) for s in SECTIONS]
        seg = path.split("/")
        if seg[0] == "Games":
            if len(seg) == 1:
                return [Entry(True, l, 0) for l in LETTERS]
            return self._games(seg[1])
        return []

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """Dynamic /v1 server only: download the entry's URL as-is."""
        url = next((e.url for e in self.list(path)
                    if not e.is_dir and e.name == name and e.url), "")
        if not url:
            raise FileNotFoundError(name)
        return self._client.get(url).content, url.rsplit("/", 1)[-1]
