"""UKRI / Gateway to Research connector.

Source: UKRI Gateway to Research (GtR) API -- https://gtr.ukri.org/gtr/api .
Public, JSON, no auth. The plain ``Accept: application/json`` 400s; you must
send the vendor versioned media type ``application/vnd.rcuk.gtr.json-v7`` (the
``Accept`` header is set on every request by this connector).

Pagination: ``/projects?p=N&s=100`` (1-based page, ``s`` is page size; the API
rejects ``s < 10``). Each project payload embeds its participant organizations
(with the per-org grant offer in GBP) and links out to persons (PI/co-I) and
funds. We use the embedded ``participantValues`` for orgs + money, ``leadFunder``
for the specific council, and (optionally, off by default) follow ``PI_PER`` /
``COI_PER`` links to resolve people.

Maps each project to the canonical graph:
- Funder(UKRI) -- the umbrella, plus a per-council Funder row (EPSRC, BBSRC, …)
  derived from ``leadFunder``; the council awards the grant.
- Grant -- the project (amount in GBP -> USD via a documented fixed FX).
- Organization -- lead + participant orgs, resolved to ROR where feasible.
- Person -- PI / co-I (only when person links are resolved; off by default to
  keep sample fetches cheap and polite).
- Field -- GtR ``researchTopics`` (GtR's own taxonomy; OpenAlex reconciliation
  is a separate connector).
- Edges: funder->grant, grant->org(recipient/host), grant->person(pi/co-pi),
  person->org(affiliation).

Money: GtR amounts are GBP. We normalize with a documented fixed FX rate
(:data:`GBP_TO_USD`, stamped with :data:`FX_AS_OF`). Unknown money stays None.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Iterator

from atlas.connectors.base import Connector
from atlas.ror import RorResolver
from atlas.schema import Row, make_id, now_iso

GTR_API = "https://gtr.ukri.org/gtr/api"
GTR_PROJECTS_URL = f"{GTR_API}/projects"
GTR_PROJECT_PAGE_TMPL = "https://gtr.ukri.org/projects?ref={}"
# Vendor versioned media type. The plain application/json 400s.
GTR_ACCEPT = "application/vnd.rcuk.gtr.json-v7"
PAGE_SIZE = 100  # GtR rejects s < 10; 100 is a polite ceiling.

# Documented fixed FX. GtR reports GBP; we normalize to USD with a single,
# auditable rate stamped on every grant via fx_rate_to_usd + fx_as_of.
GBP_TO_USD = 1.27
FX_AS_OF = "2026-06-01"

# Crossref Funder Registry ids for UKRI and its councils (specific council
# wins where known; falls back to the UKRI umbrella id).
UKRI_CROSSREF_ID = "100014013"
COUNCIL_CROSSREF = {
    "EPSRC": "501100000266",
    "BBSRC": "501100000268",
    "MRC": "501100000265",
    "NERC": "501100000270",
    "ESRC": "501100000269",
    "AHRC": "501100000267",
    "STFC": "501100000271",
    "Innovate UK": "501100000266",  # no distinct Crossref id; tag via name
    "Research England": "100014013",
}


def _ms_to_iso(ms) -> str | None:
    """GtR epoch-millis -> ISO date (YYYY-MM-DD), or None."""
    if ms in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _parse_money(v) -> float | None:
    """GtR money is a number in GBP. Empty/<=0 -> None (never silent 0)."""
    if v in (None, "", 0, "0"):
        return None
    try:
        val = float(v)
    except (ValueError, TypeError):
        return None
    return val if val > 0 else None


def _to_usd(gbp: float | None) -> float | None:
    return round(gbp * GBP_TO_USD, 2) if gbp is not None else None


def _status(end_iso: str | None) -> str:
    if not end_iso:
        return "unknown"
    try:
        today = datetime.now(timezone.utc).date()
        return "active" if datetime.fromisoformat(end_iso).date() >= today else "completed"
    except ValueError:
        return "unknown"


class UkriConnector(Connector):
    source = "ukri"
    delay = 0.3

    UKRI_FUNDER_ATLAS_ID = make_id("funder", f"crossref:{UKRI_CROSSREF_ID}")

    def __init__(self, *args, resolve_ror: bool = True,
                 resolve_persons: bool = False, resolve_orgs: bool = True,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.resolve_ror = resolve_ror
        self.resolve_persons = resolve_persons
        # When a project has no embedded participantValues (common for
        # fellowships/studentships), follow LEAD_ORG / PARTICIPANT_ORG links to
        # resolve org names. Cached; on by default for real org coverage.
        self.resolve_orgs = resolve_orgs
        self._ror = RorResolver(
            cache_path=self.raw_dir / "_ror_cache.json", http=self.http
        ) if resolve_ror else None

    # ----- fetch ---------------------------------------------------------- #

    def _get_json(self, url: str, params: dict | None = None):
        """GtR GET that forces the vendor Accept header."""
        prev = self.http.session.headers.get("Accept")
        self.http.session.headers["Accept"] = GTR_ACCEPT
        try:
            resp = self.http.get(url, params=params)
        finally:
            if prev is None:
                self.http.session.headers.pop("Accept", None)
            else:
                self.http.session.headers["Accept"] = prev
        return resp.json() if resp is not None else None

    def fetch(self, term: str | None = None, limit: int = 200,
              page_size: int = PAGE_SIZE) -> Iterable[dict]:
        """Page through GtR projects, caching each page. Resumable.

        ``term`` does a GtR free-text search (``?q=``); ``limit`` caps the number
        of projects pulled (sample-friendly).
        """
        s = max(10, min(page_size, limit if limit >= 10 else 10))
        fetched = 0
        page = 1
        while fetched < limit:
            page_key = f"{(term or 'all').replace(' ', '_')}_p{page}_s{s}"
            data = self.load_raw(page_key)
            if data is None:
                params = {"p": page, "s": s}
                if term:
                    params["q"] = term
                data = self._get_json(GTR_PROJECTS_URL, params=params)
                if data is None:
                    break
                self.cache_raw(page_key, data)

            projects = data.get("project", [])
            if not projects:
                break
            yield data
            fetched += len(projects)
            page += 1
            total_pages = data.get("totalPages") or 0
            if total_pages and page > total_pages:
                break
            if len(projects) < s:
                break

    def _fetch_person(self, href: str) -> dict | None:
        """Resolve a GtR person link to its name/orcid (cached)."""
        pid = href.rstrip("/").rsplit("/", 1)[-1]
        page_key = f"person_{pid}"
        data = self.load_raw(page_key)
        if data is None:
            url = href.replace("http://", "https://")
            data = self._get_json(url)
            if data is None:
                return None
            self.cache_raw(page_key, data)
        return data

    def _fetch_org(self, href: str) -> dict | None:
        """Resolve a GtR organisation link to its name/region (cached)."""
        oid = href.rstrip("/").rsplit("/", 1)[-1]
        page_key = f"org_{oid}"
        data = self.load_raw(page_key)
        if data is None:
            url = href.replace("http://", "https://")
            data = self._get_json(url)
            if data is None:
                return None
            self.cache_raw(page_key, data)
        return data

    # ----- normalize ------------------------------------------------------ #

    def normalize(self, raw_pages: Iterable[dict]) -> Iterator[Row]:
        ts = now_iso()
        seen_funders: set[str] = set()
        seen_orgs: set[str] = set()
        seen_persons: set[str] = set()
        seen_fields: set[str] = set()

        for page in raw_pages:
            for p in page.get("project", []):
                proj_id = str(p.get("id") or "").strip()
                if not proj_id:
                    continue
                ref = None
                for ident in (p.get("identifiers") or {}).get("identifier", []):
                    if ident.get("value"):
                        ref = ident["value"]
                        break
                proj_url = GTR_PROJECT_PAGE_TMPL.format(ref or proj_id)

                # --- Funder: UKRI umbrella + the specific lead council ---
                council = (p.get("leadFunder") or "").strip()
                if self.UKRI_FUNDER_ATLAS_ID not in seen_funders:
                    seen_funders.add(self.UKRI_FUNDER_ATLAS_ID)
                    yield Row("funder", {
                        "atlas_id": self.UKRI_FUNDER_ATLAS_ID,
                        "name": "UK Research and Innovation",
                        "short_name": "UKRI",
                        "country_code": "GB",
                        "funder_type": "government",
                        "ror_id": "https://ror.org/001aqnf71",
                        "crossref_funder_id": UKRI_CROSSREF_ID,
                        "homepage": "https://www.ukri.org",
                        "source": self.source, "source_id": "ukri",
                        "source_url": "https://www.ukri.org", "as_of": ts,
                    })
                awarder_id = self.UKRI_FUNDER_ATLAS_ID
                if council:
                    cx = COUNCIL_CROSSREF.get(council)
                    awarder_id = make_id("funder", f"crossref:{cx}", council) \
                        if cx else make_id("funder", self.source, council)
                    if awarder_id not in seen_funders:
                        seen_funders.add(awarder_id)
                        yield Row("funder", {
                            "atlas_id": awarder_id,
                            "name": council,
                            "short_name": council,
                            "country_code": "GB",
                            "funder_type": "government",
                            "ror_id": None,
                            "crossref_funder_id": cx,
                            "homepage": "https://www.ukri.org",
                            "source": self.source, "source_id": council,
                            "source_url": "https://www.ukri.org", "as_of": ts,
                        })

                # --- Grant ---
                start = _ms_to_iso(p.get("start"))
                end = _ms_to_iso(p.get("end"))
                gbp = self._project_amount_gbp(p)
                grant_id = make_id("grant", self.source, proj_id)
                yield Row("grant", {
                    "atlas_id": grant_id,
                    "title": p.get("title"),
                    "abstract": p.get("abstractText"),
                    "amount_original": gbp,
                    "currency": "GBP" if gbp is not None else None,
                    "amount_usd": _to_usd(gbp),
                    "fx_rate_to_usd": GBP_TO_USD if gbp is not None else None,
                    "fx_as_of": FX_AS_OF if gbp is not None else None,
                    "start_date": start,
                    "end_date": end,
                    "status": _status(end),
                    "program": p.get("grantCategory"),
                    "source": self.source, "source_id": proj_id,
                    "source_url": proj_url, "as_of": ts,
                })
                yield Row("funder_grant", {
                    "src_id": awarder_id, "dst_id": grant_id, "role": "awarder",
                    "source": self.source, "source_id": proj_id,
                    "source_url": proj_url, "as_of": ts,
                })

                # --- Organizations ---
                # Prefer embedded participantValues (org name + money). Fall back
                # to LEAD_ORG / PARTICIPANT_ORG links (fetched + cached) when the
                # project carries no embedded participants (fellowships etc.).
                lead_org_id = None
                org_specs: list[tuple[str, str | None, str]] = []  # (name, region, role)
                for part in (p.get("participantValues") or {}).get("participant", []):
                    name = (part.get("organisationName") or "").strip()
                    if not name:
                        continue
                    role_src = (part.get("role") or "").upper()
                    role = "recipient" if "LEAD" in role_src else "host"
                    org_specs.append((name, None, role))
                if not org_specs and self.resolve_orgs:
                    for href, role in self._org_links(p):
                        org = self._fetch_org(href)
                        name = (org or {}).get("name", "").strip() if org else ""
                        if not name:
                            continue
                        region = self._org_region(org)
                        org_specs.append((name, region, role))

                for name, region, role in org_specs:
                    org_id, org_row = self._org(name, "GB", seen_orgs, proj_url,
                                                ts, region=region)
                    if org_row is not None:
                        yield org_row
                    if role == "recipient" and lead_org_id is None:
                        lead_org_id = org_id
                    yield Row("grant_org", {
                        "src_id": grant_id, "dst_id": org_id, "role": role,
                        "source": self.source, "source_id": proj_id,
                        "source_url": proj_url, "as_of": ts,
                    })

                # --- Persons (PI / co-I) -- optional, link-resolved ---
                if self.resolve_persons:
                    for href, role in self._person_links(p):
                        person = self._fetch_person(href)
                        if not person:
                            continue
                        orcid = person.get("orcidId")
                        full = " ".join(
                            x for x in [person.get("firstName"),
                                        person.get("otherNames"),
                                        person.get("surname")] if x
                        ).strip()
                        if not full:
                            continue
                        person_id = make_id("person", f"orcid:{orcid}") if orcid \
                            else make_id("person", self.source, person.get("id") or full)
                        if person_id not in seen_persons:
                            seen_persons.add(person_id)
                            yield Row("person", {
                                "atlas_id": person_id,
                                "full_name": full,
                                "first_name": person.get("firstName"),
                                "last_name": person.get("surname"),
                                "orcid": orcid,
                                "openalex_author_id": None,
                                "source": self.source,
                                "source_id": person.get("id") or full,
                                "source_url": proj_url, "as_of": ts,
                            })
                        yield Row("grant_person", {
                            "src_id": grant_id, "dst_id": person_id, "role": role,
                            "source": self.source, "source_id": proj_id,
                            "source_url": proj_url, "as_of": ts,
                        })
                        if lead_org_id:
                            yield Row("person_org", {
                                "src_id": person_id, "dst_id": lead_org_id,
                                "role": "affiliation",
                                "source": self.source, "source_id": proj_id,
                                "source_url": proj_url, "as_of": ts,
                            })

                # --- Fields (GtR research topics) ---
                for topic in (p.get("researchTopics") or {}).get("researchTopic", []):
                    name = (topic.get("text") or "").strip()
                    if not name or name.lower() == "unclassified":
                        continue
                    field_id = make_id("field", self.source, name)
                    if field_id not in seen_fields:
                        seen_fields.add(field_id)
                        yield Row("field", {
                            "atlas_id": field_id,
                            "name": name,
                            "openalex_id": None,
                            "level": "field",
                            "parent_atlas_id": None,
                            "source": self.source, "source_id": name,
                            "source_url": proj_url, "as_of": ts,
                        })

    # ----- helpers -------------------------------------------------------- #

    @staticmethod
    def _project_amount_gbp(project: dict) -> float | None:
        """Sum participant grant offers (GBP). None if all unknown."""
        total = 0.0
        seen_any = False
        for part in (project.get("participantValues") or {}).get("participant", []):
            v = _parse_money(part.get("grantOffer")) or _parse_money(part.get("projectCost"))
            if v is not None:
                total += v
                seen_any = True
        return total if seen_any and total > 0 else None

    def _org(self, name: str, country: str | None, seen: set[str],
             proj_url: str, ts: str, region: str | None = None
             ) -> tuple[str, Row | None]:
        """Resolve/mint an org id; return (org_id, Row or None if already seen)."""
        ror = self._ror.resolve(name, country) if self.resolve_ror else None
        ror_id = (ror or {}).get("ror_id")
        org_id = make_id("organization", ror_id) if ror_id \
            else make_id("organization", self.source, name)
        if org_id in seen:
            return org_id, None
        seen.add(org_id)
        row = Row("organization", {
            "atlas_id": org_id,
            "name": name,
            "ror_id": ror_id,
            "country_code": (ror or {}).get("country_code") or country,
            "city": (ror or {}).get("city"),
            "region": region,
            "org_type": "education",
            "homepage": (ror or {}).get("homepage"),
            "lat": (ror or {}).get("lat"),
            "lon": (ror or {}).get("lon"),
            "source": self.source, "source_id": ror_id or name,
            "source_url": proj_url, "as_of": ts,
        })
        return org_id, row

    @staticmethod
    def _org_links(project: dict) -> list[tuple[str, str]]:
        """(href, role) for the lead/participant org links of a project."""
        out: list[tuple[str, str]] = []
        for link in (project.get("links") or {}).get("link", []):
            rel = (link.get("rel") or "").upper()
            href = link.get("href")
            if not href:
                continue
            if rel == "LEAD_ORG":
                out.append((href, "recipient"))
            elif rel == "PARTICIPANT_ORG":
                out.append((href, "host"))
        return out

    @staticmethod
    def _org_region(org: dict | None) -> str | None:
        if not org:
            return None
        addrs = (org.get("addresses") or {}).get("address") or []
        if addrs:
            return addrs[0].get("region")
        return None

    @staticmethod
    def _person_links(project: dict) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for link in (project.get("links") or {}).get("link", []):
            rel = (link.get("rel") or "").upper()
            href = link.get("href")
            if not href:
                continue
            if rel == "PI_PER":
                out.append((href, "pi"))
            elif rel == "COI_PER":
                out.append((href, "co-pi"))
        return out
