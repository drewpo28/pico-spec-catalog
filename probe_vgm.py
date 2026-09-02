#!/usr/bin/env python3
"""One-shot diagnostic — round 8: verify the "(Systems)" name suffix live.

Runs the real adapter against small chip listings and prints the pack names
it would publish, to confirm the row-order assumption (systems anchors follow
their pack's anchors) against the live markup before the full rebuild lands.
Delete with .github/workflows/probe.yml when confirmed.
"""
from __future__ import annotations

import os

os.environ["VGM_REQ_GAP"] = "0.4"

from app.adapters.vgm import VgmAdapter  # noqa: E402

a = VgmAdapter()
for chip in ("SAA1099", "YMF262"):
    packs = a.list(chip)
    print(f"[{chip}] {len(packs)} packs; first 12 names:")
    for e in packs[:12]:
        print(f"    {e.name!r}")
print("probe done")
