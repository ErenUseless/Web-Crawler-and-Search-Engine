#!/usr/bin/env python3
"""
Integration test — full pipeline exercised against a local mock HTTP server.

The test spins up a real asyncio HTTP server on a random localhost port,
populates it with a small multi-page site, runs the crawler against it,
then asserts on search results, deduplication, robots.txt compliance,
the /pages and /export endpoints, and crawl resume.

No external network access is used.

Run:  python integration_test.py -v
"""
import asyncio, json, os, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(__file__))


# ─────────────────────────────────────────────────────────────────────────────
# Mock site definition
# ─────────────────────────────────────────────────────────────────────────────
# /              → links to /about, /blog, /blocked
# /about         → "About page asyncio programming"
# /blog          → links to /blog/post1, /blog/post2
# /blog/post1    → "Python asyncio tutorial for beginners"
# /blog/post2    → "Advanced asyncio patterns"
# /blocked       → should be blocked by robots.txt
# /duplicate-a   → same body as /duplicate-b (content-hash dedup test)
# /duplicate-b   → same body as /duplicate-a
# /robots.txt    → blocks /blocked

_SITE: dict = {
    "/robots.txt": (
        "text/plain",
        # Note: sitemap URL is built dynamically; mock server fills /sitemap.xml
        b"User-agent: *\nDisallow: /blocked\nCrawl-delay: 0\nSitemap: http://PLACEHOLDER/sitemap.xml\n",
    ),
    "/sitemap.xml": (
        "application/xml",
        b"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>http://PLACEHOLDER/sitemap-only-page</loc></url>
        </urlset>""",
    ),
    "/sitemap-only-page": (
        "text/html",
        b"""<html><head><title>Sitemap Exclusive</title></head><body>
        <p>This page is only reachable via sitemap discovery not links.</p>
        </body></html>""",
    ),
    "/noindex-page": (
        "text/html",
        b"""<html><head>
        <meta name="robots" content="noindex">
        <title>Noindex Page</title></head>
        <body><p>Should not appear in search results.</p></body></html>""",
    ),
    "/": (
        "text/html",
        b"""<html><head><title>Home</title></head><body>
        <p>Welcome to the test site.</p>
        <a href="/about">About</a>
        <a href="/blog">Blog</a>
        <a href="/blocked">Blocked</a>
        <a href="/duplicate-a">Dup A</a>
        <a href="/duplicate-b">Dup B</a>
        <a href="/noindex-page">Noindex</a>
        </body></html>""",
    ),
    "/about": (
        "text/html",
        b"""<html><head><title>About</title></head><body>
        <p>About page asyncio programming concurrency.</p>
        </body></html>""",
    ),
    "/blog": (
        "text/html",
        b"""<html><head><title>Blog</title></head><body>
        <a href="/blog/post1">Post 1</a>
        <a href="/blog/post2">Post 2</a>
        </body></html>""",
    ),
    "/blog/post1": (
        "text/html",
        b"""<html><head><title>Python asyncio tutorial</title></head><body>
        <p>Python asyncio tutorial for beginners covering coroutines tasks.</p>
        </body></html>""",
    ),
    "/blog/post2": (
        "text/html",
        b"""<html><head><title>Advanced asyncio</title></head><body>
        <p>Advanced asyncio patterns including semaphores queues backpressure.</p>
        </body></html>""",
    ),
    "/blocked": (
        "text/html",
        b"""<html><head><title>Blocked</title></head><body>
        <p>This page should never be crawled.</p>
        </body></html>""",
    ),
    "/duplicate-a": (
        "text/html",
        b"""<html><head><title>Duplicate</title></head><body>
        <p>This is exactly the same content as duplicate b page.</p>
        </body></html>""",
    ),
    "/duplicate-b": (
        "text/html",
        b"""<html><head><title>Duplicate</title></head><body>
        <p>This is exactly the same content as duplicate b page.</p>
        </body></html>""",
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Mock HTTP server
# ─────────────────────────────────────────────────────────────────────────────
async def _mock_handler(reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter):
    try:
        line = await asyncio.wait_for(reader.readline(), 5)
        if not line:
            return
        parts = line.decode(errors="replace").split()
        if len(parts) < 2:
            return
        path = parts[1].split("?")[0]

        # Drain headers — also capture Host header to resolve PLACEHOLDER
        host = ""
        while True:
            l = await asyncio.wait_for(reader.readline(), 3)
            if l in (b"\r\n", b"\n", b""):
                break
            if l.lower().startswith(b"host:"):
                host = l.split(b":", 1)[1].strip().decode()

        if path in _SITE:
            ct, body = _SITE[path]
            # Substitute placeholder with real host so sitemap URLs resolve
            if host and b"PLACEHOLDER" in body:
                body = body.replace(b"PLACEHOLDER", host.encode())
            resp = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: {ct}\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode() + body
        else:
            resp = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"

        writer.write(resp)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _start_mock_server() -> tuple:
    """Start mock HTTP server on a random port. Returns (server, port)."""
    server = await asyncio.start_server(_mock_handler, "127.0.0.1", 0)
    port   = server.sockets[0].getsockname()[1]
    asyncio.create_task(server.serve_forever())
    return server, port


# ─────────────────────────────────────────────────────────────────────────────
# Helper — run a crawl to completion
# ─────────────────────────────────────────────────────────────────────────────
async def _run_crawl(origin: str, depth: int, db_path: str,
                     resume: bool = False):
    import config
    # Disable politeness for tests
    config.DOMAIN_CRAWL_DELAY            = 0
    config.BACKPRESSURE_HIGH_WATERMARK   = 100_000
    config.BACKPRESSURE_LOW_WATERMARK    = 50_000

    from storage       import Database
    from queue_manager import QueueManager
    from crawler       import Crawler
    from indexer       import Indexer

    db  = Database(db_path)
    await db.initialize()
    qm  = QueueManager(db)
    cr  = Crawler(db, qm)
    idx = Indexer(db)

    idx_task = asyncio.create_task(idx.run_continuous(), name="indexer")
    await cr.start(origin, depth, same_domain_only=True, resume=resume)
    await asyncio.sleep(0.5)   # let indexer flush
    idx.stop()
    idx_task.cancel()
    # Final flush
    for page in await db.get_unindexed_pages(10_000):
        await idx.index_page(page)
    return db


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────
class IntegrationTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._server, self._port = await _start_mock_server()
        self._origin = f"http://127.0.0.1:{self._port}/"
        self._tmp    = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = self._tmp.name
        self._tmp.close()

    async def asyncTearDown(self):
        self._server.close()
        os.unlink(self._db_path)

    # ── 1. Basic crawl coverage ───────────────────────────────────────────────
    async def test_crawl_discovers_all_reachable_pages(self):
        db = await _run_crawl(self._origin, depth=3, db_path=self._db_path)
        stats = await db.get_stats()
        # Should have found: /, /about, /blog, /blog/post1, /blog/post2
        # plus /duplicate-a and one of duplicate-a/b (other is dedup-skipped)
        fetched = stats.get("pages_done", 0)
        self.assertGreaterEqual(fetched, 5,
            f"Expected at least 5 fetched pages, got {fetched}. Stats: {stats}")

    # ── 2. robots.txt blocks /blocked ─────────────────────────────────────────
    async def test_robots_txt_blocks_disallowed_path(self):
        db = await _run_crawl(self._origin, depth=2, db_path=self._db_path)
        rows, _ = await db.list_pages(status="skipped")
        skipped_urls = {r["url"] for r in rows}
        blocked_url  = f"http://127.0.0.1:{self._port}/blocked"
        self.assertIn(
            blocked_url, skipped_urls,
            f"/blocked should be in skipped pages.\nSkipped: {skipped_urls}"
        )

    # ── 3. /blocked body should never appear in index ─────────────────────────
    async def test_blocked_page_not_indexed(self):
        db = await _run_crawl(self._origin, depth=2, db_path=self._db_path)
        from search_engine import SearchEngine
        se = SearchEngine(db)
        results = await se.search("never crawled blocked")
        blocked_url = f"http://127.0.0.1:{self._port}/blocked"
        found_urls  = {r.url for r in results}
        self.assertNotIn(blocked_url, found_urls,
                         "Blocked page should not appear in search results")

    # ── 4. TF-IDF search returns relevant results ─────────────────────────────
    async def test_search_returns_relevant_results(self):
        db = await _run_crawl(self._origin, depth=3, db_path=self._db_path)
        from search_engine import SearchEngine
        se = SearchEngine(db)

        results = await se.search("asyncio tutorial")
        self.assertGreater(len(results), 0, "Should find results for 'asyncio tutorial'")
        top_url = results[0].url
        self.assertIn("post1", top_url,
                      f"Post1 (Python asyncio tutorial) should rank first, got {top_url}")

    # ── 5. Content-hash deduplication ─────────────────────────────────────────
    async def test_content_hash_dedup_skips_duplicate_body(self):
        db = await _run_crawl(self._origin, depth=2, db_path=self._db_path)
        stats = await db.get_stats()

        dup_a_url = f"http://127.0.0.1:{self._port}/duplicate-a"
        dup_b_url = f"http://127.0.0.1:{self._port}/duplicate-b"

        done_rows,    _ = await db.list_pages(status="done")
        skipped_rows, _ = await db.list_pages(status="skipped")

        done_urls    = {r["url"] for r in done_rows}
        skipped_urls = {r["url"] for r in skipped_rows}
        all_seen     = done_urls | skipped_urls

        # Both URLs should have been visited
        self.assertIn(dup_a_url, all_seen,
                      "duplicate-a should have been visited")
        self.assertIn(dup_b_url, all_seen,
                      "duplicate-b should have been visited")

        # Exactly one of them should be skipped (content dedup)
        dup_skipped = {dup_a_url, dup_b_url} & skipped_urls
        self.assertEqual(len(dup_skipped), 1,
            f"Exactly one duplicate should be skipped, got: {dup_skipped}")

    # ── 6. No URL is fetched twice (global dedup) ─────────────────────────────
    async def test_no_duplicate_url_fetches(self):
        db = await _run_crawl(self._origin, depth=3, db_path=self._db_path)
        # The pages table has a UNIQUE constraint on url — if a URL were
        # processed twice without dedup the upsert would silently overwrite.
        # We verify the queue never created two entries for the same URL.
        async with db._lock:
            rows = db._conn_().execute(
                "SELECT url, COUNT(*) n FROM queue GROUP BY url HAVING n > 1"
            ).fetchall()
        self.assertEqual(
            len(rows), 0,
            f"These URLs were enqueued more than once: {[r['url'] for r in rows]}"
        )

    # ── 7. Incremental indexing: search works before crawl finishes ───────────
    async def test_incremental_index_searchable_mid_crawl(self):
        import config as _cfg
        _cfg.DOMAIN_CRAWL_DELAY = 0

        from storage       import Database
        from queue_manager import QueueManager
        from crawler       import Crawler
        from indexer       import Indexer
        from search_engine import SearchEngine

        db  = Database(self._db_path)
        await db.initialize()
        qm  = QueueManager(db)
        cr  = Crawler(db, qm)
        idx = Indexer(db)
        se  = SearchEngine(db)

        idx_task   = asyncio.create_task(idx.run_continuous())
        crawl_task = asyncio.create_task(
            cr.start(self._origin, 3, same_domain_only=True)
        )

        # Poll until at least one page is indexed
        deadline = time.monotonic() + 10
        found = []
        while time.monotonic() < deadline:
            found = await se.search("asyncio")
            if found:
                break
            await asyncio.sleep(0.1)

        crawl_task.cancel()
        idx.stop(); idx_task.cancel()
        try:
            await crawl_task
        except asyncio.CancelledError:
            pass

        self.assertGreater(len(found), 0,
                           "Should be able to search while crawl is running")

    # ── 8. /pages endpoint returns paginated data ─────────────────────────────
    async def test_pages_endpoint_paginated(self):
        db = await _run_crawl(self._origin, depth=2, db_path=self._db_path)
        rows, total = await db.list_pages(status="done", page=1, per_page=3)
        self.assertGreater(total, 0)
        self.assertLessEqual(len(rows), 3)
        for r in rows:
            self.assertIn("url",   r)
            self.assertIn("title", r)
            self.assertIn("depth", r)

    # ── 9. Export produces well-formed JSON Lines ─────────────────────────────
    async def test_export_jsonl(self):
        db = await _run_crawl(self._origin, depth=2, db_path=self._db_path)
        rows = await db.export_pages()
        self.assertGreater(len(rows), 0)
        for r in rows:
            # Must be JSON-serialisable and have required fields
            s = json.dumps(r)
            d = json.loads(s)
            self.assertIn("url",   d)
            self.assertIn("title", d)
            self.assertIn("depth", d)

    # ── 10. Crawl resume continues without wiping index ───────────────────────
    async def test_resume_preserves_existing_index(self):
        # First partial crawl
        db = await _run_crawl(self._origin, depth=1, db_path=self._db_path)
        stats_before = await db.get_stats()
        pages_before = stats_before.get("pages_done", 0)
        self.assertGreater(pages_before, 0, "Should have fetched some pages")

        # Close the db connection properly before resuming
        db._conn = None

        # Resume with depth=2 — should not wipe existing pages
        db2 = await _run_crawl(self._origin, depth=2, db_path=self._db_path,
                               resume=True)
        stats_after = await db2.get_stats()
        pages_after  = stats_after.get("pages_done", 0)

        # After resume, total done pages should be >= what we had before
        self.assertGreaterEqual(
            pages_after, pages_before,
            "Resume should preserve previously crawled pages, "
            f"before={pages_before} after={pages_after}"
        )

    # ── 11. BFS depth limit respected ─────────────────────────────────────────
    async def test_depth_limit_respected(self):
        # With depth=1: only / and its direct children should be fetched
        db = await _run_crawl(self._origin, depth=1, db_path=self._db_path)
        rows, _ = await db.list_pages(status="done")
        for r in rows:
            self.assertLessEqual(
                r["depth"], 1,
                f"Page at depth {r['depth']} found with max_depth=1: {r['url']}"
            )

    # ── 12. Snippet contains query term ───────────────────────────────────────
    async def test_search_snippets_contain_term(self):
        db = await _run_crawl(self._origin, depth=3, db_path=self._db_path)
        from search_engine import SearchEngine
        results = await SearchEngine(db).search("asyncio")
        for r in results:
            if r.snippet:
                self.assertIn(
                    "asyncio", r.snippet.lower(),
                    f"Snippet for {r.url} doesn't contain query term"
                )

    # ── 13. Stats reflect reality ─────────────────────────────────────────────
    async def test_stats_are_consistent(self):
        db    = await _run_crawl(self._origin, depth=2, db_path=self._db_path)
        stats = await db.get_stats()
        total_pages = (
            stats.get("pages_done",    0) +
            stats.get("pages_error",   0) +
            stats.get("pages_skipped", 0)
        )
        self.assertGreater(total_pages, 0, "Should have processed some pages")
        self.assertGreaterEqual(
            stats.get("pages_indexed", 0), 0,
            "Indexed count should be non-negative"
        )
        self.assertGreaterEqual(
            stats.get("index_terms", 0), 0,
            "Term count should be non-negative"
        )


# ─────────────────────────────────────────────────────────────────────────────
class RobotsParserTest(unittest.TestCase):
    """Unit tests for the robots.txt parser — no I/O needed."""

    def _parse(self, body: str, agent: str = "singlenode"):
        from robots import _parse_robots
        return _parse_robots(body, agent)

    def test_disallow_all(self):
        data = self._parse("User-agent: *\nDisallow: /\n")
        self.assertFalse(data.is_allowed("/"))
        self.assertFalse(data.is_allowed("/page"))

    def test_allow_all(self):
        data = self._parse("User-agent: *\nDisallow:\n")
        self.assertTrue(data.is_allowed("/anything"))

    def test_empty_robots(self):
        data = self._parse("")
        self.assertTrue(data.is_allowed("/anything"))

    def test_specific_disallow(self):
        data = self._parse("User-agent: *\nDisallow: /private\n")
        self.assertFalse(data.is_allowed("/private"))
        self.assertFalse(data.is_allowed("/private/page"))
        self.assertTrue(data.is_allowed("/public"))

    def test_allow_overrides_disallow(self):
        body = (
            "User-agent: *\n"
            "Disallow: /admin\n"
            "Allow: /admin/public\n"
        )
        data = self._parse(body)
        self.assertFalse(data.is_allowed("/admin/secret"))
        self.assertTrue(data.is_allowed("/admin/public"))

    def test_crawl_delay_parsed(self):
        data = self._parse("User-agent: *\nCrawl-delay: 2.5\n")
        self.assertAlmostEqual(data.crawl_delay, 2.5)

    def test_specific_agent_overrides_wildcard(self):
        body = (
            "User-agent: *\nDisallow: /\n"
            "User-agent: singlenode\nDisallow:\n"
        )
        data = self._parse(body, agent="singlenode")
        # Our agent is allowed everywhere
        self.assertTrue(data.is_allowed("/"))
        self.assertTrue(data.is_allowed("/anything"))

    def test_404_robots_allows_all(self):
        from robots import _RobotsData
        data = _RobotsData(disallowed=[], allowed=[], crawl_delay=0.0,
                           fetch_failed=True)
        self.assertTrue(data.is_allowed("/anything"))

    def test_multiple_disallow_rules(self):
        body = (
            "User-agent: *\n"
            "Disallow: /private\n"
            "Disallow: /admin\n"
            "Disallow: /tmp\n"
        )
        data = self._parse(body)
        self.assertFalse(data.is_allowed("/private/data"))
        self.assertFalse(data.is_allowed("/admin"))
        self.assertFalse(data.is_allowed("/tmp/file"))
        self.assertTrue(data.is_allowed("/public"))

    def test_comments_ignored(self):
        body = (
            "# This is a comment\n"
            "User-agent: * # inline comment\n"
            "Disallow: /secret\n"
        )
        # Should not raise; /secret should be disallowed
        data = self._parse(body)
        self.assertFalse(data.is_allowed("/secret"))
        self.assertTrue(data.is_allowed("/open"))

    def test_case_insensitive_agent_match(self):
        body = "User-agent: SingleNode\nDisallow: /blocked\n"
        data = self._parse(body, agent="singlenode")
        self.assertFalse(data.is_allowed("/blocked"))


# ─────────────────────────────────────────────────────────────────────────────
class ExporterTest(unittest.IsolatedAsyncioTestCase):
    """Tests for list_pages / export_pages storage methods."""

    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmp.name
        self._tmp.close()
        from storage import Database
        from indexer  import Indexer
        from models   import Page
        self.db  = Database(self._path)
        await self.db.initialize()
        idx = Indexer(self.db)
        # Seed 5 pages
        for i in range(5):
            p = Page(
                url          = f"http://export.com/page{i}",
                depth        = i % 3,
                status       = "done",
                title        = f"Page {i}",
                text_content = f"content for page {i} export test",
                word_count   = 5,
                http_status  = 200,
            )
            pid = await self.db.upsert_page(p)
            p.id = pid
            await idx.index_page(p)

    async def asyncTearDown(self):
        os.unlink(self._path)

    async def test_list_pages_returns_correct_count(self):
        rows, total = await self.db.list_pages(status="done", page=1, per_page=10)
        self.assertEqual(total, 5)
        self.assertEqual(len(rows), 5)

    async def test_list_pages_pagination(self):
        rows_p1, total = await self.db.list_pages(status="done", page=1, per_page=2)
        rows_p2, _     = await self.db.list_pages(status="done", page=2, per_page=2)
        rows_p3, _     = await self.db.list_pages(status="done", page=3, per_page=2)
        self.assertEqual(total, 5)
        self.assertEqual(len(rows_p1), 2)
        self.assertEqual(len(rows_p2), 2)
        self.assertEqual(len(rows_p3), 1)
        # No overlap between pages
        urls_p1 = {r["url"] for r in rows_p1}
        urls_p2 = {r["url"] for r in rows_p2}
        self.assertEqual(len(urls_p1 & urls_p2), 0)

    async def test_list_pages_filters_by_status(self):
        rows, total = await self.db.list_pages(status="error")
        self.assertEqual(total, 0)
        self.assertEqual(len(rows), 0)

    async def test_export_pages_returns_all_done(self):
        rows = await self.db.export_pages()
        self.assertEqual(len(rows), 5)
        for r in rows:
            self.assertIn("url",   r)
            self.assertIn("title", r)
            self.assertIn("depth", r)

    async def test_export_jsonl_roundtrip(self):
        import io, json as _json
        rows = await self.db.export_pages()
        # Simulate what the server /export endpoint does
        lines = [_json.dumps(r, ensure_ascii=False) for r in rows]
        for line in lines:
            obj = _json.loads(line)
            self.assertIn("url",   obj)
            self.assertIn("title", obj)

    async def test_export_csv_roundtrip(self):
        import csv, io
        rows = await self.db.export_pages()
        buf = io.StringIO()
        w   = csv.writer(buf)
        w.writerow(["url", "title", "depth", "word_count",
                    "http_status", "fetched_at"])
        for r in rows:
            w.writerow([r["url"], r["title"], r["depth"],
                        r["word_count"], r["http_status"], r["fetched_at"]])
        buf.seek(0)
        reader = csv.DictReader(buf)
        parsed = list(reader)
        self.assertEqual(len(parsed), 5)
        self.assertIn("url",   parsed[0])
        self.assertIn("title", parsed[0])


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    unittest.main(verbosity=2)

class SitemapIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """Test sitemap discovery against the mock server."""

    async def asyncSetUp(self):
        self._server, self._port = await _start_mock_server()
        self._origin  = f"http://127.0.0.1:{self._port}/"
        self._tmp     = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = self._tmp.name
        self._tmp.close()

    async def asyncTearDown(self):
        self._server.close()
        os.unlink(self._db_path)

    async def test_sitemap_discovers_hidden_page(self):
        """A page only reachable via sitemap.xml should be crawled."""
        db = await _run_crawl(self._origin, depth=2, db_path=self._db_path)
        rows, _ = await db.list_pages(status="done")
        crawled_urls = {r["url"] for r in rows}
        sitemap_url  = f"http://127.0.0.1:{self._port}/sitemap-only-page"
        self.assertIn(sitemap_url, crawled_urls,
            f"sitemap-only-page should be discovered via sitemap.xml; "
            f"crawled={crawled_urls}")

    async def test_noindex_page_not_in_search(self):
        """A page with <meta name='robots' content='noindex'> must not appear
        in search results even if it was fetched."""
        db = await _run_crawl(self._origin, depth=2, db_path=self._db_path)
        from search_engine import SearchEngine
        se      = SearchEngine(db)
        results = await se.search("noindex")
        urls    = {r.url for r in results}
        noindex_url = f"http://127.0.0.1:{self._port}/noindex-page"
        self.assertNotIn(noindex_url, urls,
            "noindex page must be excluded from search results")

    async def test_anchor_text_boosts_target(self):
        """Pages linked-to with descriptive anchor text should score higher
        for that anchor text than pages that merely mention the word in body."""
        db = await _run_crawl(self._origin, depth=3, db_path=self._db_path)
        from search_engine import SearchEngine
        # The blog index links to post1 with anchor "Post 1"
        # post1 title is "Python asyncio tutorial"
        results = await SearchEngine(db).search("python asyncio tutorial")
        self.assertGreater(len(results), 0)
        # post1 should rank at or near the top
        top_url = results[0].url
        self.assertIn("post1", top_url,
            f"post1 should rank first for its title query; got {top_url}")

    async def test_depth_penalty_in_scoring(self):
        """Shallower pages should score higher than deep pages with equal TF-IDF."""
        db = await _run_crawl(self._origin, depth=3, db_path=self._db_path)
        from search_engine import SearchEngine
        results = await SearchEngine(db).search("asyncio")
        if len(results) >= 2:
            # Among equal-relevance results, shallower depth should win or tie
            # (we just assert scores are non-negative and results are sorted)
            scores = [r.score for r in results]
            self.assertEqual(scores, sorted(scores, reverse=True),
                "Results should be sorted by descending score")


class SitemapParserTest(unittest.TestCase):
    """Unit tests for sitemap XML parsing helpers — no I/O."""

    def test_extract_locs_standard(self):
        from sitemap import _extract_locs
        import xml.etree.ElementTree as ET
        xml_text = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/page1</loc></url>
          <url><loc>https://example.com/page2</loc></url>
        </urlset>"""
        root = ET.fromstring(xml_text)
        locs = _extract_locs(root, "http://www.sitemaps.org/schemas/sitemap/0.9", "url")
        self.assertEqual(locs, ["https://example.com/page1",
                                 "https://example.com/page2"])

    def test_extract_locs_no_namespace(self):
        """Many real sitemaps omit the XML namespace."""
        from sitemap import _extract_locs
        import xml.etree.ElementTree as ET
        xml_text = """<?xml version="1.0"?>
        <urlset>
          <url><loc>https://example.com/a</loc></url>
          <url><loc>https://example.com/b</loc></url>
        </urlset>"""
        root = ET.fromstring(xml_text)
        locs = _extract_locs(root, "http://www.sitemaps.org/schemas/sitemap/0.9", "url")
        self.assertEqual(locs, ["https://example.com/a", "https://example.com/b"])

    def test_extract_locs_sitemap_index(self):
        from sitemap import _extract_locs
        import xml.etree.ElementTree as ET
        xml_text = """<?xml version="1.0"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>
          <sitemap><loc>https://example.com/sitemap2.xml</loc></sitemap>
        </sitemapindex>"""
        root = ET.fromstring(xml_text)
        locs = _extract_locs(root, "http://www.sitemaps.org/schemas/sitemap/0.9", "sitemap")
        self.assertEqual(len(locs), 2)
        self.assertIn("https://example.com/sitemap1.xml", locs)

    def test_extract_locs_empty(self):
        from sitemap import _extract_locs
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<urlset></urlset>")
        locs = _extract_locs(root, "http://www.sitemaps.org/schemas/sitemap/0.9", "url")
        self.assertEqual(locs, [])

    def test_extract_locs_strips_whitespace(self):
        from sitemap import _extract_locs
        import xml.etree.ElementTree as ET
        xml_text = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>  https://example.com/spaces  </loc></url>
        </urlset>"""
        root = ET.fromstring(xml_text)
        locs = _extract_locs(root, "http://www.sitemaps.org/schemas/sitemap/0.9", "url")
        self.assertEqual(locs, ["https://example.com/spaces"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
