from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse
from typing import Callable, Iterable

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = "qwen3:4b"
MAX_TOKENS = 3000
CHARS_PER_TOKEN = 4
WORKERS = 4
FIELDS = ("name", "title", "departments", "school", "research_areas", "email", "orcid", "profile_url")
SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "departments": {"type": "array", "items": {"type": "string"}},
        "school": {"type": ["string", "null"]},
        "research_areas": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
        "orcid": {"type": ["string", "null"]},
        "profile_url": {"type": ["string", "null"]},
    },
    "required": list(FIELDS),
}
PROMPT = (
    "Extract the one faculty member this university profile page is about. Return JSON with keys "
    + ", ".join(FIELDS)
    + ". Copy values exactly as printed on the page. Use null for anything the page does not print. "
    "Copy an email only when the page prints that exact address; never build one from a name. "
    "If the page is not a profile of one person, return every key as null and departments as [].\n\n"
    "URL: {url}\n\nPAGE TEXT:\n{text}"
)

Post = Callable[[str, dict], dict]
Get = Callable[[str], dict]


def _post(url: str, payload: dict) -> dict:
    import requests

    r = requests.post(url, json=payload, timeout=180)
    r.raise_for_status()
    return r.json()


def _get(url: str) -> dict:
    import requests

    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def clean_text(page_html: str, max_tokens: int = MAX_TOKENS) -> str:
    import trafilatura

    from atlas.users.extract.structured import context_lines

    body = trafilatura.extract(page_html, include_links=False, include_comments=False, favor_recall=True) or ""
    context = context_lines(page_html)
    text = ("Page context:\n" + "\n".join(context) + "\n\n" if context else "") + body
    mailtos = sorted(set(re.findall(r'href="mailto:([^"?]+)', page_html)))
    if mailtos:
        text += "\n\nLinked addresses: " + ", ".join(mailtos)
    return text[: max_tokens * CHARS_PER_TOKEN]


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def check_loopback(url: str) -> str:
    host = urlparse(url).hostname or ""
    if host not in LOOPBACK:
        raise ValueError(f"Ollama URL must be loopback, got host {host!r}")
    return url


@dataclass
class LocalExtractor:
    model: str = MODEL
    base_url: str = OLLAMA_URL
    post: Post = _post
    get: Get = _get
    workers: int = WORKERS
    _digest: str | None = None
    failures: list = field(default_factory=list)

    def __post_init__(self) -> None:
        check_loopback(self.base_url)

    def digest(self) -> str | None:
        if self._digest is None:
            try:
                models = self.get(f"{self.base_url}/api/tags").get("models", [])
            except (OSError, ValueError):
                models = []
            self._digest = next((m.get("digest") for m in models if m.get("name") == self.model), None)
        return self._digest

    def payload(self, text: str, url: str) -> dict:
        return {
            "model": self.model, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0, "num_ctx": 4096},
            "messages": [{"role": "user", "content": PROMPT.format(url=url, text=text)}],
        }

    def extract_text(self, text: str, url: str, page_html: str | None = None,
                     domains: Iterable[str] = ()) -> dict | None:
        for _ in range(2):
            try:
                reply = self.post(f"{self.base_url}/api/chat", self.payload(text, url))
            except (OSError, ValueError) as exc:
                self.failures.append(f"{url}: {type(exc).__name__}: {exc}")
                return None
            parsed = parse_reply(reply)
            if parsed is not None:
                parsed = ground(parsed, text, url, page_html if page_html is not None else text, domains)
                parsed["extractor"] = self.model
                parsed["extractor_digest"] = self.digest()
                return parsed
        self.failures.append(f"{url}: invalid JSON twice")
        return None

    def extract(self, page_html: str, url: str, domains: Iterable[str] = ()) -> dict | None:
        return self.extract_text(clean_text(page_html), url, page_html, domains)

    def extract_many(self, pages: Iterable[tuple[str, str]], domains: Iterable[str] = ()) -> list[dict | None]:
        items = list(pages)
        doms = tuple(domains)
        with ThreadPoolExecutor(self.workers) as pool:
            return list(pool.map(lambda p: self.extract(p[0], p[1], doms), items))


def parse_reply(reply: dict) -> dict | None:
    content = (reply.get("message") or {}).get("content") or reply.get("response") or ""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    out = {k: data.get(k) for k in FIELDS}
    depts = out.get("departments")
    out["departments"] = [d for d in depts if isinstance(d, str) and d.strip()] if isinstance(depts, list) else []
    for k in FIELDS:
        if k != "departments" and not isinstance(out[k], str):
            out[k] = None
        elif k != "departments" and out[k] is not None:
            out[k] = out[k].strip() or None
    return out


def page_email_candidates(page_html: str) -> set[str]:
    import html as htmllib

    from atlas.users.extract.structured import EMAIL_RE, deobfuscate, mailto_emails, parse_html

    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", page_html))
    found = mailto_emails(parse_html(page_html)) + EMAIL_RE.findall(text) + deobfuscate(page_html)
    return {e.strip().rstrip(".").lower() for e in found}


def verified_email(candidate: str | None, page_html: str, domains: Iterable[str]) -> str | None:
    from atlas.users.contacts import _is_acceptable_email
    from atlas.users.extract.structured import email_domain_ok, is_role_mailbox

    if not candidate:
        return None
    email = candidate.strip().rstrip(".").lower()
    if email not in page_email_candidates(page_html):
        return None
    if not email_domain_ok(email, domains) or not _is_acceptable_email(email) or is_role_mailbox(email):
        return None
    return email


ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")


def _norm(v: str) -> str:
    return re.sub(r"\s+", " ", v).strip().lower()


def ground(parsed: dict, text: str, url: str, page_html: str = "", domains: Iterable[str] = ()) -> dict:
    hay = _norm(text)
    out = dict(parsed)
    for k in ("name", "title", "school", "research_areas"):
        if out.get(k) and _norm(out[k]) not in hay:
            out[k] = None
    out["departments"] = [d for d in out.get("departments") or [] if _norm(d) in hay]
    out["email"] = verified_email(out.get("email"), page_html or text, domains)
    m = ORCID_RE.search(out.get("orcid") or "")
    out["orcid"] = m.group(0) if m and m.group(0) in text and not m.group(0).startswith("0000-0000") else None
    out["profile_url"] = url
    if not out.get("name"):
        out.update({k: None for k in ("title", "school", "research_areas", "email", "orcid")}, departments=[])
    return out
