"""ZX-Art (https://zxart.ee) adapter — Games + Demoscene via the JSON export API.

Unlike vtrd (HTML scrape) zxart has a clean public JSON API, so no browser UA or
HTML parsing is needed:

  GET /api/types:zxProd/language:eng/start:N/limit:L/export:zxProd
      → productions; each carries `categoriesString` ("Games/Action/…",
        "Demoscene/Intro/…") + `releasesIds` + `title` + `year`.
  GET /api/types:zxRelease/language:eng/start:N/limit:L/export:zxRelease
      → releases; each carries `prodId`, `releaseFormat` (['trd'|'tap'|…]),
        a direct `file` URL (https://zxart.ee/release/id:<id>/<name>) and a
        `releaseStructure` whose root element gives the byte size.

We page through ALL releases once to pick the best-format downloadable per prod,
then page through ALL prods, keeping only the two sections the user asked for and
bucketing each by the title's first character (0-9 + A-Z), like the vtrd/SC trees.
Every file Entry carries the direct release URL → the static exporter emits it in
link mode (the device downloads the .zip itself; nothing is mirrored).

Built once per catalog run (cron Action), so the ~165 API calls are amortised.
"""

from __future__ import annotations

import html
import time

import httpx

from .base import Adapter, Entry

API = "https://zxart.ee/api"
PAGE = 1000                       # limit:5000 errors; 1000 is safe + reliable
SECTIONS = ("Games", "Demoscene")  # categoriesString first segment
LETTERS = ["0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
# Download-format preference: native TR-DOS first, then tape, then snapshots.
FMT_RANK = {"trd": 0, "scl": 1, "fdi": 2, "udi": 3, "dsk": 4,
            "tap": 5, "tzx": 6, "z80": 7, "sna": 8}


def _bucket(title: str) -> str:
    c = title[:1].upper()
    return c if "A" <= c <= "Z" else "0-9"


class ZxartAdapter(Adapter):
    id = "zxart"
    name = "ZX-Art"

    def __init__(self):
        self._client = httpx.Client(
            headers={"User-Agent": "pico-spec-catalog/1.0"},
            timeout=30.0, follow_redirects=True,
        )
        # section -> letter -> [Entry]; built lazily on first list().
        self._index: dict[str, dict[str, list[Entry]]] | None = None

    # ── API helpers ──────────────────────────────────────────────────────────--
    def _get(self, url: str) -> dict:
        for attempt in range(4):
            try:
                r = self._client.get(url)
                r.raise_for_status()
                return r.json()
            except Exception:  # noqa: BLE001 — transient API hiccup, retry
                time.sleep(1.0 + attempt)
        return {}

    def _paged(self, entity: str):
        start = 0
        while True:
            d = self._get(f"{API}/types:{entity}/language:eng/"
                          f"start:{start}/limit:{PAGE}/export:{entity}")
            rows = (d.get("responseData") or {}).get(entity) or []
            if not rows:
                break
            yield from rows
            total = int(d.get("totalAmount", 0) or 0)
            start += PAGE
            if start >= total:
                break

    @staticmethod
    def _clean(s: str) -> str:
        s = html.unescape(s or "")
        return s.replace("\t", " ").replace("\r", " ").replace("\n", " ").replace("/", "_").strip()

    # ── index build ──────────────────────────────────────────────────────────--
    def _best_releases(self) -> dict[int, tuple[str, int]]:
        """prodId -> (direct file URL, byte size) for the best-format release."""
        best: dict[int, tuple[int, str, int]] = {}  # pid -> (rank, url, size)
        for r in self._paged("zxRelease"):
            url = r.get("file") or ""
            pid = r.get("prodId")
            if not url or pid is None:
                continue
            fmts = [str(x).lower() for x in (r.get("releaseFormat") or [])]
            rank = min((FMT_RANK.get(x, 90) for x in fmts), default=90)
            size = 0
            for el in (r.get("releaseStructure") or []):
                if el.get("parentId") == 0:
                    size = int(el.get("size", 0) or 0)
                    break
            cur = best.get(pid)
            if cur is None or rank < cur[0]:
                best[pid] = (rank, url, size)
        return {pid: (url, size) for pid, (rank, url, size) in best.items()}

    def _build(self) -> None:
        if self._index is not None:
            return
        rel = self._best_releases()
        idx: dict[str, dict[str, list[Entry]]] = {s: {l: [] for l in LETTERS} for s in SECTIONS}
        seen: dict[str, set[str]] = {s: set() for s in SECTIONS}

        for p in self._paged("zxProd"):
            sec = (p.get("categoriesString") or "").split("/")[0]
            if sec not in SECTIONS:
                continue
            r = rel.get(p.get("id"))
            if not r:
                continue  # no downloadable release → skip
            url, size = r
            title = self._clean(p.get("title") or "")
            if not title:
                continue
            year = p.get("year")
            name = title
            if name in seen[sec]:  # disambiguate same-named prods by year, then a counter
                name = f"{title} ({year})" if year else title
                i = 2
                while name in seen[sec]:
                    name = f"{title} ({year}) {i}" if year else f"{title} {i}"
                    i += 1
            seen[sec].add(name)
            idx[sec][_bucket(title)].append(Entry(False, name, size, url=url))

        for sec in SECTIONS:
            for l in LETTERS:
                idx[sec][l].sort(key=lambda e: e.name.lower())
        self._index = idx

    # ── Adapter API ──────────────────────────────────────────────────────────--
    def list(self, path: str) -> list[Entry]:
        self._build()
        parts = [s for s in path.split("/") if s]
        if not parts:                                   # root → the two sections
            return [Entry(True, s, 0) for s in SECTIONS]
        sec = parts[0]
        if sec not in SECTIONS:
            return []
        if len(parts) == 1:                             # section → non-empty letters
            return [Entry(True, l, 0) for l in LETTERS if self._index[sec][l]]
        return list(self._index[sec].get(parts[1], [])) # letter → files

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """Dynamic /v1 path: download the entry's release .zip (link mode skips this)."""
        for e in self.list(path):
            if not e.is_dir and e.name == name and e.url:
                r = self._client.get(e.url)
                r.raise_for_status()
                fname = e.url.rstrip("/").split("/")[-1] or (name + ".zip")
                return r.content, fname
        raise FileNotFoundError(name)
