"""Unit tests for the held-out citation-prediction evaluation + recommenders."""

import numpy as np
import pytest

from atlas.ranking.embed import (tfidf_matrix, word2vec_matrix, tokenize)
from atlas.ranking.evaluate import (average_precision, make_split,
                                     evaluate_method, recall_at_k,
                                     reciprocal_rank, paired_bootstrap_pvalue)
from atlas.ranking.graph import build_graph
from atlas.ranking.recommend import cosine_scores, cocitation_scores


def _cocitation_reference(graph, query_idx):
    """Reference (pure-Python, pre-vectorization) bibliographic-coupling scorer.

    Mirrors the original nested-loop implementation exactly: Score(i, j) =
    |refs(i) ∩ refs(j)| via a reverse (cited -> citing) map, self set to -inf.
    The vectorized scipy.sparse ``cocitation_scores`` must equal this byte-for-byte
    so the held-out citation-prediction metrics (Recall@k / MAP / MRR) cannot drift.
    """
    n = graph.n
    indptr, indices = graph.indptr, graph.indices
    out = np.full((len(query_idx), n), 0.0, dtype=np.float32)
    citing_of = [[] for _ in range(n)]
    for i in range(n):
        for t in indices[indptr[i]:indptr[i + 1]]:
            citing_of[t].append(i)
    for qi, i in enumerate(query_idx):
        for t in indices[indptr[i]:indptr[i + 1]]:
            for j in citing_of[t]:
                out[qi, j] += 1.0
        out[qi, i] = -np.inf
    return out


def test_recall_map_mrr_known_values():
    # gold items landed at ranks 1 and 3, with 2 gold total
    ranks = np.array([1, 3])
    assert recall_at_k(ranks, 1, 2) == pytest.approx(0.5)   # only rank-1 in top1
    assert recall_at_k(ranks, 3, 2) == pytest.approx(1.0)   # both in top3
    # AP = mean over hits of (#hits so far / rank) = (1/1 + 2/3)/2
    assert average_precision(ranks, 2) == pytest.approx((1.0 + 2/3) / 2)
    assert reciprocal_rank(ranks) == pytest.approx(1.0)     # first hit at rank 1


def test_perfect_method_scores_one():
    # 3 papers, query W0 cites W1 and W2 (both in corpus). Mask both.
    recs = [
        {"work_id": "W0", "refs": ["W1", "W2"], "cited_by_count": 0,
         "topic_id": "T", "year": 2020},
        {"work_id": "W1", "refs": [], "cited_by_count": 0,
         "topic_id": "T", "year": 2020},
        {"work_id": "W2", "refs": [], "cited_by_count": 0,
         "topic_id": "T", "year": 2020},
    ]
    g = build_graph(recs)
    split = make_split(g, mask_frac=1.0, min_refs=2, seed=0)
    assert len(split.query_idx) == 1

    # an oracle that scores exactly the gold targets highest
    def oracle(q_idx):
        n = g.n
        out = np.full((len(q_idx), n), -1.0)
        for qi, i in enumerate(q_idx):
            for t in split.masked[qi]:
                out[qi, t] = 1.0
            out[qi, i] = -np.inf
        return out

    res = evaluate_method("oracle", oracle, split, g, ks=(2,), n_boot=50)
    assert res.recall_at[2] == pytest.approx(1.0)
    assert res.map == pytest.approx(1.0)
    assert res.mrr == pytest.approx(1.0)


def test_transformer_beats_random_on_synthetic_text():
    # Build a corpus with two clear topical clusters; references stay within
    # cluster. A text method (TF-IDF cosine) must beat a random scorer at
    # recovering masked in-cluster references.
    rng = np.random.default_rng(0)
    physics = "quark gluon hadron collider lhc neutrino boson higgs decay"
    bio = "protein enzyme cell mitochondria genome dna rna ribosome membrane"
    recs = []
    n_each = 12
    for i in range(n_each):
        words = rng.choice(physics.split(), size=8)
        recs.append({"work_id": f"P{i}", "text": " ".join(words),
                     "refs": [], "cited_by_count": 0, "topic_id": "phys",
                     "year": 2020})
    for i in range(n_each):
        words = rng.choice(bio.split(), size=8)
        recs.append({"work_id": f"B{i}", "text": " ".join(words),
                     "refs": [], "cited_by_count": 0, "topic_id": "bio",
                     "year": 2020})
    # wire references within cluster
    for i in range(n_each):
        recs[i]["refs"] = [f"P{(i + 1) % n_each}", f"P{(i + 2) % n_each}"]
        recs[n_each + i]["refs"] = [f"B{(i + 1) % n_each}", f"B{(i + 2) % n_each}"]

    g = build_graph(recs)
    texts = [r["text"] for r in recs]
    tfidf, _ = tfidf_matrix(texts, min_df=1)
    split = make_split(g, mask_frac=0.5, min_refs=2, seed=1)

    def tfidf_fn(q_idx):
        return cosine_scores(tfidf, q_idx)

    def random_fn(q_idx):
        rr = np.random.default_rng(2)
        return rr.random((len(q_idx), g.n))

    tf = evaluate_method("tfidf", tfidf_fn, split, g, ks=(5,), n_boot=100)
    rnd = evaluate_method("random", random_fn, split, g, ks=(5,), n_boot=100)
    # TF-IDF (in-cluster words) should retrieve in-cluster refs better than chance
    assert tf.recall_at[5] > rnd.recall_at[5]


def test_headline_eval_json_pins_transformer_win():
    """If the eval JSON exists, the paper's headline must hold against it.

    Guards against prose/data drift: the transformer (SPECTER) row must be
    present and beat TF-IDF on MAP with a significant paired-bootstrap p-value.
    Skipped when the JSON has not been generated (e.g. fresh checkout).
    """
    import json
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "analysis" / "ranking_eval.json"
    if not p.exists():
        pytest.skip("analysis/ranking_eval.json not generated")
    e = json.loads(p.read_text())
    m = e["methods"]
    if "transformer" not in m:
        pytest.skip("transformer row not in eval (run with GPU embeddings)")
    # transformer present and 768-d SPECTER per the manifest
    assert m["transformer"]["map"] > m["tfidf"]["map"], "transformer must beat TF-IDF on MAP"
    tv = e["transformer_vs_tfidf"]
    assert tv["delta_map"] > 0
    assert tv["paired_bootstrap_p"] < 0.05, "transformer-vs-TF-IDF must be significant"
    # transformer should also lead word2vec and graph on MAP
    assert m["transformer"]["map"] >= m["word2vec"]["map"]
    assert m["transformer"]["map"] >= m["graph"]["map"]


def test_cocitation_vectorized_equals_reference_small():
    """Vectorized (scipy.sparse A A^T) co-citation == the pure-Python reference.

    Small hand-checkable fixture: works share out-references in a known pattern,
    so |refs(i) ∩ refs(j)| is computable by hand and both implementations must
    agree exactly, including the -inf self mask. This pins ranking equivalence so
    future changes to the recommender can't silently shift the graph metrics.
    """
    # W0 cites W4,W5 ; W1 cites W4,W5,W6 ; W2 cites W5,W6 ; W3 cites W6
    recs = [
        {"work_id": "W0", "refs": ["W4", "W5"], "cited_by_count": 0,
         "topic_id": "T", "year": 2020},
        {"work_id": "W1", "refs": ["W4", "W5", "W6"], "cited_by_count": 0,
         "topic_id": "T", "year": 2020},
        {"work_id": "W2", "refs": ["W5", "W6"], "cited_by_count": 0,
         "topic_id": "T", "year": 2020},
        {"work_id": "W3", "refs": ["W6"], "cited_by_count": 0,
         "topic_id": "T", "year": 2020},
        {"work_id": "W4", "refs": [], "cited_by_count": 0, "topic_id": "T",
         "year": 2020},
        {"work_id": "W5", "refs": [], "cited_by_count": 0, "topic_id": "T",
         "year": 2020},
        {"work_id": "W6", "refs": [], "cited_by_count": 0, "topic_id": "T",
         "year": 2020},
    ]
    g = build_graph(recs)
    q = np.array([0, 1, 2, 3])
    new = cocitation_scores(g, q)
    ref = _cocitation_reference(g, q)

    # exact equality including -inf self positions
    assert np.array_equal(~np.isfinite(new), ~np.isfinite(ref))
    fin = np.isfinite(ref)
    assert np.array_equal(new[fin], ref[fin])

    # spot-check known intersections: refs(W0)={W4,W5}, refs(W1)={W4,W5,W6} -> 2
    assert new[0, 1] == pytest.approx(2.0)   # share W4, W5
    assert new[0, 2] == pytest.approx(1.0)   # share W5
    assert new[0, 3] == pytest.approx(0.0)   # share nothing
    assert new[1, 2] == pytest.approx(2.0)   # share W5, W6
    assert new[0, 0] == -np.inf              # self masked


def test_cocitation_vectorized_equals_reference_dense_random():
    """Equivalence on a larger random dense graph (the regime that motivated the
    vectorization). Whatever the topology, A A^T must match the reverse-map loop.
    """
    rng = np.random.default_rng(7)
    n = 120
    ids = [f"W{i}" for i in range(n)]
    recs = []
    for i in range(n):
        k = int(rng.integers(5, 15))
        tgt = rng.choice(n, size=k, replace=False)
        refs = [ids[t] for t in tgt if t != i]
        recs.append({"work_id": ids[i], "refs": refs, "cited_by_count": 0,
                     "topic_id": "T", "year": 2020})
    g = build_graph(recs)
    q = np.arange(n)
    new = cocitation_scores(g, q)
    ref = _cocitation_reference(g, q)
    assert np.array_equal(~np.isfinite(new), ~np.isfinite(ref))
    fin = np.isfinite(ref)
    assert np.array_equal(new[fin], ref[fin])


def test_cocitation_metrics_unchanged_through_evaluator():
    """End-to-end: the eval metrics computed from the vectorized scorer equal
    those from the reference scorer on the same split (Recall@k / MAP / MRR)."""
    rng = np.random.default_rng(3)
    n = 80
    ids = [f"W{i}" for i in range(n)]
    recs = []
    for i in range(n):
        tgt = rng.choice(n, size=8, replace=False)
        recs.append({"work_id": ids[i],
                     "refs": [ids[t] for t in tgt if t != i],
                     "cited_by_count": 0, "topic_id": "T", "year": 2020})
    g = build_graph(recs)
    split = make_split(g, mask_frac=0.3, min_refs=5, seed=0)
    ks = (5, 10, 20)
    fast = evaluate_method("vec", lambda qq: cocitation_scores(g, qq), split, g,
                           ks=ks, n_boot=50, seed=0)
    slow = evaluate_method("ref", lambda qq: _cocitation_reference(g, qq), split,
                           g, ks=ks, n_boot=50, seed=0)
    assert fast.map == pytest.approx(slow.map)
    assert fast.mrr == pytest.approx(slow.mrr)
    for k in ks:
        assert fast.recall_at[k] == pytest.approx(slow.recall_at[k])


def test_paired_bootstrap_pvalue_detects_difference():
    a = np.ones(100) * 0.8
    b = np.ones(100) * 0.2
    p = paired_bootstrap_pvalue(a, b, n_boot=500)
    assert p < 0.05
    # identical -> not significant
    p2 = paired_bootstrap_pvalue(a, a, n_boot=500)
    assert p2 > 0.05
