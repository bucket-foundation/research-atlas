"""ERC (European Research Council) connector, via CORDIS bulk exports.

ERC grants are administered under the EU framework programmes (Horizon Europe,
H2020) and published in **CORDIS** bulk CSV exports. We reuse the CORDIS
archives already downloaded by a sibling project rather than re-downloading:

    data/raw/cordis/cordis_horizon.zip   (Horizon Europe)
    data/raw/cordis/cordis_h2020.zip     (H2020)

``scripts/ingest_erc.py`` copies those archives into this repo's raw cache from
the sibling project (it never modifies the originals). The connector reads the
CSVs directly from the zip -- no network, fully cached.

ERC projects are identified by ``fundingScheme``:
- H2020:  ERC-STG / ERC-COG / ERC-ADG / ERC-SyG / ERC-POC (+ -LS / -LVG)
- Horizon: HORIZON-ERC / HORIZON-ERC-POC / HORIZON-ERC-SYG

Maps each ERC project to the canonical graph:
- Funder(ERC) -- one fixed funder row (supranational).
- Grant -- the project (EU contribution in EUR -> USD via documented fixed FX).
- Organization -- participants (coordinator = recipient), resolved to ROR.
- Person -- PI. NOTE: the CORDIS *bulk* export does not carry PI personal names
  (only organizations); person rows are therefore not emitted from the bulk
  feed. Resolving PIs requires the per-project CORDIS web record -- a live-fetch
  TODO, deliberately not done here to avoid hammering the portal.
- Field -- euroSciVoc classification (EU's own taxonomy).
- Edges: funder->grant, grant->org(recipient/host), work->field N/A here.

Money: CORDIS amounts are EUR (``ecMaxContribution``). Normalized to USD with a
documented fixed FX (:data:`EUR_TO_USD`, stamped with :data:`FX_AS_OF`).
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Iterable, Iterator

from atlas.connectors.base import Connector
from atlas.ror import RorResolver
from atlas.schema import Row, make_id, now_iso

ERC_CROSSREF_ID = "100010663"  # European Research Council, Crossref Funder Registry
CORDIS_PROJECT_URL_TMPL = "https://cordis.europa.eu/project/id/{}"

# Documented fixed FX. CORDIS reports EUR; normalize to USD with one auditable
# rate stamped on every grant via fx_rate_to_usd + fx_as_of.
EUR_TO_USD = 1.08
FX_AS_OF = "2026-06-01"

# fundingScheme prefixes that identify an ERC grant in CORDIS.
ERC_SCHEME_PREFIXES = ("ERC-", "HORIZON-ERC")

# CSV files inside each CORDIS zip.
PROJECT_CSV = "project.csv"
ORG_CSV = "organization.csv"
SCIVOC_CSV = "euroSciVoc.csv"


def _is_erc(scheme: str) -> bool:
    s = (scheme or "").strip().upper()
    return any(s.startswith(p) for p in ERC_SCHEME_PREFIXES)


def _parse_money(v) -> float | None:
    """CORDIS money is a EUR string. Empty/<=0 -> None (never silent 0)."""
    if v in (None, "", 0, "0"):
        return None
    try:
        val = float(str(v).replace(",", ".").strip()) if "," in str(v) and "." not in str(v) \
            else float(str(v).strip())
    except (ValueError, TypeError):
        return None
    return val if val > 0 else None


def _to_usd(eur: float | None) -> float | None:
    return round(eur * EUR_TO_USD, 2) if eur is not None else None


def _norm_date(s: str | None) -> str | None:
    """CORDIS dates are ISO (YYYY-MM-DD) or empty."""
    if not s:
        return None
    s = s.strip()[:10]
    return s or None


def _status(status: str | None, end: str | None) -> str:
    s = (status or "").strip().upper()
    if s == "SIGNED" or s == "ONGOING":
        return "active"
    if s in ("CLOSED", "TERMINATED"):
        return "completed"
    return "unknown"


def _read_csv_from_zip(zip_path: Path, member: str) -> Iterator[dict]:
    """Stream rows of a CORDIS CSV (semicolon-delimited, quoted) from a zip."""
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(member) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
            yield from csv.DictReader(text, delimiter=";")


class ErcConnector(Connector):
    source = "erc"
    delay = 0.0  # offline: reads CORDIS zips, no network for the bulk feed

    FUNDER_ATLAS_ID = make_id("funder", f"crossref:{ERC_CROSSREF_ID}")

    def __init__(self, *args, resolve_ror: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.resolve_ror = resolve_ror
        self._ror = RorResolver(
            cache_path=self.raw_dir / "_ror_cache.json", http=self.http
        ) if resolve_ror else None

    # ----- fetch ---------------------------------------------------------- #

    def fetch(self, zips: Iterable[Path] | None = None,
              limit: int = 200) -> Iterable[dict]:
        """Yield ERC project records (with joined orgs + fields) from CORDIS zips.

        ``zips`` defaults to every ``cordis_*.zip`` under ``data/raw/cordis/``.
        ``limit`` caps the number of ERC projects (sample-friendly). No network.
        """
        cordis_dir = self.raw_dir
        if zips is None:
            zips = sorted(cordis_dir.glob("cordis_*.zip"))
        else:
            zips = [Path(z) for z in zips]

        emitted = 0
        for zip_path in zips:
            if emitted >= limit:
                break
            if not zip_path.exists():
                continue

            # Index orgs + fields by projectID once per zip (memory-light: we
            # only keep rows for ERC project ids).
            erc_ids: set[str] = set()
            projects: dict[str, dict] = {}
            for row in _read_csv_from_zip(zip_path, PROJECT_CSV):
                if _is_erc(row.get("fundingScheme", "")):
                    pid = row.get("id")
                    if pid:
                        erc_ids.add(pid)
                        projects[pid] = row
                        if len(erc_ids) >= limit - emitted:
                            break

            orgs: dict[str, list[dict]] = {}
            for row in _read_csv_from_zip(zip_path, ORG_CSV):
                pid = row.get("projectID")
                if pid in erc_ids:
                    orgs.setdefault(pid, []).append(row)

            fields: dict[str, list[dict]] = {}
            try:
                for row in _read_csv_from_zip(zip_path, SCIVOC_CSV):
                    pid = row.get("projectID")
                    if pid in erc_ids:
                        fields.setdefault(pid, []).append(row)
            except KeyError:
                pass  # euroSciVoc.csv absent in some archives

            for pid, proj in projects.items():
                if emitted >= limit:
                    break
                yield {
                    "project": proj,
                    "organizations": orgs.get(pid, []),
                    "fields": fields.get(pid, []),
                }
                emitted += 1

    # ----- normalize ------------------------------------------------------ #

    def normalize(self, raw_pages: Iterable[dict]) -> Iterator[Row]:
        ts = now_iso()
        seen_orgs: set[str] = set()
        seen_fields: set[str] = set()
        funder_emitted = False

        for rec in raw_pages:
            proj = rec.get("project", {})
            pid = str(proj.get("id") or "").strip()
            if not pid:
                continue
            proj_url = CORDIS_PROJECT_URL_TMPL.format(pid)

            # --- Funder (ERC), once ---
            if not funder_emitted:
                yield Row("funder", {
                    "atlas_id": self.FUNDER_ATLAS_ID,
                    "name": "European Research Council",
                    "short_name": "ERC",
                    "country_code": None,  # supranational (EU)
                    "funder_type": "supranational",
                    "ror_id": "https://ror.org/0472cxd90",
                    "crossref_funder_id": ERC_CROSSREF_ID,
                    "homepage": "https://erc.europa.eu",
                    "source": self.source, "source_id": "erc",
                    "source_url": "https://erc.europa.eu", "as_of": ts,
                })
                funder_emitted = True

            # --- Grant ---
            eur = _parse_money(proj.get("ecMaxContribution")) \
                or _parse_money(proj.get("totalCost"))
            start = _norm_date(proj.get("startDate"))
            end = _norm_date(proj.get("endDate"))
            grant_id = make_id("grant", self.source, pid)
            yield Row("grant", {
                "atlas_id": grant_id,
                "title": proj.get("title"),
                "abstract": proj.get("objective"),
                "amount_original": eur,
                "currency": "EUR" if eur is not None else None,
                "amount_usd": _to_usd(eur),
                "fx_rate_to_usd": EUR_TO_USD if eur is not None else None,
                "fx_as_of": FX_AS_OF if eur is not None else None,
                "start_date": start,
                "end_date": end,
                "status": _status(proj.get("status"), end),
                "program": proj.get("fundingScheme"),
                "source": self.source, "source_id": pid,
                "source_url": proj_url, "as_of": ts,
            })
            yield Row("funder_grant", {
                "src_id": self.FUNDER_ATLAS_ID, "dst_id": grant_id,
                "role": "awarder",
                "source": self.source, "source_id": pid,
                "source_url": proj_url, "as_of": ts,
            })

            # --- Organizations ---
            for o in rec.get("organizations", []):
                name = (o.get("name") or "").strip()
                if not name:
                    continue
                country = (o.get("country") or "").strip() or None
                role_src = (o.get("role") or "").lower()
                role = "recipient" if role_src == "coordinator" else "host"
                ror = self._ror.resolve(name, country) if self.resolve_ror else None
                ror_id = (ror or {}).get("ror_id")
                org_id = make_id("organization", ror_id) if ror_id \
                    else make_id("organization", self.source, name)
                if org_id not in seen_orgs:
                    seen_orgs.add(org_id)
                    lat, lon = self._geo(o.get("geolocation"))
                    yield Row("organization", {
                        "atlas_id": org_id,
                        "name": name,
                        "ror_id": ror_id,
                        "country_code": (ror or {}).get("country_code") or country,
                        "city": (ror or {}).get("city") or (o.get("city") or None),
                        "region": None,
                        "org_type": self._org_type(o.get("activityType")),
                        "homepage": (ror or {}).get("homepage")
                        or (o.get("organizationURL") or None),
                        "lat": (ror or {}).get("lat") or lat,
                        "lon": (ror or {}).get("lon") or lon,
                        "source": self.source, "source_id": ror_id or name,
                        "source_url": proj_url, "as_of": ts,
                    })
                yield Row("grant_org", {
                    "src_id": grant_id, "dst_id": org_id, "role": role,
                    "source": self.source, "source_id": pid,
                    "source_url": proj_url, "as_of": ts,
                })

            # --- Fields (euroSciVoc) ---
            for f in rec.get("fields", []):
                name = (f.get("euroSciVocTitle") or "").strip()
                if not name:
                    continue
                code = (f.get("euroSciVocCode") or "").strip()
                field_id = make_id("field", self.source, code or name)
                if field_id not in seen_fields:
                    seen_fields.add(field_id)
                    yield Row("field", {
                        "atlas_id": field_id,
                        "name": name,
                        "openalex_id": None,
                        "level": "field",
                        "parent_atlas_id": None,
                        "source": self.source, "source_id": code or name,
                        "source_url": proj_url, "as_of": ts,
                    })

    # ----- helpers -------------------------------------------------------- #

    @staticmethod
    def _geo(geoloc: str | None) -> tuple[float | None, float | None]:
        """CORDIS geolocation is 'lat,lon' or empty."""
        if not geoloc or "," not in geoloc:
            return None, None
        try:
            lat, lon = geoloc.split(",", 1)
            return float(lat), float(lon)
        except ValueError:
            return None, None

    @staticmethod
    def _org_type(activity: str | None) -> str:
        return {
            "HES": "education",      # higher/secondary education
            "REC": "facility",       # research org
            "PRC": "company",        # private for-profit
            "PUB": "government",     # public body
            "OTH": "other",
        }.get((activity or "").strip().upper(), "other")
