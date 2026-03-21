import os

# Concurrency & fetching
MAX_CONCURRENT_FETCHES = 8
FETCH_TIMEOUT_SECONDS  = 15
MAX_RETRIES            = 2
MAX_CONTENT_LENGTH     = 5 * 1024 * 1024   # 5 MB
USER_AGENT             = "SingleNodeCrawler/1.0 (educational)"

# Backpressure
BACKPRESSURE_HIGH_WATERMARK = 5_000
BACKPRESSURE_LOW_WATERMARK  = 1_000
BACKPRESSURE_SLEEP_SECONDS  = 0.5

# Per-domain politeness delay (seconds)
DOMAIN_CRAWL_DELAY = 0.2

# Storage
DB_PATH = os.environ.get("CRAWLER_DB", "crawler.db")

# Search
MAX_SEARCH_RESULTS = 20
SNIPPET_LENGTH     = 250   # characters

# Web UI server
SERVER_HOST = "0.0.0.0"
SERVER_PORT = int(os.environ.get("PORT", 8080))

# Indexer
MIN_TOKEN_LENGTH = 2
