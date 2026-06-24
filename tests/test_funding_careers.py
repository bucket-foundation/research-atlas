"""Validation: paper 04's headline numbers must match the analysis output.

Guards against *drift*. If someone edits the prose in
``docs/papers/04-funding-careers/`` or changes a query, the constants asserted
here (the numbers quoted in the paper) must still be produced by the live
analysis. We re-derive from the real DuckDB when present; when it is absent (CI
without the 3 GB db), the data-dependent tests skip but the pure-logic tests
(matching discipline, bias direction invariants) always run.

The paper cites ``analysis/careers/results.json`` as its source of truth; this
file asserts that the prose constants equal that JSON, and that the JSON in turn
equals a fresh recomputation from the database.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DB = REPO_ROOT / "research_atlas.duckdb"
RESULTS = REPO_ROOT / "analysis" / "careers" / "results.json"

sys.path.insert(0, str(REPO_ROOT))
from analysis.careers import funding_careers as fc  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure-logic tests (no database) — always run.
# --------------------------------------------------------------------------- #
def test_constants_are_conservative():
    # the matching floors and funder floor are the documented, conservative ones
    assert fc.MIN_STRATUM == 30
    assert fc.MIN_FUNDER_PEOPLE == 300
    assert fc.BOOT_N == 2000
    assert fc.BOOT_SEED == 42


def test_seniority_order_is_canonical():
    assert fc.SENIORITY_ORDER == [
        "eminent", "established", "rising-star", "early/unknown"]


# --------------------------------------------------------------------------- #
# results.json must exist and be internally consistent — always run.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def results():
    if not RESULTS.exists():
        pytest.skip("results.json not built; run analysis/careers/run.py")
    return json.loads(RESULTS.read_text())


def test_results_have_all_sections(results):
    for k in ("coverage", "resolution_bias", "stage_composition",
              "portfolio_concentration", "funder_portfolios",
              "matched_productivity", "matched_by_stage"):
        assert k in results, f"missing section {k}"


# ---- headline numbers quoted in the paper --------------------------------- #
def test_coverage_headline(results):
    c = results["coverage"]
    assert c["pi_edges"] == 1_740_326
    assert c["resolved_edges"] == 528_570
    assert c["resolved_pct"] == pytest.approx(30.37, abs=0.05)
    assert c["distinct_resolved_researchers"] == 59_180
    assert c["distinct_recipient_orgs"] == 1_574
    assert c["resolved_with_orcid"] == 490_839


def test_resolution_bias_headline(results):
    rb = results["resolution_bias"]
    assert rb["all_researchers"]["pct_orcid"] == pytest.approx(69.1, abs=0.1)
    assert rb["funded_researchers"]["pct_orcid"] == pytest.approx(89.9, abs=0.1)
    assert rb["orcid_gap_pp"] == pytest.approx(20.8, abs=0.1)
    assert rb["funded_researchers"]["mean_works"] == pytest.approx(5.12, abs=0.05)
    assert rb["all_researchers"]["mean_works"] == pytest.approx(2.41, abs=0.05)
    assert rb["naive_works_ratio_funded_over_all"] == pytest.approx(2.124, abs=0.02)


def test_matched_productivity_headline(results):
    mp = results["matched_productivity"]
    assert mp["n_resolvable_population"] == 565_867
    assert mp["n_funded"] == 59_180
    assert mp["n_comparison"] == 506_687
    assert mp["n_strata"] == 193
    # the central finding: naive ~2.1x collapses to ~1.23x after matching
    assert mp["naive_works_ratio_within_resolvable"] == pytest.approx(2.111, abs=0.02)
    assert mp["matched_works_ratio"] == pytest.approx(1.227, abs=0.02)
    assert mp["matched_works_ci"][0] == pytest.approx(1.183, abs=0.03)
    assert mp["matched_works_ci"][1] == pytest.approx(1.281, abs=0.03)
    assert mp["matched_h_ratio"] == pytest.approx(1.234, abs=0.02)
    # the citation advantage is almost entirely selection
    assert mp["matched_citations_ratio"] == pytest.approx(1.057, abs=0.02)
    # matched residual must be far below the naive ratio (the whole point)
    assert mp["matched_works_ratio"] < 0.7 * mp["naive_works_ratio_within_resolvable"]
    # CI must exclude parity (a real, if small, descriptive residual) and 2.0
    assert mp["matched_works_ci"][0] > 1.0
    assert mp["matched_works_ci"][1] < 2.0


def test_stage_composition_headline(results):
    sc = results["stage_composition"]
    assert sc["n_researchers"] == 59_180
    assert sc["share_pct"]["rising-star"] == pytest.approx(58.8, abs=0.2)
    assert sc["share_pct"]["established"] == pytest.approx(19.2, abs=0.2)
    assert sc["share_pct"]["eminent"] == pytest.approx(4.6, abs=0.2)
    # shares sum to 100
    assert sum(sc["share_pct"].values()) == pytest.approx(100.0, abs=0.3)


def test_portfolio_concentration_headline(results):
    pc = results["portfolio_concentration"]
    assert pc["median_grants_per_researcher"] == pytest.approx(4.0, abs=0.01)
    assert pc["mean_grants_per_researcher"] == pytest.approx(8.9, abs=0.1)
    assert pc["max_grants_per_researcher"] == 394
    assert pc["multi_funder_share_pct"] == pytest.approx(6.55, abs=0.1)


def test_funder_portfolios_headline(results):
    fp = {f["funder"]: f for f in results["funder_portfolios"]["funders"]}
    # five funders clear the 300-person floor
    assert set(fp) == {"nih", "nsf", "wellcome", "sloan", "dfg"}
    # NIH is overwhelmingly biomedical; NSF is broad
    assert fp["nih"]["top_fields"][0]["field"] == "biomed-bio"
    assert fp["nih"]["top_fields"][0]["share_pct"] == pytest.approx(88.0, abs=0.5)
    assert fp["nsf"]["top_fields"][0]["share_pct"] < 40  # NSF no field dominates
    # Sloan funds the most eminent-skewed, highest-impact portfolio
    assert fp["sloan"]["stage_share_pct"]["eminent"] == pytest.approx(9.5, abs=0.3)
    assert fp["sloan"]["median_citations"] == pytest.approx(451.5, abs=1.0)
    assert max(f["stage_share_pct"]["eminent"] for f in fp.values()) == \
        fp["sloan"]["stage_share_pct"]["eminent"]
    # DFG / Wellcome are the most rising-star-skewed
    assert fp["dfg"]["stage_share_pct"]["rising-star"] == pytest.approx(69.0, abs=0.5)


def test_matched_by_stage_headline(results):
    mbs = results["matched_by_stage"]
    # rising-stars have the largest matched residual gap
    assert mbs["rising-star"]["matched_works_ratio"] == pytest.approx(1.375, abs=0.03)
    assert mbs["established"]["matched_works_ratio"] == pytest.approx(1.156, abs=0.03)
    assert mbs["rising-star"]["matched_works_ratio"] == max(
        mbs[s]["matched_works_ratio"] for s in mbs
        if mbs[s]["matched_works_ratio"] is not None)


# --------------------------------------------------------------------------- #
# results.json must equal a fresh recomputation from the DB — runs only when
# the 3 GB DuckDB is present.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def con():
    if not DB.exists():
        pytest.skip("research_atlas.duckdb absent (CI without the heavy db)")
    c = fc.connect()
    yield c
    c.close()


def test_coverage_matches_db(con, results):
    fresh = fc.coverage(con)
    assert fresh["resolved_edges"] == results["coverage"]["resolved_edges"]
    assert fresh["distinct_resolved_researchers"] == \
        results["coverage"]["distinct_resolved_researchers"]


def test_resolution_bias_matches_db(con, results):
    fresh = fc.resolution_bias(con)
    assert fresh["naive_works_ratio_funded_over_all"] == pytest.approx(
        results["resolution_bias"]["naive_works_ratio_funded_over_all"], abs=0.01)


def test_matched_productivity_matches_db(con, results):
    fresh = fc.matched_productivity(con)
    # deterministic (seeded bootstrap) -> exact agreement on point + CI
    assert fresh["matched_works_ratio"] == pytest.approx(
        results["matched_productivity"]["matched_works_ratio"], abs=1e-9)
    assert fresh["matched_works_ci"] == results["matched_productivity"]["matched_works_ci"]
    assert fresh["n_strata"] == results["matched_productivity"]["n_strata"]


def test_funder_portfolios_match_db(con, results):
    fresh = {f["funder"]: f for f in fc.funder_portfolios(con)["funders"]}
    saved = {f["funder"]: f for f in results["funder_portfolios"]["funders"]}
    assert set(fresh) == set(saved)
    for k in fresh:
        assert fresh[k]["n_researchers"] == saved[k]["n_researchers"]
