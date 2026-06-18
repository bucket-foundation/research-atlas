#!/usr/bin/env python3
"""Full-scale UKRI / Gateway to Research ingestion -- the ENTIRE project set.

Paginates the full GtR /projects API (~174k projects, ~1745 pages at s=100) with
the vendor media type, caching every page for idempotent resume, and streams
normalized rows through the BulkWriter into partitioned parquet shards.

For tractability at full scale, org resolution uses ONLY the embedded
participantValues (no per-project LEAD_ORG link-following), persons are off, and
ROR is off by default. These can be backfilled later.

Usage:
    python scripts/ingest_ukri_full.py                 # full corpus
    python scripts/ingest_ukri_full.py --max-pages 20  # quick slice
    python scripts/ingest_ukri_full.py --ror           # resolve ROR (slow)

After this, run scripts/consolidate.py to fold shards into flat parquet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.bulkwrite import BulkWriter  # noqa: E402
from atlas.connectors.ukri import UkriConnector  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Full UKRI GtR ingest -> sharded parquet.")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="cap pages (default: ALL ~1745)")
    ap.add_argument("--ror", action="store_true", help="resolve ROR (network)")
    ap.add_argument("--persons", action="store_true",
                    help="resolve PI/co-I persons (per-project fetches; slow)")
    ap.add_argument("--batch-rows", type=int, default=200_000)
    args = ap.parse_args()

    # resolve_orgs=False: use only embedded participantValues (no extra fetches).
    conn = UkriConnector(resolve_ror=args.ror, resolve_persons=args.persons,
                         resolve_orgs=False)
    bw = BulkWriter(source="ukri", batch_rows=args.batch_rows)

    print(f"UKRI full ingest: max_pages={args.max_pages or 'ALL'} "
          f"ror={'on' if args.ror else 'off'} "
          f"persons={'on' if args.persons else 'off'}")
    pages = conn.fetch_all(max_pages=args.max_pages)
    n_grants = 0
    for row in conn.normalize(pages):
        bw.add(row)
        if row.table == "grant":
            n_grants += 1
            if n_grants % 10000 == 0:
                print(f"    {n_grants:,} projects ...")
    bw.flush_all()

    print(f"\n{n_grants:,} projects ingested.")
    print("Shard rows written per table (pre-dedup):")
    for table, c in sorted(bw.partition_counts().items()):
        print(f"  {table:16s} {c:>9,}")
    print("\nNext: python scripts/consolidate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
