#!/usr/bin/env python3
"""Ingest NSF awards into the atlas. Sample-friendly; idempotent + resumable.

Usage:
    python scripts/ingest_nsf.py --keyword biophysics --limit 300
    python scripts/ingest_nsf.py --keyword "machine learning" --limit 500 --no-ror

Re-running with the same args resumes from the raw cache and converges the
parquet (no duplicates). Updates data/MANIFEST.json at the end.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.nsf import NsfConnector  # noqa: E402
from atlas.manifest import build_manifest  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest NSF awards into research-atlas.")
    ap.add_argument("--keyword", default="biophysics", help="NSF keyword search")
    ap.add_argument("--limit", type=int, default=300, help="max awards to pull")
    ap.add_argument("--date-start", default=None, help="MM/DD/YYYY")
    ap.add_argument("--date-end", default=None, help="MM/DD/YYYY")
    ap.add_argument("--no-ror", action="store_true", help="skip ROR resolution")
    args = ap.parse_args()

    print(f"NSF ingest: keyword={args.keyword!r} limit={args.limit} "
          f"ror={'off' if args.no_ror else 'on'}")
    conn = NsfConnector(resolve_ror=not args.no_ror)
    counts = conn.run(
        keyword=args.keyword, limit=args.limit,
        date_start=args.date_start, date_end=args.date_end,
    )
    print("\nEmitted rows per table:")
    for table, n in sorted(counts.items()):
        print(f"  {table:16s} {n:>7,}")

    manifest = build_manifest()
    print(f"\nManifest: {manifest['totals']['tables']} tables, "
          f"{manifest['totals']['rows']:,} rows -> data/MANIFEST.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
