from __future__ import annotations

import csv
import hashlib
import hmac
import os
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
LEGACY = PRIVATE / "tombstones_legacy.csv"
LEGACY_COLUMNS = ("kind", "sha256", "as_of")
OPT_OUT_COLUMNS = ("orcid", "email", "name", "ror_id", "reason", "as_of")
TOMBSTONE_COLUMNS = ("kind", "sha256", "as_of", "scheme")
SCHEME = "hmac-sha256"
KEY_ENV = "RESEARCH_ATLAS_TOMBSTONE_KEY"
DEFAULT_KEY = Path.home() / ".config" / "research-atlas" / "tombstone.key"
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


def key_path() -> Path:
    return Path(os.environ[KEY_ENV]) if os.environ.get(KEY_ENV) else DEFAULT_KEY


KEY_BYTES = 32


_KEYS: dict[str, bytes] = {}


def load_key(attempts: int = 3, pause: float = 0.05) -> bytes:
    path = key_path()
    cached = _KEYS.get(str(path))
    if cached is not None and path.exists():
        return cached
    key = _read_or_create_key(path, attempts, pause)
    _KEYS[str(path)] = key
    return key


def _read_or_create_key(path: Path, attempts: int, pause: float) -> bytes:
    import time

    for _ in range(attempts):
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(KEY_BYTES))
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
            finally:
                tmp.unlink()
        data = path.read_bytes() if path.exists() else b""
        if len(data) == KEY_BYTES:
            os.chmod(path, 0o600)
            return data
        time.sleep(pause)
    raise RuntimeError(f"tombstone key at {path} is not {KEY_BYTES} bytes")


def keyed(value: str, key: bytes | None = None) -> str:
    return hmac.new(key if key is not None else load_key(), value.encode(), hashlib.sha256).hexdigest()


def norm_url(url: str | None) -> str:
    from urllib.parse import urlparse

    if not url:
        return ""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{p.path}".rstrip("/").lower()


def url_key(url: str, key: bytes | None = None) -> tuple[str, str]:
    return "url", keyed(norm_url(url), key)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _raw_keys(name, ror_id, orcid, email) -> list[tuple[str, str]]:
    keys = []
    if (o := norm_orcid(orcid)):
        keys.append(("orcid", o))
    if email and email.strip():
        keys.append(("email", email.strip().lower()))
    if (n := norm_name(name)) and ror_id:
        keys.append(("name_ror", f"{n}|{normalize_ror(ror_id)}"))
    return keys


def person_keys(name: str | None = None, ror_id: str | None = None, orcid: str | None = None,
                email: str | None = None, key: bytes | None = None) -> list[tuple[str, str]]:
    raw = _raw_keys(name, ror_id, orcid, email)
    if not raw:
        return []
    k = key if key is not None else load_key()
    return [(kind, keyed(v, k)) for kind, v in raw]


def _opt_out_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def migrate_tombstones(opt_out_path: Path = OPT_OUT, tombstone_path: Path = TOMBSTONES,
                       legacy_path: Path | None = None) -> dict:
    legacy_path = legacy_path or tombstone_path.with_name("tombstones_legacy.csv")
    result = {"kept": 0, "rewritten": 0, "legacy": 0}
    if not tombstone_path.exists():
        return result
    with tombstone_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    legacy_rows = _csv_rows(legacy_path)
    if all(r.get("scheme") == SCHEME for r in rows) and not legacy_rows:
        result["kept"] = len(rows)
        return result
    k = load_key()
    plain_to_keyed = {}
    for row in _opt_out_rows(opt_out_path):
        for kind, v in _raw_keys(row.get("name"), row.get("ror_id"), row.get("orcid"), row.get("email")):
            plain_to_keyed[(kind, sha(v))] = keyed(v, k)
    rows = rows + [{**r, "scheme": "sha256"} for r in legacy_rows]
    out, legacy = [], []
    for r in rows:
        if r.get("scheme") == SCHEME:
            out.append(r)
            result["kept"] += 1
        elif (hit := plain_to_keyed.get((r["kind"], r["sha256"]))):
            out.append({"kind": r["kind"], "sha256": hit, "as_of": r.get("as_of", ""), "scheme": SCHEME})
            result["rewritten"] += 1
        else:
            legacy.append({"kind": r["kind"], "sha256": r["sha256"], "as_of": r.get("as_of", "")})
    legacy = list({(r["kind"], r["sha256"]): r for r in legacy}.values())
    result["legacy"] = len(legacy)
    _write_csv(tombstone_path, TOMBSTONE_COLUMNS, out)
    if legacy or legacy_path.exists():
        _write_csv(legacy_path, LEGACY_COLUMNS, legacy)
    return result


def _csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, columns, rows: list[dict]) -> None:
    import io

    buf = io.StringIO()
    w = csv.DictWriter(buf, columns, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, buf.getvalue())


@dataclass
class Suppression:
    hashes: set[tuple[str, str]] = field(default_factory=set)
    opt_out_path: Path = OPT_OUT
    tombstone_path: Path = TOMBSTONES
    migration: dict = field(default_factory=dict)
    legacy: set[tuple[str, str]] = field(default_factory=set)

    @classmethod
    def load(cls, opt_out_path: Path = OPT_OUT, tombstone_path: Path = TOMBSTONES) -> "Suppression":
        s = cls(opt_out_path=opt_out_path, tombstone_path=tombstone_path)
        s.migration = migrate_tombstones(opt_out_path, tombstone_path)
        for row in _opt_out_rows(opt_out_path):
            s.hashes.update(person_keys(row.get("name"), row.get("ror_id"), row.get("orcid"), row.get("email")))
        if tombstone_path.exists():
            with tombstone_path.open(newline="") as f:
                s.hashes.update((row["kind"], row["sha256"]) for row in csv.DictReader(f)
                                if row.get("scheme") == SCHEME)
        s.legacy = {(r["kind"], r["sha256"]) for r in _csv_rows(tombstone_path.with_name("tombstones_legacy.csv"))}
        return s

    def blocks(self, name: str | None = None, ror_id: str | None = None, orcid: str | None = None,
               email: str | None = None) -> bool:
        if any(k in self.hashes for k in person_keys(name, ror_id, orcid, email)):
            return True
        return bool(self.legacy) and any((kind, sha(v)) in self.legacy
                                         for kind, v in _raw_keys(name, ror_id, orcid, email))

    def blocks_url(self, url: str | None) -> bool:
        if not norm_url(url):
            return False
        return url_key(url) in self.hashes or ("url", sha(norm_url(url))) in self.legacy

    def blocks_record(self, rec) -> bool:
        get = rec.get if isinstance(rec, dict) else lambda k: getattr(rec, k, None)
        return self.blocks(get("name"), get("ror_id"), get("orcid"), get("email"))

    def remove(self, cache_root: Path, name: str | None = None, ror_id: str | None = None,
               orcid: str | None = None, email: str | None = None, profile_url: str | None = None) -> dict:
        keys = person_keys(name, ror_id, orcid, email)
        probe = Suppression(set(keys))
        all_urls: set[str] = {profile_url} if profile_url else set()
        written = set(keys) | {url_key(u) for u in all_urls}
        self.hashes.update(written)
        self._write_tombstones(sorted(written))
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
                _atomic_write(path, "".join(k + "\n" for k in kept))
            all_urls |= urls
            purged["pages"] += _purge_pages(d / "pages", urls | all_urls, email, name)
        late = [k for k in dict.fromkeys(url_key(u) for u in all_urls) if k not in written]
        self.hashes.update(late)
        if late:
            self._write_tombstones(late)
        purged["urls"] = len(all_urls)
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
                w.writerow((kind, digest, now, SCHEME))


def _page_mentions(text: str, email: str | None, name: str | None) -> bool:
    if email and email.strip().lower() in text.lower():
        return True
    n = norm_name(name)
    if len(n.split()) < 2:
        return False
    plain = norm_name(re.sub(r"<[^>]+>", " ", text))
    return f" {n} " in f" {plain} "


def _purge_pages(pages: Path, urls: set[str], email: str | None, name: str | None = None) -> int:
    if not pages.is_dir():
        return 0
    removed = 0
    targets = {sha(u) for u in urls}
    for meta in pages.glob("*.json"):
        key = meta.name[:-5]
        bodies = [p for p in (pages / f"{key}.html", pages / f"{key}.html.gz") if p.exists()]
        hit = key in targets
        if not hit and (email or name):
            import gzip

            for b in bodies:
                raw = b.read_bytes()
                text = (gzip.decompress(raw) if b.suffix == ".gz" else raw).decode("utf-8", "replace")
                if _page_mentions(text, email, name):
                    hit = True
                    break
        if hit:
            for p in [meta, *bodies]:
                p.unlink()
            removed += 1
    return removed
