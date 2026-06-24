"""Slim-DB query surface — same public contract as :mod:`queries`, but reads the
pre-aggregated tables built by ``build_slim.py`` instead of traversing the full
graph.

Used when the API serves ``research_atlas_slim.duckdb`` (env ``ATLAS_SLIM=1``).
Every function has the *same name, signature, and return shape* as in
``queries`` so ``server.py`` can swap the module transparently. The same safety
rules hold: fixed SQL, ``?`` bound params only, clamped limits, no PII.
"""

from __future__ import annotations

# Reuse the validators + bounds + the static safety registries so the test-suite
# and server treat both modules identically.
from queries import (  # noqa: F401
    DEFAULT_LIMIT,
    FORBIDDEN_COLUMNS,
    FORBIDDEN_TABLES,
    MAX_LIMIT,
    MAX_SEARCH_LEN,
    MAX_YEAR,
    MIN_YEAR,
    clamp_limit,
    clamp_year,
    clean_term,
    _rows,
)

_FIELD_LEVELS = {"topic", "subfield", "field", "domain"}
_SEARCH_KINDS = {"funder", "field", "org", "all"}

STATS_SQL = "SELECT * FROM stats"


def stats(con) -> dict:
    return _rows(con.execute(STATS_SQL))[0]


FUNDERS_SQL = """
SELECT atlas_id, name, short_name, country_code, funder_type, ror_id, homepage,
       grants
FROM funder
ORDER BY grants DESC
LIMIT ?
"""


def funders(con, limit: int = DEFAULT_LIMIT) -> list[dict]:
    return _rows(con.execute(FUNDERS_SQL, [clamp_limit(limit)]))


FUNDER_PORTFOLIO_SQL = """
SELECT field_id, field, level, works, grants
FROM funder_field
WHERE funder_id = ? AND level = ?
ORDER BY works DESC
LIMIT ?
"""


def funder_portfolio(con, funder_id: str, level: str = "topic",
                     limit: int = DEFAULT_LIMIT) -> list[dict]:
    lvl = level if level in _FIELD_LEVELS else "topic"
    return _rows(con.execute(
        FUNDER_PORTFOLIO_SQL, [clean_term(funder_id), lvl, clamp_limit(limit)]))


FIELD_TOP_FUNDERS_SQL = """
SELECT funder_id AS atlas_id, funder, short_name, country_code, works, grants
FROM field_funder
WHERE field_id = ?
ORDER BY works DESC
LIMIT ?
"""


def field_top_funders(con, field_id: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    return _rows(con.execute(
        FIELD_TOP_FUNDERS_SQL, [clean_term(field_id), clamp_limit(limit)]))


FIELD_TOP_WORKS_SQL = """
SELECT work_id AS atlas_id, title, doi, openalex_id, publication_year, type,
       cited_by_count, is_oa
FROM field_work
WHERE field_id = ?
ORDER BY cited_by_count DESC
LIMIT ?
"""


def field_top_works(con, field_id: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    return _rows(con.execute(
        FIELD_TOP_WORKS_SQL, [clean_term(field_id), clamp_limit(limit)]))


ORG_SUMMARY_SQL = """
SELECT atlas_id, name, ror_id, country_code, city, org_type, homepage,
       grants, usd_funded, works
FROM org_summary
WHERE ror_id = ?
LIMIT 1
"""


def org_summary(con, ror: str) -> dict | None:
    rows = _rows(con.execute(ORG_SUMMARY_SQL, [clean_term(ror)]))
    return rows[0] if rows else None


SEARCH_FUNDERS_SQL = """
SELECT 'funder' AS kind, atlas_id AS id, name, short_name AS detail, country_code
FROM funder
WHERE lower(name) LIKE lower(?) OR lower(coalesce(short_name,'')) LIKE lower(?)
ORDER BY length(name)
LIMIT ?
"""

SEARCH_FIELDS_SQL = """
SELECT 'field' AS kind, atlas_id AS id, name, level AS detail, NULL AS country_code
FROM field
WHERE lower(name) LIKE lower(?)
ORDER BY (level = 'field') DESC, length(name)
LIMIT ?
"""

SEARCH_ORGS_SQL = """
SELECT 'org' AS kind, ror_id AS id, name, org_type AS detail, country_code
FROM org_summary
WHERE lower(name) LIKE lower(?)
ORDER BY length(name)
LIMIT ?
"""


def search(con, term: str, kind: str = "all", limit: int = DEFAULT_LIMIT) -> list[dict]:
    k = kind if kind in _SEARCH_KINDS else "all"
    t = clean_term(term)
    if not t:
        return []
    like = f"%{t}%"
    lim = clamp_limit(limit)
    out: list[dict] = []
    if k in ("funder", "all"):
        out += _rows(con.execute(SEARCH_FUNDERS_SQL, [like, like, lim]))
    if k in ("field", "all"):
        out += _rows(con.execute(SEARCH_FIELDS_SQL, [like, lim]))
    if k in ("org", "all"):
        out += _rows(con.execute(SEARCH_ORGS_SQL, [like, lim]))
    return out[:lim]


# --- metascience (read precomputed tables) -------------------------------- #

TOP_FUNDERS_BY_OUTPUT_SQL = """
SELECT funder, short_name, country_code, sum(works) AS works, sum(grants) AS grants
FROM field_funder ff
JOIN field fld ON fld.atlas_id = ff.field_id
WHERE lower(fld.name) LIKE lower(?)
GROUP BY funder, short_name, country_code
ORDER BY works DESC
LIMIT ?
"""


def top_funders_by_output(con, topic: str, year_from: int = 2018,
                          year_to: int = 2025, limit: int = 15) -> list[dict]:
    # Note: the slim build aggregates across all years (no per-year work rows are
    # carried), so year_from/year_to are accepted + clamped for API parity but do
    # not subset here. The full-DB module honors them precisely.
    clamp_year(year_from, 2018)
    clamp_year(year_to, 2025)
    return _rows(con.execute(
        TOP_FUNDERS_BY_OUTPUT_SQL, [f"%{clean_term(topic)}%", clamp_limit(limit)]))


CROSS_FUNDER_ORGS_SQL = """
SELECT name, country_code, ror_id, funders, funder_list, grants
FROM cross_funder_orgs
ORDER BY funders DESC, grants DESC
LIMIT ?
"""


def cross_funder_orgs(con, limit: int = 20) -> list[dict]:
    return _rows(con.execute(CROSS_FUNDER_ORGS_SQL, [clamp_limit(limit)]))


FUNDING_BY_COUNTRY_SQL = """
SELECT country_code, grants, usd_funded
FROM funding_by_country
ORDER BY usd_funded DESC NULLS LAST
LIMIT ?
"""


def funding_by_country(con, limit: int = 20) -> list[dict]:
    return _rows(con.execute(FUNDING_BY_COUNTRY_SQL, [clamp_limit(limit)]))


RISING_FIELDS_SQL = ""  # not available in the slim build (needs per-year works)


def rising_fields(con, early_from: int = 2016, early_to: int = 2019,
                  late_from: int = 2021, late_to: int = 2024,
                  min_late: int = 30, limit: int = 20) -> list[dict]:
    # The slim DB does not carry per-year work counts, so rising_fields (a
    # year-window growth metric) is unavailable here. Return empty rather than
    # error so the endpoint stays well-behaved.
    return []


RESEARCHERS_BY_SEGMENT_SQL = """
SELECT field_slug, seniority, activity_tier, researchers
FROM researcher_segment
ORDER BY researchers DESC
LIMIT ?
"""


def researchers_by_segment(con, limit: int = 30) -> list[dict]:
    return _rows(con.execute(RESEARCHERS_BY_SEGMENT_SQL, [clamp_limit(limit)]))


METASCIENCE = {
    "top_funders_by_output": top_funders_by_output,
    "cross_funder_orgs": cross_funder_orgs,
    "funding_by_country": funding_by_country,
    "rising_fields": rising_fields,
    "researchers_by_segment": researchers_by_segment,
}

# Same static-safety registry shape as queries.QUERY_SPECS, for the test-suite.
QUERY_SPECS = {
    "stats": STATS_SQL,
    "funders": FUNDERS_SQL,
    "funder_portfolio": FUNDER_PORTFOLIO_SQL,
    "field_top_funders": FIELD_TOP_FUNDERS_SQL,
    "field_top_works": FIELD_TOP_WORKS_SQL,
    "org_summary": ORG_SUMMARY_SQL,
    "search_funders": SEARCH_FUNDERS_SQL,
    "search_fields": SEARCH_FIELDS_SQL,
    "search_orgs": SEARCH_ORGS_SQL,
    "top_funders_by_output": TOP_FUNDERS_BY_OUTPUT_SQL,
    "cross_funder_orgs": CROSS_FUNDER_ORGS_SQL,
    "funding_by_country": FUNDING_BY_COUNTRY_SQL,
    "researchers_by_segment": RESEARCHERS_BY_SEGMENT_SQL,
}
