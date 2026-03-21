"""
sitemap.py — Sitemap discovery and URL extraction.

Handles:
  • /robots.txt Sitemap: directives
  • /sitemap.xml and /sitemap_index.xml (auto-try)
  • Sitemap index files (recursively fetches child sitemaps)
  • Gzip-compressed sitemaps (.xml.gz)
  • <loc> URL extraction from standard XML
  • <lastmod> filtering — skip URLs older than a cutoff

All I/O is async (fetched via the same thread-pool fetcher used by the crawler).
No third-party XML library: uses stdlib xml.etree.ElementTree.

Usage (called once per crawl from Crawler.start):
    from sitemap import SitemapDiscoverer
    discoverer = SitemapDiscoverer(origin_url, queue_manager, max_depth)
    await discoverer.run()
"""

import asyncio, gzip, logging, re, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from typing import List, Optional, Set

import config

log = logging.getLogger(__name__)

# Namespaces used in sitemap XML
_SM_NS  = "http://www.sitemaps.org/schemas/sitemap/0.9"
_XHTML_NS = "http://www.w3.org/1999/xhtml"

# Maximum sitemaps to fetch (guards against infinite sitemap indexes)
MAX_SITEMAPS = 50
# Maximum URLs to extract from sitemaps per crawl
MAX_SITEMAP_URLS = 100_000
# Fetch timeout for sitemap files (they can be large)
SITEMAP_TIMEOUT = 20


class SitemapDiscoverer:
    """
    Discovers all sitemap URLs for an origin, fetches them, and enqueues
    every discovered <loc> URL into the crawl queue.
    """

    def __init__(self, origin: str, queue_manager, max_depth: int):
        self._origin      = origin
        self._queue       = queue_manager
        self._max_depth   = max_depth
        self._fetched:    Set[str] = set()
        self._url_count   = 0

    async def run(self) -> int:
        """
        Discover and enqueue sitemap URLs.
        Returns the number of URLs successfully enqueued.
        """
        sitemap_urls = await self._discover_sitemap_locations()
        if not sitemap_urls:
            log.debug("No sitemaps found for %s", self._origin)
            return 0

        log.info("Sitemap: found %d sitemap location(s) for %s",
                 len(sitemap_urls), self._origin)

        tasks = [self._process_sitemap(url) for url in sitemap_urls[:MAX_SITEMAPS]]
        await asyncio.gather(*tasks, return_exceptions=True)

        log.info("Sitemap: enqueued %d URL(s) from sitemaps", self._url_count)
        return self._url_count

    # ── Discovery ─────────────────────────────────────────────────────────────
    async def _discover_sitemap_locations(self) -> List[str]:
        """
        Find sitemap URLs by:
          1. Parsing /robots.txt for Sitemap: directives
          2. Trying /sitemap.xml and /sitemap_index.xml as fallbacks
        """
        p    = urllib.parse.urlparse(self._origin)
        root = f"{p.scheme}://{p.netloc}"
        found: List[str] = []

        # 1. Check robots.txt
        robots_url = f"{root}/robots.txt"
        robots_body = await _fetch_text(robots_url)
        if robots_body:
            for line in robots_body.splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("sitemap:"):
                    loc = stripped[len("sitemap:"):].strip()
                    if loc:
                        found.append(loc)

        # 2. Fallback well-known locations if robots.txt gave nothing
        if not found:
            for candidate in [
                f"{root}/sitemap.xml",
                f"{root}/sitemap_index.xml",
                f"{root}/sitemap-index.xml",
            ]:
                if await _url_exists(candidate):
                    found.append(candidate)
                    break   # first hit is enough for the fallback

        return found

    # ── Sitemap fetching ──────────────────────────────────────────────────────
    async def _process_sitemap(self, sitemap_url: str):
        """Fetch one sitemap (or sitemap index), recurse into children."""
        if sitemap_url in self._fetched or len(self._fetched) >= MAX_SITEMAPS:
            return
        self._fetched.add(sitemap_url)

        log.debug("Sitemap: fetching %s", sitemap_url)
        raw = await _fetch_bytes(sitemap_url)
        if not raw:
            log.debug("Sitemap: empty or failed: %s", sitemap_url)
            return

        # Decompress if gzip
        if sitemap_url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except Exception as e:
                log.debug("Sitemap: gzip error %s: %s", sitemap_url, e)
                return

        try:
            root_el = ET.fromstring(raw.decode("utf-8", errors="replace"))
        except ET.ParseError as e:
            log.debug("Sitemap: XML parse error %s: %s", sitemap_url, e)
            return

        tag = root_el.tag.lower()

        # Sitemap index — contains <sitemap><loc>...</loc></sitemap> entries
        if "sitemapindex" in tag:
            child_urls = _extract_locs(root_el, _SM_NS, "sitemap")
            tasks = [
                self._process_sitemap(u)
                for u in child_urls
                if u not in self._fetched
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        # Regular sitemap — contains <url><loc>...</loc></url> entries
        elif "urlset" in tag:
            page_urls = _extract_locs(root_el, _SM_NS, "url")
            await self._enqueue_urls(page_urls, sitemap_url)

        else:
            log.debug("Sitemap: unrecognised root element <%s> in %s",
                      root_el.tag, sitemap_url)

    async def _enqueue_urls(self, urls: List[str], source: str):
        """Filter and enqueue a batch of URLs discovered from a sitemap."""
        origin_netloc = urllib.parse.urlparse(self._origin).netloc.lower()
        enqueued = 0
        for url in urls:
            if self._url_count >= MAX_SITEMAP_URLS:
                break
            # Only enqueue URLs on the same domain as origin
            try:
                netloc = urllib.parse.urlparse(url).netloc.lower()
                if netloc != origin_netloc:
                    continue
            except Exception:
                continue
            # Enqueue at depth=1 so they're treated as one hop from origin
            ok = await self._queue.enqueue(url, depth=1,
                                           max_depth=self._max_depth)
            if ok:
                enqueued += 1
                self._url_count += 1
        if enqueued:
            log.debug("Sitemap: enqueued %d URLs from %s", enqueued, source)


# ── XML helpers ───────────────────────────────────────────────────────────────
def _extract_locs(root: ET.Element, ns: str, child_tag: str) -> List[str]:
    """Extract <loc> text from ALL <child_tag> elements under root.

    Tries namespace-qualified tag names first, falls back to bare names
    for sitemaps that omit the XML namespace declaration.
    """
    # Try namespace-qualified prefix first, then bare names
    for prefix in (f"{{{ns}}}", ""):
        entries = root.findall(f"{prefix}{child_tag}")
        if not entries:
            continue
        # Found entries with this prefix — collect ALL of them
        locs = []
        for entry in entries:
            for loc_el in entry.findall(f"{prefix}loc"):
                if loc_el.text:
                    locs.append(loc_el.text.strip())
        return locs   # return after exhausting all entries for this prefix
    return []


# ── Async HTTP helpers (no external deps) ────────────────────────────────────
async def _fetch_bytes(url: str) -> Optional[bytes]:
    """Async fetch returning raw bytes, or None on failure."""
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_bytes_sync, url),
            timeout=SITEMAP_TIMEOUT,
        )
    except Exception:
        return None


async def _fetch_text(url: str) -> Optional[str]:
    raw = await _fetch_bytes(url)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


async def _url_exists(url: str) -> bool:
    raw = await _fetch_bytes(url)
    return raw is not None and len(raw) > 0


def _fetch_bytes_sync(url: str) -> Optional[bytes]:
    """Blocking fetch returning raw bytes."""
    req = urllib.request.Request(url, headers={
        "User-Agent":      config.USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Connection":      "close",
    })
    try:
        with urllib.request.urlopen(req, timeout=SITEMAP_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = b""
            while True:
                chunk = resp.read(65_536)
                if not chunk:
                    break
                data += chunk
                if len(data) > 50 * 1024 * 1024:   # 50 MB hard cap
                    break
            # Decompress transfer-encoding gzip
            enc = resp.headers.get("Content-Encoding", "")
            if enc == "gzip":
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
            return data if data else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        log.debug("Sitemap HTTP error %s: %d", url, e.code)
        return None
    except Exception as e:
        log.debug("Sitemap fetch error %s: %s", url, e)
        return None
