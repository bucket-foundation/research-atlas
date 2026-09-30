from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import sys
import time
import urllib.robotparser
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from atlas.users.contacts import CONTACT_CACHE, UA, _is_acceptable_email

OFFICIAL_CACHE = CONTACT_CACHE / "official"
EMAIL_SOURCE = "official_directory"
LICENCE = "institution-copyright"
ADAPTER_VERSION = "0.3"
RUN_ONLY = ("dropped_emails", "org_mailbox")
JOIN_MATCH_TIER = "T2"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass
class FacultyRecord:
    ror_id: str
    slug: str
    name: str
    title: str | None
    departments: list[str]
    school: str | None
    research_areas: str | None
    email: str | None
    profile_url: str
    fetched_at: str
    html_sha256: str
    email_source: str | None = None
    email_source_url: str | None = None
    email_as_of: str | None = None
    dropped_emails: list[str] = field(default_factory=list)
    source_kind: str = "profile"
    org_mailbox: str | None = None
    source: str = EMAIL_SOURCE
    source_id: str | None = None
    source_url: str | None = None
    as_of: str | None = None
    match_tier: str | None = None
    match_candidates: int | None = None
    licence: str = LICENCE
    storage: str = "link_only"
    retrieved_by: str | None = None

    def __post_init__(self) -> None:
        self.source_id = self.source_id or self.slug
        self.source_url = self.source_url or self.profile_url
        self.as_of = self.as_of or self.fetched_at

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k not in RUN_ONLY}


def email_domain_ok(email: str | None, domains: Iterable[str]) -> bool:
    if not email or "@" not in email:
        return False
    host = email.rsplit("@", 1)[1].lower().strip(".")
    for d in domains:
        d = d.lower().strip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def accept_email(record: FacultyRecord, candidate: str | None, domains: Iterable[str]) -> FacultyRecord:
    if not candidate:
        return record
    email = candidate.strip().rstrip(".").lower()
    if email_domain_ok(email, domains) and _is_acceptable_email(email):
        record.email = email
        record.email_source = EMAIL_SOURCE
        record.email_source_url = record.profile_url
        record.email_as_of = record.fetched_at
    else:
        record.dropped_emails.append(email)
    return record


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"<br\s*/?>|</p>|</li>", "; ", value, flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s*;\s*(;\s*)*", "; ", re.sub(r"[ \t\r\n]+", " ", text)).strip(" ;")
    return text or None


def next_data(page: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


class RobotsDenied(Exception):
    pass


class BudgetExhausted(Exception):
    pass


RETRY_STATUS = (429, 502, 503, 504, 599)
CHALLENGE_RE = re.compile(r"<title>\s*(Just a moment|Attention Required|Access denied)", re.IGNORECASE)


@dataclass
class Page:
    url: str
    status: int
    text: str
    fetched_at: str
    sha256: str
    from_cache: bool
    invalid: str | None = None
    source: str = "official_directory"
    source_url: str | None = None
    crawl_id: str | None = None


class PoliteFetcher:
    def __init__(self, cache_dir: Path, *, delay: float = 1.0, max_age_days: int = 30,
                 get: Callable[[str], tuple[int, str]] | None = None,
                 now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep, attempts: int = 3,
                 budget: int | None = None, log: Callable[[str], None] | None = None,
                 validator: Callable[[str, str], str | None] | None = None,
                 archive: Callable[[str], object] | None = None) -> None:
        self.cache_dir = cache_dir
        self.delay = delay
        self.max_age = timedelta(days=max_age_days)
        self._get = get or _requests_get
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.requests = 0
        self.attempts = attempts
        self.errors: list[str] = []
        self.budget = budget
        self.validator = validator
        self.archive = archive
        self.invalid = 0
        self.archived = 0
        self.log = log
        self.challenged = 0
        self._host_delay: dict[str, float] = {}

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / f"{key}.html.gz", self.cache_dir / f"{key}.json"

    def _wait(self, host: str) -> None:
        delay = max(self.delay, self._host_delay.get(host, 0.0))
        last = self._last.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < delay:
                self._sleep(delay - gap)
        self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            self._wait(p.netloc)
            try:
                status, body = self._get(base + "/robots.txt")
            except OSError:
                status, body = 599, ""
            self.requests += 1
            if status >= 500:
                rp.disallow_all = True
            elif status in (401, 403):
                rp.disallow_all = True
            elif status >= 400:
                rp.allow_all = True
            else:
                rp.parse(body.splitlines())
                cd = rp.crawl_delay(UA)
                if cd:
                    self._host_delay[p.netloc] = min(float(cd), 30.0)
            self._robots[base] = rp
        return rp.can_fetch(UA, url)

    def sitemaps(self, url: str) -> list[str]:
        p = urlparse(url)
        self.allowed(url)
        rp = self._robots.get(f"{p.scheme}://{p.netloc}")
        return list((rp.site_maps() if rp else None) or [])

    def fetch(self, url: str) -> Page:
        body_path, meta_path = self._paths(url)
        if body_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text())
            fetched = datetime.fromisoformat(meta["fetched_at"].replace("Z", "+00:00"))
            age = self._now() - fetched
            if age < self.max_age:
                if age > timedelta(days=1) and not self.allowed(url):
                    raise RobotsDenied(url)
                text = gzip.decompress(body_path.read_bytes()).decode()
                return Page(url, meta["status"], text, meta["fetched_at"], meta["sha256"], True, meta.get("invalid"),
                            meta.get("source", "official_directory"), meta.get("source_url"),
                            (meta.get("provenance") or {}).get("crawl_id"))
        if not self.allowed(url):
            raise RobotsDenied(url)
        if self.budget is not None and self.requests >= self.budget:
            raise BudgetExhausted(url)
        status, text = 599, ""
        for attempt in range(self.attempts):
            self._wait(urlparse(url).netloc)
            self.requests += 1
            started = time.monotonic()
            try:
                status, text = self._get(url)
            except OSError as exc:
                self.errors.append(f"{url}: {type(exc).__name__}")
                status, text = 599, ""
            if self.log:
                self.log(f"{status} {time.monotonic() - started:.1f}s {url}")
            if status not in RETRY_STATUS:
                break
            self._sleep(self.delay * 5 * (attempt + 1))
        fetched_at = self._now().strftime("%Y-%m-%dT%H:%M:%SZ")
        if status in (403, 503) and CHALLENGE_RE.search(text[:4000]):
            self.challenged += 1
        source, source_url, crawl_id = "official_directory", None, None
        if self.archive is not None and _needs_archive(status, text):
            rec = self.archive(url)
            if rec is not None:
                self.archived += 1
                status, text = 200, rec.html
                source_url, crawl_id = rec.location, rec.crawl_id
        digest = hashlib.sha256(text.encode()).hexdigest()
        invalid = self.validator(text, url) if self.validator and status == 200 else None
        if invalid:
            self.invalid += 1
        if status == 200:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            body_path.write_bytes(gzip.compress(text.encode()))
            meta = {"url": url, "status": status, "fetched_at": fetched_at, "sha256": digest, "source": source}
            if source_url:
                meta.update(source_url=source_url, provenance={"crawl_id": crawl_id, "archive": "commoncrawl"})
            if invalid:
                meta["invalid"] = invalid
            meta_path.write_text(json.dumps(meta))
        return Page(url, status, text, fetched_at, digest, False, invalid, source, source_url, crawl_id)


def _needs_archive(status: int, text: str) -> bool:
    from atlas.users.extract.commoncrawl import needs_archive

    return needs_archive(status, text)


def _better(new: FacultyRecord, old: FacultyRecord) -> bool:
    rank = lambda r: (r.email is not None, r.source_kind == "profile")
    return rank(new) > rank(old)


def drop_shared_emails(records: list[FacultyRecord]) -> int:
    owners: dict[str, int] = {}
    for r in records:
        if r.email:
            owners[r.email] = owners.get(r.email, 0) + 1
    shared = {e for e, n in owners.items() if n > 1}
    for r in records:
        if r.email in shared:
            r.dropped_emails.append(r.email)
            r.email = r.email_source = r.email_source_url = r.email_as_of = None
    return len(shared)


def _requests_get(url: str) -> tuple[int, str]:
    import requests

    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    return r.status_code, r.text


class DirectoryAdapter(Protocol):
    ror_id: str
    domains: tuple[str, ...]

    def seeds(self, fetcher: PoliteFetcher) -> list[str]: ...

    def parse(self, page: Page) -> list[FacultyRecord]: ...


def retrieved_by(adapter) -> str:
    name = getattr(adapter, "platform", None) or type(adapter).__name__
    return f"{name}/{getattr(adapter, 'version', ADAPTER_VERSION)}"


def crawl(adapter: DirectoryAdapter, fetcher: PoliteFetcher, suppression=None) -> tuple[list[FacultyRecord], dict]:
    stats = {"seeds": 0, "fetched": 0, "cached": 0, "robots_denied": 0, "http_errors": 0,
             "records": 0, "emails": 0, "dropped_emails": 0, "budget_exhausted": False,
             "parse_errors": 0, "parse_error_urls": [], "suppressed_seeds": 0, "suppressed_records": 0}
    seeds = adapter.seeds(fetcher)
    stats["seeds"] = len(seeds)
    out: dict[str, FacultyRecord] = {}
    hints = getattr(adapter, "seed_people", {}) or {}
    for url in seeds:
        hint = hints.get(url)
        if suppression is not None and suppression.blocks_url(url):
            stats["suppressed_seeds"] += 1
            continue
        if suppression is not None and hint and suppression.blocks(name=hint.get("name"), ror_id=adapter.ror_id,
                                                                   orcid=hint.get("orcid"), email=hint.get("email")):
            stats["suppressed_seeds"] += 1
            continue
        try:
            page = fetcher.fetch(url)
        except RobotsDenied:
            stats["robots_denied"] += 1
            continue
        except BudgetExhausted:
            stats["budget_exhausted"] = True
            break
        stats["cached" if page.from_cache else "fetched"] += 1
        if page.status != 200:
            stats["http_errors"] += 1
            continue
        if page.invalid:
            stats["invalid_pages"] = stats.get("invalid_pages", 0) + 1
            continue
        try:
            parsed = adapter.parse(page)
        except Exception as exc:  # noqa: BLE001
            stats["parse_errors"] += 1
            stats["parse_error_urls"].append(f"{url}: {type(exc).__name__}: {exc}")
            print(f"parse error {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        for rec in parsed:
            rec.retrieved_by = retrieved_by(adapter)
            if suppression is not None and suppression.blocks_record(rec):
                stats["suppressed_records"] += 1
                continue
            prior = out.get(rec.slug)
            if prior is None or _better(rec, prior):
                out[rec.slug] = rec
    records = sorted(out.values(), key=lambda r: r.slug)
    stats["shared_emails"] = drop_shared_emails(records)
    stats["records"] = len(records)
    stats["emails"] = sum(1 for r in records if r.email)
    stats["dropped_emails"] = sum(len(r.dropped_emails) for r in records)
    stats["legacy_tombstones"] = len(getattr(suppression, "legacy", ()) or ())
    stats["org_mailboxes"] = sum(1 for r in records if r.org_mailbox)
    stats["network_errors"] = len(fetcher.errors)
    stats["challenged"] = fetcher.challenged
    stats["requests"] = fetcher.requests
    stats["departments"] = len({d for r in records for d in r.departments})
    stats["from_profile"] = sum(1 for r in records if r.source_kind == "profile")
    stats["from_listing"] = sum(1 for r in records if r.source_kind == "listing")
    return records, stats
