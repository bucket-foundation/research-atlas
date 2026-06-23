#!/usr/bin/env python3
"""Emit the 4-checkpoint convergence figure from analysis/crossfield/convergence.jsonl.

Reads the convergence log (one JSON object per checkpoint) and the checkpoint
JSONs for the combined-test confidence intervals, and writes a two-panel figure
to analysis/crossfield/figures/convergence.png:

  (left)  combined field-level mean ΔMAP (SPECTER − TF-IDF) with 95% CI vs total
          corpus size — the decay toward null.
  (right) fraction of 26 fields where SPECTER beats TF-IDF vs total corpus size.

No network, no GPU: pure read-from-JSON + matplotlib. Re-running is idempotent.

Usage:  python scripts/crossfield_figure.py
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CDIR = REPO / "analysis" / "crossfield"
FIGDIR = CDIR / "figures"


def main() -> int:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    conv = [json.loads(line) for line in
            (CDIR / "convergence.jsonl").read_text().splitlines() if line.strip()]
    conv.sort(key=lambda r: r["checkpoint"])

    # pull the combined-test CI per checkpoint from the checkpoint JSONs
    ci_lo, ci_hi = [], []
    for r in conv:
        cj = json.loads((CDIR / f"checkpoint_{r['checkpoint']}.json").read_text())
        c = cj["crossfield"]["generalization"]["combined_field_level"]
        ci_lo.append(c["ci"][0])
        ci_hi.append(c["ci"][1])

    works = [r["total_works"] for r in conv]
    dmap = [r["mean_delta_map"] for r in conv]
    winfrac = [r["win_fraction"] for r in conv]
    labels = [f"ckpt {r['checkpoint']}\n{r['total_works']//1000}k" for r in conv]
    x = list(range(len(conv)))

    plt.rcParams.update({"font.size": 9, "font.family": "serif",
                         "axes.edgecolor": "#444", "axes.titlecolor": "#11203f"})
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(8.6, 3.6))

    # --- left: combined mean ΔMAP with 95% CI ---
    yerr = [[d - lo for d, lo in zip(dmap, ci_lo)],
            [hi - d for d, hi in zip(dmap, ci_hi)]]
    ax1.axhline(0.0, color="#c0392b", lw=1.0, ls="--", zorder=1)
    ax1.errorbar(x, dmap, yerr=yerr, fmt="o-", color="#11203f", ecolor="#7a8aa8",
                 elinewidth=1.3, capsize=4, ms=6, lw=1.6, zorder=3)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)
    ax1.set_ylabel("combined mean ΔMAP\n(SPECTER − TF-IDF), 95% CI")
    ax1.set_title("Transformer edge decays to null\nas the corpus broadens")
    for xi, d, p in zip(x, dmap, [r["combined_p"] for r in conv]):
        ax1.annotate(f"{d:+.4f}\np={p:.2f}", (xi, d), textcoords="offset points",
                     xytext=(8, 8), fontsize=7, color="#333")

    # --- right: win fraction ---
    ax2.axhline(0.5, color="#888", lw=0.9, ls=":", zorder=1)
    ax2.plot(x, winfrac, "s-", color="#1f6f54", ms=6, lw=1.6, zorder=3)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels)
    ax2.set_ylim(0.30, 0.70)
    ax2.set_ylabel("fraction of 26 fields\nSPECTER > TF-IDF")
    ax2.set_title("Win fraction crosses below\nthe 0.5 coin-flip line")
    for xi, w, nw in zip(x, winfrac, [r["fields_specter_wins"] for r in conv]):
        ax2.annotate(f"{nw}/26", (xi, w), textcoords="offset points",
                     xytext=(0, 9), ha="center", fontsize=7, color="#333")

    fig.suptitle("Cross-field convergence: 4 checkpoints, 78k → 362k works",
                 fontsize=11, color="#11203f", y=1.02)
    fig.tight_layout()
    FIGDIR.mkdir(parents=True, exist_ok=True)
    out = FIGDIR / "convergence.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"wrote {out}  ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
