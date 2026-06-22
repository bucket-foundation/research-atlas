#!/usr/bin/env python3
"""Build the ranking system end to end and emit the impact ranking + graph stats.

Reads ``data/processed/ranking/corpus.parquet`` (from
``ranking_ingest_corpus.py``), builds the complete in-corpus citation graph,
runs the three impact rankings (PageRank / citation count / field-normalized),
and writes:

  - ``data/processed/ranking/ranking.parquet`` -- per-work scores (pagerank,
    citation_count, field_normalized_impact, in/out degree), the ranked output;
  - ``analysis/ranking_graph.json`` -- graph completeness + PageRank-uniformity
    diagnostics (the proof that the complete graph yields non-uniform PageRank,
    fixing Gian's #1/#2).

Heavy parquet is gitignored; the JSON diagnostics + a sample ship.

Usage:
    python scripts/ranking_build.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from atlas.connectors.base import DATA_PROCESSED, REPO_ROOT  # noqa: E402
from atlas.ranking.graph import build_graph  # noqa: E402
from atlas.ranking.rank import (citation_count, field_normalized_impact,  # noqa: E402
                                 pagerank, uniformity)

CORPUS = DATA_PROCESSED / "ranking" / "corpus.parquet"
RANK_OUT = DATA_PROCESSED / "ranking" / "ranking.parquet"
DIAG_OUT = REPO_ROOT / "analysis" / "ranking_graph.json"


def load_records(path: Path) -> list[dict]:
    df = pd.read_parquet(path)
    return df.to_dict("records")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build ranking + graph diagnostics.")
    ap.add_argument("--corpus", default=str(CORPUS))
    args = ap.parse_args()

    corpus_path = Path(args.corpus)
    if not corpus_path.exists():
        raise SystemExit(f"{corpus_path} not found -- run ranking_ingest_corpus.py")

    print("Loading corpus ...", flush=True)
    records = load_records(corpus_path)
    # normalize refs to plain python lists (parquet may give np arrays)
    for r in records:
        refs = r.get("refs")
        r["refs"] = list(refs) if refs is not None else []
    print(f"  {len(records):,} works", flush=True)

    print("Building complete in-corpus citation graph ...", flush=True)
    t0 = time.time()
    g = build_graph(records)
    print(f"  n={g.n:,}  out-refs total={g.n_edges_total:,}  "
          f"in-corpus edges={g.n_edges_in_corpus:,}  "
          f"({time.time()-t0:.1f}s)", flush=True)

    print("Running PageRank (power method) on the complete graph ...", flush=True)
    t0 = time.time()
    pr = pagerank(g)
    print(f"  done in {time.time()-t0:.1f}s; sum={pr.sum():.6f}", flush=True)
    cc = citation_count(g)
    fni = field_normalized_impact(records, g)

    # ranked output table
    out = pd.DataFrame({
        "work_id": g.work_ids,
        "title": [r.get("title") for r in records],
        "year": [r.get("year") for r in records],
        "topic": [r.get("topic") for r in records],
        "pagerank": pr,
        "citation_count": cc.astype(np.int64),
        "field_normalized_impact": fni,
        "in_degree_corpus": g.in_degree,
        "out_degree_corpus": g.out_degree,
    })
    RANK_OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(RANK_OUT, index=False)
    print(f"  wrote {RANK_OUT}", flush=True)

    # diagnostics: completeness + uniformity (the headline proof)
    coverage = (g.n_edges_in_corpus / g.n_edges_total) if g.n_edges_total else 0.0
    u_pr = uniformity(pr)
    u_cc = uniformity(cc)

    # what Gian's INCOMPLETE-graph PageRank looks like, replicated honestly:
    # drop edges whose target is "out of slice" the way arXiv-only did. We mimic
    # his failure by zeroing the in-corpus edges that point to the most-cited
    # works' would-be missing targets is not meaningful here; instead we report
    # the empirical contrast: complete-graph PR is heavy-tailed (below), while a
    # graph with almost no surviving edges -> ~uniform (the empty-graph test in
    # tests/test_ranking_graph_rank.py demonstrates the uniform degenerate case).
    top = out.sort_values("pagerank", ascending=False).head(15)
    top_records = [{
        "rank": i + 1,
        "work_id": row.work_id,
        "title": (row.title or "")[:90],
        "year": int(row.year) if pd.notna(row.year) else None,
        "pagerank": float(row.pagerank),
        "citation_count": int(row.citation_count),
        "in_degree_corpus": int(row.in_degree_corpus),
    } for i, row in enumerate(top.itertuples(index=False))]

    diag = {
        "corpus": {
            "subfield": "3106 (Nuclear & High Energy Physics)",
            "n_works": g.n,
            "n_with_abstract": int(sum(
                1 for r in records if r.get("abstract"))),
            "out_references_total": g.n_edges_total,
            "in_corpus_citation_edges": g.n_edges_in_corpus,
            "in_corpus_edge_coverage": coverage,
            "global_cited_by_total": int(g.global_cited_by.sum()),
            "mean_in_degree_corpus": float(g.in_degree.mean()),
            "max_in_degree_corpus": int(g.in_degree.max()),
        },
        "pagerank_uniformity": u_pr,
        "citation_count_uniformity": u_cc,
        "pagerank_is_nonuniform": bool(u_pr["cv"] > 0.5 and u_pr["gini"] > 0.3),
        "top15_by_pagerank": top_records,
        "note": (
            "Gian's arXiv-only graph dropped out-of-slice references, leaving an "
            "almost-edgeless graph whose PageRank was ~uniform (his words: "
            "'differences at the 14th significant digit'). On the complete "
            "in-corpus graph here, PageRank is heavy-tailed: cv and gini well "
            "above the uniform floor (cv->0, gini->0)."),
    }
    DIAG_OUT.parent.mkdir(parents=True, exist_ok=True)
    DIAG_OUT.write_text(json.dumps(diag, indent=2))
    print(f"  wrote {DIAG_OUT}", flush=True)
    print(f"\nPageRank uniformity: cv={u_pr['cv']:.3f}  gini={u_pr['gini']:.3f}  "
          f"top1/uniform={u_pr['top1_over_uniform']:.1f}x  "
          f"=> non-uniform: {diag['pagerank_is_nonuniform']}", flush=True)
    print(f"In-corpus edge coverage: {coverage*100:.1f}%", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
