"""ZXTunes adapter — zxtunes.com, AY/YM music by author.

Tree exposed to the device:

    <author> / <track>

played straight in the firmware's Pico-Zx-Player (Enter = play from here, F2 on
a folder = the whole author, Auto/Shuffle walk the folder).

Everything below was checked against the live site (2026-09-27):

  - authors: /authors_list.php?lm=<N>&fr=1&order=nickname&up=ASC&letter=ALL
    lists every author on ONE page when lm (the page size) is at least the
    total — 977 today. Each row carries
    `<a class='m' href='/ru/authors/<slug>'>Nick</a>` plus optional real name
    and group spans. The slug is unique; nicks are not (two "Alex"), so a
    repeated nick is disambiguated with its group (or the slug).
  - tracks: /ru/authors/<slug> embeds the whole playlist as JSON in
    <script type="application/json" id="zxtunes-player-config"> — one object
    per SUB-SONG: {url: /downloads.php?id=N, filename, title, subsong}. A
    multi-song .ay is several objects sharing one url; the firmware's .ay
    player walks every sub-song itself, so entries are merged by url.
  - download: /downloads.php?id=N answers the raw file (Content-Disposition
    names it). No anti-bot wall, any User-Agent works. A trailing
    `&fn=/<name>.<ext>` is accepted and ignored (verified byte-identical), and
    the device names the saved file — i.e. learns the FORMAT — after the
    locator's last path segment, the s4e/tosec/vgm trick. So the tree is
    link-mode: listings only on Pages, the device fetches from zxtunes.com.
  - the site also carries formats the firmware cannot play (.psg, .asc0, ...);
    only PLAYABLE ones are listed, and an author left with none is dropped.

Display names follow the site's own track row, "<file> - <title>": the file
name alone is often an 8.3 stub ("hnyear") and the title alone the module's
raw text ("mmcm.ru 231220060050 ABC YM2149 - Happy new year"). A multi-song
.ay shows just its file stem (the title is sub-song 0's).

Knobs: ZXTUNES_MAX_AUTHORS (env) caps authors, 0/unset = all;
ZXTUNES_REQ_GAP (env) is the minimum seconds between requests (default 0.5).
"""

from __future__ import annotations

import html
import json
import os
import re
import time

from .base import Adapter, Entry, SourceDown, http_client

SITE = "https://zxtunes.com"
LIST_URL = SITE + "/authors_list.php?id=&lm={lm}&fr=1&order=nickname&up=ASC&letter=ALL&sr="
AUTHOR_URL = SITE + "/ru/authors/{slug}"

# What Pico-Zx-Player plays (pico-speccy src/player/PicoPlayer.cpp playableExt).
PLAYABLE = {
    "pt3", "pt2", "stc", "stp", "sqt", "ay",          # AY
    "psc", "pt1", "asc", "ftc", "fls", "gtr", "fxm", "psm", "zxs", "stp2", "vtx",
    "tfc", "tfd", "tfe",                              # TurboSound FM
    "etc", "saa", "cop", "sng",                       # SAA1099
    "vgm", "vgz", "mp3",
    "mod", "s3m", "xm", "it",
    "mid", "midi", "kar", "rmi",
}

ROW = re.compile(
    r"authors-grid__cell--nick\"><a class='m' href='/ru/authors/([^']+)'>(.*?)</a>(.*?)</div>",
    re.S)
GROUP = re.compile(r"authors-nick__groups\">\^\s*<a[^>]*>(.*?)</a>", re.S)
PLAYER_JSON = re.compile(
    r'<script type="application/json" id="zxtunes-player-config">(.*?)</script>', re.S)


def _clean(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = s.replace("/", "-").replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


def _ascii_fn(name: str) -> str:
    """The &fn= tail: ASCII-safe (it goes into the device's HTTP request line)."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "track"
    ext = re.sub(r"[^A-Za-z0-9]+", "", ext).lower()
    return f"{stem[:48]}.{ext}" if ext else stem[:48]


class ZxtunesAdapter(Adapter):
    id = "zxtunes"
    name = "ZXTunes"

    def __init__(self):
        self._client = http_client(
            headers={"User-Agent": "Mozilla/5.0 pico-spec-catalog/1.0"},
            timeout=30.0, follow_redirects=True,
        )
        self._gap = float(os.environ.get("ZXTUNES_REQ_GAP", "0.5"))
        self._max = int(os.environ.get("ZXTUNES_MAX_AUTHORS", "0") or 0)
        self._last = 0.0
        self._authors: list[tuple[str, str]] | None = None   # (display, slug)
        self._tracks: dict[str, list[Entry]] = {}            # slug -> entries

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _get(self, url: str) -> str:
        err = None
        for attempt in range(4):
            wait = self._last + self._gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                r = self._client.get(url)
                self._last = time.monotonic()
                r.raise_for_status()
                return r.text
            except Exception as e:  # noqa: BLE001 — retried, then reported
                self._last = time.monotonic()
                err = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"{url}: {err}")

    # ── authors ──────────────────────────────────────────────────────────────
    def _load_authors(self) -> None:
        if self._authors is not None:
            return
        try:
            # One page holds every author when lm (page size) >= the total.
            page = self._get(LIST_URL.format(lm=5000))
            rows = ROW.findall(page)
            if not rows:
                raise ValueError("no author rows")
        except Exception as e:  # noqa: BLE001
            raise SourceDown(f"zxtunes: author list unusable: {e}")
        seen: dict[str, int] = {}
        out: list[tuple[str, str]] = []
        for slug, nick, rest in rows:
            nick = _clean(nick) or slug
            seen[nick.lower()] = seen.get(nick.lower(), 0) + 1
            g = GROUP.search(rest)
            out.append((nick, slug, _clean(g.group(1)) if g else ""))
        authors: list[tuple[str, str]] = []
        used: set[str] = set()
        for nick, slug, grp in out:
            disp = nick
            if seen[nick.lower()] > 1:
                disp = f"{nick} ({grp})" if grp else f"{nick} [{slug}]"
            if disp.lower() in used:
                disp = f"{nick} [{slug}]"
            used.add(disp.lower())
            authors.append((disp, slug))
        authors.sort(key=lambda a: a[0].lower())
        if self._max:
            authors = authors[: self._max]
        self._authors = authors

    # ── tracks ───────────────────────────────────────────────────────────────
    def _load_tracks(self, slug: str) -> list[Entry]:
        if slug in self._tracks:
            return self._tracks[slug]
        page = self._get(AUTHOR_URL.format(slug=slug))
        m = PLAYER_JSON.search(page)
        entries: list[Entry] = []
        if m:
            playlist = json.loads(m.group(1)).get("playlist", [])
            by_url: dict[str, dict] = {}
            order: list[str] = []
            for p in playlist:
                url = p.get("url", "")
                if not url:
                    continue
                if url not in by_url:
                    by_url[url] = {"subs": 0}
                    order.append(url)
                rec = by_url[url]
                rec["subs"] += 1
                if int(p.get("subsong", 0) or 0) == 0 or "filename" not in rec:
                    rec["filename"] = p.get("filename", "")
                    rec["title"] = p.get("title", "")
            names: set[str] = set()
            for url in order:
                rec = by_url[url]
                fn = rec.get("filename", "")
                stem, dot, ext = fn.rpartition(".")
                ext = ext.lower() if dot else ""
                if ext not in PLAYABLE:
                    continue
                stem = _clean(stem) or "track"
                title = _clean(rec.get("title", ""))
                if rec["subs"] > 1 or not title or title.lower().startswith(stem.lower()):
                    disp = stem if rec["subs"] > 1 or not title else title
                else:
                    disp = f"{stem} - {title}"
                disp = disp[:96]
                base, k = disp, 2
                while disp.lower() in names:
                    disp = f"{base} ({k})"
                    k += 1
                names.add(disp.lower())
                entries.append(Entry(is_dir=False, name=disp, size=0,
                                     url=f"{SITE}{url}&fn=/{_ascii_fn(fn)}"))
            entries.sort(key=lambda e: e.name.lower())
        self._tracks[slug] = entries
        return entries

    # ── Adapter ──────────────────────────────────────────────────────────────
    def list(self, path: str) -> list[Entry]:
        self._load_authors()
        if path == "":
            out = []
            for disp, slug in self._authors or []:
                try:
                    if self._load_tracks(slug):   # authors with nothing playable are dropped
                        out.append(Entry(is_dir=True, name=disp))
                except Exception:  # noqa: BLE001 — one dead page is not the site
                    continue
            return out
        for disp, slug in self._authors or []:
            if disp == path:
                return self._load_tracks(slug)
        return []

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        # Link mode normally avoids this; provided so --no-link mirroring works.
        for e in self.list(path):
            if e.name == name:
                r = self._client.get(e.url)
                r.raise_for_status()
                return r.content, e.url.rsplit("/", 1)[-1]
        raise FileNotFoundError(name)
