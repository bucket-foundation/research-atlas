"""Segmentation: turn raw activity/impact numbers into CRM segments + tool-fit.

Three orthogonal axes, all computed from atlas data (never self-reported):

- **seniority** (career-stage proxy): from career span + impact. ``rising-star``
  = recent first appearance but already cited / corresponding; ``established`` =
  a decade-ish career with solid impact; ``eminent`` = very high impact;
  ``early/unknown`` otherwise. This generalizes the prototype's
  ``rising-star`` / ``eminent`` flags in ``funnel_targets.csv``.
- **activity_tier**: ``active-pi`` (active *and* a corresponding author -- the PI
  signal), ``active`` (published recently), ``dormant`` otherwise.
- **tool_fit**: which cross-field software needs (the roadmap rows in
  ``docs/USERS_NEEDS.md``) match this researcher, keyed by their field slug.

Thresholds are deliberately conservative and documented; they are proxies over
*the atlas corpus only* (a bounded slice of each person's true output), so they
under-count rather than over-claim -- the honest direction.
"""

from __future__ import annotations

# field slug -> the tool/need slugs from the cross-field roadmap that fit a
# researcher in that field. These are the slugs in docs/USERS_NEEDS.md's table.
FIELD_TOOL_FIT = {
    "biomed-bio": [
        "seq-pipelines", "imaging-analysis", "structure-pred",
        "fair-data-mgmt", "stats-reproducibility",
    ],
    "chemistry": [
        "reaction-informatics", "spectra-analysis", "molecular-sim",
        "lab-eln", "fair-data-mgmt",
    ],
    "physics-astro": [
        "hpc-sim", "large-data-pipelines", "instrument-control",
        "ml-surrogates", "fair-data-mgmt",
    ],
    "materials": [
        "molecular-sim", "ml-surrogates", "high-throughput-screening",
        "lab-eln", "fair-data-mgmt",
    ],
    "cs-ml": [
        "reproducibility-mlops", "compute-orchestration", "benchmark-tooling",
        "model-cards-eval",
    ],
    "earth-climate": [
        "large-data-pipelines", "geospatial-analysis", "hpc-sim",
        "fair-data-mgmt", "model-intercomparison",
    ],
    "econ-social": [
        "stats-reproducibility", "survey-data-mgmt", "causal-inference-tooling",
        "fair-data-mgmt",
    ],
    "math": [
        "proof-assistants", "symbolic-compute", "reproducibility-mlops",
    ],
    "engineering": [
        "hpc-sim", "ml-surrogates", "lab-eln", "fair-data-mgmt",
    ],
    "other": ["fair-data-mgmt", "stats-reproducibility"],
}

THIS_YEAR = 2026
ACTIVE_WINDOW = 3          # published within the last N years -> "active"


def seniority(first_year, last_year, total_citations, max_citations,
              h_index_proxy, is_corresponding) -> str:
    """Career-stage proxy from corpus span + impact.

    Honest proxy, biased to under-claim. Career *span* is bounded by the corpus
    window (works since ~2013), so an "established" researcher whose early work
    predates the corpus may read as a shorter span -- impact carries the call.
    """
    if first_year is None or last_year is None:
        return "early/unknown"
    span = (last_year - first_year) + 1
    cit = total_citations or 0
    mx = max_citations or 0
    h = h_index_proxy or 0

    # Eminent: clearly high impact regardless of span.
    if h >= 25 or cit >= 5000 or mx >= 1500:
        return "eminent"
    # Established: a real track record with solid impact.
    if span >= 8 and (h >= 8 or cit >= 600):
        return "established"
    # Rising-star: short career so far but already landing (cited or leading).
    if span <= 7 and (cit >= 80 or mx >= 50 or (is_corresponding and cit >= 30)):
        return "rising-star"
    return "early/unknown"


def activity_tier(last_year, is_corresponding) -> str:
    """``active-pi`` / ``active`` / ``dormant`` from recency + PI signal."""
    if last_year is None:
        return "dormant"
    active = (THIS_YEAR - last_year) <= ACTIVE_WINDOW
    if not active:
        return "dormant"
    return "active-pi" if is_corresponding else "active"


def active_recent(last_year) -> bool:
    return last_year is not None and (THIS_YEAR - last_year) <= ACTIVE_WINDOW


def tool_fit(field_slug: str) -> list[str]:
    """The roadmap tool/need slugs that fit a researcher in this field."""
    return FIELD_TOOL_FIT.get(field_slug, FIELD_TOOL_FIT["other"])


def make_segment(field_slug: str, sen: str, act: str) -> str:
    """Compact CRM segment label, e.g. ``physics-astro/rising-star/active-pi``."""
    return f"{field_slug}/{sen}/{act}"


def is_high_value(seniority_label: str, activity: str, is_corresponding: bool) -> bool:
    """The segment we deep-enrich contact for: active corresponding authors /
    PIs and rising stars across the top fields. Mirrors the prototype's funnel
    target rule (active PIs + rising stars), generalized to all fields.
    """
    if activity == "active-pi":
        return True
    if seniority_label in ("rising-star", "established", "eminent") and \
            activity in ("active-pi", "active") and is_corresponding:
        return True
    return False
