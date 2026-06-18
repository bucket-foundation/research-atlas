"""Unit tests for the NSF normalizer (no network: ROR resolution disabled)."""

from atlas.connectors.nsf import NsfConnector, _parse_date, _parse_money
from atlas.schema import coerce

# A representative NSF award page (trimmed real shape).
SAMPLE_PAGE = {
    "response": {
        "award": [
            {
                "id": "2543297",
                "title": "CAREER: Biomolecular condensates in plant stress",
                "abstractText": "Plants must constantly balance growth...",
                "awardeeName": "OHIO STATE UNIVERSITY, THE",
                "awardeeCity": "COLUMBUS",
                "awardeeStateCode": "OH",
                "awardeeCountryCode": "US",
                "pdPIName": "Shuai Huang",
                "piFirstName": "Shuai",
                "piLastName": "Huang",
                "startDate": "09/01/2026",
                "expDate": "08/31/2031",
                "estimatedTotalAmt": "1548896",
                "fundsObligatedAmt": "1330337",
                "fundProgramName": "Cell, Dev, & Physio",
                "orgLongName2": "Division of Molecular and Cellular Biosciences",
            },
            {
                "id": "9999999",
                "title": "Award with unknown money",
                "awardeeName": "Example Institute",
                "awardeeCountryCode": "US",
                "pdPIName": "Jane Roe",
                "startDate": "01/01/2020",
                "expDate": "12/31/2022",
                "estimatedTotalAmt": "",
                "fundsObligatedAmt": "0",
                "fundProgramName": "Some Program",
            },
        ]
    }
}


def _normalize_sample(tmp_path):
    conn = NsfConnector(
        raw_dir=tmp_path / "raw", processed_dir=tmp_path / "proc", resolve_ror=False
    )
    return list(conn.normalize([SAMPLE_PAGE]))


def test_parse_date():
    assert _parse_date("09/01/2026") == "2026-09-01"
    assert _parse_date("") is None
    assert _parse_date("garbage") is None


def test_parse_money_unknown_is_none_not_zero():
    assert _parse_money("1548896") == 1548896.0
    assert _parse_money("0") is None
    assert _parse_money("") is None
    assert _parse_money(None) is None


def test_normalizer_emits_each_entity_type(tmp_path):
    rows = _normalize_sample(tmp_path)
    tables = {r.table for r in rows}
    assert {"funder", "grant", "organization", "person", "field"} <= tables
    assert {"funder_grant", "grant_org", "grant_person", "person_org"} <= tables


def test_funder_emitted_once(tmp_path):
    rows = _normalize_sample(tmp_path)
    funders = [r for r in rows if r.table == "funder"]
    assert len(funders) == 1
    assert funders[0].data["short_name"] == "NSF"


def test_grant_money_normalized_to_usd(tmp_path):
    rows = _normalize_sample(tmp_path)
    grants = {r.data["source_id"]: r.data for r in rows if r.table == "grant"}
    g = grants["2543297"]
    assert g["amount_usd"] == 1548896.0
    assert g["currency"] == "USD"
    assert g["fx_rate_to_usd"] == 1.0
    assert g["status"] == "active"  # ends 2031


def test_grant_unknown_money_is_none(tmp_path):
    rows = _normalize_sample(tmp_path)
    grants = {r.data["source_id"]: r.data for r in rows if r.table == "grant"}
    g = grants["9999999"]
    assert g["amount_usd"] is None
    assert g["currency"] is None
    assert g["status"] == "completed"  # ended 2022


def test_all_rows_pass_coerce(tmp_path):
    # every normalized row must validate against the canonical schema
    rows = _normalize_sample(tmp_path)
    for r in rows:
        coerce(r.table, r.data)


def test_grant_links_to_funder_and_org(tmp_path):
    rows = _normalize_sample(tmp_path)
    grant = next(r for r in rows if r.table == "grant"
                 and r.data["source_id"] == "2543297")
    gid = grant.data["atlas_id"]
    fg = [r for r in rows if r.table == "funder_grant" and r.data["dst_id"] == gid]
    go = [r for r in rows if r.table == "grant_org" and r.data["src_id"] == gid]
    assert fg and fg[0].data["role"] == "awarder"
    assert go and go[0].data["role"] == "recipient"
