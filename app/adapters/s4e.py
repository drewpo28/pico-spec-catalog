"""Spectrum 4 Ever adapter — the "Full Tape Crack Pack" release archive.

spectrum4ever.org lists ~6100 tape releases (cracked/translated versions with
per-release cracker credits) on one page per letter — no pagination, no API, no
UA filtering. Rows are rigid markup, parsed with regexes (2026-07 redesign:
divs instead of table rows, semantic release__* classes, the blue T_B_ flags
column dropped, a grn "release__special" span with verbose Cyrillic loader
details added — not shown, too long for the 36-col OSD):

    <img class="icon" src="js/tape.png">
    <a ... href='download.php?t=fulltape&id=N'>TITLE</a>
    <span class="release__format cian">.TAP</span>
    <a ... by=cracker">CRACKER</a>
    <span class="release__comment red">NOTE</span>
    <span class="release__special grn">(loader details)</span>
    <span class="release__lang magn">LANG</span>
    <span class="release__actions">…play/download buttons…</span>

A row = the chunk between consecutive tape.png imgs, cut at release__actions
(the player area has its own cian/magn spans that must not leak into fields).

Display name = "TITLE .TAP  CRACKER  NOTE  LANG" (empty fields omitted) —
e.g. "A TEAM .TAP  ANDREW STRIKES CODE  SPN".

download.php serves the RAW .TAP/.TZX (no zip). The device names the saved file
after the locator's last path segment (HttpCatalogFs::downloadBasename), so the
locator gets a dummy trailing param whose value starts with '/' and ends with a
real ASCII filename — the server ignores it (verified byte-identical):

    download.php?t=fulltape&id=5498&fn=/A_TEAM_(ANDREW_STRIKES_CODE).TAP

Tree: <letter>/ at the root (0-9, A-Z, RUS). The Cyrillic bucket is exposed as
ASCII "RUS", NOT "А-Я": gen_static's slug() keeps Unicode alnum chars but the
device's byte-wise slugPath() maps them to '_' — a non-ASCII dir name would
break slug parity and 404 the .tsv.

TLS: Let's Encrypt RSA-4096, TLS1.2 ECDHE-RSA-AES128-GCM-SHA256 — matches the
device mbedTLS config. https only (http:// 301-redirects).
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

import httpx

from .base import Adapter, Entry

BASE = "https://spectrum4ever.org"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 3600

# dir name shown on the device → letter= query token on the site
LETTERS: "list[tuple[str, str]]" = (
    [("0-9", "0-9")]
    + [(c, c) for c in (chr(x) for x in range(ord("A"), ord("Z") + 1))]
    + [("RUS", "А-Я")]
)

_ROW_SPLIT = re.compile(r'<img[^>]*src="js/tape\.png"[^>]*>')
_ROW_ID = re.compile(r"download\.php\?t=fulltape&id=(\d+)'>([^<]*)</a>")
_ROW_EXT = re.compile(r'class="release__format[^"]*">([^<]*)</span>')
_ROW_CRK = re.compile(r'by=cracker">([^<]*)</a>')
_ROW_RED = re.compile(r'class="release__comment[^"]*">([^<]*)</span>')
_ROW_MAGN = re.compile(r'class="release__lang[^"]*">([^<]*)</span>')
_FN_SAFE = re.compile(r"[^A-Za-z0-9._()-]+")


def _parse_rows(html: str):
    """(id, title, ext, cracker, note, lang) per release row."""
    out = []
    for ch in _ROW_SPLIT.split(html)[1:]:
        ch = ch.split("release__actions", 1)[0]
        m = _ROW_ID.search(ch)
        if not m:
            continue
        grab = lambda rx: (lambda g: g.group(1).strip() if g else "")(rx.search(ch))
        out.append((m.group(1), m.group(2).strip(), grab(_ROW_EXT),
                    grab(_ROW_CRK), grab(_ROW_RED), grab(_ROW_MAGN)))
    return out


def _fn_slug(title: str, cracker: str, rid: str, ext: str) -> str:
    """ASCII filename for the &fn=/ trick (spaces → '_', Cyrillic → id fallback).
    Must stay URL-safe verbatim — the device sends the locator unencoded."""
    t = _FN_SAFE.sub("_", title).strip("_")
    if not any(c.isascii() and c.isalpha() for c in t):
        t = f"s4e_{rid}"                     # Cyrillic/empty title → unique id stem
    c = _FN_SAFE.sub("_", cracker).strip("_")
    stem = (t + (f"_({c})" if c else ""))[:56]
    return stem + ext.lower()


class S4eAdapter(Adapter):
    id = "s4e"
    name = "Spectrum 4 Ever"

    def __init__(self):
        self._client = httpx.Client(
            timeout=60.0, follow_redirects=True, headers={"User-Agent": UA},
        )
        self._cache: dict[str, tuple[float, list[Entry]]] = {}

    @staticmethod
    def _display(title, ext, cracker, note, lang) -> str:
        parts = [f"{title} {ext}"]
        if cracker:
            parts.append(cracker)
        if note:
            parts.append(note[:24])
        if lang:
            parts.append(lang)
        return "  ".join(parts)

    def _letter(self, token: str) -> "list[Entry]":
        hit = self._cache.get(token)
        if hit and hit[0] > time.time():
            return hit[1]
        url = f"{BASE}/fulltape.php?go=releases&letter={quote(token)}"
        try:
            r = self._client.get(url)
            r.raise_for_status()
            rows = _parse_rows(r.text)
        except Exception as e:  # noqa: BLE001
            print(f"  s4e: {token}: fetch/parse failed: {e}")
            rows = []
        entries: list[Entry] = []
        seen: set[str] = set()
        for rid, title, ext, cracker, note, lang in rows:
            if cracker.lower() == "n/a":
                cracker = ""
            name = self._display(title, ext, cracker, note, lang) \
                       .replace("\t", " ")
            if name in seen:                    # same title+cracker+note → number it
                i = 2
                while f"{name} {i}" in seen:
                    i += 1
                name = f"{name} {i}"
            seen.add(name)
            fn = _fn_slug(title, cracker, rid, ext)
            url = f"{BASE}/download.php?t=fulltape&id={rid}&fn=/{fn}"
            entries.append(Entry(False, name, 0, url=url))
        print(f"  s4e {token}: {len(entries)} releases")
        self._cache[token] = (time.time() + CACHE_TTL, entries)
        return entries

    # ── RemoteFs surface ─────────────────────────────────────────────────────
    def list(self, path: str) -> list[Entry]:
        if not path:
            return [Entry(True, d, 0) for d, _ in LETTERS]
        seg = path.split("/")
        token = next((t for d, t in LETTERS if d == seg[0]), None)
        if token is None or len(seg) != 1:
            return []
        return self._letter(token)

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """Dynamic /v1 server only: download the entry's URL as-is."""
        url = next((e.url for e in self.list(path)
                    if not e.is_dir and e.name == name and e.url), "")
        if not url:
            raise FileNotFoundError(name)
        fn = url.rsplit("/", 1)[-1]
        return self._client.get(url).content, fn
