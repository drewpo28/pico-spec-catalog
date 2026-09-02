#!/usr/bin/env python3
"""One-shot diagnostic for the vgm adapter — round 3.

Round 2 established: a non-Mozilla bot UA passes Anubis; chip pages live at
/packs/chip/<slug> (sn76489, saa1099, ym2413, ym3812, ymf262 confirmed) with
?p=N pagination and absolute /packs/pack/<slug> anchors; /packs/chip/2xsn76489
is 404, so the "2x…" chips-page entries link somewhere else. Round 3:

  1. ALL chip hrefs of /packs/chips (round 2 truncated at 30 of 78) + raw
     context around the ">2x" entries to see how dual-chip tags are linked;
  2. a real pack page (round 2 accidentally probed the favicon): the
     whole-pack zip link shape and any per-track .vgz links.

Delete together with .github/workflows/probe.yml once the adapter is fixed.
"""
from __future__ import annotations

import re

import httpx

BASE = "https://vgmrips.net"
UA_BOT = "pico-spec-catalog/1.0 (+https://github.com/drewpo28/pico-spec-catalog)"
c = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": UA_BOT})


def show(tag: str, url: str) -> str:
    try:
        r = c.get(url)
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] {url} -> EXC {e}")
        return ""
    print(f"[{tag}] {url} -> {r.status_code} len={len(r.text)}")
    return r.text if r.status_code == 200 else ""


def hrefs(html: str, needle: str, n: int = 200) -> "list[str]":
    out = [m.group(1) for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I)
           if needle.lower() in m.group(1).lower()]
    uniq = list(dict.fromkeys(out))
    print(f"  {len(uniq)} unique hrefs containing {needle!r}:")
    for h in uniq[:n]:
        print(f"    {h!r}")
    return uniq


def around(html: str, pattern: str, span: int = 380, count: int = 4) -> None:
    for i, m in enumerate(re.finditer(pattern, html, re.I)):
        if i >= count:
            break
        s = max(0, m.start() - 60)
        print(f"  context {i} for /{pattern}/: {html[s:m.start() + span]!r}")


# 1) chips index: full chip href list + how "2x…" entries are rendered
t = show("chips", f"{BASE}/packs/chips")
if t:
    hrefs(t, "chip", 200)
    around(t, r">\s*2\s*[x×]", 380, 6)
    around(t, r"2x?sn76489|sn76489.{0,80}2", 380, 4)

# 2) a real pack page: zip + per-track link shapes
t = show("pack", f"{BASE}/packs/pack/comet-summoner-ibm-pc-at")
if t:
    hrefs(t, ".zip", 20)
    hrefs(t, "files", 20)
    hrefs(t, ".vgz", 12)
    hrefs(t, "vgm", 20)
    around(t, r"\.zip", 420, 2)

print("probe done")
