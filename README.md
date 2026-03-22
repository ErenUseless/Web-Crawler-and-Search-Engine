# Single-Node Web Crawler & Search Engine

A production-quality web crawler and full-text search engine built entirely from
scratch using **Python's standard library and SQLite**. No crawler frameworks, no
search engines, no external dependencies.

---

## Features

| Feature | Details |
|---|---|
| BFS crawl to depth *k* | Breadth-first with configurable max depth |
| Global URL deduplication | SHA-256 hash table, zero re-fetches |
| Backpressure | High/low watermark on queue length |
| Per-domain politeness | Configurable delay, respects `Crawl-delay` from robots.txt |
| robots.txt compliance | Full `Disallow`/`Allow`/`Crawl-delay`, group-aware parser |
| Sitemap discovery | Reads `robots.txt` `Sitemap:` directives, index recursion, gzip |
| Content-hash dedup | Skips re-indexing identical pages served at different URLs |
| `<meta robots>` support | Respects `noindex` and `nofollow` per page |
| Anchor text indexing | Links from other pages boost target page relevance |
| TF-IDF search | Smoothed IDF, title ×3 boost, depth penalty, domain filter |
| Incremental indexing | Background task — search works while crawl is running |
| Crawl resume | Continue an interrupted crawl without losing progress |
| Link graph | Outbound links with anchor text stored for ranking |
| Clean shutdown | In-flight fetches abort promptly on stop |
| Web UI | Dark-themed single-page interface at `localhost:8080` |
| REST API | `/status`, `/crawl`, `/stop`, `/search`, `/pages`, `/export` |
| CLI | `crawl`, `search`, `status`, `export` subcommands |
| Persistent storage | SQLite WAL mode, survives restarts |
| Zero dependencies | Pure Python 3.8+ stdlib + SQLite |

---

## Quick Start

```bash
# No install required — just Python 3.8+
python3 --version

# Start the web server
python3 main.py

# Open http://localhost:8080 in your browser
```

Type a URL, set depth, click **START**. Search while the crawl is running.

---

## Requirements

- Python 3.8 or newer
- No `pip install` needed — everything uses the standard library

```bash
python3 -c "import sqlite3, asyncio, html.parser, urllib.request; print('OK')"
```

---

## Usage

### Web UI

```bash
python3 main.py                          # default: http://localhost:8080
python3 main.py --port 9000
python3 main.py --host 127.0.0.1 --db my_crawl.db
python3 main.py --verbose                # show every fetch in terminal
```

Open `http://localhost:8080`:

- **CRAWL ENGINE** — enter URL, depth, same-domain toggle, resume option
- **Active Telemetry** — live stats: Fetched, Indexed, Queued, Failed, Skipped, Terms, Rate
- **Index Search** — search while the crawl is still running

### CLI

```bash
# Crawl
python3 cli.py crawl https://example.com --depth 2
python3 cli.py crawl https://example.com --depth 3 --cross-domain
python3 cli.py crawl https://example.com --depth 2 --resume    # continue from last stop

# Search
python3 cli.py search "python asyncio"
python3 cli.py search "machine learning" --limit 5

# Status
python3 cli.py status
python3 cli.py status --json

# Export
python3 cli.py export --format jsonl --out pages.jsonl
python3 cli.py export --format csv   --out pages.csv

# Use a specific database
python3 cli.py --db my_crawl.db crawl https://example.com
```

### REST API

```bash
# Start crawl
curl -X POST http://localhost:8080/crawl \
     -H "Content-Type: application/json" \
     -d '{"url":"https://example.com","depth":2,"same_domain":true}'

# Resume interrupted crawl
curl -X POST http://localhost:8080/crawl \
     -H "Content-Type: application/json" \
     -d '{"url":"","depth":2,"resume":true}'

# Stop
curl -X POST http://localhost:8080/stop

# Status
curl http://localhost:8080/status

# Search
curl "http://localhost:8080/search?q=python+asyncio&limit=10"

# Browse indexed pages (paginated)
curl "http://localhost:8080/pages?page=1&per_page=20&status=done"

# Export
curl "http://localhost:8080/export?format=jsonl" > pages.jsonl
curl "http://localhost:8080/export?format=csv"   > pages.csv
```

---

## Good Sites to Test With

| Site | Depth | Pages | Notes |
|---|---|---|---|
| `https://example.com` | 1 | ~1 | Instant smoke test |
| `https://quotes.toscrape.com` | 2 | ~140 | Made for scraper testing |
| `https://books.toscrape.com` | 3 | ~200 | Structured catalogue |
| Any Wikipedia article | 1 | ~50-100 | Keep depth=1 or it crawls thousands |

---

## Configuration

Edit `config.py` or use environment variables:

| Setting | Default | Description |
|---|---|---|
| `MAX_CONCURRENT_FETCHES` | `8` | Simultaneous in-flight requests |
| `DOMAIN_CRAWL_DELAY` | `1.0s` | Wait between requests to same domain |
| `FETCH_TIMEOUT_SECONDS` | `15` | Per-request timeout |
| `MAX_RETRIES` | `2` | Retry count (exponential backoff: 2s, 4s) |
| `MAX_CONTENT_LENGTH` | `5 MB` | Per-page download cap |
| `BACKPRESSURE_HIGH_WATERMARK` | `5000` | Queue depth that pauses link discovery |
| `BACKPRESSURE_LOW_WATERMARK` | `1000` | Queue depth that resumes link discovery |
| `CRAWLER_DB` | `crawler.db` | SQLite file path (env var) |
| `PORT` | `8080` | HTTP server port (env var) |
| `MIN_TOKEN_LENGTH` | `2` | Minimum word length for indexing |

```bash
# Environment variable examples
CRAWLER_DB=wiki.db PORT=9000 python3 main.py
```

**For faster testing** (lower politeness):
```python
# config.py
DOMAIN_CRAWL_DELAY = 0.2   # 5x faster, less polite
```

---

## Architecture

```
HTTP Server  (asyncio.start_server — no web framework)
  GET /   POST /crawl   POST /stop   GET /status   GET /search   GET /pages   GET /export
              |
    +---------v--------------------------+
    |         Crawler                    |  asyncio tasks + Semaphore(8)
    |  BFS orchestrator                  |
    +--+---------------------------------+
       |
       +-- QueueManager      backpressure · dedup · per-domain politeness
       |     └── enqueue_batch()   single transaction for all links per page
       |
       +-- RobotsChecker     per-domain cache · Allow/Disallow/Crawl-delay
       |
       +-- SitemapDiscoverer sitemap index recursion · gzip · no-namespace XML
       |
       +-- Fetcher           urllib + ThreadPoolExecutor · retry · clean shutdown
       |
       +-- Parser            html.parser · title · text · links · anchor text
                             noindex/nofollow · URL normalisation
                                   |
                    +--------------v--------------------------------------+
                    |           SQLite  (WAL mode)                        |
                    |  pages · queue · seen_urls                          |
                    |  inverted_index · links · crawl_meta                |
                    +--------------+--------------------------------------+
                                   |
                    +--------------v----------+
                    |       Indexer            |  background asyncio task
                    |  TF  title×3 anchor×2   |  no inter-batch sleep
                    +--------------+-----------+
                                   |
                    +--------------v----------+
                    |     SearchEngine         |  TF-IDF · smoothed IDF
                    |  depth penalty · domain  |  snippet extraction
                    +--------------------------+
```

---

## Database Schema

```sql
pages           url, title, text, depth, status, word_count,
                http_status, fetched_at, indexed, content_hash

queue           url, depth, added_at, status
                (status: pending → processing → done/failed)

seen_urls       url_hash PRIMARY KEY
                (global dedup — checked before every enqueue)

inverted_index  term, page_id, tf
                (tf = term frequency in weighted doc: title×3 + anchors×2 + body)

links           source_id, target_url, anchor
                (outbound link graph with anchor text)

crawl_meta      key, value
                (origin_url, max_depth, same_domain, start_time)
```

---

## How Search Works

1. **Tokenise** the query — split into words, lowercase, remove stopwords and short words
2. **TF-IDF scoring** — for each term, look up all pages containing it:
   - *TF* (term frequency) — fraction of page words that are this term, computed over a weighted document where title terms count 3× and anchor texts from other pages count 2×
   - *IDF* (inverse document frequency) — `log((total + 1) / (matches + 1)) + 1` — rare terms score higher
   - Score = sum of TF × IDF across all query terms
3. **Depth penalty** — `1.0 / (1.0 + log(depth + 1) × 0.15)` — shallower pages rank slightly higher
4. **Sort** by final score, return top results
5. **Snippet** — scan text for best window containing query terms

---

## Resuming a Crawl

Crawl state persists in `crawler.db`. To continue after an interruption:

**Web UI:** tick **Resume session** before clicking Start. The URL field shows what will be resumed.

**CLI:**
```bash
python3 cli.py crawl https://example.com --depth 2 --resume
```

If the previous crawl **completed** (queue drained), resume re-enqueues the origin so deeper pages can be discovered. If it was **interrupted**, resume continues from the remaining queue.

---

## Running Tests

```bash
# Unit tests (~2 seconds, no network)
python3 tests.py -v

# Integration tests (~75 seconds, uses a local mock HTTP server)
python3 integration_test.py -v

# Both together
python3 -m unittest tests integration_test -v
```

Expected: **112 tests, 0 failures**.

---

## Project Structure

```
crawler_project/
├── main.py             Entry point — starts web server
├── cli.py              Command-line interface
├── config.py           All tunable constants
├── models.py           Data classes (Page, QueueItem, SearchResult, CrawlStatus)
├── storage.py          SQLite layer — all DB access goes through here
├── queue_manager.py    BFS queue with backpressure and politeness
├── fetcher.py          Async HTTP downloader (urllib + thread pool)
├── parser.py           HTML parser — text, links, anchor text, meta robots
├── robots.py           robots.txt compliance with per-domain caching
├── sitemap.py          Sitemap discovery and URL extraction
├── indexer.py          Background TF indexer with title and anchor boost
├── search_engine.py    TF-IDF search with depth penalty and domain filter
├── crawler.py          Main orchestrator — coordinates all components
├── server.py           Raw asyncio HTTP server + web UI
├── tests.py            Unit tests
└── integration_test.py Integration tests (real local mock HTTP server)
```

---

## Design Decisions

**Why no external HTTP library?** The project requires standard library only. `urllib` inside a thread pool is effectively non-blocking from asyncio's perspective — the event loop runs freely while threads wait on the network.

**Why SQLite?** It needs no setup, survives restarts, and WAL mode handles concurrent reads and writes correctly. At normal crawl rates it runs at under 10% capacity.

**Why a single DB connection with a lock?** SQLite has a single-writer limitation. A lock is simpler and more correct than a connection pool, and the lock overhead (0.02ms) is thousands of times smaller than network latency (200–500ms). The hot path (`enqueue_batch`) uses a single transaction for all links from one page, making bulk writes 35× faster than one-at-a-time.

**Why asyncio + thread pool for fetching?** `urllib` is blocking. Running it in a `ThreadPoolExecutor` gives true parallelism for network I/O while keeping the rest of the system async. The thread pool is sized at 32 but `asyncio.Semaphore` limits actual concurrency to `MAX_CONCURRENT_FETCHES`.

---

## Common Issues

| Problem | Cause | Fix |
|---|---|---|
| Crawl finds thousands of pages | High depth on a large site (e.g. Wikipedia) | Use depth 1–2 for large sites |
| Very slow crawl | `DOMAIN_CRAWL_DELAY = 1.0s` is intentionally polite | Lower to `0.2` in `config.py` for testing |
| Search returns nothing | Index still building in background | Wait a few seconds and try again |
| Stats don't update | Normal after crawl completes — stats freeze at final values | Expected behaviour |
| Resume does nothing | Previous crawl completed fully | Resume re-enqueues origin automatically |
| `crawler.db` grows large | Full text stored for all pages | Delete the file to start fresh |
