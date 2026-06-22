#!/usr/bin/env python3
"""Ingest a coherent OpenAlex subfield corpus for the ranking system.

Pulls one subfield window (default: subfield 3106 = Nuclear & High Energy
Physics, 2015-2024, the complete analogue of Gian's arXiv hep-ph) WITH
out-references (``referenced_works``) and global in-citation counts
(``cited_by_count``) and abstracts. Caches every page; a re-run resumes from
disk (idempotent, polite).

Writes ``data/processed/ranking/corpus.parquet`` (one row per work: id, title,
abstract text, year, type, cited_by_count, topic, refs[]). The heavy parquet is
gitignored; a committed sample + manifest ship.

Usage:
    # bounded smoke test
    python scripts/ranking_ingest_corpus.py --max-pages 3

    # the real pull (all pages of the subfield window)
    python scripts/ranking_ingest_corpus.py

    # rebuild corpus.parquet from cache only (no network)
    python scripts/ranking_ingest_corpus.py --cache-only
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from atlas.ranking.corpus import CorpusConnector, DEFAULT_SUBFIELD  # noqa: E402
from atlas.connectors.base import DATA_PROCESSED  # noqa: E402

OUT = DATA_PROCESSED / "ranking" / "corpus.parquet"


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest a subfield corpus.")
    ap.add_argument("--subfield", default=DEFAULT_SUBFIELD)
    ap.add_argument("--from", dest="from_date", default="2015-01-01")
    ap.add_argument("--to", dest="to_date", default="2024-12-31")
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--cache-only", action="store_true")
    args = ap.parse_args()

    conn = CorpusConnector()
    print(f"Fetching subfield {args.subfield} "
          f"{args.from_date}..{args.to_date} "
          f"(max_pages={args.max_pages or 'all'}, "
          f"cache_only={args.cache_only}) ...", flush=True)
    t0 = time.time()
    pages = conn.fetch(subfield=args.subfield, from_date=args.from_date,
                       to_date=args.to_date, max_pages=args.max_pages,
                       cache_only=args.cache_only)
    records = list(conn.normalize(pages))
    print(f"  normalized {len(records):,} works in {time.time()-t0:.0f}s",
          flush=True)

    if not records:
        print("No records -- nothing written.", flush=True)
        return 1

    # store refs as a list column; parquet handles it natively via pyarrow
    df = pd.DataFrame(records)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)

    n_with_abs = df["abstract"].notna().sum()
    n_refs = df["refs"].map(len).sum()
    print(f"\nWrote {OUT}", flush=True)
    print(f"  works:                {len(df):,}", flush=True)
    print(f"  with abstract:        {n_with_abs:,} "
          f"({100*n_with_abs/len(df):.1f}%)", flush=True)
    print(f"  total out-references: {n_refs:,}", flush=True)
    print(f"  mean refs/work:       {n_refs/len(df):.1f}", flush=True)
    print(f"  median cited_by:      {int(df['cited_by_count'].median())}",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
