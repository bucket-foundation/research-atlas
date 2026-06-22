"""Held-out citation-prediction evaluation -- the headline Gian never had.

Gian's report has **no quantitative evaluation** of his recommender; it shows a
few cherry-picked similar abstracts and word clouds. We add a real, standard
information-retrieval evaluation that all four methods are scored on identically:

Task (held-out citation prediction / link prediction)
-----------------------------------------------------
A paper's reference list is a gold set of papers its authors judged relevant.
For each *query* paper with enough in-corpus references:
  1. **mask** a random fraction of its in-corpus references (the held-out set);
  2. ask each method to rank all other papers by relevance to the query;
  3. measure how high the *masked* references rank.

A method that recommends genuinely relevant papers will rank the held-out true
references near the top. Metrics (over the held-out set per query, averaged):
  - **Recall@k** -- fraction of held-out references retrieved in the top k;
  - **MAP**      -- mean average precision over the ranked list;
  - **MRR**      -- mean reciprocal rank of the first held-out reference hit.

Methods scored
--------------
  (a) TF-IDF cosine            -- Gian's baseline;
  (b) word2vec mean-pool cosine-- Gian's baseline;
  (c) transformer cosine       -- the neural fix (#3);
  (d) graph / co-citation      -- bibliographic-coupling, text-free.

Statistical rigor: per-query metrics are aggregated with a **bootstrap 95% CI**
so the transformer-vs-TF-IDF gap is reported with uncertainty, not a point
estimate. The masking is seeded for reproducibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from atlas.ranking.graph import CitationGraph


@dataclass
class EvalSplit:
    """A reproducible held-out split for citation prediction."""

    query_idx: np.ndarray                 # (Q,) query work row indices
    masked: list[np.ndarray]              # per query: held-out (masked) target idxs
    observed: list[np.ndarray]            # per query: still-visible target idxs
    seed: int = 0


def make_split(graph: CitationGraph, mask_frac: float = 0.3,
               min_refs: int = 5, max_queries: int | None = None,
               seed: int = 0) -> EvalSplit:
    """Build the held-out split: mask ``mask_frac`` of each eligible query's refs.

    Eligible queries have at least ``min_refs`` in-corpus out-references (so there
    is a meaningful gold set to predict). For each, a seeded random subset is
    masked (the gold held-out set); the rest stay observed.
    """
    rng = np.random.default_rng(seed)
    indptr, indices = graph.indptr, graph.indices
    q_idx, masked, observed = [], [], []
    for i in range(graph.n):
        targets = indices[indptr[i]:indptr[i + 1]]
        if len(targets) < min_refs:
            continue
        n_mask = max(1, int(round(len(targets) * mask_frac)))
        perm = rng.permutation(len(targets))
        m = targets[perm[:n_mask]]
        o = targets[perm[n_mask:]]
        q_idx.append(i)
        masked.append(np.asarray(m))
        observed.append(np.asarray(o))
    q_idx = np.asarray(q_idx)
    if max_queries is not None and len(q_idx) > max_queries:
        sel = rng.choice(len(q_idx), size=max_queries, replace=False)
        sel.sort()
        q_idx = q_idx[sel]
        masked = [masked[j] for j in sel]
        observed = [observed[j] for j in sel]
    return EvalSplit(query_idx=q_idx, masked=masked, observed=observed, seed=seed)


# --------------------------------------------------------------------------- #
# Metrics (per query, from a full score row + a gold/held-out set)
# --------------------------------------------------------------------------- #

def _rank_targets(scores_row: np.ndarray, gold: np.ndarray,
                  forbid: np.ndarray | None = None):
    """Return ranks (1-based) of each gold item in the descending score order.

    ``forbid`` (e.g. the still-observed references and self) are removed from the
    candidate pool before ranking so the method is scored only on retrieving the
    *held-out* items against true negatives.
    """
    row = scores_row.copy()
    if forbid is not None and len(forbid):
        row[forbid] = -np.inf
    # descending order of all candidates
    order = np.argsort(-row, kind="stable")
    rank_of = np.empty(len(row), dtype=np.int64)
    rank_of[order] = np.arange(1, len(row) + 1)
    return rank_of[gold]


def recall_at_k(gold_ranks: np.ndarray, k: int, n_gold: int) -> float:
    if n_gold == 0:
        return 0.0
    return float((gold_ranks <= k).sum()) / n_gold


def average_precision(gold_ranks: np.ndarray, n_gold: int) -> float:
    """AP for a single query from the ranks of its gold items."""
    if n_gold == 0:
        return 0.0
    sr = np.sort(gold_ranks)
    # precision at each gold hit position = (#gold at or above) / rank
    precisions = (np.arange(1, len(sr) + 1)) / sr
    return float(precisions.sum() / n_gold)


def reciprocal_rank(gold_ranks: np.ndarray) -> float:
    if len(gold_ranks) == 0:
        return 0.0
    return float(1.0 / gold_ranks.min())


# --------------------------------------------------------------------------- #
# Scoring a method over the whole split
# --------------------------------------------------------------------------- #

@dataclass
class MethodResult:
    name: str
    recall_at: dict[int, float] = field(default_factory=dict)
    recall_at_ci: dict[int, tuple] = field(default_factory=dict)
    map: float = 0.0
    map_ci: tuple = (0.0, 0.0)
    mrr: float = 0.0
    mrr_ci: tuple = (0.0, 0.0)
    n_queries: int = 0
    # raw per-query arrays for significance testing / bootstrap
    per_query_ap: np.ndarray = field(default_factory=lambda: np.zeros(0))
    per_query_rr: np.ndarray = field(default_factory=lambda: np.zeros(0))
    per_query_recall: dict[int, np.ndarray] = field(default_factory=dict)


def _bootstrap_ci(x: np.ndarray, n_boot: int = 1000, seed: int = 0,
                  alpha: float = 0.05) -> tuple:
    if len(x) == 0:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    n = len(x)
    means = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, n)
        means[b] = x[idx].mean()
    lo = float(np.quantile(means, alpha / 2))
    hi = float(np.quantile(means, 1 - alpha / 2))
    return (lo, hi)


def evaluate_method(name: str, score_fn, split: EvalSplit, graph: CitationGraph,
                    ks=(5, 10, 20, 50), n_boot: int = 1000,
                    seed: int = 0) -> MethodResult:
    """Score one method over the split.

    ``score_fn(query_idx) -> (Q, n)`` returns a relevance score row per query
    (self already -inf). For each query we forbid the still-observed references
    (and self) and rank the held-out gold against the rest.
    """
    q_idx = split.query_idx
    scores = score_fn(q_idx)                       # (Q, n)
    Q = len(q_idx)
    aps = np.zeros(Q)
    rrs = np.zeros(Q)
    recalls = {k: np.zeros(Q) for k in ks}
    for qi in range(Q):
        gold = split.masked[qi]
        forbid = np.concatenate([split.observed[qi], [q_idx[qi]]]).astype(np.int64)
        ranks = _rank_targets(scores[qi], gold, forbid=forbid)
        n_gold = len(gold)
        aps[qi] = average_precision(ranks, n_gold)
        rrs[qi] = reciprocal_rank(ranks)
        for k in ks:
            recalls[k][qi] = recall_at_k(ranks, k, n_gold)

    res = MethodResult(name=name, n_queries=Q,
                       per_query_ap=aps, per_query_rr=rrs,
                       per_query_recall=recalls)
    res.map = float(aps.mean())
    res.map_ci = _bootstrap_ci(aps, n_boot, seed)
    res.mrr = float(rrs.mean())
    res.mrr_ci = _bootstrap_ci(rrs, n_boot, seed)
    for k in ks:
        res.recall_at[k] = float(recalls[k].mean())
        res.recall_at_ci[k] = _bootstrap_ci(recalls[k], n_boot, seed)
    return res


def paired_bootstrap_pvalue(a: np.ndarray, b: np.ndarray, n_boot: int = 2000,
                            seed: int = 0) -> float:
    """Two-sided paired bootstrap p-value for mean(a) - mean(b) != 0.

    Used to test transformer (a) vs TF-IDF (b) on the same queries.
    """
    diff = a - b
    n = len(diff)
    if n == 0:
        return 1.0
    rng = np.random.default_rng(seed)
    obs = diff.mean()
    centered = diff - obs
    boots = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, n)
        boots[i] = centered[idx].mean()
    # p = fraction of centered bootstrap means at least as extreme as obs
    p = (np.abs(boots) >= abs(obs)).mean()
    return float(p)
