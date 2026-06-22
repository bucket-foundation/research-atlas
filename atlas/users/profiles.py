"""Build enriched researcher profiles from the atlas (full, cheap enrichment).

The atlas schema has no ``work_person`` edge -- author->work links live only in
the **raw OpenAlex work pages** under ``data/raw/openalex/``. This module
reconstructs person->work links by streaming those raw pages once, accumulating
per-person stats:

- which OpenAlex topics / subfields / fields / domains they publish in,
- works count, first/last year, total + max citations,
- an **h-index proxy** computed over their *corpus* works,
- corresponding-author count (the PI signal -- ``authorships[].is_corresponding``),
- their most-frequent ROR-resolved institution.

Person identity uses the **same key rule the works connector uses**
(ORCID > OpenAlex author id > name), so a profile's ``atlas_id`` matches the
Person node in ``person.parquet`` -- profiles are a *join target*, not a fork.

This is the FULL-set enrichment: it runs over every author in the corpus from
data already on disk, no network. Contact enrichment (network, high-value
segment only) is :mod:`atlas.users.contacts`.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Iterator

from atlas.schema import make_id, now_iso
from atlas.users import segment
from atlas.users.schema import coerce_user, field_to_slug

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_OPENALEX = REPO_ROOT / "data" / "raw" / "openalex"


def _short_oa_id(url: str | None) -> str | None:
    if not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1]


def _clean_orcid(orcid: str | None) -> str | None:
    if not orcid:
        return None
    return orcid.rstrip("/").rsplit("/", 1)[-1]


def _person_key(orcid, oa_author, name) -> str | None:
    """Replicates the works connector's person id rule so ids align."""
    if orcid:
        return make_id("person", f"orcid:{orcid}")
    if oa_author:
        return make_id("person", f"openalex:{oa_author}")
    if name:
        return make_id("person", "openalex", name)
    return None


@dataclass
class _Accum:
    """Mutable per-person accumulator while streaming raw works."""
    full_name: str | None = None
    orcid: str | None = None
    oa_author: str | None = None
    works: set = dc_field(default_factory=set)        # openalex work ids
    citations: list = dc_field(default_factory=list)  # cited_by per work
    years: list = dc_field(default_factory=list)
    corresponding: int = 0
    # field/topic frequency
    domains: "defaultdict[str,int]" = dc_field(default_factory=lambda: defaultdict(int))
    fields: "defaultdict[str,int]" = dc_field(default_factory=lambda: defaultdict(int))
    subfields: "defaultdict[str,int]" = dc_field(default_factory=lambda: defaultdict(int))
    topics: "defaultdict[str,int]" = dc_field(default_factory=lambda: defaultdict(int))
    # institution frequency: ror -> (count, name, country)
    orgs: "defaultdict[str,list]" = dc_field(
        default_factory=lambda: defaultdict(lambda: [0, None, None]))


def _h_index(citations: list[int]) -> int:
    """Standard h-index over a list of per-work citation counts."""
    h = 0
    for i, c in enumerate(sorted((x or 0 for x in citations), reverse=True), 1):
        if c >= i:
            h = i
        else:
            break
    return h


def _top(counter: "defaultdict[str,int]", n: int = 1) -> list[str]:
    return [k for k, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def iter_raw_pages(raw_dir: Path | None = None) -> Iterator[dict]:
    """Yield every cached raw OpenAlex work page."""
    raw_dir = raw_dir or RAW_OPENALEX
    for p in sorted(raw_dir.glob("*.json")):
        try:
            yield json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue


def accumulate(raw_dir: Path | None = None,
               progress_every: int = 0) -> dict[str, _Accum]:
    """Stream raw works once, returning ``person_atlas_id -> _Accum``."""
    acc: dict[str, _Accum] = {}
    seen_work_for_person: dict[str, set] = defaultdict(set)
    page_no = 0
    for page in iter_raw_pages(raw_dir):
        page_no += 1
        if progress_every and page_no % progress_every == 0:
            print(f"  accumulate: page {page_no}, {len(acc):,} people")
        for w in (page.get("results") or []):
            oa_work = _short_oa_id(w.get("id"))
            if not oa_work:
                continue
            cited = w.get("cited_by_count")
            year = w.get("publication_year")
            topic = w.get("primary_topic") or {}
            dom = (topic.get("domain") or {}).get("display_name")
            fld = (topic.get("field") or {}).get("display_name")
            sub = (topic.get("subfield") or {}).get("display_name")
            top = topic.get("display_name")

            for a in (w.get("authorships") or []):
                author = a.get("author") or {}
                name = (author.get("display_name") or "").strip() or None
                orcid = _clean_orcid(author.get("orcid"))
                oa_author = _short_oa_id(author.get("id"))
                pid = _person_key(orcid, oa_author, name)
                if not pid:
                    continue
                ac = acc.get(pid)
                if ac is None:
                    ac = acc[pid] = _Accum()
                # identity (prefer first non-null)
                ac.full_name = ac.full_name or name
                ac.orcid = ac.orcid or orcid
                ac.oa_author = ac.oa_author or oa_author
                # per-work stats: only count a work once per person
                if oa_work not in seen_work_for_person[pid]:
                    seen_work_for_person[pid].add(oa_work)
                    ac.works.add(oa_work)
                    ac.citations.append(cited or 0)
                    if year:
                        ac.years.append(int(year))
                    if dom:
                        ac.domains[dom] += 1
                    if fld:
                        ac.fields[fld] += 1
                    if sub:
                        ac.subfields[sub] += 1
                    if top:
                        ac.topics[top] += 1
                if a.get("is_corresponding"):
                    ac.corresponding += 1
                # institution frequency (ROR-resolved only)
                for inst in (a.get("institutions") or []):
                    ror = inst.get("ror")
                    if not ror:
                        continue
                    rec = ac.orgs[ror]
                    rec[0] += 1
                    rec[1] = rec[1] or inst.get("display_name")
                    rec[2] = rec[2] or inst.get("country_code")
    return acc


def build_profiles(raw_dir: Path | None = None,
                   progress_every: int = 0) -> Iterator[dict]:
    """Yield coerced enriched user rows (no email -- that is contacts.py).

    Each row is run through :func:`coerce_user`, so it is schema-true and
    compliance-checked (no email present here, so contactable=False by default).
    """
    ts = now_iso()
    acc = accumulate(raw_dir, progress_every=progress_every)
    for pid, ac in acc.items():
        years = ac.years
        first_year = min(years) if years else None
        last_year = max(years) if years else None
        total_cit = sum(ac.citations)
        max_cit = max(ac.citations) if ac.citations else 0
        h = _h_index(ac.citations)
        is_corr = ac.corresponding > 0

        primary_field = (_top(ac.fields) or [None])[0]
        field_slug = field_to_slug(primary_field)
        sen = segment.seniority(first_year, last_year, total_cit, max_cit, h, is_corr)
        act = segment.activity_tier(last_year, is_corr)

        # most-frequent institution
        org_ror = org_name = country = None
        org_atlas = None
        if ac.orgs:
            ror, rec = max(ac.orgs.items(), key=lambda kv: kv[1][0])
            org_ror, org_name, country = ror, rec[1], rec[2]
            org_atlas = make_id("organization", ror)

        first = last = None
        if ac.full_name and "," in ac.full_name:
            last, _, first = ac.full_name.partition(",")
            last, first = last.strip(), first.strip()

        row = coerce_user({
            "atlas_id": pid,
            "full_name": ac.full_name,
            "first_name": first,
            "last_name": last,
            "orcid": ac.orcid,
            "openalex_author_id": ac.oa_author,
            "primary_domain": (_top(ac.domains) or [None])[0],
            "primary_field": primary_field,
            "primary_subfield": (_top(ac.subfields) or [None])[0],
            "field_slug": field_slug,
            "top_topics": ";".join(_top(ac.topics, 5)) or None,
            "n_fields": len(ac.fields),
            "primary_org_atlas_id": org_atlas,
            "primary_org_name": org_name,
            "primary_org_ror": org_ror,
            "country_code": country,
            "works_count": len(ac.works),
            "first_year": first_year,
            "last_year": last_year,
            "active_recent": segment.active_recent(last_year),
            "total_citations": total_cit,
            "max_citations": max_cit,
            "h_index_proxy": h,
            "corresponding_count": ac.corresponding,
            "is_corresponding_author": is_corr,
            "seniority": sen,
            "activity_tier": act,
            "segment": segment.make_segment(field_slug, sen, act),
            "tool_fit": ";".join(segment.tool_fit(field_slug)),
            # contact fields stay null in the full build
            "email": None,
            "contactable": False,
            "opt_out": False,
            "engagement_status": "not-contacted",
            "source": "atlas-users",
            "source_url": (f"https://orcid.org/{ac.orcid}" if ac.orcid
                           else (f"https://openalex.org/{ac.oa_author}"
                                 if ac.oa_author else None)),
            "as_of": ts,
        })
        yield row
