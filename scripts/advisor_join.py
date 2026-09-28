from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from atlas.advisor_join import Advisor, match_advisors, openalex_author_id, ror_id, summarize  # noqa: E402
from atlas.connectors.base import REPO_ROOT  # noqa: E402
from atlas.schema import now_iso  # noqa: E402


def load_advisors(ranked: Path, roster: Path | None) -> list[Advisor]:
    rors: dict[str, str] = {}
    if roster:
        with roster.open(newline="") as f:
            for row in csv.DictReader(f):
                oa = openalex_author_id(row.get("author_id"))
                r = ror_id(row.get("ror"))
                if oa and r and oa not in rors:
                    rors[oa] = r
    out = []
    with ranked.open(newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            oa = openalex_author_id(row.get("openalex_url"))
            out.append(Advisor(
                key=oa or f"row{i}",
                name=row.get("author_name") or "",
                openalex_id=oa,
                ror=ror_id(row.get("ror")) or rors.get(oa or ""),
                country_code=row.get("country_code") or None,
                tier=row.get("tier") or None,
            ))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Join cockpit advisors to atlas people and report match rates.")
    ap.add_argument("--advisors", type=Path, required=True)
    ap.add_argument("--roster", type=Path)
    ap.add_argument("--db", type=Path, default=REPO_ROOT / "research_atlas.duckdb")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    advisors = load_advisors(args.advisors, args.roster)
    con = duckdb.connect(str(args.db), read_only=True)
    report = summarize(match_advisors(con, advisors)).as_dict()
    report["as_of"] = now_iso()
    report["inputs"] = {"advisors": args.advisors.name, "roster": args.roster.name if args.roster else None,
                        "db": args.db.name}
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
