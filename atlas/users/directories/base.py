from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.robotparser
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol
from urllib.parse import urlparse

from atlas.users.contacts import CONTACT_CACHE, UA, _is_acceptable_email

OFFICIAL_CACHE = CONTACT_CACHE / "official"
EMAIL_SOURCE = "official_directory"
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

    def as_dict(self) -> dict:
        return asdict(self)


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
    text = re.sub(r"<br\s*/?>|</p>|</li>", "; ", value, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s*;\s*(;\s*)*", "; ", re.sub(r"[ \t\r\n]+", " ", text)).strip(" ;")
    return text or None


def next_data(page: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


class RobotsDenied(Exception):
    pass


@dataclass
class Page:
    url: str
    status: int
    text: str
    fetched_at: str
    sha256: str
    from_cache: bool


class PoliteFetcher:
    def __init__(self, cache_dir: Path, *, delay: float = 1.0, max_age_days: int = 30,
                 get: Callable[[str], tuple[int, str]] | None = None,
                 now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep, attempts: int = 3) -> None:
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

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / f"{key}.html", self.cache_dir / f"{key}.json"

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < self.delay:
                self._sleep(self.delay - gap)
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
            elif status >= 400:
                rp.allow_all = True
            else:
                rp.parse(body.splitlines())
            self._robots[base] = rp
        return rp.can_fetch(UA, url)

    def fetch(self, url: str) -> Page:
        body_path, meta_path = self._paths(url)
        if body_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text())
            fetched = datetime.fromisoformat(meta["fetched_at"].replace("Z", "+00:00"))
            if self._now() - fetched < self.max_age:
                text = body_path.read_text()
                return Page(url, meta["status"], text, meta["fetched_at"], meta["sha256"], True)
        if not self.allowed(url):
            raise RobotsDenied(url)
        status, text = 599, ""
        for attempt in range(self.attempts):
            self._wait(urlparse(url).netloc)
            self.requests += 1
            try:
                status, text = self._get(url)
            except OSError as exc:
                self.errors.append(f"{url}: {type(exc).__name__}")
                status, text = 599, ""
            if status < 500 and status != 429:
                break
            self._sleep(self.delay * 5 * (attempt + 1))
        fetched_at = self._now().strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = hashlib.sha256(text.encode()).hexdigest()
        if status == 200:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            body_path.write_text(text)
            meta_path.write_text(json.dumps({"url": url, "status": status, "fetched_at": fetched_at, "sha256": digest}))
        return Page(url, status, text, fetched_at, digest, False)


def _better(new: FacultyRecord, old: FacultyRecord) -> bool:
    rank = lambda r: (r.email is not None, r.source_kind == "profile")
    return rank(new) > rank(old)


def _requests_get(url: str) -> tuple[int, str]:
    import requests

    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    return r.status_code, r.text


class DirectoryAdapter(Protocol):
    ror_id: str
    domains: tuple[str, ...]

    def seeds(self, fetcher: PoliteFetcher) -> list[str]: ...

    def parse(self, page: Page) -> list[FacultyRecord]: ...


def crawl(adapter: DirectoryAdapter, fetcher: PoliteFetcher) -> tuple[list[FacultyRecord], dict]:
    stats = {"seeds": 0, "fetched": 0, "cached": 0, "robots_denied": 0, "http_errors": 0,
             "records": 0, "emails": 0, "dropped_emails": 0}
    seeds = adapter.seeds(fetcher)
    stats["seeds"] = len(seeds)
    out: dict[str, FacultyRecord] = {}
    for url in seeds:
        try:
            page = fetcher.fetch(url)
        except RobotsDenied:
            stats["robots_denied"] += 1
            continue
        stats["cached" if page.from_cache else "fetched"] += 1
        if page.status != 200:
            stats["http_errors"] += 1
            continue
        for rec in adapter.parse(page):
            prior = out.get(rec.slug)
            if prior is None or _better(rec, prior):
                out[rec.slug] = rec
    records = sorted(out.values(), key=lambda r: r.slug)
    stats["records"] = len(records)
    stats["emails"] = sum(1 for r in records if r.email)
    stats["dropped_emails"] = sum(len(r.dropped_emails) for r in records)
    stats["network_errors"] = len(fetcher.errors)
    stats["from_profile"] = sum(1 for r in records if r.source_kind == "profile")
    stats["from_listing"] = sum(1 for r in records if r.source_kind == "listing")
    return records, stats
