"""Unit tests for the OpenAlex works normalizer (no network, fixture-based)."""

import pytest

from atlas.connectors.openalex_works import OpenAlexWorksConnector
from atlas.schema import ENTITY_COLUMNS, EDGE_COLUMNS, coerce, make_id

# One realistic OpenAlex work page (trimmed to the fields the normalizer reads).
PAGE = {
    "meta": {"next_cursor": None},
    "results": [
        {
            "id": "https://openalex.org/W4300000001",
            "doi": "https://doi.org/10.1234/abc",
            "title": "Mitochondrial bioenergetics in cortical neurons",
            "display_name": "Mitochondrial bioenergetics in cortical neurons",
            "publication_year": 2023,
            "publication_date": "2023-04-15",
            "type": "article",
            "cited_by_count": 42,
            "open_access": {"is_oa": True, "oa_status": "gold"},
            "primary_topic": {
                "id": "https://openalex.org/T12267",
                "display_name": "Diet and metabolism studies",
                "score": 0.97,
                "subfield": {"id": "https://openalex.org/subfields/2737",
                             "display_name": "Physiology"},
                "field": {"id": "https://openalex.org/fields/27",
                          "display_name": "Medicine"},
                "domain": {"id": "https://openalex.org/domains/4",
                           "display_name": "Health Sciences"},
            },
            "authorships": [
                {
                    "author": {
                        "id": "https://openalex.org/A5000000001",
                        "display_name": "Doe, Jane",
                        "orcid": "https://orcid.org/0000-0002-1825-0097",
                    },
                    "institutions": [
                        {"id": "https://openalex.org/I1",
                         "display_name": "Stanford University",
                         "ror": "https://ror.org/00f54p054",
                         "country_code": "US", "type": "education"},
                    ],
                },
                {
                    "author": {"id": "https://openalex.org/A5000000002",
                               "display_name": "Smith, John", "orcid": None},
                    "institutions": [],
                },
            ],
            "awards": [
                {"id": "https://openalex.org/G1", "funder_award_id": "CA068377",
                 "funder_id": "https://openalex.org/F4320332161",
                 "funder_display_name": "National Institutes of Health"},
                {"id": "https://openalex.org/G2", "funder_award_id": "ZZ999999",
                 "funder_id": "https://openalex.org/F4320332161",
                 "funder_display_name": "National Institutes of Health"},
            ],
        }
    ],
}


@pytest.fixture
def rows():
    conn = OpenAlexWorksConnector.__new__(OpenAlexWorksConnector)
    conn.source = "openalex"
    # our grant CA068377 is known; ZZ999999 is not -> only one grant_work edge
    grant_atlas = make_id("grant", "nih", "5R01CA068377-23")
    gindex = {"NIH:CA068377": grant_atlas}
    out = list(conn.normalize([PAGE], grant_key_index=gindex))
    return out, grant_atlas


def _by_table(rows):
    d = {}
    for r in rows:
        d.setdefault(r.table, []).append(r.data)
    return d


def test_emits_one_schema_true_work(rows):
    out, _ = rows
    works = _by_table(out).get("work", [])
    assert len(works) == 1
    w = works[0]
    # coerce must accept it (schema-true)
    coerce("work", w)
    assert w["openalex_id"] == "W4300000001"
    assert w["doi"] == "https://doi.org/10.1234/abc"
    assert w["publication_year"] == 2023
    assert w["is_oa"] is True
    assert w["cited_by_count"] == 42


def test_person_keyed_on_orcid_when_present(rows):
    out, _ = rows
    people = {p["full_name"]: p for p in _by_table(out)["person"]}
    jane = people["Doe, Jane"]
    assert jane["orcid"] == "0000-0002-1825-0097"
    assert jane["openalex_author_id"] == "A5000000001"
    assert jane["atlas_id"] == make_id("person", "orcid:0000-0002-1825-0097")
    # author with no ORCID keys on the OpenAlex author id
    john = people["Smith, John"]
    assert john["orcid"] is None
    assert john["atlas_id"] == make_id("person", "openalex:A5000000002")


def test_org_keyed_on_ror(rows):
    out, _ = rows
    orgs = _by_table(out)["organization"]
    assert len(orgs) == 1
    assert orgs[0]["ror_id"] == "https://ror.org/00f54p054"
    assert orgs[0]["atlas_id"] == make_id("organization", "https://ror.org/00f54p054")


def test_topic_chain_has_parent_links(rows):
    out, _ = rows
    fields = {f["level"]: f for f in _by_table(out)["field"]}
    assert set(fields) == {"topic", "subfield", "field", "domain"}
    # domain is the root (no parent); topic chains up to it
    assert fields["domain"]["parent_atlas_id"] is None
    assert fields["topic"]["parent_atlas_id"] == fields["subfield"]["atlas_id"]
    assert fields["subfield"]["parent_atlas_id"] == fields["field"]["atlas_id"]
    assert fields["field"]["parent_atlas_id"] == fields["domain"]["atlas_id"]


def test_work_field_edge_carries_score(rows):
    out, _ = rows
    wf = _by_table(out)["work_field"]
    assert len(wf) == 1
    assert wf[0]["score"] == pytest.approx(0.97)


def test_grant_work_links_known_award_only(rows):
    out, grant_atlas = rows
    gw = _by_table(out)["grant_work"]
    # CA068377 links; ZZ999999 is unknown -> exactly one edge
    assert len(gw) == 1
    assert gw[0]["src_id"] == grant_atlas
    assert gw[0]["role"] == "acknowledges"


def test_person_org_affiliation_edge(rows):
    out, _ = rows
    po = _by_table(out)["person_org"]
    # only Jane has an institution
    assert len(po) == 1
    assert po[0]["role"] == "affiliation"


def test_all_emitted_rows_are_schema_true(rows):
    out, _ = rows
    for r in out:
        cols = ENTITY_COLUMNS.get(r.table) or EDGE_COLUMNS[r.table]
        coerced = coerce(r.table, r.data)
        assert set(coerced) == set(cols)
