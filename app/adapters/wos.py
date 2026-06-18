"""World of Spectrum adapter — built from the downloadable ZXDB database dump.

The public ZXInfo API can't be bulk-crawled (it 503s on any deep page and byletter
omits the file paths). Instead we download the full **ZXDB MySQL dump once**
(github.com/zxdb/ZXDB → ZXDB_mysql.sql.zip), stream-parse just the `entries`
(id,title) and `downloads` (entry_id,file_link) tables, and join them: any download
under /pub/sinclair/games/ with a playable extension becomes a catalog entry. No
API rate limits, full coverage. Files are served by the worldofspectrum.net mirror.

Tree:  Games/<letter>/ → playable files (links to worldofspectrum.net/pub/sinclair/games/…).
"""

from __future__ import annotations

import io
import time
import zipfile

import httpx

from .base import Adapter, Entry

ZXDB_ZIP_URL = "https://github.com/zxdb/ZXDB/raw/HEAD/ZXDB_mysql.sql.zip"  # HEAD = default branch
FILE_BASE = "https://worldofspectrum.net"   # serves /pub/sinclair/… directly (200)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 3600
PLAY_TOKENS = (".tap", ".tzx", ".z80", ".sna", ".trd", ".scl",
               ".dsk", ".szx", ".udi", ".fdi")
GAMES_PREFIX = "/pub/sinclair/games/"        # the playable-game subtree
LETTERS = ["0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
SECTIONS = ["Games"]


def _split_rows(vals: str):
    """Parse a MySQL extended-insert value list "(...),(...),..." into a list of
    rows (each a list of field strings; NULL/numbers kept as their literal text).
    Handles '...'-quoted strings with \\-escapes and '' doubled quotes."""
    rows: list[list[str]] = []
    i, n = 0, len(vals)
    while i < n:
        if vals[i] != "(":
            i += 1
            continue
        i += 1
        fields: list[str] = []
        buf: list[str] = []
        in_str = False
        while i < n:
            c = vals[i]
            if in_str:
                if c == "\\" and i + 1 < n:
                    buf.append(vals[i + 1]); i += 2; continue
                if c == "'":
                    if i + 1 < n and vals[i + 1] == "'":
                        buf.append("'"); i += 2; continue
                    in_str = False; i += 1; continue
                buf.append(c); i += 1; continue
            if c == "'":
                in_str = True; i += 1; continue
            if c == ",":
                fields.append("".join(buf)); buf = []; i += 1; continue
            if c == ")":
                fields.append("".join(buf)); i += 1
                rows.append(fields)
                break
            buf.append(c); i += 1
        while i < n and vals[i] != "(":   # skip to next tuple / past trailing ;
            i += 1
    return rows


class WosAdapter(Adapter):
    id = "wos"
    name = "World of Spectrum"

    def __init__(self):
        self._client = httpx.Client(
            timeout=180.0, follow_redirects=True,
            headers={"User-Agent": UA},
        )
        self._index_cache: tuple[float, dict[str, list[Entry]]] | None = None

    @staticmethod
    def _clean(s: str) -> str:
        return (s or "").replace("\t", " ").replace("\r", " ").replace("\n", " ") \
                        .replace("/", "_").strip()

    @staticmethod
    def _bucket(title: str) -> str:
        c = title[:1].upper()
        return c if ("A" <= c <= "Z") else "0-9"

    def _index(self) -> "dict[str, list[Entry]]":
        if self._index_cache and self._index_cache[0] > time.time():
            return self._index_cache[1]
        buckets: dict[str, list[Entry]] = {l: [] for l in LETTERS}
        try:
            r = self._client.get(ZXDB_ZIP_URL)
            r.raise_for_status()
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            sqlname = next(n for n in zf.namelist() if n.lower().endswith(".sql"))
        except Exception as e:  # noqa: BLE001
            print(f"  wos: ZXDB dump download/open failed: {e}")
            self._index_cache = (time.time() + CACHE_TTL, buckets)
            return buckets

        # Single streaming pass: capture column orders from CREATE TABLE, collect
        # entries{id:title} and the game downloads (entry_id, path).
        titles: dict[str, str] = {}
        dls: list[tuple[str, str]] = []          # (entry_id, file_link)
        cols: dict[str, list[str]] = {}          # table -> column names (order)
        cur_table = None                          # table whose CREATE block we're in
        ent_pin = ("INSERT INTO `entries` VALUES ")
        dl_pin = ("INSERT INTO `downloads` VALUES ")

        with zf.open(sqlname) as fh:
            for raw in io.TextIOWrapper(fh, encoding="utf-8", errors="replace"):
                line = raw.rstrip("\n")
                if cur_table:                     # inside a CREATE TABLE block
                    s = line.strip()
                    if s.startswith(")"):
                        cur_table = None
                    elif s.startswith("`"):
                        end = s.find("`", 1)
                        if end > 1:
                            cols[cur_table].append(s[1:end])
                    continue
                if line.startswith("CREATE TABLE `entries`"):
                    cur_table = "entries"; cols["entries"] = []
                elif line.startswith("CREATE TABLE `downloads`"):
                    cur_table = "downloads"; cols["downloads"] = []
                elif line.startswith(ent_pin):
                    c = cols.get("entries", [])
                    ti = c.index("title") if "title" in c else 1
                    ii = c.index("id") if "id" in c else 0
                    for row in _split_rows(line[len(ent_pin):]):
                        if len(row) > max(ti, ii):
                            titles[row[ii]] = row[ti]
                elif line.startswith(dl_pin):
                    c = cols.get("downloads", [])
                    ei = c.index("entry_id") if "entry_id" in c else 1
                    fi = c.index("file_link") if "file_link" in c else None
                    if fi is None:
                        continue
                    for row in _split_rows(line[len(dl_pin):]):
                        if len(row) <= max(ei, fi):
                            continue
                        path = row[fi]
                        low = path.lower()
                        if (low.startswith(GAMES_PREFIX)
                                and any(t in low for t in PLAY_TOKENS)):
                            dls.append((row[ei], path))

        seen: dict[str, set[str]] = {l: set() for l in LETTERS}
        files = 0
        for entry_id, path in dls:
            title = self._clean(titles.get(entry_id, "")) or f"#{entry_id}"
            letter = self._bucket(title)
            base = path.rsplit("/", 1)[-1]
            name = title
            if name in seen[letter]:
                stem = base.rsplit(".", 1)[0]
                name = f"{title} ({stem})"
                i = 2
                while name in seen[letter]:
                    name = f"{title} ({stem}) {i}"
                    i += 1
            seen[letter].add(name)
            buckets[letter].append(Entry(False, name, 0, url=FILE_BASE + path))
            files += 1

        print(f"  wos: {files} game files, {len(titles)} entries parsed from ZXDB dump")
        self._index_cache = (time.time() + CACHE_TTL, buckets)
        return buckets

    # ── RemoteFs surface ──────────────────────────────────────────────────────--
    def list(self, path: str) -> list[Entry]:
        if not path:
            return [Entry(True, s, 0) for s in SECTIONS]
        seg = path.split("/")
        if seg[0] == "Games":
            if len(seg) == 1:
                return [Entry(True, l, 0) for l in LETTERS]
            return self._index().get(seg[1], [])
        return []

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """Dynamic /v1 server only: download the entry's URL as-is."""
        url = next((e.url for e in self.list(path)
                    if not e.is_dir and e.name == name and e.url), "")
        if not url:
            raise FileNotFoundError(name)
        return self._client.get(url).content, url.rsplit("/", 1)[-1]
