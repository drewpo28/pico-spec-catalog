"""Spectrum 4 Ever adapter — the "Full Tape Crack Pack" release archive.

spectrum4ever.org lists ~6100 tape releases (cracked/translated versions with
per-release cracker credits) on one page per letter — no pagination, no API, no
UA filtering. Rows are rigid markup, parsed with regexes. 2026-09 redesign
(verified against the live markup 2026-09-03): the listing moved to
/<lang>/releases?letter=X (the old fulltape.php URL 301s to the *Russian*
edition, so the English one is requested explicitly — loader notes are
localised), one <article class="tape tape--release"> per row, the fields became
release-card__* blocks, the .TAP/.TZX format is only visible in the download
button's title, and the loader note + language share one "meta" block:

    <article class="tape tape--release">
      <div class="release-card__title"> <a class="yel" href="…/releases/slug">TITLE</a></div>
      <div class="release-card__authors"> <a class="grey" href="…/authors/x">CRACKER</a>, <a class="grey" …>CRACKER2</a></div>
      <div class="release-card__comment red">NOTE</div>
      <div class="release-card__meta magn">(loader details) LANG</div>
      <div class="release-card__actions release__actions">
        <a class="release__download cian" href="…/download.php?t=fulltape&id=N" title="Download TAP">
        …play/emulator buttons (data-tape-title/author attrs)…
      </div>
      <div class="release-card__screens">…<img alt="TITLE — ZX SPECTRUM GAME">…</div>
    </article>

Three redesigns in a row renamed every CSS class (release__* → article.release
→ release-card__*), so the parser deliberately does NOT key on class names. It
walks the DOM (selectolax) from the one thing every layout has had — the
download link, download.php?t=fulltape&id=N — up to the smallest ancestor that
holds exactly one such link (the "row"), then reads the fields off semantic
anchors inside that row, most stable first:

    title    text of the download link itself (pre-2026 layouts) → a[href*=/releases/]
             → the play button's data-tape-title → first other link
    crackers a[href*=/authors/] / a[href*=by=cracker] → data-tape-author
    format   TAP|TZX in the download link's title/aria-label → a class *containing*
             "format" → a leaf text that is exactly ".TAP"/"TAP"/…
    note     element whose class contains "comment" or the colour token "red"
    lang     element whose class contains "lang"/"meta" or the token "magn",
             parenthesised loader details stripped → trailing 3-letter code

Only note/lang still lean on class *fragments* (the colour tokens survived every
redesign); everything that decides whether a release is listed at all does not.
The authors/comment/meta blocks are all optional (≈30% of rows have no cracker).

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

TLS: Let's Encrypt RSA-4096 (YR2 → Root YR → ISRG Root X1), TLS1.2
ECDHE-RSA-AES128-GCM-SHA256 — matches the device mbedTLS config. https only
(http:// 301-redirects).
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser, Node

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

_ROW_ID = re.compile(r"download\.php\?t=fulltape&(?:amp;)?id=(\d+)")
_FMT = re.compile(r"\b(TAP|TZX)\b", re.I)
_FMT_LEAF = re.compile(r"^\.?(TAP|TZX)$", re.I)
_PARENS = re.compile(r"\([^)]*\)")
_LANG = re.compile(r"\b([A-Z]{3})$")
_WS = re.compile(r"\s+")
_FN_SAFE = re.compile(r"[^A-Za-z0-9._()-]+")


def _attr(n: Node, name: str) -> str:
    return n.attributes.get(name) or ""     # selectolax: empty attr → None


def _txt(n: Node | None) -> str:
    return _WS.sub(" ", n.text(separator=" ", strip=True)).strip() if n is not None else ""


def _classes(n: Node) -> list[str]:
    return (n.attributes.get("class") or "").lower().split()


def _by_class(row: Node, *, contains=(), token=()) -> Node | None:
    """First descendant whose class list has a token containing one of
    `contains` or equal to one of `token` — a redesign-tolerant selector."""
    for n in row.css("[class]"):
        cls = _classes(n)
        if any(c in t for t in cls for c in contains) or any(t in token for t in cls):
            return n
    return None


def _row_of(a: Node) -> Node:
    """Smallest ancestor of the download link that still holds exactly ONE
    download link — the release row, whatever tag/class it has this year."""
    node = a
    while node.parent is not None and node.parent.tag not in ("body", "html"):
        if len(node.parent.css('a[href*="t=fulltape"]')) != 1:
            break
        node = node.parent
    return node


def _parse_rows(html: str):
    """(id, title, ext, cracker, note, lang) per release row."""
    tree = HTMLParser(html)
    out = []
    seen: set[str] = set()
    for a in tree.css('a[href*="t=fulltape"]'):
        m = _ROW_ID.search(_attr(a, "href"))
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        row = _row_of(a)
        play = row.css_first("[data-tape-title]")

        title = _txt(a)
        if not title:
            rel = row.css_first('a[href*="/releases/"]')
            title = _txt(rel)
        if not title and play is not None:
            title = _attr(play, "data-tape-title").strip()
        if not title:
            for other in row.css("a[href]"):
                h = _attr(other, "href")
                if "t=fulltape" in h or "/authors/" in h or "by=cracker" in h or "qaop" in h:
                    continue
                if _txt(other):
                    title = _txt(other)
                    break

        crackers = [_txt(n) for n in row.css('a[href*="/authors/"], a[href*="by=cracker"]')]
        crackers = [c for c in crackers if c]
        if not crackers and play is not None:
            crackers = [c.strip() for c in _attr(play, "data-tape-author").split(",") if c.strip()]

        fmt = ""
        for attr in ("title", "aria-label", "data-format"):
            f = _FMT.search(_attr(a, attr))
            if f:
                fmt = f.group(1)
                break
        if not fmt:
            n = _by_class(row, contains=("format",))
            f = _FMT.search(_txt(n)) if n is not None else None
            if f:
                fmt = f.group(1)
        if not fmt:
            for n in row.css("span, div, td, b, i, em, strong"):
                f = _FMT_LEAF.match(_txt(n))
                if f:
                    fmt = f.group(1)
                    break
        ext = "." + fmt.upper() if fmt else ""

        note = _txt(_by_class(row, contains=("comment",), token=("red",)))
        meta = _txt(_by_class(row, contains=("lang", "meta"), token=("magn",)))
        lang = _PARENS.sub("", meta).strip()
        lm = _LANG.search(lang)
        lang = lm.group(1) if lm else ""

        out.append((m.group(1), title, ext, ", ".join(crackers), note, lang))
    return out


def _fn_slug(title: str, cracker: str, rid: str, ext: str) -> str:
    """ASCII filename for the &fn=/ trick (spaces → '_', Cyrillic → id fallback).
    Must stay URL-safe verbatim — the device sends the locator unencoded."""
    t = _FN_SAFE.sub("_", title).strip("_")
    if not t or (not title.isascii()
                 and not any(c.isascii() and c.isalpha() for c in t)):
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
        url = f"{BASE}/en/releases?letter={quote(token)}"
        try:
            r = self._client.get(url)
            r.raise_for_status()
            rows = _parse_rows(r.text)
            if not rows and "t=fulltape" in r.text:
                raise RuntimeError("markup changed: download links present, 0 rows parsed")
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
