from __future__ import annotations

import argparse
import csv
import fcntl
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import REPO_ROOT  # noqa: E402
from atlas.users.directories.base import OFFICIAL_CACHE, RobotsDenied, _better, _requests_get  # noqa: E402
from atlas.users.directories.stevens import StevensAdapter  # noqa: E402
from atlas.users.haul import (  # noqa: E402
    BudgetExceeded, DiskBudget, OptOut, HaulFetcher, HaulState, PageCapReached, RobotsCache, TokenBucket, status_line,
)

OPT_OUT = REPO_ROOT / "data" / "private" / "opt_out.csv"
SEEDS = REPO_ROOT / "data" / "seeds" / "institutions.csv"
LICENCE = "institution-copyright"
BUILTIN = {StevensAdapter.ror_id: StevensAdapter}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _registry():
    try:
        from atlas.users import directories
    except Exception:
        return {}, None
    reg = {}
    for attr in ("REGISTRY", "ADAPTERS", "PLATFORMS"):
        got = getattr(directories, attr, None)
        if isinstance(got, dict):
            reg.update(got)
    return reg, getattr(directories, "detect_platform", None)


def _build(cls, row: dict):
    kw = {"ror_id": row["ror_id"], "domains": tuple(d for d in row.get("domains", "").split(";") if d),
          "homepage": row.get("homepage") or "",
          "entry_urls": [u for u in (row.get("directory_entry_urls") or "").split(";") if u]}
    for keys in (tuple(kw), ("ror_id", "domains", "homepage"), ("ror_id", "domains"), ()):
        try:
            obj = cls(**{k: kw[k] for k in keys})
        except TypeError:
            continue
        for k in ("ror_id", "domains"):
            if not getattr(obj, k, None):
                setattr(obj, k, kw[k])
        return obj
    return None


def adapter_for(row: dict):
    if row["ror_id"] in BUILTIN:
        return BUILTIN[row["ror_id"]]()
    reg, detect = _registry()
    platform = row.get("platform_guess") or ""
    if detect and not platform:
        try:
            platform = detect(row["ror_id"], row.get("homepage") or "") or ""
        except Exception:
            platform = ""
    cls = reg.get(row["ror_id"]) or reg.get(platform) or reg.get("generic")
    return _build(cls, row) if cls else None


def haul_one(row: dict, *, state: HaulState, bucket: TokenBucket, robots: RobotsCache, budget: DiskBudget,
             stop: threading.Event, cache_root: Path, max_pages: int, get=None, adapter_factory=adapter_for, opt_out: OptOut | None = None) -> str:
    ror = row["ror_id"]
    if stop.is_set():
        return "pending"
    adapter = adapter_factory(row)
    if adapter is None:
        state.update(ror, status="failed", error="no_adapter", finished_at=_now())
        return "failed"
    state.update(ror, status="running", started_at=_now(), error=None)
    root = cache_root / ror
    kw = {"get": get} if get else {}
    fetcher = HaulFetcher(root / "pages", bucket=bucket, robots=robots, budget=budget, max_pages=max_pages,
                          stop=stop, **kw)
    out: dict = {}
    denied = errors = 0
    capped = False
    try:
        try:
            seeds = adapter.seeds(fetcher)
        except PageCapReached:
            seeds, capped = [], True
        for url in seeds:
            try:
                page = fetcher.fetch(url)
            except RobotsDenied:
                denied += 1
                continue
            except PageCapReached:
                capped = True
                break
            if page.status != 200:
                errors += 1
                continue
            for rec in adapter.parse(page):
                prior = out.get(rec.slug)
                if prior is None or _better(rec, prior):
                    out[rec.slug] = rec
    except BudgetExceeded:
        state.update(ror, status="pending", error="disk_budget")
        return "pending"
    except Exception as exc:
        state.update(ror, status="failed", error=f"{type(exc).__name__}: {exc}"[:300], finished_at=_now())
        return "failed"
    opt_out = opt_out or OptOut()
    kept = [r for r in out.values() if not opt_out.matches(r.as_dict())]
    skipped = len(out) - len(kept)
    records = sorted(kept, key=lambda r: r.slug)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "records.jsonl.tmp"
    with tmp.open("w") as f:
        for r in records:
            f.write(json.dumps(provenance(r.as_dict()), sort_keys=True) + "\n")
    tmp.replace(root / "records.jsonl")
    emails = sum(1 for r in records if r.email)
    status = "done" if emails else "zero_yield"
    state.update(ror, status=status, profiles=len(records), emails=emails, pages=fetcher.pages,
                 requests=fetcher.requests, opt_out_skipped=skipped, robots_denied=denied, http_errors=errors, capped=capped,
                 finished_at=_now())
    return status


def provenance(d: dict) -> dict:
    d.update(source="official_directory", source_id=d.get("slug"), source_url=d.get("profile_url"),
             as_of=d.get("fetched_at"), match_tier=None, licence=LICENCE)
    return d


def load_seeds(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def run(rows: list[dict], *, cache_root: Path, workers: int, max_pages: int, budget_bytes: int,
        only: list[str] | None = None, limit: int | None = None, get=None, bucket: TokenBucket | None = None,
        adapter_factory=adapter_for, refresh: bool = False, opt_out: OptOut | None = None) -> tuple[int, HaulState]:
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
        todo = [r for r in todo if state.institutions[r]["status"] in ("pending", "failed")]
    if limit:
        todo = todo[:limit]
    bucket = bucket or TokenBucket(1.0)
    robots = RobotsCache(get or _requests_get, bucket)
    budget = DiskBudget(cache_root, budget_bytes)
    stop = threading.Event()
    if budget.exceeded:
        print(f"disk budget reached: {budget.used / 1e9:.2f}GB", file=sys.stderr)
        return 0, state

    def one(ror: str) -> str:
        return haul_one(by_ror[ror], state=state, bucket=bucket, robots=robots, budget=budget, stop=stop,
                        cache_root=cache_root, max_pages=max_pages, get=get, adapter_factory=adapter_factory,
                        opt_out=opt_out)

    with ThreadPoolExecutor(max(1, workers)) as ex:
        results = list(ex.map(one, todo))
    state.save()
    if stop.is_set():
        print(f"disk budget reached: {budget.used / 1e9:.2f}GB, stopped cleanly", file=sys.stderr)
    return (2 if "zero_yield" in results else 0), state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Polite, resumable crawl of official faculty directories over the seed list.")
    ap.add_argument("--seeds", type=Path, default=SEEDS)
    ap.add_argument("--cache-root", type=Path, default=OFFICIAL_CACHE)
    ap.add_argument("--opt-out", type=Path, default=OPT_OUT)
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--max-pages", type=int, default=5000)
    ap.add_argument("--budget-gb", type=float, default=8.0)
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
    code, state = run(load_seeds(args.seeds), cache_root=args.cache_root, workers=args.workers,
                      max_pages=args.max_pages, budget_bytes=int(args.budget_gb * 1e9), only=args.only,
                      limit=args.limit, refresh=args.refresh, opt_out=OptOut.load(args.opt_out))
    print(status_line(state, args.cache_root))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
