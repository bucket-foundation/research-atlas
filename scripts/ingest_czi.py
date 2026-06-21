#!/usr/bin/env python3
"""Full Chan Zuckerberg Initiative (CZI) ingestion -- the public grants REST API.

Downloads the single CZI grants document
(https://chanzuckerberg.com/wp-json/czi/v1/grants/) once into data/raw/czi/
(idempotent resume: a present file is reused), then streams every grant through
the memory-bounded BulkWriter into partitioned parquet shards.

ROR is resolved in the cross-source backfill (scripts/resolve_ror.py), not here.

Usage:
    python scripts/ingest_czi.py

After this, run scripts/consolidate.py (or scripts/build_all.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.bulkwrite import BulkWriter  # noqa: E402
from atlas.connectors.czi import CziConnector  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Full CZI ingest -> sharded parquet.")
    ap.add_argument("--batch-rows", type=int, default=200_000)
    args = ap.parse_args()

    conn = CziConnector(resolve_ror=False)
    bw = BulkWriter(source="czi", batch_rows=args.batch_rows)

    print("CZI ingest: grants REST API (single document)")
    pages = conn.fetch()
    n_grants = 0
    for row in conn.normalize(pages):
        bw.add(row)
        if row.table == "grant":
            n_grants += 1
            if n_grants % 1000 == 0:
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
