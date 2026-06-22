#!/usr/bin/env python3
"""Advanceable cross-field paper-ranking runner (checkpointed + resumable).

Runs the all-26-field, impact-ranked, producer/consumer pipeline ONE checkpoint
at a time. Re-running advances the corpus to the next tranche; a crash mid-run
resumes from the durable per-field partial + the raw-page / embedding caches.

State lives in ``analysis/crossfield/state.json``. Results land in
``analysis/crossfield/checkpoint_<N>.json`` + ``manifest.json`` +
``convergence.jsonl``.

Usage:
    # run the NEXT checkpoint (first run -> checkpoint 1, top ~3k/field)
    python scripts/crossfield_run.py

    # run a specific checkpoint (idempotent; re-analyzes only unfinished fields)
    python scripts/crossfield_run.py --checkpoint 1

    # custom tranche schedule (per-field work targets)
    python scripts/crossfield_run.py --tranches 3000,5000,20000,50000

    # baselines only (no GPU), smaller sample -- fast smoke
    python scripts/crossfield_run.py --no-transformer --eval-sample 800 \
        --fields 26,21 --tranches 1000

    # just measure GPU embedding throughput on the current cache
    python scripts/crossfield_run.py --measure-throughput
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.ranking.corpus import CorpusConnector  # noqa: E402
from atlas.ranking.crossfield import (  # noqa: E402
    ANALYSIS_DIR, DEFAULT_TRANCHES, DEFAULT_WINDOW, FIELDS, CrossFieldState,
    run_checkpoint, measure_embed_throughput, load_field_records)


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross-field ranking runner.")
    ap.add_argument("--checkpoint", type=int, default=0,
                    help="run this checkpoint (1-based); 0 = next after state")
    ap.add_argument("--tranches", default=None,
                    help="comma-separated per-field work targets "
                         f"(default {','.join(map(str, DEFAULT_TRANCHES))})")
    ap.add_argument("--fields", default=None,
                    help="comma-separated OpenAlex field ids (default all 26)")
    ap.add_argument("--window", default=",".join(DEFAULT_WINDOW),
                    help="from,to publication date window")
    ap.add_argument("--eval-sample", type=int, default=4000)
    ap.add_argument("--min-refs", type=int, default=5)
    ap.add_argument("--max-queries", type=int, default=2000)
    ap.add_argument("--mask-frac", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--embed-batch", type=int, default=64)
    ap.add_argument("--no-transformer", action="store_true")
    ap.add_argument("--measure-throughput", action="store_true",
                    help="measure steady-state GPU docs/s on the loaded sample "
                         "and exit (no eval)")
    args = ap.parse_args()

    tranches = ([int(x) for x in args.tranches.split(",")]
                if args.tranches else list(DEFAULT_TRANCHES))
    fields = (args.fields.split(",") if args.fields else list(FIELDS))
    window = tuple(args.window.split(","))

    state_path = ANALYSIS_DIR / "state.json"
    state = CrossFieldState.load(state_path)
    if state is None:
        state = CrossFieldState(tranches=tranches, window=window, fields=fields)
    else:
        # honor a new schedule / field set on re-run (the corpus only grows)
        state.tranches = tranches
        state.fields = fields
        state.window = window

    ckpt = args.checkpoint or (state.checkpoint + 1)
    conn = CorpusConnector()

    # single-runner lock: refuse to start if another run is alive (avoids two
    # processes racing on the shared partial/state + embedding cache).
    lock = ANALYSIS_DIR / "run.lock"
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    if lock.exists() and not args.measure_throughput:
        try:
            old = int(lock.read_text().strip())
            import os as _os
            _os.kill(old, 0)            # raises if pid is dead
            print(f"Another crossfield run (pid {old}) is active. "
                  f"Wait for it or remove {lock} if it is stale.", flush=True)
            return 2
        except (ValueError, ProcessLookupError, PermissionError):
            pass                         # stale lock -> reclaim
    if not args.measure_throughput:
        import os as _os
        lock.write_text(str(_os.getpid()))

    if args.measure_throughput:
        # measure GPU docs/s on whatever is cached for the requested tranche
        tval = ckpt if ckpt <= len(tranches) else len(tranches)
        target = tranches[tval - 1]
        work_ids, texts = [], []
        for fid in fields:
            recs = load_field_records(conn, fid, target, window, cache_only=True)
            for r in recs:
                if r.get("text"):
                    work_ids.append(r["work_id"]); texts.append(r["text"])
        print(f"Measuring GPU throughput on {len(work_ids):,} cached docs ...",
              flush=True)
        res = measure_embed_throughput(work_ids, texts, batch_size=args.embed_batch)
        print(f"  {res}", flush=True)
        return 0

    if ckpt > len(tranches):
        print(f"Checkpoint {ckpt} exceeds the {len(tranches)}-tranche schedule. "
              f"Add more tranches with --tranches to grow further.", flush=True)
        return 1

    print(f"=== Cross-field checkpoint {ckpt} "
          f"(top {tranches[ckpt-1]:,} works/field, {len(fields)} fields) ===",
          flush=True)

    report = run_checkpoint(
        ckpt, tranches=tranches, window=window, fields=fields,
        eval_sample=args.eval_sample, min_refs=args.min_refs,
        max_queries=args.max_queries, mask_frac=args.mask_frac, seed=args.seed,
        do_transformer=not args.no_transformer, n_boot=args.n_boot,
        embed_batch=args.embed_batch, conn=conn, analysis_dir=ANALYSIS_DIR)

    # advance state only after a clean checkpoint
    if args.checkpoint == 0 or ckpt == state.checkpoint + 1:
        state.checkpoint = max(state.checkpoint, ckpt)
    state.done_fields[str(ckpt)] = [f["field_id"] for f in report["fields"]]
    state.save(state_path)

    # ---- console summary -------------------------------------------------- #
    gen = report["crossfield"]["generalization"]
    conc = report["crossfield"]["concentration"]
    inter = report["crossfield"]["interdisciplinarity"]
    print(f"\n--- checkpoint {ckpt} summary ---", flush=True)
    print(f"fields with results : {report['fields_with_results']}/{len(fields)}",
          flush=True)
    print(f"total works loaded  : {report['total_works_loaded']:,}", flush=True)
    if report.get("embed_docs_per_sec"):
        print(f"GPU embed docs/s    : {report['embed_docs_per_sec']:.1f} "
              f"(steady-state)", flush=True)
    print(f"SPECTER>TF-IDF      : {gen['fields_specter_wins']}/"
          f"{gen['fields_evaluated']} fields  "
          f"(win frac {gen['win_fraction']:.2f}, sign-test p={gen['sign_test_p']:.4g})",
          flush=True)
    if gen["combined_field_level"]:
        c = gen["combined_field_level"]
        print(f"combined field-level: mean dMAP={c['mean']:+.4f} "
              f"CI[{c['ci'][0]:+.4f},{c['ci'][1]:+.4f}] p={c['p']:.4g}", flush=True)
    cr = conc["citation_gini_range"]
    print(f"citation Gini range : [{cr[0]:.3f}, {cr[1]:.3f}]", flush=True)
    if inter:
        ir = inter["range"]
        print(f"interdisc. range    : [{ir[0]:.3f}, {ir[1]:.3f}] "
              f"(mean {inter['mean']:.3f})", flush=True)
    print(f"\nwrote {ANALYSIS_DIR / f'checkpoint_{ckpt}.json'}", flush=True)
    nxt = ckpt + 1
    if nxt <= len(tranches):
        print(f"advance the loop : python scripts/crossfield_run.py  "
              f"(-> checkpoint {nxt}, top {tranches[nxt-1]:,}/field)", flush=True)
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
