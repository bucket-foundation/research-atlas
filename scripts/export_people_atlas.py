#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from atlas.users.directories.optout import Suppression, norm_name, norm_orcid

DATA = REPO / "data"
WIDE = Path("/home/gian/agfarms/.wt-atlas-openalex-wide/data/processed/openalex_authors_wide")
OFFICIAL = DATA / "raw" / "contacts" / "official"
PROCESSED = DATA / "processed"
WORKS_CACHE = DATA / "raw" / "openalex" / "works_top5"
TOPICS_CACHE = DATA / "raw" / "openalex" / "topics.jsonl"
SEEDS = "data/seeds/institutions.csv"
OUT = Path.home() / ".local" / "share" / "bucket-advisor-review" / "people-atlas.jsonl"
API = "https://api.openalex.org"
SLICES = ("r1-cs-hci-education", "all-r1")
SLICE_FIELDS = {"Computer Science"}
SLICE_SUBFIELDS = {"Human-Computer Interaction", "Education"}
SLICE_TERMS = ("learning sciences", "educational technology", "learning analytics")
RECENT_YEARS = 5


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bare_ror(value: str | None) -> str:
    return (value or "").strip().rstrip("/").rsplit("/", 1)[-1].lower()


def bare_id(value: str | None) -> str:
    return (value or "").strip().rstrip("/").rsplit("/", 1)[-1]


def read_seeds(path: Path | None = None) -> str:
    p = path or REPO / SEEDS
    if p.exists():
        return p.read_text()
    return subprocess.run(["git", "-C", str(REPO), "show", f"HEAD:{SEEDS}"],
                          check=True, capture_output=True, text=True).stdout


def r1_rors(text: str) -> dict[str, str]:
    return {bare_ror(r["ror_id"]): r["name"] for r in csv.DictReader(io.StringIO(text))
            if r.get("carnegie_class") == "R1" and r.get("ror_id")}


def mailto() -> str:
    from atlas.users.contacts import MAILTO
    return MAILTO


class Limiter:
    def __init__(self, per_second: float):
        self.gap = 1.0 / per_second
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            t = time.monotonic()
            if t < self.next:
                time.sleep(self.next - t)
            self.next = max(t, self.next) + self.gap


def get_json(session, url: str, params: dict, limiter: Limiter, tries: int = 4) -> dict | None:
    for attempt in range(tries):
        limiter.wait()
        try:
            r = session.get(url, params=params, timeout=(10, 30))
        except Exception:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        time.sleep(min(float(r.headers.get("Retry-After") or 2 ** attempt), 60))
    return None


def load_topics(session, limiter: Limiter, cache: Path = TOPICS_CACHE) -> dict[str, dict]:
    if not cache.exists():
        rows, cursor = [], "*"
        while cursor:
            page = get_json(session, f"{API}/topics", {"per_page": 200, "cursor": cursor, "mailto": mailto(),
                                                        "select": "id,display_name,subfield,field"}, limiter)
            if page is None:
                raise RuntimeError("topics fetch failed")
            rows += [{"id": bare_id(t["id"]), "name": t["display_name"],
                      "subfield": (t.get("subfield") or {}).get("display_name"),
                      "field": (t.get("field") or {}).get("display_name")} for t in page["results"]]
            cursor = page["meta"].get("next_cursor") if page["results"] else None
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.name + ".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        os.replace(tmp, cache)
    return {r["id"]: r for r in map(json.loads, cache.read_text().splitlines())}


def topic_in_slice(topic: dict, index: dict[str, dict]) -> bool:
    meta = index.get(bare_id(topic.get("id")), {})
    name = (topic.get("display_name") or meta.get("name") or "").lower()
    field = topic.get("field") or meta.get("field")
    return (field in SLICE_FIELDS or meta.get("subfield") in SLICE_SUBFIELDS
            or any(t in name for t in SLICE_TERMS))


def slice_topic_ids(index: dict[str, dict]) -> list[str]:
    return sorted(tid for tid, meta in index.items() if topic_in_slice({"id": tid}, index))


def select_authors(wide: Path, rors: set[str], topic_ids: list[str] | None) -> list[dict]:
    import duckdb

    con = duckdb.connect()
    want = [f"https://ror.org/{r}" for r in sorted(rors)]
    params: list = [want]
    topic_sql = ""
    if topic_ids is not None:
        terms = " or ".join(f"lower(t.display_name) like '%{t}%'" for t in SLICE_TERMS)
        topic_sql = f"""and (list_has_any(list_transform(topics, t -> regexp_extract(t.id, '[^/]+$')), ?::varchar[])
                        or len(list_filter(topics, t -> t.field in ({", ".join(f"'{f}'" for f in SLICE_FIELDS)})
                                                    or {terms})) > 0)"""
        params.append(topic_ids)
    q = f"""
        select * from read_parquet('{wide}/**/*.parquet', hive_partitioning=false)
        where id is not null
          and list_has_any(list_transform(last_known_institutions, x -> x.ror), ?::varchar[])
          {topic_sql}
    """
    return con.execute(q, params).to_arrow_table().to_pylist()


def load_official(root: Path, rors: set[str]) -> tuple[dict, dict]:
    by_orcid, by_name = {}, {}
    for ror in sorted(rors):
        d = root / ror
        for fname in ("extracted.jsonl", "records.jsonl"):
            path = d / fname
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                rec = json.loads(line)
                if not rec.get("name"):
                    continue
                rec["ror_id"] = bare_ror(rec.get("ror_id") or ror)
                key = (norm_name(rec["name"]), rec["ror_id"])
                merged = merge_official(by_name.get(key), rec)
                by_name[key] = merged
                if (o := norm_orcid(rec.get("orcid"))):
                    by_orcid[o] = merged
    return by_orcid, by_name


def merge_official(old: dict | None, new: dict) -> dict:
    if old is None:
        out = dict(new)
        out["departments"] = list(new.get("departments") or [])
        return out
    for k, v in new.items():
        if k == "departments":
            old["departments"] += [x for x in v or [] if x not in old["departments"]]
        elif v not in (None, "", []) and old.get(k) in (None, "", []):
            old[k] = v
    return old


def official_for(author: dict, ror: str, by_orcid: dict, by_name: dict) -> dict | None:
    if (o := norm_orcid(author.get("orcid"))) and o in by_orcid:
        return by_orcid[o]
    names = [author.get("display_name")] + list(author.get("display_name_alternatives") or [])
    for n in names:
        if (hit := by_name.get((norm_name(n), ror))):
            return hit
    return None


def load_funding(processed: Path, openalex_ids: list[str], orcids: list[str]) -> dict[str, dict]:
    import duckdb

    if not (processed / "grant_pi_person.parquet").exists():
        return {}
    con = duckdb.connect()
    con.execute("create temp table want(openalex varchar, orcid varchar)")
    con.executemany("insert into want values (?, ?)", list(zip(openalex_ids, orcids)))
    rows = con.execute(f"""
        with pp as (
            select atlas_id, regexp_extract(coalesce(openalex_author_id, ''), '[^/]+$') as oa
            from read_parquet('{processed}/person.parquet')),
        link as (
            select w.openalex, g.grant_id, g.role, g.person_atlas_id, 'orcid' as how
            from want w join read_parquet('{processed}/grant_pi_person.parquet') g
              on w.orcid <> '' and upper(regexp_extract(g.orcid, '[^/]+$')) = w.orcid
            union
            select w.openalex, g.grant_id, g.role, g.person_atlas_id, 'openalex_author_id:' || g.match_method
            from want w join pp on pp.oa = w.openalex
            join read_parquet('{processed}/grant_pi_person.parquet') g on g.person_atlas_id = pp.atlas_id),
        best as (
            select openalex, grant_id, any_value(role) as role, min(person_atlas_id) as atlas_id, min(how) as how
            from link group by 1, 2)
        select b.openalex, b.atlas_id, b.how, b.role, g.title, g.amount_usd, g.start_date, g.end_date,
               g.source, g.source_url
        from best b join read_parquet('{processed}/grant.parquet') g on g.atlas_id = b.grant_id
    """).fetchall()
    out: dict[str, dict] = {}
    today = datetime.now(timezone.utc).date().isoformat()
    cutoff = str(int(today[:4]) - RECENT_YEARS)
    for oa, atlas_id, how, role, title, usd, start, end, src, url in rows:
        f = out.setdefault(oa, {"active": False, "atlas_person_id": atlas_id, "match": how, "n_grants": 0,
                                "n_recent": 0, "recent_grants": [], "recent_usd": 0, "total_usd": 0})
        f["n_grants"] += 1
        f["total_usd"] += int(usd or 0)
        if end and end >= today:
            f["active"] = True
        if (end or start or "") >= cutoff:
            f["n_recent"] += 1
            f["recent_usd"] += int(usd or 0)
            f["recent_grants"].append({"amount_usd": usd, "end": end, "funder_source": src, "role": role,
                                       "start": start, "title": title,
                                       "url": url if (url or "").startswith("https://") else None})
    for f in out.values():
        f["recent_grants"].sort(key=lambda g: g["start"] or "", reverse=True)
    return out


def works_path(cache: Path, author_id: str) -> Path:
    return cache / author_id[-2:] / f"{author_id}.json"


def fetch_works(ids: list[str], cache: Path, rate: float, budget_s: float, session=None) -> dict:
    import requests
    from concurrent.futures import ThreadPoolExecutor

    todo = [a for a in ids if not works_path(cache, a).exists()]
    stats = {"cached": len(ids) - len(todo), "fetched": 0, "failed": 0, "deferred": 0}
    if not todo:
        return stats
    session = session or requests.Session()
    session.headers["User-Agent"] = f"research-atlas people-atlas export (mailto:{mailto()})"
    limiter = Limiter(rate)
    deadline = time.monotonic() + budget_s

    def one(aid: str) -> str:
        if time.monotonic() > deadline:
            return "deferred"
        page = get_json(session, f"{API}/works", {
            "filter": f"author.id:{aid}", "sort": "cited_by_count:desc", "per_page": 5, "mailto": mailto(),
            "select": "id,display_name,publication_year,cited_by_count"}, limiter)
        if page is None:
            return "failed"
        works = [{"id": bare_id(w["id"]), "title": w.get("display_name"), "year": w.get("publication_year"),
                  "cited_by_count": w.get("cited_by_count")} for w in page.get("results", [])]
        p = works_path(cache, aid)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"as_of": now_iso(), "works": works}))
        os.replace(tmp, p)
        return "fetched"

    with ThreadPoolExecutor(max_workers=max(1, int(rate))) as pool:
        for result in pool.map(one, todo):
            stats[result] += 1
    return stats


def cached_works(cache: Path, author_id: str) -> list | None:
    p = works_path(cache, author_id)
    return json.loads(p.read_text())["works"] if p.exists() else None


def https_or_none(url: str | None) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


def build_row(a: dict, ror: str, inst: str, official: dict | None, funding: dict | None,
              works: list | None, supp: Suppression, as_of: str) -> dict | None:
    aid = bare_id(a["id"])
    orcid = norm_orcid(a.get("orcid")) or None
    tier = "T0" if orcid else "T1"
    off = official or {}
    email = off.get("email") if off.get("email") and (off.get("email_source") or off.get("source")) == "official_directory" else None
    profile = https_or_none(off.get("profile_url"))
    if supp.blocks(a.get("display_name"), ror, orcid, email) or supp.blocks_url(profile):
        return None
    if off and supp.blocks(off.get("name"), ror, orcid, off.get("email")):
        return None
    sources = [{"source": "openalex", "source_id": aid, "source_url": f"{API}/authors/{aid}",
                "as_of": a.get("as_of") or as_of, "match_tier": tier, "licence": "CC0-1.0"}]
    if works is not None:
        sources.append({"source": "openalex_works", "source_id": aid,
                        "source_url": f"{API}/works?filter=author.id:{aid}&sort=cited_by_count:desc&per_page=5",
                        "as_of": as_of, "match_tier": tier, "licence": "CC0-1.0"})
    if off:
        sources.append({"source": "official_directory", "source_id": off.get("source_id") or off.get("profile_url"),
                        "source_url": https_or_none(off.get("source_url") or off.get("profile_url")),
                        "as_of": off.get("as_of"), "match_tier": "T0" if norm_orcid(off.get("orcid")) else tier,
                        "licence": off.get("licence") or "institution-copyright"})
    if funding:
        sources.append({"source": "atlas_funding", "source_id": funding["atlas_person_id"],
                        "source_url": "https://github.com/bucket-foundation/research-atlas",
                        "as_of": as_of, "match_tier": "T0" if funding["match"] == "orcid" else "T1",
                        "licence": "per-funder, see recent_grants url"})
    depts = off.get("departments") or []
    areas = off.get("research_areas")
    if isinstance(areas, list):
        areas = "; ".join(map(str, areas)) or None
    return {
        "id": aid, "openalex_id": aid, "name": a.get("display_name"), "names_seen": a.get("display_name_alternatives") or [],
        "institution": inst, "country": "US", "ror": ror, "orcid": orcid,
        "title": off.get("title"), "department": "; ".join(depts) or None, "research_areas_official": areas,
        "author_topics": [{"name": t.get("display_name"), "field": t.get("field"), "count": t.get("count")}
                          for t in a.get("topics") or []],
        "h_index": a.get("h_index"), "cited_by_count": a.get("cited_by_count"), "works_count": a.get("works_count"),
        "works": works or [], "works_status": "ok" if works is not None else "pending",
        "funding": funding, "sources": sources, "profile_url": profile,
        "email": email, "email_source": "official_directory" if email else None,
        "email_source_url": https_or_none(off.get("email_source_url") or off.get("source_url")) if email else None,
        "email_as_of": (off.get("email_as_of") or off.get("as_of")) if email else None,
    }


def atomic_write(out: Path, rows: list[dict]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    with tmp.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    if out.exists():
        prev = out.with_name(out.name.replace(".jsonl", ".prev.jsonl"))
        ptmp = prev.with_name(f".{prev.name}.{os.getpid()}.tmp")
        shutil.copy2(out, ptmp)
        os.replace(ptmp, prev)
    os.replace(tmp, out)


def fill_counts(rows: list[dict]) -> dict[str, int]:
    c = collections.Counter()
    for r in rows:
        for k, v in r.items():
            if v not in (None, "", [], {}):
                c[k] += 1
    return dict(sorted(c.items()))


def run(args) -> dict:
    import requests

    as_of = now_iso()
    session = requests.Session()
    session.headers["User-Agent"] = f"research-atlas people-atlas export (mailto:{mailto()})"
    rors = r1_rors(read_seeds(args.seeds))
    topic_ids = None
    if args.slice == "r1-cs-hci-education":
        topic_ids = slice_topic_ids(load_topics(session, Limiter(args.rate), args.topics_cache))
    authors = select_authors(args.wide, set(rors), topic_ids)
    authors = [a for a in authors if bare_id(a.get("id")).startswith("A")]
    authors.sort(key=lambda a: -(a.get("cited_by_count") or 0))
    if args.limit:
        authors = authors[: args.limit]
    ids = [bare_id(a["id"]) for a in authors]
    work_stats = {"skipped": len(ids)} if args.no_fetch else fetch_works(ids, args.works_cache, args.rate,
                                                                         args.works_budget, session)
    by_orcid, by_name = load_official(args.official, set(rors))
    funding = load_funding(args.processed, ids, [norm_orcid(a.get("orcid")) for a in authors])
    supp = Suppression.load(args.private / "opt_out.csv", args.private / "tombstones.csv") if args.private else Suppression.load()
    rows, suppressed = [], 0
    for a in authors:
        ror = next(bare_ror(li["ror"]) for li in a["last_known_institutions"] if bare_ror(li.get("ror")) in rors)
        row = build_row(a, ror, rors[ror], official_for(a, ror, by_orcid, by_name),
                        funding.get(bare_id(a["id"])), cached_works(args.works_cache, bare_id(a["id"])), supp, as_of)
        if row is None:
            suppressed += 1
        else:
            rows.append(row)
    atomic_write(args.out, rows)
    by_field = collections.Counter((r["author_topics"][0]["field"] if r["author_topics"] else None) for r in rows)
    return {"rows": len(rows), "suppressed": suppressed, "works": work_stats, "fill": fill_counts(rows),
            "by_openalex_field": dict(by_field.most_common()), "out": str(args.out)}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--slice", choices=SLICES, default=SLICES[0])
    p.add_argument("--limit", type=int)
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--wide", type=Path, default=WIDE)
    p.add_argument("--official", type=Path, default=OFFICIAL)
    p.add_argument("--processed", type=Path, default=PROCESSED)
    p.add_argument("--works-cache", type=Path, default=WORKS_CACHE)
    p.add_argument("--topics-cache", type=Path, default=TOPICS_CACHE)
    p.add_argument("--seeds", type=Path)
    p.add_argument("--private", type=Path)
    p.add_argument("--rate", type=float, default=10.0)
    p.add_argument("--works-budget", type=float, default=90 * 60)
    p.add_argument("--no-fetch", action="store_true")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    args.rate = min(args.rate, 10.0)
    print(json.dumps(run(args), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
