from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from atlas.users.directories import REGISTRY, detect_platform, detect_site
from atlas.users.directories.base import Page, PoliteFetcher, RobotsDenied, crawl
from atlas.users.directories.domains import (
    allowed_domains,
    email_in_domain,
    record_domains,
)
from atlas.users.directories.generic import (
    GenericAdapter,
    decode_cfemail,
    deobfuscate,
    sitemap_locs,
)
from atlas.users.directories.platforms import (
    CmsPeopleAdapter,
    PureAdapter,
    SymplecticAdapter,
    VivoAdapter,
)
from scripts.build_ror_domains import build_rows

FIX = Path(__file__).parent / "fixtures" / "directories"
ROR = "0abcde123"
DOMAINS = ("example.edu",)
TABLE = {ROR: DOMAINS}


def fx(name: str) -> str:
    return (FIX / name).read_text()


def page(url: str, name: str) -> Page:
    return Page(url, 200, fx(name), "2026-09-29T00:00:00Z", "0" * 64, False)


class FakeSite:
    def __init__(self, pages):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        return self.pages.get(url, (404, ""))


def fetcher(tmp_path, site, **kw):
    return PoliteFetcher(tmp_path / "pages", delay=0, get=site, sleep=lambda s: None, **kw)


def adapter(cls=GenericAdapter, entries=("https://www.example.edu/people",), **kw):
    return cls(ROR, entries, domains=DOMAINS, **kw)


def test_ror_rows_take_domains_and_website_hosts():
    [row] = build_rows([{
        "id": "https://ror.org/0ABCDE123", "types": ["education"],
        "names": [{"value": "Alias U", "types": ["alias"]}, {"value": "Example University", "types": ["ror_display"]}],
        "locations": [{"geonames_details": {"country_code": "US"}}],
        "domains": ["example.edu"],
        "links": [{"type": "website", "value": "https://www.med.example.edu/"},
                  {"type": "wikipedia", "value": "https://en.wikipedia.org/wiki/Example"}],
    }])
    assert row == {"ror_id": "0abcde123", "name": "Example University", "country_code": "US",
                   "domains": ["example.edu", "med.example.edu"], "types": ["education"]}
    assert record_domains({"links": [{"type": "website", "value": "https://sites.google.com/x"}]}) == []


def test_domain_gate_uses_ror_table():
    assert allowed_domains("https://ror.org/0abcde123", TABLE) == DOMAINS
    assert email_in_domain("a@bio.example.edu", ROR, TABLE)
    assert not email_in_domain("a@example.edu.evil.org", ROR, TABLE)
    assert not email_in_domain("a@gmail.com", ROR, TABLE)
    assert not email_in_domain("a@example.edu", "0unknown00", TABLE)


def test_jsonld_profile_keeps_plan_fields_and_skips_footer_mailto():
    [r] = adapter().parse(page("https://www.example.edu/people/ada-testperson", "generic_jsonld.html"))
    assert r.name == "Ada Testperson" and r.title == "Professor of Synthetic Studies"
    assert r.email == "ada.testperson@example.edu" and r.email_source == "official_directory"
    assert r.departments == ["Department of Placeholder Physics"]
    assert r.research_areas == "Membrane models; Toy proteins"
    d = r.as_dict()
    assert (d["source"], d["source_id"], d["source_url"], d["as_of"], d["match_tier"], d["licence"]) == (
        "official_directory", "www.example.edu/people/ada-testperson",
        "https://www.example.edu/people/ada-testperson", "2026-09-29T00:00:00Z", None, "institution-copyright")
    assert not {"office", "phone", "telephone", "image", "photo"} & set(d)


def test_microdata_person():
    [r] = adapter().parse(page("https://www.example.edu/people/bo-fixture", "generic_microdata.html"))
    assert (r.name, r.title, r.email) == ("Bo Fixture", "Associate Professor", "bfixture@chem.example.edu")
    assert r.departments == ["Department of Mock Chemistry"]


def test_next_data_person():
    [r] = adapter().parse(page("https://www.example.edu/people/cy-synthetic", "generic_nextdata.html"))
    assert (r.name, r.title, r.email) == ("Cy Synthetic", "Lecturer", "cy.synthetic@example.edu")
    assert r.departments == ["Imaginary Biology"]


def test_obfuscated_emails_decode():
    [r] = adapter().parse(page("https://www.example.edu/people/di-madeup", "obfuscated.html"))
    assert r.name == "Di Madeup" and r.email == "dmadeup@example.edu"
    [r2] = adapter().parse(page("https://www.example.edu/people/ed-notreal", "obfuscated_at.html"))
    assert r2.email == "enotreal@bio.example.edu"
    assert deobfuscate("reach me: x (at) example (dot) edu") == ["x@example.edu"]
    assert decode_cfemail("zz") is None


def test_off_domain_email_dropped_and_counted():
    [r] = adapter(CmsPeopleAdapter).parse(page("https://www.example.edu/people/jo-samplename", "cms_person.html"))
    assert r.email == "jsamplename@example.edu"
    assert r.dropped_emails == ["jsamplename@gmail.com"]
    assert r.departments == ["Department of Pretend Engineering"]
    [r2] = GenericAdapter(ROR, [], domains=("other.edu",)).parse(
        page("https://www.other.edu/people/jo-samplename", "cms_person.html"))
    assert r2.email is None and len(r2.dropped_emails) == 2


def test_pure_person_and_departments():
    a = adapter(PureAdapter, ("https://experts.example.edu/",))
    [r] = a.parse(page("https://experts.example.edu/en/persons/fay-placeholder", "pure_person.html"))
    assert (r.name, r.title, r.email) == ("Fay Placeholder", "Professor", "fplaceholder@example.edu")
    assert r.departments == ["Fake Physics"]


def test_vivo_individual():
    a = adapter(VivoAdapter, ("https://vivo.example.edu/",))
    [r] = a.parse(page("https://vivo.example.edu/individual/n123", "vivo_individual.html"))
    assert (r.name, r.title, r.email) == ("Hal Invented", "Research Scientist", "hinvented@example.edu")
    assert r.departments == ["Department of Sample Neuroscience"]


def test_symplectic_profile():
    a = adapter(SymplecticAdapter, ("https://scholars.example.edu/",))
    [r] = a.parse(page("https://scholars.example.edu/display/ivy-madeupname", "symplectic_profile.html"))
    assert (r.name, r.email) == ("Ivy Madeupname", "ivy.madeupname@example.edu")
    assert r.departments == ["School of Fictional Medicine"]


def test_listing_page_is_not_a_profile():
    assert adapter().parse(page("https://www.example.edu/people", "listing.html")) == []


SITEMAP_INDEX = """<sitemapindex><sitemap><loc>https://www.example.edu/sitemap-news.xml</loc></sitemap>
<sitemap><loc>https://www.example.edu/sitemap-people.xml</loc></sitemap></sitemapindex>"""
SITEMAP_PEOPLE = """<urlset><url><loc>https://www.example.edu/people/di-madeup</loc></url>
<url><loc>https://www.example.edu/people/secret-one</loc></url>
<url><loc>https://www.example.edu/news/story</loc></url></urlset>"""

SITE = {
    "https://www.example.edu/robots.txt": (200, "User-agent: *\nDisallow: /people/secret-one\n"
                                                "Sitemap: https://www.example.edu/sitemap-index.xml\n"),
    "https://www.example.edu/sitemap-index.xml": (200, SITEMAP_INDEX),
    "https://www.example.edu/sitemap-people.xml": (200, SITEMAP_PEOPLE),
    "https://www.example.edu/people": (200, fx("listing.html")),
    "https://www.example.edu/people?page=2": (200, fx("listing_page2.html")),
    "https://www.example.edu/people/ada-testperson": (200, fx("generic_jsonld.html")),
    "https://www.example.edu/people/bo-fixture/": (200, fx("generic_microdata.html")),
    "https://www.example.edu/people/cy-synthetic": (200, fx("generic_nextdata.html")),
    "https://www.example.edu/people/di-madeup": (200, fx("obfuscated.html")),
}


def test_sitemap_index_is_split():
    nested, urls = sitemap_locs(SITEMAP_INDEX)
    assert len(nested) == 2 and urls == []


def test_generic_crawl_discovers_listing_pagination_and_sitemap(tmp_path):
    site = FakeSite(SITE)
    a = adapter()
    records, stats = crawl(a, fetcher(tmp_path, site))
    emails = sorted(r.email for r in records)
    assert emails == ["ada.testperson@example.edu", "bfixture@chem.example.edu",
                      "cy.synthetic@example.edu", "dmadeup@example.edu"]
    assert "https://elsewhere.org/people/zed" not in site.calls
    assert "https://www.example.edu/files/cv.pdf" not in site.calls
    assert "https://www.example.edu/people/secret-one" not in site.calls
    assert a.discovery["sitemap_profiles"] == 2 and a.discovery["listing_pages"] == 2
    assert stats["robots_denied"] == 1 and stats["departments"] == 3
    cached = list((tmp_path / "pages").glob("*.html.gz"))
    assert cached and gzip.decompress(cached[0].read_bytes())


def test_robots_deny_blocks_generic_crawl(tmp_path):
    site = FakeSite({**SITE, "https://www.example.edu/robots.txt": (200, "User-agent: *\nDisallow: /\n")})
    records, _ = crawl(adapter(), fetcher(tmp_path, site))
    assert records == [] and site.calls == ["https://www.example.edu/robots.txt"]
    with pytest.raises(RobotsDenied):
        fetcher(tmp_path, site).fetch("https://www.example.edu/people/ada-testperson")


def test_budget_caps_requests(tmp_path):
    site = FakeSite(SITE)
    f = fetcher(tmp_path, site, budget=3)
    crawl(adapter(max_pages=3), f)
    assert f.requests <= 3 and len(site.calls) <= 3


def test_crawl_delay_is_honored(tmp_path):
    slept = []
    site = FakeSite({**SITE, "https://www.example.edu/robots.txt": (200, "User-agent: *\nCrawl-delay: 5\n")})
    f = PoliteFetcher(tmp_path / "p", delay=1, get=site, sleep=slept.append)
    f.fetch("https://www.example.edu/people/ada-testperson")
    f.fetch("https://www.example.edu/people/bo-fixture/")
    assert slept and max(slept) > 4


def test_challenge_pages_are_counted(tmp_path):
    site = FakeSite({"https://www.example.edu/robots.txt": (200, ""),
                     "https://www.example.edu/people": (403, "<html><title>Just a moment...</title></html>")})
    f = fetcher(tmp_path, site)
    assert f.fetch("https://www.example.edu/people").status == 403 and f.challenged == 1


def test_registry_and_detection(tmp_path):
    assert set(REGISTRY) == {"generic", "pure", "vivo", "symplectic", "cms"}
    site = FakeSite({
        "https://www.example.edu/robots.txt": (200, ""),
        "https://www.example.edu/": (200, "<html><p>welcome</p></html>"),
        "https://experts.example.edu/robots.txt": (200, ""),
        "https://experts.example.edu/": (200, "<html>portal</html>"),
        "https://experts.example.edu/en/persons/": (200, fx("pure_listing.html")),
    })
    cls, base = detect_site(ROR, "https://www.example.edu", fetcher(tmp_path, site), DOMAINS)
    assert cls is PureAdapter and base == "https://experts.example.edu/"
    cms = FakeSite({"https://www.example.edu/robots.txt": (200, ""),
                    "https://www.example.edu/": (200, fx("cms_person.html"))})
    assert detect_platform(ROR, "https://www.example.edu", fetcher(tmp_path / "b", cms), DOMAINS) is CmsPeopleAdapter
    nothing = FakeSite({"https://www.example.edu/robots.txt": (200, ""),
                        "https://www.example.edu/": (200, "<html></html>")})
    assert detect_platform(ROR, "https://www.example.edu", fetcher(tmp_path / "c", nothing), DOMAINS) is None


def test_email_on_two_records_is_dropped_from_both():
    from atlas.users.directories.base import drop_shared_emails
    a = adapter()
    [r1] = a.parse(page("https://www.example.edu/people/ada-testperson", "generic_jsonld.html"))
    [r2] = a.parse(page("https://www.example.edu/people/ada-copy", "generic_jsonld.html"))
    assert drop_shared_emails([r1, r2]) == 1
    assert r1.email is None and r2.email is None and r1.email_source is None


@pytest.mark.parametrize("prose", [
    "Meet us at admissions.example.edu for a tour.",
    "The lab is at bio.example.edu and at chem dot example dot edu.",
    "Classes start at 9.30 and end at noon.",
    "Contact us at the front desk dot example.",
])
def test_prose_yields_no_email(prose):
    assert deobfuscate(f"<p>{prose}</p>") == []


def test_bracketed_forms_still_decode():
    assert deobfuscate("x.y [at] example [dot] edu") == ["x.y@example.edu"]
    assert deobfuscate("zed at bio[dot]example[dot]edu") == ["zed@bio.example.edu"]
    assert deobfuscate("q(at)example.edu") == ["q@example.edu"]


def test_role_mailbox_is_kept_apart_from_person_email():
    html = "<html><h1>Kim Sampleton</h1><a href='mailto:registrar@example.edu'>Registrar</a></html>"
    [r] = adapter().parse(Page("https://www.example.edu/people/kim-sampleton", 200, html, "t", "0", False))
    assert r.email is None and r.org_mailbox == "registrar@example.edu"


def test_nested_sitemap_off_site_is_skipped(tmp_path):
    index = """<sitemapindex><sitemap><loc>https://tracker.other.org/people.xml</loc></sitemap>
    <sitemap><loc>https://www.example.edu/sitemap-people.xml</loc></sitemap></sitemapindex>"""
    site = FakeSite({"https://www.example.edu/robots.txt": (200, "Sitemap: https://cdn.other.org/sm.xml\n"),
                     "https://www.example.edu/sitemap.xml": (200, index),
                     "https://www.example.edu/sitemap-people.xml": (200, SITEMAP_PEOPLE)})
    found = adapter().sitemap_profiles(fetcher(tmp_path, site), "https://www.example.edu/", 50)
    assert found == ["https://www.example.edu/people/di-madeup", "https://www.example.edu/people/secret-one"]
    assert not [c for c in site.calls if "other.org" in c]


class Exploding(GenericAdapter):
    def parse(self, page):
        raise ValueError("bad markup")


def test_parse_failure_is_counted_with_url(tmp_path, capsys):
    site = FakeSite(SITE)
    a = Exploding(ROR, ["https://www.example.edu/people"], domains=DOMAINS)
    records, stats = crawl(a, fetcher(tmp_path, site))
    assert records == [] and stats["parse_errors"] > 0
    assert stats["parse_error_urls"][0].startswith("https://www.example.edu/")
    assert "parse error https://www.example.edu/" in capsys.readouterr().err


def test_zero_yield_run_exits_non_zero(tmp_path, monkeypatch):
    import scripts.crawl_official_directory as cli
    from atlas.users.directories import base
    monkeypatch.setattr(cli, "OFFICIAL_CACHE", tmp_path)
    monkeypatch.setattr(base, "_requests_get", lambda url: (200, "<html></html>") if url.endswith("robots.txt")
                        else (404, ""))
    import atlas.users.directories.generic as gen
    monkeypatch.setattr(gen, "allowed_domains", lambda ror: DOMAINS)
    code = cli.main([ROR, "--platform", "generic", "--entry", "https://www.example.edu/people", "--delay", "0"])
    assert code == 2
    assert cli.exit_code({"records": 3, "emails": 1}) == 0
    assert cli.exit_code({"records": 3, "emails": 0}) == 2


def test_missing_ror_domains_table_raises(tmp_path, monkeypatch):
    from atlas.users.directories import domains
    monkeypatch.setattr(domains, "ROR_DOMAINS", tmp_path / "absent.parquet")
    with pytest.raises(FileNotFoundError, match="build_ror_domains"):
        allowed_domains(ROR)


def test_robots_forbidden_status_denies(tmp_path):
    site = FakeSite({"https://www.example.edu/robots.txt": (403, "")})
    with pytest.raises(RobotsDenied):
        fetcher(tmp_path, site).fetch("https://www.example.edu/people")


def test_cached_page_rechecks_robots_after_a_day(tmp_path):
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    site = FakeSite(dict(SITE))
    fetcher(tmp_path, site, now=lambda: t0).fetch("https://www.example.edu/people/ada-testperson")
    site.pages["https://www.example.edu/robots.txt"] = (200, "User-agent: *\nDisallow: /people/\n")
    later = fetcher(tmp_path, site, now=lambda: t0 + timedelta(days=2))
    with pytest.raises(RobotsDenied):
        later.fetch("https://www.example.edu/people/ada-testperson")


def test_default_delay_spaces_same_host_only(tmp_path):
    slept = []
    f = PoliteFetcher(tmp_path / "p", get=FakeSite(SITE), sleep=slept.append)
    f.fetch("https://www.example.edu/people/ada-testperson")
    assert len(slept) == 1 and 0.8 < slept[0] <= 1.0
    f._wait("other.example.edu")
    assert len(slept) == 1
    f._wait("www.example.edu")
    assert len(slept) == 2 and 0.8 < slept[1] <= 1.0


def suppression(tmp_path, rows):
    import csv
    from atlas.users.directories.optout import OPT_OUT_COLUMNS, Suppression
    p = tmp_path / "private" / "opt_out.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, OPT_OUT_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return Suppression.load(p, tmp_path / "private" / "tombstones.csv")


def test_suppressed_seed_makes_no_request(tmp_path):
    from atlas.users.directories.stevens import StevensAdapter
    from tests.test_official_directory import SITE as STEVENS, FakeSite as SFake
    site = SFake(STEVENS)
    sup = suppression(tmp_path, [{"name": "Di Listed", "ror_id": "02z43xh36", "reason": "request"}])
    records, stats = crawl(StevensAdapter(), fetcher(tmp_path, site), sup)
    assert "https://www.stevens.edu/profile/ddd4" not in site.calls
    assert stats["suppressed_seeds"] == 1 and "ddd4" not in {r.slug for r in records}


def test_suppressed_record_is_never_written(tmp_path):
    import scripts.crawl_official_directory as cli
    sup = suppression(tmp_path, [{"email": "ada.testperson@example.edu"}])
    [rec] = adapter().parse(page("https://www.example.edu/people/ada-testperson", "generic_jsonld.html"))
    [other] = adapter().parse(page("https://www.example.edu/people/bo-fixture", "generic_microdata.html"))
    out = tmp_path / "faculty.jsonl"
    assert cli.write_records(out, [rec, other], sup) == 1
    text = out.read_text()
    assert "Ada Testperson" not in text and "Bo Fixture" in text
    assert "dropped_emails" not in text and "org_mailbox" not in text


def test_crawl_drops_suppressed_records(tmp_path):
    sup = suppression(tmp_path, [{"orcid": "", "name": "Bo Fixture", "ror_id": ROR}])
    records, stats = crawl(adapter(), fetcher(tmp_path, FakeSite(SITE)), sup)
    assert "Bo Fixture" not in {r.name for r in records} and stats["suppressed_records"] >= 1
    assert all(r.retrieved_by == "generic/0.3" and r.storage == "link_only" for r in records)


def test_removal_purges_cache_and_writes_tombstone(tmp_path):
    import csv
    from atlas.users.directories.optout import Suppression
    root = tmp_path / "official"
    f = PoliteFetcher(root / ROR / "pages", delay=0, get=FakeSite(SITE), sleep=lambda s: None)
    records, _ = crawl(adapter(), f)
    (root / ROR / "faculty.jsonl").write_text("".join(json.dumps(r.as_dict()) + "\n" for r in records))
    before = len(list((root / ROR / "pages").glob("*.json")))
    sup = Suppression.load(tmp_path / "none.csv", tmp_path / "private" / "tombstones.csv")
    purged = sup.remove(root, name="Ada Testperson", ror_id=ROR, email="ada.testperson@example.edu")
    assert purged["records"] == 1 and purged["pages"] >= 1
    assert len(list((root / ROR / "pages").glob("*.json"))) < before
    assert "Ada Testperson" not in (root / ROR / "faculty.jsonl").read_text()
    rows = list(csv.DictReader((tmp_path / "private" / "tombstones.csv").open()))
    assert {r["kind"] for r in rows} == {"email", "name_ror", "url"}
    assert all(r["scheme"] == "hmac-sha256" for r in rows)
    assert not any("ada" in r["sha256"] for r in rows) and all(len(r["sha256"]) == 64 for r in rows)
    reloaded = Suppression.load(tmp_path / "none.csv", tmp_path / "private" / "tombstones.csv")
    assert reloaded.blocks(name="ada  TESTPERSON", ror_id=f"https://ror.org/{ROR}")


def test_join_sets_t2_and_skips_suppressed(tmp_path):
    from atlas.users.directories.join import join_records
    sup = suppression(tmp_path, [{"name": "Bo Fixture", "ror_id": ROR}])
    [a] = adapter().parse(page("https://www.example.edu/people/ada-testperson", "generic_jsonld.html"))
    [b] = adapter().parse(page("https://www.example.edu/people/bo-fixture", "generic_microdata.html"))
    people = [{"atlas_id": "p1", "name": "A. Testperson", "ror_id": ROR},
              {"atlas_id": "p2", "name": "Bo Fixture", "ror_id": ROR}]
    joined = join_records([a, b], people, sup)
    assert [(r.name, pid) for r, pid in joined] == [("Ada Testperson", "p1")]
    assert a.match_tier == "T2" and b.match_tier is None


def test_removed_url_is_never_requested_again(tmp_path):
    from atlas.users.directories.optout import Suppression
    root = tmp_path / "official"
    tomb = tmp_path / "private" / "tombstones.csv"
    target = "https://www.example.edu/people/cy-synthetic"
    crawl(adapter(), PoliteFetcher(root / ROR / "pages", delay=0, get=FakeSite(SITE), sleep=lambda s: None))
    Suppression.load(tmp_path / "none.csv", tomb).remove(root, ror_id=ROR, profile_url=target)
    site = FakeSite(SITE)
    sup = Suppression.load(tmp_path / "none.csv", tomb)
    _, stats = crawl(adapter(), PoliteFetcher(root / ROR / "pages", delay=0, get=site, sleep=lambda s: None,
                                              max_age_days=0), sup)
    assert target not in site.calls and stats["suppressed_seeds"] >= 1
    assert sup.blocks_url("http://example.edu/people/cy-synthetic/")


def test_hash_digests_are_stable_under_the_test_key():
    from atlas.users.directories.optout import keyed, person_keys, url_key
    k = b"k" * 32
    assert keyed("ada.testperson@example.edu", k) == PINNED["email"]
    assert person_keys("Ada Testperson", ROR, key=k) == [("name_ror", PINNED["name_ror"])]
    assert url_key("https://www.example.edu/people/ada-testperson/", k) == ("url", PINNED["url"])
    assert keyed("x", b"j" * 32) != keyed("x", k)


def test_key_file_is_created_private(tmp_path, monkeypatch):
    import os
    import stat
    from atlas.users.directories.optout import load_key
    path = tmp_path / "cfg" / "tombstone.key"
    monkeypatch.setenv("RESEARCH_ATLAS_TOMBSTONE_KEY", str(path))
    key = load_key()
    assert len(key) == 32 and load_key() == key
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_plain_tombstones_migrate_or_move_to_legacy(tmp_path):
    import csv
    from atlas.users.directories.optout import Suppression, sha
    priv = tmp_path / "private"
    priv.mkdir()
    (priv / "opt_out.csv").write_text("orcid,email,name,ror_id,reason,as_of\n,ada.testperson@example.edu,,,r,t\n")
    (priv / "tombstones.csv").write_text(
        "kind,sha256,as_of\n"
        f"email,{sha('ada.testperson@example.edu')},t\n"
        f"email,{sha('gone.person@example.edu')},t\n")
    sup = Suppression.load(priv / "opt_out.csv", priv / "tombstones.csv")
    assert sup.migration == {"kept": 0, "rewritten": 1, "legacy": 1}
    rows = list(csv.DictReader((priv / "tombstones.csv").open()))
    assert len(rows) == 1 and rows[0]["scheme"] == "hmac-sha256" and rows[0]["sha256"] != sha("ada.testperson@example.edu")
    assert sup.blocks(email="gone.person@example.edu") and sup.blocks(email="ada.testperson@example.edu")
    assert len(list(csv.DictReader((priv / "tombstones_legacy.csv").open()))) == 1


def test_purge_removes_listing_pages_that_name_the_person(tmp_path):
    from atlas.users.directories.optout import Suppression
    root = tmp_path / "official"
    f = PoliteFetcher(root / ROR / "pages", delay=0, get=FakeSite(SITE), sleep=lambda s: None)
    f.fetch("https://www.example.edu/people")
    f.fetch("https://www.example.edu/people?page=2")
    sup = Suppression.load(tmp_path / "none.csv", tmp_path / "private" / "tombstones.csv")
    sup.remove(root, name="Cy Synthetic", ror_id=ROR)
    left = [json.loads(p.read_text())["url"] for p in (root / ROR / "pages").glob("*.json")]
    assert "https://www.example.edu/people?page=2" not in left
    assert "https://www.example.edu/people" in left


def test_join_records_candidate_count(tmp_path):
    from atlas.users.directories.join import join_records
    sup = suppression(tmp_path, [])
    [a] = adapter().parse(page("https://www.example.edu/people/ada-testperson", "generic_jsonld.html"))
    people = [{"atlas_id": "p1", "name": "Ada Testperson", "ror_id": ROR},
              {"atlas_id": "p2", "name": "Alan Testperson", "ror_id": ROR}]
    assert join_records([a], people, sup) == []
    assert a.match_tier is None and a.match_candidates == 2


PINNED = {
    "email": "fe0f5f981171ce1cf8f1b4a94302d95c57a62a3deee0ed0e62d1fd86d4985630",
    "name_ror": "9e9e145782e20fb29062e8f793bdc69e9059cfee61d30a720acaddbe08ce0fa0",
    "url": "d0b8917f4da5061e9bf64b69bd303673a6fad2c1dd82ef91c5a0bfa31dae6717",
}


def test_concurrent_key_creation_yields_one_key(tmp_path, monkeypatch):
    import threading
    from atlas.users.directories.optout import load_key
    monkeypatch.setenv("RESEARCH_ATLAS_TOMBSTONE_KEY", str(tmp_path / "k" / "tombstone.key"))
    got, start = [], threading.Barrier(8)

    def worker():
        start.wait()
        got.append(load_key())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 8 and len(set(got)) == 1 and len(got[0]) == 32
    assert list((tmp_path / "k").iterdir()) == [tmp_path / "k" / "tombstone.key"]


def test_short_key_file_is_rejected(tmp_path, monkeypatch):
    from atlas.users.directories.optout import load_key
    bad = tmp_path / "short.key"
    bad.write_bytes(b"abc")
    monkeypatch.setenv("RESEARCH_ATLAS_TOMBSTONE_KEY", str(bad))
    with pytest.raises(RuntimeError, match="32 bytes"):
        load_key(pause=0)


@pytest.mark.parametrize("path", ["data/private/opt_out.csv", "data/private/tombstones.csv",
                                  "data/private/tombstones_legacy.csv", "data/private/any/nested.txt"])
def test_private_files_are_gitignored(path):
    import subprocess
    repo = Path(__file__).resolve().parents[1]
    assert subprocess.run(["git", "check-ignore", "-q", path], cwd=repo).returncode == 0


def test_only_gitkeep_is_tracked_under_private():
    import subprocess
    repo = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(["git", "ls-files", "data/private"], cwd=repo, capture_output=True, text=True).stdout.split()
    assert tracked == ["data/private/.gitkeep"]


def test_legacy_hash_blocks_seed_record_and_join(tmp_path):
    from atlas.users.directories.join import join_records
    from atlas.users.directories.optout import Suppression, norm_url, sha
    from atlas.users.directories.stevens import StevensAdapter
    from tests.test_official_directory import SITE as STEVENS, FakeSite as SFake
    priv = tmp_path / "private"
    priv.mkdir()
    (priv / "tombstones.csv").write_text("kind,sha256,as_of,scheme\n")
    (priv / "tombstones_legacy.csv").write_text(
        "kind,sha256,as_of\n"
        f"url,{sha(norm_url('https://www.stevens.edu/profile/ddd4'))},t\n"
        f"email,{sha('aaa1' + '@' + 'stevens.edu')},t\n"
        f"name_ror,{sha('ada testperson|' + ROR)},t\n")
    sup = Suppression.load(priv / "none.csv", priv / "tombstones.csv")
    site = SFake(STEVENS)
    records, stats = crawl(StevensAdapter(), fetcher(tmp_path, site), sup)
    assert "https://www.stevens.edu/profile/ddd4" not in site.calls and stats["suppressed_seeds"] >= 1
    assert "aaa1" not in {r.slug for r in records} and stats["suppressed_records"] >= 1
    assert stats["legacy_tombstones"] == 3
    [a] = adapter().parse(page("https://www.example.edu/people/ada-testperson", "generic_jsonld.html"))
    assert join_records([a], [{"atlas_id": "p1", "name": "Ada Testperson", "ror_id": ROR}], sup) == []


def test_key_is_read_once_per_process(tmp_path, monkeypatch):
    from atlas.users.directories import optout
    path = tmp_path / "once.key"
    path.write_bytes(b"z" * 32)
    monkeypatch.setenv("RESEARCH_ATLAS_TOMBSTONE_KEY", str(path))
    reads = []
    real = optout._read_or_create_key
    monkeypatch.setattr(optout, "_read_or_create_key", lambda *a: reads.append(1) or real(*a))
    for _ in range(50):
        optout.keyed("x")
    assert len(reads) == 1


def test_tombstones_are_written_before_the_purge(tmp_path, monkeypatch):
    from atlas.users.directories import optout
    order = []
    monkeypatch.setattr(optout, "_purge_pages", lambda *a, **k: order.append("purge") or 0)
    sup = optout.Suppression.load(tmp_path / "none.csv", tmp_path / "private" / "tombstones.csv")
    real = sup._write_tombstones
    monkeypatch.setattr(sup, "_write_tombstones", lambda keys: order.append("tombstone") or real(keys))
    (tmp_path / "official" / ROR).mkdir(parents=True)
    sup.remove(tmp_path / "official", name="Ada Testperson", ror_id=ROR,
               profile_url="https://www.example.edu/people/ada-testperson")
    assert order[0] == "tombstone" and "purge" in order


def test_malformed_html_is_counted_by_url(tmp_path, monkeypatch, capsys):
    from atlas.users.directories import generic
    def boom(self, data):
        raise AssertionError("bad tag")
    monkeypatch.setattr(generic._TreeBuilder, "feed", boom)
    records, stats = crawl(adapter(), fetcher(tmp_path, FakeSite(SITE)))
    assert stats["malformed_html"] > 0
    assert stats["malformed_html_urls"][0].startswith("https://www.example.edu/")
    assert "malformed html https://www.example.edu/" in capsys.readouterr().err


def test_role_mailbox_list_loads_from_config(tmp_path):
    from atlas.users.directories.generic import ROLE_MAILBOX_FILE, is_role_mailbox, load_role_mailboxes
    assert ROLE_MAILBOX_FILE.exists() and is_role_mailbox("registrar@example.edu")
    assert not is_role_mailbox("ada.testperson@example.edu")
    custom = tmp_path / "roles.txt"
    custom.write_text("labmanager\n\nbiz\n")
    pat = load_role_mailboxes(custom)
    assert pat.match("labmanager") and pat.match("biz.office") and not pat.match("info")
    empty = tmp_path / "empty.txt"
    empty.write_text("\n")
    with pytest.raises(ValueError, match="empty"):
        load_role_mailboxes(empty)
