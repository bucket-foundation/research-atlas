"""Uniform Connector interface every data source implements.

Lifecycle: ``fetch()`` (paginated, polite, cached) -> ``normalize()`` (raw ->
canonical :class:`~atlas.schema.Row` objects) -> ``emit()`` (merge into
``data/processed/<table>.parquet`` by stable id, dedup, append provenance).

Design goals
------------
- **Idempotent + resumable.** ``fetch()`` caches every raw response under
  ``data/raw/<source>/`` keyed by a stable page key; re-running skips pages
  already on disk. ``emit()`` merges on ``atlas_id`` so re-emitting converges
  instead of duplicating.
- **Polite.** A shared :class:`HttpClient` honors ``Retry-After``, backs off on
  429/503, sends a descriptive User-Agent (browser UA available where a site
  needs it), and sleeps a configurable delay between requests.
- **Schema-true.** Every row passes through :func:`atlas.schema.coerce`, so an
  emitted parquet always has the canonical column set in canonical order.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Iterator

import requests

from atlas import schema
from atlas.schema import Row

# Repo root = two levels above this file (atlas/connectors/base.py -> repo).
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_RAW = REPO_ROOT / "data" / "raw"
DATA_PROCESSED = REPO_ROOT / "data" / "processed"

DEFAULT_UA = "research-atlas/0.1 (+https://github.com/bucket-foundation/research-atlas; mailto:gianyrox@gmail.com)"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class HttpClient:
    """Polite HTTP client: retries, ``Retry-After`` honoring, backoff."""

    def __init__(self, user_agent: str = DEFAULT_UA, delay: float = 0.2,
                 max_retries: int = 5, max_retry_after: float = 120.0):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.delay = delay
        self.max_retries = max_retries
        self.max_retry_after = max_retry_after

    def get(self, url: str, params: dict | None = None, timeout: float = 45):
        return self._request("GET", url, params=params, timeout=timeout)

    def post(self, url: str, json_body: dict | None = None, timeout: float = 45):
        return self._request("POST", url, json_body=json_body, timeout=timeout)

    def _request(self, method, url, params=None, json_body=None, timeout=45):
        for attempt in range(self.max_retries):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body, timeout=timeout
                )
            except requests.RequestException as exc:  # network blip
                wait = self.delay * (2 ** attempt)
                print(f"  net retry ({exc}); sleeping {wait:.1f}s")
                time.sleep(wait)
                continue

            if resp.status_code == 200:
                if self.delay:
                    time.sleep(self.delay)
                return resp

            if resp.status_code in (429, 503):
                ra = resp.headers.get("Retry-After")
                if ra is not None:
                    try:
                        wait = min(float(ra), self.max_retry_after)
                    except ValueError:
                        wait = self.delay * (2 ** attempt)
                else:
                    wait = self.delay * (2 ** attempt)
                print(f"  HTTP {resp.status_code}; backoff {wait:.1f}s "
                      f"(attempt {attempt + 1}/{self.max_retries})")
                time.sleep(wait)
                continue

            print(f"  HTTP {resp.status_code}: {resp.text[:160]}")
            return None
        return None


class Connector(ABC):
    """Base class. A source implements ``source``, ``fetch`` and ``normalize``."""

    #: short source key used in provenance and raw-cache pathing
    source: str = "base"
    #: override with BROWSER_UA for sites that block default agents
    user_agent: str = DEFAULT_UA
    #: polite inter-request delay in seconds
    delay: float = 0.2

    def __init__(self, raw_dir: Path | None = None, processed_dir: Path | None = None):
        self.raw_dir = (raw_dir or DATA_RAW) / self.source
        self.processed_dir = processed_dir or DATA_PROCESSED
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.http = HttpClient(user_agent=self.user_agent, delay=self.delay)

    # ----- raw cache (idempotent / resumable) ----------------------------- #

    def _raw_path(self, page_key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in page_key)
        return self.raw_dir / f"{safe}.json"

    def cache_raw(self, page_key: str, payload) -> Path:
        path = self._raw_path(page_key)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def load_raw(self, page_key: str):
        path = self._raw_path(page_key)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def has_raw(self, page_key: str) -> bool:
        return self._raw_path(page_key).exists()

    def iter_cached_raw(self) -> Iterator[dict]:
        """Yield every cached raw page (for re-normalizing without re-fetching)."""
        for path in sorted(self.raw_dir.glob("*.json")):
            yield json.loads(path.read_text(encoding="utf-8"))

    # ----- the interface a source implements ------------------------------ #

    @abstractmethod
    def fetch(self, **kwargs) -> Iterable[dict]:
        """Yield raw pages from the source, caching each under ``data/raw``.

        Must be paginated, polite, and resumable: skip pages already cached.
        """

    @abstractmethod
    def normalize(self, raw_pages: Iterable[dict]) -> Iterator[Row]:
        """Turn raw pages into canonical :class:`Row` objects (entities + edges)."""

    # ----- emit (merge into parquet by stable id) ------------------------- #

    def emit(self, rows: Iterable[Row]) -> dict[str, int]:
        """Merge rows into ``data/processed/<table>.parquet`` by ``atlas_id``.

        Dedup rule:
        - entities dedup on ``atlas_id`` (last write wins -- newest ``as_of``).
        - edges dedup on ``(src_id, dst_id, role/score, source)`` so the same
          edge attested by two sources is kept once per source.
        Returns a per-table count of rows after merge.
        """
        import pandas as pd

        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            grouped[row.table].append(schema.coerce(row.table, row.data))

        counts: dict[str, int] = {}
        for table, new_rows in grouped.items():
            new_df = pd.DataFrame(new_rows)
            path = self.processed_dir / f"{table}.parquet"
            if path.exists():
                old_df = pd.read_parquet(path)
                combined = pd.concat([old_df, new_df], ignore_index=True)
            else:
                combined = new_df

            if table in schema.ENTITY_COLUMNS:
                # newest as_of wins per atlas_id
                combined = combined.sort_values("as_of").drop_duplicates(
                    subset=["atlas_id"], keep="last"
                )
                cols = schema.ENTITY_COLUMNS[table]
            else:
                key = "role" if "role" in combined.columns else "score"
                subset = ["src_id", "dst_id", key, "source"]
                combined = combined.sort_values("as_of").drop_duplicates(
                    subset=subset, keep="last"
                )
                cols = schema.EDGE_COLUMNS[table]

            combined = combined.reindex(columns=cols)
            combined.to_parquet(path, index=False)
            counts[table] = len(combined)

        return counts

    # ----- convenience: full run ------------------------------------------ #

    def run(self, **fetch_kwargs) -> dict[str, int]:
        """fetch -> normalize -> emit, end to end. Returns per-table counts."""
        pages = list(self.fetch(**fetch_kwargs))
        rows = self.normalize(pages)
        return self.emit(rows)
