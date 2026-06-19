#!/usr/bin/env python3
"""Full-scale DFG (GEPRIS) ingestion -- a real corpus, not a sample.

GEPRIS has no JSON API or bulk export, so this is a **polite, cached, resumable
HTML crawl** of English project detail pages, discovered from the GEPRIS sitemap
(~172k project ids across 4 sitemaps). Every detail page is cached under
data/raw/dfg/projekt_<id>.json, so a re-run only fetches pages not yet on disk
(the politeness-friendly path: hammer the site once, never twice).

Unlike scripts/ingest_dfg.py (a tiny default sample), this drives the same
DfgConnector at scale and streams rows through the memory-bounded BulkWriter into
partitioned parquet shards. ROR is resolved in the cross-source backfill
(scripts/resolve_ror.py), so the connector's per-name ROR API resolver is OFF.

GEPRIS rarely publishes a funding amount on the English project page, so most DFG
grants carry amount=None (money invariant: unknown is null, never 0).

Politeness: default 1.5s inter-request delay. A full cold crawl of 172k pages is
many hours -- run it in chunks with --limit and resume; the cache makes resume
free. Project detail pages are NOT disallowed by GEPRIS robots.txt (only faceted
search query-param URLs are).

Usage:
    python scripts/ingest_dfg_full.py --limit 2000            # a chunk
    python scripts/ingest_dfg_full.py --limit 200000          # everything
    python scripts/ingest_dfg_full.py --cached-only           # normalize cache only

After this, run scripts/consolidate.py (or scripts/build_all.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.bulkwrite import BulkWriter  # noqa: E402
from atlas.connectors.dfg import DfgConnector  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Full DFG/GEPRIS ingest -> shards.")
    ap.add_argument("--limit", type=int, default=2000,
                    help="max projects this run (resume-friendly; cache reused)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="polite inter-request delay (s)")
    ap.add_argument("--cached-only", action="store_true",
                    help="re-normalize only already-cached pages (no network)")
    ap.add_argument("--batch-rows", type=int, default=100_000)
    args = ap.parse_args()

    conn = DfgConnector(resolve_ror=False)
    conn.delay = args.delay
    conn.http.delay = args.delay
    bw = BulkWriter(source="dfg", batch_rows=args.batch_rows)

    if args.cached_only:
        print("DFG: normalizing cached pages only (no network) ...")
        pages = list(conn.iter_cached_raw())
        # only project pages carry an 'html' + 'id'
        pages = [p for p in pages if isinstance(p, dict) and p.get("id")
                 and p.get("html")]
    else:
        print(f"DFG full ingest: limit={args.limit} delay={args.delay}s "
              "(polite HTML crawl, cached/resumable)")
        pages = conn.fetch(limit=args.limit)

    n_grants = 0
    for row in conn.normalize(pages):
        bw.add(row)
        if row.table == "grant":
            n_grants += 1
            if n_grants % 1000 == 0:
                print(f"    {n_grants:,} projects ...")
    bw.flush_all()

    print(f"\n{n_grants:,} DFG projects normalized.")
    print("Shard rows written per table (pre-dedup):")
    for table, c in sorted(bw.partition_counts().items()):
        print(f"  {table:16s} {c:>9,}")
    print("\nNext: python scripts/consolidate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
