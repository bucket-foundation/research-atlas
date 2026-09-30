from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
import urllib.robotparser
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from atlas.users.contacts import UA
from atlas.users.directories.base import Page, PoliteFetcher, RobotsDenied

STATUSES = ("pending", "pending_adapter", "running", "done", "failed", "zero_yield")
TIER_RANK = {"A+": 2, "A": 1}


def norm_name(value: str | None) -> str:
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", value)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ").replace("–", " ").replace("—", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"^the ", "", s.strip())
    s = re.sub(r"\b(main campus|campus)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def wiki_key(value: str | None) -> str:
    if not value:
        return ""
    title = value.rsplit("/wiki/", 1)[-1] if "/wiki/" in value else value
    title = title.split("#", 1)[0].replace("_", " ")
    return norm_name(unquote(title))


def _row_link(row: str) -> tuple[str, str] | None:
    m = re.search(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]", row)
    if not m:
        return None
    title = m.group(1).strip()
    label = (m.group(2) or title).strip()
    return label, title


def parse_wiki_carnegie(text: str) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {"R1": [], "R2": []}
    sections = re.split(r"^==([^=].*?)==\s*$", text, flags=re.MULTILINE)
    for i in range(1, len(sections) - 1, 2):
        head, body = sections[i], sections[i + 1]
        cls = "R1" if '"R1:' in head else "R2" if '"R2:' in head else None
        if not cls:
            continue
        table = body.split("{|", 1)[-1].split("|}", 1)[0]
        seen = set()
        for row in table.split("|-")[1:]:
            link = _row_link(row)
            if link and link[1] not in seen:
                seen.add(link[1])
                out[cls].append(link)
    return out


def ror_short(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1]


def ror_names(rec: dict) -> list[str]:
    return [n["value"] for n in rec.get("names", []) if "acronym" not in n.get("types", [])]


def ror_country(rec: dict) -> str:
    for loc in rec.get("locations", []):
        cc = (loc.get("geonames_details") or {}).get("country_code")
        if cc:
            return cc
    return ""


def ror_homepage(rec: dict) -> str:
    for link in rec.get("links", []):
        if link.get("type") == "website":
            return link.get("value") or ""
    return ""


def ror_domains(rec: dict) -> list[str]:
    doms = [d.lower() for d in rec.get("domains", []) if d]
    host = urlparse(ror_homepage(rec)).netloc.lower().split(":")[0]
    host = re.sub(r"^www\d?\.", "", host)
    if host and host not in doms:
        doms.append(host)
    return doms


def ror_display(rec: dict) -> str:
    for n in rec.get("names", []):
        if "ror_display" in n.get("types", []):
            return n["value"]
    names = ror_names(rec)
    return names[0] if names else ""


class RorIndex:
    def __init__(self, records: Iterable[dict], countries: tuple[str, ...] = ("US", "PR"), rtype: str = "education") -> None:
        self.by_id: dict[str, dict] = {}
        self.by_wiki: dict[str, list[dict]] = {}
        self.by_name: dict[str, list[dict]] = {}
        for rec in records:
            self.by_id[ror_short(rec["id"])] = rec
            if rec.get("status", "active") != "active":
                continue
            if countries and ror_country(rec) not in countries:
                continue
            if rtype and rtype not in rec.get("types", []):
                continue
            for link in rec.get("links", []):
                if link.get("type") == "wikipedia":
                    self.by_wiki.setdefault(wiki_key(link.get("value")), []).append(rec)
            for n in ror_names(rec):
                self.by_name.setdefault(norm_name(n), []).append(rec)

    @staticmethod
    def _pick(cands: list[dict]) -> dict | None:
        uniq = {c["id"]: c for c in cands}
        if not uniq:
            return None
        roots = [c for c in uniq.values() if not any(r.get("type") == "parent" for r in c.get("relationships", []))]
        pool = roots or list(uniq.values())
        return pool[0] if len(pool) == 1 else None

    def ambiguous(self, name: str, wiki_title: str | None = None) -> bool:
        keys = ((wiki_key(wiki_title), self.by_wiki), (norm_name(name), self.by_name), (norm_name(wiki_title), self.by_name))
        return any(k and len({c["id"] for c in idx.get(k, [])}) > 1 for k, idx in keys)

    def resolve(self, name: str, wiki_title: str | None = None) -> dict | None:
        for key, idx in ((wiki_key(wiki_title), self.by_wiki), (norm_name(name), self.by_name),
                         (norm_name(wiki_title), self.by_name)):
            if key and (hit := self._pick(idx.get(key, []))):
                return hit
        return None


def advisor_tiers(ranked_rows: Iterable[dict], roster_rows: Iterable[dict]) -> dict[str, tuple[str, int]]:
    from atlas.advisor_join import openalex_author_id, ror_id

    rors: dict[str, str] = {}
    for row in roster_rows:
        oa, r = openalex_author_id(row.get("author_id") or row.get("openalex_url")), ror_id(row.get("ror"))
        if oa and r:
            rors.setdefault(oa, r)
    out: dict[str, tuple[str, int]] = {}
    for row in ranked_rows:
        tier = (row.get("tier") or "").strip()
        if tier not in TIER_RANK:
            continue
        r = ror_id(row.get("ror")) or rors.get(openalex_author_id(row.get("openalex_url")) or "")
        if not r:
            continue
        best, n = out.get(r, (tier, 0))
        out[r] = (best if TIER_RANK[best] >= TIER_RANK[tier] else tier, n + 1)
    return out


class TokenBucket:
    def __init__(self, interval: float = 1.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._next: dict[str, float] = {}
        self._custom: dict[str, float] = {}
        self._lock = threading.Lock()

    def set_interval(self, host: str, seconds: float) -> None:
        with self._lock:
            self._custom[host] = max(self.interval, seconds)

    def acquire(self, host: str) -> float:
        with self._lock:
            now = self._clock()
            at = max(now, self._next.get(host, now))
            self._next[host] = at + self._custom.get(host, self.interval)
        wait = at - now
        if wait > 0:
            self._sleep(wait)
        return wait


class RobotsCache:
    def __init__(self, get: Callable[[str], tuple[int, str]], bucket: TokenBucket) -> None:
        self._get = get
        self._bucket = bucket
        self._rules: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self.requests = 0

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self._lock:
            host_lock = self._locks.setdefault(base, threading.Lock())
        with host_lock:
            rp = self._rules.get(base)
            if rp is None:
                rp = urllib.robotparser.RobotFileParser()
                self._bucket.acquire(p.netloc)
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
                    delay = rp.crawl_delay(UA)
                    if delay:
                        self._bucket.set_interval(p.netloc, float(delay))
                self._rules[base] = rp
        return rp.can_fetch(UA, url)


class BudgetExceeded(Exception):
    pass


def cache_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file() and not f.name.startswith("_"))


class DiskBudget:
    def __init__(self, root: Path, limit_bytes: int) -> None:
        self.limit = limit_bytes
        self._lock = threading.Lock()
        self.used = cache_bytes(root)

    def add(self, n: int) -> None:
        with self._lock:
            self.used += n

    @property
    def exceeded(self) -> bool:
        return self.used >= self.limit


class Suppressed(Exception):
    pass


class HaulFetcher(PoliteFetcher):
    def __init__(self, cache_dir: Path, *, bucket: TokenBucket, budget: DiskBudget, max_pages: int = 2000,
                 stop: threading.Event | None = None, shared_robots: dict | None = None,
                 shared_delay: dict | None = None, suppression=None, **kw) -> None:
        super().__init__(cache_dir, delay=bucket.interval, budget=max_pages, **kw)
        self.bucket = bucket
        self.disk = budget
        self.stop = stop or threading.Event()
        self.suppression = suppression
        self.suppressed = 0
        if shared_robots is not None:
            self._robots = shared_robots
        if shared_delay is not None:
            self._host_delay = shared_delay

    def _wait(self, host: str) -> None:
        if host in self._host_delay:
            self.bucket.set_interval(host, self._host_delay[host])
        self.bucket.acquire(host)

    def fetch(self, url: str) -> Page:
        if self.stop.is_set():
            raise BudgetExceeded(url)
        if self.suppression is not None and self.suppression.blocks_url(url):
            self.suppressed += 1
            raise RobotsDenied(url)
        before = self.requests
        page = super().fetch(url)
        if self.requests > before and page.status == 200:
            body, meta = self._paths(url)
            self.disk.add(sum(p.stat().st_size for p in (body, meta) if p.exists()))
            if self.disk.exceeded:
                self.stop.set()
        return page


@dataclass
class HaulState:
    path: Path
    institutions: dict[str, dict] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, path: Path, reset_running: bool = True) -> HaulState:
        st = cls(path)
        if path.exists():
            st.institutions = json.loads(path.read_text()).get("institutions", {})
        for rec in st.institutions.values():
            if reset_running and rec.get("status") == "running":
                rec["status"] = "pending"
        return st

    def ensure(self, ror: str, name: str) -> None:
        with self._lock:
            self.institutions.setdefault(ror, {"name": name, "status": "pending", "profiles": 0, "emails": 0})

    def update(self, ror: str, **fields) -> None:
        with self._lock:
            self.institutions.setdefault(ror, {"status": "pending", "profiles": 0, "emails": 0}).update(fields)
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "institutions": self.institutions}, indent=1, sort_keys=True))
        os.replace(tmp, self.path)

    def save(self) -> None:
        with self._lock:
            self._save()

    def counts(self) -> dict[str, int]:
        c = {s: 0 for s in STATUSES}
        for rec in self.institutions.values():
            c[rec.get("status", "pending")] = c.get(rec.get("status", "pending"), 0) + 1
        c["profiles"] = sum(r.get("profiles", 0) for r in self.institutions.values())
        c["emails"] = sum(r.get("emails", 0) for r in self.institutions.values())
        c["opt_out_skipped"] = sum(r.get("opt_out_skipped", 0) for r in self.institutions.values())
        return c

    def todo(self, retry_after: timedelta = timedelta(hours=6)) -> list[str]:
        now = datetime.now(timezone.utc)
        out = []
        for ror, rec in self.institutions.items():
            st = rec.get("status")
            if st in ("pending", "pending_adapter"):
                out.append(ror)
            elif st == "failed":
                last = rec.get("finished_at")
                if not last or now - datetime.fromisoformat(last.replace("Z", "+00:00")) >= retry_after:
                    out.append(ror)
        return out


def status_line(state: HaulState, cache_root: Path) -> str:
    c = state.counts()
    size = cache_bytes(cache_root)
    return (f"institutions done={c['done']} running={c['running']} pending={c['pending']} pending_adapter={c['pending_adapter']} failed={c['failed']} "
            f"zero_yield={c['zero_yield']} profiles={c['profiles']} emails={c['emails']} opt_out_skipped={c['opt_out_skipped']} cache={size / 1e9:.2f}GB")
