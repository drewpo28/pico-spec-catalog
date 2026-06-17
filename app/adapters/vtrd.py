"""Virtual TR-DOS (https://vtrd.in) adapter.

vtrd.in has no API: it is plain HTML (.php/.htm) and returns HTTP 403 to
non-browser User-Agents, so all access happens here on the server with a real
browser UA and a TTL cache (the device never touches the live site or parses
HTML). Listings are cached so repeated browsing doesn't hammer vtrd.in.

The .trd/.scl files are often inside .zip archives; fetch() transparently
unpacks the first disk/tape image so the device receives a ready-to-mount file.

URL scheme (validated against the live site 2026-06-17):
  - games are indexed by first letter via  games.php?t=<x>  where <x> is the
    lowercase letter a-z, or "123" for the digit/symbol bucket (the site labels
    it "123"); we expose it as the "0-9" directory.
  - each game row is a *direct* archive link  /gamez/<letter>/<NAME>.zip  whose
    anchor text is the human title. There is no game-detail indirection to
    follow (a release.php?r=<hash> page also exists but is not needed).

The HTTP layer treats an empty listing as "nothing here", so if vtrd ever drifts
the export degrades gracefully (empty letter) rather than crashing.
"""

from __future__ import annotations

import io
import time
import zipfile

import httpx
from selectolax.parser import HTMLParser

from .base import Adapter, Entry

BASE = "https://vtrd.in"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 1800  # seconds
DISK_EXTS = (".trd", ".scl", ".tap", ".tzx", ".z80", ".sna", ".fdi", ".udi")
LETTERS = ["0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]


class VtrdAdapter(Adapter):
    id = "vtrd"
    name = "Virtual TR-DOS"

    def __init__(self):
        self._client = httpx.Client(
            headers={"User-Agent": UA, "Accept-Language": "en,ru;q=0.8"},
            timeout=20.0, follow_redirects=True,
        )
        # path -> (expires_at, entries, {name: download_url})
        self._cache: dict[str, tuple[float, list[Entry], dict[str, str]]] = {}

    # ── caching ──────────────────────────────────────────────────────────────
    def _cached(self, path: str):
        c = self._cache.get(path)
        if c and c[0] > time.time():
            return c[1], c[2]
        entries, urlmap = self._scrape(path)
        self._cache[path] = (time.time() + CACHE_TTL, entries, urlmap)
        return entries, urlmap

    # ── scraping ─────────────────────────────────────────────────────────────
    def _get(self, url: str) -> str:
        r = self._client.get(url)
        r.raise_for_status()
        return r.text

    def _scrape(self, path: str) -> tuple[list[Entry], dict[str, str]]:
        if path == "":
            # Root: present an A-Z / 0-9 index. Each letter is a sub-directory.
            return [Entry(True, l, 0) for l in LETTERS], {}
        letter = path.split("/")[0]
        return self._scrape_letter(letter)

    def _scrape_letter(self, letter: str) -> tuple[list[Entry], dict[str, str]]:
        # The site buckets digits/symbols under "123"; letters are lowercase.
        t = "123" if letter == "0-9" else letter.lower()
        url = f"{BASE}/games.php?t={t}"
        entries: list[Entry] = []
        urlmap: dict[str, str] = {}
        try:
            html = self._get(url)
        except Exception:
            return entries, urlmap
        tree = HTMLParser(html)
        for a in tree.css("a[href]"):
            href = a.attributes.get("href", "")
            low = href.lower()
            # Direct archive links only: /gamez/<letter>/<NAME>.zip (or a bare image).
            if "/gamez/" not in low or not (low.endswith(".zip") or low.endswith(DISK_EXTS)):
                continue
            absurl = href if href.startswith("http") else f"{BASE}/{href.lstrip('/')}"
            base = href.rstrip("/").split("/")[-1]            # e.g. ATAC_SL.zip
            # Display the human title; fall back to the file name. Titles repeat
            # (multiple dumps of one game) — disambiguate with the file stem so
            # every entry maps to exactly one download in urlmap.
            title = ((a.text() or "").strip() or base).replace("/", "_")
            name = title
            if name in urlmap:
                stem = base.rsplit(".", 1)[0]
                name = f"{title} ({stem})"
                i = 2
                while name in urlmap:
                    name = f"{title} ({stem}) {i}"
                    i += 1
            # Carry the direct .zip URL on the entry so the static exporter can emit
            # a link-only catalog (device downloads + unzips). urlmap stays for the
            # dynamic /v1 server's fetch().
            entries.append(Entry(False, name, 0, url=absurl))
            urlmap[name] = absurl
        return entries, urlmap

    # ── RemoteFs surface ──────────────────────────────────────────────────────
    def list(self, path: str) -> list[Entry]:
        entries, _ = self._cached(path)
        return entries

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        _, urlmap = self._cached(path)
        url = urlmap.get(name)
        if not url:
            raise FileNotFoundError(name)
        data = self._client.get(url).content
        # If it's a game detail page (HTML), follow to the real file link.
        if data[:512].lstrip()[:1] in (b"<",) and b"<html" in data[:2048].lower():
            tree = HTMLParser(data.decode("utf-8", "replace"))
            file_url = None
            for a in tree.css("a[href]"):
                href = a.attributes.get("href", "")
                if href.lower().endswith(DISK_EXTS) or href.lower().endswith(".zip") or "d.php" in href:
                    file_url = href if href.startswith("http") else f"{BASE}/{href.lstrip('/')}"
                    break
            if file_url:
                data = self._client.get(file_url).content
        # Transparently unpack a zip to the first disk/tape image.
        if data[:2] == b"PK":
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
                inner = next((n for n in zf.namelist() if n.lower().endswith(DISK_EXTS)), None)
                if inner:
                    return zf.read(inner), inner.split("/")[-1]
            except Exception:
                pass
        return data, name
