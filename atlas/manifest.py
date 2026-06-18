"""The atlas manifest -- the authoritative record of what is published.

``data/MANIFEST.json`` lists every published dataset (parquet path, schema
version, row count, as_of, contributing sources). It is the CANON_INDEX of the
atlas: if a parquet is not in the manifest, treat it as not-published. The
manifest is what a downstream Bucket canon artifact cites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from atlas import schema
from atlas.connectors.base import DATA_PROCESSED, REPO_ROOT
from atlas.schema import now_iso

MANIFEST_PATH = REPO_ROOT / "data" / "MANIFEST.json"


def build_manifest(processed_dir: Path | None = None,
                   manifest_path: Path | None = None) -> dict:
    """Scan ``data/processed`` and (re)write the manifest. Idempotent."""
    processed_dir = processed_dir or DATA_PROCESSED
    manifest_path = manifest_path or MANIFEST_PATH

    datasets = []
    for table in schema.all_tables():
        path = processed_dir / f"{table}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        kind = "entity" if table in schema.ENTITY_COLUMNS else "edge"
        sources = sorted(df["source"].dropna().unique().tolist()) \
            if "source" in df.columns else []
        as_of = max(df["as_of"].dropna()) if "as_of" in df.columns and len(df) else None
        datasets.append({
            "table": table,
            "kind": kind,
            "path": str(path.relative_to(REPO_ROOT)),
            "schema_version": schema.SCHEMA_VERSION,
            "row_count": int(len(df)),
            "columns": list(df.columns),
            "sources": sources,
            "as_of": as_of,
        })

    # Per-source grant + $ breakdown (real funder totals) when a grant parquet
    # is present. Money is USD-normalized; unknown amounts contribute null.
    by_source: dict = {}
    grant_path = processed_dir / "grant.parquet"
    if grant_path.exists():
        g = pd.read_parquet(grant_path, columns=["source", "amount_usd"])
        agg = g.groupby("source").agg(
            grants=("source", "size"),
            usd_funded=("amount_usd", "sum"),
            grants_with_amount=("amount_usd", "count"),
        )
        for src, row in agg.iterrows():
            usd = row["usd_funded"]
            by_source[str(src)] = {
                "grants": int(row["grants"]),
                "grants_with_amount": int(row["grants_with_amount"]),
                "usd_funded": float(usd) if pd.notna(usd) else None,
            }

    manifest = {
        "name": "research-atlas",
        "description": "Normalized graph of the global research economy.",
        "schema_version": schema.SCHEMA_VERSION,
        "generated_at": now_iso(),
        "license": "MIT (code) / CC-BY-4.0 (data)",
        "publisher": "bucket-foundation",
        "datasets": datasets,
        "totals": {
            "tables": len(datasets),
            "rows": sum(d["row_count"] for d in datasets),
            "by_source": by_source,
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
