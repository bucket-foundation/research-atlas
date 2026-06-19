#!/usr/bin/env python3
"""Full Gates Foundation ingestion -- the entire Committed Grants database.

Downloads the Gates committed-grants CSV once (cached under data/raw/gates/),
then streams every grant through the memory-bounded BulkWriter into partitioned
parquet shards under data/processed/<table>/source=gates/year=.../part-*.parquet.
Idempotent (the cached CSV is reused; per-(source,year) partitions overwrite);
offline after the one download.

ROR is resolved in the cross-source backfill (scripts/resolve_ror.py), not here,
so org rows are name-keyed at ingest (same pattern as CORDIS / NSF bulk).

Usage:
    python scripts/ingest_gates.py
    python scripts/ingest_gates.py --csv-url <override>

After this, run scripts/consolidate.py (or scripts/build_all.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.bulkwrite import BulkWriter  # noqa: E402
from atlas.connectors.gates import GATES_CSV_URL, GatesConnector  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Full Gates ingest -> sharded parquet.")
    ap.add_argument("--csv-url", default=GATES_CSV_URL)
    ap.add_argument("--batch-rows", type=int, default=200_000)
    args = ap.parse_args()

    conn = GatesConnector(resolve_ror=False)
    bw = BulkWriter(source="gates", batch_rows=args.batch_rows)

    print("Gates full ingest (Committed Grants CSV) ...")
    pages = conn.fetch(csv_url=args.csv_url)
    n_grants = 0
    for row in conn.normalize(pages):
        bw.add(row)
        if row.table == "grant":
            n_grants += 1
            if n_grants % 10000 == 0:
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
