"""Validation: paper 03's headline numbers must match the analysis output.

Guards against *drift*. If someone edits the prose in
``docs/papers/03-funder-specialization/`` or changes a query, the constants
asserted here (the numbers quoted in the paper) must still be produced by the
live analysis. We re-derive from the real DuckDB when present; when it is absent
(CI without the 3 GB db), the data-dependent tests skip but the pure-statistics
tests (HHI / Spearman / cosine) always run.

The paper cites ``analysis/specialization/results.json`` as its source of truth;
this file asserts that the prose constants equal that JSON, and that the JSON in
turn equals a fresh recomputation from the database.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DB = REPO_ROOT / "research_atlas.duckdb"
RESULTS = REPO_ROOT / "analysis" / "specialization" / "results.json"

sys.path.insert(0, str(REPO_ROOT))
from analysis.specialization import funder_specialization as fs  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure-statistics tests (no database) — always run.
# --------------------------------------------------------------------------- #
def test_hhi_even_split():
    assert fs.hhi([1, 1, 1, 1]) == pytest.approx(0.25, abs=1e-9)


def test_hhi_monopoly():
    assert fs.hhi([0, 0, 10]) == pytest.approx(1.0, abs=1e-9)


def test_hhi_26_field_even():
    # a perfect generalist over 26 fields has HHI = 1/26
    assert fs.hhi([1] * 26) == pytest.approx(1.0 / 26, abs=1e-9)


def test_spearman_monotone():
    assert fs.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0, abs=1e-9)


def test_spearman_reversed():
    assert fs.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0, abs=1e-9)


def test_cosine_identical():
    assert fs.cosine([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0, abs=1e-9)


def test_cosine_orthogonal():
    assert fs.cosine([1, 0], [0, 1]) == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# results.json must exist and carry the structure the paper reads.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def results():
    if not RESULTS.exists():
        pytest.skip("results.json not built; run analysis/specialization/run.py")
    return json.loads(RESULTS.read_text())


def test_results_has_sections(results):
    for k in ("coverage", "specialization_gradient", "funder_similarity",
              "specialization_stability", "composition_shift"):
        assert k in results, f"missing section {k}"


# --------------------------------------------------------------------------- #
# Paper headline constants == results.json  (drift guard for the prose).
# --------------------------------------------------------------------------- #
def _funder(grad, name):
    return next(r for r in grad if r["funder"] == name)


def test_paper_coverage(results):
    cov = results["coverage"]
    assert cov["n_funders_with_output_edges"] == 33
    assert cov["n_funders_in_specialization"] == 23
    assert cov["distinct_linked_works_in_window"] == 202909
    assert cov["work_window"] == [2016, 2024]


def test_paper_specialization_gradient(results):
    grad = results["specialization_gradient"]
    # sorted ascending by HHI -> generalist first, specialist last
    assert grad[0]["funder"] == "EC"
    assert grad[0]["hhi"] == pytest.approx(0.092, abs=0.002)
    nsf = _funder(grad, "NSF")
    assert nsf["hhi"] == pytest.approx(0.095, abs=0.002)
    assert nsf["top_field_share"] == pytest.approx(0.156, abs=0.01)
    nhgri = grad[-1]
    assert nhgri["funder"] == "NHGRI"
    assert nhgri["hhi"] == pytest.approx(0.466, abs=0.005)
    assert nhgri["top_field_share"] == pytest.approx(0.66, abs=0.02)
    assert "Biochemistry" in nhgri["top_field"]


def test_paper_similarity(results):
    sim = results["funder_similarity"]
    assert sim["nih_ic_internal_mean_cosine"] == pytest.approx(0.819, abs=0.01)
    assert sim["nsf_mean_cosine_to_nih"] == pytest.approx(0.385, abs=0.01)
    assert sim["ec_mean_cosine_to_nih"] == pytest.approx(0.621, abs=0.01)
    md = sim["most_distinct"][0]
    assert {md["funder_a"], md["funder_b"]} == {"NIDCD", "NSF"}
    assert md["cosine"] == pytest.approx(0.217, abs=0.01)


def test_paper_stability(results):
    stab = results["specialization_stability"]
    assert stab["spearman_rank_corr"] == pytest.approx(0.973, abs=0.005)
    assert stab["pearson_corr"] == pytest.approx(0.990, abs=0.005)
    assert stab["mean_abs_delta_hhi"] == pytest.approx(0.011, abs=0.003)


def test_paper_composition_shift(results):
    cs = results["composition_shift"]
    assert cs["window"] == [2016, 2024]
    phys = cs["endpoint_shift"]["Physical Sciences"]
    assert phys["delta_pp"] == pytest.approx(3.83, abs=0.3)
    lo, hi = phys["ci95_pp"]
    assert lo > 0 and hi > lo  # CI clears zero
    # the honest null: within NSF the trend is flat-to-negative
    assert cs["within_nsf_domain"]["physical_sciences_shift_pp"] < 0.5


# --------------------------------------------------------------------------- #
# results.json must equal a fresh recomputation from the live DB.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not DB.exists(), reason="DuckDB not present")
def test_results_match_live_db(results):
    con = fs.connect()
    fs.build_topic_rollup(con)
    grad = fs.specialization_gradient(con)
    sim = fs.funder_similarity(con)
    stab = fs.specialization_stability(con)
    con.close()

    assert grad[0]["funder"] == results["specialization_gradient"][0]["funder"]
    assert grad[0]["hhi"] == pytest.approx(
        results["specialization_gradient"][0]["hhi"], abs=1e-6)
    assert grad[-1]["funder"] == results["specialization_gradient"][-1]["funder"]
    assert sim["nih_ic_internal_mean_cosine"] == pytest.approx(
        results["funder_similarity"]["nih_ic_internal_mean_cosine"], abs=1e-6)
    assert stab["spearman_rank_corr"] == pytest.approx(
        results["specialization_stability"]["spearman_rank_corr"], abs=1e-6)
