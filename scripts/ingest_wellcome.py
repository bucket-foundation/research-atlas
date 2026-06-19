#!/usr/bin/env python3
"""Full Wellcome Trust ingestion -- the entire 360Giving grants list.

Downloads the Wellcome 360Giving XLSX once (cached as data/raw/wellcome/grants.xlsx,
URL discovered from the GrantNav publisher page or passed via --xlsx-url), then
streams every grant through the memory-bounded BulkWriter into partitioned parquet
shards. Idempotent + resumable; offline after the one download.

GBP amounts are normalized to USD with one documented fixed FX, stamped per grant.
ROR is resolved in the cross-source backfill (scripts/resolve_ror.py), not here.

Usage:
    python scripts/ingest_wellcome.py
    python scripts/ingest_wellcome.py --xlsx-url <override>

After this, run scripts/consolidate.py (or scripts/build_all.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.bulkwrite import BulkWriter  # noqa: E402
from atlas.connectors.wellcome import WellcomeConnector  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Full Wellcome ingest -> sharded parquet.")
    ap.add_argument("--xlsx-url", default=None,
                    help="override the discovered 360Giving XLSX URL")
    ap.add_argument("--batch-rows", type=int, default=200_000)
    args = ap.parse_args()

    conn = WellcomeConnector(resolve_ror=False)
    bw = BulkWriter(source="wellcome", batch_rows=args.batch_rows)

    print("Wellcome full ingest (360Giving XLSX) ...")
    pages = conn.fetch(xlsx_url=args.xlsx_url)
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
