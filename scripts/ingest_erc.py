#!/usr/bin/env python3
"""Ingest ERC grants into the atlas, from CORDIS bulk exports.

Reuses the CORDIS archives already downloaded by the sibling biophysics-phd-review
project. The originals are NEVER modified -- this script copies them into this
repo's raw cache (data/raw/cordis/) on first run, then reads the ERC slice.

Sample-friendly; idempotent + resumable. No network for the bulk feed (ROR
resolution still hits the network unless --no-ror).

Usage:
    python scripts/ingest_erc.py --limit 200
    python scripts/ingest_erc.py --limit 100 --no-ror
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import DATA_RAW  # noqa: E402
from atlas.connectors.erc import ErcConnector  # noqa: E402
from atlas.manifest import build_manifest  # noqa: E402

# Where the sibling project downloaded the CORDIS archives.
SIBLING_CORDIS = Path(
    "/home/gian/agfarms/biophysics-phd-review/data/raw"
)
CORDIS_ARCHIVES = ("cordis_horizon.zip", "cordis_h2020.zip")


def _ensure_cordis_cache() -> Path:
    """Copy the sibling CORDIS zips into data/raw/cordis/ (idempotent)."""
    dest = DATA_RAW / "cordis"
    dest.mkdir(parents=True, exist_ok=True)
    for name in CORDIS_ARCHIVES:
        src = SIBLING_CORDIS / name
        dst = dest / name
        if dst.exists():
            continue
        if src.exists():
            print(f"  copying {name} -> data/raw/cordis/ ({src.stat().st_size:,} B)")
            shutil.copy2(src, dst)
        else:
            print(f"  WARNING: sibling archive not found: {src}")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest ERC grants from CORDIS.")
    ap.add_argument("--limit", type=int, default=200, help="max ERC projects")
    ap.add_argument("--no-ror", action="store_true", help="skip ROR resolution")
    args = ap.parse_args()

    cordis_dir = _ensure_cordis_cache()
    print(f"ERC ingest: limit={args.limit} ror={'off' if args.no_ror else 'on'}")
    conn = ErcConnector(resolve_ror=not args.no_ror)
    # ErcConnector reads cordis_*.zip from its own raw_dir (data/raw/erc/), so
    # point it at the shared cordis cache explicitly.
    zips = sorted(cordis_dir.glob("cordis_*.zip"))
    counts = conn.run(zips=zips, limit=args.limit)
    print("\nEmitted rows per table:")
    for table, n in sorted(counts.items()):
        print(f"  {table:16s} {n:>7,}")

    manifest = build_manifest()
    print(f"\nManifest: {manifest['totals']['tables']} tables, "
          f"{manifest['totals']['rows']:,} rows -> data/MANIFEST.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
