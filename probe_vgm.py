#!/usr/bin/env python3
"""One-shot diagnostic for the vgm adapter — round 4: download timings.

The mirroring build times out on GETs of the direct per-track links
(/packs/vgm/…/<track>.vgz). HTML pages come back fast for the bot UA, but the
track endpoint was never probed for an actual download. Measure, with the bot
UA: a .vgz GET with and without a Referer, two in a row (rate-limit check),
and the whole-pack /files/…zip the Download button uses — to pick the right
fetch path. Delete together with .github/workflows/probe.yml when done.
"""
from __future__ import annotations

import re
import time

import httpx

BASE = "https://vgmrips.net"
UA = "pico-spec-catalog/1.0 (+https://github.com/drewpo28/pico-spec-catalog)"
PACK = f"{BASE}/packs/pack/comet-summoner-ibm-pc-at"
c = httpx.Client(timeout=45, follow_redirects=True, headers={"User-Agent": UA})


def timed(tag: str, url: str, **kw) -> None:
    t0 = time.monotonic()
    try:
        r = c.get(url, **kw)
        dt = time.monotonic() - t0
        ct = r.headers.get("content-type", "?")
        print(f"[{tag}] {r.status_code} {len(r.content)}B in {dt:.1f}s ct={ct} "
              f"head={r.content[:8]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] EXC after {time.monotonic() - t0:.1f}s: {e!r}")


r = c.get(PACK)
print(f"[pack-page] {r.status_code} len={len(r.text)}")
vgz = re.findall(r'href="(https://vgmrips\.net/packs/vgm/[^"]+\.vgz)"', r.text)
zips = re.findall(r'href="(https://vgmrips\.net/files/[^"]+\.zip)"', r.text)
print(f"  {len(vgz)} vgz links, zip={zips[:1]}")

if vgz:
    timed("vgz-1 no-referer", vgz[0])
    timed("vgz-2 no-referer", vgz[1] if len(vgz) > 1 else vgz[0])
    timed("vgz-3 referer", vgz[0], headers={"Referer": PACK})
if zips:
    timed("pack-zip", zips[0])
    timed("pack-zip range", zips[0], headers={"Range": "bytes=0-1023"})

print("probe done")
