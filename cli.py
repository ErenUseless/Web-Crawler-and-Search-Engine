#!/usr/bin/env python3
"""
CLI interface — use without the web server.

Usage
─────
  python cli.py crawl https://example.com --depth 2
  python cli.py crawl https://example.com --depth 3 --cross-domain
  python cli.py search "python asyncio"
  python cli.py search "machine learning" --limit 5
  python cli.py status
  python cli.py status --json
"""
import argparse, asyncio, json, logging, sys

from storage import Database
from queue_manager import QueueManager
from crawler import Crawler
from indexer import Indexer
from search_engine import SearchEngine


# ── Crawl ─────────────────────────────────────────────────────────────────────
async def cmd_crawl(args):
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)-14s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    db  = Database(args.db)
    await db.initialize()
    qm  = QueueManager(db)
    idx = Indexer(db)
    cr  = Crawler(db, qm, indexer=idx)

    # run indexer in the background while crawling
    idx_task = asyncio.create_task(idx.run_continuous(), name="indexer")

    print(f"\n  Crawling  {args.url}  (depth={args.depth}"
          + ("  RESUME" if args.resume else "") + ")\n")
    await cr.start(args.url, args.depth, not args.cross_domain,
                   resume=args.resume)

    # flush remaining un-indexed pages
    idx.stop()
    idx_task.cancel()
    pages = await db.get_unindexed_pages(limit=100_000)
    if pages:
        print(f"\n  Indexing {len(pages)} remaining page(s)…")
        for p in pages:
            await idx.index_page(p)

    s = await cr.get_status()
    print(f"\n  ✓ Done — fetched={s.pages_fetched}  "
          f"indexed={s.pages_indexed}  "
          f"failed={s.pages_failed}  "
          f"elapsed={s.elapsed_seconds:.1f}s\n")


# ── Search ────────────────────────────────────────────────────────────────────
async def cmd_search(args):
    db = Database(args.db)
    await db.initialize()
    se = SearchEngine(db)
    rs = await se.search(args.query, limit=args.limit)

    if not rs:
        print("No results.")
        return

    print(f"\n  {len(rs)} result(s) for '{args.query}'\n")
    for i, r in enumerate(rs, 1):
        print(f"  {i:>2}. {r.title or r.url}")
        print(f"      {r.url}")
        print(f"      score={r.score:.5f}  depth={r.depth}")
        if r.snippet:
            print(f"      {r.snippet[:200]}")
        print()


# ── Status ────────────────────────────────────────────────────────────────────
async def cmd_status(args):
    db = Database(args.db)
    await db.initialize()
    stats = await db.get_stats()
    if getattr(args, "json", False):
        print(json.dumps(stats, indent=2))
    else:
        print()
        for k, v in stats.items():
            print(f"  {k:<22} {v}")
        print()


# ── Export ────────────────────────────────────────────────────────────────────
async def cmd_export(args):
    import csv, io, sys
    db = Database(args.db)
    await db.initialize()
    rows = await db.export_pages()
    if not rows:
        print("No pages to export.", file=sys.stderr)
        return

    out = open(args.out, "w", newline="", encoding="utf-8") \
          if args.out != "-" else sys.stdout

    try:
        if args.format == "jsonl":
            import json
            for r in rows:
                print(json.dumps(r, ensure_ascii=False), file=out)
        else:  # csv
            w = csv.writer(out)
            w.writerow(["url", "title", "depth", "word_count",
                        "http_status", "fetched_at"])
            for r in rows:
                w.writerow([r["url"], r["title"], r["depth"],
                            r["word_count"], r["http_status"], r["fetched_at"]])
        if args.out != "-":
            print(f"\n  Exported {len(rows)} pages to {args.out}\n",
                  file=sys.stderr)
    finally:
        if args.out != "-":
            out.close()


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Single-Node Web Crawler CLI",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument("--db", default="crawler.db",
                    help="SQLite database path (default: crawler.db)")

    sub = ap.add_subparsers(dest="cmd", required=True)

    # crawl
    cp = sub.add_parser("crawl", help="Start a BFS crawl")
    cp.add_argument("url", help="Origin URL")
    cp.add_argument("--depth", type=int, default=2,
                    help="Maximum crawl depth (default: 2)")
    cp.add_argument("--cross-domain", action="store_true",
                    help="Follow links to other domains")
    cp.add_argument("--resume", action="store_true",
                    help="Resume a previously interrupted crawl")
    cp.add_argument("-v", "--verbose", action="store_true")

    # search
    sp = sub.add_parser("search", help="Search indexed content")
    sp.add_argument("query", help="Search query")
    sp.add_argument("--limit", type=int, default=10,
                    help="Max results (default: 10)")

    # status
    stp = sub.add_parser("status", help="Show DB stats")
    stp.add_argument("--json", action="store_true",
                     help="Output as JSON")

    # export
    ep = sub.add_parser("export", help="Export crawled pages")
    ep.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    ep.add_argument("--out", default="-",
                    help="Output file path (default: stdout)")

    args = ap.parse_args()

    try:
        if   args.cmd == "crawl":  asyncio.run(cmd_crawl(args))
        elif args.cmd == "search": asyncio.run(cmd_search(args))
        elif args.cmd == "status": asyncio.run(cmd_status(args))
        elif args.cmd == "export": asyncio.run(cmd_export(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
