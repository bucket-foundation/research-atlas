from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.users.directories.domains import (
    ROR_DOMAINS,
    normalize_ror,
    record_domains,
)

REPO = Path(__file__).resolve().parents[1]
ROR_DUMP = REPO / "data" / "raw" / "ror" / "v2.8-2026-06-02-ror-data.json"


def display_name(record: dict) -> str | None:
    names = record.get("names") or []
    for n in names:
        if "ror_display" in (n.get("types") or []):
            return n.get("value")
    return names[0].get("value") if names else record.get("name")


def country_code(record: dict) -> str | None:
    for loc in record.get("locations") or []:
        code = (loc.get("geonames_details") or {}).get("country_code")
        if code:
            return code
    return None


def build_rows(records: list[dict]) -> list[dict]:
    return [{
        "ror_id": normalize_ror(r["id"]),
        "name": display_name(r),
        "country_code": country_code(r),
        "domains": record_domains(r),
        "types": list(r.get("types") or []),
    } for r in records if r.get("id")]


def main(argv: list[str] | None = None) -> int:
    import pandas as pd

    ap = argparse.ArgumentParser(description="Build the ror_domains table from the ROR data dump.")
    ap.add_argument("--dump", type=Path, default=ROR_DUMP)
    ap.add_argument("--out", type=Path, default=ROR_DOMAINS)
    args = ap.parse_args(argv)
    rows = build_rows(json.loads(args.dump.read_text()))
    df = pd.DataFrame(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)
    print(json.dumps({"rows": len(df), "with_domains": int(df["domains"].map(len).gt(0).sum())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
