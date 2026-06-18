#!/usr/bin/env python3
"""Full-scale CORDIS (EU) ingestion -- ALL projects, all schemes, all orgs/fields.

Streams every project from the CORDIS bulk zips (Horizon Europe + H2020) through
the memory-bounded BulkWriter into partitioned parquet shards under
data/processed/<table>/source=cordis/year=.../part-*.parquet. Idempotent per
(source, year) partition; resumable; offline (no network unless --ror).

Usage:
    python scripts/ingest_cordis.py                 # full: ~56k projects
    python scripts/ingest_cordis.py --limit 500     # quick slice for testing
    python scripts/ingest_cordis.py --ror           # resolve ROR (slow, network)

After this, run scripts/consolidate.py to fold shards into flat parquet.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# CORDIS objective/keywords fields can exceed the default csv field cap.
csv.field_size_limit(10_000_000)

from atlas.bulkwrite import BulkWriter  # noqa: E402
from atlas.connectors.base import DATA_RAW  # noqa: E402
from atlas.connectors.cordis import CordisConnector  # noqa: E402

SIBLING_CORDIS = Path("/home/gian/agfarms/biophysics-phd-review/data/raw")
CORDIS_ARCHIVES = ("cordis_horizon.zip", "cordis_h2020.zip")


def _ensure_cordis_cache() -> Path:
    dest = DATA_RAW / "cordis"
    dest.mkdir(parents=True, exist_ok=True)
    for name in CORDIS_ARCHIVES:
        dst = dest / name
        if dst.exists():
            continue
        src = SIBLING_CORDIS / name
        if src.exists():
            print(f"  copying {name} -> data/raw/cordis/ ({src.stat().st_size:,} B)")
            shutil.copy2(src, dst)
        else:
            print(f"  WARNING: sibling archive not found: {src}")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="Full CORDIS ingest -> sharded parquet.")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap projects per zip (default: ALL)")
    ap.add_argument("--ror", action="store_true",
                    help="resolve ROR (network; slow at full scale)")
    ap.add_argument("--batch-rows", type=int, default=200_000)
    args = ap.parse_args()

    cordis_dir = _ensure_cordis_cache()
    zips = sorted(cordis_dir.glob("cordis_*.zip"))
    if not zips:
        print("No CORDIS zips found.")
        return 1

    conn = CordisConnector(resolve_ror=args.ror)
    bw = BulkWriter(source="cordis", batch_rows=args.batch_rows)

    print(f"CORDIS full ingest: limit={args.limit or 'ALL'} "
          f"ror={'on' if args.ror else 'off'}")
    for zip_path in zips:
        print(f"  streaming {zip_path.name} ...")
        n = 0
        for row in conn.iter_rows(zip_path, limit=args.limit):
            bw.add(row)
            if row.table == "grant":
                n += 1
                if n % 5000 == 0:
                    print(f"    {n:,} projects ...")
        print(f"  {zip_path.name}: {n:,} projects streamed")
    bw.flush_all()

    print("\nShard rows written per table (pre-dedup):")
    for table, c in sorted(bw.partition_counts().items()):
        print(f"  {table:16s} {c:>9,}")
    print("\nNext: python scripts/consolidate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
