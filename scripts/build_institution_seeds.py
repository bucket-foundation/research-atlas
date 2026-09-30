from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import REPO_ROOT
from atlas.users.contacts import UA
from atlas.users.haul import (
    RobotsCache,
    RorIndex,
    TokenBucket,
    advisor_tiers,
    parse_wiki_carnegie,
    ror_country,
    ror_display,
    ror_domains,
    ror_homepage,
    ror_short,
)

ROR_DUMP = REPO_ROOT / "data" / "raw" / "ror" / "v2.8-2026-06-02-ror-data.json"
RAW = REPO_ROOT / "data" / "raw" / "seeds"
OUT = REPO_ROOT / "data" / "seeds" / "institutions.csv"
COCKPIT = REPO_ROOT.parent / "biophysics-phd-review" / "data" / "processed"
WIKI = "https://en.wikipedia.org/w/index.php?title=List_of_research_universities_in_the_United_States&action=raw"
CARNEGIE = "https://carnegieclassifications.acenet.edu/"
PROBE_PATHS = ("/directory", "/people", "/faculty", "/faculty-directory", "/experts", "/sitemap.xml")
HAND_MAP = {
    "Rutgers University\u2013New Brunswick": "05vt9qd57",
    "Rutgers University\u2013Camden": "05vt9qd57",
    "Rutgers University\u2013Newark": "05vt9qd57",
    "CUNY Hunter College": "00g2xk477",
    "LSU New Orleans": "01qv8fp92",
    "Louisiana State University New Orleans": "01qv8fp92",
    "Oklahoma State University Center for Health Sciences": "02mfxdp77",
    "University of Oklahoma Health Sciences Center": "0457zbj98",
    "University of Oklahoma-Health Sciences Center": "0457zbj98",
}
FIELDS = ("name", "ror_id", "country_code", "domains", "homepage", "carnegie_class",
          "directory_entry_urls", "entry_kind", "platform_guess", "licence", "source_revision",
          "source_retrieved")
PRIVATE_COUNTS = REPO_ROOT / "data" / "private" / "seed_advisor_counts.csv"
PRIVATE_EXTRA = REPO_ROOT / "data" / "private" / "seed_advisor_extra.csv"
LICENCES = {"wikipedia": "Carnegie list via Wikipedia CC-BY-SA-4.0; ROR CC0-1.0",
            "carnegie": "Carnegie Classification terms; ROR CC0-1.0", "": "ROR CC0-1.0"}


def http_get(url: str, limit: int = 600_000, deadline: float = 20.0) -> tuple[int, str, str]:
    import time

    import requests

    start = time.monotonic()
    with requests.get(url, headers={"User-Agent": UA}, timeout=(10, 10), stream=True, allow_redirects=True) as r:
        chunks, size = [], 0
        if r.status_code == 200:
            while chunk := r.raw.read1(65536, decode_content=True):
                chunks.append(chunk)
                size += len(chunk)
                if size >= limit or time.monotonic() - start > deadline:
                    break
        return r.status_code, b"".join(chunks).decode(r.encoding or "utf-8", "replace"), r.url


WIKI_API = ("https://en.wikipedia.org/w/api.php?action=query&prop=revisions&rvprop=ids|timestamp|content"
            "&rvslots=main&format=json&formatversion=2&titles=List_of_research_universities_in_the_United_States")


def carnegie_lists() -> tuple[dict[str, list[tuple[str, str]]], str, dict]:
    RAW.mkdir(parents=True, exist_ok=True)
    cached = RAW / "wikipedia_research_universities.json"
    if not cached.exists():
        status, text, _ = http_get(WIKI_API, limit=5_000_000)
        if status != 200:
            raise SystemExit(f"wikipedia list HTTP {status}")
        data = json.loads(text)
        rev = data["query"]["pages"][0]["revisions"][0]
        from datetime import datetime, timezone

        cached.write_text(json.dumps({"revid": rev["revid"], "rev_timestamp": rev["timestamp"],
                                      "retrieved": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                                      "content": rev["slots"]["main"]["content"]}))
    meta = json.loads(cached.read_text())
    prov = {"source_revision": f"enwiki:{meta['revid']}", "source_retrieved": meta["retrieved"]}
    return parse_wiki_carnegie(meta["content"]), "wikipedia", prov


def detect(ror: str, homepage: str, body: str) -> str:
    try:
        from atlas.users import directories

        fn = getattr(directories, "detect_platform", None)
        if fn:
            got = fn(ror, homepage)
            if got:
                return str(got)
    except Exception:
        pass
    low = body.lower()
    for marker, name in (("__next_data__", "nextjs"), ("drupal", "drupal"), ("wp-content", "wordpress"),
                         ("sitecore", "sitecore"), ("/_nuxt/", "nuxt"), ("cascade", "cascade"),
                         ("omniupdate", "omni_cms"), ("t4 site manager", "terminalfour")):
        if marker in low:
            return name
    return ""


def entry_kind(urls: str) -> str:
    items = [u for u in urls.split(";") if u]
    if any(not u.rstrip("/").endswith("sitemap.xml") for u in items):
        return "directory"
    return "sitemap_only" if items else "none"


def probe(row: dict, bucket: TokenBucket, robots: RobotsCache) -> dict:
    home = row["homepage"]
    if not home:
        return row
    p = urlparse(home)
    base = f"{p.scheme or 'https'}://{p.netloc}"
    doms = row["domains"].split(";") if row["domains"] else []
    keep, body = [], ""
    for path in PROBE_PATHS:
        url = base + path
        try:
            if not robots.allowed(url):
                continue
            bucket.acquire(p.netloc)
            status, text, final = http_get(url)
        except Exception:
            continue
        host = urlparse(final).netloc.lower()
        if status != 200 or not any(host == d or host.endswith("." + d) for d in doms):
            continue
        body = body or text
        keep.append(final)
    row["directory_entry_urls"] = ";".join(dict.fromkeys(keep))
    row["entry_kind"] = entry_kind(row["directory_entry_urls"])
    row["platform_guess"] = detect(row["ror_id"], home, body)
    return row


STRAGGLERS = 0


def probe_all(rows: list[dict], bucket: TokenBucket, robots: RobotsCache, workers: int, out: Path,
              timeout: float = 900.0) -> list[dict]:
    import time
    from concurrent.futures import as_completed

    RAW.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_suffix(".partial.csv")
    start = time.monotonic()
    done = []
    global STRAGGLERS
    ex = ThreadPoolExecutor(workers)
    with partial.open("w", newline="") as pf, (RAW / "probe.log").open("a") as log:
        w = csv.DictWriter(pf, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        futs = {ex.submit(probe, dict(r), bucket, robots): r for r in rows}
        left = set(futs)
        try:
            finished = list(as_completed(futs, timeout=timeout))
        except TimeoutError:
            finished = [f for f in futs if f.done()]
        left -= set(finished)
        for fut in left:
            STRAGGLERS += 1
            fut.cancel()
            done.append(futs[fut])
            log.write(f"timeout {futs[fut]['ror_id']} {futs[fut]['homepage']}\n")
        for fut in finished:
            try:
                row = fut.result()
            except Exception as exc:
                row = futs[fut]
                log.write(f"error {row['ror_id']} {type(exc).__name__}\n")
            done.append(row)
            w.writerow(row)
            pf.flush()
            n = len(row["directory_entry_urls"].split(";")) if row["directory_entry_urls"] else 0
            log.write(f"{time.monotonic() - start:.0f}s {len(done)}/{len(rows)} {row['ror_id']} "
                      f"{urlparse(row['homepage']).netloc} entries={n}\n")
            log.flush()
    order = {r["ror_id"]: i for i, r in enumerate(rows)}
    partial.unlink()
    return sorted(done, key=lambda r: order[r["ror_id"]])


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def build_rows(ror_records: list[dict], lists: dict[str, list[tuple[str, str]]],
               tiers: dict[str, tuple[str, int]]) -> tuple[list[dict], list[str]]:
    idx = RorIndex(ror_records)
    rows: dict[str, dict] = {}
    unresolved = []
    for cls in ("R1", "R2"):
        for name, title in lists.get(cls, []):
            rec = idx.resolve(name, title)
            hand = HAND_MAP.get(title) or HAND_MAP.get(name)
            if not rec and hand in idx.by_id:
                rec = idx.by_id[hand]
            if not rec:
                reason = "ambiguous ROR match" if idx.ambiguous(name, title) else "no ROR match"
                unresolved.append({"list": cls, "name": name, "wiki_title": title, "reason": reason})
                continue
            rid = ror_short(rec["id"])
            rows.setdefault(rid, {"rec": rec, "carnegie_class": cls})
    for rid in tiers:
        if rid not in rows and rid in idx.by_id:
            rows[rid] = {"rec": idx.by_id[rid], "carnegie_class": ""}
        elif rid not in idx.by_id:
            unresolved.append({"list": "advisor", "name": rid, "wiki_title": "", "reason": "ROR id not in dump"})
    out = []
    for rid, v in rows.items():
        rec = v["rec"]
        tier, n = tiers.get(rid, ("", 0))
        out.append({"name": ror_display(rec), "ror_id": rid, "country_code": ror_country(rec),
                    "domains": ";".join(ror_domains(rec)), "homepage": ror_homepage(rec),
                    "carnegie_class": v["carnegie_class"], "advisor_institution": "yes" if tier else "",
                    "tier": tier, "advisor_count": n,
                    "directory_entry_urls": "", "entry_kind": "none", "platform_guess": ""})
    out.sort(key=public_key)
    return out, unresolved


def public_key(row: dict) -> tuple[str, str]:
    return (row["carnegie_class"] or "R9", row["name"])


def split_public(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    public = sorted((r for r in rows if r["carnegie_class"] in ("R1", "R2")), key=public_key)
    extra = sorted((r for r in rows if r["carnegie_class"] not in ("R1", "R2")), key=lambda r: r["name"])
    return public, extra


def write_rows(path: Path, rows: list[dict], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the faculty-haul institution seed list.")
    ap.add_argument("--ror", type=Path, default=ROR_DUMP)
    ap.add_argument("--advisors", type=Path, default=COCKPIT / "advisors_ranked.csv")
    ap.add_argument("--roster", type=Path, default=COCKPIT / "advisors.csv")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--workers", type=int, default=40)
    args = ap.parse_args(argv)
    lists, source, prov = carnegie_lists()
    tiers = advisor_tiers(read_csv(args.advisors), read_csv(args.roster))
    rows, unresolved = build_rows(json.loads(args.ror.read_text()), lists, tiers)
    if args.no_probe and args.out.exists():
        prior = {r["ror_id"]: r for r in read_csv(args.out)}
        for r in rows:
            for k in ("directory_entry_urls", "entry_kind", "platform_guess"):
                r[k] = (prior.get(r["ror_id"]) or {}).get(k, r.get(k, ""))
    if not args.no_probe:
        bucket = TokenBucket(1.0)
        robots = RobotsCache(lambda u: http_get(u)[:2], bucket)
        rows = probe_all(rows, bucket, robots, args.workers, args.out)
    for r in rows:
        r["licence"] = LICENCES[source if r["carnegie_class"] else ""]
        r["source_revision"] = prov["source_revision"] if r["carnegie_class"] else ""
        r["source_retrieved"] = prov["source_retrieved"] if r["carnegie_class"] else ""
    public, extra = split_public(rows)
    write_rows(args.out, public, FIELDS)
    write_rows(PRIVATE_EXTRA, extra, FIELDS)
    tier_rank = {"A+": 0, "A": 1}
    ranked = sorted((r for r in rows if r["tier"]), key=lambda r: (tier_rank[r["tier"]], public_key(r)))
    write_rows(PRIVATE_COUNTS, ranked, ("ror_id", "tier", "advisor_count"))
    RAW.mkdir(parents=True, exist_ok=True)
    with (args.out.parent / "unresolved.csv").open("w", newline="") as f:
        uw = csv.DictWriter(f, fieldnames=("list", "name", "wiki_title", "reason"))
        uw.writeheader()
        uw.writerows(unresolved)
    summary = {"source": source, "r1_listed": len(lists["R1"]), "r2_listed": len(lists["R2"]),
               "r1": sum(r["carnegie_class"] == "R1" for r in rows), "r2": sum(r["carnegie_class"] == "R2" for r in rows),
               "advisor_institutions": sum(1 for r in rows if r["tier"]),
               "advisor_only": sum(1 for r in rows if r["tier"] and not r["carnegie_class"]),
               "unresolved": len(unresolved), "rows": len(rows),
               "with_entry_urls": sum(1 for r in rows if r["directory_entry_urls"]),
               "directory": sum(1 for r in rows if r.get("entry_kind") == "directory"),
               "sitemap_only": sum(1 for r in rows if r.get("entry_kind") == "sitemap_only")}
    for line in unresolved:
        print("unresolved", line, file=sys.stderr)
    summary["probe_timeouts"] = STRAGGLERS
    print(json.dumps(summary, indent=1))
    sys.stdout.flush()
    if STRAGGLERS:
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
