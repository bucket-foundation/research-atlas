from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

import pytest

from atlas.users.directories import REGISTRY
from atlas.users.directories.base import Page, PoliteFetcher, crawl
from atlas.users.directories.k12 import (
    K12_REGISTRY,
    ApptegyAdapter,
    BlackboardAdapter,
    EdlioAdapter,
    FinalsiteAdapter,
    detect_k12,
    is_student_title,
    site_domains,
)
from atlas.users.directories.optout import Suppression
from atlas.users.haul import TokenBucket, status_line
from scripts import build_school_seeds as bss
from scripts import faculty_haul as fh

FIX = Path(__file__).parent / "fixtures" / "k12"
AT = "@"


def fx(name: str) -> str:
    return (FIX / name).read_text().replace("__AT__", AT)


def page(url: str, name: str) -> Page:
    return Page(url, 200, fx(name), "2026-09-30T00:00:00Z", "0" * 64, False)


def addr(local: str, domain: str) -> str:
    return local + AT + domain


CASES = [
    (FinalsiteAdapter, "finalsite.html", "maplegrove.k12.zz.us"),
    (BlackboardAdapter, "blackboard.html", "example.org"),
    (EdlioAdapter, "edlio.html", "example.net"),
    (ApptegyAdapter, "apptegy.html", "example.org"),
]


@pytest.mark.parametrize("cls,name,domain", CASES)
def test_detect_by_signature(cls, name, domain):
    assert detect_k12(fx(name)) is cls
    assert K12_REGISTRY[cls.platform] is cls and REGISTRY[cls.platform] is cls


def test_finalsite_parses_staff_and_skips_student():
    a = FinalsiteAdapter("nces-000000000001", [], domains=("maplegrove.k12.zz.us",), school="Maple Grove Elementary")
    recs = a.parse(page("https://www.maplegrove.k12.zz.us/staff", "finalsite.html"))
    got = {(r.name, r.title, tuple(r.departments), r.school, r.email) for r in recs}
    assert got == {
        ("Avery Placeholder", "Grade 3 Teacher", ("Elementary",), "Maple Grove Elementary",
         addr("aplaceholder", "maplegrove.k12.zz.us")),
        ("Blake Fixture", "Principal", ("Administration",), "Maple Grove Elementary",
         addr("bfixture", "maplegrove.k12.zz.us")),
    }
    assert a.student_skips == {"pages": 0, "records": 1}


def test_blackboard_parses_staff_and_skips_class_of():
    a = BlackboardAdapter("nces-000000000002", [], domains=("example.org",))
    recs = a.parse(page("https://riverbend.example.org/staff", "blackboard.html"))
    assert [(r.name, r.title, r.departments, r.email) for r in recs] == [
        ("Dana Sample", "Science Teacher", ["Science"], addr("dsample", "riverbend.example.org")),
        ("Frankie Mock", "Student Services Coordinator", [], addr("fmock", "riverbend.example.org")),
    ]
    assert a.student_skips["records"] == 1


def test_edlio_parses_staff_without_guessing_email():
    a = EdlioAdapter("nces-000000000003", [], domains=("example.net",))
    recs = a.parse(page("https://lakeside.example.net/apps/staff/", "edlio.html"))
    assert [(r.name, r.title, r.departments, r.email) for r in recs] == [
        ("Gale Dummy", "Math Teacher", ["Mathematics"], addr("gdummy", "lakeside.example.net")),
        ("Harper Stub", "Librarian", [], None),
    ]


def test_apptegy_parses_data_email_and_building():
    a = ApptegyAdapter("nces-000000000004", [], domains=("example.org",))
    recs = a.parse(page("https://pineridge.example.org/staff", "apptegy.html"))
    assert [(r.name, r.title, r.school, r.email) for r in recs] == [
        ("Indigo Double", "Counselor", "Pine Ridge High School", addr("idouble", "pineridge.example.org")),
        ("Jules Proxy", "Bus Driver", None, addr("jproxy", "pineridge.example.org")),
    ]


def test_student_page_is_skipped_and_counted():
    a = FinalsiteAdapter("nces-000000000001", [], domains=("maplegrove.k12.zz.us",))
    assert a.parse(page("https://www.maplegrove.k12.zz.us/students", "students.html")) == []
    assert a.student_skips == {"pages": 1, "records": 0}


@pytest.mark.parametrize("title,student", [
    ("Student", True), ("Students", True), ("Class of 2028", True), ("Grade 5", True), ("Senior", True),
    ("Student Ambassador", True), ("Student Services Director", False), ("Grade 3 Teacher", False),
    ("Principal", False), (None, False), ("Student Support Specialist", False),
])
def test_student_title_rule(title, student):
    assert is_student_title(title) is student


def test_records_carry_provenance_and_pass_optout(tmp_path):
    site = {"https://www.maplegrove.k12.zz.us/robots.txt": (404, ""),
            "https://www.maplegrove.k12.zz.us/staff": (200, fx("finalsite.html")),
            "https://www.maplegrove.k12.zz.us/staff?const_page=2": (200, fx("students.html"))}
    f = PoliteFetcher(tmp_path, delay=0, get=lambda u: site.get(u, (404, "")), sleep=lambda s: None)
    a = FinalsiteAdapter("nces-000000000001", ["https://www.maplegrove.k12.zz.us/staff"],
                         domains=("maplegrove.k12.zz.us",), school="Maple Grove Elementary")
    opt = tmp_path / "opt_out.csv"
    opt.write_text("orcid,email,name,ror_id,reason,as_of\n,,Blake Fixture,nces-000000000001,request,2026-09-30\n")
    import os

    os.environ["RESEARCH_ATLAS_TOMBSTONE_KEY"] = str(tmp_path / "key")
    recs, stats = crawl(a, f, Suppression.load(opt, tmp_path / "tomb.csv"))
    assert [r.name for r in recs] == ["Avery Placeholder"] and stats["suppressed_records"] == 1
    d = fh.provenance(recs[0].as_dict(), "x")
    assert d["source"] == "official_directory" and d["source_url"].endswith("/staff") and d["as_of"]
    assert d["match_tier"] is None and d["licence"] == "institution-copyright" and d["storage"] == "link_only"
    assert d["retrieved_by"].startswith("k12_finalsite/")
    assert a.student_skips == {"pages": 1, "records": 1}


def test_site_domains_drops_hosted_platforms():
    assert site_domains("https://www.district.k12.zz.us/", "https://x.finalsite.com/staff",
                        "https://sub.school.example.org/") == ("district.k12.zz.us", "example.org")


def _zip(path: Path, name: str, rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, buf.getvalue())


def test_school_seed_rows_and_probe(tmp_path):
    sch = [{"NCESSCH": "1", "SCH_NAME": "A School", "LEA_NAME": "D", "LEAID": "9", "ST": "ZZ",
            "WEBSITE": "", "LEVEL": "High", "UPDATED_STATUS_TEXT": "Open"},
           {"NCESSCH": "2", "SCH_NAME": "B School", "LEA_NAME": "D", "LEAID": "9", "ST": "ZZ",
            "WEBSITE": "b.example.org", "LEVEL": "Middle", "UPDATED_STATUS_TEXT": "Closed"}]
    _zip(tmp_path / "s.zip", "s.csv", sch)
    _zip(tmp_path / "l.zip", "l.csv", [{"LEAID": "9", "WEBSITE": "https://d.example.org"}])
    rows = bss.build_rows(bss.zip_rows(tmp_path / "s.zip"), bss.zip_rows(tmp_path / "l.zip"), "s.zip", "2026-09-30")
    assert [(r["nces_id"], r["website"], r["licence"]) for r in rows] == [("1", "https://d.example.org", bss.LICENCE)]
    pages = {"https://d.example.org/": (200, fx("edlio.html"), "https://d.example.org/"),
             "https://d.example.org/staff": (200, fx("edlio.html"), "https://d.example.org/staff")}
    get = lambda u: pages.get(u, (404, "", u))
    bucket = TokenBucket(0)
    from atlas.users.haul import RobotsCache

    probe = bss.SiteProbe(bucket, RobotsCache(lambda u: (404, ""), bucket), get=get)
    out = bss.probe_rows(rows, probe, 2, tmp_path / "p.csv", tmp_path / "p.log")
    assert out[0]["directory_entry_urls"] == "https://d.example.org/staff" and out[0]["platform_guess"] == "k12_edlio"
    assert (tmp_path / "p.log").read_text().count("platform=k12_edlio") == 1


def test_runner_schools_mode_uses_k12_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_ATLAS_TOMBSTONE_KEY", str(tmp_path / "key"))
    seeds = tmp_path / "schools.csv"
    with seeds.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=bss.FIELDS)
        w.writeheader()
        w.writerow({"nces_id": "000000000002", "name": "Riverbend High School", "website": "https://riverbend.example.org/",
                    "directory_entry_urls": "https://riverbend.example.org/staff", "platform_guess": "k12_blackboard"})
    assert fh.is_school_seeds(seeds)
    rows = fh.school_rows(seeds)
    site = {"https://riverbend.example.org/robots.txt": (404, ""),
            "https://riverbend.example.org/staff": (200, fx("blackboard.html"))}
    _, state = fh.run(rows, cache_root=tmp_path / "c", workers=1, max_pages=50, budget_bytes=10**9,
                         get=lambda u: site.get(u, (404, "")), bucket=TokenBucket(0),
                         adapter_factory=fh.school_adapter_for, min_free=0,
                         suppression=Suppression.load(tmp_path / "o.csv", tmp_path / "t.csv"))
    inst = state.institutions["nces-000000000002"]
    assert inst["profiles"] == 2 and inst["emails"] == 2 and inst["student_skipped"] == {"pages": 0, "records": 1}
    line = status_line(state, tmp_path / "c")
    assert "student_skipped=1" in line
    recs = [json.loads(x) for x in (tmp_path / "c" / "nces-000000000002" / "records.jsonl").read_text().splitlines()]
    assert all(r["storage"] == "link_only" and r["retrieved_by"].startswith("k12_blackboard/") for r in recs)
