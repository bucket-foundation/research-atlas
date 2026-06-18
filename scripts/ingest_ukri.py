#!/usr/bin/env python3
"""Ingest UKRI / Gateway to Research projects into the atlas.

Sample-friendly; idempotent + resumable (caches raw under data/raw/ukri/).
Re-running with the same args resumes from cache and converges the parquet.

Usage:
    python scripts/ingest_ukri.py --term biophysics --limit 100
    python scripts/ingest_ukri.py --limit 200 --persons      # resolve PIs
    python scripts/ingest_ukri.py --limit 100 --no-ror
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.ukri import UkriConnector  # noqa: E402
from atlas.manifest import build_manifest  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest UKRI GtR projects.")
    ap.add_argument("--term", default=None, help="GtR free-text search term")
    ap.add_argument("--limit", type=int, default=100, help="max projects to pull")
    ap.add_argument("--persons", action="store_true",
                    help="resolve PI/co-I persons (extra fetches; slower)")
    ap.add_argument("--no-ror", action="store_true", help="skip ROR resolution")
    args = ap.parse_args()

    print(f"UKRI ingest: term={args.term!r} limit={args.limit} "
          f"persons={'on' if args.persons else 'off'} "
          f"ror={'off' if args.no_ror else 'on'}")
    conn = UkriConnector(resolve_ror=not args.no_ror, resolve_persons=args.persons)
    counts = conn.run(term=args.term, limit=args.limit)
    print("\nEmitted rows per table:")
    for table, n in sorted(counts.items()):
        print(f"  {table:16s} {n:>7,}")

    manifest = build_manifest()
    print(f"\nManifest: {manifest['totals']['tables']} tables, "
          f"{manifest['totals']['rows']:,} rows -> data/MANIFEST.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
