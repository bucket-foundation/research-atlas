"""The complete in-corpus citation graph (CSR), fixing Gian's weaknesses #1/#2.

Gian's graph was incomplete two ways:
  #1 every cited paper outside his arXiv slice was *dropped*, so most edges
     vanished;
  #2 he never built the reverse map (cited -> citing), so in-citations were
     undercounted and his PageRank came out almost uniform (an artifact of a
     graph with almost no surviving edges).

Here every work carries its global OpenAlex out-references. We keep the edges
whose target is also in the corpus -- the **in-corpus citation graph** -- which
is complete by construction (no edge silently dropped; every endpoint resolves).
We also keep the GLOBAL ``cited_by_count`` so the impact signal is not capped at
the corpus boundary.

We build a CSR adjacency exactly as Gian did (row pointer / col index / values),
but on the *complete* graph, so the power-method PageRank in :mod:`atlas.ranking.rank`
now has real structure to find.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class CitationGraph:
    """A complete in-corpus citation graph in CSR form.

    Attributes
    ----------
    work_ids : list[str]
        Node i is ``work_ids[i]`` (OpenAlex id). Stable order = the corpus order.
    indptr, indices : np.ndarray
        CSR of the **citation** adjacency A where ``A[i, j] = 1`` iff work i
        *cites* work j (i -> j is an out-reference). Row i's targets are
        ``indices[indptr[i]:indptr[i+1]]``.
    out_degree, in_degree : np.ndarray
        In-corpus out/in citation degrees (per node).
    global_cited_by : np.ndarray
        OpenAlex global ``cited_by_count`` per node (NOT capped to the corpus).
    n_edges_total, n_edges_in_corpus : int
        Total out-references seen vs. those whose target is in-corpus -- the
        honest measure of how "complete" the slice is.
    """

    work_ids: list[str]
    indptr: np.ndarray
    indices: np.ndarray
    out_degree: np.ndarray
    in_degree: np.ndarray
    global_cited_by: np.ndarray
    n_edges_total: int
    n_edges_in_corpus: int

    @property
    def n(self) -> int:
        return len(self.work_ids)


def build_graph(records: list[dict]) -> CitationGraph:
    """Build the complete in-corpus citation CSR from corpus records.

    ``records`` are the dicts produced by :meth:`CorpusConnector.normalize`
    (must include ``work_id``, ``refs``, ``cited_by_count``).
    """
    work_ids = [r["work_id"] for r in records]
    index = {wid: i for i, wid in enumerate(work_ids)}
    n = len(work_ids)

    rows: list[list[int]] = [[] for _ in range(n)]
    n_edges_total = 0
    n_edges_in_corpus = 0
    in_degree = np.zeros(n, dtype=np.int64)
    for i, r in enumerate(records):
        seen_targets: set[int] = set()
        for ref in r.get("refs") or []:
            n_edges_total += 1
            j = index.get(ref)
            if j is None or j == i:
                continue  # target outside corpus, or self-citation
            if j in seen_targets:
                continue
            seen_targets.add(j)
            rows[i].append(j)
            in_degree[j] += 1
            n_edges_in_corpus += 1

    indptr = np.zeros(n + 1, dtype=np.int64)
    for i in range(n):
        indptr[i + 1] = indptr[i] + len(rows[i])
    indices = np.empty(int(indptr[-1]), dtype=np.int64)
    for i in range(n):
        s = indptr[i]
        indices[s:s + len(rows[i])] = rows[i]

    out_degree = np.diff(indptr).astype(np.int64)
    global_cited_by = np.array(
        [r.get("cited_by_count") or 0 for r in records], dtype=np.int64)

    return CitationGraph(
        work_ids=work_ids, indptr=indptr, indices=indices,
        out_degree=out_degree, in_degree=in_degree,
        global_cited_by=global_cited_by,
        n_edges_total=n_edges_total, n_edges_in_corpus=n_edges_in_corpus,
    )
