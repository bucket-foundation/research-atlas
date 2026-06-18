"""Consolidate partitioned parquet shards into the flat published parquet.

:class:`~atlas.bulkwrite.BulkWriter` writes many small shards under
``data/processed/<table>/source=.../year=.../part-*.parquet``. Downstream
(``build_db.py``, ``manifest.py``, ``build_sample.py``) expects one flat
``data/processed/<table>.parquet`` per table, de-duplicated by the same rule the
base emitter uses:

- **entities** dedup on ``atlas_id`` (keep newest ``as_of``);
- **edges** dedup on ``(src_id, dst_id, role|score, source)`` (keep newest).

This is done in **DuckDB**, which de-duplicates out-of-core (spilling to disk),
so it never has to hold the whole table in RAM -- the point of the scale path.
"""

from __future__ import annotations

from pathlib import Path

from atlas import schema
from atlas.connectors.base import DATA_PROCESSED


def _quote_cols(cols: list[str]) -> str:
    return ", ".join(f'"{c}"' for c in cols)


def consolidate_table(con, table: str, processed_dir: Path) -> int | None:
    """Read all shards for ``table`` and write the flat deduped parquet.

    Returns the final row count, or ``None`` if there are no shards.
    """
    shard_root = processed_dir / table
    if not shard_root.exists():
        return None
    glob = str(shard_root / "**" / "*.parquet")
    # does the glob match anything?
    if not any(shard_root.rglob("*.parquet")):
        return None

    cols = schema.ENTITY_COLUMNS.get(table) or schema.EDGE_COLUMNS[table]
    collist = _quote_cols(cols)

    if table in schema.ENTITY_COLUMNS:
        partition = "atlas_id"
    else:
        key = "role" if "role" in cols else "score"
        partition = f'src_id, dst_id, "{key}", source'

    out = processed_dir / f"{table}.parquet"
    # hive_partitioning=false: source=/year= are *also* real columns in the data,
    # so we read the explicit column set and ignore the directory-encoded copies.
    con.execute(
        f"""
        COPY (
            SELECT {collist} FROM (
                SELECT {collist},
                       row_number() OVER (
                           PARTITION BY {partition}
                           ORDER BY as_of DESC
                       ) AS _rn
                FROM read_parquet('{glob}', union_by_name=true)
            )
            WHERE _rn = 1
        ) TO '{out}' (FORMAT parquet, COMPRESSION zstd)
        """
    )
    n = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
    return int(n)


def consolidate_all(processed_dir: Path | None = None,
                    tables: list[str] | None = None,
                    memory_limit: str = "4GB",
                    temp_dir: str | None = None) -> dict[str, int]:
    """Consolidate every sharded table into flat parquet. Returns row counts."""
    import duckdb

    processed_dir = processed_dir or DATA_PROCESSED
    tables = tables or schema.all_tables()
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute("SET preserve_insertion_order=false")
    if temp_dir:
        con.execute(f"SET temp_directory='{temp_dir}'")

    counts: dict[str, int] = {}
    for table in tables:
        n = consolidate_table(con, table, processed_dir)
        if n is not None:
            counts[table] = n
    con.close()
    return counts
