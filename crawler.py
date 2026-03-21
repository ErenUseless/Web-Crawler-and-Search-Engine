"""
Main crawl orchestrator.

New in this revision:
  • robots.txt compliance  — checked before every fetch via RobotsChecker
  • content-hash dedup     — skip re-indexing pages whose body we've seen before
  • crawl resume           — start(resume=True) continues from a previous run
                             without wiping the database
"""
import asyncio, logging, time
from typing import Optional, Set

import config
from models import Page, CrawlStatus
from storage import Database, content_hash
from queue_manager import QueueManager
from fetcher import fetch
from parser import parse_page, normalize_url, same_domain
from robots import RobotsChecker
from sitemap import SitemapDiscoverer

log = logging.getLogger(__name__)


class Crawler:
    def __init__(self, db: Database, queue: QueueManager, indexer=None):
        self.db         = db
        self.queue      = queue
        self.robots     = RobotsChecker()
        self._indexer   = indexer   # held so we can pause it around clear_all
        self._sem       = asyncio.Semaphore(config.MAX_CONCURRENT_FETCHES)
        self._running   = False
        self._origin    = ""
        self._max_depth = 0
        self._start_ts  = 0.0
        self._active: Set[asyncio.Task] = set()
        # In-memory content-hash set for duplicate-body detection
        self._seen_content: Set[str] = set()

    # ── Public API ────────────────────────────────────────────────────────────
    async def start(self, url: str, max_depth: int,
                    same_domain_only: bool = True,
                    resume: bool = False):
        if self._running:
            log.warning("Already running – ignoring start()")
            return

        self._origin    = normalize_url(url) or url
        self._max_depth = max_depth
        self._start_ts  = time.time()
        self._running   = True
        self._sem       = asyncio.Semaphore(config.MAX_CONCURRENT_FETCHES)

        if resume:
            saved_origin = await self.db.get_meta("origin_url")
            if saved_origin:
                self._origin = saved_origin
                log.info("Resuming crawl from %s", self._origin)
            else:
                log.info("No previous crawl found — starting fresh")
                if self._indexer: self._indexer.pause()
                await self.db.clear_all()
                if self._indexer: self._indexer.resume_indexing()
        else:
            # Pause indexer BEFORE wiping DB to prevent orphan index rows
            if self._indexer: self._indexer.pause()
            await self.db.clear_all()
            self._seen_content.clear()
            if self._indexer: self._indexer.resume_indexing()

        await self.db.set_meta("origin_url", self._origin)
        await self.db.set_meta("max_depth",  str(max_depth))
        await self.db.set_meta("start_time", str(self._start_ts))

        if not resume:
            await self.queue.enqueue(self._origin, 0, max_depth)

        log.info("Crawl started  %s  depth=%d  same_domain=%s  resume=%s",
                 self._origin, max_depth, same_domain_only, resume)

        # Pre-fetch robots.txt for the origin domain in the background
        await self.robots.prefetch(self._origin)

        # Discover sitemap URLs and pre-populate the queue
        # (runs concurrently with early fetches via asyncio.create_task)
        sitemap_task = asyncio.create_task(
            SitemapDiscoverer(self._origin, self.queue, max_depth).run(),
            name="sitemap_discovery",
        )

        try:
            await self._loop(same_domain_only)
        finally:
            sitemap_task.cancel()
            try:
                await sitemap_task
            except (asyncio.CancelledError, Exception):
                pass
            self._running = False
            stats = await self.db.get_stats()
            log.info("Crawl finished — %s", stats)

    def stop(self):
        self._running = False
        for t in list(self._active):
            t.cancel()

    @property
    def is_running(self) -> bool:
        return self._running

    async def get_status(self) -> CrawlStatus:
        stats  = await self.db.get_stats()
        origin = self._origin or (await self.db.get_meta("origin_url") or "")
        return CrawlStatus(
            is_running    = self._running,
            origin_url    = origin,
            max_depth     = self._max_depth,
            pages_fetched = stats.get("pages_done", 0),
            pages_indexed = stats.get("pages_indexed", 0),
            pages_queued  = stats.get("queue_pending", 0),
            pages_failed  = stats.get("pages_error", 0),
            pages_skipped = stats.get("pages_skipped", 0),
            start_time    = self._start_ts,
        )

    # ── Internal loop ─────────────────────────────────────────────────────────
    async def _loop(self, same_domain_only: bool):
        idle_rounds = 0
        batch       = config.MAX_CONCURRENT_FETCHES * 2

        while self._running:
            items = await self.queue.dequeue(batch)

            if not items:
                if not self._active:
                    idle_rounds += 1
                    if idle_rounds >= 4:
                        break
                await asyncio.sleep(0.5)
                continue

            idle_rounds = 0
            for item in items:
                t = asyncio.create_task(
                    self._process(item, same_domain_only),
                    name=f"fetch:{item.url[:60]}",
                )
                self._active.add(t)
                t.add_done_callback(self._active.discard)

            await asyncio.sleep(0.05)

        if self._active:
            await asyncio.gather(*self._active, return_exceptions=True)

    # ── Per-page worker ───────────────────────────────────────────────────────
    async def _process(self, item, same_domain_only: bool):
        url, depth = item.url, item.depth
        async with self._sem:

            # ── robots.txt check ─────────────────────────────────────────────
            if not await self.robots.allowed(url):
                log.debug("[robots] blocked %s", url)
                await self.db.upsert_page(Page(
                    url=url, depth=depth, status="skipped",
                    error_msg="blocked by robots.txt",
                    fetched_at=time.time(),
                ))
                await self.queue.mark_done(item.id, True)
                return

            # ── politeness — robots crawl-delay overrides global config ───────
            domain_delay = await self.robots.crawl_delay(url)
            if domain_delay > 0:
                await asyncio.sleep(domain_delay)
            else:
                await self.queue.politeness_delay(url)

            log.debug("[d%d] → %s", depth, url)
            result = await fetch(url)

            page = Page(
                url         = url,
                depth       = depth,
                status      = "done" if result.ok else "error",
                http_status = result.status,
                error_msg   = result.error,
                fetched_at  = time.time(),
            )

            if result.ok and result.is_html and result.content:
                try:
                    parsed = parse_page(result.final_url, result.content)
                    page.title      = parsed.title
                    page.word_count = len(parsed.text.split())

                    # ── meta robots: noindex ───────────────────────────────────
                    if parsed.noindex:
                        log.debug("  ↳ noindex meta tag: %s", url)
                        page.status    = "skipped"
                        page.error_msg = "meta noindex"
                    else:
                        # ── content-hash deduplication ────────────────────────
                        ch = content_hash(parsed.text)
                        if ch in self._seen_content:
                            log.debug("  ↳ duplicate content, not indexing: %s", url)
                            page.status    = "skipped"
                            page.error_msg = "duplicate content"
                        else:
                            self._seen_content.add(ch)
                            page.text_content = parsed.text

                    # ── link discovery ─────────────────────────────────────────
                    # Respect meta nofollow: skip link following but still
                    # record the page itself.
                    if not parsed.nofollow and depth < self._max_depth:
                        candidates = [
                            lnk for lnk in parsed.links
                            if not same_domain_only
                            or same_domain(lnk, self._origin)
                        ]
                        added = await self.queue.enqueue_batch(
                            candidates, depth + 1, self._max_depth
                        )
                        log.debug("  ↳ %d/%d new links queued",
                                  added, len(candidates))

                except Exception as e:
                    log.error("Parse error %s: %s", url, e)
                    page.status    = "error"
                    page.error_msg = str(e)
                    parsed         = None

            elif result.ok and not result.is_html:
                page.status = "skipped"
                parsed      = None
            else:
                parsed = None

            pid = await self.db.upsert_page(page)

            # ── Store link graph (for anchor-text indexing) ────────────────────
            if parsed is not None and page.status != "error":
                await self.db.store_links(pid, parsed.anchor_links)

            await self.queue.mark_done(item.id, result.ok)
