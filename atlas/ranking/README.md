# `atlas.ranking` — complete-citation-graph paper ranking & recommendation

A stronger successor to Gian Dichio's *Academic Paper Ranking and Recommendation
System* (arXiv-only). It fixes the three weaknesses he stated in his own
write-up and adds the quantitative evaluation he never had.

## What it does

| stage | module | what |
|-------|--------|------|
| corpus | `corpus.py` | pull a coherent OpenAlex subfield (default 3106 = Nuclear & High-Energy Physics, 2015–2024) with out-references **and** global cited-by **and** abstracts; cached, idempotent |
| graph | `graph.py` | build the **complete in-corpus** citation graph as CSR (the edges Gian dropped are now resolvable; in-citations no longer undercounted) |
| rank | `rank.py` | PageRank (power method, like his), raw citation count (global), field-normalized impact; `uniformity()` proves PageRank is now heavy-tailed |
| embed | `embed.py` | **transformer** embeddings (Ollama `nomic-embed-text`, 768-d) + his **TF-IDF**, **TF-IDF+NMF**, and **word2vec mean-pool** baselines |
| recommend | `recommend.py` | cosine-kNN over each representation + a text-free graph co-citation recommender |
| evaluate | `evaluate.py` | **held-out citation prediction**: mask refs, rank candidates, score Recall@k / MAP / MRR with bootstrap 95% CIs + a paired significance test |
| **crossfield** | `crossfield.py` | scale the whole pipeline to **all 26 OpenAlex top-level fields**, impact-ranked, **checkpointed + resumable**, with ingestion (network) running **concurrent** with analysis (GPU). Per-checkpoint per-field PageRank/Gini + the eval, plus cross-field generalization (does SPECTER beat TF-IDF in *every* field?), Gini-by-field, and interdisciplinarity |

## Cross-field generalization study (`crossfield.py` + `scripts/crossfield_run.py`)

The single-subfield study proves the pattern on HEP; the cross-field orchestrator
asks whether it **generalizes**. It is checkpointed, resumable, and grows the
corpus by tranches (e.g. 3k → 5k → 20k → 50k works/field). Ingestion is concurrent
with analysis (a producer thread downloads the next field while the GPU embeds the
ready one), every checkpoint writes a durable
`analysis/crossfield/checkpoint_<N>.json` + updates `manifest.json` +
`convergence.jsonl`, and a SIGINT/crash mid-run resumes cleanly from the per-field
partial + the raw-page / per-id embedding caches.

```bash
# run the NEXT checkpoint (first run = checkpoint 1, top ~3k works/field)
HSA_OVERRIDE_GFX_VERSION=11.0.0 python scripts/crossfield_run.py
# advance the loop: re-run grows the corpus to the next tranche
HSA_OVERRIDE_GFX_VERSION=11.0.0 python scripts/crossfield_run.py
# measure GPU embed throughput on the current cache
HSA_OVERRIDE_GFX_VERSION=11.0.0 python scripts/crossfield_run.py --measure-throughput
```

## The three fixes (each maps to a weakness he listed)

1. **Incomplete graph** → complete in-corpus edge set from global OpenAlex ids.
2. **In-citations undercounted → uniform PageRank** → complete graph + global
   `cited_by_count`; PageRank is now non-uniform (see `uniformity()`).
3. **TF-IDF/word2vec lose phrase meaning; no eval** → transformer embeddings +
   a real held-out citation-prediction evaluation with CIs.

## Run it

```bash
python scripts/ranking_ingest_corpus.py        # corpus  (idempotent, cached)
python scripts/ranking_build.py                # graph + PageRank + impact
python scripts/ranking_embed.py --topic T10048 --sample 3000   # transformer (resumable per-id cache)
python scripts/ranking_evaluate.py --topic T10048 --sample 3000  # the eval table
python scripts/ranking_report.py               # fill docs/papers/02-paper-ranking/RESULTS.md
python scripts/ranking_build_sample.py         # committable sample + manifest
pytest tests/test_ranking_graph_rank.py tests/test_ranking_eval.py
```

Heavy artifacts (`data/processed/ranking/*.parquet`, `data/raw/ranking/`) are
gitignored; a 1000-work sample, the top-100-by-PageRank slice, the manifest, and
the two analysis JSONs are committed.
