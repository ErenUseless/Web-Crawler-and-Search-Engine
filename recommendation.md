## Production Deployment — Recommendations

### Server & Database Improvements

Right now this crawler works well on a single machine and uses SQLite, which is fine for learning but won't handle real production loads. The biggest priority would be **replacing the basic asyncio server with an actual production web server** like Gunicorn or Uvicorn running behind Nginx. This matters because Nginx handles SSL/HTTPS, rate limiting, and connection pooling automatically, which we don't get from the raw Python HTTP layer.

For the database, SQLite is really convenient but can only handle one writer at a time. If we want to run multiple crawlers in parallel or have better concurrency, we'd need to switch to **PostgreSQL** instead. The tricky part is replacing the single `asyncio.Lock` with proper connection pooling using libraries like asyncpg or psycopg3 so multiple threads can safely write to the database simultaneously.

### Monitoring & Logging

Right now we only see logs when we look at the terminal output, which is not great for a production system running 24/7. We should add **proper logging** using Python's `logging.handlers.RotatingFileHandler` or the `structlog` library so logs get saved to files that we can review later. We should also connect the `/status` endpoint to a monitoring tool like **Prometheus** so we can track crawler health over time and get alerts if something breaks.

### Crawler Functionality Gaps

There are two main features missing that real websites need:

1. **Authentication**: Many sites require login cookies or session headers. We could add this by extending `fetcher.py` to accept custom headers and cookies, but it would require a config system to manage credentials safely.

2. **JavaScript Support**: Right now our HTML parser only sees what's in the initial HTML response. Modern websites often load content dynamically using JavaScript, which we completely miss. Fixing this would require integrating a headless browser like **Playwright or Pyppeteer**, but that's a pretty big change to the architecture and would slow things down significantly.

### Rate Limiting & Politeness

The current rate limiting only works while the server is running — it resets every time we restart, which could accidentally hammer a website. We should **persist rate limits to the database** so they survive restarts. Also, we should enforce the `Crawl-delay` value from robots.txt as a hard limit, so even if someone sets an aggressive crawl speed in `config.py`, we never violate a site's requests.