"""NIH RePORTER connector -- the largest single research funder.

Source: the NIH RePORTER v2 API (``https://api.reporter.nih.gov/v2/projects/search``),
public JSON, no auth. This is the programmatic equivalent of the ExPORTER bulk
project CSVs (the ExPORTER download is JS/Playwright-gated; the v2 API serves the
same project records with richer, typed fields). Two constraints:

- ``offset + limit`` must be ``<= 15000`` per query (hard cap);
- ``limit`` maxes at 500.

A single fiscal year (~80k+ projects) blows the 15k cap, so we window by
**(fiscal_year, agency IC)** -- the largest IC-year (NCI) is ~13k, safely under
the cap. Every page is cached under ``data/raw/nih/`` keyed by ``FY<y>_<ic>_off<n>``
so the run is idempotent + resumable.

Maps each project to the canonical graph:
- Funder: NIH umbrella + the specific awarding IC (NCI, NIAID, …) as a sub-funder.
- Grant: the project (award_amount in USD; unknown -> None, never silent 0).
- Organization: the awardee org (keyed on ROR where resolved, else name).
- Person: the contact + other PIs (keyed on RePORTER profile_id, stable).
- Field: the administering IC as a coarse field node (OpenAlex reconciliation later).
- Edges: funder->grant, grant->org(recipient), grant->person(pi), person->org.
"""

from __future__ import annotations

from typing import Iterable, Iterator

from atlas.connectors.base import Connector
from atlas.ror import RorResolver
from atlas.schema import Row, make_id, now_iso

NIH_API = "https://api.reporter.nih.gov/v2/projects/search"
NIH_PROJECT_URL_TMPL = "https://reporter.nih.gov/project-details/{}"
PAGE_LIMIT = 500
OFFSET_CAP = 14_999  # offset+? must stay <= 15000

NIH_CROSSREF_ID = "100000002"  # National Institutes of Health, Crossref

INCLUDE_FIELDS = [
    "ProjectNum", "ProjectTitle", "FiscalYear", "AwardAmount",
    "Organization", "PrincipalInvestigators", "ProjectStartDate",
    "ProjectEndDate", "AgencyIcAdmin", "ProjectNumSplit",
]

# Canonical NIH Institutes & Centers (awarding agency codes). Stable set used to
# window the fiscal-year query under the 15k offset cap. "OD" + others included.
NIH_ICS = [
    "NCI", "NIAID", "NIGMS", "NHLBI", "NIDDK", "NINDS", "NIMH", "NICHD",
    "NIA", "NIDA", "NEI", "NIEHS", "NIAMS", "NIDCD", "NIDCR", "NIAAA",
    "NINR", "NHGRI", "NIBIB", "NIMHD", "NCATS", "NCCIH", "FIC", "NLM",
    "OD", "CLC", "RG", "TR", "CIT", "NCRR",
]


def _money(v) -> float | None:
    if v in (None, "", 0, "0", 0.0):
        return None
    try:
        val = float(v)
    except (ValueError, TypeError):
        return None
    return val if val > 0 else None


def _date(s) -> str | None:
    if not s:
        return None
    return str(s)[:10] or None


class NihConnector(Connector):
    source = "nih"
    delay = 0.3

    NIH_FUNDER_ATLAS_ID = make_id("funder", f"crossref:{NIH_CROSSREF_ID}")

    def __init__(self, *args, resolve_ror: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.resolve_ror = resolve_ror
        self._ror = RorResolver(
            cache_path=self.raw_dir / "_ror_cache.json", http=self.http
        ) if resolve_ror else None

    # ----- fetch ----------------------------------------------------------- #

    def fetch(self, year_start: int = 2018, year_end: int = 2025,
              ics: list[str] | None = None, **kwargs) -> Iterable[dict]:
        ics = ics or NIH_ICS
        for fy in range(year_end, year_start - 1, -1):
            for ic in ics:
                offset = 0
                while offset <= OFFSET_CAP:
                    page_key = f"FY{fy}_{ic}_off{offset}"
                    page = self.load_raw(page_key)
                    if page is None:
                        body = {
                            "criteria": {"fiscal_years": [fy], "agencies": [ic]},
                            "include_fields": INCLUDE_FIELDS,
                            "offset": offset, "limit": PAGE_LIMIT,
                        }
                        resp = self.http.post(NIH_API, json_body=body)
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
                    if len(results) < PAGE_LIMIT:
                        break
                    offset += PAGE_LIMIT

    # ----- normalize ------------------------------------------------------- #

    def normalize(self, raw_pages: Iterable[dict]) -> Iterator[Row]:
        ts = now_iso()
        seen_funders: set[str] = set()
        seen_orgs: set[str] = set()
        seen_persons: set[str] = set()
        seen_fields: set[str] = set()

        for page in raw_pages:
            for r in (page.get("results") or []):
                pnum = str(r.get("project_num") or "").strip()
                if not pnum:
                    continue
                proj_url = NIH_PROJECT_URL_TMPL.format(pnum)

                # Funder: NIH umbrella, once.
                if self.NIH_FUNDER_ATLAS_ID not in seen_funders:
                    seen_funders.add(self.NIH_FUNDER_ATLAS_ID)
                    yield Row("funder", {
                        "atlas_id": self.NIH_FUNDER_ATLAS_ID,
                        "name": "National Institutes of Health",
                        "short_name": "NIH",
                        "country_code": "US",
                        "funder_type": "government",
                        "ror_id": "https://ror.org/01cwqze88",
                        "crossref_funder_id": NIH_CROSSREF_ID,
                        "homepage": "https://www.nih.gov",
                        "source": self.source, "source_id": "nih",
                        "source_url": "https://www.nih.gov", "as_of": ts,
                    })
                awarder_id = self.NIH_FUNDER_ATLAS_ID
                ic = r.get("agency_ic_admin") or {}
                ic_abbr = (ic.get("abbreviation") or "").strip()
                ic_name = (ic.get("name") or "").strip()
                if ic_abbr:
                    awarder_id = make_id("funder", self.source, "ic", ic_abbr)
                    if awarder_id not in seen_funders:
                        seen_funders.add(awarder_id)
                        yield Row("funder", {
                            "atlas_id": awarder_id,
                            "name": ic_name or ic_abbr,
                            "short_name": ic_abbr,
                            "country_code": "US",
                            "funder_type": "government",
                            "ror_id": None,
                            "crossref_funder_id": None,
                            "homepage": "https://www.nih.gov",
                            "source": self.source, "source_id": f"ic:{ic_abbr}",
                            "source_url": "https://www.nih.gov", "as_of": ts,
                        })

                amount = _money(r.get("award_amount"))
                start = _date(r.get("project_start_date"))
                end = _date(r.get("project_end_date"))
                grant_id = make_id("grant", self.source, pnum)
                status = "active" if (end and end >= ts[:10]) else \
                    ("completed" if end else "unknown")
                yield Row("grant", {
                    "atlas_id": grant_id,
                    "title": r.get("project_title"),
                    "abstract": None,
                    "amount_original": amount,
                    "currency": "USD" if amount is not None else None,
                    "amount_usd": amount,
                    "fx_rate_to_usd": 1.0 if amount is not None else None,
                    "fx_as_of": start if amount is not None else None,
                    "start_date": start,
                    "end_date": end,
                    "status": status,
                    "program": ic_abbr or None,
                    "source": self.source, "source_id": pnum,
                    "source_url": proj_url, "as_of": ts,
                })
                yield Row("funder_grant", {
                    "src_id": awarder_id, "dst_id": grant_id, "role": "awarder",
                    "source": self.source, "source_id": pnum,
                    "source_url": proj_url, "as_of": ts,
                })

                # Organization.
                org = r.get("organization") or {}
                org_name = (org.get("org_name") or "").strip()
                org_id = None
                if org_name:
                    country = org.get("org_country") or org.get("fips_country_code")
                    cc = "US" if country in ("UNITED STATES", "US", None) else None
                    ror = self._ror.resolve(org_name, cc) if self.resolve_ror else None
                    ror_id = (ror or {}).get("ror_id")
                    org_id = make_id("organization", ror_id) if ror_id \
                        else make_id("organization", self.source, org_name)
                    if org_id not in seen_orgs:
                        seen_orgs.add(org_id)
                        yield Row("organization", {
                            "atlas_id": org_id,
                            "name": org_name,
                            "ror_id": ror_id,
                            "country_code": (ror or {}).get("country_code") or cc,
                            "city": (ror or {}).get("city") or org.get("org_city"),
                            "region": org.get("org_state"),
                            "org_type": "education",
                            "homepage": (ror or {}).get("homepage"),
                            "lat": (ror or {}).get("lat"),
                            "lon": (ror or {}).get("lon"),
                            "source": self.source, "source_id": ror_id or org_name,
                            "source_url": proj_url, "as_of": ts,
                        })
                    yield Row("grant_org", {
                        "src_id": grant_id, "dst_id": org_id, "role": "recipient",
                        "source": self.source, "source_id": pnum,
                        "source_url": proj_url, "as_of": ts,
                    })

                # Persons (PIs).
                for pi in (r.get("principal_investigators") or []):
                    full = (pi.get("full_name") or "").strip()
                    if not full:
                        continue
                    profile = pi.get("profile_id")
                    person_id = make_id("person", self.source, f"profile:{profile}") \
                        if profile else make_id("person", self.source, full)
                    if person_id not in seen_persons:
                        seen_persons.add(person_id)
                        yield Row("person", {
                            "atlas_id": person_id,
                            "full_name": full,
                            "first_name": pi.get("first_name"),
                            "last_name": pi.get("last_name"),
                            "orcid": None,
                            "openalex_author_id": None,
                            "source": self.source,
                            "source_id": str(profile) if profile else full,
                            "source_url": proj_url, "as_of": ts,
                        })
                    role = "pi" if pi.get("is_contact_pi") else "co-pi"
                    yield Row("grant_person", {
                        "src_id": grant_id, "dst_id": person_id, "role": role,
                        "source": self.source, "source_id": pnum,
                        "source_url": proj_url, "as_of": ts,
                    })
                    if org_id:
                        yield Row("person_org", {
                            "src_id": person_id, "dst_id": org_id,
                            "role": "affiliation",
                            "source": self.source, "source_id": pnum,
                            "source_url": proj_url, "as_of": ts,
                        })

                # Field (administering IC).
                if ic_name:
                    field_id = make_id("field", self.source, ic_abbr or ic_name)
                    if field_id not in seen_fields:
                        seen_fields.add(field_id)
                        yield Row("field", {
                            "atlas_id": field_id,
                            "name": ic_name,
                            "openalex_id": None,
                            "level": "field",
                            "parent_atlas_id": None,
                            "source": self.source, "source_id": ic_abbr or ic_name,
                            "source_url": proj_url, "as_of": ts,
                        })
