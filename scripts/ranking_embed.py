#!/usr/bin/env python3
"""Pre-compute transformer embeddings for the eval subset, per-work-id cached.

Transformer embeddings are the throughput bottleneck (Ollama on CPU here is
~0.5 docs/s; on Gian's GPU box this is far faster). To make the embedding step
resumable and reusable across eval runs, this script embeds a *selected* set of
works and caches each vector to ``data/raw/ranking/embed_cache/by_id/<wid>.npy``
keyed by the work id. A re-run skips ids already on disk -- so an interrupted
embed resumes, and the eval reads the same cache for free.

The eval (:mod:`scripts.ranking_evaluate`) selects the SAME dense subset by the
same seed/topic, so the per-id cache is hit directly.

Usage:
    # embed the densest topic's top-N works (the eval subset)
    python scripts/ranking_embed.py --topic T10048 --sample 3000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import time as _time  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

from atlas.connectors.base import DATA_PROCESSED, DATA_RAW  # noqa: E402


# nomic-embed-text has a bounded context (~2048 tokens). A long abstract can
# exceed it and return HTTP 500 "input length exceeds the context length", so we
# cap to a safe char budget and, on a context error, shrink and retry.
MAX_CHARS = 4000


def embed_one(sess, text, retries=5):
    """Embed one text via Ollama with retry/backoff + context-overflow shrink."""
    last = None
    budget = MAX_CHARS
    for attempt in range(retries):
        prompt = (text or "")[:budget] or " "
        try:
            resp = sess.post(OLLAMA_URL,
                             json={"model": MODEL, "prompt": prompt},
                             timeout=120)
            if resp.status_code == 200:
                return np.array(resp.json()["embedding"], dtype=np.float32)
            last = f"HTTP {resp.status_code}: {resp.text[:120]}"
            if "context length" in resp.text:
                budget = max(500, budget // 2)  # shrink and retry immediately
                continue
        except requests.RequestException as exc:
            last = str(exc)
        _time.sleep(1.0 * (2 ** attempt))  # gentle backoff; Ollama can be busy
    raise RuntimeError(f"embed failed after {retries} retries: {last}")

CORPUS = DATA_PROCESSED / "ranking" / "corpus.parquet"
BY_ID = DATA_RAW / "ranking" / "embed_cache" / "by_id"
OLLAMA_URL = "http://localhost:11434/api/embeddings"
MODEL = "nomic-embed-text"


def main() -> int:
    ap = argparse.ArgumentParser(description="Embed the eval subset (per-id cache).")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--topic", default="T10048")
    ap.add_argument("--sample", type=int, default=3000)
    ap.add_argument("--min-refs", type=int, default=5)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.ranking_evaluate import select_sample

    df = pd.read_parquet(args.corpus)
    df["refs"] = df["refs"].map(lambda x: list(x) if x is not None else [])
    sample = select_sample(df, args.sample, args.min_refs, seed=0,
                           topic=args.topic)
    print(f"Embedding {len(sample):,} works (topic {args.topic}) ...", flush=True)

    from atlas.ranking.embed import embed_texts_by_id

    work_ids = sample["work_id"].tolist()
    texts = sample["text"].tolist()
    already = sum(1 for w in work_ids if (BY_ID / f"{w}.npy").exists())
    t0 = time.time()
    done = embed_texts_by_id(work_ids, texts)  # batched, resumable
    dt = time.time() - t0
    rate = done / dt if dt > 0 else 0
    print(f"Done: {done} newly embedded ({rate:.2f}/s), {already} already cached "
          f"({dt:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
