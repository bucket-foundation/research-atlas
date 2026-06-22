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
