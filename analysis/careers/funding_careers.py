"""Public funding and researcher careers — reproducible statistics.

Study 04 of the research-atlas metascience series. Papers 01/02/03 never touched
the people side: a grant's PI was a name-only ``person`` node with no ORCID, a
*different node set* from the canonical OpenAlex-author ``researchers`` layer, so
funding could not be joined to careers at all. The ``grant_pi_person`` bridge
(``atlas/users/pi_resolve.py``) closes that gap — conservatively — and lets us ask
how public funding maps onto researcher careers.

THE CENTRAL METHODOLOGICAL RISK (handled, not hidden)
-----------------------------------------------------
Only **30.4%** of PI edges resolve to a canonical researcher, and they resolve
*precisely toward* ORCID-era, OpenAlex-indexed, more-productive researchers (the
resolver requires a ROR-resolved recipient org and an OpenAlex-author candidate
sharing org + surname + given name). So the resolved "funded" population is a
**positively selected** sample of all grant-holders, and the canonical
``researchers`` layer is itself a positively selected sample of all researchers.
A naive "funded researchers publish 2x more than all researchers" is therefore
**confounded by resolution/selection bias, not a funding effect**. This module:

* **states the bias quantitatively** (`resolution_bias`): funded researchers are
  far more ORCID'd and more productive than the researcher pool they are drawn
  from — that gap is *selection*, the thing to be controlled, not a finding;
* **never claims a causal return to funding.** Every comparison is framed as the
  DESCRIPTIVE structure of the *resolvable* population;
* **restricts every comparison to the resolvable population** — researchers at
  the same grant-receiving (ROR-resolved) organizations who *could* have been
  matched — and then **matches on field x career-stage x entry-era** so the
  funded/comparison contrast holds those confounds fixed (`matched_productivity`);
* **leans on within-funder / within-population structure** that does not depend
  on the unfunded baseline at all (`funder_portfolios`, `stage_composition`,
  `portfolio_concentration`).

Honesty discipline (mirrors papers 01/03, see docs/GRAPH.md):

* **Counts and per-researcher proxies, never dollars.** Every statistic is a
  distinct-person count or a per-researcher productivity proxy (works, citations,
  h-index proxy) computed over the atlas corpus. No per-org/funder dollar sum is
  used, so the fuzzy-match and shared-grant double-counting noise documented in
  docs/GRAPH.md cannot touch any number here.
* **Career stage / productivity are atlas-corpus proxies** (``researchers``
  table, ``atlas/users/segment.py``), bounded by the corpus window and biased to
  *under*-claim — the honest direction.
* **Uncertainty is quantified.** The matched-productivity ratio carries a
  2,000-sample stratum bootstrap CI.

All functions are read-only.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "research_atlas.duckdb"

# Funders with enough resolved grant-holders to estimate a researcher-portfolio.
MIN_FUNDER_PEOPLE = 300
# A matched stratum is kept only if it has this many people in BOTH groups, so
# each within-stratum mean is estimable and the match is honest.
MIN_STRATUM = 30
SENIORITY_ORDER = ["eminent", "established", "rising-star", "early/unknown"]
BOOT_N = 2000
BOOT_SEED = 42


def connect(db_path=None):
    import duckdb

    return duckdb.connect(str(db_path or DEFAULT_DB), read_only=True)


def _rows(con, sql, params=None):
    cur = con.execute(sql, params or [])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _scalar(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()[0]


# --------------------------------------------------------------------------- #
# Coverage + the resolution-bias statement (front and centre)
# --------------------------------------------------------------------------- #
def coverage(con) -> dict:
    """Resolution coverage of the grant_pi_person bridge."""
    edges = _scalar(con, "SELECT count(*) FROM grant_person")
    resolved = _scalar(con, "SELECT count(*) FROM grant_pi_person")
    with_orcid = _scalar(con, "SELECT count(*) FROM grant_pi_person WHERE orcid IS NOT NULL")
    distinct_people = _scalar(con, "SELECT count(DISTINCT person_atlas_id) FROM grant_pi_person")
    distinct_orgs = _scalar(
        con, "SELECT count(DISTINCT recipient_ror) FROM grant_pi_person "
             "WHERE recipient_ror IS NOT NULL")
    by_method = {m: n for m, n in con.execute(
        "SELECT match_method, count(*) FROM grant_pi_person "
        "GROUP BY 1 ORDER BY 2 DESC").fetchall()}
    return {
        "pi_edges": edges,
        "resolved_edges": resolved,
        "resolved_pct": round(100.0 * resolved / edges, 2) if edges else 0.0,
        "resolved_with_orcid": with_orcid,
        "resolved_with_orcid_pct": round(100.0 * with_orcid / edges, 2) if edges else 0.0,
        "distinct_resolved_researchers": distinct_people,
        "distinct_recipient_orgs": distinct_orgs,
        "by_method": by_method,
    }


def resolution_bias(con) -> dict:
    """Quantify the selection: how the *resolved/funded* researcher population
    differs from the canonical ``researchers`` pool it is drawn from.

    This is the bias to be controlled, reported up front so the reader sees the
    confound before any comparison. The gap here is *selection*, not a funding
    effect.
    """
    def stats(where):
        r = con.execute(
            f"SELECT count(*) n, "
            f"  round(100.0*count(*) FILTER (WHERE orcid IS NOT NULL)/count(*),1) pct_orcid, "
            f"  round(avg(works_count),2) mean_works, median(works_count) med_works, "
            f"  round(avg(total_citations),1) mean_cit, median(total_citations) med_cit, "
            f"  round(avg(h_index_proxy),2) mean_h "
            f"FROM researchers {where}").fetchone()
        return {"n": r[0], "pct_orcid": r[1], "mean_works": r[2],
                "median_works": float(r[3]), "mean_citations": r[4],
                "median_citations": float(r[5]), "mean_h": r[6]}

    allpop = stats("")
    funded = stats("WHERE atlas_id IN (SELECT person_atlas_id FROM grant_pi_person)")
    return {
        "all_researchers": allpop,
        "funded_researchers": funded,
        "naive_works_ratio_funded_over_all": round(
            funded["mean_works"] / allpop["mean_works"], 3),
        "naive_citations_ratio_funded_over_all": round(
            funded["mean_citations"] / allpop["mean_citations"], 3),
        "orcid_gap_pp": round(funded["pct_orcid"] - allpop["pct_orcid"], 1),
    }


# --------------------------------------------------------------------------- #
# Funder researcher-portfolios (within-funder structure, no baseline needed)
# --------------------------------------------------------------------------- #
def funder_portfolios(con) -> dict:
    """Who each funder funds, by career stage / field / productivity.

    Distinct resolved researchers per funder (the bridge's ``source`` is the
    funder feed: nih / nsf / wellcome / sloan / dfg). Career-stage and field
    composition + median productivity. This is pure within-funder structure — it
    does not reference the unfunded baseline at all, so the resolution bias
    cannot distort the *relative* shape across funders (it applies to all funders
    alike).
    """
    funders = [f for f, n in con.execute(
        "SELECT source, count(DISTINCT person_atlas_id) n FROM grant_pi_person "
        "GROUP BY 1 ORDER BY 2 DESC").fetchall() if n >= MIN_FUNDER_PEOPLE]

    out = []
    for src in funders:
        n = _scalar(con,
                    "SELECT count(DISTINCT person_atlas_id) FROM grant_pi_person WHERE source=?",
                    [src])
        stage = {s: c for s, c in con.execute(
            "WITH dp AS (SELECT DISTINCT person_atlas_id FROM grant_pi_person WHERE source=?) "
            "SELECT r.seniority, count(*) FROM dp JOIN researchers r "
            "ON dp.person_atlas_id=r.atlas_id GROUP BY 1", [src]).fetchall()}
        stage_share = {s: round(100.0 * stage.get(s, 0) / n, 1) for s in SENIORITY_ORDER}
        fields = con.execute(
            "WITH dp AS (SELECT DISTINCT person_atlas_id FROM grant_pi_person WHERE source=?) "
            "SELECT r.field_slug, count(*) c FROM dp JOIN researchers r "
            "ON dp.person_atlas_id=r.atlas_id GROUP BY 1 ORDER BY 2 DESC LIMIT 4", [src]).fetchall()
        top_fields = [{"field": f, "share_pct": round(100.0 * c / n, 1)} for f, c in fields]
        prod = con.execute(
            "WITH dp AS (SELECT DISTINCT person_atlas_id FROM grant_pi_person WHERE source=?) "
            "SELECT median(r.works_count), median(r.h_index_proxy), median(r.total_citations) "
            "FROM dp JOIN researchers r ON dp.person_atlas_id=r.atlas_id", [src]).fetchone()
        out.append({
            "funder": src,
            "n_researchers": n,
            "stage_share_pct": stage_share,
            "top_fields": top_fields,
            "median_works": float(prod[0]),
            "median_h_index": float(prod[1]),
            "median_citations": float(prod[2]),
        })
    return {"min_people": MIN_FUNDER_PEOPLE, "funders": out}


def stage_composition(con) -> dict:
    """Career-stage composition of the *whole* funded (grant-holder) population,
    distinct researchers. The baseline-free headline on who holds grants."""
    n = _scalar(con, "SELECT count(DISTINCT person_atlas_id) FROM grant_pi_person")
    stage = {s: c for s, c in con.execute(
        "WITH dp AS (SELECT DISTINCT person_atlas_id FROM grant_pi_person) "
        "SELECT r.seniority, count(*) FROM dp JOIN researchers r "
        "ON dp.person_atlas_id=r.atlas_id GROUP BY 1").fetchall()}
    return {
        "n_researchers": n,
        "share_pct": {s: round(100.0 * stage.get(s, 0) / n, 1) for s in SENIORITY_ORDER},
        "counts": {s: stage.get(s, 0) for s in SENIORITY_ORDER},
    }


def portfolio_concentration(con) -> dict:
    """How concentrated grant-holding is across resolved researchers — distinct
    grants per resolved PI, and the multi-funder tail. Baseline-free."""
    r = con.execute(
        "WITH pg AS (SELECT person_atlas_id, count(DISTINCT grant_id) ng "
        "  FROM grant_pi_person GROUP BY 1) "
        "SELECT round(avg(ng),2), median(ng), max(ng), "
        "  count(*) FILTER (WHERE ng=1), count(*) FILTER (WHERE ng>=10), count(*) "
        "FROM pg").fetchone()
    multi = {nf: c for nf, c in con.execute(
        "WITH pf AS (SELECT person_atlas_id, count(DISTINCT source) nf "
        "  FROM grant_pi_person GROUP BY 1) "
        "SELECT nf, count(*) FROM pf GROUP BY 1 ORDER BY 1").fetchall()}
    total = sum(multi.values())
    return {
        "mean_grants_per_researcher": float(r[0]),
        "median_grants_per_researcher": float(r[1]),
        "max_grants_per_researcher": int(r[2]),
        "single_grant_researchers": int(r[3]),
        "ten_plus_grant_researchers": int(r[4]),
        "multi_funder_share_pct": round(
            100.0 * sum(v for k, v in multi.items() if k >= 2) / total, 2) if total else 0.0,
        "by_distinct_funders": {int(k): int(v) for k, v in multi.items()},
    }


# --------------------------------------------------------------------------- #
# Funded vs comparison cohort — RESTRICTED to the resolvable population,
# then MATCHED on field x stage x entry-era. The honest comparison.
# --------------------------------------------------------------------------- #
def _resolvable_labeled(con):
    """Per-researcher rows restricted to the *resolvable* population: anyone whose
    primary org is one of the grant-receiving (ROR-resolved) orgs — i.e. someone
    who *could* have been matched to a grant at a funded org. Labeled funded=1 if
    in the bridge. Returns a pandas DataFrame.
    """
    return con.execute(
        """
        WITH funded AS (SELECT DISTINCT person_atlas_id pid FROM grant_pi_person),
        funded_orgs AS (SELECT DISTINCT recipient_ror FROM grant_pi_person
                        WHERE recipient_ror IS NOT NULL)
        SELECT r.field_slug, r.seniority, (r.first_year/5)*5 AS era5,
               CASE WHEN r.atlas_id IN (SELECT pid FROM funded) THEN 1 ELSE 0 END AS funded,
               r.works_count, r.h_index_proxy, r.total_citations
        FROM researchers r
        JOIN funded_orgs fo ON r.primary_org_ror = fo.recipient_ror
        WHERE r.first_year IS NOT NULL
        """
    ).df()


def matched_productivity(con) -> dict:
    """Funded vs comparison productivity, MATCHED on field x career-stage x
    entry-era within the funded-org population.

    The naive funded/all ratio is mostly *selection*. Here we hold the three
    biggest confounds fixed: same field, same career-stage proxy, same 5-year
    entry era, and the same pool of grant-receiving organizations. We keep only
    strata with >= MIN_STRATUM people in *both* groups, compute the within-stratum
    funded and comparison means, and combine them with funded-count weights (the
    funded-population-standardized contrast). A 2,000-sample stratum bootstrap
    gives the CI. This is descriptive structure of the funded population, NOT a
    causal return to funding.
    """
    df = _resolvable_labeled(con)
    n_resolvable = int(len(df))
    n_funded = int(df.funded.sum())
    n_comparison = int((1 - df.funded).sum())

    g = df.groupby(["field_slug", "seniority", "era5"])
    recs = []   # (n_funded, works_f, works_c, h_f, h_c, cit_f, cit_c)
    for _, sub in g:
        nf = int(sub.funded.sum())
        nc = int((1 - sub.funded).sum())
        if nf < MIN_STRATUM or nc < MIN_STRATUM:
            continue
        f = sub[sub.funded == 1]
        c = sub[sub.funded == 0]
        recs.append((nf,
                     f.works_count.mean(), c.works_count.mean(),
                     f.h_index_proxy.mean(), c.h_index_proxy.mean(),
                     f.total_citations.mean(), c.total_citations.mean()))
    recs = np.array(recs, dtype=float)
    w = recs[:, 0] / recs[:, 0].sum()

    def wratio(fi, ci):
        return float((w * recs[:, fi]).sum() / (w * recs[:, ci]).sum())

    works_ratio = wratio(1, 2)
    h_ratio = wratio(3, 4)
    cit_ratio = wratio(5, 6)

    rng = np.random.default_rng(BOOT_SEED)
    k = len(recs)
    boots = {"works": [], "h": [], "cit": []}
    for _ in range(BOOT_N):
        idx = rng.integers(0, k, k)
        s = recs[idx]
        ww = s[:, 0] / s[:, 0].sum()
        boots["works"].append((ww * s[:, 1]).sum() / (ww * s[:, 2]).sum())
        boots["h"].append((ww * s[:, 3]).sum() / (ww * s[:, 4]).sum())
        boots["cit"].append((ww * s[:, 5]).sum() / (ww * s[:, 6]).sum())

    def ci(key):
        return [round(float(x), 3) for x in np.percentile(boots[key], [2.5, 97.5])]

    # the naive (unmatched) funded/comparison ratio within the resolvable pop
    naive_works = float(df[df.funded == 1].works_count.mean() /
                        df[df.funded == 0].works_count.mean())

    return {
        "n_resolvable_population": n_resolvable,
        "n_funded": n_funded,
        "n_comparison": n_comparison,
        "n_strata": k,
        "min_stratum": MIN_STRATUM,
        "n_funded_in_strata": int(recs[:, 0].sum()),
        "naive_works_ratio_within_resolvable": round(naive_works, 3),
        "matched_works_ratio": round(works_ratio, 3),
        "matched_works_ci": ci("works"),
        "matched_h_ratio": round(h_ratio, 3),
        "matched_h_ci": ci("h"),
        "matched_citations_ratio": round(cit_ratio, 3),
        "matched_citations_ci": ci("cit"),
    }


def matched_by_stage(con) -> dict:
    """Matched funded/comparison works ratio broken out by career stage —
    where in the career the (descriptive) funded gap is largest. Same matching
    discipline as :func:`matched_productivity`, restricted within each seniority.
    """
    df = _resolvable_labeled(con)
    out = {}
    for sen in SENIORITY_ORDER:
        d = df[df.seniority == sen]
        g = d.groupby(["field_slug", "era5"])
        recs = []
        for _, sub in g:
            nf = int(sub.funded.sum())
            nc = int((1 - sub.funded).sum())
            if nf < MIN_STRATUM or nc < MIN_STRATUM:
                continue
            f = sub[sub.funded == 1].works_count.mean()
            c = sub[sub.funded == 0].works_count.mean()
            recs.append((nf, f, c))
        if not recs:
            out[sen] = {"n_strata": 0, "matched_works_ratio": None}
            continue
        recs = np.array(recs, dtype=float)
        w = recs[:, 0] / recs[:, 0].sum()
        out[sen] = {
            "n_strata": len(recs),
            "n_funded": int(recs[:, 0].sum()),
            "matched_works_ratio": round(
                float((w * recs[:, 1]).sum() / (w * recs[:, 2]).sum()), 3),
        }
    return out
