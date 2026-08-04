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
bucketing each by the title's first character (0-9 + A-Z + Russian for Cyrillic
titles), like the vtrd/SC trees.
Every file Entry carries the direct release URL → the static exporter emits it in
link mode (the device downloads the .zip itself; nothing is mirrored).

Built once per catalog run (cron Action), so the ~165 API calls are amortised.
"""

from __future__ import annotations

import html
import sys
import time

import httpx

from .base import Adapter, Entry

API = "https://zxart.ee/api"


class _EmptyExport(Exception):
    """Empty-body 200 or HTTP 5xx — a poisoned record inside the window."""
PAGE = 1000                       # limit:5000 errors; 1000 is safe + reliable
SECTIONS = ("Games", "Demoscene")  # categoriesString first segment
LETTERS = ["Russian", "0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
# Download-format preference: native TR-DOS first, then tape, then snapshots.
FMT_RANK = {"trd": 0, "scl": 1, "fdi": 2, "udi": 3, "dsk": 4,
            "tap": 5, "tzx": 6, "z80": 7, "sna": 8}


def _bucket(title: str) -> str:
    c = title[:1].upper()
    if "A" <= c <= "Z":
        return c
    if "А" <= c <= "Я" or c == "Ё":   # Cyrillic titles get their own shelf
        return "Russian"
    return "0-9"                       # digits + everything else (Ø, É, quotes…)


class ZxartAdapter(Adapter):
    id = "zxart"
    name = "ZX-Art"

    def __init__(self):
        # A cold (uncached) 1000-row export page takes the server 30+ s to
        # generate — a 30 s timeout made every cold page look like a failure
        # and silently truncated the crawl (hw-hit: catalog stopped at release
        # id ~447832 of ~601k, dropping every prod newer than ~2023).
        self._client = httpx.Client(
            headers={"User-Agent": "pico-spec-catalog/1.0"},
            timeout=120.0, follow_redirects=True,
        )
        # section -> letter -> [Entry]; built lazily on first list().
        self._index: dict[str, dict[str, list[Entry]]] | None = None

    # ── API helpers ──────────────────────────────────────────────────────────--
    def _get(self, url: str) -> dict:
        err: Exception | None = None
        for attempt in range(5):
            try:
                r = self._client.get(url)
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001 — transient API hiccup, retry
                # A 5xx here is (empirically) as deterministic as the empty
                # body: zxProd windows around offset 43000 500-error until the
                # poisoned record is excluded. Send it to the bisect path; a
                # genuinely transient 5xx just re-resolves in the sub-windows.
                status = (getattr(getattr(e, "response", None), "status_code", None)
                          or getattr(e, "code", None))
                if isinstance(status, int) and status >= 500:
                    raise _EmptyExport(url) from e
                err = e
                time.sleep(2.0 + 2.0 * attempt)
                continue
            try:
                return r.json()
            except Exception as e:  # noqa: BLE001
                # HTTP 200 with an empty/unparseable body: the server-side
                # export serializer died on a poisoned record inside this
                # window. Deterministic — retrying the same window won't help,
                # the caller must bisect around the record instead.
                raise _EmptyExport(url) from e
        # Never mask a failed page as an empty one: an empty result ends the
        # paging loop, and a truncated catalog would silently replace the full
        # one on Pages. Raising fails the whole build instead (previous deploy
        # stays live).
        raise RuntimeError(f"zxart API failed after retries: {url}: {err!r}")

    def _window(self, entity: str, start: int, count: int) -> tuple[int, list]:
        """Rows [start, start+count) + totalAmount; bisects around poisoned rows.

        Some records crash the server's export serializer (HTTP 200, empty
        body — hw-found 2026-08-04 at zxRelease offset 89084, a release id in
        448216..448222), killing the whole window deterministically. Every
        nightly build died on that same page, which is what truncated the
        published catalog at release id 447832. Split the window down to the
        single bad record and skip just it."""
        for retry in (False, True):
            try:
                d = self._get(f"{API}/types:{entity}/language:eng/"
                              f"start:{start}/limit:{count}/export:{entity}")
                rows = (d.get("responseData") or {}).get(entity) or []
                return int(d.get("totalAmount", 0) or 0), rows
            except _EmptyExport:
                if count > 1:
                    half = count // 2
                    t1, a = self._window(entity, start, half)
                    t2, b = self._window(entity, start + half, count - half)
                    return (t1 or t2), a + b
        print(f"  ! zxart {entity}: skipping poisoned record at offset {start}",
              file=sys.stderr)
        return 0, []

    def _paged(self, entity: str):
        start = 0
        got = 0
        total = 0
        while True:
            t, rows = self._window(entity, start, PAGE)
            total = t or total
            got += len(rows)
            yield from rows
            start += PAGE
            if total == 0 or start >= total:
                break
        # Pages may legally return slightly fewer rows than `limit` (items
        # hidden from the language:eng view are dropped after slicing, ~1%),
        # and poisoned records are skipped one by one; anything beyond a few
        # percent means real truncation (or a 5xx storm skipping wholesale).
        if got < total * 0.95:
            raise RuntimeError(
                f"zxart {entity}: fetched only {got} of {total} rows — truncated export")

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
