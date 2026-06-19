"""Bill & Melinda Gates Foundation connector, via the Committed Grants CSV.

The Gates Foundation publishes its **entire committed-grants database** as one
flat CSV download (recipient, purpose, division, date committed, duration,
amount committed, grantee city/state/country, region served, topic). The CSV is
the bulk, machine-readable form of the searchable "Committed Grants" database at
https://www.gatesfoundation.org/about/committed-grants .

Canonical CSV: https://www.gatesfoundation.org/-/media/files/bmgf-grants.csv
(updated monthly; ~17 MB; the first physical line is a "Updated <date>" banner,
the second line is the real header).

This is an **offline-after-download** connector: :meth:`fetch` downloads the CSV
once into ``data/raw/gates/`` (resumable -- a present file is reused), and
:meth:`normalize` parses it row by row. No per-row network.

Maps each grant to the canonical graph:
- Funder(Gates) -- one fixed funder row (private foundation).
- Grant -- the committed grant. ``AMOUNT COMMITTED`` is already USD (1.0 FX,
  stamped). ``DATE COMMITTED`` (YYYY-MM) -> start_date; + DURATION months ->
  end_date. Unknown amounts stay None (money invariant).
- Organization -- the grantee, with city/state/country, resolved to ROR via the
  bulk matcher (off by default at the connector; resolved in a backfill pass).
- Field -- the grant's TOPIC (Gates's own programmatic classification).
- Edges: funder->grant(awarder), grant->org(recipient).

People: the Gates CSV carries no PI personal names, so no ``person`` rows are
emitted (documented limitation).
"""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

from atlas.connectors.base import BROWSER_UA, Connector
from atlas.ror_bulk import RorIndex
from atlas.schema import Row, make_id, now_iso

GATES_CROSSREF_ID = "100000865"  # Bill & Melinda Gates Foundation, Crossref
GATES_CSV_URL = "https://www.gatesfoundation.org/-/media/files/bmgf-grants.csv"
GATES_DB_URL = "https://www.gatesfoundation.org/about/committed-grants"
GATES_ROR_ID = "https://ror.org/0456r8d26"

# Gates reports USD already; FX is the identity, stamped for auditability.
USD_FX = 1.0
FX_AS_OF = "2026-06-01"

# Map Gates "GRANTEE COUNTRY" free-text to ISO-3166 alpha-2 for ROR gating.
_COUNTRY_TO_ISO = {
    "united states": "US", "united states of america": "US", "usa": "US",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB",
    "switzerland": "CH", "bangladesh": "BD", "ethiopia": "ET", "india": "IN",
    "kenya": "KE", "nigeria": "NG", "south africa": "ZA", "canada": "CA",
    "germany": "DE", "france": "FR", "netherlands": "NL", "australia": "AU",
    "china": "CN", "japan": "JP", "tanzania": "TZ", "uganda": "UG",
    "ghana": "GH", "senegal": "SN", "malawi": "MW", "zambia": "ZM",
    "mozambique": "MZ", "pakistan": "PK", "indonesia": "ID", "brazil": "BR",
    "belgium": "BE", "sweden": "SE", "denmark": "DK", "norway": "NO",
    "italy": "IT", "spain": "ES", "ireland": "IE", "mexico": "MX",
    "vietnam": "VN", "thailand": "TH", "philippines": "PH", "egypt": "EG",
    "rwanda": "RW", "burkina faso": "BF", "mali": "ML", "niger": "NE",
    "democratic republic of the congo": "CD", "cote d'ivoire": "CI",
    "zimbabwe": "ZW", "cameroon": "CM", "nepal": "NP", "myanmar": "MM",
    "new zealand": "NZ", "austria": "AT", "finland": "FI", "portugal": "PT",
    "singapore": "SG", "south korea": "KR", "korea, republic of": "KR",
    "israel": "IL", "colombia": "CO", "peru": "PE", "argentina": "AR",
    "chile": "CL",
}

# Gates CSV column names (line 2 of the file).
COL_ID = "GRANT ID"
COL_GRANTEE = "GRANTEE"
COL_PURPOSE = "PURPOSE"
COL_DIVISION = "DIVISION"
COL_DATE = "DATE COMMITTED"
COL_DURATION = "DURATION (MONTHS)"
COL_AMOUNT = "AMOUNT COMMITTED"
COL_WEBSITE = "GRANTEE WEBSITE"
COL_CITY = "GRANTEE CITY"
COL_STATE = "GRANTEE STATE"
COL_COUNTRY = "GRANTEE COUNTRY"
COL_REGION = "REGION SERVED"
COL_TOPIC = "TOPIC"


def _iso_country(country: str | None) -> str | None:
    if not country:
        return None
    return _COUNTRY_TO_ISO.get(country.strip().lower())


def _parse_amount(v) -> float | None:
    """Gates amount is a plain USD number (sometimes with commas). <=0 -> None."""
    if v in (None, ""):
        return None
    s = str(v).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        val = float(s)
    except (ValueError, TypeError):
        return None
    return val if val > 0 else None


def _parse_dates(date_committed: str | None, duration_months: str | None
                 ) -> tuple[str | None, str | None]:
    """'2021-02' + duration months -> (start ISO, end ISO). Robust to junk."""
    start = end = None
    if date_committed:
        s = str(date_committed).strip()
        if len(s) >= 7 and s[:4].isdigit() and s[5:7].isdigit():
            y, m = int(s[:4]), int(s[5:7])
            if 1 <= m <= 12:
                start = f"{y:04d}-{m:02d}-01"
                months = None
                if duration_months not in (None, ""):
                    try:
                        months = int(float(str(duration_months).strip()))
                    except (ValueError, TypeError):
                        months = None
                if months and months > 0:
                    total = (y * 12 + (m - 1)) + months
                    ey, em = divmod(total, 12)
                    end = date(ey, em + 1, 1).isoformat()
    return start, end


def parse_csv_text(text: str) -> Iterator[dict]:
    """Yield grant dicts from the Gates CSV text (skips the banner line)."""
    # Gates "PURPOSE" free-text can exceed the default 128 KB csv field cap.
    if csv.field_size_limit() < 10_000_000:
        csv.field_size_limit(10_000_000)
    buf = io.StringIO(text)
    first = buf.readline()
    # The real file leads with a quoted "Updated <date>" banner before the header.
    # If the first line is not the header, the next line is. Detect the header.
    if COL_ID in first:
        buf.seek(0)
    reader = csv.DictReader(buf)
    yield from reader


class GatesConnector(Connector):
    source = "gates"
    user_agent = BROWSER_UA
    delay = 0.5

    FUNDER_ATLAS_ID = make_id("funder", f"crossref:{GATES_CROSSREF_ID}")

    def __init__(self, *args, ror_index: RorIndex | None = None,
                 resolve_ror: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.resolve_ror = resolve_ror
        self._ror_index = ror_index

    # ----- fetch ---------------------------------------------------------- #

    def fetch(self, csv_url: str = GATES_CSV_URL, **kwargs) -> Iterable[dict]:
        """Download the Gates committed-grants CSV once (cached). Resumable."""
        page_key = "bmgf_grants_csv"
        rec = self.load_raw(page_key)
        if rec is None:
            resp = self.http.get(csv_url)
            if resp is None:
                return
            rec = {"url": csv_url, "csv": resp.text}
            self.cache_raw(page_key, rec)
        yield rec

    # ----- normalize ------------------------------------------------------ #

    def normalize(self, raw_pages: Iterable[dict]) -> Iterator[Row]:
        ts = now_iso()
        seen_orgs: set[str] = set()
        seen_fields: set[str] = set()
        funder_emitted = False

        for rec in raw_pages:
            text = rec.get("csv") or ""
            if not text:
                continue
            for grant in parse_csv_text(text):
                gid_src = (grant.get(COL_ID) or "").strip()
                if not gid_src:
                    continue

                if not funder_emitted:
                    yield Row("funder", {
                        "atlas_id": self.FUNDER_ATLAS_ID,
                        "name": "Bill & Melinda Gates Foundation",
                        "short_name": "Gates Foundation",
                        "country_code": "US",
                        "funder_type": "nonprofit",
                        "ror_id": GATES_ROR_ID,
                        "crossref_funder_id": GATES_CROSSREF_ID,
                        "homepage": "https://www.gatesfoundation.org",
                        "source": self.source, "source_id": "gates",
                        "source_url": GATES_DB_URL, "as_of": ts,
                    })
                    funder_emitted = True

                grant_url = f"{GATES_DB_URL}?q={gid_src}"
                amount = _parse_amount(grant.get(COL_AMOUNT))
                start, end = _parse_dates(grant.get(COL_DATE),
                                          grant.get(COL_DURATION))
                status = "unknown"
                if end:
                    try:
                        status = ("completed"
                                  if date.fromisoformat(end) < date.today()
                                  else "active")
                    except ValueError:
                        status = "unknown"
                grant_id = make_id("grant", self.source, gid_src)
                division = (grant.get(COL_DIVISION) or "").strip() or None
                topic = (grant.get(COL_TOPIC) or "").strip() or None
                program = " / ".join(p for p in [division, topic] if p) or None
                yield Row("grant", {
                    "atlas_id": grant_id,
                    "title": (grant.get(COL_PURPOSE) or "").strip() or None,
                    "abstract": None,
                    "amount_original": amount,
                    "currency": "USD" if amount is not None else None,
                    "amount_usd": amount,  # already USD
                    "fx_rate_to_usd": USD_FX if amount is not None else None,
                    "fx_as_of": FX_AS_OF if amount is not None else None,
                    "start_date": start,
                    "end_date": end,
                    "status": status,
                    "program": program,
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
                name = (grant.get(COL_GRANTEE) or "").strip()
                if name:
                    country = (grant.get(COL_COUNTRY) or "").strip() or None
                    iso = _iso_country(country)
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
                            "city": ror_city or (grant.get(COL_CITY) or "").strip()
                            or None,
                            "region": (grant.get(COL_STATE) or "").strip() or None,
                            "org_type": "nonprofit",
                            "homepage": ror_home
                            or (grant.get(COL_WEBSITE) or "").strip() or None,
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

                # --- Field (TOPIC) ---
                if topic:
                    field_id = make_id("field", self.source, topic)
                    if field_id not in seen_fields:
                        seen_fields.add(field_id)
                        yield Row("field", {
                            "atlas_id": field_id,
                            "name": topic,
                            "openalex_id": None,
                            "level": "field",
                            "parent_atlas_id": None,
                            "source": self.source, "source_id": topic,
                            "source_url": grant_url, "as_of": ts,
                        })
