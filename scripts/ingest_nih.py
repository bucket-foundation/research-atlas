#!/usr/bin/env python3
"""Full-scale NIH RePORTER ingestion -- the largest single funder.

Pulls every NIH project for fiscal years [--year-start, --year-end] via the
RePORTER v2 API, windowed by (fiscal_year, agency IC) to stay under the 15k
offset cap, paginated at limit=500, every page cached for idempotent resume.
Streams normalized rows through the BulkWriter into partitioned parquet shards.

Usage:
    python scripts/ingest_nih.py --year-start 2018 --year-end 2025
    python scripts/ingest_nih.py --year-start 2024 --year-end 2024   # one FY
    python scripts/ingest_nih.py --ror   # resolve ROR (slow, network)

After this, run scripts/consolidate.py to fold shards into flat parquet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.bulkwrite import BulkWriter  # noqa: E402
from atlas.connectors.nih import NihConnector  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Full NIH ingest -> sharded parquet.")
    ap.add_argument("--year-start", type=int, default=2018)
    ap.add_argument("--year-end", type=int, default=2025)
    ap.add_argument("--ror", action="store_true", help="resolve ROR (network)")
    ap.add_argument("--batch-rows", type=int, default=200_000)
    args = ap.parse_args()

    conn = NihConnector(resolve_ror=args.ror)
    bw = BulkWriter(source="nih", batch_rows=args.batch_rows)

    print(f"NIH full ingest: FY {args.year_start}-{args.year_end} "
          f"ror={'on' if args.ror else 'off'}")
    pages = conn.fetch(year_start=args.year_start, year_end=args.year_end)
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
