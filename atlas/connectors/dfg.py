"""DFG (Deutsche Forschungsgemeinschaft) connector, via GEPRIS.

DFG's project database is **GEPRIS** (https://gepris.dfg.de). There is no clean
JSON API or bulk export, so this is a **polite, cached HTML connector** that
scrapes individual English-language project detail pages
(``/gepris/projekt/<id>?language=en``). Project detail pages are *not* disallowed
by GEPRIS ``robots.txt`` (only the faceted-search query-param URLs are). Project
ids are discovered from the GEPRIS sitemap (``sitemap_index.xml`` ->
``sitemap.projects.N.xml``), which is the politeness-friendly path.

To avoid hammering GEPRIS we run a **small sample** by default (``limit`` low,
inter-request ``delay`` high) and cache every fetched page under
``data/raw/dfg/``. A no-network normalizer test runs against recorded HTML
fixtures (see ``tests/fixtures/dfg/``).

Maps each project to the canonical graph:
- Funder(DFG) -- one fixed funder row (government).
- Grant -- the project. NOTE: GEPRIS rarely publishes the funding amount on the
  English project page, so ``amount_*`` is typically None (money invariant:
  unknown is null, never 0).
- Organization -- the applicant institution, resolved to ROR.
- Person -- the spokesperson / applicant (PI), and participating researchers.
- Field -- the GEPRIS "Subject Area" (DFG's own classification).
- Edges: funder->grant, grant->org(recipient), grant->person(pi/co-pi),
  person->org(affiliation).

GEPRIS does not expose ORCID on project pages; persons fall back to
(source, name) keys.

**Live-fetch status:** the HTML connector is implemented and works against the
live site; the default ``ingest_dfg.py`` run is intentionally tiny (polite). The
recorded fixtures guarantee the normalizer is testable offline.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Iterable, Iterator

from atlas.connectors.base import BROWSER_UA, Connector
from atlas.ror import RorResolver
from atlas.schema import Row, make_id, now_iso

DFG_CROSSREF_ID = "501100001659"  # Deutsche Forschungsgemeinschaft, Crossref
GEPRIS_PROJECT_URL_TMPL = "https://gepris.dfg.de/gepris/projekt/{}?language=en"
GEPRIS_PROJECT_CITE_TMPL = "https://gepris.dfg.de/gepris/projekt/{}"
GEPRIS_SITEMAP_INDEX = "https://gepris.dfg.de/gepris/sitemap_index.xml"

_NAME_VALUE_RE = re.compile(
    r'<span class="name">\s*(.*?)\s*</span>\s*<span class="value">(.*?)</span>',
    re.S,
)
_H1_RE = re.compile(r'<h1 class="facelift"[^>]*>(.*?)</h1>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_TERM_RE = re.compile(r"from\s+(\d{4})(?:\s+to\s+(\d{4}))?")


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def parse_project_html(page_html: str) -> dict:
    """Parse a GEPRIS English project page into a flat dict of fields."""
    out: dict = {}
    m = _H1_RE.search(page_html)
    if m:
        out["title"] = _strip(m.group(1))
    for name, value in _NAME_VALUE_RE.findall(page_html):
        out[_strip(name)] = value  # keep raw value html for link extraction
    return out


def _split_people(value_html: str) -> list[str]:
    """Participating researchers are 'Name ; Name ; …' (links or text)."""
    text = _strip(value_html)
    return [p.strip() for p in text.split(";") if p.strip()]


class DfgConnector(Connector):
    source = "dfg"
    user_agent = BROWSER_UA  # GEPRIS is picky about default agents
    delay = 1.0  # polite: GEPRIS is HTML-scraped, go slow

    FUNDER_ATLAS_ID = make_id("funder", f"crossref:{DFG_CROSSREF_ID}")

    def __init__(self, *args, resolve_ror: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.resolve_ror = resolve_ror
        self._ror = RorResolver(
            cache_path=self.raw_dir / "_ror_cache.json", http=self.http
        ) if resolve_ror else None

    # ----- fetch ---------------------------------------------------------- #

    def _project_ids(self, limit: int) -> list[str]:
        """Discover project ids from the GEPRIS sitemap (cached)."""
        idx = self.load_raw("_sitemap_index")
        if idx is None:
            resp = self.http.get(GEPRIS_SITEMAP_INDEX)
            idx = resp.text if resp is not None else ""
            self.cache_raw("_sitemap_index", idx)
        sm_urls = [u for u in re.findall(r"<loc>(.*?)</loc>", idx)
                   if "sitemap.projects" in u]
        ids: list[str] = []
        for sm in sm_urls:
            if len(ids) >= limit:
                break
            key = "_sm_" + sm.rstrip("/").rsplit("/", 1)[-1]
            body = self.load_raw(key)
            if body is None:
                resp = self.http.get(sm)
                body = resp.text if resp is not None else ""
                self.cache_raw(key, body)
            for u in re.findall(r"<loc>(.*?)</loc>", body):
                m = re.search(r"/projekt/(\d+)", u)
                if m:
                    ids.append(m.group(1))
                    if len(ids) >= limit:
                        break
        return ids[:limit]

    def fetch(self, project_ids: Iterable[str] | None = None,
              limit: int = 10) -> Iterable[dict]:
        """Yield raw GEPRIS project records. Polite, cached, resumable.

        ``project_ids`` overrides sitemap discovery (handy for testing). Each
        page is cached under ``data/raw/dfg/projekt_<id>.json``.
        """
        ids = list(project_ids) if project_ids is not None \
            else self._project_ids(limit)
        for pid in ids[:limit]:
            page_key = f"projekt_{pid}"
            rec = self.load_raw(page_key)
            if rec is None:
                url = GEPRIS_PROJECT_URL_TMPL.format(pid)
                resp = self.http.get(url)
                if resp is None:
                    continue
                rec = {"id": pid, "html": resp.text}
                self.cache_raw(page_key, rec)
            yield rec

    # ----- normalize ------------------------------------------------------ #

    def normalize(self, raw_pages: Iterable[dict]) -> Iterator[Row]:
        ts = now_iso()
        seen_orgs: set[str] = set()
        seen_persons: set[str] = set()
        seen_fields: set[str] = set()
        funder_emitted = False

        for rec in raw_pages:
            pid = str(rec.get("id") or "").strip()
            page_html = rec.get("html") or ""
            if not pid or not page_html:
                continue
            fields = parse_project_html(page_html)
            proj_url = GEPRIS_PROJECT_CITE_TMPL.format(pid)

            # --- Funder (DFG), once ---
            if not funder_emitted:
                yield Row("funder", {
                    "atlas_id": self.FUNDER_ATLAS_ID,
                    "name": "Deutsche Forschungsgemeinschaft",
                    "short_name": "DFG",
                    "country_code": "DE",
                    "funder_type": "government",
                    "ror_id": "https://ror.org/018mejw64",
                    "crossref_funder_id": DFG_CROSSREF_ID,
                    "homepage": "https://www.dfg.de",
                    "source": self.source, "source_id": "dfg",
                    "source_url": "https://www.dfg.de", "as_of": ts,
                })
                funder_emitted = True

            # --- Grant ---
            start, end = self._term(fields.get("Term"))
            grant_id = make_id("grant", self.source, pid)
            yield Row("grant", {
                "atlas_id": grant_id,
                "title": fields.get("title"),
                "abstract": None,
                # GEPRIS does not publish a funding amount: money is unknown,
                # therefore None (never a silent 0).
                "amount_original": None,
                "currency": None,
                "amount_usd": None,
                "fx_rate_to_usd": None,
                "fx_as_of": None,
                "start_date": start,
                "end_date": end,
                "status": "completed" if end else "unknown",
                "program": _strip(fields.get("DFG Programme", "")) or None,
                "source": self.source, "source_id": pid,
                "source_url": proj_url, "as_of": ts,
            })
            yield Row("funder_grant", {
                "src_id": self.FUNDER_ATLAS_ID, "dst_id": grant_id,
                "role": "awarder",
                "source": self.source, "source_id": pid,
                "source_url": proj_url, "as_of": ts,
            })

            # --- Organization (applicant institution) ---
            org_id = None
            inst = _strip(fields.get("Applicant Institution", "")) \
                or _strip(fields.get("Institution", ""))
            if inst:
                ror = self._ror.resolve(inst, "DE") if self.resolve_ror else None
                ror_id = (ror or {}).get("ror_id")
                org_id = make_id("organization", ror_id) if ror_id \
                    else make_id("organization", self.source, inst)
                if org_id not in seen_orgs:
                    seen_orgs.add(org_id)
                    yield Row("organization", {
                        "atlas_id": org_id,
                        "name": inst,
                        "ror_id": ror_id,
                        "country_code": (ror or {}).get("country_code") or "DE",
                        "city": (ror or {}).get("city"),
                        "region": None,
                        "org_type": "education",
                        "homepage": (ror or {}).get("homepage"),
                        "lat": (ror or {}).get("lat"),
                        "lon": (ror or {}).get("lon"),
                        "source": self.source, "source_id": ror_id or inst,
                        "source_url": proj_url, "as_of": ts,
                    })
                yield Row("grant_org", {
                    "src_id": grant_id, "dst_id": org_id, "role": "recipient",
                    "source": self.source, "source_id": pid,
                    "source_url": proj_url, "as_of": ts,
                })

            # --- Persons (spokesperson/applicant = PI; participants = co-pi) ---
            people: list[tuple[str, str]] = []
            for label in ("Spokesperson", "Applicant", "Applicants"):
                if fields.get(label):
                    for n in _split_people(fields[label]):
                        people.append((n, "pi"))
            if fields.get("Participating Researchers"):
                for n in _split_people(fields["Participating Researchers"]):
                    people.append((n, "co-pi"))
            seen_in_proj: set[str] = set()
            for full, role in people:
                if not full or full.lower() in seen_in_proj:
                    continue
                seen_in_proj.add(full.lower())
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
                        "source_url": proj_url, "as_of": ts,
                    })
                yield Row("grant_person", {
                    "src_id": grant_id, "dst_id": person_id, "role": role,
                    "source": self.source, "source_id": pid,
                    "source_url": proj_url, "as_of": ts,
                })
                if org_id:
                    yield Row("person_org", {
                        "src_id": person_id, "dst_id": org_id,
                        "role": "affiliation",
                        "source": self.source, "source_id": pid,
                        "source_url": proj_url, "as_of": ts,
                    })

            # --- Field (Subject Area) ---
            subj = _strip(fields.get("Subject Area", ""))
            if subj:
                field_id = make_id("field", self.source, subj)
                if field_id not in seen_fields:
                    seen_fields.add(field_id)
                    yield Row("field", {
                        "atlas_id": field_id,
                        "name": subj,
                        "openalex_id": None,
                        "level": "field",
                        "parent_atlas_id": None,
                        "source": self.source, "source_id": subj,
                        "source_url": proj_url, "as_of": ts,
                    })

    # ----- helpers -------------------------------------------------------- #

    @staticmethod
    def _term(value_html: str | None) -> tuple[str | None, str | None]:
        """'from 1997 to 2003' -> ('1997-01-01', '2003-12-31'); years only."""
        if not value_html:
            return None, None
        m = _TERM_RE.search(_strip(value_html))
        if not m:
            return None, None
        start = f"{m.group(1)}-01-01" if m.group(1) else None
        end = f"{m.group(2)}-12-31" if m.group(2) else None
        return start, end
