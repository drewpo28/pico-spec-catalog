"""ZX-Art (https://zxart.ee) adapter — Games + Demoscene via the JSON export API.

Unlike vtrd (HTML scrape) zxart has a clean public JSON API, so no browser UA or
HTML parsing is needed:

  GET /api/types:zxProd/language:eng/start:N/limit:L/export:zxProd
      → productions; each carries `categoriesString` ("Games/Action/…",
        "Demoscene/Intro/…") + `releasesIds` + `title` + `year`.
  GET /api/types:zxRelease/language:eng/start:N/limit:L/export:zxRelease
      → releases; each carries `prodId`, `releaseFormat` (['trd'|'tap'|…]),
        a direct `file` URL (https://zxart.ee/release/id:<id>/<name>) and a
        `releaseStructure` whose root element gives the byte size.

We page through ALL releases once, collecting every downloadable one per prod,
then page through ALL prods, keeping only the two sections the user asked for and
bucketing each by the title's first character (0-9 + A-Z + Russian for Cyrillic
titles), like the vtrd/SC trees. A prod with a single downloadable release is a
flat file entry; one with several (languages / formats / versions) becomes a
directory whose entries are the releases themselves (named by their download
filename, best format first), so the user can pick the version.
Every file Entry carries the direct release URL → the static exporter emits it in
link mode (the device downloads the .zip itself; nothing is mirrored).

Hardware. Every release has a set of hardware tags mixing machines (zx48,
zx128, zx+3, pentagon128, samcoupe, atm2 …) with storage, OSes, sound cards and
controllers (betadisk, trdos, ay, kempston …). The export API's
`hardwareRequired` carries only a small part of them (~14% of releases have any;
the SAM Rick Dangerous of 2021 has none there although the site files it under
SAM Coupé), so the tags are read from where the site's own "Select hardware"
filter reads them:

  GET /jsonElementData/?releases=1&preset=zxProdsList
      &structureType=zxProdCategoriesCatalogue&sorting=date,asc
      &elementsOnPage=N&page=P
      → `prods` (releases, despite the name), each with `id` and
        `hardwareInfo` [{id, category: computers|storage|dos|sound|…}].

One unfiltered walk (~101k releases, ~34 pages of 3000) tags every release;
`hardwareRequired` from the export is unioned in. If the walk fails, the
catalog still builds from the export tags alone and says so on stderr — the
machine shelves come out thin, `All` is untouched. The tree is

    <shelf>/<section>/<letter>/<prod>[/<release>]

where the first shelf is `All` (every prod, the full catalog) and the others are
machines (`ZX Spectrum 48K`, `Pentagon`, `SAM Coupé` …): a prod sits on a
machine's shelf when at least one of its downloadable releases is tagged with
that machine, and inside the prod only those releases are listed.
Prod names get a `[48K|128K|AY]` tail — one label per machine shelf plus the
sound hardware, unioned over the prod's releases — so the requirement shows in
`All` too; storage, OS and controllers are left out of it. `/` is the path
separator and cannot appear in a name, hence `|`.

Built once per catalog run (cron Action), so the ~200 API calls are amortised.
"""

from __future__ import annotations

import html
import sys
import time
from urllib.parse import unquote


from .base import Adapter, Entry, http_client

API = "https://zxart.ee/api"


class _EmptyExport(Exception):
    """Empty-body 200 or HTTP 5xx — a poisoned record inside the window."""
PAGE = 1000                       # limit:5000 errors; 1000 is safe + reliable
SECTIONS = ("Games", "Demoscene")  # categoriesString first segment
LETTERS = ["Russian", "0-9"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
# Download-format preference: native TR-DOS first, then tape, then snapshots.
FMT_RANK = {"trd": 0, "scl": 1, "fdi": 2, "udi": 3, "dsk": 4,
            "tap": 5, "tzx": 6, "z80": 7, "sna": 8}

HW_URL = ("https://zxart.ee/jsonElementData/?releases=1&preset=zxProdsList"
          "&structureType=zxProdCategoriesCatalogue&sorting=date,asc")
HW_PAGE = 3000                    # ~2.5 MB / ~15 s a page; 1000 also works

ALL = "All"                       # first root shelf: the whole catalog
# Root shelves after `All`, in picker order: (shelf, tail label, site tags).
# Tags are the site's "computers" vocabulary (hardwareSelector of the page
# above). A computers tag missing here is reported on stderr at build time.
MACHINES: list[tuple[str, str, set[str]]] = [
    ("ZX Spectrum 16K",  "16K",      {"zx16"}),
    ("ZX Spectrum 48K",  "48K",      {"zx48"}),
    ("ZX Spectrum 128K", "128K",     {"zx128", "zx+2"}),
    ("ZX Spectrum +3",   "+3",       {"zx+3", "zx+2b"}),          # +2A/+2B = +3 family
    ("Pentagon",         "Pentagon", {"pentagon128", "pentagon512", "pentagon1024",
                                      "pentagon2666"}),
    ("Scorpion",         "Scorpion", {"scorpion", "scorpion1024", "gmx"}),
    ("ATM Turbo",        "ATM",      {"atm", "atm2"}),
    ("Profi",            "Profi",    {"profi"}),
    ("ZX Evolution",     "Evo",      {"zxevolution", "baseconf", "tsconf"}),
    ("eLeMeNt ZX",       "eLeMeNt",  {"elementzxmb"}),
    ("ZX-Uno",           "Uno",      {"zxuno"}),
    ("ZX Spectrum Next", "Next",     {"zxnext"}),
    ("Sprinter",         "Sprinter", {"sprinter"}),
    ("SAM Coupé",        "SAM",      {"samcoupe"}),
    ("Timex",            "Timex",    {"timex2048", "timex2068"}),
    ("Byte",             "Byte",     {"byte"}),
    ("ZXM-Phoenix",      "Phoenix",  {"zxmphoenix"}),
    ("Didaktik",         "Didaktik", {"didaktik80"}),
    ("TK-90x TK-95",     "TK",       {"tk9x"}),
    ("HC-2000",          "HC2000",   {"hc2000"}),
    ("ALF TV Game",      "ALF",      {"alf"}),
    ("Sinclair QL",      "QL",       {"sinclairql"}),
    ("ZX80 ZX81",        "ZX81",     {"zx80", "zx811", "zx812", "zx8116", "zx8132",
                                      "zx8164", "lambda8300"}),
]
# Sound hardware: no shelf of their own, but they go into the name tail after
# the machines — "why doesn't it play on my beeper-only 48K". (`ts` is
# TurboSound here, not TS-Conf: that one is `tsconf`.)
SOUND: dict[str, str] = {
    "ay": "AY", "aynext": "AY", "ts": "TS", "tsfm": "TSFM",
    "gs": "GS", "ngs": "GS", "covoxfb": "Covox", "covoxdd": "Covox", "covoxgs": "Covox",
    "soundrive": "Soundrive", "specdrum": "SpecDrum", "saa": "SAA",
    "sid": "SID", "sidelzx": "SID", "midi": "MIDI", "beeper": "Beeper",
}
_SHELF_TAGS = {shelf: tags for shelf, _l, tags in MACHINES}
_LABEL = {t: lbl for _s, lbl, tags in MACHINES for t in tags} | SOUND
_LABEL_ORDER = {lbl: i for i, lbl in enumerate(dict.fromkeys(_LABEL.values()))}


def _tail(tags: set[str]) -> str:
    """`[48K|128K|AY]` for the machine + sound tags present, '' when none."""
    labels = sorted({_LABEL[t] for t in tags if t in _LABEL}, key=_LABEL_ORDER.__getitem__)
    return f"[{'|'.join(labels)}]" if labels else ""


def _bucket(title: str) -> str:
    c = title[:1].upper()
    if "A" <= c <= "Z":
        return c
    if "А" <= c <= "Я" or c == "Ё":   # Cyrillic titles get their own shelf
        return "Russian"
    return "0-9"                       # digits + everything else (Ø, É, quotes…)


class ZxartAdapter(Adapter):
    id = "zxart"
    name = "ZX-Art"

    def __init__(self):
        # A cold (uncached) 1000-row export page takes the server 30+ s to
        # generate — a 30 s timeout made every cold page look like a failure
        # and silently truncated the crawl (hw-hit: catalog stopped at release
        # id ~447832 of ~601k, dropping every prod newer than ~2023).
        self._client = http_client(
            headers={"User-Agent": "pico-spec-catalog/1.0"},
            timeout=120.0, follow_redirects=True,
        )
        # shelf -> section -> letter -> [Entry]; built lazily on first list().
        self._index: dict[str, dict[str, dict[str, list[Entry]]]] | None = None
        # "shelf/section/letter/prod-name" -> [release Entry] for multi-release prods.
        self._prod_rel: dict[str, list[Entry]] = {}

    # ── API helpers ──────────────────────────────────────────────────────────--
    def _get(self, url: str) -> dict:
        err: Exception | None = None
        for attempt in range(5):
            try:
                r = self._client.get(url)
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001 — transient API hiccup, retry
                # A 5xx here is (empirically) as deterministic as the empty
                # body: zxProd windows around offset 43000 500-error until the
                # poisoned record is excluded. Send it to the bisect path; a
                # genuinely transient 5xx just re-resolves in the sub-windows.
                status = (getattr(getattr(e, "response", None), "status_code", None)
                          or getattr(e, "code", None))
                if isinstance(status, int) and status >= 500:
                    raise _EmptyExport(url) from e
                err = e
                time.sleep(2.0 + 2.0 * attempt)
                continue
            try:
                return r.json()
            except Exception as e:  # noqa: BLE001
                # HTTP 200 with an empty/unparseable body: the server-side
                # export serializer died on a poisoned record inside this
                # window. Deterministic — retrying the same window won't help,
                # the caller must bisect around the record instead.
                raise _EmptyExport(url) from e
        # Never mask a failed page as an empty one: an empty result ends the
        # paging loop, and a truncated catalog would silently replace the full
        # one on Pages. Raising fails the whole build instead (previous deploy
        # stays live).
        raise RuntimeError(f"zxart API failed after retries: {url}: {err!r}")

    def _window(self, entity: str, start: int, count: int) -> tuple[int, list]:
        """Rows [start, start+count) + totalAmount; bisects around poisoned rows.

        Some records crash the server's export serializer (HTTP 200, empty
        body — hw-found 2026-08-04 at zxRelease offset 89084, a release id in
        448216..448222), killing the whole window deterministically. Every
        nightly build died on that same page, which is what truncated the
        published catalog at release id 447832. Split the window down to the
        single bad record and skip just it."""
        for retry in (False, True):
            try:
                d = self._get(f"{API}/types:{entity}/language:eng/"
                              f"start:{start}/limit:{count}/export:{entity}")
                rows = (d.get("responseData") or {}).get(entity) or []
                return int(d.get("totalAmount", 0) or 0), rows
            except _EmptyExport:
                if count > 1:
                    half = count // 2
                    t1, a = self._window(entity, start, half)
                    t2, b = self._window(entity, start + half, count - half)
                    return (t1 or t2), a + b
        print(f"  ! zxart {entity}: skipping poisoned record at offset {start}",
              file=sys.stderr)
        return 0, []

    def _paged(self, entity: str):
        start = 0
        got = 0
        total = 0
        while True:
            t, rows = self._window(entity, start, PAGE)
            total = t or total
            got += len(rows)
            yield from rows
            start += PAGE
            if total == 0 or start >= total:
                break
        # Pages may legally return slightly fewer rows than `limit` (items
        # hidden from the language:eng view are dropped after slicing, ~1%),
        # and poisoned records are skipped one by one; anything beyond a few
        # percent means real truncation (or a 5xx storm skipping wholesale).
        if got < total * 0.95:
            raise RuntimeError(
                f"zxart {entity}: fetched only {got} of {total} rows — truncated export")

    @staticmethod
    def _clean(s: str) -> str:
        s = html.unescape(s or "")
        s = s.replace("\t", " ").replace("\r", " ").replace("\n", " ").replace("/", "_").strip()
        # The static listing of a prod directory is named by its slugged path
        # (`All~Games~M~<name>-<hash>.tsv`, bytes, Cyrillic → 2 '_' per char),
        # and ext4/Pages cap a file name at 255 bytes. One 240-character
        # Spanish title (Maritrini … featuring Los Fratelli) crossed it once the
        # shelf prefix and the hardware tail were added. 150 bytes of title plus
        # the worst-case prefix, year, counter and tail stays well under.
        if len(s.encode()) > 150:
            s = s.encode()[:150].decode(errors="ignore").rstrip()
        return s

    # ── index build ──────────────────────────────────────────────────────────--
    def _site_hardware(self) -> dict[int, frozenset[str]]:
        """release id -> hardware tags, as the site's hardware filter sees them.

        Best effort: any failure returns what was read so far (possibly {})
        with a warning — the shelves then lean on the export's sparse
        `hardwareRequired`, but the catalog itself is never held back by it.
        Also records the site's `computers` tags the shelf map lacks."""
        hw: dict[int, frozenset[str]] = {}
        self._new_computers: dict[str, int] = {}
        total, page = 0, 1
        try:
            while True:
                d = self._get(f"{HW_URL}&elementsOnPage={HW_PAGE}&page={page}")
                el = (d.get("responseData") or {}).get("elementData") or {}
                total = int(el.get("prodsAmount") or total)
                rows = el.get("prods") or []
                for r in rows:
                    tags = set()
                    for h in r.get("hardwareInfo") or []:
                        t = str(h.get("id", "")).lower()
                        tags.add(t)
                        if h.get("category") == "computers" and t not in _LABEL:
                            self._new_computers[t] = self._new_computers.get(t, 0) + 1
                    if r.get("id") is not None:
                        hw[int(r["id"])] = frozenset(tags)
                if not rows or page * HW_PAGE >= total:
                    break
                page += 1
        except Exception as e:  # noqa: BLE001 — degrade to the export tags
            print(f"  ! zxart hardware walk failed on page {page}: {e!r}", file=sys.stderr)
        # sorting=date,asc keeps pages stable while the walk runs; releases
        # sharing one import timestamp can still straddle a page edge.
        if len(hw) < total * 0.95:
            print(f"  ! zxart hardware walk: tagged {len(hw)} of {total} releases — "
                  "machine shelves will be incomplete", file=sys.stderr)
        return hw

    def _releases_by_prod(self) -> dict[int, list[tuple[int, str, int, str, frozenset[str]]]]:
        """prodId -> [(fmt rank, display name, byte size, URL, hardware tags)] —
        every downloadable release; the display name is the download filename
        (carries language / format / version, e.g. vsjo-kubikami-1.0-cs.trd.zip)."""
        site_hw = self._site_hardware()
        by_prod: dict[int, list[tuple[int, str, int, str, frozenset[str]]]] = {}
        for r in self._paged("zxRelease"):
            url = r.get("file") or ""
            pid = r.get("prodId")
            # A `file` with no filename (https://zxart.ee/releasefile/id:N/) is a
            # release whose upload is gone: every one probed answers 404, and
            # there were 9803 of them (12% of the catalog) before this check.
            if not url or url.endswith("/") or pid is None:
                continue
            fmts = [str(x).lower() for x in (r.get("releaseFormat") or [])]
            rank = min((FMT_RANK.get(x, 90) for x in fmts), default=90)
            size = 0
            for el in (r.get("releaseStructure") or []):
                if el.get("parentId") == 0:
                    size = int(el.get("size", 0) or 0)
                    break
            name = self._clean(unquote(url.rstrip("/").split("/")[-1])) \
                or f"release {r.get('id')}"
            tags = frozenset(str(t).lower() for t in (r.get("hardwareRequired") or [])) \
                | site_hw.get(int(r.get("id") or 0), frozenset())
            by_prod.setdefault(pid, []).append((rank, name, size, url, tags))
        return by_prod

    @staticmethod
    def _place(shelf: list[Entry], prod_rel: dict[str, list[Entry]], key: str,
               name: str, rs: list) -> None:
        """Add prod `name` to `shelf`: a flat file when it has one release, else
        a directory (listed under `key`) whose entries are the releases."""
        if len(rs) == 1:
            _rank, _rname, size, url, _tags = rs[0]
            shelf.append(Entry(False, name, size, url=url))
            return
        # several downloadable releases (languages / formats / versions):
        # the prod becomes a directory and the user picks the release.
        shelf.append(Entry(True, name, 0))
        files: list[Entry] = []
        used: set[str] = set()
        for _rank, rname, size, url, _tags in sorted(rs, key=lambda t: (t[0], t[1].lower())):
            n, i = rname, 2
            while n in used:
                n = f"{rname} ({i})"
                i += 1
            used.add(n)
            files.append(Entry(False, n, size, url=url))
        prod_rel[key] = files

    def _build(self) -> None:
        if self._index is not None:
            return
        rel = self._releases_by_prod()
        shelves = [ALL] + [s for s, _l, _t in MACHINES]
        idx: dict[str, dict[str, dict[str, list[Entry]]]] = {
            sh: {s: {l: [] for l in LETTERS} for s in SECTIONS} for sh in shelves}
        seen: dict[str, set[str]] = {s: set() for s in SECTIONS}
        prod_rel: dict[str, list[Entry]] = {}

        for p in self._paged("zxProd"):
            sec = (p.get("categoriesString") or "").split("/")[0]
            if sec not in SECTIONS:
                continue
            rs = rel.get(p.get("id"))
            if not rs:
                continue  # no downloadable release → skip
            title = self._clean(p.get("title") or "")
            if not title:
                continue
            tags: set[str] = set().union(*(r[4] for r in rs))
            tail = _tail(tags)
            year = p.get("year")

            def named(stem: str) -> str:
                return f"{stem} {tail}" if tail else stem
            name = named(title)
            if name in seen[sec]:  # disambiguate same-named prods by year, then a counter
                name = named(f"{title} ({year})") if year else name
                i = 2
                while name in seen[sec]:
                    name = named(f"{title} ({year}) {i}" if year else f"{title} {i}")
                    i += 1
            seen[sec].add(name)
            letter = _bucket(title)
            # `All` takes every release; a machine shelf takes the prod only when
            # some release requires that machine, and lists just those releases.
            self._place(idx[ALL][sec][letter], prod_rel, f"{ALL}/{sec}/{letter}/{name}", name, rs)
            for sh, want in _SHELF_TAGS.items():
                sel = [r for r in rs if r[4] & want]
                if sel:
                    self._place(idx[sh][sec][letter], prod_rel, f"{sh}/{sec}/{letter}/{name}",
                                name, sel)

        for sh in shelves:
            for sec in SECTIONS:
                for l in LETTERS:
                    idx[sh][sec][l].sort(key=lambda e: e.name.lower())
        new = getattr(self, "_new_computers", {})
        if new:  # a new machine upstream shows up here, not as a silent gap
            print("  ! zxart: computers without a shelf: "
                  + ", ".join(f"{t}×{n}" for t, n in sorted(new.items(), key=lambda kv: -kv[1])),
                  file=sys.stderr)
        self._prod_rel = prod_rel
        self._index = idx

    def _shelf_nonempty(self, sh: str) -> bool:
        return any(self._index[sh][s][l] for s in SECTIONS for l in LETTERS)

    # ── Adapter API ──────────────────────────────────────────────────────────--
    def list(self, path: str) -> list[Entry]:
        self._build()
        parts = [s for s in path.split("/") if s]
        if not parts:                                   # root → All + machines with content
            return [Entry(True, sh, 0) for sh in self._index if self._shelf_nonempty(sh)]
        sh = parts[0]
        if sh not in self._index:
            return []
        if len(parts) == 1:                             # shelf → non-empty sections
            return [Entry(True, s, 0) for s in SECTIONS
                    if any(self._index[sh][s][l] for l in LETTERS)]
        sec = parts[1]
        if sec not in SECTIONS:
            return []
        if len(parts) == 2:                             # section → non-empty letters
            return [Entry(True, l, 0) for l in LETTERS if self._index[sh][sec][l]]
        if len(parts) == 3:                             # letter → prods (files or dirs)
            return list(self._index[sh][sec].get(parts[2], []))
        if len(parts) == 4:                             # multi-release prod → releases
            return list(self._prod_rel.get("/".join(parts), []))
        return []

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """Dynamic /v1 path: download the entry's release .zip (link mode skips this)."""
        for e in self.list(path):
            if not e.is_dir and e.name == name and e.url:
                r = self._client.get(e.url)
                r.raise_for_status()
                fname = e.url.rstrip("/").split("/")[-1] or (name + ".zip")
                return r.content, fname
        raise FileNotFoundError(name)
