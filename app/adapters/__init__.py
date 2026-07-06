"""Adapter registry. Sources enabled via the CATALOG_SITES env var (comma-list).

Default: "vtrd". The order here is the order shown in the device picker.
"""

from __future__ import annotations

import os

from .base import Adapter, Entry  # re-export


def build_registry() -> "dict[str, Adapter]":
    enabled = [s.strip() for s in os.environ.get("CATALOG_SITES", "vtrd").split(",") if s.strip()]
    reg: dict[str, Adapter] = {}
    for sid in enabled:
        if sid == "vtrd":
            from .vtrd import VtrdAdapter  # lazy: only this source needs httpx/selectolax
            reg["vtrd"] = VtrdAdapter()
        elif sid == "sc":
            from .sc import ScAdapter      # lazy (httpx)
            reg["sc"] = ScAdapter()
        elif sid == "wos":
            from .sc import WosAdapter     # same ZXDB parse as sc, wos.net links
            reg["wos"] = WosAdapter()
        elif sid == "zxart":
            from .zxart import ZxartAdapter  # lazy (httpx) — JSON export API
            reg["zxart"] = ZxartAdapter()
        elif sid == "alf":
            from .alf import AlfAdapter      # lazy (httpx) — zxbyte.org cartridges
            reg["alf"] = AlfAdapter()
    return reg


__all__ = ["Adapter", "Entry", "build_registry"]
