"""Researcher / user (CRM) schema for research-atlas.

The atlas Person node is a thin identity record (name + ORCID + OpenAlex id +
provenance). This module defines the *enriched, contactable, segmented*
researcher profile built on top of it -- the platform's **users**.

A researcher profile is a Person joined to:

- the fields/topics they actually publish in (from their works' OpenAlex topics),
- an activity/impact summary (works count, citations, recency, an h-index
  *proxy*, a career-stage proxy),
- their primary institution (ROR-resolved),
- and -- on the high-value segment only -- a PUBLIC professional contact
  (corresponding-author email harvested from PubMed/EuropePMC metadata or an
  ORCID public profile), every contact row carrying its own provenance and a
  ``contactable`` / ``opt_out`` flag.

Compliance is structural, not a side note:

- ``email`` is **null unless** it came from a public professional source
  (PubMed/EuropePMC corresponding-author metadata, an ORCID public email, or a
  public lab page). Emails are **never** pattern-fabricated. ``coerce_user``
  rejects any row that claims an email without a matching
  ``email_source`` + ``email_source_url`` + ``email_as_of``.
- ``contactable`` defaults to public-source-only and is false whenever there is
  no public-sourced email.
- ``opt_out`` defaults false but exists so an unsubscribe / removal request can
  be honored without deleting the profile (the profile stays; outreach stops).

The email-bearing dataset is **local/gitignored**. Only the schema, aggregates,
and an **email-free** sample are committed (see ``scripts/build_users_sample.py``
and ``docs/USERS_POLICY.md``).
"""

from __future__ import annotations

import re

USERS_SCHEMA_VERSION = "0.1.0"

# Every column the enriched researcher/user table carries, in canonical order.
USER_COLUMNS = [
    # ---- identity (from the atlas Person node) ------------------------------
    "atlas_id",              # the Person surrogate key (FK to person.parquet)
    "full_name",
    "first_name",
    "last_name",
    "orcid",                 # canonical global person id, when public
    "openalex_author_id",

    # ---- segmentation: field / topic ---------------------------------------
    "primary_domain",        # OpenAlex domain: Life/Health/Physical/Social Sci
    "primary_field",         # OpenAlex field (26): Neuroscience, Physics, ...
    "primary_subfield",      # OpenAlex subfield
    "field_slug",            # normalized roadmap key (see FIELD_SLUGS)
    "top_topics",            # ';'-joined top OpenAlex topics they publish in
    "n_fields",              # how many distinct OpenAlex fields they span

    # ---- institution --------------------------------------------------------
    "primary_org_atlas_id",  # FK to organization.parquet (most-frequent ROR)
    "primary_org_name",
    "primary_org_ror",
    "country_code",

    # ---- activity / impact proxies -----------------------------------------
    "works_count",           # works of theirs in the atlas corpus
    "first_year",            # earliest pub year in corpus
    "last_year",             # latest pub year in corpus (recency)
    "active_recent",         # bool: published in the last ~3 years
    "total_citations",       # sum cited_by_count over their corpus works
    "max_citations",         # most-cited single work (impact proxy)
    "h_index_proxy",         # h-index computed over their *corpus* works only
    "corresponding_count",   # times they were the corresponding author (PI signal)
    "is_corresponding_author",  # bool: ever a corresponding author

    # ---- segments -----------------------------------------------------------
    "seniority",             # rising-star | established | eminent | early/unknown
    "activity_tier",         # active-pi | active | dormant
    "segment",               # compact '<field_slug>/<seniority>/<activity_tier>'
    "tool_fit",              # ';'-joined atlas tool/need slugs that match them

    # ---- PUBLIC contact (high-value segment; null elsewhere) ---------------
    "email",                 # PUBLIC professional email or NULL (never fabricated)
    "email_source",          # 'pubmed' | 'europepmc' | 'orcid' | 'labpage' | None
    "email_source_url",      # citeable URL the email was read from
    "email_as_of",           # ISO-8601 UTC timestamp the email was harvested
    "email_method",          # 'corresponding-author-metadata' | 'orcid-public' | ...
    "contactable",           # bool: have a public-sourced email AND not opted out
    "opt_out",               # bool: honored unsubscribe / removal request

    # ---- CRM state ----------------------------------------------------------
    "engagement_status",     # not-contacted | contacted | replied | meeting | ...

    # ---- provenance of the *profile* row -----------------------------------
    "source",                # always 'atlas-users' (derived dataset)
    "source_url",            # the person's OpenAlex/ORCID URL
    "as_of",                 # ISO-8601 UTC timestamp the profile was built
]

# Columns that, if committed, would leak personal contact data. The public
# sample / aggregates pipeline drops exactly these.
PII_COLUMNS = [
    "email",
    "email_source",
    "email_source_url",
    "email_as_of",
    "email_method",
]

# Allowed public email sources. An email is only ever stored if its source is in
# this set; anything else (including a guessed firstname.lastname@) is rejected.
ALLOWED_EMAIL_SOURCES = {"pubmed", "europepmc", "orcid", "labpage", "crossref"}

# Engagement-status state machine (CRM lifecycle). Default = not-contacted.
ENGAGEMENT_STATES = [
    "not-contacted",
    "queued",
    "contacted",
    "bounced",
    "replied",
    "meeting",
    "converted",
    "declined",
    "opted-out",
]

# Seniority buckets (career-stage proxy, computed from the corpus only).
SENIORITY = ["early/unknown", "rising-star", "established", "eminent"]

# Activity tiers.
ACTIVITY_TIERS = ["active-pi", "active", "dormant"]

# OpenAlex field name -> roadmap field slug. The cross-field tool roadmap
# (docs/USERS_NEEDS.md) is keyed on these slugs. OpenAlex has 26 fields; we
# fold them into the 8 roadmap buckets the brief asks for.
FIELD_SLUGS = {
    # biomed / bio
    "Biochemistry, Genetics and Molecular Biology": "biomed-bio",
    "Immunology and Microbiology": "biomed-bio",
    "Neuroscience": "biomed-bio",
    "Pharmacology, Toxicology and Pharmaceutics": "biomed-bio",
    "Medicine": "biomed-bio",
    "Nursing": "biomed-bio",
    "Dentistry": "biomed-bio",
    "Health Professions": "biomed-bio",
    "Veterinary": "biomed-bio",
    "Agricultural and Biological Sciences": "biomed-bio",
    # chemistry
    "Chemistry": "chemistry",
    "Chemical Engineering": "chemistry",
    # physics / astro
    "Physics and Astronomy": "physics-astro",
    # materials
    "Materials Science": "materials",
    # CS / ML
    "Computer Science": "cs-ml",
    "Decision Sciences": "cs-ml",
    # earth / climate
    "Earth and Planetary Sciences": "earth-climate",
    "Environmental Science": "earth-climate",
    "Energy": "earth-climate",
    # economics / social science
    "Economics, Econometrics and Finance": "econ-social",
    "Business, Management and Accounting": "econ-social",
    "Social Sciences": "econ-social",
    "Psychology": "econ-social",
    "Arts and Humanities": "econ-social",
    # math
    "Mathematics": "math",
    # engineering (folded toward materials/physics depending; default eng)
    "Engineering": "engineering",
}

ROADMAP_FIELDS = [
    "biomed-bio", "chemistry", "physics-astro", "materials",
    "cs-ml", "earth-climate", "econ-social", "math", "engineering",
]

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def field_to_slug(field_name: str | None) -> str:
    """Map an OpenAlex field name to a roadmap slug (default ``'other'``)."""
    if not field_name:
        return "other"
    return FIELD_SLUGS.get(field_name.strip(), "other")


def is_plausible_email(value: str | None) -> bool:
    """True if ``value`` is a syntactically valid single email address."""
    return bool(value) and bool(_EMAIL_RE.match(value.strip()))


def coerce_user(data: dict) -> dict:
    """Validate a researcher/user row and fill missing columns with sensible
    defaults.

    Compliance invariants enforced here (the heart of the policy):

    1. **No fabricated emails.** If ``email`` is set it must (a) be syntactically
       valid and (b) carry a known-public ``email_source`` + an
       ``email_source_url`` + an ``email_as_of``. A row that claims an email
       without provenance, or with a non-public source, is rejected.
    2. **contactable implies a public email and no opt-out.** ``contactable`` is
       coerced to false whenever there is no public-sourced email or
       ``opt_out`` is true.
    3. **opt_out is respected.** If ``opt_out`` is true, ``contactable`` is forced
       false and ``engagement_status`` is set to ``opted-out``.

    Raises ``ValueError`` on a fabricated / unprovenanced email or an unknown
    column.
    """
    extra = set(data) - set(USER_COLUMNS)
    if extra:
        raise ValueError(f"users: unknown columns {sorted(extra)}")

    row = {c: data.get(c) for c in USER_COLUMNS}

    email = row.get("email")
    if email is not None:
        email = str(email).strip() or None
        row["email"] = email
    if email is not None:
        if not is_plausible_email(email):
            raise ValueError(f"users: invalid email syntax {email!r}")
        src = row.get("email_source")
        if src not in ALLOWED_EMAIL_SOURCES:
            raise ValueError(
                f"users: email present but email_source {src!r} is not a known "
                f"public source {sorted(ALLOWED_EMAIL_SOURCES)} -- refusing a "
                f"possibly-fabricated address"
            )
        if not row.get("email_source_url"):
            raise ValueError("users: email present without email_source_url provenance")
        if not row.get("email_as_of"):
            raise ValueError("users: email present without email_as_of provenance")
    else:
        # No email -> no email provenance and not contactable.
        row["email_source"] = None
        row["email_source_url"] = None
        row["email_as_of"] = None
        row["email_method"] = None

    # opt_out / contactable invariants
    row["opt_out"] = bool(row.get("opt_out"))
    has_public_email = email is not None
    if row["opt_out"]:
        row["contactable"] = False
        row["engagement_status"] = "opted-out"
    else:
        row["contactable"] = bool(has_public_email and row.get("contactable", has_public_email))

    if not row.get("engagement_status"):
        row["engagement_status"] = "not-contacted"
    if row["engagement_status"] not in ENGAGEMENT_STATES:
        raise ValueError(f"users: unknown engagement_status {row['engagement_status']!r}")

    return row
