#!/usr/bin/env python3
"""Build the enriched researcher/users table from the atlas.

Stage 1 (FULL, cheap, no network): stream the cached raw OpenAlex work pages and
emit one enriched profile per author -- field, activity, impact, seniority,
segment, tool-fit -- for *every* researcher in the corpus.

Output: ``data/processed/researchers.parquet`` (the email-bearing CRM table; the
email columns are null after this stage and filled by
``scripts/enrich_contacts.py``). This parquet is **gitignored** (it will carry
PII once contacts are enriched); only the schema, aggregates, and an email-free
sample are committed.

Usage:
    python scripts/build_users.py                 # full build
    python scripts/build_users.py --progress 200  # log every 200 raw pages
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import DATA_PROCESSED  # noqa: E402
from atlas.users.profiles import build_profiles  # noqa: E402
from atlas.users.schema import USER_COLUMNS  # noqa: E402

OUT = DATA_PROCESSED / "researchers.parquet"


def main() -> int:
    import pandas as pd

    ap = argparse.ArgumentParser(description="Build enriched researcher/users table.")
    ap.add_argument("--progress", type=int, default=200,
                    help="log every N raw pages (0 = silent)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    print("Building researcher profiles from raw OpenAlex works (no network)...")
    rows = list(build_profiles(progress_every=args.progress))
    df = pd.DataFrame(rows).reindex(columns=USER_COLUMNS)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)

    print(f"\nWrote {len(df):,} researcher profiles -> {args.out}")
    print(f"  with ORCID            : {df['orcid'].notna().sum():,}")
    print(f"  corresponding authors : {df['is_corresponding_author'].sum():,}")
    print(f"  active-pi             : {(df['activity_tier']=='active-pi').sum():,}")
    print("  by roadmap field slug :")
    for slug, n in df["field_slug"].value_counts().items():
        print(f"    {slug:16s} {n:>8,}")
    print("  by seniority          :")
    for sen, n in df["seniority"].value_counts().items():
        print(f"    {sen:16s} {n:>8,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
