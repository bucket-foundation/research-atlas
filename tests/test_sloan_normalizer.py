"""Unit tests for the Sloan Foundation normalizer (no network: HTML listing fixture)."""

from pathlib import Path

from atlas.connectors.sloan import (
    SloanConnector,
    _split_city_state,
    parse_listing_html,
)
from atlas.schema import coerce

FIXTURE = Path(__file__).parent / "fixtures" / "sloan" / "listing.html"


def _html():
    return FIXTURE.read_text(encoding="utf-8")


def _normalize(tmp_path):
    conn = SloanConnector(
        raw_dir=tmp_path / "raw", processed_dir=tmp_path / "proc", resolve_ror=False
    )
    return list(conn.normalize([{"html": _html()}]))


def test_parse_listing_extracts_cards():
    cards = parse_listing_html(_html())
    assert len(cards) == 3
    c0 = cards[0]
    assert c0["grantee"]
    assert c0["amount"] and c0["amount"] > 0
    assert c0["year"] and 2000 <= c0["year"] <= 2030
    assert c0["id"].startswith("g-")


def test_split_city_state():
    assert _split_city_state("Brooklyn, NY") == ("Brooklyn", "NY")
    assert _split_city_state("London") == ("London", None)
    assert _split_city_state(None) == (None, None)


def test_normalizer_emits_each_entity_type(tmp_path):
    rows = _normalize(tmp_path)
    tables = {r.table for r in rows}
    assert {"funder", "grant", "organization", "person", "field"} <= tables
    assert {"funder_grant", "grant_org", "grant_person", "person_org"} <= tables


def test_funder_emitted_once(tmp_path):
    rows = _normalize(tmp_path)
    funders = [r for r in rows if r.table == "funder"]
    assert len(funders) == 1
    assert funders[0].data["short_name"] == "Sloan Foundation"
    assert funders[0].data["country_code"] == "US"
    assert funders[0].data["ror_id"] == "https://ror.org/052csg198"


def test_amount_is_usd_with_identity_fx(tmp_path):
    rows = _normalize(tmp_path)
    g = next(r.data for r in rows if r.table == "grant" and r.data["amount_usd"])
    assert g["currency"] == "USD"
    assert g["fx_rate_to_usd"] == 1.0
    assert g["amount_usd"] == g["amount_original"]


def test_year_becomes_jan1_start(tmp_path):
    rows = _normalize(tmp_path)
    g = next(r.data for r in rows if r.table == "grant" and r.data["start_date"])
    assert g["start_date"].endswith("-01-01")
    assert g["end_date"] is None  # Sloan publishes only a year


def test_investigator_is_pi(tmp_path):
    rows = _normalize(tmp_path)
    gp = [r for r in rows if r.table == "grant_person"]
    assert gp
    assert all(r.data["role"] == "pi" for r in gp)


def test_program_becomes_field(tmp_path):
    rows = _normalize(tmp_path)
    field_names = {r.data["name"] for r in rows if r.table == "field"}
    assert any("Research" in n or "Higher Education" in n for n in field_names)


def test_all_rows_pass_coerce(tmp_path):
    rows = _normalize(tmp_path)
    for r in rows:
        coerce(r.table, r.data)
