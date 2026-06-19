#!/usr/bin/env python3
"""Generate docs/LANDSCAPE.md -- the full funding-landscape report (REAL numbers).

Queries the consolidated flat parquet (via DuckDB, out-of-core) for the real
totals the founder asked for: funders, grants, orgs, people, $ funded
(USD-normalized), broken down by funder, by year, and by field, plus an honest
coverage table (ingested vs total-available per source). Writes docs/LANDSCAPE.md.

Usage:
    python scripts/landscape_report.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import DATA_PROCESSED, REPO_ROOT  # noqa: E402

OUT = REPO_ROOT / "docs" / "LANDSCAPE.md"

# Honest "total available at source" denominators, measured during ingest design
# (see each connector's module docstring + scripts/). These are the universe
# sizes we compare our ingested counts against in the coverage table.
SOURCE_UNIVERSE = {
    "cordis": {
        "label": "CORDIS (EU: Horizon Europe + H2020)",
        "grants_available": 56585,
        "note": "Full bulk zips ingested in entirety (all funding schemes). "
                "Older framework programmes (FP7 and earlier) not in these zips.",
    },
    "nsf": {
        "label": "NSF (US National Science Foundation)",
        "grants_available": None,  # no single authoritative total via API
        "note": "research.gov API, award start-date FY2015-2025, monthly windows. "
                "Pre-2015 and post-2025 not ingested.",
    },
    "nih": {
        "label": "NIH (US National Institutes of Health, RePORTER)",
        "grants_available": None,
        "note": "RePORTER v2 API, fiscal years 2018-2025, by (FY, IC). "
                "Pre-2018 (incl. CRISP 1970-2009) not ingested.",
    },
    "ukri": {
        "label": "UKRI (UK Research and Innovation, Gateway to Research)",
        "grants_available": 174405,
        "note": "Full GtR /projects corpus. Org coverage limited to embedded "
                "participantValues (no link-following); persons not resolved.",
    },
    "erc": {
        "label": "ERC (legacy ERC-only CORDIS slice)",
        "grants_available": None,
        "note": "Superseded by the full CORDIS connector; kept for back-compat.",
    },
    "dfg": {
        "label": "DFG (Germany, GEPRIS HTML)",
        "grants_available": 172292,
        "note": "Polite cached HTML crawl of English project detail pages "
                "(no bulk export exists). ~172k project ids are published in the "
                "GEPRIS sitemap; this is a real corpus chunk, resumable to the "
                "full set (cache makes re-runs free). GEPRIS rarely publishes a "
                "funding amount on the English page, so most DFG grants carry "
                "amount=null (money invariant).",
    },
    "gates": {
        "label": "Gates Foundation (Committed Grants CSV)",
        "grants_available": None,  # the CSV is the universe; equals ingested
        "note": "Full committed-grants CSV ingested in entirety (the bulk form "
                "of gatesfoundation.org/about/committed-grants). Amounts are USD. "
                "Coverage = 100% of the published CSV at download time.",
    },
    "wellcome": {
        "label": "Wellcome Trust (360Giving XLSX)",
        "grants_available": None,  # the XLSX is the universe; equals ingested
        "note": "Full 360Giving grants list (awarded since 2000-10-01) ingested "
                "in entirety. GBP normalized to USD via a stamped fixed FX. "
                "Coverage = 100% of the published XLSX at download time.",
    },
    "sloan": {
        "label": "Alfred P. Sloan Foundation (Grants Database HTML)",
        "grants_available": None,  # the public DB is the universe; equals ingested
        "note": "Full public grants database crawled (polite, cached). Sloan's "
                "DB covers currently-operating programs back to ~2008; pre-2008 "
                "completed programs live only in annual reports (not in the DB). "
                "Amounts are USD; Sloan publishes only an award year.",
    },
}

# Real funders that publish grant data but are NOT machine-ingestible without a
# headless browser (JS-rendered listings behind un-discoverable AJAX endpoints)
# or that actively block scraping. Documented honestly rather than faked.
NOT_INGESTIBLE = [
    ("Chan Zuckerberg Initiative (CZI)",
     "Publishes a searchable Grants Database (chanzuckerberg.com/grants-ventures/"
     "grants/) of $7.2B+ since 2015, but the listing is rendered by a JS bundle "
     "via a WordPress admin-ajax action that is not discoverable from static "
     "HTML. Needs a headless-browser connector (deferred)."),
    ("Howard Hughes Medical Institute (HHMI)",
     "~300 current Investigators (~$9M / 7-yr term each) are listed at "
     "hhmi.org/programs/investigators, but the scientist directory is JS-rendered "
     "and the browse endpoint returns HTTP 403 to non-browser agents. No per-"
     "investigator award amount is published. Honestly sparse; deferred."),
    ("Simons Foundation",
     "Funded-projects listing (simonsfoundation.org/funded-projects/) is "
     "JS-rendered with no static data or discoverable API. Deferred to a "
     "headless-browser connector."),
    ("Gordon and Betty Moore Foundation",
     "Grants are published at moore.org/grants ($401.8M / 952 grants in 2024) but "
     "the listing is JS-rendered behind an internal API not exposed in static "
     "HTML. Deferred to a headless-browser connector."),
]


def _md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def _fmt_usd(v):
    if v is None:
        return "n/a"
    return f"${v:,.0f}"


def main() -> int:
    import duckdb

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    p = DATA_PROCESSED

    def rp(table):
        return f"read_parquet('{p / f'{table}.parquet'}')"

    def count(table):
        f = p / f"{table}.parquet"
        if not f.exists():
            return 0
        return con.execute(f"SELECT count(*) FROM {rp(table)}").fetchone()[0]

    n_funder = count("funder")
    n_grant = count("grant")
    n_org = count("organization")
    n_person = count("person")
    n_field = count("field")
    total_rows = sum(count(t) for t in [
        "funder", "grant", "organization", "person", "field",
        "funder_grant", "grant_org", "grant_person", "person_org",
    ])

    total_usd = con.execute(
        f"SELECT sum(amount_usd) FROM {rp('grant')} WHERE amount_usd IS NOT NULL"
    ).fetchone()[0]
    n_grant_with_money = con.execute(
        f"SELECT count(*) FROM {rp('grant')} WHERE amount_usd IS NOT NULL"
    ).fetchone()[0]

    # By source.
    by_source = con.execute(f"""
        SELECT source,
               count(*) AS grants,
               sum(amount_usd) AS usd,
               count(amount_usd) AS with_money
        FROM {rp('grant')}
        GROUP BY source ORDER BY grants DESC
    """).fetchall()

    # By year (grant start year).
    by_year = con.execute(f"""
        SELECT substr(start_date,1,4) AS yr, count(*) AS grants,
               sum(amount_usd) AS usd
        FROM {rp('grant')}
        WHERE start_date IS NOT NULL AND substr(start_date,1,4) >= '2010'
        GROUP BY yr ORDER BY yr DESC
    """).fetchall()

    # Top funders by $ (join funder_grant -> grant, attribute $ to awarder).
    top_funders = con.execute(f"""
        SELECT f.name, f.short_name,
               count(DISTINCT fg.dst_id) AS grants,
               sum(g.amount_usd) AS usd
        FROM {rp('funder_grant')} fg
        JOIN {rp('funder')} f ON f.atlas_id = fg.src_id
        JOIN {rp('grant')} g ON g.atlas_id = fg.dst_id
        GROUP BY f.name, f.short_name
        ORDER BY usd DESC NULLS LAST
        LIMIT 20
    """).fetchall()

    # Top fields by grant count (via grant program string is coarse; we report
    # the field entity counts by source instead, plus euroSciVoc top fields).
    top_fields = con.execute(f"""
        SELECT name, source FROM {rp('field')}
        ORDER BY name LIMIT 0
    """).fetchall()  # placeholder; fields are taxonomy nodes, see note below

    # Top recipient orgs by total $ received.
    top_orgs = con.execute(f"""
        SELECT o.name, o.country_code,
               count(DISTINCT go.src_id) AS grants,
               sum(g.amount_usd) AS usd
        FROM {rp('grant_org')} go
        JOIN {rp('organization')} o ON o.atlas_id = go.dst_id
        JOIN {rp('grant')} g ON g.atlas_id = go.src_id
        WHERE go.role = 'recipient'
        GROUP BY o.name, o.country_code
        ORDER BY usd DESC NULLS LAST
        LIMIT 25
    """).fetchall()

    # ROR resolution coverage.
    org_with_ror = con.execute(
        f"SELECT count(*) FROM {rp('organization')} WHERE ror_id IS NOT NULL"
    ).fetchone()[0]

    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    lines = []
    lines.append("# research-atlas — Global Research Funding Landscape\n")
    lines.append(f"*Generated {ts} · schema 0.1.0 · "
                 "all money USD-normalized (see money invariant)*\n")
    lines.append("> This is the **full-scale** ingest report. Money totals cover "
                 "only grants whose source publishes an amount; grants with "
                 "unknown money are counted but contribute `null` (never a "
                 "silent 0) to `$ funded`.\n")

    lines.append("## Headline totals\n")
    lines.append(_md_table(
        ["metric", "value"],
        [
            ["Funders", f"{n_funder:,}"],
            ["Grants", f"{n_grant:,}"],
            ["Organizations", f"{n_org:,}"],
            ["People (PIs)", f"{n_person:,}"],
            ["Fields (taxonomy nodes)", f"{n_field:,}"],
            ["Total $ funded (USD)", _fmt_usd(total_usd)],
            ["Grants with a known amount", f"{n_grant_with_money:,} / {n_grant:,}"],
            ["Total rows in graph", f"{total_rows:,}"],
            ["Orgs resolved to ROR", f"{org_with_ror:,} / {n_org:,}"],
        ]))
    lines.append("")

    lines.append("## By funder source\n")
    rows = []
    for src, grants, usd, wm in by_source:
        rows.append([src, f"{grants:,}", _fmt_usd(usd),
                     f"{wm:,}" if wm else "0"])
    lines.append(_md_table(["source", "grants", "$ funded (USD)",
                            "grants w/ amount"], rows))
    lines.append("")

    lines.append("## Top funders by $ awarded\n")
    rows = []
    for name, short, grants, usd in top_funders:
        rows.append([f"{name} ({short})" if short else name,
                     f"{grants:,}", _fmt_usd(usd)])
    lines.append(_md_table(["funder", "grants", "$ awarded (USD)"], rows))
    lines.append("")

    lines.append("## By year (grant start year, 2010+)\n")
    rows = [[yr, f"{grants:,}", _fmt_usd(usd)] for yr, grants, usd in by_year]
    lines.append(_md_table(["year", "grants", "$ funded (USD)"], rows))
    lines.append("")

    lines.append("## Top 25 recipient organizations by $ received\n")
    rows = []
    for name, cc, grants, usd in top_orgs:
        rows.append([name, cc or "?", f"{grants:,}", _fmt_usd(usd)])
    lines.append(_md_table(["organization", "country", "grants",
                            "$ received (USD)"], rows))
    lines.append("")

    lines.append("## Coverage (ingested vs available, honest)\n")
    rows = []
    src_counts = {s: (g, usd) for s, g, usd, _ in by_source}
    for src, info in SOURCE_UNIVERSE.items():
        if src not in src_counts:
            continue
        ing, usd = src_counts[src]
        avail = info["grants_available"]
        pct = f"{100*ing/avail:.1f}%" if avail else "n/a"
        rows.append([info["label"], f"{ing:,}",
                     f"{avail:,}" if avail else "n/a", pct, info["note"]])
    lines.append(_md_table(
        ["source", "grants ingested", "grants available",
         "coverage", "notes"], rows))
    lines.append("")

    lines.append("## Published funders not yet machine-ingestible (honest)\n")
    lines.append("These funders publish real grant data, but their listings are "
                 "JS-rendered behind un-discoverable AJAX endpoints, or block "
                 "non-browser agents. They are documented here rather than "
                 "faked; each needs a headless-browser connector (deferred).\n")
    lines.append(_md_table(
        ["funder", "why deferred"],
        [[name, note] for name, note in NOT_INGESTIBLE]))
    lines.append("")

    lines.append("## Field taxonomies\n")
    field_by_source = con.execute(f"""
        SELECT source, count(*) FROM {rp('field')}
        GROUP BY source ORDER BY count(*) DESC
    """).fetchall()
    lines.append("Each source contributes its own field taxonomy (euroSciVoc for "
                 "CORDIS, NSF directorates, NIH ICs, GtR research topics). "
                 "OpenAlex topic reconciliation is a separate, not-yet-built "
                 "connector.\n")
    lines.append(_md_table(["source", "field nodes"],
                           [[s, f"{n:,}"] for s, n in field_by_source]))
    lines.append("")

    lines.append("## What a full-completion run still needs\n")
    lines.append(
        "- **NSF**: extend below FY2015 (API supports it; just more monthly "
        "windows). Resolve awardee orgs to ROR (currently name-keyed).\n"
        "- **NIH**: extend below FY2018 (RePORTER covers to 1985; CRISP to 1970). "
        "Pull abstracts + publication/patent link tables.\n"
        "- **UKRI**: follow LEAD_ORG/PARTICIPANT_ORG links so fellowships/"
        "studentships (no embedded participants) get org edges; resolve PIs "
        "(person link-following, off for the bulk run); GBP→USD already applied.\n"
        "- **CORDIS**: add FP7 and earlier framework programmes (separate zips); "
        "resolve PI names from per-project web records (not in bulk export).\n"
        "- **DFG**: a real GEPRIS corpus chunk is ingested; the crawl is "
        "resumable to the full ~172k sitemap set (cache makes re-runs free) at "
        "polite rate. Most pages omit the funding amount (money stays null).\n"
        "- **Gates / Wellcome / Sloan**: published feeds ingested in full at "
        "download time; re-run to refresh (Gates CSV monthly, Wellcome XLSX "
        "monthly, Sloan DB live).\n"
        "- **CZI / HHMI / Simons / Moore**: real funders whose listings are "
        "JS-rendered behind un-discoverable APIs (or block scraping); need a "
        "headless-browser connector (deferred, see table above).\n"
        "- **Cross-source**: ROR resolution as a batch backfill (bulk ROR dump) "
        "to merge duplicate orgs across funders; OpenAlex works + topic fields; "
        "ORCID person reconciliation; currency FX refresh.\n")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    con.close()
    print(f"Wrote {OUT}")
    print(f"  funders={n_funder:,} grants={n_grant:,} orgs={n_org:,} "
          f"people={n_person:,} fields={n_field:,}")
    print(f"  total $ funded (USD) = {_fmt_usd(total_usd)}")
    print(f"  total rows in graph  = {total_rows:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
