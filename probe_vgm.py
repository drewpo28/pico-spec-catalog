#!/usr/bin/env python3
"""One-shot diagnostic — round 7: where does the RoboCop (Data East) pack live?

The pack https://vgmrips.net/packs/pack/robocop-the-future-of-law-enforcement-
data-east (Sound chips: YM2203, YM3812) is absent from the built YM3812 dir
even after the full pagination walk. Find the chip slugs its pack page really
links, then walk those chip listings adapter-style and report on which page
(if any) the pack shows up. Also size the two chips being added to the tree
(AY-3-8910, YM2203). Delete with .github/workflows/probe.yml when solved.
"""
from __future__ import annotations

import os
import re

os.environ["VGM_REQ_GAP"] = "0.4"

from app.adapters.vgm import VgmAdapter, _PACK_A, _PAGE_P  # noqa: E402

SLUG = "robocop-the-future-of-law-enforcement-data-east"
a = VgmAdapter()

r = a._get(f"https://vgmrips.net/packs/pack/{SLUG}")
chips = re.findall(r'href="(?:https://vgmrips\.net)?/packs/chip/([^"?#]+)"', r.text)
chips = list(dict.fromkeys(chips))
print(f"[pack page] status={r.status_code} chip slugs on the page: {chips}")

for slug in list(dict.fromkeys(chips + ["ym2203", "ay-3-8910", "ym2149"])):
    p, max_p, found, total = 1, 1, None, 0
    while p <= min(max_p, 60):
        rr = a._get(f"https://vgmrips.net/packs/chip/{slug}",
                    params={"p": p} if p > 1 else None)
        ms = list(_PACK_A.finditer(rr.text))
        total += sum(1 for m in ms)
        if found is None and any(m.group(1) == SLUG for m in ms):
            found = p
        for pm in _PAGE_P.finditer(rr.text):
            max_p = max(max_p, int(pm.group(1)))
        p += 1
    print(f"[chip {slug}] pages={max_p} pack-anchors={total} robocop_on_page={found}")

print("probe done")
