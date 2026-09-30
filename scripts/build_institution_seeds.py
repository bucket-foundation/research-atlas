from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import REPO_ROOT  # noqa: E402
from atlas.users.contacts import UA  # noqa: E402
from atlas.users.haul import (  # noqa: E402
    RobotsCache, RorIndex, TokenBucket, advisor_tiers, parse_wiki_carnegie, ror_country, ror_display,
    ror_domains, ror_homepage, ror_short,
)

ROR_DUMP = REPO_ROOT / "data" / "raw" / "ror" / "v2.8-2026-06-02-ror-data.json"
RAW = REPO_ROOT / "data" / "raw" / "seeds"
OUT = REPO_ROOT / "data" / "seeds" / "institutions.csv"
COCKPIT = REPO_ROOT.parent / "biophysics-phd-review" / "data" / "processed"
WIKI = "https://en.wikipedia.org/w/index.php?title=List_of_research_universities_in_the_United_States&action=raw"
CARNEGIE = "https://carnegieclassifications.acenet.edu/"
PROBE_PATHS = ("/directory", "/people", "/faculty", "/faculty-directory", "/experts", "/sitemap.xml")
FIELDS = ("name", "ror_id", "country_code", "domains", "homepage", "carnegie_class", "tier", "advisor_count",
          "directory_entry_urls", "platform_guess", "licence")
LICENCES = {"wikipedia": "Carnegie list via Wikipedia CC-BY-SA-4.0; ROR CC0-1.0",
            "carnegie": "Carnegie Classification terms; ROR CC0-1.0", "": "ROR CC0-1.0"}


def http_get(url: str, limit: int = 600_000) -> tuple[int, str, str]:
    import requests

    with requests.get(url, headers={"User-Agent": UA}, timeout=(5, 8), stream=True, allow_redirects=True) as r:
        body = r.raw.read(limit, decode_content=True) if r.status_code == 200 else b""
        return r.status_code, body.decode(r.encoding or "utf-8", "replace"), r.url


def carnegie_lists() -> tuple[dict[str, list[tuple[str, str]]], str]:
    RAW.mkdir(parents=True, exist_ok=True)
    cached = RAW / "wikipedia_research_universities.txt"
    if not cached.exists():
        status, text, _ = http_get(WIKI, limit=5_000_000)
        if status != 200:
            raise SystemExit(f"wikipedia list HTTP {status}")
        cached.write_text(text)
    return parse_wiki_carnegie(cached.read_text()), "wikipedia"


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


def probe(row: dict, bucket: TokenBucket, robots: RobotsCache) -> dict:
    home = row["homepage"]
    if not home:
        return row
    p = urlparse(home)
    base = f"{p.scheme or 'https'}://{p.netloc}"
    doms = row["domains"].split(";") if row["domains"] else []
    keep, body = [], ""
    for path in ("/",) + PROBE_PATHS:
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
        if path == "/":
            body = text
        else:
            keep.append(final)
    row["directory_entry_urls"] = ";".join(dict.fromkeys(keep))
    row["platform_guess"] = detect(row["ror_id"], home, body)
    return row


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
            if not rec:
                unresolved.append(f"{cls}\t{name}\t{title}")
                continue
            rid = ror_short(rec["id"])
            rows.setdefault(rid, {"rec": rec, "carnegie_class": cls})
    for rid in tiers:
        if rid not in rows and rid in idx.by_id:
            rows[rid] = {"rec": idx.by_id[rid], "carnegie_class": ""}
        elif rid not in idx.by_id:
            unresolved.append(f"advisor\t{rid}\tnot in ROR dump")
    out = []
    for rid, v in rows.items():
        rec = v["rec"]
        tier, n = tiers.get(rid, ("", 0))
        out.append({"name": ror_display(rec), "ror_id": rid, "country_code": ror_country(rec),
                    "domains": ";".join(ror_domains(rec)), "homepage": ror_homepage(rec),
                    "carnegie_class": v["carnegie_class"], "tier": tier, "advisor_count": n,
                    "directory_entry_urls": "", "platform_guess": ""})
    order = {"A+": 0, "A": 1, "": 2}
    out.sort(key=lambda r: (order[r["tier"]], r["carnegie_class"] or "R9", r["name"]))
    return out, unresolved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the faculty-haul institution seed list.")
    ap.add_argument("--ror", type=Path, default=ROR_DUMP)
    ap.add_argument("--advisors", type=Path, default=COCKPIT / "advisors_ranked.csv")
    ap.add_argument("--roster", type=Path, default=COCKPIT / "advisors.csv")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--workers", type=int, default=40)
    args = ap.parse_args(argv)
    lists, source = carnegie_lists()
    tiers = advisor_tiers(read_csv(args.advisors), read_csv(args.roster))
    rows, unresolved = build_rows(json.loads(args.ror.read_text()), lists, tiers)
    if not args.no_probe:
        bucket = TokenBucket(1.0)
        robots = RobotsCache(lambda u: http_get(u)[:2], bucket)
        with ThreadPoolExecutor(args.workers) as ex:
            rows = list(ex.map(lambda r: probe(r, bucket, robots), rows))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            r["licence"] = LICENCES[source if r["carnegie_class"] else ""]
            w.writerow(r)
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / "unresolved.tsv").write_text("\n".join(unresolved) + "\n")
    summary = {"source": source, "r1_listed": len(lists["R1"]), "r2_listed": len(lists["R2"]),
               "r1": sum(r["carnegie_class"] == "R1" for r in rows), "r2": sum(r["carnegie_class"] == "R2" for r in rows),
               "advisor_institutions": sum(1 for r in rows if r["tier"]),
               "advisor_only": sum(1 for r in rows if r["tier"] and not r["carnegie_class"]),
               "unresolved": len(unresolved), "rows": len(rows),
               "with_entry_urls": sum(1 for r in rows if r["directory_entry_urls"])}
    for line in unresolved:
        print("unresolved", line, file=sys.stderr)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
