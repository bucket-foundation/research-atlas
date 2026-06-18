"""Unit tests for the UKRI/GtR normalizer (no network: ROR + persons disabled)."""

from atlas.connectors.ukri import (
    UkriConnector,
    _ms_to_iso,
    _parse_money,
    _to_usd,
    GBP_TO_USD,
)
from atlas.schema import coerce

# A representative GtR projects page (trimmed real shape, v7 JSON).
SAMPLE_PAGE = {
    "totalPages": 1,
    "project": [
        {
            "id": "ABC-123",
            "identifiers": {"identifier": [{"value": "EP/X000001/1", "type": "RCUK"}]},
            "title": "Quantum biophysics of photosynthesis",
            "abstractText": "We study coherent energy transfer...",
            "status": "Active",
            "grantCategory": "Research Grant",
            "leadFunder": "EPSRC",
            "start": 1735689600000,   # 2025-01-01
            "end": 2051222400000,     # 2035-01-01 (future -> active)
            "researchTopics": {"researchTopic": [
                {"id": "T1", "text": "Biophysics"},
                {"id": "T2", "text": "Unclassified"},
            ]},
            "participantValues": {"participant": [
                {"organisationName": "University of Cambridge", "role": "LEAD_PARTICIPANT",
                 "projectCost": 1000000.0, "grantOffer": 800000.0},
                {"organisationName": "University of Oxford", "role": "PARTICIPANT",
                 "projectCost": 200000.0, "grantOffer": 150000.0},
            ]},
            "links": {"link": [
                {"rel": "PI_PER", "href": "http://gtr.ukri.org/gtr/api/persons/P1"},
            ]},
        },
        {
            "id": "DEF-456",
            "identifiers": {"identifier": [{"value": "BB/Y000002/1", "type": "RCUK"}]},
            "title": "Grant with unknown money",
            "status": "Closed",
            "leadFunder": "BBSRC",
            "start": 1262304000000,   # 2010-01-01
            "end": 1325289600000,     # 2011-12-31 (past -> completed)
            "researchTopics": {"researchTopic": []},
            "participantValues": {"participant": [
                {"organisationName": "Some Institute", "role": "LEAD_PARTICIPANT",
                 "projectCost": 0, "grantOffer": 0},
            ]},
            "links": {"link": []},
        },
    ],
}


def _normalize_sample(tmp_path):
    conn = UkriConnector(
        raw_dir=tmp_path / "raw", processed_dir=tmp_path / "proc",
        resolve_ror=False, resolve_persons=False,
    )
    return list(conn.normalize([SAMPLE_PAGE]))


def test_ms_to_iso():
    assert _ms_to_iso(1735689600000) == "2025-01-01"
    assert _ms_to_iso("") is None
    assert _ms_to_iso(None) is None


def test_parse_money_unknown_is_none_not_zero():
    assert _parse_money(800000.0) == 800000.0
    assert _parse_money(0) is None
    assert _parse_money("0") is None
    assert _parse_money("") is None
    assert _parse_money(None) is None


def test_to_usd_uses_fixed_fx():
    assert _to_usd(1000.0) == round(1000.0 * GBP_TO_USD, 2)
    assert _to_usd(None) is None


def test_normalizer_emits_each_entity_type(tmp_path):
    rows = _normalize_sample(tmp_path)
    tables = {r.table for r in rows}
    assert {"funder", "grant", "organization", "field"} <= tables
    assert {"funder_grant", "grant_org"} <= tables


def test_umbrella_and_council_funders(tmp_path):
    rows = _normalize_sample(tmp_path)
    funders = {r.data["short_name"] for r in rows if r.table == "funder"}
    # UKRI umbrella + the two lead councils
    assert "UKRI" in funders
    assert "EPSRC" in funders
    assert "BBSRC" in funders


def test_grant_money_gbp_normalized_to_usd(tmp_path):
    rows = _normalize_sample(tmp_path)
    grants = {r.data["source_id"]: r.data for r in rows if r.table == "grant"}
    g = grants["ABC-123"]
    # sum of grant offers: 800000 + 150000 = 950000 GBP
    assert g["amount_original"] == 950000.0
    assert g["currency"] == "GBP"
    assert g["amount_usd"] == round(950000.0 * GBP_TO_USD, 2)
    assert g["fx_rate_to_usd"] == GBP_TO_USD
    assert g["fx_as_of"] is not None
    assert g["status"] == "active"


def test_grant_unknown_money_is_none(tmp_path):
    rows = _normalize_sample(tmp_path)
    grants = {r.data["source_id"]: r.data for r in rows if r.table == "grant"}
    g = grants["DEF-456"]
    assert g["amount_original"] is None
    assert g["amount_usd"] is None
    assert g["currency"] is None
    assert g["status"] == "completed"


def test_unclassified_field_skipped(tmp_path):
    rows = _normalize_sample(tmp_path)
    field_names = {r.data["name"] for r in rows if r.table == "field"}
    assert "Biophysics" in field_names
    assert "Unclassified" not in field_names


def test_lead_org_is_recipient(tmp_path):
    rows = _normalize_sample(tmp_path)
    grant = next(r for r in rows if r.table == "grant"
                 and r.data["source_id"] == "ABC-123")
    gid = grant.data["atlas_id"]
    go = [r for r in rows if r.table == "grant_org" and r.data["src_id"] == gid]
    roles = {r.data["role"] for r in go}
    assert "recipient" in roles
    assert "host" in roles  # the participant


def test_all_rows_pass_coerce(tmp_path):
    rows = _normalize_sample(tmp_path)
    for r in rows:
        coerce(r.table, r.data)
