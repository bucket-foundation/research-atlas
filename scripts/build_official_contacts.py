from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import REPO_ROOT  # noqa: E402
from atlas.users.directories.base import OFFICIAL_CACHE  # noqa: E402
from atlas.users.haul import OptOut  # noqa: E402

OPT_OUT = REPO_ROOT / "data" / "private" / "opt_out.csv"
OUT = REPO_ROOT / "data" / "processed" / "contacts_official.parquet"


def fold(cache_root: Path, opt_out: OptOut | None = None) -> tuple[list[dict], int]:
    opt_out = opt_out or OptOut()
    rows, dropped = [], 0
    for path in sorted(cache_root.glob("*/records.jsonl")):
        with path.open() as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    if opt_out.matches(rec):
                        dropped += 1
                        continue
                    rec["departments"] = ";".join(rec.get("departments") or [])
                    rec["dropped_emails"] = len(rec.get("dropped_emails") or [])
                    rec.setdefault("match_tier", None)
                    rec.setdefault("licence", "institution-copyright")
                    rows.append(rec)
    return rows, dropped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fold every official-directory records.jsonl into the private parquet.")
    ap.add_argument("--cache-root", type=Path, default=OFFICIAL_CACHE)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--opt-out", type=Path, default=OPT_OUT)
    args = ap.parse_args(argv)
    import pandas as pd

    rows, dropped = fold(args.cache_root, OptOut.load(args.opt_out))
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["ror_id", "slug"], keep="last")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)
    print(json.dumps({"rows": len(df), "emails": int(df["email"].notna().sum()) if len(df) else 0,
                      "institutions": int(df["ror_id"].nunique()) if len(df) else 0, "opt_out_dropped": dropped, "out": str(args.out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
