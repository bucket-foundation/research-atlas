from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlparse

from atlas.users.directories.base import (
    BudgetExhausted,
    FacultyRecord,
    Page,
    PoliteFetcher,
    RobotsDenied,
    accept_email,
    email_domain_ok,
)
from atlas.users.directories.domains import SHARED_HOSTS, host_domain
from atlas.users.directories.generic import (
    GenericAdapter,
    Node,
    Person,
    is_role_mailbox,
    mailto_emails,
    page_links,
    parse_html,
    pick_email,
    slug_for,
)

STAFF_PATHS = ("/staff", "/directory", "/staff-directory", "/faculty-staff", "/our-staff")
PAGE_PARAM = re.compile(r"[?&](page|pageindex|const_page|p|pagenum|pg)=\d+", re.IGNORECASE)
STUDENT_PAGE = re.compile(
    r"\bstudents? (directory|roster|list|gallery|spotlight|profiles?)\b|\bclass roster\b|\b(meet|our) (the |our )?students\b"
    r"|\bhonor roll\b|\bstudents? of the (month|week|year)\b|\bclass of (19|20)\d{2}\b",
    re.IGNORECASE)
STUDENT_TITLE = re.compile(
    r"^\s*(student|pupil|learner)s?\s*$|^\s*(student|pupil)\b(?!\s+(services?|support|affairs|success|activities|"
    r"assistance|information|data|nutrition|health|body advisor|council advisor))"
    r"|\bclass of (19|20)\d{2}\b|^\s*grade \d{1,2}\s*$|^\s*(\d{1,2}(st|nd|rd|th)|k) grader?\s*$"
    r"|^\s*(senior|junior|sophomore|freshman|freshmen)s?\s*$",
    re.IGNORECASE)


def is_student_title(title: str | None) -> bool:
    return bool(title) and bool(STUDENT_TITLE.search(title))


def lists_students(root: Node) -> bool:
    heads = [n.text() for n in root.iter() if n.tag in ("title", "h1", "h2")]
    return any(STUDENT_PAGE.search(h) for h in heads if h)


def has_class(n: Node, *names: str) -> bool:
    cls = n.attrs.get("class", "").split()
    return any(name in cls for name in names)


def find(root: Node, *names: str) -> list[Node]:
    return [n for n in root.iter() if has_class(n, *names)]


def first_text(root: Node, *names: str) -> str | None:
    for n in find(root, *names):
        if (t := n.text()):
            return t
    return None


HOSTED = SHARED_HOSTS + ("finalsite.com", "finalsite.net", "schoolwires.net", "schoolwires.com", "edlio.com",
                        "edliostudio.com", "apptegy.net", "thrillshare.com", "weebly.com", "wixsite.com")


def site_domains(*urls: str | None) -> tuple[str, ...]:
    out = []
    for u in urls:
        host = host_domain(u)
        if not host or any(host == h or host.endswith("." + h) for h in HOSTED):
            continue
        parts = host.split(".")
        keep = 4 if len(parts) >= 4 and parts[-3] == "k12" and parts[-1] == "us" else 2
        out.append(".".join(parts[-keep:]))
    return tuple(dict.fromkeys(out))


class K12Adapter(GenericAdapter):
    platform = "k12"
    signatures: tuple[str, ...] = ()
    card_classes: tuple[str, ...] = ()
    name_classes: tuple[str, ...] = ()
    title_classes: tuple[str, ...] = ()
    dept_classes: tuple[str, ...] = ()
    listing_paths = STAFF_PATHS

    def __init__(self, ror_id: str, entry_urls: Iterable[str], domains: Iterable[str] | None = None,
                 max_pages: int = 50, school: str | None = None) -> None:
        super().__init__(ror_id, entry_urls, domains=tuple(domains or ()), max_pages=max_pages)
        self.school = school
        self.student_skips = {"pages": 0, "records": 0}

    @classmethod
    def matches(cls, text: str) -> bool:
        low = text.lower()
        return any(s in low for s in cls.signatures)

    @classmethod
    def probe(cls, fetcher: PoliteFetcher, base: str) -> bool:
        try:
            page = fetcher.fetch(base)
        except (RobotsDenied, BudgetExhausted):
            return False
        return page.status == 200 and cls.matches(page.text)

    def next_pages(self, page: Page, root: Node) -> list[str]:
        path = urlparse(page.url).path
        return [u for u in page_links(root, page.url)
                if PAGE_PARAM.search(u) and urlparse(u).path == path]

    def seeds(self, fetcher: PoliteFetcher) -> list[str]:
        starts = list(dict.fromkeys(self.entry_urls))
        queue, seen, keep = list(starts), set(), []
        try:
            while queue and len(seen) < self.max_pages:
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                page = self._fetch(fetcher, url)
                if page is None:
                    continue
                root = parse_html(page.text)
                if find(root, *self.card_classes):
                    keep.append(url)
                queue += [u for u in self.next_pages(page, root) if u not in seen]
        except BudgetExhausted:
            pass
        self.discovery["listing_pages"] = len(keep)
        return keep

    def card_person(self, card: Node) -> Person | None:
        name = first_text(card, *self.name_classes)
        if not name:
            return None
        depts = [t for n in find(card, *self.dept_classes) if (t := n.text())]
        return Person(name=name, title=first_text(card, *self.title_classes), departments=depts,
                      school=self.school, emails=mailto_emails(card))

    def cards(self, root: Node) -> list[Node]:
        found = find(root, *self.card_classes)
        ids = {id(n) for n in found}

        def nested(n: Node) -> bool:
            p = n.parent
            while p is not None:
                if id(p) in ids:
                    return True
                p = p.parent
            return False

        return [n for n in found if not nested(n)]

    def parse(self, page: Page) -> list[FacultyRecord]:
        root = parse_html(page.text)
        if lists_students(root):
            self.student_skips["pages"] += 1
            return []
        out = []
        for i, card in enumerate(self.cards(root)):
            p = self.card_person(card)
            if p is None:
                continue
            if is_student_title(p.title):
                self.student_skips["records"] += 1
                continue
            email, dropped = pick_email(p.emails, self.domains, p.name)
            rec = FacultyRecord(
                ror_id=self.ror_id, slug=f"{slug_for(page.url)}#{re.sub(r'[^a-z0-9]+', '-', p.name.lower()).strip('-')}",
                name=p.name, title=p.title, departments=sorted(set(p.departments)), school=p.school,
                research_areas=None, email=None, profile_url=page.url, fetched_at=page.fetched_at,
                html_sha256=page.sha256, source_kind="listing")
            rec.source_url = urljoin(page.url, p.url) if p.url else page.url
            rec.dropped_emails.extend(dropped)
            rec.org_mailbox = next((e for e in dropped if is_role_mailbox(e) and email_domain_ok(e, self.domains)), None)
            out.append(accept_email(rec, email, self.domains))
        return out
