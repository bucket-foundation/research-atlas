"""Tests for the cross-field orchestrator: aggregation, interdisciplinarity,
stats helpers, state resume, and the per-checkpoint analysis on a synthetic corpus.

No network and no GPU: ``analyze_field`` is driven with ``do_transformer=False``
(baselines + graph) on a synthetic two-cluster corpus, and the producer/consumer
``run_checkpoint`` is exercised with a stub connector that serves cached pages.
"""

import json

import numpy as np
import pytest

from atlas.ranking import crossfield as cf
from atlas.ranking.crossfield import (
    CrossFieldState, analyze_field, compute_interdisciplinarity,
    crossfield_analyses, _binom_two_sided, _one_sample_bootstrap, FIELDS,
    DEFAULT_TRANCHES)


# --------------------------------------------------------------------------- #
# stats helpers
# --------------------------------------------------------------------------- #

def test_binom_two_sided_extremes():
    # all wins out of 10 fair coins -> very significant
    assert _binom_two_sided(10, 10, 0.5) < 0.01
    # half wins -> not significant
    assert _binom_two_sided(5, 10, 0.5) > 0.5
    assert _binom_two_sided(0, 0, 0.5) == 1.0


def test_one_sample_bootstrap_detects_positive_mean():
    pos = _one_sample_bootstrap(np.full(40, 0.1), n_boot=1000)
    assert pos["mean"] == pytest.approx(0.1)
    assert pos["ci"][0] > 0          # CI excludes 0
    zero = _one_sample_bootstrap(np.zeros(40), n_boot=1000)
    assert zero["p"] >= 0.99


# --------------------------------------------------------------------------- #
# interdisciplinarity
# --------------------------------------------------------------------------- #

def test_interdisciplinarity_counts_cross_field_edges():
    # field A: A0 cites A1 (in-field) and B0 (cross-field)
    recs_a = [
        {"work_id": "A0", "refs": ["A1", "B0"]},
        {"work_id": "A1", "refs": []},
    ]
    recs_b = [{"work_id": "B0", "refs": ["A1"]}]   # B0 cites A1 -> cross
    out = compute_interdisciplinarity({"A": recs_a, "B": recs_b})
    # A: 2 resolvable refs (A1 in-field, B0 cross) -> 1 cross
    assert out["A"]["resolvable_refs"] == 2
    assert out["A"]["cross_field_refs"] == 1
    assert out["A"]["cross_field_fraction"] == pytest.approx(0.5)
    # B: 1 resolvable ref (A1) -> cross
    assert out["B"]["cross_field_fraction"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# cross-field generalization aggregation
# --------------------------------------------------------------------------- #

def _fake_field_result(fid, delta, win, gini=0.4):
    return {
        "field_id": fid, "field": FIELDS.get(fid, fid),
        "works_loaded": 100, "citation_gini": gini,
        "pagerank": {"gini": gini + 0.1},
        "transformer_vs_tfidf": {
            "delta_map": delta, "rel_improvement_pct": delta * 100,
            "paired_bootstrap_p": 0.01, "specter_wins": win,
        },
        "_interdisc": {"resolvable_refs": 10, "cross_field_refs": int(3 + int(fid) % 3),
                       "cross_field_fraction": 0.3 + (int(fid) % 3) * 0.1},
    }


def test_crossfield_generalization_counts_wins_and_combines():
    frs = [_fake_field_result(str(11 + i), delta=0.02 if i < 8 else -0.01,
                              win=i < 8, gini=0.3 + 0.01 * i) for i in range(10)]
    out = crossfield_analyses(frs)
    gen = out["generalization"]
    assert gen["fields_evaluated"] == 10
    assert gen["fields_specter_wins"] == 8
    assert gen["win_fraction"] == pytest.approx(0.8)
    assert "combined_field_level" in gen and gen["combined_field_level"]["n"] == 10
    conc = out["concentration"]
    assert conc["citation_gini_range"][0] <= conc["citation_gini_range"][1]
    assert out["interdisciplinarity"] is not None
    assert out["interdisciplinarity"]["range"][0] <= out["interdisciplinarity"]["range"][1]


# --------------------------------------------------------------------------- #
# per-field analysis on a synthetic two-cluster corpus (no GPU, no network)
# --------------------------------------------------------------------------- #

def _synthetic_records(n_each=40, seed=0):
    rng = np.random.default_rng(seed)
    phys = "quark gluon hadron collider neutrino boson higgs decay lattice qcd".split()
    bio = "protein enzyme cell mitochondria genome dna rna ribosome membrane lipid".split()
    recs = []
    for i in range(n_each):
        words = rng.choice(phys, size=10)
        recs.append({"work_id": f"P{i}", "title": " ".join(words[:3]),
                     "abstract": " ".join(words), "text": " ".join(words),
                     "refs": [], "cited_by_count": int(rng.integers(0, 500)),
                     "topic_id": "Tphys", "field_id": "26", "year": 2020})
    for i in range(n_each):
        words = rng.choice(bio, size=10)
        recs.append({"work_id": f"B{i}", "title": " ".join(words[:3]),
                     "abstract": " ".join(words), "text": " ".join(words),
                     "refs": [], "cited_by_count": int(rng.integers(0, 500)),
                     "topic_id": "Tbio", "field_id": "26", "year": 2020})
    # dense within-cluster references with a few hubs (so PageRank is non-uniform,
    # not a perfectly regular ring): everyone cites the cluster's hub (index 0)
    # plus a rolling window.
    for i in range(n_each):
        recs[i]["refs"] = [f"P0", f"P1", f"P{(i+2) % n_each}",
                           f"P{(i+3) % n_each}", f"P{(i+5) % n_each}"]
        recs[n_each+i]["refs"] = [f"B0", f"B1", f"B{(i+2) % n_each}",
                                  f"B{(i+3) % n_each}", f"B{(i+5) % n_each}"]
    return recs


def test_analyze_field_baselines_only_runs_and_reports_graph():
    recs = _synthetic_records()
    fr = analyze_field("26", recs, eval_sample=80, min_refs=3, max_queries=40,
                       do_transformer=False, n_boot=100, log=lambda *a: None)
    assert fr["field"] == FIELDS["26"]
    assert fr["works_loaded"] == 80
    assert fr["graph"]["n_edges_in_corpus"] > 0
    # PageRank on a real graph is non-uniform (Gini well above 0)
    assert fr["pagerank"]["gini"] > 0.0
    assert fr["eval"] is not None
    assert "tfidf" in fr["eval"] and "graph" in fr["eval"]
    # no transformer row when do_transformer is False
    assert "transformer" not in fr["eval"]


# --------------------------------------------------------------------------- #
# state: durable, atomic, resumable
# --------------------------------------------------------------------------- #

def test_state_roundtrip_and_resume(tmp_path):
    p = tmp_path / "state.json"
    s = CrossFieldState(tranches=[1000, 2000], window=("2015-01-01", "2024-12-31"),
                        fields=["26", "21"])
    s.checkpoint = 1
    s.done_fields["1"] = ["26", "21"]
    s.save(p)
    assert p.exists()
    s2 = CrossFieldState.load(p)
    assert s2.checkpoint == 1
    assert s2.tranches == [1000, 2000]
    assert s2.done_fields["1"] == ["26", "21"]
    # missing file -> None (fresh run)
    assert CrossFieldState.load(tmp_path / "nope.json") is None


# --------------------------------------------------------------------------- #
# producer/consumer checkpoint with a stub connector (no network) + resume
# --------------------------------------------------------------------------- #

class _StubConn:
    """Serves a fixed synthetic corpus per field as a single 'cached' page."""

    def __init__(self, per_field):
        self._per_field = per_field   # field_id -> list[normalized records]

    def fetch_field(self, field_id, target, from_date, to_date,
                    cache_only=False):
        recs = self._per_field.get(field_id, [])[:target]
        # re-shape into the raw OpenAlex page form that _normalize_pages expects
        results = [{
            "id": f"https://openalex.org/{r['work_id']}",
            "title": r["title"],
            "abstract_inverted_index": None,
            "referenced_works": [f"https://openalex.org/{x}" for x in r["refs"]],
            "cited_by_count": r["cited_by_count"],
            "publication_year": r["year"],
            "type": "article",
            "primary_topic": {"id": "https://openalex.org/" + r["topic_id"],
                              "display_name": r["topic_id"],
                              "field": {"id": "https://openalex.org/fields/" + field_id,
                                        "display_name": FIELDS.get(field_id, field_id)}},
        } for r in recs]
        # inject the abstract text directly via a sentinel the normalizer keeps
        for raw, r in zip(results, recs):
            raw["title"] = r["text"]      # title+abstract feed -> text
        yield {"results": results, "meta": {"next_cursor": None}}


def test_checkpoint1_json_pins_crossfield_headline():
    """If checkpoint_1.json exists, the paper's cross-field headline must hold.

    Guards prose/data drift: all 26 fields evaluated, SPECTER wins a clear
    majority (>= 15/26), and the per-field win/loss split and Gini/interdisc
    ranges are present. Skipped on a fresh checkout where the run hasn't been
    executed (the JSON is committed, so this normally runs).
    """
    import json
    from pathlib import Path

    p = (Path(__file__).resolve().parents[1] / "analysis" / "crossfield"
         / "checkpoint_1.json")
    if not p.exists():
        pytest.skip("analysis/crossfield/checkpoint_1.json not generated")
    r = json.loads(p.read_text())
    assert r["fields_with_results"] == 26
    g = r["crossfield"]["generalization"]
    assert g["fields_evaluated"] == 26
    # SPECTER wins a majority of fields (the headline generalization direction)
    assert g["fields_specter_wins"] >= 15
    # combined field-level test is present with a CI and p
    c = g["combined_field_level"]
    assert c is not None and c["n"] == 26 and c["mean"] > 0
    # concentration + interdisciplinarity ranges are present and ordered
    conc = r["crossfield"]["concentration"]["citation_gini_range"]
    assert conc[0] <= conc[1]
    inter = r["crossfield"]["interdisciplinarity"]["range"]
    assert inter[0] <= inter[1]
    # GPU throughput recorded
    assert r["embed_docs_per_sec"] and r["embed_docs_per_sec"] > 0


def test_run_checkpoint_producer_consumer_and_resume(tmp_path):
    recs26 = _synthetic_records(seed=1)
    recs21 = _synthetic_records(seed=2)
    conn = _StubConn({"26": recs26, "21": recs21})
    adir = tmp_path / "crossfield"

    report = cf.run_checkpoint(
        1, tranches=[80], fields=["26", "21"], eval_sample=80, min_refs=3,
        max_queries=40, do_transformer=False, n_boot=100, conn=conn,
        analysis_dir=adir, log=lambda *a: None)

    assert report["checkpoint"] == 1
    assert report["fields_with_results"] == 2
    assert (adir / "checkpoint_1.json").exists()
    assert (adir / "manifest.json").exists()
    assert (adir / "convergence.jsonl").exists()
    # partial file is cleaned on completion
    assert not (adir / "checkpoint_1.partial.json").exists()
    # cross-field block present
    gen = report["crossfield"]["generalization"]
    assert gen["fields_evaluated"] == 0  # no transformer -> no specter-vs-tfidf
    # interdisciplinarity computed (both fields share no ids -> 0 cross, but key present)
    assert report["crossfield"]["concentration"]["citation_gini_range"][0] >= 0

    # RESUME: write a partial with one field 'finished', re-run -> reuses it
    finished_one = [report["fields"][0]]
    (adir / "checkpoint_1.partial.json").write_text(
        json.dumps({"checkpoint": 1, "fields": finished_one}))
    report2 = cf.run_checkpoint(
        1, tranches=[80], fields=["26", "21"], eval_sample=80, min_refs=3,
        max_queries=40, do_transformer=False, n_boot=100, conn=conn,
        analysis_dir=adir, log=lambda *a: None)
    assert report2["fields_with_results"] == 2


def test_convergence_decay_pins_paper_thesis():
    """If the 4 checkpoints exist, the paper's convergence thesis must hold.

    Guards prose/data drift for the headline finding: the SPECTER-vs-TF-IDF
    across-field edge starts positive at the head of the impact distribution
    (ckpt 1) and decays monotonically toward null as the corpus broadens, and
    the corpus plateaus at ckpt 4 (ckpt3 == ckpt4 -> converged). Skipped on a
    fresh checkout where the loop hasn't run all 4 checkpoints.
    """
    import json
    from pathlib import Path

    cdir = Path(__file__).resolve().parents[1] / "analysis" / "crossfield"
    conv_path = cdir / "convergence.jsonl"
    if not conv_path.exists():
        pytest.skip("analysis/crossfield/convergence.jsonl not generated")
    rows = [json.loads(ln) for ln in conv_path.read_text().splitlines() if ln.strip()]
    by_ck = {r["checkpoint"]: r for r in rows}
    if not {1, 2, 3, 4}.issubset(by_ck):
        pytest.skip("all 4 checkpoints not present in convergence.jsonl")

    c1, c2, c3, c4 = by_ck[1], by_ck[2], by_ck[3], by_ck[4]

    # head of the distribution: SPECTER wins a majority, positive combined edge
    assert c1["fields_specter_wins"] >= 15
    assert c1["mean_delta_map"] > 0

    # monotonic decay of the across-field edge toward null as corpus broadens
    assert c1["mean_delta_map"] > c2["mean_delta_map"] > c3["mean_delta_map"]
    assert c1["win_fraction"] > c2["win_fraction"] >= c3["win_fraction"]

    # broad-literature edge is gone (non-significant, sign flipped) by ckpt 3
    assert c3["mean_delta_map"] < 0
    assert c3["combined_p"] > 0.05
    assert c3["fields_specter_wins"] < 13  # below the 13/26 coin-flip majority

    # CONVERGED: ckpt 4 hit the corpus plateau and reproduced ckpt 3 exactly
    assert c4["total_works"] == c3["total_works"]
    assert c4["fields_specter_wins"] == c3["fields_specter_wins"]
    assert c4["mean_delta_map"] == c3["mean_delta_map"]
    assert c4["combined_p"] == c3["combined_p"]

    # the converged combined test is present in the checkpoint JSON
    cj4 = json.loads((cdir / "checkpoint_4.json").read_text())
    comb = cj4["crossfield"]["generalization"]["combined_field_level"]
    assert comb["n"] == 26 and comb["mean"] < 0 and comb["ci"][0] < 0 < comb["ci"][1]
