"""Unit tests for the full CORDIS connector (offline: reads a tiny zip fixture)."""

import csv
import io
import zipfile

from atlas.connectors.cordis import (
    CordisConnector, _is_erc, _parse_money, _to_usd, _org_type, EUR_TO_USD,
)
from atlas.schema import coerce


def _make_zip(tmp_path):
    """Build a 2-project CORDIS-shaped zip (semicolon CSVs)."""
    z = tmp_path / "cordis_test.zip"

    def w(rows):
        buf = io.StringIO()
        wr = csv.writer(buf, delimiter=";")
        for r in rows:
            wr.writerow(r)
        return buf.getvalue()

    projects = [
        ["id", "title", "objective", "startDate", "endDate", "totalCost",
         "ecMaxContribution", "status", "fundingScheme", "frameworkProgramme"],
        ["101", "ERC project", "obj1", "2022-01-01", "2026-12-31", "0",
         "1500000,50", "SIGNED", "HORIZON-ERC", "HORIZON"],
        ["202", "MSCA project", "obj2", "2019-01-01", "2021-12-31", "300000",
         "", "CLOSED", "MSCA-IF", "H2020"],
    ]
    orgs = [
        ["projectID", "name", "country", "city", "nutsCode", "geolocation",
         "organizationURL", "activityType", "role"],
        ["101", "UNIV HELSINKI", "FI", "HELSINKI", "FI1", "60.17,24.94",
         "http://helsinki.fi", "HES", "coordinator"],
        ["101", "PARTNER GMBH", "DE", "BERLIN", "DE3", "", "", "PRC", "participant"],
        ["202", "LONE ORG", "FR", "PARIS", "FR1", "", "", "REC", "coordinator"],
    ]
    scivoc = [
        ["projectID", "euroSciVocCode", "euroSciVocPath", "euroSciVocTitle",
         "euroSciVocDescription"],
        ["101", "/23/49/315", "/natural sciences/biology", "proteins", ""],
    ]
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("project.csv", w(projects))
        zf.writestr("organization.csv", w(orgs))
        zf.writestr("euroSciVoc.csv", w(scivoc))
    return z


def _rows(tmp_path):
    z = _make_zip(tmp_path)
    conn = CordisConnector(
        raw_dir=tmp_path / "raw", processed_dir=tmp_path / "proc", resolve_ror=False
    )
    return list(conn.iter_rows(z))


def test_is_erc():
    assert _is_erc("HORIZON-ERC")
    assert _is_erc("ERC-STG")
    assert not _is_erc("MSCA-IF")


def test_parse_money_comma_decimal():
    assert _parse_money("1500000,50") == 1500000.50
    assert _parse_money("300000") == 300000.0
    assert _parse_money("0") is None
    assert _parse_money("") is None


def test_to_usd():
    assert _to_usd(1000.0) == round(1000.0 * EUR_TO_USD, 2)
    assert _to_usd(None) is None


def test_org_type_mapping():
    assert _org_type("HES") == "education"
    assert _org_type("PRC") == "company"
    assert _org_type("REC") == "facility"
    assert _org_type("ZZZ") == "other"


def test_emits_all_schemes_not_just_erc(tmp_path):
    rows = _rows(tmp_path)
    grant_schemes = {r.data["program"] for r in rows if r.table == "grant"}
    # both ERC and MSCA schemes present -> full feed, not just ERC
    assert any("HORIZON-ERC" in s for s in grant_schemes)
    assert any("MSCA-IF" in s for s in grant_schemes)


def test_ec_and_erc_funders(tmp_path):
    rows = _rows(tmp_path)
    shorts = {r.data["short_name"] for r in rows if r.table == "funder"}
    assert {"EC", "ERC"} <= shorts


def test_erc_grant_has_two_awarders(tmp_path):
    rows = _rows(tmp_path)
    gid = next(r.data["atlas_id"] for r in rows
               if r.table == "grant" and r.data["source_id"] == "101")
    awarders = [r for r in rows if r.table == "funder_grant"
                and r.data["dst_id"] == gid]
    assert len(awarders) == 2  # EC + ERC


def test_non_erc_grant_has_one_awarder(tmp_path):
    rows = _rows(tmp_path)
    gid = next(r.data["atlas_id"] for r in rows
               if r.table == "grant" and r.data["source_id"] == "202")
    awarders = [r for r in rows if r.table == "funder_grant"
                and r.data["dst_id"] == gid]
    assert len(awarders) == 1  # EC only


def test_money_eur_normalized(tmp_path):
    rows = _rows(tmp_path)
    g = next(r.data for r in rows if r.table == "grant"
             and r.data["source_id"] == "101")
    assert g["amount_original"] == 1500000.50
    assert g["currency"] == "EUR"
    assert g["amount_usd"] == round(1500000.50 * EUR_TO_USD, 2)


def test_falls_back_to_total_cost_when_ec_contribution_empty(tmp_path):
    # project 202 has ecMaxContribution="" but totalCost="300000" -> use totalCost
    rows = _rows(tmp_path)
    g = next(r.data for r in rows if r.table == "grant"
             and r.data["source_id"] == "202")
    assert g["amount_original"] == 300000.0
    assert g["currency"] == "EUR"
    assert g["amount_usd"] == round(300000.0 * EUR_TO_USD, 2)


def test_all_orgs_emitted(tmp_path):
    rows = _rows(tmp_path)
    names = {r.data["name"] for r in rows if r.table == "organization"}
    assert {"UNIV HELSINKI", "PARTNER GMBH", "LONE ORG"} <= names


def test_all_rows_pass_coerce(tmp_path):
    for r in _rows(tmp_path):
        coerce(r.table, r.data)
