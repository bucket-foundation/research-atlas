from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import duckdb

from atlas.users.pi_resolve import parse_name

_OPENALEX_AUTHOR = re.compile(r"(A\d+)\s*$")
_ROR = re.compile(r"([0-9a-z]{9})/?\s*$")


def openalex_author_id(value: str | None) -> str | None:
    if not value:
        return None
    m = _OPENALEX_AUTHOR.search(str(value).strip())
    return m.group(1) if m else None


def ror_id(value: str | None) -> str | None:
    if not value:
        return None
    m = _ROR.search(str(value).strip().lower())
    return m.group(1) if m else None


@dataclass(frozen=True)
class Advisor:
    key: str
    name: str
    openalex_id: str | None
    ror: str | None
    country_code: str | None = None
    tier: str | None = None


@dataclass
class AdvisorMatch:
    advisor: Advisor
    t1: tuple[str, ...] = ()
    t1_orcid: bool = False
    t2: tuple[str, ...] = ()
    t2_sources: tuple[str, ...] = ()

    @property
    def t1_unique(self) -> str | None:
        return self.t1[0] if len(self.t1) == 1 else None

    @property
    def t2_unique(self) -> str | None:
        return self.t2[0] if len(self.t2) == 1 else None

    @property
    def tier(self) -> str:
        if self.t1_unique:
            return "t1"
        if self.t2_unique:
            return "t2"
        if self.t1 or self.t2:
            return "ambiguous"
        return "none"


@dataclass
class MatchReport:
    total: int
    counts: dict[str, int] = field(default_factory=dict)
    rates: dict[str, float] = field(default_factory=dict)
    t1_rate_by_tier: dict[str, float] = field(default_factory=dict)
    t2_by_source: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "counts": self.counts,
            "rates": self.rates,
            "t1_rate_by_tier": self.t1_rate_by_tier,
            "t2_by_source": self.t2_by_source,
        }


def _t1(con: duckdb.DuckDBPyConnection, ids: list[str]) -> dict[str, list[tuple[str, bool]]]:
    if not ids:
        return {}
    rows = con.execute(
        "SELECT openalex_author_id, atlas_id, orcid IS NOT NULL "
        "FROM person WHERE openalex_author_id IN (SELECT UNNEST(?)) ORDER BY atlas_id",
        [ids],
    ).fetchall()
    out: dict[str, list[tuple[str, bool]]] = {}
    for oa, pid, has_orcid in rows:
        out.setdefault(oa, []).append((pid, bool(has_orcid)))
    return out


def _t2_candidates(con: duckdb.DuckDBPyConnection, surnames: list[str], rors: list[str]):
    if not surnames or not rors:
        return []
    return con.execute(
        """
        SELECT DISTINCT p.atlas_id, p.full_name, p.first_name, p.last_name, p.source,
               regexp_extract(lower(o.ror_id), '([0-9a-z]{9})/?$', 1) AS ror
        FROM person p
        JOIN person_org po ON po.src_id = p.atlas_id
        JOIN organization o ON o.atlas_id = po.dst_id
        WHERE o.ror_id IS NOT NULL
          AND regexp_extract(lower(o.ror_id), '([0-9a-z]{9})/?$', 1) IN (SELECT UNNEST(?))
          AND list_last(string_split(trim(regexp_replace(
                lower(strip_accents(coalesce(p.last_name, ''))), '[^a-z0-9]+', ' ', 'g')), ' '))
              IN (SELECT UNNEST(?))
        """,
        [rors, surnames],
    ).fetchall()


def _key(name: str | None, first: str | None = None, last: str | None = None):
    parts = parse_name(name, first, last)
    if parts is None or not parts.surname or not parts.first_initial:
        return None
    return parts.surname, parts.first_initial


def match_advisors(con: duckdb.DuckDBPyConnection, advisors: list[Advisor]) -> list[AdvisorMatch]:
    t1 = _t1(con, sorted({a.openalex_id for a in advisors if a.openalex_id}))
    keys = {a.key: _key(a.name) for a in advisors}
    surnames = sorted({k[0] for k in keys.values() if k})
    rors = sorted({a.ror for a in advisors if a.ror})
    index: dict[tuple[str, str, str], dict[str, str]] = {}
    for pid, full, first, last, source, ror in _t2_candidates(con, surnames, rors):
        k = _key(full, first, last)
        if k:
            index.setdefault((k[0], k[1], ror), {})[pid] = source
    out = []
    for a in advisors:
        m = AdvisorMatch(advisor=a)
        hits = t1.get(a.openalex_id or "", [])
        m.t1 = tuple(pid for pid, _ in hits)
        m.t1_orcid = any(o for _, o in hits)
        k = keys[a.key]
        if k and a.ror:
            cands = index.get((k[0], k[1], a.ror), {})
            m.t2 = tuple(sorted(cands))
            m.t2_sources = tuple(sorted(set(cands.values())))
        out.append(m)
    return out


def summarize(matches: list[AdvisorMatch]) -> MatchReport:
    n = len(matches)
    rate = lambda c: round(c / n, 4) if n else 0.0
    c = {
        "t1_any": sum(1 for m in matches if m.t1),
        "t1_unique": sum(1 for m in matches if m.t1_unique),
        "t1_multi": sum(1 for m in matches if len(m.t1) > 1),
        "t1_orcid": sum(1 for m in matches if m.t1 and m.t1_orcid),
        "t2_any": sum(1 for m in matches if m.t2),
        "t2_unique": sum(1 for m in matches if m.t2_unique),
        "t2_multi": sum(1 for m in matches if len(m.t2) > 1),
        "t1_or_t2": sum(1 for m in matches if m.t1 or m.t2),
        "linked": sum(1 for m in matches if m.tier in ("t1", "t2")),
    }
    by_tier: dict[str, list[AdvisorMatch]] = {}
    for m in matches:
        by_tier.setdefault(m.advisor.tier or "?", []).append(m)
    src = Counter(s for m in matches for s in m.t2_sources)
    return MatchReport(
        total=n,
        counts=c,
        rates={k: rate(v) for k, v in c.items()},
        t1_rate_by_tier={t: round(sum(1 for m in ms if m.t1) / len(ms), 4) for t, ms in sorted(by_tier.items())},
        t2_by_source=dict(sorted(src.items())),
    )
