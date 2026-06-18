#!/usr/bin/env python3
"""Fold partitioned parquet shards into the flat published parquet (deduped).

The bulk ingest scripts write many small shards under
data/processed/<table>/source=.../year=.../part-*.parquet. This step reads them
back with DuckDB (out-of-core), de-duplicates by the canonical rule
(entities on atlas_id, edges on (src,dst,role|score,source), newest as_of wins),
and writes the flat data/processed/<table>.parquet that build_db.py /
manifest.py / build_sample.py consume. Then it rebuilds the manifest.

Usage:
    python scripts/consolidate.py
    python scripts/consolidate.py --memory-limit 6GB --temp-dir /tmp/atlas_duck
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.consolidate import consolidate_all  # noqa: E402
from atlas.manifest import build_manifest  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Consolidate shards -> flat parquet.")
    ap.add_argument("--memory-limit", default="4GB")
    ap.add_argument("--temp-dir", default=None)
    args = ap.parse_args()

    print("Consolidating sharded parquet -> flat parquet (DuckDB, out-of-core) ...")
    counts = consolidate_all(memory_limit=args.memory_limit,
                             temp_dir=args.temp_dir)
    print("\nFlat parquet rows per table (post-dedup):")
    for table, n in sorted(counts.items()):
        print(f"  {table:16s} {n:>10,}")

    manifest = build_manifest()
    print(f"\nManifest: {manifest['totals']['tables']} tables, "
          f"{manifest['totals']['rows']:,} rows -> data/MANIFEST.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
