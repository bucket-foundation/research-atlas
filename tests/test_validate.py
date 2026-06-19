"""Unit tests for the validation suite (fixture-based, no network).

Builds tiny processed parquet sets in a tmp dir -- one clean, several with a
single deliberate violation each -- and asserts the suite passes the clean one
and flags exactly the broken invariant in each dirty one.
"""

from __future__ import annotations

import pandas as pd
import pytest

from scripts import validate
from atlas.schema import now_iso

TS = now_iso()


def _write(d, table, rows):
    pd.DataFrame(rows).to_parquet(d / f"{table}.parquet", index=False)


def _clean(d):
    """A minimal but internally-consistent graph."""
    _write(d, "funder", [{"atlas_id": "fund:1", "name": "NSF", "short_name": "NSF",
                          "country_code": "US", "funder_type": "government",
                          "ror_id": "https://ror.org/021nxhr62",
                          "crossref_funder_id": None, "homepage": None,
                          "source": "nsf", "source_id": "nsf",
                          "source_url": "x", "as_of": TS}])
    _write(d, "grant", [{"atlas_id": "grant:1", "title": "G", "abstract": None,
                         "amount_original": 100.0, "currency": "USD",
                         "amount_usd": 100.0, "fx_rate_to_usd": 1.0,
                         "fx_as_of": "2020-01-01", "start_date": "2020-01-01",
                         "end_date": "2021-01-01", "status": "completed",
                         "program": None, "source": "nsf", "source_id": "1",
                         "source_url": "x", "as_of": TS}])
    _write(d, "organization", [{"atlas_id": "org:1", "name": "Stanford",
                                "ror_id": "https://ror.org/00f54p054",
                                "country_code": "US", "city": None, "region": None,
                                "org_type": "education", "homepage": None,
                                "lat": None, "lon": None, "source": "nsf",
                                "source_id": "x", "source_url": "x", "as_of": TS}])
    _write(d, "funder_grant", [{"src_id": "fund:1", "dst_id": "grant:1",
                                "role": "awarder", "source": "nsf",
                                "source_id": "1", "source_url": "x", "as_of": TS}])
    _write(d, "grant_org", [{"src_id": "grant:1", "dst_id": "org:1",
                             "role": "recipient", "source": "nsf",
                             "source_id": "1", "source_url": "x", "as_of": TS}])


def test_clean_graph_passes(tmp_path):
    _clean(tmp_path)
    s = validate.run(tmp_path)
    assert not s.hard_failures, [r.name for r in s.hard_failures]


def test_orphan_edge_is_caught(tmp_path):
    _clean(tmp_path)
    # point a grant_org edge at a non-existent org
    _write(tmp_path, "grant_org", [{"src_id": "grant:1", "dst_id": "org:GHOST",
                                    "role": "recipient", "source": "nsf",
                                    "source_id": "1", "source_url": "x",
                                    "as_of": TS}])
    s = validate.run(tmp_path)
    names = {r.name for r in s.hard_failures}
    assert any("orphan grant_org.dst_id" in n for n in names)


def test_zero_amount_is_caught(tmp_path):
    _clean(tmp_path)
    g = pd.read_parquet(tmp_path / "grant.parquet")
    g.loc[0, "amount_usd"] = 0.0
    g.to_parquet(tmp_path / "grant.parquet", index=False)
    s = validate.run(tmp_path)
    assert any("amount_usd" in r.name for r in s.hard_failures)


def test_duplicate_ror_org_is_caught(tmp_path):
    _clean(tmp_path)
    o = pd.read_parquet(tmp_path / "organization.parquet")
    dup = o.iloc[0].copy()
    dup["atlas_id"] = "org:2"  # different node, same ROR id -> not deduped
    o = pd.concat([o, pd.DataFrame([dup])], ignore_index=True)
    o.to_parquet(tmp_path / "organization.parquet", index=False)
    s = validate.run(tmp_path)
    names = {r.name for r in s.hard_failures}
    assert "one canonical org per ROR id" in names
    assert "organization.atlas_id unique" not in names  # ids still distinct


def test_malformed_ror_is_caught(tmp_path):
    _clean(tmp_path)
    o = pd.read_parquet(tmp_path / "organization.parquet")
    o.loc[0, "ror_id"] = "ror.org/not-a-url"
    o.to_parquet(tmp_path / "organization.parquet", index=False)
    s = validate.run(tmp_path)
    assert any("ROR ids well-formed" in r.name for r in s.hard_failures)


def test_missing_provenance_is_caught(tmp_path):
    _clean(tmp_path)
    g = pd.read_parquet(tmp_path / "grant.parquet")
    g.loc[0, "source"] = None
    g.to_parquet(tmp_path / "grant.parquet", index=False)
    s = validate.run(tmp_path)
    assert any("provenance" in r.name for r in s.hard_failures)


def test_unfunded_grant_is_caught(tmp_path):
    _clean(tmp_path)
    # remove the only awarder edge -> grant:1 becomes an orphan grant
    _write(tmp_path, "funder_grant", [{"src_id": "fund:1", "dst_id": "grant:GHOST",
                                       "role": "awarder", "source": "nsf",
                                       "source_id": "1", "source_url": "x",
                                       "as_of": TS}])
    s = validate.run(tmp_path)
    names = {r.name for r in s.hard_failures}
    # the missing-awarder check fires (the ghost endpoint also trips orphan check)
    assert "every grant has an awarder" in names


def test_wrong_fx_arithmetic_is_caught(tmp_path):
    _clean(tmp_path)
    g = pd.read_parquet(tmp_path / "grant.parquet")
    # amount_usd no longer equals amount_original * fx_rate_to_usd
    g.loc[0, "amount_usd"] = 999.0  # 100 * 1.0 != 999
    g.to_parquet(tmp_path / "grant.parquet", index=False)
    s = validate.run(tmp_path)
    names = {r.name for r in s.hard_failures}
    assert "grant amount_usd = amount_original * fx" in names


def test_money_without_fx_date_is_caught(tmp_path):
    _clean(tmp_path)
    g = pd.read_parquet(tmp_path / "grant.parquet")
    g.loc[0, "fx_as_of"] = None  # has amount_usd but no fx date
    g.to_parquet(tmp_path / "grant.parquet", index=False)
    s = validate.run(tmp_path)
    names = {r.name for r in s.hard_failures}
    assert "money rows carry an FX date" in names


def test_report_written(tmp_path):
    _clean(tmp_path)
    s = validate.run(tmp_path)
    report = tmp_path / "VALIDATION.md"
    validate.write_report(s, report)
    text = report.read_text()
    assert "Validation Report" in text and "Status:" in text
