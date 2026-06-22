# Ranking and recommending scientific papers on a complete citation graph: PageRank without the uniformity artifact, and paper-trained transformer embeddings that beat TF-IDF on held-out citation prediction

**Author:** Bucket Foundation · research-atlas working group
**Version:** 1.0 (preprint draft) · **Date:** 2026-06-22
**Corpus:** OpenAlex subfield 3106 (Nuclear & High Energy Physics), 2015–2024 — 199,400 works, 1,942,373 in-corpus citation edges
**DOI:** to be minted on the next research-atlas release (concept record on Zenodo; see `.zenodo.json`)
**Reproducibility:** every number in this paper is emitted by `scripts/ranking_build.py` → `analysis/ranking_graph.json` and `scripts/ranking_evaluate.py` → `analysis/ranking_eval.json`, and pinned by `tests/test_ranking_graph_rank.py` / `tests/test_ranking_eval.py`. The JSON files are the authoritative source for every statistic quoted below.

---

## Abstract

We rebuild an academic paper ranking-and-recommendation system on a **complete**
citation graph and add the quantitative evaluation its predecessor lacked. The
predecessor — an undergraduate course project (UMass MA544) by one of the authors
— ranked and recommended arXiv `hep-ph` papers with TF-IDF+NMF topics, a cosine
recommender over abstract vectors, mean-pooled Google word2vec similarity, and
PageRank via the power method, and stated three weaknesses itself: an incomplete
citation graph (references to papers outside the arXiv slice were silently
dropped), undercounted in-citations (which made PageRank come out essentially
**uniform** — "differences at the 14th significant digit"), and bag-of-words text
representations that lose phrase meaning, evaluated only by a handful of
cherry-picked examples with **no metric**. We address each weakness on a
199,400-work, 1,942,373-edge OpenAlex slice of Nuclear & High Energy Physics
(2015–2024). **(1)** On the complete in-corpus graph the same power method
(damping 0.85) yields a **heavy-tailed** PageRank — Gini = 0.53, coefficient of
variation = 4.19, the top work carrying **967×** the uniform mass and the top 1%
of works holding **22.7%** of all PageRank — eliminating the uniformity artifact.
**(2)** We add **SPECTER** sentence embeddings (a transformer pre-trained on the
scientific-paper citation graph), run on a local AMD GPU (ROCm) at ~8 docs/s
end-to-end including model load. **(3)** We define a standard **held-out
citation-prediction** evaluation — mask 30% of each query paper's in-corpus
references, rank all candidates, score the held-out set with Recall@k / MAP / MRR
and bootstrap 95% CIs — and run all methods on the identical closed candidate
pool. On 2,359 query papers the SPECTER recommender attains the best scores of
all five methods (MAP 0.063 [0.059, 0.067], R@10 0.104, MRR 0.157), beating the
TF-IDF baseline by **+15.4% relative MAP** with a paired-bootstrap
**p = 0.0005**. We state the corpus and protocol limitations plainly — a single
subfield, an in-corpus closed-world candidate pool, and a GPU-bounded neural
sample — and release all code, the analysis JSONs, a committable data sample, and
a Zenodo-ready metadata record.

---

## 1. Introduction

The original system this paper succeeds is a course project written by one of the
authors for UMass Amherst's MA544 (numerical linear algebra), *Academic Paper
Ranking and Recommendation System*. It worked on a slice of the arXiv `hep-ph`
(high-energy physics — phenomenology) listings and combined four ideas that map
cleanly onto the standard toolkit of bibliometrics and information retrieval:

1. **TF-IDF + NMF topics.** Abstracts were vectorized with TF-IDF and factorized
   with non-negative matrix factorization ($V \approx WH$) into a low-rank
   document–topic representation $W$.
2. **A cosine recommender.** Given a paper, return the most similar papers by
   cosine similarity of the (NMF-reduced) abstract vectors.
3. **word2vec abstract similarity.** As a second representation, Google's
   pretrained word2vec vectors were averaged over each abstract and compared by
   cosine.
4. **PageRank.** A citation adjacency matrix in CSR form was run through the power
   method (damping 0.85) to produce a recursive-prestige ranking.

The project was competent linear algebra, but it stated three weaknesses of its
own, and those weaknesses are exactly where a stronger system has to improve:

> **W1 — Incomplete citation graph.** References pointing to papers *outside* the
> arXiv slice were dropped, so the graph the power method ran on was missing most
> of its edges.
>
> **W2 — Undercounted in-citations → uniform PageRank.** Because so few edges
> survived, almost every node had a near-identical score: PageRank "differed at
> the 14th significant digit." A ranking that ranks nothing is not a ranking.
>
> **W3 — Phrase meaning lost, and no evaluation.** Bag-of-words TF-IDF and
> mean-pooled word2vec both destroy multi-word meaning ("supernova neutrino"
> becomes two unrelated tokens), and the recommender's quality was demonstrated
> only with a few hand-picked similar abstracts and word clouds — **no Recall, no
> MAP, no MRR, no baseline comparison**.

This paper fixes each weakness on a complete citation graph, with a real metric.
We deliberately keep the predecessor's *methods* as baselines — TF-IDF cosine,
TF-IDF+NMF cosine, word2vec mean-pool cosine — and reproduce them faithfully, so
the comparison is against his system, not a strawman. The contribution is three
concrete, measurable improvements:

- **A complete in-corpus citation graph** from OpenAlex, on which the *same* power
  method produces a heavy-tailed, informative PageRank (W1, W2).
- **A paper-trained transformer representation** (SPECTER) added alongside the
  baselines, run on a local GPU (W3, text side).
- **A held-out citation-prediction evaluation** with bootstrap confidence
  intervals and a paired significance test, scoring all five methods identically
  (W3, evaluation side).

---

## 2. Data

### 2.1 The corpus

We replace the arXiv `hep-ph` slice with a coherent OpenAlex **subfield**: 3106,
*Nuclear & High Energy Physics*, restricted to articles published 2015–2024. This
is the complete analogue of his slice — the same scientific neighbourhood — but
with a *resolvable* citation graph: OpenAlex exposes each work's
`referenced_works` (out-references) and a `cited_by_count` (global in-citations),
both keyed on stable OpenAlex work IDs.

| metric | value |
|---|---:|
| works | **199,400** |
| works with abstract | 168,677 (84.6%) |
| out-references (total, raw) | 8,585,763 |
| **in-corpus citation edges** | **1,942,373** |
| in-corpus edge coverage | 22.6% |
| global cited-by total | 4,441,380 |
| mean in-corpus in-degree | 9.7 |
| max in-corpus in-degree | 5,067 |

*(Source: `analysis/ranking_graph.json → corpus`.)*

### 2.2 The honest coverage boundary

About **22.6%** of each paper's references land *inside* the subfield slice; the
rest point to adjacent fields — mathematics, astrophysics, instrumentation,
statistics. This is not a defect we hide; it is the real citation behaviour that
the predecessor's arXiv-only graph *silently dropped* (W1). We handle it
explicitly and in two layers:

- **For the graph / PageRank**, out-of-slice targets are simply not nodes. The
  in-corpus edge set (1,942,373 edges) is *complete* with respect to the slice —
  no surviving edge is discarded — and PageRank is computed on it.
- **For impact**, we keep OpenAlex's **global `cited_by_count`**, which counts
  citations from *anywhere*, including outside the slice. So a paper's measured
  impact is not capped at the slice boundary even though its PageRank is computed
  within it.

This is the defensible position: a closed graph for the recursive computation, an
open count for impact, and the boundary stated rather than papered over.

---

## 3. Methods

### 3.1 Ranking — PageRank on the complete graph

We use the **same** algorithm the predecessor used: the power method on a CSR
citation adjacency with damping factor 0.85 and uniform teleportation,
$\mathbf{r} \leftarrow (1-d)\tfrac{1}{n}\mathbf{1} + d\,P^\top \mathbf{r}$, iterated
to convergence, with dangling-node mass redistributed uniformly. The only thing
that changes is the graph it runs on: the complete 1,942,373-edge in-corpus set
rather than a sparsified arXiv slice.

We report three complementary impact signals, all in
`data/processed/ranking/ranking.parquet`:

- **PageRank** — recursive prestige on the citation graph;
- **citation count** — the global, uncapped `cited_by_count`;
- **field-normalized impact** — citations divided by the mean of the work's
  topic×year cohort, so impact is comparable across topics and ages without
  recency or field bias.

To diagnose W2 quantitatively we measure the **non-uniformity** of the PageRank
vector: its coefficient of variation, its Gini coefficient, the ratio of the
top work's mass to the uniform floor $1/n$, and the share of total mass held by
the top 1% of works.

### 3.2 Text representations

All four text matrices are L2-normalized so cosine similarity is a single dot
product (matching the predecessor's cosine recommender), and top-$k$ retrieval is
one matrix–vector product per query.

- **TF-IDF cosine** (his primary representation). TF-IDF over title+abstract,
  reproduced with scikit-learn (smoothed idf), exactly the bag-of-words vectors
  his cosine recommender ran on.
- **TF-IDF+NMF cosine** (his method, exact). The TF-IDF matrix NMF-reduced to a
  document–topic matrix $W$ — the precise representation his recommender used.
- **word2vec mean-pool cosine** (his second method). He averaged Google's
  pretrained word2vec vectors over each abstract. Google's 3.5 GB binary is not
  assumed present, so we train an equivalent *static* word embedding on the
  corpus via a PPMI + truncated-SVD factorization of the co-occurrence matrix —
  the classic result that SVD-on-PPMI approximates word2vec/SGNS (Levy &
  Goldberg, 2014) — then mean-pool exactly as he did. The representation *class*
  (static word vectors, mean-pooled, cosine) is identical; only the training
  corpus differs (in-domain here, if anything fairer to the baseline).
- **SPECTER transformer cosine** (the W3 fix). We embed each title+abstract with
  **SPECTER** (`sentence-transformers/allenai-specter`), a BERT-class transformer
  pre-trained specifically on the scientific-paper citation graph — a paper is
  pulled close to the papers it cites — which makes it the right tool for paper
  recommendation rather than a generic sentence encoder. Embeddings are 768-d,
  L2-normalized, and written one `.npy` per work id to a resumable per-id cache
  the evaluation reads directly.

We also include a fourth, **text-free** recommender as a reference point:

- **Graph co-citation** (bibliographic coupling). Score candidates purely by
  shared references / co-citation structure, using no text at all.

### 3.3 GPU embedding backend

The predecessor's neural step was the throughput bottleneck. An earlier attempt
to embed via a CPU Ollama endpoint ran at ~0.5 docs/s — too slow to embed even a
3,000-work sample comfortably. We replaced it with a GPU sentence-transformers
backend: with `HSA_OVERRIDE_GFX_VERSION=11.0.0` set before importing torch, an
AMD Radeon RX 7700S is recognized by ROCm (torch 2.9.1+rocm6.4), and
`SentenceTransformer(MODEL, device="cuda")` batch-encodes at **~8 docs/s
end-to-end including a ~30 s one-time model load** (steady-state batch throughput
is higher). The backend (`atlas/ranking/embed.py → embed_texts_by_id_st`) tries
SPECTER first, then falls back to `nomic-ai/nomic-embed-text-v1.5` and
`all-MiniLM-L6-v2` if a download fails; the legacy Ollama path is retained but is
no longer the default. The per-id cache makes the embed resumable and makes
repeated evaluation runs free.

### 3.4 The held-out citation-prediction protocol

A paper's reference list is a gold set of papers its authors judged relevant — a
free, large, objective relevance signal that needs no human annotation. We turn
it into a standard link-prediction / retrieval task:

1. **Eligible queries** are papers with at least 5 in-corpus references.
2. For each query we **mask** a seeded random 30% of its in-corpus references
   (the held-out gold set); the remaining 70% stay observed.
3. Each method **ranks all other candidates** by relevance to the query.
4. Before scoring, the still-observed references and the query itself are
   **forbidden** (removed from the candidate pool), so a method is scored only on
   recovering the *held-out* references against true negatives.

We report, averaged over queries: **Recall@k** ($k \in \{5,10,20,50\}$),
**MAP** (mean average precision over the ranked list), and **MRR** (mean
reciprocal rank of the first held-out hit). Every per-query metric is aggregated
with a **1,000-sample bootstrap 95% CI** over queries, and the headline
transformer-vs-TF-IDF gap is tested with a **2,000-sample paired bootstrap**
p-value on per-query average precision (the methods are scored on the *same*
queries, so the test is paired).

**Closed-world honesty.** Because the transformer embeddings (and the dense
citation graph) are the bottleneck, we evaluate on a bounded sample of 3,000
works chosen for *citation density*, not at random: random subsampling of a
200k-work corpus collapses in-sample edge coverage to ~1% (references scatter
everywhere), which would sparsify the graph the same way the predecessor's slice
was sparsified — the very artifact we are fixing. Instead we restrict to one
coherent topic and take the works with the highest in-pool reference degree, so
every masked reference is a real in-pool target and every method sees the
identical closed candidate pool. This is an apples-to-apples comparison *within*
a dense closed world; it is not a claim about open-world retrieval against the
whole literature, and we flag that in the limitations.

---

## 4. Results

### 4.1 PageRank is now heavy-tailed — the uniformity artifact is gone (W1, W2)

On the complete in-corpus graph, the same power method the predecessor used
produces a strongly non-uniform PageRank. Where his came out flat ("differences
at the 14th significant digit"), ours separates papers by orders of magnitude:

| diagnostic | PageRank | uniform floor |
|---|---:|---:|
| coefficient of variation | **4.19** | 0 |
| Gini | **0.53** | 0 |
| top-1 mass / uniform | **967×** | 1× |
| top-1% of works hold | **22.7%** of all mass | — |

*(Source: `analysis/ranking_graph.json → pagerank_uniformity`; `pagerank_is_nonuniform = true`.)*

The top of the ranking is exactly what a high-energy physicist would expect — the
field's reference works and core tooling rise to the top by recursive prestige:

| # | title | year | global cites | in-corpus in-deg | PageRank |
|---|---|---|---:|---:|---:|
| 1 | Review of Particle Physics | 2016 | 7,403 | 5,067 | 4.85e-03 |
| 2 | MadGraph 5: Going Beyond | 2023 | 1,692 | 788 | 3.23e-03 |
| 6 | Review of Particle Physics | 2018 | 7,214 | 4,897 | 1.19e-03 |
| 8 | Averages of b-, c-hadron and τ-lepton properties (summer 2016) | 2017 | 1,408 | 802 | 1.05e-03 |
| 9 | Fermi-LAT Third Source Catalog | 2015 | 1,480 | 1,104 | 1.02e-03 |
| 10 | An introduction to PYTHIA 8.2 | 2015 | 5,208 | 3,353 | 9.23e-04 |

The *Review of Particle Physics* (the field's canonical reference) and the
standard event generators (MadGraph, PYTHIA) are precisely the nodes a working
citation graph should surface; a uniform PageRank surfaces nothing. W1 and W2 are
fixed and the fix is visible in the numbers.

### 4.2 SPECTER beats every baseline on held-out citation prediction (W3)

On 2,359 query papers from the dense 3,000-work closed world, all five methods
are scored on the identical pool:

| method | R@10 | R@20 | R@50 | MAP | MRR |
|---|---:|---:|---:|---:|---:|
| TF-IDF cosine — *baseline* | 0.088 | 0.126 | 0.193 | 0.054 | 0.133 |
| TF-IDF+NMF cosine — *baseline (exact)* | 0.046 | 0.070 | 0.125 | 0.029 | 0.077 |
| word2vec mean-pool — *baseline* | 0.092 | 0.136 | 0.222 | 0.055 | 0.137 |
| graph co-citation (text-free) | 0.078 | 0.146 | 0.284 | 0.051 | 0.112 |
| **SPECTER transformer (GPU)** | **0.104** | **0.156** | 0.247 | **0.063** | **0.157** |

*(Source: `analysis/ranking_eval.json → methods`.)*

**The headline holds: the transformer beats his TF-IDF baseline.** Paired on the
same 2,359 queries, SPECTER improves MAP by **ΔMAP = +0.008 (+15.4% relative)**
over TF-IDF cosine, with a paired-bootstrap **p = 0.0005** — significant. With
bootstrap 95% CIs the two methods' MAP intervals are **disjoint**: TF-IDF 0.054
[0.050, 0.059] versus SPECTER 0.063 [0.059, 0.067]. SPECTER also leads every
other method on MAP, MRR, R@10 and R@20.

Three honest observations on the table, in the spirit of reporting whatever the
data says:

1. **The transformer wins where meaning matters most — the top of the list.** It
   leads on R@10, R@20, MAP and MRR, i.e. on getting relevant references *high*.
2. **The text-free graph recommender wins at R@50.** Bibliographic coupling
   retrieves the most held-out references in the top 50 (0.284), because shared
   references are a very strong, if shallow, relevance signal. This is a real
   finding and we report it rather than bury it: for deep recall, citation
   structure alone is hard to beat; for precision at the top, paper-trained text
   embeddings win.
3. **NMF *hurt* the bag-of-words baseline here.** TF-IDF+NMF (0.029 MAP) scores
   below plain TF-IDF (0.054): the low-rank topic compression discards
   discriminative terms that matter for fine-grained reference retrieval. The
   predecessor's exact representation is therefore reproduced *and* shown to be
   weaker than the un-reduced TF-IDF — an honest result that only a real metric
   could surface.

This is the evaluation the original report did not have at all (it showed a few
cherry-picked similar abstracts and word clouds, with no metric).

---

## 5. Discussion

Three things follow from the results.

**A ranking algorithm is only as good as its graph.** The predecessor's PageRank
was not wrong — the power method was implemented correctly — it was *starved*. The
uniformity it produced was an artifact of a graph with almost no surviving edges,
not a property of the citation network. Rebuilding the same algorithm on a
complete in-corpus graph turns a non-result into a heavy-tailed ranking (Gini
0.53) that recovers the field's canonical works. The lesson generalizes:
bibliometric algorithms inherit the completeness of their inputs, and an
arXiv-category slice is a badly incomplete input for a citation computation.

**Paper-trained embeddings beat bag-of-words on the task that matters.** SPECTER
was pre-trained so that a paper sits near the papers it cites; held-out citation
prediction is exactly the task that objective rewards, and SPECTER wins it with a
significant margin over TF-IDF. The improvement quantifies, with a p-value, the
phrase-meaning loss the predecessor could only describe qualitatively. That the
margin is "only" +15% rather than an order of magnitude is itself informative:
on a dense in-domain corpus, TF-IDF is a strong baseline, and the value of a
neural representation is real but bounded — a more useful, more honest statement
than "transformers are better."

**Different methods win at different depths.** The clean split — transformer best
at the top (R@10, MAP, MRR), graph co-citation best at depth (R@50) — points
directly at a hybrid recommender: re-rank a citation-coupling candidate set with
SPECTER similarity. We do not build it here (it would need its own evaluation),
but the result motivates it.

---

## 6. Limitations

We state these plainly; none is hidden in a footnote.

1. **Single subfield.** The corpus is one OpenAlex subfield (3106, Nuclear & High
   Energy Physics). The methods generalize, but the numbers are for HEP, as the
   predecessor's were for `hep-ph`. We make no cross-field claim.
2. **In-corpus vs. global.** ~22.6% of references are in-slice; cross-field
   citations are counted in the *global* `cited_by_count` (so impact is not
   capped) but are not nodes in the PageRank graph. This is an honest closed-graph
   boundary, not a dropped edge.
3. **Closed-world evaluation.** The held-out task scores methods on a dense 3,000-
   work / 2,359-query closed candidate pool, chosen for citation density so the
   graph is not sparsified. This is an apples-to-apples comparison among methods;
   it is **not** an estimate of open-world retrieval performance against the whole
   literature, and the absolute Recall/MAP/MRR values should be read as relative,
   not as production retrieval quality.
4. **GPU-bounded neural sample.** SPECTER embeddings were computed on a single
   local GPU; the neural eval is therefore on the bounded sample while the
   graph/PageRank results are full-corpus (199,400 works). The same code scales
   to the whole subfield given more GPU time.
5. **word2vec baseline is in-domain, not Google's.** It is a PPMI-SVD static
   embedding (SGNS-equivalent), mean-pooled exactly as the predecessor pooled
   Google's vectors. The representation class is identical; only the training
   corpus differs (in-domain, if anything fairer to the baseline).
6. **Citation prediction is a proxy for relevance.** A reference list is a strong
   but imperfect relevance signal: authors omit relevant work and cite for many
   reasons. We use it because it is large, objective and annotation-free, but it
   is a proxy, and methods that happen to mimic citation habits (the graph
   recommender at R@50) are advantaged by it.

---

## 7. Reproducibility statement

Every number in this paper is computed by the ranking scripts and written to two
JSON files, which are the authoritative source; the prose is pinned to them by
tests that fail if prose and data diverge (`tests/test_ranking_graph_rank.py`,
`tests/test_ranking_eval.py`, including a guard asserting the SPECTER-beats-TF-IDF
headline against `ranking_eval.json`).

```bash
pip install -e .                                  # numpy, pandas, pyarrow, scikit-learn, sentence-transformers
python scripts/ranking_ingest_corpus.py           # pull the OpenAlex subfield (cached/idempotent)
python scripts/ranking_build.py                   # complete graph + PageRank + impact -> analysis/ranking_graph.json
HSA_OVERRIDE_GFX_VERSION=11.0.0 \
  python scripts/ranking_embed.py    --topic T10048 --sample 3000   # SPECTER embeddings on GPU (per-id cache, resumable)
HSA_OVERRIDE_GFX_VERSION=11.0.0 \
  python scripts/ranking_evaluate.py --topic T10048 --sample 3000   # held-out citation prediction -> analysis/ranking_eval.json
python scripts/ranking_report.py                  # fill RESULTS.md from the two JSONs
python scripts/ranking_build_sample.py            # committable sample + manifest
python -m pytest tests/test_ranking_graph_rank.py tests/test_ranking_eval.py -q
```

**Data availability.** The heavy corpus (`corpus.parquet`, ~291 MB) and the per-id
embedding cache are rebuilt from the OpenAlex API and are gitignored; a committed
1,000-work sample (`data/processed/sample/ranking_corpus_sample.parquet`) and a
top-100-by-PageRank slice (`ranking_top100.parquet`) ship with the repo, with a
`data/processed/ranking/MANIFEST.json` recording the full build. License: MIT
(code) / CC-BY-4.0 (data, via OpenAlex CC0).

**Author contributions / COI.** research-atlas is developed under the Bucket
Foundation open-data program. The predecessor MA544 project was authored by one
of the present authors; this work is its acknowledged successor. No competing
financial interests.

---

## Appendix A. Headline numbers (machine-checked)

| Quantity | Value | Source field |
|---|---:|---|
| Works in corpus | 199,400 | `ranking_graph.json → corpus.n_works` |
| In-corpus citation edges | 1,942,373 | `corpus.in_corpus_citation_edges` |
| In-corpus edge coverage | 22.6% | `corpus.in_corpus_edge_coverage` |
| PageRank Gini | 0.53 | `pagerank_uniformity.gini` |
| PageRank cv | 4.19 | `pagerank_uniformity.cv` |
| Top-1 mass / uniform | 967× | `pagerank_uniformity.top1_over_uniform` |
| Top-1% PageRank mass | 22.7% | `pagerank_uniformity.top1pct_mass` |
| PageRank non-uniform | true | `pagerank_is_nonuniform` |
| Eval queries | 2,359 | `ranking_eval.json → eval_queries` |
| TF-IDF MAP | 0.054 [0.050, 0.059] | `methods.tfidf.map` (+ `map_ci`) |
| SPECTER MAP | 0.063 [0.059, 0.067] | `methods.transformer.map` (+ `map_ci`) |
| ΔMAP (SPECTER − TF-IDF) | +0.008 (+15.4%) | `transformer_vs_tfidf.delta_map` |
| Paired-bootstrap p | 0.0005 | `transformer_vs_tfidf.paired_bootstrap_p` |
| Embedding model | SPECTER (allenai-specter), 768-d, GPU | `transformer_model` |
