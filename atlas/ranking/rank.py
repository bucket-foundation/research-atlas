"""Impact ranking: PageRank (power method), citation count, field-normalized impact.

Gian ranked papers with a PageRank power method over a CSR adjacency -- the right
idea, but on an almost-edgeless graph, so his dominant eigenvector came out
"very small equivalent components ... differences at the 14th significant digit"
(his words): effectively **uniform**, an artifact of the incomplete graph.

Here we run the *same* power method on the **complete in-corpus citation graph**
(:mod:`atlas.ranking.graph`) and it yields a real, heavy-tailed, non-uniform
distribution -- the fix to weaknesses #1/#2 made visible.

We compute three rankings and let the eval/report compare them:
- ``pagerank``                 -- recursive prestige on the citation graph;
- ``citation_count``           -- raw global ``cited_by_count`` (popularity);
- ``field_normalized_impact``  -- citations normalized by the expected count for
  a work's topic+year cohort (the metascience-standard way to compare impact
  across fields/ages without a recency/field bias).

PageRank convention: the citation edge i -> j means "i cites j", so prestige must
flow from the *citing* paper to the *cited* paper. We therefore run PageRank on
the citation adjacency A (random surfer follows references), which accumulates
mass on highly-referenced (cited) works -- the standard citation-PageRank.
"""

from __future__ import annotations

import numpy as np

from atlas.ranking.graph import CitationGraph


def pagerank(graph: CitationGraph, damping: float = 0.85,
             max_iter: int = 200, tol: float = 1e-10) -> np.ndarray:
    """PageRank via the power method on the citation CSR (Gian's method).

    Returns a probability vector ``r`` (sums to 1) over the ``graph.n`` works.
    Dangling nodes (no out-references in-corpus) redistribute their mass
    uniformly -- the standard correction that keeps ``r`` a true stationary
    distribution.
    """
    n = graph.n
    if n == 0:
        return np.zeros(0)
    indptr, indices, outdeg = graph.indptr, graph.indices, graph.out_degree
    r = np.full(n, 1.0 / n)
    teleport = (1.0 - damping) / n
    dangling = (outdeg == 0)
    for _ in range(max_iter):
        contrib = np.zeros(n)
        # each node i pushes r[i]/outdeg[i] to every node it cites
        active = r / np.where(outdeg == 0, 1, outdeg)
        # scatter-add along CSR rows
        # (vectorized: build per-edge source contribution, add at targets)
        src_contrib = np.repeat(active, outdeg)
        np.add.at(contrib, indices, src_contrib)
        dangling_mass = damping * r[dangling].sum() / n
        new_r = teleport + damping * contrib + dangling_mass
        if np.abs(new_r - r).sum() < tol:
            r = new_r
            break
        r = new_r
    # renormalize against float drift so it sums to exactly ~1
    s = r.sum()
    if s > 0:
        r = r / s
    return r


def citation_count(graph: CitationGraph) -> np.ndarray:
    """Raw global ``cited_by_count`` per work (popularity baseline)."""
    return graph.global_cited_by.astype(float)


def field_normalized_impact(records: list[dict],
                            graph: CitationGraph) -> np.ndarray:
    """Citations normalized by the topic+year cohort mean (field-normalized impact).

    For each work, divide its global ``cited_by_count`` by the mean
    ``cited_by_count`` of all works sharing its ``(topic_id, year)`` cohort.
    A value > 1 means "cited more than the typical paper of its topic and age".
    This removes the field/recency bias that makes raw citation counts unfair to
    compare -- the standard metascience normalization (a la MNCS / RCR).
    """
    n = graph.n
    cby = graph.global_cited_by.astype(float)
    cohorts: dict[tuple, list[int]] = {}
    for i, r in enumerate(records):
        key = (r.get("topic_id"), r.get("year"))
        cohorts.setdefault(key, []).append(i)
    out = np.zeros(n)
    for key, idxs in cohorts.items():
        vals = cby[idxs]
        mean = vals.mean()
        if mean > 0:
            out[idxs] = vals / mean
        else:
            out[idxs] = 0.0
    return out


def uniformity(r: np.ndarray) -> dict:
    """Diagnostics that show whether a ranking is uniform (Gian's failure mode).

    Returns spread statistics: a near-uniform vector has ``cv`` ~ 0, ``gini`` ~ 0,
    and ``top1_over_uniform`` ~ 1; a real heavy-tailed ranking has all three well
    above those floors.
    """
    n = len(r)
    if n == 0:
        return {"n": 0}
    mean = r.mean()
    std = r.std()
    cv = float(std / mean) if mean > 0 else 0.0
    # Gini coefficient
    sorted_r = np.sort(r)
    cum = np.cumsum(sorted_r)
    gini = float((n + 1 - 2 * (cum.sum() / cum[-1])) / n) if cum[-1] > 0 else 0.0
    uniform_val = 1.0 / n
    return {
        "n": n,
        "min": float(r.min()),
        "max": float(r.max()),
        "mean": float(mean),
        "std": float(std),
        "cv": cv,                         # coefficient of variation
        "gini": gini,                     # 0 = uniform, ->1 = concentrated
        "top1_over_uniform": float(r.max() / uniform_val),
        "top1pct_mass": float(
            np.sort(r)[::-1][: max(1, n // 100)].sum()),
    }
