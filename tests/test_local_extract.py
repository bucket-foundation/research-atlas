from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from atlas.users.extract.commoncrawl import CommonCrawl, needs_archive, parse_warc
from atlas.users.extract.local_llm import LocalExtractor, clean_text, ground, parse_reply
from atlas.users.extract.structured import deobfuscate, orcid_in, page_people
from atlas.users.extract.synthesize import Synthesizer, agreement, apply_selectors, slim_html

FIX = Path(__file__).parent / "fixtures" / "extract"
DOMAINS = ("example.edu",)
URL = "https://www.example.edu/people/ada-testperson"


def fx(name: str) -> str:
    return (FIX / name).read_text()


def test_structured_pass_reads_mailto_and_orcid():
    [p] = page_people(fx("profile.html"), URL, DOMAINS)
    assert p["name"] == "Ada Testperson" and p["email"] == "ada.testperson@example.edu"
    assert p["orcid"] == "0000-0002-1825-0097"


def test_structured_pass_rejects_prose_and_off_domain():
    html = "<html><h1>Bo Fixture</h1><p>Meet us at admissions.example.edu</p><a href='mailto:bo@gmail.com'>x</a></html>"
    [p] = page_people(html, URL, DOMAINS)
    assert p["email"] is None and "bo@gmail.com" in p["dropped_emails"]
    assert deobfuscate("Meet us at admissions.example.edu") == []
    assert orcid_in("no id here") is None


class Recorded:
    def __init__(self, replies):
        self.replies = list(replies)
        self.payloads = []

    def __call__(self, url, payload):
        self.payloads.append(payload)
        return self.replies.pop(0)


def extractor(replies):
    tags = json.loads(fx("ollama_tags.json"))
    return LocalExtractor(post=Recorded(replies), get=lambda url: tags, workers=2)


def test_ollama_client_uses_fixed_options_and_tags_output():
    reply = json.loads(fx("ollama_chat.json"))
    ex = extractor([reply])
    out = ex.extract(fx("profile.html"), URL)
    payload = ex.post.payloads[0]
    assert payload["model"] == "qwen3:4b" and payload["think"] is False and payload["stream"] is False
    assert payload["options"] == {"temperature": 0, "num_ctx": 4096}
    assert payload["format"]["required"][0] == "name"
    assert out["extractor"] == "qwen3:4b" and out["extractor_digest"] == "feedface0000"
    assert out["email"] == "ada.testperson@example.edu" and out["orcid"] == "0000-0002-1825-0097"
    assert out["departments"] == ["Placeholder Physics"]
    assert out["profile_url"] == URL


def test_ollama_retries_once_on_invalid_json():
    good = json.loads(fx("ollama_chat.json"))
    bad = {"message": {"content": "not json {"}}
    ex = extractor([bad, good])
    assert ex.extract(fx("profile.html"), URL)["name"] == "Ada Testperson"
    ex2 = extractor([bad, bad])
    assert ex2.extract(fx("profile.html"), URL) is None and len(ex2.post.payloads) == 2


def test_ungrounded_llm_values_are_dropped():
    parsed = parse_reply({"message": {"content": json.dumps({
        "name": "Invented Person", "title": "Dean", "departments": ["Nowhere"], "school": None,
        "research_areas": None, "email": "invented.person@example.edu", "orcid": "0000-0000-0000-0000",
        "profile_url": None})}})
    out = ground(parsed, "A page about the search box.", URL)
    assert out["name"] is None and out["email"] is None and out["orcid"] is None and out["departments"] == []


def test_clean_text_caps_tokens_and_keeps_mailto():
    text = clean_text(fx("profile.html"), max_tokens=10)
    assert len(text) <= 40
    assert "ada.testperson@example.edu" in clean_text(fx("profile.html"))


def test_extract_many_runs_concurrently():
    reply = json.loads(fx("ollama_chat.json"))
    ex = extractor([reply, reply, reply])
    outs = ex.extract_many([(fx("profile.html"), URL)] * 3)
    assert [o["name"] for o in outs] == ["Ada Testperson"] * 3


SELECTORS = {"name": "h1.person-name", "title": ".person-title", "departments": ".dept", "school": None,
             "research_areas": None, "email": "a.mail", "orcid": None}


def test_selectors_apply_and_score():
    got = apply_selectors(fx("profile.html"), SELECTORS)
    assert got["name"] == "Ada Testperson" and got["email"] == "ada.testperson@example.edu"
    assert got["departments"] == ["Placeholder Physics"]
    ref = {"name": "Ada Testperson", "title": "Professor of Synthetic Studies", "email": "ada.testperson@example.edu"}
    assert agreement([got], [ref]) == 1.0
    assert agreement([{"name": "Other"}], [ref]) == 0.0
    assert "<script" not in slim_html("<html><script>x</script><main>hi</main></html>")


def test_bad_selector_yields_nulls():
    assert apply_selectors(fx("profile.html"), {"name": "h1[[["})["name"] is None


def pages(n):
    html = fx("profile.html")
    return [html] * n


def test_synthesis_accepts_at_ninety_percent(tmp_path):
    reply = {"message": {"content": json.dumps(SELECTORS)}}
    s = Synthesizer(post=lambda url, payload: reply)
    ref = lambda h: {**page_people(h, URL, DOMAINS)[0], "orcid": None}  # noqa: E731
    out = s.run("www.example.edu", pages(13), ref, tmp_path / "queue.jsonl", tmp_path / "sel")
    assert out["accepted"] and (tmp_path / "sel" / "www.example.edu.json").exists()
    assert not (tmp_path / "queue.jsonl").exists()


def test_synthesis_escalates_on_disagreement_or_few_pages(tmp_path):
    reply = {"message": {"content": json.dumps({**SELECTORS, "name": "p.dept", "email": "h1"})}}
    s = Synthesizer(post=lambda url, payload: reply)
    ref = lambda h: page_people(h, URL, DOMAINS)[0]  # noqa: E731
    q = tmp_path / "queue.jsonl"
    assert not s.run("www.example.edu", pages(13), ref, q, tmp_path / "sel")["accepted"]
    assert not s.run("small.example.edu", pages(4), ref, q, tmp_path / "sel")["accepted"]
    rows = [json.loads(line) for line in q.read_text().splitlines()]
    assert [r["host"] for r in rows] == ["www.example.edu", "small.example.edu"]
    assert all(r["escalate_to"] == "claude" for r in rows)


def warc_blob(url: str, html: str) -> bytes:
    http = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n{html}".encode()
    head = (f"WARC/1.0\r\nWARC-Type: response\r\nWARC-Target-URI: {url}\r\n"
            f"WARC-Date: 2026-09-20T00:00:00Z\r\nContent-Length: {len(http)}\r\n\r\n").encode()
    return gzip.compress(head + http + b"\r\n\r\n")


def test_warc_record_parses():
    warc, status, html = parse_warc(warc_blob(URL, fx("profile.html")))
    assert warc["warc-target-uri"] == URL and status == 200 and "Ada Testperson" in html


def test_commoncrawl_index_then_byte_range():
    blob = warc_blob(URL, fx("profile.html"))
    rows = [{"url": URL, "status": "200", "mime": "text/html", "timestamp": "20260920",
             "filename": "crawl-data/x.warc.gz", "offset": "100", "length": str(len(blob))},
            {"url": URL, "status": "200", "mime": "text/html", "timestamp": "20260901",
             "filename": "crawl-data/old.warc.gz", "offset": "0", "length": "5"},
            {"url": "https://www.example.edu/news/a", "status": "200", "mime": "text/html",
             "filename": "y", "offset": "0", "length": "1"}]
    calls = []

    def get(url, headers):
        calls.append((url, headers))
        if "index.commoncrawl.org" in url:
            return 200, "\n".join(json.dumps(r) for r in rows).encode()
        return 206, blob

    cc = CommonCrawl(get=get, sleep=lambda s: None)
    import re
    [row] = cc.profile_urls("www.example.edu", re.compile(r"/people/[^/]+$"))
    assert row["filename"] == "crawl-data/x.warc.gz"
    rec = cc.record(row)
    assert rec.status == 200 and "Ada Testperson" in rec.html
    assert calls[0][0].startswith("https://index.commoncrawl.org/CC-MAIN-2026-39-index?url=www.example.edu/*")
    assert calls[1] == ("https://data.commoncrawl.org/crawl-data/x.warc.gz",
                        {"Range": f"bytes=100-{100 + len(blob) - 1}"})


@pytest.mark.parametrize("status,body,want", [
    (403, "", True), (503, "<title>Just a moment...</title>", True), (200, "<title>Just a moment</title>", False),
    (404, "", False),
])
def test_archive_fallback_trigger(status, body, want):
    assert needs_archive(status, body) is want


def test_extract_local_walks_cache_and_scores_gold(tmp_path):
    from scripts.extract_local import run, score

    pages = tmp_path / "0abcde123" / "pages"
    pages.mkdir(parents=True)
    (pages / "k1.json").write_text(json.dumps({"url": URL, "status": 200, "fetched_at": "2026-09-29T00:00:00Z",
                                               "sha256": "0"}))
    (pages / "k1.html.gz").write_bytes(gzip.compress(fx("profile.html").encode()))
    (pages / "k2.json").write_text(json.dumps({"url": "https://www.example.edu/people/nobody", "status": 200,
                                               "fetched_at": "t", "sha256": "1"}))
    (pages / "k2.html").write_text("<html><body><p>No profile here.</p></body></html>")
    reply = {"message": {"content": json.dumps({"name": None, "title": None, "departments": [], "school": None,
                                                "research_areas": None, "email": None, "orcid": None,
                                                "profile_url": None})}}
    recs, stats = run("0abcde123", tmp_path, use_llm=True, extractor=extractor([reply]))
    assert stats["pages"] == 1 and stats["structured_hits"] == 1 and stats["invalid_pages"] == 1
    [r] = recs
    assert r["extractor"] == "structured" and r["licence"] == "institution-copyright" and r["match_tier"] is None
    assert r["source_id"] == URL and r["as_of"] == "2026-09-29T00:00:00Z"
    gold = [{"name": "Ada Testperson", "email": "ada.testperson@example.edu", "profile_url": URL, "title": None}]
    s = score(recs, gold, {URL})
    assert s["email"] == {"precision": 1.0, "recall": 1.0, "predicted": 1, "gold": 1}


SEARCH_PAGE = ('<html><head><title>Search | Example University</title></head><body><h1>Search</h1>'
               '<script id="__NEXT_DATA__" type="application/json">'
               '{"props":{"pageProps":{"pageData":{"__typename":"Search"}}}}</script></body></html>')
LISTING_PAGE = ('<html><head><title>Faculty</title><script type="application/ld+json">[{"@type":"Person","name":"Ada One",'
                '"jobTitle":"Professor"},{"@type":"Person","name":"Bo Two","jobTitle":"Lecturer"}]</script></head></html>')


def test_profile_validity_rejects_search_and_listing_pages():
    from atlas.users.extract.structured import profile_invalid_reason
    assert profile_invalid_reason(SEARCH_PAGE, URL, DOMAINS) == "search page"
    assert profile_invalid_reason(LISTING_PAGE, URL, DOMAINS) == "listing page"
    assert profile_invalid_reason("<html><h1>Kim</h1></html>", URL, DOMAINS) == "no title"
    assert profile_invalid_reason(fx("profile.html"), URL, DOMAINS) is None


def test_validator_marks_cache_entry_at_write(tmp_path):
    from atlas.users.directories.base import PoliteFetcher, crawl
    from atlas.users.extract.structured import profile_validator
    from scripts.extract_local import is_profile, load_pages, run

    site = {"https://www.example.edu/robots.txt": (200, ""), URL: (200, SEARCH_PAGE)}
    f = PoliteFetcher(tmp_path / "0abcde123" / "pages", delay=0, get=lambda u: site.get(u, (404, "")),
                      sleep=lambda s: None, validator=profile_validator(is_profile, DOMAINS))
    page = f.fetch(URL)
    assert page.invalid == "search page" and f.invalid == 1
    [(meta, _)] = load_pages(tmp_path / "0abcde123")
    assert meta["invalid"] == "search page"
    assert f.fetch(URL).invalid == "search page"
    _, stats = run("0abcde123", tmp_path, use_llm=False)
    assert stats["invalid_pages"] == 1 and stats["invalid_reasons"] == {"search page": 1}

    class One:
        ror_id, domains = "0abcde123", DOMAINS

        def seeds(self, fetcher):
            return [URL]

        def parse(self, page):
            raise AssertionError("invalid pages are not parsed")

    _, cstats = crawl(One(), f)
    assert cstats["invalid_pages"] == 1


def test_structured_maps_next_data_positions_and_jsonld_school():
    from atlas.users.extract.structured import context_lines
    nd = ('<html><head><title>P</title></head><script id="__NEXT_DATA__" type="application/json">'
          '{"props":{"pageProps":{"pageData":{"__typename":"PageProfile","title":"Cy Synthetic",'
          '"email":"cy.synthetic@example.edu","school":{"title":"School of Pretend Science"},'
          '"positionsCollection":{"items":[{"department":{"title":"Imaginary Biology"}}]}}}}}</script></html>')
    [p] = page_people(nd, URL, DOMAINS)
    assert p["departments"] == ["Imaginary Biology"] and p["school"] == "School of Pretend Science"
    ld = ('<html><script type="application/ld+json">{"@type":"Person","name":"Di Madeup","affiliation":'
          '[{"@type":"CollegeOrUniversity","name":"College of Fake Arts"},{"@type":"Organization",'
          '"name":"Department of Mock Music"}]}</script></html>')
    [q] = page_people(ld, URL, DOMAINS)
    assert q["school"] == "College of Fake Arts" and q["departments"] == ["Department of Mock Music"]
    ctx = context_lines('<html><nav class="breadcrumb">Home / School of X / Dept of Y</nav>'
                        '<div class="profile-department">Dept of Y</div></html>')
    assert ctx == ["Home / School of X / Dept of Y", "Dept of Y"]
    assert "Page context:" in clean_text('<html><div id="school-name">School of X</div><p>body</p></html>')
