from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from atlas.users.extract.local_llm import FIELDS, OLLAMA_URL, _post
from atlas.users.directories.base import EMAIL_RE

CODER = "qwen2.5-coder:7b"
SELECTOR_FIELDS = tuple(f for f in FIELDS if f != "profile_url")
ACCEPT = 0.9
SAMPLE_CHARS = 3500
PROMPT = (
    "Here are {n} HTML pages from one university site, each a profile of one faculty member. "
    "Write CSS selectors that pick each field on any profile page of this site. Return JSON with keys "
    + ", ".join(SELECTOR_FIELDS)
    + "; each value is a CSS selector string or null. For email pick the mailto anchor.\n\n{pages}"
)


def slim_html(page_html: str, limit: int = SAMPLE_CHARS) -> str:
    s = re.sub(r"<(script|style|svg|noscript|head)\b.*?</\1>", "", page_html, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"\s+", " ", s)
    main = re.search(r"<main\b.*?</main>", s, re.S | re.I)
    return (main.group(0) if main else s)[:limit]


def apply_selectors(page_html: str, selectors: dict) -> dict:
    import lxml.html
    from cssselect import SelectorError

    tree = lxml.html.fromstring(page_html)
    out: dict = {"departments": []}
    for key in SELECTOR_FIELDS:
        sel = selectors.get(key)
        values: list[str] = []
        if isinstance(sel, str) and sel.strip():
            try:
                nodes = tree.cssselect(sel)
            except (SelectorError, ValueError, TypeError):
                nodes = []
            for n in nodes:
                href = n.get("href") or ""
                if key == "email" and href.lower().startswith("mailto:"):
                    values.append(href[7:].split("?")[0])
                else:
                    t = re.sub(r"\s+", " ", n.text_content()).strip()
                    if t:
                        values.append(t)
        if key == "departments":
            out[key] = values
        elif key == "email":
            out[key] = next((v for v in values if EMAIL_RE.fullmatch(v)), None)
        else:
            out[key] = values[0] if values else None
    return out


def _same(a, b) -> bool:
    if isinstance(a, list) or isinstance(b, list):
        return {x.lower() for x in a or []} == {x.lower() for x in b or []}
    return (a or "").strip().lower() == (b or "").strip().lower()


def agreement(predicted: list[dict], reference: list[dict]) -> float:
    hits = total = 0
    for p, r in zip(predicted, reference):
        for k in SELECTOR_FIELDS:
            if r.get(k):
                total += 1
                hits += _same(p.get(k), r.get(k))
    return hits / total if total else 0.0


@dataclass
class Synthesizer:
    post: Callable[[str, dict], dict] = _post
    model: str = CODER
    base_url: str = OLLAMA_URL

    def propose(self, samples: list[str]) -> dict | None:
        pages = "\n\n".join(f"PAGE {i + 1}:\n{slim_html(h)}" for i, h in enumerate(samples))
        payload = {"model": self.model, "stream": False, "format": "json",
                   "options": {"temperature": 0, "num_ctx": 8192},
                   "messages": [{"role": "user", "content": PROMPT.format(n=len(samples), pages=pages)}]}
        for _ in range(2):
            try:
                reply = self.post(f"{self.base_url}/api/chat", payload)
                data = json.loads((reply.get("message") or {}).get("content") or "")
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                return {k: data.get(k) for k in SELECTOR_FIELDS}
        return None

    def run(self, host: str, pages: list[str], reference: Callable[[str], dict | None],
            queue: Path, store: Path) -> dict:
        result = {"host": host, "pages": len(pages), "accepted": False, "agreement": 0.0, "selectors": None}
        if len(pages) < 13:
            result["reason"] = "fewer than 13 cached pages"
        else:
            selectors = self.propose(pages[:3])
            held = pages[3:13]
            refs = [reference(h) for h in held]
            pairs = [(apply_selectors(h, selectors), r) for h, r in zip(held, refs) if r] if selectors else []
            result["selectors"] = selectors
            result["agreement"] = round(agreement([p for p, _ in pairs], [r for _, r in pairs]), 3)
            result["accepted"] = bool(pairs) and result["agreement"] >= ACCEPT
            if not result["accepted"]:
                result["reason"] = "no selectors" if not selectors else "agreement below 0.9"
        if result["accepted"]:
            store.mkdir(parents=True, exist_ok=True)
            (store / f"{host}.json").write_text(json.dumps(result, indent=1))
        else:
            queue.parent.mkdir(parents=True, exist_ok=True)
            with queue.open("a") as f:
                f.write(json.dumps({"host": host, "escalate_to": "claude", **result}) + "\n")
        return result
