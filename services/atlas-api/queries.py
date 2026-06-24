"""Vetted, parameterized query surface for the research-atlas API.

This module is the *safety boundary* of the public atlas API. The design rule is
absolute and is the whole point of the file:

    There is NO arbitrary-SQL path. Every query is a fixed, hand-written SQL
    string that lives in this file. The only thing a caller controls is a small
    set of *bound parameters* — never SQL text, never table/column names, never
    identifiers spliced into a string.

Concretely, every public function here:

  * holds its SQL as a constant with ``?`` placeholders only;
  * passes user-supplied values exclusively through DuckDB's parameter binding
    (``con.execute(SQL, [params...])``) — so injection is impossible by
    construction (a value can never become SQL);
  * clamps every ``limit`` to ``[1, MAX_LIMIT]`` via :func:`clamp_limit`;
  * validates year ranges via :func:`clamp_year`;
  * returns funder / org / field / aggregate-level data only. **No PII ever.**
    The ``person``-shaped table (``researchers``) is never read here; the only
    person-adjacent query returns aggregate *counts* over the PII-free
    ``researchers_public`` view, never a name, ORCID, email, or contact field.

The functions take a DuckDB connection (opened read-only by the server) and
return ``list[dict]`` rows, so they compose into the FastAPI handlers and the
tests alike. The query builders are split from execution (:data:`QUERY_SPECS`
exposes each ``(sql, param_builder)``) so the test-suite can assert the bound
parameters are safe and bounded *without* a database.
"""

from __future__ import annotations

from typing import Any, Callable

# --------------------------------------------------------------------------- #
#  Bounds + validators (the only place user input is shaped)                   #
# --------------------------------------------------------------------------- #

MAX_LIMIT = 200        # hard cap on any result set returned to the public
DEFAULT_LIMIT = 25
MIN_YEAR = 1900
MAX_YEAR = 2100
MAX_SEARCH_LEN = 120   # search terms longer than this are nonsense / abuse


def clamp_limit(limit: Any, default: int = DEFAULT_LIMIT) -> int:
    """Coerce ``limit`` to an int in ``[1, MAX_LIMIT]``. Never raises."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return 1
    if n > MAX_LIMIT:
        return MAX_LIMIT
    return n


def clamp_year(year: Any, fallback: int) -> int:
    """Coerce ``year`` to an int in ``[MIN_YEAR, MAX_YEAR]``. Never raises."""
    try:
        y = int(year)
    except (TypeError, ValueError):
        return fallback
    if y < MIN_YEAR:
        return MIN_YEAR
    if y > MAX_YEAR:
        return MAX_YEAR
    return y


def clean_term(term: Any) -> str:
    """Normalize a free-text search/identifier param.

    Returns a stripped string truncated to :data:`MAX_SEARCH_LEN`. The value is
    only ever used as a *bound parameter* (e.g. in a ``LIKE ?`` or ``= ?``), so
    no character is dangerous; truncation is purely an abuse/cost guard.
    """
    if term is None:
        return ""
    return str(term).strip()[:MAX_SEARCH_LEN]


def _rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
#  Graph totals                                                                #
# --------------------------------------------------------------------------- #

# A single round-trip of count(*)s over the public-safe tables. No params.
STATS_SQL = """
SELECT
  (SELECT count(*) FROM funder)        AS funders,
  (SELECT count(*) FROM grant)         AS grants,
  (SELECT count(*) FROM organization)  AS organizations,
  (SELECT count(*) FROM person)        AS persons,
  (SELECT count(*) FROM work)          AS works,
  (SELECT count(*) FROM field)         AS fields,
  (SELECT count(*) FROM funder_grant)  AS funder_grant_edges,
  (SELECT count(*) FROM grant_org)     AS grant_org_edges,
  (SELECT count(*) FROM grant_person)  AS grant_person_edges,
  (SELECT count(*) FROM grant_work)    AS grant_work_edges,
  (SELECT count(*) FROM person_org)    AS person_org_edges,
  (SELECT count(*) FROM work_field)    AS work_field_edges,
  (SELECT round(sum(amount_usd)) FROM grant WHERE amount_usd IS NOT NULL) AS usd_funded
"""


def stats(con) -> dict:
    """Graph totals — entity + edge counts and total USD funded. No PII."""
    return _rows(con.execute(STATS_SQL))[0]


# --------------------------------------------------------------------------- #
#  Funders                                                                     #
# --------------------------------------------------------------------------- #

FUNDERS_SQL = """
SELECT f.atlas_id, f.name, f.short_name, f.country_code, f.funder_type,
       f.ror_id, f.homepage,
       count(DISTINCT fg.dst_id) AS grants
FROM funder f
LEFT JOIN funder_grant fg ON fg.src_id = f.atlas_id
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY grants DESC
LIMIT ?
"""


def funders(con, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """All funders with their grant counts. Funder-level only — no PII."""
    return _rows(con.execute(FUNDERS_SQL, [clamp_limit(limit)]))


# funder -> grant -> work -> field, broken down by field. ``atlas_id`` is a
# bound parameter (never interpolated).
FUNDER_PORTFOLIO_SQL = """
SELECT fld.atlas_id AS field_id, fld.name AS field, fld.level,
       count(DISTINCT w.atlas_id) AS works,
       count(DISTINCT g.atlas_id) AS grants
FROM funder f
JOIN funder_grant fg ON fg.src_id = f.atlas_id
JOIN grant g         ON g.atlas_id = fg.dst_id
JOIN grant_work gw   ON gw.src_id = g.atlas_id
JOIN work w          ON w.atlas_id = gw.dst_id
JOIN work_field wf   ON wf.src_id = w.atlas_id
JOIN field fld       ON fld.atlas_id = wf.dst_id
WHERE f.atlas_id = ?
  AND fld.level = ?
GROUP BY 1, 2, 3
ORDER BY works DESC
LIMIT ?
"""

_FIELD_LEVELS = {"topic", "subfield", "field", "domain"}


def funder_portfolio(con, funder_id: str, level: str = "topic",
                     limit: int = DEFAULT_LIMIT) -> list[dict]:
    """A funder's research-output breakdown by field/topic.

    ``level`` is validated against the closed OpenAlex taxonomy set and bound as
    a parameter; ``funder_id`` is bound, never interpolated. Defaults to
    ``topic`` because that is the level the OpenAlex work_field edges resolve to
    in this graph build.
    """
    lvl = level if level in _FIELD_LEVELS else "topic"
    return _rows(con.execute(
        FUNDER_PORTFOLIO_SQL, [clean_term(funder_id), lvl, clamp_limit(limit)]))


# --------------------------------------------------------------------------- #
#  Fields                                                                      #
# --------------------------------------------------------------------------- #

FIELD_TOP_FUNDERS_SQL = """
SELECT f.atlas_id, f.name AS funder, f.short_name, f.country_code,
       count(DISTINCT w.atlas_id) AS works,
       count(DISTINCT g.atlas_id) AS grants
FROM field fld
JOIN work_field wf   ON wf.dst_id = fld.atlas_id
JOIN work w          ON w.atlas_id = wf.src_id
JOIN grant_work gw   ON gw.dst_id = w.atlas_id
JOIN grant g         ON g.atlas_id = gw.src_id
JOIN funder_grant fg ON fg.dst_id = g.atlas_id
JOIN funder f        ON f.atlas_id = fg.src_id
WHERE fld.atlas_id = ?
GROUP BY 1, 2, 3, 4
ORDER BY works DESC
LIMIT ?
"""


def field_top_funders(con, field_id: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Top funders of work in a given field, by linked-work count. No PII."""
    return _rows(con.execute(
        FIELD_TOP_FUNDERS_SQL, [clean_term(field_id), clamp_limit(limit)]))


# Top works in a field by citation count (PageRank proxy: cited_by_count). Works
# are public outputs (title/DOI/year/citations) — no person data.
FIELD_TOP_WORKS_SQL = """
SELECT w.atlas_id, w.title, w.doi, w.openalex_id, w.publication_year,
       w.type, w.cited_by_count, w.is_oa
FROM field fld
JOIN work_field wf ON wf.dst_id = fld.atlas_id
JOIN work w        ON w.atlas_id = wf.src_id
WHERE fld.atlas_id = ?
  AND w.cited_by_count IS NOT NULL
ORDER BY w.cited_by_count DESC
LIMIT ?
"""


def field_top_works(con, field_id: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Most-cited works in a field — public outputs only, no PII."""
    return _rows(con.execute(
        FIELD_TOP_WORKS_SQL, [clean_term(field_id), clamp_limit(limit)]))


# --------------------------------------------------------------------------- #
#  Organizations                                                               #
# --------------------------------------------------------------------------- #

# ROR-keyed org summary: identity + grants received + USD + works produced. All
# aggregate; no person/contact data. ``ror`` is a bound parameter.
ORG_SUMMARY_SQL = """
WITH og AS (
    SELECT go.src_id AS grant_id
    FROM grant_org go
    JOIN organization o ON o.atlas_id = go.dst_id
    WHERE o.ror_id = ? AND go.role = 'recipient'
)
SELECT o.atlas_id, o.name, o.ror_id, o.country_code, o.city, o.org_type,
       o.homepage,
       count(DISTINCT og.grant_id) AS grants,
       round(sum(g.amount_usd))    AS usd_funded,
       count(DISTINCT gw.dst_id)   AS works
FROM organization o
LEFT JOIN og          ON TRUE
LEFT JOIN grant g     ON g.atlas_id = og.grant_id
LEFT JOIN grant_work gw ON gw.src_id = og.grant_id
WHERE o.ror_id = ?
GROUP BY 1, 2, 3, 4, 5, 6, 7
LIMIT 1
"""


def org_summary(con, ror: str) -> dict | None:
    """One ROR-resolved org: grants received, USD funded, works produced.

    Aggregate, org-level only — no investigators, no contacts. ``ror`` is bound
    twice as a parameter (CTE filter + outer filter); never interpolated.
    """
    r = clean_term(ror)
    rows = _rows(con.execute(ORG_SUMMARY_SQL, [r, r]))
    return rows[0] if rows else None


# --------------------------------------------------------------------------- #
#  Search (funders / orgs / fields by name)                                    #
# --------------------------------------------------------------------------- #

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
FROM organization
WHERE ror_id IS NOT NULL AND lower(name) LIKE lower(?)
ORDER BY length(name)
LIMIT ?
"""

_SEARCH_KINDS = {"funder", "field", "org", "all"}


def search(con, term: str, kind: str = "all", limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Name search over funders / fields / orgs.

    ``term`` is wrapped into a ``%term%`` LIKE pattern and passed as a *bound
    parameter* — the ``%`` are added in Python, the value never touches SQL text.
    ``kind`` is validated against a closed set. Result size is clamped.
    """
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


# --------------------------------------------------------------------------- #
#  Metascience queries (curated from atlas/analysis.py)                        #
# --------------------------------------------------------------------------- #

TOP_FUNDERS_BY_OUTPUT_SQL = """
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
"""


def top_funders_by_output(con, topic: str, year_from: int = 2018,
                          year_to: int = 2025, limit: int = 15) -> list[dict]:
    """Top funders of works in a topic, by linked-work count (bound LIKE)."""
    yf = clamp_year(year_from, 2018)
    yt = clamp_year(year_to, 2025)
    if yf > yt:
        yf, yt = yt, yf
    return _rows(con.execute(TOP_FUNDERS_BY_OUTPUT_SQL,
                             [f"%{clean_term(topic)}%", yf, yt, clamp_limit(limit)]))


CROSS_FUNDER_ORGS_SQL = """
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
"""


def cross_funder_orgs(con, limit: int = 20) -> list[dict]:
    """Orgs pulling money from the most distinct funders. Org-level, no PII."""
    return _rows(con.execute(CROSS_FUNDER_ORGS_SQL, [clamp_limit(limit)]))


FUNDING_BY_COUNTRY_SQL = """
SELECT o.country_code,
       count(DISTINCT g.atlas_id) AS grants,
       round(sum(g.amount_usd))   AS usd_funded
FROM grant_org go
JOIN organization o ON o.atlas_id = go.dst_id
JOIN grant g        ON g.atlas_id = go.src_id
WHERE go.role = 'recipient' AND o.country_code IS NOT NULL
GROUP BY 1
ORDER BY usd_funded DESC NULLS LAST
LIMIT ?
"""


def funding_by_country(con, limit: int = 20) -> list[dict]:
    """Total recipient funding $ by org country — the geography of money."""
    return _rows(con.execute(FUNDING_BY_COUNTRY_SQL, [clamp_limit(limit)]))


RISING_FIELDS_SQL = """
WITH counts AS (
    SELECT fld.atlas_id, fld.name, fld.level,
           count(DISTINCT w.atlas_id) FILTER (
               WHERE w.publication_year BETWEEN ? AND ?) AS early,
           count(DISTINCT w.atlas_id) FILTER (
               WHERE w.publication_year BETWEEN ? AND ?) AS late
    FROM field fld
    JOIN work_field wf ON wf.dst_id = fld.atlas_id
    JOIN work w        ON w.atlas_id = wf.src_id
    JOIN grant_work gw ON gw.dst_id = w.atlas_id
    WHERE fld.level = 'topic'
    GROUP BY 1, 2, 3
)
SELECT name, early, late,
       round(late / nullif(early, 0)::DOUBLE, 2) AS growth_ratio
FROM counts
WHERE late >= ?
ORDER BY growth_ratio DESC NULLS LAST, late DESC
LIMIT ?
"""


def rising_fields(con, early_from: int = 2016, early_to: int = 2019,
                  late_from: int = 2021, late_to: int = 2024,
                  min_late: int = 30, limit: int = 20) -> list[dict]:
    """Topics whose funded-work volume grew most early->late window. No PII."""
    ef = clamp_year(early_from, 2016)
    et = clamp_year(early_to, 2019)
    lf = clamp_year(late_from, 2021)
    lt = clamp_year(late_to, 2024)
    ml = clamp_limit(min_late, default=30) if min_late else 30
    return _rows(con.execute(
        RISING_FIELDS_SQL, [ef, et, lf, lt, ml, clamp_limit(limit)]))


# PII-FREE: aggregate researcher counts only (segment buckets), read from the
# researchers_public view (contacts excluded by construction). Returns counts,
# never a name / ORCID / email.
RESEARCHERS_BY_SEGMENT_SQL = """
SELECT field_slug, seniority, activity_tier,
       count(*) AS researchers
FROM researchers_public
GROUP BY 1, 2, 3
ORDER BY researchers DESC
LIMIT ?
"""


def researchers_by_segment(con, limit: int = 30) -> list[dict]:
    """Aggregate researcher *counts* by field x seniority x activity tier.

    Reads the PII-free ``researchers_public`` view and returns only counts.
    No name, ORCID, email, or any contact field is selected or returned.
    """
    return _rows(con.execute(RESEARCHERS_BY_SEGMENT_SQL, [clamp_limit(limit)]))


# Named metascience queries the API exposes under /metascience/<name>. Each value
# is (callable, default_kwargs) — all inputs flow through the clamps above.
METASCIENCE: dict[str, Callable] = {
    "top_funders_by_output": top_funders_by_output,
    "cross_funder_orgs": cross_funder_orgs,
    "funding_by_country": funding_by_country,
    "rising_fields": rising_fields,
    "researchers_by_segment": researchers_by_segment,
}

# Registry the tests introspect: name -> SQL string. Used to assert every query
# uses placeholders and never string-formats a user value into SQL.
QUERY_SPECS: dict[str, str] = {
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
    "rising_fields": RISING_FIELDS_SQL,
    "researchers_by_segment": RESEARCHERS_BY_SEGMENT_SQL,
}

# Columns that must NEVER appear in any public query (defense-in-depth: the
# test-suite asserts no QUERY_SPECS SQL references these). The researchers
# table's contact columns + raw person identifiers in a contact context.
FORBIDDEN_COLUMNS = (
    "email", "email_source", "email_method", "contactable", "opt_out",
)
# Tables that must never be read by a public query (the PII-bearing CRM table).
FORBIDDEN_TABLES = ("researchers ", "researchers\n", "FROM researchers")
