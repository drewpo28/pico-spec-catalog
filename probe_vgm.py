#!/usr/bin/env python3
"""One-shot diagnostic for the vgm adapter — round 5: direct-.vgz viability.

The catalog is moving to link mode (device downloads the .vgz itself), which
needs three facts about vgmrips.net checked from a runner:

  1. does a static /packs/vgm/…/track.vgz URL tolerate a dummy ?fn=/Name.vgz
     query (the s4e/tosec basename trick) — same status and identical bytes?
  2. does the download work under the DEVICE's UA ("pico-speccy/1.0 …",
     non-Mozilla → Anubis should pass it)?
  3. does the TLS endpoint accept the device's exact mbedTLS profile —
     TLS 1.2, ECDHE-RSA/ECDSA + AES-GCM (P-256/384/521, SHA-256/384) — and
     what does the cert chain look like?

Delete together with .github/workflows/probe.yml when done.
"""
from __future__ import annotations

import re
import subprocess

import httpx

BASE = "https://vgmrips.net"
UA_DEV = "pico-speccy/1.0 (+https://github.com/drewpo28/pico-speccy)"
PACK = f"{BASE}/packs/pack/comet-summoner-ibm-pc-at"
c = httpx.Client(timeout=45, follow_redirects=True, headers={"User-Agent": UA_DEV})

r = c.get(PACK)
print(f"[pack-page under device UA] {r.status_code} len={len(r.text)} "
      f"anubis={'Making sure you' in r.text[:3000]}")
vgz = re.findall(r'href="(https://vgmrips\.net/packs/vgm/[^"]+\.vgz)"', r.text)
print(f"  {len(vgz)} vgz links; first: {vgz[0] if vgz else None!r}")

if vgz:
    plain = c.get(vgz[0])
    fn = c.get(vgz[0] + "?fn=/Test_Name.vgz")
    print(f"[vgz plain]  {plain.status_code} {len(plain.content)}B head={plain.content[:4]!r}")
    print(f"[vgz ?fn=]   {fn.status_code} {len(fn.content)}B identical={fn.content == plain.content}")
    # a second query shape, in case bare '?' is stripped but '&' isn't etc.
    fn2 = c.get(vgz[0] + "?x=1&fn=/Test_Name.vgz")
    print(f"[vgz ?x&fn=] {fn2.status_code} {len(fn2.content)}B identical={fn2.content == plain.content}")

# TLS: the device's exact profile (TLS1.2 + ECDHE + AES-GCM), then cert summary.
for ciphers in ("ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:"
                "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384",):
    p = subprocess.run(
        ["openssl", "s_client", "-connect", "vgmrips.net:443", "-servername",
         "vgmrips.net", "-tls1_2", "-cipher", ciphers, "-brief"],
        input=b"", capture_output=True, timeout=30)
    print(f"[tls1.2 gcm-only] rc={p.returncode}")
    print((p.stderr or p.stdout).decode(errors="replace")[:900])

p = subprocess.run(["bash", "-c",
                    "echo | openssl s_client -connect vgmrips.net:443 -servername vgmrips.net 2>/dev/null "
                    "| openssl x509 -noout -issuer -subject -dates "
                    "-ext subjectAltName 2>/dev/null; "
                    "echo | openssl s_client -connect vgmrips.net:443 -servername vgmrips.net 2>/dev/null "
                    "| openssl x509 -noout -text 2>/dev/null | grep -E 'Signature Algorithm|Public-Key|ASN1 OID|NIST CURVE' | head"],
                   capture_output=True, timeout=60)
print("[cert]")
print(p.stdout.decode(errors="replace")[:1200])

print("probe done")
