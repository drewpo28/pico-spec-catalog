import httpx, re, json, sys, io, zipfile, collections
from urllib.parse import urlparse, unquote
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
c = httpx.Client(headers=UA, follow_redirects=True, timeout=60)
P = lambda *a: (print(*a), sys.stdout.flush())
def csrf_of(p):
    m = re.search(r'<meta name="csrf_token" value="([^"]+)"', p); return m.group(1) if m else ""
def uploads_of(p):
    out = []
    for m in re.finditer(r'<div class="upload">(.*?)</div>\s*</div>', p, re.S):
        b = m.group(1)
        uid = re.search(r'data-upload_id="(\d+)"', b)
        nm = re.search(r'class="name"[^>]*title="([^"]*)"', b) or re.search(r'class="name"[^>]*>([^<]*)<', b)
        sz = re.search(r'class="file_size"[^>]*><span>([^<]*)<', b)
        out.append((uid.group(1) if uid else None, nm.group(1) if nm else "?", sz.group(1) if sz else "?"))
    return out
def get_file(base, uid, csrf, key=None):
    q = f"{base}/file/{uid}?source=game_download" + (f"&key={key}" if key else "") if key else f"{base}/file/{uid}?source=view_game&as_props=1&after_download_lightbox=1"
    r = c.post(q, data={"csrf_token": csrf})
    try: return r.json()
    except Exception: return {"status": r.status_code, "text": r.text[:300]}

# ---- 1. the DB xlsx
base = "https://leonisx.itch.io/zx-spectrum-next-db"
p = c.get(base).text
du = c.post(base + "/download_url", data={"csrf_token": csrf_of(p)}).json()["url"]
key = unquote(du.rsplit("/", 1)[1])
dp = c.get(du).text
ups = uploads_of(dp); P("DB download page uploads", ups)
j = get_file(base, ups[0][0], csrf_of(dp), key); P("DB file json", j)
x = c.get(j["url"]); P("xlsx", x.status_code, len(x.content), str(x.url)[:160])
import openpyxl
wb = openpyxl.load_workbook(io.BytesIO(x.content), data_only=False)
games = []
for ws in wb.worksheets:
    P(f"=== sheet '{ws.title}' rows={ws.max_row} cols={ws.max_column}")
    for r_i, row in enumerate(ws.iter_rows(min_row=1, max_row=6), 1):
        vals = []
        for cell in row:
            v = cell.value
            h = cell.hyperlink.target if cell.hyperlink else None
            vals.append(f"{v!r}" + (f" <{h}>" if h else ""))
        P(f"  r{r_i}:", " | ".join(vals)[:900])
    # collect hyperlinks
    hl = collections.Counter()
    for row in ws.iter_rows():
        for cell in row:
            if cell.hyperlink and cell.hyperlink.target:
                hl[urlparse(cell.hyperlink.target).netloc.split(".",1)[-1] if "itch.io" in cell.hyperlink.target else urlparse(cell.hyperlink.target).netloc] += 1
    P("  link hosts:", hl.most_common(15))
    # value distribution of every column with few distinct values
    for col in ws.iter_cols(min_row=2):
        vs = collections.Counter(str(c_.value) for c_ in col if c_.value is not None)
        if 1 < len(vs) <= 25: P(f"  col {col[0].column_letter}: {vs.most_common(25)}")
    if ws.title.lower().startswith("game"):
        for row in ws.iter_rows(min_row=2):
            itch = [cl.hyperlink.target for cl in row if cl.hyperlink and "itch.io" in (cl.hyperlink.target or "")]
            if itch: games.append(([cl.value for cl in row], itch[0]))
P("games with itch links:", len(games))

# ---- 2. sample game pages
sample = ["https://pepperfriesgames.itch.io/tetranex", "https://7towersoftgames.itch.io/sokoback"]
sample += [g[1] for g in games[:300:25]]
for url in sample[:14]:
    try:
        r = c.get(url); p = r.text
    except Exception as e:
        P("ERR", url, e); continue
    price = re.search(r'class="(buy_btn|button buy_btn)[^"]*"[^>]*>([^<]*)', p)
    pm = re.search(r'"offers":\{[^}]*\}', p)
    ups = uploads_of(p)
    P(f"\n### {url} {r.status_code} buy={price.group(2) if price else None} offers={pm.group(0) if pm else None}")
    P("   direct uploads:", ups[:8])
    if "download_url" in p or "purchase" in p and not ups:
        P("   (has download_url/purchase path)")
    cs = csrf_of(p)
    target = ups; k = None
    if not ups:
        try:
            d = c.post(url + "/download_url", data={"csrf_token": cs}).json()
            P("   download_url ->", d)
            if "url" in d:
                k = unquote(d["url"].rsplit("/",1)[1]); dp = c.get(d["url"]).text; target = uploads_of(dp); cs = csrf_of(dp)
                P("   keyed uploads:", target[:8])
        except Exception as e: P("   dl err", e)
    for uid, nm, sz in target[:3]:
        if not uid: continue
        j = get_file(url, uid, cs, k); P("   file", uid, nm, sz, "->", str(j)[:260])
        u = j.get("url") if isinstance(j, dict) else None
        if u and nm.lower().endswith(".zip") and ("MB" not in sz or float(sz.split()[0]) < 30):
            z = c.get(u); P("   got", z.status_code, len(z.content), "final", str(z.url)[:140])
            try:
                zf = zipfile.ZipFile(io.BytesIO(z.content))
                names = zf.namelist(); P("   zip members", len(names))
                for n in names[:40]: P("     ", n, zf.getinfo(n).file_size)
            except Exception as e: P("   not zip", e)
