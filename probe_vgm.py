#!/usr/bin/env python3
"""One-shot diagnostic — round 6: why does SAA1099 list 0 packs in builds?

Two consecutive builds produced zero SAA1099 pack tsvs while the raw probe of
/packs/chip/saa1099 showed a pack anchor. Run the REAL adapter against the
live site for that one chip; if it still comes up empty, dump the raw HTML of
the exact request the adapter makes (?p=1) so the regex/HTTP mismatch is
visible. Delete together with .github/workflows/probe.yml when solved.
"""
from __future__ import annotations

import os
import re

os.environ["VGM_REQ_GAP"] = "0.5"

from app.adapters.vgm import VgmAdapter, _PACK_A  # noqa: E402

a = VgmAdapter()
packs = a.list("SAA1099")
print(f"[adapter] SAA1099 -> {len(packs)} packs")
for e in packs[:10]:
    print(f"    {e.name!r}")

if not packs:
    r = a._get("https://vgmrips.net/packs/chip/saa1099", params={"p": 1})
    print(f"[raw ?p=1] status={r.status_code} len={len(r.text)}")
    hrefs = re.findall(r'href="([^"]*)"', r.text)
    pl = [h for h in hrefs if "/packs/pack/" in h]
    print(f"  {len(pl)} pack hrefs: {pl[:6]}")
    m = list(_PACK_A.finditer(r.text))
    print(f"  _PACK_A matches: {len(m)}")
    for mm in m[:5]:
        print(f"    slug={mm.group(1)!r} text={mm.group(2)[:60]!r}")
    i = r.text.find("/packs/pack/")
    print(f"  context: {r.text[max(0, i-300):i+300]!r}")

# also: one YM2413 pack listing end-to-end (sanity for track entries + fn tail)
t = a.list("YMF262")
if t:
    tr = a.list("YMF262/" + t[0].name)
    print(f"[tracks] {t[0].name!r}: {len(tr)} tracks; first url: {tr[0].url if tr else None!r}")

print("probe done")
