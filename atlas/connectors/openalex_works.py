"""OpenAlex works connector -- the research *output* side of the atlas.

The four funder connectors give the money side (who awarded what to whom).
OpenAlex gives what came *out*: the papers, datasets and preprints those awards
produced, plus the topics they belong to, the ORCIDs of their authors, and the
ROR-resolved institutions those authors sat in. This is what turns four funding
piles into a connected graph with an output dimension.

Strategy (scalable + honest)
----------------------------
OpenAlex indexes ~250M works; we do not pull them all. We pull works that
**acknowledge one of our funders**, via the ``awards.funder_id`` filter, bounded
by publication date and a per-funder page cap. Every work in that slice carries
an ``awards[]`` array whose ``funder_award_id`` we normalize
(:mod:`atlas.award_match`) and intersect with our grant ids to draw
``grant -> work`` edges. The slice is therefore exactly the works that *can*
link to our grants -- the highest-value works for a funding-flow graph.

Polite + idempotent
-------------------
- polite pool: ``?mailto=`` on every call (faster, nicer rate limits);
- cursor paging (``cursor=*`` then ``meta.next_cursor``), ``per-page=200``;
- every page cached under ``data/raw/openalex/`` keyed by
  ``F<funder>_<from>_<cursorhash>`` so a re-run resumes instead of re-fetching;
- emitted via the schema's ``coerce`` so the parquet is schema-true.

Normalized output (entities + edges)
------------------------------------
- ``work``      : the paper/dataset (keyed on OpenAlex work id)
- ``person``    : each authorship author (keyed on ORCID when present, else
                  OpenAlex author id, else name) with ``orcid`` filled
- ``organization``: each authorship institution that carries a ROR (keyed on ROR)
- ``field``     : the work's topic + its subfield/field/domain ancestry
- ``grant_work``: grant -> work, drawn when an award id links to one of our grants
- ``person_org``: author -> institution affiliation (ROR-keyed)
- ``work_field``: work -> topic (with the OpenAlex topic score)

Author -> work and work -> funder edges are intentionally out of scope for the
current schema (no ``work_person`` / ``work_funder`` edge tables); affiliation
and field coverage already connect people and topics into the graph.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Iterator

from atlas.connectors.base import Connector
from atlas.schema import Row, make_id, now_iso

OPENALEX_WORKS = "https://api.openalex.org/works"
MAILTO = "gianyrox@gmail.com"
PER_PAGE = 200

# OpenAlex funder id (F...) -> our connector source key, for award-id namespacing
# and the grant-link match. Extend as we add funders.
FUNDER_OPENALEX_TO_SOURCE = {
    "F4320332161": "nih",     # National Institutes of Health
    "F4320306076": "nsf",     # National Science Foundation
    "F4320320300": "ec",      # European Commission (CORDIS / Horizon / FP7)
    "F4320334678": "ec",      # European Research Council (ERC, in CORDIS)
    "F4320314731": "ukri",    # UK Research and Innovation
}


def _short_oa_id(url: str | None) -> str | None:
    """``https://openalex.org/W123`` -> ``W123``; passthrough for bare ids."""
    if not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1]


def _clean_orcid(orcid: str | None) -> str | None:
    """Bare ORCID (``0000-0002-1825-0097``) from an ORCID URL, or None."""
    if not orcid:
        return None
    return orcid.rstrip("/").rsplit("/", 1)[-1]


class OpenAlexWorksConnector(Connector):
    source = "openalex"
    delay = 0.12  # polite pool tolerates ~10 req/s; stay well under

    # ----- fetch ----------------------------------------------------------- #

    def fetch(self, funder_ids: list[str] | None = None,
              from_date: str = "2015-01-01",
              max_pages_per_funder: int = 0, **kwargs) -> Iterable[dict]:
        """Yield raw OpenAlex work pages for each funder, cursor-paged.

        ``max_pages_per_funder`` of 0 means *all* pages (the full slice). Each
        page is cached; a re-run with the same args resumes from disk.
        """
        funder_ids = funder_ids or list(FUNDER_OPENALEX_TO_SOURCE)
        for fid in funder_ids:
            cursor = "*"
            page_no = 0
            while cursor:
                chash = hashlib.sha1(cursor.encode()).hexdigest()[:12]
                page_key = f"{fid}_{from_date}_{chash}"
                page = self.load_raw(page_key)
                if page is None:
                    params = {
                        "filter": f"awards.funder_id:{fid},"
                                  f"from_publication_date:{from_date}",
                        "per-page": PER_PAGE,
                        "cursor": cursor,
                        "mailto": MAILTO,
                    }
                    resp = self.http.get(OPENALEX_WORKS, params=params)
                    if resp is None:
                        break
                    try:
                        page = resp.json()
                    except ValueError:
                        break
                    self.cache_raw(page_key, page)
                results = page.get("results") or []
                if not results:
                    break
                yield page
                page_no += 1
                cursor = (page.get("meta") or {}).get("next_cursor")
                if max_pages_per_funder and page_no >= max_pages_per_funder:
                    break

    # ----- normalize ------------------------------------------------------- #

    def normalize(self, raw_pages: Iterable[dict],
                  grant_key_index: dict[str, str] | None = None) -> Iterator[Row]:
        """Raw work pages -> canonical Rows.

        ``grant_key_index`` maps a normalized award join key
        (see :mod:`atlas.award_match`) to one of *our* grant ``atlas_id``s. When
        supplied, ``grant_work`` edges are emitted for every work whose award id
        resolves to a known grant. When absent (e.g. unit tests of the
        normalizer alone) only entity + non-grant edges are produced.
        """
        from atlas.award_match import award_keys

        ts = now_iso()
        seen_work: set[str] = set()
        seen_person: set[str] = set()
        seen_org: set[str] = set()
        seen_field: set[str] = set()
        seen_work_field: set[tuple[str, str]] = set()
        seen_person_org: set[tuple[str, str]] = set()
        seen_grant_work: set[tuple[str, str]] = set()

        for page in raw_pages:
            for w in (page.get("results") or []):
                oa_id = _short_oa_id(w.get("id"))
                if not oa_id:
                    continue
                doi = w.get("doi")
                work_id = make_id("work", oa_id)
                work_url = w.get("id")
                if work_id not in seen_work:
                    seen_work.add(work_id)
                    oa = w.get("open_access") or {}
                    yield Row("work", {
                        "atlas_id": work_id,
                        "title": w.get("title") or w.get("display_name"),
                        "doi": doi,
                        "openalex_id": oa_id,
                        "publication_year": w.get("publication_year"),
                        "publication_date": w.get("publication_date"),
                        "type": w.get("type"),
                        "cited_by_count": w.get("cited_by_count"),
                        "is_oa": oa.get("is_oa"),
                        "source": self.source, "source_id": oa_id,
                        "source_url": work_url, "as_of": ts,
                    })

                # ----- fields: primary topic + its ancestry ----------------- #
                topic = w.get("primary_topic") or {}
                if topic.get("id"):
                    yield from self._emit_topic_chain(
                        topic, work_id, ts, seen_field, seen_work_field,
                        score=topic.get("score"))

                # ----- authors -> person, person_org (ROR affiliations) ----- #
                for a in (w.get("authorships") or []):
                    author = a.get("author") or {}
                    name = (author.get("display_name") or "").strip()
                    orcid = _clean_orcid(author.get("orcid"))
                    oa_author = _short_oa_id(author.get("id"))
                    if not (name or orcid or oa_author):
                        continue
                    # key preference: ORCID > OpenAlex author id > name
                    if orcid:
                        person_id = make_id("person", f"orcid:{orcid}")
                    elif oa_author:
                        person_id = make_id("person", f"openalex:{oa_author}")
                    else:
                        person_id = make_id("person", self.source, name)
                    if person_id not in seen_person:
                        seen_person.add(person_id)
                        first = last = None
                        if "," in name:
                            last, _, first = name.partition(",")
                            last, first = last.strip(), first.strip()
                        yield Row("person", {
                            "atlas_id": person_id,
                            "full_name": name or None,
                            "first_name": first,
                            "last_name": last,
                            "orcid": orcid,
                            "openalex_author_id": oa_author,
                            "source": self.source,
                            "source_id": orcid or oa_author or name,
                            "source_url": author.get("id") or work_url,
                            "as_of": ts,
                        })

                    for inst in (a.get("institutions") or []):
                        ror = inst.get("ror")
                        if not ror:
                            continue
                        org_id = make_id("organization", ror)
                        if org_id not in seen_org:
                            seen_org.add(org_id)
                            yield Row("organization", {
                                "atlas_id": org_id,
                                "name": inst.get("display_name"),
                                "ror_id": ror,
                                "country_code": inst.get("country_code"),
                                "city": None, "region": None,
                                "org_type": inst.get("type"),
                                "homepage": None, "lat": None, "lon": None,
                                "source": self.source, "source_id": ror,
                                "source_url": ror, "as_of": ts,
                            })
                        po = (person_id, org_id)
                        if po not in seen_person_org:
                            seen_person_org.add(po)
                            yield Row("person_org", {
                                "src_id": person_id, "dst_id": org_id,
                                "role": "affiliation",
                                "source": self.source, "source_id": oa_id,
                                "source_url": work_url, "as_of": ts,
                            })

                # ----- grant -> work links via award ids -------------------- #
                if grant_key_index:
                    for award in (w.get("awards") or []):
                        fid = _short_oa_id(award.get("funder_id"))
                        src = FUNDER_OPENALEX_TO_SOURCE.get(fid)
                        aid = award.get("funder_award_id")
                        if not aid:
                            continue
                        for key in award_keys(src or "", aid):
                            grant_atlas = grant_key_index.get(key)
                            if grant_atlas:
                                gw = (grant_atlas, work_id)
                                if gw not in seen_grant_work:
                                    seen_grant_work.add(gw)
                                    yield Row("grant_work", {
                                        "src_id": grant_atlas, "dst_id": work_id,
                                        "role": "acknowledges",
                                        "source": self.source, "source_id": oa_id,
                                        "source_url": work_url, "as_of": ts,
                                    })
                                break  # one edge per (grant, work)

    # ----- helpers --------------------------------------------------------- #

    def _emit_topic_chain(self, topic: dict, work_id: str, ts: str,
                          seen_field: set, seen_work_field: set,
                          score=None) -> Iterator[Row]:
        """Emit the topic and its subfield/field/domain ancestry as fields.

        The work links to its leaf topic (with score); the topic chain links via
        ``parent_atlas_id`` so the OpenAlex taxonomy is navigable in the graph.
        """
        # domain -> field -> subfield -> topic, so each node knows its parent
        chain = [
            ("domain", topic.get("domain") or {}),
            ("field", topic.get("field") or {}),
            ("subfield", topic.get("subfield") or {}),
            ("topic", topic),
        ]
        parent_atlas = None
        leaf_atlas = None
        for level, node in chain:
            oa = _short_oa_id(node.get("id"))
            if not oa:
                continue
            field_atlas = make_id("field", oa)
            leaf_atlas = field_atlas
            if field_atlas not in seen_field:
                seen_field.add(field_atlas)
                yield Row("field", {
                    "atlas_id": field_atlas,
                    "name": node.get("display_name"),
                    "openalex_id": oa,
                    "level": level,
                    "parent_atlas_id": parent_atlas,
                    "source": self.source, "source_id": oa,
                    "source_url": node.get("id"), "as_of": ts,
                })
            parent_atlas = field_atlas
        # work -> leaf topic edge
        if leaf_atlas:
            wf = (work_id, leaf_atlas)
            if wf not in seen_work_field:
                seen_work_field.add(wf)
                yield Row("work_field", {
                    "src_id": work_id, "dst_id": leaf_atlas,
                    "score": float(score) if score is not None else None,
                    "source": self.source, "source_id": leaf_atlas,
                    "source_url": None, "as_of": ts,
                })
