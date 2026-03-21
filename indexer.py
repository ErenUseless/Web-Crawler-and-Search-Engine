"""
Background indexer: consumes pages with indexed=0 and builds the inverted index.
TF weighting: title terms counted 3× to boost title relevance.
"""
import asyncio, logging
from collections import Counter
from typing import List, Tuple

from storage import Database, tokenize
from models import Page

log = logging.getLogger(__name__)


class Indexer:
    def __init__(self, db: Database):
        self.db       = db
        self._running = False
        self._paused  = False   # set True to temporarily hold indexing

    # ── Public ────────────────────────────────────────────────────────────────
    async def index_page(self, page: Page) -> int:
        """Index one page. Returns number of unique terms stored.

        Weighting scheme (additive, all normalised to TF in a virtual doc):
          title        ×3  — highest signal for relevance
          anchor texts ×2  — how other pages describe this one
          body         ×1  — raw content
        """
        if not page.text_content or not page.id:
            return 0

        # Pull anchor texts written by other pages pointing here
        try:
            anchors = await self.db.get_anchor_texts(page.url)
            anchor_blob = " ".join(anchors)
        except Exception:
            anchor_blob = ""

        # Build weighted virtual document
        parts = []
        if page.title:
            parts.append((page.title + " ") * 3)
        if anchor_blob:
            parts.append((anchor_blob + " ") * 2)
        parts.append(page.text_content)
        weighted = " ".join(parts)

        entries = self._tf(weighted)
        if not entries:
            return 0
        await self.db.index_page_terms(page.id, entries)
        await self.db.mark_indexed(page.id)
        return len(entries)

    async def run_continuous(self, interval: float = 1.5):
        """Runs as a background asyncio task, indexing newly fetched pages."""
        self._running = True
        log.info("Indexer started")
        while self._running:
            if self._paused:
                await asyncio.sleep(0.1)
                continue
            pages = await self.db.get_unindexed_pages(limit=30)
            if not pages:
                await asyncio.sleep(interval)
                continue
            for page in pages:
                if self._paused:
                    break
                try:
                    n = await self.index_page(page)
                    if n:
                        log.info("Indexed  %-60s  (%d terms)", page.url[:60], n)
                except Exception as e:
                    log.error("Index error %s: %s", page.url, e)
                    try:
                        await self.db.mark_indexed(page.id)   # skip bad page
                    except Exception:
                        pass
            await asyncio.sleep(0.05)

    def pause(self):
        """Pause indexing (call before clear_all to avoid race condition)."""
        self._paused = True

    def resume_indexing(self):
        """Resume indexing after a clear_all / new crawl start."""
        self._paused = False

    def stop(self):
        self._running = False

    # ── Private ───────────────────────────────────────────────────────────────
    @staticmethod
    def _tf(text: str) -> List[Tuple[str, float]]:
        """Compute term-frequency map for text."""
        tokens = tokenize(text)
        if not tokens:
            return []
        total = len(tokens)
        return [(term, count / total)
                for term, count in Counter(tokens).items()]
