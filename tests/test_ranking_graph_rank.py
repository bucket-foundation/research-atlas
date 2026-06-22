"""Unit tests for the ranking graph + impact ranking (no network, synthetic)."""

import numpy as np
import pytest

from atlas.ranking.graph import build_graph
from atlas.ranking.rank import (citation_count, field_normalized_impact,
                                 pagerank, uniformity)


def _records():
    # A tiny corpus where W3 is cited by everyone -> must dominate PageRank.
    # refs are out-references (i cites j).
    return [
        {"work_id": "W1", "refs": ["W3", "W2"], "cited_by_count": 5,
         "topic_id": "T1", "year": 2020},
        {"work_id": "W2", "refs": ["W3"], "cited_by_count": 50,
         "topic_id": "T1", "year": 2020},
        {"work_id": "W3", "refs": [], "cited_by_count": 500,
         "topic_id": "T1", "year": 2020},
        {"work_id": "W4", "refs": ["W3", "W2", "W1"], "cited_by_count": 1,
         "topic_id": "T1", "year": 2020},
        # a target outside the corpus must be dropped, not crash
        {"work_id": "W5", "refs": ["W3", "WX_NOT_IN_CORPUS"], "cited_by_count": 0,
         "topic_id": "T2", "year": 2021},
    ]


def test_graph_drops_out_of_corpus_edges_but_keeps_complete_in_corpus():
    g = build_graph(_records())
    assert g.n == 5
    # W5 references W3 (in) and WX (out) -> one in-corpus edge counted, one dropped
    assert g.n_edges_total == 2 + 1 + 0 + 3 + 2  # = 8 raw out-refs
    # in-corpus: W1->{W3,W2}=2, W2->{W3}=1, W4->{W3,W2,W1}=3, W5->{W3}=1  => 7
    assert g.n_edges_in_corpus == 7
    # W3 is cited in-corpus by W1,W2,W4,W5 = 4
    w3 = g.work_ids.index("W3")
    assert g.in_degree[w3] == 4


def test_pagerank_sums_to_one_and_is_nonuniform():
    g = build_graph(_records())
    r = pagerank(g)
    assert r.shape == (5,)
    assert r.sum() == pytest.approx(1.0, abs=1e-9)
    # the most-cited node must carry the most PageRank mass
    w3 = g.work_ids.index("W3")
    assert r[w3] == r.max()
    # and it must be clearly non-uniform (Gian's failure was ~uniform)
    u = uniformity(r)
    assert u["cv"] > 0.1
    assert u["top1_over_uniform"] > 1.5


def test_pagerank_on_empty_graph_is_uniform_baseline():
    # no edges at all -> stationary dist is the uniform teleport (the degenerate
    # case Gian was effectively stuck in); still sums to 1.
    recs = [{"work_id": f"W{i}", "refs": [], "cited_by_count": 0,
             "topic_id": "T", "year": 2020} for i in range(4)]
    g = build_graph(recs)
    r = pagerank(g)
    assert r.sum() == pytest.approx(1.0, abs=1e-9)
    assert np.allclose(r, 0.25)
    u = uniformity(r)
    assert u["cv"] == pytest.approx(0.0, abs=1e-9)


def test_citation_count_is_global_not_capped():
    g = build_graph(_records())
    c = citation_count(g)
    w3 = g.work_ids.index("W3")
    # global cited_by (500) is preserved, NOT capped to the in-corpus 4
    assert c[w3] == 500.0


def test_field_normalized_impact_centers_cohorts():
    g = build_graph(_records())
    fni = field_normalized_impact(_records(), g)
    # cohort (T1, 2020) has cited_by [5,50,500,1] mean=139 -> W3 ~ 500/139 > 1
    w3 = g.work_ids.index("W3")
    assert fni[w3] > 1.0
    # the single-member cohort (T2,2021)=W5 has cby 0 -> normalized 0
    w5 = g.work_ids.index("W5")
    assert fni[w5] == 0.0
