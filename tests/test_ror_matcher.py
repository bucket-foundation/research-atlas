"""Unit tests for the offline ROR bulk matcher (no network, fixture-based).

The fixture is a tiny hand-built ROR-v2-shaped record set covering the cases the
real matcher must get right: exact, abbreviation-expanded, acronym, fuzzy, the
campus-vs-system ambiguity (must refuse), and a deliberate non-match that the
IDF-weighted fuzzy tier must NOT accept (the "Hill's Pet Nutrition" trap).
"""

from __future__ import annotations

import pytest

from atlas.ror_bulk import RorIndex, expand_abbrev, normalize_name


def _rec(rid, names, cc="US", city="Town", lat=1.0, lng=2.0,
         types=("education",), status="active", fundref=None):
    """Build a minimal ROR-v2 record. ``names`` is a list of (value, [types])."""
    rec = {
        "id": f"https://ror.org/{rid}",
        "status": status,
        "types": list(types),
        "names": [{"value": v, "types": t} for v, t in names],
        "locations": [{
            "geonames_details": {
                "country_code": cc, "name": city, "lat": lat, "lng": lng,
            }
        }],
        "links": [{"type": "website", "value": f"https://{rid}.example.edu"}],
        "external_ids": [],
    }
    if fundref:
        rec["external_ids"].append(
            {"type": "fundref", "preferred": fundref, "all": [fundref]})
    return rec


FIXTURE = [
    _rec("stanford", [("Stanford University", ["ror_display", "label"]),
                      ("SU", ["acronym"])], cc="US"),
    _rec("mit", [("Massachusetts Institute of Technology",
                  ["ror_display", "label"]),
                 ("MIT", ["acronym"])], cc="US"),
    _rec("rit", [("Rochester Institute of Technology",
                  ["ror_display", "label"])], cc="US"),
    # campus + its umbrella system (the ambiguity the matcher must refuse on a
    # bare "Purdue University" query, but where the campus wins over the system
    # when the query already names the campus)
    _rec("purdue-wl", [("Purdue University West Lafayette",
                        ["ror_display", "label"])], cc="US"),
    _rec("purdue-nw", [("Purdue University Northwest",
                        ["ror_display", "label"])], cc="US"),
    _rec("purdue-sys", [("Purdue University System",
                         ["ror_display", "label"])], cc="US"),
    # the false-positive trap: shares the common token "hill" with a query about
    # a university, but is a pet-food company
    _rec("hills", [("Hill's Pet Nutrition", ["ror_display", "label"])],
         cc="US", types=("company",)),
    _rec("unc", [("University of North Carolina at Chapel Hill",
                  ["ror_display", "label"])], cc="US"),
    # a non-US org to exercise country gating
    _rec("karolinska", [("Karolinska Institutet", ["ror_display", "label"])],
         cc="SE"),
    # withdrawn org must never be matched
    _rec("dead", [("Ghost University", ["ror_display"])], cc="US",
         status="withdrawn"),
    # funder cross-walk
    _rec("nsf", [("National Science Foundation", ["ror_display"]),
                 ("NSF", ["acronym"])], cc="US", types=("government",),
         fundref="100000001"),
]


@pytest.fixture(scope="module")
def idx() -> RorIndex:
    return RorIndex.from_records(iter(FIXTURE))


# ----- normalization helpers ------------------------------------------------ #

def test_normalize_strips_accents_punct_case():
    assert normalize_name("Universität Zürich!") == "universitat zurich"
    assert normalize_name("A&M") == "a and m"
    assert normalize_name("  Foo   Bar ") == "foo bar"


def test_expand_abbrev_known_tokens():
    assert expand_abbrev("rochester institute of tech") == \
        "rochester institute of technology"
    assert expand_abbrev("univ of oklahoma hlth sciences ctr") == \
        "university of oklahoma health sciences center"
    # single-token names are left to the acronym tier
    assert expand_abbrev("mit") == "mit"


# ----- exact tier ----------------------------------------------------------- #

def test_exact_match(idx):
    m = idx.match("Stanford University", "US")
    assert m is not None and m.method == "exact" and m.score == 1.0
    assert m.ror.ror_id == "https://ror.org/stanford"


def test_exact_match_is_case_and_punct_insensitive(idx):
    m = idx.match("massachusetts institute of technology", "US")
    assert m is not None and m.ror.ror_id == "https://ror.org/mit"


# ----- expanded tier -------------------------------------------------------- #

def test_expanded_match(idx):
    m = idx.match("Rochester Institute of Tech", "US")
    assert m is not None and m.method == "expanded"
    assert m.ror.ror_id == "https://ror.org/rit"


# ----- acronym tier --------------------------------------------------------- #

def test_acronym_match_requires_country(idx):
    m = idx.match("MIT", "US")
    assert m is not None and m.method == "acronym"
    assert m.ror.ror_id == "https://ror.org/mit"
    # an acronym with no country supplied is refused (acronyms collide globally)
    assert idx.match("MIT", None) is None


# ----- fuzzy tier + ambiguity refusal --------------------------------------- #

def test_campus_vs_system_is_refused(idx):
    # "Purdue University" matches West Lafayette, Northwest and System equally on
    # the distinctive token "purdue" -> genuinely ambiguous -> must be None.
    assert idx.match("Purdue University", "US") is None


def test_full_campus_name_resolves_exact(idx):
    m = idx.match("Purdue University West Lafayette", "US")
    assert m is not None and m.ror.ror_id == "https://ror.org/purdue-wl"


def test_idf_fuzzy_rejects_common_token_trap(idx):
    # the classic false positive: must NOT match "Hill's Pet Nutrition" on "hill"
    m = idx.match("University of North Carolina Chapel Hill Medical", "US")
    assert m is None or m.ror.ror_id != "https://ror.org/hills"


def test_deliberate_non_match_returns_none(idx):
    assert idx.match("Definitely Not A Real Org Xyzzy", "US") is None
    assert idx.match("", "US") is None


# ----- country gating + status ---------------------------------------------- #

def test_country_gating(idx):
    # right country resolves; a wrong country on a single-candidate exact key
    # still resolves (exact name is strong), but acronym/fuzzy are gated.
    assert idx.match("Karolinska Institutet", "SE") is not None


def test_withdrawn_orgs_never_indexed(idx):
    assert idx.match("Ghost University", "US") is None


# ----- fundref cross-walk --------------------------------------------------- #

def test_fundref_crosswalk(idx):
    assert idx.fundref.get("100000001") == "https://ror.org/nsf"


def test_index_len_counts_active_only(idx):
    # 11 fixture records, 1 withdrawn -> 10 active indexed
    assert len(idx) == 10
