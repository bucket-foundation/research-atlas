#!/usr/bin/env python3
"""Enrich PUBLIC professional contacts for the high-value researcher segment.

Stage 2 (network, bounded, polite, idempotent): for the high-value segment
(active corresponding authors / PIs and rising stars, balanced across the top
fields), look up a PUBLIC professional email from ORCID public profiles and
EuropePMC/PubMed corresponding-author metadata, with full provenance.

COMPLIANCE: every email is public-sourced + provenanced, or null. Nothing is
fabricated. ``opt_out`` is honored (opted-out rows are skipped and never
contacted). The email-bearing parquet stays gitignored.

The lookup is **capped** (``--limit`` per field) so the pull is polite and the
reported coverage is an honest per-field sample rate, not a claim about the
whole corpus. Re-running resumes from the on-disk cache.

Usage:
    python scripts/enrich_contacts.py --limit 150           # per-field cap
    python scripts/enrich_contacts.py --limit 50 --orcid-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import DATA_PROCESSED  # noqa: E402
from atlas.users import contacts as C  # noqa: E402
from atlas.users.schema import USER_COLUMNS, coerce_user  # noqa: E402
from atlas.users.segment import is_high_value  # noqa: E402

RESEARCHERS = DATA_PROCESSED / "researchers.parquet"


def main() -> int:
    import pandas as pd

    ap = argparse.ArgumentParser(description="Enrich public contacts (high-value).")
    ap.add_argument("--limit", type=int, default=120,
                    help="max people to look up PER roadmap field (politeness cap)")
    ap.add_argument("--delay", type=float, default=0.15)
    ap.add_argument("--orcid-only", action="store_true",
                    help="only use ORCID public email (skip EuropePMC)")
    ap.add_argument("--in", dest="inp", default=str(RESEARCHERS))
    args = ap.parse_args()

    df = pd.read_parquet(args.inp)
    print(f"Loaded {len(df):,} profiles from {args.inp}", flush=True)

    # high-value mask (vectorized -- row-wise apply over 1M+ rows is too slow):
    # active-pi, OR (active/active-pi corresponding author with real seniority).
    corr = df["is_corresponding_author"].fillna(False).astype(bool)
    act = df["activity_tier"]
    sen = df["seniority"]
    hv = (act == "active-pi") | (
        corr & act.isin(["active-pi", "active"]) &
        sen.isin(["rising-star", "established", "eminent"]))
    pool = df[hv & (~df["opt_out"].fillna(False).astype(bool))].copy()
    print(f"High-value, not-opted-out pool: {len(pool):,}", flush=True)

    # rank within field by impact so we spend the budget on the richest targets
    pool = pool.sort_values(
        ["field_slug", "h_index_proxy", "total_citations"],
        ascending=[True, False, False])

    sess = C._session()
    attempted = found = 0
    per_field_attempt: dict[str, int] = {}
    per_field_found: dict[str, int] = {}
    by_source: dict[str, int] = {}
    updates: dict[str, dict] = {}

    for _, r in pool.iterrows():
        fs = r["field_slug"]
        if per_field_attempt.get(fs, 0) >= args.limit:
            continue
        per_field_attempt[fs] = per_field_attempt.get(fs, 0) + 1
        attempted += 1
        if args.orcid_only:
            hit = C.orcid_public_email(r["orcid"], sess=sess, delay=args.delay)
        else:
            hit = C.find_public_contact(r["orcid"], r["full_name"], fs,
                                        sess=sess, delay=args.delay)
        if hit:
            found += 1
            per_field_found[fs] = per_field_found.get(fs, 0) + 1
            by_source[hit["email_source"]] = by_source.get(hit["email_source"], 0) + 1
            updates[r["atlas_id"]] = hit
        if attempted % 25 == 0:
            print(f"  attempted {attempted}, found {found}", flush=True)

    # apply updates through coerce_user (re-validates: no fabricated emails).
    # Build validated replacement rows keyed by atlas_id, then assign by position.
    if updates:
        idx_by_id = {aid: i for i, aid in enumerate(df["atlas_id"].tolist())}
        for aid, hit in updates.items():
            i = idx_by_id.get(aid)
            if i is None:
                continue
            base = df.iloc[i].to_dict()
            base["atlas_id"] = aid
            base.update(hit)
            base["contactable"] = True
            base["engagement_status"] = base.get("engagement_status") or "not-contacted"
            validated = coerce_user(base)
            for k, v in validated.items():
                df.iat[i, df.columns.get_loc(k)] = v

    df = df.reindex(columns=USER_COLUMNS)
    df.to_parquet(args.inp, index=False)

    print(f"\nAttempted {attempted}, found public emails for {found} "
          f"({100*found/max(1,attempted):.1f}%)")
    print("By source:", by_source)
    print("\nPer-field contact coverage (of attempted):")
    for fs in sorted(per_field_attempt):
        a = per_field_attempt[fs]
        f = per_field_found.get(fs, 0)
        print(f"  {fs:16s} {f:>5}/{a:<5} ({100*f/max(1,a):.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
