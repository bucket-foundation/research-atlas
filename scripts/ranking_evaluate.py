#!/usr/bin/env python3
"""Held-out citation-prediction evaluation -- the headline result Gian lacked.

Scores four recommenders on the SAME held-out split, with bootstrap 95% CIs:
  (a) TF-IDF cosine            -- Gian's baseline (his primary method),
  (b) word2vec mean-pool cosine-- Gian's baseline (his second method),
  (c) transformer cosine       -- neural embeddings (Ollama nomic-embed-text),
  (d) graph / co-citation      -- bibliographic coupling, text-free.

Design (honest + closed-world): the transformer embeddings are the bottleneck
(one local-GPU call per doc), so we evaluate on a bounded random sample of works
that (i) have an abstract and (ii) have >= min_refs in-corpus references, and we
build the citation sub-graph *restricted to that sample* so every candidate is
embedded and every gold reference is a real in-sample target. All four methods
see the identical closed candidate pool, so the comparison is apples-to-apples.

Writes ``analysis/ranking_eval.json`` (the eval table + CIs + the
transformer-vs-TF-IDF paired bootstrap p-value).

Usage:
    python scripts/ranking_evaluate.py --sample 8000
    python scripts/ranking_evaluate.py --sample 8000 --no-transformer  # baselines only
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
from atlas.ranking.embed import (tfidf_matrix, nmf_reduce,  # noqa: E402
                                  word2vec_matrix, transformer_matrix_by_id)
from atlas.ranking.recommend import cosine_scores, cocitation_scores  # noqa: E402
from atlas.ranking.evaluate import (make_split, evaluate_method,  # noqa: E402
                                    paired_bootstrap_pvalue)

CORPUS = DATA_PROCESSED / "ranking" / "corpus.parquet"
EVAL_OUT = REPO_ROOT / "analysis" / "ranking_eval.json"
KS = (5, 10, 20, 50)


def select_sample(df: pd.DataFrame, n: int, min_refs: int, seed: int,
                  topic: str | None = None):
    """Pick a DENSE closed-world sample so the citation graph isn't sparsified.

    Random subsampling of a 255k corpus collapses in-sample edge coverage to ~1%
    (references point all over the corpus). To keep a real, dense citation graph
    AND a bounded transformer-embedding cost, we:

      1. optionally restrict to a single coherent OpenAlex topic (references
         concentrate within a topic);
      2. keep works with an abstract and >= min_refs references;
      3. take the ``n`` works with the highest in-pool reference degree, i.e. the
         densely-connected citation core, rather than a random scatter.

    This yields a closed candidate pool where masked references are real
    in-pool targets for every method -- an apples-to-apples comparison.
    """
    has_abs = df[df["abstract"].notna()].copy()
    if topic:
        has_abs = has_abs[has_abs["topic_id"] == topic]
    has_abs["nref"] = has_abs["refs"].map(len)
    pool = has_abs[has_abs["nref"] >= min_refs].reset_index(drop=True)

    if len(pool) > n:
        # rank by in-pool degree: how many of a work's refs land inside the pool.
        pool_ids = set(pool["work_id"])
        in_pool_deg = pool["refs"].map(
            lambda rs: sum(1 for r in rs if r in pool_ids))
        pool = pool.assign(_inpool=in_pool_deg.values)
        pool = pool.sort_values("_inpool", ascending=False).head(n)
        pool = pool.drop(columns=["_inpool"]).reset_index(drop=True)
    return pool


def main() -> int:
    ap = argparse.ArgumentParser(description="Held-out citation-prediction eval.")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--sample", type=int, default=8000,
                    help="number of works to embed + evaluate on (bounded by "
                         "transformer embedding throughput)")
    ap.add_argument("--topic", default=None,
                    help="restrict the eval to one OpenAlex topic id (dense "
                         "closed world, e.g. T10048 = Particle physics)")
    ap.add_argument("--mask-frac", type=float, default=0.3)
    ap.add_argument("--min-refs", type=int, default=5)
    ap.add_argument("--max-queries", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-transformer", action="store_true",
                    help="skip the neural embeddings (baselines + graph only)")
    ap.add_argument("--n-boot", type=int, default=1000)
    args = ap.parse_args()

    df = pd.read_parquet(args.corpus)
    for c in ("refs",):
        df[c] = df[c].map(lambda x: list(x) if x is not None else [])
    print(f"Corpus: {len(df):,} works", flush=True)

    sample = select_sample(df, args.sample, args.min_refs, args.seed,
                            topic=args.topic)
    records = sample.to_dict("records")
    for r in records:
        r["refs"] = list(r["refs"])
    print(f"Sample: {len(records):,} works (abstract + >= {args.min_refs} refs)",
          flush=True)

    g = build_graph(records)
    cov = g.n_edges_in_corpus / g.n_edges_total if g.n_edges_total else 0
    print(f"  in-sample edges: {g.n_edges_in_corpus:,} "
          f"(coverage within sample {cov*100:.1f}%)", flush=True)

    split = make_split(g, mask_frac=args.mask_frac, min_refs=args.min_refs,
                       max_queries=args.max_queries, seed=args.seed)
    print(f"  eval queries: {len(split.query_idx):,}", flush=True)

    texts = [r["text"] for r in records]
    results = {}

    # (a) TF-IDF cosine -- Gian's baseline (NMF-reduced, as he ran it)
    print("Building TF-IDF (Gian baseline) ...", flush=True)
    tfidf, _ = tfidf_matrix(texts, min_df=2, ngram=1)
    tfidf_nmf = nmf_reduce(tfidf, n_components=min(100, tfidf.shape[1] - 1),
                           seed=args.seed)
    results["tfidf"] = evaluate_method(
        "TF-IDF cosine (Gian baseline)",
        lambda q: cosine_scores(tfidf, q), split, g, ks=KS, n_boot=args.n_boot,
        seed=args.seed)
    results["tfidf_nmf"] = evaluate_method(
        "TF-IDF+NMF cosine (Gian baseline, exact)",
        lambda q: cosine_scores(tfidf_nmf, q), split, g, ks=KS,
        n_boot=args.n_boot, seed=args.seed)

    # (b) word2vec mean-pool cosine -- Gian's baseline
    print("Building word2vec mean-pool (Gian baseline) ...", flush=True)
    w2v, _ = word2vec_matrix(texts, dim=100, min_df=5, seed=args.seed)
    results["word2vec"] = evaluate_method(
        "word2vec mean-pool cosine (Gian baseline)",
        lambda q: cosine_scores(w2v, q), split, g, ks=KS, n_boot=args.n_boot,
        seed=args.seed)

    # (d) graph / co-citation -- text-free
    print("Scoring graph / co-citation recommender ...", flush=True)
    results["graph"] = evaluate_method(
        "graph co-citation (bibliographic coupling)",
        lambda q: cocitation_scores(g, q), split, g, ks=KS, n_boot=args.n_boot,
        seed=args.seed)

    # (c) transformer cosine -- the neural fix (SPECTER, GPU)
    if not args.no_transformer:
        print("Building transformer embeddings (SPECTER, GPU/ROCm) ...",
              flush=True)
        t0 = time.time()
        work_ids = [r["work_id"] for r in records]
        tfm = transformer_matrix_by_id(work_ids, texts)
        print(f"  embedded {len(texts):,} works ({time.time()-t0:.0f}s), "
              f"dim={tfm.shape[1]}", flush=True)
        results["transformer"] = evaluate_method(
            "transformer cosine (SPECTER)",
            lambda q: cosine_scores(tfm, q), split, g, ks=KS,
            n_boot=args.n_boot, seed=args.seed)

    # ---- assemble report --------------------------------------------------- #
    def pack(r):
        return {
            "name": r.name,
            "n_queries": r.n_queries,
            "recall_at": {str(k): r.recall_at[k] for k in KS},
            "recall_at_ci": {str(k): list(r.recall_at_ci[k]) for k in KS},
            "map": r.map, "map_ci": list(r.map_ci),
            "mrr": r.mrr, "mrr_ci": list(r.mrr_ci),
        }

    report = {
        "task": "held-out citation prediction (mask {:.0%} of each query's "
                "in-sample references, rank all candidates, score the held-out "
                "set)".format(args.mask_frac),
        "sample_works": len(records),
        "eval_queries": int(len(split.query_idx)),
        "mask_frac": args.mask_frac,
        "min_refs": args.min_refs,
        "seed": args.seed,
        "ks": list(KS),
        "topic": args.topic,
        "transformer_model": (
            "sentence-transformers/allenai-specter (SPECTER, GPU/ROCm)"
            if not args.no_transformer else None),
        "methods": {k: pack(v) for k, v in results.items()},
    }

    # transformer vs TF-IDF significance (paired bootstrap on per-query MAP)
    if "transformer" in results:
        a = results["transformer"].per_query_ap
        b = results["tfidf"].per_query_ap
        report["transformer_vs_tfidf"] = {
            "delta_map": float(a.mean() - b.mean()),
            "rel_improvement_pct": float(
                100 * (a.mean() - b.mean()) / b.mean()) if b.mean() > 0 else None,
            "paired_bootstrap_p": paired_bootstrap_pvalue(
                a, b, n_boot=2000, seed=args.seed),
        }

    EVAL_OUT.parent.mkdir(parents=True, exist_ok=True)
    EVAL_OUT.write_text(json.dumps(report, indent=2))
    print(f"\nWrote {EVAL_OUT}\n", flush=True)

    # console table
    hdr = f"{'method':<42}{'R@10':>9}{'R@20':>9}{'MAP':>9}{'MRR':>9}"
    print(hdr); print("-" * len(hdr))
    for key in ("tfidf", "tfidf_nmf", "word2vec", "graph", "transformer"):
        if key not in results:
            continue
        r = results[key]
        print(f"{r.name[:41]:<42}{r.recall_at[10]:>9.3f}"
              f"{r.recall_at[20]:>9.3f}{r.map:>9.3f}{r.mrr:>9.3f}")
    if "transformer_vs_tfidf" in report:
        tv = report["transformer_vs_tfidf"]
        print(f"\ntransformer vs TF-IDF: dMAP={tv['delta_map']:+.3f} "
              f"({tv['rel_improvement_pct']:+.1f}%), "
              f"paired-bootstrap p={tv['paired_bootstrap_p']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
