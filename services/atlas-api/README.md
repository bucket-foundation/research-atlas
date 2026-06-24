# atlas-api — research-atlas read-only query API

A small FastAPI service that makes the research-atlas graph **queryable by
researchers** over a *vetted, parameterized* HTTP surface. There is **no
arbitrary-SQL endpoint**: the public can only call the named endpoints below,
each backed by a hand-written, parameter-bound query.

**Live:** `https://atlas-api.agfarms.dev` (deployed as a systemd `--user`
uvicorn process behind host nginx + Let's Encrypt on prod-hetzner-1 — see
[`DEPLOY.md`](./DEPLOY.md)).

## Files

| File | Role |
|---|---|
| `server.py` | FastAPI app. DuckDB opened `read_only=True`, CORS, rate limit, TTL cache. Picks the slim or full query module by `ATLAS_SLIM`. |
| `queries.py` | Vetted query surface over the **full** graph. Fixed SQL, `?` bound params, clamps, no PII. The safety boundary. |
| `queries_slim.py` | Same public contract over the **slim** pre-aggregated DB. |
| `build_slim.py` | Builds `research_atlas_slim.duckdb` (~64 MB) from the full graph — exactly the aggregate tables the endpoints need. |
| `requirements.txt` | `fastapi`, `uvicorn[standard]`, `duckdb`. No GPU, no ML. |
| `atlas-api.service`, `*.nginx`, `deploy.sh` | Deploy artifacts. |

## Endpoints

`/healthz` · `/stats` · `/funders` · `/funder/<id>/portfolio` ·
`/field/<id>/top-funders` · `/field/<id>/top-works` · `/org/<ror>/summary` ·
`/search?q=&kind=` · `/metascience` + `/metascience/<name>`.

See `DEPLOY.md §4` for the full table.

## Run locally

```bash
# full graph
ATLAS_DB=../../research_atlas.duckdb uvicorn server:app --port 8092
# or the slim, pre-aggregated DB
python build_slim.py
ATLAS_SLIM=1 ATLAS_DB=../../research_atlas_slim.duckdb uvicorn server:app --port 8092
```

## Safety

Read-only DuckDB; every user input is a `?` bound parameter or a closed-set
enum (never SQL text); results clamped to `MAX_LIMIT`; CORS limited to
bucket.foundation + localhost; per-IP rate limit; **no PII** (funder / org /
field / aggregate-level only — the one person-adjacent query returns aggregate
counts from the PII-free `researchers_public` view). Enforced by
`tests/test_atlas_api_queries.py`.

## Deploy / slim vs full

The full DuckDB is ~3.2 GB; the **slim** DB (~64 MB, `build_slim.py`) serves
every endpoint identically and ships in seconds. `deploy.sh` ships the slim DB
by default; `ATLAS_FULL=1 bash deploy.sh` ships the full graph (slow rsync).
The live service runs the slim DB.
