"""TOSEC adapter — the ZX Spectrum TOSEC set on archive.org, cut alphabetically.

Item choice (probed live from a GitHub runner, 2026-08-27): the canonical
`tosec-main` item is login-gated (`access-restricted-item: true`, collection
"loggedin") — anonymous requests get 403 on zip listings and 401 on downloads,
so neither the exporter nor the device could ever read it. The newer per-system
sets (2020-02-18, September-2023) are public but ship ONE multi-GB zip per
section — useless for browsing. `ZXSpectrumTOSECSetV20171101LadyEklipse`
(ZX Spectrum TOSEC Set v2017-11-01) is public AND unpacked: 71k files, one
small zip per title:

    Games/[TAP]/10th Frame (1987)(U.S. Gold).zip       (46060 under Games/)
    Demos/[TRD]/128 (2017-04)(Wishers)[Multimatograf].zip  (2838 under Demos/)

So no HTML scraping at all: the item's metadata API
(`/metadata/<item>` — one JSON with every file's path + size) is the whole
catalog, re-cut here as

    Demos|Games / <FMT> / 0-9|A..Z / <title entries>

with only device-playable formats shown (FORMATS below; [SP]/[MGT]/[IPF]/…
dats are skipped). The entry name is the TOSEC title (zip basename without
the .zip), which carries year/publisher tags.

Device download URLs: `archive.org/download/…` answers 302 and the device's
HttpsGet does not follow redirects (see sc.py), but the datanode path from the
metadata `server`/`dir` fields serves the file DIRECTLY (verified: 206 on
ranged GET, no Location) — the nightly rebuild re-resolves them, so datanode
rebalancing heals itself:

    https://<server><dir>/<urlencoded zip path>?fn=/<NAME>.zip

The trailing `fn` is the s4e trick: the device names the saved file after the
locator's last path segment (HttpCatalogFs::downloadBasename), and the encoded
zip path would otherwise leave a %-escaped mess; nginx ignores the query
string on static files (verified 206). The device downloads the small zip and
unzips it itself, exactly like vtrd links; fetch() (the dynamic /v1 server)
unzips server-side the same way vtrd does.
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from urllib.parse import quote

import httpx

from .base import Adapter, Entry

BASE = "https://archive.org"
ITEM = "ZXSpectrumTOSECSetV20171101LadyEklipse"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 3600

SECTIONS = ["Demos", "Games"]
# TOSEC format dirs the device can play, in picker order ("[TRD]" → "TRD").
FORMATS = ["TRD", "SCL", "TAP", "TZX", "Z80", "SNA", "DSK", "FDI", "SZX", "UDI"]
LETTERS = ["0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
DISK_EXTS = (".trd", ".scl", ".tap", ".tzx", ".z80", ".sna", ".fdi", ".udi",
             ".dsk", ".szx")

_FN_SAFE = re.compile(r"[^A-Za-z0-9._()\[\]-]+")


def _fn_slug(stem: str) -> str:
    """ASCII filename for the ?fn=/ trick. Must stay URL-safe verbatim — the
    device sends the locator unencoded (same contract as s4e)."""
    t = _FN_SAFE.sub("_", stem).strip("_")
    if not any(c.isascii() and c.isalnum() for c in t):
        t = "tosec"
    return f"{t[:80]}.zip"


class TosecAdapter(Adapter):
    id = "tosec"
    name = "TOSEC (archive.org)"

    def __init__(self):
        self._client = httpx.Client(
            timeout=120.0, follow_redirects=True, headers={"User-Agent": UA},
        )
        # (expires, {(section, fmt): {letter: [(name, size, item path)]}})
        self._idx: "tuple[float, dict[tuple[str, str], dict[str, list[tuple[str, int, str]]]]] | None" = None
        self._base = ""            # https://<server><dir> of the item

    # ── the whole catalog from one metadata JSON ─────────────────────────────
    def _index(self) -> "dict[tuple[str, str], dict[str, list[tuple[str, int, str]]]]":
        if self._idx and self._idx[0] > time.time():
            return self._idx[1]
        idx: dict[tuple[str, str], dict[str, list[tuple[str, int, str]]]] = {}
        try:
            r = self._client.get(f"{BASE}/metadata/{ITEM}")
            r.raise_for_status()
            j = r.json()
            server, root = j.get("server", ""), j.get("dir", "")
            if not server:
                raise RuntimeError("metadata carries no server field")
            self._base = f"https://{server}{root}"
            n = 0
            for f in j.get("files", []):
                path = f.get("name", "")
                seg = path.split("/")
                if (len(seg) != 3 or seg[0] not in SECTIONS
                        or not seg[2].lower().endswith(".zip")):
                    continue
                fmt = seg[1][1:-1].upper() if seg[1][:1] == "[" and seg[1][-1:] == "]" else ""
                if fmt not in FORMATS:
                    continue
                stem = seg[2][:-4].replace("\t", " ").strip()
                c = stem[:1].upper()
                letter = c if "A" <= c <= "Z" else "0-9"
                try:
                    size = int(f.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                idx.setdefault((seg[0], fmt), {}).setdefault(letter, []) \
                   .append((stem, size, path))
                n += 1
            for buckets in idx.values():
                for b in buckets.values():   # alphabetical within each letter
                    b.sort(key=lambda t: t[0].lower())
            counts = {s: sum(len(b) for (sec, _), ls in idx.items() if sec == s
                             for b in ls.values()) for s in SECTIONS}
            print(f"  tosec: metadata ok, base={self._base} titles={counts} (kept {n})")
        except Exception as e:  # noqa: BLE001 — degrade to empty listings
            print(f"  tosec: metadata fetch failed: {e}")
            idx = {}
        self._idx = (time.time() + CACHE_TTL, idx)
        return idx

    def _formats(self, section: str) -> "list[str]":
        idx = self._index()
        return [f for f in FORMATS if (section, f) in idx]

    def _letter(self, section: str, fmt: str, letter: str) -> "list[Entry]":
        rows = self._index().get((section, fmt), {}).get(letter, [])
        return [Entry(False, name, size,
                      url=f"{self._base}/{quote(path)}?fn=/{_fn_slug(name)}")
                for name, size, path in rows]

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
        """Dynamic /v1 server only: download the title zip and unzip the first
        disk/tape image (the static device path downloads + unzips on its own)."""
        url = next((e.url for e in self.list(path)
                    if not e.is_dir and e.name == name and e.url), "")
        if not url:
            raise FileNotFoundError(name)
        data = self._client.get(url).content
        if data[:2] == b"PK":
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
                inner = next((n for n in zf.namelist()
                              if n.lower().endswith(DISK_EXTS)), None)
                if inner:
                    return zf.read(inner), inner.split("/")[-1]
            except Exception:  # noqa: BLE001
                pass
        return data, _fn_slug(name)
