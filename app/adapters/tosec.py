"""TOSEC adapter — the ZX Spectrum TOSEC v2023-06-10 set on archive.org.

Item choice (probed live from GitHub runners, 2026-08-27): the canonical
`tosec-main` item is login-gated (`access-restricted-item: true`) — anonymous
zip listings 403 and downloads 401, for the device as much as for the build.
Public mirrors ship the set two ways:

  - per-title zips (`ZXSpectrumTOSECSetV20171101LadyEklipse`) — plain static
    files, but frozen at TOSEC v2017-11-01;
  - one zip per section (`zx_spectrum_tosec_set_september_2023`, ≈TOSEC
    v2023-06-10: Demos.zip 137 MB / 3.3k files, Games.zip 1.8 GB / 46k files) —
    fresher, used here.

archive.org lists a public zip's contents (`GET /download/<item>/<zip>/` →
view_archive.php HTML, 23 MB / 1.4 s for Games.zip) and streams single members
out of it. Members are the RAW playable files ("Games/<Title>/<Title
(year)(pub).tap>"), so the device doesn't even unzip. The tree exposed:

    Demos|Games / <FMT from extension> / 0-9|A..Z / <title entries>

Listing markup (captured live): rows are UNCLOSED `<tr><td><a href=…>name</a>
<td><td>timestamp<td id="size">bytes` — parsed by splitting on `<tr` and never
requiring closing tags. Member hrefs have shipped two shapes over the years
(`…/<zip>/<urlencoded member>` and `view_archive.php?…&file=<member>`); both
are handled. Sizes are the uncompressed byte counts from the `id="size"` cell.

Device download URLs: `archive.org/download/…` answers 302 and the device
doesn't follow redirects (see sc.py), so locators point straight at the
datanode (server/dir from the metadata API, re-resolved every nightly build):

    https://<server>/view_archive.php?archive=<dir>/<Section>.zip
        &file=<urlencoded member>&fn=/<NAME>.<ext>

view_archive.php streams the extracted member CHUNKED with no Content-Length —
fine since the firmware's 2026-07 HttpsGet chunked support (older builds fail
these downloads; every other catalog source is unaffected). The `fn` tail is
the s4e trick: the device names the saved file after the locator's last path
segment (HttpCatalogFs::downloadBasename). fetch() (the dynamic /v1 server)
just proxies the same URL.
"""

from __future__ import annotations

import html as _html
import re
import time
from urllib.parse import quote, unquote

import httpx

from .base import Adapter, Entry

BASE = "https://archive.org"
ITEM = "zx_spectrum_tosec_set_september_2023"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 3600

SECTIONS = ["Demos", "Games"]
# Playable extensions, in picker order — the format level of the tree.
FORMATS = ["TRD", "SCL", "TAP", "TZX", "Z80", "SNA", "DSK", "FDI", "SZX", "UDI"]
LETTERS = ["0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]

_A = re.compile(r'<a\s[^>]*href="([^"]+)"', re.I)
_SIZE = re.compile(r'id="size"[^>]*>\s*([\d,]+)', re.I)
_FN_SAFE = re.compile(r"[^A-Za-z0-9._()\[\]-]+")


def _member_from_href(href: str) -> str:
    """Zip-member path out of a listing anchor; "" for anything else (parent
    links, the whole-zip link, page chrome)."""
    href = _html.unescape(href)
    m = re.search(r"[?&]file=([^&\"']+)", href)
    if m:
        return unquote(m.group(1))
    m = re.search(r"\.zip/(.+?)/?$", href, re.I)
    if m:
        return unquote(m.group(1))
    return ""


def _parse_listing(html: str) -> "list[tuple[str, int]]":
    """(member path, size) rows of a view_archive.php page. The real markup
    leaves <tr>/<td> unclosed, so rows are the text between consecutive <tr."""
    out: list[tuple[str, int]] = []
    for chunk in html.split("<tr")[1:]:
        a = _A.search(chunk)
        if not a:
            continue
        member = _member_from_href(a.group(1))
        if not member or member.endswith("/"):
            continue
        s = _SIZE.search(chunk)
        out.append((member, int(s.group(1).replace(",", "")) if s else 0))
    return out


def _fn_slug(basename: str) -> str:
    """ASCII filename for the &fn=/ trick. Must stay URL-safe verbatim — the
    device sends the locator unencoded (same contract as s4e)."""
    stem, dot, ext = basename.rpartition(".")
    if not dot:
        stem, ext = basename, "bin"
    t = _FN_SAFE.sub("_", stem).strip("_")
    if not any(c.isascii() and c.isalnum() for c in t):
        t = "tosec"
    return f"{t[:80]}.{_FN_SAFE.sub('_', ext)[:8]}"


class TosecAdapter(Adapter):
    id = "tosec"
    name = "TOSEC 2023 (archive.org)"

    def __init__(self):
        # The Games.zip listing is a 23 MB page — generous timeout.
        self._client = httpx.Client(
            timeout=300.0, follow_redirects=True, headers={"User-Agent": UA},
        )
        self._base: "tuple[float, str, str] | None" = None   # (expires, server, dir)
        # section -> (expires, {(fmt, letter): [(name, size, member)]})
        self._idx: "dict[str, tuple[float, dict[tuple[str, str], list[tuple[str, int, str]]]]]" = {}

    # ── item datanode from the metadata API ─────────────────────────────────
    def _datanode(self) -> "tuple[str, str]":
        if self._base and self._base[0] > time.time():
            return self._base[1], self._base[2]
        server = root = ""
        try:
            r = self._client.get(f"{BASE}/metadata/{ITEM}")
            r.raise_for_status()
            j = r.json()
            server, root = j.get("server", ""), j.get("dir", "")
            print(f"  tosec: metadata ok, server={server} dir={root}")
        except Exception as e:  # noqa: BLE001 — degrade to empty listings
            print(f"  tosec: metadata fetch failed: {e}")
        self._base = (time.time() + CACHE_TTL, server, root)
        return server, root

    # ── one section = one zip's member listing, bucketed ────────────────────
    def _section(self, section: str) -> "dict[tuple[str, str], list[tuple[str, int, str]]]":
        hit = self._idx.get(section)
        if hit and hit[0] > time.time():
            return hit[1]
        idx: dict[tuple[str, str], list[tuple[str, int, str]]] = {}
        try:
            r = self._client.get(f"{BASE}/download/{ITEM}/{section}.zip/")
            r.raise_for_status()
            rows = _parse_listing(r.text)
        except Exception as e:  # noqa: BLE001
            print(f"  tosec: {section}.zip listing failed: {e}")
            rows = []
        n = 0
        for member, size in rows:
            base = member.rsplit("/", 1)[-1]
            stem, dot, ext = base.rpartition(".")
            fmt = ext.upper() if dot else ""
            if fmt not in FORMATS:
                continue
            name = stem.replace("\t", " ").strip()
            c = name[:1].upper()
            letter = c if "A" <= c <= "Z" else "0-9"
            idx.setdefault((fmt, letter), []).append((name, size, member))
            n += 1
        for b in idx.values():                 # alphabetical within each letter
            b.sort(key=lambda t: t[0].lower())
        print(f"  tosec {section}: {n} files in {len(idx)} buckets")
        self._idx[section] = (time.time() + CACHE_TTL, idx)
        return idx

    def _formats(self, section: str) -> "list[str]":
        idx = self._section(section)
        present = {fmt for fmt, _ in idx}
        return [f for f in FORMATS if f in present]

    def _letter(self, section: str, fmt: str, letter: str) -> "list[Entry]":
        server, root = self._datanode()
        if not server:
            return []
        arc = quote(f"{root}/{section}.zip", safe="/")
        entries: list[Entry] = []
        seen: set[str] = set()
        for name, size, member in self._section(section).get((fmt, letter), []):
            if name in seen:                   # same stem twice (rare) → number it
                i = 2
                while f"{name} {i}" in seen:
                    i += 1
                name = f"{name} {i}"
            seen.add(name)
            url = (f"https://{server}/view_archive.php?archive={arc}"
                   f"&file={quote(member, safe='')}"
                   f"&fn=/{_fn_slug(member.rsplit('/', 1)[-1])}")
            entries.append(Entry(False, name, size, url=url))
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
        """Dynamic /v1 server only: download the entry's URL as-is (members are
        raw playable files — nothing to unzip)."""
        url = next((e.url for e in self.list(path)
                    if not e.is_dir and e.name == name and e.url), "")
        if not url:
            raise FileNotFoundError(name)
        fn = url.rsplit("&fn=/", 1)[-1] if "&fn=/" in url else name
        return self._client.get(url).content, fn
