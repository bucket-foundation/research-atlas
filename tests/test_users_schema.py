"""Compliance + schema tests for the researcher/users layer.

The non-negotiable invariant: **the pipeline never emits a fabricated email.**
An email is only ever stored if it came from a known public source AND carries
provenance (source + url + as_of). Anything else is rejected by ``coerce_user``.
We also test opt_out is respected and the contactable flag is sound.
"""

import pytest

from atlas.users.schema import (
    coerce_user,
    is_plausible_email,
    field_to_slug,
    ALLOWED_EMAIL_SOURCES,
    USER_COLUMNS,
    PII_COLUMNS,
)


def _base(**over):
    row = {
        "atlas_id": "person:abc123",
        "full_name": "Ada Lovelace",
        "orcid": "0000-0000-0000-0001",
        "primary_field": "Mathematics",
        "field_slug": "math",
        "works_count": 3,
        "first_year": 2018,
        "last_year": 2025,
        "seniority": "rising-star",
        "activity_tier": "active",
        "source": "atlas-users",
        "as_of": "2026-06-21T00:00:00Z",
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# The headline test: no fabricated emails ever leave coerce_user.
# --------------------------------------------------------------------------- #

def test_email_requires_public_source():
    """An email without a known public source is rejected (anti-fabrication)."""
    with pytest.raises(ValueError):
        coerce_user(_base(email="ada@example.com", email_source="guessed",
                          email_source_url="x", email_as_of="2026-01-01"))


def test_email_requires_source_url_provenance():
    with pytest.raises(ValueError):
        coerce_user(_base(email="ada@example.com", email_source="europepmc",
                          email_as_of="2026-01-01"))  # no url


def test_email_requires_as_of_provenance():
    with pytest.raises(ValueError):
        coerce_user(_base(email="ada@example.com", email_source="orcid",
                          email_source_url="https://orcid.org/x"))  # no as_of


def test_email_must_be_valid_syntax():
    with pytest.raises(ValueError):
        coerce_user(_base(email="not-an-email", email_source="europepmc",
                          email_source_url="x", email_as_of="2026-01-01"))


def test_valid_public_email_passes_with_full_provenance():
    row = coerce_user(_base(
        email="ada@uni.edu",
        email_source="europepmc",
        email_source_url="https://europepmc.org/article/MED/123",
        email_as_of="2026-06-21T00:00:00Z",
        email_method="corresponding-author-metadata",
        contactable=True,
    ))
    assert row["email"] == "ada@uni.edu"
    assert row["email_source"] in ALLOWED_EMAIL_SOURCES
    assert row["email_source_url"]
    assert row["email_as_of"]
    assert row["contactable"] is True


def test_no_email_means_not_contactable_and_null_provenance():
    row = coerce_user(_base(email=None, contactable=True))  # claim ignored
    assert row["email"] is None
    assert row["email_source"] is None
    assert row["email_source_url"] is None
    assert row["email_as_of"] is None
    assert row["contactable"] is False


# --------------------------------------------------------------------------- #
# opt_out respected
# --------------------------------------------------------------------------- #

def test_opt_out_forces_not_contactable_and_status():
    row = coerce_user(_base(
        email="ada@uni.edu", email_source="orcid",
        email_source_url="https://orcid.org/x", email_as_of="2026-06-21T00:00:00Z",
        opt_out=True, contactable=True))
    assert row["opt_out"] is True
    assert row["contactable"] is False
    assert row["engagement_status"] == "opted-out"


def test_opt_out_default_false():
    assert coerce_user(_base())["opt_out"] is False


def test_default_engagement_status_not_contacted():
    assert coerce_user(_base())["engagement_status"] == "not-contacted"


def test_unknown_engagement_status_rejected():
    with pytest.raises(ValueError):
        coerce_user(_base(engagement_status="totally-made-up"))


# --------------------------------------------------------------------------- #
# schema hygiene
# --------------------------------------------------------------------------- #

def test_unknown_column_rejected():
    with pytest.raises(ValueError):
        coerce_user(_base(nonexistent_col="x"))


def test_coerce_fills_all_canonical_columns():
    row = coerce_user(_base())
    assert set(row) == set(USER_COLUMNS)


def test_pii_columns_are_a_subset_of_schema():
    assert set(PII_COLUMNS) <= set(USER_COLUMNS)
    assert "email" in PII_COLUMNS


def test_every_allowed_email_source_actually_validates():
    for src in ALLOWED_EMAIL_SOURCES:
        row = coerce_user(_base(
            email="x@uni.edu", email_source=src,
            email_source_url="https://example.org/x",
            email_as_of="2026-06-21T00:00:00Z"))
        assert row["email"] == "x@uni.edu"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def test_is_plausible_email():
    assert is_plausible_email("a@b.edu")
    assert not is_plausible_email("a@b")
    assert not is_plausible_email("firstname.lastname@")
    assert not is_plausible_email(None)
    assert not is_plausible_email("")


def test_field_to_slug_known_and_unknown():
    assert field_to_slug("Mathematics") == "math"
    assert field_to_slug("Physics and Astronomy") == "physics-astro"
    assert field_to_slug("Neuroscience") == "biomed-bio"
    assert field_to_slug("Something Unmapped") == "other"
    assert field_to_slug(None) == "other"
