#!/usr/bin/env python3
"""Build the committable ranking sample + manifest (heavy parquet is gitignored).

Ships, under ``data/processed/sample/``:
  - ``ranking_corpus_sample.parquet`` -- a small (default 1000-work) abstract +
    refs slice of the corpus, so a fresh clone can run the whole pipeline on a
    real-but-tiny corpus without a network pull;
  - ``ranking_top100.parquet``       -- the top-100 works by PageRank from the
    full build (the headline ranked output, citeable).

Also writes ``data/processed/ranking/MANIFEST.json`` describing the full
(gitignored) datasets: path, row count, edge count, embedding model/dim, as_of.

Usage:
    python scripts/ranking_build_sample.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from atlas.connectors.base import DATA_PROCESSED  # noqa: E402

CORPUS = DATA_PROCESSED / "ranking" / "corpus.parquet"
RANKING = DATA_PROCESSED / "ranking" / "ranking.parquet"
SAMPLE_DIR = DATA_PROCESSED / "sample"
MANIFEST = DATA_PROCESSED / "ranking" / "MANIFEST.json"


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def main() -> int:
    if not CORPUS.exists():
        raise SystemExit(f"{CORPUS} not found -- run ranking_ingest_corpus.py")
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(CORPUS)
    df["refs"] = df["refs"].map(lambda x: list(x) if x is not None else [])

    # sample: works with abstracts + refs, deterministic
    has = df[df["abstract"].notna() & (df["refs"].map(len) >= 5)]
    sample = has.sample(n=min(1000, len(has)), random_state=0).reset_index(drop=True)
    sample_path = SAMPLE_DIR / "ranking_corpus_sample.parquet"
    sample.to_parquet(sample_path, index=False)
    print(f"Wrote {sample_path}  ({len(sample):,} works)")

    n_refs_total = int(df["refs"].map(len).sum())
    manifest = {
        "name": "research-atlas ranking corpus",
        "generated_at": now_iso(),
        "license": "MIT (code) / CC-BY-4.0 (data, via OpenAlex CC0)",
        "source": "OpenAlex",
        "subfield": "3106 (Nuclear & High Energy Physics)",
        "window": "2015-01-01..2024-12-31",
        "datasets": [
            {
                "name": "corpus",
                "path": "data/processed/ranking/corpus.parquet",
                "gitignored": True,
                "row_count": int(len(df)),
                "n_with_abstract": int(df["abstract"].notna().sum()),
                "out_references_total": n_refs_total,
                "columns": list(df.columns),
            },
        ],
        "embeddings": {
            "model": "nomic-embed-text (Ollama, local)",
            "dim": 768,
            "baselines": ["tfidf", "tfidf+nmf", "word2vec-meanpool (PPMI-SVD)"],
            "cache": "data/raw/ranking/embed_cache/ (gitignored)",
        },
        "samples": [
            {"name": "ranking_corpus_sample",
             "path": "data/processed/sample/ranking_corpus_sample.parquet",
             "row_count": int(len(sample)), "committed": True},
        ],
    }

    if RANKING.exists():
        rdf = pd.read_parquet(RANKING)
        top = rdf.sort_values("pagerank", ascending=False).head(100).reset_index(
            drop=True)
        top_path = SAMPLE_DIR / "ranking_top100.parquet"
        top.to_parquet(top_path, index=False)
        print(f"Wrote {top_path}  (top 100 by PageRank)")
        manifest["datasets"].append({
            "name": "ranking",
            "path": "data/processed/ranking/ranking.parquet",
            "gitignored": True,
            "row_count": int(len(rdf)),
            "columns": list(rdf.columns),
        })
        manifest["samples"].append({
            "name": "ranking_top100",
            "path": "data/processed/sample/ranking_top100.parquet",
            "row_count": int(len(top)), "committed": True})

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
