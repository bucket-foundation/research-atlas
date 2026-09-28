from __future__ import annotations

import json

import duckdb
import pytest

from atlas.advisor_join import Advisor, match_advisors, openalex_author_id, ror_id, summarize


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE person (atlas_id VARCHAR, full_name VARCHAR, first_name VARCHAR, last_name VARCHAR, "
              "orcid VARCHAR, openalex_author_id VARCHAR, source VARCHAR)")
    c.execute("CREATE TABLE organization (atlas_id VARCHAR, ror_id VARCHAR)")
    c.execute("CREATE TABLE person_org (src_id VARCHAR, dst_id VARCHAR)")
    c.execute("CREATE TABLE grant_person (src_id VARCHAR, dst_id VARCHAR, role VARCHAR)")
    c.executemany("INSERT INTO person VALUES (?,?,?,?,?,?,?)", [
        ("p1", "Ada Lovelace", None, None, "0000-0001", "A1", "openalex"),
        ("p2", "Grace Hopper", None, None, None, "A2", "openalex"),
        ("p3", "Grace Hopper", None, None, None, "A2", "openalex"),
        ("n1", "MÜLLER-SCHÄFER, JAN", "JAN", "MÜLLER-SCHÄFER", None, None, "nih"),
        ("n2", "SMITH, JOHN", "JOHN", "SMITH", None, None, "nih"),
        ("n3", "SMITH, JANE", "JANE", "SMITH", None, None, "nsf"),
        ("n4", "SMITH, JILL", "JILL", "SMITH", None, None, "nsf"),
        ("n5", "SMITH, JOHN", "JOHN", "SMITH", None, None, "nih"),
        ("x1", "Rosalind Franklin", "Rosalind", "Franklin", None, "A77", "openalex"),
        ("x2", "FRANKLIN, ROSA", "ROSA", "FRANKLIN", None, None, "nih"),
        ("x3", "FRANKLIN, RUTH", "RUTH", "FRANKLIN", None, None, "nsf"),
    ])
    c.executemany("INSERT INTO organization VALUES (?,?)", [
        ("o1", "https://ror.org/0abcdefg1"), ("o2", "https://ror.org/0zzzzzzz9"), ("o3", None)])
    c.executemany("INSERT INTO person_org VALUES (?,?)", [
        ("n1", "o1"), ("n2", "o1"), ("n3", "o1"), ("n4", "o1"), ("n5", "o2"),
        ("x1", "o2"), ("x2", "o2"), ("x3", "o2")])
    c.executemany("INSERT INTO grant_person VALUES (?,?,?)", [
        ("g1", "n1", "pi"), ("g2", "n2", "pi"), ("g3", "n3", "co-pi"), ("g4", "n4", "pi"),
        ("g5", "n5", "pi"), ("g6", "x1", "pi"), ("g7", "x2", "pi"), ("g8", "x3", "program-officer")])
    return c


def adv(key, name, oa=None, ror=None, tier="A"):
    return Advisor(key=key, name=name, openalex_id=oa, ror=ror, tier=tier)


def test_id_parsers():
    assert openalex_author_id("https://openalex.org/A5051156147") == "A5051156147"
    assert openalex_author_id("https://openalex.org/W123") is None
    assert openalex_author_id(None) is None
    assert ror_id("https://ror.org/0168r3w48") == "0168r3w48"
    assert ror_id("0168R3W48/") == "0168r3w48"
    assert ror_id("not-a-ror") is None


def test_t1_exact_openalex_with_orcid(con):
    [m] = match_advisors(con, [adv("A1", "Ada Lovelace", "A1")])
    assert m.tier == "t1" and m.t1 == ("p1",) and m.t1_orcid


def test_t1_duplicate_person_is_ambiguous(con):
    [m] = match_advisors(con, [adv("A2", "Grace Hopper", "A2")])
    assert m.t1 == ("p2", "p3") and m.t1_unique is None and m.tier == "ambiguous"


def test_t2_accent_and_hyphen_surname(con):
    [m] = match_advisors(con, [adv("A9", "Jan Müller-Schäfer", "A9", "0abcdefg1")])
    assert m.tier == "t2" and m.t2 == ("n1",) and m.t2_sources == ("nih",)


def test_t2_same_initial_same_org_is_ambiguous(con):
    [m] = match_advisors(con, [adv("A8", "J. Smith", "A8", "0abcdefg1")])
    assert set(m.t2) == {"n2", "n3", "n4"} and m.tier == "ambiguous"


def test_t2_requires_same_ror(con):
    [m] = match_advisors(con, [adv("A7", "Ada Lovelace", None, "0zzzzzzz9")])
    assert m.tier == "none"
    [m] = match_advisors(con, [adv("A6", "John Smith", None, "0zzzzzzz9")])
    assert m.t2 == ("n5",)


def test_t2_skipped_without_ror(con):
    [m] = match_advisors(con, [adv("A5", "John Smith")])
    assert m.t2 == () and m.tier == "none"


def test_summary_counts_and_rates(con):
    ms = match_advisors(con, [
        adv("A1", "Ada Lovelace", "A1", tier="A"),
        adv("A2", "Grace Hopper", "A2", tier="A"),
        adv("A6", "John Smith", None, "0zzzzzzz9", tier="B"),
        adv("A5", "Nobody Here", tier="B"),
    ])
    r = summarize(ms)
    assert r.total == 4
    assert r.counts["t1_any"] == 2 and r.counts["t1_unique"] == 1 and r.counts["t1_multi"] == 1
    assert r.counts["t2_unique"] == 1 and r.counts["linked"] == 2
    assert r.rates["t1_any"] == 0.5
    assert r.t1_rate_by_tier == {"A": 1.0, "B": 0.0}
    assert r.t2_by_source == {"nih": 1}


def test_report_carries_no_names_or_ids(con):
    ms = match_advisors(con, [adv("A1", "Ada Lovelace", "A1"), adv("A6", "John Smith", None, "0zzzzzzz9")])
    text = json.dumps(summarize(ms).as_dict())
    for leak in ("Lovelace", "Smith", "A1", "p1", "n5", "0000-0001", "@"):
        assert leak not in text


def test_empty_input(con):
    r = summarize(match_advisors(con, []))
    assert r.total == 0 and r.rates["t1_any"] == 0.0


def test_t2_pool_excludes_openalex_and_non_pi_rows(con):
    [m] = match_advisors(con, [adv("A4", "R. Franklin", None, "0zzzzzzz9")])
    assert m.t2 == ("x2",) and m.t2_sources == ("nih",)
