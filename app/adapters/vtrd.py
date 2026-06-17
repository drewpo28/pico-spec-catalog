"""Virtual TR-DOS (https://vtrd.in) adapter.

vtrd.in has no API: it is plain HTML (.php/.htm framesets) and returns HTTP 403 to
non-browser User-Agents, so all access happens here with a real browser UA and a
TTL cache. The static exporter emits the *direct source URL* per file (link mode);
the device downloads + unzips it itself. fetch() (the dynamic /v1 server) does the
same download/unzip server-side.

Tree (sections → sub-index → files), validated against the live site 2026-06-17:
  Games/<letter>/         games.php?t=<a..z|123>        → /gamez/<l>/<NAME>.zip
  GS/                     gs.php                        → /gs/<NAME>.zip (+ others)
  Press/                  press.php?l=1 (A-N) + ?l=2    → /press/<NAME>.zip
  Demoz/<year>/<party>/   demos_top.php → party.php?year=Y → demo.php?party=N
                                                        → /demoz/demoz/<NAME>.zip
Every leaf row is a direct archive link (anchor text = human title); a
release.php?r=<hash> detail page also exists but is not needed. The HTTP layer
treats an empty listing as "nothing here", so selector drift degrades to an empty
directory rather than crashing.
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from .base import Adapter, Entry

BASE = "https://vtrd.in"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 1800  # seconds
DISK_EXTS = (".trd", ".scl", ".tap", ".tzx", ".z80", ".sna", ".fdi", ".udi")
ARCHIVE_EXTS = (".zip",) + DISK_EXTS
LETTERS = ["0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
SECTIONS = ["Games", "Demoz", "Press", "GS"]


class VtrdAdapter(Adapter):
    id = "vtrd"
    name = "Virtual TR-DOS"

    def __init__(self):
        self._client = httpx.Client(
            headers={"User-Agent": UA, "Accept-Language": "en,ru;q=0.8"},
            timeout=20.0, follow_redirects=True,
        )
        self._html_cache: dict[str, tuple[float, str]] = {}  # url -> (expires, text)

    # ── fetching ────────────────────────────────────────────────────────────────
    def _html(self, url: str) -> str:
        c = self._html_cache.get(url)
        if c and c[0] > time.time():
            return c[1]
        try:
            r = self._client.get(url)
            r.raise_for_status()
            text = r.text
        except Exception:  # noqa: BLE001 — degrade to empty (caller yields empty dir)
            text = ""
        self._html_cache[url] = (time.time() + CACHE_TTL, text)
        return text

    @staticmethod
    def _clean(s: str) -> str:
        return s.replace("\t", " ").replace("\r", " ").replace("\n", " ").replace("/", "_").strip()

    # ── generic file scrape (any page whose download anchors are direct archives) ─
    def _files(self, url: str, *, seen: set[str] | None = None) -> list[Entry]:
        if seen is None:
            seen = set()
        entries: list[Entry] = []
        html = self._html(url)
        if not html:
            return entries
        for a in HTMLParser(html).css("a[href]"):
            href = a.attributes.get("href", "")
            low = href.lower()
            if not (low.endswith(".zip") or low.endswith(DISK_EXTS)):
                continue
            absurl = urljoin(url, href)
            base = absurl.rstrip("/").split("/")[-1]
            title = self._clean(a.text() or "") or base
            name = title
            if name in seen:  # duplicate titles → disambiguate with the file stem
                stem = base.rsplit(".", 1)[0]
                name = f"{title} ({stem})"
                i = 2
                while name in seen:
                    name = f"{title} ({stem}) {i}"
                    i += 1
            seen.add(name)
            entries.append(Entry(False, name, 0, url=absurl))
        return entries

    # ── Demoz helpers ─────────────────────────────────────────────────────────--
    def _demoz_years(self) -> list[Entry]:
        html = self._html(f"{BASE}/skin/demos_top.php")
        years: list[str] = []
        for m in re.finditer(r"party\.php\?year=(\d{4})", html):
            if m.group(1) not in years:
                years.append(m.group(1))
        years.sort(reverse=True)  # newest first
        return [Entry(True, y, 0) for y in years]

    def _demoz_parties(self, year: str) -> list[tuple[str, str]]:
        """(party title, party id) for a year, in page order."""
        html = self._html(f"{BASE}/skin/party.php?year={year}")
        out: list[tuple[str, str]] = []
        if not html:
            return out
        for a in HTMLParser(html).css("a[href]"):
            m = re.search(r"demo\.php\?party=(\d+)", a.attributes.get("href", ""))
            if not m:
                continue
            title = self._clean(a.text() or "")
            if title:
                out.append((title, m.group(1)))
        return out

    def _demoz_party_dirs(self, year: str) -> list[Entry]:
        entries: list[Entry] = []
        seen: set[str] = set()
        for title, pid in self._demoz_parties(year):
            name = title if title not in seen else f"{title} #{pid}"
            seen.add(name)
            entries.append(Entry(True, name, 0))
        return entries

    def _demoz_files(self, year: str, party_name: str) -> list[Entry]:
        pid = None
        for title, i in self._demoz_parties(year):
            if title == party_name:
                pid = i
                break
        if pid is None:  # disambiguated "title #id" form
            m = re.search(r"#(\d+)$", party_name)
            if m:
                pid = m.group(1)
        if pid is None:
            return []
        return self._files(f"{BASE}/demo.php?party={pid}")

    # ── RemoteFs surface ──────────────────────────────────────────────────────--
    def list(self, path: str) -> list[Entry]:
        if not path:
            return [Entry(True, s, 0) for s in SECTIONS]
        seg = path.split("/")
        sec = seg[0]
        if sec == "Games":
            if len(seg) == 1:
                return [Entry(True, l, 0) for l in LETTERS]
            t = "123" if seg[1] == "0-9" else seg[1].lower()
            return self._files(f"{BASE}/games.php?t={t}")
        if sec == "GS":
            return self._files(f"{BASE}/gs.php")
        if sec == "Press":
            seen: set[str] = set()
            return (self._files(f"{BASE}/press.php?l=1", seen=seen) +
                    self._files(f"{BASE}/press.php?l=2", seen=seen))
        if sec == "Demoz":
            if len(seg) == 1:
                return self._demoz_years()
            if len(seg) == 2:
                return self._demoz_party_dirs(seg[1])
            return self._demoz_files(seg[1], seg[2])
        return []

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """Dynamic /v1 server only: download the entry's URL and unzip the first
        disk/tape image (the static device path downloads + unzips on its own)."""
        url = next((e.url for e in self.list(path)
                    if not e.is_dir and e.name == name and e.url), "")
        if not url:
            raise FileNotFoundError(name)
        data = self._client.get(url).content
        if data[:2] == b"PK":  # transparently unpack to the first disk/tape image
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
                inner = next((n for n in zf.namelist() if n.lower().endswith(DISK_EXTS)), None)
                if inner:
                    return zf.read(inner), inner.split("/")[-1]
            except Exception:  # noqa: BLE001
                pass
        return data, name
