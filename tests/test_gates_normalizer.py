"""Unit tests for the Gates Foundation normalizer (no network: CSV fixture)."""

from pathlib import Path

from atlas.connectors.gates import (
    GatesConnector,
    _parse_amount,
    _parse_dates,
    parse_csv_text,
)
from atlas.schema import coerce

FIXTURE = Path(__file__).parent / "fixtures" / "gates" / "sample.csv"


def _normalize(tmp_path):
    conn = GatesConnector(
        raw_dir=tmp_path / "raw", processed_dir=tmp_path / "proc", resolve_ror=False
    )
    text = FIXTURE.read_text(encoding="utf-8")
    return list(conn.normalize([{"csv": text}]))


def test_parse_csv_skips_banner_line():
    rows = list(parse_csv_text(FIXTURE.read_text(encoding="utf-8")))
    assert len(rows) == 3
    assert rows[0]["GRANT ID"] == "INV-003934"
    assert rows[0]["GRANTEE"] == "Smithsonian Institution"


def test_parse_amount():
    assert _parse_amount("1,500,000") == 1500000.0
    assert _parse_amount("$250000") == 250000.0
    assert _parse_amount("") is None
    assert _parse_amount("0") is None


def test_parse_dates_adds_duration():
    # 2021-02 + 59 months -> ends 2026-01-01
    start, end = _parse_dates("2021-02", "59")
    assert start == "2021-02-01"
    assert end == "2026-01-01"


def test_parse_dates_no_duration_leaves_end_none():
    start, end = _parse_dates("2020-06", "")
    assert start == "2020-06-01"
    assert end is None


def test_normalizer_emits_each_entity_type(tmp_path):
    rows = _normalize(tmp_path)
    tables = {r.table for r in rows}
    assert {"funder", "grant", "organization", "field"} <= tables
    assert {"funder_grant", "grant_org"} <= tables


def test_funder_emitted_once(tmp_path):
    rows = _normalize(tmp_path)
    funders = [r for r in rows if r.table == "funder"]
    assert len(funders) == 1
    assert funders[0].data["short_name"] == "Gates Foundation"
    assert funders[0].data["funder_type"] == "nonprofit"
    assert funders[0].data["ror_id"] == "https://ror.org/0456r8d26"


def test_grant_money_is_usd_with_identity_fx(tmp_path):
    rows = _normalize(tmp_path)
    g = next(r.data for r in rows
             if r.table == "grant" and r.data["source_id"] == "INV-003934")
    assert g["amount_original"] == 1500000.0
    assert g["amount_usd"] == 1500000.0
    assert g["currency"] == "USD"
    assert g["fx_rate_to_usd"] == 1.0
    assert g["fx_as_of"] == "2026-06-01"


def test_grant_unknown_amount_is_none(tmp_path):
    # money invariant: unknown amount -> None, never 0
    rows = _normalize(tmp_path)
    g = next(r.data for r in rows
             if r.table == "grant" and r.data["source_id"] == "INV-099999")
    assert g["amount_original"] is None
    assert g["amount_usd"] is None
    assert g["currency"] is None
    assert g["fx_rate_to_usd"] is None


def test_topic_becomes_field(tmp_path):
    rows = _normalize(tmp_path)
    names = {r.data["name"] for r in rows if r.table == "field"}
    assert "Polio" in names
    assert "Community Engagement Grantmaking" in names


def test_org_carries_city_and_country(tmp_path):
    rows = _normalize(tmp_path)
    smithsonian = next(
        r.data for r in rows
        if r.table == "organization" and r.data["name"] == "Smithsonian Institution"
    )
    assert smithsonian["country_code"] == "US"
    assert smithsonian["city"] == "Washington"


def test_all_rows_pass_coerce(tmp_path):
    rows = _normalize(tmp_path)
    for r in rows:
        coerce(r.table, r.data)
