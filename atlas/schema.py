"""Canonical schema for research-atlas.

A normalized graph of the global research economy. Six entity types and a set
of edge (relationship) tables. Every row carries provenance and an ``as_of``
timestamp. Money is stored in its original currency *and* a normalized USD
column; unknown amounts are ``None`` (never silently 0).

The schema is the single source of truth that every connector conforms to.
Connectors produce ``dict`` rows whose keys are a subset of the column lists
below; :func:`coerce` validates and fills missing canonical columns with
``None`` so that emitted parquet has a stable, predictable column set.

Surrogate keys
--------------
Each entity has a stable ``atlas_id`` surrogate key derived deterministically
from ``(source, source_id)`` (or a resolved global id like a ROR/ORCID/DOI when
available) via :func:`make_id`. This means re-ingesting the same source record
always produces the same ``atlas_id`` -- the basis for idempotent merges.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

SCHEMA_VERSION = "0.1.0"


# --------------------------------------------------------------------------- #
# Provenance: every row carries these. Stored inline on each entity/edge row.
# --------------------------------------------------------------------------- #
PROVENANCE_COLUMNS = [
    "source",      # short source key, e.g. "nsf", "openalex", "nih", "cordis"
    "source_id",   # the id of the record in that source's own namespace
    "source_url",  # canonical URL to the record at the source (citeable)
    "as_of",       # ISO-8601 UTC timestamp the record was fetched/normalized
]


# --------------------------------------------------------------------------- #
# Entity column definitions. The first column of each is the surrogate key.
# --------------------------------------------------------------------------- #

FUNDER_COLUMNS = [
    "atlas_id",            # surrogate key
    "name",                # display name, e.g. "National Science Foundation"
    "short_name",          # acronym, e.g. "NSF"
    "country_code",        # ISO-3166 alpha-2 of the funder, where known
    "funder_type",         # government | private | nonprofit | corporate | supranational
    "ror_id",              # ROR id of the funder org, if resolvable
    "crossref_funder_id",  # Crossref Funder Registry DOI fragment, if known
    "homepage",
    *PROVENANCE_COLUMNS,
]

GRANT_COLUMNS = [
    "atlas_id",            # surrogate key
    "title",
    "abstract",
    "amount_original",     # numeric amount in original currency, or None
    "currency",            # ISO-4217 code of amount_original, e.g. "USD", "EUR"
    "amount_usd",          # normalized USD amount, or None when unknown
    "fx_rate_to_usd",      # multiplier used for normalization (1.0 for USD)
    "fx_as_of",            # date the fx rate is valid for (ISO date)
    "start_date",          # ISO date or None
    "end_date",            # ISO date or None
    "status",              # active | completed | terminated | unknown
    "program",             # funding program / mechanism name at the source
    *PROVENANCE_COLUMNS,
]

ORGANIZATION_COLUMNS = [
    "atlas_id",            # surrogate key (keyed on ROR id where resolvable)
    "name",
    "ror_id",              # ROR id, the canonical global org identifier
    "country_code",        # ISO-3166 alpha-2
    "city",
    "region",              # state / province
    "org_type",            # education | government | company | nonprofit | facility | other
    "homepage",
    "lat",
    "lon",
    *PROVENANCE_COLUMNS,
]

PERSON_COLUMNS = [
    "atlas_id",            # surrogate key (keyed on ORCID where resolvable)
    "full_name",
    "first_name",
    "last_name",
    "orcid",               # ORCID id, the canonical global person identifier
    "openalex_author_id",  # OpenAlex author id, if resolved
    *PROVENANCE_COLUMNS,
]

WORK_COLUMNS = [
    "atlas_id",            # surrogate key (keyed on OpenAlex id / DOI)
    "title",
    "doi",
    "openalex_id",         # OpenAlex Work id
    "publication_year",
    "publication_date",    # ISO date or None
    "type",                # article | preprint | book-chapter | dataset | ...
    "cited_by_count",
    "is_oa",               # open-access flag, bool or None
    *PROVENANCE_COLUMNS,
]

FIELD_COLUMNS = [
    "atlas_id",            # surrogate key (keyed on OpenAlex topic/field id)
    "name",
    "openalex_id",         # OpenAlex topic/subfield/field/domain id
    "level",               # topic | subfield | field | domain
    "parent_atlas_id",     # parent in the OpenAlex topic taxonomy, or None
    *PROVENANCE_COLUMNS,
]


# --------------------------------------------------------------------------- #
# Edge (relationship) column definitions. Edges are directed; each carries its
# own provenance so a single edge can be attested by multiple sources.
# --------------------------------------------------------------------------- #

EDGE_COLUMNS = {
    # funder -> grant : the funder awarded the grant
    "funder_grant": ["src_id", "dst_id", "role", *PROVENANCE_COLUMNS],
    # grant -> org : the recipient / host organization of the grant
    "grant_org": ["src_id", "dst_id", "role", *PROVENANCE_COLUMNS],
    # grant -> person : PI / co-PI / program officer
    "grant_person": ["src_id", "dst_id", "role", *PROVENANCE_COLUMNS],
    # grant <-> work : a work acknowledges funding from the grant
    "grant_work": ["src_id", "dst_id", "role", *PROVENANCE_COLUMNS],
    # person -> org : affiliation
    "person_org": ["src_id", "dst_id", "role", *PROVENANCE_COLUMNS],
    # work -> field : the work belongs to a field / topic
    "work_field": ["src_id", "dst_id", "score", *PROVENANCE_COLUMNS],
}


# All entities, keyed by their canonical table name.
ENTITY_COLUMNS = {
    "funder": FUNDER_COLUMNS,
    "grant": GRANT_COLUMNS,
    "organization": ORGANIZATION_COLUMNS,
    "person": PERSON_COLUMNS,
    "work": WORK_COLUMNS,
    "field": FIELD_COLUMNS,
}

# Roles that are meaningful on edges (free-form is allowed, these are the canon).
ROLES = {
    "funder_grant": ["awarder"],
    "grant_org": ["recipient", "host", "subaward"],
    "grant_person": ["pi", "co-pi", "program-officer"],
    "grant_work": ["acknowledges"],
    "person_org": ["affiliation"],
}


@dataclass
class Row:
    """A single normalized row plus the table it belongs to.

    Connectors emit ``Row`` objects; the connector base class groups them by
    ``table`` and writes one parquet per table.
    """

    table: str
    data: dict = field(default_factory=dict)


def now_iso() -> str:
    """UTC ``as_of`` timestamp, second precision, ISO-8601 with ``Z``."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def make_id(entity: str, *parts: str) -> str:
    """Deterministic surrogate key for an entity.

    Derived from the entity type plus the most-stable identifying parts a
    connector can supply (prefer a resolved global id -- ROR/ORCID/DOI/OpenAlex
    -- falling back to ``source`` + ``source_id``). Same inputs -> same id, which
    is what makes :meth:`Connector.emit` idempotent.

    Returns a short, URL-safe id like ``org:9b1c0f3a2d4e5f60``.
    """
    norm = [str(p).strip().lower() for p in parts if p is not None and str(p).strip()]
    if not norm:
        raise ValueError(f"make_id({entity!r}) needs at least one non-empty part")
    digest = hashlib.sha1("|".join([entity, *norm]).encode("utf-8")).hexdigest()[:16]
    prefix = {
        "funder": "fund",
        "grant": "grant",
        "organization": "org",
        "person": "person",
        "work": "work",
        "field": "field",
    }.get(entity, entity[:4])
    return f"{prefix}:{digest}"


def coerce(table: str, data: dict) -> dict:
    """Validate a row against the canonical column set and fill gaps with None.

    - Raises ``KeyError`` for the table being unknown.
    - Raises ``ValueError`` for keys not in the canonical schema (typo guard).
    - Ensures every canonical column is present (missing -> ``None``).
    - Enforces the money invariant: a row may not carry ``amount_usd == 0`` --
      unknown money must be ``None``.
    """
    if table in ENTITY_COLUMNS:
        cols = ENTITY_COLUMNS[table]
    elif table in EDGE_COLUMNS:
        cols = EDGE_COLUMNS[table]
    else:
        raise KeyError(f"unknown table {table!r}")

    colset = set(cols)
    extra = set(data) - colset
    if extra:
        raise ValueError(f"{table}: unknown columns {sorted(extra)}")

    if table == "grant":
        if data.get("amount_usd") == 0:
            raise ValueError(
                "grant.amount_usd == 0 is not allowed; unknown money must be None"
            )

    return {c: data.get(c) for c in cols}


def all_tables() -> list[str]:
    """Every emittable table name (entities + edges)."""
    return list(ENTITY_COLUMNS) + list(EDGE_COLUMNS)
