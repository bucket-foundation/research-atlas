# research-atlas — Architecture

```
                         sources (NSF, OpenAlex, NIH, CORDIS, ROR, ORCID …)
                                          │
                          ┌───────────────┴───────────────┐
                          │        Connector (per source)  │
                          │                                │
                          │  fetch()   → data/raw/<source>/ │  paginated, polite,
                          │             (idempotent cache)  │  Retry-After honored
                          │  normalize()→ canonical Rows    │  raw → entities + edges
                          │  emit()    → data/processed/    │  merge by atlas_id,
                          │             <table>.parquet     │  dedup, append provenance
                          └───────────────┬────────────────┘
                                          │
                       data/processed/*.parquet  (one per entity + edge)
                                          │
                 ┌────────────────────────┼─────────────────────────┐
                 │                         │                         │
        scripts/build_db.py      data/MANIFEST.json        data/processed/sample/
        research_atlas.duckdb    (authoritative index       (small committed slice
        (graph queries, keys,     of published datasets)      proving real output)
         indexes)
                                          │
                                          ▼
                          Publish-to-Bucket seam (see below)
```

## Components

- **`atlas/schema.py`** — the canonical schema (entities, edges, provenance,
  surrogate-key derivation, `coerce()` validation, money invariant). The single
  source of truth every connector conforms to.
- **`atlas/connectors/base.py`** — `Connector` ABC + polite `HttpClient`. Defines
  the uniform `fetch → normalize → emit` lifecycle, the raw-cache contract
  (idempotent/resumable), and the parquet merge/dedup logic.
- **`atlas/connectors/<source>.py`** — one module per source. `nsf.py` is the
  reference implementation.
- **`atlas/ror.py`** — conservative, cached name→ROR resolver (only accepts the
  ROR `chosen` flag or a high-confidence score; better `null` than wrong). Used
  for ad-hoc single lookups; the scale path is `ror_bulk.py`.
- **`atlas/ror_bulk.py`** — the **at-scale** offline org→ROR resolver. Builds an
  in-memory normalized name/alias/acronym index from the ROR bulk dump and matches
  all orgs locally in four tiers (exact → abbreviation-**expanded** → acronym →
  **IDF-weighted fuzzy**), recording `match_method` + `match_score`. Conservative:
  records ambiguity (campus-vs-system) and common-token traps as **no match**
  rather than guessing wrong. Inverted-index retrieval keeps it ~1s over 99k orgs.
- **`atlas/connectors/openalex_works.py`** — the research-**output** connector.
  Pulls works that acknowledge our funders (`awards.funder_id` filter, polite
  pool, cursor-paged, cached), emitting works + people (ORCID) + ROR-keyed orgs +
  the OpenAlex topic taxonomy, plus `grant_work` / `person_org` / `work_field`
  edges.
- **`atlas/award_match.py`** — funder-specific award-id normalization producing
  the `grant↔work` join keys (NIH IC+serial, NSF/EC bare number, UKRI), so output
  works link back to input grants across each funder's id conventions.
- **`atlas/analysis.py`** — read-only metascience query affordances over the
  DuckDB (top funders of a field, org funding-vs-output, rising-field detection,
  cross-funder hubs, funding-by-country). Exposed via `scripts/query.py`.
- **`atlas/manifest.py`** — (re)builds `data/MANIFEST.json` from the parquet.
- **`scripts/`** — `ingest_<source>.py` (run a connector), `resolve_ror.py`
  (org→ROR + dedup/merge), `ingest_openalex_works.py` (output side + grant links),
  `consolidate.py` (shards → flat parquet), `build_db.py` (parquet → DuckDB),
  `validate.py` (data-quality gate → `docs/VALIDATION.md`), `query.py` (run a
  metascience query), `build_sample.py` (committable slice).

## Validation gate

`scripts/validate.py` runs after a build and **asserts** the invariants a
trustworthy graph must hold — referential integrity (no orphan edge endpoints),
the money/FX invariants, ROR/ORCID well-formedness, one canonical org per ROR id,
unique entity keys, provenance on every row. It writes `docs/VALIDATION.md` and
**exits non-zero on any hard failure**, so it can gate CI or a publish step. Soft
coverage metrics (ROR %, ORCID %, works/links counts) are reported but never fail
the run.

## Idempotency & resumability

1. `fetch()` writes every raw page to `data/raw/<source>/<page_key>.json` and
   **skips pages already on disk** — interrupted runs resume for free, and a
   rate-limit wall costs nothing on retry.
2. `emit()` merges on the stable `atlas_id`, so re-emitting **converges** rather
   than duplicating. Re-normalizing from cache (no network) is supported via
   `Connector.iter_cached_raw()`.
3. `RorResolver` caches name→ROR lookups to `data/raw/<source>/_ror_cache.json`.

## Politeness

`HttpClient` honors `Retry-After` (capped at 120s to defang absurd values),
backs off on 429/503, sleeps a configurable delay between requests, and sends a
descriptive User-Agent with a contact mailto. A browser UA is available for
sources that block default agents (`base.BROWSER_UA`).

## Money normalization

Grants store the original currency amount **and** a normalized `amount_usd`,
the `fx_rate_to_usd` used, and the `fx_as_of` date. Unknown money is `null` —
the schema forbids a silent `0`. USD-native sources (NSF) set `fx_rate_to_usd =
1.0`. Non-USD sources (CORDIS in EUR, etc.) attach the rate they used so the
conversion is auditable.

---

## Publish-to-Bucket seam  *(specified here, not built here)*

A published dataset becomes a **citable Bucket Foundation canon artifact** in
two clearly-marked steps. This repo builds the dataset + manifest; the mint is
**out of scope for research-atlas** and lives in the Bucket Foundation stack.

```
  data/processed/<table>.parquet  +  data/MANIFEST.json
                     │
                     ▼   (1) feed402 envelope  — the citation seam
   POST /api/research  →  three-tier feed402 envelope:
        raw     : the parquet rows (citation.type = "source")
        query   : a scoped graph query result
        insight : a derived figure (e.g. "$ by field, 2020-2025")
   Each dataset's MANIFEST entry (path, schema_version, row_count, as_of,
   sources) becomes the citation metadata; source_url on every row provides
   the per-fact attribution chain.
                     │
                     ▼   (2) Story Protocol mint — the canon seam
   A manifest entry + its parquet hash → a Bucket canon artifact:
        - register as an IP asset (Story Protocol)
        - store the parquet on Walrus, pin the hash in the IP metadata
        - record in gdrive:AGFarms/Nucleus/research/<topic>-canon/CANON_INDEX.md
   Citation fees route to the dataset's authors, per Bucket's thesis.
```

**Seam contract (what research-atlas guarantees for the publisher):**
- every published parquet has a `MANIFEST.json` entry with a `schema_version`,
  `row_count`, `as_of`, and contributing `sources`;
- every row is independently citeable via its `source_url`;
- the parquet is content-addressable (hash the file) so a mint can pin it;
- re-publishing is idempotent — a converged parquet + manifest, not a new dump.

**Not built here:** the actual `POST /api/research` call, the Story Protocol
mint, and the Walrus upload. Those belong to the Bucket Foundation repo
(`bucket.foundation` — Story Protocol / Walrus / Dynamic / Supabase). This seam
exists so a publisher can wire research-atlas in without reshaping the data.
