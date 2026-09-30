from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.users.directories.base import OFFICIAL_CACHE  # noqa: E402
from atlas.users.extract.local_llm import LocalExtractor  # noqa: E402
from atlas.users.extract.structured import page_people, profile_invalid_reason  # noqa: E402
from atlas.users.extract.synthesize import Synthesizer  # noqa: E402

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


def domains_for(ror: str, pages: list[tuple[dict, str]]) -> tuple[str, ...]:
    try:
        from atlas.users.directories.domains import allowed_domains

        doms = allowed_domains(ror)
        if doms:
            return doms
    except (ImportError, FileNotFoundError):
        pass
    hosts = {urlparse(m["url"]).hostname or "" for m, _ in pages}
    return tuple(sorted({h[4:] if h.startswith("www.") else h for h in hosts if h}))


def is_profile(url: str) -> bool:
    return bool(PROFILE_PATH.search(urlparse(url).path))


def record(ror: str, meta: dict, person: dict, extractor: str, digest: str | None = None) -> dict:
    url = person.get("profile_url") or meta["url"]
    rec = {k: person.get(k) for k in ("name", "title", "departments", "school", "research_areas", "email", "orcid")}
    rec.update(profile_url=url, ror_id=ror, source="official_directory", source_id=url, source_url=meta["url"],
               as_of=meta.get("fetched_at"), match_tier=None, licence=LICENCE, extractor=extractor,
               extractor_digest=digest, html_sha256=meta.get("sha256"))
    rec["departments"] = rec["departments"] or []
    return rec


def is_miss(url: str, people: list[dict]) -> bool:
    return is_profile(url) and not any(p.get("name") and p.get("email") for p in people)


def run(ror: str, root: Path, use_llm: bool, extractor: LocalExtractor | None = None,
        limit: int | None = None) -> tuple[list[dict], dict]:
    ror_dir = root / ror
    pages, invalid = split_invalid(load_pages(ror_dir)[:limit], domains_for(ror, load_pages(ror_dir)[:limit]), ror_dir)
    doms = domains_for(ror, pages)
    out, misses = [], []
    started = time.monotonic()
    for meta, html in pages:
        people = page_people(html, meta["url"], doms, profile=is_profile(meta["url"]))
        if people and not is_miss(meta["url"], people):
            out += [record(ror, meta, p, "structured") for p in people]
        else:
            misses.append((meta, html, people))
    structured_s = time.monotonic() - started
    llm_s = 0.0
    if use_llm and misses:
        ex = extractor or LocalExtractor()
        t0 = time.monotonic()
        results = ex.extract_many([(html, meta["url"]) for meta, html, _ in misses])
        llm_s = time.monotonic() - t0
        for (meta, _, people), res in zip(misses, results):
            if res and res.get("name"):
                out.append(record(ror, meta, res, res["extractor"], res.get("extractor_digest")))
            else:
                out += [record(ror, meta, p, "structured") for p in people]
    else:
        for meta, _, people in misses:
            out += [record(ror, meta, p, "structured") for p in people]
    stats = {"invalid_pages": sum(invalid.values()), "invalid_reasons": dict(invalid), "pages": len(pages), "structured_hits": len(pages) - len(misses), "llm_pages": len(misses) if use_llm else 0,
             "records": len(out), "emails": sum(1 for r in out if r["email"]),
             "structured_pages_per_min": round(len(pages) / structured_s * 60, 1) if structured_s else None,
             "llm_pages_per_min": round(len(misses) / llm_s * 60, 1) if llm_s else None}
    return out, stats


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
        rank = (bool(p.get("email")), is_profile(p["source_url"]), sum(bool(p.get(f)) for f in EVAL_FIELDS))
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


def gold_eval(ror: str, root: Path, limit: int | None, synth: bool) -> dict:
    gold = [json.loads(line) for line in (root / ror / "faculty.jsonl").open()]
    loaded = load_pages(root / ror)[:limit]
    pages, _ = split_invalid(loaded, domains_for(ror, loaded), root / ror)
    urls = {m["url"] for m, _ in pages}
    s_only, s_stats = run(ror, root, use_llm=False, limit=limit)
    s_llm, l_stats = run(ror, root, use_llm=True, limit=limit)
    report = {"ror_id": ror, "pages": len(pages), "structured_only": score(s_only, gold, urls),
              "structured_plus_llm": score(s_llm, gold, urls), "stats": {"structured": s_stats, "llm": l_stats}}
    if synth:
        report["synthesis"] = synthesize_hosts(ror, root, pages)
    return report


def synthesize_hosts(ror: str, root: Path, pages: list[tuple[dict, str]]) -> list[dict]:
    doms = domains_for(ror, pages)
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract faculty records from the local page cache.")
    ap.add_argument("ror", nargs="?", help="one ROR id; omit to walk every cached institution")
    ap.add_argument("--root", type=Path, default=OFFICIAL_CACHE)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--gold", action="store_true")
    ap.add_argument("--synthesize", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    if args.gold:
        print(json.dumps(gold_eval(args.ror, args.root, args.limit, args.synthesize), indent=1))
        return 0
    rors = [args.ror] if args.ror else sorted(p.name for p in args.root.iterdir() if (p / "pages").is_dir())
    for ror in rors:
        recs, stats = run(ror, args.root, use_llm=not args.no_llm, limit=args.limit)
        with (args.root / ror / "extracted.jsonl").open("w") as f:
            for r in recs:
                f.write(json.dumps(r, sort_keys=True) + "\n")
        print(f"{ror}: {stats['pages']} pages, {stats['invalid_pages']} invalid {stats['invalid_reasons']}, "
              f"{stats['records']} records, {stats['emails']} emails", file=sys.stderr)
        print(json.dumps({"ror_id": ror, **stats}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
