from __future__ import annotations

import re

from atlas.users.directories.base import (
    FacultyRecord,
    Page,
    PoliteFetcher,
    RobotsDenied,
    accept_email,
    html_to_text,
    next_data,
)

BASE = "https://www.stevens.edu"
SITEMAP = BASE + "/sitemap.xml"
LISTING = re.compile(r"/(faculty|meet-our-faculty|hass-faculty|affiliated-faculty|faculty-researchers|"
                     r"faculty-staff|craft-leadership-and-faculty|scs-sustainability-faculty)$")
PROFILE = re.compile(r"^https://www\.stevens\.edu/profile/([a-z0-9_-]+)$")


def dept_from_tag(tag: str) -> str | None:
    m = re.search(r"Department([A-Z][A-Za-z]*)$", tag)
    if not m:
        return None
    words = re.findall(r"[A-Z][a-z]*", m.group(1))
    return " ".join(w.lower() if w in ("And", "Of") else w for w in words)


def listing_items(page_text: str) -> list[dict]:
    data = next_data(page_text)
    found: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("__typename") == "PageProfile" and isinstance(o.get("slug"), str):
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    if data:
        walk(data)
    return found


def profile_url(slug: str) -> str:
    return f"{BASE}/profile/{slug}"


def sitemap_urls(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def listing_slugs(page_text: str) -> set[str]:
    return {o["slug"] for o in listing_items(page_text)}


class StevensAdapter:
    ror_id = "02z43xh36"
    domains = ("stevens.edu",)

    def seeds(self, fetcher: PoliteFetcher) -> list[str]:
        sm = fetcher.fetch(SITEMAP)
        if sm.status != 200:
            raise RuntimeError(f"sitemap HTTP {sm.status}")
        urls = sitemap_urls(sm.text)
        slugs = {m.group(1) for u in urls if (m := PROFILE.match(u))}
        listings = []
        for u in urls:
            if LISTING.search(u):
                try:
                    page = fetcher.fetch(u)
                except RobotsDenied:
                    continue
                if page.status == 200:
                    listings.append(u)
                    slugs |= listing_slugs(page.text)
        return listings + [profile_url(s) for s in sorted(slugs)]

    def parse(self, page: Page) -> list[FacultyRecord]:
        if LISTING.search(page.url):
            return self.parse_listing(page)
        return self.parse_profile(page)

    def parse_listing(self, page: Page) -> list[FacultyRecord]:
        out = []
        for o in listing_items(page.text):
            name = (o.get("title") or "").strip()
            if not name:
                continue
            tags = [t.get("id", "") for t in (o.get("contentfulMetadata") or {}).get("tags", [])]
            rec = FacultyRecord(
                ror_id=self.ror_id, slug=o["slug"], name=name, title=o.get("status"),
                departments=sorted({d for t in tags if (d := dept_from_tag(t))}),
                school=None, research_areas=None, email=None, profile_url=page.url,
                fetched_at=page.fetched_at, html_sha256=page.sha256, source_kind="listing",
            )
            out.append(accept_email(rec, o.get("email"), self.domains))
        return out

    def parse_profile(self, page: Page) -> list[FacultyRecord]:
        m = PROFILE.match(page.url)
        data = next_data(page.text)
        if not m or not data:
            return []
        pd = (data.get("props") or {}).get("pageProps", {}).get("pageData") or {}
        if pd.get("__typename") != "PageProfile":
            return []
        depts = [it.get("department", {}).get("title") for it in (pd.get("positionsCollection") or {}).get("items", [])]
        research = None
        for sec in (pd.get("sectionsCollection") or {}).get("items", []):
            if (sec.get("title") or "").strip().lower() in ("research", "research interests", "research areas"):
                obj = sec.get("object")
                research = html_to_text(obj.get("value") if isinstance(obj, dict) else None)
                break
        rec = FacultyRecord(
            ror_id=self.ror_id,
            slug=pd.get("slug") or m.group(1),
            name=(pd.get("title") or "").strip(),
            title=pd.get("status"),
            departments=sorted({d.strip() for d in depts if d}),
            school=(pd.get("school") or {}).get("title"),
            research_areas=research,
            email=None,
            profile_url=page.url,
            fetched_at=page.fetched_at,
            html_sha256=page.sha256,
        )
        return [accept_email(rec, pd.get("email"), self.domains)] if rec.name else []
