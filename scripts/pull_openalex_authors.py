from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from atlas.connectors.base import REPO_ROOT  # noqa: E402
from atlas.schema import now_iso  # noqa: E402
from atlas.users.contacts import MAILTO  # noqa: E402

API = "https://api.openalex.org/authors"
SNAPSHOT_BASE = "https://openalex.s3.amazonaws.com/"
SNAPSHOT_MANIFEST = SNAPSHOT_BASE + "data/parquet/manifest.json"
OUT_DIR = REPO_ROOT / "data" / "processed" / "openalex_authors_wide"
MIN_WORKS = 2
PER_PAGE = 200
MAX_RPS = 10.0
BUDGET_BYTES = 10 * 1024 ** 3
SELECT = ("id,display_name,display_name_alternatives,orcid,works_count,cited_by_count,summary_stats,"
          "last_known_institutions,affiliations,topics,updated_date")

INST = pa.struct([("ror", pa.string()), ("display_name", pa.string()),
                  ("country_code", pa.string()), ("type", pa.string())])
AFF = pa.struct([("ror", pa.string()), ("years", pa.list_(pa.int32()))])
TOPIC = pa.struct([("id", pa.string()), ("display_name", pa.string()), ("field", pa.string())])
SCHEMA = pa.schema([
    ("id", pa.string()),
    ("display_name", pa.string()),
    ("display_name_alternatives", pa.list_(pa.string())),
    ("orcid", pa.string()),
    ("works_count", pa.int64()),
    ("cited_by_count", pa.int64()),
    ("h_index", pa.int64()),
    ("i10_index", pa.int64()),
    ("last_known_institutions", pa.list_(INST)),
    ("affiliations", pa.list_(AFF)),
    ("topics", pa.list_(TOPIC)),
    ("updated_date", pa.string()),
    ("pulled_at", pa.string()),
])


def tail(value: str | None, marker: str) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    i = s.rfind(marker)
    return s[i + len(marker):] if i >= 0 else s


def normalize(rec: dict, pulled_at: str) -> dict:
    stats = rec.get("summary_stats") or {}
    topics = sorted(rec.get("topics") or [], key=lambda t: (t.get("count") or 0, t.get("id") or ""),
                    reverse=True)[:5]
    return {
        "id": tail(rec.get("id"), "openalex.org/"),
        "display_name": rec.get("display_name"),
        "display_name_alternatives": list(rec.get("display_name_alternatives") or []),
        "orcid": tail(rec.get("orcid"), "orcid.org/"),
        "works_count": rec.get("works_count"),
        "cited_by_count": rec.get("cited_by_count"),
        "h_index": stats.get("h_index"),
        "i10_index": stats.get("i10_index"),
        "last_known_institutions": [
            {"ror": i.get("ror"), "display_name": i.get("display_name"),
             "country_code": i.get("country_code"), "type": i.get("type")}
            for i in rec.get("last_known_institutions") or []],
        "affiliations": [
            {"ror": (a.get("institution") or {}).get("ror"), "years": sorted(a.get("years") or [])}
            for a in rec.get("affiliations") or []],
        "topics": [
            {"id": tail(t.get("id"), "openalex.org/"), "display_name": t.get("display_name"),
             "field": (t.get("field") or {}).get("display_name")}
            for t in topics],
        "updated_date": str(rec["updated_date"])[:10] if rec.get("updated_date") else None,
        "pulled_at": pulled_at,
    }


def countries_of(rec: dict) -> set[str]:
    return {i.get("country_code") for i in rec.get("last_known_institutions") or [] if i.get("country_code")}


def dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {
            "status": "new", "countries": [], "partitions": {}}

    def part(self, cc: str) -> dict:
        return self.data["partitions"].setdefault(
            cc, {"status": "pending", "cursor": "*", "next_part": 0, "rows": 0, "files_done": []})

    def save(self, **fields) -> None:
        self.data.update(fields)
        self.data["updated_at"] = now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)


class Http:
    def __init__(self, max_rps: float = MAX_RPS, api_key: str | None = None,
                 opener: Callable[[str], dict] | None = None):
        self.gap = 1.0 / max_rps
        self.last = 0.0
        self.api_key = api_key
        self.opener = opener or self._open

    @staticmethod
    def _open(url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "research-atlas-openalex-wide/0.1"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)

    def get(self, params: dict) -> dict:
        wait = self.last + self.gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        q = {**params, "mailto": MAILTO}
        if self.api_key:
            q["api_key"] = self.api_key
        return self.opener(API + "?" + urllib.parse.urlencode(q))


class RateLimited(Exception):
    pass


def fetch_countries(http: Http) -> list[list]:
    out: dict[str, int] = {}
    cursor = "*"
    while cursor:
        d = http.get({"filter": f"works_count:>{MIN_WORKS}", "group_by": "last_known_institutions.country_code",
                      "per_page": PER_PAGE, "cursor": cursor})
        for g in d.get("group_by") or []:
            cc = tail(g.get("key"), "/countries/")
            if cc and len(cc) == 2:
                out[cc.upper()] = g.get("count") or 0
        cursor = (d.get("meta") or {}).get("next_cursor") if d.get("group_by") else None
    ordered = sorted(out.items(), key=lambda kv: (kv[0] != "US", -kv[1], kv[0]))
    return [list(kv) for kv in ordered]


def write_part(out: Path, cc: str, n: int | str, rows: list[dict] | pa.Table) -> Path:
    d = out / f"country={cc}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"part-{n}.parquet"
    tmp = path.with_suffix(".tmp")
    tbl = rows if isinstance(rows, pa.Table) else pa.Table.from_pylist(rows, schema=SCHEMA)
    pq.write_table(tbl, tmp, compression="zstd")
    tmp.replace(path)
    return path


def pull_api(state: State, out: Path, cc: str, http: Http, budget: int,
             flush_pages: int = 25, deadline: float | None = None) -> str:
    p = state.part(cc)
    if p["status"] == "done":
        return "done"
    p["status"] = "running"
    cursor, buf, pages = p["cursor"], [], 0
    while cursor:
        if deadline and time.monotonic() > deadline:
            break
        if dir_bytes(out) >= budget:
            state.save(status="budget_exhausted")
            return "budget_exhausted"
        try:
            d = http.get({"filter": f"works_count:>{MIN_WORKS},last_known_institutions.country_code:{cc}",
                          "per_page": PER_PAGE, "cursor": cursor, "select": SELECT})
        except urllib.error.HTTPError as e:
            if e.code in (402, 403, 429):
                state.save(status="rate_limited", rate_limited_at=now_iso())
                return "rate_limited"
            raise
        stamp = now_iso()
        buf.extend(normalize(r, stamp) for r in d.get("results") or [])
        pages += 1
        cursor = (d.get("meta") or {}).get("next_cursor")
        if cursor and not d.get("results"):
            cursor = None
        if pages >= flush_pages or not cursor:
            if buf:
                write_part(out, cc, p["next_part"], buf)
                p["next_part"] += 1
                p["rows"] += len(buf)
            p["cursor"] = cursor
            buf, pages = [], 0
            state.save(bytes=dir_bytes(out))
    if not cursor:
        p["status"] = "done"
        p["cursor"] = None
    state.save(bytes=dir_bytes(out))
    return p["status"]


def snapshot_files(opener: Callable[[str], dict] | None = None) -> list[str]:
    m = (opener or Http._open)(SNAPSHOT_MANIFEST)
    authors = next(e for e in m["entities"] if e["entity"] == "authors")
    return [f["url"].replace("s3://openalex/", SNAPSHOT_BASE) for f in authors["files"]]


NORMALIZE_SQL = f"""
    SELECT regexp_replace(id, '^.*/', '') AS id,
           display_name,
           coalesce(display_name_alternatives, []) AS display_name_alternatives,
           nullif(regexp_replace(orcid, '^.*/', ''), '') AS orcid,
           works_count::BIGINT AS works_count,
           cited_by_count::BIGINT AS cited_by_count,
           summary_stats.h_index::BIGINT AS h_index,
           summary_stats.i10_index::BIGINT AS i10_index,
           list_transform(coalesce(last_known_institutions, []), i -> {{'ror': i.ror, 'display_name': i.display_name,
               'country_code': i.country_code, 'type': i."type"}}) AS last_known_institutions,
           list_transform(coalesce(affiliations, []), a -> {{'ror': a.institution.ror,
               'years': list_sort(a."years")::INTEGER[]}}) AS affiliations,
           list_transform(list_slice(list_reverse_sort(list_transform(coalesce(topics, []),
               t -> {{'count': t.count, 'id': t.id, 'display_name': t.display_name, 'field': t.field.display_name}})), 1, 5),
               t -> {{'id': regexp_replace(t.id, '^.*/', ''), 'display_name': t.display_name, 'field': t.field}}) AS topics,
           left(CAST(updated_date AS VARCHAR), 10) AS updated_date,
           ? AS pulled_at
    FROM read_parquet(?)
    WHERE works_count > {MIN_WORKS}
      AND list_has_any(list_transform(last_known_institutions, x -> x.country_code), ?)
"""


def read_snapshot_file(url: str, countries: list[str], pulled_at: str) -> pa.Table:
    import duckdb
    con = duckdb.connect()
    if url.startswith("http"):
        con.execute("INSTALL httpfs; LOAD httpfs;")
    tbl = con.execute(NORMALIZE_SQL, [pulled_at, url, countries]).to_arrow_table()
    con.close()
    return tbl.cast(SCHEMA)


def split_by_country(tbl: pa.Table, countries: list[str]) -> dict[str, pa.Table]:
    import duckdb
    con = duckdb.connect()
    con.register("t", tbl)
    out = {cc: con.execute("SELECT * FROM t WHERE list_contains(list_transform(last_known_institutions, "
                           "x -> x.country_code), ?)", [cc]).to_arrow_table().cast(SCHEMA)
           for cc in countries}
    con.close()
    return out


def pull_snapshot(state: State, out: Path, targets: list[str], files: list[str], budget: int,
                  reader: Callable[[str, list[str], str], pa.Table] = read_snapshot_file,
                  deadline: float | None = None, workers: int = 4) -> str:
    from concurrent.futures import ThreadPoolExecutor
    todo = [cc for cc in targets if state.part(cc)["status"] != "done"]
    for cc in todo:
        state.part(cc)["status"] = "running"
    jobs = [(i, url, [cc for cc in todo if i not in state.part(cc)["files_done"]]) for i, url in enumerate(files)]
    jobs = [j for j in jobs if j[2]]
    result = "done"
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(jobs), workers):
            if deadline and time.monotonic() > deadline:
                result = "running"
                break
            if dir_bytes(out) >= budget:
                result = "budget_exhausted"
                break
            batch = jobs[start:start + workers]
            stamp = now_iso()
            tables = list(pool.map(lambda j: reader(j[1], j[2], stamp), batch))
            for (i, _, need), tbl in zip(batch, tables):
                for cc, part in split_by_country(tbl, need).items():
                    p = state.part(cc)
                    if part.num_rows:
                        write_part(out, cc, i, part)
                        p["rows"] += part.num_rows
                    p["files_done"].append(i)
                    p["next_part"] = max(p["next_part"], i + 1)
            state.save(bytes=dir_bytes(out), snapshot_files=len(files))
    for cc in todo:
        p = state.part(cc)
        if len(set(p["files_done"])) >= len(files):
            p["status"] = "done"
    state.save(bytes=dir_bytes(out), **({"status": result} if result != "done" else {}))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pull OpenAlex authors by last known institution country.")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--source", choices=["snapshot", "api"], default="snapshot")
    ap.add_argument("--countries", default="US", help="comma list, or 'all' for every partition in order")
    ap.add_argument("--budget-gb", type=float, default=BUDGET_BYTES / 1024 ** 3)
    ap.add_argument("--max-minutes", type=float)
    ap.add_argument("--max-rps", type=float, default=MAX_RPS)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    budget = int(args.budget_gb * 1024 ** 3)
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None
    state = State(args.out / "_state.json")
    http = Http(args.max_rps, os.environ.get("OPENALEX_API_KEY"))
    if not state.data["countries"]:
        state.save(countries=fetch_countries(http), source=args.source)
    order = [cc for cc, _ in state.data["countries"]]
    targets = order if args.countries == "all" else [c.strip().upper() for c in args.countries.split(",")]
    state.save(status="running", source=args.source)
    if args.source == "snapshot":
        result = pull_snapshot(state, args.out, targets, snapshot_files(), budget, deadline=deadline,
                               workers=args.workers)
    else:
        result = "done"
        for cc in targets:
            result = pull_api(state, args.out, cc, http, budget, deadline=deadline)
            if result != "done":
                break
    parts = state.data["partitions"]
    all_done = all(parts.get(cc, {}).get("status") == "done" for cc in order)
    final = result if result in ("budget_exhausted", "rate_limited") else (
        "complete" if all_done else ("partial" if all(parts.get(c, {}).get("status") == "done"
                                                      for c in targets) else "running"))
    state.save(status=final)
    done = {cc: parts[cc]["rows"] for cc in targets if cc in parts}
    print(json.dumps({"status": final, "rows": done, "bytes": dir_bytes(args.out)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
