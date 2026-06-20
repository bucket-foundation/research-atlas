# research-atlas

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20774322.svg)](https://doi.org/10.5281/zenodo.20774322)

**A normalized, citable graph of the global research economy.**

research-atlas ingests the world's research **funding** (funders, grants/awards)
and **output** (organizations, people, works, fields) into one normalized graph
and publishes it as open, citable datasets for
[bucket.foundation](https://bucket.foundation).

It is source-agnostic: every data source (NSF, OpenAlex, NIH RePORTER, CORDIS,
ROR, ORCID, …) is a **connector** that maps that source's records onto one
canonical schema, with full provenance on every row.

### Connectors

| Source | Connector | What | Currency → USD |
|--------|-----------|------|----------------|
| **CORDIS (EU)** | `atlas/connectors/cordis.py` | **Full** EU framework-programme feed (Horizon Europe + H2020): every funding scheme, every participant org, every euroSciVoc field. Funder = European Commission (+ ERC for ERC schemes). | EUR → USD @ 1.08 (`fx_as_of` stamped) |
| **NSF (US)** | `atlas/connectors/nsf_bulk.py` | **Full** NSF awards via the research.gov API, windowed by month to dodge the 10k/query cap (`nsf.py` is the small-sample reference connector). | USD (1.0) |
| **NIH (US)** | `atlas/connectors/nih.py` | **Full** NIH RePORTER v2 feed (the largest single funder), windowed by (fiscal year, IC) under the 15k offset cap. Funder = NIH + awarding IC (NCI, NIAID, …). | USD (1.0) |
| **UKRI / GtR** | `atlas/connectors/ukri.py` | **Full** UK Gateway to Research corpus (`fetch_all`); Funder = UKRI + lead council (EPSRC, BBSRC, …). | GBP → USD @ 1.27 (`fx_as_of` stamped) |
| **ERC** | `atlas/connectors/erc.py` | Legacy ERC-only CORDIS slice (superseded by the full CORDIS connector; kept for back-compat). | EUR → USD @ 1.08 |
| **DFG** | `atlas/connectors/dfg.py` | Deutsche Forschungsgemeinschaft via GEPRIS (polite, cached, **resumable HTML crawl** of ~172k project ids from the sitemap; `scripts/ingest_dfg_full.py` is the scale path). | EUR (amount rarely published by GEPRIS → `null`) |
| **Gates Foundation** | `atlas/connectors/gates.py` | Bill & Melinda Gates Foundation **full Committed Grants CSV** (the bulk form of the public grants DB): grantee, purpose, division, topic, USD amount, dates. | USD (1.0, `fx_as_of` stamped) |
| **Wellcome Trust** | `atlas/connectors/wellcome.py` | **Full 360Giving grants list** (XLSX, awarded since 2000): applicants (PI/co-PI), recipient org + 360Giving id, programme, GBP amount, dates. | GBP → USD @ 1.27 (`fx_as_of` stamped) |
| **Sloan Foundation** | `atlas/connectors/sloan.py` | Alfred P. Sloan Foundation **full public Grants Database** (polite, cached HTML crawl): grantee, USD amount, city, year, program/sub-program, investigator. | USD (1.0, `fx_as_of` stamped) |
| **OpenAlex (output)** | `atlas/connectors/openalex_works.py` | The research-**output** side: works that acknowledge our funders (`awards.funder_id`), polite pool + cursor paging, cached/resumable. Emits works, ORCID-keyed people, ROR-keyed institutions, the OpenAlex topic taxonomy, and `grant_work` / `person_org` / `work_field` edges. | n/a |

> **Published but not yet machine-ingestible** (documented honestly in
> `docs/LANDSCAPE.md`, not faked): **CZI**, **HHMI**, **Simons**, **Moore** —
> all JS-rendered behind un-discoverable AJAX endpoints (or block scraping);
> each needs a headless-browser connector (deferred).

### Connecting the piles (input ↔ output)

The four funder feeds are reconciled into **one** graph:

- **Org → ROR** — `scripts/resolve_ror.py` + `atlas/ror_bulk.py` resolve every org
  to a ROR id **offline** from the ROR bulk dump (no 99k API calls) in four tiers
  (`exact` → abbreviation-`expanded` → `acronym` → IDF-weighted `fuzzy`), recording
  `match_method` + `match_score` in an audit map (`org_resolution.parquet`).
  Duplicate orgs across funders merge into **one canonical node per ROR id**.
  Matching is conservative — ambiguous and common-token cases resolve to **null**,
  never a wrong ROR id.
- **Grant → work** — `atlas/award_match.py` normalizes each funder's award-id
  conventions into shared join keys, so a work's `awards[].funder_award_id`
  intersects with our grant ids and draws a `grant_work` edge.
- **Person → ORCID / org** — authors carry ORCIDs and ROR-resolved affiliations
  straight from OpenAlex; people reconcile on ORCID (then OpenAlex author id, then
  name).
- **Validation** — `scripts/validate.py` asserts the graph's data-quality
  invariants after every build → [`docs/VALIDATION.md`](VALIDATION.md).
- **Querying** — `scripts/query.py` runs the metascience queries in
  `atlas/analysis.py` → [`docs/GRAPH.md`](GRAPH.md).

**Current full-scale ingest** (see [`docs/LANDSCAPE.md`](docs/LANDSCAPE.md) for the
full report): **~958k grants · ~140.6k organizations · ~1.22M people · 73 funders ·
~$658B funded (USD-normalized) · ~8.1M total graph rows** across NIH (FY2018-25),
NSF (FY2015-25), UKRI (full), CORDIS (full H2020 + Horizon Europe), the **Gates
Foundation** (full Committed Grants CSV), the **Wellcome Trust** (full 360Giving
list), the **Sloan Foundation** (full grants DB), and a resumable **DFG/GEPRIS**
corpus chunk.

- **Code:** MIT · **Published datasets:** CC-BY-4.0
- **Author:** Gianangelo Dichio · **Publisher:** bucket-foundation

---

## The schema (at a glance)

Six entities connected by directed edges. Every row carries provenance
(`source`, `source_id`, `source_url`, `as_of`) and a stable surrogate key
(`atlas_id`). Full detail in [`docs/SCHEMA.md`](docs/SCHEMA.md).

```
  Funder ──awards──▶ Grant ──recipient──▶ Organization ◀──affiliation── Person
                       │                                                  ▲
                       ├──pi / co-pi──────────────────────────────────────┘
                       └──acknowledges──▶ Work ──belongs-to──▶ Field/Topic
```

| Entity        | Keyed on (where resolvable)        |
|---------------|------------------------------------|
| Funder        | Crossref Funder id                 |
| Grant/Award   | source award id                    |
| Organization  | **ROR id**                         |
| Person        | **ORCID**                          |
| Work/Output   | **OpenAlex id / DOI**              |
| Field/Topic   | **OpenAlex topic id**              |

Edge tables: `funder_grant`, `grant_org`, `grant_person`, `grant_work`,
`person_org`, `work_field`.

**Money:** stored in the original currency *and* a normalized `amount_usd`.
Unknown money is `null` — never a silent `0`.

---

## Quickstart

```bash
pip install -r requirements.txt          # or: pip install -e ".[dev]"

# --- small sample (fast) ---
python scripts/ingest_nsf.py --keyword biophysics --limit 300
python scripts/build_db.py               # → research_atlas.duckdb
python scripts/build_sample.py --max-grants 240
```

### Full-scale ingestion (the real funding landscape)

The bulk path streams every record through a memory-bounded `BulkWriter` into
partitioned parquet shards (`data/processed/<table>/source=.../year=.../`), then
`consolidate.py` folds them into the flat published parquet with out-of-core
DuckDB de-dup. Each step is idempotent + resumable (raw pages + shards are
cached, so a re-run converges and an interrupted run resumes for free).

```bash
# 1. Ingest each funder at full scale (run in parallel; all cache raw + resume)
python scripts/ingest_cordis.py                              # full EU (~56k)
python scripts/ingest_nsf_bulk.py  --year-start 2015 --year-end 2025   # ~132k
python scripts/ingest_nih.py       --year-start 2018 --year-end 2025   # ~624k
python scripts/ingest_ukri_full.py                           # full UKRI (~174k)
python scripts/ingest_gates.py                               # full Gates CSV (~41k)
python scripts/ingest_wellcome.py                            # full Wellcome XLSX (~26k)
python scripts/ingest_sloan.py                               # full Sloan DB (~3.4k)
python scripts/ingest_dfg_full.py --limit 200000             # DFG/GEPRIS (resumable)

# 2. Reconcile orgs to ROR (offline, from the bulk dump) + merge duplicates
#    (download the dump from https://zenodo.org/communities/ror-data first)
python scripts/resolve_ror.py

# 3. Ingest the OUTPUT side: works acknowledging our funders + grant→work links
python scripts/ingest_openalex_works.py --from 2016-01-01

# 4. Fold shards → flat parquet (deduped, out-of-core) + rebuild the manifest
python scripts/consolidate.py --memory-limit 6GB --temp-dir /tmp/atlas_duck

# 5. Build the DuckDB graph DB, validate it, write the reports
python scripts/build_db.py
python scripts/validate.py                                   # → docs/VALIDATION.md (gates the build)
python scripts/landscape_report.py                           # → docs/LANDSCAPE.md
python scripts/query.py top_funders_by_output --topic mitochondri
```

### Example graph query (DuckDB)

```sql
-- Top recipient organizations by total NSF award $ (funder → grant → org)
SELECT o.name, round(sum(g.amount_usd)/1e6, 2) AS total_M_usd, count(*) AS grants
FROM funder_grant fg
JOIN grant g       ON g.atlas_id = fg.dst_id
JOIN grant_org go  ON go.src_id = g.atlas_id AND go.role = 'recipient'
JOIN organization o ON o.atlas_id = go.dst_id
WHERE g.amount_usd IS NOT NULL
GROUP BY o.name ORDER BY total_M_usd DESC LIMIT 10;
```

A committed sample lives under [`data/processed/sample/`](data/processed/sample/)
with its own `MANIFEST.json` so the repo demonstrates real output. The full
processed parquet, raw caches, and the DuckDB file are gitignored (rebuilt
locally).

---

## Repository layout

```
atlas/
  schema.py            canonical schema + surrogate keys + coerce() (source of truth)
  manifest.py          (re)builds data/MANIFEST.json (+ per-source $ breakdown)
  bulkwrite.py         memory-bounded, partitioned parquet writer (the scale path)
  consolidate.py       fold shards → flat parquet, out-of-core DuckDB de-dup
  ror.py               conservative cached name → ROR resolver
  connectors/
    base.py            Connector ABC + polite HttpClient (fetch/normalize/emit)
    nsf.py             reference connector: NSF Award Search (small sample)
    nsf_bulk.py        FULL NSF: research.gov API, monthly-windowed
    nih.py             FULL NIH RePORTER v2 (largest funder), (FY, IC)-windowed
    cordis.py          FULL CORDIS EU: all schemes, all orgs, all euroSciVoc
    ukri.py            UKRI / GtR (GBP→USD); fetch_all() walks the full corpus
    erc.py             ERC-only CORDIS slice (legacy; superseded by cordis.py)
    dfg.py             DFG via GEPRIS (cached HTML scrape, small sample)
scripts/
  ingest_nsf.py        small NSF sample            ingest_nsf_bulk.py   FULL NSF
  ingest_ukri.py       small UKRI sample           ingest_ukri_full.py  FULL UKRI
  ingest_erc.py        ERC-only slice              ingest_cordis.py     FULL CORDIS
  ingest_dfg.py        DFG sample                  ingest_nih.py        FULL NIH
  consolidate.py       shards → flat parquet (deduped) + rebuild manifest
  build_db.py          parquet → research_atlas.duckdb (keys + indexes)
  build_sample.py      stratified committable slice under data/processed/sample/
  landscape_report.py  query the graph → docs/LANDSCAPE.md (real totals)
analysis/
  funding_landscape.py queries + statistics (Gini/HHI/bootstrap) — read-only
  run.py               recompute results.json + figures (idempotent)
  results.json         machine-readable results (paper + tests read this)
  figures/             the 5 paper figures (PNG)
docs/
  SCHEMA.md            the canonical schema, in prose
  ARCHITECTURE.md      data flow, idempotency, money, Publish-to-Bucket seam
  LANDSCAPE.md         the full funding-landscape report (real numbers)
  papers/01-funding-landscape/   first preprint: paper.md + paper.pdf + .zenodo.json
tests/                 schema invariants + per-connector normalizers + scale path + paper-vs-data (no network)
data/
  raw/                 raw source caches (gitignored)
  processed/           flat + sharded entity/edge parquet (gitignored except sample/)
  MANIFEST.json        authoritative index of published datasets
```

---

## Adding a new connector

A connector is one module under `atlas/connectors/` that subclasses
`Connector`. You implement exactly three things:

1. **`source`** — a short, stable key (e.g. `"nih"`). Used in provenance and to
   path the raw cache under `data/raw/<source>/`.
2. **`fetch(**kwargs) -> Iterable[dict]`** — page through the source. For each
   page, build a stable `page_key`, check `self.load_raw(page_key)` first, and
   `self.cache_raw(page_key, payload)` after a live request. Use `self.http`
   (the polite client) for all I/O. `yield` each raw page. This gives you
   idempotent, resumable ingestion for free.
3. **`normalize(raw_pages) -> Iterator[Row]`** — turn raw pages into canonical
   rows. For each record:
   - mint ids with `make_id(entity, <best stable id>)` (prefer a resolved
     ROR/ORCID/DOI; fall back to `source` + `source_id`);
   - emit `Row("<entity>", {...})` for each node and `Row("<edge>", {...})` for
     each relationship;
   - put the original + normalized USD money on grants (`null` if unknown);
   - stamp `source`, `source_id`, `source_url`, `as_of` on every row.

Then register it in `atlas/connectors/__init__.py` (`REGISTRY`) and add a
`scripts/ingest_<source>.py` mirroring `ingest_nsf.py`. `Connector.emit()` and
the manifest handle merge/dedup/publish — you never touch parquet directly.

Validation is automatic: every emitted row passes through `schema.coerce()`,
which rejects unknown columns and enforces the money invariant. Add a
no-network normalizer test (see `tests/test_nsf_normalizer.py`) with a small
recorded sample page.

---

## How it's published to Bucket

A published dataset (parquet + its `MANIFEST.json` entry) becomes a **citable
Bucket Foundation canon artifact** via a clearly-marked seam:

1. the dataset is served through the **feed402** three-tier envelope
   (`raw` / `query` / `insight`) at `/api/research`, with the manifest entry as
   citation metadata and each row's `source_url` as the per-fact attribution;
2. the manifest entry + parquet hash is **minted** as a Story Protocol IP asset,
   stored on Walrus, and recorded in the Bucket canon index — citation fees
   route to the dataset's authors.

research-atlas builds the dataset + manifest and **guarantees the seam
contract** (versioned manifest, per-row citeability, content-addressable
parquet, idempotent re-publish). The actual feed402 call and the mint live in
the Bucket Foundation stack — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
