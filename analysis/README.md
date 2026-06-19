# analysis/ — funding-landscape study

The first metascience study conducted on the research-atlas connected graph:
**"The structure of public research funding, 2015–2025."** The paper lives at
[`docs/papers/01-funding-landscape/`](../docs/papers/01-funding-landscape/);
this directory is the reproducible engine behind it.

## What's here

| File | Role |
|---|---|
| `funding_landscape.py` | All queries + statistics (Gini, HHI, bootstrap CIs). Read-only. The single source of truth. |
| `run.py` | Runs everything, writes `results.json`, renders the 5 figures. Idempotent. |
| `results.json` | Machine-readable results — **the paper and the tests both read this**, so prose can't drift from data. |
| `figures/` | The 5 PNG figures embedded in the paper. |

## Reproduce

From the repo root, with the graph (`research_atlas.duckdb`) present:

```bash
pip install -e .                       # duckdb, pandas, pyarrow, numpy, matplotlib
python analysis/run.py                 # recompute results.json + figures
python -m pytest tests/test_funding_landscape.py -q   # assert paper == data
python docs/papers/01-funding-landscape/build_pdf.py  # render paper.pdf
```

`python analysis/run.py --no-figures` skips rendering; `--db PATH` points at a
different DuckDB. Re-running is safe — it overwrites `results.json` and the
figures in place and converges to the same output.

If `research_atlas.duckdb` is absent, rebuild it from the published parquet /
source connectors per [`docs/GRAPH.md §3`](../docs/GRAPH.md).

## Honesty discipline (why these statistics, not others)

The graph has two documented noise sources — recipient fuzzy-match and
shared-grant double-counting — that affect **per-organization dollar sums only**.
Every headline result is therefore built on **counts** (grants, works) and on
**country/funder-level aggregates**, restricted to the **ROR-resolved subset**
where it matters. The one place a per-org *dollar* statistic appears
(`org_concentration_dollars_sensitivity`) is an explicit, labelled sensitivity
check. Sampled distributions carry **2,000-sample bootstrap 95% CIs**. Output
linkage exists for **NIH/NSF/EC only**, so all funding→output results are scoped
to those funders and say so. See the paper's §2.2, §2.3 and §5.
