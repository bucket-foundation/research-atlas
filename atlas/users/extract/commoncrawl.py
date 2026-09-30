from __future__ import annotations

import gzip
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote, urlparse

from atlas.users.contacts import UA

INDEX = "https://index.commoncrawl.org"
DATA = "https://data.commoncrawl.org"
COLLECTION = "CC-MAIN-2026-39"
CHALLENGE_RE = re.compile(r"<title>\s*(Just a moment|Attention Required|Access denied)", re.I)

Get = Callable[[str, dict], tuple[int, bytes]]


def _get(url: str, headers: dict) -> tuple[int, bytes]:
    import requests

    r = requests.get(url, headers={"User-Agent": UA, **headers}, timeout=60)
    return r.status_code, r.content


def needs_archive(status: int, body: str) -> bool:
    return status == 403 or (status in (403, 429, 503) and bool(CHALLENGE_RE.search(body[:4000])))


@dataclass
class ArchiveRecord:
    url: str
    status: int
    html: str
    warc_date: str | None
    filename: str
    offset: int


def parse_warc(blob: bytes) -> tuple[dict, int, str]:
    raw = gzip.decompress(blob)
    head, _, rest = raw.partition(b"\r\n\r\n")
    warc = {}
    for line in head.decode("utf-8", "replace").split("\r\n")[1:]:
        k, _, v = line.partition(":")
        warc[k.strip().lower()] = v.strip()
    http_head, _, body = rest.partition(b"\r\n\r\n")
    status_line = http_head.split(b"\r\n", 1)[0].decode("latin-1")
    m = re.match(r"HTTP/\S+\s+(\d{3})", status_line)
    charset = re.search(rb"charset=([\w-]+)", http_head, re.I)
    text = body.decode(charset.group(1).decode() if charset else "utf-8", "replace")
    return warc, int(m.group(1)) if m else 0, text


@dataclass
class CommonCrawl:
    collection: str = COLLECTION
    get: Get = _get
    delay: float = 1.0
    sleep: Callable[[float], None] = time.sleep
    _last: dict = field(default_factory=dict)

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None and time.monotonic() - last < self.delay:
            self.sleep(self.delay - (time.monotonic() - last))
        self._last[host] = time.monotonic()

    def _fetch(self, url: str, headers: dict | None = None) -> tuple[int, bytes]:
        self._wait(urlparse(url).netloc)
        return self.get(url, headers or {})

    def lookup(self, url_pattern: str, limit: int = 500) -> list[dict]:
        q = f"{INDEX}/{self.collection}-index?url={quote(url_pattern, safe='*/:')}&output=json&limit={limit}"
        status, body = self._fetch(q)
        if status != 200:
            return []
        rows = [json.loads(line) for line in body.decode().splitlines() if line.strip()]
        best: dict[str, dict] = {}
        for r in rows:
            if str(r.get("status")) == "200" and "html" in (r.get("mime") or r.get("mime-detected") or ""):
                if r["url"] not in best or r.get("timestamp", "") > best[r["url"]].get("timestamp", ""):
                    best[r["url"]] = r
        return list(best.values())

    def profile_urls(self, host: str, profile_re: re.Pattern, limit: int = 500) -> list[dict]:
        return [r for r in self.lookup(f"{host}/*", limit) if profile_re.search(urlparse(r["url"]).path)]

    def record(self, row: dict) -> ArchiveRecord | None:
        start = int(row["offset"])
        end = start + int(row["length"]) - 1
        status, blob = self._fetch(f"{DATA}/{row['filename']}", {"Range": f"bytes={start}-{end}"})
        if status not in (200, 206):
            return None
        warc, http_status, html = parse_warc(blob)
        return ArchiveRecord(url=warc.get("warc-target-uri", row["url"]), status=http_status, html=html,
                             warc_date=warc.get("warc-date"), filename=row["filename"], offset=start)
