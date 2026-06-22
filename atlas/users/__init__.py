"""Researcher / users (CRM) layer on top of the research-atlas graph.

Generalizes the single-field biophysics-PhD funnel prototype
(``advisors_to_email.csv`` / ``funnel_targets.csv`` from
``biophysics-phd-review``) into an all-field, atlas-grounded layer of rich,
contactable, segmented researcher profiles -- the platform's users.

Modules:
- :mod:`atlas.users.schema`   -- the enriched user/CRM schema + compliance coerce
- :mod:`atlas.users.profiles` -- build profiles from the atlas (full enrichment)
- :mod:`atlas.users.contacts` -- harvest PUBLIC professional contacts (high-value)
- :mod:`atlas.users.segment`  -- seniority / activity / tool-fit segmentation
"""

from atlas.users.schema import (  # noqa: F401
    USERS_SCHEMA_VERSION,
    USER_COLUMNS,
    PII_COLUMNS,
    ALLOWED_EMAIL_SOURCES,
    ENGAGEMENT_STATES,
    ROADMAP_FIELDS,
    coerce_user,
    field_to_slug,
    is_plausible_email,
)
