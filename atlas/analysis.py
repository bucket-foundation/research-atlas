"""Metascience query affordances over the research-atlas DuckDB.

This is the substrate for *our own* studies of the research economy -- funding
flows, who-funds-what, field dynamics, org productivity. Every function takes a
DuckDB connection (or opens the default db) and returns a list of dict rows, so
they compose into notebooks, the CLI (``scripts/query.py``), or a service.

The queries are written against the canonical graph tables and lean on the
indexes ``build_db.py`` creates (entity ``atlas_id`` PKs, edge ``src_id``/
``dst_id``). They are read-only.
"""

from __future__ import annotations

from pathlib import Path

from atlas.connectors.base import REPO_ROOT

DEFAULT_DB = REPO_ROOT / "research_atlas.duckdb"


def connect(db_path: str | Path | None = None):
    import duckdb
    return duckdb.connect(str(db_path or DEFAULT_DB), read_only=True)


def _rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def top_funders_by_output(con, topic_like: str, year_from: int = 2018,
                          year_to: int = 2025, limit: int = 15) -> list[dict]:
    """Top funders of works in a topic/field, by linked-work count.

    Joins funder -> grant -> work -> field. ``topic_like`` is matched
    case-insensitively against the field name at any level of the OpenAlex
    taxonomy (topic/subfield/field/domain), so "mitochondri" catches the
    mitochondrial-biophysics topics.
    """
    return _rows(con.execute("""
        SELECT f.name AS funder, f.short_name, f.country_code,
               count(DISTINCT w.atlas_id) AS works,
               count(DISTINCT g.atlas_id) AS grants
        FROM field fld
        JOIN work_field wf ON wf.dst_id = fld.atlas_id
        JOIN work w        ON w.atlas_id = wf.src_id
        JOIN grant_work gw ON gw.dst_id = w.atlas_id
        JOIN grant g       ON g.atlas_id = gw.src_id
        JOIN funder_grant fg ON fg.dst_id = g.atlas_id
        JOIN funder f      ON f.atlas_id = fg.src_id
        WHERE lower(fld.name) LIKE lower(?)
          AND w.publication_year BETWEEN ? AND ?
        GROUP BY 1, 2, 3
        ORDER BY works DESC
        LIMIT ?
    """, [f"%{topic_like}%", year_from, year_to, limit]))


def org_funding_vs_output(con, min_grants: int = 50, limit: int = 25) -> list[dict]:
    """Per-org funding $ vs research output -- the productivity scatter.

    For each ROR-resolved org: total USD received (sum over recipient grants)
    and the number of distinct works those grants produced. Restricted to orgs
    with at least ``min_grants`` recipient grants so the ratio is meaningful.
    """
    return _rows(con.execute("""
        WITH org_grants AS (
            SELECT go.dst_id AS org_id, g.atlas_id AS grant_id, g.amount_usd
            FROM grant_org go
            JOIN grant g ON g.atlas_id = go.src_id
            WHERE go.role = 'recipient'
        )
        SELECT o.name, o.country_code, o.ror_id,
               count(DISTINCT og.grant_id) AS grants,
               sum(og.amount_usd) AS usd_funded,
               count(DISTINCT gw.dst_id) AS works,
               round(count(DISTINCT gw.dst_id)
                     / nullif(sum(og.amount_usd), 0) * 1e6, 3) AS works_per_million_usd
        FROM org_grants og
        JOIN organization o ON o.atlas_id = og.org_id
        LEFT JOIN grant_work gw ON gw.src_id = og.grant_id
        WHERE o.ror_id IS NOT NULL
        GROUP BY 1, 2, 3
        HAVING count(DISTINCT og.grant_id) >= ?
        ORDER BY usd_funded DESC NULLS LAST
        LIMIT ?
    """, [min_grants, limit]))


def rising_fields(con, early_from: int = 2016, early_to: int = 2019,
                  late_from: int = 2021, late_to: int = 2024,
                  min_late: int = 30, limit: int = 20) -> list[dict]:
    """Rising-field detection: topics whose linked-work volume grew most.

    Compares funded-work counts per topic between an early and a late window and
    ranks by growth ratio. Only topics with at least ``min_late`` works in the
    late window are considered (avoids divide-by-noise on tiny topics).
    """
    return _rows(con.execute("""
        WITH counts AS (
            SELECT fld.atlas_id, fld.name, fld.level,
                   count(DISTINCT w.atlas_id) FILTER (
                       WHERE w.publication_year BETWEEN ? AND ?) AS early,
                   count(DISTINCT w.atlas_id) FILTER (
                       WHERE w.publication_year BETWEEN ? AND ?) AS late
            FROM field fld
            JOIN work_field wf ON wf.dst_id = fld.atlas_id
            JOIN work w        ON w.atlas_id = wf.src_id
            JOIN grant_work gw ON gw.dst_id = w.atlas_id  -- funded works only
            WHERE fld.level = 'topic'
            GROUP BY 1, 2, 3
        )
        SELECT name, early, late,
               round(late / nullif(early, 0)::DOUBLE, 2) AS growth_ratio
        FROM counts
        WHERE late >= ?
        ORDER BY growth_ratio DESC NULLS LAST, late DESC
        LIMIT ?
    """, [early_from, early_to, late_from, late_to, min_late, limit]))


def cross_funder_orgs(con, limit: int = 20) -> list[dict]:
    """Orgs funded by the most distinct funders -- the cross-funded hubs.

    A side effect of merging orgs on ROR across funders: we can now see which
    institutions pull money from NIH *and* NSF *and* the EC *and* UKRI.
    """
    return _rows(con.execute("""
        SELECT o.name, o.country_code, o.ror_id,
               count(DISTINCT f.atlas_id) AS funders,
               string_agg(DISTINCT f.short_name, ', ') AS funder_list,
               count(DISTINCT g.atlas_id) AS grants
        FROM grant_org go
        JOIN organization o   ON o.atlas_id = go.dst_id
        JOIN grant g          ON g.atlas_id = go.src_id
        JOIN funder_grant fg  ON fg.dst_id = g.atlas_id
        JOIN funder f         ON f.atlas_id = fg.src_id
        WHERE go.role = 'recipient' AND o.ror_id IS NOT NULL
        GROUP BY 1, 2, 3
        ORDER BY funders DESC, grants DESC
        LIMIT ?
    """, [limit]))


def funding_by_country(con, limit: int = 20) -> list[dict]:
    """Total recipient funding $ by org country -- the geography of money."""
    return _rows(con.execute("""
        SELECT o.country_code,
               count(DISTINCT g.atlas_id) AS grants,
               sum(g.amount_usd) AS usd_funded
        FROM grant_org go
        JOIN organization o ON o.atlas_id = go.dst_id
        JOIN grant g        ON g.atlas_id = go.src_id
        WHERE go.role = 'recipient' AND o.country_code IS NOT NULL
        GROUP BY 1
        ORDER BY usd_funded DESC NULLS LAST
        LIMIT ?
    """, [limit]))


def researchers_by_segment(con, limit: int = 30) -> list[dict]:
    """Researcher/user counts by field x seniority x activity tier.

    Reads the PII-free ``researchers_public`` view so this query can never
    surface contact data. The basis for tool-targeting: who, in which field, at
    which career stage, is active.
    """
    return _rows(con.execute("""
        SELECT field_slug, seniority, activity_tier,
               count(*) AS researchers,
               sum(CASE WHEN is_corresponding_author THEN 1 ELSE 0 END) AS corresponding
        FROM researchers_public
        GROUP BY 1, 2, 3
        ORDER BY researchers DESC
        LIMIT ?
    """, [limit]))


def tool_targets(con, tool_slug: str, limit: int = 25) -> list[dict]:
    """Active-PI researchers whose ``tool_fit`` includes a given roadmap tool.

    Joins a cross-field software need (from ``docs/USERS_NEEDS.md``) to the exact
    active researchers who need it -- the discovery layer of the tool roadmap.
    PII-free (``researchers_public``); pair with the contactable view for the
    public-contact subset (governed by ``docs/USERS_POLICY.md``).
    """
    return _rows(con.execute("""
        SELECT field_slug, full_name, primary_org_name, country_code,
               works_count, h_index_proxy, seniority, segment
        FROM researchers_public
        WHERE tool_fit LIKE ?
          AND activity_tier = 'active-pi'
        ORDER BY h_index_proxy DESC, works_count DESC
        LIMIT ?
    """, [f"%{tool_slug}%", limit]))


QUERIES = {
    "top_funders_by_output": top_funders_by_output,
    "org_funding_vs_output": org_funding_vs_output,
    "rising_fields": rising_fields,
    "cross_funder_orgs": cross_funder_orgs,
    "funding_by_country": funding_by_country,
    "researchers_by_segment": researchers_by_segment,
    "tool_targets": tool_targets,
}
