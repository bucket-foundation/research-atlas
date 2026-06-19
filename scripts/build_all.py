#!/usr/bin/env python3
"""One-shot rebuild: consolidate -> manifest -> DuckDB -> validate.

Runs the post-ingest pipeline end to end so the published parquet, the manifest,
the DuckDB graph DB, and the validation report are all regenerated from the
current shards/flat parquet in one command. Exits non-zero if validation's hard
checks fail (so it can gate CI / a publish step).

Usage:
    python scripts/build_all.py
    python scripts/build_all.py --skip-consolidate   # parquet already flat
    python scripts/build_all.py --memory-limit 6GB --temp-dir /tmp/atlas_duck
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str]) -> int:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild the atlas end to end.")
    ap.add_argument("--skip-consolidate", action="store_true")
    ap.add_argument("--memory-limit", default="6GB")
    ap.add_argument("--temp-dir", default="/tmp/atlas_duck")
    args = ap.parse_args()

    py = sys.executable
    if not args.skip_consolidate:
        rc = run([py, "scripts/consolidate.py",
                  "--memory-limit", args.memory_limit,
                  "--temp-dir", args.temp_dir])
        if rc:
            return rc
    else:
        # consolidate also rebuilds the manifest; rebuild it explicitly otherwise
        rc = run([py, "-c",
                  "from atlas.manifest import build_manifest; build_manifest()"])
        if rc:
            return rc

    rc = run([py, "scripts/build_db.py"])
    if rc:
        return rc

    # validate last; its exit code gates the whole build
    rc = run([py, "scripts/validate.py"])
    print("\nbuild_all complete." if rc == 0
          else "\nbuild_all: VALIDATION FAILED.", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
