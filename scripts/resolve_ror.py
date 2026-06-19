#!/usr/bin/env python3
"""Resolve every organization to a ROR id from the bulk dump, then dedup.

This is the org side of "4 funding piles -> one connected graph". Orgs are
ingested per-funder keyed on ``make_id("organization", source, name)``, so the
same university appears up to 4x. This script:

1. Loads the ROR bulk dump once and builds an in-memory index
   (:class:`atlas.ror_bulk.RorIndex`).
2. Matches all org names locally (no API calls) -- exact / acronym / fuzzy,
   recording ``match_method`` + ``match_score``; unmatched stay null.
3. Writes a resolution map ``data/processed/org_resolution.parquet``
   (old atlas_id -> ror_id, canonical atlas_id, method, score) -- the audit trail.
4. Rewrites ``organization.parquet`` into canonical nodes: orgs sharing a ROR id
   collapse to one node keyed ``org:<sha1(ror_id)>`` (via make_id), carrying ROR
   geo + a ``source`` provenance list; unmatched orgs keep their original node.
5. Rewrites the ``grant_org`` and ``person_org`` edges to point at canonical ids.

Idempotent: re-running recomputes from the dump + the *original* per-source org
shards is not required -- it operates on the consolidated parquet and is safe to
re-run because the mapping is deterministic (same dump + same names -> same map).
To re-run cleanly after a fresh ingest, restore from the pre-resolution backup
written alongside (``*.preror.parquet``).

Usage:
    python scripts/resolve_ror.py
    python scripts/resolve_ror.py --dump data/raw/ror/v2.8-2026-06-02-ror-data.zip
    python scripts/resolve_ror.py --min-fuzzy 0.85 --limit 0
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from atlas.connectors.base import DATA_PROCESSED, REPO_ROOT  # noqa: E402
from atlas.ror_bulk import RorIndex  # noqa: E402
from atlas.schema import make_id, now_iso  # noqa: E402

DEFAULT_DUMP_DIR = REPO_ROOT / "data" / "raw" / "ror"


def _find_dump() -> Path:
    if not DEFAULT_DUMP_DIR.exists():
        raise SystemExit(
            f"ROR dump dir {DEFAULT_DUMP_DIR} missing -- download the bulk dump "
            "from https://zenodo.org/communities/ror-data into it first."
        )
    zips = sorted(DEFAULT_DUMP_DIR.glob("*ror-data.zip"))
    jsons = sorted(DEFAULT_DUMP_DIR.glob("*ror-data.json"))
    cand = zips or jsons
    if not cand:
        raise SystemExit(f"no ROR dump (*.zip/*.json) in {DEFAULT_DUMP_DIR}")
    return cand[-1]


def _backup(path: Path) -> None:
    bak = path.with_suffix(".preror.parquet")
    if not bak.exists():
        path.replace(bak)
        # leave the backup, write a fresh canonical copy from it
        pd.read_parquet(bak).to_parquet(path, index=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="ROR-resolve + dedup organizations.")
    ap.add_argument("--dump", default=None, help="path to ROR dump zip/json")
    ap.add_argument("--processed", default=str(DATA_PROCESSED))
    ap.add_argument("--min-fuzzy", type=float, default=0.85)
    ap.add_argument("--limit", type=int, default=0,
                    help="limit orgs processed (0 = all; for smoke tests)")
    args = ap.parse_args()

    processed = Path(args.processed)
    org_path = processed / "organization.parquet"
    if not org_path.exists():
        raise SystemExit(f"{org_path} not found -- run ingest + consolidate first")

    dump = Path(args.dump) if args.dump else _find_dump()
    print(f"Loading ROR bulk index from {dump.name} ...")
    idx = RorIndex.from_dump(dump)
    print(f"  indexed {len(idx):,} active ROR orgs "
          f"({len(idx.exact):,} exact-name keys, "
          f"{len(idx.acronym):,} acronyms, {len(idx.fundref):,} fundref ids)")

    orgs = pd.read_parquet(org_path)
    if args.limit:
        orgs = orgs.head(args.limit).copy()
    print(f"Resolving {len(orgs):,} org rows ...")

    # ----- match every org row -------------------------------------------- #
    # Cache by (norm_name, country) so duplicate names across sources match once.
    ror_ids, methods, scores = [], [], []
    canon_names, canon_cc, canon_city = [], [], []
    canon_lat, canon_lon, canon_home, canon_type = [], [], [], []
    cache: dict[tuple[str, str | None], object] = {}
    matched = 0
    for name, cc in zip(orgs["name"].tolist(), orgs["country_code"].tolist()):
        ckey = (name or "", cc)
        if ckey in cache:
            m = cache[ckey]
        else:
            m = idx.match(name or "", cc, min_fuzzy=args.min_fuzzy)
            cache[ckey] = m
        if m is None:
            ror_ids.append(None); methods.append(None); scores.append(None)
            canon_names.append(None); canon_cc.append(None); canon_city.append(None)
            canon_lat.append(None); canon_lon.append(None)
            canon_home.append(None); canon_type.append(None)
        else:
            matched += 1
            r = m.ror
            ror_ids.append(r.ror_id); methods.append(m.method); scores.append(m.score)
            canon_names.append(r.name); canon_cc.append(r.country_code)
            canon_city.append(r.city); canon_lat.append(r.lat); canon_lon.append(r.lon)
            canon_home.append(r.homepage)
            canon_type.append(r.types[0] if r.types else None)

    orgs["_ror_id"] = ror_ids
    orgs["_method"] = methods
    orgs["_score"] = scores

    rate = matched / len(orgs) if len(orgs) else 0.0
    print(f"  matched {matched:,}/{len(orgs):,} = {rate:.1%}")
    by_method = orgs[orgs["_method"].notna()].groupby("_method").size()
    for meth, n in by_method.items():
        print(f"    {meth:8s} {n:>7,}")

    # ----- canonical org id per row --------------------------------------- #
    # resolved -> org:<sha1(ror_id)>; unresolved -> keep original atlas_id
    def canon_id(row):
        if row["_ror_id"]:
            return make_id("organization", row["_ror_id"])
        return row["atlas_id"]

    orgs["canonical_id"] = orgs.apply(canon_id, axis=1)

    # ----- resolution map (audit trail) ----------------------------------- #
    res_map = orgs[["atlas_id", "canonical_id", "_ror_id", "_method", "_score",
                    "name", "source", "source_id"]].rename(
        columns={"_ror_id": "ror_id", "_method": "match_method",
                 "_score": "match_score"})
    res_path = processed / "org_resolution.parquet"
    res_map.to_parquet(res_path, index=False)
    print(f"  wrote resolution map -> {res_path.name} ({len(res_map):,} rows)")

    # ----- build canonical organization nodes ----------------------------- #
    # For resolved orgs, the canonical node carries ROR-authoritative fields and
    # a comma-joined source provenance list. For unresolved, keep the row as-is.
    now = now_iso()
    canon_rows = []

    # group resolved orgs by canonical id; pick ROR fields (identical per group)
    orgs["_canon_name"] = canon_names
    orgs["_canon_cc"] = canon_cc
    orgs["_canon_city"] = canon_city
    orgs["_canon_lat"] = canon_lat
    orgs["_canon_lon"] = canon_lon
    orgs["_canon_home"] = canon_home
    orgs["_canon_type"] = canon_type

    resolved = orgs[orgs["_ror_id"].notna()]
    for cid, grp in resolved.groupby("canonical_id"):
        r0 = grp.iloc[0]
        srcs = ",".join(sorted(grp["source"].dropna().unique().tolist()))
        canon_rows.append({
            "atlas_id": cid,
            "name": r0["_canon_name"] or r0["name"],
            "ror_id": r0["_ror_id"],
            "country_code": r0["_canon_cc"],
            "city": r0["_canon_city"],
            "region": r0["region"],
            "org_type": r0["_canon_type"] or r0["org_type"],
            "homepage": r0["_canon_home"],
            "lat": r0["_canon_lat"],
            "lon": r0["_canon_lon"],
            "source": srcs,
            "source_id": r0["_ror_id"],
            "source_url": r0["_ror_id"],
            "as_of": now,
        })

    unresolved = orgs[orgs["_ror_id"].isna()]
    ent_cols = ["atlas_id", "name", "ror_id", "country_code", "city", "region",
                "org_type", "homepage", "lat", "lon", "source", "source_id",
                "source_url", "as_of"]
    canon_df = pd.DataFrame(canon_rows, columns=ent_cols)
    unres_df = unresolved[ent_cols].copy()
    out = pd.concat([canon_df, unres_df], ignore_index=True)

    n_before = len(orgs)
    n_after = len(out)
    n_resolved_nodes = len(canon_df)
    print(f"  canonical org nodes: {n_after:,} "
          f"(was {n_before:,}; {n_resolved_nodes:,} ROR-resolved canonical, "
          f"{len(unres_df):,} unmatched kept) -- "
          f"deduped {n_before - n_after:,} duplicate rows")

    # ----- rewrite organization.parquet (backup original) ---------------- #
    _backup(org_path)
    out.to_parquet(org_path, index=False)

    # ----- remap edges: old org atlas_id -> canonical id ------------------ #
    remap = dict(zip(orgs["atlas_id"], orgs["canonical_id"]))
    for edge in ("grant_org", "person_org"):
        epath = processed / f"{edge}.parquet"
        if not epath.exists():
            continue
        edf = pd.read_parquet(epath)
        _backup(epath)
        before = len(edf)
        edf["dst_id"] = edf["dst_id"].map(lambda x: remap.get(x, x))
        # collapse edges that became identical after remap
        key = ["src_id", "dst_id", "role", "source"]
        edf = edf.sort_values("as_of").drop_duplicates(subset=key, keep="last")
        edf.to_parquet(epath, index=False)
        print(f"  rewrote {edge}: {before:,} -> {len(edf):,} edges "
              f"(collapsed {before - len(edf):,})")

    print("\nDone. Re-run scripts/build_db.py + manifest to publish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
