import importlib.util
import json
import threading
from pathlib import Path

from atlas.users.directories.base import FacultyRecord, Page, accept_email
from atlas.users.haul import (
    DiskBudget, HaulFetcher, HaulState, RobotsCache, RorIndex, TokenBucket, advisor_tiers, parse_wiki_carnegie,
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
                           get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r: FakeAdapter(r["ror_id"]))
    assert code == 0 and state.institutions["r1"]["status"] == "done"
    lines = (tmp_path / "r1" / "records.jsonl").read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["email_source"] == "official_directory"
    first = json.loads(lines[0])
    assert first["licence"] == "institution-copyright" and first["match_tier"] is None
    assert first["source_url"] == first["profile_url"] and first["as_of"] == first["fetched_at"]
    assert list((tmp_path / "r1" / "pages").glob("*.html.gz"))
    assert "done=1" in status_line(state, tmp_path) and "emails=3" in status_line(state, tmp_path)


def test_resume_from_state_file(tmp_path):
    (tmp_path / "_state.json").write_text(json.dumps({"institutions": {
        "r1": {"name": "r1", "status": "done", "profiles": 4, "emails": 4},
        "r2": {"name": "r2", "status": "running", "profiles": 0, "emails": 0},
        "r3": {"name": "r3", "status": "pending", "profiles": 0, "emails": 0}}}))
    seen = []

    def factory(r):
        seen.append(r["ror_id"])
        return FakeAdapter(r["ror_id"])

    code, state = haul.run(rows("r1", "r2", "r3"), cache_root=tmp_path, workers=1, max_pages=50,
                           budget_bytes=10**9, get=fake_get, bucket=fast_bucket(), adapter_factory=factory)
    assert sorted(seen) == ["r2", "r3"]
    assert {k: v["status"] for k, v in state.institutions.items()} == {"r1": "done", "r2": "done", "r3": "done"}
    assert HaulState.load(tmp_path / "_state.json").counts()["profiles"] == 10


def test_disk_budget_stops_cleanly(tmp_path):
    code, state = haul.run(rows("r1", "r2"), cache_root=tmp_path, workers=1, max_pages=500, budget_bytes=1500,
                           get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r: FakeAdapter(r["ror_id"], n=20))
    assert code == 0
    assert state.institutions["r1"]["status"] == "pending" and state.institutions["r1"]["error"] == "disk_budget"
    assert state.institutions["r2"]["status"] == "pending"
    assert len(list(tmp_path.rglob("*.html.gz"))) == 1
    code2, _ = haul.run(rows("r1"), cache_root=tmp_path, workers=1, max_pages=500, budget_bytes=1500,
                        get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r: FakeAdapter(r["ror_id"]))
    assert code2 == 0 and len(list(tmp_path.rglob("*.html.gz"))) == 1


def test_zero_yield_exit_code(tmp_path):
    code, state = haul.run(rows("r1"), cache_root=tmp_path, workers=1, max_pages=50, budget_bytes=10**9,
                           get=fake_get, bucket=fast_bucket(),
                           adapter_factory=lambda r: FakeAdapter(r["ror_id"], with_email=False))
    assert code == 2 and state.institutions["r1"]["status"] == "zero_yield"


def test_page_cap_and_robots(tmp_path):
    class Private(FakeAdapter):
        def seeds(self, fetcher):
            return [f"https://www.{DOMAIN}/private/x"] + super().seeds(fetcher)

    code, state = haul.run(rows("r1"), cache_root=tmp_path, workers=1, max_pages=3, budget_bytes=10**9,
                           get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r: Private(r["ror_id"], n=10))
    rec = state.institutions["r1"]
    assert rec["robots_denied"] == 1 and rec["capped"] and rec["profiles"] == 2


def test_no_adapter_marks_failed(tmp_path):
    code, state = haul.run(rows("r1"), cache_root=tmp_path, workers=1, max_pages=5, budget_bytes=10**9,
                           get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r: None)
    assert state.institutions["r1"]["status"] == "failed" and state.institutions["r1"]["error"] == "no_adapter"


def test_fetcher_cache_skips_refetch(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return fake_get(url)

    b = fast_bucket()
    mk = lambda: HaulFetcher(tmp_path, bucket=b, robots=RobotsCache(get, b), budget=DiskBudget(tmp_path, 10**9), get=get)
    mk().fetch(f"https://www.{DOMAIN}/a")
    mk().fetch(f"https://www.{DOMAIN}/a")
    assert [c for c in calls if not c.endswith("robots.txt")] == [f"https://www.{DOMAIN}/a"]


def test_fold_into_private_parquet(tmp_path):
    import pytest

    pytest.importorskip("pandas")
    haul.run(rows("r1"), cache_root=tmp_path, workers=1, max_pages=50, budget_bytes=10**9, get=fake_get,
             bucket=fast_bucket(), adapter_factory=lambda r: FakeAdapter(r["ror_id"]))
    build = _load("build_official_contacts")
    out = tmp_path / "contacts_official.parquet"
    assert build.main(["--cache-root", str(tmp_path), "--out", str(out)]) == 0
    import pandas as pd

    df = pd.read_parquet(out)
    assert len(df) == 3 and df["email"].notna().all()
    assert set(df["licence"]) == {"institution-copyright"} and df["match_tier"].isna().all()
    assert "contacts_official.parquet" in (REPO / ".gitignore").read_text()


def test_opt_out_skips_at_crawl_and_fold(tmp_path):
    import csv

    from atlas.users.haul import OptOut

    opt = tmp_path / "opt_out.csv"
    with opt.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["orcid", "email", "name", "ror_id", "reason", "as_of"])
        w.writeheader()
        w.writerow({"email": email("p0").upper(), "reason": "asked", "as_of": "2026-09-29"})
        w.writerow({"name": "  test PERSON ", "ror_id": "https://ror.org/r2", "reason": "asked"})
        w.writerow({"orcid": "https://orcid.org/0000-0000-0000-0001", "reason": "asked"})
    oo = OptOut.load(opt)
    assert oo.matches({"orcid": "0000-0000-0000-0001"})
    assert not oo.matches({"name": "Test Person", "ror_id": "r9"})
    cache = tmp_path / "cache"
    code, state = haul.run(rows("r1", "r2"), cache_root=cache, workers=1, max_pages=50, budget_bytes=10**9,
                           get=fake_get, bucket=fast_bucket(), adapter_factory=lambda r: FakeAdapter(r["ror_id"]),
                           opt_out=oo)
    assert state.institutions["r1"]["opt_out_skipped"] == 1 and state.institutions["r1"]["profiles"] == 2
    assert state.institutions["r2"]["opt_out_skipped"] == 3 and state.institutions["r2"]["status"] == "zero_yield"
    assert "opt_out_skipped=4" in status_line(state, cache)
    (cache / "r1" / "records.jsonl").open("a").write(json.dumps({"slug": "late", "name": "X", "ror_id": "r1",
                                                                  "email": email("p0")}) + "\n")
    build = _load("build_official_contacts")
    got, dropped = build.fold(cache, oo)
    assert dropped == 1 and len(got) == 2


def test_private_dir_is_gitignored():
    import subprocess

    out = subprocess.run(["git", "check-ignore", "-q", "data/private/opt_out.csv"], cwd=REPO)
    assert out.returncode == 0
