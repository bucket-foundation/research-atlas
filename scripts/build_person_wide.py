from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from atlas.connectors.base import REPO_ROOT  # noqa: E402
from atlas.schema import now_iso  # noqa: E402

PROCESSED = REPO_ROOT / "data" / "processed"
PERSON_COLS = ["atlas_id", "full_name", "first_name", "last_name", "orcid", "openalex_author_id",
               "source", "source_id", "source_url", "as_of"]
METRIC_COLS = ["works_count", "cited_by_count", "h_index", "i10_index", "last_known_ror",
               "last_known_country", "in_person"]
SOURCE = "openalex_authors"


MINT_SQL = ("'person:' || left(sha1('person|' || lower(trim(CASE WHEN a.orcid IS NOT NULL "
            "THEN 'orcid:' || a.orcid ELSE 'openalex:' || a.id END))), 16)")


def q(path: str | Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def build(con: duckdb.DuckDBPyConnection, person: str, authors: str, organization: str,
          out_person: Path, out_org: Path) -> dict:
    stamp = now_iso()
    con.execute(f"CREATE OR REPLACE TEMP VIEW p AS SELECT {', '.join(PERSON_COLS)} FROM read_parquet({q(person)})")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE a AS
        SELECT * EXCLUDE (rn) FROM (
            SELECT *, row_number() OVER (PARTITION BY id ORDER BY updated_date DESC NULLS LAST, pulled_at DESC) rn
            FROM read_parquet({q(authors)}, hive_partitioning = false)
        ) WHERE rn = 1
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE by_oa AS
        SELECT openalex_author_id AS oa, min(atlas_id) AS atlas_id FROM p
        WHERE openalex_author_id IS NOT NULL GROUP BY 1
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE by_orcid AS
        SELECT orcid, min(atlas_id) AS atlas_id FROM p WHERE orcid IS NOT NULL GROUP BY 1
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE resolved AS
        SELECT a.*,
               coalesce(o.atlas_id, r.atlas_id, {MINT_SQL}) AS atlas_id,
               CASE WHEN o.atlas_id IS NOT NULL THEN 'openalex_author_id'
                    WHEN r.atlas_id IS NOT NULL THEN 'orcid' ELSE 'minted' END AS match_rule,
               a.last_known_institutions[1].ror AS last_known_ror,
               a.last_known_institutions[1].country_code AS last_known_country
        FROM a LEFT JOIN by_oa o ON o.oa = a.id
               LEFT JOIN by_orcid r ON r.orcid = a.orcid AND o.atlas_id IS NULL
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE metrics AS
        SELECT atlas_id, arg_max(id, works_count) AS oa, max(works_count) AS works_count,
               max(cited_by_count) AS cited_by_count, max(h_index) AS h_index, max(i10_index) AS i10_index,
               arg_max(last_known_ror, works_count) AS last_known_ror,
               arg_max(last_known_country, works_count) AS last_known_country
        FROM resolved WHERE match_rule <> 'minted' GROUP BY 1
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE person_wide AS
        SELECT p.atlas_id, p.full_name, p.first_name, p.last_name,
               p.orcid,
               coalesce(p.openalex_author_id, m.oa) AS openalex_author_id,
               p.source, p.source_id, p.source_url, p.as_of,
               m.works_count, m.cited_by_count, m.h_index, m.i10_index,
               m.last_known_ror, m.last_known_country, true AS in_person
        FROM p LEFT JOIN metrics m ON m.atlas_id = p.atlas_id
        UNION ALL
        SELECT atlas_id, display_name, NULL, NULL, orcid, id, '{SOURCE}', id,
               'https://openalex.org/' || id, pulled_at,
               works_count, cited_by_count, h_index, i10_index, last_known_ror, last_known_country, false
        FROM (SELECT *, row_number() OVER (PARTITION BY atlas_id ORDER BY works_count DESC, id) k
              FROM resolved WHERE match_rule = 'minted') WHERE k = 1
    """)
    con.execute(f"CREATE OR REPLACE TEMP VIEW org AS SELECT atlas_id, lower(ror_id) AS ror "
                f"FROM read_parquet({q(organization)}) WHERE ror_id IS NOT NULL")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE person_org_wide AS
        WITH edges AS (
            SELECT r.atlas_id AS src_id, lower(i.ror) AS ror, 'last_known' AS role, r.id, NULL::INTEGER[] AS years
            FROM resolved r, UNNEST(r.last_known_institutions) t(i) WHERE i.ror IS NOT NULL
            UNION ALL
            SELECT r.atlas_id, lower(f.ror), 'affiliation', r.id, f.years
            FROM resolved r, UNNEST(r.affiliations) t(f) WHERE f.ror IS NOT NULL
        )
        SELECT DISTINCT ON (e.src_id, o.atlas_id, e.role)
               e.src_id, o.atlas_id AS dst_id, e.role, e.ror AS ror_id,
               list_min(e.years) AS first_year, list_max(e.years) AS last_year,
               '{SOURCE}' AS source, e.id AS source_id, 'https://openalex.org/' || e.id AS source_url,
               '{stamp}' AS as_of
        FROM edges e JOIN (SELECT ror, min(atlas_id) AS atlas_id FROM org GROUP BY 1) o ON o.ror = e.ror
        ORDER BY e.src_id, o.atlas_id, e.role, e.id
    """)
    out_person.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY person_wide TO {q(out_person)} (FORMAT parquet, COMPRESSION zstd)")
    con.execute(f"COPY person_org_wide TO {q(out_org)} (FORMAT parquet, COMPRESSION zstd)")
    rules = dict(con.execute("SELECT match_rule, count(*) FROM resolved GROUP BY 1").fetchall())
    return {
        "authors": con.execute("SELECT count(*) FROM a").fetchone()[0],
        "match_rule": rules,
        "person_rows": con.execute("SELECT count(*) FROM p").fetchone()[0],
        "person_wide_rows": con.execute("SELECT count(*) FROM person_wide").fetchone()[0],
        "person_wide_new": con.execute("SELECT count(*) FROM person_wide WHERE NOT in_person").fetchone()[0],
        "person_org_wide_rows": con.execute("SELECT count(*) FROM person_org_wide").fetchone()[0],
        "as_of": stamp,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Merge OpenAlex wide authors into the atlas people table.")
    ap.add_argument("--processed", type=Path, default=PROCESSED)
    ap.add_argument("--authors", default=None)
    ap.add_argument("--out-dir", type=Path)
    args = ap.parse_args(argv)
    out = args.out_dir or args.processed
    authors = args.authors or str(args.processed / "openalex_authors_wide" / "country=*" / "*.parquet")
    report = build(duckdb.connect(), str(args.processed / "person.parquet"), authors,
                   str(args.processed / "organization.parquet"),
                   out / "person_wide.parquet", out / "person_org_wide.parquet")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
