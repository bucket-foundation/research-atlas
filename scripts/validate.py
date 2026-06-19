#!/usr/bin/env python3
"""Data-quality validation suite for the research-atlas graph.

Runs after a build and ASSERTS the invariants a trustworthy graph must hold.
Reads the flat ``data/processed/*.parquet`` (the published source of truth) via
DuckDB out-of-core, runs each check, prints a pass/fail table, writes
``docs/VALIDATION.md``, and **exits non-zero if any hard check fails** so it can
gate CI / a publish step.

Checks
------
Hard (failure => non-zero exit):
- referential integrity: every edge endpoint resolves to an existing entity
  (no orphan edges) across all six edge tables;
- money invariant: no ``grant.amount_usd`` <= 0 (unknown must be null);
  ``amount_original``/``currency`` consistency; FX columns consistent;
- ROR ids well-formed (``https://ror.org/<crockford>``);
- no duplicate canonical orgs (one node per ROR id after the merge);
- entity primary keys unique (one row per ``atlas_id``);
- provenance present on every row (``source`` + ``as_of`` non-null);
- ``grant_work`` links point at real grants AND real works.

Soft (reported, never fails the run):
- ROR org coverage (orgs + grant-recipient edges);
- ORCID coverage on people;
- works/fields presence and counts.

Usage:
    python scripts/validate.py
    python scripts/validate.py --processed data/processed --report docs/VALIDATION.md
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas import schema  # noqa: E402
from atlas.connectors.base import DATA_PROCESSED, REPO_ROOT  # noqa: E402
from atlas.schema import now_iso  # noqa: E402

REPORT_PATH = REPO_ROOT / "docs" / "VALIDATION.md"

# the directed edges and the entity table each endpoint must resolve into
EDGE_ENDPOINTS = {
    "funder_grant": ("funder", "grant"),
    "grant_org": ("grant", "organization"),
    "grant_person": ("grant", "person"),
    "grant_work": ("grant", "work"),
    "person_org": ("person", "organization"),
    "work_field": ("work", "field"),
}


@dataclass
class Result:
    name: str
    hard: bool
    passed: bool
    detail: str = ""


@dataclass
class Suite:
    results: list[Result] = field(default_factory=list)

    def check(self, name, hard, passed, detail=""):
        self.results.append(Result(name, hard, bool(passed), detail))

    @property
    def hard_failures(self):
        return [r for r in self.results if r.hard and not r.passed]


def _exists(processed: Path, table: str) -> bool:
    return (processed / f"{table}.parquet").exists()


def _rp(processed: Path, table: str) -> str:
    return f"read_parquet('{processed / f'{table}.parquet'}')"


def run(processed: Path) -> Suite:
    import duckdb

    s = Suite()
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")

    present = {t for t in schema.all_tables() if _exists(processed, t)}

    # ----- referential integrity (HARD) ----------------------------------- #
    for edge, (src_tbl, dst_tbl) in EDGE_ENDPOINTS.items():
        if edge not in present:
            continue
        for endpoint, tbl in (("src_id", src_tbl), ("dst_id", dst_tbl)):
            if tbl not in present:
                s.check(f"{edge}.{endpoint} -> {tbl}", True, False,
                        f"entity table {tbl} missing")
                continue
            orphans = con.execute(f"""
                SELECT count(*) FROM {_rp(processed, edge)} e
                LEFT JOIN {_rp(processed, tbl)} t ON e.{endpoint} = t.atlas_id
                WHERE t.atlas_id IS NULL
            """).fetchone()[0]
            s.check(f"no orphan {edge}.{endpoint} -> {tbl}", True, orphans == 0,
                    f"{orphans:,} orphan endpoints" if orphans else "0 orphans")

    # ----- entity primary-key uniqueness (HARD) ---------------------------- #
    for tbl in schema.ENTITY_COLUMNS:
        if tbl not in present:
            continue
        n, d = con.execute(
            f"SELECT count(*), count(DISTINCT atlas_id) FROM {_rp(processed, tbl)}"
        ).fetchone()
        s.check(f"{tbl}.atlas_id unique", True, n == d,
                f"{n:,} rows / {d:,} distinct" + (
                    f" -- {n - d:,} dup ids" if n != d else ""))

    # ----- provenance present (HARD) --------------------------------------- #
    for tbl in present:
        miss = con.execute(f"""
            SELECT count(*) FROM {_rp(processed, tbl)}
            WHERE source IS NULL OR as_of IS NULL
        """).fetchone()[0]
        s.check(f"{tbl} provenance (source+as_of)", True, miss == 0,
                f"{miss:,} rows missing provenance" if miss else "all rows carry provenance")

    # ----- money invariants (HARD) ----------------------------------------- #
    if "grant" in present:
        g = _rp(processed, "grant")
        bad_zero = con.execute(
            f"SELECT count(*) FROM {g} WHERE amount_usd <= 0").fetchone()[0]
        s.check("grant.amount_usd > 0 or null", True, bad_zero == 0,
                f"{bad_zero:,} rows with amount_usd <= 0" if bad_zero else "ok")

        # if amount_original is set, currency must be set (and vice versa)
        cur_mismatch = con.execute(f"""
            SELECT count(*) FROM {g}
            WHERE (amount_original IS NOT NULL AND currency IS NULL)
               OR (amount_original IS NULL AND currency IS NOT NULL)
        """).fetchone()[0]
        s.check("grant amount/currency consistent", True, cur_mismatch == 0,
                f"{cur_mismatch:,} amount/currency mismatches" if cur_mismatch else "ok")

        # FX consistency: USD amount present => fx_rate present; USD currency => rate 1.0
        fx_bad = con.execute(f"""
            SELECT count(*) FROM {g}
            WHERE (amount_usd IS NOT NULL AND fx_rate_to_usd IS NULL)
               OR (currency = 'USD' AND fx_rate_to_usd IS NOT NULL
                   AND abs(fx_rate_to_usd - 1.0) > 1e-9)
        """).fetchone()[0]
        s.check("grant FX columns consistent", True, fx_bad == 0,
                f"{fx_bad:,} FX inconsistencies" if fx_bad else "ok")

    # ----- ROR well-formed + no duplicate canonical orgs (HARD) ------------ #
    if "organization" in present:
        o = _rp(processed, "organization")
        malformed = con.execute(f"""
            SELECT count(*) FROM {o}
            WHERE ror_id IS NOT NULL
              AND CAST(ror_id AS VARCHAR) NOT SIMILAR TO 'https://ror\\.org/0[a-z0-9]{{8}}'
        """).fetchone()[0]
        s.check("ROR ids well-formed", True, malformed == 0,
                f"{malformed:,} malformed ROR ids" if malformed else "all ROR ids well-formed")

        dup_ror = con.execute(f"""
            SELECT count(*) FROM (
                SELECT ror_id FROM {o} WHERE ror_id IS NOT NULL
                GROUP BY ror_id HAVING count(*) > 1
            )
        """).fetchone()[0]
        s.check("one canonical org per ROR id", True, dup_ror == 0,
                f"{dup_ror:,} ROR ids on >1 node" if dup_ror else "no duplicate canonical orgs")

    # ----- ORCID well-formed (HARD, only on the populated subset) ---------- #
    if "person" in present:
        p = _rp(processed, "person")
        bad_orcid = con.execute(f"""
            SELECT count(*) FROM {p}
            WHERE orcid IS NOT NULL
              AND CAST(orcid AS VARCHAR) NOT SIMILAR TO '[0-9]{{4}}-[0-9]{{4}}-[0-9]{{4}}-[0-9]{{3}}[0-9X]'
        """).fetchone()[0]
        s.check("ORCID ids well-formed", True, bad_orcid == 0,
                f"{bad_orcid:,} malformed ORCIDs" if bad_orcid else "all ORCIDs well-formed")

    # ----- every grant has an awarding funder (HARD) ----------------------- #
    # A grant with no funder_grant(awarder) edge is an orphaned grant: it would
    # never appear in any funder's portfolio. Every connector must attach one.
    if {"grant", "funder_grant"} <= present:
        unfunded = con.execute(f"""
            SELECT count(*) FROM {_rp(processed, 'grant')} g
            LEFT JOIN (
                SELECT DISTINCT dst_id FROM {_rp(processed, 'funder_grant')}
                WHERE role = 'awarder'
            ) fg ON g.atlas_id = fg.dst_id
            WHERE fg.dst_id IS NULL
        """).fetchone()[0]
        s.check("every grant has an awarder", True, unfunded == 0,
                f"{unfunded:,} grants with no funder" if unfunded
                else "all grants attributed to a funder")

    # ----- FX normalization is arithmetically sound (HARD) ----------------- #
    # Where a grant carries amount_original + currency + fx_rate, the normalized
    # amount_usd must equal amount_original * fx_rate within rounding tolerance.
    # This guards every money-bearing connector (Gates/Wellcome/Sloan/UKRI/CORDIS)
    # against a stamped-but-wrong FX.
    if "grant" in present:
        g = _rp(processed, "grant")
        fx_wrong = con.execute(f"""
            SELECT count(*) FROM {g}
            WHERE amount_original IS NOT NULL
              AND fx_rate_to_usd IS NOT NULL
              AND amount_usd IS NOT NULL
              AND abs(amount_usd - (amount_original * fx_rate_to_usd)) > 1.0
        """).fetchone()[0]
        s.check("grant amount_usd = amount_original * fx", True, fx_wrong == 0,
                f"{fx_wrong:,} FX arithmetic mismatches" if fx_wrong
                else "all USD amounts reconcile with their FX rate")

        # Money-bearing rows must carry a non-null fx_as_of date (auditability).
        fx_undated = con.execute(f"""
            SELECT count(*) FROM {g}
            WHERE amount_usd IS NOT NULL AND fx_as_of IS NULL
        """).fetchone()[0]
        s.check("money rows carry an FX date", True, fx_undated == 0,
                f"{fx_undated:,} USD amounts with no fx_as_of" if fx_undated
                else "all money rows stamp an fx_as_of")

    # ----- SOFT coverage metrics ------------------------------------------- #
    if "organization" in present:
        tot, res = con.execute(
            f"SELECT count(*), count(ror_id) FROM {_rp(processed,'organization')}"
        ).fetchone()
        s.check("ROR org coverage", False, True,
                f"{res:,}/{tot:,} orgs ROR-resolved = {res/tot:.1%}" if tot else "n/a")
    if {"grant_org", "organization"} <= present:
        r = con.execute(f"""
            SELECT count(*) tot, count(*) FILTER (WHERE o.ror_id IS NOT NULL) res
            FROM {_rp(processed,'grant_org')} e
            JOIN {_rp(processed,'organization')} o ON e.dst_id=o.atlas_id
            WHERE e.role='recipient'
        """).fetchone()
        if r[0]:
            s.check("ROR grant-recipient coverage", False, True,
                    f"{r[1]:,}/{r[0]:,} recipient edges ROR-resolved = {r[1]/r[0]:.1%}")
    if "person" in present:
        tot, orc = con.execute(
            f"SELECT count(*), count(orcid) FROM {_rp(processed,'person')}"
        ).fetchone()
        s.check("ORCID person coverage", False, True,
                f"{orc:,}/{tot:,} people with ORCID = {orc/tot:.1%}" if tot else "n/a")
    if "work" in present:
        nw = con.execute(f"SELECT count(*) FROM {_rp(processed,'work')}").fetchone()[0]
        s.check("works ingested", False, True, f"{nw:,} works")
    if "grant_work" in present:
        ng = con.execute(f"SELECT count(*) FROM {_rp(processed,'grant_work')}").fetchone()[0]
        s.check("grant->work links", False, True, f"{ng:,} links")

    # Per-source grant + $ breakdown (soft): one line per funder feed so a glance
    # at the report shows every source's real contribution after a build.
    if "grant" in present:
        rows = con.execute(f"""
            SELECT source, count(*) AS grants,
                   count(amount_usd) AS with_money,
                   coalesce(sum(amount_usd), 0) AS usd
            FROM {_rp(processed, 'grant')}
            GROUP BY source ORDER BY grants DESC
        """).fetchall()
        for src, grants, wm, usd in rows:
            s.check(f"source[{src}] grants", False, True,
                    f"{grants:,} grants, {wm:,} w/ amount, ${usd:,.0f} USD")

    con.close()
    return s


def write_report(s: Suite, path: Path) -> None:
    lines = ["# research-atlas — Validation Report", "",
             f"_Generated {now_iso()}_", ""]
    hard = [r for r in s.results if r.hard]
    soft = [r for r in s.results if not r.hard]
    n_pass = sum(1 for r in hard if r.passed)
    status = "PASS" if not s.hard_failures else "FAIL"
    lines += [f"**Status: {status}** — {n_pass}/{len(hard)} hard checks passed, "
              f"{len(s.hard_failures)} failed.", ""]

    lines += ["## Hard checks (gate the build)", "",
              "| check | result | detail |", "|---|---|---|"]
    for r in hard:
        lines.append(f"| {r.name} | {'PASS' if r.passed else 'FAIL'} | {r.detail} |")
    lines += ["", "## Soft metrics (coverage, informational)", "",
              "| metric | value |", "|---|---|"]
    for r in soft:
        lines.append(f"| {r.name} | {r.detail} |")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the atlas graph.")
    ap.add_argument("--processed", default=str(DATA_PROCESSED))
    ap.add_argument("--report", default=str(REPORT_PATH))
    args = ap.parse_args()

    s = run(Path(args.processed))
    write_report(s, Path(args.report))

    hard = [r for r in s.results if r.hard]
    print(f"\nVALIDATION: {sum(r.passed for r in hard)}/{len(hard)} hard checks passed")
    for r in s.results:
        tag = "HARD" if r.hard else "soft"
        mark = "PASS" if r.passed else "FAIL"
        print(f"  [{tag}] {mark}  {r.name:42s} {r.detail}")
    if s.hard_failures:
        print(f"\n{len(s.hard_failures)} HARD CHECK(S) FAILED -> exit 1")
        return 1
    print("\nAll hard checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
