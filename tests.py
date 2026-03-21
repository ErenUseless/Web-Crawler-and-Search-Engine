#!/usr/bin/env python3
"""
Unit tests for core components.
No network access — all tests are fully self-contained.

Run:  python tests.py -v
"""
import asyncio, math, os, sys, tempfile, unittest

# ── bootstrap path ────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))


# ═══════════════════════════════════════════════════════════════════════════════
# Parser tests
# ═══════════════════════════════════════════════════════════════════════════════
class TestParser(unittest.TestCase):

    def test_normalize_url_strips_fragment(self):
        from parser import normalize_url
        self.assertEqual(
            normalize_url("http://example.com/page#anchor"),
            "http://example.com/page",
        )

    def test_normalize_url_sorts_query(self):
        from parser import normalize_url
        a = normalize_url("http://example.com/?b=2&a=1")
        b = normalize_url("http://example.com/?a=1&b=2")
        self.assertEqual(a, b)

    def test_normalize_url_strips_trailing_slash(self):
        from parser import normalize_url
        self.assertEqual(
            normalize_url("http://example.com/foo/"),
            "http://example.com/foo",
        )

    def test_normalize_url_strips_default_port(self):
        from parser import normalize_url
        self.assertEqual(
            normalize_url("http://example.com:80/"),
            "http://example.com/",
        )

    def test_normalize_url_rejects_non_http(self):
        from parser import normalize_url
        self.assertEqual(normalize_url("ftp://example.com/"), "")
        self.assertEqual(normalize_url("mailto:foo@bar.com"), "")

    def test_normalize_url_rejects_bad_extensions(self):
        from parser import normalize_url
        self.assertEqual(normalize_url("http://example.com/img.png"), "")
        self.assertEqual(normalize_url("http://example.com/doc.pdf"), "")

    def test_same_domain(self):
        from parser import same_domain
        self.assertTrue(same_domain("http://www.example.com/a",
                                    "https://example.com/b"))
        self.assertFalse(same_domain("http://a.com/", "http://b.com/"))

    def test_parse_page_extracts_title_and_links(self):
        from parser import parse_page
        html = b"""<html><head><title>Hello World</title></head>
        <body><p>Some text here.</p>
        <a href="/about">About</a>
        <a href="https://other.com/page">Other</a>
        </body></html>"""
        parsed = parse_page("http://example.com/", html)
        self.assertEqual(parsed.title, "Hello World")
        self.assertIn("Some text here", parsed.text)
        self.assertTrue(any("about" in l for l in parsed.links))
        # Backward compat: NamedTuple destructures as (title, text, links, ...)
        title, text, links = parsed.title, parsed.text, parsed.links
        self.assertEqual(title, "Hello World")

    def test_parse_page_skips_scripts(self):
        from parser import parse_page
        html = b"""<html><body>
        <script>var x = "secret";</script>
        <p>Visible</p>
        </body></html>"""
        parsed = parse_page("http://example.com/", html)
        self.assertNotIn("secret", parsed.text)
        self.assertIn("Visible", parsed.text)

    def test_parse_page_extracts_anchor_text(self):
        from parser import parse_page
        html = b"""<html><body>
        <a href="/docs">Read the documentation</a>
        <a href="/about">   </a>
        </body></html>"""
        parsed = parse_page("http://example.com/", html)
        anchors = {url: anchor for url, anchor in parsed.anchor_links}
        self.assertEqual(anchors.get("http://example.com/docs"), "Read the documentation")
        # whitespace-only anchor should be empty string after strip
        self.assertEqual(anchors.get("http://example.com/about", "").strip(), "")

    def test_parse_page_noindex_flag(self):
        from parser import parse_page
        html = b"""<html><head>
        <meta name="robots" content="noindex, nofollow">
        </head><body><p>Hidden</p></body></html>"""
        parsed = parse_page("http://example.com/", html)
        self.assertTrue(parsed.noindex)
        self.assertTrue(parsed.nofollow)

    def test_parse_page_noindex_false_by_default(self):
        from parser import parse_page
        html = b"<html><body><p>Normal page</p></body></html>"
        parsed = parse_page("http://example.com/", html)
        self.assertFalse(parsed.noindex)
        self.assertFalse(parsed.nofollow)

    def test_parse_page_partial_robots_meta(self):
        from parser import parse_page
        html = b"""<html><head>
        <meta name="robots" content="noindex">
        </head><body></body></html>"""
        parsed = parse_page("http://example.com/", html)
        self.assertTrue(parsed.noindex)
        self.assertFalse(parsed.nofollow)   # nofollow NOT set


# ═══════════════════════════════════════════════════════════════════════════════
# Tokenizer / storage helpers
# ═══════════════════════════════════════════════════════════════════════════════
class TestTokenizer(unittest.TestCase):

    def test_tokenize_removes_stopwords(self):
        from storage import tokenize
        tokens = tokenize("the quick brown fox jumps")
        self.assertNotIn("the", tokens)
        self.assertIn("quick", tokens)

    def test_tokenize_lowercases(self):
        from storage import tokenize
        tokens = tokenize("Python ASYNCIO")
        self.assertIn("python", tokens)
        self.assertIn("asyncio", tokens)

    def test_tokenize_filters_short(self):
        from storage import tokenize
        # "a", "i" are single-char (below MIN_TOKEN_LENGTH=2) → filtered
        # "to", "in" are stop words → filtered
        # "am" is 2 chars and NOT a stop word → kept
        tokens = tokenize("a i to in")
        self.assertEqual(tokens, [])
        # "am" survives (length==MIN_TOKEN_LENGTH, not a stop word)
        tokens2 = tokenize("am")
        self.assertEqual(tokens2, ["am"])

    def test_url_hash_stable(self):
        from storage import url_hash
        h1 = url_hash("http://example.com/page")
        h2 = url_hash("http://example.com/page")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 20)

    def test_content_hash_differs(self):
        from storage import content_hash
        self.assertNotEqual(
            content_hash("hello world"),
            content_hash("goodbye world"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Database (async)
# ═══════════════════════════════════════════════════════════════════════════════
class TestDatabase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp  = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmp.name
        self._tmp.close()
        from storage import Database
        self.db = Database(self._path)
        await self.db.initialize()

    async def asyncTearDown(self):
        os.unlink(self._path)

    # ── URL deduplication ─────────────────────────────────────────────────────
    async def test_queue_url_dedup(self):
        first  = await self.db.queue_url("http://example.com/", 0)
        second = await self.db.queue_url("http://example.com/", 0)
        self.assertTrue(first)
        self.assertFalse(second)

    async def test_is_seen_after_queue(self):
        await self.db.queue_url("http://example.com/seen", 0)
        self.assertTrue(await self.db.is_seen("http://example.com/seen"))
        self.assertFalse(await self.db.is_seen("http://example.com/not-seen"))

    # ── Queue mechanics ───────────────────────────────────────────────────────
    async def test_dequeue_returns_pending(self):
        await self.db.queue_url("http://a.com/", 0)
        await self.db.queue_url("http://b.com/", 1)
        items = await self.db.dequeue_batch(10)
        self.assertEqual(len(items), 2)
        urls = {i.url for i in items}
        self.assertIn("http://a.com/", urls)
        self.assertIn("http://b.com/", urls)

    async def test_dequeue_bfs_order(self):
        """BFS: shallower items should come first."""
        await self.db.queue_url("http://c.com/deep", 3)
        await self.db.queue_url("http://c.com/",     0)
        items = await self.db.dequeue_batch(10)
        self.assertEqual(items[0].depth, 0)

    async def test_queue_size(self):
        await self.db.queue_url("http://x.com/1", 0)
        await self.db.queue_url("http://x.com/2", 0)
        self.assertEqual(await self.db.queue_size(), 2)

    async def test_finish_queue_item_reduces_pending(self):
        await self.db.queue_url("http://fin.com/", 0)
        items = await self.db.dequeue_batch(1)
        self.assertEqual(await self.db.queue_size(), 0)
        await self.db.finish_queue_item(items[0].id, True)
        self.assertEqual(await self.db.queue_size(), 0)

    # ── Pages ─────────────────────────────────────────────────────────────────
    async def test_upsert_page_roundtrip(self):
        from models import Page
        p  = Page(url="http://page.com/", depth=0, status="done",
                  title="Test", text_content="hello world", word_count=2)
        pid = await self.db.upsert_page(p)
        self.assertIsNotNone(pid)
        self.assertGreater(pid, 0)

    async def test_upsert_page_updates_on_conflict(self):
        from models import Page
        p = Page(url="http://dup.com/", depth=0, status="done",
                 title="Old", text_content="old text", word_count=2)
        await self.db.upsert_page(p)
        p.title        = "New"
        p.text_content = "new text"
        await self.db.upsert_page(p)
        pages = await self.db.get_unindexed_pages(10)
        matching = [x for x in pages if x.url == "http://dup.com/"]
        self.assertEqual(matching[0].title, "New")

    async def test_get_unindexed_returns_done_only(self):
        from models import Page
        p_done  = Page(url="http://idx.com/done",  depth=0,
                       status="done",  text_content="some content")
        p_error = Page(url="http://idx.com/error", depth=0,
                       status="error", text_content="")
        await self.db.upsert_page(p_done)
        await self.db.upsert_page(p_error)
        pages = await self.db.get_unindexed_pages(10)
        urls = {p.url for p in pages}
        self.assertIn("http://idx.com/done", urls)
        self.assertNotIn("http://idx.com/error", urls)

    async def test_mark_indexed(self):
        from models import Page
        p   = Page(url="http://mark.com/", depth=0, status="done",
                   text_content="mark me")
        pid = await self.db.upsert_page(p)
        await self.db.mark_indexed(pid)
        pages = await self.db.get_unindexed_pages(10)
        self.assertNotIn(pid, [pp.id for pp in pages])

    # ── Inverted index + search ────────────────────────────────────────────────
    async def test_index_and_search(self):
        from models import Page
        p   = Page(url="http://search.com/", depth=0, status="done",
                   title="Python asyncio", text_content="asyncio is great for io bound tasks")
        pid = await self.db.upsert_page(p)
        await self.db.index_page_terms(pid, [("asyncio", 0.3), ("python", 0.2)])
        await self.db.mark_indexed(pid)

        results = await self.db.search(["asyncio"])
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].url, "http://search.com/")

    async def test_search_returns_empty_for_unknown_term(self):
        results = await self.db.search(["xyzzy_not_a_real_word"])
        self.assertEqual(results, [])

    async def test_idf_boosts_rare_terms(self):
        """A term that appears in fewer docs should have higher IDF weight."""
        from models import Page
        # page A: has both "common" and "rare"
        pA = Page(url="http://idf.com/a", depth=0, status="done",
                  text_content="common rare")
        # page B: has only "common"
        pB = Page(url="http://idf.com/b", depth=0, status="done",
                  text_content="common")
        idA = await self.db.upsert_page(pA)
        idB = await self.db.upsert_page(pB)
        await self.db.index_page_terms(idA, [("common", 0.5), ("rare", 0.5)])
        await self.db.index_page_terms(idB, [("common", 1.0)])
        await self.db.mark_indexed(idA)
        await self.db.mark_indexed(idB)

        results = await self.db.search(["rare"])
        # Only page A should appear (page B doesn't have "rare")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "http://idf.com/a")

    # ── Meta ──────────────────────────────────────────────────────────────────
    async def test_meta_set_get(self):
        await self.db.set_meta("test_key", "hello")
        val = await self.db.get_meta("test_key")
        self.assertEqual(val, "hello")

    async def test_meta_missing_key_returns_none(self):
        self.assertIsNone(await self.db.get_meta("no_such_key"))

    # ── clear_all ─────────────────────────────────────────────────────────────
    async def test_clear_all(self):
        await self.db.queue_url("http://clear.com/", 0)
        await self.db.clear_all()
        self.assertEqual(await self.db.queue_size(), 0)
        self.assertFalse(await self.db.is_seen("http://clear.com/"))


# ═══════════════════════════════════════════════════════════════════════════════
# Indexer
# ═══════════════════════════════════════════════════════════════════════════════
class TestIndexer(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp  = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmp.name
        self._tmp.close()
        from storage import Database
        from indexer  import Indexer
        self.db  = Database(self._path)
        await self.db.initialize()
        self.idx = Indexer(self.db)

    async def asyncTearDown(self):
        os.unlink(self._path)

    async def test_index_page_produces_terms(self):
        from models import Page
        p   = Page(id=1, url="http://idx.com/", depth=0,
                   title="Fast Python", text_content="Python is fast and great")
        # upsert so page exists
        pid = await self.db.upsert_page(p)
        p.id = pid
        n   = await self.idx.index_page(p)
        self.assertGreater(n, 0)

    async def test_index_empty_page_returns_zero(self):
        from models import Page
        p = Page(id=99, url="http://empty.com/", depth=0, text_content="")
        n = await self.idx.index_page(p)
        self.assertEqual(n, 0)

    async def test_title_boost_visible_in_score(self):
        """Pages where query appears in the title should outscore body-only pages."""
        from models import Page
        from storage import tokenize
        # page A: query term in TITLE (gets 3× weight in indexer)
        pA = Page(url="http://boost.com/a", depth=0, status="done",
                  title="asyncio tutorial", text_content="asyncio is great")
        # page B: query term only in body, no title match
        pB = Page(url="http://boost.com/b", depth=0, status="done",
                  title="general programming",
                  text_content="asyncio asyncio asyncio basics")

        idA = await self.db.upsert_page(pA); pA.id = idA
        idB = await self.db.upsert_page(pB); pB.id = idB
        await self.idx.index_page(pA)
        await self.idx.index_page(pB)
        await self.db.mark_indexed(idA)
        await self.db.mark_indexed(idB)

        results = await self.db.search(["asyncio"])
        self.assertEqual(len(results), 2)
        url_order = [r.url for r in results]
        # page A should rank first because title is boosted
        self.assertEqual(url_order[0], "http://boost.com/a")


# ═══════════════════════════════════════════════════════════════════════════════
# QueueManager
# ═══════════════════════════════════════════════════════════════════════════════
class TestQueueManager(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp  = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmp.name
        self._tmp.close()
        import config
        config.DOMAIN_CRAWL_DELAY = 0   # no politeness in tests
        from storage      import Database
        from queue_manager import QueueManager
        self.db = Database(self._path)
        await self.db.initialize()
        self.qm = QueueManager(self.db)

    async def asyncTearDown(self):
        os.unlink(self._path)

    async def test_enqueue_respects_max_depth(self):
        ok = await self.qm.enqueue("http://example.com/", 5, max_depth=3)
        self.assertFalse(ok)

    async def test_enqueue_dedup(self):
        a = await self.qm.enqueue("http://example.com/", 0, max_depth=3)
        b = await self.qm.enqueue("http://example.com/", 0, max_depth=3)
        self.assertTrue(a)
        self.assertFalse(b)

    async def test_enqueue_batch_counts(self):
        urls = [f"http://example.com/{i}" for i in range(5)]
        n    = await self.qm.enqueue_batch(urls, 1, max_depth=3)
        self.assertEqual(n, 5)

    async def test_backpressure_blocks_enqueue(self):
        """Backpressure must block new URLs from being enqueued."""
        import config
        config.BACKPRESSURE_HIGH_WATERMARK = 2
        config.BACKPRESSURE_LOW_WATERMARK  = 1

        await self.qm.enqueue("http://bp.com/1", 0, max_depth=5)
        await self.qm.enqueue("http://bp.com/2", 0, max_depth=5)
        # Third enqueue should be blocked by backpressure
        result = await self.qm.enqueue("http://bp.com/3", 0, max_depth=5)
        self.assertFalse(result)

        # Restore
        import config as _c
        _c.BACKPRESSURE_HIGH_WATERMARK = 5_000
        _c.BACKPRESSURE_LOW_WATERMARK  = 1_000

    async def test_dequeue_never_blocks_under_backpressure(self):
        """Dequeue must always return immediately — backpressure must NOT block it.
        Previously dequeue() spun in a sleep loop when _bp_active was True,
        which caused the crawler to detect an idle condition and exit prematurely
        while thousands of items were still in the queue."""
        import config
        config.BACKPRESSURE_HIGH_WATERMARK = 2
        config.BACKPRESSURE_LOW_WATERMARK  = 1

        await self.qm.enqueue("http://nodelay.com/1", 0, max_depth=5)
        await self.qm.enqueue("http://nodelay.com/2", 0, max_depth=5)
        # Trigger backpressure: a 3rd enqueue should fail (activates the flag)
        refused = await self.qm.enqueue("http://nodelay.com/3", 0, max_depth=5)
        self.assertFalse(refused, "3rd enqueue must be refused by backpressure")
        self.assertTrue(self.qm.backpressure_active, "backpressure flag must be set")

        # Dequeue must return immediately with available items (not block)
        import asyncio, time
        t0    = time.monotonic()
        items = await asyncio.wait_for(self.qm.dequeue(10), timeout=1.0)
        elapsed = time.monotonic() - t0

        self.assertEqual(len(items), 2, "Should return both queued items")
        self.assertLess(elapsed, 0.5,
            f"Dequeue blocked for {elapsed:.2f}s — backpressure must not block dequeue")

        # Restore
        import config as _c
        _c.BACKPRESSURE_HIGH_WATERMARK = 5_000
        _c.BACKPRESSURE_LOW_WATERMARK  = 1_000


# ═══════════════════════════════════════════════════════════════════════════════
# SearchEngine
# ═══════════════════════════════════════════════════════════════════════════════
class TestSearchEngine(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp  = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmp.name
        self._tmp.close()
        from storage       import Database
        from indexer        import Indexer
        from search_engine  import SearchEngine
        from models         import Page
        self.db  = Database(self._path)
        await self.db.initialize()
        self.se  = SearchEngine(self.db)
        idx      = Indexer(self.db)

        # seed two pages
        for url, title, body in [
            ("http://se.com/py",   "Python Guide",   "python asyncio concurrency"),
            ("http://se.com/js",   "JS Reference",   "javascript async await promise"),
        ]:
            p   = Page(url=url, depth=0, status="done",
                       title=title, text_content=body)
            pid = await self.db.upsert_page(p)
            p.id = pid
            await idx.index_page(p)
            await self.db.mark_indexed(pid)

    async def asyncTearDown(self):
        os.unlink(self._path)

    async def test_search_finds_relevant_page(self):
        results = await self.se.search("python asyncio")
        self.assertTrue(any("py" in r.url for r in results))

    async def test_search_returns_empty_for_no_match(self):
        results = await self.se.search("zzz_no_match_xyz")
        self.assertEqual(results, [])

    async def test_search_limit_respected(self):
        results = await self.se.search("async", limit=1)
        self.assertLessEqual(len(results), 1)

    async def test_domain_filter(self):
        results = await self.se.search("async",
                                       domain_filter="http://se.com")
        self.assertTrue(all("se.com" in r.url for r in results))

    async def test_snippet_not_empty(self):
        results = await self.se.search("python")
        self.assertTrue(any(r.snippet for r in results))


# ═══════════════════════════════════════════════════════════════════════════════
# Snippet helper
# ═══════════════════════════════════════════════════════════════════════════════
class TestSnippet(unittest.TestCase):

    def test_snippet_contains_term(self):
        from storage import _make_snippet
        text    = "The quick brown fox jumps over the lazy dog"
        snippet = _make_snippet(text, ["fox"])
        self.assertIn("fox", snippet.lower())

    def test_snippet_adds_ellipsis_when_truncated(self):
        from storage import _make_snippet
        long_text = "a " * 500 + "needle " + "b " * 500
        snippet   = _make_snippet(long_text, ["needle"])
        # should have at least one ellipsis since needle is in the middle
        self.assertIn("...", snippet)

    def test_snippet_empty_text(self):
        from storage import _make_snippet
        self.assertEqual(_make_snippet("", ["foo"]), "")


# ─────────────────────────────────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════════
# Robots parser — additional correctness tests
# ═══════════════════════════════════════════════════════════════════════════════
class TestRobotsParser(unittest.TestCase):

    def _parse(self, body, agent="singlenode"):
        from robots import _parse_robots
        return _parse_robots(body, agent)

    def test_no_blank_line_between_groups_does_not_bleed(self):
        """Bug fix: groups without blank-line separator must NOT bleed rules
        from one agent into another."""
        body = (
            "User-agent: *\n"
            "Disallow: /\n"
            "User-agent: googlebot\n"
            "Allow: /\n"
        )
        data = self._parse(body, "singlenode")
        # Wildcard says disallow /, googlebot gets allow /
        # Our agent (singlenode) only matches wildcard → disallowed
        self.assertFalse(data.is_allowed("/"),
            "Our agent should inherit '*' Disallow:/ not googlebot's Allow:/")

    def test_no_blank_line_googlebot_sees_allow(self):
        """Googlebot group without blank line must get its Allow rule."""
        body = (
            "User-agent: *\n"
            "Disallow: /\n"
            "User-agent: googlebot\n"
            "Allow: /\n"
        )
        data = self._parse(body, "googlebot")
        self.assertTrue(data.is_allowed("/"),
            "Googlebot's Allow:/ should override the wildcard Disallow:/")

    def test_blank_line_group_separation_unchanged(self):
        """Blank-line separated groups must work as before."""
        body = "User-agent: *\nDisallow: /private\n\nUser-agent: singlenode\nAllow: /private\n"
        data = self._parse(body, "singlenode")
        # singlenode has explicit Allow rule
        self.assertTrue(data.is_allowed("/private"))

    def test_in_rule_section_flag_reset_on_blank_line(self):
        """After a blank line, the next User-agent should start a fresh group."""
        body = (
            "User-agent: *\n"
            "Disallow: /secret\n"
            "\n"
            "User-agent: singlenode\n"
            "Disallow: /other\n"
        )
        data_wild  = self._parse(body, "wildcard_only")
        data_us    = self._parse(body, "singlenode")
        self.assertFalse(data_wild.is_allowed("/secret"))
        self.assertTrue( data_wild.is_allowed("/other"))
        self.assertFalse(data_us.is_allowed("/other"))
        self.assertTrue( data_us.is_allowed("/secret"))


# ═══════════════════════════════════════════════════════════════════════════════
# Indexer pause / resume — clear_all race condition fix
# ═══════════════════════════════════════════════════════════════════════════════
class TestIndexerPause(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        import tempfile, os
        self._tmp  = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._path = self._tmp.name
        self._tmp.close()
        from storage import Database
        from indexer  import Indexer
        self.db  = Database(self._path)
        await self.db.initialize()
        self.idx = Indexer(self.db)

    async def asyncTearDown(self):
        import os
        os.unlink(self._path)

    async def test_pause_stops_indexing(self):
        """While paused, run_continuous must not process any pages."""
        from models import Page
        p   = Page(url="http://pause.com/", depth=0, status="done",
                   text_content="pausable content here")
        pid = await self.db.upsert_page(p)
        p.id = pid

        self.idx.pause()

        # Run the continuous loop briefly while paused
        task = asyncio.create_task(self.idx.run_continuous())
        await asyncio.sleep(0.3)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

        # Page should still be unindexed
        pages = await self.db.get_unindexed_pages(10)
        self.assertTrue(any(p.url == "http://pause.com/" for p in pages),
                        "Paused indexer should not have processed the page")

    async def test_resume_after_pause_indexes_pages(self):
        """After resume_indexing(), pages should be processed normally."""
        from models import Page
        p   = Page(url="http://resume.com/", depth=0, status="done",
                   text_content="resumable content words")
        pid = await self.db.upsert_page(p)
        p.id = pid

        self.idx.pause()
        self.idx.resume_indexing()

        task = asyncio.create_task(self.idx.run_continuous())
        await asyncio.sleep(0.5)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

        pages = await self.db.get_unindexed_pages(10)
        self.assertFalse(any(pp.url == "http://resume.com/" for pp in pages),
                         "Page should have been indexed after resume")

    async def test_no_orphan_index_rows_after_clear_all(self):
        """Simulates the clear_all race: pause → clear → resume.
        After the sequence, inverted_index must be empty."""
        from models import Page
        import asyncio

        # 1. Index a page
        p   = Page(url="http://orphan.com/", depth=0, status="done",
                   text_content="some indexable content words here")
        pid = await self.db.upsert_page(p); p.id = pid
        await self.idx.index_page(p)

        stats_before = await self.db.get_stats()
        self.assertGreater(stats_before.get("index_terms", 0), 0)

        # 2. Proper pause → clear → resume sequence
        self.idx.pause()
        await self.db.clear_all()
        self.idx.resume_indexing()

        # 3. Index table must be empty — no orphan rows
        stats_after = await self.db.get_stats()
        self.assertEqual(stats_after.get("index_terms", 0), 0,
            "clear_all should remove all index rows; no orphans should remain")


if __name__ == "__main__":
    unittest.main(verbosity=2)
