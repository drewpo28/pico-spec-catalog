"""VGMRips adapter — VGM music packs from vgmrips.net, by sound chip.

vgmrips.net catalogs VGM rips as *packs* (one game/system = one pack of tracks)
and tags every pack with the sound chips it drives. pico-spec cares only about
the chips it can play, so the tree exposed to the device is:

    <chip> / <pack title> / <track>.vgm

with the chip level fixed to the supported list (picker order):

    2xSAA1099  2xSN76489  SAA1099  SN76489  YM2413  YM3812  YMF262

("2x…" is vgmrips' own tag for dual-chip rips — a separate chip slug, e.g.
/packs/chip/2xsn76489, not a variant of the single-chip page.)

How each level is resolved (HTML scrape — vgmrips has no public API):

  - packs of a chip: /packs/chip/<slug> (+ ?p=N pagination), anchors to
    /packs/pack/<pack-slug>;
  - tracks of a pack: the pack's whole-archive ZIP (found on the pack page as
    an /files/…\\.zip link). The member list is read CHEAPLY via two HTTP Range
    requests (EOCD → central directory), so listing a pack does NOT download
    its archive; servers that ignore Range degrade to a full (cached) download.

The tracks themselves are stored on the site as .vgz — a plain GZIP-compressed
.vgm ("распаковать до или после скачивания": the choice made here is BEFORE).
The device's unzipper speaks ZIP, not gzip, so track entries carry NO direct
url on purpose: gen_static then falls into mirror mode, fetch() extracts the
.vgz member from the pack zip and gunzips it, and the Pages tree serves a
ready-to-play .vgm the device just GETs. The dynamic /v1 server does the same
per request. Plain .vgm members (a few old packs) pass through unchanged.

The whole pack zip is downloaded only inside fetch() and kept in a single-slot
cache — the exporter mirrors a directory's files right after listing it, so
one download serves every track of the pack.

Selectors are best-effort per the repo's scraping contract (this build
environment cannot reach vgmrips.net, so they are written for the known packs
UI and must be tuned against the live markup if the site changes; the adapter
degrades to empty listings rather than failing the build).

Knobs: VGM_MAX_PACKS (env) caps packs kept per chip, 0/unset = all.
"""

from __future__ import annotations

import gzip
import html as _html
import io
import os
import re
import struct
import time
import zipfile

import httpx

from .base import Adapter, Entry

BASE = "https://vgmrips.net"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 3600
MAX_PAGES = 60                # hard cap on pagination walks per chip
EOCD_TAIL = 65557             # max EOCD record + zip comment

# device dir name -> candidate chip slugs on the site, tried in order
# (lowercase is the expected form; the verbatim tag is a fallback).
CHIPS: "list[tuple[str, list[str]]]" = [
    ("2xSAA1099", ["2xsaa1099", "2xSAA1099"]),
    ("2xSN76489", ["2xsn76489", "2xSN76489"]),
    ("SAA1099",   ["saa1099", "SAA1099"]),
    ("SN76489",   ["sn76489", "SN76489"]),
    ("YM2413",    ["ym2413", "YM2413"]),
    ("YM3812",    ["ym3812", "YM3812"]),
    ("YMF262",    ["ymf262", "YMF262"]),
]

_PACK_A = re.compile(
    r'''href=["'](?:https?://(?:www\.)?vgmrips\.net)?/packs/pack/([^"'/?#]+)/?["'][^>]*>(.*?)</a>''',
    re.I | re.S)
_PAGE_P = re.compile(r'''href=["'][^"']*[?&](?:amp;)?p=(\d+)[^"']*["']''', re.I)
_ZIP_A = re.compile(r'''href=["']((?:https?://[^"']*)?/files/[^"']+\.zip)["']''', re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_FN_SAFE = re.compile(r"[^A-Za-z0-9._()-]+")


def _text(markup: str) -> str:
    """Anchor body -> plain title (tags stripped, entities decoded)."""
    return _WS.sub(" ", _html.unescape(_TAGS.sub(" ", markup))).strip()


def _fn_slug(stem: str) -> str:
    """ASCII download filename — written verbatim into the Pages tree and the
    locator URL, and the device sends locators unencoded (same contract as
    s4e/tosec)."""
    t = _FN_SAFE.sub("_", stem).strip("_")
    if not any(c.isascii() and c.isalnum() for c in t):
        t = "vgm"
    return t[:80] + ".vgm"


def _parse_cd(cd: bytes) -> "list[tuple[str, int]]":
    """(member name, uncompressed size) rows of a raw zip central directory.
    Names decode per-entry: UTF-8 when general-purpose flag bit 11 is set,
    else cp437 — matching what zipfile.ZipFile does, so members listed here
    resolve verbatim in fetch()'s full-zip extraction."""
    out: list[tuple[str, int]] = []
    i, n = 0, len(cd)
    while i + 46 <= n and cd[i:i + 4] == b"PK\x01\x02":
        (flags,) = struct.unpack_from("<H", cd, i + 8)
        (usize,) = struct.unpack_from("<I", cd, i + 24)
        nlen, elen, clen = struct.unpack_from("<HHH", cd, i + 28)
        raw = cd[i + 46:i + 46 + nlen]
        name = raw.decode("utf-8" if flags & 0x800 else "cp437", "replace")
        if not name.endswith("/"):
            out.append((name, usize))
        i += 46 + nlen + elen + clen
    return out


class VgmAdapter(Adapter):
    id = "vgm"
    name = "VGMRips music"

    def __init__(self):
        # Pack zips run to tens of MB — generous timeout for fetch().
        self._client = httpx.Client(
            timeout=300.0, follow_redirects=True, headers={"User-Agent": UA},
        )
        self._max_packs = int(os.environ.get("VGM_MAX_PACKS", "0") or "0")
        # chip dir -> (expires, [(pack dir name, pack slug)])
        self._packs: "dict[str, tuple[float, list[tuple[str, str]]]]" = {}
        # pack slug -> (expires, zip url)
        self._zipurl: "dict[str, tuple[float, str]]" = {}
        # pack slug -> (expires, [(display name, zip member, size)])
        self._track: "dict[str, tuple[float, list[tuple[str, str, int]]]]" = {}
        self._zipblob: "tuple[str, bytes] | None" = None   # single-slot zip cache

    # ── packs of a chip (paginated listing scrape) ───────────────────────────
    def _crawl_chip(self, slug: str) -> "dict[str, str]":
        """Ordered {pack slug: title} across the chip's listing pages."""
        order: dict[str, str] = {}
        p, max_p = 1, 1
        while p <= min(max_p, MAX_PAGES):
            r = self._client.get(f"{BASE}/packs/chip/{slug}", params={"p": p})
            r.raise_for_status()
            new = 0
            for m in _PACK_A.finditer(r.text):
                s, t = m.group(1), _text(m.group(2))
                if s not in order:
                    order[s] = t
                    new += 1
                elif t and not order[s]:     # image anchor came first — keep title
                    order[s] = t
            for pm in _PAGE_P.finditer(r.text):
                max_p = max(max_p, int(pm.group(1)))
            if new == 0:                     # page repeated / ran past the end
                break
            p += 1
        return order

    def _chip_packs(self, chip: str) -> "list[tuple[str, str]]":
        hit = self._packs.get(chip)
        if hit and hit[0] > time.time():
            return hit[1]
        slugs = next((c for d, c in CHIPS if d == chip), [])
        order: dict[str, str] = {}
        for cand in slugs:
            try:
                order = self._crawl_chip(cand)
            except Exception as e:  # noqa: BLE001 — degrade to empty listing
                print(f"  vgm {chip}: chip page ({cand}) failed: {e}")
                order = {}
            if order:
                break
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for s, t in order.items():
            # '/' is the path separator on the wire; keep titles single-segment.
            name = _WS.sub(" ", (t or s).replace("/", "-")).strip()[:80] or s
            if name in seen:
                i = 2
                while f"{name} {i}" in seen:
                    i += 1
                name = f"{name} {i}"
            seen.add(name)
            out.append((name, s))
            if self._max_packs and len(out) >= self._max_packs:
                break
        print(f"  vgm {chip}: {len(out)} packs")
        self._packs[chip] = (time.time() + CACHE_TTL, out)
        return out

    # ── pack zip: url, cheap member listing, cached full download ───────────
    def _zip_url(self, slug: str) -> str:
        hit = self._zipurl.get(slug)
        if hit and hit[0] > time.time():
            return hit[1]
        r = self._client.get(f"{BASE}/packs/pack/{slug}")
        r.raise_for_status()
        m = _ZIP_A.search(r.text)
        if not m:
            raise FileNotFoundError(f"no zip link on pack page: {slug}")
        url = _html.unescape(m.group(1))
        if url.startswith("/"):
            url = BASE + url
        self._zipurl[slug] = (time.time() + CACHE_TTL, url)
        return url

    def _zip_bytes(self, url: str) -> bytes:
        if self._zipblob and self._zipblob[0] == url:
            return self._zipblob[1]
        r = self._client.get(url)
        r.raise_for_status()
        self._zipblob = (url, r.content)
        return r.content

    def _members(self, url: str) -> "list[tuple[str, int]]":
        """(member, uncompressed size) of the zip at `url` WITHOUT downloading
        it when possible: suffix Range for the EOCD, one more Range for the
        central directory. Falls back to a full (cached) download when the
        server ignores Range or the zip needs zip64 fields."""
        blob = self._zipblob[1] if self._zipblob and self._zipblob[0] == url else None
        if blob is None:
            r = self._client.get(url, headers={"Range": f"bytes=-{EOCD_TAIL}"})
            r.raise_for_status()
            if r.status_code == 206:
                tail = r.content
                i = tail.rfind(b"PK\x05\x06")
                if i >= 0 and i + 22 <= len(tail):
                    cdsize, cdoff = struct.unpack_from("<II", tail, i + 12)
                    if 0 < cdsize < 0xFFFFFFFF and cdoff < 0xFFFFFFFF:
                        rc = self._client.get(
                            url, headers={"Range": f"bytes={cdoff}-{cdoff + cdsize - 1}"})
                        rc.raise_for_status()
                        if rc.status_code == 206 and len(rc.content) == cdsize:
                            return _parse_cd(rc.content)
                # odd EOCD / zip64 / short read → full download below
            else:
                blob = r.content                  # Range ignored: got the whole zip
                self._zipblob = (url, blob)
        if blob is None:
            blob = self._zip_bytes(url)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            return [(zi.filename, zi.file_size) for zi in z.infolist() if not zi.is_dir()]

    def _tracks(self, slug: str) -> "list[tuple[str, str, int]]":
        hit = self._track.get(slug)
        if hit and hit[0] > time.time():
            return hit[1]
        out: list[tuple[str, str, int]] = []
        try:
            members = self._members(self._zip_url(slug))
        except Exception as e:  # noqa: BLE001 — degrade to empty listing
            print(f"  vgm: pack {slug}: listing failed: {e}")
            members = []
        seen: set[str] = set()
        for member, usize in members:            # zip order == track order
            base = member.rsplit("/", 1)[-1]
            stem, dot, ext = base.rpartition(".")
            if not dot or ext.lower() not in ("vgz", "vgm"):
                continue                          # skip .txt / .png / .m3u extras
            disp = _WS.sub(" ", stem.replace("\t", " ")).strip() or "track"
            name = disp + ".vgm"
            if name in seen:
                i = 2
                while f"{disp} {i}.vgm" in seen:
                    i += 1
                name = f"{disp} {i}.vgm"
            seen.add(name)
            # size is the stored .vgz (best-effort; the exporter overwrites
            # mirrored entries with the exact gunzipped size)
            out.append((name, member, usize))
        self._track[slug] = (time.time() + CACHE_TTL, out)
        return out

    def _pack_slug(self, chip: str, packdir: str) -> str:
        slug = next((s for n, s in self._chip_packs(chip) if n == packdir), "")
        if not slug:
            raise FileNotFoundError(f"{chip}/{packdir}")
        return slug

    # ── RemoteFs surface ─────────────────────────────────────────────────────
    def list(self, path: str) -> list[Entry]:
        if not path:
            return [Entry(True, d, 0) for d, _ in CHIPS]
        seg = path.split("/")
        if all(d != seg[0] for d, _ in CHIPS):
            return []
        if len(seg) == 1:
            return [Entry(True, n, 0) for n, _ in self._chip_packs(seg[0])]
        if len(seg) == 2:
            try:
                slug = self._pack_slug(seg[0], seg[1])
            except FileNotFoundError:
                return []
            # No url on purpose: .vgz needs a gunzip the device doesn't have,
            # so the exporter mirrors fetch()'s ready .vgm bytes instead.
            return [Entry(False, n, sz) for n, _, sz in self._tracks(slug)]
        return []

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        seg = path.split("/")
        if len(seg) != 2:
            raise FileNotFoundError(name)
        slug = self._pack_slug(seg[0], seg[1])
        member = next((m for n, m, _ in self._tracks(slug) if n == name), "")
        if not member:
            raise FileNotFoundError(name)
        with zipfile.ZipFile(io.BytesIO(self._zip_bytes(self._zip_url(slug)))) as z:
            data = z.read(member)
        if data[:2] == b"\x1f\x8b":              # .vgz = gzipped .vgm → unpack here
            data = gzip.decompress(data)
        return data, _fn_slug(member.rsplit("/", 1)[-1].rpartition(".")[0])
