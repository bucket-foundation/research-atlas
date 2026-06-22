"""Unit tests for the held-out citation-prediction evaluation + recommenders."""

import numpy as np
import pytest

from atlas.ranking.embed import (tfidf_matrix, word2vec_matrix, tokenize)
from atlas.ranking.evaluate import (average_precision, make_split,
                                     evaluate_method, recall_at_k,
                                     reciprocal_rank, paired_bootstrap_pvalue)
from atlas.ranking.graph import build_graph
from atlas.ranking.recommend import cosine_scores


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


def test_paired_bootstrap_pvalue_detects_difference():
    a = np.ones(100) * 0.8
    b = np.ones(100) * 0.2
    p = paired_bootstrap_pvalue(a, b, n_boot=500)
    assert p < 0.05
    # identical -> not significant
    p2 = paired_bootstrap_pvalue(a, a, n_boot=500)
    assert p2 > 0.05
