#!/usr/bin/env python3
"""
Entry point — starts the HTTP server.

Usage
─────
  python main.py
  python main.py --port 9000
  python main.py --host 127.0.0.1 --port 8080 --db my_crawl.db --verbose
  PORT=9000 python main.py
"""
import argparse, asyncio, logging

import config
from storage import Database
from queue_manager import QueueManager
from crawler import Crawler
from indexer import Indexer
from search_engine import SearchEngine
from server import WebServer


async def run(host: str, port: int, db_path: str, verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)-14s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    db  = Database(db_path)
    await db.initialize()

    qm  = QueueManager(db)
    idx = Indexer(db)
    cr  = Crawler(db, qm, indexer=idx)
    se  = SearchEngine(db)
    ws  = WebServer(db, cr, se, idx)

    loc = f"http://{'localhost' if host == '0.0.0.0' else host}:{port}"
    print(f"""
╔══════════════════════════════════════════════════════╗
║  🕷  Single-Node Web Crawler                         ║
║                                                      ║
║  UI      →  {loc:<40}║
║  API     →  {loc + '/status':<40}║
╚══════════════════════════════════════════════════════╝
""")

    config.SERVER_HOST = host
    config.SERVER_PORT = port
    await ws.start()


def main():
    ap = argparse.ArgumentParser(description="Web Crawler — HTTP server")
    ap.add_argument("--host",    default=config.SERVER_HOST)
    ap.add_argument("--port",    type=int, default=config.SERVER_PORT)
    ap.add_argument("--db",      default=config.DB_PATH,
                    help="SQLite database path")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Enable DEBUG logging")
    a = ap.parse_args()

    try:
        asyncio.run(run(a.host, a.port, a.db, a.verbose))
    except KeyboardInterrupt:
        print("\n  Bye.\n")


if __name__ == "__main__":
    main()
