#!/usr/bin/env python3
"""One-shot diagnostic for the vgm adapter — round 2.

Round 1 showed vgmrips.net is behind Anubis (proof-of-work anti-bot wall):
every page under a browser UA returns the challenge HTML. Anubis' default
policy only challenges User-Agents containing "Mozilla" — an honestly named
bot UA may pass. This round checks:

  1. the same pages with a non-Mozilla bot UA (and what the markup then
     really looks like: chip hrefs, pack hrefs, pagination, zip links);
  2. whether /files/ downloads are Anubis-gated too;
  3. archive.org for VGMRips pack mirrors (fallback source), incl. whether
     any mirror ships chip metadata.

Delete together with .github/workflows/probe.yml once the adapter is fixed.
"""
from __future__ import annotations

import json
import re

import httpx

BASE = "https://vgmrips.net"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
UA_BOT = "pico-spec-catalog/1.0 (+https://github.com/drewpo28/pico-spec-catalog)"


def client(ua: str) -> httpx.Client:
    return httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": ua})


def show(c: httpx.Client, tag: str, url: str) -> str:
    try:
        r = c.get(url)
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] {url} -> EXC {e}")
        return ""
    body = r.text
    anubis = "Making sure you" in body or "anubis" in body[:3000].lower()
    print(f"[{tag}] {url} -> {r.status_code} len={len(body)} anubis={anubis}")
    return body if r.status_code == 200 and not anubis else ""


def hrefs(html: str, needle: str, n: int = 30) -> "list[str]":
    out = [m.group(1) for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I)
           if needle.lower() in m.group(1).lower()]
    uniq = list(dict.fromkeys(out))
    print(f"  {len(uniq)} unique hrefs containing {needle!r}:")
    for h in uniq[:n]:
        print(f"    {h!r}")
    return uniq


# ── 1) vgmrips with a bot UA ────────────────────────────────────────────────
bot = client(UA_BOT)
t = show(bot, "bot/chips", f"{BASE}/packs/chips")
chip_urls = [f"{BASE}/packs/chip/sn76489", f"{BASE}/packs/chip/2xsn76489"]
if t:
    print(f"  head: {t[:260]!r}")
    for h in hrefs(t, "chip"):
        if re.search(r"(sn76489|saa1099|ym2413|ym3812|ymf262)", h, re.I):
            chip_urls.append(h if h.startswith("http") else BASE + (h if h.startswith("/") else "/" + h))
chip_urls = list(dict.fromkeys(chip_urls))[:8]

pack_href = ""
for u in chip_urls:
    t = show(bot, "bot/chip", u)
    if not t:
        continue
    ph = hrefs(t, "pack", 20)
    hrefs(t, "p=", 10)
    for h in ph:
        if not pack_href and "/pack" in h.lower() and "chips" not in h.lower():
            pack_href = h

if pack_href:
    if not pack_href.startswith("http"):
        pack_href = BASE + (pack_href if pack_href.startswith("/") else "/" + pack_href)
    t = show(bot, "bot/pack", pack_href)
    if t:
        hrefs(t, ".zip", 15)
        hrefs(t, "files", 15)
        hrefs(t, ".vgz", 10)

# ── 2) are /files/ downloads gated? (404 = reachable path, anubis = gated) ──
for ua, tag in ((UA_BOT, "bot"), (UA_BROWSER, "browser")):
    c = client(ua)
    try:
        r = c.get(f"{BASE}/files/probe-nonexistent-pack.zip")
        anubis = "Making sure you" in r.text[:3000]
        print(f"[{tag}/files-404-test] -> {r.status_code} len={len(r.content)} anubis={anubis}")
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}/files-404-test] -> EXC {e}")

# ── 3) archive.org mirrors of the vgmrips pack collection ───────────────────
ia = client(UA_BOT)
try:
    r = ia.get("https://archive.org/advancedsearch.php",
               params={"q": "vgmrips", "fl[]": ["identifier", "title", "item_size",
                                                "publicdate", "mediatype"],
                       "rows": "30", "output": "json"})
    docs = r.json()["response"]["docs"]
    print(f"[ia/search] {len(docs)} items for 'vgmrips':")
    for d in docs:
        print(f"    {d.get('identifier')!r} | {d.get('title')!r} | "
              f"{d.get('item_size')} | {d.get('publicdate')} | {d.get('mediatype')}")
except Exception as e:  # noqa: BLE001
    docs = []
    print(f"[ia/search] EXC {e}")

# top few items: what files do they ship (pack zips? metadata json/xml?)
for d in docs[:6]:
    ident = d.get("identifier")
    try:
        r = ia.get(f"https://archive.org/metadata/{ident}")
        j = r.json()
        files = j.get("files", [])
        print(f"[ia/meta] {ident}: server={j.get('server')} dir={j.get('dir')} "
              f"{len(files)} files; first 15:")
        for f in files[:15]:
            print(f"    {f.get('name')!r} ({f.get('size')})")
        interesting = [f["name"] for f in files
                       if re.search(r"\.(json|xml|csv|txt|sqlite)$", f.get("name", ""), re.I)]
        print(f"    metadata-ish files: {interesting[:15]}")
    except Exception as e:  # noqa: BLE001
        print(f"[ia/meta] {ident}: EXC {e}")

print("probe done")
