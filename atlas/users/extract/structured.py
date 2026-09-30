from __future__ import annotations

import html as htmllib
import re
from collections.abc import Iterable
from urllib.parse import urljoin

from atlas.users.directories.base import EMAIL_RE, email_domain_ok, next_data
from atlas.users.directories.generic import (
    Node,
    Person,
    decode_cfemail,
    deobfuscate,
    is_role_mailbox,
    jsonld_people,
    mailto_emails,
    microdata_people,
    next_data_people,
    page_name,
    parse_html,
    pick_email,
)

__all__ = ["EMAIL_RE", "Node", "decode_cfemail", "deobfuscate", "email_domain_ok", "is_role_mailbox",
           "mailto_emails", "page_people", "parse_html", "profile_invalid_reason", "context_lines"]


def page_people(page_text: str, url: str, domains: Iterable[str], profile: bool = True) -> list[dict]:
    root = parse_html(page_text)
    people = jsonld_people(page_text) + microdata_people(root) + next_data_people(page_text)
    if profile and not people and (name := page_name(root)):
        people = [Person(name=name)]
    page_emails = mailto_emails(root) + deobfuscate(page_text) if profile else []
    out = []
    for p in people:
        own = profile and len(people) == 1
        email, dropped = pick_email(p.emails + (page_emails if own else []), domains, p.name)
        out.append({
            "name": p.name, "title": p.title, "departments": sorted({d for d in p.departments if d}),
            "school": p.school, "research_areas": p.research_areas, "email": email,
            "orcid": orcid_in(page_text) if own else None,
            "profile_url": urljoin(url, p.url) if p.url else url, "dropped_emails": dropped,
        })
    return out


ORCID_RE = re.compile(r"orcid\.org/(\d{4}-\d{4}-\d{4}-\d{3}[\dX])")


def orcid_in(page_text: str) -> str | None:
    found = set(ORCID_RE.findall(page_text))
    return found.pop() if len(found) == 1 else None


SEARCH_TITLE = re.compile(r"^\s*(search|search results)\b|\bsearch results\b", re.I)
SEARCH_TYPES = {"search", "searchresultspage", "pagesearch"}


def profile_invalid_reason(page_text: str, url: str, domains: Iterable[str] = ()) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", page_text, re.S | re.I)
    title = htmllib.unescape(m.group(1)).strip() if m else ""
    if not title:
        return "no title"
    data = next_data(page_text) or {}
    page_type = str(((data.get("props") or {}).get("pageProps") or {}).get("pageData", {}).get("__typename", "")
                    if isinstance(((data.get("props") or {}).get("pageProps") or {}).get("pageData"), dict) else "")
    if SEARCH_TITLE.search(title) or page_type.lower() in SEARCH_TYPES:
        return "search page"
    people = [p for p in page_people(page_text, url, domains, profile=True) if p.get("name")]
    if not people:
        return "no person name"
    if len({p["name"] for p in people}) > 1:
        return "listing page"
    p = people[0]
    in_domain = any(email_domain_ok(e, domains) for e in p.get("dropped_emails") or [])
    if not in_domain and not any(p.get(k) for k in ("title", "email", "departments", "research_areas", "school", "orcid")):
        return "no schema field"
    return None


def profile_validator(is_profile, domains: Iterable[str] = ()):
    def check(page_text: str, url: str) -> str | None:
        return profile_invalid_reason(page_text, url, domains) if is_profile(url) else None

    return check


CONTEXT_KEY = re.compile(r"department|school|college|unit|affiliation|breadcrumb", re.I)


def context_lines(page_text: str, limit: int = 20) -> list[str]:
    root = parse_html(page_text)
    lines: list[str] = []
    for n in root.iter():
        key = f"{n.attrs.get('class', '')} {n.attrs.get('id', '')} {n.attrs.get('aria-label', '')}"
        if n.tag == "nav" and "breadcrumb" not in key.lower() and n.parent is not None and n.parent.tag == "header":
            continue
        if CONTEXT_KEY.search(key) or (n.tag == "nav" and "breadcrumb" in key.lower()):
            t = n.text()
            if t and len(t) < 300 and t not in lines:
                lines.append(t)
        if len(lines) >= limit:
            break
    return lines
