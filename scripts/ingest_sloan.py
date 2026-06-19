#!/usr/bin/env python3
"""Full Alfred P. Sloan Foundation ingestion -- the public Grants Database.

Pages the Sloan grants database (https://sloan.org/grants-database?page=N),
caching every page under data/raw/sloan/ (idempotent resume), and streams every
grant card through the memory-bounded BulkWriter into partitioned parquet shards.
Polite: 1s inter-request delay (robots.txt is empty / unrestricted). Stops at the
last page automatically (an empty page ends the crawl).

ROR is resolved in the cross-source backfill (scripts/resolve_ror.py), not here.

Usage:
    python scripts/ingest_sloan.py                 # full crawl (~342 pages)
    python scripts/ingest_sloan.py --limit-pages 5 # quick slice for testing

After this, run scripts/consolidate.py (or scripts/build_all.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.bulkwrite import BulkWriter  # noqa: E402
from atlas.connectors.sloan import SloanConnector  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Full Sloan ingest -> sharded parquet.")
    ap.add_argument("--limit-pages", type=int, default=None,
                    help="cap pages crawled (default: ALL)")
    ap.add_argument("--start-page", type=int, default=1)
    ap.add_argument("--batch-rows", type=int, default=200_000)
    args = ap.parse_args()

    conn = SloanConnector(resolve_ror=False)
    bw = BulkWriter(source="sloan", batch_rows=args.batch_rows)

    print(f"Sloan ingest: limit_pages={args.limit_pages or 'ALL'} "
          f"start_page={args.start_page}")
    pages = conn.fetch(limit_pages=args.limit_pages, start_page=args.start_page)
    n_grants = 0
    for row in conn.normalize(pages):
        bw.add(row)
        if row.table == "grant":
            n_grants += 1
            if n_grants % 500 == 0:
                print(f"    {n_grants:,} grants ...")
    bw.flush_all()

    print(f"\n{n_grants:,} grants ingested.")
    print("Shard rows written per table (pre-dedup):")
    for table, c in sorted(bw.partition_counts().items()):
        print(f"  {table:16s} {c:>9,}")
    print("\nNext: python scripts/consolidate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
