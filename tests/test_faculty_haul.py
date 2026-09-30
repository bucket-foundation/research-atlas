import importlib.util
import json
import threading
from pathlib import Path

from atlas.users.directories.base import FacultyRecord, Page, accept_email
from atlas.users.haul import (
    DiskBudget,
    HaulFetcher,
    HaulState,
    RorIndex,
    TokenBucket,
    advisor_tiers,
    parse_wiki_carnegie,
    status_line,
)

REPO = Path(__file__).resolve().parents[1]
DOMAIN = "fixture.test"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


haul = _load("faculty_haul")


def ror(rid, name, *, cc="US", types=("education",), aliases=(), wiki=None, parent=False):
    names = [{"value": name, "types": ["ror_display", "label"]}] + [{"value": a, "types": ["alias"]} for a in aliases]
    links = [{"type": "website", "value": f"https://www.{rid}.test/"}]
    if wiki:
        links.append({"type": "wikipedia", "value": f"http://en.wikipedia.org/wiki/{wiki}"})
    return {"id": f"https://ror.org/{rid}", "names": names, "links": links, "types": list(types), "domains": [],
            "status": "active", "relationships": [{"type": "parent", "id": "x"}] if parent else [],
            "locations": [{"geonames_details": {"country_code": cc}}]}


def test_seed_resolution_on_five_synthetic_names():
    idx = RorIndex([
        ror("0aaaaaa01", "Northfield Institute of Technology", wiki="Northfield_Institute_of_Technology"),
        ror("0aaaaaa02", "University of Eastvale", aliases=["Eastvale University"]),
        ror("0aaaaaa03", "Saint Quill College", cc="CA"),
        ror("0aaaaaa04", "Harbor State University"),
        ror("0aaaaaa05", "Harbor State University", parent=True),
        ror("0aaaaaa06", "Lumen Research Hospital", types=("healthcare",)),
    ])
    got = {n: (r and r["id"][-9:]) for n, t in [
        ("Northfield Tech", "Northfield Institute of Technology"),
        ("The University of Eastvale", None),
        ("Eastvale University", None),
        ("Saint Quill College", None),
        ("Lumen Research Hospital", None),
    ] for r in [idx.resolve(n, t)]}
    assert got == {"Northfield Tech": "0aaaaaa01", "The University of Eastvale": "0aaaaaa02",
                   "Eastvale University": "0aaaaaa02", "Saint Quill College": None, "Lumen Research Hospital": None}
    assert idx.resolve("Harbor State University")["id"].endswith("0aaaaaa04")


def test_parse_wiki_carnegie_sections():
    text = ('== Universities classified among "R1: Doctoral Universities" ==\n{| class="wikitable"\n!Name\n|-\n'
            '|[[Alpha University]]\n|Town\n|-\n| {{sort|B|[[Beta Tech|Beta]]}} ||x\n|}\n'
            '== Universities classified among "R2: Doctoral Universities" ==\n{|\n|-\n|[[Gamma College]]\n|}\n')
    got = parse_wiki_carnegie(text)
    assert got["R1"] == [("Alpha University", "Alpha University"), ("Beta", "Beta Tech")]
    assert got["R2"] == [("Gamma College", "Gamma College")]


def test_advisor_tiers_takes_best_tier_and_counts():
    ranked = [{"tier": "A", "openalex_url": "https://openalex.org/A1"},
              {"tier": "A+", "openalex_url": "https://openalex.org/A2"},
              {"tier": "B", "openalex_url": "https://openalex.org/A3"}]
    roster = [{"author_id": "A1", "ror": "https://ror.org/0aaaaaa01"},
              {"author_id": "A2", "ror": "https://ror.org/0aaaaaa01"},
              {"author_id": "A3", "ror": "https://ror.org/0aaaaaa02"}]
    assert advisor_tiers(ranked, roster) == {"0aaaaaa01": ("A+", 2)}


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_token_bucket_spaces_requests_per_host():
    clock = FakeClock()
    b = TokenBucket(1.0, clock=clock, sleep=clock.sleep)
    waits = [b.acquire("a.test") for _ in range(4)]
    assert waits == [0, 1.0, 1.0, 1.0] and clock.t == 3.0
    assert b.acquire("b.test") == 0
    b.set_interval("c.test", 5)
    b.acquire("c.test")
    assert b.acquire("c.test") == 5


def test_token_bucket_real_time_under_threads():
    import time

    b = TokenBucket(0.05)
    start = time.monotonic()
    ts = [threading.Thread(target=b.acquire, args=("h.test",)) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert time.monotonic() - start >= 0.19


def email(local):
    return local + "@" + DOMAIN


class FakeAdapter:
    domains = (DOMAIN,)

    def __init__(self, ror_id, n=3, with_email=True):
        self.ror_id, self.n, self.with_email = ror_id, n, with_email

    def seeds(self, fetcher):
        return [f"https://www.{DOMAIN}/{self.ror_id}/p{i}" for i in range(self.n)]

    def parse(self, page: Page):
        slug = page.url.rsplit("/", 1)[1]
        rec = FacultyRecord(self.ror_id, slug, "Test Person", None, [], None, None, None, page.url,
                            page.fetched_at, page.sha256)
        return [accept_email(rec, email(slug) if self.with_email else None, self.domains)]


def fake_get(url):
    if url.endswith("/robots.txt"):
        return 200, "User-agent: *\nDisallow: /private\n"
    import hashlib

    blob = "".join(hashlib.sha256(f"{url}{i}".encode()).hexdigest() for i in range(40))
    return 200, "<html>" + blob + "</html>"


def rows(*rors):
    return [{"ror_id": r, "name": r, "domains": DOMAIN, "homepage": "", "directory_entry_urls": ""} for r in rors]


def fast_bucket():
    return TokenBucket(0.0)


def test_run_writes_records_and_status(tmp_path):
    code, state = haul.run(rows("r1"), cache_root=tmp_path, workers=2, max_pages=50, budget_bytes=10**9,
                           get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r, f: FakeAdapter(r["ror_id"]))
    assert code == 0 and state.institutions["r1"]["status"] == "low_yield"
    lines = (tmp_path / "r1" / "records.jsonl").read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["email_source"] == "official_directory"
    first = json.loads(lines[0])
    assert first["licence"] == "institution-copyright" and first["match_tier"] is None
    assert first["source_url"] == first["profile_url"] and first["as_of"] == first["fetched_at"]
    assert list((tmp_path / "r1" / "pages").glob("*.html.gz"))
    assert "low_yield=1" in status_line(state, tmp_path) and "emails=3" in status_line(state, tmp_path)


def test_resume_from_state_file(tmp_path):
    (tmp_path / "_state.json").write_text(json.dumps({"institutions": {
        "r1": {"name": "r1", "status": "done", "profiles": 4, "emails": 4},
        "r2": {"name": "r2", "status": "running", "profiles": 0, "emails": 0},
        "r3": {"name": "r3", "status": "pending", "profiles": 0, "emails": 0}}}))
    seen = []

    def factory(r, f):
        seen.append(r["ror_id"])
        return FakeAdapter(r["ror_id"])

    code, state = haul.run(rows("r1", "r2", "r3"), cache_root=tmp_path, workers=1, max_pages=50,
                           budget_bytes=10**9, get=fake_get, bucket=fast_bucket(), adapter_factory=factory)
    assert sorted(seen) == ["r2", "r3"]
    assert {k: v["status"] for k, v in state.institutions.items()} == {"r1": "done", "r2": "low_yield", "r3": "low_yield"}
    assert HaulState.load(tmp_path / "_state.json").counts()["profiles"] == 10


def test_disk_budget_stops_cleanly(tmp_path):
    code, state = haul.run(rows("r1", "r2"), cache_root=tmp_path, workers=1, max_pages=500, budget_bytes=1500,
                           get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r, f: FakeAdapter(r["ror_id"], n=20))
    assert code == 3
    assert state.institutions["r1"]["status"] == "pending" and state.institutions["r1"]["error"] == "disk_budget"
    assert state.institutions["r2"]["status"] == "pending"
    assert len(list(tmp_path.rglob("*.html.gz"))) == 1
    code2, _ = haul.run(rows("r1"), cache_root=tmp_path, workers=1, max_pages=500, budget_bytes=1500,
                        get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r, f: FakeAdapter(r["ror_id"]))
    assert code2 == 3 and len(list(tmp_path.rglob("*.html.gz"))) == 1


def test_zero_yield_exit_code(tmp_path):
    code, state = haul.run(rows("r1"), cache_root=tmp_path, workers=1, max_pages=50, budget_bytes=10**9,
                           get=fake_get, bucket=fast_bucket(),
                           adapter_factory=lambda r, f: FakeAdapter(r["ror_id"], with_email=False))
    assert code == 2 and state.institutions["r1"]["status"] == "zero_yield"


def test_page_cap_and_robots(tmp_path):
    class Private(FakeAdapter):
        def seeds(self, fetcher):
            return [f"https://www.{DOMAIN}/private/x"] + super().seeds(fetcher)

    code, state = haul.run(rows("r1"), cache_root=tmp_path, workers=1, max_pages=3, budget_bytes=10**9,
                           get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r, f: Private(r["ror_id"], n=10))
    rec = state.institutions["r1"]
    assert rec["robots_denied"] == 1 and rec["capped"] and rec["profiles"] == 2


def test_no_adapter_marks_failed(tmp_path):
    code, state = haul.run(rows("r1"), cache_root=tmp_path, workers=1, max_pages=5, budget_bytes=10**9,
                           get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r, f: None)
    assert state.institutions["r1"]["status"] == "pending_adapter" and state.institutions["r1"]["error"] == "no_adapter"
    assert "r1" in state.todo()
    code, state = haul.run(rows("r1"), cache_root=tmp_path, workers=1, max_pages=50, budget_bytes=10**9,
                           get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r, f: FakeAdapter(r["ror_id"], n=6))
    assert state.institutions["r1"]["status"] == "done"


def test_fetcher_cache_skips_refetch(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return fake_get(url)

    b = fast_bucket()
    mk = lambda: HaulFetcher(tmp_path, bucket=b, budget=DiskBudget(tmp_path, 10**9), get=get)
    mk().fetch(f"https://www.{DOMAIN}/a")
    mk().fetch(f"https://www.{DOMAIN}/a")
    assert [c for c in calls if not c.endswith("robots.txt")] == [f"https://www.{DOMAIN}/a"]


def test_fold_into_private_parquet(tmp_path):
    import pytest

    pytest.importorskip("pandas")
    haul.run(rows("r1"), cache_root=tmp_path, workers=1, max_pages=50, budget_bytes=10**9, get=fake_get,
             bucket=fast_bucket(), adapter_factory=lambda r, f: FakeAdapter(r["ror_id"]))
    import hashlib

    public = [REPO / "data" / "processed" / "sample" / "researchers_sample.parquet", REPO / "research_atlas.duckdb",
              REPO / "data" / "MANIFEST.json"]
    digest = lambda: {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in public if p.exists()}
    before = digest()
    assert before
    build = _load("build_official_contacts")
    out = tmp_path / "contacts_official.parquet"
    assert build.main(["--cache-root", str(tmp_path), "--out", str(out), "--opt-out", str(tmp_path / "none.csv"),
                       "--tombstones", str(tmp_path / "none-t.csv")]) == 0
    assert digest() == before
    import pandas as pd

    df = pd.read_parquet(out)
    assert len(df) == 3 and df["email"].notna().all()
    assert set(df["licence"]) == {"institution-copyright"} and df["match_tier"].isna().all()
    assert "contacts_official.parquet" in (REPO / ".gitignore").read_text()


def test_opt_out_skips_at_crawl_purges_and_fold(tmp_path, monkeypatch):
    import csv

    from atlas.users.directories.optout import Suppression

    monkeypatch.setenv("RESEARCH_ATLAS_TOMBSTONE_KEY", str(tmp_path / "key"))
    cache = tmp_path / "cache"
    haul.run(rows("r1"), cache_root=cache, workers=1, max_pages=50, budget_bytes=10**9, get=fake_get,
             bucket=fast_bucket(), adapter_factory=lambda r, f: FakeAdapter(r["ror_id"]))
    assert len(list((cache / "r1" / "pages").glob("*.html.gz"))) == 3
    opt = tmp_path / "opt_out.csv"
    with opt.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["orcid", "email", "name", "ror_id", "reason", "as_of"])
        w.writeheader()
        w.writerow({"email": email("p0").upper(), "ror_id": "r1", "reason": "asked", "as_of": "2026-09-29"})
        w.writerow({"name": "  test PERSON ", "ror_id": "https://ror.org/r2", "reason": "asked"})
    sup = Suppression.load(opt, tmp_path / "tombstones.csv")
    rows_opt = list(csv.DictReader(opt.open()))
    code, state = haul.run(rows("r1", "r2"), cache_root=cache, workers=1, max_pages=50, budget_bytes=10**9,
                           get=fake_get, bucket=fast_bucket(), refresh=True, only=["r1", "r2"],
                           adapter_factory=lambda r, f: FakeAdapter(r["ror_id"]), suppression=sup, opt_rows=rows_opt)
    r1, r2 = state.institutions["r1"], state.institutions["r2"]
    assert r1["purged_items"] >= 1 and r1["opt_out_skipped"] >= 1 and r1["profiles"] == 2
    assert r2["profiles"] == 0 and r2["status"] == "zero_yield" and r2["opt_out_skipped"] == 3
    assert "opt_out_skipped=" in status_line(state, cache)
    tomb = (tmp_path / "tombstones.csv").read_text()
    assert email("p0") not in tomb and "p0" not in tomb
    urls = [json.loads(m.read_text())["url"] for m in (cache / "r1" / "pages").glob("*.json")]
    assert not any(u.endswith("/p0") for u in urls)
    with (cache / "r1" / "records.jsonl").open("a") as f:
        f.write(json.dumps({"slug": "late", "name": "X", "ror_id": "r1", "email": email("p0")}) + "\n")
    build = _load("build_official_contacts")
    got, dropped = build.fold(cache, Suppression.load(opt, tmp_path / "tombstones.csv"))
    assert dropped == 1 and len(got) == 2
    assert all(g["storage"] == "link_only" and g["retrieved_by"] for g in got if g["slug"] != "late")


def test_tombstoned_url_is_never_fetched_or_cached(tmp_path, monkeypatch):
    from atlas.users.directories.optout import Suppression, url_key

    monkeypatch.setenv("RESEARCH_ATLAS_TOMBSTONE_KEY", str(tmp_path / "key"))
    sup = Suppression(hashes={url_key(f"https://www.{DOMAIN}/r1/p1")})
    calls = []

    def get(url):
        calls.append(url)
        return fake_get(url)

    code, state = haul.run(rows("r1"), cache_root=tmp_path / "c", workers=1, max_pages=50, budget_bytes=10**9,
                           get=get, bucket=fast_bucket(), adapter_factory=lambda r, f: FakeAdapter(r["ror_id"]),
                           suppression=sup)
    assert not any(c.endswith("/p1") for c in calls)
    assert state.institutions["r1"]["profiles"] == 2


def test_exit_codes_all_failed_and_budget(tmp_path):
    class Boom(FakeAdapter):
        def seeds(self, fetcher):
            raise ValueError("x")

    code, _ = haul.run(rows("r1", "r2"), cache_root=tmp_path / "a", workers=1, max_pages=5, budget_bytes=10**9,
                       get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r, f: Boom(r["ror_id"]))
    assert code == 1
    code, _ = haul.run(rows("r1"), cache_root=tmp_path / "b", workers=1, max_pages=50, budget_bytes=1500,
                       get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r, f: FakeAdapter(r["ror_id"], n=20))
    assert code == 3


def test_service_is_bounded_and_reports_zero_yield():
    svc = (REPO / "scripts" / "systemd" / "faculty-haul.service").read_text()
    assert "RuntimeMaxSec=" in svc and "infinity" not in svc and "SuccessExitStatus" not in svc


def test_private_dir_is_gitignored():
    import subprocess

    out = subprocess.run(["git", "check-ignore", "-q", "data/private/opt_out.csv"], cwd=REPO)
    assert out.returncode == 0


def test_low_yield_below_five_profiles(tmp_path):
    code, state = haul.run(rows("r1", "r2"), cache_root=tmp_path, workers=1, max_pages=50, budget_bytes=10**9,
                           get=fake_get, bucket=fast_bucket(),
                           adapter_factory=lambda r, f: FakeAdapter(r["ror_id"], n=1 if r["ror_id"] == "r1" else 5))
    assert state.institutions["r1"]["status"] == "low_yield" and state.institutions["r2"]["status"] == "done"
    assert code == 0 and "low_yield=1" in status_line(state, tmp_path)


def test_fold_dedupes_on_profile_url_keeping_newest(tmp_path):
    import pytest

    pytest.importorskip("pandas")
    d = tmp_path / "r1"
    d.mkdir()
    recs = [{"ror_id": "r1", "slug": "a", "profile_url": "https://x.test/a", "as_of": "2026-09-01T00:00:00Z", "name": "old"},
            {"ror_id": "r1", "slug": "a2", "profile_url": "https://x.test/a", "as_of": "2026-09-29T00:00:00Z", "name": "new"}]
    (d / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    build = _load("build_official_contacts")
    out = tmp_path / "o.parquet"
    build.main(["--cache-root", str(tmp_path), "--out", str(out), "--opt-out", str(tmp_path / "n.csv"),
                "--tombstones", str(tmp_path / "t.csv")])
    import pandas as pd

    df = pd.read_parquet(out)
    assert list(df["name"]) == ["new"]


def test_tracked_seeds_hold_no_advisor_counts():
    header = (REPO / "data" / "seeds" / "institutions.csv").read_text().splitlines()[0].split(",")
    assert "tier" not in header and "advisor_count" not in header
    assert "source_revision" in header and "source_retrieved" in header


def test_service_dir_is_templated():
    svc = (REPO / "scripts" / "systemd" / "faculty-haul.service").read_text()
    inst = (REPO / "scripts" / "systemd" / "install-faculty-haul.sh").read_text()
    assert "@ATLAS_DIR@" in svc and ".wt-atlas-ops" in inst and "--now" not in inst
