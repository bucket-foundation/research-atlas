"""Chan Zuckerberg Initiative (CZI) connector, via the public grants REST API.

CZI's grants page (https://chanzuckerberg.com/grants-ventures/grants/) renders
its grant cards client-side, but the data is served whole by a discoverable
WordPress REST endpoint the page calls:

    https://chanzuckerberg.com/wp-json/czi/v1/grants/

This returns the **entire CZI grants database in one JSON document** (no paging,
no auth): ~5,500 grants since 2018, each with Account Name (recipient), Amount
(USD), Commitment Year, Initiative (CZI's programmatic classification), Grant
Purpose, and the recipient's website. This connector caches that one document
once under ``data/raw/czi/`` (idempotent re-download) and normalizes it offline.

(Historical note: an earlier landscape report listed CZI as "deferred -- behind
an un-discoverable admin-ajax action". That was wrong: the ``czi/v1/grants/``
REST route is plainly visible in the page's network calls and serves the full
database. This connector ingests it.)

Maps each grant to the canonical graph:
- Funder(CZI) -- one fixed funder row (private LLC / philanthropy).
- Grant -- the committed grant. ``Amount`` is USD (1.0 FX, stamped). Only a
  ``Commitment Year`` is published, recorded as a Jan-1 start_date so the by-year
  rollups work; end_date stays None. Unknown amounts stay None (money invariant).
- Organization -- the grantee (Account Name + website), resolved to ROR in the
  cross-source backfill pass (scripts/resolve_ror.py), not here.
- Field -- the grant's Initiative (CZI's own classification).
- Edges: funder->grant(awarder), grant->org(recipient).

People: the CZI feed carries no PI personal names, so no ``person`` rows are
emitted (documented limitation).
"""

from __future__ import annotations

from typing import Iterable, Iterator

from atlas.connectors.base import BROWSER_UA, Connector
from atlas.schema import Row, make_id, now_iso

CZI_CROSSREF_ID = "100014989"  # Chan Zuckerberg Initiative, Crossref Funder Registry
CZI_ROR_ID = "https://ror.org/02qenvm24"
CZI_API_URL = "https://chanzuckerberg.com/wp-json/czi/v1/grants/"
CZI_GRANTS_URL = "https://chanzuckerberg.com/grants-ventures/grants/"

USD_FX = 1.0
FX_AS_OF = "2026-06-01"


def _money(v) -> float | None:
    """Amount is a number already; <=0/empty -> None (money invariant)."""
    if v in (None, "", 0, "0", 0.0):
        return None
    try:
        val = float(v)
    except (ValueError, TypeError):
        return None
    return val if val > 0 else None


def _year(v) -> int | None:
    if not v:
        return None
    s = str(v).strip()[:4]
    return int(s) if s.isdigit() else None


def _flatten(payload) -> list[dict]:
    """The API returns {"grants": [[<record>, ...]]} -- one list, nested once.

    Be tolerant of either {"grants": [[...]]} or {"grants": [...]} so a future
    shape change does not silently drop records.
    """
    grants = (payload or {}).get("grants")
    if not isinstance(grants, list):
        return []
    out: list[dict] = []
    for chunk in grants:
        if isinstance(chunk, list):
            out.extend(x for x in chunk if isinstance(x, dict))
        elif isinstance(chunk, dict):
            out.append(chunk)
    return out


def _initiatives(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


class CziConnector(Connector):
    source = "czi"
    user_agent = BROWSER_UA
    delay = 0.5

    FUNDER_ATLAS_ID = make_id("funder", f"crossref:{CZI_CROSSREF_ID}")

    def __init__(self, *args, resolve_ror: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.resolve_ror = resolve_ror  # backfilled cross-source; off here

    # ----- fetch ---------------------------------------------------------- #

    def fetch(self, **kwargs) -> Iterable[dict]:
        """Download the single grants document once (cached, idempotent)."""
        page_key = "grants_all"
        rec = self.load_raw(page_key)
        if rec is None:
            resp = self.http.get(CZI_API_URL)
            if resp is None:
                return
            try:
                rec = resp.json()
            except ValueError:
                return
            self.cache_raw(page_key, rec)
        yield rec

    # ----- normalize ------------------------------------------------------ #

    def normalize(self, raw_pages: Iterable[dict]) -> Iterator[Row]:
        ts = now_iso()
        seen_orgs: set[str] = set()
        seen_fields: set[str] = set()
        funder_emitted = False

        for page in raw_pages:
            for rec in _flatten(page):
                gid_src = str(rec.get("id") or "").strip()
                f = rec.get("fields") or {}
                if not gid_src or not f:
                    continue

                if not funder_emitted:
                    yield Row("funder", {
                        "atlas_id": self.FUNDER_ATLAS_ID,
                        "name": "Chan Zuckerberg Initiative",
                        "short_name": "CZI",
                        "country_code": "US",
                        "funder_type": "private",
                        "ror_id": CZI_ROR_ID,
                        "crossref_funder_id": CZI_CROSSREF_ID,
                        "homepage": "https://chanzuckerberg.com",
                        "source": self.source, "source_id": "czi",
                        "source_url": CZI_GRANTS_URL, "as_of": ts,
                    })
                    funder_emitted = True

                amount = _money(f.get("Amount"))
                year = _year(f.get("Commitment Year"))
                start = f"{year:04d}-01-01" if year else None
                purpose = (f.get("Grant Purpose") or "").strip() or None
                inits = _initiatives(f.get("Initiative"))
                program = " / ".join(inits) or None
                grant_id = make_id("grant", self.source, gid_src)
                yield Row("grant", {
                    "atlas_id": grant_id,
                    "title": purpose,
                    "abstract": None,
                    "amount_original": amount,
                    "currency": "USD" if amount is not None else None,
                    "amount_usd": amount,
                    "fx_rate_to_usd": USD_FX if amount is not None else None,
                    "fx_as_of": FX_AS_OF if amount is not None else None,
                    "start_date": start,
                    "end_date": None,
                    "status": "unknown",
                    "program": program,
                    "source": self.source, "source_id": gid_src,
                    "source_url": CZI_GRANTS_URL, "as_of": ts,
                })
                yield Row("funder_grant", {
                    "src_id": self.FUNDER_ATLAS_ID, "dst_id": grant_id,
                    "role": "awarder",
                    "source": self.source, "source_id": gid_src,
                    "source_url": CZI_GRANTS_URL, "as_of": ts,
                })

                # --- Organization (grantee / Account Name) ---
                name = (f.get("Account Name") or "").strip()
                if name:
                    homepage = (f.get("Account Website (Plaintext)") or "").strip() \
                        or None
                    org_id = make_id("organization", self.source, name)
                    if org_id not in seen_orgs:
                        seen_orgs.add(org_id)
                        yield Row("organization", {
                            "atlas_id": org_id,
                            "name": name,
                            "ror_id": None,
                            "country_code": None,
                            "city": None,
                            "region": None,
                            "org_type": "nonprofit",
                            "homepage": homepage,
                            "lat": None,
                            "lon": None,
                            "source": self.source, "source_id": name,
                            "source_url": CZI_GRANTS_URL, "as_of": ts,
                        })
                    yield Row("grant_org", {
                        "src_id": grant_id, "dst_id": org_id, "role": "recipient",
                        "source": self.source, "source_id": gid_src,
                        "source_url": CZI_GRANTS_URL, "as_of": ts,
                    })

                # --- Field (Initiative) ---
                for init in inits:
                    field_id = make_id("field", self.source, init)
                    if field_id not in seen_fields:
                        seen_fields.add(field_id)
                        yield Row("field", {
                            "atlas_id": field_id,
                            "name": init,
                            "openalex_id": None,
                            "level": "field",
                            "parent_atlas_id": None,
                            "source": self.source, "source_id": init,
                            "source_url": CZI_GRANTS_URL, "as_of": ts,
                        })
