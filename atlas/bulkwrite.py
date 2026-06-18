"""Streaming, memory-bounded bulk writer for full-scale ingestion.

The base :meth:`Connector.emit` reads an entire parquet into memory, concats the
new rows, and de-duplicates -- correct and convenient for thousands of rows, but
it does not scale to the millions of rows a full funder ingest produces (it
would hold every previously-emitted row in RAM on every flush).

:class:`BulkWriter` is the scale path. It:

- buffers canonical rows per table and flushes to **partitioned parquet shards**
  under ``data/processed/<table>/source=<src>/year=<yyyy>/part-NNNN.parquet``
  whenever a per-table buffer crosses ``batch_rows`` -- so peak memory is bounded
  by the batch size, not the dataset size;
- partitions by ``source`` and ``year`` (derived from a row's ``start_date`` /
  ``publication_year`` / ``as_of``), which makes a per-source / per-year ingest
  **idempotent + resumable**: a ``(source, year)`` partition is rewritten wholesale
  on re-ingest of that slice, never duplicated across slices;
- keeps the same canonical column set + ``coerce()`` validation as the base
  emitter, so the money invariant and schema-true guarantees still hold.

A separate :func:`consolidate` step (run after all shards are written) reads the
sharded dataset back with a streaming, hash-bucketed de-dup and produces the flat
``data/processed/<table>.parquet`` that ``build_db.py`` / ``manifest.py`` consume.
De-dup is done in DuckDB (out-of-core) so it never has to fit the whole table in
RAM.
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from atlas import schema
from atlas.connectors.base import DATA_PROCESSED
from atlas.schema import Row

# Partition directory name for "year unknown".
NO_YEAR = "0000"


def _year_of(table: str, data: dict) -> str:
    """Best-effort partition year for a row. Falls back to NO_YEAR."""
    cand = None
    if table == "grant":
        cand = data.get("start_date") or data.get("end_date")
    elif table == "work":
        py = data.get("publication_year")
        cand = str(py) if py else data.get("publication_date")
    if not cand:
        cand = data.get("as_of")
    if cand:
        s = str(cand)
        if len(s) >= 4 and s[:4].isdigit():
            return s[:4]
    return NO_YEAR


class BulkWriter:
    """Memory-bounded, partitioned parquet writer.

    Usage::

        bw = BulkWriter(source="cordis")
        for row in connector.normalize(pages):
            bw.add(row)
        bw.flush_all()
        counts = bw.partition_counts()
    """

    def __init__(self, source: str, processed_dir: Path | None = None,
                 batch_rows: int = 200_000):
        self.source = source
        self.processed_dir = processed_dir or DATA_PROCESSED
        self.batch_rows = batch_rows
        self._buf: dict[str, list[dict]] = defaultdict(list)
        # part index per (table, source, year) so re-flushing appends shards
        self._part_idx: dict[tuple[str, str, str], int] = {}
        self._written: dict[str, int] = defaultdict(int)

    # ----- partition pathing ---------------------------------------------- #

    def _part_dir(self, table: str, source: str, year: str) -> Path:
        return (self.processed_dir / table /
                f"source={source}" / f"year={year}")

    def reset_partition(self, table: str, source: str, year: str) -> None:
        """Delete an existing (table, source, year) partition before rewrite.

        Makes a per-slice ingest idempotent: re-ingesting a slice clears its
        shards first so a re-run converges instead of doubling.
        """
        d = self._part_dir(table, source, year)
        if d.exists():
            shutil.rmtree(d)

    # ----- buffering + flush ---------------------------------------------- #

    def add(self, row: Row) -> None:
        data = schema.coerce(row.table, row.data)
        self._buf[row.table].append(data)
        if len(self._buf[row.table]) >= self.batch_rows:
            self._flush_table(row.table)

    def add_many(self, rows) -> None:
        for r in rows:
            self.add(r)

    def _flush_table(self, table: str) -> None:
        rows = self._buf.pop(table, None)
        if not rows:
            return
        cols = (schema.ENTITY_COLUMNS.get(table)
                or schema.EDGE_COLUMNS.get(table))
        # group this buffer by (source, year) so each shard lands in one partition
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for d in rows:
            src = d.get("source") or self.source
            year = _year_of(table, d)
            groups[(src, year)].append(d)

        for (src, year), grp in groups.items():
            d = self._part_dir(table, src, year)
            d.mkdir(parents=True, exist_ok=True)
            key = (table, src, year)
            idx = self._part_idx.get(key, 0)
            self._part_idx[key] = idx + 1
            # build a column-major arrow table with the canonical column set
            arrays = {c: [r.get(c) for r in grp] for c in cols}
            tbl = pa.table({c: pa.array(arrays[c]) for c in cols})
            pq.write_table(tbl, d / f"part-{idx:05d}.parquet",
                           compression="zstd")
            self._written[table] += len(grp)

    def flush_all(self) -> None:
        for table in list(self._buf.keys()):
            self._flush_table(table)

    def partition_counts(self) -> dict[str, int]:
        """Rows written per table during this writer's lifetime (pre-dedup)."""
        return dict(self._written)
