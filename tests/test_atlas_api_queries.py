"""Tests for the atlas-api vetted query surface (services/atlas-api/queries.py).

Two layers:

1. *Static safety* — assert, without any database, that every query in
   ``QUERY_SPECS`` uses bound placeholders (no Python string-formatting of user
   values into SQL) and that no public query references a PII-bearing column or
   the PII-bearing ``researchers`` table. This is the SQL-injection / no-PII
   guarantee enforced as a unit test.

2. *Behavioral* — build a tiny in-memory DuckDB graph fixture and run each query
   end-to-end, asserting result shapes, that limits are clamped, that year
   ranges are clamped, that hostile inputs (``'; DROP TABLE``, ``%``) are inert
   (treated as data, return rows or empty, never error/inject), and that the
   ``researchers_by_segment`` rows carry only aggregate counts — no name / orcid
   / email key ever appears.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb
import pytest

# services/atlas-api is not a package; import its modules directly.
SERVICES = Path(__file__).resolve().parents[1] / "services" / "atlas-api"
sys.path.insert(0, str(SERVICES))

import queries  # noqa: E402
import queries_slim  # noqa: E402


# --------------------------------------------------------------------------- #
#  1. Static safety (both the full and the slim query modules)                 #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mod", [queries, queries_slim],
                         ids=["full", "slim"])
def test_static_safety_per_module(mod):
    """Both query modules: no string-formatting, placeholders present, no PII."""
    for name, sql in mod.QUERY_SPECS.items():
        low = sql.lower()
        assert "%s" not in sql, f"{mod.__name__}.{name} uses %s formatting"
        assert "%d" not in sql, f"{mod.__name__}.{name} uses %d formatting"
        assert not re.search(r"\{[^}]*\}", sql), f"{mod.__name__}.{name} brace placeholder"
        for col in mod.FORBIDDEN_COLUMNS:
            assert col not in low, f"{mod.__name__}.{name} references {col!r}"
        assert "from researchers\n" not in low, f"{mod.__name__}.{name} reads researchers"
        assert "from researchers " not in low, f"{mod.__name__}.{name} reads researchers"
        assert "join researchers " not in low, f"{mod.__name__}.{name} joins researchers"


def test_no_python_string_formatting_in_any_query():
    """No query SQL may contain a Python format/f-string interpolation marker.

    User values must arrive via DuckDB ``?`` binding only. We scan each SQL
    constant for ``%s``/``%d``/``{`` placeholders and bare ``+`` concatenation
    artifacts that would indicate a value was spliced into SQL text.
    """
    for name, sql in queries.QUERY_SPECS.items():
        assert "%s" not in sql, f"{name} uses %s formatting"
        assert "%d" not in sql, f"{name} uses %d formatting"
        assert not re.search(r"\{[^}]*\}", sql), f"{name} has a brace placeholder"
        assert "f\"" not in sql and "f'" not in sql, f"{name} looks like an f-string"


def test_every_query_uses_parameter_placeholders_or_is_constant():
    """Queries that take input expose it as ``?`` placeholders, never literals."""
    # stats and a handful of fixed aggregates take no params -> 0 placeholders.
    no_param = {"stats"}
    for name, sql in queries.QUERY_SPECS.items():
        n = sql.count("?")
        if name in no_param:
            assert n == 0, f"{name} should be parameterless"
        else:
            assert n >= 1, f"{name} takes input but has no ? placeholder"


def test_no_public_query_touches_pii_columns_or_table():
    """Defense-in-depth: no public query references contact columns or the
    PII-bearing researchers table. Aggregate counts come from researchers_public.
    """
    for name, sql in queries.QUERY_SPECS.items():
        low = sql.lower()
        for col in queries.FORBIDDEN_COLUMNS:
            assert col not in low, f"{name} references forbidden column {col!r}"
        # the only allowed researchers reference is the _public view
        assert "from researchers\n" not in low, f"{name} reads researchers table"
        assert "from researchers " not in low, f"{name} reads researchers table"
        assert "join researchers " not in low, f"{name} joins researchers table"


def test_clamp_limit_bounds():
    assert queries.clamp_limit(5) == 5
    assert queries.clamp_limit(0) == 1
    assert queries.clamp_limit(-99) == 1
    assert queries.clamp_limit(10_000) == queries.MAX_LIMIT
    assert queries.clamp_limit("not-a-number") == queries.DEFAULT_LIMIT
    assert queries.clamp_limit(None) == queries.DEFAULT_LIMIT


def test_clamp_year_bounds():
    assert queries.clamp_year(2020, 2018) == 2020
    assert queries.clamp_year(1500, 2018) == queries.MIN_YEAR
    assert queries.clamp_year(9999, 2018) == queries.MAX_YEAR
    assert queries.clamp_year("xyz", 2018) == 2018


def test_clean_term_truncates_and_strips():
    assert queries.clean_term("  hello  ") == "hello"
    assert len(queries.clean_term("a" * 5000)) == queries.MAX_SEARCH_LEN
    assert queries.clean_term(None) == ""


# --------------------------------------------------------------------------- #
#  2. Behavioral — tiny in-memory graph fixture                                #
# --------------------------------------------------------------------------- #

@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE funder(atlas_id VARCHAR, name VARCHAR, short_name VARCHAR,
            country_code VARCHAR, funder_type VARCHAR, ror_id VARCHAR,
            crossref_funder_id VARCHAR, homepage VARCHAR);
        CREATE TABLE grant(atlas_id VARCHAR, amount_usd DOUBLE);
        CREATE TABLE organization(atlas_id VARCHAR, name VARCHAR, ror_id VARCHAR,
            country_code VARCHAR, city VARCHAR, region VARCHAR, org_type VARCHAR,
            homepage VARCHAR, lat DOUBLE, lon DOUBLE);
        CREATE TABLE person(atlas_id VARCHAR, full_name VARCHAR);
        CREATE TABLE work(atlas_id VARCHAR, title VARCHAR, doi VARCHAR,
            openalex_id VARCHAR, publication_year BIGINT, type VARCHAR,
            cited_by_count BIGINT, is_oa BOOLEAN);
        CREATE TABLE field(atlas_id VARCHAR, name VARCHAR, openalex_id VARCHAR,
            level VARCHAR, parent_atlas_id VARCHAR);
        CREATE TABLE funder_grant(src_id VARCHAR, dst_id VARCHAR, role VARCHAR);
        CREATE TABLE grant_org(src_id VARCHAR, dst_id VARCHAR, role VARCHAR);
        CREATE TABLE grant_person(src_id VARCHAR, dst_id VARCHAR, role VARCHAR);
        CREATE TABLE grant_work(src_id VARCHAR, dst_id VARCHAR, role VARCHAR);
        CREATE TABLE person_org(src_id VARCHAR, dst_id VARCHAR, role VARCHAR);
        CREATE TABLE work_field(src_id VARCHAR, dst_id VARCHAR, score DOUBLE);
        -- PII-free researcher view stand-in (counts only in our queries)
        CREATE TABLE researchers_public(atlas_id VARCHAR, field_slug VARCHAR,
            seniority VARCHAR, activity_tier VARCHAR, is_corresponding_author BOOLEAN);
    """)
    # one funder -> one grant -> one org + one work -> one field
    c.execute("INSERT INTO funder VALUES "
              "('fund:1','National Science Foundation','NSF','US','government',"
              "'https://ror.org/021nxhr62',NULL,'https://nsf.gov')")
    c.execute("INSERT INTO grant VALUES ('grant:1', 1000000.0)")
    c.execute("INSERT INTO organization VALUES "
              "('org:1','MIT','https://ror.org/042nb2s44','US','Cambridge','MA',"
              "'education','https://mit.edu',42.3,-71.1)")
    c.execute("INSERT INTO person VALUES ('person:1','Ada Lovelace')")
    c.execute("INSERT INTO work VALUES "
              "('work:1','A study of CRISPR delivery','10.1/x','W1',2022,"
              "'article',500,TRUE)")
    c.execute("INSERT INTO field VALUES "
              "('field:1','Genetics','T1','field',NULL),"
              "('field:2','crispr delivery','T2','topic',NULL)")
    c.execute("INSERT INTO funder_grant VALUES ('fund:1','grant:1','awarder')")
    c.execute("INSERT INTO grant_org VALUES ('grant:1','org:1','recipient')")
    c.execute("INSERT INTO grant_work VALUES ('grant:1','work:1','acknowledges')")
    c.execute("INSERT INTO work_field VALUES "
              "('work:1','field:1',0.9),('work:1','field:2',0.8)")
    c.execute("INSERT INTO researchers_public VALUES "
              "('person:1','genetics','senior','active-pi',TRUE),"
              "('person:2','genetics','junior','active',FALSE)")
    yield c
    c.close()


def test_stats(con):
    s = queries.stats(con)
    assert s["funders"] == 1
    assert s["grants"] == 1
    assert s["works"] == 1
    assert s["usd_funded"] == 1000000


def test_funders(con):
    rows = queries.funders(con, limit=10)
    assert rows[0]["short_name"] == "NSF"
    assert rows[0]["grants"] == 1


def test_funder_portfolio(con):
    rows = queries.funder_portfolio(con, "fund:1", level="field", limit=10)
    assert any(r["field"] == "Genetics" for r in rows)
    # topic level filters differently
    topics = queries.funder_portfolio(con, "fund:1", level="topic", limit=10)
    assert any(r["field"] == "crispr delivery" for r in topics)


def test_funder_portfolio_invalid_level_falls_back(con):
    # a hostile/unknown level must not error and must not inject
    rows = queries.funder_portfolio(con, "fund:1", level="; DROP TABLE funder", limit=10)
    assert isinstance(rows, list)
    # funder table still exists
    assert queries.stats(con)["funders"] == 1


def test_field_top_funders(con):
    rows = queries.field_top_funders(con, "field:1", limit=10)
    assert rows[0]["short_name"] == "NSF"
    assert rows[0]["works"] == 1


def test_field_top_works(con):
    rows = queries.field_top_works(con, "field:1", limit=10)
    assert rows[0]["title"] == "A study of CRISPR delivery"
    assert rows[0]["cited_by_count"] == 500
    # works are public outputs: no person column leaks in
    assert "full_name" not in rows[0]


def test_org_summary(con):
    row = queries.org_summary(con, "https://ror.org/042nb2s44")
    assert row["name"] == "MIT"
    assert row["grants"] == 1
    assert row["usd_funded"] == 1000000
    assert row["works"] == 1
    # no person/contact data
    assert all("email" not in k for k in row)


def test_org_summary_unknown_returns_none(con):
    assert queries.org_summary(con, "https://ror.org/doesnotexist") is None


def test_search_matches_by_name(con):
    rows = queries.search(con, "science", kind="funder", limit=10)
    assert any(r["name"] == "National Science Foundation" for r in rows)
    fields = queries.search(con, "genet", kind="field", limit=10)
    assert any(r["name"] == "Genetics" for r in fields)
    orgs = queries.search(con, "MIT", kind="org", limit=10)
    assert any(r["name"] == "MIT" for r in orgs)


def test_search_injection_is_inert(con):
    # classic injection payload is treated purely as a LIKE value -> no match,
    # no error, tables intact.
    rows = queries.search(con, "'; DROP TABLE funder; --", kind="all", limit=10)
    assert rows == []
    assert queries.stats(con)["funders"] == 1


def test_search_percent_is_literal_data(con):
    # a lone % must not become a wildcard that returns everything as an attack
    rows = queries.search(con, "%", kind="funder", limit=10)
    # %%% pattern still matches (it's wrapped), but result is clamped + bounded
    assert len(rows) <= 10


def test_search_empty_returns_nothing(con):
    assert queries.search(con, "   ", kind="all") == []


def test_limit_is_clamped_in_query(con):
    # ask for a million; must come back bounded by MAX_LIMIT (only 1 funder here)
    rows = queries.funders(con, limit=1_000_000)
    assert len(rows) <= queries.MAX_LIMIT


def test_researchers_by_segment_is_counts_only_no_pii(con):
    rows = queries.researchers_by_segment(con, limit=10)
    assert rows, "expected aggregate rows"
    pii_keys = {"full_name", "first_name", "last_name", "orcid", "email",
                "openalex_author_id", "atlas_id"}
    for r in rows:
        # only aggregate dimensions + a count
        assert pii_keys.isdisjoint(r.keys()), f"PII key leaked: {r.keys()}"
        assert "researchers" in r
        assert isinstance(r["researchers"], int)


def test_top_funders_by_output_year_clamp(con):
    # absurd years get clamped; query still runs and returns a list
    rows = queries.top_funders_by_output(con, "crispr", year_from=10, year_to=99999, limit=5)
    assert isinstance(rows, list)


def test_metascience_registry_callables(con):
    for name, fn in queries.METASCIENCE.items():
        assert callable(fn), name


# --------------------------------------------------------------------------- #
#  3. Slim-DB module — same shapes over pre-aggregated tables                  #
# --------------------------------------------------------------------------- #

@pytest.fixture()
def slim():
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE stats(funders BIGINT, grants BIGINT, organizations BIGINT,
            persons BIGINT, works BIGINT, fields BIGINT,
            funder_grant_edges BIGINT, grant_org_edges BIGINT,
            grant_person_edges BIGINT, grant_work_edges BIGINT,
            person_org_edges BIGINT, work_field_edges BIGINT, usd_funded DOUBLE);
        CREATE TABLE funder(atlas_id VARCHAR, name VARCHAR, short_name VARCHAR,
            country_code VARCHAR, funder_type VARCHAR, ror_id VARCHAR,
            homepage VARCHAR, grants BIGINT);
        CREATE TABLE field(atlas_id VARCHAR, name VARCHAR, openalex_id VARCHAR, level VARCHAR);
        CREATE TABLE funder_field(funder_id VARCHAR, field_id VARCHAR, field VARCHAR,
            level VARCHAR, works BIGINT, grants BIGINT);
        CREATE TABLE field_funder(field_id VARCHAR, funder_id VARCHAR, funder VARCHAR,
            short_name VARCHAR, country_code VARCHAR, works BIGINT, grants BIGINT);
        CREATE TABLE field_work(field_id VARCHAR, work_id VARCHAR, title VARCHAR,
            doi VARCHAR, openalex_id VARCHAR, publication_year BIGINT, type VARCHAR,
            cited_by_count BIGINT, is_oa BOOLEAN);
        CREATE TABLE org_summary(atlas_id VARCHAR, name VARCHAR, ror_id VARCHAR,
            country_code VARCHAR, city VARCHAR, org_type VARCHAR, homepage VARCHAR,
            grants BIGINT, usd_funded DOUBLE, works BIGINT);
        CREATE TABLE cross_funder_orgs(name VARCHAR, country_code VARCHAR,
            ror_id VARCHAR, funders BIGINT, funder_list VARCHAR, grants BIGINT);
        CREATE TABLE funding_by_country(country_code VARCHAR, grants BIGINT, usd_funded DOUBLE);
        CREATE TABLE researcher_segment(field_slug VARCHAR, seniority VARCHAR,
            activity_tier VARCHAR, researchers BIGINT);
    """)
    c.execute("INSERT INTO stats VALUES (1,1,1,1,1,1,1,1,1,1,1,1,1000000.0)")
    c.execute("INSERT INTO funder VALUES "
              "('fund:1','National Science Foundation','NSF','US','government',"
              "'https://ror.org/021nxhr62','https://nsf.gov',5)")
    c.execute("INSERT INTO field VALUES ('field:1','Genetics','T1','field')")
    c.execute("INSERT INTO funder_field VALUES ('fund:1','field:1','Genetics','field',10,3)")
    c.execute("INSERT INTO field_funder VALUES "
              "('field:1','fund:1','National Science Foundation','NSF','US',10,3)")
    c.execute("INSERT INTO field_work VALUES "
              "('field:1','work:1','A study','10.1/x','W1',2022,'article',500,TRUE)")
    c.execute("INSERT INTO org_summary VALUES "
              "('org:1','MIT','https://ror.org/042nb2s44','US','Cambridge',"
              "'education','https://mit.edu',2,2000000.0,4)")
    c.execute("INSERT INTO cross_funder_orgs VALUES ('MIT','US','https://ror.org/042nb2s44',3,'NSF, NIH',9)")
    c.execute("INSERT INTO funding_by_country VALUES ('US',100,5e9)")
    c.execute("INSERT INTO researcher_segment VALUES ('genetics','senior','active-pi',42)")
    yield c
    c.close()


def test_slim_stats(slim):
    assert queries_slim.stats(slim)["usd_funded"] == 1000000


def test_slim_funders(slim):
    rows = queries_slim.funders(slim, 10)
    assert rows[0]["short_name"] == "NSF" and rows[0]["grants"] == 5


def test_slim_portfolio(slim):
    rows = queries_slim.funder_portfolio(slim, "fund:1", level="field", limit=10)
    assert rows[0]["field"] == "Genetics" and rows[0]["works"] == 10


def test_slim_field_top_funders_and_works(slim):
    f = queries_slim.field_top_funders(slim, "field:1", 10)
    assert f[0]["short_name"] == "NSF"
    w = queries_slim.field_top_works(slim, "field:1", 10)
    assert w[0]["cited_by_count"] == 500
    assert "full_name" not in w[0]


def test_slim_org_summary(slim):
    row = queries_slim.org_summary(slim, "https://ror.org/042nb2s44")
    assert row["name"] == "MIT" and row["works"] == 4
    assert all("email" not in k for k in row)


def test_slim_search_injection_inert(slim):
    rows = queries_slim.search(slim, "'; DROP TABLE funder; --", kind="all", limit=10)
    assert rows == []
    assert queries_slim.stats(slim)["funders"] == 1


def test_slim_researchers_by_segment_counts_only(slim):
    rows = queries_slim.researchers_by_segment(slim, 10)
    pii = {"full_name", "orcid", "email", "atlas_id", "first_name", "last_name"}
    for r in rows:
        assert pii.isdisjoint(r.keys())
        assert isinstance(r["researchers"], int)


def test_slim_limit_clamped(slim):
    assert len(queries_slim.funders(slim, 10_000)) <= queries_slim.MAX_LIMIT


def test_slim_rising_fields_graceful_empty(slim):
    # slim build lacks per-year work counts -> returns [] (never errors)
    assert queries_slim.rising_fields(slim) == []
