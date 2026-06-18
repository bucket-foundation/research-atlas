#!/usr/bin/env python3
"""Ingest DFG grants into the atlas, from GEPRIS (polite HTML scrape).

GEPRIS has no JSON API or bulk export, so this scrapes individual English
project detail pages (allowed by robots.txt) discovered via the GEPRIS sitemap.
Runs a SMALL sample by default and caches every page under data/raw/dfg/ --
re-running is free. Be polite: keep --limit low.

Usage:
    python scripts/ingest_dfg.py --limit 10
    python scripts/ingest_dfg.py --ids 268853 268879 --no-ror
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.dfg import DfgConnector  # noqa: E402
from atlas.manifest import build_manifest  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest DFG grants from GEPRIS.")
    ap.add_argument("--limit", type=int, default=10, help="max projects (keep low)")
    ap.add_argument("--ids", nargs="*", default=None,
                    help="explicit GEPRIS project ids (skips sitemap discovery)")
    ap.add_argument("--no-ror", action="store_true", help="skip ROR resolution")
    args = ap.parse_args()

    print(f"DFG ingest: limit={args.limit} ids={args.ids} "
          f"ror={'off' if args.no_ror else 'on'}")
    conn = DfgConnector(resolve_ror=not args.no_ror)
    counts = conn.run(project_ids=args.ids, limit=args.limit)
    print("\nEmitted rows per table:")
    for table, n in sorted(counts.items()):
        print(f"  {table:16s} {n:>7,}")

    manifest = build_manifest()
    print(f"\nManifest: {manifest['totals']['tables']} tables, "
          f"{manifest['totals']['rows']:,} rows -> data/MANIFEST.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
