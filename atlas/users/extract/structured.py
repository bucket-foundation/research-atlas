from __future__ import annotations

import contextlib
import html as htmllib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from atlas.users.contacts import _is_acceptable_email
from atlas.users.directories.base import EMAIL_RE, email_domain_ok, html_to_text, next_data

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
        return _text_list(value.get("name") or value.get("@value") or value.get("value") or value.get("title"))
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


SCHOOL_TYPES = {"collegeoruniversity", "educationalorganization"}
SCHOOL_NAME = re.compile(r"\b(school|college|faculty) of\b", re.I)


def _is_school(o: dict) -> bool:
    return bool(_types(o) & SCHOOL_TYPES) or bool(SCHOOL_NAME.search(_first(o) or ""))


def _person_from_schema(obj: dict) -> Person | None:
    name = _first(obj.get("name"))
    if not name and (obj.get("givenName") or obj.get("familyName")):
        name = " ".join(filter(None, [_first(obj.get("givenName")), _first(obj.get("familyName"))]))
    if not name:
        return None
    emails = [e.removeprefix("mailto:") for e in _text_list(obj.get("email"))]
    org_objs = [o for k in ("worksFor", "affiliation", "memberOf", "department")
                for o in (obj.get(k) if isinstance(obj.get(k), list) else [obj.get(k)]) if o]
    for o in list(org_objs):
        if isinstance(o, dict):
            org_objs += [x for x in (o.get("department") if isinstance(o.get("department"), list)
                                     else [o.get("department")]) if x]
    school = next((_first(o) for o in org_objs if isinstance(o, dict) and _is_school(o)), None)
    orgs = [t for o in org_objs if not (isinstance(o, dict) and _is_school(o)) for t in _text_list(o)]
    areas = _text_list(obj.get("knowsAbout"))
    return Person(name=html_to_text(name) or name, title=_first(obj.get("jobTitle")),
                  departments=orgs, school=school, research_areas="; ".join(areas) or None,
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
        positions = (o.get("positionsCollection") or {}).get("items") if isinstance(o.get("positionsCollection"), dict) \
            else o.get("positions")
        for pos in positions or []:
            if isinstance(pos, dict):
                depts += _text_list(pos.get("department"))
        school = _first(o.get("school")) or _first(o.get("college"))
        out.append(Person(name=name.strip(), title=title, departments=depts, school=school, emails=[email],
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
