"""Funder specialization & temporal stability — reproducible statistics.

Study 03 of the research-atlas metascience series. Where paper 01 measured
*concentration* (who gets grants), *co-funding* (who shares works), and the
*funding->output rate* (works per dollar), this module measures a different
structural feature: **what each funder funds** — the field composition of its
linked output, how *specialized* that portfolio is, how *similar* funders are to
one another, and whether those portfolios *drift* over time.

It computes every headline number in ``docs/papers/03-funder-specialization/``
directly from the canonical DuckDB and writes ``analysis/specialization/
results.json`` (the single source of truth the paper and the test both read).

Honesty discipline (mirrors paper 01, see docs/GRAPH.md):

* **Counts, never dollars.** Every statistic is a *distinct linked-work count*.
  No per-org/funder dollar sum is used anywhere, so the fuzzy-match and
  shared-grant double-counting noise documented in docs/GRAPH.md does not touch
  any result here.
* **Field mapping is via output, so the analysis is funder-bounded.** A grant
  only acquires a field through the works that acknowledge it
  (``grant_work`` -> ``work_field``), and ``grant_work`` edges exist for NIH,
  NSF and the EC only. Every funder in this study is therefore an NIH Institute/
  Center, NSF, or an EC body (EC/ERC). This is the single most important scope
  limit and is repeated wherever it applies.
* **Dense work window.** Field shares use ``work.publication_year`` in
  2016-2024, the window with near-complete OpenAlex coverage in the corpus
  (2015 ramps in, 2025 is partial); see paper 01 §2.1.
* **Uncertainty is quantified.** The temporal-shift claim carries a 2,000-sample
  bootstrap CI; the stability claim carries a rank correlation.

The OpenAlex topic taxonomy is walked recursively (``parent_atlas_id``) to roll
each work's topics up to the 26 top-level *fields* and the 4 *domains*.
All functions are read-only.
"""

from __future__ import annotations

import itertools
import math
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "research_atlas.duckdb"

WORK_YEAR_FROM = 2016
WORK_YEAR_TO = 2024
# Funders kept in the specialization cross-section need enough linked works for
# their field portfolio to be estimable. Conservative floor.
MIN_FUNDER_WORKS = 1000
# Per-window floor for the stability comparison (each window is half as long).
MIN_FUNDER_WORKS_WINDOW = 500


def connect(db_path=None):
    import duckdb

    return duckdb.connect(str(db_path or DEFAULT_DB), read_only=True)


def _rows(con, sql, params=None):
    cur = con.execute(sql, params or [])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Topic -> field26 / domain rollup (built once into a temp table)
# --------------------------------------------------------------------------- #
def build_topic_rollup(con) -> None:
    """Create TEMP table ``topic_roll(topic_id, field26, dom)`` by walking the
    OpenAlex field hierarchy from each topic up to its level-`field` ancestor
    (the 26 top-level fields) and its level-`domain` ancestor (the 4 domains)."""
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE topic_roll AS
        WITH RECURSIVE chain AS (
            SELECT atlas_id AS topic_id, atlas_id AS cur FROM field WHERE level = 'topic'
            UNION ALL
            SELECT c.topic_id, f.parent_atlas_id
            FROM chain c JOIN field f ON f.atlas_id = c.cur
            WHERE f.parent_atlas_id IS NOT NULL
        )
        SELECT c.topic_id,
               max(CASE WHEN d.level = 'field'  THEN d.name END) AS field26,
               max(CASE WHEN d.level = 'domain' THEN d.name END) AS dom
        FROM chain c JOIN field d ON d.atlas_id = c.cur
        WHERE d.level IN ('field', 'domain')
        GROUP BY c.topic_id
        """
    )


# --------------------------------------------------------------------------- #
# Statistics helpers
# --------------------------------------------------------------------------- #
def hhi(values) -> float:
    """Herfindahl-Hirschman Index on shares (sum of squared shares).

    1/n = perfectly even, 1 = a single field has everything. Here it measures
    how *concentrated* a funder's portfolio is across the 26 fields.
    """
    x = np.asarray(values, dtype=float)
    total = x.sum()
    if total == 0:
        return float("nan")
    s = x / total
    return float(np.sum(s ** 2))


def spearman(a, b) -> float:
    """Spearman rank correlation without a scipy dependency."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    ra = a.argsort().argsort().astype(float)
    rb = b.argsort().argsort().astype(float)
    ra -= ra.mean()
    rb -= rb.mean()
    denom = math.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / denom) if denom else float("nan")


def cosine(u, v) -> float:
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    nu, nv = np.linalg.norm(u), np.linalg.norm(v)
    return float(u.dot(v) / (nu * nv)) if nu and nv else float("nan")


# --------------------------------------------------------------------------- #
# Core queries
# --------------------------------------------------------------------------- #
def funder_field_matrix(con, y0=WORK_YEAR_FROM, y1=WORK_YEAR_TO):
    """Return (funders, fields, M) where M[i,j] = distinct works funder i links
    to field j, over works published in [y0, y1]. Scoped to funders with
    output edges (NIH/NSF/EC family) by construction of grant_work."""
    rows = _rows(
        con,
        """
        WITH fw AS (
            SELECT f.short_name AS funder, f.country_code AS cc,
                   tr.field26 AS fld, wf.src_id AS wid
            FROM funder_grant fg
            JOIN funder f       ON fg.src_id = f.atlas_id
            JOIN grant_work gw  ON fg.dst_id = gw.src_id
            JOIN work_field wf  ON wf.src_id = gw.dst_id
            JOIN work w         ON w.atlas_id = wf.src_id
            JOIN topic_roll tr  ON tr.topic_id = wf.dst_id
            WHERE tr.field26 IS NOT NULL
              AND w.publication_year BETWEEN ? AND ?
        )
        SELECT funder, cc, fld, count(DISTINCT wid) AS n
        FROM fw GROUP BY funder, cc, fld
        """,
        [y0, y1],
    )
    funders = sorted({r["funder"] for r in rows})
    fields = sorted({r["fld"] for r in rows})
    cc = {r["funder"]: r["cc"] for r in rows}
    fidx = {f: i for i, f in enumerate(fields)}
    M = np.zeros((len(funders), len(fields)))
    uidx = {f: i for i, f in enumerate(funders)}
    for r in rows:
        M[uidx[r["funder"]], fidx[r["fld"]]] = r["n"]
    return funders, fields, M, cc


def specialization_gradient(con):
    """HHI of each funder's field portfolio + its single dominant field.

    Funders with >= MIN_FUNDER_WORKS linked works in the window are kept so the
    portfolio is estimable. Returns a list sorted by HHI ascending (generalist
    -> specialist)."""
    funders, fields, M, cc = funder_field_matrix(con)
    totals = M.sum(axis=1)
    keep = totals >= MIN_FUNDER_WORKS
    out = []
    for i, f in enumerate(funders):
        if not keep[i]:
            continue
        row = M[i]
        s = row / row.sum()
        top = int(np.argmax(row))
        out.append({
            "funder": f,
            "country_code": cc.get(f),
            "works": int(row.sum()),
            "hhi": round(hhi(row), 4),
            "top_field": fields[top],
            "top_field_share": round(float(s[top]), 4),
            "n_fields_nonzero": int((row > 0).sum()),
        })
    out.sort(key=lambda r: (r["hhi"], r["funder"]))
    return out


def funder_similarity(con):
    """Cosine similarity between funder field-share vectors, plus the summary
    statistics that describe the complementarity structure (NIH-IC internal
    cohesion, NSF / EC distance to the NIH cluster, most-similar/distinct pairs).
    """
    funders, fields, M, cc = funder_field_matrix(con)
    totals = M.sum(axis=1)
    keep_idx = [i for i in range(len(funders)) if totals[i] >= MIN_FUNDER_WORKS]
    kf = [funders[i] for i in keep_idx]
    shares = np.array([M[i] / M[i].sum() for i in keep_idx])

    cos = {}
    pairs = []
    for a, b in itertools.combinations(range(len(kf)), 2):
        c = cosine(shares[a], shares[b])
        cos[(kf[a], kf[b])] = c
        pairs.append({"funder_a": kf[a], "funder_b": kf[b], "cosine": round(c, 4)})
    pairs.sort(key=lambda r: -r["cosine"])

    nih = [f for f in kf if f not in ("NSF", "EC", "ERC")]
    nidx = {f: i for i, f in enumerate(kf)}

    def mean_cos(f, group):
        vals = [cosine(shares[nidx[f]], shares[nidx[g]]) for g in group if g != f]
        return float(np.mean(vals)) if vals else float("nan")

    nih_internal = [cos[(a, b)] if (a, b) in cos else cos[(b, a)]
                    for a, b in itertools.combinations(nih, 2)]

    return {
        "n_funders": len(kf),
        "funders": kf,
        "pairs": pairs,
        "most_similar": pairs[:8],
        "most_distinct": pairs[-8:][::-1],
        "nih_ic_internal_mean_cosine": round(float(np.mean(nih_internal)), 4),
        "nsf_mean_cosine_to_nih": round(mean_cos("NSF", nih), 4) if "NSF" in nidx else None,
        "ec_mean_cosine_to_nih": round(mean_cos("EC", nih), 4) if "EC" in nidx else None,
        "erc_mean_cosine_to_nih": round(mean_cos("ERC", nih), 4) if "ERC" in nidx else None,
    }


def specialization_stability(con):
    """Is a funder's specialization a fixed fingerprint or does it drift?

    Compute each funder's portfolio HHI in an early window (2016-2019) and a
    late window (2021-2024) and report the rank/linear correlation and the mean
    absolute HHI change. High correlation + tiny |ΔHHI| == stable fingerprints.
    """
    def window_hhi(y0, y1):
        funders, fields, M, _ = funder_field_matrix(con, y0, y1)
        totals = M.sum(axis=1)
        d = {}
        for i, f in enumerate(funders):
            if totals[i] >= MIN_FUNDER_WORKS_WINDOW:
                d[f] = hhi(M[i])
        return d

    early = window_hhi(2016, 2019)
    late = window_hhi(2021, 2024)
    common = sorted(set(early) & set(late))
    e = np.array([early[f] for f in common])
    l = np.array([late[f] for f in common])
    per = [{"funder": f, "hhi_early": round(early[f], 4),
            "hhi_late": round(late[f], 4),
            "delta": round(late[f] - early[f], 4)} for f in common]
    per.sort(key=lambda r: r["hhi_early"])
    return {
        "n_funders": len(common),
        "spearman_rank_corr": round(spearman(e, l), 4),
        "pearson_corr": round(float(np.corrcoef(e, l)[0, 1]), 4),
        "mean_abs_delta_hhi": round(float(np.mean(np.abs(l - e))), 4),
        "max_abs_delta_hhi": round(float(np.max(np.abs(l - e))), 4),
        "per_funder": per,
    }


def domain_composition_by_year(con, y0=WORK_YEAR_FROM, y1=WORK_YEAR_TO):
    """Share of distinct linked works in each of the 4 domains, per year."""
    rows = _rows(
        con,
        """
        WITH x AS (
            SELECT DISTINCT wf.src_id AS wid, w.publication_year AS yr, tr.dom AS dom
            FROM work_field wf
            JOIN grant_work gw ON gw.dst_id = wf.src_id
            JOIN work w        ON w.atlas_id = wf.src_id
            JOIN topic_roll tr ON tr.topic_id = wf.dst_id
            WHERE tr.dom IS NOT NULL AND w.publication_year BETWEEN ? AND ?
        )
        SELECT yr, dom, count(DISTINCT wid) AS n
        FROM x GROUP BY yr, dom ORDER BY yr, dom
        """,
        [y0, y1],
    )
    years = sorted({r["yr"] for r in rows})
    domains = sorted({r["dom"] for r in rows})
    by = {(r["yr"], r["dom"]): r["n"] for r in rows}
    series = {d: [] for d in domains}
    for y in years:
        tot = sum(by.get((y, d), 0) for d in domains)
        for d in domains:
            series[d].append(by.get((y, d), 0) / tot if tot else 0.0)
    return {"years": years, "domains": domains, "share": series}


def composition_shift(con, n_boot: int = 2000, seed: int = 1234):
    """The headline temporal claim, computed honestly.

    Endpoint shift in each domain's share (last year - first year) of the dense
    window, with a percentile bootstrap CI obtained by resampling the per-work
    (year, domain) assignments within the two endpoint years. We also report the
    funder-FAMILY mix over time (the obvious confound) and the WITHIN-NSF domain
    composition over time (mix-controlled), so a reader can see whether any
    aggregate shift is a real portfolio change or a composition/coverage
    artifact.
    """
    comp = domain_composition_by_year(con)
    years = comp["years"]
    y0, y1 = years[0], years[-1]

    # Per-work (year, domain) at the two endpoints, one row per (work, domain).
    end_rows = _rows(
        con,
        """
        SELECT DISTINCT wf.src_id AS wid, w.publication_year AS yr, tr.dom AS dom
        FROM work_field wf
        JOIN grant_work gw ON gw.dst_id = wf.src_id
        JOIN work w        ON w.atlas_id = wf.src_id
        JOIN topic_roll tr ON tr.topic_id = wf.dst_id
        WHERE tr.dom IS NOT NULL AND w.publication_year IN (?, ?)
        """,
        [y0, y1],
    )
    domains = comp["domains"]
    didx = {d: i for i, d in enumerate(domains)}

    def shares_for_year(rows):
        cnt = np.zeros(len(domains))
        for r in rows:
            cnt[didx[r["dom"]]] += 1
        return cnt / cnt.sum() if cnt.sum() else cnt

    early_rows = [r for r in end_rows if r["yr"] == y0]
    late_rows = [r for r in end_rows if r["yr"] == y1]
    obs_shift = shares_for_year(late_rows) - shares_for_year(early_rows)

    rng = np.random.default_rng(seed)
    e_dom = np.array([didx[r["dom"]] for r in early_rows])
    l_dom = np.array([didx[r["dom"]] for r in late_rows])
    boots = np.empty((n_boot, len(domains)))
    for b in range(n_boot):
        es = np.bincount(e_dom[rng.integers(0, e_dom.size, e_dom.size)],
                         minlength=len(domains)) / e_dom.size
        ls = np.bincount(l_dom[rng.integers(0, l_dom.size, l_dom.size)],
                         minlength=len(domains)) / l_dom.size
        boots[b] = ls - es

    shift = {}
    for d in domains:
        j = didx[d]
        shift[d] = {
            "early_share": round(float(shares_for_year(early_rows)[j]), 4),
            "late_share": round(float(shares_for_year(late_rows)[j]), 4),
            "delta_pp": round(float(obs_shift[j]) * 100, 2),
            "ci95_pp": [round(float(np.percentile(boots[:, j], 2.5)) * 100, 2),
                        round(float(np.percentile(boots[:, j], 97.5)) * 100, 2)],
        }

    # Confound 1: funder-family mix over time.
    fam_rows = _rows(
        con,
        """
        WITH x AS (
            SELECT DISTINCT gw.dst_id AS wid, w.publication_year AS yr,
                CASE WHEN f.short_name IN ('NSF','EC','ERC')
                     THEN 'NSF/EC family' ELSE 'NIH family' END AS fam
            FROM funder_grant fg
            JOIN funder f      ON fg.src_id = f.atlas_id
            JOIN grant_work gw ON fg.dst_id = gw.src_id
            JOIN work w        ON w.atlas_id = gw.dst_id
            WHERE w.publication_year BETWEEN ? AND ?
        )
        SELECT yr, fam, count(DISTINCT wid) AS n FROM x GROUP BY yr, fam
        """,
        [y0, y1],
    )
    fam_by = {(r["yr"], r["fam"]): r["n"] for r in fam_rows}
    fams = sorted({r["fam"] for r in fam_rows})
    fam_series = {f: [] for f in fams}
    for y in years:
        tot = sum(fam_by.get((y, f), 0) for f in fams)
        for f in fams:
            fam_series[f].append(round(fam_by.get((y, f), 0) / tot, 4) if tot else 0.0)

    # Confound 2: within-NSF domain composition over time (mix-controlled).
    nsf_rows = _rows(
        con,
        """
        WITH x AS (
            SELECT DISTINCT wf.src_id AS wid, w.publication_year AS yr, tr.dom AS dom
            FROM funder_grant fg
            JOIN funder f      ON fg.src_id = f.atlas_id
            JOIN grant_work gw ON fg.dst_id = gw.src_id
            JOIN work_field wf ON wf.src_id = gw.dst_id
            JOIN work w        ON w.atlas_id = wf.src_id
            JOIN topic_roll tr ON tr.topic_id = wf.dst_id
            WHERE f.short_name = 'NSF' AND tr.dom IS NOT NULL
              AND w.publication_year BETWEEN ? AND ?
        )
        SELECT yr, dom, count(DISTINCT wid) AS n FROM x GROUP BY yr, dom
        """,
        [y0, y1],
    )
    nsf_by = {(r["yr"], r["dom"]): r["n"] for r in nsf_rows}
    nsf_series = {d: [] for d in domains}
    for y in years:
        tot = sum(nsf_by.get((y, d), 0) for d in domains)
        for d in domains:
            nsf_series[d].append(round(nsf_by.get((y, d), 0) / tot, 4) if tot else 0.0)
    nsf_phys = nsf_series.get("Physical Sciences", [])
    nsf_phys_shift = (round((nsf_phys[-1] - nsf_phys[0]) * 100, 2)
                      if len(nsf_phys) >= 2 else None)

    return {
        "window": [y0, y1],
        "endpoint_shift": shift,
        "funder_family_mix": {"years": years, "series": fam_series},
        "within_nsf_domain": {"years": years, "series": nsf_series,
                              "physical_sciences_shift_pp": nsf_phys_shift},
    }


def coverage(con):
    """Honest framing numbers."""
    funders, fields, M, _ = funder_field_matrix(con)
    totals = M.sum(axis=1)
    kept = int((totals >= MIN_FUNDER_WORKS).sum())
    total_works = _rows(
        con,
        """
        SELECT count(DISTINCT wf.src_id) AS n
        FROM work_field wf
        JOIN grant_work gw ON gw.dst_id = wf.src_id
        JOIN work w        ON w.atlas_id = wf.src_id
        WHERE w.publication_year BETWEEN ? AND ?
        """,
        [WORK_YEAR_FROM, WORK_YEAR_TO],
    )[0]["n"]
    return {
        "work_window": [WORK_YEAR_FROM, WORK_YEAR_TO],
        "n_funders_with_output_edges": len(funders),
        "n_funders_in_specialization": kept,
        "min_funder_works": MIN_FUNDER_WORKS,
        "n_fields": len(fields),
        "distinct_linked_works_in_window": int(total_works),
        "scope_note": ("grant_work output edges exist for NIH, NSF and the EC "
                       "only; every funder analyzed is an NIH IC, NSF, or an EC "
                       "body (EC/ERC). All counts are distinct-work counts; no "
                       "dollar column is used anywhere in this study."),
    }
