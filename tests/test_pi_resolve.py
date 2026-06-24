"""Unit tests for the conservative grant-PI -> canonical-person resolver.

Mirrors the discipline of ``test_ror_matcher.py``: a tiny hand-built candidate
set covering the cases the resolver must get right --

- a clean name + same-org + same-field link (the happy path),
- a name + same-org link with no field signal (tier 2),
- title stripping (Wellcome's "Dr/Prof/..." prefixes) and hyphenated surnames,
- the **refusals** that protect against wrong links: a common surname shared by
  two people at the same org with no separating given name (ambiguous), a PI with
  no recipient-org overlap (no-org-overlap), and a same-name colleague in a
  *different* field (field-conflict). The whole point of the resolver is that
  these resolve to None, never to a wrong person.
"""

from __future__ import annotations

import pytest

from atlas.users.pi_resolve import (
    PiResolver,
    normalize_person_name,
    parse_name,
)


# ----- name helpers --------------------------------------------------------- #

def test_normalize_strips_accents_titles_punct():
    assert normalize_person_name("Müller-Schäfer") == "muller schafer"
    assert normalize_person_name("O'Brien") == "o brien"


def test_parse_name_explicit_first_last():
    np = parse_name("GREGORY  AARONS", "GREGORY", "AARONS")
    assert np is not None
    assert np.surname == "aarons"
    assert np.first_given == "gregory"
    assert np.first_initial == "g"


def test_parse_name_strips_title_prefix():
    # Wellcome style: "Dr Raheleh Rahbari"
    np = parse_name("Dr Raheleh Rahbari", "Dr", "Rahbari")
    assert np is not None and np.surname == "rahbari"
    # the title must not be mistaken for the given name
    assert np.first_given == "raheleh"


def test_parse_name_hyphenated_surname():
    np = parse_name("Cory  Abate-Shen", "Cory", "Abate-Shen")
    assert np is not None and np.surname == "shen"  # last token of flattened name
    assert np.first_given == "cory"


def test_parse_name_rejects_multi_pi_blob():
    # "Ms Heidi Good, Dr Garrett Livingston Mehl" -> two commas / two people
    np = parse_name("Ms Heidi Good, Dr Garrett Livingston Mehl",
                    "Ms", "Mehl")
    # explicit last/first present, so it parses to the explicit parts (we don't
    # fail it here -- the blob guard only fires when we must parse full_name) --
    # but a raw two-comma blob with no explicit parts is rejected:
    np2 = parse_name("Smith, John, Jones", None, None)
    assert np2 is None


# ----- a small candidate index ---------------------------------------------- #

@pytest.fixture(scope="module")
def resolver() -> PiResolver:
    r = PiResolver()
    # the target PI: Gregory Aarons, UC San Diego, biomed
    r.add_candidate("person:aarons", "Gregory A. Aarons",
                    orcid="0000-0001-1111-1111",
                    ror="https://ror.org/0168r3w48", field_slug="biomed-bio")
    # a same-org, same-surname different person with a different first name
    # (must NOT be matched to "Gregory")
    r.add_candidate("person:aarons2", "Jane Aarons", orcid=None,
                    ror="https://ror.org/0168r3w48", field_slug="biomed-bio")
    # TWO same-org, same-surname people sharing the first initial "J" -> the
    # ambiguous common-name trap the resolver must refuse.
    r.add_candidate("person:lee1", "J. Lee", orcid=None,
                    ror="https://ror.org/00f54p054", field_slug="physics-astro")
    r.add_candidate("person:lee2", "John Lee", orcid=None,
                    ror="https://ror.org/00f54p054", field_slug="physics-astro")
    # a clean physics person at Stanford, unique surname
    r.add_candidate("person:oertel", "Catherine Oertel", orcid=None,
                    ror="https://ror.org/00f54p054", field_slug="chemistry")
    # a same-name colleague as oertel's PI but in a different field, to test
    # field-conflict refusal: PI "Catherine Oertel" in physics, candidate chem.
    return r


# ----- the happy paths ------------------------------------------------------ #

def test_links_name_org_field(resolver):
    m = resolver.resolve(full_name="GREGORY  AARONS", first_name="GREGORY",
                         last_name="AARONS", pi_orcid=None,
                         recipient_ror="https://ror.org/0168r3w48",
                         field_slug="biomed-bio")
    assert m.resolved
    assert m.person_atlas_id == "person:aarons"
    assert m.method == "name+org+field"
    assert m.orcid == "0000-0001-1111-1111"


def test_links_name_org_no_field_signal(resolver):
    # no field signal on the PI side -> tier 2, still unique (Jane vs Gregory
    # separated by first name), so Gregory resolves to person:aarons.
    m = resolver.resolve(full_name="Gregory Aarons", first_name="Gregory",
                         last_name="Aarons", pi_orcid=None,
                         recipient_ror="https://ror.org/0168r3w48",
                         field_slug=None)
    assert m.resolved and m.person_atlas_id == "person:aarons"
    assert m.method == "name+org"


def test_orcid_tier_wins_when_present(resolver):
    m = resolver.resolve(full_name="anything", first_name=None, last_name=None,
                         pi_orcid="0000-0001-1111-1111",
                         recipient_ror=None, field_slug=None)
    assert m.resolved and m.method == "orcid" and m.score == 1.0


# ----- the refusals (never a wrong link) ------------------------------------ #

def test_refuses_no_org_overlap(resolver):
    # right name, but the PI's grant has no recipient ROR at all
    m = resolver.resolve(full_name="Gregory Aarons", first_name="Gregory",
                         last_name="Aarons", pi_orcid=None,
                         recipient_ror=None, field_slug="biomed-bio")
    assert not m.resolved and m.reason == "no-org-overlap"


def test_refuses_org_with_no_matching_candidate(resolver):
    m = resolver.resolve(full_name="Gregory Aarons", first_name="Gregory",
                         last_name="Aarons", pi_orcid=None,
                         recipient_ror="https://ror.org/zzzznotreal",
                         field_slug="biomed-bio")
    assert not m.resolved and m.reason == "no-org-overlap"


def test_refuses_ambiguous_common_name(resolver):
    # "J. Lee" at Stanford: two candidates (J. Lee, John Lee) both compatible
    # with the initial "J" and same field -> genuinely ambiguous -> refuse.
    m = resolver.resolve(full_name="J. Lee", first_name="J", last_name="Lee",
                         pi_orcid=None, recipient_ror="https://ror.org/00f54p054",
                         field_slug="physics-astro")
    assert not m.resolved
    assert m.reason in ("ambiguous", "field-conflict", "ambiguous-field")


def test_refuses_field_conflict(resolver):
    # PI "Catherine Oertel" in PHYSICS; the only Oertel candidate is in CHEMISTRY
    # -> a present, conflicting field must NOT link (same-name, wrong discipline).
    m = resolver.resolve(full_name="Catherine Oertel", first_name="Catherine",
                         last_name="Oertel", pi_orcid=None,
                         recipient_ror="https://ror.org/00f54p054",
                         field_slug="physics-astro")
    assert not m.resolved
    assert m.reason in ("field-conflict", "ambiguous-field")


def test_refuses_wrong_first_name_same_org(resolver):
    # A PI "Zachary Aarons" at UCSD: surname+org match Gregory/Jane Aarons but
    # neither first name is compatible -> no-name-overlap, never a wrong link.
    m = resolver.resolve(full_name="Zachary Aarons", first_name="Zachary",
                         last_name="Aarons", pi_orcid=None,
                         recipient_ror="https://ror.org/0168r3w48",
                         field_slug="biomed-bio")
    assert not m.resolved and m.reason == "no-name-overlap"
