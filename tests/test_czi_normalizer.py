"""Unit tests for the CZI normalizer (no network: JSON document fixture)."""

import json
from pathlib import Path

from atlas.connectors.czi import CziConnector, _flatten, _money, _year
from atlas.schema import coerce

FIXTURE = Path(__file__).parent / "fixtures" / "czi" / "grants.json"


def _payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _normalize(tmp_path):
    conn = CziConnector(
        raw_dir=tmp_path / "raw", processed_dir=tmp_path / "proc", resolve_ror=False
    )
    return list(conn.normalize([_payload()]))


def test_flatten_unwraps_nested_list():
    recs = _flatten(_payload())
    assert len(recs) == 3
    assert all("fields" in r for r in recs)


def test_flatten_tolerates_flat_list():
    # a future shape change to a single flat list must not drop records
    recs = _flatten({"grants": [{"id": "x", "fields": {"Amount": 1}}]})
    assert len(recs) == 1


def test_money_invariant():
    assert _money(0) is None
    assert _money("0") is None
    assert _money(None) is None
    assert _money(200000) == 200000.0
    assert _money(-5) is None


def test_year_parsing():
    assert _year("2022") == 2022
    assert _year("") is None
    assert _year(None) is None


def test_normalizer_emits_each_entity_type(tmp_path):
    rows = _normalize(tmp_path)
    tables = {r.table for r in rows}
    assert {"funder", "grant", "organization", "field"} <= tables
    assert {"funder_grant", "grant_org"} <= tables


def test_funder_emitted_once(tmp_path):
    rows = _normalize(tmp_path)
    funders = [r for r in rows if r.table == "funder"]
    assert len(funders) == 1
    assert funders[0].data["short_name"] == "CZI"
    assert funders[0].data["country_code"] == "US"
    assert funders[0].data["ror_id"] == "https://ror.org/02qenvm24"
    assert funders[0].data["crossref_funder_id"] == "100014989"


def test_amount_is_usd_with_identity_fx(tmp_path):
    rows = _normalize(tmp_path)
    g = next(r.data for r in rows if r.table == "grant" and r.data["amount_usd"])
    assert g["currency"] == "USD"
    assert g["fx_rate_to_usd"] == 1.0
    assert g["amount_usd"] == g["amount_original"]


def test_zero_amount_becomes_null_not_zero(tmp_path):
    rows = _normalize(tmp_path)
    # rec2 has Amount 0 -> must be None (money invariant), still a valid grant
    grants = [r.data for r in rows if r.table == "grant"]
    zero_grant = next(g for g in grants if g["source_id"] == "rec2")
    assert zero_grant["amount_usd"] is None
    assert zero_grant["currency"] is None


def test_year_becomes_jan1_start(tmp_path):
    rows = _normalize(tmp_path)
    g = next(r.data for r in rows if r.table == "grant" and r.data["start_date"])
    assert g["start_date"].endswith("-01-01")
    assert g["end_date"] is None  # CZI publishes only a year


def test_initiative_becomes_field(tmp_path):
    rows = _normalize(tmp_path)
    field_names = {r.data["name"] for r in rows if r.table == "field"}
    assert {"Science", "Education", "Community"} <= field_names


def test_org_deduped_across_grants(tmp_path):
    rows = _normalize(tmp_path)
    # rec1 + rec3 share "Example University" -> one org node, two grant_org edges
    orgs = [r for r in rows if r.table == "organization"]
    names = [o.data["name"] for o in orgs]
    assert names.count("Example University") == 1
    edges = [r for r in rows if r.table == "grant_org"]
    assert len(edges) == 3


def test_all_rows_pass_coerce(tmp_path):
    rows = _normalize(tmp_path)
    for r in rows:
        coerce(r.table, r.data)
