"""Funding-landscape study — reproducible statistics over the research-atlas graph.

This module computes every headline number in the paper
``docs/papers/01-funding-landscape/`` directly from the canonical DuckDB. It is
the single source of truth: the paper, the figures, and the validation test all
consume the JSON it writes (``analysis/results.json``), so the prose can never
drift from the data.

Design principles (mirroring docs/GRAPH.md's honesty guardrail):

* **Counts over dollars.** Concentration, co-funding and field-dynamics results
  are built on *grant counts* and *work counts*, which are not affected by the
  fuzzy-match / shared-grant double-counting that adds noise to per-org dollar
  sums. Dollar aggregates are reported only at the country and funder level
  (robust) and are explicitly flagged where the org-level dollar column is noisy.
* **ROR-resolved subset.** Organization-level concentration is computed on the
  ROR-resolved recipient subset, where institution identity is canonical.
* **Output linkage is funder-bounded.** ``grant_work`` links cover NIH, NSF and
  the European Commission (EC) only; UKRI outputs are not yet ingested. Every
  funding->output statistic is therefore scoped to {NIH, NSF, EC} and says so.
* **Uncertainty is quantified.** Gini coefficients carry bootstrap 95% CIs;
  funding->output rates carry the raw counts they are built from.

All functions are read-only. Run ``python analysis/run.py`` to regenerate
``analysis/results.json`` and the figures.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "research_atlas.duckdb"

# Dense, well-covered grant window (see docs/GRAPH.md §3 and exploratory counts).
YEAR_FROM = 2015
YEAR_TO = 2025
# This paper analyzes the four large *government/supranational* research funders
# the atlas ingested first. Later-added private foundations (Gates, Wellcome,
# Sloan) and the DFG HTML corpus are part of the broader atlas (see
# docs/LANDSCAPE.md) but are deliberately OUT OF SCOPE for this paper so its
# published numbers stay reproducible as the atlas grows. The filter is applied
# uniformly to every windowed query below.
PAPER_FUNDER_SOURCES = ("nih", "nsf", "cordis", "ukri")
_PAPER_SRC_LIST = ", ".join(f"'{s}'" for s in PAPER_FUNDER_SOURCES)
PAPER_SOURCE_CLAUSE = f"AND g.source IN ({_PAPER_SRC_LIST})"
# Funders with grant_work output linkage (UKRI outputs not yet ingested).
OUTPUT_FUNDER_SOURCES = ("nih", "nsf", "cordis")
# Work publication window with near-complete coverage (2025 is partial, 2026 negligible).
WORK_YEAR_FROM = 2016
WORK_YEAR_TO = 2024


def connect(db_path=None):
    import duckdb

    return duckdb.connect(str(db_path or DEFAULT_DB), read_only=True)


def _scalar(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()[0]


def _rows(con, sql, params=None):
    cur = con.execute(sql, params or [])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Inequality statistics
# --------------------------------------------------------------------------- #
def gini(values) -> float:
    """Gini coefficient of a 1-D array of non-negative values.

    0 = perfect equality, 1 = maximal concentration. Uses the standard
    rank-weighted formulation, which is exact for discrete samples.
    """
    x = np.sort(np.asarray(values, dtype=float))
    n = x.size
    if n == 0 or x.sum() == 0:
        return float("nan")
    if (x < 0).any():
        raise ValueError("Gini is undefined for negative values")
    index = np.arange(1, n + 1)
    return float((np.sum((2 * index - n - 1) * x)) / (n * x.sum()))


def hhi(values) -> float:
    """Herfindahl-Hirschman Index on shares (sum of squared shares).

    Reported on a 0-1 scale (multiply by 1e4 for the antitrust convention).
    1/n = perfectly even split across n players, 1 = a single player has all.
    """
    x = np.asarray(values, dtype=float)
    total = x.sum()
    if total == 0:
        return float("nan")
    shares = x / total
    return float(np.sum(shares ** 2))


def gini_bootstrap_ci(values, n_boot: int = 2000, seed: int = 1234,
                      alpha: float = 0.05) -> tuple[float, float, float]:
    """Point estimate + percentile bootstrap CI for the Gini coefficient."""
    x = np.asarray(values, dtype=float)
    point = gini(x)
    rng = np.random.default_rng(seed)
    n = x.size
    boots = np.empty(n_boot)
    for i in range(n_boot):
        boots[i] = gini(x[rng.integers(0, n, n)])
    lo = float(np.nanpercentile(boots, 100 * alpha / 2))
    hi = float(np.nanpercentile(boots, 100 * (1 - alpha / 2)))
    return point, lo, hi


def top_share(values, k_frac: float) -> float:
    """Share of total held by the top ``k_frac`` fraction of holders."""
    x = np.sort(np.asarray(values, dtype=float))[::-1]
    if x.sum() == 0:
        return float("nan")
    k = max(1, int(math.ceil(len(x) * k_frac)))
    return float(x[:k].sum() / x.sum())


# --------------------------------------------------------------------------- #
# Data pulls
# --------------------------------------------------------------------------- #
def org_grant_counts(con, ror_only: bool = True) -> list[dict]:
    """Recipient grant count per organization in the study window.

    Count-based (robust). When ``ror_only`` the long unmatched tail of one-off
    SBIR/SME recipients is excluded so org identity is canonical.
    """
    ror_clause = "AND o.ror_id IS NOT NULL" if ror_only else ""
    return _rows(con, f"""
        SELECT o.atlas_id, o.name, o.country_code, o.ror_id,
               count(DISTINCT go.src_id) AS grants
        FROM grant_org go
        JOIN grant g        ON g.atlas_id = go.src_id
        JOIN organization o ON o.atlas_id = go.dst_id
        WHERE go.role = 'recipient'
          AND substr(g.start_date, 1, 4) BETWEEN ? AND ?
          {PAPER_SOURCE_CLAUSE}
          {ror_clause}
        GROUP BY 1, 2, 3, 4
        ORDER BY grants DESC, o.atlas_id   -- deterministic order for reproducible bootstrap
    """, [str(YEAR_FROM), str(YEAR_TO)])


def org_grant_dollars(con) -> list[dict]:
    """Recipient dollar sum per ROR org — the NOISY column, for sensitivity only."""
    return _rows(con, f"""
        SELECT o.atlas_id, o.name, o.country_code,
               sum(g.amount_usd) AS usd
        FROM grant_org go
        JOIN grant g        ON g.atlas_id = go.src_id
        JOIN organization o ON o.atlas_id = go.dst_id
        WHERE go.role = 'recipient' AND o.ror_id IS NOT NULL
          AND g.amount_usd IS NOT NULL
          AND substr(g.start_date, 1, 4) BETWEEN ? AND ?
          {PAPER_SOURCE_CLAUSE}
        GROUP BY 1, 2, 3
        HAVING sum(g.amount_usd) > 0
        ORDER BY usd DESC, o.atlas_id   -- deterministic order for reproducible bootstrap
    """, [str(YEAR_FROM), str(YEAR_TO)])


def country_funding(con) -> list[dict]:
    """Recipient grant count + dollar sum by org country (robust aggregate)."""
    return _rows(con, f"""
        SELECT o.country_code,
               count(DISTINCT g.atlas_id) AS grants,
               sum(g.amount_usd) AS usd
        FROM grant_org go
        JOIN grant g        ON g.atlas_id = go.src_id
        JOIN organization o ON o.atlas_id = go.dst_id
        WHERE go.role = 'recipient' AND o.country_code IS NOT NULL
          AND substr(g.start_date, 1, 4) BETWEEN ? AND ?
          {PAPER_SOURCE_CLAUSE}
        GROUP BY 1
        ORDER BY grants DESC, o.country_code
    """, [str(YEAR_FROM), str(YEAR_TO)])


def funder_pair_cofunding(con, limit: int = 25) -> list[dict]:
    """Funder pairs that co-acknowledge the same works.

    For each work linked to >=2 distinct funders, every unordered funder pair on
    that work gets a co-funding tally. This is the cross-funder collaboration
    network, recovered from the output side (works), independent of dollars.
    """
    return _rows(con, """
        WITH work_funder AS (
            SELECT DISTINCT gw.dst_id AS work_id, fg.src_id AS funder_id
            FROM grant_work gw
            JOIN funder_grant fg ON fg.dst_id = gw.src_id
        )
        SELECT fa.short_name AS funder_a, fb.short_name AS funder_b,
               count(*) AS shared_works
        FROM work_funder a
        JOIN work_funder b ON a.work_id = b.work_id AND a.funder_id < b.funder_id
        JOIN funder fa ON fa.atlas_id = a.funder_id
        JOIN funder fb ON fb.atlas_id = b.funder_id
        GROUP BY 1, 2
        ORDER BY shared_works DESC, funder_a, funder_b
        LIMIT ?
    """, [limit])


def cofunding_summary(con) -> dict:
    """How many works carry 1 vs >1 distinct funder."""
    rows = _rows(con, """
        WITH wf AS (
            SELECT gw.dst_id AS work_id, count(DISTINCT fg.src_id) AS n_funders
            FROM grant_work gw
            JOIN funder_grant fg ON fg.dst_id = gw.src_id
            GROUP BY 1
        )
        SELECT n_funders, count(*) AS works FROM wf GROUP BY 1 ORDER BY 1
    """)
    total = sum(r["works"] for r in rows)
    multi = sum(r["works"] for r in rows if r["n_funders"] >= 2)
    return {
        "distribution": rows,
        "total_linked_works": total,
        "multi_funder_works": multi,
        "multi_funder_share": (multi / total) if total else float("nan"),
    }


def funder_output_rate(con) -> list[dict]:
    """Works per $M by funder, scoped to funders with output linkage.

    Robust at the funder level: dollars are the funder's total awarded in the
    window (not subject to per-org fuzzy reattribution); works are distinct
    linked works in the publication window.
    """
    return _rows(con, """
        WITH funded AS (
            SELECT f.atlas_id, f.short_name, f.country_code,
                   g.atlas_id AS grant_id, g.amount_usd
            FROM funder f
            JOIN funder_grant fg ON fg.src_id = f.atlas_id
            JOIN grant g         ON g.atlas_id = fg.dst_id
            WHERE g.source IN ('nih', 'nsf', 'cordis')
              AND g.amount_usd IS NOT NULL
              AND substr(g.start_date, 1, 4) BETWEEN ? AND ?
        ),
        works AS (
            SELECT fg.src_id AS funder_id, count(DISTINCT gw.dst_id) AS works
            FROM grant_work gw
            JOIN funder_grant fg ON fg.dst_id = gw.src_id
            JOIN work w          ON w.atlas_id = gw.dst_id
            WHERE w.publication_year BETWEEN ? AND ?
            GROUP BY 1
        )
        SELECT fu.short_name AS funder, fu.country_code,
               count(DISTINCT fd.grant_id) AS grants,
               sum(fd.amount_usd) AS usd,
               coalesce(wk.works, 0) AS works,
               round(coalesce(wk.works, 0) / nullif(sum(fd.amount_usd), 0) * 1e6, 3)
                   AS works_per_million_usd
        FROM funded fd
        JOIN funder fu ON fu.atlas_id = fd.atlas_id
        LEFT JOIN works wk ON wk.funder_id = fd.atlas_id
        GROUP BY 1, 2, wk.works
        HAVING count(DISTINCT fd.grant_id) >= 100
        ORDER BY works_per_million_usd DESC NULLS LAST, funder
    """, [str(YEAR_FROM), str(YEAR_TO), WORK_YEAR_FROM, WORK_YEAR_TO])


def domain_composition(con) -> list[dict]:
    """Funded-work counts by OpenAlex domain (top of the field taxonomy)."""
    return _rows(con, """
        WITH RECURSIVE chain AS (
            SELECT atlas_id AS topic_id, atlas_id AS cur FROM field WHERE level = 'topic'
            UNION ALL
            SELECT c.topic_id, f.parent_atlas_id
            FROM chain c JOIN field f ON f.atlas_id = c.cur
            WHERE f.parent_atlas_id IS NOT NULL
        ),
        td AS (
            SELECT c.topic_id, d.name AS dom
            FROM chain c JOIN field d ON d.atlas_id = c.cur WHERE d.level = 'domain'
        )
        SELECT td.dom AS domain, count(DISTINCT wf.src_id) AS works
        FROM work_field wf
        JOIN grant_work gw ON gw.dst_id = wf.src_id
        JOIN td ON td.topic_id = wf.dst_id
        GROUP BY 1
        ORDER BY works DESC, domain
    """)


def rising_falling_fields(con, min_total: int = 40, limit: int = 12) -> dict:
    """Topics whose funded-work volume rose or fell most, early vs late window.

    Early = 2016-2019, late = 2021-2024 (symmetric 4-year windows, omitting the
    2020 COVID inflection year as a clean break). Growth = late / early.
    """
    rows = _rows(con, """
        WITH counts AS (
            SELECT fld.name,
                   count(DISTINCT w.atlas_id) FILTER (
                       WHERE w.publication_year BETWEEN 2016 AND 2019) AS early,
                   count(DISTINCT w.atlas_id) FILTER (
                       WHERE w.publication_year BETWEEN 2021 AND 2024) AS late
            FROM field fld
            JOIN work_field wf ON wf.dst_id = fld.atlas_id
            JOIN work w        ON w.atlas_id = wf.src_id
            JOIN grant_work gw ON gw.dst_id = w.atlas_id
            WHERE fld.level = 'topic'
            GROUP BY 1
        )
        SELECT name, early, late, (early + late) AS total,
               round(late / nullif(early, 0)::DOUBLE, 2) AS growth_ratio
        FROM counts
        WHERE (early + late) >= ?
    """, [min_total])
    # stable tiebreak on (-total, name) so equal growth ratios order deterministically
    rising = sorted(
        [r for r in rows if r["early"] and r["early"] > 0],
        key=lambda r: (-r["growth_ratio"], -r["total"], r["name"]))[:limit]
    falling = sorted(
        [r for r in rows if r["late"] and r["early"] and r["late"] > 0],
        key=lambda r: (r["growth_ratio"], -r["total"], r["name"]))[:limit]
    return {"rising": rising, "falling": falling}


def coverage_stats(con) -> dict:
    """The honest coverage numbers that frame every result."""
    grants_window = _scalar(con, f"""
        SELECT count(*) FROM grant g
        WHERE substr(g.start_date, 1, 4) BETWEEN ? AND ?
          {PAPER_SOURCE_CLAUSE}
    """, [str(YEAR_FROM), str(YEAR_TO)])
    recip_total, recip_ror = con.execute(f"""
        SELECT count(*),
               count(*) FILTER (WHERE o.ror_id IS NOT NULL)
        FROM grant_org go
        JOIN grant g        ON g.atlas_id = go.src_id
        JOIN organization o ON o.atlas_id = go.dst_id
        WHERE go.role = 'recipient'
          AND substr(g.start_date, 1, 4) BETWEEN ? AND ?
          {PAPER_SOURCE_CLAUSE}
    """, [str(YEAR_FROM), str(YEAR_TO)]).fetchone()
    return {
        "year_from": YEAR_FROM,
        "year_to": YEAR_TO,
        "grants_in_window": grants_window,
        "recipient_edges_in_window": recip_total,
        "recipient_edges_ror_resolved": recip_ror,
        "ror_recipient_coverage": recip_ror / recip_total if recip_total else float("nan"),
        "grant_work_links": _scalar(con, "SELECT count(*) FROM grant_work"),
        "linked_works": _scalar(con, "SELECT count(DISTINCT dst_id) FROM grant_work"),
        "output_funder_sources": list(OUTPUT_FUNDER_SOURCES),
        "work_year_from": WORK_YEAR_FROM,
        "work_year_to": WORK_YEAR_TO,
    }
