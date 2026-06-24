#!/usr/bin/env python3
"""Run the funder-specialization study end to end (idempotent).

Computes every statistic in ``funder_specialization.py``, writes
``analysis/specialization/results.json`` (the single source of truth the paper
and the test both read), and renders the figures under
``analysis/specialization/figures/``.

Usage:
    python analysis/specialization/run.py
    python analysis/specialization/run.py --no-figures
    python analysis/specialization/run.py --db /path/to/research_atlas.duckdb
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from analysis.specialization import funder_specialization as fs  # noqa: E402

HERE = Path(__file__).resolve().parent
FIG_DIR = HERE / "figures"
RESULTS = HERE / "results.json"


def compute(con) -> dict:
    fs.build_topic_rollup(con)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coverage": fs.coverage(con),
        "specialization_gradient": fs.specialization_gradient(con),
        "funder_similarity": fs.funder_similarity(con),
        "specialization_stability": fs.specialization_stability(con),
        "domain_composition_by_year": fs.domain_composition_by_year(con),
        "composition_shift": fs.composition_shift(con),
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
    INK = "#1b2a4a"; ACCENT = "#c0392b"; GREY = "#8a94a6"; GOLD = "#b8860b"

    def fam_color(cc):
        return INK if cc == "US" else ACCENT

    # Fig 1 — Specialization gradient (HHI bar, generalist -> specialist) ----- #
    grad = results["specialization_gradient"]
    fig, ax = plt.subplots(figsize=(7.2, 6.2))
    names = [r["funder"] for r in grad][::-1]
    vals = [r["hhi"] for r in grad][::-1]
    colors = [fam_color(r["country_code"]) for r in grad][::-1]
    bars = ax.barh(names, vals, color=colors)
    for r, b in zip(grad[::-1], bars):
        ax.text(b.get_width() + 0.005, b.get_y() + b.get_height() / 2,
                f"{r['top_field'][:22]} ({r['top_field_share']*100:.0f}%)",
                va="center", fontsize=6.5, color="#444")
    ax.set_xlim(0, max(vals) * 1.55)
    ax.set_xlabel("portfolio HHI over 26 OpenAlex fields  (low = generalist, high = specialist)")
    ax.set_title("Fig 1 · Funder specialization gradient\n"
                 "(distinct linked works 2016–2024; navy = US, red = EC/supranational;\n"
                 "label = dominant field & its share)")
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig1_specialization_gradient.png"); plt.close(fig)

    # Fig 2 — Funder similarity heatmap (cosine of field-share vectors) ------- #
    sim = results["funder_similarity"]
    funders = sim["funders"]
    idx = {f: i for i, f in enumerate(funders)}
    M = np.eye(len(funders))
    for p in sim["pairs"]:
        a, b = p["funder_a"], p["funder_b"]
        M[idx[a], idx[b]] = p["cosine"]
        M[idx[b], idx[a]] = p["cosine"]
    # order: NSF, EC, ERC first (the generalists/europe), then NIH ICs
    head = [f for f in ("NSF", "EC", "ERC") if f in funders]
    rest = [f for f in funders if f not in head]
    order = head + rest
    oi = [idx[f] for f in order]
    Mo = M[np.ix_(oi, oi)]
    fig, ax = plt.subplots(figsize=(7.6, 6.6))
    im = ax.imshow(Mo, cmap="magma", vmin=0, vmax=1)
    ax.set_xticks(range(len(order))); ax.set_yticks(range(len(order)))
    ax.set_xticklabels(order, rotation=90, fontsize=7)
    ax.set_yticklabels(order, fontsize=7)
    ax.set_title("Fig 2 · Funder portfolio similarity\n"
                 "(cosine of 26-field share vectors; bright = same fields)")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="cosine similarity")
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig2_similarity.png"); plt.close(fig)

    # Fig 3 — Specialization stability (early vs late HHI scatter) ------------ #
    stab = results["specialization_stability"]
    per = stab["per_funder"]
    e = [r["hhi_early"] for r in per]
    l = [r["hhi_late"] for r in per]
    fig, ax = plt.subplots(figsize=(6.4, 6.0))
    lim = max(max(e), max(l)) * 1.08
    ax.plot([0, lim], [0, lim], "--", color=GREY, lw=1, label="no drift (y = x)")
    ax.scatter(e, l, color=INK, s=36, zorder=3)
    for r in per:
        ax.annotate(r["funder"], (r["hhi_early"], r["hhi_late"]),
                    fontsize=6.5, xytext=(3, 3), textcoords="offset points", color="#444")
    ax.set_xlim(0, lim); ax.set_ylim(0, lim)
    ax.set_xlabel("portfolio HHI, early window (2016–2019)")
    ax.set_ylabel("portfolio HHI, late window (2021–2024)")
    ax.set_title("Fig 3 · Specialization is a stable fingerprint\n"
                 f"(Spearman ρ = {stab['spearman_rank_corr']:.3f}, "
                 f"mean |ΔHHI| = {stab['mean_abs_delta_hhi']:.3f})")
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig3_stability.png"); plt.close(fig)

    # Fig 4 — Domain composition over time + the confounds -------------------- #
    comp = results["domain_composition_by_year"]
    years = comp["years"]
    shift = results["composition_shift"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.5, 4.8))
    dom_colors = {"Physical Sciences": INK, "Life Sciences": ACCENT,
                  "Health Sciences": GOLD, "Social Sciences": GREY}
    for d in comp["domains"]:
        a1.plot(years, [s * 100 for s in comp["share"][d]],
                marker="o", ms=3, lw=1.6, color=dom_colors.get(d, "#333"), label=d)
    a1.set_xlabel("publication year"); a1.set_ylabel("share of funded works (%)")
    a1.set_title("Aggregate domain composition", fontsize=10)
    a1.legend(frameon=False, fontsize=7.5, loc="center left")
    a1.set_ylim(0, 55)
    # right: within-NSF (mix-controlled) to show stability
    nsf = shift["within_nsf_domain"]
    for d in comp["domains"]:
        a2.plot(nsf["years"], [s * 100 for s in nsf["series"][d]],
                marker="s", ms=3, lw=1.6, color=dom_colors.get(d, "#333"), label=d)
    a2.set_xlabel("publication year"); a2.set_ylabel("share of NSF-funded works (%)")
    a2.set_title("Within-NSF composition (mix-controlled)", fontsize=10)
    a2.set_ylim(0, 80)
    fig.suptitle("Fig 4 · Funded-output composition is broadly stable 2016–2024; "
                 "the aggregate endpoint wobble is not a within-funder trend",
                 fontweight="bold", fontsize=11)
    fig.tight_layout(); fig.savefig(FIG_DIR / "fig4_composition.png"); plt.close(fig)

    return ["fig1_specialization_gradient.png", "fig2_similarity.png",
            "fig3_stability.png", "fig4_composition.png"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-figures", action="store_true")
    args = ap.parse_args()

    con = fs.connect(args.db)
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

    grad = results["specialization_gradient"]
    sim = results["funder_similarity"]
    stab = results["specialization_stability"]
    lo, hi = grad[0], grad[-1]
    print(f"\nHHI gradient: {lo['funder']} {lo['hhi']:.3f} -> {hi['funder']} {hi['hhi']:.3f}"
          f"  ({hi['funder']} = {hi['top_field_share']*100:.0f}% {hi['top_field']})")
    print(f"NIH-IC internal mean cosine: {sim['nih_ic_internal_mean_cosine']:.3f}")
    print(f"NSF mean cosine to NIH ICs:  {sim['nsf_mean_cosine_to_nih']:.3f}")
    print(f"most distinct pair: {sim['most_distinct'][0]['funder_a']}<->"
          f"{sim['most_distinct'][0]['funder_b']} cos={sim['most_distinct'][0]['cosine']:.3f}")
    print(f"stability: Spearman rho={stab['spearman_rank_corr']:.3f}, "
          f"mean|dHHI|={stab['mean_abs_delta_hhi']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
