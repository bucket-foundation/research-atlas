"""Unit tests for the NIH RePORTER normalizer (no network)."""

from atlas.connectors.nih import NihConnector, _money, _date
from atlas.schema import coerce

SAMPLE_PAGE = {
    "meta": {"total": 2},
    "results": [
        {
            "project_num": "5R01CA123456-03",
            "project_title": "Cancer immunotherapy mechanisms",
            "fiscal_year": 2024,
            "award_amount": 512345,
            "project_start_date": "2022-04-01T00:00:00Z",
            "project_end_date": "2027-03-31T00:00:00Z",
            "organization": {
                "org_name": "DANA-FARBER CANCER INST",
                "org_city": "BOSTON", "org_state": "MA",
                "org_country": "UNITED STATES",
            },
            "principal_investigators": [
                {"profile_id": 111, "first_name": "Jane", "last_name": "Doe",
                 "full_name": "Jane Doe", "is_contact_pi": True},
                {"profile_id": 222, "first_name": "John", "last_name": "Roe",
                 "full_name": "John Roe", "is_contact_pi": False},
            ],
            "agency_ic_admin": {"abbreviation": "NCI",
                                "name": "National Cancer Institute"},
        },
        {
            "project_num": "5F32DK999999-01",
            "project_title": "Fellowship with unknown amount",
            "fiscal_year": 2024,
            "award_amount": 0,
            "project_start_date": "2024-01-01",
            "project_end_date": None,
            "organization": {"org_name": "SOME UNIV", "org_country": "UNITED STATES"},
            "principal_investigators": [],
            "agency_ic_admin": {"abbreviation": "NIDDK",
                                "name": "National Institute of Diabetes..."},
        },
    ],
}


def _rows(tmp_path):
    conn = NihConnector(
        raw_dir=tmp_path / "raw", processed_dir=tmp_path / "proc", resolve_ror=False
    )
    return list(conn.normalize([SAMPLE_PAGE]))


def test_money_zero_is_none():
    assert _money(512345) == 512345.0
    assert _money(0) is None
    assert _money("") is None
    assert _money(None) is None


def test_date_trims():
    assert _date("2022-04-01T00:00:00Z") == "2022-04-01"
    assert _date(None) is None


def test_nih_umbrella_and_ic_funders(tmp_path):
    rows = _rows(tmp_path)
    shorts = {r.data["short_name"] for r in rows if r.table == "funder"}
    assert "NIH" in shorts
    assert "NCI" in shorts and "NIDDK" in shorts


def test_grant_money_usd(tmp_path):
    rows = _rows(tmp_path)
    g = next(r.data for r in rows if r.table == "grant"
             and r.data["source_id"] == "5R01CA123456-03")
    assert g["amount_usd"] == 512345.0
    assert g["currency"] == "USD"
    assert g["fx_rate_to_usd"] == 1.0


def test_grant_zero_amount_is_none(tmp_path):
    rows = _rows(tmp_path)
    g = next(r.data for r in rows if r.table == "grant"
             and r.data["source_id"] == "5F32DK999999-01")
    assert g["amount_original"] is None
    assert g["amount_usd"] is None


def test_contact_pi_role(tmp_path):
    rows = _rows(tmp_path)
    gid = next(r.data["atlas_id"] for r in rows if r.table == "grant"
               and r.data["source_id"] == "5R01CA123456-03")
    roles = {(r.data["dst_id"], r.data["role"]) for r in rows
             if r.table == "grant_person" and r.data["src_id"] == gid}
    assert any(role == "pi" for _, role in roles)
    assert any(role == "co-pi" for _, role in roles)


def test_person_keyed_on_profile(tmp_path):
    rows = _rows(tmp_path)
    persons = [r.data for r in rows if r.table == "person"]
    # two distinct PIs from project 1
    assert len([p for p in persons if p["full_name"] in ("Jane Doe", "John Roe")]) == 2


def test_all_rows_pass_coerce(tmp_path):
    for r in _rows(tmp_path):
        coerce(r.table, r.data)
