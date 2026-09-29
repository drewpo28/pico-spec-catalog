"""TS-Conf prods adapter — prods.tslabs.info, the TS-Config (ZX Evolution) software shelf.

The TSLabs "prods" site lists the software written for the TS-Config platform —
demos, games, utilities — one Bootstrap card per production on a single page per
section (no pagination, no API, plain nginx, no UA filtering; verified against
the live site 2026-09-06):

    index.php?t=1   Demos       (~42 productions)
    index.php?t=2   Games       (~32)
    index.php?t=4   Examples    (24 — hardware test/effect snippets, ~7 MB of zips;
                                 added 2026-09-29, all runnable: .spg/.trd/.scl/.sna)
    index.php?t=3/5 Utilities / PC Tools — not exposed

Card markup (one <div class=row> per production):

    <a href="index.php?t=1"><span class="… label-warning">Demo</span></a>
    <h2>3BM Outro</h2>
    <h5>By Fishbone Crew</h5>                              (optional group line)
    <dl class="dl-horizontal"><dt>Code</dt><dd>Breeze</dd><dt>Music</dt><dd>…</dd>…</dl>
    <a href="files/fishbone.zip" class="btn …">Download</a>
    [<a href="files/fishbone_src.zip" …>Sources</a>]     (a few cards)
    <span data-modal-iframe="emul/tsconf.php?f=temp%2F…-fishbone.spg&t=spg…">Run Online</span>

Following the house rule (see s4e), the parser keys on the one invariant — the
files/*.zip download link — and walks up to the smallest ancestor holding exactly
one <h2>, then reads the title (h2), the group (h5 "By …") and the credits
(dt/dd) off that card. Class names are never consulted. The FIRST files/ link of
a card is the release; the others are source archives and are ignored. A card
with no files/ link at all (The Tale of Rabbits) is simply not listed.

Every release is a .zip holding the .spg (SpectrumProg — the TS-Conf program
format pico-speccy's `FileSPG::load` runs) plus, now and then, an nfo, a
file_id.diz, screenshots, or sources and data files. Surveyed 2026-09-06 (all
85 zips, 57 MB): most carry exactly one .spg; a few carry several variants
(Otter & Smoker NEOGS/NoFX/TAY, Rustles ENG/RUS, Demorama TS/YM, TS-Fract ×4);
a handful have no .spg but .trd/.scl images (Copter, TSolitaire, Synchronization,
ZX Battle City, the two brightentayle .scl demos); one (TS Game Pack) holds only
a .wmf and is unusable. Unlike the other link-mode sources this one is
MIRRORED: list() downloads every release zip of the section (paced, ~57 MB a
night), keeps the runnable members in memory, and exposes ONE ENTRY PER MEMBER
with its real size; gen_static then calls fetch(), which hands the bytes back
without another request. Mirroring is what lets the catalog pick the right
member and show each variant as its own entry, and spares the device a 10 MB
zip for a 20 KB program (Fractus3D). The programs themselves run from 4 KB to
4 MB (TS-Fract, Demorama), so the mirrored tree is ~60 MB — about the size of
the zips, well inside the Pages budget. "Runnable" = every .spg in the zip, or — when there is
none — every member of the first extension the device's file browser accepts
(.trd, .scl, .tap, …). A zip with nothing runnable is skipped with a log line
rather than published as a dead entry.

Display name = "TITLE  GROUP" (h5) or, without a group line, "TITLE  CODER"
(the Code credit) — e.g. "3BM Outro  Fishbone Crew", "0x7e1  wbc". When a zip
yields several members each gets a "[variant]" suffix: the member's stem with
the stem prefix common to all of them stripped ("Otter & Smoker  ERA … [NEOGS]",
"[NoFX]", "[TAY]"), or the whole stem when stripping would leave a stub.

Tree: Demos/, Games/ and Examples/ at the root, each a flat, alphabetically sorted list.

TLS (device side — the mirrored files come from Pages, this only matters for the
dynamic /v1 server): Let's Encrypt RSA (YR1 → Root YR → ISRG Root X1),
TLS1.2 ECDHE-RSA-AES128-GCM-SHA256 accepted. IPv4 only (no AAAA record).
"""

from __future__ import annotations

import io
import os
import re
import time
import zipfile
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from .base import Adapter, Entry, SourceDown, http_client
from dataclasses import dataclass

BASE = "https://prods.tslabs.info/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 6 * 3600       # a listing costs the whole ~60 MB crawl — keep it a while
REQ_GAP = float(os.environ.get("TSLABS_REQ_GAP", "0.5"))   # seconds between requests

# dir shown on the device → index.php?t=<n>
SECTIONS = [("Demos", 1), ("Games", 2), ("Examples", 4)]

# What the device's file browser (pico-speccy FileUtils DISK_ALLFILE) will open.
# Order = preference: a zip's entries are the members of the FIRST of these
# extensions it contains (.spg is the TS-Conf native; the disk images are how
# a few releases ship instead).
RUNNABLE_EXTS = (".spg", ".trd", ".scl", ".tap", ".tzx", ".sna", ".z80", ".dsk",
                 ".udi", ".fdi", ".td0", ".pzx")
# A crawl that is mostly failing is a blocked/dead site, not a few dead links.
FAIL_RATIO, FAIL_MIN = 0.2, 5
_FN_SAFE = re.compile(r"[^A-Za-z0-9._()-]+")
_WS = re.compile(r"\s+")


def _txt(n: Node | None) -> str:
    return _WS.sub(" ", n.text(separator=" ", strip=True)).strip() if n is not None else ""


def _card_of(a: Node) -> Node | None:
    """Smallest ancestor of the download link holding exactly one <h2>."""
    n = a.parent
    while n is not None and n.tag != "body":
        h2 = n.css("h2")
        if len(h2) == 1:
            return n
        if len(h2) > 1:
            return None            # walked past the card into the list
        n = n.parent
    return None


def _fn_name(member: str) -> str:
    """ASCII, URL-safe basename for the mirrored file: "ANOTHER/another.spg" →
    "another.spg". Spaces and odd characters become '_'."""
    fn = member.replace("\\", "/").rsplit("/", 1)[-1]
    stem, ext = os.path.splitext(fn)
    stem = _FN_SAFE.sub("_", stem).strip("_")[:60] or "prod"
    return stem + ext.lower()


def _pick_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The runnable members of a release zip: all of the first RUNNABLE_EXTS
    extension present, in archive order. Empty when nothing is recognised."""
    files = [i for i in zf.infolist() if not i.is_dir()]
    for ext in RUNNABLE_EXTS:
        hit = [i for i in files if i.filename.lower().endswith(ext)]
        if hit:
            return hit
    return []


def _variants(stems: list[str]) -> list[str]:
    """Per-member "[variant]" tags for a multi-member zip: the stems with their
    common prefix stripped ("Otter&Smocker v1.6 (NEOGS)" … → "NEOGS", "NoFX",
    "TAY"); the whole stem when the shared prefix ends mid-word ("Rustle2.2_ENG",
    "Rustles2.2_RUS") or stripping would leave a stub under 2 chars; "" for a
    single member."""
    if len(stems) < 2:
        return [""] * len(stems)
    lcp = os.path.commonprefix(stems)
    # Cut the prefix back to a word boundary: "Rustle2.2_ENG"/"Rustles2.2_RUS"
    # share "Rustle", which is not a word — keep both stems whole instead.
    while lcp and lcp[-1] not in " _-.([":
        lcp = lcp[:-1]
    cut = [s[len(lcp):].strip(" _-.([").rstrip(")]") for s in stems]
    if any(len(c) < 2 for c in cut):
        return stems
    return cut


@dataclass
class _Card:
    name: str       # "TITLE  GROUP"
    zip_url: str


class TslabsAdapter(Adapter):
    id = "tslabs"
    name = "TS-Conf prods (tslabs.info)"

    def __init__(self):
        self._client = http_client(
            timeout=60.0, follow_redirects=True, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/zip,*/*;q=0.8",
            },
        )
        self._next_req = 0.0
        # section dir -> (expires, entries)
        self._cache: dict[str, tuple[float, list[Entry]]] = {}
        # (section dir, display name) -> (member bytes, mirrored filename)
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

    # ── Index page → cards ───────────────────────────────────────────────────
    def _parse(self, html: str) -> list[_Card]:
        """One card per production with a download link, in page order."""
        tree = HTMLParser(html)
        out: list[_Card] = []
        seen_cards: set[str] = set()
        for a in tree.css("a[href]"):
            href = a.attributes.get("href") or ""
            if not href.startswith("files/"):
                continue
            card = _card_of(a)
            if card is None:
                continue
            key = card.html or ""   # first files/ link per card = the release
                                    # (selectolax hands out a fresh Node per lookup, so
                                    # identity can't key the card — its markup does)
            if key in seen_cards:
                continue
            seen_cards.add(key)
            title = _txt(card.css_first("h2"))
            if not title:
                continue
            group = re.sub(r"^by\s+", "", _txt(card.css_first("h5")), flags=re.I)
            coder = ""
            for dt in card.css("dt"):
                if _txt(dt).lower() == "code":
                    coder = _txt(dt.next)
                    break
            who = group or coder
            name = f"{title}  {who}" if who else title
            out.append(_Card(name.replace("\t", " "), urljoin(BASE, href)))
        return out

    # ── Section → entries (downloads the zips) ───────────────────────────────
    def _section(self, dir_: str) -> list[Entry]:
        hit = self._cache.get(dir_)
        if hit and hit[0] > time.time():
            return hit[1]
        url = f"{BASE}index.php?t={dict(SECTIONS)[dir_]}"
        try:
            cards = self._parse(self._get(url).decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001 — an unloadable shelf is fatal
            raise SourceDown(f"prods.tslabs.info: {url} failed ({e})") from e
        if not cards:
            raise SourceDown(f"prods.tslabs.info: no download cards on {url} — "
                             f"the page markup changed")

        rows: list[tuple[str, int, bytes, str]] = []   # name, size, bytes, mirrored fn
        fails = 0
        for c in cards:
            zip_fn = c.zip_url.rsplit("/", 1)[-1]
            try:
                zf = zipfile.ZipFile(io.BytesIO(self._get(c.zip_url)))
                members = _pick_members(zf)
            except Exception as e:  # noqa: BLE001 — one bad link, or a blocked crawl
                fails += 1
                print(f"  tslabs: skip {zip_fn}: {e}")
                continue
            if not members:
                print(f"  tslabs: skip {zip_fn}: nothing runnable inside")
                continue
            stems = [os.path.splitext(_fn_name(m.filename))[0] for m in members]
            for m, var in zip(members, _variants(stems)):
                name = f"{c.name} [{var}]" if var else c.name
                rows.append((name, m.file_size, zf.read(m), _fn_name(m.filename)))
        if len(cards) >= FAIL_MIN and fails > FAIL_RATIO * len(cards):
            raise SourceDown(f"prods.tslabs.info: {fails} of {len(cards)} release zips "
                             f"failed to download — the site looks down, not the links dead")

        rows.sort(key=lambda r: r[0].casefold())
        entries: list[Entry] = []
        names: set[str] = set()
        fns: set[str] = set()
        for name, size, data, fn in rows:
            if name in names:                      # same title twice → number them
                i = 2
                while f"{name} {i}" in names:
                    i += 1
                name = f"{name} {i}"
            names.add(name)
            if fn in fns:                          # two zips, same inner filename
                stem, ext = os.path.splitext(fn)
                i = 2
                while f"{stem}_{i}{ext}" in fns:
                    i += 1
                fn = f"{stem}_{i}{ext}"
            fns.add(fn)
            self._blobs[(dir_, name)] = (data, fn)
            entries.append(Entry(False, name, size))   # no url: mirrored via fetch()
        print(f"  tslabs {dir_}: {len(cards)} productions, {len(entries)} files")
        self._cache[dir_] = (time.time() + CACHE_TTL, entries)
        return entries

    # ── RemoteFs surface ─────────────────────────────────────────────────────
    def list(self, path: str) -> list[Entry]:
        if not path:
            return [Entry(True, d, 0) for d, _ in SECTIONS]
        if path not in dict(SECTIONS):
            return []
        return self._section(path)

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """The member bytes list() already unpacked, and the filename to save
        them under (ASCII-safe, real extension)."""
        if path not in dict(SECTIONS):
            raise FileNotFoundError(name)
        self._section(path)                        # (re)fill the blobs if expired
        hit = self._blobs.get((path, name))
        if hit is None:
            raise FileNotFoundError(name)
        return hit
