#!/usr/bin/env python3
"""Run a named metascience query against the atlas DuckDB and print results.

A thin CLI over :mod:`atlas.analysis`. Lists available queries, runs one by
name, prints a table (and optional JSON). The substrate for our own studies.

Usage:
    python scripts/query.py --list
    python scripts/query.py top_funders_by_output --topic mitochondri
    python scripts/query.py rising_fields --limit 15
    python scripts/query.py org_funding_vs_output --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas import analysis  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a metascience query.")
    ap.add_argument("query", nargs="?", help="query name (see --list)")
    ap.add_argument("--list", action="store_true", help="list available queries")
    ap.add_argument("--db", default=None)
    ap.add_argument("--topic", default="mitochondri")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--min-grants", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.list or not args.query:
        print("Available queries:")
        for name in analysis.QUERIES:
            print(f"  {name}")
        return 0

    fn = analysis.QUERIES.get(args.query)
    if fn is None:
        print(f"unknown query {args.query!r}; --list to see options")
        return 2

    con = analysis.connect(args.db)
    kwargs = {}
    if args.query == "top_funders_by_output":
        kwargs = {"topic_like": args.topic, "limit": args.limit}
    elif args.query == "org_funding_vs_output":
        kwargs = {"min_grants": args.min_grants, "limit": args.limit}
    else:
        kwargs = {"limit": args.limit}
    rows = fn(con, **kwargs)
    con.close()

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("(no rows)")
        return 0
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
