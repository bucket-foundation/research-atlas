#!/usr/bin/env python3
"""Enrich PUBLIC professional contacts for the high-value researcher segment.

Stage 2 (network, bounded, polite, idempotent): for the high-value segment
(active corresponding authors / PIs and rising stars, balanced across the top
fields), look up a PUBLIC professional email from four public sources -- ORCID
public profiles, EuropePMC/PubMed corresponding-author metadata, Crossref author
metadata, and the researcher's own public homepage -- with full provenance.

COMPLIANCE: every email is public-sourced + provenanced, or null. Nothing is
fabricated. ``opt_out`` is honored (opted-out rows are skipped and never
contacted). The email-bearing parquet stays gitignored.

Source priority is per-field: **biomed authors hit EuropePMC first** (the
highest-yield public source for them); other fields lead with ORCID public
email. Override with ``--epmc-first`` (all fields EuropePMC-first) or
``--orcid-only`` / ``--sources``.

The lookup is **capped** (``--limit`` per field) so the pull is polite and the
reported coverage is an honest per-field sample rate. Re-running resumes from
the on-disk cache (a cache hit is never re-fetched), and an already-found
contact is skipped, so re-runs only spend budget on not-yet-found people.

Usage:
    python scripts/enrich_contacts.py --limit 3000            # per-field cap
    python scripts/enrich_contacts.py --limit 2000 --epmc-first
    python scripts/enrich_contacts.py --limit 500 --sources orcid,europepmc
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

RESEARCHERS = DATA_PROCESSED / "researchers.parquet"

# Fields for which EuropePMC corresponding-author metadata is the highest-yield
# public source, so we try it first.
EPMC_FIRST_FIELDS = {"biomed-bio", "chemistry"}


def main() -> int:
    import pandas as pd

    ap = argparse.ArgumentParser(description="Enrich public contacts (high-value).")
    ap.add_argument("--limit", type=int, default=3000,
                    help="max people to look up PER roadmap field (politeness cap)")
    ap.add_argument("--delay", type=float, default=0.1)
    ap.add_argument("--orcid-only", action="store_true",
                    help="only use ORCID public email (skip other sources)")
    ap.add_argument("--epmc-first", action="store_true",
                    help="try EuropePMC first for ALL fields (default: only biomed/chem)")
    ap.add_argument("--sources", default=None,
                    help="comma list restricting sources, e.g. 'orcid,europepmc,crossref,labpage'")
    ap.add_argument("--in", dest="inp", default=str(RESEARCHERS))
    ap.add_argument("--progress", type=int, default=50)
    ap.add_argument("--checkpoint", type=int, default=25,
                    help="flush the parquet to disk every N found contacts "
                         "(0 = only at the end); makes the run kill-safe")
    args = ap.parse_args()

    sources = None
    if args.orcid_only:
        sources = {"orcid"}
    elif args.sources:
        sources = {s.strip() for s in args.sources.split(",") if s.strip()}

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
    not_opted = ~df["opt_out"].fillna(False).astype(bool)
    # skip rows that already have a public email (idempotent resume across runs)
    already = df["email"].notna() if "email" in df.columns else pd.Series(False, index=df.index)
    pool = df[hv & not_opted & (~already)].copy()
    print(f"High-value pool: {int((hv & not_opted).sum()):,} "
          f"(already have email: {int((hv & not_opted & already).sum()):,}) "
          f"-> to look up: {len(pool):,}", flush=True)

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

    idx_by_id = {aid: i for i, aid in enumerate(df["atlas_id"].tolist())}

    def flush():
        """Apply pending hits through coerce_user (re-validates: no fabricated
        emails) and checkpoint the parquet to disk, so a kill never loses banked
        contacts. Idempotent: a re-run skips rows that already have an email."""
        if not updates:
            return
        for aid, hit in list(updates.items()):
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
        updates.clear()
        df.reindex(columns=USER_COLUMNS).to_parquet(args.inp, index=False)

    try:
        for _, r in pool.iterrows():
            fs = r["field_slug"]
            if per_field_attempt.get(fs, 0) >= args.limit:
                continue
            per_field_attempt[fs] = per_field_attempt.get(fs, 0) + 1
            attempted += 1
            epmc_first = args.epmc_first or (fs in EPMC_FIRST_FIELDS)
            hit = C.find_public_contact(
                r["orcid"], r["full_name"], fs, sess=sess, delay=args.delay,
                epmc_first=epmc_first, sources=sources)
            if hit:
                found += 1
                per_field_found[fs] = per_field_found.get(fs, 0) + 1
                by_source[hit["email_source"]] = by_source.get(hit["email_source"], 0) + 1
                updates[r["atlas_id"]] = hit
            if args.progress and attempted % args.progress == 0:
                print(f"  attempted {attempted}, found {found} "
                      f"({100*found/max(1,attempted):.0f}%)  {by_source}", flush=True)
            # checkpoint every `checkpoint` hits so progress survives a kill
            if args.checkpoint and len(updates) >= args.checkpoint:
                flush()
                print(f"  [checkpoint] wrote {args.inp} "
                      f"(found so far {found})", flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted -- flushing partial progress...", flush=True)
    finally:
        flush()

    df = df.reindex(columns=USER_COLUMNS)

    total_emails = int(df["email"].notna().sum())
    print(f"\nThis run: attempted {attempted}, found {found} new public emails "
          f"({100*found/max(1,attempted):.1f}%)")
    print("By source (this run):", by_source)
    print(f"Total public emails in table now: {total_emails:,}")
    print("\nPer-field contact attempts this run (found/attempted):")
    for fs in sorted(per_field_attempt):
        a = per_field_attempt[fs]
        f = per_field_found.get(fs, 0)
        print(f"  {fs:16s} {f:>5}/{a:<6} ({100*f/max(1,a):.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
