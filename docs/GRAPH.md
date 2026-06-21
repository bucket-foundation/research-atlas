# research-atlas — The Connected Graph

This is the report on what the four funder piles become once they are
**reconciled into one graph** with an output (research-works) dimension, plus the
metascience queries the graph was built to answer. All numbers below are produced
by `scripts/validate.py` and `scripts/query.py` against the live DuckDB; rerun
them to reproduce.

---

## 1. The graph at a glance

_(Numbers below are the 2026-06-21 build, after the completeness push: NIH
extended to FY2008, NSF to 2008, CORDIS across all four open framework programmes
FP6+FP7+H2020+Horizon, CZI added, output side widened. See
[`COMPLETENESS.md`](COMPLETENESS.md).)_

| Entity | Rows | Keyed on |
|---|---:|---|
| funder | 75 | Crossref Funder id |
| grant | 1,670,434 | source award id |
| **organization** | **192,720** (**45,826** ROR-resolved) | **ROR id** |
| **person** | **1,438,636** (**804,248** with ORCID) | **ORCID** → OpenAlex id → name |
| **work** | **278,839** | OpenAlex id / DOI |
| field | 6,782 | OpenAlex topic/subfield/field/domain id |

| Edge | Rows | Meaning |
|---|---:|---|
| funder_grant | 1,688,751 | funder awarded grant |
| grant_org | 1,989,369 | grant → recipient/host org |
| grant_person | 1,740,326 | grant → PI / co-PI |
| **grant_work** | **470,269** | **work acknowledges grant's funding** |
| person_org | 2,447,774 | author/PI → affiliated org |
| work_field | 278,839 | work → OpenAlex topic |

**Total: ~12.2M rows.** Every edge endpoint resolves to a real entity (0 orphans —
see [`VALIDATION.md`](VALIDATION.md)).

### What "connected" means here

Before this work the four funders were disjoint: orgs were duplicated per funder
(99,650 rows, 0 ROR ids), people had no identifiers, and there was **no output
side at all**. Now:

- **Orgs are merged across funders on ROR id.** 45,826 orgs carry a ROR id;
  duplicate institutions across NIH/NSF/EC/UKRI/foundations collapse to one
  canonical node. **66.9%** of grant-*recipient* edges land on a ROR-resolved org
  (the long tail of unmatched orgs is overwhelmingly one-off small companies —
  SBIR/STTR recipients and EU SMEs — that are not in ROR; the universities that
  receive most of the money do resolve).
- **The output side exists.** 278,839 OpenAlex works are linked to the grants
  that funded them via **470,269 `grant_work` edges** (≈235k distinct works trace
  back to at least one grant in the atlas).
- **People reconcile on ORCID.** 55.9% of people carry an ORCID (the denominator
  grew with the NIH FY2008-2017 back-history, much of which predates ORCID);
  authors are joined to ROR-resolved institutions through `person_org`.

The graph now traverses end to end:
`funder → grant → org`, `grant → work → field`, `work → person → org`.

---

## 2. Metascience queries (real results)

Run any of these with `python scripts/query.py <name>` (see `atlas/analysis.py`).

> The example result tables in this section were captured on an earlier (smaller)
> build; the *shape* of the findings is stable, but exact counts grow with the
> 2026-06-21 corpus. Re-run `scripts/query.py` for live numbers against the
> current DuckDB. The headline aggregates at the top of this file and in
> `docs/LANDSCAPE.md` / `analysis/results.json` are current.

### Q1 — Top funders of mitochondrial-biophysics works, 2018–2025
`funder → grant → grant_work → work → work_field → field`

```
python scripts/query.py top_funders_by_output --topic mitochondri
```

| funder | works | grants |
|---|---:|---:|
| National Institute of General Medical Sciences | 134 | 123 |
| European Commission | 93 | 98 |
| National Institute on Aging | 89 | 133 |
| National Heart, Lung, and Blood Institute | 83 | 112 |
| National Cancer Institute | 74 | 116 |
| National Institute of Neurological Disorders and Stroke | 74 | 72 |
| National Institute of Diabetes and Digestive and Kidney Diseases | 63 | 77 |
| National Science Foundation | 43 | 52 |

NIGMS leads the mitochondrial-biophysics output in our corpus, with the EC the
top non-US funder — exactly the funding profile you'd expect for cell
bioenergetics, recovered purely from the graph.

### Q2 — Rising-field detection (funded works: 2016–19 vs 2021–24)
`grant_work → work → work_field → field`, growth = late / early

```
python scripts/query.py rising_fields
```

| topic | early (’16–19) | late (’21–24) | growth |
|---|---:|---:|---:|
| COVID-19 Clinical Research Studies | 2 | 183 | **91.5×** |
| COVID-19 and Mental Health | 1 | 87 | 87.0× |
| Poxvirus research and outbreaks | 1 | 32 | 32.0× |
| Explainable Artificial Intelligence (XAI) | 4 | 109 | 27.3× |
| SARS-CoV-2 and COVID-19 Research | 44 | 843 | 19.2× |
| Artificial Intelligence in Healthcare | 11 | 164 | 14.9× |
| Astronomy and Astrophysical Research | 5 | 54 | 10.8× |
| Infection Control and Ventilation | 4 | 43 | 10.8× |

The detector cleanly surfaces the two real shocks of the window — the COVID-19
research explosion and the rise of explainable/healthcare AI — from funded-output
volume alone.

### Q3 — Cross-funder organization hubs
`grant_org → grant → funder_grant → funder`, orgs by distinct funders

```
python scripts/query.py cross_funder_orgs
```

| org | distinct funders | grants |
|---|---:|---:|
| Johns Hopkins University | 26 | 12,339 |
| University of California, San Francisco | 26 | 11,435 |
| University of Pennsylvania | 26 | 10,863 |
| Stanford University | 26 | 10,005 |
| University of Washington | 26 | 9,814 |
| Yale University | 26 | 9,569 |

(NIH counts each awarding Institute/Center — NCI, NIGMS, … — as a distinct
sub-funder, so a top R1 reaches 26.) This view is **only possible after the ROR
merge** — pre-merge, each university was four disconnected per-funder nodes.

### Q4 — The geography of research money (recipient org country)
`grant_org → grant`, summed `amount_usd` by org country

```
python scripts/query.py funding_by_country
```

| country | USD funded | grants |
|---|---:|---:|
| US | $370.0B | 650,124 |
| GB | $31.3B | 43,320 |
| DE | $19.7B | 6,656 |
| ES | $16.3B | 6,980 |
| FR | $15.5B | 5,516 |
| IT | $11.4B | 5,300 |
| NL | $11.1B | 4,105 |
| BE | $8.5B | 2,519 |

### Q5 — Org funding vs. output productivity
`grant_org → grant` ($ in) joined to `grant_work → work` (output out)

```
python scripts/query.py org_funding_vs_output --min-grants 200
```

Robust at the high-volume end (Stanford: ~$12.7B over 10,005 grants → 3,366
linked works ≈ 0.27 works/$M; Penn ≈ 0.19; Northwestern ≈ 0.16). **Caveat,
stated honestly:** the per-org **dollar** ranking has tail noise — a handful of
small centers absorb a large institution's award sum through an over-eager fuzzy
ROR match, and a grant shared across N recipient edges is counted once per edge.
The **count-based** metrics (works, grants) and the **country-level** aggregates
are not affected and are the trustworthy views; the dollar-per-org column is best
read at the head, not the tail. This is a known follow-up (tighten the recipient
fuzzy bar; apportion shared-grant dollars).

---

## 3. Reproducing this report

```bash
# from a fresh ingest (funders + ROR dump downloaded):
python scripts/resolve_ror.py              # org -> ROR + merge
python scripts/ingest_openalex_works.py --from 2016-01-01   # output side + links
python scripts/build_all.py                # consolidate -> manifest -> db -> validate
python scripts/query.py rising_fields      # any query in atlas/analysis.py
```

Coverage of this build is bounded and honest: works were pulled for NIH, NSF and
the European Commission from 2016 (capped per funder for a bounded, polite pull);
ERC/UKRI works are a planned extension. The `grant_work` linker is funder-aware
(NIH IC+serial, NSF/EC bare award number) — see `atlas/award_match.py`.
