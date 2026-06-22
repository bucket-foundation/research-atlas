"""Integration compliance guards over the BUILT users artifacts.

These run against whatever is on disk (the full table if present locally, and the
committed email-free sample). They are the last line of defense before a commit:

- the committed sample must contain **zero** email-like strings and none of the
  PII columns;
- if the (local, gitignored) full table is present, every email it carries must
  be public-sourced + provenanced (no fabrication) and every contactable row
  must have a public email and not be opted out.

If an artifact is absent the relevant test is skipped (so CI on a fresh clone,
which has only the sample, still runs the sample guard).
"""

import re
from pathlib import Path

import pytest

from atlas.users.schema import (
    PII_COLUMNS,
    ALLOWED_EMAIL_SOURCES,
    is_plausible_email,
)

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "processed" / "sample" / "researchers_sample.parquet"
FULL = REPO / "data" / "processed" / "researchers.parquet"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _read(path):
    pd = pytest.importorskip("pandas")
    if not path.exists():
        pytest.skip(f"{path.name} not present")
    return pd.read_parquet(path)


# --------------------------------------------------------------------------- #
# the committed sample: must be email-free and PII-column-free
# --------------------------------------------------------------------------- #

def test_committed_sample_has_no_pii_columns():
    df = _read(SAMPLE)
    leaked = [c for c in PII_COLUMNS if c in df.columns]
    assert leaked == [], f"PII columns leaked into committed sample: {leaked}"


def test_committed_sample_has_no_email_strings():
    df = _read(SAMPLE)
    for col in df.columns:
        if df[col].dtype == object:
            vals = df[col].dropna().astype(str)
            hits = vals[vals.apply(lambda v: bool(EMAIL_RE.search(v)))]
            assert hits.empty, f"email-like string in committed sample col {col!r}"


# --------------------------------------------------------------------------- #
# the full (local) table: no fabricated emails, provenance + flags sound
# --------------------------------------------------------------------------- #

def test_full_table_emails_are_public_sourced_and_provenanced():
    df = _read(FULL)
    if "email" not in df.columns:
        pytest.skip("no email column")
    em = df[df["email"].notna()]
    if em.empty:
        pytest.skip("no emails harvested yet")
    # every email valid syntax
    assert em["email"].apply(is_plausible_email).all()
    # every email from a known PUBLIC source (anti-fabrication)
    assert set(em["email_source"]) <= ALLOWED_EMAIL_SOURCES
    # every email carries full provenance
    assert em["email_source_url"].notna().all()
    assert em["email_as_of"].notna().all()


def test_full_table_contactable_implies_public_email_and_not_opted_out():
    df = _read(FULL)
    if "contactable" not in df.columns:
        pytest.skip("no contactable column")
    c = df[df["contactable"].fillna(False).astype(bool)]
    if c.empty:
        pytest.skip("no contactable rows")
    assert c["email"].notna().all(), "contactable row without a public email"
    assert (~c["opt_out"].fillna(False).astype(bool)).all(), \
        "contactable row that is opted out"


def test_full_table_no_opted_out_row_is_contactable():
    df = _read(FULL)
    if "opt_out" not in df.columns:
        pytest.skip("no opt_out column")
    opted = df[df["opt_out"].fillna(False).astype(bool)]
    if opted.empty:
        return  # vacuously fine
    assert (~opted["contactable"].fillna(False).astype(bool)).all()


def test_full_table_no_fabricated_pattern_emails():
    """No email may look like a fabricated firstname.lastname@inst guess that
    is NOT backed by a public source. (All emails are already source-checked;
    this asserts the property explicitly for the brief's requirement.)"""
    df = _read(FULL)
    if "email" not in df.columns:
        pytest.skip("no email column")
    em = df[df["email"].notna()]
    if em.empty:
        pytest.skip("no emails")
    # the ONLY way an email exists is via coerce_user, which requires a public
    # source -> assert that invariant holds in the persisted data too.
    bad = em[~em["email_source"].isin(ALLOWED_EMAIL_SOURCES)]
    assert bad.empty, f"{len(bad)} emails without a public source (fabrication risk)"
