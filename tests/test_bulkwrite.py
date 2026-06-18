"""Tests for the scale path: BulkWriter shards + DuckDB consolidate dedup."""

from atlas.bulkwrite import BulkWriter, _year_of, NO_YEAR
from atlas.consolidate import consolidate_all
from atlas.schema import Row, make_id, now_iso


def _grant_row(gid, year, as_of):
    return Row("grant", {
        "atlas_id": make_id("grant", "t", gid),
        "title": f"grant {gid}",
        "amount_original": 100.0, "currency": "USD", "amount_usd": 100.0,
        "fx_rate_to_usd": 1.0, "fx_as_of": f"{year}-01-01",
        "start_date": f"{year}-01-01", "end_date": f"{year}-12-31",
        "status": "completed", "program": "p",
        "source": "t", "source_id": gid,
        "source_url": "http://x", "as_of": as_of,
    })


def test_year_of_uses_start_date():
    assert _year_of("grant", {"start_date": "2021-05-01", "as_of": "2026-01-01Z"}) == "2021"


def test_year_of_falls_back_to_as_of():
    assert _year_of("grant", {"as_of": "2026-06-18Z"}) == "2026"


def test_year_of_unknown():
    assert _year_of("organization", {}) == NO_YEAR


def test_shards_partitioned_by_source_and_year(tmp_path):
    bw = BulkWriter(source="t", processed_dir=tmp_path, batch_rows=1000)
    bw.add(_grant_row("a", 2020, "2026-01-01Z"))
    bw.add(_grant_row("b", 2021, "2026-01-01Z"))
    bw.flush_all()
    parts = sorted(p.relative_to(tmp_path).as_posix()
                   for p in tmp_path.rglob("*.parquet"))
    assert any("source=t/year=2020" in p for p in parts)
    assert any("source=t/year=2021" in p for p in parts)


def test_consolidate_dedups_entities_newest_wins(tmp_path):
    bw = BulkWriter(source="t", processed_dir=tmp_path, batch_rows=1000)
    # same grant id emitted twice with different as_of; newest title should win
    old = _grant_row("dup", 2020, "2026-01-01T00:00:00Z")
    old.data["title"] = "OLD"
    new = _grant_row("dup", 2020, "2026-06-01T00:00:00Z")
    new.data["title"] = "NEW"
    bw.add(old)
    bw.add(new)
    bw.flush_all()

    counts = consolidate_all(processed_dir=tmp_path, tables=["grant"])
    assert counts["grant"] == 1
    import pandas as pd
    df = pd.read_parquet(tmp_path / "grant.parquet")
    assert df.iloc[0]["title"] == "NEW"


def test_consolidate_preserves_canonical_columns(tmp_path):
    from atlas import schema
    bw = BulkWriter(source="t", processed_dir=tmp_path, batch_rows=1000)
    bw.add(_grant_row("x", 2022, now_iso()))
    bw.flush_all()
    consolidate_all(processed_dir=tmp_path, tables=["grant"])
    import pandas as pd
    df = pd.read_parquet(tmp_path / "grant.parquet")
    assert list(df.columns) == schema.GRANT_COLUMNS
