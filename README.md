# pico-spec catalog

Catalog of ZX-Spectrum disk/tape images for the
[pico-spec](https://github.com/drewpo28/pico-spec) device (RP2350 + ESP-01S) to
browse and download from internet archives. It hides every per-site difference
(HTML scraping, JSON APIs, HTTPS, unzipping) behind one trivial line protocol, so
the firmware stays thin and new sources are added **here** — no reflashing.

Two ways to serve it:

- **Serverless (default, live)** — a GitHub Action pre-renders the catalog to a
  static tree and publishes it to **GitHub Pages**; the device reads it directly over
  TLS. No always-on server. This is what `pico-spec` uses out of the box. Jump to
  [Serverless mode](#serverless-mode--static-export-to-github-pages).
- **Dynamic server** — run the FastAPI service (Docker) as an always-fresh, cached
  HTTP proxy. Useful for local development. See [Run the dynamic server](#run-the-dynamic-server).

Live catalog: <https://drewpo28.github.io/pico-spec-catalog/>

## Why a proxy/exporter (and not on-device)

- `vtrd.in` has **no API** (plain HTML) and returns **403** to non-browser
  User-Agents. Scraping + a browser UA belong on a server, not in firmware.
- Upstream downloads are **HTTPS** and often **zipped**. Resolving the real link and
  unzipping at build time means the device just GETs a plain static URL.
- Listings are pre-rendered/cached, so the device is fast and the archives aren't
  hammered.

## How the device consumes it

The firmware side is `src/HttpCatalogFs.{h,cpp}` in pico-spec (a `RemoteFs`
implementation behind **Network → F5 → Web Archives**). The device does **HTTPS
itself** (mbedTLS / `TlsSock` on the RP2350; the ESP-01S is a plain-TCP bridge).

`Config::catalog_host` (saved in `wifi.cfg`) picks the source —
`HttpCatalogFs::useStaticTree()`:

| `catalog_host` | Mode |
|----------------|------|
| *empty* (default) | static tree at the built-in `https://drewpo28.github.io/pico-spec-catalog` |
| a full `http(s)://…` base URL | static tree at that base |
| a bare `host` / `host:port` | dynamic `/v1` server (below) |

> pico-spec's *own* always-on catalog-server code was removed from the firmware; the
> `/v1` **client** path stays, so a bare `host:port` still talks to the Docker server.

## Protocol (device ⇄ dynamic server)

```
GET /v1/sites                          -> "<id>\t<display>\n"  per source
GET /v1/list?site=<s>&path=<p>         -> "F\t<name>\t<size>\n" | "D\t<name>\t0\n"
GET /v1/get?site=<s>&path=<p>&name=<n> -> raw file bytes (Content-Length set)
```

`text/plain`, tab-separated, newline-terminated. `path` is `/`-joined segments
(empty = root); addressing is path+name (FTP-style), so the server resolves
`(site, path, name)` to the real source — no opaque ids on the device.

## Run the dynamic server

```bash
docker compose up --build         # from the repo root; listens on :8080
```

On the device there is **no menu** for this — set the `catalog_host` key in
`/.config/pico-spec/wifi.cfg` on the SD card to the server as `host` or `host:port`.
Leave the key unset/empty to use the serverless GitHub-Pages tree instead (the default).

## Verify

```bash
curl 'http://localhost:8080/v1/sites'
curl 'http://localhost:8080/v1/list?site=vtrd&path=A'
curl -OJ 'http://localhost:8080/v1/get?site=vtrd&path=A&name=<file>'
```

## Adapters (`app/adapters/`)

Enable sources with `CATALOG_SITES` (comma/space list; code default `vtrd`, the
Action builds `vtrd sc zxart`). The order is the order shown in the device picker.

| id      | source                       | how |
|---------|------------------------------|-----|
| `vtrd`  | [vtrd.in](https://vtr.dscaler.ru/) | HTML scrape (no API; 403s non-browser UAs → crawl runs server-side) |
| `sc`    | [Spectrum Computing](https://spectrumcomputing.co.uk/) | listing built from the ZXDB MySQL dump; files served from `spectrumcomputing.co.uk` (device TLS handles its cert via mbedTLS `SHA384_C`) |
| `zxart` | [zxart.ee](https://zxart.ee/) | JSON export API (Games + Demoscene) |

Add a new archive by implementing `Adapter.list()` / `Adapter.fetch()` (see
`app/adapters/base.py`) and registering it in `app/adapters/__init__.py` — the
firmware needs no changes. `gen_static.py` reuses the **same adapters** as the
server, so there's no second scraper to maintain.

> Scraping selectors (e.g. `vtrd`) are best-effort — upstream sites have no stable
> contract. The API layer, caching, zip-unpacking and streaming are production shape;
> tune selectors against the live markup when a site changes.

## Serverless mode — static export to GitHub Pages

Instead of running the service 24/7, a **GitHub Action (cron, 04:17 UTC)** pre-renders
the catalog into a **static tree** and publishes it to GitHub Pages. The device
fetches it directly **over TLS** (`TlsSock` on the RP2350) — no always-on server. The
crawl runs on GitHub's runners (their IP, a real browser UA), so the device never
touches the live site.

`gen_static.py` reuses the same adapters as the server, so there's no second scraper.

### Static layout (under the Pages root)

```
sites.tsv                 "<id>\t<display>\n" per source        (== /v1/sites)
<site>/_root.tsv          root directory listing of a site
<site>/<slug>.tsv         listing of directory <path>  (slug == path, '' → _root, '/' → '~')
<site>/files/<slug>/<fn>  mirrored file bytes (the download targets)
```

### Listing line format (TAB-separated)

A **superset** of the dynamic `/v1/list` body with a 4th *locator* column so a
static client needs no server to resolve names:

```
D<TAB><name><TAB>0<TAB><child-slug>      sub-dir → GET <site>/<child-slug>.tsv
F<TAB><name><TAB><size><TAB><url>        file   → GET <url> (relative to Pages
                                         root, or absolute if it starts with http)
```

Example (`vtrd` root, committed under `static-sample/`):

```
# vtrd/_root.tsv
D	A	0	A
D	B	0	B
...
```

Files are resolved at build time: the exporter either **mirrors** the bytes (fetch()
resolves the real link and unzips to a ready `.trd`/`.tap`, written under
`<site>/files/…`) or, when an entry carries a direct URL, writes that **absolute URL**
as the locator (tiny Pages, the device downloads + unzips it itself). Either way the
device just follows the `F`-line's 4th column.

### Build it locally

```bash
pip install -r requirements.txt
python3 gen_static.py --out _site --site vtrd --max-files 400 --max-depth 2
python3 gen_static.py --out _site --site sc
python3 gen_static.py --out _site --site zxart
# _site/ is the Pages root; sites.tsv + per-site trees live there.
```

`static-sample/` is a committed, ready-to-serve example of the exporter's output
(`sites.tsv` + a `vtrd/_root.tsv` letter index).

### Deploy (one-time setup)

This repo **is** the dedicated catalog, so deployment is just enabling Pages:

1. Push this repo to GitHub (`drewpo28/pico-spec-catalog`).
2. **Settings → Pages → Source: GitHub Actions**.
3. **Actions → Build catalog (Pages) → Run workflow** (or wait for the daily cron
   at 04:17 UTC). It runs `gen_static.py` (`SITES="vtrd sc zxart"`, `MAX_FILES=400`,
   `MAX_DEPTH=4` by default) and deploys `_site/` to Pages.
4. The catalog is then live at `https://drewpo28.github.io/pico-spec-catalog/` — which
   is the device's built-in default (`catalog_host` empty).

The workflow lives at `.github/workflows/catalog.yml` (daily cron +
`workflow_dispatch` with `sites` / `max_files` / `max_depth` inputs).
