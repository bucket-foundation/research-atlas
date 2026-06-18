"""Resolve organization names to ROR ids via the public ROR API.

ROR (Research Organization Registry) is the canonical global org identifier we
key :class:`~atlas.schema.Organization` rows on where resolvable. This is a
best-effort, cached, conservative resolver: it only accepts a match when the
ROR ``chosen`` flag is set or the top score clears a confidence threshold, so
we never invent a wrong ROR id (better ``None`` than wrong).
"""

from __future__ import annotations

import json
from pathlib import Path

from atlas.connectors.base import HttpClient

ROR_API = "https://api.ror.org/organizations"


class RorResolver:
    """Cached, conservative name -> ROR resolver."""

    def __init__(self, cache_path: Path | None = None, http: HttpClient | None = None,
                 min_score: float = 0.9):
        self.http = http or HttpClient(delay=0.1)
        self.min_score = min_score
        self.cache_path = cache_path
        self._cache: dict[str, dict | None] = {}
        if cache_path and cache_path.exists():
            self._cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def _save(self):
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache), encoding="utf-8")

    def resolve(self, name: str, country_code: str | None = None) -> dict | None:
        """Return ``{ror_id, name, country_code, city, lat, lon, homepage}`` or None."""
        if not name or not name.strip():
            return None
        key = name.strip().lower()
        if key in self._cache:
            return self._cache[key]

        resp = self.http.get(ROR_API, params={"affiliation": name})
        result = None
        if resp is not None:
            items = resp.json().get("items", [])
            for it in items:
                org = it.get("organization", {})
                if it.get("chosen") or it.get("score", 0) >= self.min_score:
                    result = self._shape(org)
                    break

        self._cache[key] = result
        self._save()
        return result

    @staticmethod
    def _shape(org: dict) -> dict:
        ror_id = org.get("id", "")
        country = (org.get("country") or {}).get("country_code")
        # ROR v1 nests geo under addresses; be defensive across schema versions.
        addr = (org.get("addresses") or [{}])[0]
        geo = addr.get("geonames_city") or {}
        return {
            "ror_id": ror_id,
            "name": org.get("name"),
            "country_code": country,
            "city": addr.get("city") or geo.get("city"),
            "lat": addr.get("lat"),
            "lon": addr.get("lng"),
            "homepage": RorResolver._homepage(org.get("links")),
        }

    @staticmethod
    def _homepage(links) -> str | None:
        """Extract the website URL string from ROR ``links``.

        ROR v1 returns ``links`` as a list of URL strings; ROR v2 returns a list
        of ``{"type": "website"|"wikipedia", "value": <url>}`` dicts. Always
        return a plain string (or None) so the ``homepage`` column stays scalar.
        """
        for link in (links or []):
            if isinstance(link, str):
                return link
            if isinstance(link, dict):
                if link.get("type") == "website" and link.get("value"):
                    return link["value"]
        # fall back to the first dict value if no explicit website type
        for link in (links or []):
            if isinstance(link, dict) and link.get("value"):
                return link["value"]
        return None
