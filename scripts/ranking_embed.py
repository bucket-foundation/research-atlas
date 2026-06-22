#!/usr/bin/env python3
"""Pre-compute transformer embeddings for the eval subset, per-work-id cached.

Default backend is **GPU sentence-transformers SPECTER**
(``sentence-transformers/allenai-specter`` -- a transformer trained on
scientific papers), which on the AMD RX 7700S (ROCm, ``HSA_OVERRIDE_GFX_VERSION
=11.0.0``) embeds at hundreds of docs/s, versus the abandoned CPU Ollama path
(~0.5 docs/s). Each vector is cached to
``data/raw/ranking/embed_cache/by_id/<wid>.npy`` keyed by the work id; a re-run
skips ids already on disk (resumable), and the eval reads the same cache.

The eval (:mod:`scripts.ranking_evaluate`) selects the SAME dense subset by the
same seed/topic, so the per-id cache is hit directly.

Usage:
    # embed the densest topic's top-N works (the eval subset), GPU SPECTER
    HSA_OVERRIDE_GFX_VERSION=11.0.0 python scripts/ranking_embed.py \
        --topic T10048 --sample 3000
    # legacy CPU Ollama path (slow, not recommended)
    python scripts/ranking_embed.py --backend ollama
"""

from __future__ import annotations

# Must set the ROCm override BEFORE torch is imported anywhere downstream.
import os  # noqa: E402
os.environ.setdefault("HSA_OVERRIDE_GFX_VERSION", "11.0.0")

import argparse  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from atlas.connectors.base import DATA_PROCESSED, DATA_RAW  # noqa: E402

CORPUS = DATA_PROCESSED / "ranking" / "corpus.parquet"
BY_ID = DATA_RAW / "ranking" / "embed_cache" / "by_id"


def main() -> int:
    ap = argparse.ArgumentParser(description="Embed the eval subset (per-id cache).")
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--topic", default="T10048")
    ap.add_argument("--sample", type=int, default=3000)
    ap.add_argument("--min-refs", type=int, default=5)
    ap.add_argument("--backend", default="st", choices=("st", "ollama"),
                    help="st = GPU sentence-transformers SPECTER (default); "
                         "ollama = legacy CPU path")
    ap.add_argument("--model", default=None,
                    help="override the embedding model name")
    args = ap.parse_args()

    from scripts.ranking_evaluate import select_sample

    df = pd.read_parquet(args.corpus)
    df["refs"] = df["refs"].map(lambda x: list(x) if x is not None else [])
    sample = select_sample(df, args.sample, args.min_refs, seed=0,
                           topic=args.topic)
    print(f"Embedding {len(sample):,} works (topic {args.topic}, "
          f"backend={args.backend}) ...", flush=True)

    from atlas.ranking.embed import embed_texts_by_id

    work_ids = sample["work_id"].tolist()
    texts = sample["text"].tolist()
    already = sum(1 for w in work_ids if (BY_ID / f"{w}.npy").exists())
    t0 = time.time()
    done = embed_texts_by_id(work_ids, texts, model=args.model,
                             backend=args.backend)  # batched, resumable
    dt = time.time() - t0
    rate = done / dt if dt > 0 else 0
    print(f"Done: {done} newly embedded ({rate:.2f} docs/s), {already} already "
          f"cached ({dt:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
