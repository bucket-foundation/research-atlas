"""Harvest PUBLIC professional contacts for the high-value researcher segment.

COMPLIANCE IS THE POINT OF THIS MODULE. It only ever returns an email that was
read verbatim from a **public professional source**, and it returns full
provenance with every one. It never constructs, guesses, or pattern-completes an
address. If no public email is found, the result is ``None`` -- honestly empty.

Two public sources, in priority order:

1. **EuropePMC / PubMed corresponding-author metadata** (best for biomed).
   PubMed records embed the corresponding author's email inside the author's
   affiliation string as ``"... Electronic address: x@y.edu."`` -- this is the
   author's own published contact, fully public, and citeable to the PMID. We
   match the email to a specific author by parsing that author's affiliation.

2. **ORCID public email** (any field). The ORCID public API returns an email
   ONLY when the researcher set it to public on their own profile -- explicit
   opt-in by definition.

A future ``labpage`` source (public faculty/lab page scrape) is allowed by the
schema but not implemented here.

Network politeness mirrors the connector base: descriptive UA, mailto, a small
delay, and an on-disk cache so a re-run resumes instead of re-querying.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

from atlas.schema import now_iso

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTACT_CACHE = REPO_ROOT / "data" / "raw" / "contacts"
MAILTO = "gianyrox@gmail.com"
UA = ("research-atlas-users/0.1 (+https://github.com/bucket-foundation/"
      "research-atlas; mailto:gianyrox@gmail.com)")

EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
ORCID_EMAIL = "https://pub.orcid.org/v3.0/{orcid}/email"

# "Electronic address: someone@uni.edu" (PubMed's corresponding-email convention)
_ELEC_RE = re.compile(
    r"[Ee]lectronic address:\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_ANY_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    return s


def _cache_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in key)
    CONTACT_CACHE.mkdir(parents=True, exist_ok=True)
    return CONTACT_CACHE / f"{safe}.json"


def _cached_get(sess, url, params, key, delay=0.2, timeout=30):
    """GET with on-disk JSON cache; returns parsed json or None."""
    cp = _cache_path(key)
    if cp.exists():
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except ValueError:
            pass
    try:
        r = sess.get(url, params=params, timeout=timeout)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    cp.write_text(json.dumps(data), encoding="utf-8")
    if delay:
        time.sleep(delay)
    return data


def _name_tokens(name: str | None) -> set[str]:
    if not name:
        return set()
    return {t.lower() for t in re.split(r"[\s,.]+", name) if len(t) > 1}


def europepmc_email(orcid: str | None, full_name: str | None,
                    sess=None, delay: float = 0.2) -> dict | None:
    """Best public corresponding-author email for a person from EuropePMC.

    Searches their most recent works (by ORCID when available, else by author
    name) and returns the email embedded in *their* affiliation's
    ``Electronic address:`` field. Matching to the right author is by ORCID id
    (authoritative) or by surname token overlap (conservative fallback).

    Returns a provenance dict or None. Never fabricates.
    """
    sess = sess or _session()
    if orcid:
        query = f"AUTHORID:{orcid} AND HAS_EMAIL:Y"
        key = f"epmc_orcid_{orcid}"
    elif full_name:
        # surname-first PubMed name form is unreliable; require email present
        query = f'AUTH:"{full_name}" AND HAS_EMAIL:Y'
        key = f"epmc_name_{full_name}"
    else:
        return None

    params = {
        "query": query,
        "resultType": "core",
        "format": "json",
        "pageSize": 25,
        "sort": "P_PDATE_D desc",
        "email": MAILTO,
    }
    data = _cached_get(sess, EPMC_SEARCH, params, key, delay=delay)
    if not data:
        return None
    results = (data.get("resultList") or {}).get("result") or []
    want_tokens = _name_tokens(full_name)
    for r in results:
        pmid = r.get("pmid") or r.get("id")
        src_url = (f"https://europepmc.org/article/MED/{pmid}" if pmid
                   else None)
        authors = (r.get("authorList") or {}).get("author") or []
        for a in authors:
            a_orcid = (a.get("authorId") or {}).get("value") \
                if isinstance(a.get("authorId"), dict) else a.get("authorId")
            affs = a.get("authorAffiliationDetailsList") or {}
            aff_list = affs.get("authorAffiliation") or []
            aff_text = " ".join(x.get("affiliation", "") for x in aff_list)
            m = _ELEC_RE.search(aff_text)
            if not m:
                continue
            email = m.group(1).rstrip(".")
            # authoritative match: ORCID id on the author equals our person
            matched = False
            if orcid and a_orcid and orcid in str(a_orcid):
                matched = True
            else:
                # conservative surname-token fallback
                a_tokens = _name_tokens(a.get("fullName"))
                if want_tokens and (want_tokens & a_tokens):
                    matched = True
            if matched:
                return {
                    "email": email,
                    "email_source": "europepmc",
                    "email_source_url": src_url,
                    "email_as_of": now_iso(),
                    "email_method": "corresponding-author-metadata",
                }
    return None


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
    if not chosen:
        return None
    return {
        "email": chosen,
        "email_source": "orcid",
        "email_source_url": f"https://orcid.org/{orcid}",
        "email_as_of": now_iso(),
        "email_method": "orcid-public",
    }


def find_public_contact(orcid: str | None, full_name: str | None,
                        field_slug: str | None = None,
                        sess=None, delay: float = 0.2) -> dict | None:
    """Best public professional contact for a person, with provenance, or None.

    Source priority: ORCID public email (explicit opt-in) first, then EuropePMC
    corresponding-author metadata. Biomed researchers are far more likely to be
    found in EuropePMC; non-biomed fields lean on ORCID. Either way: public
    source + provenance, or None. Never fabricated.
    """
    sess = sess or _session()
    hit = orcid_public_email(orcid, sess=sess, delay=delay)
    if hit:
        return hit
    return europepmc_email(orcid, full_name, sess=sess, delay=delay)
