"""World of SAM adapter — worldofsam.org, the SAM Coupé software archive.

Tree exposed to the device:

    Games | Demos | Utilities  /  <letter>  /  <release>      letters: 0-9, A..Z

Everything below was checked against the live site (2026-10-23 — a Drupal 10
site on nginx, no anti-bot wall, bot UAs answered like browsers, IPv4 only):

  - the three menu shelves are one Drupal view filtered by category:

        /products?field_category_target_id=3&page=N    The Games       (≈215)
        /products?field_category_target_id=4&page=N    The Demos       (≈107)
        /products?field_category_target_id=5&page=N    The Utilities   (≈152)

    25 rows a page, a `page=N` pager; a page past the end comes back with no
    rows. Each row is one <tr> whose cells run image, title, author, publisher,
    release year — the row is keyed on its `/products/<slug>` title link and the
    cells are read in order after it; the view's CSS classes are never consulted.
    The fourth menu item, "Missing In Action!" (`/missing`), lists the releases
    the archive has NO file for, so there is nothing there for the device.
  - every release has a detail page `/products/<slug>` with a Download field.
    A downloadable file is Drupal's file formatter,

        <a href="https://www.worldofsam.org/system/files/2021-05/13Out_0.mgt"
           type="application/octet-stream; length=819200">13Out_0.mgt</a>

    (older uploads live under /sites/default/files/dl-NN/ instead) — a plain
    direct link, no redirect, Content-Length set, Accept-Ranges, byte-identical
    with or without a trailing query string. So the parser keys on an <a> whose
    href is a worldofsam.org file with a disk-image extension and reads the size
    off its `type="…; length=N"` attribute. A release whose copyright holder
    declined ("Copyrights Declined") shows the file name in a <span> with a
    padlock and no <a> at all — it is skipped, as are the
    `Download-not-applicable.txt` placeholders (not a disk image) and locked
    releases that end up with nothing (around a third of the Games).
  - formats: `.mgt` and `.sad` (SAM disk images, 819200 bytes for a plain DD
    disk) and `.dsk` (EDSK with SAM geometry — the firmware tells by geometry).
    pico-speccy opens all three in its SAM Coupé core (FileUtils IFACE_SAM);
    pico-spec has no SAM core and does not list this source. Some 40 releases
    are `.zip` bundles. Surveyed 2026-10-23: most wrap one .dsk/.mgt (often with
    a readme), a dozen hold SAM tapes (.tzx/.tap), and a few hold nothing the
    device can run — PC tools (.exe), sources (.asm/.cpp/.s), a .docx, a 256 MB
    .hdf (TimeGal). So a zip's central directory is read with HTTP Range
    requests (two or three small requests, the site answers Content-Range) and
    the zip is listed only when it carries a disk or tape member; the device
    downloads it whole and unzips the member itself, as for rzx/atm.

Link mode: listings only on Pages, the device downloads from worldofsam.org
(TLS: Let's Encrypt RSA, YR2 → ISRG Root X1). The saved file is named after the
locator's last path segment, and many uploads carry spaces and brackets
percent-encoded ("Wizard%20Demo%20%28Original…%29.mgt"), so such a link gets
the `?fn=/<ascii-name>` tail the other adapters use — Drupal ignores the query
string (verified byte-identical) and the device saves under a clean name.

Display name: "TITLE (YEAR)  AUTHOR" — the publisher when there is no author —
plus " [variant]" when a release ships several files: the file's stem with the
title stripped off ("Wizard Demo (2004)  Jiri Vavra [Original - 1Mb Required]",
"Boing! (1992)  … [Deprotected and Fixed]"); a file named exactly after the
title gets no tag, and when two files would still read alike the format tells
them apart ("[DSK]" / "[MGT]", for a zip the member's: "[TAP]").

Knob: SAM_REQ_GAP (env) — minimum seconds between requests (default 0.5); the
full crawl is ≈490 requests, ~4 min.
"""

from __future__ import annotations

import html
import io
import os
import re
import time
import zipfile
from urllib.parse import unquote

from .base import Adapter, Entry, SourceDown, http_client

SITE = "https://www.worldofsam.org"
SHELVES = [("Games", 3), ("Demos", 4), ("Utilities", 5)]
MAX_PAGES = 60                     # the longest shelf has 9 today

# What pico-speccy's SAM core opens. The site also attaches .txt placeholders
# and PDFs; anything outside this set is not a release file.
EXTS = ("mgt", "sad", "dsk", "zip")
# A zip is worth listing when it holds one of these (the device unzips it).
MEMBER_EXTS = ("mgt", "sad", "dsk", "tzx", "tap")

ROW = re.compile(r"<tr\b.*?</tr>", re.I | re.S)
CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
TITLE_LINK = re.compile(r'''<a\s[^>]*href\s*=\s*["'](?:/index\.php)?(/products/[^"'?#]+)["'][^>]*>(.*?)</a>''', re.I | re.S)
# A Drupal file-formatter link: a worldofsam.org file with its size in `type`.
FILE_LINK = re.compile(
    r'''<a\s[^>]*href\s*=\s*["'](?P<href>(?:https?://(?:www\.)?worldofsam\.org)?/(?:system|sites/default)/files/[^"'\s]+?\.(?P<ext>[A-Za-z0-9]+))["'][^>]*>''',
    re.I | re.S)
LENGTH = re.compile(r'''type\s*=\s*["'][^"']*;\s*length=(\d+)''', re.I)
PAGER_LAST = re.compile(r"page=(\d+)")

# Fewer files than this across the whole site means the markup moved, not that
# the archive shrank — refuse rather than publish a gutted tree.
MIN_FILES = 100
# A crawl that is mostly failing is a blocked/dead site, not a few dead links.
FAIL_RATIO, FAIL_MIN = 0.2, 5

_WS = re.compile(r"\s+")
_FN_SAFE = re.compile(r"[^A-Za-z0-9._()-]+")


def _text(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\xa0", " ")
    s = s.replace("/", "-").replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+([,;:)])", r"\1", _WS.sub(" ", s)).strip()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _letter(title: str) -> str:
    """The site's own buckets: the raw first character, digits together as 0-9
    ("The Abbey of Crime" sits under T on worldofsam.org too)."""
    c = title[:1].upper()
    if c.isdigit():
        return "0-9"
    return c if "A" <= c <= "Z" else "0-9"


def _fn_name(href: str) -> str:
    """ASCII, URL-safe basename for the saved file: the percent-decoded upload
    name with spaces and odd characters as '_'."""
    fn = unquote(href.rsplit("/", 1)[-1])
    stem, _, ext = fn.rpartition(".")
    stem = _FN_SAFE.sub("_", stem).strip("_")[:60] or "sam"
    return f"{stem}.{ext.lower()}"


def parse_shelf(page: str) -> list[tuple[str, str, str, str, str]]:
    """(path, title, author, publisher, year) per release row, in page order."""
    out = []
    for row in ROW.findall(page):
        cells = CELL.findall(row)
        hit = None
        for i, c in enumerate(cells):
            m = TITLE_LINK.search(c)
            if m:
                hit = (i, m)
                break
        if hit is None:
            continue
        i, m = hit
        title = _text(m.group(2))
        if not title:
            continue
        rest = [_text(c) for c in cells[i + 1:]] + ["", "", ""]
        author, publisher = rest[0], rest[1]
        ym = re.search(r"\b(19\d\d|20\d\d)\b", rest[2])
        year = ym.group(1) if ym else ""
        out.append((m.group(1), title, author, publisher, year))
    return out


def parse_files(page: str) -> list[tuple[str, int]]:
    """(absolute href, size) of every disk-image download on a detail page."""
    out = []
    seen = set()
    for m in FILE_LINK.finditer(page):
        if m.group("ext").lower() not in EXTS:
            continue
        href = m.group("href")
        if href.startswith("/"):
            href = SITE + href
        if href in seen:
            continue
        seen.add(href)
        lm = LENGTH.search(m.group(0))
        out.append((href, int(lm.group(1)) if lm else 0))
    return out


def _variant(title: str, href: str, keep_num: bool = False) -> str:
    """The part of a file's name that is not the release title: "Boing (Deprotected
    and Fixed).dsk" under "Boing!" → "Deprotected and Fixed"; "" when the file is
    named just after the title ("boing.dsk", "BattleZone.dsk"). A trailing "_N"
    is Drupal's re-upload suffix ("13Out_0.mgt") and dropped unless `keep_num`."""
    stem = unquote(href.rsplit("/", 1)[-1]).rpartition(".")[0]
    if not keep_num:
        stem = re.sub(r"_\d+$", "", stem)
    stem = re.sub(r"\.(mgt|sad|dsk|tzx|tap)_?$", "", stem, flags=re.I)   # "x.mgt.zip"
    nt = _norm(title)
    ns = _norm(stem)
    if nt and ns.startswith(nt):
        if len(ns) == len(nt):
            return ""
        # strip the title prefix from the stem, keeping the stem's own spelling
        i, k = 0, 0
        while i < len(stem) and k < len(nt):
            if stem[i].lower() == nt[k]:
                k += 1
            i += 1
        stem = stem[i:]
    stem = stem.replace("[", "(").replace("]", ")")   # the tag is shown in brackets
    tag = _text(stem).strip(" -_")
    if tag.startswith("(") and tag.endswith(")") and tag.count("(") == 1:
        tag = tag[1:-1].strip()                          # "(Deprotected and Fixed)"
    return tag


def _variants(title: str, files: list[tuple[str, int, str]]) -> list[str]:
    """One tag per file of a multi-file release; two files that would read alike
    (same stripped stem — "BackToThePast.mgt" and "BackToThePast.dsk") get the
    format appended instead."""
    tags = [_variant(title, href) for href, _, _ in files]
    if len(set(tags)) < len(tags):                       # "lerm_samtape_2" / "_3"
        tags = [_variant(title, href, keep_num=True) for href, _, _ in files]
    if len(set(tags)) < len(tags):
        tags = [f"{t} {kind.upper()}".strip() for t, (_, _, kind) in zip(tags, files)]
    return tags


class _RangeFile(io.RawIOBase):
    """A seekable read-only file over HTTP Range requests, for zipfile to read a
    central directory without downloading the archive."""

    def __init__(self, fetch, url: str, size: int):
        self._fetch, self._url, self._size, self._pos = fetch, url, size, 0

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self._pos

    def seek(self, off, whence=0):
        self._pos = {0: off, 1: self._pos + off, 2: self._size + off}[whence]
        return self._pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self._size - self._pos
        n = min(n, self._size - self._pos)
        if n <= 0:
            return b""
        data = self._fetch(self._url, self._pos, self._pos + n - 1)[:n]
        self._pos += len(data)
        return data

    def readinto(self, b):
        d = self.read(len(b))
        b[:len(d)] = d
        return len(d)


class SamAdapter(Adapter):
    id = "sam"
    name = "World of SAM"

    def __init__(self):
        self._client = http_client(
            headers={"User-Agent": "Mozilla/5.0 pico-spec-catalog/1.0"},
            timeout=30.0, follow_redirects=True,
        )
        self._gap = float(os.environ.get("SAM_REQ_GAP", "0.5"))
        self._last = 0.0
        self._tree: dict[str, list[Entry]] | None = None

    def _get(self, url: str, *, fatal: bool = True) -> bytes | None:
        err = None
        for attempt in range(4):
            wait = self._last + self._gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                r = self._client.get(url)
                self._last = time.monotonic()
                if r.status_code == 404 and not fatal:
                    return None
                r.raise_for_status()
                return r.content
            except Exception as e:  # noqa: BLE001 — retried, then reported
                self._last = time.monotonic()
                err = e
                time.sleep(2 * (attempt + 1))
        if fatal:
            raise SourceDown(f"sam: {url}: {err}")
        return None

    def _range(self, url: str, start: int, end: int) -> bytes:
        wait = self._last + self._gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            r = self._client.get(url, headers={"Range": f"bytes={start}-{end}"})
        finally:
            self._last = time.monotonic()
        r.raise_for_status()
        if r.status_code != 206:
            raise RuntimeError(f"no Range support ({r.status_code})")
        return r.content

    def _zip_kind(self, url: str, size: int) -> str:
        """The extension of the member the device will run out of a zip bundle
        (the first MEMBER_EXTS present, shallowest first), "" when there is none
        or the archive cannot be read."""
        try:
            with zipfile.ZipFile(_RangeFile(self._range, url, size)) as zf:
                names = [i.filename for i in zf.infolist() if not i.is_dir()
                         and not i.filename.startswith("__MACOSX/")]
        except Exception as e:  # noqa: BLE001 — a bad zip is a skipped entry, not a dead site
            print(f"  ! sam: {url}: {e}")
            return ""
        for ext in MEMBER_EXTS:
            if any(n.lower().endswith("." + ext) for n in names):
                return ext
        return ""

    def _shelf(self, cat: int) -> list[tuple[str, str, str, str, str]]:
        rows: list[tuple[str, str, str, str, str]] = []
        seen: set[str] = set()
        for page in range(MAX_PAGES):
            body = self._get(f"{SITE}/products?field_category_target_id={cat}&page={page}")
            found = parse_shelf(body.decode("utf-8", "replace"))
            new = [r for r in found if r[0] not in seen]
            if not new:
                break
            for r in new:
                seen.add(r[0])
            rows.extend(new)
            last = max((int(x) for x in PAGER_LAST.findall(body.decode("utf-8", "replace"))), default=0)
            if page >= last:
                break
        return rows

    def _load(self) -> dict[str, list[Entry]]:
        if self._tree is not None:
            return self._tree
        tree: dict[str, list[Entry]] = {}
        total = failed = fetched = 0
        files_by_path: dict[str, list[tuple[str, int, str]] | None] = {}
        for shelf, cat in SHELVES:
            rows = self._shelf(cat)
            if not rows:
                raise SourceDown(f"sam: shelf {shelf} came back empty — markup changed?")
            buckets: dict[str, list[Entry]] = {}
            for path, title, author, publisher, year in rows:
                if path not in files_by_path:
                    fetched += 1
                    body = self._get(SITE + path, fatal=False)
                    if body is None:
                        failed += 1
                        files_by_path[path] = None
                        continue
                    found = []
                    for href, size in parse_files(body.decode("utf-8", "replace")):
                        kind = href.rsplit(".", 1)[-1].lower()
                        if kind == "zip":
                            kind = self._zip_kind(href, size) if size else ""
                            if not kind:
                                continue          # nothing in it the device can run
                        found.append((href, size, kind))
                    files_by_path[path] = found
                files = files_by_path[path]
                if not files:
                    continue
                who = author or publisher
                base = f"{title} ({year})" if year else title
                if who:
                    base = f"{base}  {who}"
                names = buckets.setdefault(_letter(title), [])
                taken = {e.name for e in names}
                tags = _variants(title, files) if len(files) > 1 else [""]
                for (href, size, _), tag in zip(files, tags):
                    name = f"{base} [{tag}]" if tag else base
                    if name in taken:
                        n = 2
                        while f"{name} #{n}" in taken:
                            n += 1
                        name = f"{name} #{n}"
                    taken.add(name)
                    fn = _fn_name(href)
                    url = href if href.rsplit("/", 1)[-1] == fn else f"{href}?fn=/{fn}"
                    names.append(Entry(is_dir=False, name=name, size=size, url=url))
                    total += 1
            for letter in buckets:
                buckets[letter].sort(key=lambda e: e.name.lower())
            tree[shelf] = [Entry(is_dir=True, name=l) for l in sorted(buckets)]
            for letter, entries in buckets.items():
                tree[f"{shelf}/{letter}"] = entries
        if failed >= FAIL_MIN and failed > FAIL_RATIO * fetched:
            raise SourceDown(f"sam: {failed}/{fetched} detail pages failed — site down or blocked")
        if total < MIN_FILES:
            raise SourceDown(f"sam: only {total} files parsed — markup changed?")
        self._tree = tree
        return tree

    def list(self, path: str) -> list[Entry]:
        tree = self._load()
        if path == "":
            return [Entry(is_dir=True, name=s) for s, _ in SHELVES if tree.get(s)]
        return tree.get(path, [])

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        # Link mode normally avoids this; provided so --no-link mirroring works.
        for e in self._load().get(path, []):
            if e.name == name:
                data = self._get(e.url)
                return data, e.url.rsplit("/", 1)[-1]
        raise FileNotFoundError(name)
