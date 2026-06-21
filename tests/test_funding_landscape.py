"""Validation: the paper's headline numbers must match the analysis output.

These tests guard against *drift* — if someone edits the prose in
``docs/papers/01-funding-landscape/`` or changes a query, the numbers asserted
here (which are the numbers quoted in the paper) must still be produced by the
live analysis. We re-run the analysis against the real DuckDB when it is present;
when it is not (CI without the 2 GB db), the data-dependent tests skip but the
pure-statistics tests (Gini/HHI/top-share) always run.

The paper cites ``analysis/results.json`` as its source of truth; this file
asserts that the prose constants equal that JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DB = REPO_ROOT / "research_atlas.duckdb"
RESULTS = REPO_ROOT / "analysis" / "results.json"

import sys
sys.path.insert(0, str(REPO_ROOT))
from analysis import funding_landscape as fl  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure-statistics tests (no database) — always run.
# --------------------------------------------------------------------------- #
def test_gini_perfect_equality():
    assert fl.gini([5, 5, 5, 5]) == pytest.approx(0.0, abs=1e-9)


def test_gini_maximal_inequality():
    # one holder has everything -> Gini -> (n-1)/n; close to 1 for large n
    n = 1000
    x = [0] * (n - 1) + [1]
    assert fl.gini(x) == pytest.approx((n - 1) / n, abs=1e-6)


def test_gini_known_value():
    # closed-form check on a small textbook sample
    assert fl.gini([1, 2, 3, 4, 5]) == pytest.approx(0.2667, abs=1e-3)


def test_gini_rejects_negative():
    with pytest.raises(ValueError):
        fl.gini([1, -2, 3])


def test_hhi_even_split():
    assert fl.hhi([1, 1, 1, 1]) == pytest.approx(0.25, abs=1e-9)


def test_hhi_monopoly():
    assert fl.hhi([0, 0, 10]) == pytest.approx(1.0, abs=1e-9)


def test_top_share_monotone():
    x = np.random.default_rng(0).pareto(2.0, 500) + 1
    assert fl.top_share(x, 0.01) <= fl.top_share(x, 0.10)


def test_bootstrap_ci_brackets_point():
    x = np.random.default_rng(1).pareto(2.0, 300) + 1
    point, lo, hi = fl.gini_bootstrap_ci(x, n_boot=300)
    assert lo <= point <= hi


# --------------------------------------------------------------------------- #
# Paper <-> data consistency. Requires the DuckDB + results.json.
# --------------------------------------------------------------------------- #
requires_db = pytest.mark.skipif(
    not DB.exists() or not RESULTS.exists(),
    reason="research_atlas.duckdb / results.json not present (run analysis/run.py)",
)

# The constants below are the EXACT numbers quoted in
# docs/papers/01-funding-landscape/paper.md. Tolerances allow for bootstrap
# seed-stability (CI bounds) but pin point estimates tightly.
PAPER = {
    "grants_in_window": 692_237,
    "linked_works": 235_122,
    "n_orgs": 5_049,
    "gini_grants": 0.932,          # ROR-resolved, count-based
    "top1pct_share": 0.555,
    "us_grant_share": 0.918,
    "multi_funder_share": 0.283,
    "n_domains_works_top": "Physical Sciences",
}


@requires_db
def test_results_json_fresh_matches_live_db():
    """The committed results.json must equal a fresh recompute (idempotency)."""
    con = fl.connect()
    cov = fl.coverage_stats(con)
    stored = json.loads(RESULTS.read_text())
    assert cov["grants_in_window"] == stored["coverage"]["grants_in_window"]
    assert cov["linked_works"] == stored["coverage"]["linked_works"]
    con.close()


@requires_db
def test_paper_coverage_numbers():
    r = json.loads(RESULTS.read_text())
    assert r["coverage"]["grants_in_window"] == PAPER["grants_in_window"]
    assert r["coverage"]["linked_works"] == PAPER["linked_works"]


@requires_db
def test_paper_org_concentration():
    r = json.loads(RESULTS.read_text())["org_concentration"]
    assert r["n_orgs"] == PAPER["n_orgs"]
    assert r["gini_grants"] == pytest.approx(PAPER["gini_grants"], abs=0.002)
    assert r["top1pct_share"] == pytest.approx(PAPER["top1pct_share"], abs=0.005)
    # CI must bracket the point estimate and be a real (positive-width) interval
    lo, hi = r["gini_grants_ci95"]
    assert lo < r["gini_grants"] < hi


@requires_db
def test_paper_dollar_sensitivity_lower_than_or_near_count_gini():
    """Sanity: the noisy dollar Gini is reported separately and is in-range."""
    r = json.loads(RESULTS.read_text())
    d = r["org_concentration_dollars_sensitivity"]["gini_dollars"]
    assert 0.85 < d < 0.95


@requires_db
def test_paper_geography():
    r = json.loads(RESULTS.read_text())["geography"]
    assert r["us_grant_share"] == pytest.approx(PAPER["us_grant_share"], abs=0.005)
    assert r["countries"][0]["country_code"] == "US"


@requires_db
def test_paper_cofunding():
    r = json.loads(RESULTS.read_text())["cofunding"]
    assert r["multi_funder_share"] == pytest.approx(PAPER["multi_funder_share"], abs=0.005)
    # ERC+EC is the dominant intra-European co-funding pair
    top = r["top_pairs"][0]
    assert {top["funder_a"], top["funder_b"]} == {"ERC", "EC"}


@requires_db
def test_paper_domains_ordering():
    r = json.loads(RESULTS.read_text())["domain_composition"]
    assert r[0]["domain"] == PAPER["n_domains_works_top"]
    works = [d["works"] for d in r]
    assert works == sorted(works, reverse=True)  # monotone non-increasing


@requires_db
def test_paper_field_dynamics_covid_is_top_riser():
    r = json.loads(RESULTS.read_text())["field_dynamics"]
    top_riser = r["rising"][0]["name"].lower()
    assert "covid" in top_riser
    # falling fields must actually be < 1.0 growth
    assert all(f["growth_ratio"] < 1.0 for f in r["falling"])
