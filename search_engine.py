"""
TF-IDF search engine backed by the SQLite inverted index.
Supports optional domain filtering and query expansion via stemming prefix.
"""
import logging
from typing import List, Optional
from urllib.parse import urlparse

from storage import Database, tokenize
from models import SearchResult

log = logging.getLogger(__name__)


class SearchEngine:
    def __init__(self, db: Database):
        self.db = db

    async def search(
        self,
        query: str,
        limit: int = 20,
        domain_filter: Optional[str] = None,
    ) -> List[SearchResult]:
        terms = tokenize(query)
        if not terms:
            return []

        # fetch more than needed so filtering doesn't starve results
        results = await self.db.search(terms, limit=limit * 3)

        if domain_filter:
            target = urlparse(domain_filter).netloc.lower().lstrip("www.")
            results = [
                r for r in results
                if urlparse(r.url).netloc.lower().lstrip("www.") == target
            ]

        return results[:limit]
