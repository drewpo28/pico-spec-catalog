"""Spectrum Computing / World of Spectrum adapters — built from the ZXDB dump.

spectrumcomputing.co.uk is the active community ZXDB archive and hosts the game
files directly (the same /pub/sinclair/games/… paths World of Spectrum used); the
worldofspectrum.net mirror serves the identical paths. The ZXInfo API can't be
bulk-crawled (503s on deep pages), so we download the full **ZXDB MySQL dump
once** (github.com/zxdb/ZXDB → ZXDB_mysql.sql.zip), stream-parse the `entries`
(id,title) and `downloads` (entry_id,file_link) tables, and join them: any
download with a playable extension becomes a catalog entry. No API rate limits,
full coverage.

Two sources share ONE parse (module-level cache of host-independent
(name, path) buckets); each adapter only prepends its own download host:
    sc   → https://spectrumcomputing.co.uk
    wos  → https://worldofspectrum.net   (Cloudflare; ECDSA-P256/SHA-256 leaf —
           fine for the device TLS config. https only: http:// 301-redirects
           and the device's HttpsGet does not follow redirects.)

Tree:  Games/<letter>/ → playable files (links to <file_base><file_link>).
"""

from __future__ import annotations

import io
import time
import zipfile

import httpx

from .base import Adapter, Entry

ZXDB_ZIP_URL = "https://github.com/zxdb/ZXDB/raw/HEAD/ZXDB_mysql.sql.zip"  # HEAD = default branch
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 3600
PLAY_TOKENS = (".tap", ".tzx", ".z80", ".sna", ".trd", ".scl",
               ".dsk", ".szx", ".udi", ".fdi")
GAMES_MARK = "/sinclair/games/"   # the playable-game subtree (substring — tolerant of
                                  # /pub/ vs /zxdb/ root and a missing leading slash;
                                  # excludes /sinclair/games-info|games-inlays/)
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
            if c in " \t\r\n":          # formatting whitespace BETWEEN values — skip
                i += 1; continue        # (whitespace inside a 'string' is kept above)
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


def _clean(s: str) -> str:
    return (s or "").replace("\t", " ").replace("\r", " ").replace("\n", " ") \
                    .replace("/", "_").strip()


def _bucket_of(title: str) -> str:
    c = title[:1].upper()
    return c if ("A" <= c <= "Z") else "0-9"


# Host-independent (name, path) buckets shared by every ZXDB-backed adapter —
# the dump is downloaded and parsed ONCE per process even when both sc and wos
# are exported in the same run.
_zxdb_cache: "tuple[float, dict[str, list[tuple[str, str]]]] | None" = None


def _zxdb_buckets(client: "httpx.Client") -> "dict[str, list[tuple[str, str]]]":
    global _zxdb_cache
    if _zxdb_cache and _zxdb_cache[0] > time.time():
        return _zxdb_cache[1]
    buckets: dict[str, list[tuple[str, str]]] = {l: [] for l in LETTERS}
    try:
        r = client.get(ZXDB_ZIP_URL)
        r.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        sqlname = next(n for n in zf.namelist() if n.lower().endswith(".sql"))
    except Exception as e:  # noqa: BLE001
        print(f"  sc: ZXDB dump download/open failed: {e}")
        _zxdb_cache = (time.time() + CACHE_TTL, buckets)
        return buckets

    # ZXDB inserts are MULTI-LINE (the VALUES tuples wrap onto following lines),
    # so we buffer each full `INSERT INTO ...;` statement before parsing. Columns
    # come from the statement's own inline list (complete-insert); CREATE TABLE is
    # not relied on. Collect entries{id:title} + game downloads (entry_id, path).
    titles: dict[str, str] = {}
    dls: list[tuple[str, str]] = []          # (entry_id, file_link)
    stats = {"ent_ins": 0, "dl_ins": 0, "ent_cols": [], "dl_cols": [], "samples": []}

    def payload(stmt: str, prefix: str):
        """(inline-columns-or-None, values-str) from a full INSERT statement."""
        rest = stmt[len(prefix):].lstrip()
        inline = None
        if rest.startswith("("):                 # complete-insert column list
            end = rest.find(")")
            if end != -1:
                inline = [c.strip().strip("` ") for c in rest[1:end].split(",")]
                rest = rest[end + 1:].lstrip()
        if rest[:6].upper() == "VALUES":
            return inline, rest[6:].lstrip()
        return None, None

    def flush(stmt: str, table: str):
        prefix = f"INSERT INTO `{table}`"
        ic, vals = payload(stmt, prefix)
        if vals is None:
            return
        if table == "entries":
            stats["ent_ins"] += 1
            c = ic or stats["ent_cols"]
            if ic and not stats["ent_cols"]:
                stats["ent_cols"] = ic
            ti = c.index("title") if "title" in c else 1
            ii = c.index("id") if "id" in c else 0
            for row in _split_rows(vals):
                if len(row) > max(ti, ii):
                    titles[row[ii]] = row[ti]
        else:
            stats["dl_ins"] += 1
            c = ic or stats["dl_cols"]
            if ic and not stats["dl_cols"]:
                stats["dl_cols"] = ic
            ei = c.index("entry_id") if "entry_id" in c else 1
            fi = c.index("file_link") if "file_link" in c else None
            if fi is None:
                return
            for row in _split_rows(vals):
                if len(row) <= max(ei, fi):
                    continue
                path = row[fi]
                if len(stats["samples"]) < 8:     # diagnostic: see real path format
                    stats["samples"].append(path)
                low = path.lower()
                if GAMES_MARK in low and any(t in low for t in PLAY_TOKENS):
                    dls.append((row[ei], path))

    buf: list[str] | None = None
    buf_table = None
    with zf.open(sqlname) as fh:
        for raw in io.TextIOWrapper(fh, encoding="utf-8", errors="replace"):
            line = raw.rstrip("\r\n")
            if buf is None:
                if line.startswith("INSERT INTO `entries`"):
                    buf, buf_table = [line], "entries"
                elif line.startswith("INSERT INTO `downloads`"):
                    buf, buf_table = [line], "downloads"
                else:
                    continue
            else:
                buf.append(line)
            if line.rstrip().endswith(";"):       # statement complete
                flush("\n".join(buf), buf_table)
                buf = buf_table = None

    print(f"  sc: ZXDB parse — ent_cols={stats['ent_cols'][:3]} ent_ins={stats['ent_ins']}; "
          f"dl_cols={stats['dl_cols'][:5]} dl_ins={stats['dl_ins']}; "
          f"titles={len(titles)} game-dls={len(dls)}")
    print(f"  sc: sample file_link values: {stats['samples']}")

    seen: dict[str, set[str]] = {l: set() for l in LETTERS}
    files = 0
    for entry_id, path in dls:
        title = _clean(titles.get(entry_id, "")) or f"#{entry_id}"
        letter = _bucket_of(title)
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
        buckets[letter].append((name, path if path.startswith("/") else "/" + path))
        files += 1

    for b in buckets.values():            # alphabetical within each letter (device
        b.sort(key=lambda p: p[0].lower())    # keeps static order — no client sort)
    print(f"  sc: {files} game files, {len(titles)} entries parsed from ZXDB dump")
    _zxdb_cache = (time.time() + CACHE_TTL, buckets)
    return buckets


class ScAdapter(Adapter):
    id = "sc"
    name = "Spectrum Computing"
    file_base = "https://spectrumcomputing.co.uk"  # active archive, serves /pub/sinclair/games/
                                                   # directly. Its ECDSA-P256 leaf (signed
                                                   # ecdsa-with-SHA384) needs MBEDTLS_SHA384_C on
                                                   # the device or the handshake fails -0x4E00.

    def __init__(self):
        self._client = httpx.Client(
            timeout=180.0, follow_redirects=True,
            headers={"User-Agent": UA},
        )
        self._index_cache: dict[str, list[Entry]] | None = None

    def _index(self) -> "dict[str, list[Entry]]":
        if self._index_cache is None:
            pairs = _zxdb_buckets(self._client)
            self._index_cache = {
                l: [Entry(False, name, 0, url=self.file_base + path)
                    for name, path in b]
                for l, b in pairs.items()
            }
        return self._index_cache

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


class WosAdapter(ScAdapter):
    """Same ZXDB catalog, downloads served by the worldofspectrum.net mirror."""
    id = "wos"
    name = "World of Spectrum"
    file_base = "https://worldofspectrum.net"
