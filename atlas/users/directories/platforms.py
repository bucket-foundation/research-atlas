from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlparse

from atlas.users.directories.base import (
    BudgetExhausted,
    Page,
    PoliteFetcher,
    RobotsDenied,
)
from atlas.users.directories.domains import allowed_domains, normalize_ror
from atlas.users.directories.generic import (
    GenericAdapter,
    Node,
    Person,
)


def _get(fetcher: PoliteFetcher, url: str) -> Page | None:
    try:
        page = fetcher.fetch(url)
    except (RobotsDenied, BudgetExhausted):
        return None
    return page if page.status == 200 else None


def _class_has(n: Node, name: str) -> bool:
    return name in n.attrs.get("class", "").split()


class PureAdapter(GenericAdapter):
    platform = "pure"
    profile_re = re.compile(r"/(en/)?persons/[^/?#]+/?$")
    listing_re = re.compile(r"/(en/)?persons/?(\?.*)?$")
    listing_paths = ("/en/persons/",)
    max_listing_pages = 60

    @classmethod
    def probe(cls, fetcher: PoliteFetcher, base: str) -> bool:
        page = _get(fetcher, urljoin(base, "/en/persons/"))
        return bool(page) and bool(re.search(r"/en/persons/[^\"/?#]+/?\"|Pure|elsevier", page.text))

    def departments(self, page: Page, root: Node) -> list[str]:
        out = []
        for n in root.iter():
            if n.tag == "a" and re.search(r"/organisations/[^/?#]+/?$", n.attrs.get("href", "")):
                if (t := n.text()):
                    out.append(t)
        return out


class VivoAdapter(GenericAdapter):
    platform = "vivo"
    profile_re = re.compile(r"/(vivo/)?(individual|display)/[^/?#]+/?$")
    listing_re = re.compile(r"/(vivo/)?(people|browse|vclassAlpha)/?(\?.*)?$")
    listing_paths = ("/people",)

    @classmethod
    def probe(cls, fetcher: PoliteFetcher, base: str) -> bool:
        page = _get(fetcher, base)
        return bool(page) and bool(re.search(r"VIVO|vivoweb|/individual/", page.text))

    def people(self, page: Page, root: Node) -> list[Person]:
        found = super().people(page, root)
        if found or not self.is_profile(page.url):
            return found
        name = title = None
        depts = []
        for n in root.iter():
            if n.tag == "h1" and _class_has(n, "fn") and not name:
                name = n.text()
            elif _class_has(n, "display-title") and not title:
                title = n.text()
            elif n.attrs.get("id") == "individual-personInPosition":
                depts += [a.text() for a in n.iter() if a.tag == "a"]
        return [Person(name=name, title=title, departments=depts)] if name else []


class SymplecticAdapter(GenericAdapter):
    platform = "symplectic"
    profile_re = re.compile(r"/(display|profiles?|en/profiles)/[^/?#]+/?$")
    listing_re = re.compile(r"/(search|browse|people|profiles)/?(\?.*)?$")
    listing_paths = ("/people",)

    @classmethod
    def probe(cls, fetcher: PoliteFetcher, base: str) -> bool:
        page = _get(fetcher, base)
        return bool(page) and bool(re.search(r"Symplectic|discovery[- ]module|Elements Discovery", page.text, re.IGNORECASE))

    def people(self, page: Page, root: Node) -> list[Person]:
        found = super().people(page, root)
        if found or not self.is_profile(page.url):
            return found
        name = None
        depts = []
        for n in root.iter():
            cls = n.attrs.get("class", "")
            if not name and ("profile-name" in cls or n.tag == "h1"):
                name = n.text() or None
            elif "profile-department" in cls or "department" in cls.split():
                depts.append(n.text())
        return [Person(name=name, departments=depts)] if name else []


class CmsPeopleAdapter(GenericAdapter):
    platform = "cms"
    listing_paths = ("/people", "/faculty", "/directory", "/people/faculty", "/faculty-directory")

    @classmethod
    def probe(cls, fetcher: PoliteFetcher, base: str) -> bool:
        page = _get(fetcher, base)
        return bool(page) and bool(re.search(r'name="generator" content="(Drupal|WordPress)|wp-content|/sites/default/files',
                                             page.text, re.IGNORECASE))

    def departments(self, page: Page, root: Node) -> list[str]:
        out = []
        for n in root.iter():
            cls = n.attrs.get("class", "")
            if re.search(r"field--name-field-(department|unit|affiliation)|person-department", cls):
                if (t := n.text()):
                    out.append(t)
        return out


PROBE_ORDER: tuple[type[GenericAdapter], ...] = (PureAdapter, VivoAdapter, SymplecticAdapter, CmsPeopleAdapter)
PORTAL_PREFIXES = ("experts", "research", "pure", "scholars", "vivo", "profiles", "researchers")


def candidate_bases(homepage_url: str, domains: Iterable[str]) -> list[str]:
    p = urlparse(homepage_url if "//" in homepage_url else "https://" + homepage_url)
    bases = [f"{p.scheme or 'https'}://{p.netloc}/"]
    for d in domains:
        bases += [f"https://{prefix}.{d}/" for prefix in PORTAL_PREFIXES]
    return list(dict.fromkeys(bases))


def detect_site(ror_id: str, homepage_url: str, fetcher: PoliteFetcher,
                domains: Iterable[str] | None = None) -> tuple[type[GenericAdapter] | None, str | None]:
    doms = tuple(domains) if domains is not None else allowed_domains(normalize_ror(ror_id))
    for base in candidate_bases(homepage_url, doms):
        page = _get(fetcher, base)
        if page is None:
            continue
        for cls in PROBE_ORDER:
            if cls.probe(fetcher, base):
                return cls, base
    return None, None


def detect_platform(ror_id: str, homepage_url: str, fetcher: PoliteFetcher,
                    domains: Iterable[str] | None = None) -> type[GenericAdapter] | None:
    return detect_site(ror_id, homepage_url, fetcher, domains)[0]
