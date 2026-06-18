# research-atlas

**A normalized, citable graph of the global research economy.**

research-atlas ingests the world's research **funding** (funders, grants/awards)
and **output** (organizations, people, works, fields) into one normalized graph
and publishes it as open, citable datasets for
[bucket.foundation](https://bucket.foundation).

It is source-agnostic: every data source (NSF, OpenAlex, NIH RePORTER, CORDIS,
ROR, ORCID, …) is a **connector** that maps that source's records onto one
canonical schema, with full provenance on every row.

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

# 1. Ingest a sample (idempotent + resumable; caches raw under data/raw/)
python scripts/ingest_nsf.py --keyword biophysics --limit 300

# 2. Build the DuckDB graph database
python scripts/build_db.py               # → research_atlas.duckdb

# 3. (optional) refresh the small committed sample
python scripts/build_sample.py --max-grants 100
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
  manifest.py          (re)builds data/MANIFEST.json
  ror.py               conservative cached name → ROR resolver
  connectors/
    base.py            Connector ABC + polite HttpClient (fetch/normalize/emit)
    nsf.py             reference connector: NSF Award Search
scripts/
  ingest_nsf.py        run the NSF connector (sample-friendly)
  build_db.py          parquet → research_atlas.duckdb (keys + indexes)
  build_sample.py      write a small committable slice under data/processed/sample/
docs/
  SCHEMA.md            the canonical schema, in prose
  ARCHITECTURE.md      data flow, idempotency, money, Publish-to-Bucket seam
tests/                 schema invariants + NSF normalizer (no network)
data/
  raw/                 raw source caches (gitignored)
  processed/           entity/edge parquet (gitignored except sample/)
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
