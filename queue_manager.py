"""
BFS queue manager with:
  • global URL deduplication (backed by SQLite seen_urls table)
  • backpressure when pending queue exceeds high-watermark
  • per-domain politeness delay
"""
import asyncio, logging, time
from collections import defaultdict
from typing import List
from urllib.parse import urlparse

import config
from models import QueueItem
from storage import Database

log = logging.getLogger(__name__)


class QueueManager:
    def __init__(self, db: Database):
        self.db           = db
        self._domain_ts: dict  = defaultdict(float)
        self._domain_lock = asyncio.Lock()
        self._bp_active   = False

    # ── Enqueue ───────────────────────────────────────────────────────────────
    async def enqueue(self, url: str, depth: int, max_depth: int) -> bool:
        if depth > max_depth:
            return False
        if await self._is_backpressure():
            return False
        return await self.db.queue_url(url, depth)

    async def enqueue_batch(self, urls: List[str], depth: int,
                            max_depth: int) -> int:
        if depth > max_depth:
            return 0
        if await self._is_backpressure():
            return 0
        # Single lock acquisition + single commit for the whole batch.
        # Previously called queue_url() N times: N locks, N commits.
        return await self.db.queue_urls_batch(urls, depth)

    async def _is_backpressure(self) -> bool:
        size = await self.db.queue_size()
        if size >= config.BACKPRESSURE_HIGH_WATERMARK:
            if not self._bp_active:
                log.warning("Backpressure ON  (queue=%d)", size)
            self._bp_active = True
        elif size < config.BACKPRESSURE_LOW_WATERMARK and self._bp_active:
            log.info("Backpressure OFF (queue=%d)", size)
            self._bp_active = False
        return self._bp_active

    # ── Dequeue ───────────────────────────────────────────────────────────────
    async def dequeue(self, n: int = 20) -> List[QueueItem]:
        """Always returns items immediately — backpressure only blocks enqueue."""
        return await self.db.dequeue_batch(n)

    # ── Politeness ────────────────────────────────────────────────────────────
    async def politeness_delay(self, url: str) -> None:
        """Sleep if we've recently crawled this domain."""
        domain = urlparse(url).netloc.lower()
        async with self._domain_lock:
            wait = config.DOMAIN_CRAWL_DELAY - (time.time() - self._domain_ts[domain])
            self._domain_ts[domain] = time.time() + max(wait, 0)
        if wait > 0:
            await asyncio.sleep(wait)

    async def mark_done(self, item_id: int, ok: bool):
        await self.db.finish_queue_item(item_id, ok)

    @property
    def backpressure_active(self) -> bool:
        return self._bp_active