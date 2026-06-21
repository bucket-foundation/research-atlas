"""CORDIS (EU) full connector -- ALL framework-programme projects.

Source: CORDIS bulk CSV exports for Horizon Europe + H2020, already downloaded
into ``data/raw/cordis/`` (``cordis_horizon.zip``, ``cordis_h2020.zip``). This is
the **full** EU funding feed: every ``fundingScheme`` (ERC, MSCA, RIA, IA, CSA,
EIC, …), every participant organization, every euroSciVoc field -- not just the
ERC slice that :mod:`atlas.connectors.erc` carves out.

Scale: ~56k projects, ~300k organization-participations, ~157k field links. The
connector is a **streaming, offline** reader -- it reads the CSVs straight from
the zip in a single forward pass per file and yields canonical
:class:`~atlas.schema.Row` objects, so it pairs with
:class:`atlas.bulkwrite.BulkWriter` for memory-bounded ingestion.

Money: CORDIS reports the EC contribution in EUR (``ecMaxContribution``);
normalized to USD with one documented, auditable fixed FX
(:data:`EUR_TO_USD` stamped via ``fx_rate_to_usd`` + ``fx_as_of``). Unknown money
stays ``None`` (never a silent 0).

Funder model: the **European Commission** is the awarding funder (one row), with
the specific framework programme (Horizon Europe / H2020) recorded on the grant's
``program`` together with the ``fundingScheme``. The ERC scheme is *also* tagged
with the ERC funder so ERC grants are attributable to the ERC specifically.

People: the CORDIS bulk export carries organizations but **not** PI personal
names, so no ``person`` rows are emitted from this feed (documented limitation;
PI resolution needs the per-project CORDIS web record).
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

# European Commission, Crossref Funder Registry; ERC is a sub-funder.
EC_CROSSREF_ID = "100011102"
ERC_CROSSREF_ID = "100010663"
CORDIS_PROJECT_URL_TMPL = "https://cordis.europa.eu/project/id/{}"

# Documented fixed FX. CORDIS reports EUR; normalize to USD with one auditable
# rate stamped on every grant via fx_rate_to_usd + fx_as_of.
EUR_TO_USD = 1.08
FX_AS_OF = "2026-06-01"

PROJECT_CSV = "project.csv"
ORG_CSV = "organization.csv"
SCIVOC_CSV = "euroSciVoc.csv"

ERC_SCHEME_PREFIXES = ("ERC-", "HORIZON-ERC")


def _is_erc(scheme: str) -> bool:
    s = (scheme or "").strip().upper()
    return any(s.startswith(p) for p in ERC_SCHEME_PREFIXES)


def _parse_money(v) -> float | None:
    """CORDIS money is a EUR string, sometimes comma-decimal. <=0/empty -> None."""
    if v in (None, "", 0, "0"):
        return None
    s = str(v).strip()
    # comma-decimal like "217965,12" (no dot) -> dot-decimal
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        val = float(s)
    except (ValueError, TypeError):
        return None
    return val if val > 0 else None


def _to_usd(eur: float | None) -> float | None:
    return round(eur * EUR_TO_USD, 2) if eur is not None else None


def _norm_date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()[:10]
    return s or None


def _status(status: str | None, end: str | None) -> str:
    s = (status or "").strip().upper()
    if s in ("SIGNED", "ONGOING"):
        return "active"
    if s in ("CLOSED", "TERMINATED"):
        return "completed"
    return "unknown"


def _resolve_member(zip_path: Path, member: str) -> str:
    """Find a CSV member inside a CORDIS zip, tolerating layout differences.

    The Horizon/H2020/FP7 bulk zips store ``project.csv`` at the root; the FP6
    zip nests them under ``csv/`` (``csv/project.csv``). Match by basename so the
    same connector reads every framework programme's export. Raises ``KeyError``
    if no member ends with the requested basename.
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    if member in names:
        return member
    base = member.rsplit("/", 1)[-1]
    for n in names:
        if n.rsplit("/", 1)[-1] == base:
            return n
    raise KeyError(f"{member!r} not found in {zip_path.name}")


def _read_csv_from_zip(zip_path: Path, member: str) -> Iterator[dict]:
    """Stream rows of a CORDIS CSV (semicolon-delimited, quoted) from a zip."""
    resolved = _resolve_member(zip_path, member)
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(resolved) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
            # CORDIS objective/keywords can be very long; lift the field cap.
            reader = csv.DictReader(text, delimiter=";")
            yield from reader


def _org_type(activity: str | None) -> str:
    return {
        "HES": "education",
        "REC": "facility",
        "PRC": "company",
        "PUB": "government",
        "OTH": "other",
    }.get((activity or "").strip().upper(), "other")


def _geo(geoloc: str | None) -> tuple[float | None, float | None]:
    if not geoloc or "," not in geoloc:
        return None, None
    try:
        lat, lon = geoloc.split(",", 1)
        return float(lat), float(lon)
    except ValueError:
        return None, None


class CordisConnector(Connector):
    """Full CORDIS connector: every framework-programme project.

    Designed for streaming bulk ingestion. ``iter_rows()`` yields canonical Rows
    across all projects in the given zips without ever holding all projects in
    memory -- it indexes the (much smaller, per-project) org + field rows by
    project id in a forward pass, then joins while streaming projects.

    ROR resolution is OFF by default at full scale (one network call per unique
    org name does not scale to ~100k unique orgs). Org rows still get a stable
    surrogate id from (source, name); ROR can be backfilled later from a bulk
    ROR dump keyed on the same names.
    """

    source = "cordis"
    delay = 0.0  # offline: reads zips, no network for the bulk feed

    EC_FUNDER_ATLAS_ID = make_id("funder", f"crossref:{EC_CROSSREF_ID}")
    ERC_FUNDER_ATLAS_ID = make_id("funder", f"crossref:{ERC_CROSSREF_ID}")

    def __init__(self, *args, resolve_ror: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.resolve_ror = resolve_ror
        self._ror = RorResolver(
            cache_path=self.raw_dir / "_ror_cache.json", http=self.http
        ) if resolve_ror else None

    # The base class is abstract; we satisfy fetch/normalize but the real entry
    # point for scale is iter_rows() (driven by the ingest script + BulkWriter).
    def fetch(self, **kwargs) -> Iterable[dict]:  # pragma: no cover - unused at scale
        return []

    def normalize(self, raw_pages: Iterable[dict]) -> Iterator[Row]:  # pragma: no cover
        yield from ()

    # ----- the scale entry point ------------------------------------------ #

    def iter_rows(self, zip_path: Path, limit: int | None = None) -> Iterator[Row]:
        """Yield canonical Rows for every project in one CORDIS zip.

        Memory: indexes organization + euroSciVoc rows by projectID (these are
        small per project), then streams projects and joins. Two auxiliary
        indexes for one zip fit comfortably in RAM (~300k small dicts worst case).
        """
        ts = now_iso()

        # Index orgs by projectID.
        orgs: dict[str, list[dict]] = {}
        for o in _read_csv_from_zip(zip_path, ORG_CSV):
            pid = o.get("projectID")
            if pid:
                orgs.setdefault(pid, []).append(o)

        # Index euroSciVoc fields by projectID (optional file).
        fields: dict[str, list[dict]] = {}
        try:
            for f in _read_csv_from_zip(zip_path, SCIVOC_CSV):
                pid = f.get("projectID")
                if pid:
                    fields.setdefault(pid, []).append(f)
        except KeyError:
            pass

        # Funders, emitted once.
        yield Row("funder", {
            "atlas_id": self.EC_FUNDER_ATLAS_ID,
            "name": "European Commission",
            "short_name": "EC",
            "country_code": None,
            "funder_type": "supranational",
            "ror_id": "https://ror.org/00k4n6c32",
            "crossref_funder_id": EC_CROSSREF_ID,
            "homepage": "https://commission.europa.eu",
            "source": self.source, "source_id": "ec",
            "source_url": "https://cordis.europa.eu", "as_of": ts,
        })
        yield Row("funder", {
            "atlas_id": self.ERC_FUNDER_ATLAS_ID,
            "name": "European Research Council",
            "short_name": "ERC",
            "country_code": None,
            "funder_type": "supranational",
            "ror_id": "https://ror.org/0472cxd90",
            "crossref_funder_id": ERC_CROSSREF_ID,
            "homepage": "https://erc.europa.eu",
            "source": self.source, "source_id": "erc",
            "source_url": "https://erc.europa.eu", "as_of": ts,
        })

        seen_orgs: set[str] = set()
        seen_fields: set[str] = set()
        n = 0
        for proj in _read_csv_from_zip(zip_path, PROJECT_CSV):
            if limit is not None and n >= limit:
                break
            pid = str(proj.get("id") or "").strip()
            if not pid:
                continue
            n += 1
            proj_url = CORDIS_PROJECT_URL_TMPL.format(pid)
            scheme = proj.get("fundingScheme") or ""
            framework = proj.get("frameworkProgramme") or ""

            eur = _parse_money(proj.get("ecMaxContribution")) \
                or _parse_money(proj.get("totalCost"))
            start = _norm_date(proj.get("startDate"))
            end = _norm_date(proj.get("endDate"))
            grant_id = make_id("grant", self.source, pid)
            program = " / ".join(p for p in [framework, scheme] if p) or None
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
                "program": program,
                "source": self.source, "source_id": pid,
                "source_url": proj_url, "as_of": ts,
            })
            # Awarder: EC, plus ERC when the scheme is an ERC scheme.
            yield Row("funder_grant", {
                "src_id": self.EC_FUNDER_ATLAS_ID, "dst_id": grant_id,
                "role": "awarder",
                "source": self.source, "source_id": pid,
                "source_url": proj_url, "as_of": ts,
            })
            if _is_erc(scheme):
                yield Row("funder_grant", {
                    "src_id": self.ERC_FUNDER_ATLAS_ID, "dst_id": grant_id,
                    "role": "awarder",
                    "source": self.source, "source_id": pid,
                    "source_url": proj_url, "as_of": ts,
                })

            # Organizations (every participant).
            for o in orgs.get(pid, []):
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
                    lat, lon = _geo(o.get("geolocation"))
                    yield Row("organization", {
                        "atlas_id": org_id,
                        "name": name,
                        "ror_id": ror_id,
                        "country_code": (ror or {}).get("country_code") or country,
                        "city": (ror or {}).get("city") or (o.get("city") or None),
                        "region": (o.get("nutsCode") or None),
                        "org_type": _org_type(o.get("activityType")),
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

            # Fields (euroSciVoc), deduped on code.
            for f in fields.get(pid, []):
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
