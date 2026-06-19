"""Unit tests for the Wellcome Trust normalizer (no network: 360Giving rows fixture).

The fixture is real Wellcome 360Giving rows (first records of the published
XLSX) serialized to JSON, fed via the connector's ``{"rows": [...]}`` test hook
so the XLSX binary path is exercised by the same normalize() code path.
"""

import json
from pathlib import Path

from atlas.connectors.wellcome import (
    GBP_TO_USD,
    WellcomeConnector,
    _parse_amount,
    _split_others,
)
from atlas.schema import coerce

FIXTURE = Path(__file__).parent / "fixtures" / "wellcome" / "sample_rows.json"


def _rows():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _normalize(tmp_path):
    conn = WellcomeConnector(
        raw_dir=tmp_path / "raw", processed_dir=tmp_path / "proc", resolve_ror=False
    )
    return list(conn.normalize([{"rows": _rows()}]))


def test_parse_amount():
    assert _parse_amount(752086) == 752086.0
    assert _parse_amount("200,000") == 200000.0
    assert _parse_amount(0) is None
    assert _parse_amount(None) is None


def test_split_others():
    assert _split_others("A Smith; B Jones") == ["A Smith", "B Jones"]
    assert _split_others(None) == []


def test_normalizer_emits_each_entity_type(tmp_path):
    rows = _normalize(tmp_path)
    tables = {r.table for r in rows}
    assert {"funder", "grant", "organization", "person", "field"} <= tables
    assert {"funder_grant", "grant_org", "grant_person", "person_org"} <= tables


def test_funder_emitted_once(tmp_path):
    rows = _normalize(tmp_path)
    funders = [r for r in rows if r.table == "funder"]
    assert len(funders) == 1
    assert funders[0].data["short_name"] == "Wellcome"
    assert funders[0].data["country_code"] == "GB"
    assert funders[0].data["ror_id"] == "https://ror.org/029chgv08"


def test_gbp_normalized_to_usd_with_stamped_fx(tmp_path):
    rows = _normalize(tmp_path)
    g = next(r.data for r in rows if r.table == "grant")
    assert g["currency"] == "GBP"
    assert g["fx_rate_to_usd"] == GBP_TO_USD
    assert g["amount_usd"] == round(g["amount_original"] * GBP_TO_USD, 2)
    assert g["fx_as_of"] == "2026-06-01"


def test_lead_applicant_is_pi(tmp_path):
    rows = _normalize(tmp_path)
    grant = next(r for r in rows if r.table == "grant")
    gid = grant.data["atlas_id"]
    gp = [r for r in rows if r.table == "grant_person" and r.data["src_id"] == gid]
    roles = {r.data["role"] for r in gp}
    assert "pi" in roles


def test_grant_programme_becomes_field(tmp_path):
    rows = _normalize(tmp_path)
    field_names = {r.data["name"] for r in rows if r.table == "field"}
    assert field_names  # at least one programme classified


def test_recipient_org_has_grant_org_edge(tmp_path):
    rows = _normalize(tmp_path)
    assert any(
        r.table == "grant_org" and r.data["role"] == "recipient" for r in rows
    )


def test_all_rows_pass_coerce(tmp_path):
    rows = _normalize(tmp_path)
    for r in rows:
        coerce(r.table, r.data)
