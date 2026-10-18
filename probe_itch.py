import httpx, re, json, sys
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
c = httpx.Client(headers=UA, follow_redirects=True, timeout=30)
def show(tag, s, n=6000):
    print(f"===== {tag} ({len(s)} chars) ====="); print(s[:n]); sys.stdout.flush()
r = c.get("https://leonisx.itch.io/zx-spectrum-next-db")
print("DB status", r.status_code, r.url)
p = r.text
show("DB head", p, 3000)
# description block
m = re.search(r'class="formatted_description[^"]*"(.*?)</div>\s*</div>', p, re.S)
if m: show("DB description", re.sub(r"\s+"," ",m.group(1)), 20000)
ups = re.findall(r'data-upload_id="(\d+)"', p)
print("uploads", ups)
for blk in re.findall(r'<div class="upload">.*?</div>\s*</div>', p, re.S)[:20]:
    print("UPLOAD:", re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",blk))[:300])
links = sorted(set(re.findall(r'href="(https?://[^"]+)"', p)))
print("LINKS", len(links)); [print(" ", l) for l in links[:400]]
csrf = re.search(r'name="csrf_token" value="([^"]+)"', p) or re.search(r'<meta name="csrf_token" value="([^"]+)"', p)
print("csrf", bool(csrf))
for u in ups[:6]:
    rr = c.post(f"https://leonisx.itch.io/zx-spectrum-next-db/file/{u}?source=view_game&as_props=1",
                data={"csrf_token": csrf.group(1) if csrf else ""})
    print("POST file", u, rr.status_code, rr.text[:600])
    try:
        url = rr.json().get("url")
        if url:
            h = c.get(url, headers={"Range": "bytes=0-1023"})
            print("  CDN", h.status_code, h.headers.get("content-type"), h.headers.get("content-range"), h.headers.get("content-disposition"), str(h.url)[:200])
            print("  first bytes", h.content[:64])
    except Exception as e:
        print("  err", e)
# a download page if no direct buttons
du = c.post("https://leonisx.itch.io/zx-spectrum-next-db/download_url", data={"csrf_token": csrf.group(1) if csrf else ""})
print("download_url", du.status_code, du.text[:400])
