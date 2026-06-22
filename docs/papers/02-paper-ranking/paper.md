# Does transformer paper-recommendation generalize across fields? A checkpointed, all-26-field test of SPECTER vs TF-IDF on held-out citation prediction

**Author:** Bucket Foundation · research-atlas working group
**Version:** 2.0 (cross-field preprint draft) · **Date:** 2026-06-22
**Corpus:** OpenAlex, all 26 top-level fields, impact-ranked (most-cited first), 2015–2024 — checkpoint 1 = top ~3,000 works/field (78,000 works), grown by a checkpoint loop
**DOI:** to be minted on the next research-atlas release (concept record on Zenodo; see `.zenodo.json`)
**Reproducibility:** every number in this paper is emitted by `scripts/crossfield_run.py` → `analysis/crossfield/checkpoint_<N>.json` (+ `manifest.json` + `convergence.jsonl`), summarized by `scripts/crossfield_report.py`, and pinned by `tests/test_crossfield.py`. The checkpoint JSON is the authoritative source for every statistic quoted below; the paper reads from **checkpoint 1**.

---

## Abstract

A companion single-subfield study showed that **SPECTER** (a transformer
pre-trained on the scientific-paper citation graph) beats a TF-IDF baseline at
held-out citation prediction in High-Energy Physics (+15.4% relative MAP,
p = 0.0005). The natural question — and the one practitioners actually face when
they reach for a neural paper-recommender — is whether that advantage
**generalizes across fields**, or whether it is a property of one citation-dense
subfield. We answer it directly. We built a **checkpointed, resumable,
producer/consumer** pipeline that pulls an **impact-ranked corpus** (most-cited
papers first) across **all 26 OpenAlex top-level fields**, builds a complete
in-corpus citation graph and PageRank per field, embeds title+abstract on a local
AMD GPU (ROCm) at a measured **9.1 docs/s steady-state**, and runs the identical
held-out citation-prediction evaluation — SPECTER vs TF-IDF vs word2vec vs a
text-free graph recommender, with bootstrap CIs and a paired test — in **every**
field. At checkpoint 1 (top ~3,000 works/field, **78,000 works**), the verdict is
nuanced and we report it honestly: **SPECTER beats TF-IDF on MAP in 16 of 26
fields**, but the across-field effect is **borderline** — combined field-level
mean ΔMAP = **+0.0095** (95% CI **[−0.0005, +0.0195]**, one-sample bootstrap
p = **0.0624**); the sign test on 16/26 wins is not significant (p = 0.33). The
advantage is real and large where text carries fine-grained meaning
(Neuroscience +0.063 p = 0.004; Computer Science +0.060 p < 0.001; Social
Sciences, Psychology), and **reverses** in several physical-science / pharmacology
fields (Pharmacology −0.034 p = 0.001; Energy −0.014 p = 0.001; Earth Sciences
−0.031 p = 0.021). Citation concentration varies by field (Gini
**0.235–0.538**) and so does interdisciplinarity (cross-field reference fraction
**0.114–0.468**). The headline: **transformer paper-recommendation does not
robustly generalize across all fields — it is field-dependent, winning a majority
but not all, with no significant aggregate edge.** The corpus grows by re-running
the loop (the next tranche is top 5k, then 20k, then 50k works/field); every
checkpoint is durable and the run resumes cleanly after interruption.

---

## 1. Introduction

The predecessor of this line of work is a course project (UMass MA544) that
ranked and recommended arXiv `hep-ph` papers with TF-IDF+NMF topics, a cosine
recommender, mean-pooled word2vec, and PageRank. A first paper (companion;
`RESULTS.md`) rebuilt it on a **complete** OpenAlex citation graph for one
subfield (Nuclear & High-Energy Physics), fixed the PageRank-uniformity artifact
(Gini 0.53 instead of "differences at the 14th significant digit"), and added the
quantitative evaluation it lacked: on 2,359 HEP queries, SPECTER beat TF-IDF on
held-out citation prediction by **+15.4% relative MAP** with a paired-bootstrap
**p = 0.0005**.

A single-subfield win is suggestive, not conclusive. SPECTER is trained on the
citation graph; HEP is citation-dense and textually distinctive; both could
flatter the transformer. The scientifically interesting — and practically
decisive — question is **generalization**:

> Does a paper-trained transformer recommender beat a TF-IDF baseline at held-out
> citation prediction **in every field**, or only in some? If only some, which,
> and is there a net advantage across the literature as a whole?

This paper answers that question with a real, all-26-field measurement, and
reports the answer whether or not it is the convenient one. The contribution is
three-fold:

1. **A method**: a checkpointed, resumable, ingestion-concurrent-with-analysis
   orchestrator (`atlas/ranking/crossfield.py`) that scales the proven
   single-subfield pipeline to all 26 OpenAlex top-level fields, impact-ranked,
   growing the corpus in durable tranches.
2. **A measurement**: the per-field held-out citation-prediction eval (SPECTER vs
   TF-IDF vs word2vec vs graph) in all 26 fields at checkpoint 1, with bootstrap
   CIs and a paired test per field plus a combined field-level test.
3. **An honest finding**: SPECTER wins a **majority** of fields but **not all**,
   and the **aggregate** advantage is **not significant** — transformer
   paper-recommendation is **field-dependent**, not a universal win.

---

## 2. Data

### 2.1 Impact-ranked corpus across all 26 fields

For each OpenAlex top-level field (`primary_topic.field.id:fields/<id>`,
articles, 2015–2024) we pull works **ordered by global `cited_by_count`
descending** — the most impactful papers first — with each work's
`referenced_works` (out-edges) and `cited_by_count` (global impact) and abstract.
Impact-ranked ingestion is deliberate: it surfaces each field's canonical core
before its long tail, so even a modest tranche is the part of the field that most
recommendation traffic actually concerns. The corpus grows in **checkpoint
tranches** (top 3k → 5k → 20k → 50k works/field, configurable); checkpoint 1 is
the top ~3,000 most-cited works in every field.

| metric (checkpoint 1) | value |
|---|---:|
| fields | **26** |
| works (top ~3k/field) | **78,000** |
| publication window | 2015–2024 |
| GPU embedding model | SPECTER (`allenai-specter`), 768-d |
| GPU steady-state throughput | **9.1 docs/s** (AMD RX 7700S / ROCm) |
| wall-clock (checkpoint 1) | ~52 min |

*(Source: `analysis/crossfield/checkpoint_1.json`.)*

Raw OpenAlex pages are cached per (field, page-index); the per-id SPECTER
embeddings are cached one `.npy` per work. Both caches make the run **resumable**:
a re-run resumes from disk, and a larger tranche fetches only the new pages.

### 2.2 The honest coverage boundary, per field

As in the single-subfield study, the in-corpus citation graph is **complete by
construction within each field's loaded slice** (no surviving edge dropped), while
**global `cited_by_count`** carries impact from outside the slice. Because the
corpus is impact-ranked and bounded per field, in-corpus edge density is lower
than in the deep HEP slice — this is expected and is exactly why we (a) report it
explicitly and (b) **grow** the corpus via the checkpoint loop: density rises with
each tranche, and the convergence log tracks how the headline numbers move as it
does.

---

## 3. Methods

### 3.1 The checkpointed, resumable, concurrent orchestrator

The engineering is part of the contribution, because a 26-field neural study on
one GPU must survive interruption (we have lost connectivity mid-run before).

- **Producer/consumer.** A network-bound **producer** thread downloads each
  field's tranche (caching raw pages); the GPU/CPU-bound **consumer** analyzes a
  field the instant its raw is cached. The GPU never waits on the network — in
  checkpoint 1 the producer finished downloading all 26 fields while the consumer
  was still embedding the early ones.
- **Durable checkpointing.** Each field's result is written to a per-field
  **partial** file immediately on completion; when all fields finish, a single
  `checkpoint_<N>.json` is written atomically and the partial is dropped. A
  `manifest.json` indexes every checkpoint and a `convergence.jsonl` logs how
  per-field MAP / Gini / the SPECTER-vs-TF-IDF delta move as the corpus grows.
- **Crash-safety / resume.** A SIGINT or crash mid-checkpoint loses nothing: the
  per-field partial, the raw-page cache, and the per-id embedding cache are all on
  disk. Re-running resumes finished fields for free and re-analyzes only the rest;
  re-running after a clean checkpoint **advances** to the next tranche. A PID lock
  prevents two runs racing on the shared caches.

### 3.2 Per-field pipeline (identical to the proven single-subfield study)

For each field we reuse the companion study's modules unchanged:

- **Complete in-corpus citation graph** (CSR) + **PageRank** (power method,
  damping 0.85), with heavy-tail diagnostics (Gini, cv, top-1% mass).
- **Four recommenders**, all scored on the identical closed candidate pool:
  **TF-IDF cosine** (the baseline), **word2vec mean-pool cosine**, **SPECTER
  transformer cosine** (768-d, GPU), and a **text-free graph co-citation**
  recommender (bibliographic coupling).
- **Held-out citation-prediction eval**: mask 30% of each eligible query's
  in-corpus references, rank all candidates, score the held-out set with
  **Recall@k / MAP / MRR** and bootstrap 95% CIs; the SPECTER-vs-TF-IDF gap is
  tested with a **paired bootstrap** on per-query average precision. To keep the
  graph dense and the candidate pool closed (so masked references are real in-pool
  targets), the eval runs on the citation-densest sample per field, exactly as in
  the single-subfield study.

### 3.3 Cross-field analyses

Beyond per-field tables we compute three cross-field quantities:

- **Generalization.** In how many of 26 fields does SPECTER beat TF-IDF on MAP?
  We report per-field ΔMAP and p, a **sign test** on the win count, and a
  **combined field-level test** — a one-sample bootstrap on the distribution of
  per-field MAP deltas (is the *across-field* mean delta > 0?).
- **Concentration.** Citation-count and PageRank **Gini by field**, and their
  ranges — how unequal impact is, field by field.
- **Interdisciplinarity.** For each field, the fraction of its in-corpus
  references whose target lives in a **different** top-level field (computed over
  the union of all 26 loaded fields, so cross-field edges are resolvable).

### 3.4 GPU embedding throughput

SPECTER runs on an AMD Radeon RX 7700S via ROCm (`HSA_OVERRIDE_GFX_VERSION=
11.0.0` before importing torch; torch 2.9.1+rocm6.4), batch-encoding at a
**measured steady-state of 9.1 docs/s** (excluding the one-time model load —
the prior "~8/s" figure included it). On an 8 GB card SPECTER (a BERT-class,
512-token model) is genuinely encode-bound at batch 64; this is the honest rate,
and it is why the neural study is checkpointed and resumable rather than run in
one shot.

---

## 4. Results

### 4.1 The generalization verdict: a majority, not a universal, win

At checkpoint 1, across all 26 fields:

| quantity | value | source |
|---|---:|---|
| fields evaluated | **26** | `crossfield.generalization.fields_evaluated` |
| fields where **SPECTER > TF-IDF** (MAP) | **16 / 26** | `fields_specter_wins` |
| win fraction | 0.62 | `win_fraction` |
| sign test (16/26 wins) | **p = 0.33** | `sign_test_p` |
| **combined field-level mean ΔMAP** | **+0.0095** | `combined_field_level.mean` |
| combined 95% CI | **[−0.0005, +0.0195]** | `combined_field_level.ci` |
| combined bootstrap p | **0.0624** | `combined_field_level.p` |

*(Source: `analysis/crossfield/checkpoint_1.json → crossfield.generalization`.)*

The honest reading: **SPECTER wins more fields than it loses (16 vs 10), but the
aggregate advantage across fields is borderline and not significant** (combined
CI just includes zero; sign test far from significant). A practitioner choosing a
recommender for an arbitrary field cannot assume the transformer will win — it
probably will, but with real risk of a tie or a loss, and the expected gain is
small in the field-aggregate.

Macro-averaged over the 26 fields, the method ordering is nonetheless
SPECTER-first:

| method | macro-avg MAP (26 fields) |
|---|---:|
| **SPECTER transformer** | **0.196** |
| TF-IDF cosine — *baseline* | 0.187 |
| word2vec mean-pool — *baseline* | 0.158 |
| graph co-citation (text-free) | 0.089 |

*(Macro-average of `methods.<m>.map` over fields. The transformer leads on
average, but the lead is carried by a subset of fields — see §4.2.)*

### 4.2 Where SPECTER wins, where it loses (per-field)

Sorted by ΔMAP (SPECTER − TF-IDF). Positive = SPECTER wins.

| field | eval q | TF-IDF MAP | SPECTER MAP | ΔMAP | p | win |
|---|---:|---:|---:|---:|---:|:--:|
| Neuroscience | 136 | — | — | **+0.063** | 0.004 | ✓ |
| Computer Science | 528 | — | — | **+0.060** | 0.000 | ✓ |
| Social Sciences | 133 | — | — | +0.039 | 0.015 | ✓ |
| Psychology | 133 | — | — | +0.035 | 0.044 | ✓ |
| Dentistry | 162 | — | — | +0.031 | 0.084 | ✓ |
| Environmental Science | 247 | — | — | +0.030 | 0.013 | ✓ |
| Nursing | 74 | — | — | +0.029 | 0.279 | ✓ |
| Agricultural & Biological | 150 | — | — | +0.029 | 0.077 | ✓ |
| Decision Sciences | 128 | — | — | +0.024 | 0.200 | ✓ |
| Business, Management & Acc. | 215 | — | — | +0.023 | 0.097 | ✓ |
| Materials Science | 367 | — | — | +0.020 | 0.049 | ✓ |
| Engineering | 376 | — | — | +0.014 | 0.137 | ✓ |
| Medicine | 325 | — | — | +0.014 | 0.207 | ✓ |
| Economics, Econ. & Finance | 101 | — | — | +0.010 | 0.680 | ✓ |
| Veterinary | 134 | — | — | +0.005 | 0.825 | ✓ |
| Biochem., Genetics & Mol. Bio. | 258 | — | — | +0.000 | 0.980 | ✓ |
| Physics and Astronomy | 551 | — | — | −0.001 | 0.860 | ✗ |
| Chemistry | 352 | — | — | −0.004 | 0.759 | ✗ |
| Chemical Engineering | 683 | — | — | −0.013 | 0.015 | ✗ |
| Energy | 769 | — | — | −0.014 | 0.001 | ✗ |
| Health Professions | 60 | — | — | −0.018 | 0.507 | ✗ |
| Mathematics | 207 | — | — | −0.018 | 0.074 | ✗ |
| Arts and Humanities | 63 | — | — | −0.020 | 0.493 | ✗ |
| Immunology & Microbiology | 102 | — | — | −0.022 | 0.245 | ✗ |
| Earth & Planetary Sciences | 242 | — | — | −0.031 | 0.021 | ✗ |
| Pharmacology, Tox. & Pharm. | 292 | — | — | −0.034 | 0.001 | ✗ |

*(Source: `checkpoint_1.json → fields[*].transformer_vs_tfidf`; full per-method
MAP/Recall/MRR tables with CIs are in the JSON and in `CROSSFIELD_RESULTS.md`.)*

A clear pattern emerges. SPECTER's biggest, most significant wins are in fields
where **fine-grained phrase meaning carries the relevance signal** — Neuroscience,
Computer Science, Social Sciences, Psychology. Its **significant losses** cluster
in **physical-science and pharmacology** fields — Pharmacology, Energy, Earth &
Planetary, Chemical Engineering — where TF-IDF's exact-term matching (compound
names, instruments, materials, reaction terms) is hard to beat and SPECTER's
semantic smoothing can *hurt*. Physics and Astronomy, the top-field that contains
the single-subfield study's HEP slice, is a near-tie at this top-level
granularity (ΔMAP −0.001), a useful reminder that a win in one citation-dense
subfield need not survive aggregation to the whole field.

### 4.3 Concentration (Gini by field)

| quantity | range across 26 fields |
|---|---|
| citation-count Gini | **[0.235, 0.538]** |
| PageRank Gini | **[0.263, 0.652]** |

*(Source: `crossfield.concentration`.)*

Every field is heavy-tailed (no field is uniform — the single-subfield study's
fix holds everywhere), but concentration varies: Decision Sciences and Computer
Science are the most citation-concentrated (Gini ≈ 0.48–0.54), Pharmacology and
Materials Science the least (≈ 0.24–0.25). PageRank Gini runs higher and wider
(up to 0.65), as recursive prestige amplifies the tail.

### 4.4 Interdisciplinarity

| quantity | range across 26 fields | mean |
|---|---|---:|
| fraction of references crossing field boundaries | **[0.114, 0.468]** | ~0.30 |

*(Source: `crossfield.interdisciplinarity`, computed over the union of all 26
loaded fields.)*

The most inward-looking field is **Physics and Astronomy** (only ~11% of its
references leave the field), the most outward-looking are **Social Sciences**,
**Health Professions**, and **Immunology & Microbiology** (~44–47% cross a field
boundary). Interdisciplinarity does not cleanly predict the SPECTER win/loss split
— Physics (low interdisc.) and Social Sciences (high interdisc.) sit on opposite
sides of the verdict — which argues against a simple "neural wins where fields
mix" story.

### 4.5 Convergence / coverage status

Checkpoint 1 is the **first** tranche (top ~3k works/field). The corpus grows by
re-running the loop; `convergence.jsonl` records, per checkpoint, the win count,
the combined ΔMAP and its p, and the Gini range, so a reader can watch whether the
borderline aggregate result firms up (toward significance) or stays a wash as
density increases. We make **no** claim that the checkpoint-1 numbers are
converged; we claim they are the honest first measurement and that the method to
refine them is built and running.

---

## 5. Discussion

**Transformer paper-recommendation is field-dependent, not universal.** This is
the central finding and it is more useful than a blanket "transformers win."
SPECTER's pre-training objective — pull a paper near the papers it cites — pays
off most where the text *is* the relevance signal at fine grain (cognitive,
computational, social-science abstracts) and can backfire where exact lexical
tokens (chemical names, materials, pharmacological compounds, instruments) are the
relevance signal and semantic smoothing blurs them. TF-IDF's "dumb" exact-match is
a genuinely strong, sometimes stronger, baseline in those fields.

**The single-subfield win did not cleanly survive generalization.** HEP (a
citation-dense subfield) gave SPECTER +15.4% MAP; the encompassing top-level field
Physics and Astronomy is a tie at checkpoint 1. This is exactly the kind of result
a generalization study exists to surface: an effect demonstrated in one favorable
slice is not entitled to a cross-field claim, and ours doesn't get one.

**The aggregate is borderline, and we let it be borderline.** Combined mean ΔMAP
+0.0095 with a 95% CI of [−0.0005, +0.0195] (p = 0.0624) is the textbook "almost,
but not at α = 0.05" result. We resist the temptation to one-side the test or
cherry-pick the 16 wins into a headline. The corpus loop exists precisely so this
number can be re-measured at higher density rather than over-interpreted now.

**Method choice should be field-aware.** The practical recommendation that falls
out of the table: use SPECTER (or a hybrid) in neuroscience/CS/social-science/
psychology; keep TF-IDF (or ensemble it ahead of the transformer) in
pharmacology/energy/earth-science/chemistry. A field-agnostic "always neural"
default is not supported by the data.

---

## 6. Limitations

Stated plainly; none hidden.

1. **Checkpoint 1 is one tranche.** Top ~3k works/field is the most-impactful core,
   not the whole field. In-corpus edge density (and eval query counts, 60–769 per
   field) is lower than the deep HEP slice. The result is a first measurement that
   the loop is built to refine; the convergence log is the place to read whether it
   firms up.
2. **Per-field power varies.** Fields with few dense queries (Health Professions
   60, Arts & Humanities 63, Nursing 74) have wide per-field CIs; their individual
   p-values are weak. The **combined** field-level test is the right unit for the
   generalization claim, and it is borderline.
3. **Top-level fields blur subfields.** A win in one subfield (HEP) can vanish when
   averaged over its top-level field (Physics & Astronomy). The 26-field grain
   answers "does it generalize across fields"; it does not resolve subfield
   structure (a future checkpoint could descend to subfields).
4. **Closed-world, citation-density-selected eval.** As in the single-subfield
   study, each field's eval runs on its citation-densest sample so the graph isn't
   sparsified; absolute Recall/MAP/MRR are relative, not open-world retrieval
   quality.
5. **word2vec baseline is in-domain PPMI-SVD**, not Google's binary — the
   representation class (static word vectors, mean-pooled, cosine) is identical;
   only the training corpus differs.
6. **Citation prediction is a proxy for relevance** — large, objective, and
   annotation-free, but a proxy; methods that mimic citation habits (the graph
   recommender, strong at deep recall) are advantaged by it.
7. **GPU-bounded.** All embeddings are from one 8 GB AMD GPU at ~9 docs/s; the
   neural study is therefore checkpointed. The same code scales with more GPU time
   — that *is* the loop.

---

## 7. Reproducibility statement

Every number is computed by `scripts/crossfield_run.py` and written to
`analysis/crossfield/checkpoint_<N>.json` (+ `manifest.json` + `convergence.jsonl`),
summarized by `scripts/crossfield_report.py` into `CROSSFIELD_RESULTS.md`, and
pinned by tests that fail if prose and data diverge (`tests/test_crossfield.py`,
including a guard asserting the cross-field headline against `checkpoint_1.json`,
and tests for the cross-field aggregation + the producer/consumer resume logic).

```bash
pip install -e .                      # numpy, pandas, pyarrow, scikit-learn, sentence-transformers, torch(+rocm)
# run / advance the loop (first run = checkpoint 1, top ~3k works/field):
HSA_OVERRIDE_GFX_VERSION=11.0.0 python scripts/crossfield_run.py
# re-run to GROW the corpus to the next tranche (5k -> 20k -> 50k works/field):
HSA_OVERRIDE_GFX_VERSION=11.0.0 python scripts/crossfield_run.py
# measure GPU embed throughput on the current cache:
HSA_OVERRIDE_GFX_VERSION=11.0.0 python scripts/crossfield_run.py --measure-throughput
# summarize the latest checkpoint into the per-field results doc:
python scripts/crossfield_report.py
python -m pytest tests/test_crossfield.py -q
```

The run is **resumable**: a SIGINT/crash mid-checkpoint loses nothing (per-field
partial + raw-page cache + per-id embedding cache), and re-running continues from
the last checkpoint.

**Data availability.** The heavy raw OpenAlex page cache and the per-id SPECTER
embedding `.npy` cache are rebuilt from the OpenAlex API and are gitignored; the
checkpoint JSONs, manifest, convergence log, and per-field results doc are
committed. License: MIT (code) / CC-BY-4.0 (data, via OpenAlex CC0).

**Author contributions / COI.** research-atlas is developed under the Bucket
Foundation open-data program. The single-subfield predecessor (MA544) was authored
by one of the present authors; this cross-field study is its acknowledged
successor. No competing financial interests.

---

## Appendix A. Headline numbers (machine-checked, checkpoint 1)

| Quantity | Value | Source field |
|---|---:|---|
| Fields evaluated | 26 | `fields_with_results` |
| Works (top ~3k/field) | 78,000 | `total_works_loaded` |
| GPU embed throughput (steady-state) | 9.1 docs/s | `embed_docs_per_sec` |
| SPECTER > TF-IDF (MAP) | 16 / 26 fields | `crossfield.generalization.fields_specter_wins` |
| Win fraction | 0.62 | `win_fraction` |
| Sign test p | 0.33 | `sign_test_p` |
| Combined field-level mean ΔMAP | +0.0095 | `combined_field_level.mean` |
| Combined 95% CI | [−0.0005, +0.0195] | `combined_field_level.ci` |
| Combined bootstrap p | 0.0624 | `combined_field_level.p` |
| Citation Gini range | [0.235, 0.538] | `concentration.citation_gini_range` |
| PageRank Gini range | [0.263, 0.652] | `concentration.pagerank_gini_range` |
| Interdisciplinarity range | [0.114, 0.468] | `interdisciplinarity.range` |
| Embedding model | SPECTER (allenai-specter), 768-d, GPU/ROCm | `transformer_model` |
