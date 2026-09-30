from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_person_wide as bpw  # noqa: E402
import pull_openalex_authors as poa  # noqa: E402
from atlas.schema import make_id  # noqa: E402


def author(n: int, cc: str = "US", orcid: str | None = None, works: int = 5) -> dict:
    return {
        "id": f"https://openalex.org/A{n}",
        "display_name": f"Author {n}",
        "display_name_alternatives": [f"A. {n}"],
        "orcid": f"https://orcid.org/{orcid}" if orcid else None,
        "works_count": works,
        "cited_by_count": 10 * n,
        "summary_stats": {"2yr_mean_citedness": 1.0, "h_index": 3, "i10_index": 1},
        "last_known_institutions": [{"id": "https://openalex.org/I1", "ror": "https://ror.org/0abcdefg1",
                                     "display_name": "Inst One", "country_code": cc, "type": "education",
                                     "lineage": ["https://openalex.org/I1"]}],
        "affiliations": [{"institution": {"id": "https://openalex.org/I2", "ror": "https://ror.org/0zzzzzzz9",
                                          "display_name": "Inst Two", "country_code": cc, "type": "facility",
                                          "lineage": []}, "years": [2021, 2019]}],
        "topics": [{"id": f"https://openalex.org/T{k}", "display_name": f"Topic {k}", "count": k,
                    "subfield": {"id": "s", "display_name": "S"},
                    "field": {"id": "f", "display_name": "Field"},
                    "domain": {"id": "d", "display_name": "D"}} for k in range(1, 8)],
        "updated_date": "2026-09-22",
    }


class FakeApi:
    def __init__(self, pages: dict[str, tuple[list[dict], str | None]], fail_on: str | None = None):
        self.pages = pages
        self.fail_on = fail_on
        self.cursors: list[str] = []

    def __call__(self, url: str) -> dict:
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(url).query)
        cursor = q["cursor"][0]
        self.cursors.append(cursor)
        if cursor == self.fail_on:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        results, nxt = self.pages[cursor]
        return {"meta": {"next_cursor": nxt}, "results": results}


def rows(out: Path, cc: str) -> list[dict]:
    return pq.read_table(out / f"country={cc}").to_pylist() if (out / f"country={cc}").exists() else []


def test_api_resumes_from_checkpoint(tmp_path: Path):
    out = tmp_path / "wide"
    state = poa.State(out / "_state.json")
    state.part("US").update({"status": "running", "cursor": "c2", "next_part": 1, "rows": 2})
    state.save()
    api = FakeApi({"*": ([author(1)], "c2"), "c2": ([author(3), author(4)], "c3"), "c3": ([author(5)], None)})
    result = poa.pull_api(poa.State(out / "_state.json"), out, "US", poa.Http(1000, opener=api),
                          budget=10 ** 9, flush_pages=1)
    assert result == "done"
    assert api.cursors == ["c2", "c3"]
    saved = json.loads((out / "_state.json").read_text())["partitions"]["US"]
    assert saved["status"] == "done" and saved["rows"] == 5 and saved["next_part"] == 3
    assert sorted(p.name for p in (out / "country=US").iterdir()) == ["part-1.parquet", "part-2.parquet"]
    assert {r["id"] for r in rows(out, "US")} == {"A3", "A4", "A5"}


def test_api_rate_limit_keeps_cursor(tmp_path: Path):
    out = tmp_path / "wide"
    api = FakeApi({"*": ([author(1)], "c2")}, fail_on="c2")
    state = poa.State(out / "_state.json")
    assert poa.pull_api(state, out, "US", poa.Http(1000, opener=api), budget=10 ** 9, flush_pages=1) == "rate_limited"
    saved = json.loads((out / "_state.json").read_text())
    assert saved["status"] == "rate_limited" and saved["partitions"]["US"]["cursor"] == "c2"


def test_api_stops_at_disk_budget(tmp_path: Path):
    out = tmp_path / "wide"
    (out / "country=US").mkdir(parents=True)
    (out / "country=US" / "part-0.parquet").write_bytes(b"x" * 2048)
    api = FakeApi({"*": ([author(1)], None)})
    state = poa.State(out / "_state.json")
    assert poa.pull_api(state, out, "US", poa.Http(1000, opener=api), budget=1024) == "budget_exhausted"
    assert api.cursors == []
    assert json.loads((out / "_state.json").read_text())["status"] == "budget_exhausted"


def snapshot_file(path: Path, recs: list[dict]) -> str:
    pq.write_table(pa.Table.from_pylist(recs), path)
    return str(path)


def test_snapshot_normalize_matches_api(tmp_path: Path):
    recs = [author(1, orcid="0000-0001-0000-0001"), author(2, works=2), author(3, cc="GB")]
    tbl = poa.read_snapshot_file(snapshot_file(tmp_path / "a.parquet", recs), ["US"], "T")
    assert tbl.to_pylist() == [poa.normalize(recs[0], "T")]
    got = tbl.to_pylist()[0]
    assert got["orcid"] == "0000-0001-0000-0001"
    assert [t["id"] for t in got["topics"]] == ["T7", "T6", "T5", "T4", "T3"]
    assert got["affiliations"][0]["years"] == [2019, 2021]
    assert (got["source"], got["source_id"], got["as_of"], got["match_tier"], got["licence"]) == (
        "openalex", "A1", "T", "T0", "CC0-1.0")
    assert got["source_url"] == "https://api.openalex.org/authors/A1"


def test_backfill_adds_provenance(tmp_path: Path):
    old = [{k: v for k, v in poa.normalize(author(1), "T").items()
            if k not in ("source", "source_id", "source_url", "as_of", "match_tier", "licence")}]
    (tmp_path / "country=US").mkdir()
    pq.write_table(pa.Table.from_pylist(old), tmp_path / "country=US" / "part-0.parquet")
    assert poa.backfill_provenance(tmp_path) == 1
    assert poa.backfill_provenance(tmp_path) == 0
    row = pq.ParquetFile(tmp_path / "country=US" / "part-0.parquet").read().to_pylist()[0]
    assert row == poa.normalize(author(1), "T")


def test_snapshot_resume_and_budget(tmp_path: Path):
    files = [snapshot_file(tmp_path / f"f{i}.parquet", [author(10 + i), author(20 + i, cc="GB")]) for i in range(3)]
    out = tmp_path / "wide"
    state = poa.State(out / "_state.json")
    state.part("US")["files_done"] = [0]
    seen: list[str] = []

    def reader(url, ccs, stamp):
        seen.append(url)
        return poa.read_snapshot_file(url, ccs, stamp)

    assert poa.pull_snapshot(state, out, ["US", "GB"], files, 10 ** 9, reader=reader, workers=1) == "done"
    assert seen == files
    assert {r["id"] for r in rows(out, "US")} == {"A11", "A12"}
    assert {r["id"] for r in rows(out, "GB")} == {"A20", "A21", "A22"}
    assert state.part("US")["status"] == "done" and state.part("GB")["status"] == "done"

    out2 = tmp_path / "wide2"
    (out2 / "country=US").mkdir(parents=True)
    (out2 / "country=US" / "junk.parquet").write_bytes(b"x" * 4096)
    s2 = poa.State(out2 / "_state.json")
    assert poa.pull_snapshot(s2, out2, ["US"], files, 1024, workers=1) == "budget_exhausted"
    assert json.loads((out2 / "_state.json").read_text())["status"] == "budget_exhausted"
    assert s2.part("US")["status"] == "running"


@pytest.fixture()
def atlas(tmp_path: Path):
    person = [
        {"atlas_id": "person:oa", "full_name": "Known Oa", "first_name": None, "last_name": None, "orcid": None,
         "openalex_author_id": "A1", "source": "openalex", "source_id": "x", "source_url": "u", "as_of": "t"},
        {"atlas_id": "person:orcid", "full_name": "Known Orcid", "first_name": None, "last_name": None,
         "orcid": "0000-0002-0000-0002", "openalex_author_id": None, "source": "openalex", "source_id": "y",
         "source_url": "u", "as_of": "t"},
        {"atlas_id": "person:nih", "full_name": "SMITH, JOHN", "first_name": "JOHN", "last_name": "SMITH",
         "orcid": None, "openalex_author_id": None, "source": "nih", "source_id": "z", "source_url": "u", "as_of": "t"},
    ]
    pq.write_table(pa.Table.from_pylist(person), tmp_path / "person.parquet")
    pq.write_table(pa.Table.from_pylist([
        {"atlas_id": "org:one", "ror_id": "https://ror.org/0abcdefg1"},
        {"atlas_id": "org:none", "ror_id": None}]), tmp_path / "organization.parquet")
    wide = tmp_path / "wide"
    recs = [author(1), author(2, orcid="0000-0002-0000-0002"), author(3, orcid="0000-0003-0000-0003"), author(4)]
    poa.write_part(wide, "US", 0, [poa.normalize(r, "T") for r in recs])
    poa.write_part(wide, "GB", 0, [poa.normalize(author(4, cc="GB"), "T")])
    report = bpw.build(duckdb.connect(), str(tmp_path / "person.parquet"), str(wide / "country=*" / "*.parquet"),
                       str(tmp_path / "organization.parquet"), tmp_path / "pw.parquet", tmp_path / "pow.parquet")
    return report, pq.read_table(tmp_path / "pw.parquet"), pq.read_table(tmp_path / "pow.parquet")


def test_dedupe_rules(atlas):
    report, pw, _ = atlas
    by_id = {r["atlas_id"]: r for r in pw.to_pylist()}
    assert report["match_rule"] == {"openalex_author_id": 1, "orcid": 1, "minted": 2}
    assert report["person_wide_rows"] == 5 and report["person_wide_new"] == 2
    assert by_id["person:oa"]["works_count"] == 5 and by_id["person:oa"]["in_person"]
    assert by_id["person:orcid"]["openalex_author_id"] == "A2"
    assert by_id["person:nih"]["works_count"] is None
    assert by_id[make_id("person", "orcid:0000-0003-0000-0003")]["openalex_author_id"] == "A3"
    minted = by_id[make_id("person", "openalex:A4")]
    assert (minted["source"], minted["match_tier"], minted["licence"]) == ("openalex", "T1", "CC0-1.0")
    assert by_id[make_id("person", "orcid:0000-0003-0000-0003")]["match_tier"] == "T0"
    assert by_id["person:nih"]["licence"] is None and by_id["person:oa"]["match_tier"] == "T1"
    assert minted["last_known_ror"] == "https://ror.org/0abcdefg1"
    assert pw.column("atlas_id").to_pylist().count(make_id("person", "openalex:A4")) == 1


def test_person_org_links_through_ror_and_has_no_email(atlas):
    _, pw, pow_ = atlas
    edges = pow_.to_pylist()
    assert {e["dst_id"] for e in edges} == {"org:one"}
    assert {e["role"] for e in edges} == {"last_known"}
    assert len(edges) == 4
    for t in (pw, pow_):
        assert not [c for c in t.column_names if "email" in c.lower()]
