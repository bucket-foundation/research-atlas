"""OpenAlex subfield corpus -- a coherent, complete-citation slice of one field.

Gian's original used an arXiv ``hep-ph`` slice and dropped every reference whose
target was not also in arXiv. That made the citation graph badly incomplete and
the in-citation counts an undercount, which is why his PageRank came out nearly
uniform. We fix that here by pulling a *coherent OpenAlex subfield* and keeping
both directions of the citation signal:

- ``referenced_works`` -- the out-references of every work (the edges Gian had,
  but now resolvable because the target ids are global OpenAlex ids, not arXiv
  ids that may be missing);
- ``cited_by_count``   -- OpenAlex's GLOBAL in-citation count for every work,
  which fixes the undercount directly (it counts citations from *outside* the
  slice too);
- the **in-corpus** citation edges (out-references whose target is also in the
  corpus) form a graph that is complete by construction *within the corpus* --
  no edge silently dropped.

Default corpus: OpenAlex subfield **3106 = Nuclear & High Energy Physics**
(~255k articles 2015-2024), the direct analogue of Gian's ``hep-ph`` but with a
resolvable, complete citation graph.

Polite + idempotent (same contract as :mod:`atlas.connectors`):
- ``?mailto=`` on every call;
- cursor paging, ``per-page=200``, ``select=`` only the fields we need;
- every page cached under ``data/raw/ranking/`` keyed by subfield+window+cursor,
  so a re-run resumes from disk instead of re-fetching.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Iterator

from atlas.connectors.base import DATA_RAW, HttpClient

OPENALEX_WORKS = "https://api.openalex.org/works"
MAILTO = "gianyrox@gmail.com"
PER_PAGE = 200

# The fields we need -- keep the payload small and polite. referenced_works is
# the out-edge list; cited_by_count is the global in-citation count.
SELECT = ",".join([
    "id", "title", "publication_year", "publication_date", "type",
    "cited_by_count", "referenced_works", "abstract_inverted_index",
    "primary_topic",
])

# Default coherent subfield: Nuclear & High Energy Physics (Gian used hep-ph).
DEFAULT_SUBFIELD = "3106"


def short_oa_id(url: str | None) -> str | None:
    """``https://openalex.org/W123`` -> ``W123``; passthrough for bare ids."""
    if not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1]


def reconstruct_abstract(inv: dict | None) -> str | None:
    """Rebuild plain-text abstract from OpenAlex's inverted index.

    OpenAlex ships abstracts as ``{word: [positions...]}`` (for copyright
    reasons). We invert it back to running text -- this is the title+abstract
    feed for every embedding method (TF-IDF, word2vec, transformer).
    """
    if not inv:
        return None
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    if not positions:
        return None
    positions.sort()
    return " ".join(w for _, w in positions)


class CorpusConnector:
    """Fetch a coherent OpenAlex subfield slice, cached + resumable."""

    source = "ranking"
    delay = 0.12  # polite pool tolerates ~10 req/s; stay well under

    def __init__(self, raw_dir: Path | None = None):
        self.raw_dir = (raw_dir or DATA_RAW) / self.source
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.http = HttpClient(user_agent=(
            "research-atlas-ranking/0.1 "
            "(+https://github.com/bucket-foundation/research-atlas; "
            f"mailto:{MAILTO})"), delay=self.delay)

    # ----- raw cache ------------------------------------------------------- #

    def _raw_path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in key)
        return self.raw_dir / f"{safe}.json"

    def _load(self, key: str):
        import json
        p = self._raw_path(key)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    def _cache(self, key: str, payload) -> None:
        import json
        self._raw_path(key).write_text(json.dumps(payload), encoding="utf-8")

    # ----- fetch ----------------------------------------------------------- #

    def fetch(self, subfield: str = DEFAULT_SUBFIELD,
              from_date: str = "2015-01-01", to_date: str = "2024-12-31",
              max_pages: int = 0, cache_only: bool = False,
              work_type: str = "article") -> Iterable[dict]:
        """Yield raw OpenAlex work pages for a subfield window, cursor-paged.

        ``max_pages`` of 0 means all pages. Each page is cached; a re-run with
        the same args resumes from disk. ``cache_only`` replays only cached
        pages and stops at the first uncached page (no network).
        """
        flt = (f"primary_topic.subfield.id:{subfield},"
               f"from_publication_date:{from_date},"
               f"to_publication_date:{to_date}")
        if work_type:
            flt += f",type:{work_type}"
        cursor = "*"
        page_no = 0
        while cursor:
            chash = hashlib.sha1(cursor.encode()).hexdigest()[:12]
            key = f"sf{subfield}_{from_date}_{to_date}_{chash}"
            page = self._load(key)
            if page is None:
                if cache_only:
                    break
                params = {
                    "filter": flt, "per-page": PER_PAGE, "cursor": cursor,
                    "select": SELECT, "mailto": MAILTO,
                }
                resp = self.http.get(OPENALEX_WORKS, params=params)
                if resp is None:
                    break
                try:
                    page = resp.json()
                except ValueError:
                    break
                self._cache(key, page)
            results = page.get("results") or []
            if not results:
                break
            yield page
            page_no += 1
            cursor = (page.get("meta") or {}).get("next_cursor")
            if max_pages and page_no >= max_pages:
                break

    def iter_cached(self) -> Iterator[dict]:
        """Yield every cached raw page (re-normalize without re-fetching)."""
        import json
        for p in sorted(self.raw_dir.glob("*.json")):
            yield json.loads(p.read_text(encoding="utf-8"))

    # ----- normalize ------------------------------------------------------- #

    def normalize(self, raw_pages: Iterable[dict]) -> Iterator[dict]:
        """Raw pages -> flat work records (the corpus rows).

        Each record:
        ``{work_id, title, abstract, text, year, type, cited_by_count,
           topic, refs[]}`` where ``refs`` is the list of out-reference
        OpenAlex ids (global -- resolvable, unlike Gian's arXiv-only ids).
        """
        seen: set[str] = set()
        for page in raw_pages:
            for w in (page.get("results") or []):
                wid = short_oa_id(w.get("id"))
                if not wid or wid in seen:
                    continue
                seen.add(wid)
                title = w.get("title") or ""
                abstract = reconstruct_abstract(
                    w.get("abstract_inverted_index"))
                refs = [short_oa_id(r) for r in (w.get("referenced_works") or [])]
                refs = [r for r in refs if r]
                topic = (w.get("primary_topic") or {})
                yield {
                    "work_id": wid,
                    "title": title,
                    "abstract": abstract,
                    "text": (title + ". " + (abstract or "")).strip(),
                    "year": w.get("publication_year"),
                    "type": w.get("type"),
                    "cited_by_count": w.get("cited_by_count") or 0,
                    "topic": topic.get("display_name"),
                    "topic_id": short_oa_id(topic.get("id")),
                    "refs": refs,
                }
