"""kNN recommenders over each representation + the graph co-citation recommender.

Gian's recommender: given a paper, return the most similar papers by **cosine
similarity** of the (NMF-reduced TF-IDF) abstract vectors. We reproduce that
exactly and add the same cosine-kNN over word2vec and transformer vectors, so the
recommender quality of each representation is directly comparable, plus a
graph-based recommender (co-citation / bibliographic coupling) that uses no text
at all.

Because every matrix from :mod:`atlas.ranking.embed` is L2-normalized, cosine is
a dot product and top-k is a single matvec per query -- cheap enough to score the
whole held-out set in the eval.
"""

from __future__ import annotations

import numpy as np

from atlas.ranking.graph import CitationGraph


def cosine_topk(matrix: np.ndarray, query_idx: np.ndarray, k: int,
                exclude_self: bool = True) -> np.ndarray:
    """Top-k most cosine-similar rows for each query row.

    ``matrix`` is L2-normalized ``(n, d)``. ``query_idx`` are the row indices to
    score. Returns an ``(len(query_idx), k)`` int array of column indices ranked
    by descending cosine. Self is excluded when ``exclude_self``.
    """
    q = matrix[query_idx]                      # (Q, d)
    sims = q @ matrix.T                        # (Q, n)
    if exclude_self:
        sims[np.arange(len(query_idx)), query_idx] = -np.inf
    kk = min(k, sims.shape[1] - 1)
    # argpartition for top-k, then sort those k
    part = np.argpartition(-sims, kk, axis=1)[:, :kk]
    rows = np.arange(len(query_idx))[:, None]
    order = np.argsort(-sims[rows, part], axis=1)
    return part[rows, order]


def cosine_scores(matrix: np.ndarray, query_idx: np.ndarray) -> np.ndarray:
    """Full cosine score matrix ``(Q, n)`` for the given queries (self set to -inf)."""
    sims = matrix[query_idx] @ matrix.T
    sims[np.arange(len(query_idx)), query_idx] = -np.inf
    return sims


def cocitation_scores(graph: CitationGraph,
                      query_idx: np.ndarray) -> np.ndarray:
    """Graph recommender score matrix ``(Q, n)`` from bibliographic coupling.

    Two papers are related if they cite the same papers (bibliographic coupling)
    -- a text-free signal. Score(i, j) = | refs(i) ∩ refs(j) |. This is the
    "graph/co-citation" method in the evaluation. Self is set to -inf.
    """
    n = graph.n
    indptr, indices = graph.indptr, graph.indices
    # build the set of out-targets per query once
    out = np.full((len(query_idx), n), 0.0, dtype=np.float32)
    # represent each node's out-set as a boolean row lazily via the CSR
    # (corpus is bounded; this is O(Q * avg_refs * avg_co) which is fine)
    # Precompute, for each target work t, the list of works citing t (reverse map)
    citing_of: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for t in indices[indptr[i]:indptr[i + 1]]:
            citing_of[t].append(i)
    for qi, i in enumerate(query_idx):
        targets = indices[indptr[i]:indptr[i + 1]]
        for t in targets:
            for j in citing_of[t]:
                out[qi, j] += 1.0
        out[qi, i] = -np.inf
    return out
