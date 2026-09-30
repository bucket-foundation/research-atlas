from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.users.directories.base import OFFICIAL_CACHE  # noqa: E402
from atlas.users.extract.commoncrawl import CommonCrawl, needs_archive, same_host  # noqa: E402
from atlas.users.directories.optout import Suppression  # noqa: E402
from atlas.users.extract.local_llm import LocalExtractor, page_email_candidates, verified_email  # noqa: E402
from atlas.users.extract.structured import page_people, profile_invalid_reason  # noqa: E402
from atlas.users.extract.synthesize import Synthesizer, apply_selectors, load_selectors  # noqa: E402

LICENCE = "institution-copyright"
PROFILE_PATH = re.compile(r"/(profile|profiles|people|person|persons|faculty|directory|staff|experts|scientists|"
                          r"researchers|individual|display)/[^/?#]+/?$", re.I)
EVAL_FIELDS = ("name", "title", "email", "departments", "school")


def load_pages(ror_dir: Path) -> list[tuple[dict, str]]:
    out = []
    for meta_path in sorted((ror_dir / "pages").glob("*.json")):
        meta = json.loads(meta_path.read_text())
        stem = meta_path.with_suffix("")
        bodies = [p for p in (Path(f"{stem}.html.gz"), Path(f"{stem}.html")) if p.exists()]
        if not bodies or meta.get("status") != 200:
            continue
        body = max(bodies, key=lambda p: p.stat().st_mtime)
        raw = body.read_bytes()
        out.append((meta, (gzip.decompress(raw) if body.suffix == ".gz" else raw).decode("utf-8", "replace")))
    return out


def split_invalid(pages: list[tuple[dict, str]], doms: tuple[str, ...],
                  ror_dir: Path) -> tuple[list[tuple[dict, str]], dict[str, int]]:
    keep, reasons = [], defaultdict(int)
    for meta, html in pages:
        reason = meta.get("invalid") or (profile_invalid_reason(html, meta["url"], doms) if is_profile(meta["url"]) else None)
        if reason:
            reasons[reason] += 1
            if not meta.get("invalid"):
                mark_invalid(ror_dir, meta, reason)
        else:
            keep.append((meta, html))
    return keep, reasons


def mark_invalid(ror_dir: Path, meta: dict, reason: str) -> None:
    import hashlib

    path = ror_dir / "pages" / f"{hashlib.sha256(meta['url'].encode()).hexdigest()}.json"
    if path.exists():
        path.write_text(json.dumps({**meta, "invalid": reason}))


ROR_DOMAINS = Path(__file__).resolve().parents[1] / "data" / "processed" / "ror_domains.parquet"


class DomainLookupError(RuntimeError):
    pass


def domains_for(ror: str, table: Path | None = None) -> tuple[str, ...]:
    import pandas as pd

    path = table or ROR_DOMAINS
    if not path.exists():
        raise DomainLookupError(f"ror_domains table missing at {path}; run scripts/build_ror_domains.py")
    df = pd.read_parquet(path, columns=["ror_id", "domains"])
    hit = df[df["ror_id"] == ror.rsplit("/", 1)[-1].lower()]
    if hit.empty or len(hit.iloc[0]["domains"]) == 0:
        raise DomainLookupError(f"no domains for ROR {ror} in {path}")
    return tuple(hit.iloc[0]["domains"])


def is_profile(url: str) -> bool:
    return bool(PROFILE_PATH.search(urlparse(url).path))


RETRIEVED_BY = "extract_local/0.2"


def record(ror: str, meta: dict, person: dict, extractor: str, digest: str | None = None) -> dict:
    url = person.get("profile_url") or meta["url"]
    rec = {k: person.get(k) for k in ("name", "title", "departments", "school", "research_areas", "email", "orcid")}
    rec.update(profile_url=url, page_url=meta["url"], ror_id=ror, source=meta.get("source", "official_directory"),
               source_id=url, source_url=meta.get("source_url") or meta["url"],
               provenance={"crawl_id": (meta.get("provenance") or {}).get("crawl_id")},
               as_of=meta.get("fetched_at"), match_tier=None, licence=LICENCE, storage="link_only",
               retrieved_by=f"{RETRIEVED_BY}+{extractor}", extractor=extractor, extractor_digest=digest,
               html_sha256=meta.get("sha256"))
    rec["departments"] = rec["departments"] or []
    return rec


def is_miss(url: str, people: list[dict]) -> bool:
    return is_profile(url) and not any(p.get("name") and p.get("email") for p in people)


def selector_person(html: str, url: str, selectors: dict, doms: tuple[str, ...]) -> dict | None:
    got = apply_selectors(html, selectors)
    if not got.get("name"):
        return None
    got["email"] = verified_email(got.get("email"), html, doms)
    got["profile_url"] = url
    return got


def run(ror: str, root: Path, use_llm: bool, extractor: LocalExtractor | None = None,
        limit: int | None = None, domains: tuple[str, ...] | None = None,
        table: Path | None = None, suppression: Suppression | None = None) -> tuple[list[dict], dict]:
    ror_dir = root / ror
    sup = suppression if suppression is not None else Suppression.load()
    doms = domains if domains is not None else domains_for(ror, table)
    loaded = load_pages(ror_dir)[:limit]
    blocked_pages = [m for m, _ in loaded if sup.blocks_url(m["url"])]
    loaded = [(m, h) for m, h in loaded if not sup.blocks_url(m["url"])]
    pages, invalid = split_invalid(loaded, doms, ror_dir)
    suppressed = {"pages": len(blocked_pages), "records": 0, "llm_pages": 0}

    def allowed(person: dict) -> bool:
        if sup.blocks(person.get("name"), ror, person.get("orcid"), person.get("email")):
            suppressed["records"] += 1
            return False
        return True

    out, misses = [], []
    started = time.monotonic()
    for meta, html in pages:
        people = page_people(html, meta["url"], doms, profile=is_profile(meta["url"]))
        if people and not is_miss(meta["url"], people):
            out += [record(ror, meta, p, "structured") for p in people if allowed(p)]
        else:
            misses.append((meta, html, people))
    safe = []
    for meta, html, people in misses:
        emails = page_email_candidates(html)
        if any(not allowed(p) for p in people) or any(sup.blocks(email=e) for e in emails):
            suppressed["llm_pages"] += 1
            continue
        safe.append((meta, html, people))
    misses = safe
    selector_hits, still = 0, []
    for meta, html, people in misses:
        sel = load_selectors(root / "_selectors", urlparse(meta["url"]).netloc)
        got = selector_person(html, meta["url"], sel, doms) if sel else None
        if got and got.get("email") and allowed(got):
            out.append(record(ror, meta, got, "selectors"))
            selector_hits += 1
        else:
            still.append((meta, html, people))
    misses = still
    structured_s = time.monotonic() - started
    llm_s, failures = 0.0, []
    if use_llm and misses:
        ex = extractor or LocalExtractor()
        t0 = time.monotonic()
        results = ex.extract_many([(html, meta["url"]) for meta, html, _ in misses], doms)
        llm_s = time.monotonic() - t0
        failures = list(ex.failures)
        for (meta, _, people), res in zip(misses, results):
            if res and res.get("name"):
                if allowed(res):
                    out.append(record(ror, meta, res, res["extractor"], res.get("extractor_digest")))
            else:
                out += [record(ror, meta, p, "structured") for p in people if allowed(p)]
    else:
        for meta, _, people in misses:
            out += [record(ror, meta, p, "structured") for p in people if allowed(p)]
    stats = {"suppressed": suppressed, "invalid_pages": sum(invalid.values()), "invalid_reasons": dict(invalid), "pages": len(pages),
             "structured_hits": len(pages) - len(misses) - selector_hits, "selector_hits": selector_hits,
             "llm_pages": len(misses) if use_llm else 0, "llm_failures": len(failures), "llm_failure_urls": failures,
             "archived_pages": sum(1 for m, _ in pages if (m.get("provenance") or {}).get("crawl_id")),
             "records": len(out), "emails": sum(1 for r in out if r["email"]),
             "structured_pages_per_min": round(len(pages) / structured_s * 60, 1) if structured_s else None,
             "llm_pages_per_min": round(len(misses) / llm_s * 60, 1) if llm_s else None}
    return out, stats


def archive_host(ror: str, host: str, root: Path, cc: CommonCrawl, live_status: tuple[int, str],
                 limit: int = 400, suppression: Suppression | None = None) -> dict:
    sup = suppression if suppression is not None else Suppression.load()
    if not needs_archive(*live_status):
        return {"host": host, "archived": 0, "reason": "live host is reachable; fetch it live"}
    pages = root / ror / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    rows = cc.profile_urls(host, PROFILE_PATH, limit)[:limit]
    written = rejected = blocked = 0
    for row in rows:
        if sup.blocks_url(row["url"]):
            blocked += 1
            continue
        rec = cc.record(row)
        if rec is None or rec.status != 200 or not same_host(rec.url, f"https://{host}/"):
            rejected += 1
            continue
        key = hashlib.sha256(row["url"].encode()).hexdigest()
        (pages / f"{key}.html.gz").write_bytes(gzip.compress(rec.html.encode()))
        (pages / f"{key}.json").write_text(json.dumps({
            "url": row["url"], "status": 200, "fetched_at": rec.warc_date,
            "sha256": hashlib.sha256(rec.html.encode()).hexdigest(), "source": "official_directory",
            "source_url": rec.location, "provenance": {"crawl_id": rec.crawl_id, "archive": "commoncrawl"}}))
        written += 1
    return {"host": host, "archived": written, "rejected": rejected, "suppressed": blocked,
            "crawl_id": cc.collection}


def _norm(v) -> str:
    return re.sub(r"[^a-z0-9@.]+", " ", (v or "").lower()).strip()


def _field_eq(field: str, pred, gold) -> bool:
    if field == "departments":
        return bool({_norm(d) for d in pred or []} & {_norm(d) for d in gold or []})
    return _norm(pred) == _norm(gold)


def dedupe(pred: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for p in pred:
        key = _norm(p.get("name"))
        if not key:
            continue
        rank = (bool(p.get("email")), is_profile(p["page_url"]), sum(bool(p.get(f)) for f in EVAL_FIELDS))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, p)
    return [p for _, p in best.values()]


def score(pred: list[dict], gold: list[dict], page_urls: set[str]) -> dict:
    pred = dedupe(pred)
    gold_by_name = {_norm(g["name"]): g for g in gold}
    pred_by_name = {_norm(p["name"]): p for p in pred}
    reachable = [g for g in gold if g["profile_url"] in page_urls]
    result = {"gold_records": len(reachable), "predicted_records": len(pred)}
    for f in EVAL_FIELDS:
        preds = [p for p in pred if p.get(f)]
        right = sum(1 for p in preds if (g := gold_by_name.get(_norm(p["name"]))) and g.get(f)
                    and _field_eq(f, p[f], g[f]))
        wanted = [g for g in reachable if g.get(f)]
        found = sum(1 for g in wanted if (p := pred_by_name.get(_norm(g["name"]))) and p.get(f)
                    and _field_eq(f, p[f], g[f]))
        result[f] = {"precision": round(right / len(preds), 3) if preds else None,
                     "recall": round(found / len(wanted), 3) if wanted else None,
                     "predicted": len(preds), "gold": len(wanted)}
    return result


def gold_eval(ror: str, root: Path, limit: int | None, synth: bool, table: Path | None = None) -> dict:
    gold = [json.loads(line) for line in (root / ror / "faculty.jsonl").open()]
    doms = domains_for(ror, table)
    pages, _ = split_invalid(load_pages(root / ror)[:limit], doms, root / ror)
    urls = {m["url"] for m, _ in pages}
    report = {"ror_id": ror, "pages": len(pages)}
    if synth:
        report["synthesis"] = synthesize_hosts(ror, root, pages, doms)
    s_only, s_stats = run(ror, root, use_llm=False, limit=limit, domains=doms)
    s_llm, l_stats = run(ror, root, use_llm=True, limit=limit, domains=doms)
    report.update(structured_only=score(s_only, gold, urls), structured_plus_llm=score(s_llm, gold, urls),
                  stats={"structured": s_stats, "llm": l_stats})
    return report


def synthesize_hosts(ror: str, root: Path, pages: list[tuple[dict, str]], doms: tuple[str, ...]) -> list[dict]:
    by_host: dict[str, list[str]] = defaultdict(list)
    for meta, html in pages:
        people = page_people(html, meta["url"], doms, profile=True)
        if is_profile(meta["url"]) and people and people[0].get("name"):
            by_host[urlparse(meta["url"]).netloc].append(html)

    def reference(html: str) -> dict | None:
        found = page_people(html, "", doms, profile=True)
        return found[0] if found else None

    s = Synthesizer()
    return [s.run(host, htmls, reference, root / "_escalation.jsonl", root / "_selectors")
            for host, htmls in by_host.items()]


def table_line(report: dict) -> str:
    rows = ["| Field | Structured P / R | Structured plus LLM P / R |", "|---|---|---|"]
    for f in EVAL_FIELDS:
        a, b = report["structured_only"][f], report["structured_plus_llm"][f]
        rows.append(f"| {f} | {a['precision']} / {a['recall']} | {b['precision']} / {b['recall']} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract faculty records from the local page cache.")
    ap.add_argument("ror", nargs="?", help="one ROR id; omit to walk every cached institution")
    ap.add_argument("--root", type=Path, default=OFFICIAL_CACHE)
    ap.add_argument("--ror-domains", type=Path, default=ROR_DOMAINS)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--gold", action="store_true")
    ap.add_argument("--synthesize", action="store_true")
    ap.add_argument("--archive-host", help="host that returned 403 or a challenge; read it from Common Crawl")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    if args.gold:
        report = gold_eval(args.ror, args.root, args.limit, args.synthesize, args.ror_domains)
        print(json.dumps(report, indent=1))
        print(table_line(report), file=sys.stderr)
        return 0
    if args.archive_host:
        from atlas.users.directories.base import _requests_get

        live = _requests_get(f"https://{args.archive_host}/")
        print(json.dumps(archive_host(args.ror, args.archive_host, args.root, CommonCrawl(), live,
                                      args.limit or 400)))
        return 0
    rors = [args.ror] if args.ror else sorted(p.name for p in args.root.iterdir() if (p / "pages").is_dir())
    code = 0
    for ror in rors:
        recs, stats = run(ror, args.root, use_llm=not args.no_llm, limit=args.limit, table=args.ror_domains)
        with (args.root / ror / "extracted.jsonl").open("w") as f:
            for r in recs:
                f.write(json.dumps(r, sort_keys=True) + "\n")
        print(f"{ror}: {stats['pages']} pages, {stats['invalid_pages']} invalid {stats['invalid_reasons']}, "
              f"{stats['records']} records, {stats['emails']} emails, {stats['llm_failures']} llm failures",
              file=sys.stderr)
        for u in stats["llm_failure_urls"]:
            print(f"llm failure {u}", file=sys.stderr)
        code = code or (3 if stats["llm_failures"] else 0)
        print(json.dumps({"ror_id": ror, **stats}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
