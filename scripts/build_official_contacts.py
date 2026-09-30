from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import REPO_ROOT
from atlas.users.directories.base import OFFICIAL_CACHE
from atlas.users.directories.optout import (
    OPT_OUT,
    TOMBSTONES,
    Suppression,
)

OUT = REPO_ROOT / "data" / "processed" / "contacts_official.parquet"


def fold(cache_root: Path, suppression: Suppression | None = None) -> tuple[list[dict], int]:
    rows, dropped = [], 0
    for path in sorted(cache_root.glob("*/records.jsonl")):
        with path.open() as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    if suppression is not None and (suppression.blocks_record(rec) or suppression.blocks_url(rec.get("profile_url"))):
                        dropped += 1
                        continue
                    rec["departments"] = ";".join(rec.get("departments") or [])
                    rec["dropped_emails"] = len(rec.get("dropped_emails") or [])
                    rec.setdefault("match_tier", None)
                    rec.setdefault("licence", "institution-copyright")
                    rec["storage"] = "link_only"
                    rec.setdefault("retrieved_by", None)
                    rows.append(rec)
    return rows, dropped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fold every official-directory records.jsonl into the private parquet.")
    ap.add_argument("--cache-root", type=Path, default=OFFICIAL_CACHE)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--opt-out", type=Path, default=OPT_OUT)
    ap.add_argument("--tombstones", type=Path, default=TOMBSTONES)
    args = ap.parse_args(argv)
    import pandas as pd

    rows, dropped = fold(args.cache_root, Suppression.load(args.opt_out, args.tombstones))
    df = pd.DataFrame(rows)
    folded = 0
    if not df.empty:
        n = len(df)
        df = df.sort_values("as_of", na_position="first").drop_duplicates(subset=["ror_id", "profile_url"], keep="last")
        folded = n - len(df)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)
    print(json.dumps({"rows": len(df), "emails": int(df["email"].notna().sum()) if "email" in df else 0,
                      "institutions": int(df["ror_id"].nunique()) if len(df) else 0, "opt_out_dropped": dropped, "duplicates_folded": folded, "out": str(args.out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
