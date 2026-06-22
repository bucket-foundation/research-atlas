"""Tests for the researcher segmentation proxies."""

from atlas.users import segment as S


def test_eminent_on_high_impact():
    assert S.seniority(2010, 2025, 12000, 3000, 40, True) == "eminent"


def test_established_on_track_record():
    assert S.seniority(2012, 2024, 800, 200, 12, True) == "established"


def test_rising_star_short_career_with_traction():
    assert S.seniority(2021, 2025, 120, 60, 4, True) == "rising-star"


def test_early_unknown_low_evidence():
    assert S.seniority(2024, 2025, 2, 2, 1, False) == "early/unknown"


def test_unknown_years_is_early():
    assert S.seniority(None, None, 100, 50, 5, True) == "early/unknown"


def test_activity_tier_active_pi():
    assert S.activity_tier(2025, True) == "active-pi"


def test_activity_tier_active():
    assert S.activity_tier(2024, False) == "active"


def test_activity_tier_dormant():
    assert S.activity_tier(2015, True) == "dormant"
    assert S.activity_tier(None, True) == "dormant"


def test_tool_fit_known_field_nonempty():
    fit = S.tool_fit("biomed-bio")
    assert "seq-pipelines" in fit
    assert all(isinstance(x, str) for x in fit)


def test_tool_fit_unknown_field_falls_back():
    assert S.tool_fit("nonexistent") == S.FIELD_TOOL_FIT["other"]


def test_make_segment_format():
    assert S.make_segment("math", "rising-star", "active-pi") == \
        "math/rising-star/active-pi"


def test_is_high_value_active_pi():
    assert S.is_high_value("rising-star", "active-pi", True) is True


def test_is_high_value_dormant_excluded():
    assert S.is_high_value("eminent", "dormant", True) is False


def test_is_high_value_active_corresponding_established():
    assert S.is_high_value("established", "active", True) is True


def test_is_high_value_active_noncorresponding_excluded():
    # active but never a corresponding author and only rising -> not high value
    assert S.is_high_value("rising-star", "active", False) is False
