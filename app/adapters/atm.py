"""ATM-Turbo adapter — atmturbo.nedopc.com, the ATM-Turbo 1/2/2+ software shelf.

The ATM-Turbo home page's "СКАЧАТЬ" hub (atmload.htm) links one static page per
operating system; each is a hand-made cp1251 HTML table, one row per release
(no API, plain nginx, no UA filtering, untouched since 2021; verified against
the live site 2026-09-26):

    load_trdos.htm   TR-DOS     #games / #demos / #system        (~96 zips)
    load_cpm.htm     CP/M       #games / #system / #lang / #stm_mus  (~100)
    load_isdos.htm   iS-DOS     #sys_isd                          (~28)
    load_msx / load_pressa / load_nedoos / load_cdrom / load_pc — not exposed

Row markup (TR-DOS; the others are the same table shape):

    <a name="games"></a>                                  (section marker)
    <td>… <a href="download/trdos/games/pang16c/pang16c.zip">PANG 16 colours (320x200)</a></td>
    <td><a href="download/trdos/games/pang16c/pang16c.htm">здесь</a></td>   (description)
    <td>ATM2,2+</td> <td>73Кб</td> <td><img …screenshots…></td>

Following the house rule (see s4e), the parser never looks at the table layout:
it walks every <a> of the page in document order, an <a name=X> switches the
current section, and every download/….zip link is a release titled by its link
text. The same zip is sometimes linked twice from one cell — a title split over
two links ("Space Mercenary Overseers" + "(demo)(320x200)") or an empty stray
<a> — so links are merged by href and their texts joined. Links outside a known
section (the CP/M page's ATM_HDD.zip above the first anchor) are ignored. The
iS-DOS Games/Demos shelves hold only loose .ipc files, no zips, so only its
system shelf is listed.

The site is HTTP-only — its HTTPS answers with a self-signed certificate — so
the device can't fetch from it and this source is MIRRORED: list() downloads
every zip of the section (paced, ~50 MB a night) and gen_static publishes the
zips as-is on Pages; the device unzips them itself, as for vtrd/alf. The zips
carry the disk image (.trd/.scl, multi-disk .fdi sets for iS-DOS) plus the odd
.inf, file_id.diz or source image.

Display name = the link text + ".zip" ("PANG 16 colours (320x200).zip" — the
video mode in the title is kept, it tells what the program needs). Tree:
TR-DOS/, CP-M/, iS-DOS/ at the root, each holding its shelves; every shelf is
a flat, alphabetically sorted list.
"""

from __future__ import annotations

import os
import re
import time
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from .base import Adapter, Entry, SourceDown, http_client

BASE = "http://atmturbo.nedopc.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 6 * 3600       # a listing costs the whole section's zips — keep it a while
REQ_GAP = float(os.environ.get("ATM_REQ_GAP", "0.5"))   # seconds between requests

# dir shown on the device → (page, {anchor name → shelf dir})
SECTIONS: dict[str, tuple[str, dict[str, str]]] = {
    "TR-DOS": ("load_trdos.htm", {"games": "Games", "demos": "Demos", "system": "System"}),
    "CP-M":   ("load_cpm.htm",   {"games": "Games", "system": "System",
                                  "lang": "Languages", "stm_mus": "Music"}),
    "iS-DOS": ("load_isdos.htm", {"sys_isd": "System"}),
}
# A crawl that is mostly failing is a blocked/dead site, not a few dead links.
FAIL_RATIO, FAIL_MIN = 0.2, 5
_WS = re.compile(r"\s+")
_FN_SAFE = re.compile(r"[^A-Za-z0-9._()-]+")


def _fn_name(href: str) -> str:
    """ASCII, URL-safe basename for the mirrored zip: ".../HCaut401.zip" → "HCaut401.zip"."""
    stem = os.path.splitext(href.rsplit("/", 1)[-1])[0]
    return (_FN_SAFE.sub("_", stem).strip("_")[:60] or "release") + ".zip"


def _parse(html: str, shelves: dict[str, str]) -> dict[str, list[tuple[str, str]]]:
    """shelf dir → [(title, zip url)] in page order, merged by href."""
    out: dict[str, dict[str, list[str]]] = {s: {} for s in shelves.values()}
    shelf: str | None = None
    for a in HTMLParser(html).css("a"):
        at = a.attributes
        if at.get("name"):
            shelf = shelves.get((at["name"] or "").strip().lower())
            continue
        href = (at.get("href") or "").strip()
        if shelf is None or not href.startswith("download/") or not href.lower().endswith(".zip"):
            continue
        texts = out[shelf].setdefault(urljoin(BASE, href), [])
        txt = _WS.sub(" ", a.text(separator=" ", strip=True)).strip()
        if txt:
            texts.append(txt)
    return {s: [(" ".join(t) or _fn_name(u)[:-4], u) for u, t in rel.items()]
            for s, rel in out.items()}


class AtmAdapter(Adapter):
    id = "atm"
    name = "ATM-Turbo (atmturbo.nedopc.com)"

    def __init__(self):
        self._client = http_client(
            timeout=60.0, follow_redirects=True, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/zip,*/*;q=0.8",
            },
        )
        self._next_req = 0.0
        # OS dir -> (expires, shelf -> [(title, zip url)])
        self._pages: dict[str, tuple[float, dict[str, list[tuple[str, str]]]]] = {}
        # "OS/shelf" -> (expires, entries)
        self._cache: dict[str, tuple[float, list[Entry]]] = {}
        # ("OS/shelf", display name) -> (zip bytes, mirrored filename)
        self._blobs: dict[tuple[str, str], tuple[bytes, str]] = {}

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _get(self, url: str) -> bytes:
        """GET with a small gap between requests — a hobby site, not a CDN."""
        wait = self._next_req - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_req = time.monotonic() + REQ_GAP
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self._client.get(url)
                r.raise_for_status()
                return r.content
            except Exception as e:  # noqa: BLE001 — transient, retry with backoff
                last = e
                time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"{url}: {last}")

    # ── OS page → shelves ────────────────────────────────────────────────────
    def _page(self, os_dir: str) -> dict[str, list[tuple[str, str]]]:
        hit = self._pages.get(os_dir)
        if hit and hit[0] > time.time():
            return hit[1]
        page, shelves = SECTIONS[os_dir]
        url = BASE + page
        try:
            parsed = _parse(self._get(url).decode("cp1251", "replace"), shelves)
        except Exception as e:  # noqa: BLE001 — an unloadable page is fatal
            raise SourceDown(f"atmturbo.nedopc.com: {url} failed ({e})") from e
        empty = [s for s, rel in parsed.items() if not rel]
        if empty:
            raise SourceDown(f"atmturbo.nedopc.com: no zip links under {empty} on {url} — "
                             f"the page markup changed")
        self._pages[os_dir] = (time.time() + CACHE_TTL, parsed)
        return parsed

    # ── Shelf → entries (downloads the zips) ─────────────────────────────────
    def _shelf(self, path: str) -> list[Entry]:
        hit = self._cache.get(path)
        if hit and hit[0] > time.time():
            return hit[1]
        os_dir, shelf = path.split("/", 1)
        releases = self._page(os_dir)[shelf]

        rows: list[tuple[str, bytes, str]] = []    # title, zip bytes, mirrored fn
        fails = 0
        for title, url in releases:
            try:
                data = self._get(url)
            except Exception as e:  # noqa: BLE001 — one bad link, or a dead site
                fails += 1
                print(f"  atm: skip {url}: {e}")
                continue
            rows.append((title.replace("\t", " "), data, _fn_name(url)))
        if len(releases) >= FAIL_MIN and fails > FAIL_RATIO * len(releases):
            raise SourceDown(f"atmturbo.nedopc.com: {fails} of {len(releases)} zips in "
                             f"{path} failed to download — the site looks down, not the links dead")

        rows.sort(key=lambda r: r[0].casefold())
        entries: list[Entry] = []
        names: set[str] = set()
        fns: set[str] = set()
        for title, data, fn in rows:
            name = f"{title}.zip"
            if name in names:                      # same title twice → number them
                i = 2
                while f"{title} ({i}).zip" in names:
                    i += 1
                name = f"{title} ({i}).zip"
            names.add(name)
            if fn in fns:                          # two dirs, same zip filename
                stem = fn[:-4]
                i = 2
                while f"{stem}_{i}.zip" in fns:
                    i += 1
                fn = f"{stem}_{i}.zip"
            fns.add(fn)
            self._blobs[(path, name)] = (data, fn)
            entries.append(Entry(False, name, len(data)))   # no url: mirrored via fetch()
        print(f"  atm {path}: {len(releases)} releases, {len(entries)} zips")
        self._cache[path] = (time.time() + CACHE_TTL, entries)
        return entries

    def _is_shelf(self, path: str) -> bool:
        os_dir, _, shelf = path.partition("/")
        return os_dir in SECTIONS and shelf in SECTIONS[os_dir][1].values()

    # ── RemoteFs surface ─────────────────────────────────────────────────────
    def list(self, path: str) -> list[Entry]:
        if not path:
            return [Entry(True, d, 0) for d in SECTIONS]
        if path in SECTIONS:
            return [Entry(True, s, 0) for s in SECTIONS[path][1].values()]
        if not self._is_shelf(path):
            return []
        return self._shelf(path)

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """The zip bytes list() already downloaded, and the filename to save
        them under (the upstream zip's ASCII basename)."""
        if not self._is_shelf(path):
            raise FileNotFoundError(name)
        self._shelf(path)                          # (re)fill the blobs if expired
        hit = self._blobs.get((path, name))
        if hit is None:
            raise FileNotFoundError(name)
        return hit
