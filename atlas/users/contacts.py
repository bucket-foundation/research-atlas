"""Harvest PUBLIC professional contacts for the high-value researcher segment.

COMPLIANCE IS THE POINT OF THIS MODULE. It only ever returns an email that was
read verbatim from a **public professional source**, and it returns full
provenance with every one. It never constructs, guesses, or pattern-completes an
address. If no public email is found, the result is ``None`` -- honestly empty.

Four public sources, each fully provenanced (``email_source`` +
``email_source_url`` + ``email_as_of`` + ``email_method``):

1. **EuropePMC / PubMed corresponding-author metadata** (highest yield for
   biomed). PubMed records embed the corresponding author's email inside the
   author's affiliation string as ``"... Electronic address: x@y.edu."`` -- this
   is the author's own published contact, fully public, and citeable to the
   PMID. We match the email to a specific author by ORCID id (authoritative) or
   by surname-token overlap (conservative fallback), scanning the author's
   *recent works* across several result pages until we find one where THEIR
   affiliation carries the email.

2. **Crossref public author metadata.** Crossref author records occasionally
   carry an email in the author object or in an ``affiliation[].name`` string
   (publishers sometimes deposit the corresponding-author email there). We read
   it only when it is literally present and matches the author. Public scholarly
   metadata, citeable to the DOI.

3. **ORCID public email.** The ORCID public API returns an email ONLY when the
   researcher set it to public on their own profile -- explicit opt-in by
   definition.

4. **Lab / faculty page** (``labpage``, best-effort, conservative). Reads the
   researcher's own public ``researcher-urls`` from their ORCID profile, fetches
   each public web page, and extracts an email **literally present** on that
   page (a ``mailto:`` link or visible address). Public web pages the
   researcher/institution publishes; citeable to the exact URL. Never guessed.

Network politeness mirrors the connector base: descriptive UA, mailto, a small
delay, honored ``Retry-After`` on 429/503, and an on-disk cache so a re-run
resumes instead of re-querying.
"""

from __future__ import annotations

import json
import re
import time
from html import unescape
from pathlib import Path

import requests

from atlas.schema import now_iso

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTACT_CACHE = REPO_ROOT / "data" / "raw" / "contacts"
MAILTO = "gianyrox@gmail.com"
UA = ("research-atlas-users/0.2 (+https://github.com/bucket-foundation/"
      "research-atlas; mailto:gianyrox@gmail.com)")

EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
ORCID_EMAIL = "https://pub.orcid.org/v3.0/{orcid}/email"
ORCID_URLS = "https://pub.orcid.org/v3.0/{orcid}/researcher-urls"
CROSSREF_WORKS = "https://api.crossref.org/works"

# How many EuropePMC result pages (of pageSize each) to scan per person looking
# for THEIR corresponding-author email before giving up. Politeness cap.
EPMC_PAGE_SIZE = 50         # one page already covers most authors' recent works
EPMC_MAX_PAGES = 2          # up to ~100 recent works scanned per person
CROSSREF_ROWS = 20          # recent works scanned per person on Crossref
LABPAGE_MAX_URLS = 2        # public homepage URLs fetched per person
LABPAGE_MAX_BYTES = 300_000  # cap page body we scan for an email

# "Electronic address: someone@uni.edu" (PubMed's corresponding-email convention)
_ELEC_RE = re.compile(
    r"[Ee]lectronic address:\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_ANY_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# emails inside a mailto: href on a public page
_MAILTO_RE = re.compile(
    r"mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", re.IGNORECASE)

# obvious non-personal / publisher / boilerplate addresses we never store
_EMAIL_DENY = re.compile(
    r"(?:no-?reply|do-?not-?reply|webmaster|postmaster|info|admin|support|"
    r"help|contact|sales|press|media|privacy|abuse|example\.(?:com|org|net))",
    re.IGNORECASE)
_DENY_DOMAINS = {
    "sentry.io", "sentry-next.wixpress.com", "example.com", "example.org",
    "domain.com", "email.com", "wixpress.com",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    return s


def _cache_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in key)
    CONTACT_CACHE.mkdir(parents=True, exist_ok=True)
    return CONTACT_CACHE / f"{safe}.json"


def _sleep_for_retry(resp, default: float) -> float:
    """Return how long to sleep, honoring Retry-After but capping absurd values."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            secs = float(ra)
            return max(0.0, min(secs, 120.0))
        except ValueError:
            pass
    return default


def _cached_get(sess, url, params, key, delay=0.2, timeout=30,
                want_json=True):
    """GET with on-disk cache. Honors Retry-After on 429/503.

    Returns parsed json (or, when ``want_json`` is False, the raw text wrapped
    as ``{"_text": ...}``) or None. The cache is the resume mechanism: a hit is
    never re-fetched, so re-runs are cheap and polite.
    """
    cp = _cache_path(key)
    if cp.exists():
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            return data
        except ValueError:
            pass
    for attempt in range(3):
        try:
            r = sess.get(url, params=params, timeout=timeout)
        except requests.RequestException:
            return None
        if r.status_code in (429, 503):
            wait = _sleep_for_retry(r, delay * 4)
            if attempt < 2:
                time.sleep(wait)
                continue
            return None
        if r.status_code != 200:
            return None
        if want_json:
            try:
                data = r.json()
            except ValueError:
                return None
        else:
            text = r.text or ""
            if len(text) > LABPAGE_MAX_BYTES:
                text = text[:LABPAGE_MAX_BYTES]
            data = {"_text": text, "_url": r.url}
        cp.write_text(json.dumps(data), encoding="utf-8")
        if delay:
            time.sleep(delay)
        return data
    return None


def _name_tokens(name: str | None) -> set[str]:
    if not name:
        return set()
    return {t.lower() for t in re.split(r"[\s,.]+", name) if len(t) > 1}


def _is_acceptable_email(email: str | None) -> bool:
    """Reject obvious non-personal / boilerplate / placeholder addresses.

    This is an anti-noise filter, NOT a fabrication path: it only ever removes
    candidates, never invents one. A real personal address that happens to look
    odd is kept; only clearly institutional/automated mailboxes are dropped.
    """
    if not email:
        return False
    email = email.strip().rstrip(".").lower()
    if "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if domain in _DENY_DOMAINS:
        return False
    if _EMAIL_DENY.search(local):
        return False
    if email.endswith((".png", ".jpg", ".gif", ".svg", ".webp")):
        return False
    return True


def _prov(email: str, source: str, url: str | None, method: str) -> dict:
    return {
        "email": email.strip().rstrip("."),
        "email_source": source,
        "email_source_url": url,
        "email_as_of": now_iso(),
        "email_method": method,
    }


def _author_matches(a: dict, orcid: str | None, want_tokens: set[str]) -> bool:
    """Is this EuropePMC/Crossref author OUR person? ORCID authoritative, else
    a conservative surname-token overlap fallback."""
    a_orcid = a.get("authorId")
    if isinstance(a_orcid, dict):
        a_orcid = a_orcid.get("value")
    # Crossref ORCID lives under 'ORCID' as a full URL
    cr_orcid = a.get("ORCID")
    if orcid:
        if a_orcid and orcid in str(a_orcid):
            return True
        if cr_orcid and orcid in str(cr_orcid):
            return True
        # if we have an ORCID but it doesn't match, fall through to name only
        # when the record has no ORCID at all on this author
        if a_orcid or cr_orcid:
            return False
    a_name = a.get("fullName")
    if not a_name:
        given = a.get("given")
        family = a.get("family")
        a_name = " ".join(x for x in (given, family) if x)
    a_tokens = _name_tokens(a_name)
    return bool(want_tokens and (want_tokens & a_tokens))


# --------------------------------------------------------------------------- #
# 1. EuropePMC / PubMed corresponding-author metadata
# --------------------------------------------------------------------------- #

def europepmc_email(orcid: str | None, full_name: str | None,
                    sess=None, delay: float = 0.2) -> dict | None:
    """Best public corresponding-author email for a person from EuropePMC.

    Searches their works (by ORCID when available, else by author name),
    scanning up to ``EPMC_MAX_PAGES`` pages of recent results, and returns the
    email embedded in *their* affiliation's ``Electronic address:`` field.
    Matching to the right author is by ORCID id (authoritative) or by surname
    token overlap (conservative fallback).

    Returns a provenance dict or None. Never fabricates.
    """
    sess = sess or _session()
    if orcid:
        # quoted AUTHORID is the documented EuropePMC form. (The old
        # "AND HAS_EMAIL:Y" filter is NOT a valid field and zeroed every result
        # set -- that bug capped a 1M-corpus enrichment at 184 hits.)
        query = f'AUTHORID:"{orcid}"'
        base_key = f"epmc_orcid_{orcid}"
    elif full_name:
        query = f'AUTH:"{full_name}"'
        base_key = f"epmc_name_{full_name}"
    else:
        return None

    want_tokens = _name_tokens(full_name)
    cursor = "*"
    for page in range(EPMC_MAX_PAGES):
        params = {
            "query": query,
            "resultType": "core",
            "format": "json",
            "pageSize": EPMC_PAGE_SIZE,
            "sort": "P_PDATE_D desc",
            "cursorMark": cursor,
            "email": MAILTO,
        }
        # page 0 keeps the legacy cache key so prior cache (now repaired by the
        # query fix on refetch) and tests stay aligned; later pages get a suffix.
        key = base_key if page == 0 else f"{base_key}_p{page}"
        data = _cached_get(sess, EPMC_SEARCH, params, key, delay=delay)
        if not data:
            return None
        results = (data.get("resultList") or {}).get("result") or []
        if not results:
            return None
        for r in results:
            pmid = r.get("pmid") or r.get("id")
            src_url = (f"https://europepmc.org/article/MED/{pmid}" if pmid
                       else None)
            authors = (r.get("authorList") or {}).get("author") or []
            for a in authors:
                affs = a.get("authorAffiliationDetailsList") or {}
                aff_list = affs.get("authorAffiliation") or []
                aff_text = " ".join(x.get("affiliation", "") for x in aff_list)
                m = _ELEC_RE.search(aff_text)
                if not m:
                    continue
                email = m.group(1).rstrip(".")
                if not _is_acceptable_email(email):
                    continue
                if _author_matches(a, orcid, want_tokens):
                    return _prov(email, "europepmc", src_url,
                                 "corresponding-author-metadata")
        nxt = data.get("nextCursorMark")
        if not nxt or nxt == cursor:
            break
        cursor = nxt
    return None


# --------------------------------------------------------------------------- #
# 2. Crossref public author metadata
# --------------------------------------------------------------------------- #

def crossref_email(orcid: str | None, full_name: str | None,
                   sess=None, delay: float = 0.2) -> dict | None:
    """Public author email from Crossref metadata, when literally present.

    Crossref author objects sometimes carry an email directly, or inside an
    ``affiliation[].name`` string deposited by the publisher. We read it only
    when present and matchable to our author. Citeable to the DOI. Never
    fabricated; returns None when absent.
    """
    sess = sess or _session()
    if orcid:
        params = {"filter": f"orcid:{orcid}", "rows": CROSSREF_ROWS,
                  "select": "DOI,author", "sort": "published",
                  "order": "desc", "mailto": MAILTO}
        key = f"crossref_orcid_{orcid}"
    elif full_name:
        params = {"query.author": full_name, "rows": CROSSREF_ROWS,
                  "select": "DOI,author", "mailto": MAILTO}
        key = f"crossref_name_{full_name}"
    else:
        return None

    data = _cached_get(sess, CROSSREF_WORKS, params, key, delay=delay)
    if not data:
        return None
    items = ((data.get("message") or {}).get("items")) or []
    want_tokens = _name_tokens(full_name)
    for it in items:
        doi = it.get("DOI")
        src_url = f"https://doi.org/{doi}" if doi else None
        for a in (it.get("author") or []):
            if not _author_matches(a, orcid, want_tokens):
                continue
            # author-level email (rare but authoritative)
            cands: list[str] = []
            ae = a.get("email")
            if isinstance(ae, str):
                cands.append(ae)
            elif isinstance(ae, list):
                cands.extend(str(x) for x in ae)
            # email embedded in affiliation names (publisher deposits)
            for aff in (a.get("affiliation") or []):
                nm = aff.get("name") if isinstance(aff, dict) else None
                if nm:
                    cands.extend(_ANY_EMAIL_RE.findall(nm))
            for email in cands:
                email = email.strip().rstrip(".")
                if _is_acceptable_email(email):
                    return _prov(email, "crossref", src_url,
                                 "crossref-author-metadata")
    return None


# --------------------------------------------------------------------------- #
# 3. ORCID public email
# --------------------------------------------------------------------------- #

def orcid_public_email(orcid: str | None, sess=None,
                       delay: float = 0.2) -> dict | None:
    """Public email from the researcher's own ORCID profile, or None.

    The ORCID public API returns an email only if the researcher set it public
    -- an explicit opt-in. We take the primary public email if present.
    """
    if not orcid:
        return None
    sess = sess or _session()
    data = _cached_get(sess, ORCID_EMAIL.format(orcid=orcid), None,
                       f"orcid_{orcid}", delay=delay)
    if not data:
        return None
    emails = data.get("email") or []
    chosen = None
    for e in emails:
        if not e.get("email"):
            continue
        if e.get("primary"):
            chosen = e["email"]
            break
        chosen = chosen or e["email"]
    if not chosen or not _is_acceptable_email(chosen):
        return None
    return _prov(chosen, "orcid", f"https://orcid.org/{orcid}", "orcid-public")


# --------------------------------------------------------------------------- #
# 4. Lab / faculty page (best-effort, conservative, public pages only)
# --------------------------------------------------------------------------- #

def labpage_email(orcid: str | None, full_name: str | None,
                  sess=None, delay: float = 0.2) -> dict | None:
    """Email literally present on a researcher's own public homepage.

    Reads the public ``researcher-urls`` the researcher listed on their ORCID
    profile (their own declared homepages/lab pages), fetches each, and extracts
    an email that is literally on the page (a ``mailto:`` link preferred, else a
    visible address). Conservative: public pages only, capped count + size,
    boilerplate addresses filtered, never guessed. Citeable to the exact URL.
    """
    if not orcid:
        return None
    sess = sess or _session()
    data = _cached_get(sess, ORCID_URLS.format(orcid=orcid), None,
                       f"orcidurls_{orcid}", delay=delay)
    if not data:
        return None
    urls = []
    for ru in (data.get("researcher-url") or []):
        u = (ru.get("url") or {}).get("value") if isinstance(ru.get("url"), dict) else None
        if u and u.lower().startswith(("http://", "https://")):
            urls.append(u)
    want_tokens = _name_tokens(full_name)
    for u in urls[:LABPAGE_MAX_URLS]:
        key = "labpage_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[:80] + f"_{orcid}"
        page = _cached_get(sess, u, None, key, delay=delay, want_json=False,
                           timeout=20)
        if not page:
            continue
        html = page.get("_text", "")
        final_url = page.get("_url", u)
        # mailto: links first (an explicitly published contact link)
        cands = list(_MAILTO_RE.findall(html))
        # then visible addresses on the page
        cands += _ANY_EMAIL_RE.findall(html)
        for email in cands:
            email = unescape(email).strip().rstrip(".")
            if not _is_acceptable_email(email):
                continue
            # conservative match: if we know the surname, prefer an address whose
            # local part shares a name token (reduces grabbing a colleague's).
            local = email.split("@", 1)[0].lower()
            if want_tokens and not any(t[:4] in local or local[:4] in t
                                       for t in want_tokens if len(t) >= 3):
                # no name signal in the local part -> only accept a mailto link
                if email not in _MAILTO_RE.findall(html):
                    continue
            return _prov(email, "labpage", final_url, "public-homepage")
    return None


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

# Default source priority. Biomed researchers are far more likely to be found in
# EuropePMC corresponding-author metadata, so the pipeline can flip this to
# epmc-first per field. ORCID public email is an explicit opt-in (highest trust)
# so it leads by default; labpage is the conservative last resort.
DEFAULT_PRIORITY = ("orcid", "europepmc", "crossref", "labpage")
EPMC_FIRST_PRIORITY = ("europepmc", "orcid", "crossref", "labpage")

_SOURCE_FUNCS = {
    "orcid": lambda orcid, name, sess, delay: orcid_public_email(
        orcid, sess=sess, delay=delay),
    "europepmc": lambda orcid, name, sess, delay: europepmc_email(
        orcid, name, sess=sess, delay=delay),
    "crossref": lambda orcid, name, sess, delay: crossref_email(
        orcid, name, sess=sess, delay=delay),
    "labpage": lambda orcid, name, sess, delay: labpage_email(
        orcid, name, sess=sess, delay=delay),
}


def find_public_contact(orcid: str | None, full_name: str | None,
                        field_slug: str | None = None,
                        sess=None, delay: float = 0.2,
                        priority: "tuple[str, ...] | None" = None,
                        epmc_first: bool = False,
                        sources: "set[str] | None" = None) -> dict | None:
    """Best public professional contact for a person, with provenance, or None.

    Tries each source in ``priority`` order and returns the first hit. Biomed
    authors should pass ``epmc_first=True`` (or the pipeline's ``--epmc-first``)
    so the highest-yield source for them leads. Either way: public source +
    provenance, or None. Never fabricated.

    ``sources`` optionally restricts which sources are attempted (e.g.
    ``{"orcid"}`` for an ORCID-only pull).
    """
    sess = sess or _session()
    if priority is None:
        priority = EPMC_FIRST_PRIORITY if epmc_first else DEFAULT_PRIORITY
    for name in priority:
        if sources is not None and name not in sources:
            continue
        fn = _SOURCE_FUNCS.get(name)
        if not fn:
            continue
        hit = fn(orcid, full_name, sess, delay)
        if hit:
            return hit
    return None
