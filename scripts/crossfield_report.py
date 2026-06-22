#!/usr/bin/env python3
"""Render the cross-field checkpoint JSON into a human + paper-ready report.

Reads ``analysis/crossfield/checkpoint_<N>.json`` (latest by default) and writes
``docs/papers/02-paper-ranking/CROSSFIELD_RESULTS.md`` -- the per-field eval table,
the generalization verdict, Gini-by-field, and interdisciplinarity -- plus prints
a console summary. Every number is read straight from the checkpoint JSON so the
paper can pin to it.

Usage:
    python scripts/crossfield_report.py            # latest checkpoint
    python scripts/crossfield_report.py --checkpoint 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import REPO_ROOT  # noqa: E402

ADIR = REPO_ROOT / "analysis" / "crossfield"
OUT = REPO_ROOT / "docs" / "papers" / "02-paper-ranking" / "CROSSFIELD_RESULTS.md"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", type=int, default=0)
    args = ap.parse_args()

    manifest = json.loads((ADIR / "manifest.json").read_text())
    ckpt = args.checkpoint or manifest["latest_checkpoint"]
    r = json.loads((ADIR / f"checkpoint_{ckpt}.json").read_text())

    gen = r["crossfield"]["generalization"]
    conc = r["crossfield"]["concentration"]
    inter = r["crossfield"]["interdisciplinarity"]

    lines = []
    L = lines.append
    L(f"# Cross-field results — checkpoint {ckpt}")
    L("")
    L(f"*Generated from `analysis/crossfield/checkpoint_{ckpt}.json`. "
      "Every number below is read from that file.*")
    L("")
    L(f"- Tranche: top **{r['tranche_target_per_field']:,}** most-cited works / field")
    L(f"- Fields with results: **{r['fields_with_results']}** / {r['fields_requested']}")
    L(f"- Total works loaded: **{r['total_works_loaded']:,}**")
    if r.get("embed_docs_per_sec"):
        L(f"- GPU embed throughput (steady-state): **{r['embed_docs_per_sec']:.1f} docs/s** "
          "(SPECTER, AMD RX 7700S / ROCm)")
    L(f"- Window: {r['window'][0]} .. {r['window'][1]}")
    L("")

    L("## Generalization: does SPECTER beat TF-IDF in every field?")
    L("")
    L(f"**SPECTER beats TF-IDF on MAP in {gen['fields_specter_wins']} of "
      f"{gen['fields_evaluated']} evaluated fields** "
      f"(win fraction {gen['win_fraction']:.2f}; sign-test p = {gen['sign_test_p']:.3g}).")
    c = gen["combined_field_level"]
    if c:
        L("")
        L(f"Combined field-level test (one-sample bootstrap on the per-field MAP "
          f"deltas): mean ΔMAP = **{c['mean']:+.4f}** "
          f"(95% CI [{c['ci'][0]:+.4f}, {c['ci'][1]:+.4f}], p = {c['p']:.3g}).")
    if gen["fields_lost"]:
        L("")
        L(f"Fields where SPECTER did **not** beat TF-IDF: "
          f"{', '.join(gen['fields_lost'])}.")
    L("")

    L("## Per-field results")
    L("")
    L("| field | works | eval q | cite Gini | PR Gini | interdisc | TF-IDF MAP | SPECTER MAP | ΔMAP | rel% | p | win |")
    L("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--:|")
    for f in r["fields"]:
        ev = f.get("eval") or {}
        tv = f.get("transformer_vs_tfidf")
        idf = f.get("interdisciplinarity") or {}
        tf_map = ev.get("tfidf", {}).get("map")
        sp_map = ev.get("transformer", {}).get("map")
        row = [
            f["field"], f"{f.get('works_loaded', 0):,}",
            str(f.get("eval_queries", "—")),
            f"{f.get('citation_gini', 0):.3f}",
            f"{(f.get('pagerank') or {}).get('gini', 0):.3f}",
            f"{idf.get('cross_field_fraction', 0):.3f}" if idf else "—",
            f"{tf_map:.3f}" if tf_map is not None else "—",
            f"{sp_map:.3f}" if sp_map is not None else "—",
            f"{tv['delta_map']:+.3f}" if tv else "—",
            f"{tv['rel_improvement_pct']:+.1f}" if tv and tv['rel_improvement_pct'] is not None else "—",
            f"{tv['paired_bootstrap_p']:.3f}" if tv else "—",
            ("✓" if tv and tv["specter_wins"] else ("✗" if tv else "—")),
        ]
        L("| " + " | ".join(row) + " |")
    L("")

    cr = conc["citation_gini_range"]
    pr = conc["pagerank_gini_range"]
    L("## Concentration (Gini by field)")
    L("")
    L(f"- Citation-count Gini range across fields: **[{cr[0]:.3f}, {cr[1]:.3f}]**")
    L(f"- PageRank Gini range across fields: **[{pr[0]:.3f}, {pr[1]:.3f}]**")
    L("")

    if inter:
        L("## Interdisciplinarity (fraction of references crossing field boundaries)")
        L("")
        L(f"- Range across fields: **[{inter['range'][0]:.3f}, {inter['range'][1]:.3f}]** "
          f"(mean {inter['mean']:.3f})")
        L("")
        L("Measured within the union of all loaded fields: for each field, the "
          "fraction of its references (whose target is loaded in *some* field) that "
          "point to a target in a **different** top-level field.")
        L("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT}")
    print(f"\nSPECTER>TF-IDF: {gen['fields_specter_wins']}/{gen['fields_evaluated']} "
          f"fields, combined p={c['p'] if c else None}")
    print(f"citation Gini range: [{cr[0]:.3f},{cr[1]:.3f}]")
    if inter:
        print(f"interdisciplinarity range: [{inter['range'][0]:.3f},{inter['range'][1]:.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
