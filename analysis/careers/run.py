#!/usr/bin/env python3
"""Run the funding-and-careers study end to end (idempotent, read-only).

Computes every statistic in ``funding_careers.py``, writes
``analysis/careers/results.json`` (the single source of truth the paper and the
test both read), and renders the figures under ``analysis/careers/figures/``.

Usage:
    python analysis/careers/run.py
    python analysis/careers/run.py --no-figures
    python analysis/careers/run.py --db /path/to/research_atlas.duckdb
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from analysis.careers import funding_careers as fc  # noqa: E402

HERE = Path(__file__).resolve().parent
FIG_DIR = HERE / "figures"
RESULTS = HERE / "results.json"

INK = "#1b2a4a"
ACCENT = "#c0392b"
GREY = "#8a94a6"
GOLD = "#b8860b"
GREEN = "#2e7d52"
STAGE_COLORS = {
    "eminent": INK, "established": "#3a5a8c",
    "rising-star": GOLD, "early/unknown": GREY,
}


def compute(con) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coverage": fc.coverage(con),
        "resolution_bias": fc.resolution_bias(con),
        "stage_composition": fc.stage_composition(con),
        "portfolio_concentration": fc.portfolio_concentration(con),
        "funder_portfolios": fc.funder_portfolios(con),
        "matched_productivity": fc.matched_productivity(con),
        "matched_by_stage": fc.matched_by_stage(con),
    }


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #
def make_figures(results: dict):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "figure.dpi": 130, "savefig.dpi": 130, "font.size": 10,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.titlesize": 11, "axes.titleweight": "bold",
    })
    FIG_DIR.mkdir(parents=True, exist_ok=True)

    # Fig 1 — The selection bias, then the matched comparison ----------------- #
    rb = results["resolution_bias"]
    mp = results["matched_productivity"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.4, 4.7))

    # left: resolution bias (funded vs all), the confound
    cats = ["% ORCID", "mean works", "mean citations\n(/10)"]
    allv = [rb["all_researchers"]["pct_orcid"], rb["all_researchers"]["mean_works"],
            rb["all_researchers"]["mean_citations"] / 10]
    funv = [rb["funded_researchers"]["pct_orcid"], rb["funded_researchers"]["mean_works"],
            rb["funded_researchers"]["mean_citations"] / 10]
    x = np.arange(len(cats))
    a1.bar(x - 0.2, allv, 0.4, color=GREY, label="all researchers")
    a1.bar(x + 0.2, funv, 0.4, color=ACCENT, label="funded (resolved PIs)")
    a1.set_xticks(x); a1.set_xticklabels(cats, fontsize=9)
    a1.set_title("A · The selection, not an effect\n"
                 "resolved PIs are far more ORCID'd & productive\n"
                 "than the pool they're drawn from", fontsize=9.5)
    a1.legend(frameon=False, fontsize=8, loc="upper left")

    # right: naive vs matched ratios with CIs
    labels = ["works\n(naive,\nfunded/all)", "works\n(matched)",
              "h-index\n(matched)", "citations\n(matched)"]
    vals = [rb["naive_works_ratio_funded_over_all"],
            mp["matched_works_ratio"], mp["matched_h_ratio"], mp["matched_citations_ratio"]]
    cis = [None, mp["matched_works_ci"], mp["matched_h_ci"], mp["matched_citations_ci"]]
    colors = [GREY, GREEN, GREEN, GREEN]
    xb = np.arange(len(vals))
    bars = a2.bar(xb, vals, 0.6, color=colors)
    for i, ci in enumerate(cis):
        if ci:
            a2.errorbar(i, vals[i], yerr=[[vals[i] - ci[0]], [ci[1] - vals[i]]],
                        fmt="none", ecolor=INK, capsize=4, lw=1.4)
    for b, v in zip(bars, vals):
        a2.text(b.get_x() + b.get_width() / 2, v + 0.03, f"{v:.2f}×",
                ha="center", fontsize=8.5, fontweight="bold")
    a2.axhline(1.0, color="#444", lw=1, ls="--")
    a2.set_xticks(xb); a2.set_xticklabels(labels, fontsize=8)
    a2.set_ylim(0, max(vals) * 1.2)
    a2.set_ylabel("funded ÷ comparison")
    a2.set_title("B · Match on field × stage × era and the gap collapses\n"
                 "the naive 2.1× is mostly selection; matched residual ≈ 1.2×",
                 fontsize=9.5)
    fig.suptitle("Fig 1 · A funding 'effect' on productivity is mostly resolution/selection bias "
                 "(95% bootstrap CIs)", fontweight="bold", fontsize=11)
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig1_selection_vs_matched.png"); plt.close(fig)

    # Fig 2 — Funder researcher-portfolios: career-stage composition ---------- #
    fp = results["funder_portfolios"]["funders"]
    funders = [f["funder"] for f in fp]
    stages = ["eminent", "established", "rising-star", "early/unknown"]
    fig, ax = plt.subplots(figsize=(8.0, 4.8))
    bottom = np.zeros(len(funders))
    for st in stages:
        vals = np.array([f["stage_share_pct"][st] for f in fp])
        ax.bar(funders, vals, bottom=bottom, color=STAGE_COLORS[st], label=st)
        bottom += vals
    ax.set_ylabel("share of grant-holders (%)")
    ax.set_ylim(0, 100)
    ax.set_title("Fig 2 · Who each funder funds, by career stage\n"
                 "(distinct resolved grant-holders; descriptive, within-funder structure)")
    ax.legend(frameon=False, fontsize=8, ncol=4, loc="lower center", bbox_to_anchor=(0.5, -0.22))
    for i, f in enumerate(fp):
        ax.text(i, 101, f"n={f['n_researchers']:,}", ha="center", fontsize=7, color="#444")
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig2_funder_stage_portfolios.png"); plt.close(fig)

    # Fig 3 — Funder portfolios: eminence vs productivity --------------------- #
    fig, ax = plt.subplots(figsize=(7.2, 5.2))
    emin = [f["stage_share_pct"]["eminent"] for f in fp]
    medc = [f["median_citations"] for f in fp]
    sizes = [max(40, np.sqrt(f["n_researchers"]) * 3) for f in fp]
    ax.scatter(emin, medc, s=sizes, color=ACCENT, alpha=0.7, edgecolor=INK, zorder=3)
    for f, ex, cy in zip(fp, emin, medc):
        ax.annotate(f["funder"], (ex, cy), fontsize=9, fontweight="bold",
                    xytext=(6, 4), textcoords="offset points", color=INK)
    ax.set_xlabel("% of grant-holders who are 'eminent' (high-impact)")
    ax.set_ylabel("median citations of grant-holders (corpus)")
    ax.set_title("Fig 3 · Funder researcher-portfolios differ\n"
                 "(marker area ∝ √(grant-holders); Sloan = eminent/high-impact,\n"
                 "DFG/Wellcome = rising-star-skewed)")
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig3_funder_eminence_productivity.png"); plt.close(fig)

    # Fig 4 — Matched works ratio by career stage ----------------------------- #
    mbs = results["matched_by_stage"]
    order = [s for s in stages if mbs.get(s, {}).get("matched_works_ratio") is not None]
    ratios = [mbs[s]["matched_works_ratio"] for s in order]
    ns = [mbs[s]["n_funded"] for s in order]
    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    bars = ax.bar(order, ratios, 0.6, color=[STAGE_COLORS[s] for s in order])
    ax.axhline(1.0, color="#444", lw=1, ls="--", label="parity (no matched gap)")
    for b, r, n in zip(bars, ratios, ns):
        ax.text(b.get_x() + b.get_width() / 2, r + 0.01, f"{r:.2f}×\nn={n:,}",
                ha="center", fontsize=8)
    ax.set_ylim(0, max(ratios) * 1.25)
    ax.set_ylabel("matched funded ÷ comparison (works)")
    ax.set_title("Fig 4 · Matched residual works-gap by career stage\n"
                 "(largest for rising-stars; descriptive, not causal)")
    ax.legend(frameon=False, fontsize=8, loc="upper right")
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig4_matched_by_stage.png"); plt.close(fig)

    return ["fig1_selection_vs_matched.png", "fig2_funder_stage_portfolios.png",
            "fig3_funder_eminence_productivity.png", "fig4_matched_by_stage.png"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-figures", action="store_true")
    args = ap.parse_args()

    con = fc.connect(args.db)
    results = compute(con)
    con.close()

    figs = []
    if not args.no_figures:
        figs = make_figures(results)
    results["figures"] = figs
    RESULTS.write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {RESULTS}")
    if figs:
        print("figures:", ", ".join(figs))

    cov = results["coverage"]
    rb = results["resolution_bias"]
    mp = results["matched_productivity"]
    print(f"\nresolved PI edges: {cov['resolved_edges']:,} ({cov['resolved_pct']}%) "
          f"-> {cov['distinct_resolved_researchers']:,} researchers")
    print(f"selection: funded mean works {rb['funded_researchers']['mean_works']} vs "
          f"all {rb['all_researchers']['mean_works']} (naive {rb['naive_works_ratio_funded_over_all']}×)")
    print(f"MATCHED works ratio: {mp['matched_works_ratio']}× "
          f"CI {mp['matched_works_ci']} (naive within-resolvable "
          f"{mp['naive_works_ratio_within_resolvable']}×)")
    print(f"MATCHED citations ratio: {mp['matched_citations_ratio']}× CI {mp['matched_citations_ci']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
