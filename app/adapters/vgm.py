"""VGMRips adapter — VGM music packs from vgmrips.net, by sound chip.

vgmrips.net catalogs VGM rips as *packs* (one game/system = one pack of
tracks) and tags every pack with the sound chips it drives. pico-spec cares
only about the chips it can play, so the tree exposed to the device is:

    <chip> / <pack title> / <track>.vgm

with the chip level fixed to the chips the DivMMC VGM-player card drives:
AY-3-8910, SAA1099, SN76489, YM2203, YM2413, YM3812, YMF262.
The site has no separate "2x…" categories (probed live 2026-09-02:
/packs/chip/2xsn76489 is 404 — "2xSN76489" is a per-pack label), so dual-chip
rips simply appear inside their base chip's category. Some chip tags are the
same die on a branded card ("AD-Lib (YM3812)", "Sound Blaster 16 (YMF262)",
"OPLL (YM2413)", the PC-9801-26K YM2203 boards) or a register-compatible
clone (YM2149 ~ AY-3-8910) — those alias slugs are unioned into the base
chip's dir. NOTE (probed live): a chip listing shows a pack only under its
FIRST chip — RoboCop (YM2203+YM3812) is on /packs/chip/ym2203 page 1 and
nowhere in ym3812's 12 pages — so covering the full supported-chip set is
what makes multi-chip packs findable at all.

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
    track (/packs/vgm/<Section>/<System>/<Pack>/<NN Track>.vgz) AND the
    whole-pack zip the Download button uses (/files/<Section>/<System>/
    <Pack>.zip) — both parsed from the one page fetch.
  - RATE LIMIT: individual requests answer in <1 s, but a sustained mirror
    run (hundreds of back-to-back /packs/vgm/ GETs) gets tarpitted — every
    further request hangs to the read timeout. So bulk bytes come from the
    pack ZIP (one request per pack instead of one per track), every request
    is spaced by VGM_REQ_GAP seconds, timeouts are short, and failures are
    retried with a growing backoff. The direct .vgz GET stays as the
    fallback when a pack has no zip or a member is missing from it.

Tracks are stored upstream as .vgz — a plain GZIP-compressed .vgm. Entries
carry the DIRECT .vgz URL (link mode): the Pages tree stays tiny (listings
only, no mirror budget — the full per-chip catalog fits) and the device
downloads the track itself, then unwraps the gzip on the SD. That needs the
matching pico-speccy firmware (branch claude/vgz-catalog-support): a
non-Mozilla UA towards vgmrips.net and the post-download .vgz→.vgm gunzip.
The locator ends with a dummy ?fn=/<ascii>.vgz — vgmrips serves the file
byte-identically with the query (verified live), and the device names the
saved file after the locator's last path segment (the s4e/tosec trick).
fetch() (the dynamic /v1 server, and gen_static --no-link mirroring) still
gunzips server-side, pulling bulk bytes from the whole-pack zip in a
single-slot cache.

Knobs: VGM_MAX_PACKS (env) caps packs kept per chip, 0/unset = all;
VGM_REQ_GAP (env) is the minimum seconds between requests (default 0.5).
"""

from __future__ import annotations

import gzip
import html as _html
import io
import os
import re
import time
import zipfile
from urllib.parse import unquote

import httpx

from .base import Adapter, Entry

BASE = "https://vgmrips.net"
UA = "pico-spec-catalog/1.0 (+https://github.com/drewpo28/pico-spec-catalog)"
CACHE_TTL = 3600
MAX_PAGES = 60                # hard cap on pagination walks per chip slug
TRIES = 3                     # attempts per request (timeouts / 429 / 5xx)

# device dir name -> chip slugs on the site whose packs it unions
# (base tag + same-die card aliases, from the live /packs/chips list).
CHIPS: "list[tuple[str, list[str]]]" = [
    ("AY-3-8910", ["ay-3-8910", "ym2149"]),
    ("SAA1099", ["saa1099"]),
    ("SN76489", ["sn76489"]),
    ("YM2203",  ["ym2203", "pc-9801-26k-ym2203", "ym2203-pc-9801-26k"]),
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
_ZIP_A = re.compile(
    r'''href=["']((?:https?://(?:www\.)?vgmrips\.net)?/files/[^"']+\.zip)["']''', re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_FN_SAFE = re.compile(r"[^A-Za-z0-9._()-]+")


def _text(markup: str) -> str:
    """Anchor body -> plain title (tags stripped, entities decoded)."""
    return _WS.sub(" ", _html.unescape(_TAGS.sub(" ", markup))).strip()


def _fn_slug(stem: str, ext: str = ".vgm") -> str:
    """ASCII download filename — written verbatim into the locator URL, and the
    device sends locators unencoded and names the saved file after the last
    path segment (same contract as s4e/tosec)."""
    t = _FN_SAFE.sub("_", stem).strip("_")
    if not any(c.isascii() and c.isalnum() for c in t):
        t = "vgm"
    return t[:80] + ext


class VgmAdapter(Adapter):
    id = "vgm"
    name = "VGMRips music"

    def __init__(self):
        # Short read timeout on purpose: a tarpitted request must fail fast
        # into the retry/backoff below, not stall the build for minutes.
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True, headers={"User-Agent": UA},
        )
        self._max_packs = int(os.environ.get("VGM_MAX_PACKS", "0") or "0")
        self._gap = float(os.environ.get("VGM_REQ_GAP", "0.5") or "0")
        self._next_at = 0.0                  # monotonic time the next request may fire
        # chip dir -> (expires, [(pack dir name, pack slug)])
        self._packs: "dict[str, tuple[float, list[tuple[str, str]]]]" = {}
        # pack slug -> (expires, [(display name, track url)], zip url or "")
        self._track: "dict[str, tuple[float, list[tuple[str, str]], str]]" = {}
        self._zipblob: "tuple[str, bytes] | None" = None   # single-slot zip cache

    # ── polite HTTP: request spacing + retry with backoff ────────────────────
    def _get(self, url: str, **kw) -> httpx.Response:
        last: Exception = RuntimeError("unreachable")
        for attempt in range(TRIES):
            wait = self._next_at - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_at = time.monotonic() + self._gap
            try:
                r = self._client.get(url, **kw)
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code}")   # retryable
                r.raise_for_status()          # other 4xx: no point retrying
                return r
            except httpx.HTTPStatusError:
                raise
            except Exception as e:  # noqa: BLE001 — timeout/transport/429/5xx
                last = e
                if attempt < TRIES - 1:
                    back = 20 * (attempt + 1)
                    print(f"  vgm: retry in {back}s ({e!r}) — …{url[-60:]}")
                    time.sleep(back)
        raise last

    # ── packs of a chip (paginated listing scrape) ───────────────────────────
    def _crawl_slug(self, slug: str, order: "dict[str, str]") -> None:
        """Merge {pack slug: title} from every listing page of one chip slug."""
        p, max_p = 1, 1
        while p <= min(max_p, MAX_PAGES):
            # The FIRST page must be requested WITHOUT the p param: single-page
            # chip listings (saa1099, the card-alias slugs) answer ?p=1 with an
            # empty listing (probed live 2026-09-02) — that's what silently
            # zeroed SAA1099 and shrank the alias unions. ?p=N works from p=2.
            params = {"p": p} if p > 1 else None
            matches: "list[re.Match[str]]" = []
            for attempt in range(2):
                r = self._get(f"{BASE}/packs/chip/{slug}", params=params)
                matches = list(_PACK_A.finditer(r.text))
                if matches:
                    break
                # A 200 with zero pack anchors mid-listing is a served glitch
                # (an anti-bot page slipping through, a hiccup) — retry once.
                # Never treated as end-of-list: bailing out here once silently
                # dropped the tail pages of SN76489 (packs "disappeared").
                if attempt == 0:
                    print(f"  vgm: {slug} p{p}: no pack anchors, retrying")
            for m in matches:
                s, t = m.group(1), _text(m.group(2))
                if s not in order:
                    order[s] = t
                elif t and not order[s]:     # cover-image/#autoplay twin first
                    order[s] = t
            for pm in _PAGE_P.finditer(r.text):
                max_p = max(max_p, int(pm.group(1)))
            p += 1                           # walk EVERY page up to max_p —
                                             # duplicates just merge into order

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
        out.sort(key=lambda t: t[0].casefold())  # site order is by-date — sort A-Z
        print(f"  vgm {chip}: {len(out)} packs")
        self._packs[chip] = (time.time() + CACHE_TTL, out)
        return out

    # ── tracks + zip url of a pack (one pack-page fetch for both) ────────────
    def _pack_info(self, slug: str) -> "tuple[list[tuple[str, str]], str]":
        hit = self._track.get(slug)
        if hit and hit[0] > time.time():
            return hit[1], hit[2]
        urls: list[str] = []
        zipurl = ""
        try:
            r = self._get(f"{BASE}/packs/pack/{slug}")
            for m in _TRACK_A.finditer(r.text):
                u = _html.unescape(m.group(1))
                if u.startswith("/"):
                    u = BASE + u
                if u not in urls:            # play + download twins → once
                    urls.append(u)
            zm = _ZIP_A.search(r.text)
            if zm:
                zipurl = _html.unescape(zm.group(1))
                if zipurl.startswith("/"):
                    zipurl = BASE + zipurl
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
        self._track[slug] = (time.time() + CACHE_TTL, out, zipurl)
        return out, zipurl

    def _pack_slug(self, chip: str, packdir: str) -> str:
        slug = next((s for n, s in self._chip_packs(chip) if n == packdir), "")
        if not slug:
            raise FileNotFoundError(f"{chip}/{packdir}")
        return slug

    def _zip_bytes(self, url: str) -> bytes:
        if self._zipblob and self._zipblob[0] == url:
            return self._zipblob[1]
        data = self._get(url).content
        self._zipblob = (url, data)
        return data

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
            # Direct .vgz link per track. The ?fn=/ tail names the saved file
            # (ASCII, ends .vgz so the firmware's post-download gunzip
            # triggers); vgmrips ignores the query on these URLs. Size stays 0
            # — the device reads Content-Length at download time.
            return [Entry(False, n, 0, url=u + "?fn=/" + _fn_slug(
                        unquote(u.rsplit("/", 1)[-1]).rsplit(".", 1)[0], ".vgz"))
                    for n, u in self._pack_info(slug)[0]]
        return []

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        seg = path.split("/")
        if len(seg) != 2:
            raise FileNotFoundError(name)
        slug = self._pack_slug(seg[0], seg[1])
        tracks, zipurl = self._pack_info(slug)
        url = next((u for n, u in tracks if n == name), "")
        if not url:
            raise FileNotFoundError(name)
        base = unquote(url.rsplit("/", 1)[-1])
        data = b""
        if zipurl:
            # Bulk path: one zip GET per pack (cached) instead of one GET per
            # track — sustained per-track downloads get tarpitted (see module
            # docstring). Members sit under a pack folder; match by basename.
            try:
                with zipfile.ZipFile(io.BytesIO(self._zip_bytes(zipurl))) as z:
                    member = next((i.filename for i in z.infolist()
                                   if i.filename.rsplit("/", 1)[-1] == base), "")
                    if member:
                        data = z.read(member)
            except Exception as e:  # noqa: BLE001 — fall back to the direct GET
                print(f"  vgm: pack zip failed ({e}), direct GET — {base}")
        if not data:
            data = self._get(url).content
        if data[:2] == b"\x1f\x8b":          # .vgz = gzipped .vgm → unpack here
            data = gzip.decompress(data)
        return data, _fn_slug(base.rsplit(".", 1)[0])
