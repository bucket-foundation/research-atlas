#!/usr/bin/env python3
"""Build the COMMITTABLE, email-free researcher/users artifacts.

The full ``researchers.parquet`` carries harvested public emails and is
gitignored -- a public repo must never ship harvested personal contact data
(see ``docs/USERS_POLICY.md``). This script produces what IS safe to commit:

1. ``data/processed/sample/researchers_sample.parquet`` -- a real slice of
   profiles with **all PII columns dropped** (no email/source/url/as_of), so the
   schema and segmentation are demonstrable without leaking contacts.
2. ``data/processed/sample/researchers_aggregates.json`` -- counts only:
   profiles per field, per seniority, per activity tier, and the **real
   contact-coverage %** per field (how many of each field got a public email),
   plus a tool-fit roll-up. Numbers, never addresses.

Re-running is idempotent. A guard asserts the sample contains zero email-like
strings before writing.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import DATA_PROCESSED  # noqa: E402
from atlas.users.schema import PII_COLUMNS, USERS_SCHEMA_VERSION  # noqa: E402

RESEARCHERS = DATA_PROCESSED / "researchers.parquet"
SAMPLE_DIR = DATA_PROCESSED / "sample"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _has_email_anywhere(df) -> bool:
    for col in df.columns:
        if df[col].dtype == object:
            s = df[col].dropna().astype(str)
            if s.apply(lambda v: bool(EMAIL_RE.search(v))).any():
                return True
    return False


def main() -> int:
    import pandas as pd

    if not RESEARCHERS.exists():
        print("No researchers.parquet -- run scripts/build_users.py first.")
        return 1

    df = pd.read_parquet(RESEARCHERS)
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

    # ---- aggregates (counts only, including REAL contact coverage) ---------- #
    has_email = df["email"].notna() if "email" in df.columns else pd.Series(False, index=df.index)
    contactable = df["contactable"].fillna(False).astype(bool) if "contactable" in df.columns else pd.Series(False, index=df.index)

    by_field = df.groupby("field_slug").size().sort_values(ascending=False)
    cov_attempted_note = (
        "contact_coverage_pct is computed over profiles that carry a public "
        "email / total profiles in the field -- it is an HONEST sample rate, "
        "bounded by the high-value enrichment cap, NOT a claim that the rest "
        "have no public email (they were simply not yet looked up)."
    )
    # The high-value segment is the only segment we deep-enrich contacts for, so
    # the honest coverage denominator is THAT pool, not the whole corpus.
    if {"is_corresponding_author", "activity_tier", "seniority"} <= set(df.columns):
        corr = df["is_corresponding_author"].fillna(False).astype(bool)
        act = df["activity_tier"]
        sen = df["seniority"]
        hv = (act == "active-pi") | (
            corr & act.isin(["active-pi", "active"]) &
            sen.isin(["rising-star", "established", "eminent"]))
    else:
        hv = pd.Series(False, index=df.index)

    contact_by_field = {}
    for slug, total in by_field.items():
        in_field = df["field_slug"] == slug
        n_email = int(has_email[in_field].sum())
        hv_field = int((hv & in_field).sum())
        hv_email = int((hv & in_field & has_email).sum())
        contact_by_field[slug] = {
            "profiles": int(total),
            "with_public_email": n_email,
            "contact_coverage_pct": round(100 * n_email / total, 3) if total else 0.0,
            "high_value_profiles": hv_field,
            "high_value_with_email": hv_email,
            "high_value_coverage_pct": round(100 * hv_email / hv_field, 2) if hv_field else 0.0,
        }

    # per-source breakdown (counts only, EMAIL-FREE -- never the addresses)
    by_source = {}
    if "email_source" in df.columns:
        vc = df.loc[has_email, "email_source"].value_counts()
        by_source = {str(k): int(v) for k, v in vc.items()}
    by_method = {}
    if "email_method" in df.columns:
        vc = df.loc[has_email, "email_method"].value_counts()
        by_method = {str(k): int(v) for k, v in vc.items()}

    aggregates = {
        "users_schema_version": USERS_SCHEMA_VERSION,
        "total_researchers": int(len(df)),
        "with_orcid": int(df["orcid"].notna().sum()),
        "corresponding_authors": int(df["is_corresponding_author"].fillna(False).astype(bool).sum()),
        "high_value_segment_total": int(hv.sum()),
        "high_value_with_email": int((hv & has_email).sum()),
        "high_value_coverage_pct": (round(100 * int((hv & has_email).sum()) / int(hv.sum()), 2)
                                    if int(hv.sum()) else 0.0),
        "contactable_total": int(contactable.sum()),
        "with_public_email_total": int(has_email.sum()),
        "by_email_source": by_source,
        "by_email_method": by_method,
        "by_field": {k: int(v) for k, v in by_field.items()},
        "by_seniority": {k: int(v) for k, v in df.groupby("seniority").size().items()},
        "by_activity_tier": {k: int(v) for k, v in df.groupby("activity_tier").size().items()},
        "contact_coverage_by_field": contact_by_field,
        "contact_coverage_note": cov_attempted_note,
        "engagement_status_counts": {k: int(v) for k, v in df.groupby("engagement_status").size().items()},
    }
    (SAMPLE_DIR / "researchers_aggregates.json").write_text(
        json.dumps(aggregates, indent=2), encoding="utf-8")

    # ---- email-free sample slice ------------------------------------------- #
    # A stratified, real slice across fields + seniority, PII columns dropped.
    parts = []
    for slug in df["field_slug"].unique():
        sub = df[df["field_slug"] == slug].sort_values(
            ["h_index_proxy", "works_count"], ascending=False)
        parts.append(sub.head(40))
    sample = pd.concat(parts, ignore_index=True)
    sample = sample.drop(columns=[c for c in PII_COLUMNS if c in sample.columns])

    # hard guard: zero emails may appear anywhere in the committed sample
    assert not _has_email_anywhere(sample), \
        "SECURITY: email-like string found in the email-free sample -- aborting"

    sample.to_parquet(SAMPLE_DIR / "researchers_sample.parquet", index=False)

    print(f"Wrote email-free sample: {len(sample):,} rows "
          f"({len(sample.columns)} cols, PII dropped)")
    print(f"Wrote aggregates: total {aggregates['total_researchers']:,}, "
          f"public emails {aggregates['with_public_email_total']:,}")
    print("Contact coverage by field:")
    for slug, c in contact_by_field.items():
        print(f"  {slug:16s} {c['with_public_email']:>5}/{c['profiles']:<8,} "
              f"({c['contact_coverage_pct']}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
