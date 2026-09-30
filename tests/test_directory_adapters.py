from __future__ import annotations

import gzip
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
