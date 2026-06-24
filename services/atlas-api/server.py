#!/usr/bin/env python3
"""research-atlas read-only query API.

A small FastAPI app that serves a *vetted, parameterized* query surface over the
research-atlas DuckDB. There is no arbitrary-SQL endpoint: the public can only
call the named endpoints below, each of which dispatches to a hand-written query
in :mod:`queries` with bound parameters and clamped result sizes.

Safety properties (see ``queries.py`` for the per-query guarantees):
  * DuckDB opened **read-only** (``read_only=True``) — the connection cannot
    write, attach, or install extensions to the file.
  * Every user input is a bound parameter or a closed-set enum; never SQL text.
  * Result sizes are clamped to ``queries.MAX_LIMIT``.
  * CORS limited to bucket.foundation + localhost (``ATLAS_CORS`` env).
  * Per-IP rate limit (``ATLAS_RATE`` req/min).
  * **No PII.** Funder / org / field / aggregate-level data only; the one
    person-adjacent endpoint returns aggregate counts from the PII-free
    ``researchers_public`` view — never a name, ORCID, email, or contact field.

Endpoints (all GET, JSON out):
  GET /healthz                          liveness + graph stats
  GET /stats                            graph totals (entities, edges, USD)
  GET /funders?limit=                   all funders + grant counts
  GET /funder/<id>/portfolio?level=&limit=   a funder's field breakdown
  GET /field/<id>/top-funders?limit=    top funders of a field
  GET /field/<id>/top-works?limit=      most-cited works in a field
  GET /org/<ror>/summary                a ROR org's grants/USD/works
  GET /search?q=&kind=&limit=           name search (funder/field/org)
  GET /metascience/<name>?...           curated metascience queries

Run:
  uvicorn server:app --host 127.0.0.1 --port 8092
Env:
  ATLAS_DB     path to research_atlas.duckdb (default: repo/research_atlas.duckdb)
  ATLAS_CORS   comma-sep allowed origins (default: bucket.foundation set)
  ATLAS_RATE   requests/min/IP (default 120)
  PORT         listen port (default 8092)
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from threading import Lock

import duckdb
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# The query surface is identical in shape between the full graph and the slim,
# pre-aggregated DB. ATLAS_SLIM=1 (or a *_slim.duckdb path) selects the slim
# module so the same server serves either DB transparently.
if os.environ.get("ATLAS_SLIM") == "1" or "_slim" in os.environ.get("ATLAS_DB", ""):
    import queries_slim as queries
else:
    import queries

# --------------------------------------------------------------------------- #
#  Config                                                                      #
# --------------------------------------------------------------------------- #
_DEFAULT_DB = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "research_atlas.duckdb")
)
DB_PATH = os.environ.get("ATLAS_DB", _DEFAULT_DB)

DEFAULT_CORS = (
    "https://bucket.foundation,https://www.bucket.foundation,"
    "https://bucket-foundation.vercel.app,http://localhost:3000"
)
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ATLAS_CORS", DEFAULT_CORS).split(",") if o.strip()
]
RATE_PER_MIN = int(os.environ.get("ATLAS_RATE", "120"))
CACHE_TTL_S = int(os.environ.get("ATLAS_CACHE_TTL", "300"))
CACHE_MAX = int(os.environ.get("ATLAS_CACHE_MAX", "512"))

# --------------------------------------------------------------------------- #
#  Read-only DuckDB connection (one per process, reused under a lock)          #
# --------------------------------------------------------------------------- #
_con: duckdb.DuckDBPyConnection | None = None
_con_lock = Lock()


def get_con() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        # read_only=True is the hard guarantee: the file cannot be mutated.
        _con = duckdb.connect(DB_PATH, read_only=True)
    return _con


# --------------------------------------------------------------------------- #
#  Tiny TTL cache for hot queries (key -> (ts, value))                         #
# --------------------------------------------------------------------------- #
_cache: "dict[str, tuple[float, object]]" = {}
_cache_lock = Lock()


def cached(key: str, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    val = fn()
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
        _cache[key] = (now, val)
    return val


def run(key: str, fn):
    """Run a query under the connection lock, with TTL caching."""
    def _do():
        with _con_lock:
            return fn(get_con())
    return cached(key, _do)


# --------------------------------------------------------------------------- #
#  Per-IP rate limiter (in-process; nginx also rate-limits at the edge)        #
# --------------------------------------------------------------------------- #
_hits: "defaultdict[str, deque]" = defaultdict(deque)
_hits_lock = Lock()


def rate_ok(ip: str) -> bool:
    now = time.time()
    with _hits_lock:
        dq = _hits[ip]
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= RATE_PER_MIN:
            return False
        dq.append(now)
        return True


# --------------------------------------------------------------------------- #
#  App                                                                         #
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # warm the connection + a couple of hot queries
    try:
        run("stats", queries.stats)
    except Exception:
        pass
    yield


app = FastAPI(title="research-atlas API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
    max_age=600,
)


@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    if request.url.path not in ("/healthz", "/"):
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else "?"))
        if not rate_ok(ip):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
    return await call_next(request)


# --------------------------------------------------------------------------- #
#  Endpoints                                                                   #
# --------------------------------------------------------------------------- #
@app.get("/")
@app.get("/healthz")
def healthz():
    try:
        s = run("stats", queries.stats)
        return {
            "ok": True,
            "service": "research-atlas-api",
            "db": os.path.basename(DB_PATH),
            "read_only": True,
            "funders": s["funders"],
            "grants": s["grants"],
            "works": s["works"],
            "fields": s["fields"],
        }
    except Exception as e:  # pragma: no cover - liveness should never 500
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.get("/stats")
def stats():
    return run("stats", queries.stats)


@app.get("/funders")
def funders(limit: int = queries.DEFAULT_LIMIT):
    lim = queries.clamp_limit(limit)
    return {"funders": run(f"funders:{lim}", lambda c: queries.funders(c, lim))}


@app.get("/funder/{funder_id}/portfolio")
def funder_portfolio(funder_id: str, level: str = "topic",
                     limit: int = queries.DEFAULT_LIMIT):
    fid = queries.clean_term(funder_id)
    lim = queries.clamp_limit(limit)
    rows = run(
        f"fport:{fid}:{level}:{lim}",
        lambda c: queries.funder_portfolio(c, fid, level, lim),
    )
    return {"funder_id": fid, "level": level, "portfolio": rows}


@app.get("/field/{field_id}/top-funders")
def field_top_funders(field_id: str, limit: int = queries.DEFAULT_LIMIT):
    fid = queries.clean_term(field_id)
    lim = queries.clamp_limit(limit)
    rows = run(
        f"ftf:{fid}:{lim}",
        lambda c: queries.field_top_funders(c, fid, lim),
    )
    return {"field_id": fid, "top_funders": rows}


@app.get("/field/{field_id}/top-works")
def field_top_works(field_id: str, limit: int = queries.DEFAULT_LIMIT):
    fid = queries.clean_term(field_id)
    lim = queries.clamp_limit(limit)
    rows = run(
        f"ftw:{fid}:{lim}",
        lambda c: queries.field_top_works(c, fid, lim),
    )
    return {"field_id": fid, "top_works": rows}


@app.get("/org/{ror:path}/summary")
def org_summary(ror: str):
    # ROR ids look like "https://ror.org/03qyfje32"; the :path converter keeps
    # the slashes. We also accept the bare id.
    r = queries.clean_term(ror)
    if r and not r.startswith("http"):
        r = "https://ror.org/" + r.lstrip("/")
    row = run(f"org:{r}", lambda c: queries.org_summary(c, r))
    if row is None:
        raise HTTPException(status_code=404, detail="org not found (must be ROR-resolved)")
    return row


@app.get("/search")
def search(q: str = "", kind: str = "all", limit: int = queries.DEFAULT_LIMIT):
    term = queries.clean_term(q)
    if not term:
        return {"query": "", "results": []}
    lim = queries.clamp_limit(limit)
    rows = run(
        f"search:{kind}:{lim}:{term.lower()}",
        lambda c: queries.search(c, term, kind, lim),
    )
    return {"query": term, "kind": kind, "results": rows}


@app.get("/metascience")
def metascience_list():
    return {"queries": sorted(queries.METASCIENCE.keys())}


@app.get("/metascience/{name}")
def metascience(name: str, request: Request):
    fn = queries.METASCIENCE.get(name)
    if fn is None:
        raise HTTPException(status_code=404, detail="unknown metascience query")
    qp = request.query_params
    limit = queries.clamp_limit(qp.get("limit", queries.DEFAULT_LIMIT))
    kwargs: dict = {"limit": limit}
    if name == "top_funders_by_output":
        kwargs["topic"] = queries.clean_term(qp.get("topic", "mitochondri"))
        if "year_from" in qp:
            kwargs["year_from"] = queries.clamp_year(qp.get("year_from"), 2018)
        if "year_to" in qp:
            kwargs["year_to"] = queries.clamp_year(qp.get("year_to"), 2025)
    key = f"meta:{name}:{limit}:{kwargs.get('topic','')}:{kwargs.get('year_from','')}:{kwargs.get('year_to','')}"
    rows = run(key, lambda c: fn(c, **kwargs))
    return {"query": name, "params": kwargs, "rows": rows}
