from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.connectors.base import REPO_ROOT
from atlas.users.directories.k12 import detect_k12
from atlas.users.haul import RobotsCache, TokenBucket
from scripts.build_institution_seeds import http_get

NCES_RAW = REPO_ROOT / "data" / "raw" / "nces"
LOG_DIR = REPO_ROOT / "data" / "raw" / "seeds"
OUT = REPO_ROOT / "data" / "seeds" / "schools.csv"
CCD_BASE = "https://nces.ed.gov/ccd/Data/zip/"
SCHOOL_ZIP = "ccd_sch_029_2425_w_1a_073025.zip"
LEA_ZIP = "ccd_lea_029_2425_w_1a_073025.zip"
RELEASE = "CCD 2024-25 nonfiscal directory, provisional release 1a, 2025-07-30"
LICENCE = "public domain, U.S. federal government work"
PROBE_PATHS = ("/staff", "/directory", "/staff-directory", "/faculty-staff", "/our-staff", "/sitemap.xml")
OPEN_STATUS = {"Open", "New", "Added", "Reopened", "Changed Boundary/Agency", "Changed Agency"}
FIELDS = ("nces_id", "name", "district", "district_id", "state", "website", "level", "directory_entry_urls",
          "platform_guess", "probed", "licence", "source_file", "source_release", "source_retrieved")


def fetch_ccd(name: str, raw: Path = NCES_RAW, get=None) -> tuple[Path, str]:
    raw.mkdir(parents=True, exist_ok=True)
    path, meta = raw / name, raw / (name + ".json")
    if not path.exists():
        import requests

        r = (get or requests.get)(CCD_BASE + name, timeout=(10, 300))
        r.raise_for_status()
        path.write_bytes(r.content)
        meta.write_text(json.dumps({"url": CCD_BASE + name,
                                    "retrieved": datetime.now(timezone.utc).strftime("%Y-%m-%d")}))
    retrieved = json.loads(meta.read_text())["retrieved"] if meta.exists() else \
        datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
    return path, retrieved


def zip_rows(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as z:
        member = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(member) as f:
            return list(csv.DictReader(io.TextIOWrapper(f, encoding="latin-1", newline="")))


def norm_site(url: str | None) -> str:
    url = (url or "").strip()
    if not url or url.lower() in ("n/a", "na", "none", "-"):
        return ""
    if "//" not in url:
        url = "http://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc.lower()}{p.path}" if p.netloc and "." in p.netloc else ""


def build_rows(schools: list[dict], leas: list[dict], source: str, retrieved: str) -> list[dict]:
    lea_site = {r["LEAID"]: norm_site(r.get("WEBSITE")) for r in leas}
    out = []
    for r in schools:
        if r.get("UPDATED_STATUS_TEXT", "Open") not in OPEN_STATUS:
            continue
        out.append({"nces_id": r["NCESSCH"], "name": r["SCH_NAME"].strip(), "district": r["LEA_NAME"].strip(),
                    "district_id": r["LEAID"], "state": r["ST"],
                    "website": norm_site(r.get("WEBSITE")) or lea_site.get(r["LEAID"], ""),
                    "level": r.get("LEVEL", ""), "directory_entry_urls": "", "platform_guess": "", "probed": "",
                    "licence": LICENCE, "source_file": source, "source_release": RELEASE,
                    "source_retrieved": retrieved})
    return out


class SiteProbe:
    def __init__(self, bucket: TokenBucket, robots: RobotsCache, get=http_get) -> None:
        self.bucket, self.robots, self.get = bucket, robots, get
        self.cache: dict[str, tuple[str, str]] = {}
        self.locks: dict[str, threading.Lock] = {}
        self.lock = threading.Lock()

    def _one(self, url: str) -> tuple[int, str, str]:
        if not self.robots.allowed(url):
            return 0, "", url
        self.bucket.acquire(urlparse(url).netloc)
        try:
            return self.get(url)
        except Exception:  # noqa: BLE001
            return 599, "", url

    def base(self, site: str) -> tuple[str, str]:
        p = urlparse(site)
        base = f"{p.scheme}://{p.netloc}"
        with self.lock:
            lk = self.locks.setdefault(base, threading.Lock())
        with lk:
            if base in self.cache:
                return self.cache[base]
            status, home, _ = self._one(base + "/")
            bodies, keep = [home if status == 200 else ""], []
            for path in PROBE_PATHS:
                status, text, final = self._one(base + path)
                if status == 200 and urlparse(final).path.rstrip("/") not in ("", "/"):
                    keep.append(final)
                    bodies.append(text[:200_000])
            cls = detect_k12(*bodies)
            self.cache[base] = (";".join(dict.fromkeys(keep)), cls.platform if cls else "")
            return self.cache[base]


def probe_rows(rows: list[dict], probe: SiteProbe, workers: int, partial: Path, log: Path) -> list[dict]:
    done: dict[str, dict] = {}
    if partial.exists():
        with partial.open(newline="") as f:
            done = {r["nces_id"]: r for r in csv.DictReader(f)}
    todo = [r for r in rows if r["nces_id"] not in done]
    new = not partial.exists()
    partial.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with partial.open("a", newline="") as pf, log.open("a") as lf, ThreadPoolExecutor(workers) as ex:
        w = csv.DictWriter(pf, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()

        def one(row: dict) -> dict:
            if row["website"]:
                row["directory_entry_urls"], row["platform_guess"] = probe.base(row["website"])
            row["probed"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return row

        for fut in as_completed([ex.submit(one, dict(r)) for r in todo]):
            row = fut.result()
            done[row["nces_id"]] = row
            w.writerow(row)
            pf.flush()
            lf.write(f"{time.monotonic() - start:.0f}s {len(done)}/{len(rows)} {row['nces_id']} "
                     f"{urlparse(row['website']).netloc} entries={len(row['directory_entry_urls'].split(';')) if row['directory_entry_urls'] else 0} "
                     f"platform={row['platform_guess'] or '-'}\n")
            lf.flush()
    return [done.get(r["nces_id"], r) for r in rows]


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the K-12 school seed list from the NCES Common Core of Data.")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--probe-limit", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--no-probe", action="store_true")
    args = ap.parse_args(argv)
    sch_path, retrieved = fetch_ccd(SCHOOL_ZIP)
    lea_path, _ = fetch_ccd(LEA_ZIP)
    rows = build_rows(zip_rows(sch_path), zip_rows(lea_path), f"{SCHOOL_ZIP};{LEA_ZIP}", retrieved)
    if not args.no_probe:
        bucket = TokenBucket(1.0)
        probe = SiteProbe(bucket, RobotsCache(lambda u: http_get(u)[:2], bucket))
        head = probe_rows(rows[: args.probe_limit], probe, args.workers,
                          LOG_DIR / "schools_probe.partial.csv", LOG_DIR / "schools_probe.log")
        rows = head + rows[args.probe_limit:]
    write_rows(args.out, rows)
    probed = [r for r in rows if r["probed"]]
    counts: dict[str, int] = {}
    for r in probed:
        counts[r["platform_guess"] or "none"] = counts.get(r["platform_guess"] or "none", 0) + 1
    print(f"schools={len(rows)} with_website={sum(1 for r in rows if r['website'])} probed={len(probed)} "
          f"with_entry={sum(1 for r in probed if r['directory_entry_urls'])} platforms={json.dumps(counts, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
