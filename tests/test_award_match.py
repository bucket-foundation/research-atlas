"""Unit tests for award-id normalization (the grant<->work join keys)."""

from atlas.award_match import award_keys, grant_keys, nih_core_keys


# ----- NIH core key extraction --------------------------------------------- #

def test_nih_full_application_id():
    # 5R01CA068377-23 -> activity R01, IC CA, serial 068377
    assert nih_core_keys("5R01CA068377-23") == {"CA068377", "R01CA068377"}


def test_nih_with_amendment_suffix():
    assert nih_core_keys("3P30CA016059-39S4") == {"CA016059", "P30CA016059"}


def test_nih_ic_serial_only():
    assert nih_core_keys("CA068377") == {"CA068377"}
    assert nih_core_keys("MH120498") == {"MH120498"}


def test_nih_spaced_form():
    assert nih_core_keys("R01 CA 068377") == {"CA068377", "R01CA068377"}


def test_nih_unparseable_returns_empty():
    assert nih_core_keys("not-an-nih-id") == set()
    assert nih_core_keys("") == set()


# ----- the grant<->work join: same award, different conventions ------------ #

def test_nih_grant_links_to_openalex_award_variants():
    g = grant_keys("nih", "5R01CA068377-23")
    for oa_form in ("CA068377", "R01CA068377", "R01 CA 068377", "1R01CA068377-01"):
        assert g & award_keys("nih", oa_form), f"should link {oa_form}"


def test_nsf_bare_vs_directorate_prefixed():
    g = grant_keys("nsf", "2540313")
    assert g & award_keys("nsf", "2540313")
    assert g & award_keys("nsf", "DEB-2540313")


def test_cordis_links_across_ec_namespace():
    # our grant is sourced 'cordis'; OpenAlex EC awards are tagged 'ec'
    g = grant_keys("cordis", "645651")
    assert g & award_keys("ec", "645651")
    assert g & award_keys("ec", "H2020-645651")


def test_deliberate_non_match_does_not_link():
    g = grant_keys("nih", "5R01CA068377-23")
    # a different NIH award must NOT share a key
    assert not (g & award_keys("nih", "R01HL999999"))
    # an unrelated NSF number must NOT link to an NIH grant
    assert not (g & award_keys("nsf", "2540313"))


def test_empty_inputs_are_safe():
    assert award_keys("nih", "") == set()
    assert grant_keys("nih", "") == set()
    assert award_keys("", "") == set()
