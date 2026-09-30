from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from atlas.users.directories.base import email_domain_ok

REPO = Path(__file__).resolve().parents[3]
ROR_DOMAINS = REPO / "data" / "processed" / "ror_domains.parquet"
SHARED_HOSTS = ("wikipedia.org", "wikidata.org", "facebook.com", "twitter.com", "x.com", "linkedin.com",
                "google.com", "sites.google.com", "github.io", "wordpress.com", "blogspot.com")


def normalize_ror(ror_id: str) -> str:
    return ror_id.strip().rstrip("/").rsplit("/", 1)[-1].lower()


def host_domain(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "//" in url else "//" + url).hostname
    if not host:
        return None
    host = host.lower().strip(".")
    return host.removeprefix("www.")


def record_domains(record: Mapping) -> list[str]:
    found = [d.lower().strip(".") for d in record.get("domains") or [] if d]
    for link in record.get("links") or []:
        if isinstance(link, Mapping) and link.get("type") not in (None, "website"):
            continue
        value = link.get("value") if isinstance(link, Mapping) else link
        host = host_domain(value)
        if host and not any(host == s or host.endswith("." + s) for s in SHARED_HOSTS):
            found.append(host)
    return sorted(set(found))


@lru_cache(maxsize=1)
def _table(path: str) -> dict[str, tuple[str, ...]]:
    import pandas as pd

    df = pd.read_parquet(path, columns=["ror_id", "domains"])
    return {r: tuple(d) for r, d in zip(df["ror_id"], df["domains"])}


def allowed_domains(ror_id: str, table: Mapping[str, Iterable[str]] | None = None) -> tuple[str, ...]:
    source = table if table is not None else (_table(str(ROR_DOMAINS)) if ROR_DOMAINS.exists() else {})
    return tuple(source.get(normalize_ror(ror_id), ()))


def email_in_domain(email: str | None, ror_id: str, table: Mapping[str, Iterable[str]] | None = None) -> bool:
    return email_domain_ok(email, allowed_domains(ror_id, table))
