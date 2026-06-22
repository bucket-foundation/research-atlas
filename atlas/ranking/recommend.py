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


def _adjacency_csr(graph: CitationGraph):
    """The citation adjacency ``A`` (``A[i, j] = 1`` iff i cites j) as a CSR.

    Built directly from the graph's CSR arrays -- ``data`` is all-ones, so
    ``A @ A.T`` counts shared out-references (bibliographic coupling) exactly.
    Falls back to ``None`` if scipy is unavailable so callers can use the
    dense numpy path (mirrors :mod:`atlas.ranking.embed`'s optional-dep style).
    """
    try:
        from scipy.sparse import csr_matrix
    except Exception:  # pragma: no cover - scipy is a declared analysis dep
        return None
    n = graph.n
    indptr = np.ascontiguousarray(graph.indptr, dtype=np.int64)
    indices = np.ascontiguousarray(graph.indices, dtype=np.int64)
    data = np.ones(indices.shape[0], dtype=np.float32)
    return csr_matrix((data, indices, indptr), shape=(n, n))


def cocitation_scores(graph: CitationGraph,
                      query_idx: np.ndarray) -> np.ndarray:
    """Graph recommender score matrix ``(Q, n)`` from bibliographic coupling.

    Two papers are related if they cite the same papers (bibliographic coupling)
    -- a text-free signal. Score(i, j) = | refs(i) ∩ refs(j) |. This is the
    "graph/co-citation" method in the evaluation. Self is set to -inf.

    Vectorized form: with the citation adjacency ``A`` (``A[i, j] = 1`` iff i
    cites j), the bibliographic-coupling score is exactly ``A A^T`` -- entry
    ``(i, j)`` is the size of the intersection of i's and j's out-reference sets.
    We compute ``A[query] @ A^T`` as a single sparse matmul (CSR), which is the
    same arithmetic as the previous nested-Python triple loop but runs in
    optimized C and scales to 20k-50k works/field. The dense ``(Q, n)`` result
    and the ``-inf`` self mask are byte-for-byte the prior semantics, so
    downstream ranking (Recall@k / MAP / MRR) is numerically unchanged.

    Falls back to an explicit reverse-map loop if scipy is unavailable.
    """
    n = graph.n
    query_idx = np.asarray(query_idx)
    A = _adjacency_csr(graph)
    if A is not None:
        # A[query] @ A^T : (Q, n) shared-reference counts. Densify to match the
        # historical dense-array contract the evaluator's argsort consumes.
        scores = (A[query_idx] @ A.T).toarray().astype(np.float32, copy=False)
        # mask self (each query's own row index) with -inf, exactly as before.
        scores[np.arange(len(query_idx)), query_idx] = -np.inf
        return scores

    # ---- pure-numpy fallback (no scipy): same result, reverse-map loop ----- #
    indptr, indices = graph.indptr, graph.indices
    out = np.full((len(query_idx), n), 0.0, dtype=np.float32)
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
