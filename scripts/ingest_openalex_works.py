#!/usr/bin/env python3
"""Ingest OpenAlex works (the output side) and link them to our grants.

Pulls works that acknowledge our funders (NIH/NSF/EC/ERC/UKRI) via OpenAlex's
``awards.funder_id`` filter, cursor-paged through the polite pool, caching every
page for idempotent resume. Builds the grant-award join index from our existing
``grant.parquet`` once, then streams works through the normalizer and writes them
(and the person/org/field entities + grant_work/person_org/work_field edges) to
partitioned parquet shards via :class:`~atlas.bulkwrite.BulkWriter` so peak memory
is bounded by the batch size, not the dataset size.

After this, run ``scripts/consolidate.py`` to fold shards into the flat parquet,
then ``scripts/build_db.py`` and the manifest.

Usage:
    # bounded smoke test (a few pages per funder)
    python scripts/ingest_openalex_works.py --from 2024-01-01 --max-pages 2

    # the real pull (all pages from 2015), memory-bounded
    python scripts/ingest_openalex_works.py --from 2015-01-01

    # only certain funders
    python scripts/ingest_openalex_works.py --funders F4320332161 F4320306076
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from atlas.award_match import grant_keys  # noqa: E402
from atlas.bulkwrite import BulkWriter  # noqa: E402
from atlas.connectors.base import DATA_PROCESSED  # noqa: E402
from atlas.connectors.openalex_works import (  # noqa: E402
    FUNDER_OPENALEX_TO_SOURCE, OpenAlexWorksConnector)


def build_grant_key_index(processed: Path) -> dict[str, str]:
    """Map every award join key -> our grant atlas_id (last write wins on dup)."""
    gp = processed / "grant.parquet"
    if not gp.exists():
        raise SystemExit(f"{gp} not found -- ingest funders + consolidate first")
    df = pd.read_parquet(gp, columns=["atlas_id", "source", "source_id"])
    index: dict[str, str] = {}
    for atlas_id, source, source_id in df.itertuples(index=False):
        for key in grant_keys(source, source_id):
            index[key] = atlas_id
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest OpenAlex works + grant links.")
    ap.add_argument("--from", dest="from_date", default="2015-01-01")
    ap.add_argument("--funders", nargs="*", default=None,
                    help="OpenAlex funder ids (default: all known)")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="max pages per funder (0 = all)")
    ap.add_argument("--batch-rows", type=int, default=200_000)
    ap.add_argument("--processed", default=str(DATA_PROCESSED))
    args = ap.parse_args()

    processed = Path(args.processed)
    print("Building grant-award join index from grant.parquet ...", flush=True)
    t0 = time.time()
    gindex = build_grant_key_index(processed)
    print(f"  {len(gindex):,} award join keys in {time.time()-t0:.1f}s", flush=True)

    conn = OpenAlexWorksConnector(processed_dir=processed)
    bw = BulkWriter(source="openalex", processed_dir=processed,
                    batch_rows=args.batch_rows)

    funders = args.funders or list(FUNDER_OPENALEX_TO_SOURCE)
    # reset the openalex partitions for a clean idempotent rewrite of this slice.
    # works/person/etc. are partitioned by year, so clear every source=openalex
    # year dir (other sources' shards are untouched).
    import shutil
    for table in ("work", "person", "organization", "field",
                  "grant_work", "person_org", "work_field"):
        src_dir = processed / table / "source=openalex"
        if src_dir.exists():
            shutil.rmtree(src_dir)

    print(f"Fetching works for {len(funders)} funder(s) from {args.from_date} "
          f"(max_pages_per_funder={args.max_pages or 'all'}) ...", flush=True)
    t0 = time.time()
    pages = conn.fetch(funder_ids=funders, from_date=args.from_date,
                       max_pages_per_funder=args.max_pages)
    n_rows = 0
    for row in conn.normalize(pages, grant_key_index=gindex):
        bw.add(row)
        n_rows += 1
        if n_rows % 200_000 == 0:
            print(f"  ... {n_rows:,} rows buffered "
                  f"({time.time()-t0:.0f}s)", flush=True)
    bw.flush_all()

    counts = bw.partition_counts()
    print(f"\nWrote {sum(counts.values()):,} rows in {time.time()-t0:.0f}s:",
          flush=True)
    for table, n in sorted(counts.items()):
        print(f"  {table:16s} {n:>10,}", flush=True)
    print("\nNext: scripts/consolidate.py -> scripts/build_db.py", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
