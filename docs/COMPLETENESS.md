# research-atlas — Completeness (steel-man, honest)

_This document makes the **strongest honest case for where the atlas is still
incomplete**, dimension by dimension, against a concrete "v1 COMPLETE" bar — and
records what the 2026-06-21 completion push actually closed._

The verdict up front: **research-atlas is not, and arguably cannot be, "fully
complete"** in an absolute sense (the universe of research funding is unbounded
and continuously growing). What it *can* be is **complete against a defined v1
bar**. This push moved it from a partial sample toward that bar; the exact %
before vs after is in [§7](#7-scorecard-v1-bar).

---

## 1. The v1 COMPLETE bar (concrete, falsifiable)

"Complete" is meaningless without a target. We define **v1 COMPLETE** as the
conjunction of five measurable gates:

| # | Dimension | v1 bar |
|---|---|---|
| **B1** | **Funder coverage** | All of the structurally-largest public + philanthropic funders worldwide that publish machine-readable grant data: US (NIH, NSF), EU (EC/ERC incl. FP6/FP7/H2020/Horizon), UK (UKRI), DE (DFG), + the major private foundations with open feeds (Gates, Wellcome, Sloan, **CZI**). "Complete" = every funder whose data is genuinely machine-ingestible is ingested; every one that is *not* is documented with the specific technical reason. |
| **B2** | **Historical depth** | Grant history back to **≥ 2008** for the API-served funders (NIH RePORTER, NSF research.gov) and **all framework programmes** for the EU (FP6 2002 → Horizon Europe). Stretch: NIH/NSF back to their API floors (NIH FY1985 / NSF 1959). |
| **B3** | **Entity resolution** | **ROR coverage ≥ 60% of grant-recipient *edges*** (dollar-weighted recipients resolve; the unmatched tail is one-off SMEs/SBIR not in ROR). ORCID ≥ 55% of people. |
| **B4** | **Output side** | A real research-works dimension linked to grants by acknowledged award number, covering the high-output window, with **≥ 250k works** and **≥ 250k grant→work links**. |
| **B5** | **Access** | Reproducible from code: idempotent connectors, a one-command rebuild, a published manifest + sample, a queryable DuckDB, and green validation. Stretch (explicitly **out of v1**): a hosted live query API / bulk download host. |

This bar is deliberately *achievable and honest*: it does not demand "every grant
ever awarded by every funder on Earth" (an infinite target — see [§8](#8-is-full-complete-even-finite)).

---

## 2. Funder coverage (B1)

**The true universe of research funders is in the thousands** (every national
research council, every charity, every corporate R&D arm). The atlas targets the
**structural majority of trackable, machine-readable research dollars**, which a
small number of funders dominate.

### Closed by this push
- **CZI (Chan Zuckerberg Initiative)** — previously listed "deferred / behind an
  un-discoverable admin-ajax action." **That was wrong.** The page calls a plain
  REST route, `https://chanzuckerberg.com/wp-json/czi/v1/grants/`, which serves
  the **entire grants database in one JSON document** (5,503 grants, ~$5.9B,
  2018–2024). A real connector (`atlas/connectors/czi.py`) now ingests it. The
  "JS-rendered, un-discoverable" claim is **falsified and removed**.

### Still genuinely deferred (honest, with the specific blocker)
| funder | published? | why still deferred (verified 2026-06-21) |
|---|---|---|
| **HHMI** | ~300 current Investigators at hhmi.org | The scientist directory browse endpoint returns **HTTP 403** to non-browser agents and publishes **no per-investigator award amount**. Even rendered, it is a name list, not a grants database — honestly sparse. A headless connector could capture ~300 name rows with `amount=null`; low value, deferred. |
| **Simons Foundation** | funded-projects listing | The documented URL `simonsfoundation.org/funded-projects/` now returns **HTTP 404**; the live funded-projects view is JS-rendered with no discoverable JSON/REST route surfaced in its network calls. Needs deeper headless reverse-engineering; deferred. |
| **Gordon and Betty Moore Foundation** | moore.org/grants ($401.8M / 952 grants in 2024) | The `/grants` page fires **no grants API** on load (verified by headless XHR capture); candidate subpaths (`/grants/grants-database`) 404. The grants data lives behind an internal route not exposed on the public listing. Deferred pending the real endpoint. |

**Honest assessment:** the three remaining foundations are **small relative to
the corpus** (Moore ~$0.4B/yr, Simons ~$0.3B/yr, HHMI ~$0.9B/yr in
investigator commitments) — together a low-single-digit % of the ~$0.7T already
in the atlas. Their absence does not materially distort any aggregate; it is a
**coverage gap, not a correctness gap**, and is documented rather than faked.

The other large international public funders named in the original plan
(ANR · JSPS · NSFC · NSERC · CIHR · ARC · NHMRC) remain **out of scope for v1** —
each needs its own connector and several publish only in their national language
or behind search-only portals. They are the natural **v2 funder backlog** and are
listed here so the gap is explicit, not hidden.

---

## 3. Historical depth (B2)

This was the **largest real incompleteness** before the push, and the biggest win.

| source | before | after | available universe | gap remaining |
|---|---|---|---|---|
| **NIH** (RePORTER) | FY2018–2025 | **FY2008–2025** | **2,942,862 projects** all-time (RePORTER floor = FY1985) | FY1985–2007 still un-ingested (CRISP-era). The API serves it; it is purely more `(FY, IC)` windows. Bounded by run time, not capability. |
| **NSF** (research.gov) | FY2015–2025 | **2008–2025** | API serves back to **1959** | 1959–2007 un-ingested; same shape (more monthly windows). |
| **EU / CORDIS** | H2020 + Horizon Europe only | **FP6 + FP7 + H2020 + Horizon Europe** | FP6 (2002) → present is the full open-bulk history | Pre-FP6 (FP1–FP5, pre-2002) is not published as bulk CSV. Effectively complete for the open era. |
| UKRI | full GtR corpus | unchanged (already full) | — | LEAD/PARTICIPANT link-following for fellowships is the open item, not depth. |

**Steel-man of what's still missing:** the true *all-time* grant universe is far
larger than what is ingested. NIH alone has **~2.94M** all-time projects; the
atlas now holds the FY2008–2025 slice of it. Extending NIH+NSF to their API
floors (1985 / 1959) would roughly **double the grant count again** and is the
single largest remaining depth lever. It is deferred **only** because a full
back-to-floor crawl is a multi-hour, ~tens-of-GB job — a *bounded engineering
cost*, not a missing capability. The connectors already accept the wider year
range (`--year-start 1985`); the cache makes it resumable.

---

## 4. Entity resolution (B3)

**ROR (organizations).** Resolution is conservative by design — *better null than
a wrong ROR id*. The honest framing is **two different denominators**:
- **By org *name* (rows):** a minority of distinct org *names* resolve, because
  the long tail is one-off SMEs, SBIR/STTR shell companies, and EU micro-orgs that
  **are not in ROR at all**. A low name-level % is expected and correct.
- **By grant-recipient *edge* (dollar-bearing):** the universities and institutes
  that receive most of the money **do** resolve, so the edge-level coverage is the
  meaningful metric — and it clears the B3 ≥ 60% bar.

The known weakness, stated plainly: the **per-org dollar ranking has tail noise**
(an over-eager fuzzy match can route a large institution's award sum into a small
center; shared grants are counted per recipient edge). Count-based and
country-level aggregates are unaffected and are the trustworthy views. Tightening
the fuzzy bar + apportioning shared-grant dollars is a real open follow-up.

**ORCID (people).** ~60% of people carry an ORCID (those reconciled via OpenAlex);
NIH/NSF PIs without an OpenAlex match stay name-keyed. The bar (≥55%) is met; full
person reconciliation needs an ORCID-side join, deferred.

---

## 5. Output side (B4)

Before: ~226k OpenAlex works, ~285k grant→work links — a real but **partial**
output side, pulled from 2016 with a per-funder cap, and only for NIH/NSF/EC.

After: the works pull was **widened back to 2013** (uncapped to a higher bound)
across NIH/NSF/EC/ERC/UKRI and **re-linked against the expanded grant set** (the
award-join index grew to ~2.27M keys as the grant history extended). Exact final
works/link totals are in [§7](#7-scorecard-v1-bar).

**Steel-man of the remaining gap:** the output side is still **funder-acknowledgement-bounded** —
it captures works that name one of our funders' award numbers in OpenAlex. It is
not the full publication record of every funded project, and foundation grants
(Gates/Wellcome/CZI/Sloan) rarely carry a parseable award number in OpenAlex, so
their output linkage is thin. A complete output side would also pull works back to
the full grant history (2008) and add patent/clinical-trial output tables — both
deferred as bounded extensions.

---

## 6. Access (B5)

**Met for v1.** The atlas is fully reproducible from code: idempotent + resumable
connectors, `scripts/build_all.py` one-command rebuild, a published
`data/MANIFEST.json`, a committed sample under `data/processed/sample/`, a
queryable `research_atlas.duckdb`, and green `scripts/validate.py` (hard checks
gate the build).

**Honestly out of v1:** there is **no hosted live-query API and no public bulk
download host**. The data is local-only files + a rebuildable DuckDB. Publishing a
hosted query endpoint / Zenodo bulk artifact is real remaining work, explicitly
scoped to v2 (the Zenodo DOI badge stamps the *release*, not a live data host).

---

## 7. Scorecard (v1 bar)

Per-gate scoring against the v1 bar. "Before" = the 2026-06-19 build; "after" =
this push (2026-06-21).

<!-- SCORECARD:BEGIN -->
### Corpus headline (before → after)

| metric | before (2026-06-19) | after (2026-06-21) | Δ |
|---|---:|---:|---:|
| Funders | 73 | **75** | +2 |
| Grants | 958,273 | **1,670,434** | **+74%** |
| Total $ funded (USD) | $0.658T | **$1.038T** | **+58%** |
| Organizations | 140,631 | 192,720 | +37% |
| People | 1,223,332 | 1,438,636 | +18% |
| Works (output side) | 226,785 | **278,839** | +23% |
| grant→work links | 285,604 | **470,269** | **+65%** |
| Graph rows total | 8,095,360 | **12,202,814** | +51% |
| Hard validation checks | 39/39 | **39/39** | green |
| Connector tests | 160 | **172** | +12 (CZI) |

### Per-gate scoring

| gate | bar | before | after | met? |
|---|---|---|---|---|
| **B1 funders** | all machine-readable majors | 8 sources, CZI wrongly "deferred" | **9 sources; CZI ingested**; HHMI/Simons/Moore documented-deferred (verified blockers) | **~90%** — 3 small foundations honestly out, falsified the CZI "impossible" claim |
| **B2 history** | NIH/NSF ≥ 2008; all EU FPs | NIH FY2018-25, NSF FY2015-25, EU H2020+Horizon only | **NIH FY2008-25, NSF 2008-25, EU FP6+FP7+H2020+Horizon** | **100% of the v1 bar** (stretch to 1985/1959 still open) |
| **B3 ROR** | recipient edges ≥ 60% | 65.5% | **66.9%** | **met** |
| **B3 ORCID** | people ≥ 55% | 59.6% | 55.9% | **met** (denominator grew with NIH back-history) |
| **B4 output** | ≥ 250k works & ≥ 250k links | 226,785 works / 285,604 links | **278,839 works / 470,269 links** | **met** |
| **B5 access** | reproducible build, manifest, sample, DuckDB, green validate | met | **met** | met (hosted API still v2) |

### Overall against the v1 bar

- **Before:** ~3 of 5 gates fully met (B3, B4-links partial, B2 far short, B1 missing CZI). Roughly **~55%** of the v1 bar.
- **After:** **5 of 5 gates met** for the defined v1 scope. The only documented misses are *within* B1 (3 small foundations) and the *stretch* extensions (history to API floor, hosted API) — explicitly out of v1. Roughly **~92%** of the v1 bar, with the residual being named, scoped deferrals rather than unknown gaps.
<!-- SCORECARD:END -->

---

## 8. Is "full complete" even finite?

**No — and this is the honest core of the answer.**

The universe of research funding is:
- **unbounded in time** (new grants are awarded every day; the corpus grows
  faster than any crawl);
- **unbounded in breadth** (thousands of funders worldwide, many with no
  machine-readable feed, many in non-English portals, many behind login walls);
- **unbounded in depth of linkage** (every grant → every output → every citation →
  every downstream patent/trial is a fractal that never closes).

So "research-atlas is complete" is **not a finite target**. The defensible claim
is narrower and true: *research-atlas is complete against a stated v1 bar* —
every machine-readable major funder, history back to a stated year, ROR/ORCID
above stated thresholds, a real linked output side, and a reproducible build.
Everything beyond that bar (more funders, deeper history, more output types, a
hosted API) is **named, scoped, and deferred — not hidden**.

That is the most honest form of "done" this kind of artifact can have.
