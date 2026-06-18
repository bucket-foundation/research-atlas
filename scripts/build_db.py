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

    con.close()
    print(f"Built {db_path}")
    for table, n in loaded:
        print(f"  {table:16s} {n:>7,}")
    if not loaded:
        print("  (no parquet found -- run an ingest first)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
