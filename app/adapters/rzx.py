"""The RZX Archive adapter — rzxarchive.co.uk, walkthrough input recordings.

Tree exposed to the device:

    <letter> / <recording>        letters: 0-9, A..Z

Everything below was checked against the live site (2026-09-29, from a GitHub
runner — the site is plain PHP 5.4 behind nginx, no anti-bot wall):

  - one page per letter, `0.php` and `a.php` .. `z.php` (27 in all), ≈4050
    recordings. Each recording is one table row, all on one line:

        <tr><td><a NAME="abc"></a><font size=2>A B C<br>
                <font size=1>NOTE<br>                         (0..n notes)
                <font size=1>Recorded using <A HREF="rollback.html">Rollback</A></td>
            <td align=center><font size=2>SUBMITTER</td>
            <td align=center><font size=1><A HREF="/a/abc.rzx">Download</A>
                <font size=1>(22KB) | WoS page | Spectrum Computing page | link</td></tr>

    so a row is keyed on its `/<letter>/<file>.(rzx|zip)` download link, and the
    fields are read off the three cells in order — no CSS classes to break.
  - downloads are direct static files with Content-Length: `.rzx` (≈3190) or a
    `.zip` (≈860) holding several recordings — one per level, sometimes an
    intro/ending, occasionally a .txt. The device downloads and, for a zip,
    unzips it itself, so this is link-mode: listings only on Pages.
  - the recordings carry their own snapshot (Spectaculator writes Z80, Fuse
    Z80 unless it would lose state, then SZX). Playing them needs the
    pico-speccy firmware with RZX playback (2026-09); pico-spec has none.
    SZX-based files are listed anyway — the firmware says so when it meets one.

Display name: "TITLE .RZX  SUBMITTER  NOTE" (".ZIP" for a bundle; the note is the
row's first line that is not the "Recorded using Rollback" boilerplate or a
"More info." link, clipped). The saved file is named after the locator's last
path segment, which is already a real `name.rzx` / `name.zip`.

Knob: RZX_REQ_GAP (env) — minimum seconds between requests (default 0.5).
"""

from __future__ import annotations

import html
import os
import re
import time

from .base import Adapter, Entry, SourceDown, http_client

SITE = "https://www.rzxarchive.co.uk"
LETTERS = ["0"] + [chr(c) for c in range(ord("a"), ord("z") + 1)]

# One row = from one <tr> to the next. Download links are site-absolute.
ROW_SPLIT = re.compile(r"<tr[\s>]", re.I)
DL = re.compile(r'''href\s*=\s*["']?(/[a-z0-9]/[^"'\s>]+\.(rzx|zip))["'\s>]''', re.I)
CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
SIZE = re.compile(r"\((\d+)\s*KB\)", re.I)
BR = re.compile(r"<br\s*/?>", re.I)

# Fewer rows than this across the whole site means the markup moved, not that
# the archive shrank — refuse rather than publish a gutted tree.
MIN_ROWS = 1000
NOTE_MAX = 48


def _text(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = s.replace("/", "-").replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


def _dir(letter: str) -> str:
    return "0-9" if letter == "0" else letter.upper()


def parse_page(page: str) -> list[tuple[str, str, str, str, int]]:
    """(title, submitter, note, href, size) per recording row, in page order."""
    out = []
    for row in ROW_SPLIT.split(page):
        m = DL.search(row)
        if not m:
            continue
        cells = CELL.findall(row)
        if len(cells) < 3:
            continue
        parts = [p for p in (_text(x) for x in BR.split(cells[0])) if p]
        if not parts:
            continue
        title = parts[0]
        note = ""
        for p in parts[1:]:
            low = p.lower()
            if low.startswith("recorded using") or low.startswith("more info"):
                continue
            note = p if len(p) <= NOTE_MAX else p[:NOTE_MAX - 3].rstrip() + "..."
            break
        submitter = _text(cells[1])
        sm = SIZE.search(cells[2]) or SIZE.search(row)
        size = int(sm.group(1)) * 1024 if sm else 0
        out.append((title, submitter, note, m.group(1), size))
    return out


class RzxAdapter(Adapter):
    id = "rzx"
    name = "RZX Archive"

    def __init__(self):
        self._client = http_client(
            headers={"User-Agent": "Mozilla/5.0 pico-spec-catalog/1.0"},
            timeout=30.0, follow_redirects=True,
        )
        self._gap = float(os.environ.get("RZX_REQ_GAP", "0.5"))
        self._last = 0.0
        self._tree: dict[str, list[Entry]] | None = None

    def _get(self, url: str) -> bytes:
        err = None
        for attempt in range(4):
            wait = self._last + self._gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                r = self._client.get(url)
                self._last = time.monotonic()
                r.raise_for_status()
                return r.content
            except Exception as e:  # noqa: BLE001 — retried, then reported
                self._last = time.monotonic()
                err = e
                time.sleep(2 * (attempt + 1))
        raise SourceDown(f"rzx: {url}: {err}")

    def _load(self) -> dict[str, list[Entry]]:
        if self._tree is not None:
            return self._tree
        tree: dict[str, list[Entry]] = {}
        total = 0
        for letter in LETTERS:
            page = self._get(f"{SITE}/{letter}.php").decode("utf-8", "replace")
            entries: list[Entry] = []
            seen: dict[str, int] = {}
            for title, who, note, href, size in parse_page(page):
                ext = href.rsplit(".", 1)[-1].upper()
                name = "  ".join(x for x in (f"{title} .{ext}", who, note) if x)
                if name in seen:                       # same title twice: tell them apart
                    seen[name] += 1
                    name = f"{name}  #{seen[name]}"
                else:
                    seen[name] = 1
                entries.append(Entry(is_dir=False, name=name, size=size, url=SITE + href))
            tree[_dir(letter)] = entries
            total += len(entries)
        if total < MIN_ROWS:
            raise SourceDown(f"rzx: only {total} recordings parsed — markup changed?")
        self._tree = tree
        return tree

    def list(self, path: str) -> list[Entry]:
        tree = self._load()
        if path == "":
            return [Entry(is_dir=True, name=d) for d in tree if tree[d]]
        return tree.get(path, [])

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        # Link mode normally avoids this; provided so --no-link mirroring works.
        for e in self._load().get(path, []):
            if e.name == name:
                return self._get(e.url), e.url.rsplit("/", 1)[-1]
        raise FileNotFoundError(name)
