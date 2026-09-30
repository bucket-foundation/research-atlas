from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.users.directories.base import (
    EMAIL_RE, PoliteFetcher, RobotsDenied, crawl, email_domain_ok, html_to_text,
)
from atlas.users.directories.stevens import StevensAdapter, listing_slugs
from atlas.users.schema import ALLOWED_EMAIL_SOURCES, coerce_user

REPO = Path(__file__).resolve().parents[1]


def next_page(page_data: dict) -> str:
    blob = json.dumps({"props": {"pageProps": {"pageData": page_data}}})
    return f'<html><script id="__NEXT_DATA__" type="application/json">{blob}</script></html>'


def profile(slug, name, email, dept="Mathematical Sciences", research="Group theory<br><br>Cryptography"):
    return next_page({
        "__typename": "PageProfile", "slug": slug, "title": name, "status": "Associate Professor",
        "email": email, "office": "Room 1", "phone": "15550000000",
        "school": {"title": "School of Engineering and Science"},
        "positionsCollection": {"items": [{"department": {"title": dept}}]},
        "sectionsCollection": {"items": [{"title": "Research", "object": {"value": research}}]},
    })


SITEMAP = """<urlset><url><loc>https://www.stevens.edu/profile/aaa1</loc></url>
<url><loc>https://www.stevens.edu/profile/bbb2</loc></url>
<url><loc>https://www.stevens.edu/school-x/departments/math/faculty</loc></url>
<url><loc>https://www.stevens.edu/news/x</loc></url></urlset>"""

LISTING = next_page({"__typename": "PageRightNav", "componentsCollection": {"items": [{
    "__typename": "FacultyListing", "__hydrationData": {"pageProfileCollection": {"items": [
        {"__typename": "PageProfile", "slug": "bbb2", "title": "Bo Sample", "email": "bo@stevens.edu"},
        {"__typename": "PageProfile", "slug": "ccc3"},
        {"__typename": "PageProfile", "slug": "ddd4", "title": "Di Listed", "status": "Lecturer",
         "email": "ddd4@stevens.edu",
         "contentfulMetadata": {"tags": [{"id": "affiliationFaculty"},
                                         {"id": "sesDepartmentChemistryAndChemicalBiology"}]}}]}}}]}})

SITE = {
    "https://www.stevens.edu/robots.txt": (200, "User-agent: *\nDisallow: /profile/ccc3\n"),
    "https://www.stevens.edu/sitemap.xml": (200, SITEMAP),
    "https://www.stevens.edu/school-x/departments/math/faculty": (200, LISTING),
    "https://www.stevens.edu/profile/aaa1": (200, profile("aaa1", "Ada Example", "AAA1@stevens.edu")),
    "https://www.stevens.edu/profile/bbb2": (200, profile("bbb2", "Bo Sample", "bo@gmail.com", "Physics")),
    "https://www.stevens.edu/profile/ddd4": (200, next_page({"__typename": "Search"})),
}


class FakeSite:
    def __init__(self, pages):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        return self.pages.get(url, (404, ""))


def fetcher(tmp_path, site, now=None, max_age_days=30):
    return PoliteFetcher(tmp_path / "pages", delay=0, get=site, now=now, sleep=lambda s: None,
                         max_age_days=max_age_days)


def test_domain_gate():
    assert email_domain_ok("a@stevens.edu", ["stevens.edu"])
    assert email_domain_ok("a@cs.stevens.edu", ["stevens.edu"])
    assert not email_domain_ok("a@notstevens.edu", ["stevens.edu"])
    assert not email_domain_ok("a@stevens.edu.evil.com", ["stevens.edu"])
    assert not email_domain_ok(None, ["stevens.edu"])


def test_html_to_text():
    assert html_to_text("Group theory<br><br>Crypto &amp; codes") == "Group theory; Crypto & codes"
    assert html_to_text(None) is None


def test_listing_slugs():
    assert listing_slugs(LISTING) == {"bbb2", "ccc3", "ddd4"}


def test_dept_from_tag():
    from atlas.users.directories.stevens import dept_from_tag
    assert dept_from_tag("sesDepartmentChemistryAndChemicalBiology") == "Chemistry and Chemical Biology"
    assert dept_from_tag("affiliationFaculty") is None


def test_crawl_end_to_end(tmp_path):
    site = FakeSite(SITE)
    records, stats = crawl(StevensAdapter(), fetcher(tmp_path, site))
    by = {r.slug: r for r in records}
    assert set(by) == {"aaa1", "bbb2", "ddd4"}
    d = by["ddd4"]
    assert d.source_kind == "listing" and d.email == "ddd4@stevens.edu"
    assert d.departments == ["Chemistry and Chemical Biology"]
    assert d.email_source_url == "https://www.stevens.edu/school-x/departments/math/faculty"
    a = by["aaa1"]
    assert a.email == "aaa1@stevens.edu" and a.email_source == "official_directory"
    assert a.email_source_url == "https://www.stevens.edu/profile/aaa1" and a.email_as_of == a.fetched_at
    assert a.departments == ["Mathematical Sciences"] and a.research_areas == "Group theory; Cryptography"
    assert "office" not in a.as_dict() and "phone" not in a.as_dict()
    b = by["bbb2"]
    assert b.source_kind == "listing" and b.email == "bo@stevens.edu"
    assert stats["robots_denied"] == 1 and "https://www.stevens.edu/profile/ccc3" not in site.calls
    assert stats["emails"] == 3 and stats["dropped_emails"] == 0
    assert stats["from_profile"] == 1 and stats["from_listing"] == 2


def test_cache_prevents_refetch_and_expires(tmp_path):
    t0 = datetime(2026, 9, 28, tzinfo=timezone.utc)
    clock = {"now": t0}
    site = FakeSite(SITE)
    f = fetcher(tmp_path, site, now=lambda: clock["now"])
    f.fetch("https://www.stevens.edu/profile/aaa1")
    f2 = fetcher(tmp_path, site, now=lambda: clock["now"])
    p = f2.fetch("https://www.stevens.edu/profile/aaa1")
    assert p.from_cache and site.calls.count("https://www.stevens.edu/profile/aaa1") == 1
    clock["now"] = t0 + timedelta(days=31)
    f3 = fetcher(tmp_path, site, now=lambda: clock["now"])
    assert not f3.fetch("https://www.stevens.edu/profile/aaa1").from_cache


def test_profile_with_off_domain_email_drops_it(tmp_path):
    site = FakeSite(SITE)
    f = fetcher(tmp_path, site)
    [rec] = StevensAdapter().parse(f.fetch("https://www.stevens.edu/profile/bbb2"))
    assert rec.email is None and rec.dropped_emails == ["bo@gmail.com"]


def test_robots_deny_raises(tmp_path):
    with pytest.raises(RobotsDenied):
        fetcher(tmp_path, FakeSite(SITE)).fetch("https://www.stevens.edu/profile/ccc3")


def test_robots_server_error_blocks_everything(tmp_path):
    site = FakeSite({**SITE, "https://www.stevens.edu/robots.txt": (503, "")})
    with pytest.raises(RobotsDenied):
        fetcher(tmp_path, site).fetch("https://www.stevens.edu/profile/aaa1")


def test_error_pages_are_not_cached(tmp_path):
    site = FakeSite(SITE)
    f = fetcher(tmp_path, site)
    assert f.fetch("https://www.stevens.edu/profile/zzz9").status == 404
    f.fetch("https://www.stevens.edu/profile/zzz9")
    assert site.calls.count("https://www.stevens.edu/profile/zzz9") == 2


def test_official_directory_is_an_allowed_source_with_provenance():
    assert "official_directory" in ALLOWED_EMAIL_SOURCES
    with pytest.raises(ValueError):
        coerce_user({"atlas_id": "p1", "email": "a@stevens.edu", "email_source": "official_directory",
                     "email_as_of": "2026-09-28T00:00:00Z"})


def test_official_cache_is_gitignored():
    out = subprocess.run(["git", "check-ignore", "-q", "data/raw/contacts/official/x/faculty.jsonl"], cwd=REPO)
    assert out.returncode == 0


def test_tracked_files_hold_no_institutional_emails():
    files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout.split()
    placeholder_domains = {"uni.edu", "y.edu", "inst.edu", "b.edu", "example.edu"}
    hits = []
    for f in files:
        if f == "tests/test_official_directory.py":
            continue
        p = REPO / f
        if p.suffix not in {".py", ".md", ".json", ".jsonl", ".csv", ".txt", ".toml", ".yml", ".yaml", ".cff", ".html", ".xml"}:
            continue
        for e in EMAIL_RE.findall(p.read_text(errors="ignore")):
            e = e.lower()
            dom = e.split("@")[1]
            if not any(dom == ph or dom.endswith("." + ph) for ph in placeholder_domains) and re.search(r"\.edu$|\.ac\.[a-z]+$", e):
                hits.append((f, e))
    assert hits == []


def test_timeouts_retry_then_count_as_errors(tmp_path):
    import requests

    calls = []

    def flaky(url):
        calls.append(url)
        if url.endswith("robots.txt"):
            return 200, "User-agent: *\nAllow: /\n"
        if url.endswith("aaa1") and calls.count(url) == 1:
            raise requests.exceptions.ReadTimeout("slow")
        if url.endswith("bbb2"):
            raise requests.exceptions.ConnectionError("down")
        return SITE.get(url, (404, ""))

    f = fetcher(tmp_path, flaky)
    assert f.fetch("https://www.stevens.edu/profile/aaa1").status == 200
    assert f.fetch("https://www.stevens.edu/profile/bbb2").status == 599
    assert calls.count("https://www.stevens.edu/profile/bbb2") == 3
    assert len(f.errors) == 4
