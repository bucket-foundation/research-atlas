"""Unit tests for the canonical schema."""

import pytest

from atlas import schema
from atlas.schema import coerce, make_id


def test_make_id_deterministic():
    a = make_id("organization", "https://ror.org/021nxhr62")
    b = make_id("organization", "https://ror.org/021nxhr62")
    assert a == b
    assert a.startswith("org:")


def test_make_id_distinct_per_entity():
    assert make_id("organization", "x") != make_id("person", "x")


def test_make_id_requires_a_part():
    with pytest.raises(ValueError):
        make_id("organization")
    with pytest.raises(ValueError):
        make_id("organization", "", None)


def test_coerce_fills_missing_columns_with_none():
    row = coerce("organization", {"atlas_id": "org:1", "name": "Test U"})
    assert set(row) == set(schema.ORGANIZATION_COLUMNS)
    assert row["ror_id"] is None
    assert row["atlas_id"] == "org:1"


def test_coerce_rejects_unknown_columns():
    with pytest.raises(ValueError):
        coerce("organization", {"atlas_id": "org:1", "bogus": 1})


def test_coerce_rejects_unknown_table():
    with pytest.raises(KeyError):
        coerce("not_a_table", {})


def test_money_invariant_zero_usd_forbidden():
    # unknown money must be None, never silent 0
    with pytest.raises(ValueError):
        coerce("grant", {"atlas_id": "grant:1", "amount_usd": 0})
    # None is fine
    row = coerce("grant", {"atlas_id": "grant:1", "amount_usd": None})
    assert row["amount_usd"] is None


def test_all_tables_covers_entities_and_edges():
    tables = set(schema.all_tables())
    assert tables == set(schema.ENTITY_COLUMNS) | set(schema.EDGE_COLUMNS)


def test_every_entity_carries_provenance():
    for cols in schema.ENTITY_COLUMNS.values():
        for p in schema.PROVENANCE_COLUMNS:
            assert p in cols


def test_every_edge_carries_provenance():
    for cols in schema.EDGE_COLUMNS.values():
        for p in schema.PROVENANCE_COLUMNS:
            assert p in cols
