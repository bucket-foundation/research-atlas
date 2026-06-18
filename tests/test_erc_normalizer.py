"""Unit tests for the ERC/CORDIS normalizer (no network: ROR disabled)."""

from atlas.connectors.erc import (
    ErcConnector,
    _is_erc,
    _parse_money,
    _to_usd,
    EUR_TO_USD,
)
from atlas.schema import coerce

# A representative CORDIS record as the fetch() join emits it.
SAMPLE_RECORD = {
    "project": {
        "id": "945733",
        "title": "Coherence in biological systems",
        "objective": "This project investigates...",
        "startDate": "2021-01-01",
        "endDate": "2026-12-31",
        "totalCost": "1500000",
        "ecMaxContribution": "1499000.50",
        "status": "SIGNED",
        "fundingScheme": "ERC-STG",
    },
    "organizations": [
        {"name": "HELSINGIN YLIOPISTO", "country": "FI", "city": "HELSINKI",
         "role": "coordinator", "activityType": "HES",
         "geolocation": "60.1721612,24.947271",
         "organizationURL": "http://www.helsinki.fi/"},
        {"name": "SOME PARTNER GMBH", "country": "DE", "city": "BERLIN",
         "role": "participant", "activityType": "PRC", "geolocation": ""},
    ],
    "fields": [
        {"euroSciVocCode": "/21/35/159/653", "euroSciVocTitle": "pharmaceutical drugs"},
        {"euroSciVocCode": "/21/39/179/681", "euroSciVocTitle": "diabetes"},
    ],
}

SAMPLE_RECORD_UNKNOWN_MONEY = {
    "project": {
        "id": "999000",
        "title": "ERC grant with no money published",
        "objective": "",
        "startDate": "2018-01-01",
        "endDate": "2020-12-31",
        "totalCost": "0",
        "ecMaxContribution": "",
        "status": "CLOSED",
        "fundingScheme": "HORIZON-ERC",
    },
    "organizations": [
        {"name": "Lone Coordinator", "country": "FR", "role": "coordinator",
         "activityType": "REC", "geolocation": ""},
    ],
    "fields": [],
}


def _normalize(records, tmp_path):
    conn = ErcConnector(
        raw_dir=tmp_path / "raw", processed_dir=tmp_path / "proc", resolve_ror=False
    )
    return list(conn.normalize(records))


def test_is_erc_scheme():
    assert _is_erc("ERC-STG")
    assert _is_erc("ERC-COG")
    assert _is_erc("ERC-ADG")
    assert _is_erc("ERC-SyG")
    assert _is_erc("ERC-POC")
    assert _is_erc("HORIZON-ERC")
    assert _is_erc("HORIZON-ERC-SYG")
    assert not _is_erc("HORIZON-RIA")
    assert not _is_erc("HORIZON-TMA-MSCA-PF-EF")
    assert not _is_erc("")


def test_parse_money_unknown_is_none_not_zero():
    assert _parse_money("1499000.50") == 1499000.50
    assert _parse_money("0") is None
    assert _parse_money("") is None
    assert _parse_money(None) is None


def test_to_usd_uses_fixed_fx():
    assert _to_usd(1000.0) == round(1000.0 * EUR_TO_USD, 2)
    assert _to_usd(None) is None


def test_normalizer_emits_each_entity_type(tmp_path):
    rows = _normalize([SAMPLE_RECORD], tmp_path)
    tables = {r.table for r in rows}
    assert {"funder", "grant", "organization", "field"} <= tables
    assert {"funder_grant", "grant_org"} <= tables


def test_funder_emitted_once_and_supranational(tmp_path):
    rows = _normalize([SAMPLE_RECORD, SAMPLE_RECORD_UNKNOWN_MONEY], tmp_path)
    funders = [r for r in rows if r.table == "funder"]
    assert len(funders) == 1
    assert funders[0].data["short_name"] == "ERC"
    assert funders[0].data["funder_type"] == "supranational"
    assert funders[0].data["country_code"] is None


def test_grant_money_eur_normalized_to_usd(tmp_path):
    rows = _normalize([SAMPLE_RECORD], tmp_path)
    g = next(r.data for r in rows if r.table == "grant"
             and r.data["source_id"] == "945733")
    assert g["amount_original"] == 1499000.50
    assert g["currency"] == "EUR"
    assert g["amount_usd"] == round(1499000.50 * EUR_TO_USD, 2)
    assert g["fx_rate_to_usd"] == EUR_TO_USD
    assert g["status"] == "active"  # SIGNED


def test_grant_unknown_money_is_none(tmp_path):
    rows = _normalize([SAMPLE_RECORD_UNKNOWN_MONEY], tmp_path)
    g = next(r.data for r in rows if r.table == "grant")
    assert g["amount_original"] is None
    assert g["amount_usd"] is None
    assert g["currency"] is None
    assert g["status"] == "completed"  # CLOSED


def test_coordinator_is_recipient(tmp_path):
    rows = _normalize([SAMPLE_RECORD], tmp_path)
    grant = next(r for r in rows if r.table == "grant")
    gid = grant.data["atlas_id"]
    go = {r.data["role"] for r in rows if r.table == "grant_org"
          and r.data["src_id"] == gid}
    assert "recipient" in go
    assert "host" in go


def test_euroscivoc_fields(tmp_path):
    rows = _normalize([SAMPLE_RECORD], tmp_path)
    names = {r.data["name"] for r in rows if r.table == "field"}
    assert "pharmaceutical drugs" in names
    assert "diabetes" in names


def test_geo_parsed(tmp_path):
    rows = _normalize([SAMPLE_RECORD], tmp_path)
    org = next(r.data for r in rows if r.table == "organization"
               and r.data["name"] == "HELSINGIN YLIOPISTO")
    assert org["lat"] == 60.1721612
    assert org["lon"] == 24.947271


def test_all_rows_pass_coerce(tmp_path):
    rows = _normalize([SAMPLE_RECORD, SAMPLE_RECORD_UNKNOWN_MONEY], tmp_path)
    for r in rows:
        coerce(r.table, r.data)
