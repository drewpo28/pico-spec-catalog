"""VGMRips adapter — VGM music packs from vgmrips.net, by sound chip.

vgmrips.net catalogs VGM rips as *packs* (one game/system = one pack of
tracks) and tags every pack with the sound chips it drives. pico-spec cares
only about the chips it can play, so the tree exposed to the device is:

    <chip> / <pack title> / <track>.vgm

with the chip level fixed to SAA1099, SN76489, YM2413, YM3812, YMF262.
The site has no separate "2x…" categories (probed live 2026-09-02:
/packs/chip/2xsn76489 is 404 — "2xSN76489" is a per-pack label), so dual-chip
rips simply appear inside their base chip's category. Some chip tags are the
same die on a branded card ("AD-Lib (YM3812)", "Sound Blaster 16 (YMF262)",
"OPLL (YM2413)") — those alias slugs are unioned into the base chip's dir.

Everything below was verified against the live site via the probe workflow
(GitHub runners; this dev sandbox cannot reach vgmrips.net):

  - ANUBIS: the whole site sits behind the Anubis proof-of-work anti-bot
    wall, whose policy challenges browser-like ("Mozilla…") User-Agents on
    every path, /files/ downloads included. An honestly named bot UA is
    passed straight through — so, unlike the other adapters, this one must
    NOT masquerade as a browser.
  - packs of a chip: /packs/chip/<slug> with ?p=N pagination; one absolute
    <a href="https://vgmrips.net/packs/pack/<slug>">Title</a> per pack (plus
    an untitled #autoplay twin that is ignored).
  - tracks of a pack: the pack page carries a DIRECT, urlencoded link per
    track — https://vgmrips.net/packs/vgm/<Section>/<System>/<Pack>/<NN
    Track>.vgz — so no whole-pack zip is touched at all.

Tracks are stored upstream as .vgz — a plain GZIP-compressed .vgm. The
device's unzipper speaks ZIP, not gzip (and its browser-less HTTP stack never
solves Anubis anyway), so track entries carry NO direct url on purpose:
gen_static then falls into mirror mode, fetch() downloads the .vgz and
gunzips it, and the Pages tree serves a ready-to-play .vgm the device just
GETs. The dynamic /v1 server does the same per request. The rare plain .vgm
member passes through unchanged.

Knobs: VGM_MAX_PACKS (env) caps packs kept per chip, 0/unset = all.
"""

from __future__ import annotations

import gzip
import html as _html
import os
import re
import time
from urllib.parse import unquote

import httpx

from .base import Adapter, Entry

BASE = "https://vgmrips.net"
UA = "pico-spec-catalog/1.0 (+https://github.com/drewpo28/pico-spec-catalog)"
CACHE_TTL = 3600
MAX_PAGES = 60                # hard cap on pagination walks per chip slug

# device dir name -> chip slugs on the site whose packs it unions
# (base tag + same-die card aliases, from the live /packs/chips list).
CHIPS: "list[tuple[str, list[str]]]" = [
    ("SAA1099", ["saa1099"]),
    ("SN76489", ["sn76489"]),
    ("YM2413",  ["ym2413", "opll-ym2413"]),
    ("YM3812",  ["ym3812", "ad-lib-ym3812", "adlib-soundblaster-ym3812",
                 "sound-blaster-1-0-ym3812", "sound-blaster-pro-ym3812"]),
    ("YMF262",  ["ymf262", "sound-blaster-16-ymf262", "soundblaster-ymf262"]),
]

_PACK_A = re.compile(
    r'''href=["'](?:https?://(?:www\.)?vgmrips\.net)?/packs/pack/([^"'/?#]+)/?["'][^>]*>(.*?)</a>''',
    re.I | re.S)
_PAGE_P = re.compile(r'''href=["'][^"']*\?(?:[^"']*&(?:amp;)?)?p=(\d+)[^"']*["']''', re.I)
_TRACK_A = re.compile(
    r'''href=["']((?:https?://(?:www\.)?vgmrips\.net)?/packs/vgm/[^"']+\.(?:vgz|vgm))["']''',
    re.I)
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


class VgmAdapter(Adapter):
    id = "vgm"
    name = "VGMRips music"

    def __init__(self):
        self._client = httpx.Client(
            timeout=120.0, follow_redirects=True, headers={"User-Agent": UA},
        )
        self._max_packs = int(os.environ.get("VGM_MAX_PACKS", "0") or "0")
        # chip dir -> (expires, [(pack dir name, pack slug)])
        self._packs: "dict[str, tuple[float, list[tuple[str, str]]]]" = {}
        # pack slug -> (expires, [(display name, track url)])
        self._track: "dict[str, tuple[float, list[tuple[str, str]]]]" = {}

    # ── packs of a chip (paginated listing scrape) ───────────────────────────
    def _crawl_slug(self, slug: str, order: "dict[str, str]") -> None:
        """Merge {pack slug: title} from every listing page of one chip slug."""
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
                elif t and not order[s]:     # cover-image/#autoplay twin first
                    order[s] = t
            for pm in _PAGE_P.finditer(r.text):
                max_p = max(max_p, int(pm.group(1)))
            if new == 0:                     # page repeated / ran past the end
                break
            p += 1

    def _chip_packs(self, chip: str) -> "list[tuple[str, str]]":
        hit = self._packs.get(chip)
        if hit and hit[0] > time.time():
            return hit[1]
        order: dict[str, str] = {}
        for slug in next((c for d, c in CHIPS if d == chip), []):
            try:
                self._crawl_slug(slug, order)
            except Exception as e:  # noqa: BLE001 — degrade, keep other slugs
                print(f"  vgm {chip}: chip page ({slug}) failed: {e}")
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

    # ── tracks of a pack (direct .vgz links on the pack page) ────────────────
    def _tracks(self, slug: str) -> "list[tuple[str, str]]":
        hit = self._track.get(slug)
        if hit and hit[0] > time.time():
            return hit[1]
        urls: list[str] = []
        try:
            r = self._client.get(f"{BASE}/packs/pack/{slug}")
            r.raise_for_status()
            for m in _TRACK_A.finditer(r.text):
                u = _html.unescape(m.group(1))
                if u.startswith("/"):
                    u = BASE + u
                if u not in urls:            # play + download twins → once
                    urls.append(u)
        except Exception as e:  # noqa: BLE001 — degrade to empty listing
            print(f"  vgm: pack {slug}: page failed: {e}")
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for u in urls:                       # page order == track order
            stem = unquote(u.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
            disp = _WS.sub(" ", stem.replace("\t", " ")).strip() or "track"
            name = disp + ".vgm"
            if name in seen:
                i = 2
                while f"{disp} {i}.vgm" in seen:
                    i += 1
                name = f"{disp} {i}.vgm"
            seen.add(name)
            out.append((name, u))
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
            # No url on purpose: .vgz needs a gunzip the device doesn't have
            # (and Anubis gates its browser-less HTTP stack anyway), so the
            # exporter mirrors fetch()'s ready .vgm bytes instead. Size is
            # unknown until the gunzip — the exporter fills in exact sizes
            # for everything it mirrors.
            return [Entry(False, n, 0) for n, _ in self._tracks(slug)]
        return []

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        seg = path.split("/")
        if len(seg) != 2:
            raise FileNotFoundError(name)
        slug = self._pack_slug(seg[0], seg[1])
        url = next((u for n, u in self._tracks(slug) if n == name), "")
        if not url:
            raise FileNotFoundError(name)
        r = self._client.get(url)
        r.raise_for_status()
        data = r.content
        if data[:2] == b"\x1f\x8b":          # .vgz = gzipped .vgm → unpack here
            data = gzip.decompress(data)
        stem = unquote(url.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
        return data, _fn_slug(stem)
