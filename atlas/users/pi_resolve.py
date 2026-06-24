"""Conservative grant-PI -> canonical-Person resolver.

THE GAP (found by paper 03)
---------------------------
``grant_person`` edges carry PI **names** sourced from the funder feeds
(NIH/NSF/Wellcome/DFG/Sloan). Those PI Person nodes have **no ORCID** and are a
*different node set* from the canonical people/users layer, which is built from
**OpenAlex work authors** (and *those* carry ORCID + ROR-resolved affiliations +
field/topic profiles). So a grant's PI never joins to the researcher who
actually published -- funding<->researcher analysis is impossible.

This module bridges that gap with a **conservative, tiered** resolver in exactly
the same discipline as the ROR org matcher (:mod:`atlas.ror_bulk`): every match
records a ``match_method`` + ``match_score``, and an ambiguous / common-name /
no-org-overlap case resolves to ``None`` -- **never a wrong link**. Many PIs will
not resolve (they predate ORCID, were never OpenAlex authors, or their grant's
recipient org doesn't ROR-resolve) -- that is expected and reported honestly.

The signals we join on
-----------------------
For each PI we assemble:

- **name**       -- ``person.full_name`` (+ ``first_name`` / ``last_name``), with
                    courtesy titles (Dr/Prof/Mr/Ms/Mrs/...) stripped;
- **org ROR**    -- the ROR id of the grant's *recipient* organization
                    (``grant_org`` role=recipient -> ``organization.ror_id``);
- **field slug** -- the roadmap field slug of the grant, derived from the grant's
                    funded works' OpenAlex topics (``grant_work -> work_field ->
                    topic -> ... -> field`` -> :func:`field_to_slug`), when the
                    grant links to any work (a minority -- absence is fine, it
                    just means no field signal, never a wrong field).

The candidate side is the canonical people layer (the ``researchers`` table /
``Person`` nodes that are OpenAlex authors): ``full_name``, ``orcid``,
``primary_org_ror``, ``field_slug``.

Match tiers (highest confidence first)
--------------------------------------
0. ``orcid``        -- PI already carries an ORCID that a candidate also carries.
                       (Grant feeds almost never supply ORCID today, so this tier
                       is effectively dormant, but it is the correct top tier and
                       is here for forward-compat / future feeds.) score 1.0.
1. ``name+org+field`` -- normalized surname matches a candidate at the **same
                       recipient ROR org**, the given-name agrees (full token or a
                       shared initial), the field slug **agrees**, and there is a
                       **unique** such candidate. score 0.97.
2. ``name+org``     -- same as tier 1 but with **no field signal available** on
                       the PI side (the grant links to no work). Still requires a
                       unique same-org + surname + given-name candidate. score 0.90.

Refusals (-> ``None``, recorded as ``unresolved`` with a reason):
- **no org overlap**     -- PI grant has no ROR-resolved recipient org, OR no
                            candidate shares that ROR. We never match on bare name.
- **ambiguous**          -- more than one candidate clears the bar in the bucket
                            and they cannot be separated (common surname + same
                            org + compatible given name). Better null than wrong.
- **field conflict**     -- a unique same-org+name candidate exists but its field
                            slug *disagrees* with a present PI field signal: we
                            **drop to tier 2 semantics** only if the field signal
                            is absent; a present-and-conflicting field downgrades
                            the match to a refusal (we'd rather miss than wrongly
                            link a same-name colleague in a different discipline).

The result is a small bridge table ``grant_pi_person`` (one row per resolved
``grant_person`` PI edge) carrying ``person_atlas_id`` (the canonical Person),
``orcid`` (when the matched candidate has one), ``match_method`` and
``match_score`` -- the funding<->researcher join the atlas was missing.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field as dc_field

from atlas.users.schema import field_to_slug

# Courtesy / academic titles stripped from the *front* of a PI name before
# parsing. Wellcome (360Giving) prefixes every PI with one of these; NIH/NSF
# rarely do, but stripping is safe (a real surname is never one of these).
_TITLES = {
    "dr", "prof", "professor", "mr", "mrs", "ms", "miss", "mx", "sir", "dame",
    "lord", "lady", "rev", "fr", "doctor", "phd", "md", "dphil",
}

# Generic given-name-position tokens that are not real first names (initials are
# handled separately). Kept tiny and safe.
_GENERIC = {"the", "and"}


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def normalize_person_name(name: str | None) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace.

    Hyphens are turned into spaces so "Abate-Shen" and "Abate Shen" agree, and
    so a hyphenated surname token-matches. Deterministic and stable.
    """
    if not name:
        return ""
    s = _strip_accents(str(name)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


@dataclass(frozen=True)
class NameParts:
    """A parsed person name reduced to comparison-ready parts."""

    surname: str          # normalized last name (single token, hyphens flattened)
    given_tokens: tuple[str, ...]  # normalized given-name tokens (in order)
    initials: tuple[str, ...]      # first letter of each given token

    @property
    def first_initial(self) -> str | None:
        return self.initials[0] if self.initials else None

    @property
    def first_given(self) -> str | None:
        return self.given_tokens[0] if self.given_tokens else None


def parse_name(full_name: str | None,
               first_name: str | None = None,
               last_name: str | None = None) -> NameParts | None:
    """Parse a person into (surname, given tokens, initials).

    Prefers explicit ``last_name`` / ``first_name`` when supplied (the funder
    feeds give them), falling back to parsing ``full_name``. Handles both
    "Last, First" and "First Middle Last" orderings and strips courtesy titles.

    Returns ``None`` when no usable surname can be recovered (e.g. a multi-PI
    blob or an empty name) -- a ``None`` here means "do not attempt to match".
    """
    # explicit last name wins for the surname.
    surname = normalize_person_name(last_name) if last_name else ""
    # a multi-token last_name (e.g. "Abdar Esfahani") keeps its last token as the
    # primary surname key but we remember the full thing for a stricter compare.
    # Strip courtesy titles from the explicit given (Wellcome puts the title in
    # the first_name field, e.g. first_name="Dr") -- a title-only given is empty,
    # which makes us fall back to parsing full_name for the real given name.
    given_toks = [t for t in normalize_person_name(first_name).split()
                  if t not in _TITLES] if first_name else []
    given = " ".join(given_toks)

    if not surname or not given:
        fn = normalize_person_name(full_name)
        if not fn:
            return None
        # reject obvious multi-PI blobs: a comma with a second capitalized name,
        # or more than ~5 tokens with multiple commas in the raw string.
        raw = (full_name or "")
        if raw.count(",") >= 2:
            return None
        # drop leading titles
        toks = [t for t in fn.split() if t not in _TITLES and t not in _GENERIC]
        if not toks:
            return None
        if "," in raw and len(raw.split(",")) == 2:
            # "Last, First Middle" ordering
            last_part, first_part = raw.split(",", 1)
            lt = [t for t in normalize_person_name(last_part).split()
                  if t not in _TITLES]
            ft = [t for t in normalize_person_name(first_part).split()
                  if t not in _TITLES]
            if lt:
                surname = surname or lt[-1]
                given = given or (" ".join(ft) if ft else "")
        if not surname:
            surname = toks[-1]
        if not given and len(toks) > 1:
            given = " ".join(toks[:-1])

    surname = surname.split()[-1] if surname else ""
    given_tokens = tuple(t for t in given.split() if t and t not in _TITLES)
    if not surname:
        return None
    initials = tuple(t[0] for t in given_tokens if t)
    return NameParts(surname=surname, given_tokens=given_tokens, initials=initials)


def _given_compatible(pi: NameParts, cand: NameParts) -> bool:
    """Are two given-name signals compatible (same person plausibly)?

    Compatible iff:
    - both have a first given token and they are equal, OR
    - one's first token equals the other's full first token, OR
    - their first initials match AND at least one side is initial-only
      (i.e. we never claim "Jonathan" == "Jane" -- only "J." vs "Jonathan").
    A side with *no* given signal at all is treated as incompatible (we require
    some given-name evidence; surname+org alone is too weak).
    """
    if not pi.given_tokens or not cand.given_tokens:
        return False
    pg, cg = pi.first_given, cand.first_given
    if pg == cg:
        return True
    # one is an initial form of the other (single-letter token)
    if len(pg) == 1 or len(cg) == 1:
        return pi.first_initial == cand.first_initial
    # both are full but different first names -> not compatible
    return False


def _given_strength(pi: NameParts, cand: NameParts) -> int:
    """How strong the given-name agreement is (for tie-breaking, higher better).

    2 = full first-name token equality where BOTH sides are full tokens (not an
        initial); 1 = initial-only agreement (one side is a single letter).

    Strength 2 requires both sides to be real multi-letter names so an
    initial-only PI ("J. Lee") can never out-rank one full-name candidate over
    another sharing the same initial -- those stay tied at strength 1 and the
    unique-winner check refuses them (the ambiguous common-name trap).
    """
    pg, cg = pi.first_given, cand.first_given
    if pg and cg and len(pg) > 1 and len(cg) > 1 and pg == cg:
        return 2
    return 1


@dataclass
class PiMatch:
    """A resolved PI -> canonical person link (or an explicit refusal)."""

    person_atlas_id: str | None = None
    orcid: str | None = None
    method: str | None = None        # orcid | name+org+field | name+org
    score: float | None = None
    reason: str | None = None        # set when unresolved: why we refused

    @property
    def resolved(self) -> bool:
        return self.person_atlas_id is not None


@dataclass
class Candidate:
    """A canonical-person candidate (an OpenAlex-author Person)."""

    atlas_id: str
    name_parts: NameParts
    orcid: str | None
    ror: str | None
    field_slug: str | None


class PiResolver:
    """Resolve grant PIs to canonical people, conservatively.

    Build the candidate index once from the ``researchers`` / Person rows
    (``add_candidate``), then call :meth:`resolve` per PI. The index is keyed on
    ``(ror, surname)`` -- the conservative join unit -- so we only ever consider
    candidates who share both the recipient org and the surname. An optional
    ORCID index supports the (dormant) top tier.
    """

    def __init__(self) -> None:
        # (ror, surname) -> list[Candidate]
        self.by_org_surname: dict[tuple[str, str], list[Candidate]] = {}
        # orcid -> candidate atlas_id (forward-compat top tier)
        self.by_orcid: dict[str, str] = {}
        self.n_candidates = 0

    # ----- construction --------------------------------------------------- #

    def add_candidate(self, atlas_id: str, full_name: str | None,
                      orcid: str | None, ror: str | None,
                      field_slug: str | None,
                      first_name: str | None = None,
                      last_name: str | None = None) -> None:
        np = parse_name(full_name, first_name, last_name)
        if orcid:
            self.by_orcid.setdefault(orcid, atlas_id)
        if np is None or not ror:
            return  # a candidate with no surname or no ROR can't anchor a match
        cand = Candidate(atlas_id=atlas_id, name_parts=np, orcid=orcid,
                         ror=ror, field_slug=field_slug)
        self.by_org_surname.setdefault((ror, np.surname), []).append(cand)
        self.n_candidates += 1

    # ----- matching ------------------------------------------------------- #

    def resolve(self, *, full_name: str | None, first_name: str | None,
                last_name: str | None, pi_orcid: str | None,
                recipient_ror: str | None,
                field_slug: str | None) -> PiMatch:
        """Resolve one PI. Returns a :class:`PiMatch` (resolved or refused)."""
        # tier 0: ORCID (forward-compat; PI feeds rarely carry it today)
        if pi_orcid and pi_orcid in self.by_orcid:
            return PiMatch(person_atlas_id=self.by_orcid[pi_orcid],
                           orcid=pi_orcid, method="orcid", score=1.0)

        np = parse_name(full_name, first_name, last_name)
        if np is None:
            return PiMatch(reason="unparseable-name")
        if not recipient_ror:
            return PiMatch(reason="no-org-overlap")

        bucket = self.by_org_surname.get((recipient_ror, np.surname))
        if not bucket:
            return PiMatch(reason="no-org-overlap")

        # keep only candidates whose given name is compatible with the PI's
        compatible = [c for c in bucket if _given_compatible(np, c.name_parts)]
        if not compatible:
            return PiMatch(reason="no-name-overlap")

        have_field = bool(field_slug) and field_slug != "other"

        if have_field:
            # tier 1: same org + name + field agreement, unique winner.
            field_ok = [c for c in compatible
                        if c.field_slug and c.field_slug == field_slug]
            winner = _unique_winner(np, field_ok)
            if winner is not None:
                return PiMatch(person_atlas_id=winner.atlas_id,
                               orcid=winner.orcid,
                               method="name+org+field", score=0.97)
            # a present-and-conflicting field: if there's a clean unique
            # same-org+name candidate but its field disagrees, refuse (we will
            # not link a same-name colleague in a different discipline).
            if field_ok:
                return PiMatch(reason="ambiguous-field")
            # field signal present but NO same-field candidate in the bucket ->
            # the publishing researcher in this discipline isn't here. Refuse
            # rather than fall back to a possibly-wrong different-field colleague.
            return PiMatch(reason="field-conflict")

        # tier 2: no PI field signal -- same org + name, unique winner only.
        winner = _unique_winner(np, compatible)
        if winner is not None:
            return PiMatch(person_atlas_id=winner.atlas_id,
                           orcid=winner.orcid,
                           method="name+org", score=0.90)
        return PiMatch(reason="ambiguous")


def _unique_winner(pi: NameParts, cands: list[Candidate]) -> Candidate | None:
    """Pick the single unambiguous candidate, else ``None``.

    If exactly one candidate, take it. If several, prefer the unique candidate
    with the strongest given-name agreement (full-name equality beats
    initial-only); a tie at the top strength among *distinct people* is refused.
    Candidates that are the *same* atlas_id collapse to one (a person can appear
    once per (ror, surname) key already, but be defensive).
    """
    distinct = {c.atlas_id: c for c in cands}
    cands = list(distinct.values())
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    scored = sorted(cands, key=lambda c: _given_strength(pi, c.name_parts),
                    reverse=True)
    top = _given_strength(pi, scored[0].name_parts)
    top_cands = [c for c in scored if _given_strength(pi, c.name_parts) == top]
    if len(top_cands) == 1:
        return top_cands[0]
    return None  # genuinely ambiguous -> refuse
