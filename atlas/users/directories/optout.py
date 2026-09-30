from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from atlas.users.directories.domains import normalize_ror

REPO = Path(__file__).resolve().parents[3]
PRIVATE = REPO / "data" / "private"
OPT_OUT = PRIVATE / "opt_out.csv"
TOMBSTONES = PRIVATE / "tombstones.csv"
OPT_OUT_COLUMNS = ("orcid", "email", "name", "ror_id", "reason", "as_of")
TOMBSTONE_COLUMNS = ("kind", "sha256", "as_of")
RECORD_FILES = ("faculty.jsonl", "records.jsonl", "extracted.jsonl")


def norm_name(value: str | None) -> str:
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", value)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_orcid(value: str | None) -> str:
    return (value or "").strip().rstrip("/").rsplit("/", 1)[-1].upper()


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def person_keys(name: str | None = None, ror_id: str | None = None, orcid: str | None = None,
                email: str | None = None) -> list[tuple[str, str]]:
    keys = []
    if (o := norm_orcid(orcid)):
        keys.append(("orcid", sha(o)))
    if email and email.strip():
        keys.append(("email", sha(email.strip().lower())))
    if (n := norm_name(name)) and ror_id:
        keys.append(("name_ror", sha(f"{n}|{normalize_ror(ror_id)}")))
    return keys


@dataclass
class Suppression:
    hashes: set[tuple[str, str]] = field(default_factory=set)
    opt_out_path: Path = OPT_OUT
    tombstone_path: Path = TOMBSTONES

    @classmethod
    def load(cls, opt_out_path: Path = OPT_OUT, tombstone_path: Path = TOMBSTONES) -> "Suppression":
        s = cls(opt_out_path=opt_out_path, tombstone_path=tombstone_path)
        if opt_out_path.exists():
            with opt_out_path.open(newline="") as f:
                for row in csv.DictReader(f):
                    s.hashes.update(person_keys(row.get("name"), row.get("ror_id"), row.get("orcid"), row.get("email")))
        if tombstone_path.exists():
            with tombstone_path.open(newline="") as f:
                s.hashes.update((row["kind"], row["sha256"]) for row in csv.DictReader(f))
        return s

    def blocks(self, name: str | None = None, ror_id: str | None = None, orcid: str | None = None,
               email: str | None = None) -> bool:
        return any(k in self.hashes for k in person_keys(name, ror_id, orcid, email))

    def blocks_record(self, rec) -> bool:
        get = rec.get if isinstance(rec, dict) else lambda k: getattr(rec, k, None)
        return self.blocks(get("name"), get("ror_id"), get("orcid"), get("email"))

    def remove(self, cache_root: Path, name: str | None = None, ror_id: str | None = None,
               orcid: str | None = None, email: str | None = None) -> dict:
        keys = person_keys(name, ror_id, orcid, email)
        self.hashes.update(keys)
        self._write_tombstones(keys)
        probe = Suppression(set(keys))
        purged = {"records": 0, "pages": 0}
        dirs = [cache_root / normalize_ror(ror_id)] if ror_id else [p for p in cache_root.iterdir() if p.is_dir()]
        for d in dirs:
            urls: set[str] = set()
            for fname in RECORD_FILES:
                path = d / fname
                if not path.exists():
                    continue
                kept = []
                for line in path.read_text().splitlines():
                    rec = json.loads(line)
                    if probe.blocks_record({**rec, "ror_id": rec.get("ror_id") or d.name}):
                        purged["records"] += 1
                        urls.update(u for u in (rec.get("profile_url"), rec.get("source_url")) if u)
                    else:
                        kept.append(line)
                path.write_text("".join(k + "\n" for k in kept))
            purged["pages"] += _purge_pages(d / "pages", urls, email)
        return purged

    def _write_tombstones(self, keys: Iterable[tuple[str, str]]) -> None:
        self.tombstone_path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.tombstone_path.exists()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.tombstone_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(TOMBSTONE_COLUMNS)
            for kind, digest in keys:
                w.writerow((kind, digest, now))


def _purge_pages(pages: Path, urls: set[str], email: str | None) -> int:
    if not pages.is_dir():
        return 0
    removed = 0
    targets = {sha(u) for u in urls}
    for meta in pages.glob("*.json"):
        key = meta.name[:-5]
        bodies = [p for p in (pages / f"{key}.html", pages / f"{key}.html.gz") if p.exists()]
        hit = key in targets
        if not hit and email:
            import gzip

            for b in bodies:
                raw = b.read_bytes()
                text = (gzip.decompress(raw) if b.suffix == ".gz" else raw).decode("utf-8", "replace").lower()
                if email.strip().lower() in text:
                    hit = True
                    break
        if hit:
            for p in [meta, *bodies]:
                p.unlink()
            removed += 1
    return removed
