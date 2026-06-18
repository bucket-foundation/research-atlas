#!/usr/bin/env python3
"""Produce a small, committable sample of the processed atlas.

The full processed parquet is gitignored (it grows with bulk ingestion). This
script copies a trimmed slice into ``data/processed/sample/`` so the repo ships
real, demonstrable output without committing large dumps. Abstracts are
truncated to keep the sample small and the grant table light.

Usage:
    python scripts/ingest_nsf.py --keyword biophysics --limit 300
    python scripts/build_sample.py --max-grants 100
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas import schema  # noqa: E402
from atlas.connectors.base import DATA_PROCESSED  # noqa: E402
from atlas.manifest import build_manifest  # noqa: E402

SAMPLE_DIR = DATA_PROCESSED / "sample"
ABSTRACT_CAP = 600  # chars; keeps the sample grant table small


def main() -> int:
    import pandas as pd

    ap = argparse.ArgumentParser(description="Build committable sample.")
    ap.add_argument("--max-grants", type=int, default=100)
    args = ap.parse_args()

    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

    grant_path = DATA_PROCESSED / "grant.parquet"
    if not grant_path.exists():
        print("No grant.parquet -- run an ingest first.")
        return 1

    # Stratified slice: take an even share per source so the sample shows every
    # funder's shape (CORDIS/NSF/NIH/UKRI), not just whichever wrote first.
    all_grants = pd.read_parquet(grant_path)
    if "source" in all_grants.columns:
        sources = sorted(all_grants["source"].dropna().unique())
        per = max(1, args.max_grants // max(1, len(sources)))
        grants = pd.concat(
            [all_grants[all_grants["source"] == s].head(per) for s in sources],
            ignore_index=True,
        ).copy()
    else:
        grants = all_grants.head(args.max_grants).copy()
    if "abstract" in grants.columns:
        grants["abstract"] = grants["abstract"].astype("string").str.slice(
            0, ABSTRACT_CAP
        )
    keep_grant_ids = set(grants["atlas_id"])
    grants.to_parquet(SAMPLE_DIR / "grant.parquet", index=False)

    # Edges touching kept grants, then the entities those edges reference.
    keep_org, keep_person, keep_field = set(), set(), set()
    for table in ("funder_grant", "grant_org", "grant_person"):
        p = DATA_PROCESSED / f"{table}.parquet"
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        key = "dst_id" if table == "funder_grant" else "src_id"
        df = df[df[key].isin(keep_grant_ids)]
        df.to_parquet(SAMPLE_DIR / f"{table}.parquet", index=False)
        if table == "grant_org":
            keep_org |= set(df["dst_id"])
        if table == "grant_person":
            keep_person |= set(df["dst_id"])

    # person_org edges among kept people
    po = DATA_PROCESSED / "person_org.parquet"
    if po.exists():
        df = pd.read_parquet(po)
        df = df[df["src_id"].isin(keep_person)]
        df.to_parquet(SAMPLE_DIR / "person_org.parquet", index=False)
        keep_org |= set(df["dst_id"])

    # Entities
    for table, keep in (("organization", keep_org), ("person", keep_person)):
        p = DATA_PROCESSED / f"{table}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            df[df["atlas_id"].isin(keep)].to_parquet(
                SAMPLE_DIR / f"{table}.parquet", index=False
            )
    for table in ("funder", "field"):
        p = DATA_PROCESSED / f"{table}.parquet"
        if p.exists():
            pd.read_parquet(p).to_parquet(SAMPLE_DIR / f"{table}.parquet", index=False)

    manifest = build_manifest(processed_dir=SAMPLE_DIR,
                              manifest_path=SAMPLE_DIR / "MANIFEST.json")
    print(f"Sample written to {SAMPLE_DIR}")
    for d in manifest["datasets"]:
        print(f"  {d['table']:16s} {d['row_count']:>6,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
