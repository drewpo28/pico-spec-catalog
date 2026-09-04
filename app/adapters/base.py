"""Adapter contract shared by every catalog source.

The HTTP layer (app/main.py) is source-agnostic: it only ever calls `list()` and
`fetch()` and serialises the result into the tiny line protocol the pico-spec
device understands. All per-site knowledge (HTML scraping, JSON APIs, download
URL resolution, unzipping) lives inside an Adapter, so new sources are added here
without ever touching the firmware.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Entry:
    """One directory entry. `size` is best-effort (0 if unknown)."""
    is_dir: bool
    name: str
    size: int = 0
    # Direct download URL on the source site (when the file need not be mirrored —
    # the device fetches + unzips it itself). Empty when the source has no stable
    # direct link (then the static exporter mirrors the bytes instead).
    url: str = ""


class Adapter:
    id: str = ""        # stable identifier used in ?site=<id>
    name: str = ""      # human-readable label shown in the device picker

    def list(self, path: str) -> list[Entry]:
        """Entries of the directory `path` ("" = root). '/'-joined segments."""
        raise NotImplementedError

    def fetch(self, path: str, name: str) -> tuple[bytes, str]:
        """Return (file_bytes, download_filename) for `name` inside `path`."""
        raise NotImplementedError


class SourceDown(BaseException):
    """The source is unusable right now — stop the whole export of that site.

    Deliberately NOT an Exception: gen_static's per-directory handler swallows
    Exceptions and writes an empty listing, so a mid-crawl failure would publish
    a truncated catalog (a few directories full, the rest empty). Raising this
    instead aborts the site, and gen_static then falls back to the tree already
    live on Pages rather than deploying the wreckage.

    Raise it when the failure is wholesale — an index page that will not load or
    parse, a CDN block that outlasts every backoff, a crawl whose errors have
    stopped looking like the odd dead link — not for a single missing file.
    Aborting early also spares the runner a doomed retry per directory: on
    2026-09-04 spectrum3.es answered nothing at all and sp3 spent four hours
    re-proving it, once per letter, before the empty-tree guard failed the build.
    """


def http_client(**kw):
    """An httpx.Client pinned to IPv4 — the only way any adapter should make one.

    Sources are reached over IPv4 on purpose. httpx (unlike curl) has no Happy
    Eyeballs: it connects to the FIRST address getaddrinfo returns and gives up
    on failure. Several sources publish AAAA records (spectrum3.es and
    worldofspectrum.net today, any of them tomorrow), and a GitHub Actions runner
    has no route to the IPv6 internet — so whenever the resolver puts the AAAA
    first, every request dies instantly with "[Errno 101] Network is
    unreachable". That is what silently emptied the sp3 tree on 2026-09-04 and
    burned four hours of runner time retrying it.

    Binding the socket to the wildcard IPv4 address forces AF_INET, so only the
    A records are ever tried. Every source has A records; nothing is lost.

    httpx is imported lazily, keeping `base` importable without the scrape deps.
    """
    import httpx
    kw.setdefault("transport", httpx.HTTPTransport(local_address="0.0.0.0"))
    return httpx.Client(**kw)
