"""Alfred P. Sloan Foundation connector, via the public Grants Database.

Sloan publishes its grants (currently operating programs, back to ~2008) in a
paginated public grants database at https://sloan.org/grants-database . There is
no JSON API or bulk export, so this is a **polite, cached HTML connector** that
pages the listing (``?page=N``) and parses each grant card. Every card carries
the load-bearing fields inline: grantee org, amount (USD), city, year, brief
description (the grant's purpose), Program, Sub-program, Investigator, and a
``/grant-detail/g-<year>-<id>`` permalink. ``robots.txt`` is empty (no
restrictions); we still go slow (default 1s delay) and cache every page.

Maps each grant to the canonical graph:
- Funder(Sloan) -- one fixed funder row (private foundation).
- Grant -- the grant. Amount is USD (1.0 FX, stamped). Sloan publishes only an
  award *year* (no start/end dates), recorded as a Jan-1 start_date so the
  by-year rollups work; end_date stays None.
- Organization -- the grantee, with city/state, resolved to ROR (backfill pass).
- Person -- the named Investigator (PI). No ORCID published; (source, name) key.
- Field -- "Program / Sub-program" (Sloan's own classification).
- Edges: funder->grant, grant->org(recipient), grant->person(pi),
  person->org(affiliation).
"""

from __future__ import annotations

import html
import re
from typing import Iterable, Iterator

from atlas.connectors.base import BROWSER_UA, Connector
from atlas.ror_bulk import RorIndex
from atlas.schema import Row, make_id, now_iso

SLOAN_CROSSREF_ID = "100000879"  # Alfred P. Sloan Foundation, Crossref
SLOAN_ROR_ID = "https://ror.org/052csg198"
SLOAN_DB_URL = "https://sloan.org/grants-database"
SLOAN_GRANT_URL_TMPL = "https://sloan.org{}"  # the /grant-detail/... permalink

USD_FX = 1.0
FX_AS_OF = "2026-06-01"

_TAG_RE = re.compile(r"<[^>]+>")
# One grant card = the <li> whose immediate child is <header>. We split the
# listing on each "<li> ... <header>" boundary (cards contain nested <li> items
# for Program/Investigator, so a naive non-greedy </li> match would stop early).
_CARD_SPLIT_RE = re.compile(r"<li>\s*<header>", re.S)
_GRANTEE_RE = re.compile(
    r'class="grantee">\s*(?:<span[^>]*>[^<]*</span>)?\s*([^<]+)', re.S)
_AMOUNT_RE = re.compile(r'class="amount">.*?\$([\d,]+)', re.S)
_CITY_RE = re.compile(
    r'class="city">\s*(?:<span[^>]*>[^<]*</span>)?\s*([^<]+)', re.S)
_YEAR_RE = re.compile(r'class="year">.*?(?:</span>)?\s*(\d{4})', re.S)
_PERMA_RE = re.compile(r'href="(/grant-detail/[^"]+)"')
_DESC_RE = re.compile(r'class="brief-description">\s*<p>(.*?)</p>', re.S)
_LABEL_RE = re.compile(
    r'<span class="label">\s*(.*?)\s*</span>\s*([^<]*)', re.S)


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s))).strip()
    return s or None


def parse_listing_html(page_html: str) -> list[dict]:
    """Parse one Sloan grants-database listing page into grant dicts."""
    out: list[dict] = []
    parts = _CARD_SPLIT_RE.split(page_html or "")
    # parts[0] is the page chrome before the first card; the rest each begin
    # right after "<li><header>" of a card and run to the next card boundary.
    for body in parts[1:]:
        card = "<header>" + body
        perma = _PERMA_RE.search(card)
        if not perma:
            continue
        path = perma.group(1)
        gid = path.rsplit("/", 1)[-1]
        grantee = _clean(_GRANTEE_RE.search(card).group(1)) \
            if _GRANTEE_RE.search(card) else None
        am = _AMOUNT_RE.search(card)
        amount = float(am.group(1).replace(",", "")) if am else None
        city = _clean(_CITY_RE.search(card).group(1)) \
            if _CITY_RE.search(card) else None
        ym = _YEAR_RE.search(card)
        year = int(ym.group(1)) if ym else None
        dm = _DESC_RE.search(card)
        desc = _clean(dm.group(1)) if dm else None
        labels = {}
        for lbl, val in _LABEL_RE.findall(card):
            lbl = _clean(lbl)
            val = _clean(val)
            if lbl and val:
                labels[lbl] = val
        out.append({
            "id": gid, "path": path, "grantee": grantee, "amount": amount,
            "city": city, "year": year, "description": desc,
            "program": labels.get("Program"),
            "sub_program": labels.get("Sub-program"),
            "investigator": labels.get("Investigator"),
        })
    return out


def _split_city_state(city: str | None) -> tuple[str | None, str | None]:
    """'Brooklyn, NY' -> ('Brooklyn', 'NY'); 'London' -> ('London', None)."""
    if not city:
        return None, None
    if "," in city:
        c, s = city.rsplit(",", 1)
        return c.strip() or None, s.strip() or None
    return city, None


# US two-letter state codes (so we can gate ROR to US when a state is present).
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


class SloanConnector(Connector):
    source = "sloan"
    user_agent = BROWSER_UA
    delay = 1.0  # polite HTML scrape

    FUNDER_ATLAS_ID = make_id("funder", f"crossref:{SLOAN_CROSSREF_ID}")

    def __init__(self, *args, ror_index: RorIndex | None = None,
                 resolve_ror: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.resolve_ror = resolve_ror
        self._ror_index = ror_index

    # ----- fetch ---------------------------------------------------------- #

    def fetch(self, limit_pages: int | None = None,
              start_page: int = 1, **kwargs) -> Iterable[dict]:
        """Page through the Sloan grants database, caching each page. Resumable.

        Stops when a page yields no grant cards (the end) or ``limit_pages`` is
        reached. Each page cached under ``data/raw/sloan/page_<N>.json``.
        """
        page = start_page
        fetched = 0
        while True:
            if limit_pages is not None and fetched >= limit_pages:
                break
            page_key = f"page_{page}"
            rec = self.load_raw(page_key)
            if rec is None:
                url = SLOAN_DB_URL if page == 1 else f"{SLOAN_DB_URL}?page={page}"
                resp = self.http.get(url)
                if resp is None:
                    break
                rec = {"page": page, "html": resp.text}
                self.cache_raw(page_key, rec)
            cards = parse_listing_html(rec.get("html") or "")
            if not cards:
                break
            yield rec
            fetched += 1
            page += 1

    # ----- normalize ------------------------------------------------------ #

    def normalize(self, raw_pages: Iterable[dict]) -> Iterator[Row]:
        ts = now_iso()
        seen_orgs: set[str] = set()
        seen_persons: set[str] = set()
        seen_fields: set[str] = set()
        funder_emitted = False

        for page in raw_pages:
            for g in parse_listing_html(page.get("html") or ""):
                gid_src = (g.get("id") or "").strip()
                if not gid_src:
                    continue

                if not funder_emitted:
                    yield Row("funder", {
                        "atlas_id": self.FUNDER_ATLAS_ID,
                        "name": "Alfred P. Sloan Foundation",
                        "short_name": "Sloan Foundation",
                        "country_code": "US",
                        "funder_type": "nonprofit",
                        "ror_id": SLOAN_ROR_ID,
                        "crossref_funder_id": SLOAN_CROSSREF_ID,
                        "homepage": "https://sloan.org",
                        "source": self.source, "source_id": "sloan",
                        "source_url": SLOAN_DB_URL, "as_of": ts,
                    })
                    funder_emitted = True

                grant_url = SLOAN_GRANT_URL_TMPL.format(g.get("path") or "")
                amount = g.get("amount")
                amount = amount if (amount and amount > 0) else None
                year = g.get("year")
                start = f"{year:04d}-01-01" if year else None
                program = g.get("program")
                sub = g.get("sub_program")
                prog_label = " / ".join(p for p in [program, sub] if p) or None
                grant_id = make_id("grant", self.source, gid_src)
                yield Row("grant", {
                    "atlas_id": grant_id,
                    "title": g.get("description"),
                    "abstract": None,
                    "amount_original": amount,
                    "currency": "USD" if amount is not None else None,
                    "amount_usd": amount,
                    "fx_rate_to_usd": USD_FX if amount is not None else None,
                    "fx_as_of": FX_AS_OF if amount is not None else None,
                    "start_date": start,
                    "end_date": None,
                    "status": "unknown",
                    "program": prog_label,
                    "source": self.source, "source_id": gid_src,
                    "source_url": grant_url, "as_of": ts,
                })
                yield Row("funder_grant", {
                    "src_id": self.FUNDER_ATLAS_ID, "dst_id": grant_id,
                    "role": "awarder",
                    "source": self.source, "source_id": gid_src,
                    "source_url": grant_url, "as_of": ts,
                })

                # --- Organization (grantee) ---
                org_id = None
                name = g.get("grantee")
                if name:
                    city, state = _split_city_state(g.get("city"))
                    iso = "US" if (state and state in _US_STATES) else None
                    ror_id = ror_cc = ror_city = ror_lat = ror_lon = ror_home = None
                    if self.resolve_ror and self._ror_index is not None:
                        m = self._ror_index.match(name, iso)
                        if m is not None:
                            r = m.ror
                            ror_id, ror_cc = r.ror_id, r.country_code
                            ror_city, ror_lat, ror_lon = r.city, r.lat, r.lon
                            ror_home = r.homepage
                    org_id = make_id("organization", ror_id) if ror_id \
                        else make_id("organization", self.source, name)
                    if org_id not in seen_orgs:
                        seen_orgs.add(org_id)
                        yield Row("organization", {
                            "atlas_id": org_id,
                            "name": name,
                            "ror_id": ror_id,
                            "country_code": ror_cc or iso,
                            "city": ror_city or city,
                            "region": state,
                            "org_type": "nonprofit",
                            "homepage": ror_home,
                            "lat": ror_lat,
                            "lon": ror_lon,
                            "source": self.source,
                            "source_id": ror_id or name,
                            "source_url": grant_url, "as_of": ts,
                        })
                    yield Row("grant_org", {
                        "src_id": grant_id, "dst_id": org_id, "role": "recipient",
                        "source": self.source, "source_id": gid_src,
                        "source_url": grant_url, "as_of": ts,
                    })

                # --- Person (investigator = PI) ---
                inv = g.get("investigator")
                if inv:
                    person_id = make_id("person", self.source, inv)
                    if person_id not in seen_persons:
                        seen_persons.add(person_id)
                        parts = inv.split()
                        yield Row("person", {
                            "atlas_id": person_id,
                            "full_name": inv,
                            "first_name": parts[0] if parts else None,
                            "last_name": parts[-1] if len(parts) > 1 else None,
                            "orcid": None,
                            "openalex_author_id": None,
                            "source": self.source, "source_id": inv,
                            "source_url": grant_url, "as_of": ts,
                        })
                    yield Row("grant_person", {
                        "src_id": grant_id, "dst_id": person_id, "role": "pi",
                        "source": self.source, "source_id": gid_src,
                        "source_url": grant_url, "as_of": ts,
                    })
                    if org_id:
                        yield Row("person_org", {
                            "src_id": person_id, "dst_id": org_id,
                            "role": "affiliation",
                            "source": self.source, "source_id": gid_src,
                            "source_url": grant_url, "as_of": ts,
                        })

                # --- Field (Program / Sub-program) ---
                if prog_label:
                    field_id = make_id("field", self.source, prog_label)
                    if field_id not in seen_fields:
                        seen_fields.add(field_id)
                        yield Row("field", {
                            "atlas_id": field_id,
                            "name": prog_label,
                            "openalex_id": None,
                            "level": "field",
                            "parent_atlas_id": None,
                            "source": self.source, "source_id": prog_label,
                            "source_url": grant_url, "as_of": ts,
                        })
