#!/usr/bin/env python3
"""Load the processed parquet into a DuckDB graph database.

Builds ``research_atlas.duckdb`` with one table per entity/edge, primary keys on
entity ``atlas_id``, and indexes on the edge endpoints so graph traversals
(funder -> grant -> org -> person -> work -> field) are fast.

Usage:
    python scripts/build_db.py
    python scripts/build_db.py --db /tmp/atlas.duckdb
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas import schema  # noqa: E402
from atlas.connectors.base import DATA_PROCESSED, REPO_ROOT  # noqa: E402

DEFAULT_DB = REPO_ROOT / "research_atlas.duckdb"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the DuckDB graph DB.")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--processed", default=str(DATA_PROCESSED))
    args = ap.parse_args()

    import duckdb

    db_path = Path(args.db)
    if db_path.exists():
        db_path.unlink()  # rebuild clean; parquet is the source of truth
    con = duckdb.connect(str(db_path))
    processed = Path(args.processed)

    loaded = []
    for table in schema.all_tables():
        pq = processed / f"{table}.parquet"
        if not pq.exists():
            continue
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{pq}')")
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        loaded.append((table, n))

        if table in schema.ENTITY_COLUMNS:
            # unique surrogate key as the graph node primary key
            con.execute(
                f"CREATE UNIQUE INDEX idx_{table}_pk ON {table}(atlas_id)"
            )
        else:
            con.execute(f"CREATE INDEX idx_{table}_src ON {table}(src_id)")
            con.execute(f"CREATE INDEX idx_{table}_dst ON {table}(dst_id)")

    # ---- researcher/users (CRM) layer ------------------------------------- #
    # The enriched, segmented, contactable researcher profiles built on top of
    # the Person nodes (scripts/build_users.py). The full table carries public
    # contacts and is local/gitignored; a PII-stripped view is exposed for any
    # query that should never touch contact data.
    from atlas.users.schema import PII_COLUMNS, USER_COLUMNS  # noqa: E402

    researchers_pq = processed / "researchers.parquet"
    if researchers_pq.exists():
        con.execute(
            "CREATE TABLE researchers AS "
            f"SELECT * FROM read_parquet('{researchers_pq}')")
        con.execute("CREATE UNIQUE INDEX idx_researchers_pk ON researchers(atlas_id)")
        con.execute("CREATE INDEX idx_researchers_field ON researchers(field_slug)")
        con.execute("CREATE INDEX idx_researchers_seg ON researchers(segment)")
        n = con.execute("SELECT count(*) FROM researchers").fetchone()[0]
        loaded.append(("researchers", n))
        # PII-free view: every non-PII column, contacts excluded by construction.
        safe_cols = ", ".join(c for c in USER_COLUMNS if c not in PII_COLUMNS)
        con.execute(
            f"CREATE VIEW researchers_public AS SELECT {safe_cols} FROM researchers")
        # convenience view: the contactable, not-opted-out outreach segment
        con.execute(
            "CREATE VIEW researchers_contactable AS "
            "SELECT * FROM researchers "
            "WHERE contactable = TRUE AND opt_out = FALSE")
    else:
        # Build from the committed email-free sample if the full table is absent,
        # so a fresh clone still has a researchers_public view to query.
        sample_pq = processed / "sample" / "researchers_sample.parquet"
        if sample_pq.exists():
            con.execute(
                "CREATE VIEW researchers_public AS "
                f"SELECT * FROM read_parquet('{sample_pq}')")
            n = con.execute("SELECT count(*) FROM researchers_public").fetchone()[0]
            loaded.append(("researchers_public (sample)", n))

    con.close()
    print(f"Built {db_path}")
    for table, n in loaded:
        print(f"  {table:16s} {n:>7,}")
    if not loaded:
        print("  (no parquet found -- run an ingest first)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
