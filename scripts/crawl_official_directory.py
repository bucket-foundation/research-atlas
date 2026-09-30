from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.users.directories import REGISTRY, detect_site
from atlas.users.directories.base import (
    OFFICIAL_CACHE,
    PoliteFetcher,
    crawl,
)
from atlas.users.directories.domains import normalize_ror
from atlas.users.directories.stevens import StevensAdapter

INSTITUTIONS = {"stevens": StevensAdapter}


def build_adapter(args, fetcher):
    if args.institution in INSTITUTIONS:
        return INSTITUTIONS[args.institution](), "stevens"
    ror = normalize_ror(args.institution)
    platform = args.platform
    entries = list(args.entry)
    if platform == "auto":
        cls, base = detect_site(ror, args.homepage or entries[0], fetcher)
        cls = cls or REGISTRY["generic"]
        entries = entries or [base or args.homepage]
    else:
        cls = REGISTRY[platform]
    return cls(ror, entries or [args.homepage], max_pages=args.max_pages), cls.platform


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Crawl an official university faculty directory into a private JSONL.")
    ap.add_argument("institution", help="'stevens' or a ROR id")
    ap.add_argument("--entry", action="append", default=[])
    ap.add_argument("--homepage")
    ap.add_argument("--platform", default="auto", choices=["auto", *sorted(REGISTRY)])
    ap.add_argument("--max-pages", type=int, default=400)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--max-age-days", type=int, default=30)
    args = ap.parse_args(argv)
    if args.institution not in INSTITUTIONS and not (args.entry or args.homepage):
        ap.error("a ROR id needs --entry or --homepage")
    started = time.monotonic()
    ror = INSTITUTIONS[args.institution].ror_id if args.institution in INSTITUTIONS else normalize_ror(args.institution)
    root = OFFICIAL_CACHE / ror
    fetcher = PoliteFetcher(root / "pages", delay=args.delay, max_age_days=args.max_age_days,
                            budget=args.max_pages)
    adapter, platform = build_adapter(args, fetcher)
    records, stats = crawl(adapter, fetcher)
    out = root / "faculty.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for r in records:
            f.write(json.dumps(r.as_dict(), sort_keys=True) + "\n")
    stats.update(ror_id=ror, adapter=platform, elapsed_s=round(time.monotonic() - started, 1),
                 discovery=getattr(adapter, "discovery", {}))
    print(json.dumps(stats, indent=1, sort_keys=True))
    return 0 if stats["emails"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
