# research-atlas — Canonical Schema

**Schema version: `0.1.0`** · source of truth: [`atlas/schema.py`](../atlas/schema.py)

research-atlas is a **normalized graph** of the global research economy. Six
entity types are connected by directed edge tables. Every row carries
**provenance** and an **`as_of`** timestamp, and every entity has a stable
**surrogate key** (`atlas_id`).

---

## Provenance (on every row)

| column       | meaning                                                          |
|--------------|------------------------------------------------------------------|
| `source`     | short source key (`nsf`, `openalex`, `nih`, `cordis`, …)         |
| `source_id`  | the record's id in that source's own namespace                   |
| `source_url` | canonical, citeable URL to the record at the source              |
| `as_of`      | ISO-8601 UTC timestamp the row was fetched / normalized          |

## Surrogate keys

Each entity's primary key is `atlas_id`, derived deterministically from the
most-stable identifier a connector can supply via `make_id(entity, *parts)`:

- prefer a **resolved global id** — ROR (org), ORCID (person), DOI/OpenAlex
  (work), OpenAlex topic id (field), Crossref Funder id (funder);
- otherwise fall back to `(source, source_id)`.

Same inputs → same id. This is what makes ingestion **idempotent**: re-running a
connector merges onto the existing node instead of duplicating it.

---

## Entities

### Funder
`atlas_id` · `name` · `short_name` · `country_code` · `funder_type`
(`government`/`private`/`nonprofit`/`corporate`/`supranational`) · `ror_id` ·
`crossref_funder_id` · `homepage` · *+ provenance*

### Grant / Award
`atlas_id` · `title` · `abstract` · `amount_original` · `currency` (ISO-4217) ·
`amount_usd` · `fx_rate_to_usd` · `fx_as_of` · `start_date` · `end_date` ·
`status` (`active`/`completed`/`terminated`/`unknown`) · `program` · *+ provenance*

> **Money invariant:** amounts are stored in the **original currency**
> (`amount_original` + `currency`) *and* a normalized **`amount_usd`**. Unknown
> money is **`null`**, never silent `0`. `coerce()` rejects `amount_usd == 0`.

### Organization
`atlas_id` (keyed on ROR where resolvable) · `name` · `ror_id` ·
`country_code` · `city` · `region` · `org_type`
(`education`/`government`/`company`/`nonprofit`/`facility`/`other`) ·
`homepage` · `lat` · `lon` · *+ provenance*

### Person
`atlas_id` (keyed on ORCID where resolvable) · `full_name` · `first_name` ·
`last_name` · `orcid` · `openalex_author_id` · *+ provenance*

### Work / Output
`atlas_id` (keyed on OpenAlex id / DOI) · `title` · `doi` · `openalex_id` ·
`publication_year` · `publication_date` · `type` · `cited_by_count` · `is_oa` ·
*+ provenance*

### Field / Topic
`atlas_id` (keyed on OpenAlex topic id) · `name` · `openalex_id` · `level`
(`topic`/`subfield`/`field`/`domain`) · `parent_atlas_id` · *+ provenance*

---

## Edges (relationships)

Edges are **directed** and carry their own provenance, so a single edge may be
attested by multiple sources. `src_id` / `dst_id` are entity `atlas_id`s.

| edge table     | direction          | extra columns | meaning                                  |
|----------------|--------------------|---------------|------------------------------------------|
| `funder_grant` | funder → grant     | `role`        | the funder awarded the grant             |
| `grant_org`    | grant → org        | `role`        | recipient / host organization            |
| `grant_person` | grant → person     | `role`        | PI / co-PI / program officer             |
| `grant_work`   | grant → work       | `role`        | a work acknowledges the grant's funding  |
| `person_org`   | person → org       | `role`        | affiliation                              |
| `work_field`   | work → field       | `score`       | the work belongs to a field/topic        |

Canonical roles live in `schema.ROLES` (`awarder`, `recipient`, `host`,
`subaward`, `pi`, `co-pi`, `program-officer`, `acknowledges`, `affiliation`).

---

## Storage

- **Entities + edges** → one partition-friendly **parquet per table** under
  `data/processed/`. Entities dedup on `atlas_id` (newest `as_of` wins); edges
  dedup on `(src_id, dst_id, role|score, source)`.
- **DuckDB** (`scripts/build_db.py`) loads the parquet into
  `research_atlas.duckdb` with a unique index on each entity `atlas_id` and
  indexes on edge `src_id`/`dst_id` for fast graph traversal.
- **Manifest** (`data/MANIFEST.json`) is the authoritative list of published
  datasets — path, schema version, row count, `as_of`, sources. **If a parquet
  is not in the manifest, treat it as not-published.**
