from __future__ import annotations

import contextlib
import html as htmllib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

from atlas.users.contacts import _is_acceptable_email
from atlas.users.directories.base import (
    EMAIL_RE,
    BudgetExhausted,
    FacultyRecord,
    Page,
    PoliteFetcher,
    RobotsDenied,
    accept_email,
    email_domain_ok,
    html_to_text,
    next_data,
)
from atlas.users.directories.domains import allowed_domains, host_domain, normalize_ror

PROFILE_HINT = re.compile(
    r"/(people|person|persons|faculty|profile|profiles|directory|staff|experts|scientists|researchers|"
    r"individual|display|team|our-team|faculty-directory|faculty-and-staff)/[^/?#]+/?$", re.IGNORECASE)
LISTING_HINT = re.compile(
    r"/(people|persons|faculty|directory|staff|experts|scientists|researchers|team|our-team|"
    r"faculty-directory|faculty-and-staff)/?(\?.*)?$", re.IGNORECASE)
PAGINATION = re.compile(r"[?&](page|pageNumber|p)=\d+|/page/\d+/?$", re.IGNORECASE)
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|docx?|xlsx?|pptx?|zip|ics|css|js|mp4|mp3)(\?|$)", re.IGNORECASE)
LABEL = r"[A-Za-z0-9-]+"
BRACKET_AT = r"\s*(?:\[at\]|\(at\)|\{at\}|<at>|\[@\]|\(@\))\s*"
BRACKET_DOT = r"\s*(?:\[dot\]|\(dot\)|\{dot\}|<dot>)\s*"
OBFUSCATED = re.compile(
    rf"([A-Za-z0-9._%+-]+){BRACKET_AT}({LABEL}(?:(?:\.|{BRACKET_DOT}){LABEL})+)", re.IGNORECASE)
SPACED_AT = re.compile(
    rf"([A-Za-z0-9._%+-]+)\s+at\s+({LABEL}(?:{BRACKET_DOT}{LABEL})+)(?![A-Za-z0-9.-])", re.IGNORECASE)
DOT = re.compile(rf"{BRACKET_DOT}|\.", re.IGNORECASE)
ROLE_LOCAL = re.compile(
    r"^(registrar|info|information|admissions?|office|department|dept|contact|contactus|help|helpdesk|"
    r"support|webmaster|web|admin|administrator|reception|frontdesk|general|inquiries|enquiries|hr|"
    r"media|press|news|communications|events|alumni|dean|chair|staff|faculty|grad|graduate|undergrad|"
    r"secretary|mail|postmaster|noreply|no-reply)([._-][a-z0-9]+)*$", re.IGNORECASE)
PERSON_TYPES = {"person", "http://schema.org/person", "https://schema.org/person", "foaf:person"}


@dataclass
class Node:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)
    parent: Node | None = None

    def text(self) -> str:
        parts: list[str] = []

        def walk(n):
            for c in n.children:
                if isinstance(c, str):
                    parts.append(c)
                elif c.tag not in ("script", "style"):
                    walk(c)

        walk(self)
        return re.sub(r"\s+", " ", htmllib.unescape(" ".join(parts))).strip()

    def iter(self):
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.iter()


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.root = Node("#root", {})
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, parent=self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: (v or "") for k, v in attrs}, parent=self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)

    def handle_entityref(self, name):
        self.cur.children.append(f"&{name};")

    def handle_charref(self, name):
        self.cur.children.append(f"&#{name};")


def parse_html(text: str) -> Node:
    b = _TreeBuilder()
    with contextlib.suppress(AssertionError, ValueError):
        b.feed(text)
        b.close()
    return b.root


def decode_cfemail(hexstr: str) -> str | None:
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    return "".join(chr(b ^ key) for b in data[1:])


def deobfuscate(text: str) -> list[str]:
    found = []
    for m in re.finditer(r'data-cfemail="([0-9a-fA-F]+)"', text):
        if (e := decode_cfemail(m.group(1))) and EMAIL_RE.fullmatch(e):
            found.append(e)
    for m in re.finditer(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)", text):
        if (e := decode_cfemail(m.group(1))) and EMAIL_RE.fullmatch(e):
            found.append(e)
    plain = htmllib.unescape(re.sub(r"<[^>]+>", " ", text))
    for m in [*OBFUSCATED.finditer(plain), *SPACED_AT.finditer(plain)]:
        domain = DOT.sub(".", m.group(2)).strip(".")
        candidate = f"{m.group(1)}@{domain}"
        if EMAIL_RE.fullmatch(candidate):
            found.append(candidate)
    return found


def mailto_emails(root: Node) -> list[str]:
    out = []
    for n in root.iter():
        href = n.attrs.get("href", "") if n.tag == "a" else ""
        if href.lower().startswith("mailto:"):
            addr = htmllib.unescape(href[7:].split("?", 1)[0]).strip()
            if EMAIL_RE.fullmatch(addr):
                out.append(addr)
    return out


def _types(obj: dict) -> set[str]:
    t = obj.get("@type") or obj.get("type") or []
    return {str(x).lower() for x in (t if isinstance(t, list) else [t])}


def _walk(obj, visit):
    if isinstance(obj, dict):
        visit(obj)
        for v in obj.values():
            _walk(v, visit)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, visit)


def _text_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        return _text_list(value.get("name") or value.get("@value") or value.get("value"))
    if isinstance(value, list):
        return [t for v in value for t in _text_list(v)]
    return []


def _first(value) -> str | None:
    items = _text_list(value)
    return items[0] if items else None


@dataclass
class Person:
    name: str
    title: str | None = None
    departments: list[str] = field(default_factory=list)
    school: str | None = None
    research_areas: str | None = None
    emails: list[str] = field(default_factory=list)
    url: str | None = None


def _person_from_schema(obj: dict) -> Person | None:
    name = _first(obj.get("name"))
    if not name and (obj.get("givenName") or obj.get("familyName")):
        name = " ".join(filter(None, [_first(obj.get("givenName")), _first(obj.get("familyName"))]))
    if not name:
        return None
    emails = [e.removeprefix("mailto:") for e in _text_list(obj.get("email"))]
    orgs = _text_list(obj.get("worksFor")) + _text_list(obj.get("affiliation")) + _text_list(obj.get("memberOf"))
    areas = _text_list(obj.get("knowsAbout"))
    return Person(name=html_to_text(name) or name, title=_first(obj.get("jobTitle")),
                  departments=orgs, research_areas="; ".join(areas) or None,
                  emails=emails, url=_first(obj.get("url")) if isinstance(obj.get("url"), (str, list)) else None)


def jsonld_people(page_text: str) -> list[Person]:
    out: list[Person] = []
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', page_text, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(htmllib.unescape(m.group(1).strip()) if "&quot;" in m.group(1) else m.group(1).strip())
        except json.JSONDecodeError:
            continue

        def visit(o):
            if _types(o) & PERSON_TYPES and (p := _person_from_schema(o)):
                out.append(p)

        _walk(data, visit)
    return out


def microdata_people(root: Node) -> list[Person]:
    out = []
    for n in root.iter():
        itype = n.attrs.get("itemtype", "").lower()
        if "itemscope" not in n.attrs or not itype.endswith("schema.org/person"):
            continue
        props: dict[str, list[str]] = {}
        for c in n.iter():
            prop = c.attrs.get("itemprop")
            if not prop:
                continue
            if c.tag == "meta":
                value = c.attrs.get("content", "")
            elif c.tag == "a" and prop == "email" and c.attrs.get("href", "").startswith("mailto:"):
                value = c.attrs["href"][7:]
            elif c.tag in ("a", "link") and prop == "url":
                value = c.attrs.get("href", "")
            else:
                value = c.text()
            for key in prop.split():
                props.setdefault(key, []).append(value.strip())
        name = (props.get("name") or [""])[0]
        if not name:
            continue
        out.append(Person(name=name, title=(props.get("jobTitle") or [None])[0],
                          departments=props.get("worksFor", []) + props.get("affiliation", []),
                          research_areas="; ".join(props.get("knowsAbout", [])) or None,
                          emails=props.get("email", []), url=(props.get("url") or [None])[0]))
    return out


NAME_KEYS = ("fullName", "displayName", "name", "preferredName")


def next_data_people(page_text: str) -> list[Person]:
    data = next_data(page_text)
    out: list[Person] = []
    if not data:
        return out

    def visit(o):
        email = o.get("email") or o.get("emailAddress") or o.get("primaryEmail")
        if not isinstance(email, str) or "@" not in email:
            return
        name = next((o[k] for k in NAME_KEYS if isinstance(o.get(k), str) and o[k].strip()), None)
        if not name and isinstance(o.get("firstName"), str) and isinstance(o.get("lastName"), str):
            name = f"{o['firstName']} {o['lastName']}"
        if not name and isinstance(o.get("title"), str) and o.get("__typename", "").lower().endswith("profile"):
            name = o["title"]
        if not name:
            return
        title = next((o[k] for k in ("jobTitle", "position", "status") if isinstance(o.get(k), str)), None)
        depts = _text_list(o.get("department")) + _text_list(o.get("departments"))
        out.append(Person(name=name.strip(), title=title, departments=depts, emails=[email],
                          url=o.get("url") if isinstance(o.get("url"), str) else None))

    _walk(data, visit)
    return out


def _meta(root: Node, *names: str) -> str | None:
    for n in root.iter():
        if n.tag == "meta" and (n.attrs.get("property") in names or n.attrs.get("name") in names):
            if (c := n.attrs.get("content", "").strip()):
                return htmllib.unescape(c)
    return None


def page_name(root: Node) -> str | None:
    for n in root.iter():
        if n.tag == "h1" and (t := n.text()):
            return t
    og = _meta(root, "og:title")
    return og.split("|")[0].strip() if og else None


def _name_tokens(name: str) -> set[str]:
    return {t.lower() for t in re.split(r"[\s,.'-]+", name) if len(t) > 1}


def is_role_mailbox(email: str) -> bool:
    return bool(ROLE_LOCAL.match(email.split("@", 1)[0]))


def pick_email(candidates: Iterable[str], domains: Iterable[str], name: str) -> tuple[str | None, list[str]]:
    seen: list[str] = []
    for c in candidates:
        c = c.strip().rstrip(".").lower()
        if c and c not in seen:
            seen.append(c)
    good = [e for e in seen if email_domain_ok(e, domains) and _is_acceptable_email(e) and not is_role_mailbox(e)]
    dropped = [e for e in seen if e not in good]
    if len(good) == 1:
        return good[0], dropped
    tokens = _name_tokens(name)
    named = [e for e in good if any(t in e.split("@")[0] for t in tokens if len(t) > 2)]
    if len(named) == 1:
        return named[0], dropped + [e for e in good if e != named[0]]
    return None, dropped + good


def slug_for(url: str) -> str:
    p = urlparse(url)
    return (p.netloc + p.path).rstrip("/").lower()


def same_site(url: str, domains: Iterable[str]) -> bool:
    host = host_domain(url)
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def page_links(root: Node, base: str) -> list[str]:
    out = []
    for n in root.iter():
        if n.tag == "a" and (href := n.attrs.get("href")):
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            url = urldefrag(urljoin(base, htmllib.unescape(href)))[0]
            if url.startswith("http") and not SKIP_EXT.search(url):
                out.append(url)
    return list(dict.fromkeys(out))


def sitemap_locs(xml: str) -> tuple[list[str], list[str]]:
    locs = [htmllib.unescape(u) for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)]
    if re.search(r"<sitemapindex", xml, re.IGNORECASE):
        return locs, []
    return [], locs


class GenericAdapter:
    platform = "generic"
    profile_re = PROFILE_HINT
    listing_re = LISTING_HINT
    listing_paths: tuple[str, ...] = ()
    max_listing_pages = 40
    max_sitemaps = 30

    def __init__(self, ror_id: str, entry_urls: Iterable[str], domains: Iterable[str] | None = None,
                 max_pages: int = 400) -> None:
        self.ror_id = normalize_ror(ror_id)
        self.entry_urls = list(entry_urls)
        self.domains = tuple(domains) if domains is not None else allowed_domains(self.ror_id)
        self.max_pages = max_pages
        self.discovery: dict[str, int] = {"sitemap_profiles": 0, "listing_profiles": 0, "listing_pages": 0}

    @classmethod
    def probe(cls, fetcher: PoliteFetcher, base: str) -> bool:
        return True

    def is_profile(self, url: str) -> bool:
        return bool(self.profile_re.search(urlparse(url).path)) and same_site(url, self.domains)

    def is_listing(self, url: str) -> bool:
        p = urlparse(url)
        return bool(self.listing_re.search(p.path + ("?" + p.query if p.query else "")))

    def _fetch(self, fetcher: PoliteFetcher, url: str) -> Page | None:
        try:
            page = fetcher.fetch(url)
        except RobotsDenied:
            return None
        return page if page.status == 200 else None

    def same_origin(self, url: str, base: str) -> bool:
        return same_site(url, self.domains) or urlparse(url).netloc == urlparse(base).netloc

    def sitemap_profiles(self, fetcher: PoliteFetcher, base: str, limit: int) -> list[str]:
        root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
        listed = [u for u in fetcher.sitemaps(base) if self.same_origin(u, base)]
        queue = list(dict.fromkeys(listed + [root + "/sitemap.xml"]))
        seen, found = set(), []
        while queue and len(seen) < self.max_sitemaps and len(found) < limit:
            sm = queue.pop(0)
            if sm in seen or sm.endswith(".gz"):
                continue
            seen.add(sm)
            page = self._fetch(fetcher, sm)
            if page is None:
                continue
            nested, urls = sitemap_locs(page.text)
            nested = [u for u in nested if self.same_origin(u, base)]
            queue += sorted(nested, key=lambda u: 0 if re.search(r"people|person|profile|faculty|expert", u, re.IGNORECASE) else 1)
            found += [u for u in urls if self.is_profile(u)]
        return list(dict.fromkeys(found))[:limit]

    def listing_profiles(self, fetcher: PoliteFetcher, start: str, limit: int) -> tuple[list[str], list[str]]:
        queue, seen, listings, found = [start], set(), [], []
        while queue and len(seen) < self.max_listing_pages and len(found) < limit:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            page = self._fetch(fetcher, url)
            if page is None:
                continue
            listings.append(url)
            for link in page_links(parse_html(page.text), url):
                if not same_site(link, self.domains):
                    continue
                if self.is_profile(link) and not self.is_listing(link):
                    found.append(link)
                elif (PAGINATION.search(link) and urlparse(link).path == urlparse(url).path) or \
                        (self.is_listing(link) and url == start):
                    queue.append(link)
        return listings, list(dict.fromkeys(found))[:limit]

    def seeds(self, fetcher: PoliteFetcher) -> list[str]:
        listings: list[str] = []
        profiles: list[str] = []
        try:
            for entry in self.entry_urls:
                starts = [entry] + [urljoin(entry, p) for p in self.listing_paths]
                for start in starts:
                    ls, ps = self.listing_profiles(fetcher, start, self.max_pages)
                    listings += ls
                    profiles += ps
                self.discovery["listing_profiles"] = len(set(profiles))
                sm = self.sitemap_profiles(fetcher, entry, self.max_pages)
                self.discovery["sitemap_profiles"] += len(sm)
                profiles += sm
        except BudgetExhausted:
            pass
        self.discovery["listing_pages"] = len(set(listings))
        profiles = [p for p in dict.fromkeys(profiles) if p not in listings]
        return list(dict.fromkeys(listings)) + profiles[: max(0, self.max_pages - fetcher.requests)]

    def people(self, page: Page, root: Node) -> list[Person]:
        return jsonld_people(page.text) + microdata_people(root) + next_data_people(page.text)

    def departments(self, page: Page, root: Node) -> list[str]:
        return []

    def parse(self, page: Page) -> list[FacultyRecord]:
        root = parse_html(page.text)
        people = self.people(page, root)
        profile = self.is_profile(page.url) and not self.is_listing(page.url)
        page_emails = mailto_emails(root) + deobfuscate(page.text)
        if profile and not people and (name := page_name(root)):
            people = [Person(name=name, title=_meta(root, "citation_author_title"))]
        extra_depts = self.departments(page, root)
        out = []
        for p in people:
            url = urljoin(page.url, p.url) if p.url else page.url
            own = profile and (len(people) == 1 or slug_for(url) == slug_for(page.url))
            candidates = p.emails + (page_emails if own else [])
            email, dropped = pick_email(candidates, self.domains, p.name)
            rec = FacultyRecord(
                ror_id=self.ror_id, slug=slug_for(url if not own else page.url), name=p.name, title=p.title,
                departments=sorted({d for d in p.departments + (extra_depts if own else []) if d}),
                school=p.school, research_areas=p.research_areas, email=None, profile_url=page.url,
                fetched_at=page.fetched_at, html_sha256=page.sha256,
                source_kind="profile" if own else "listing",
            )
            rec.dropped_emails.extend(dropped)
            rec.org_mailbox = next((e for e in dropped if is_role_mailbox(e) and email_domain_ok(e, self.domains)), None)
            out.append(accept_email(rec, email, self.domains))
        return out
