"""
SQLite persistence layer.

All public methods are async and serialised behind a single asyncio.Lock so
the event loop is never blocked AND SQLite never sees concurrent writers.
"""
import asyncio, hashlib, math, re, sqlite3, time
from typing import List, Optional, Tuple

import config
from models import Page, QueueItem, SearchResult

# ── Stop-word list ────────────────────────────────────────────────────────────
STOP_WORDS: frozenset = frozenset({
    "the","a","an","and","or","but","in","on","at","to","for","of","with",
    "by","from","is","are","was","were","be","been","have","has","had",
    "do","does","did","will","would","could","should","this","that","these",
    "those","i","you","he","she","it","we","they","what","which","who",
    "when","where","why","how","all","any","both","each","more","most",
    "other","some","no","not","only","same","so","than","too","very",
    "just","as","into","up","out","if","its","about",
})

# ── Helpers ───────────────────────────────────────────────────────────────────
def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:20]

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:20]

def tokenize(text: str) -> List[str]:
    tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return [t for t in tokens if len(t) >= config.MIN_TOKEN_LENGTH
            and t not in STOP_WORDS]


# ── Database ──────────────────────────────────────────────────────────────────
class Database:
    _SCHEMA = """
    PRAGMA journal_mode = WAL;
    PRAGMA synchronous  = NORMAL;
    PRAGMA cache_size   = -32000;
    PRAGMA temp_store   = MEMORY;

    CREATE TABLE IF NOT EXISTS pages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        url          TEXT    UNIQUE NOT NULL,
        url_hash     TEXT    NOT NULL,
        content_hash TEXT    DEFAULT '',
        title        TEXT    DEFAULT '',
        text_content TEXT    DEFAULT '',
        depth        INTEGER NOT NULL,
        status       TEXT    DEFAULT 'pending',
        error_msg    TEXT    DEFAULT '',
        http_status  INTEGER DEFAULT 0,
        fetched_at   REAL    DEFAULT 0,
        indexed      INTEGER DEFAULT 0,
        word_count   INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_pages_status  ON pages(status);
    CREATE INDEX IF NOT EXISTS idx_pages_indexed ON pages(indexed);

    CREATE TABLE IF NOT EXISTS queue (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        url      TEXT    UNIQUE NOT NULL,
        depth    INTEGER NOT NULL,
        added_at REAL    NOT NULL,
        status   TEXT    DEFAULT 'pending'
    );
    CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);

    CREATE TABLE IF NOT EXISTS seen_urls (
        url_hash TEXT PRIMARY KEY,
        url      TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS inverted_index (
        term    TEXT    NOT NULL,
        page_id INTEGER NOT NULL,
        tf      REAL    NOT NULL,
        PRIMARY KEY (term, page_id)
    );
    CREATE INDEX IF NOT EXISTS idx_ii_term ON inverted_index(term);

    CREATE TABLE IF NOT EXISTS crawl_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS links (
        source_id  INTEGER NOT NULL,
        target_url TEXT    NOT NULL,
        anchor     TEXT    DEFAULT '',
        PRIMARY KEY (source_id, target_url)
    );
    CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_url);
    """

    def __init__(self, path: str = config.DB_PATH):
        self._path = path
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ── Internal ──────────────────────────────────────────────────────────────
    def _conn_(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def initialize(self):
        async with self._lock:
            self._conn_().executescript(self._SCHEMA)
            # Recover items stuck as 'processing' from a previous crash
            self._conn_().execute(
                "UPDATE queue SET status='pending' WHERE status='processing'"
            )
            self._conn_().commit()

    async def clear_all(self):
        async with self._lock:
            self._conn_().executescript("""
                DELETE FROM pages;
                DELETE FROM queue;
                DELETE FROM seen_urls;
                DELETE FROM inverted_index;
                DELETE FROM links;
                DELETE FROM crawl_meta;
            """)
            self._conn_().commit()

    # ── URL deduplication ─────────────────────────────────────────────────────
    async def is_seen(self, url: str) -> bool:
        async with self._lock:
            cur = self._conn_().execute(
                "SELECT 1 FROM seen_urls WHERE url_hash=?", (url_hash(url),)
            )
            return cur.fetchone() is not None

    # ── Queue ─────────────────────────────────────────────────────────────────
    async def queue_url(self, url: str, depth: int) -> bool:
        """Add url to queue (if unseen). Returns True when newly added."""
        async with self._lock:
            uh   = url_hash(url)
            conn = self._conn_()
            if conn.execute(
                "SELECT 1 FROM seen_urls WHERE url_hash=?", (uh,)
            ).fetchone():
                return False
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO seen_urls (url_hash, url) VALUES (?,?)",
                    (uh, url)
                )
                conn.execute(
                    "INSERT OR IGNORE INTO queue (url, depth, added_at) VALUES (?,?,?)",
                    (url, depth, time.time())
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    async def dequeue_batch(self, n: int = 20) -> List[QueueItem]:
        async with self._lock:
            conn = self._conn_()
            rows = conn.execute(
                """SELECT id, url, depth, added_at FROM queue
                   WHERE status='pending'
                   ORDER BY depth ASC, added_at ASC LIMIT ?""",
                (n,)
            ).fetchall()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            conn.execute(
                f"UPDATE queue SET status='processing' "
                f"WHERE id IN ({','.join('?'*len(ids))})", ids
            )
            conn.commit()
            return [QueueItem(url=r["url"], depth=r["depth"],
                              id=r["id"], added_at=r["added_at"]) for r in rows]

    async def finish_queue_item(self, item_id: int, success: bool):
        async with self._lock:
            self._conn_().execute(
                "UPDATE queue SET status=? WHERE id=?",
                ("done" if success else "failed", item_id)
            )
            self._conn_().commit()

    async def queue_size(self) -> int:
        async with self._lock:
            return self._conn_().execute(
                "SELECT COUNT(*) FROM queue WHERE status='pending'"
            ).fetchone()[0]

    # ── Pages ─────────────────────────────────────────────────────────────────
    async def upsert_page(self, page: Page) -> int:
        async with self._lock:
            conn = self._conn_()
            uh   = url_hash(page.url)
            ch   = content_hash(page.text_content) if page.text_content else ""
            conn.execute("""
                INSERT INTO pages
                    (url,url_hash,content_hash,title,text_content,depth,
                     status,error_msg,http_status,fetched_at,word_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    title=excluded.title,
                    text_content=excluded.text_content,
                    status=excluded.status,
                    error_msg=excluded.error_msg,
                    http_status=excluded.http_status,
                    fetched_at=excluded.fetched_at,
                    word_count=excluded.word_count
            """, (page.url, uh, ch, page.title, page.text_content, page.depth,
                  page.status, page.error_msg, page.http_status,
                  page.fetched_at, page.word_count))
            conn.commit()
            return conn.execute(
                "SELECT id FROM pages WHERE url=?", (page.url,)
            ).fetchone()[0]

    async def get_unindexed_pages(self, limit: int = 50) -> List[Page]:
        async with self._lock:
            rows = self._conn_().execute(
                """SELECT id,url,depth,title,text_content FROM pages
                   WHERE status='done' AND indexed=0 AND text_content!=''
                   LIMIT ?""", (limit,)
            ).fetchall()
            return [Page(id=r["id"], url=r["url"], depth=r["depth"],
                         title=r["title"], text_content=r["text_content"])
                    for r in rows]

    async def mark_indexed(self, page_id: int):
        async with self._lock:
            self._conn_().execute(
                "UPDATE pages SET indexed=1 WHERE id=?", (page_id,)
            )
            self._conn_().commit()

    # ── Link graph ────────────────────────────────────────────────────────────
    async def store_links(self, source_id: int,
                          anchor_links: List[Tuple[str, str]]):
        """Persist outbound links with anchor text from source_id."""
        if not anchor_links:
            return
        async with self._lock:
            conn = self._conn_()
            conn.execute("DELETE FROM links WHERE source_id=?", (source_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO links (source_id, target_url, anchor) "
                "VALUES (?,?,?)",
                [(source_id, url, anchor[:200]) for url, anchor in anchor_links]
            )
            conn.commit()

    async def get_anchor_texts(self, url: str) -> List[str]:
        """Return all non-empty anchor texts pointing at *url*."""
        async with self._lock:
            rows = self._conn_().execute(
                "SELECT anchor FROM links WHERE target_url=? AND anchor!=''",
                (url,)
            ).fetchall()
            return [r["anchor"] for r in rows]

    # ── Inverted index ────────────────────────────────────────────────────────
    async def index_page_terms(self, page_id: int, entries: List[Tuple[str, float]]):
        async with self._lock:
            conn = self._conn_()
            conn.execute("DELETE FROM inverted_index WHERE page_id=?", (page_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO inverted_index (term,page_id,tf) "
                "VALUES (?,?,?)",
                [(t, page_id, tf) for t, tf in entries]
            )
            conn.commit()

    # ── Search ────────────────────────────────────────────────────────────────
    async def search(self, terms: List[str], limit: int = 20) -> List[SearchResult]:
        async with self._lock:
            conn = self._conn_()
            N = max(conn.execute(
                "SELECT COUNT(*) FROM pages WHERE status='done' AND indexed=1"
            ).fetchone()[0], 1)

            scores: dict = {}
            for term in terms:
                df = conn.execute(
                    "SELECT COUNT(DISTINCT page_id) FROM inverted_index "
                    "WHERE term=?", (term,)
                ).fetchone()[0]
                if df == 0:
                    continue
                idf = math.log((N + 1) / (df + 1)) + 1.0   # smoothed IDF
                for row in conn.execute(
                    "SELECT page_id, tf FROM inverted_index WHERE term=?",
                    (term,)
                ).fetchall():
                    scores[row["page_id"]] = (
                        scores.get(row["page_id"], 0.0) + row["tf"] * idf
                    )

            if not scores:
                return []

            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
            results = []
            for pid, tfidf_score in ranked:
                row = conn.execute(
                    "SELECT url,title,text_content,depth FROM pages WHERE id=?",
                    (pid,)
                ).fetchone()
                if row:
                    # Depth penalty: pages closer to origin rank slightly higher.
                    # depth=0 → factor=1.0, depth=1 → ~0.91, depth=5 → ~0.78
                    # Gentle log curve so deep-but-relevant pages still surface.
                    depth_factor = 1.0 / (1.0 + math.log1p(row["depth"]) * 0.15)
                    final_score  = tfidf_score * depth_factor
                    results.append(SearchResult(
                        url     = row["url"],
                        title   = row["title"] or row["url"],
                        snippet = _make_snippet(row["text_content"], terms),
                        score   = final_score,
                        depth   = row["depth"],
                        page_id = pid,
                    ))
            # Re-sort after applying depth penalty (order may shift slightly)
            results.sort(key=lambda r: r.score, reverse=True)
            return results

    # ── Stats & meta ──────────────────────────────────────────────────────────
    async def get_stats(self) -> dict:
        async with self._lock:
            conn  = self._conn_()
            stats = {}
            for row in conn.execute(
                "SELECT status, COUNT(*) cnt FROM pages GROUP BY status"
            ).fetchall():
                stats[f"pages_{row['status']}"] = row["cnt"]
            stats["queue_pending"] = conn.execute(
                "SELECT COUNT(*) FROM queue WHERE status='pending'"
            ).fetchone()[0]
            stats["pages_indexed"] = conn.execute(
                "SELECT COUNT(*) FROM pages WHERE indexed=1"
            ).fetchone()[0]
            stats["index_terms"]   = conn.execute(
                "SELECT COUNT(DISTINCT term) FROM inverted_index"
            ).fetchone()[0]
            return stats

    async def set_meta(self, key: str, value: str):
        async with self._lock:
            self._conn_().execute(
                "INSERT OR REPLACE INTO crawl_meta (key,value) VALUES (?,?)",
                (key, value)
            )
            self._conn_().commit()

    async def get_meta(self, key: str) -> Optional[str]:
        async with self._lock:
            row = self._conn_().execute(
                "SELECT value FROM crawl_meta WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None


    async def list_pages(self, status: str = "done",
                         page: int = 1, per_page: int = 20):
        """Return (rows, total_count) for paginated page browsing."""
        async with self._lock:
            conn   = self._conn_()
            offset = (page - 1) * per_page
            total  = conn.execute(
                "SELECT COUNT(*) FROM pages WHERE status=?", (status,)
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT id,url,title,depth,word_count,indexed,
                          http_status,fetched_at,status,error_msg
                   FROM pages WHERE status=?
                   ORDER BY depth ASC, id ASC
                   LIMIT ? OFFSET ?""",
                (status, per_page, offset)
            ).fetchall()
            return [dict(r) for r in rows], total

    async def export_pages(self):
        """Return all successfully crawled pages for export."""
        async with self._lock:
            rows = self._conn_().execute(
                """SELECT url,title,depth,word_count,http_status,fetched_at
                   FROM pages WHERE status='done'
                   ORDER BY depth ASC, id ASC"""
            ).fetchall()
            return [dict(r) for r in rows]


# ── Snippet helper ────────────────────────────────────────────────────────────
def _make_snippet(text: str, terms: List[str], length: int = 250) -> str:
    if not text:
        return ""
    tl = text.lower()
    best, best_n = 0, 0
    for term in terms:
        p = tl.find(term)
        if p < 0:
            continue
        win = tl[max(0, p - 60): p + 80]
        n   = sum(1 for t in terms if t in win)
        if n > best_n:
            best_n, best = n, max(0, p - 60)
    snippet = text[best: best + length]
    prefix  = "..." if best > 0 else ""
    suffix  = "..." if best + length < len(text) else ""
    return prefix + snippet + suffix
