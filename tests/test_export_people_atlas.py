import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts import export_people_atlas as ex

PROFESSORS = Path.home() / ".local/share/bucket-advisor-review/professors.jsonl"
DASHBOARD_KEYS = {"id", "openalex_id", "name", "institution", "country", "ror", "orcid", "title", "department",
                  "research_areas_official", "author_topics", "h_index", "cited_by_count", "works_count", "works",
                  "funding", "sources", "email", "email_source", "email_source_url", "email_as_of", "names_seen",
                  "works_status"}
ROR = "01abcde12"


def author(aid, name, orcid=None, field="Computer Science", topic="Human-Computer Interaction Studies", tid="T1"):
    return {"id": aid, "display_name": name, "display_name_alternatives": [name], "orcid": orcid,
            "works_count": 40, "cited_by_count": 900, "h_index": 12, "i10_index": 10,
            "last_known_institutions": [{"ror": f"https://ror.org/{ROR}", "display_name": "Test U",
                                         "country_code": "US", "type": "education"}],
            "affiliations": [], "topics": [{"id": tid, "display_name": topic, "field": field}],
            "updated_date": "2026-09-01", "pulled_at": "2026-09-30T00:00:00Z", "source": "openalex",
            "source_id": aid, "source_url": f"https://api.openalex.org/authors/{aid}",
            "as_of": "2026-09-30T00:00:00Z", "match_tier": "T0" if orcid else "T1", "licence": "CC0-1.0"}


@pytest.fixture
def world(tmp_path):
    wide = tmp_path / "wide" / "country=US"
    wide.mkdir(parents=True)
    rows = [author("A1", "Ada Lovelace", "0000-0001-0000-0001"), author("A2", "Grace Hopper"),
            author("A3", "Opted Out", "0000-0003-0000-0003"),
            author("A4", "Off Slice", field="Medicine", topic="Cardiology", tid="T9")]
    pq.write_table(pa.Table.from_pylist(rows), wide / "part-0.parquet")
    seeds = tmp_path / "institutions.csv"
    seeds.write_text("name,ror_id,carnegie_class\nTest U,01abcde12,R1\nOther,09zzzzz99,R2\n")
    off = tmp_path / "official" / ROR
    off.mkdir(parents=True)
    (off / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"name": "Ada Lovelace", "title": "Professor", "departments": ["Computing"], "research_areas": "HCI",
         "profile_url": "https://test.edu/ada", "email": "ada@test.edu", "email_source": "official_directory",
         "email_source_url": "https://test.edu/ada", "licence": "institution-copyright", "as_of": "2026-09-30"},
        {"name": "Grace Hopper", "title": "Lecturer", "departments": [], "profile_url": "http://test.edu/grace",
         "email": "grace@test.edu", "email_source": None, "source": "scrape", "as_of": "2026-09-30"},
    ]))
    proc = tmp_path / "processed"
    proc.mkdir()
    pq.write_table(pa.Table.from_pylist([{"atlas_id": "person:1", "full_name": "Ada Lovelace",
                                          "orcid": "0000-0001-0000-0001", "openalex_author_id": None}]),
                   proc / "person.parquet")
    pq.write_table(pa.Table.from_pylist([{"grant_id": "grant:1", "pi_person_id": "person:9", "role": "pi",
                                          "person_atlas_id": "person:1", "orcid": "0000-0001-0000-0001",
                                          "match_method": "name+org"}]), proc / "grant_pi_person.parquet")
    pq.write_table(pa.Table.from_pylist([{"atlas_id": "grant:1", "title": "Engines", "amount_usd": 5000.0,
                                          "start_date": "2024-01-01", "end_date": "2099-01-01", "source": "nsf",
                                          "source_url": "https://nsf.gov/1"}]), proc / "grant.parquet")
    cache = tmp_path / "works"
    for aid in ("A1", "A2"):
        p = ex.works_path(cache, aid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"as_of": "x", "works": [{"id": "W1", "title": "t", "year": 2020,
                                                          "cited_by_count": 3}]}))
    topics = tmp_path / "topics.jsonl"
    topics.write_text(json.dumps({"id": "T1", "name": "Human-Computer Interaction Studies",
                                  "subfield": "Human-Computer Interaction", "field": "Computer Science"}) + "\n")
    private = tmp_path / "private"
    private.mkdir()
    (private / "opt_out.csv").write_text("orcid,email,name,ror_id,reason,as_of\n0000-0003-0000-0003,,,,request,2026\n")
    out = tmp_path / "out" / "people-atlas.jsonl"
    argv = ["--out", str(out), "--wide", str(tmp_path / "wide"), "--official", str(tmp_path / "official"),
            "--processed", str(proc), "--works-cache", str(cache), "--topics-cache", str(topics),
            "--seeds", str(seeds), "--private", str(private), "--no-fetch"]
    return out, argv


def run(argv):
    args = ex.parser().parse_args(argv)
    return ex.run(args)


def rows(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_shape_matches_dashboard(world):
    out, argv = world
    stats = run(argv)
    got = rows(out)
    assert stats["rows"] == 2 and {r["id"] for r in got} == {"A1", "A2"}
    for r in got:
        assert DASHBOARD_KEYS <= set(r)
        assert all(set(t) == {"name", "field", "count"} for t in r["author_topics"])
        assert all(set(w) == {"id", "title", "year", "cited_by_count"} for w in r["works"])
        assert all({"source", "source_id", "source_url", "as_of", "match_tier", "licence"} <= set(s)
                   for s in r["sources"])
    ada = next(r for r in got if r["id"] == "A1")
    assert ada["funding"]["n_grants"] == 1 and ada["funding"]["active"]
    assert ada["title"] == "Professor" and ada["department"] == "Computing"
    if PROFESSORS.exists():
        ref = json.loads(PROFESSORS.read_text().splitlines()[0])
        shared = (set(ref) & set(ada)) - {"funding", "works", "sources"}
        for k in shared:
            if ref[k] is not None and ada[k] is not None:
                assert type(ref[k]) is type(ada[k]), k


def test_https_only_and_email_needs_source(world):
    out, argv = world
    run(argv)
    for r in rows(out):
        assert r["profile_url"] is None or r["profile_url"].startswith("https://")
        assert (r["email"] is None) or r["email_source"] == "official_directory"
    grace = next(r for r in rows(out) if r["id"] == "A2")
    assert grace["profile_url"] is None and grace["email"] is None


def test_suppressed_absent(world):
    out, argv = world
    stats = run(argv)
    assert "A3" not in {r["id"] for r in rows(out)} and stats["suppressed"] == 1


def test_all_r1_slice_and_limit(world):
    out, argv = world
    assert run(argv + ["--slice", "all-r1"])["rows"] == 3
    assert run(argv + ["--slice", "all-r1", "--limit", "1"])["rows"] == 1


def test_atomic_write_keeps_previous(tmp_path):
    out = tmp_path / "people-atlas.jsonl"
    ex.atomic_write(out, [{"id": "old"}])
    ex.atomic_write(out, [{"id": "new"}])
    assert rows(out) == [{"id": "new"}]
    assert rows(tmp_path / "people-atlas.prev.jsonl") == [{"id": "old"}]
    assert not list(tmp_path.glob(".*.tmp"))
