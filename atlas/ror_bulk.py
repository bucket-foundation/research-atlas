"""Offline, at-scale organization -> ROR resolution from the ROR bulk dump.

The per-name :class:`~atlas.ror.RorResolver` hits the public ROR API once per
name -- fine for a handful of orgs, fatal for 99k. This module is the scale path:
download the ROR **bulk data dump** once (ror.org publishes it on Zenodo,
https://zenodo.org/communities/ror-data), build an in-memory normalized
name/alias/acronym + country index, and match every org name locally.

Matching is **conservative and tiered** -- we record a ``match_method`` and a
``match_score`` for every match, and we *never guess wrong*: a name that does not
clear the tier's bar resolves to ``None`` (better null than a wrong ROR id).

Tiers (highest confidence first), all gated on country agreement when both sides
carry a country:

1. ``exact``      -- normalized name == a normalized ROR name of any type
                     (display/label/alias/acronym). score 1.0.
2. ``expanded``   -- abbreviation-expanded normalized name matches (UNIV ->
                     UNIVERSITY, INST -> INSTITUTE, ...). Real funder feeds are
                     full of abbreviations ROR spells out. score 0.99.
3. ``acronym``    -- normalized name == a normalized ROR *acronym*, country must
                     agree (acronyms are ambiguous across countries). score 0.97.
4. ``fuzzy``      -- token-set match: the org's significant tokens are a subset of
                     a unique ROR candidate's tokens (or vice-versa) within the
                     same country, and the containment score clears ``min_fuzzy``.
                     A clear single winner is required. score = containment score.

Tie-breaking everywhere prefers the most-canonical ROR name type
(``ror_display`` > ``label`` > ``alias`` > ``acronym``) and, for fuzzy, the
candidate whose token set is closest in size to the query (highest Jaccard),
which makes "Purdue University" prefer the main campus over a niche sub-unit.

The index is built once and reused; building it for the ~127k-record v2 dump
takes a few seconds and a few hundred MB of RAM, then matching is O(1) per name
for the exact/expanded/acronym tiers.
"""

from __future__ import annotations

import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# ROR v2 name-type tags and a canonical-ness priority (higher = more canonical).
_DISPLAY_TYPES = {"ror_display", "label", "alias"}
_ACRONYM_TYPE = "acronym"
_TYPE_PRIORITY = {"ror_display": 3, "label": 2, "alias": 1, "acronym": 0}


def _name_priority(types: set[str]) -> int:
    """Highest canonical-ness across a name's type tags (0 if none known)."""
    return max((_TYPE_PRIORITY.get(t, 0) for t in types), default=0)


# Trailing tokens that mark an *umbrella* org (a system / consortium that funders
# rarely name directly -- they name the operating campus). Used only as a
# fuzzy tie-break to prefer the campus over its parent system, never to reject.
_UMBRELLA_TAILS = {"system", "consortium", "network"}


def _is_umbrella(name: str) -> bool:
    n = normalize_name(name)
    toks = n.split()
    return bool(toks) and toks[-1] in _UMBRELLA_TAILS


# Legal-form / generic tokens stripped before fuzzy comparison. Kept small and
# language-spanning so we do not over-strip (over-stripping causes false merges).
_STOP_TOKENS = {
    "the", "of", "and", "for", "de", "di", "der", "des", "du", "la", "le", "el",
    "y", "e", "den", "das", "und", "fur", "von", "van",
    "ltd", "limited", "inc", "incorporated", "llc", "plc", "gmbh", "ag", "sa",
    "spa", "srl", "bv", "nv", "co", "corp", "corporation", "company", "group",
}

# Abbreviation expansions seen heavily in NIH/NSF/UKRI org feeds. Applied
# token-wise to BOTH the query and (at index-build time) to ROR names so an
# abbreviated funder name collides with ROR's spelled-out display name.
# Conservative: only unambiguous, well-known academic abbreviations.
_ABBREV = {
    "univ": "university",
    "u": "university",          # only expanded in multi-token context (see below)
    "inst": "institute",
    "insts": "institutes",
    "tech": "technology",
    "technol": "technology",
    "ctr": "center",
    "cntr": "center",
    "hlth": "health",
    "sci": "science",
    "scis": "sciences",
    "natl": "national",
    "nat": "national",
    "med": "medical",
    "med.": "medical",
    "coll": "college",
    "hosp": "hospital",
    "dept": "department",
    "res": "research",
    "lab": "laboratory",
    "labs": "laboratories",
    "intl": "international",
    "int": "international",
    "mgmt": "management",
    "engr": "engineering",
    "eng": "engineering",
    "agric": "agricultural",
    "agri": "agricultural",
    "mech": "mechanical",
    "polytech": "polytechnic",
    "st": "saint",              # ambiguous (state/saint); handled conservatively below
    "soc": "society",
    "assn": "association",
    "found": "foundation",
    "fdn": "foundation",
    "ctrs": "centers",
    "div": "division",
    "ck": None,                 # placeholder noop
}
# Tokens that should NOT be expanded when standing for "state" not "saint".
# We only expand "st" -> "saint" when the following token is capitalised proper
# noun-ish; in practice the bare exact tier handles "state" forms, and the
# expanded tier is a best-effort booster, so we keep "st" -> "saint" off by
# default to avoid "Penn St" -> "Penn Saint". Remove from _ABBREV map use.
_ABBREV.pop("st", None)
_ABBREV.pop("ck", None)
_ABBREV.pop("u", None)  # single "u" is too ambiguous to expand standalone

# Two-letter ISO country aliases seen in our data that differ from ROR/geonames.
# CORDIS uses EU-internal codes: EL=Greece, UK=United Kingdom.
_COUNTRY_ALIASES = {
    "EL": "GR",
    "UK": "GB",
}


def normalize_name(name: str) -> str:
    """Aggressively normalize an org name for *exact* keying.

    Lowercase, strip accents, drop punctuation, collapse whitespace. This is the
    key used for the exact/acronym tiers, so it must be deterministic and stable.
    """
    if not name:
        return ""
    # NFKD strip accents
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    # ampersand -> 'and' so "A&M" and "A and M" collide
    s = s.replace("&", " and ")
    # drop anything not alphanumeric or space
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def expand_abbrev(norm: str) -> str:
    """Expand common academic abbreviations token-wise in a normalized name.

    Deterministic and idempotent (expanding twice == expanding once, because the
    expansions map to full words that are not themselves abbreviation keys).
    Only expands multi-token names so a bare acronym is left to the acronym tier.
    """
    if not norm:
        return ""
    toks = norm.split()
    if len(toks) < 2:
        return norm
    out = [_ABBREV.get(t, t) for t in toks]
    out = [t for t in out if t]  # drop any None
    return " ".join(out)


def _tokens(norm: str) -> frozenset[str]:
    """Significant tokens of a normalized name (stopwords + 1-char dropped)."""
    return frozenset(
        t for t in norm.split()
        if len(t) > 1 and t not in _STOP_TOKENS
    )


def _canon_country(cc: str | None) -> str | None:
    if not cc:
        return None
    cc = cc.strip().upper()
    return _COUNTRY_ALIASES.get(cc, cc) or None


@dataclass
class RorRecord:
    """The slice of a ROR record the atlas keeps on a resolved org node."""

    ror_id: str
    name: str          # ROR display name
    country_code: str | None
    city: str | None
    lat: float | None
    lon: float | None
    homepage: str | None
    types: tuple[str, ...]


@dataclass
class Match:
    ror: RorRecord
    method: str        # exact | expanded | acronym | fuzzy
    score: float


def _display_name(rec: dict) -> str:
    for n in rec.get("names", []):
        if "ror_display" in (n.get("types") or []):
            return n.get("value") or ""
    for n in rec.get("names", []):
        if "label" in (n.get("types") or []):
            return n.get("value") or ""
    names = rec.get("names") or [{}]
    return names[0].get("value") or ""


def _location(rec: dict) -> tuple[str | None, str | None, float | None, float | None]:
    for loc in rec.get("locations", []):
        g = loc.get("geonames_details") or {}
        return (
            _canon_country(g.get("country_code")),
            g.get("name"),
            g.get("lat"),
            g.get("lng"),
        )
    return (None, None, None, None)


def _homepage(rec: dict) -> str | None:
    for link in rec.get("links", []):
        if isinstance(link, dict) and link.get("type") == "website":
            return link.get("value")
    return None


def _shape(rec: dict) -> RorRecord:
    cc, city, lat, lon = _location(rec)
    return RorRecord(
        ror_id=rec.get("id", ""),
        name=_display_name(rec),
        country_code=cc,
        city=city,
        lat=lat,
        lon=lon,
        homepage=_homepage(rec),
        types=tuple(rec.get("types") or []),
    )


class RorIndex:
    """In-memory normalized ROR index built from the bulk dump.

    Indexes (all keyed on :func:`normalize_name`):
    - ``exact``    : norm name -> dict[ror_id -> best name priority] (any type)
    - ``expanded`` : abbreviation-expanded norm name -> dict[ror_id -> priority]
    - ``acronym``  : norm acronym -> set of ror_ids
    - per-country token index for the fuzzy tier:
      ``by_country[cc]`` -> list of ``(token_frozenset, ror_id, name_priority)``
    - ``fundref``  : crossref fundref id -> ror_id (funder cross-walk)
    """

    def __init__(self):
        self.records: dict[str, RorRecord] = {}
        # norm name -> {ror_id: best name-type priority for that name on that org}
        self.exact: dict[str, dict[str, int]] = {}
        self.expanded: dict[str, dict[str, int]] = {}
        self.acronym: dict[str, set[str]] = {}
        # country -> list of (tokens, ror_id, name_priority, is_umbrella) for fuzzy
        self.by_country: dict[str, list[tuple[frozenset[str], str, int, bool]]] = {}
        # country -> {token: document frequency} for IDF weighting in fuzzy
        self.df_by_country: dict[str, dict[str, int]] = {}
        # country -> {token: [indices into by_country[cc]]} inverted index for
        # fast fuzzy candidate retrieval (built lazily on first fuzzy call)
        self._postings: dict[str, dict[str, list[int]]] = {}
        # fundref id -> ror_id (for funder cross-walk)
        self.fundref: dict[str, str] = {}

    # ----- construction --------------------------------------------------- #

    @classmethod
    def from_records(cls, records: Iterator[dict]) -> "RorIndex":
        idx = cls()
        for rec in records:
            idx._add(rec)
        return idx

    @classmethod
    def from_dump(cls, path: str | Path) -> "RorIndex":
        """Build the index from a ROR dump (a ``.json`` file or the ``.zip``)."""
        path = Path(path)
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                jsons = [n for n in zf.namelist() if n.endswith(".json")]
                if not jsons:
                    raise ValueError(f"no .json in ROR zip {path}")
                with zf.open(jsons[0]) as fh:
                    records = json.load(fh)
        else:
            records = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_records(iter(records))

    @staticmethod
    def _bump(d: dict[str, dict[str, int]], norm: str, rid: str, prio: int) -> None:
        slot = d.setdefault(norm, {})
        if prio > slot.get(rid, -1):
            slot[rid] = prio

    def _add(self, rec: dict) -> None:
        if rec.get("status") and rec["status"] != "active":
            # only index active orgs; withdrawn/inactive ids should not be assigned
            return
        shaped = _shape(rec)
        rid = shaped.ror_id
        if not rid:
            return
        self.records[rid] = shaped
        cc = shaped.country_code

        for n in rec.get("names", []):
            val = n.get("value")
            if not val:
                continue
            types = set(n.get("types") or [])
            prio = _name_priority(types)
            norm = normalize_name(val)
            if not norm:
                continue
            if types & _DISPLAY_TYPES or not types:
                self._bump(self.exact, norm, rid, prio)
                exp = expand_abbrev(norm)
                if exp != norm:
                    self._bump(self.expanded, exp, rid, prio)
                # also index the expanded form into expanded under itself so an
                # already-spelled-out ROR name is reachable from an abbreviated query
                self._bump(self.expanded, norm, rid, prio)
                toks = _tokens(norm)
                if toks and cc:
                    umbrella = norm.split()[-1] in _UMBRELLA_TAILS
                    self.by_country.setdefault(cc, []).append(
                        (toks, rid, prio, umbrella))
                    df = self.df_by_country.setdefault(cc, {})
                    for tk in toks:
                        df[tk] = df.get(tk, 0) + 1
            if _ACRONYM_TYPE in types:
                self.acronym.setdefault(norm, set()).add(rid)

        for ext in rec.get("external_ids", []):
            if ext.get("type") == "fundref":
                for fid in ext.get("all", []) or []:
                    self.fundref.setdefault(str(fid), rid)
                pref = ext.get("preferred")
                if pref:
                    self.fundref.setdefault(str(pref), rid)

    # ----- matching ------------------------------------------------------- #

    def match(self, name: str, country_code: str | None = None,
              min_fuzzy: float = 0.85) -> Match | None:
        """Resolve a single org name to a ROR record, conservatively.

        Returns a :class:`Match` (``ror``, ``method``, ``score``) or ``None``.
        """
        norm = normalize_name(name)
        if not norm:
            return None
        cc = _canon_country(country_code)

        # tier 1: exact
        hit = self._pick(self.exact.get(norm), cc)
        if hit:
            return Match(hit, "exact", 1.0)

        # tier 2: abbreviation-expanded exact
        exp = expand_abbrev(norm)
        hit = self._pick(self.expanded.get(exp), cc)
        if hit:
            return Match(hit, "expanded", 0.99)

        # tier 3: acronym (country must agree -- acronyms collide globally)
        ac = self.acronym.get(norm)
        if ac:
            hit = self._pick({r: 0 for r in ac}, cc, require_country=True)
            if hit:
                return Match(hit, "acronym", 0.97)

        # tier 4: fuzzy token-containment within country
        if cc and cc in self.by_country:
            best = self._fuzzy(norm, cc, min_fuzzy)
            if best is not None:
                rid, score = best
                return Match(self.records[rid], "fuzzy", round(score, 4))

        return None

    def _pick(self, cands: dict[str, int] | None, cc: str | None,
              require_country: bool = False) -> RorRecord | None:
        """Choose a single ROR record from candidates, gated on country.

        ``cands`` maps ror_id -> the name-type priority by which it matched.

        - no candidates -> None
        - filter to country-agreeing candidates when a country is supplied;
        - among the survivors, accept the unique highest-priority candidate
          (``ror_display`` beats ``alias`` etc.). A tie on top priority among
          *different* orgs is refused (genuinely ambiguous).
        """
        if not cands:
            return None
        # an acronym (require_country) with no country to gate on is unsafe --
        # acronyms collide globally, so refuse rather than guess.
        if require_country and not cc:
            return None
        items = [(rid, p) for rid, p in cands.items() if rid in self.records]
        if not items:
            return None

        if cc:
            same = [(rid, p) for rid, p in items if self.records[rid].country_code == cc]
            if same:
                items = same
            elif require_country:
                return None
            # else: no country agreement; fall through to priority resolution on
            # the full candidate set only if it is unambiguous.

        if len(items) == 1:
            return self.records[items[0][0]]

        # multiple candidates: prefer the unique highest name-type priority.
        items.sort(key=lambda kv: kv[1], reverse=True)
        top_prio = items[0][1]
        top = [rid for rid, p in items if p == top_prio]
        if len(top) == 1:
            return self.records[top[0]]
        return None  # ambiguous at the top priority -> refuse

    def _idf(self, cc: str, token: str) -> float:
        """Inverse document frequency of a token within a country.

        Common structural words ("university", "institute") get a low weight;
        a distinctive proper noun ("karolinska", "rensselaer") gets a high one.
        Computed from the per-country document-frequency table built at index
        time. A token unseen in this country gets the max weight (very rare).
        """
        import math
        df = self.df_by_country.get(cc, {})
        n = max(len(self.by_country.get(cc, [])), 1)
        d = df.get(token, 0)
        return math.log((n + 1) / (d + 1)) + 1.0

    def _postings_for(self, cc: str) -> dict[str, list[int]]:
        """Lazily build (and cache) the per-country token -> candidate-index map."""
        post = self._postings.get(cc)
        if post is None:
            post = {}
            for i, (toks, _rid, _prio, _umb) in enumerate(self.by_country.get(cc, [])):
                for tk in toks:
                    post.setdefault(tk, []).append(i)
            self._postings[cc] = post
        return post

    def _fuzzy(self, norm: str, cc: str, min_fuzzy: float):
        """IDF-weighted token-containment fuzzy match within a country.

        Plain token-overlap is dangerous: "Univ of North Carolina Chapel Hill"
        overlaps "Hill's Pet Nutrition" on the common-ish token "hill" and would
        match wrong. We instead score by **IDF-weighted containment** -- the
        fraction of the query's *distinctive* token weight that the candidate
        covers -- and additionally require that the query's single most
        distinctive token is covered. This makes a match hinge on the rare,
        identifying words, not the filler.

        Acceptance requires all of:
        - weighted containment >= ``min_fuzzy``;
        - the highest-IDF query token is in the overlap;
        - a *strict* unique winner on (w_containment, not_umbrella) -- ties among
          sibling campuses ("Purdue University" -> West Lafayette vs Northwest)
          are refused (better null than a wrong campus).

        Tie-breaks below the acceptance bar: prefer non-umbrella, then canonical
        name type, then highest Jaccard (closest token-set size).
        """
        toks = _tokens(norm)
        if len(toks) < 2:
            return None  # too little signal to fuzzy-match safely
        weights = {t: self._idf(cc, t) for t in toks}
        total_w = sum(weights.values())
        if total_w <= 0:
            return None
        # the query's most-distinctive token must be covered by any accepted match
        key_token = max(weights, key=weights.get)

        # Only candidates sharing the key (most-distinctive) token can clear the
        # acceptance bar (which requires that token in the overlap), so restrict
        # the scan to that token's postings -- O(postings) instead of O(country).
        post = self._postings_for(cc)
        cand_idx = post.get(key_token)
        if not cand_idx:
            return None
        country_list = self.by_country[cc]

        best_per_rid: dict[str, tuple[float, int, int, float]] = {}
        covers_key: dict[str, bool] = {}
        for i in cand_idx:
            cand_toks, rid, prio, umbrella = country_list[i]
            inter = toks & cand_toks
            if not inter:
                continue
            w_cont = sum(weights[t] for t in inter) / total_w
            jaccard = len(inter) / len(toks | cand_toks)
            key = (round(w_cont, 6), 0 if umbrella else 1, prio, jaccard)
            prev = best_per_rid.get(rid)
            if prev is None or key > prev:
                best_per_rid[rid] = key
                covers_key[rid] = key_token in inter
        if not best_per_rid:
            return None
        ranked = sorted(best_per_rid.items(), key=lambda kv: kv[1], reverse=True)
        best_rid, best_key = ranked[0]
        best_w = best_key[0]
        if best_w < min_fuzzy:
            return None
        if not covers_key.get(best_rid):
            return None
        # refuse sibling ambiguity: tie on (w_containment, not_umbrella)
        if len(ranked) > 1 and ranked[1][1][:2] == best_key[:2]:
            return None
        return best_rid, best_w

    def __len__(self) -> int:
        return len(self.records)
