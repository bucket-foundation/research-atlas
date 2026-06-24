# research-atlas query API — Deploy Runbook

**Target:** `https://atlas-api.agfarms.dev` on prod-hetzner-1 (Hetzner CPX42),
served as a **systemd `--user` uvicorn process behind host nginx + Let's
Encrypt** — the same proven pattern as the Polingual Photon API
(`bucket-foundation/services/photon-api`), **not** k3s. It is a plain user-space
process that touches no tenant namespace.

A read-only, *vetted* query surface over the research-atlas DuckDB graph
(`research_atlas.duckdb`, ~3.2 GB). There is **no arbitrary-SQL endpoint** — the
public can only call the named endpoints, each backed by a hand-written,
parameter-bound query in `queries.py`.

---

## 0. What ships

| File | Role |
|---|---|
| `server.py` | FastAPI app. Opens DuckDB **`read_only=True`**, CORS to bucket.foundation + localhost, per-IP rate limit, TTL cache. |
| `queries.py` | The safety boundary: every query is a fixed SQL string with `?` placeholders; clamps + closed-set enums; no PII. |
| `requirements.txt` | `fastapi`, `uvicorn[standard]`, `duckdb`. CPU-only, no GPU, no ML. |
| `atlas-api.service` | systemd `--user` unit (127.0.0.1:8092, MemoryMax 3.5G). |
| `atlas-api.agfarms.dev.nginx` | host nginx vhost (TLS → 127.0.0.1:8092). |
| `deploy.sh` | idempotent end-to-end deploy (rsync DB+code, venv, service, nginx+cert, verify). |

The DuckDB file is **gitignored** (`*.duckdb`); it is rsynced to the box by
`deploy.sh` (or rebuilt there with `scripts/build_db.py` from the parquet).

---

## 1. Prerequisites (satisfied on prod-hetzner-1)

- DNS: `atlas-api.agfarms.dev` resolves via the Cloudflare `*.agfarms.dev`
  wildcard (`5.161.236.151`) — no DNS change needed.
- Host nginx + certbot (Let's Encrypt) — same as the other `*.agfarms.dev`
  service vhosts. nginx <1.25.1 uses the legacy `listen 443 ssl;` form.
- `loginctl enable-linger` for the deploy user (so the `--user` service runs
  without an active login) — `deploy.sh` sets it.
- Env: `AGFARMS_PASS` (SSH+sudo), optional `AGFARMS_HOST`
  (default `giany@5.161.236.151`).

---

## 2. Deploy (one command)

```bash
cd ~/agfarms/research-atlas
export AGFARMS_PASS=...        # SSH + sudo password for the box
bash services/atlas-api/deploy.sh
```

It rsyncs the 3.2 GB `research_atlas.duckdb` (`--partial --inplace`, so a dropped
transfer resumes), installs the venv, starts the systemd `--user` service,
issues the TLS cert, installs the nginx vhost, and verifies live over HTTPS.

### Alternative: rebuild on the box (skip the 3.2 GB upload)

If you have the processed parquet on the box, build the DB there instead of
uploading it:

```bash
# on the box, in a checkout with data/processed/*.parquet present:
python scripts/build_db.py --db ~/research-atlas-api/research_atlas.duckdb
# then run deploy.sh — the rsync of an identical file is a near-noop.
```

---

## 3. Verify (what to check live)

```bash
curl https://atlas-api.agfarms.dev/healthz          # 200 {ok:true, funders, grants, ...}
curl https://atlas-api.agfarms.dev/stats            # graph totals
curl "https://atlas-api.agfarms.dev/funders?limit=5"
curl "https://atlas-api.agfarms.dev/search?q=crispr&kind=field"
curl "https://atlas-api.agfarms.dev/field/<id>/top-works?limit=5"
curl -I http://atlas-api.agfarms.dev/healthz        # 301 -> https
# CORS: Origin https://bucket.foundation echoed; https://evil.example NOT echoed.
```

---

## 4. Endpoints

| Endpoint | Returns |
|---|---|
| `GET /healthz`, `/` | liveness + headline counts |
| `GET /stats` | entity + edge counts, total USD funded |
| `GET /funders?limit=` | all funders + grant counts |
| `GET /funder/<id>/portfolio?level=&limit=` | field/topic breakdown of a funder's output |
| `GET /field/<id>/top-funders?limit=` | top funders of a field |
| `GET /field/<id>/top-works?limit=` | most-cited works in a field |
| `GET /org/<ror>/summary` | a ROR org's grants / USD / works |
| `GET /search?q=&kind=&limit=` | name search (`funder`/`field`/`org`/`all`) |
| `GET /metascience` | list of curated metascience queries |
| `GET /metascience/<name>?...` | `top_funders_by_output`, `cross_funder_orgs`, `funding_by_country`, `rising_fields`, `researchers_by_segment` |

`<id>` is an atlas funder/field id (e.g. `fund:bd3ed9ed147523ef`,
`field:b40c4d96708c2c21`); `<ror>` is a ROR id (`https://ror.org/021nxhr62` or
bare `021nxhr62`).

---

## 5. Safety guarantees

- **read-only DuckDB** (`read_only=True`) — the connection cannot write/attach/
  install.
- **No arbitrary SQL.** Every query is a fixed string in `queries.py`; user
  input flows only through `?` bound parameters or closed-set enums (`level`,
  `kind`). SQL injection is impossible by construction (a value can never become
  SQL). Verified by `tests/test_atlas_api_queries.py`.
- **Bounded results.** Every `limit` is clamped to `MAX_LIMIT` (200); year
  ranges clamped to `[1900, 2100]`; search terms truncated.
- **No PII.** Funder / org / field / aggregate-level data only. The one
  person-adjacent query (`researchers_by_segment`) returns aggregate *counts*
  from the PII-free `researchers_public` view — never a name, ORCID, email, or
  contact field. The PII-bearing `researchers` table is never read; tests assert
  no public query references it or any contact column.
- **CORS** limited to `bucket.foundation` + localhost (`ATLAS_CORS`).
- **Rate limit** per-IP in-app (`ATLAS_RATE`, default 120/min) + nginx edge
  `limit_req`.
- **Cache** TTL on hot queries (`ATLAS_CACHE_TTL`, default 300s).

---

## 6. Wire Bucket → API (`ATLAS_API_URL`)

The Bucket Next proxy (`src/app/api/research/atlas/route.ts`) reads
`ATLAS_API_URL` and defaults to `https://atlas-api.agfarms.dev`, with a graceful
503 fallback when the API is unreachable.

```bash
cd ~/agfarms/bucket-foundation
printf 'https://atlas-api.agfarms.dev' | vercel env add ATLAS_API_URL production
printf 'https://atlas-api.agfarms.dev' | vercel env add ATLAS_API_URL preview
vercel --prod   # redeploy to pick it up
```

`ATLAS_API_URL` is **server-only** (route handlers, `runtime="nodejs"`); no
`NEXT_PUBLIC_` prefix.

---

## 7. Operate / redeploy / rollback

```bash
U() { sshpass -p "$AGFARMS_PASS" ssh "$AGFARMS_HOST" "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user $*"; }
U status atlas-api.service
U restart atlas-api.service
U stop atlas-api.service
sshpass -p "$AGFARMS_PASS" ssh "$AGFARMS_HOST" "journalctl --user -u atlas-api.service -n 100"

# New DB / code: re-run deploy.sh (idempotent; rsync only ships changed bytes).
```
