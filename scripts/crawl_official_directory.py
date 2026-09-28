from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.users.directories.base import OFFICIAL_CACHE, PoliteFetcher, crawl  # noqa: E402
from atlas.users.directories.stevens import StevensAdapter  # noqa: E402

ADAPTERS = {"stevens": StevensAdapter}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Crawl an official university faculty directory into a private JSONL.")
    ap.add_argument("institution", choices=sorted(ADAPTERS))
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--max-age-days", type=int, default=30)
    args = ap.parse_args(argv)
    adapter = ADAPTERS[args.institution]()
    root = OFFICIAL_CACHE / adapter.ror_id
    fetcher = PoliteFetcher(root / "pages", delay=args.delay, max_age_days=args.max_age_days)
    records, stats = crawl(adapter, fetcher)
    out = root / "faculty.jsonl"
    with out.open("w") as f:
        for r in records:
            f.write(json.dumps(r.as_dict(), sort_keys=True) + "\n")
    stats["departments"] = len({d for r in records for d in r.departments})
    print(json.dumps(stats, indent=1, sort_keys=True))
    return 0 if stats["emails"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
