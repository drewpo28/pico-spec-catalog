#!/usr/bin/env python3
"""One-shot diagnostic for the vgm adapter — dump vgmrips.net markup shapes.

The first live run of the vgm adapter came back with 0 packs for every chip
(pages answered, selectors matched nothing), and vgmrips.net is unreachable
from the dev sandbox. This runs on a GitHub runner (probe workflow) and prints
the raw link shapes of the chips index, chip pages and one pack page, so the
adapter's regexes/slugs can be fixed against reality. Delete together with
.github/workflows/probe.yml once the adapter is validated.
"""
from __future__ import annotations

import re

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE = "https://vgmrips.net"
c = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": UA})


def show(tag: str, url: str) -> str:
    try:
        r = c.get(url)
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] {url} -> EXC {e}")
        return ""
    print(f"[{tag}] {url} -> {r.status_code} final={r.url} len={len(r.text)}")
    return r.text if r.status_code == 200 else ""


def hrefs(html: str, needle: str, n: int = 40) -> "list[str]":
    out = [m.group(1) for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I)
           if needle.lower() in m.group(1).lower()]
    uniq = list(dict.fromkeys(out))
    print(f"  {len(uniq)} unique hrefs containing {needle!r}:")
    for h in uniq[:n]:
        print(f"    {h!r}")
    return uniq


def around(html: str, needle: str, span: int = 320, count: int = 2) -> None:
    for i, m in enumerate(re.finditer(re.escape(needle), html, re.I)):
        if i >= count:
            break
        s = max(0, m.start() - span // 3)
        print(f"  context around {needle!r}: {html[s:m.start() + span]!r}")


# 1) chips index — what do chip links actually look like?
t = show("chips", f"{BASE}/packs/chips")
chip_hrefs: "list[str]" = []
if t:
    print(f"  head: {t[:300]!r}")
    chip_hrefs = hrefs(t, "chip")
    for k in ("sn76489", "saa1099", "2x", "YM3812"):
        around(t, k, 300, 1)

# 2) chip pages — guessed slugs + whatever the chips index really links to
cands = [f"{BASE}/packs/chip/sn76489", f"{BASE}/packs/chip/2xsn76489"]
for h in chip_hrefs:
    if re.search(r"(sn76489|saa1099|ym2413|ym3812|ymf262)", h, re.I):
        cands.append(h if h.startswith("http") else BASE + (h if h.startswith("/") else "/" + h))
cands = list(dict.fromkeys(cands))[:8]

pack_href = ""
for u in cands:
    t = show("chip", u)
    if not t:
        continue
    ph = hrefs(t, "pack", 25)
    hrefs(t, "p=", 12)
    around(t, "/packs/pack", 420, 1)
    if not ph:                      # nothing pack-ish → show generic anchors
        hrefs(t, "/", 25)
    for h in ph:
        if not pack_href and "/pack" in h.lower() and "chips" not in h.lower():
            pack_href = h

# 3) one pack page — where is the whole-pack zip / per-track files?
if pack_href:
    if not pack_href.startswith("http"):
        pack_href = BASE + (pack_href if pack_href.startswith("/") else "/" + pack_href)
    t = show("pack", pack_href)
    if t:
        hrefs(t, ".zip", 20)
        hrefs(t, "files", 20)
        hrefs(t, ".vgz", 12)
        around(t, ".zip", 420, 2)

print("probe done")
