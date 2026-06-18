"""Unit tests for the DFG/GEPRIS normalizer (no network: recorded HTML fixtures)."""

from pathlib import Path

from atlas.connectors.dfg import DfgConnector, parse_project_html
from atlas.schema import coerce

FIXTURES = Path(__file__).parent / "fixtures" / "dfg"


def _load_fixture(pid: str) -> dict:
    html = (FIXTURES / f"projekt_{pid}.html").read_text(encoding="utf-8")
    return {"id": pid, "html": html}


def _normalize(pids, tmp_path):
    conn = DfgConnector(
        raw_dir=tmp_path / "raw", processed_dir=tmp_path / "proc", resolve_ror=False
    )
    recs = [_load_fixture(p) for p in pids]
    return list(conn.normalize(recs))


def test_parse_project_html_extracts_fields():
    rec = _load_fixture("268853")
    fields = parse_project_html(rec["html"])
    assert "Spatial Statistics" in (fields.get("title") or "")
    assert "Mathematics" in (fields.get("Subject Area") or "")
    assert "Technische Universität Bergakademie Freiberg" in (
        fields.get("Applicant Institution") or ""
    )


def test_normalizer_emits_each_entity_type(tmp_path):
    rows = _normalize(["268853"], tmp_path)
    tables = {r.table for r in rows}
    assert {"funder", "grant", "organization", "person", "field"} <= tables
    assert {"funder_grant", "grant_org", "grant_person", "person_org"} <= tables


def test_funder_emitted_once(tmp_path):
    rows = _normalize(["268853", "268879", "268931"], tmp_path)
    funders = [r for r in rows if r.table == "funder"]
    assert len(funders) == 1
    assert funders[0].data["short_name"] == "DFG"
    assert funders[0].data["country_code"] == "DE"


def test_grant_money_unknown_is_none(tmp_path):
    # GEPRIS does not publish funding amounts -> money invariant: null, not 0.
    rows = _normalize(["268853"], tmp_path)
    g = next(r.data for r in rows if r.table == "grant")
    assert g["amount_original"] is None
    assert g["amount_usd"] is None
    assert g["currency"] is None


def test_term_parsed_to_dates(tmp_path):
    rows = _normalize(["268853"], tmp_path)
    g = next(r.data for r in rows if r.table == "grant")
    assert g["start_date"] == "1997-01-01"
    assert g["end_date"] == "2003-12-31"


def test_subject_area_field(tmp_path):
    rows = _normalize(["268853"], tmp_path)
    names = {r.data["name"] for r in rows if r.table == "field"}
    assert "Mathematics" in names


def test_spokesperson_is_pi(tmp_path):
    rows = _normalize(["268853"], tmp_path)
    grant = next(r for r in rows if r.table == "grant")
    gid = grant.data["atlas_id"]
    gp = [r for r in rows if r.table == "grant_person" and r.data["src_id"] == gid]
    roles = {r.data["role"] for r in gp}
    assert "pi" in roles


def test_all_rows_pass_coerce(tmp_path):
    rows = _normalize(["268853", "268879", "268931"], tmp_path)
    for r in rows:
        coerce(r.table, r.data)
