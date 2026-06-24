#!/usr/bin/env python3
"""Resolve grant PIs to canonical people and write the ``grant_pi_person`` bridge.

Bridges funding to the researcher/users layer (paper-03 gap): for every PI
``grant_person`` edge it assembles the PI's name + the grant's recipient-org ROR
+ the grant's field slug, and matches against the canonical OpenAlex-author
people (the ``researchers`` table) with the conservative tiered resolver in
:mod:`atlas.users.pi_resolve`.

Output: ``data/processed/grant_pi_person.parquet`` -- one row per resolved PI
edge, carrying ``person_atlas_id`` (canonical Person), ``orcid`` (when the matched
candidate has one), ``match_method`` and ``match_score``. The table is then
loaded into DuckDB by ``scripts/build_db.py`` and indexed for the funding<->
researcher join.

It also writes an **email-free aggregate** of coverage by funder and by field to
``analysis/pi_resolution.json`` (safe to commit -- counts only, no PII), which is
what the report / paper-04 cites.

Usage:
    python scripts/resolve_pis.py                 # full run against the DuckDB
    python scripts/resolve_pis.py --db /tmp/x.duckdb --out /tmp/bridge.parquet
    python scripts/resolve_pis.py --limit 50000   # quick subset (PI edges)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.schema import now_iso  # noqa: E402
from atlas.users.pi_resolve import PiResolver  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "research_atlas.duckdb"
DEFAULT_OUT = REPO_ROOT / "data" / "processed" / "grant_pi_person.parquet"
DEFAULT_AGG = REPO_ROOT / "analysis" / "pi_resolution.json"

# Bridge table columns (a derived edge: grant_person PI edge -> canonical person)
BRIDGE_COLUMNS = [
    "grant_id",          # the grant (src of the grant_person edge)
    "pi_person_id",      # the funder-sourced PI Person node (dst of the edge)
    "role",              # pi | co-pi
    "person_atlas_id",   # the canonical (OpenAlex-author) Person it resolves to
    "orcid",             # ORCID of the resolved canonical person, when present
    "match_method",      # orcid | name+org+field | name+org
    "match_score",
    "recipient_ror",     # the recipient-org ROR the match was anchored on
    "field_slug",        # the grant field slug used as a signal (may be None)
    "source",            # funder source of the PI edge (nih/nsf/wellcome/...)
    "as_of",
]


def _grant_field_slug_map(con) -> dict[str, str]:
    """grant_id -> roadmap field slug, from the grant's funded works' topics.

    Resolves each grant's modal OpenAlex *field* (topic -> subfield -> field) over
    its linked works, then folds it to the roadmap slug. Only grants that link to
    at least one work get an entry (a minority -- absence => no field signal).
    """
    rows = con.execute(
        """
        WITH topic2field AS (
            SELECT t.atlas_id AS topic_id, f.name AS field_name
            FROM field t
            JOIN field sf ON t.parent_atlas_id = sf.atlas_id AND sf.level='subfield'
            JOIN field f  ON sf.parent_atlas_id = f.atlas_id AND f.level='field'
            WHERE t.level='topic'
        ),
        grant_field AS (
            SELECT gw.src_id AS grant_id, t2f.field_name AS field_name,
                   count(*) AS c
            FROM grant_work gw
            JOIN work_field wf ON wf.src_id = gw.dst_id
            JOIN topic2field t2f ON wf.dst_id = t2f.topic_id
            GROUP BY 1, 2
        ),
        ranked AS (
            SELECT grant_id, field_name,
                   row_number() OVER (PARTITION BY grant_id
                                      ORDER BY c DESC, field_name) AS rn
            FROM grant_field
        )
        SELECT grant_id, field_name FROM ranked WHERE rn = 1
        """
    ).fetchall()
    from atlas.users.schema import field_to_slug
    return {gid: field_to_slug(fname) for gid, fname in rows}


def _recipient_ror_map(con) -> dict[str, str]:
    """grant_id -> recipient-org ROR (one per grant; recipient role preferred).

    A grant can carry several recipient/host org edges; we take the modal
    ROR-resolved *recipient* org (then host as a fallback). One ROR per grant
    keeps the join unambiguous.
    """
    rows = con.execute(
        """
        WITH ge AS (
            SELECT go.src_id AS grant_id, o.ror_id AS ror, go.role AS role,
                   count(*) AS c
            FROM grant_org go
            JOIN organization o ON go.dst_id = o.atlas_id
            WHERE o.ror_id IS NOT NULL
            GROUP BY 1, 2, 3
        ),
        ranked AS (
            SELECT grant_id, ror,
                   row_number() OVER (
                       PARTITION BY grant_id
                       ORDER BY (role='recipient') DESC, c DESC, ror) AS rn
            FROM ge
        )
        SELECT grant_id, ror FROM ranked WHERE rn = 1
        """
    ).fetchall()
    return dict(rows)


def build_resolver(con, progress: bool = True) -> PiResolver:
    """Index the canonical people (OpenAlex-author researchers) for matching."""
    r = PiResolver()
    rows = con.execute(
        "SELECT atlas_id, full_name, orcid, primary_org_ror, field_slug "
        "FROM researchers WHERE primary_org_ror IS NOT NULL"
    ).fetchall()
    for atlas_id, full_name, orcid, ror, slug in rows:
        r.add_candidate(atlas_id=atlas_id, full_name=full_name, orcid=orcid,
                        ror=ror, field_slug=slug)
    if progress:
        print(f"  candidate index: {r.n_candidates:,} OpenAlex-author people "
              f"with a ROR org, {len(r.by_org_surname):,} (ror,surname) buckets")
    return r


def main() -> int:
    import pandas as pd

    ap = argparse.ArgumentParser(description="Resolve grant PIs to canonical people.")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--agg", default=str(DEFAULT_AGG))
    ap.add_argument("--limit", type=int, default=0, help="cap PI edges (0=all)")
    args = ap.parse_args()

    import duckdb

    con = duckdb.connect(args.db, read_only=True)

    print("Building grant field-slug and recipient-ROR maps...")
    field_map = _grant_field_slug_map(con)
    ror_map = _recipient_ror_map(con)
    print(f"  grants with a field signal : {len(field_map):,}")
    print(f"  grants with a recipient ROR: {len(ror_map):,}")

    print("Indexing canonical people...")
    resolver = build_resolver(con)

    print("Resolving PI edges...")
    limit = f"LIMIT {args.limit}" if args.limit else ""
    pi_edges = con.execute(
        f"""
        SELECT gp.src_id, gp.dst_id, gp.role, gp.source,
               p.full_name, p.first_name, p.last_name, p.orcid
        FROM grant_person gp
        JOIN person p ON gp.dst_id = p.atlas_id
        {limit}
        """
    ).fetchall()
    con.close()

    ts = now_iso()
    out_rows: list[dict] = []
    # coverage counters
    n_total = len(pi_edges)
    n_resolved = 0
    n_orcid = 0
    by_funder: dict[str, dict] = {}
    by_field: dict[str, dict] = {}
    by_method: dict[str, int] = {}
    by_reason: dict[str, int] = {}

    for src, dst, role, source, fn, first, last, pi_orcid in pi_edges:
        ror = ror_map.get(src)
        slug = field_map.get(src)
        m = resolver.resolve(full_name=fn, first_name=first, last_name=last,
                             pi_orcid=pi_orcid, recipient_ror=ror,
                             field_slug=slug)
        f_stat = by_funder.setdefault(source, {"edges": 0, "resolved": 0,
                                                "with_orcid": 0})
        f_stat["edges"] += 1
        s_key = slug or "no-field-signal"
        s_stat = by_field.setdefault(s_key, {"edges": 0, "resolved": 0,
                                             "with_orcid": 0})
        s_stat["edges"] += 1

        if m.resolved:
            n_resolved += 1
            f_stat["resolved"] += 1
            s_stat["resolved"] += 1
            by_method[m.method] = by_method.get(m.method, 0) + 1
            if m.orcid:
                n_orcid += 1
                f_stat["with_orcid"] += 1
                s_stat["with_orcid"] += 1
            out_rows.append({
                "grant_id": src,
                "pi_person_id": dst,
                "role": role,
                "person_atlas_id": m.person_atlas_id,
                "orcid": m.orcid,
                "match_method": m.method,
                "match_score": m.score,
                "recipient_ror": ror,
                "field_slug": slug,
                "source": source,
                "as_of": ts,
            })
        else:
            by_reason[m.reason or "unknown"] = by_reason.get(m.reason or "unknown", 0) + 1

    df = pd.DataFrame(out_rows, columns=BRIDGE_COLUMNS)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)

    pct = (100.0 * n_resolved / n_total) if n_total else 0.0
    pct_orcid = (100.0 * n_orcid / n_total) if n_total else 0.0
    print(f"\nWrote {len(df):,} resolved PI->person links -> {args.out}")
    print(f"  PI edges               : {n_total:,}")
    print(f"  resolved to a person   : {n_resolved:,} ({pct:.1f}%)")
    print(f"  resolved to an ORCID   : {n_orcid:,} ({pct_orcid:.1f}%)")
    print("  by method:")
    for k, v in sorted(by_method.items(), key=lambda kv: -kv[1]):
        print(f"    {k:16s} {v:>9,}")
    print("  refusal reasons (unresolved):")
    for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"    {k:18s} {v:>9,}")

    # email-free aggregate for the report / paper-04 (counts only, no PII)
    agg = {
        "generated_at": ts,
        "pi_edges": n_total,
        "resolved": n_resolved,
        "resolved_pct": round(pct, 2),
        "resolved_to_orcid": n_orcid,
        "resolved_to_orcid_pct": round(pct_orcid, 2),
        "distinct_resolved_people": int(df["person_atlas_id"].nunique()) if len(df) else 0,
        "by_method": by_method,
        "by_reason": by_reason,
        "by_funder": _with_pct(by_funder),
        "by_field": _with_pct(by_field),
    }
    Path(args.agg).parent.mkdir(parents=True, exist_ok=True)
    Path(args.agg).write_text(json.dumps(agg, indent=2), encoding="utf-8")
    print(f"\nWrote coverage aggregate -> {args.agg}")
    return 0


def _with_pct(d: dict[str, dict]) -> dict[str, dict]:
    out = {}
    for k, v in d.items():
        e = v["edges"]
        out[k] = {
            **v,
            "resolved_pct": round(100.0 * v["resolved"] / e, 2) if e else 0.0,
        }
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["edges"]))


if __name__ == "__main__":
    raise SystemExit(main())
