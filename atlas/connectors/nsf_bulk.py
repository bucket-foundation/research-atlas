"""NSF full-scale connector -- ALL awards across a span of years.

The NSF "Download Awards" bulk files moved behind a JS-rendered page in 2025, so
the reliable programmatic bulk path is the **research.gov Award API**
(``https://www.research.gov/awardapi-service/v1/awards.json``) -- the same data,
public, JSON, no auth. Two constraints shape the strategy:

- ``rpp`` (records per page) maxes at **100**;
- ``metadata.totalCount`` is **capped at 10000 per query**, and offsets past the
  real result count return an empty page.

So a naive "all of 2024" query would silently truncate. Instead we **window by
month** (``dateStart``/``dateEnd`` filter on award start date; a month is ~600-1800
awards, comfortably under the 10k cap) and paginate each month fully. Every page
is cached under ``data/raw/nsf_bulk/`` keyed by ``YYYY-MM_offN`` so the run is
idempotent + resumable -- a completed month costs nothing on re-run.

Reuses the canonical NSF normalization (funder/grant/org/person/field) from
:mod:`atlas.connectors.nsf` by sharing its helpers; org ROR resolution is OFF by
default at full scale.
"""

from __future__ import annotations

import calendar
from typing import Iterable, Iterator

from atlas.connectors.base import Connector
from atlas.connectors.nsf import (
    NSF_AWARD_URL_TMPL, _parse_date, _parse_money, _status,
)
from atlas.ror import RorResolver
from atlas.schema import Row, make_id, now_iso

NSF_API = "https://www.research.gov/awardapi-service/v1/awards.json"
RPP = 100  # research.gov caps rpp at 100
MAX_TOTAL = 10000  # research.gov caps totalCount per query

PRINT_FIELDS = ",".join([
    "id", "title", "abstractText", "awardeeName", "awardeeCity",
    "awardeeStateCode", "awardeeCountryCode",
    "piFirstName", "piLastName", "pdPIName",
    "startDate", "expDate", "estimatedTotalAmt", "fundsObligatedAmt",
    "fundProgramName", "dirAbbr", "divAbbr", "orgLongName", "orgLongName2",
])

NSF_FUNDER_ATLAS_ID = make_id("funder", "crossref:100000001")


def _month_windows(year_start: int, year_end: int) -> Iterator[tuple[str, str, str]]:
    """Yield (key, MM/DD/YYYY start, MM/DD/YYYY end) per month, newest first."""
    for y in range(year_end, year_start - 1, -1):
        for m in range(12, 0, -1):
            last = calendar.monthrange(y, m)[1]
            key = f"{y}-{m:02d}"
            yield key, f"{m:02d}/01/{y}", f"{m:02d}/{last:02d}/{y}"


class NsfBulkConnector(Connector):
    source = "nsf"  # same source key as the API connector -> merges/dedups
    delay = 0.25

    FUNDER_ATLAS_ID = NSF_FUNDER_ATLAS_ID

    def __init__(self, *args, resolve_ror: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.resolve_ror = resolve_ror
        self._ror = RorResolver(
            cache_path=self.raw_dir / "_ror_cache.json", http=self.http
        ) if resolve_ror else None

    # ----- fetch ----------------------------------------------------------- #

    def fetch(self, year_start: int = 2015, year_end: int = 2025,
              **kwargs) -> Iterable[dict]:
        """Page through every month in [year_start, year_end], caching pages."""
        for key, ds, de in _month_windows(year_start, year_end):
            offset = 1
            while True:
                page_key = f"{key}_off{offset}"
                page = self.load_raw(page_key)
                if page is None:
                    params = {
                        "dateStart": ds, "dateEnd": de,
                        "rpp": RPP, "offset": offset,
                        "printFields": PRINT_FIELDS,
                    }
                    resp = self.http.get(NSF_API, params=params)
                    if resp is None:
                        break
                    try:
                        page = resp.json()
                    except ValueError:
                        break
                    self.cache_raw(page_key, page)
                awards = (page.get("response") or {}).get("award", [])
                if not awards:
                    break
                yield page
                if len(awards) < RPP:
                    break
                offset += len(awards)
                if offset > MAX_TOTAL:  # respect the API cap
                    break

    # ----- normalize ------------------------------------------------------- #

    def normalize(self, raw_pages: Iterable[dict]) -> Iterator[Row]:
        ts = now_iso()
        seen_orgs: set[str] = set()
        seen_persons: set[str] = set()
        seen_fields: set[str] = set()
        funder_emitted = False

        for page in raw_pages:
            awards = (page.get("response") or {}).get("award", [])
            for a in awards:
                award_id = str(a.get("id") or "").strip()
                if not award_id:
                    continue
                award_url = NSF_AWARD_URL_TMPL.format(award_id)

                if not funder_emitted:
                    yield Row("funder", {
                        "atlas_id": self.FUNDER_ATLAS_ID,
                        "name": "National Science Foundation",
                        "short_name": "NSF",
                        "country_code": "US",
                        "funder_type": "government",
                        "ror_id": "https://ror.org/021nxhr62",
                        "crossref_funder_id": "100000001",
                        "homepage": "https://www.nsf.gov",
                        "source": self.source, "source_id": "nsf",
                        "source_url": "https://www.nsf.gov", "as_of": ts,
                    })
                    funder_emitted = True

                amount = _parse_money(a.get("estimatedTotalAmt")) or \
                    _parse_money(a.get("fundsObligatedAmt"))
                start = _parse_date(a.get("startDate"))
                end = _parse_date(a.get("expDate"))
                grant_id = make_id("grant", self.source, award_id)
                yield Row("grant", {
                    "atlas_id": grant_id,
                    "title": a.get("title"),
                    "abstract": a.get("abstractText"),
                    "amount_original": amount,
                    "currency": "USD" if amount is not None else None,
                    "amount_usd": amount,
                    "fx_rate_to_usd": 1.0 if amount is not None else None,
                    "fx_as_of": start if amount is not None else None,
                    "start_date": start,
                    "end_date": end,
                    "status": _status(start, end),
                    "program": a.get("fundProgramName"),
                    "source": self.source, "source_id": award_id,
                    "source_url": award_url, "as_of": ts,
                })
                yield Row("funder_grant", {
                    "src_id": self.FUNDER_ATLAS_ID, "dst_id": grant_id,
                    "role": "awarder",
                    "source": self.source, "source_id": award_id,
                    "source_url": award_url, "as_of": ts,
                })

                org_name = a.get("awardeeName")
                org_id = None
                if org_name:
                    ror = self._ror.resolve(org_name, a.get("awardeeCountryCode")) \
                        if self.resolve_ror else None
                    ror_id = (ror or {}).get("ror_id")
                    org_id = make_id("organization", ror_id) if ror_id \
                        else make_id("organization", self.source, org_name)
                    if org_id not in seen_orgs:
                        seen_orgs.add(org_id)
                        yield Row("organization", {
                            "atlas_id": org_id,
                            "name": org_name,
                            "ror_id": ror_id,
                            "country_code": a.get("awardeeCountryCode"),
                            "city": a.get("awardeeCity") or (ror or {}).get("city"),
                            "region": a.get("awardeeStateCode"),
                            "org_type": "education",
                            "homepage": (ror or {}).get("homepage"),
                            "lat": (ror or {}).get("lat"),
                            "lon": (ror or {}).get("lon"),
                            "source": self.source,
                            "source_id": ror_id or org_name,
                            "source_url": award_url, "as_of": ts,
                        })
                    yield Row("grant_org", {
                        "src_id": grant_id, "dst_id": org_id, "role": "recipient",
                        "source": self.source, "source_id": award_id,
                        "source_url": award_url, "as_of": ts,
                    })

                pi = (a.get("pdPIName") or "").strip()
                if not pi:
                    fn, ln = a.get("piFirstName"), a.get("piLastName")
                    pi = " ".join(p for p in [fn, ln] if p).strip()
                if pi:
                    person_id = make_id("person", self.source, pi)
                    if person_id not in seen_persons:
                        seen_persons.add(person_id)
                        parts = pi.split()
                        yield Row("person", {
                            "atlas_id": person_id,
                            "full_name": pi,
                            "first_name": parts[0] if parts else None,
                            "last_name": parts[-1] if len(parts) > 1 else None,
                            "orcid": None,
                            "openalex_author_id": None,
                            "source": self.source, "source_id": pi,
                            "source_url": award_url, "as_of": ts,
                        })
                    yield Row("grant_person", {
                        "src_id": grant_id, "dst_id": person_id, "role": "pi",
                        "source": self.source, "source_id": award_id,
                        "source_url": award_url, "as_of": ts,
                    })
                    if org_id:
                        yield Row("person_org", {
                            "src_id": person_id, "dst_id": org_id,
                            "role": "affiliation",
                            "source": self.source, "source_id": award_id,
                            "source_url": award_url, "as_of": ts,
                        })

                field_name = a.get("orgLongName2") or a.get("orgLongName") \
                    or a.get("divAbbr") or a.get("dirAbbr")
                if field_name:
                    field_id = make_id("field", self.source, field_name)
                    if field_id not in seen_fields:
                        seen_fields.add(field_id)
                        yield Row("field", {
                            "atlas_id": field_id,
                            "name": field_name,
                            "openalex_id": None,
                            "level": "field",
                            "parent_atlas_id": None,
                            "source": self.source, "source_id": field_name,
                            "source_url": award_url, "as_of": ts,
                        })
