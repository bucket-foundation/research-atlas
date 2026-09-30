from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import REPO_ROOT
from atlas.users.directories.base import (
    OFFICIAL_CACHE,
    crawl,
    retrieved_by,
)
from atlas.users.directories.optout import (
    OPT_OUT,
    TOMBSTONES,
    Suppression,
    _opt_out_rows,
)
from atlas.users.directories.stevens import StevensAdapter
from atlas.users.haul import (
    LOW_YIELD,
    BudgetExceeded,
    DiskBudget,
    HaulFetcher,
    HaulState,
    TokenBucket,
    status_line,
)

VERSION = "0.2"
SEEDS = REPO_ROOT / "data" / "seeds" / "institutions.csv"
PRIVATE_EXTRA = REPO_ROOT / "data" / "private" / "seed_advisor_extra.csv"
PRIVATE_COUNTS = REPO_ROOT / "data" / "private" / "seed_advisor_counts.csv"
BUILTIN = {StevensAdapter.ror_id: StevensAdapter}
EXIT_ZERO_YIELD = 2
EXIT_ALL_FAILED = 1
EXIT_BUDGET = 3


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split(value: str | None) -> list[str]:
    return [v for v in (value or "").split(";") if v]


def adapter_for(row: dict, fetcher: HaulFetcher):
    if row["ror_id"] in BUILTIN:
        return BUILTIN[row["ror_id"]]()
    try:
        from atlas.users.directories import REGISTRY, detect_site
    except ImportError:
        return None
    domains = tuple(_split(row.get("domains")))
    cls, base = REGISTRY.get(row.get("platform_guess") or ""), None
    if cls is None and row.get("homepage"):
        cls, base = detect_site(row["ror_id"], row["homepage"], fetcher, domains)
    if cls is None:
        return None
    entries = list(dict.fromkeys(([base] if base else []) + _split(row.get("directory_entry_urls"))))
    return cls(row["ror_id"], entries, domains=domains, max_pages=fetcher.budget or 400)


def purge_opt_outs(ror: str, suppression: Suppression, opt_rows: list[dict], cache_root: Path,
                   state: HaulState) -> int:
    done = set(state.institutions.get(ror, {}).get("purged", []))
    purged = 0
    for row in opt_rows:
        row_ror = (row.get("ror_id") or "").rstrip("/").rsplit("/", 1)[-1]
        if row_ror and row_ror != ror:
            continue
        if not (row_ror or row.get("orcid") or row.get("email")):
            continue
        tag = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]
        if tag in done:
            continue
        got = suppression.remove(cache_root, name=row.get("name") if row_ror else None, ror_id=ror,
                                 orcid=row.get("orcid"), email=row.get("email"))
        purged += got.get("pages", 0) + got.get("records", 0)
        done.add(tag)
    state.update(ror, purged=sorted(done))
    return purged


def provenance(d: dict, by: str) -> dict:
    d.setdefault("source", "official_directory")
    d["source_id"] = d.get("source_id") or d.get("slug")
    d["source_url"] = d.get("source_url") or d.get("profile_url")
    d["as_of"] = d.get("as_of") or d.get("fetched_at")
    d.setdefault("match_tier", None)
    d.setdefault("licence", "institution-copyright")
    d["storage"] = "link_only"
    d["retrieved_by"] = d.get("retrieved_by") or by
    return d


def haul_one(row: dict, *, state: HaulState, bucket: TokenBucket, budget: DiskBudget, stop: threading.Event,
             cache_root: Path, max_pages: int, get=None, adapter_factory=adapter_for,
             suppression: Suppression | None = None, opt_rows: list[dict] | None = None,
             robots: dict | None = None, delays: dict | None = None) -> str:
    ror = row["ror_id"]
    if stop.is_set():
        return "pending"
    state.update(ror, status="running", started_at=_now(), error=None)
    root = cache_root / ror
    kw = {"get": get} if get else {}
    fetcher = HaulFetcher(root / "pages", bucket=bucket, budget=budget, max_pages=max_pages, stop=stop,
                          shared_robots=robots, shared_delay=delays, suppression=suppression, **kw)
    try:
        purged = purge_opt_outs(ror, suppression, opt_rows or [], cache_root, state) if suppression else 0
        adapter = adapter_factory(row, fetcher)
        if adapter is None:
            state.update(ror, status="pending_adapter", error="no_adapter", finished_at=_now())
            return "pending_adapter"
        records, stats = crawl(adapter, fetcher, suppression)
    except BudgetExceeded:
        state.update(ror, status="pending", error="disk_budget" if budget.exceeded else "deadline")
        return "pending"
    except Exception as exc:  # noqa: BLE001
        state.update(ror, status="failed", error=f"{type(exc).__name__}: {exc}"[:300], finished_at=_now())
        return "failed"
    if stop.is_set():
        state.update(ror, status="pending", error="disk_budget" if budget.exceeded else "deadline")
        return "pending"
    by = retrieved_by(adapter) if not isinstance(adapter, StevensAdapter) else f"stevens/{VERSION}"
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "records.jsonl.tmp"
    with tmp.open("w") as f:
        for r in records:
            f.write(json.dumps(provenance(r.as_dict(), by or f"haul/{VERSION}"), sort_keys=True) + "\n")
    tmp.replace(root / "records.jsonl")
    emails = sum(1 for r in records if r.email)
    status = "zero_yield" if not emails else "low_yield" if len(records) < LOW_YIELD else "done"
    state.update(ror, status=status, profiles=len(records), emails=emails, requests=fetcher.requests,
                 opt_out_skipped=stats.get("suppressed_records", 0) + stats.get("suppressed_seeds", 0)
                 + fetcher.suppressed, purged_items=purged, robots_denied=stats.get("robots_denied", 0),
                 http_errors=stats.get("http_errors", 0), capped=stats.get("budget_exhausted", False),
                 adapter=by, finished_at=_now())
    return status


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def load_seeds(path: Path, extra: Path = PRIVATE_EXTRA, priority: Path = PRIVATE_COUNTS) -> list[dict]:
    rows = _read(path)
    seen = {r["ror_id"] for r in rows}
    rows += [r for r in _read(extra) if r["ror_id"] not in seen]
    rank = {r["ror_id"]: i for i, r in enumerate(_read(priority))}
    return sorted(rows, key=lambda r: rank.get(r["ror_id"], len(rank)))


def run(rows: list[dict], *, cache_root: Path, workers: int, max_pages: int, budget_bytes: int,
        only: list[str] | None = None, limit: int | None = None, get=None, bucket: TokenBucket | None = None,
        adapter_factory=adapter_for, refresh: bool = False, min_free: int | None = None, suppression: Suppression | None = None,
        opt_rows: list[dict] | None = None, deadline: float | None = None) -> tuple[int, HaulState]:
    state = HaulState.load(cache_root / "_state.json")
    by_ror = {r["ror_id"]: r for r in rows}
    for r in rows:
        state.ensure(r["ror_id"], r["name"])
    if refresh and only:
        for ror in only:
            state.update(ror, status="pending")
    state.save()
    todo = [r for r in (only or state.todo()) if r in by_ror]
    if only:
        todo = [r for r in todo if state.institutions[r]["status"] in ("pending", "pending_adapter", "failed")]
    position = {r["ror_id"]: i for i, r in enumerate(rows)}
    todo.sort(key=position.__getitem__)
    if limit:
        todo = todo[:limit]
    bucket = bucket or TokenBucket(1.0)
    budget = DiskBudget(cache_root, budget_bytes, min_free)
    stop = threading.Event()
    if budget.low_space:
        print(f"free space under {budget.min_free / 1e9:.0f}GB on {cache_root}, refusing to start", file=sys.stderr)
        return EXIT_BUDGET, state
    if budget.exceeded:
        print(f"disk budget reached: {budget.used / 1e9:.2f}GB", file=sys.stderr)
        return EXIT_BUDGET, state
    if deadline:
        timer = threading.Timer(deadline, stop.set)
        timer.daemon = True
        timer.start()
    robots: dict = {}
    delays: dict = {}

    def one(ror: str) -> str:
        return haul_one(by_ror[ror], state=state, bucket=bucket, budget=budget, stop=stop, cache_root=cache_root,
                        max_pages=max_pages, get=get, adapter_factory=adapter_factory, suppression=suppression,
                        opt_rows=opt_rows, robots=robots, delays=delays)

    with ThreadPoolExecutor(max(1, workers)) as ex:
        results = list(ex.map(one, todo))
    state.save()
    if budget.exceeded:
        print(f"disk budget or free-space floor reached: {budget.used / 1e9:.2f}GB used, stopped cleanly",
              file=sys.stderr)
        return EXIT_BUDGET, state
    if results and all(r == "failed" for r in results):
        return EXIT_ALL_FAILED, state
    return (EXIT_ZERO_YIELD if "zero_yield" in results else 0), state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Polite, resumable crawl of official faculty directories over the seed list.")
    ap.add_argument("--seeds", type=Path, default=SEEDS)
    ap.add_argument("--cache-root", type=Path, default=OFFICIAL_CACHE)
    ap.add_argument("--opt-out", type=Path, default=OPT_OUT)
    ap.add_argument("--tombstones", type=Path, default=TOMBSTONES)
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--max-pages", type=int, default=5000)
    ap.add_argument("--budget-gb", type=float, default=8.0)
    ap.add_argument("--deadline-min", type=float, default=45.0)
    ap.add_argument("--min-free-gb", type=float, default=50.0)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    if args.status:
        print(status_line(HaulState.load(args.cache_root / "_state.json", reset_running=False), args.cache_root))
        return 0
    args.cache_root.mkdir(parents=True, exist_ok=True)
    lock = (args.cache_root / "_haul.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another faculty haul is running", file=sys.stderr)
        return 0
    started = time.monotonic()
    code, state = run(load_seeds(args.seeds), cache_root=args.cache_root, workers=args.workers,
                      max_pages=args.max_pages, budget_bytes=int(args.budget_gb * 1e9), only=args.only,
                      limit=args.limit, refresh=args.refresh, min_free=int(args.min_free_gb * 1e9),
                      suppression=Suppression.load(args.opt_out, args.tombstones),
                      opt_rows=_opt_out_rows(args.opt_out), deadline=args.deadline_min * 60)
    print(status_line(state, args.cache_root), f"elapsed={time.monotonic() - started:.0f}s")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
