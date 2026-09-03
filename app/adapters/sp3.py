"""Spectrum 3 adapter — spectrum3.es, the Spanish +3 disk (.DSK) conversion archive.

Two hand-made static HTML shelves (verified against the live site 2026-09-03):

    archivo.html                → Juegos/<l>.html        classic games, one page per
                                                          letter (0 = digits, a…z)
    Nueva Era/nuevaera.html     → Nueva Era/Anos/<y>.html  modern homebrew, one page
                                                          per year (1992…2025)

Both use the same card markup —

    <div class="game-card">
        <a href="A/DSK/Abu%20Simbel%20Profanation.html">…<img …></a>
        <div class="game-title">ABU SIMBEL PROFANATION</div>
    </div>

— and every card links to a per-game detail page (≈3300 of them, some newer ones
under HTML/ instead of DSK/, a handful 404) holding one 5-column table row per
conversion (about 5560 .dsk in total, 1–7 per game):

    <tr><td>AUTHOR</td> <td><img src="…/Letreros/espanol.png">…</td> <td>UTILITY</td>
        <td>[nextlogo.jpg]</td> <td><a href="Abu%20Simbel%20Profanation/… {AUTHOR}.dsk"><img src="…/BotonDSK.jpg"></a></td></tr>

The device tree merges BOTH shelves into one alphabet — 0-9, A…Z at the root,
each letter a flat, alphabetically sorted list of every conversion of every game
whose title starts with it — but each locator points at the .dsk wherever it
really lives (Juegos/… or Nueva Era/Anos/<y>/…).

Display name = "TITLE  AUTHOR [TOOL]  LANGS" (Nueva Era: "TITLE (YEAR)  AUTHOR  LANGS"),
e.g. "Abu Simbel Profanation  nugget (Loader8)  ESP/ENG", "Astrocop (2020)  MADFOX [Z80onDSK]  ESP".
The conversion tool (3rd column, usually empty) is what tells one author's two
builds of the same game apart; remaining exact duplicates are numbered.

Files are plain static .dsk (Content-Type text/plain, no zip) served by the
Hostinger CDN, so they are exposed as direct links (link mode, nothing
mirrored). The device names the saved file after the locator's last path
segment and does not percent-decode it, so — as for s4e — the URL gets a dummy
query whose value starts with '/' and ends with an ASCII filename; static
hosting ignores the query (verified byte-identical):

    …/Aaargh!/Aaargh%20%7Bnugget%20Loader8%7D.dsk?fn=/Aaargh_(nugget_Loader8).dsk

Index pages are UTF-8, detail pages windows-1252 (declared) — decoded explicitly,
httpx would assume UTF-8 for both since the CDN sends no charset.

TLS: Let's Encrypt ECDSA P-256 (YE2 → Root YE → ISRG Root X2, cross-signed by
ISRG Root X1), TLS1.2 ECDHE-ECDSA-AES128-GCM-SHA256 offered — RSA suites are
refused, so the device needs its ECDSA key exchange (MBEDTLS_KEY_EXCHANGE_ECDHE_ECDSA_ENABLED
+ SECP256R1, both in the pico-spec mbedTLS config) and a cacert.pem carrying the
ISRG roots.
"""

from __future__ import annotations

import os
import posixpath
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape
from urllib.parse import quote, unquote

import httpx

from .base import Adapter, Entry

BASE = "https://spectrum3.es/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 3600
WORKERS = 8            # parallel detail-page fetches (static CDN, ~90 s for the whole site)
YEARS = range(1992, 2026)
LETTERS = ["0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]

# Letreros/<flag>.png → 3-letter tag shown on the device
LANGS = {
    "ingles": "ENG", "espanol": "ESP", "ruso": "RUS", "checo": "CZE",
    "portugues": "POR", "polaco": "POL", "italiano": "ITA", "frances": "FRA",
    "eslovaco": "SVK", "aleman": "GER", "euskera": "EUS", "sueco": "SWE",
    "gallego": "GLG", "catalan": "CAT",
}

_CARD = re.compile(r'<div class="game-card">\s*<a href="([^"]*)">.*?'
                   r'<div class="game-title">([^<]*)</div>', re.S)
_TR = re.compile(r"<tr>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_HREF = re.compile(r'<a\s+href="([^"]*)"', re.I)
_IMG = re.compile(r'<img[^>]*src="([^"]*)"', re.I)
_H1 = re.compile(r'size="7"[^>]*>(.*?)</font>', re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_FN_SAFE = re.compile(r"[^A-Za-z0-9._()-]+")
IMAGE_EXTS = {".dsk", ".tap", ".tzx", ".z80", ".sna", ".trd", ".scl", ".zip"}


@dataclass
class _Game:
    title: str          # card title (UPPERCASE on the site)
    year: int | None    # Nueva Era year, None for the classic shelf
    page: str           # detail page, site-relative, percent-decoded


def _text(html: str) -> str:
    return _WS.sub(" ", unescape(_TAG.sub("", html)).replace("\xa0", " ")).strip()


def _bucket(title: str) -> str:
    """First letter of the title → root dir. Accents folded ("Ñandú" → N),
    leading punctuation skipped ("¡Viva!" → V), digits → "0-9"."""
    for ch in unicodedata.normalize("NFKD", title):
        if ch.isascii() and ch.isalpha():
            return ch.upper()
        if ch.isascii() and ch.isdigit():
            return "0-9"
    return "0-9"


def _fn_name(href: str) -> str:
    """ASCII filename for the ?fn=/ trick: "Aaargh! {nuggetreggae}.dsk" →
    "Aaargh_(nuggetreggae).dsk". Must stay URL-safe verbatim."""
    fn = unquote(href).rsplit("/", 1)[-1].replace("{", "(").replace("}", ")")
    stem, ext = os.path.splitext(fn)
    stem = _FN_SAFE.sub("_", stem).strip("_")[:60] or "disk"
    return stem + (ext.lower() if ext else ".dsk")


def _decode(content: bytes) -> str:
    if re.search(rb"charset=['\"]?utf-8", content[:2048], re.I):
        return content.decode("utf-8", "replace")
    return content.decode("cp1252", "replace")


class Sp3Adapter(Adapter):
    id = "sp3"
    name = "Spectrum 3 (+3 DSK)"

    def __init__(self):
        # The Hostinger CDN WAF 403s httpx's bare default header set (Accept: */*
        # + gzip and no Accept-Language reads as a bot); a browser-shaped Accept /
        # Accept-Language pair passes. curl and urllib pass as-is.
        self._client = httpx.Client(
            timeout=60.0, follow_redirects=True, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
            },
        )
        self._games: tuple[float, list[_Game]] | None = None
        self._cache: dict[str, tuple[float, list[Entry]]] = {}

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _get(self, rel: str) -> bytes:
        """GET a site-relative, percent-DECODED path (retried: the CDN
        occasionally drops a connection under parallel load)."""
        url = BASE + quote(rel)
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self._client.get(url)
                r.raise_for_status()
                return r.content
            except httpx.HTTPStatusError:
                raise                      # 404 etc. — don't retry
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"{rel}: {last}")

    # ── Index (both shelves) ─────────────────────────────────────────────────
    def _index_page(self, rel: str, year: int | None) -> list[_Game]:
        base_dir = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
        out: list[_Game] = []
        for href, title in _CARD.findall(_decode(self._get(rel))):
            page = unquote(href)
            if not page.lower().endswith(".html"):
                continue               # a stray direct .gif/.zip card (dead upstream)
            out.append(_Game(_text(title), year, posixpath.normpath(base_dir + page)))
        return out

    def _all_games(self) -> list[_Game]:
        if self._games and self._games[0] > time.time():
            return self._games[1]
        games: list[_Game] = []
        for l in ["0"] + [chr(c) for c in range(ord("a"), ord("z") + 1)]:
            games += self._index_page(f"Juegos/{l}.html", None)
        for y in YEARS:
            games += self._index_page(f"Nueva Era/Anos/{y}.html", y)
        print(f"  sp3: {len(games)} games indexed (both shelves)")
        self._games = (time.time() + CACHE_TTL, games)
        return games

    # ── Detail page → conversions ────────────────────────────────────────────
    def _versions(self, g: _Game) -> list[tuple[str, str, str, str]]:
        """(title, author, langs, dsk_url) per conversion row of one game."""
        try:
            html = _decode(self._get(g.page))
        except Exception as e:  # noqa: BLE001
            print(f"  sp3: skip {g.page}: {e}")
            return []
        m = _H1.search(html)
        title = _text(m.group(1)) if m else ""
        if not title:
            title = g.title.title()
        dir_ = g.page.rsplit("/", 1)[0] + "/"
        out = []
        for row in _TR.findall(html):
            tds = _TD.findall(row)
            if len(tds) < 5:
                continue
            links = [h for h in _HREF.findall(tds[4])
                     if os.path.splitext(unquote(h))[1].lower() in IMAGE_EXTS
                     and "://" not in h]
            if not links:
                continue           # dead link / external itch.io page — nothing to serve
            href = links[0]
            author = _text(tds[0])
            util = _text(tds[2])               # conversion tool (Z80onDSK, TAP2DSK…)
            if util:                           # — tells apart one author's two builds
                author = f"{author} [{util}]" if author else util
            langs = []
            for src in _IMG.findall(tds[1]):
                stem = os.path.splitext(unquote(src).rsplit("/", 1)[-1])[0].lower()
                langs.append(LANGS.get(stem, stem[:3].upper()))
            path = posixpath.normpath(dir_ + unquote(href))   # HTML/../DSK/x → DSK/x
            url = BASE + quote(path) + "?fn=/" + _fn_name(href)
            out.append((title, author, "/".join(langs), url))
        return out

    def _letter(self, letter: str) -> list[Entry]:
        hit = self._cache.get(letter)
        if hit and hit[0] > time.time():
            return hit[1]
        games = [g for g in self._all_games() if _bucket(g.title) == letter]
        rows: list[tuple] = []
        with ThreadPoolExecutor(WORKERS) as ex:
            for g, vs in zip(games, ex.map(self._versions, games)):
                for title, author, langs, url in vs:
                    rows.append((title, g.year, author, langs, url))
        rows.sort(key=lambda r: (r[0].casefold(), r[1] or 0, r[2].casefold()))
        entries: list[Entry] = []
        seen: set[str] = set()
        for title, year, author, langs, url in rows:
            parts = [f"{title} ({year})" if year else title]
            if author:
                parts.append(author)
            if langs:
                parts.append(langs)
            name = "  ".join(parts).replace("\t", " ")
            if name in seen:
                i = 2
                while f"{name} {i}" in seen:
                    i += 1
                name = f"{name} {i}"
            seen.add(name)
            entries.append(Entry(False, name, 0, url=url))
        print(f"  sp3 {letter}: {len(games)} games, {len(entries)} disks")
        self._cache[letter] = (time.time() + CACHE_TTL, entries)
        return entries

    # ── RemoteFs surface ─────────────────────────────────────────────────────
    def list(self, path: str) -> list[Entry]:
        if not path:
            return [Entry(True, d, 0) for d in LETTERS]
        seg = path.split("/")
        if len(seg) != 1 or seg[0] not in LETTERS:
            return []
        return self._letter(seg[0])

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """Dynamic /v1 server only: download the entry's URL as-is."""
        url = next((e.url for e in self.list(path)
                    if not e.is_dir and e.name == name and e.url), "")
        if not url:
            raise FileNotFoundError(name)
        r = self._client.get(url)
        r.raise_for_status()
        return r.content, url.rsplit("/", 1)[-1]
