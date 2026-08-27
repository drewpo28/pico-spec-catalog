"""TOSEC adapter — the Sinclair ZX Spectrum set of the TOSEC dump on archive.org.

archive.org item `tosec-main` mirrors the full TOSEC release; the ZX Spectrum
corner lives under `Sinclair/ZX Spectrum/<Section>/[<FMT>]/` where each format
directory holds ONE huge zip per TOSEC dat, e.g.

    Sinclair/ZX Spectrum/Demos/[TRD]/Sinclair ZX Spectrum - Demos - [TRD]
                                     (TOSEC-v2023-06-13_CM).zip

A single multi-GB zip is useless to the device, but archive.org can serve a
zip's *contents*: `GET /download/<item>/<zip path>/` renders an HTML listing of
the members (view_archive.php), and `/download/<item>/<zip path>/<member>`
streams one extracted member. So the tree exposed here is

    Demos|Games / <FMT> / 0-9|A..Z / <member files>

— the TOSEC packs re-cut as an alphabetical catalog, like the other sources.
Only device-playable formats are shown (FORMATS below); [$B]/[BIN]/[Multipart]
and other non-image dats are skipped. Member names are the TOSEC filenames
("Title (year)(publisher).trd"), which already carry the extension.

Discovery is NOT scraped: the metadata API (`/metadata/tosec-main`) lists every
file in the item as JSON, and also names the datanode (`server`) and item root
(`dir`) — both needed below. Only the per-zip member listing is HTML (there is
no API for zip contents); its parse is tolerant (any <a href> whose target
names a zip member, size = last numeric cell of the row) and degrades to an
empty directory on drift, per the repo's scraping policy.

Device download URLs: `archive.org/download/…` answers 302 → datanode, and the
device's HttpsGet does not follow redirects (see sc.py). So the locator is the
datanode URL directly, resolved at build time from the metadata `server`/`dir`
fields (refreshed every nightly build, so datanode rebalancing heals itself):

    https://<server>/view_archive.php?archive=<dir>/<zip>&file=<member>&fn=/<NAME>

view_archive.php streams the raw extracted member (no zip around it) and
ignores unknown params; the trailing `fn` is the s4e trick — the device names
the saved file after the locator's last path segment
(HttpCatalogFs::downloadBasename), so `fn`'s value starts with '/' and ends
with an ASCII-safe filename. The dynamic /v1 server side (fetch()) hits the
same URL with follow_redirects on.
"""

from __future__ import annotations

import html as _html
import re
import time
from urllib.parse import quote, unquote

import httpx

from .base import Adapter, Entry

BASE = "https://archive.org"
ITEM = "tosec-main"
ROOT = "Sinclair/ZX Spectrum"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 3600

SECTIONS = ["Demos", "Games"]
# TOSEC format dirs the device can play, in picker order ("[TRD]" → "TRD").
FORMATS = ["TRD", "SCL", "TAP", "TZX", "Z80", "SNA", "DSK", "FDI", "SZX", "UDI"]
LETTERS = ["0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_A = re.compile(r"<a\s[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_FN_SAFE = re.compile(r"[^A-Za-z0-9._()\[\]-]+")


def _text(s: str) -> str:
    return _html.unescape(_TAGS.sub("", s)).strip()


def _member_from_href(href: str) -> str:
    """Zip-member path out of a listing anchor. The zip view has shipped two
    link shapes over the years — a datanode `view_archive.php?…&file=<member>`
    and a frontend `/download/<item>/<zip>/<member>` — both URL-encoded. Empty
    for anything else (parent-dir links, the whole-zip link, headers)."""
    href = _html.unescape(href)
    m = re.search(r"[?&]file=([^&\"']+)", href)
    if m:
        return unquote(m.group(1))
    m = re.search(r"\.zip/(.+?)/?$", href, re.I)
    if m:
        return unquote(m.group(1))
    return ""


def _size(cells: "list[str]") -> int:
    """Best-effort byte size: last cell that reads as a number ("123", "1,234")
    or a humanized "123.4K/M/G"."""
    for c in reversed(cells):
        t = _text(c).replace(",", "")
        if t.isdigit():
            return int(t)
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMG])B?", t, re.I)
        if m:
            return int(float(m.group(1)) * {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}[m.group(2).upper()])
    return 0


def _parse_zip_listing(html: str) -> "list[tuple[str, int]]":
    """(member, size) rows of a view_archive.php page. Members with a '/'
    (directories inside the zip — TOSEC packs are flat) are skipped."""
    out: list[tuple[str, int]] = []
    rows = _ROW.findall(html)
    for chunk in (rows if rows else [html]):
        for href, atext in _A.findall(chunk):
            member = _member_from_href(href) or ""
            if not member and rows:
                continue
            name = member or _text(atext)
            if not name or name.endswith("/") or "/" in name:
                continue
            if not member and not re.search(r"\.[A-Za-z0-9$]{1,4}$", name):
                continue  # anchor-text fallback: only filename-shaped text
            out.append((name, _size(_TD.findall(chunk)) if rows else 0))
    return out


def _fn_slug(member: str) -> str:
    """ASCII filename for the &fn=/ trick. Must stay URL-safe verbatim — the
    device sends the locator unencoded (same contract as s4e)."""
    stem, dot, ext = member.rpartition(".")
    if not dot:
        stem, ext = member, "bin"
    t = _FN_SAFE.sub("_", stem).strip("_")
    if not any(c.isascii() and c.isalnum() for c in t):
        t = "tosec"
    return f"{t[:80]}.{_FN_SAFE.sub('_', ext)[:8]}"


class TosecAdapter(Adapter):
    id = "tosec"
    name = "TOSEC (archive.org)"

    def __init__(self):
        # Listings of the Games zips run to several MB — generous timeout.
        self._client = httpx.Client(
            timeout=300.0, follow_redirects=True, headers={"User-Agent": UA},
        )
        # (expires, server, dir, {section: {fmt: [zip paths]}})
        self._meta: "tuple[float, str, str, dict[str, dict[str, list[str]]]] | None" = None
        self._members: "dict[str, tuple[float, list[tuple[str, int]]]]" = {}

    # ── item metadata: zip discovery + datanode ─────────────────────────────
    def _metadata(self) -> "tuple[str, str, dict[str, dict[str, list[str]]]]":
        if self._meta and self._meta[0] > time.time():
            return self._meta[1], self._meta[2], self._meta[3]
        server, root, zips = "", "", {s: {} for s in SECTIONS}
        try:
            r = self._client.get(f"{BASE}/metadata/{ITEM}")
            r.raise_for_status()
            j = r.json()
            server = j.get("server") or j.get("d1") or ""
            root = j.get("dir") or ""
            for f in j.get("files", []):
                name = f.get("name", "")
                if not name.lower().endswith(".zip"):
                    continue
                for sec in SECTIONS:
                    prefix = f"{ROOT}/{sec}/"
                    if not name.startswith(prefix):
                        continue
                    sub = name[len(prefix):].split("/", 1)[0]
                    fmt = sub[1:-1].upper() if sub[:1] == "[" and sub[-1:] == "]" else ""
                    if fmt in FORMATS:
                        zips[sec].setdefault(fmt, []).append(name)
            counts = {s: sum(len(v) for v in zips[s].values()) for s in SECTIONS}
            print(f"  tosec: metadata ok, server={server} zips={counts}")
        except Exception as e:  # noqa: BLE001 — degrade to empty listings
            print(f"  tosec: metadata fetch failed: {e}")
        self._meta = (time.time() + CACHE_TTL, server, root, zips)
        return server, root, zips

    def _formats(self, section: str) -> "list[str]":
        zips = self._metadata()[2].get(section, {})
        return [f for f in FORMATS if f in zips]

    # ── zip member listings ──────────────────────────────────────────────────
    def _zip_members(self, zip_path: str) -> "list[tuple[str, int]]":
        hit = self._members.get(zip_path)
        if hit and hit[0] > time.time():
            return hit[1]
        url = f"{BASE}/download/{ITEM}/{quote(zip_path)}/"
        try:
            r = self._client.get(url)
            r.raise_for_status()
            members = _parse_zip_listing(r.text)
        except Exception as e:  # noqa: BLE001
            print(f"  tosec: {zip_path}: listing failed: {e}")
            members = []
        print(f"  tosec: {zip_path.rsplit('/', 1)[-1]}: {len(members)} members")
        self._members[zip_path] = (time.time() + CACHE_TTL, members)
        return members

    def _letter(self, section: str, fmt: str, letter: str) -> "list[Entry]":
        server, root, zips = self._metadata()
        if not server:
            return []
        entries: list[Entry] = []
        seen: set[str] = set()
        for zp in zips.get(section, {}).get(fmt, []):
            arc = quote(f"{root}/{zp}", safe="/")
            for member, size in self._zip_members(zp):
                c = member[:1].upper()
                if (c if "A" <= c <= "Z" else "0-9") != letter:
                    continue
                name = member.replace("\t", " ")
                if name in seen:                    # two zips, same member name
                    i = 2
                    while f"{name} {i}" in seen:
                        i += 1
                    name = f"{name} {i}"
                seen.add(name)
                url = (f"https://{server}/view_archive.php?archive={arc}"
                       f"&file={quote(member, safe='')}&fn=/{_fn_slug(member)}")
                entries.append(Entry(False, name, size, url=url))
        entries.sort(key=lambda e: e.name.lower())
        return entries

    # ── RemoteFs surface ─────────────────────────────────────────────────────
    def list(self, path: str) -> list[Entry]:
        if not path:
            return [Entry(True, s, 0) for s in SECTIONS]
        seg = path.split("/")
        if seg[0] not in SECTIONS:
            return []
        if len(seg) == 1:
            return [Entry(True, f, 0) for f in self._formats(seg[0])]
        if seg[1] not in FORMATS:
            return []
        if len(seg) == 2:
            return [Entry(True, l, 0) for l in LETTERS]
        if len(seg) == 3 and seg[2] in LETTERS:
            return self._letter(seg[0], seg[1], seg[2])
        return []

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """Dynamic /v1 server only: download the entry's URL as-is."""
        url = next((e.url for e in self.list(path)
                    if not e.is_dir and e.name == name and e.url), "")
        if not url:
            raise FileNotFoundError(name)
        fn = url.rsplit("&fn=/", 1)[-1] if "&fn=/" in url else name
        return self._client.get(url).content, fn
