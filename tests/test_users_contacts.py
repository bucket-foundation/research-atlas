"""Tests for the public-contact harvester.

The harvester must (a) extract only emails that are literally present in public
metadata, (b) attach provenance to every one, and (c) return None -- never a
guess -- when no public email exists. These tests use a fixed EuropePMC-shaped
fixture so they run offline and deterministically.
"""

import json

import atlas.users.contacts as C
from atlas.users.schema import ALLOWED_EMAIL_SOURCES


# A minimal EuropePMC `core` response: one corresponding author with an
# "Electronic address:" email, one author with no email.
FIXTURE = {
    "resultList": {
        "result": [
            {
                "pmid": "99999999",
                "authorList": {
                    "author": [
                        {
                            "fullName": "Lovelace A",
                            "authorId": {"type": "ORCID",
                                         "value": "0000-0000-0000-0001"},
                            "authorAffiliationDetailsList": {
                                "authorAffiliation": [
                                    {"affiliation": "Dept of Computing, Uni. "
                                     "Electronic address: ada@uni.edu."}
                                ]
                            },
                        },
                        {
                            "fullName": "Babbage C",
                            "authorAffiliationDetailsList": {
                                "authorAffiliation": [
                                    {"affiliation": "Dept of Engines, Uni."}
                                ]
                            },
                        },
                    ]
                },
            }
        ]
    }
}


def _patch(monkeypatch, payload):
    monkeypatch.setattr(C, "_cached_get", lambda *a, **k: payload)


def test_extracts_public_corresponding_email_with_provenance(monkeypatch):
    _patch(monkeypatch, FIXTURE)
    hit = C.europepmc_email("0000-0000-0000-0001", "Lovelace A")
    assert hit is not None
    assert hit["email"] == "ada@uni.edu"
    assert hit["email_source"] == "europepmc"
    assert hit["email_source"] in ALLOWED_EMAIL_SOURCES
    assert hit["email_source_url"].endswith("/99999999")
    assert hit["email_as_of"]
    assert hit["email_method"] == "corresponding-author-metadata"


def test_returns_none_for_author_without_public_email(monkeypatch):
    _patch(monkeypatch, FIXTURE)
    # Babbage has no Electronic address -> must be None, never fabricated
    hit = C.europepmc_email(None, "Babbage C")
    assert hit is None


def test_returns_none_when_no_results(monkeypatch):
    _patch(monkeypatch, {"resultList": {"result": []}})
    assert C.europepmc_email("0000-0000-0000-0009", "Nobody X") is None


def test_returns_none_on_network_failure(monkeypatch):
    _patch(monkeypatch, None)  # simulate a 503 / cache miss
    assert C.europepmc_email("0000-0000-0000-0001", "Lovelace A") is None


def test_orcid_email_requires_public_email(monkeypatch):
    # ORCID returns no public email -> None (no fabrication)
    _patch(monkeypatch, {"email": []})
    assert C.orcid_public_email("0000-0000-0000-0001") is None


def test_orcid_public_email_extracted_with_provenance(monkeypatch):
    _patch(monkeypatch, {"email": [{"email": "ada@orcid-public.org",
                                    "primary": True}]})
    hit = C.orcid_public_email("0000-0000-0000-0001")
    assert hit["email"] == "ada@orcid-public.org"
    assert hit["email_source"] == "orcid"
    assert hit["email_source_url"] == "https://orcid.org/0000-0000-0000-0001"
    assert hit["email_method"] == "orcid-public"


def test_orcid_none_orcid_returns_none():
    assert C.orcid_public_email(None) is None


def test_email_regex_only_matches_electronic_address():
    # The regex must not pull a random @ from body text -- only "Electronic
    # address:" lines (PubMed's corresponding-author convention).
    aff = "Some lab, see http://x@y for the website, no contact here."
    assert C._ELEC_RE.search(aff) is None


def test_never_fabricates_from_name_pattern(monkeypatch):
    # Even with a full name present, if metadata has no email -> None.
    _patch(monkeypatch, {"resultList": {"result": [
        {"pmid": "1", "authorList": {"author": [
            {"fullName": "Firstname Lastname",
             "authorAffiliationDetailsList": {"authorAffiliation": [
                 {"affiliation": "Some University, no email here."}]}}]}}]}})
    assert C.europepmc_email(None, "Firstname Lastname") is None


def test_epmc_query_does_not_use_invalid_has_email_filter(monkeypatch):
    # Regression: the old `AND HAS_EMAIL:Y` filter is not a valid EuropePMC
    # field and zeroed every result set (capped a 1M corpus at 184 hits).
    captured = {}

    def fake(sess, url, params, key, **kw):
        captured["query"] = params["query"]
        return {"resultList": {"result": []}}

    monkeypatch.setattr(C, "_cached_get", fake)
    C.europepmc_email("0000-0000-0000-0001", "Lovelace A")
    assert "HAS_EMAIL" not in captured["query"]
    assert 'AUTHORID:"0000-0000-0000-0001"' in captured["query"]


# --------------------------------------------------------------------------- #
# Crossref source: only literal public emails, with provenance
# --------------------------------------------------------------------------- #

CROSSREF_FIXTURE = {
    "message": {
        "items": [
            {
                "DOI": "10.1234/abc",
                "author": [
                    {"given": "Ada", "family": "Lovelace",
                     "ORCID": "http://orcid.org/0000-0000-0000-0001",
                     "affiliation": [
                         {"name": "Dept of Computing, Uni. ada@uni.edu"}]},
                    {"given": "Charles", "family": "Babbage",
                     "affiliation": [{"name": "Dept of Engines, Uni."}]},
                ],
            }
        ]
    }
}


def test_crossref_extracts_affiliation_email_with_provenance(monkeypatch):
    _patch(monkeypatch, CROSSREF_FIXTURE)
    hit = C.crossref_email("0000-0000-0000-0001", "Ada Lovelace")
    assert hit["email"] == "ada@uni.edu"
    assert hit["email_source"] == "crossref"
    assert hit["email_source"] in ALLOWED_EMAIL_SOURCES
    assert hit["email_source_url"] == "https://doi.org/10.1234/abc"
    assert hit["email_as_of"]
    assert hit["email_method"] == "crossref-author-metadata"


def test_crossref_returns_none_without_email(monkeypatch):
    _patch(monkeypatch, CROSSREF_FIXTURE)
    # Babbage has no email anywhere -> None, never fabricated
    hit = C.crossref_email(None, "Charles Babbage")
    assert hit is None


def test_crossref_none_inputs():
    assert C.crossref_email(None, None) is None


# --------------------------------------------------------------------------- #
# labpage source: only emails literally on the researcher's own public page
# --------------------------------------------------------------------------- #

def test_labpage_extracts_mailto_with_page_url_provenance(monkeypatch):
    calls = {"n": 0}

    def fake(sess, url, params, key, **kw):
        calls["n"] += 1
        if "researcher-urls" in url or "orcidurls" in key:
            return {"researcher-url": [
                {"url": {"value": "https://lab.uni.edu/lovelace"}}]}
        # the homepage fetch
        return {"_text": '<a href="mailto:ada.lovelace@uni.edu">email</a>',
                "_url": "https://lab.uni.edu/lovelace"}

    monkeypatch.setattr(C, "_cached_get", fake)
    hit = C.labpage_email("0000-0000-0000-0001", "Ada Lovelace")
    assert hit["email"] == "ada.lovelace@uni.edu"
    assert hit["email_source"] == "labpage"
    assert hit["email_source_url"] == "https://lab.uni.edu/lovelace"
    assert hit["email_method"] == "public-homepage"


def test_labpage_returns_none_when_no_public_url(monkeypatch):
    _patch(monkeypatch, {"researcher-url": []})
    assert C.labpage_email("0000-0000-0000-0001", "Ada Lovelace") is None


def test_labpage_requires_orcid():
    assert C.labpage_email(None, "Ada Lovelace") is None


# --------------------------------------------------------------------------- #
# anti-noise filter never fabricates, only removes boilerplate
# --------------------------------------------------------------------------- #

def test_boilerplate_addresses_are_rejected():
    assert not C._is_acceptable_email("no-reply@uni.edu")
    assert not C._is_acceptable_email("info@uni.edu")
    assert not C._is_acceptable_email("someone@example.com")
    assert C._is_acceptable_email("ada.lovelace@uni.edu")


# --------------------------------------------------------------------------- #
# orchestration / source priority
# --------------------------------------------------------------------------- #

def test_find_public_contact_respects_source_restriction(monkeypatch):
    # restrict to crossref only -> orcid/europepmc/labpage must not be consulted
    monkeypatch.setattr(C, "orcid_public_email",
                        lambda *a, **k: pytest_fail("orcid called"))
    monkeypatch.setattr(C, "europepmc_email",
                        lambda *a, **k: pytest_fail("epmc called"))
    monkeypatch.setattr(C, "labpage_email",
                        lambda *a, **k: pytest_fail("labpage called"))
    monkeypatch.setattr(C, "crossref_email",
                        lambda *a, **k: {"email": "ada@uni.edu",
                                         "email_source": "crossref"})
    hit = C.find_public_contact("0000-0000-0000-0001", "Ada Lovelace",
                                sources={"crossref"})
    assert hit["email_source"] == "crossref"


def test_epmc_first_priority_order(monkeypatch):
    order = []
    monkeypatch.setattr(C, "orcid_public_email",
                        lambda *a, **k: order.append("orcid") or None)
    monkeypatch.setattr(C, "europepmc_email",
                        lambda *a, **k: order.append("europepmc") or None)
    monkeypatch.setattr(C, "crossref_email",
                        lambda *a, **k: order.append("crossref") or None)
    monkeypatch.setattr(C, "labpage_email",
                        lambda *a, **k: order.append("labpage") or None)
    C.find_public_contact("x", "Y", epmc_first=True)
    assert order[0] == "europepmc"


def pytest_fail(msg):
    raise AssertionError(msg)
