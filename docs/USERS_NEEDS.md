# research-atlas — Researcher Pain-Points & Software-Needs (the cross-field tool roadmap)

**Version 0.1.0** · grounded in the atlas (1.17M profiled researchers, the
funded-works topic mix per field, and the $1.04T funding landscape) plus the
published literature on research-software gaps.

This is the analysis that turns the **users** (segmented researcher profiles,
see `USERS_POLICY.md`) into a **product roadmap**: for each major field, what
computational tools and workflows researchers actually need, what is painful or
missing today, which existing atlas tool/need slug serves it, and the gap that
remains. The right-hand "gap" column is the roadmap backlog.

> Method, stated honestly. The *recurring topics per field* are computed from
> our 1.17M researcher profiles (`researchers.parquet`, the `top_topics`
> column rolled up by `field_slug`). The *what's funded* signal is the
> funder/$ landscape (`docs/LANDSCAPE.md`). The *tooling-gap* claims are the
> well-documented pain points in the research-software-engineering and
> open-science literature (reproducibility crisis, FAIR-data mandates, the
> "long tail" of bespoke lab code, HPC access friction, MLOps-for-science).
> Where a need is inferred rather than measured, it is marked *(inferred)*.

---

## 1. The cross-cutting pains (every field)

Before the per-field table, four pains recur in **every** field's topic mix and
in the literature:

1. **Reproducibility / provenance** — code + data + environment that lets a
   result be re-run. Funders (NIH Data Management & Sharing Policy 2023, NSF,
   Horizon Europe, Wellcome, Gates open-access) now *mandate* data/code sharing,
   so this is funded pressure, not a nicety. Slug: `stats-reproducibility`,
   `reproducibility-mlops`.
2. **FAIR data management** — Findable/Accessible/Interoperable/Reusable data,
   metadata, and DOIs. The single most-mandated, least-tooled need across all
   funders in the atlas. Slug: `fair-data-mgmt`.
3. **Scale / compute access** — getting bespoke analysis onto HPC or cloud
   without a dedicated RSE. Slug: `hpc-sim`, `compute-orchestration`,
   `large-data-pipelines`.
4. **The bespoke-code long tail** — most science runs on un-maintained,
   un-tested, single-author scripts. Lab ELNs and pipeline frameworks exist but
   adoption is uneven. Slug: `lab-eln`.

These four are why `fair-data-mgmt` / `stats-reproducibility` appear in almost
every field's `tool_fit` in `atlas/users/segment.py`.

---

## 2. Cross-field tool-needs roadmap (the table)

Each row: **field → top unmet software needs (grounded) → atlas tool/need slug
that serves it → the remaining gap**. Slugs are the `tool_fit` keys assigned to
researchers in `atlas/users/segment.py`, so this table and the user segments are
the same vocabulary.

| Field (slug) | Top recurring topics (from our profiles) | Top unmet software needs | Atlas slug serving it | The gap (roadmap) |
|---|---|---|---|---|
| **biomed-bio** (`biomed-bio`) — 689,684 profiled | single-cell & spatial transcriptomics; CRISPR; epigenetics/DNA methylation; cancer immunotherapy; gut microbiota; SARS-CoV-2 | reproducible **seq pipelines** (bulk/scRNA/spatial); **image analysis** (microscopy, pathology); **structure prediction** post-AlphaFold; FAIR clinical/omics data; stats reproducibility | `seq-pipelines`, `imaging-analysis`, `structure-pred`, `fair-data-mgmt`, `stats-reproducibility` | Pipelines exist (nf-core, Cell Ranger) but are **fragmented, version-fragile, hard for wet-lab PIs**; spatial-omics tooling immature; structure→function still unserved; clinical-data FAIR is mostly manual |
| **chemistry** (`chemistry`) — 20,531 | metal-organic frameworks; proteomics; mass spectrometry; C–H functionalization; NMR; click chemistry | **reaction informatics** / retrosynthesis; **spectra analysis** (MS/NMR auto-ID); **molecular simulation** (DFT/MD); ELN; FAIR reaction data | `reaction-informatics`, `spectra-analysis`, `molecular-sim`, `lab-eln`, `fair-data-mgmt` | Reaction/spectra data **locked in vendor formats**; no open auto-interpretation of MS/NMR at scale; sim tools need HPC + expertise; ELN adoption low |
| **physics-astro** (`physics-astro`) — 108,466 | particle physics; galaxy formation; pulsars/gravitational waves; gamma-ray bursts; dark matter | **HPC simulation** at scale; **large-data pipelines** (survey/telescope/detector TB–PB); **ML surrogates** for expensive sims; instrument control; FAIR data | `hpc-sim`, `large-data-pipelines`, `ml-surrogates`, `instrument-control`, `fair-data-mgmt` | Pipelines are **per-collaboration bespoke**; surrogate-model tooling early; data-volume growth outpaces tooling; reproducible end-to-end sim→analysis rare |
| **materials** (`materials`) — 44,536 | 2D materials/graphene; ML-in-materials; catalysis; nanoparticles; carbon materials | **molecular/atomistic simulation**; **ML surrogates / potentials**; **high-throughput screening**; ELN; FAIR materials data | `molecular-sim`, `ml-surrogates`, `high-throughput-screening`, `lab-eln`, `fair-data-mgmt` | "ML in Materials Science" is already a *top topic* — demand is proven; but **interatomic-potential pipelines, screening orchestration, and structured materials databases are still hand-rolled** per group |
| **cs-ml** (`cs-ml`) — 44,999 | quantum information/computing; computational drug discovery; topic modeling; privacy-preserving ML; AI-in-cancer | **reproducible MLOps** for research; **compute orchestration**; **benchmark tooling**; model cards / eval | `reproducibility-mlops`, `compute-orchestration`, `benchmark-tooling`, `model-cards-eval` | Industry MLOps **doesn't fit research** (no fixed pipeline, frequent ad-hoc experiments); benchmarking + eval reproducibility weak; experiment provenance often a spreadsheet |
| **earth-climate** (`earth-climate`) — 116,840 | atmospheric chemistry/aerosols; climate variability & models; paleoclimatology; species distribution; marine ecosystems | **large geospatial-data pipelines**; **geospatial analysis**; **HPC climate sim**; **model intercomparison**; FAIR environmental data | `large-data-pipelines`, `geospatial-analysis`, `hpc-sim`, `model-intercomparison`, `fair-data-mgmt` | Climate/EO data is **huge and heterogeneous**; intercomparison (CMIP-style) tooling heavyweight; species/ecology data scattered; reproducible geospatial workflows rare for non-specialists |
| **econ-social** (`econ-social`) — 42,276 | mental-health interventions; health disparities; misinformation; child development; neuroendocrine behavior | **stats reproducibility**; **survey/microdata management**; **causal-inference tooling**; FAIR (de-identified) human-subjects data | `stats-reproducibility`, `survey-data-mgmt`, `causal-inference-tooling`, `fair-data-mgmt` | Reproducibility crisis is **most acute here**; survey/admin-data pipelines manual; causal-inference best-practice tooling under-adopted; privacy-preserving sharing hard |
| **math** (`math`) — 5,198 | (corpus-thin; funded-output-skewed: math-bio, clinical-trial stats, causal inference, combinatorics) | **proof assistants** (Lean/Coq); **symbolic computation**; reproducible numerical experiments | `proof-assistants`, `symbolic-compute`, `reproducibility-mlops` | Formalization tooling has a **steep learning curve**; symbolic/numeric reproducibility under-served; *(NB: pure math is under-sampled in a funder-output corpus — see §4)* |
| **engineering** (`engineering`) — 93,027 | (broad; folded bucket) | **HPC simulation**; **ML surrogates**; ELN; FAIR data | `hpc-sim`, `ml-surrogates`, `lab-eln`, `fair-data-mgmt` | Same simulation/surrogate gaps as physics+materials, plus weak data-management culture in applied/industrial sub-fields |

Field profile counts above are the live `by_field` aggregate
(`data/processed/sample/researchers_aggregates.json`).

---

## 3. What the roadmap says to build first (prioritization)

Reading the table against **audience size × pain intensity × funder pressure**:

1. **`fair-data-mgmt` + `stats-reproducibility` (horizontal)** — needed by every
   field, *mandated* by every funder in the atlas (NIH 2023 DMSP, NSF, Horizon
   Europe, Wellcome, Gates). Largest addressable user base (≈ all 1.17M), lowest
   field-specific build cost. **Build once, serve everyone.**
2. **`seq-pipelines` + `imaging-analysis` (biomed-bio)** — by far the largest
   single field (689k profiles, ~59% of users) and the best-funded
   ($570B NIH + foundations). Highest-value vertical.
3. **`ml-surrogates` + `molecular-sim` (materials + physics + engineering)** —
   "ML in Materials Science" is *already a top recurring topic*, so demand is
   measured, not assumed; same tooling serves three large fields (≈ 246k
   profiles combined).
4. **`large-data-pipelines` + `geospatial-analysis` (earth-climate)** — second-
   largest field (117k), data-volume pain is structural, climate-funding rising.
5. **`reproducibility-mlops` (cs-ml)** — the builders of the above; high
   leverage even though the field is mid-sized (45k).

The atlas itself already serves the **discovery / targeting** layer of this
roadmap: it can find, for any tool, the exact segment of researchers who need it
(`researchers_public` view filtered by `field_slug` + `tool_fit`), and — for the
high-value, public-contact segment — who to reach (per `USERS_POLICY.md`).

---

## 4. Honest limitations of this grounding

- **Funder-output bias.** The corpus is built from works that acknowledge our 75
  funders (NIH/NSF/EC/UKRI/foundations). It therefore **over-samples biomed and
  big-science, and under-samples pure math, theory, and humanities** (math shows
  only 5,198 profiles, and its top topics are math-*bio*/biostatistics, not pure
  math). Per-field *needs* are still sound; per-field *audience sizes* are
  relative-within-corpus, not census.
- **Topic = primary topic.** `top_topics` is each work's OpenAlex primary topic;
  interdisciplinary work is bucketed to one field.
- **Gap claims are literature-grounded, not surveyed.** We did not survey
  researchers; the gap column synthesizes documented RSE/open-science pain
  points against the measured topic + funding mix. Items marked *(inferred)*
  lean more on literature than on our data.
- These numbers move with the corpus; re-run `scripts/build_users.py` +
  `scripts/build_users_sample.py` to refresh.
