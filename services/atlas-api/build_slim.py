#!/usr/bin/env python3
"""Build a SLIM, read-only DuckDB for the atlas API.

The full ``research_atlas.duckdb`` is ~3.2 GB — its bulk is the ``grant``
abstracts, the 1.4M-row ``person`` table, and the multi-million-row raw edge
tables. None of that is needed to *serve* the public endpoints: every endpoint
returns funder / org / field / aggregate-level data.

This script pre-computes, from the full graph, exactly the (much smaller) tables
the API needs, into a new DuckDB. The slim DB ships to the box in seconds instead
of hours. The API auto-detects the slim DB (it has the same logical tables /
views via ``ATLAS_SLIM=1``) and serves identical responses.

Materialized tables (all derived, no PII):
  * ``funder``                  — funder rows + a ``grants`` count column
  * ``field``                   — field rows (id, name, level)
  * ``org_summary``             — per-ROR org aggregate (grants, usd, works)
  * ``work_slim``               — work rows (no abstract) for top-works
  * ``funder_field``            — (funder, field) -> works, grants  (portfolios)
  * ``field_funder``            — (field, funder) -> works, grants  (top-funders)
  * ``field_work``              — (field, work)   for field top-works
  * ``stats``                   — one-row graph totals
  * ``cross_funder_orgs``       — precomputed metascience table
  * ``funding_by_country``      — precomputed metascience table
  * ``researcher_segment``      — aggregate researcher COUNTS only (PII-free)

Usage:
  python services/atlas-api/build_slim.py \
      --src research_atlas.duckdb --out research_atlas_slim.duckdb
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the slim atlas API DuckDB.")
    repo = Path(__file__).resolve().parents[2]
    ap.add_argument("--src", default=str(repo / "research_atlas.duckdb"))
    ap.add_argument("--out", default=str(repo / "research_atlas_slim.duckdb"))
    args = ap.parse_args()

    import duckdb

    out = Path(args.out)
    if out.exists():
        out.unlink()

    con = duckdb.connect(str(out))
    con.execute(f"ATTACH '{args.src}' AS srcdb (READ_ONLY)")

    say = lambda *a: print(*a, flush=True)  # noqa: E731

    # --- funder (+ grant count) ------------------------------------------- #
    say("funder…")
    con.execute("""
        CREATE TABLE funder AS
        SELECT f.atlas_id, f.name, f.short_name, f.country_code, f.funder_type,
               f.ror_id, f.homepage,
               count(DISTINCT fg.dst_id) AS grants
        FROM srcdb.funder f
        LEFT JOIN srcdb.funder_grant fg ON fg.src_id = f.atlas_id
        GROUP BY 1,2,3,4,5,6,7
    """)

    # --- field ------------------------------------------------------------ #
    say("field…")
    con.execute("""
        CREATE TABLE field AS
        SELECT atlas_id, name, openalex_id, level FROM srcdb.field
    """)

    # --- work_slim (no abstract; works only carry public output metadata) -- #
    say("work_slim…")
    con.execute("""
        CREATE TABLE work_slim AS
        SELECT atlas_id, title, doi, openalex_id, publication_year, type,
               cited_by_count, is_oa
        FROM srcdb.work
    """)

    # --- funder_field portfolio (funder -> grant -> work -> field) -------- #
    say("funder_field (portfolios)…")
    con.execute("""
        CREATE TABLE funder_field AS
        SELECT fg.src_id AS funder_id,
               fld.atlas_id AS field_id, fld.name AS field, fld.level,
               count(DISTINCT w.atlas_id) AS works,
               count(DISTINCT g.atlas_id) AS grants
        FROM srcdb.funder_grant fg
        JOIN srcdb.grant g       ON g.atlas_id = fg.dst_id
        JOIN srcdb.grant_work gw ON gw.src_id = g.atlas_id
        JOIN srcdb.work w        ON w.atlas_id = gw.dst_id
        JOIN srcdb.work_field wf ON wf.src_id = w.atlas_id
        JOIN srcdb.field fld     ON fld.atlas_id = wf.dst_id
        GROUP BY 1,2,3,4
    """)

    # --- field_funder (top funders of a field) --------------------------- #
    say("field_funder (field top-funders)…")
    con.execute("""
        CREATE TABLE field_funder AS
        SELECT wf.dst_id AS field_id,
               f.atlas_id AS funder_id, f.name AS funder, f.short_name,
               f.country_code,
               count(DISTINCT w.atlas_id) AS works,
               count(DISTINCT g.atlas_id) AS grants
        FROM srcdb.work_field wf
        JOIN srcdb.work w        ON w.atlas_id = wf.src_id
        JOIN srcdb.grant_work gw ON gw.dst_id = w.atlas_id
        JOIN srcdb.grant g       ON g.atlas_id = gw.src_id
        JOIN srcdb.funder_grant fg ON fg.dst_id = g.atlas_id
        JOIN srcdb.funder f      ON f.atlas_id = fg.src_id
        GROUP BY 1,2,3,4,5
    """)

    # --- field_work (field top-works) ------------------------------------ #
    say("field_work (field top-works)…")
    con.execute("""
        CREATE TABLE field_work AS
        SELECT wf.dst_id AS field_id, w.atlas_id AS work_id,
               w.title, w.doi, w.openalex_id, w.publication_year, w.type,
               w.cited_by_count, w.is_oa
        FROM srcdb.work_field wf
        JOIN srcdb.work w ON w.atlas_id = wf.src_id
        WHERE w.cited_by_count IS NOT NULL
    """)

    # --- org_summary (per-ROR aggregate) --------------------------------- #
    say("org_summary…")
    con.execute("""
        CREATE TABLE org_summary AS
        WITH og AS (
            SELECT o.ror_id, go.src_id AS grant_id
            FROM srcdb.grant_org go
            JOIN srcdb.organization o ON o.atlas_id = go.dst_id
            WHERE go.role = 'recipient' AND o.ror_id IS NOT NULL
        ),
        agg AS (
            SELECT og.ror_id,
                   count(DISTINCT og.grant_id) AS grants,
                   round(sum(g.amount_usd))    AS usd_funded,
                   count(DISTINCT gw.dst_id)   AS works
            FROM og
            LEFT JOIN srcdb.grant g      ON g.atlas_id = og.grant_id
            LEFT JOIN srcdb.grant_work gw ON gw.src_id = og.grant_id
            GROUP BY 1
        )
        SELECT o.atlas_id, o.name, o.ror_id, o.country_code, o.city, o.org_type,
               o.homepage,
               coalesce(a.grants, 0) AS grants, a.usd_funded,
               coalesce(a.works, 0) AS works
        FROM srcdb.organization o
        LEFT JOIN agg a ON a.ror_id = o.ror_id
        WHERE o.ror_id IS NOT NULL
    """)

    # --- stats (one row) -------------------------------------------------- #
    say("stats…")
    con.execute("""
        CREATE TABLE stats AS
        SELECT
          (SELECT count(*) FROM srcdb.funder)       AS funders,
          (SELECT count(*) FROM srcdb.grant)        AS grants,
          (SELECT count(*) FROM srcdb.organization) AS organizations,
          (SELECT count(*) FROM srcdb.person)       AS persons,
          (SELECT count(*) FROM srcdb.work)         AS works,
          (SELECT count(*) FROM srcdb.field)        AS fields,
          (SELECT count(*) FROM srcdb.funder_grant) AS funder_grant_edges,
          (SELECT count(*) FROM srcdb.grant_org)    AS grant_org_edges,
          (SELECT count(*) FROM srcdb.grant_person) AS grant_person_edges,
          (SELECT count(*) FROM srcdb.grant_work)   AS grant_work_edges,
          (SELECT count(*) FROM srcdb.person_org)   AS person_org_edges,
          (SELECT count(*) FROM srcdb.work_field)   AS work_field_edges,
          (SELECT round(sum(amount_usd)) FROM srcdb.grant WHERE amount_usd IS NOT NULL) AS usd_funded
    """)

    # --- metascience: cross_funder_orgs ----------------------------------- #
    say("cross_funder_orgs…")
    con.execute("""
        CREATE TABLE cross_funder_orgs AS
        SELECT o.name, o.country_code, o.ror_id,
               count(DISTINCT f.atlas_id) AS funders,
               string_agg(DISTINCT f.short_name, ', ') AS funder_list,
               count(DISTINCT g.atlas_id) AS grants
        FROM srcdb.grant_org go
        JOIN srcdb.organization o   ON o.atlas_id = go.dst_id
        JOIN srcdb.grant g          ON g.atlas_id = go.src_id
        JOIN srcdb.funder_grant fg  ON fg.dst_id = g.atlas_id
        JOIN srcdb.funder f         ON f.atlas_id = fg.src_id
        WHERE go.role = 'recipient' AND o.ror_id IS NOT NULL
        GROUP BY 1,2,3
        HAVING count(DISTINCT f.atlas_id) >= 2
        ORDER BY funders DESC, grants DESC
        LIMIT 500
    """)

    # --- metascience: funding_by_country ---------------------------------- #
    say("funding_by_country…")
    con.execute("""
        CREATE TABLE funding_by_country AS
        SELECT o.country_code,
               count(DISTINCT g.atlas_id) AS grants,
               round(sum(g.amount_usd))   AS usd_funded
        FROM srcdb.grant_org go
        JOIN srcdb.organization o ON o.atlas_id = go.dst_id
        JOIN srcdb.grant g        ON g.atlas_id = go.src_id
        WHERE go.role = 'recipient' AND o.country_code IS NOT NULL
        GROUP BY 1
        ORDER BY usd_funded DESC NULLS LAST
    """)

    # --- metascience: researcher segment COUNTS (PII-free) ---------------- #
    say("researcher_segment (counts only)…")
    has_rp = con.execute(
        "SELECT count(*) FROM duckdb_tables() "
        "WHERE database_name = 'srcdb' AND table_name = 'researchers_public' "
        "UNION ALL SELECT count(*) FROM duckdb_views() "
        "WHERE database_name = 'srcdb' AND view_name = 'researchers_public'"
    ).fetchall()
    has_rp = any(r[0] for r in has_rp)
    if has_rp:
        con.execute("""
            CREATE TABLE researcher_segment AS
            SELECT field_slug, seniority, activity_tier, count(*) AS researchers
            FROM srcdb.researchers_public
            GROUP BY 1,2,3
            ORDER BY researchers DESC
        """)
    else:
        con.execute(
            "CREATE TABLE researcher_segment("
            "field_slug VARCHAR, seniority VARCHAR, activity_tier VARCHAR, "
            "researchers BIGINT)")

    # --- indexes for fast point lookups ----------------------------------- #
    say("indexes…")
    con.execute("CREATE INDEX ix_funder_id ON funder(atlas_id)")
    con.execute("CREATE INDEX ix_field_id ON field(atlas_id)")
    con.execute("CREATE INDEX ix_ff_funder ON funder_field(funder_id)")
    con.execute("CREATE INDEX ix_fnd_field ON field_funder(field_id)")
    con.execute("CREATE INDEX ix_fw_field ON field_work(field_id)")
    con.execute("CREATE INDEX ix_org_ror ON org_summary(ror_id)")

    con.execute("DETACH srcdb")
    con.close()

    sz = os.path.getsize(out) / 1e6
    say(f"\nBuilt {out}  ({sz:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
