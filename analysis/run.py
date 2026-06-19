#!/usr/bin/env python3
"""Run the funding-landscape study end to end (idempotent).

Computes every statistic in ``analysis/funding_landscape.py``, writes the
machine-readable ``analysis/results.json`` (the single source of truth the paper
and the validation test both read), and renders the figures under
``analysis/figures/``.

Usage:
    python analysis/run.py            # full run: stats + figures
    python analysis/run.py --no-figures
    python analysis/run.py --db /path/to/research_atlas.duckdb
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis import funding_landscape as fl  # noqa: E402

HERE = Path(__file__).resolve().parent
FIG_DIR = HERE / "figures"
RESULTS = HERE / "results.json"


def compute(con) -> dict:
    cov = fl.coverage_stats(con)

    # --- 1. Concentration (count-based, ROR-resolved) -------------------- #
    org_counts = fl.org_grant_counts(con, ror_only=True)
    counts = np.array([r["grants"] for r in org_counts], dtype=float)
    g_point, g_lo, g_hi = fl.gini_bootstrap_ci(counts)
    org_conc = {
        "n_orgs": int(counts.size),
        "total_grants": int(counts.sum()),
        "gini_grants": g_point,
        "gini_grants_ci95": [g_lo, g_hi],
        "hhi_grants": fl.hhi(counts),
        "top1pct_share": fl.top_share(counts, 0.01),
        "top5pct_share": fl.top_share(counts, 0.05),
        "top10pct_share": fl.top_share(counts, 0.10),
    }

    # Sensitivity: Gini on the NOISY dollar column (for explicit comparison).
    org_dollars = fl.org_grant_dollars(con)
    dvals = np.array([r["usd"] for r in org_dollars], dtype=float)
    dg_point, dg_lo, dg_hi = fl.gini_bootstrap_ci(dvals)
    org_conc_dollars = {
        "n_orgs": int(dvals.size),
        "gini_dollars": dg_point,
        "gini_dollars_ci95": [dg_lo, dg_hi],
        "note": ("dollar column carries fuzzy-match + shared-grant double-count "
                 "noise; reported only as a sensitivity comparison vs the robust "
                 "count-based Gini"),
    }

    # --- 2. Geography & country-level concentration ---------------------- #
    countries = fl.country_funding(con)
    cvals = np.array([r["grants"] for r in countries], dtype=float)
    cg_point, cg_lo, cg_hi = fl.gini_bootstrap_ci(cvals)
    geo = {
        "countries": countries[:20],
        "n_countries": len(countries),
        "gini_country_grants": cg_point,
        "gini_country_grants_ci95": [cg_lo, cg_hi],
        "hhi_country_grants": fl.hhi(cvals),
        "us_grant_share": next((r["grants"] for r in countries if r["country_code"] == "US"), 0)
                          / cvals.sum(),
    }

    # --- 3. Co-funding network ------------------------------------------- #
    cofund = fl.cofunding_summary(con)
    cofund["top_pairs"] = fl.funder_pair_cofunding(con, limit=25)

    # --- 4. Funding -> output rate (NIH/NSF/EC only) --------------------- #
    out_rate = fl.funder_output_rate(con)

    # --- 5. Field dynamics ----------------------------------------------- #
    domains = fl.domain_composition(con)
    dynamics = fl.rising_falling_fields(con)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coverage": cov,
        "org_concentration": org_conc,
        "org_concentration_dollars_sensitivity": org_conc_dollars,
        "geography": geo,
        "cofunding": cofund,
        "funding_output_rate": out_rate,
        "domain_composition": domains,
        "field_dynamics": dynamics,
    }


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #
def make_figures(con, results: dict):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "figure.dpi": 130, "savefig.dpi": 130, "font.size": 10,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.titlesize": 11, "axes.titleweight": "bold",
    })
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    INK = "#1b2a4a"; ACCENT = "#c0392b"; GREY = "#8a94a6"

    # Fig 1 — Lorenz curve of org grant concentration (ROR-resolved) ------- #
    org_counts = fl.org_grant_counts(con, ror_only=True)
    x = np.sort(np.array([r["grants"] for r in org_counts], dtype=float))
    cum = np.cumsum(x) / x.sum()
    cum = np.insert(cum, 0, 0)
    p = np.linspace(0, 1, cum.size)
    fig, ax = plt.subplots(figsize=(6.0, 5.4))
    ax.plot([0, 1], [0, 1], "--", color=GREY, lw=1, label="perfect equality")
    ax.plot(p, cum, color=INK, lw=2,
            label=f"observed (Gini = {results['org_concentration']['gini_grants']:.3f})")
    ax.fill_between(p, cum, p, color=INK, alpha=0.07)
    ax.set_xlabel("cumulative share of recipient organizations (poorest→richest)")
    ax.set_ylabel("cumulative share of grants")
    ax.set_title("Fig 1 · Concentration of grants across ROR-resolved recipients\n"
                 f"({results['org_concentration']['n_orgs']:,} orgs, "
                 f"{results['org_concentration']['total_grants']:,} grants, 2015–2025)")
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig1_lorenz_orgs.png"); plt.close(fig)

    # Fig 2 — Geography of funding (top countries, grant counts) ----------- #
    cs = results["geography"]["countries"][:12]
    fig, ax = plt.subplots(figsize=(6.4, 4.4))
    names = [c["country_code"] for c in cs][::-1]
    vals = [c["grants"] / 1e3 for c in cs][::-1]
    bars = ax.barh(names, vals, color=INK)
    bars[-1].set_color(ACCENT)
    for b, v in zip(bars, vals):
        ax.text(b.get_width() + max(vals) * 0.01, b.get_y() + b.get_height() / 2,
                f"{v:.0f}k", va="center", fontsize=8, color=INK)
    ax.set_xlabel("recipient grants, thousands (2015–2025)")
    ax.set_title("Fig 2 · Geography of research grants by recipient-org country")
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig2_geography.png"); plt.close(fig)

    # Fig 3 — Funder co-funding heatmap ----------------------------------- #
    pairs = results["cofunding"]["top_pairs"]
    funders = []
    for p_ in pairs:
        for k in ("funder_a", "funder_b"):
            if p_[k] not in funders:
                funders.append(p_[k])
    funders = funders[:14]
    idx = {f: i for i, f in enumerate(funders)}
    M = np.zeros((len(funders), len(funders)))
    for p_ in pairs:
        a, b = p_["funder_a"], p_["funder_b"]
        if a in idx and b in idx:
            M[idx[a], idx[b]] = p_["shared_works"]
            M[idx[b], idx[a]] = p_["shared_works"]
    fig, ax = plt.subplots(figsize=(6.6, 5.8))
    masked = np.ma.masked_where(M == 0, M)
    im = ax.imshow(masked, cmap="rocket_r" if "rocket_r" in plt.colormaps() else "magma_r")
    ax.set_xticks(range(len(funders))); ax.set_yticks(range(len(funders)))
    ax.set_xticklabels(funders, rotation=90, fontsize=7)
    ax.set_yticklabels(funders, fontsize=7)
    ax.set_title("Fig 3 · Cross-funder co-funding\n(shared works acknowledging both funders)")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="shared works")
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig3_cofunding.png"); plt.close(fig)

    # Fig 4 — Funding -> output rate by funder ---------------------------- #
    rates = [r for r in results["funding_output_rate"] if r["works_per_million_usd"]]
    rates = sorted(rates, key=lambda r: r["works_per_million_usd"], reverse=True)[:16]
    fig, ax = plt.subplots(figsize=(6.6, 5))
    names = [r["funder"] for r in rates][::-1]
    vals = [r["works_per_million_usd"] for r in rates][::-1]
    colors = [ACCENT if r["country_code"] != "US" else INK for r in rates][::-1]
    ax.barh(names, vals, color=colors)
    ax.set_xlabel("linked works per $1M awarded  (NIH/NSF/EC; 2015–25 grants → 2016–24 works)")
    ax.set_title("Fig 4 · Funding→output rate by funder\n"
                 "(count-based; navy = US, red = EC/supranational)")
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig4_output_rate.png"); plt.close(fig)

    # Fig 5 — Rising vs declining fields ---------------------------------- #
    rising = results["field_dynamics"]["rising"][:8][::-1]
    falling = results["field_dynamics"]["falling"][:8]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.6))
    a1.barh([r["name"][:42] for r in rising], [r["growth_ratio"] for r in rising],
            color=ACCENT)
    a1.set_title("Rising fields (late/early funded-work ratio)", fontsize=10)
    a1.set_xlabel("growth ratio  (2021–24 / 2016–19)")
    a2.barh([r["name"][:42] for r in falling], [r["growth_ratio"] for r in falling],
            color=INK)
    a2.set_title("Declining fields", fontsize=10)
    a2.set_xlabel("growth ratio  (2021–24 / 2016–19)")
    a2.axvline(1.0, color=GREY, ls="--", lw=1)
    a1.axvline(1.0, color=GREY, ls="--", lw=1)
    fig.suptitle("Fig 5 · Field dynamics in funded output, 2016–2019 vs 2021–2024",
                 fontweight="bold")
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig5_field_dynamics.png"); plt.close(fig)

    return ["fig1_lorenz_orgs.png", "fig2_geography.png", "fig3_cofunding.png",
            "fig4_output_rate.png", "fig5_field_dynamics.png"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-figures", action="store_true")
    args = ap.parse_args()

    con = fl.connect(args.db)
    results = compute(con)
    figs = []
    if not args.no_figures:
        figs = make_figures(con, results)
    con.close()

    results["figures"] = figs
    RESULTS.write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {RESULTS}")
    if figs:
        print("figures:", ", ".join(figs))
    # echo headline numbers
    oc = results["org_concentration"]
    print(f"\norg Gini (grants, ROR): {oc['gini_grants']:.3f} "
          f"CI95 [{oc['gini_grants_ci95'][0]:.3f}, {oc['gini_grants_ci95'][1]:.3f}]")
    print(f"top 1% of orgs hold {oc['top1pct_share']*100:.1f}% of grants")
    print(f"US grant share: {results['geography']['us_grant_share']*100:.1f}%")
    print(f"multi-funder works: {results['cofunding']['multi_funder_share']*100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
