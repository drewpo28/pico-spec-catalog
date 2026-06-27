"""ALF (Эльф) cartridge adapter — zxbyte.org cartridge ROM dumps.

A flat list of cartridge ROM .zip archives scraped from zxbyte.org (links of the
form doc/alf/*.zip). Two source pages are scraped: alf_games.htm (the ALF-1..10
cartridges + the 1-4 compilation) and alf_dopsoft.htm (additional software/games:
ALF-11/12, L'Abbaye, Metal Man Reloaded, Test4-30…). Each is a direct .zip the
device downloads and unzips itself; the inner .rom is flashed into the shared ALF
cartridge region by the firmware (link mode → nothing mirrored, tiny Pages tree).
"""

from __future__ import annotations

import re

import httpx

from .base import Adapter, Entry

PAGES = [
    "https://zxbyte.org/alf_games.htm",     # ALF-1..10 + 1-4 compilation
    "https://zxbyte.org/alf_dopsoft.htm",   # ALF-11/12, L'Abbaye, Metal Man Reloaded, Test4-30
]
BASE = "https://zxbyte.org/"
# href="doc/alf/Alf-1_ROM.zip" (single or double quoted, any case)
HREF = re.compile(r'''href\s*=\s*["'](doc/alf/[^"']+\.zip)["']''', re.I)


def _display(href: str) -> str:
    """doc/alf/Alf-1_ROM.zip -> "ALF-1.zip"; metal_man_reloaded.zip -> "Metal Man Reloaded.zip"."""
    fn = href.rsplit("/", 1)[-1]
    name = re.sub(r"\.zip$", "", fn, flags=re.I)        # strip extension
    name = re.sub(r"_ROM$", "", name, flags=re.I)       # strip _ROM suffix
    if re.match(r"^alf", name, flags=re.I):             # ALF-N cartridges -> uppercase prefix
        name = re.sub(r"^alf", "ALF", name, flags=re.I).replace("_", " ")
    else:                                               # named games -> Title Case
        name = name.replace("_", " ").title()
    return name + ".zip"


class AlfAdapter(Adapter):
    id = "alf"
    name = "ALF cartridges"

    def __init__(self):
        self._client = httpx.Client(
            headers={"User-Agent": "Mozilla/5.0 pico-spec-catalog/1.0"},
            timeout=30.0, follow_redirects=True,
        )
        self._entries: list[Entry] | None = None

    def _load(self) -> None:
        if self._entries is not None:
            return
        out: list[Entry] = []
        seen: set[str] = set()
        for page in PAGES:                         # page order = ALF-1..10, compilation, then dopsoft
            r = self._client.get(page)
            r.raise_for_status()
            for href in HREF.findall(r.text):
                if href in seen:
                    continue
                seen.add(href)
                # Skip the raw system EPROM dumps (Original_ROM_27c256/27c010) —
                # the firmware has the system ROM built in. Everything else under
                # doc/alf/ is a cartridge dump (Alf-N or a named game).
                if "original" in href.rsplit("/", 1)[-1].lower():
                    continue
                out.append(Entry(is_dir=False, name=_display(href), size=0, url=BASE + href))
        self._entries = out

    def list(self, path: str) -> list[Entry]:
        self._load()
        return self._entries if path == "" else []   # flat: no sub-directories

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        # Link mode normally avoids this; provided so --no-link mirroring still works.
        self._load()
        for e in self._entries or []:
            if e.name == name:
                r = self._client.get(e.url)
                r.raise_for_status()
                return r.content, e.url.rsplit("/", 1)[-1]
        raise FileNotFoundError(name)
