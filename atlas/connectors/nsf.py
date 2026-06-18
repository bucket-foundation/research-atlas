"""NSF Award Search connector -- the reference implementation.

Source: NSF Award Search API (https://api.nsf.gov/services/v1/awards.json).
Public, JSON, no auth. Paginates via ``offset`` + ``rpp`` (max 25 per page).

Maps each award to the canonical graph:
- Funder(NSF) -- one fixed funder row.
- Grant -- the award (amount in USD, dates, status, abstract, program).
- Organization -- the awardee, resolved to a ROR id where feasible.
- Person -- the PI (and co-PIs when present).
- Field -- NSF directorate/division as a coarse field row (OpenAlex topic
  resolution is a separate connector; here we record NSF's own program taxonomy
  so the field node exists and can be reconciled later).
- Edges: funder->grant, grant->org(recipient), grant->person(pi/co-pi),
  person->org(affiliation), work->field via the grant's program.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Iterator

from atlas import schema
from atlas.connectors.base import Connector
from atlas.ror import RorResolver
from atlas.schema import Row, make_id, now_iso

NSF_AWARDS_URL = "https://api.nsf.gov/services/v1/awards.json"
NSF_AWARD_URL_TMPL = "https://www.nsf.gov/awardsearch/showAward?AWD_ID={}"
RPP = 25  # NSF caps records-per-page at 25

# Fields we request from the API (keeps payloads small + stable).
PRINT_FIELDS = ",".join([
    "id", "title", "abstractText", "awardeeName", "awardeeCity",
    "awardeeStateCode", "awardeeCountryCode", "awardeeZipCode", "awardeeAddress",
    "piFirstName", "piLastName", "pdPIName", "coPDPI", "piEmail",
    "startDate", "expDate", "estimatedTotalAmt", "fundsObligatedAmt",
    "fundProgramName", "primaryProgram", "dirAbbr", "divAbbr",
    "orgLongName", "orgLongName2", "agency", "transType", "date",
])


def _parse_date(s: str | None) -> str | None:
    """NSF dates are MM/DD/YYYY -> ISO YYYY-MM-DD, or None."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _parse_money(s) -> float | None:
    """NSF money is a string of dollars. Empty/0 -> None (never silent 0)."""
    if s in (None, "", "0", 0):
        return None
    try:
        val = float(str(s).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None
    return val if val > 0 else None


def _status(start: str | None, end: str | None) -> str:
    if not end:
        return "unknown"
    try:
        today = datetime.now(timezone.utc).date()
        return "active" if datetime.fromisoformat(end).date() >= today else "completed"
    except ValueError:
        return "unknown"


class NsfConnector(Connector):
    source = "nsf"
    delay = 0.3

    # Fixed canonical funder row for NSF.
    FUNDER_ATLAS_ID = make_id("funder", "crossref:100000001")  # NSF Crossref Funder id

    def __init__(self, *args, resolve_ror: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.resolve_ror = resolve_ror
        self._ror = RorResolver(
            cache_path=self.raw_dir / "_ror_cache.json", http=self.http
        ) if resolve_ror else None

    # ----- fetch ----------------------------------------------------------- #

    def fetch(self, keyword: str | None = None, date_start: str | None = None,
              date_end: str | None = None, limit: int = 300) -> Iterable[dict]:
        """Page through NSF awards, caching each page. Resumable.

        ``date_start`` / ``date_end`` are MM/DD/YYYY (NSF format). ``limit`` caps
        the number of awards pulled (sample-friendly).
        """
        fetched = 0
        offset = 1  # NSF offset is 1-based
        while fetched < limit:
            rpp = min(RPP, limit - fetched)
            page_key = f"{keyword or 'all'}_off{offset}_rpp{rpp}"
            page = self.load_raw(page_key)
            if page is None:
                params = {"rpp": rpp, "offset": offset, "printFields": PRINT_FIELDS}
                if keyword:
                    params["keyword"] = keyword
                if date_start:
                    params["dateStart"] = date_start
                if date_end:
                    params["dateEnd"] = date_end
                resp = self.http.get(NSF_AWARDS_URL, params=params)
                if resp is None:
                    break
                page = resp.json()
                self.cache_raw(page_key, page)

            awards = (page.get("response") or {}).get("award", [])
            if not awards:
                break
            yield page
            n = len(awards)
            fetched += n
            offset += n
            if n < rpp:
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

                # --- Funder (NSF), once ---
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

                # --- Grant ---
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
                    "amount_usd": amount,          # already USD; None stays None
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

                # --- Organization (awardee) ---
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

                # --- Person (PI + co-PIs) ---
                for full, role in self._people(a):
                    person_id = make_id("person", self.source, full)
                    if person_id not in seen_persons:
                        seen_persons.add(person_id)
                        parts = full.split()
                        yield Row("person", {
                            "atlas_id": person_id,
                            "full_name": full,
                            "first_name": parts[0] if parts else None,
                            "last_name": parts[-1] if len(parts) > 1 else None,
                            "orcid": None,
                            "openalex_author_id": None,
                            "source": self.source, "source_id": full,
                            "source_url": award_url, "as_of": ts,
                        })
                    yield Row("grant_person", {
                        "src_id": grant_id, "dst_id": person_id, "role": role,
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

                # --- Field (NSF directorate/division taxonomy) ---
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
                    # The grant's program ties to the field; we record it as a
                    # grant_work-style attribution is N/A here (no work yet), so
                    # we link via the grant's recipient context using work_field
                    # only when a Work exists. NSF awards have no work rows at
                    # ingest time, so field attribution is carried on the grant's
                    # program string and reconciled by the OpenAlex connector.

    @staticmethod
    def _people(award: dict) -> list[tuple[str, str]]:
        """(full_name, role) for PI and co-PIs, deduped within the award."""
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        pi = (award.get("pdPIName") or "").strip()
        if not pi:
            fn, ln = award.get("piFirstName"), award.get("piLastName")
            pi = " ".join(p for p in [fn, ln] if p).strip()
        if pi:
            out.append((pi, "pi"))
            seen.add(pi.lower())
        copd = award.get("coPDPI")
        if isinstance(copd, list):
            for name in copd:
                name = (name or "").strip()
                if name and name.lower() not in seen:
                    out.append((name, "co-pi"))
                    seen.add(name.lower())
        return out
