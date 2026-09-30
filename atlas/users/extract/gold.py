from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
UIUC_GOLD = REPO / "data" / "private" / "gold" / "uiuc"
LABEL_FIELDS = ("name", "title", "departments", "school", "email")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SYNTHETIC_DOMAIN = "example.edu"


@dataclass
class GoldPage:
    page_id: str
    url: str
    html: str
    label: dict


class GoldError(ValueError):
    pass


def load_gold(root: Path = UIUC_GOLD) -> list[GoldPage]:
    labels_path = root / "labels.jsonl"
    if not labels_path.exists():
        raise GoldError(f"gold labels missing at {labels_path}")
    pages = []
    for line in labels_path.read_text().splitlines():
        label = json.loads(line)
        missing = [k for k in ("page_id", "url", *LABEL_FIELDS) if k not in label]
        if missing:
            raise GoldError(f"{label.get('page_id')}: missing {missing}")
        html_path = root / "pages" / f"{label['page_id']}.html"
        if not html_path.exists():
            raise GoldError(f"{label['page_id']}: page file missing")
        html = html_path.read_text()
        for e in EMAIL_RE.findall(html):
            if not e.lower().endswith("@" + SYNTHETIC_DOMAIN):
                raise GoldError(f"{label['page_id']}: unsubstituted address on {e.split('@')[1]}")
        email = label.get("email")
        if email and (not email.endswith("@" + SYNTHETIC_DOMAIN) or email not in html):
            raise GoldError(f"{label['page_id']}: label email is not a synthetic address printed on the page")
        pages.append(GoldPage(label["page_id"], label["url"], html, {k: label[k] for k in LABEL_FIELDS}))
    return pages


def label_counts(pages: list[GoldPage]) -> dict[str, int]:
    return {k: sum(1 for p in pages if p.label.get(k)) for k in LABEL_FIELDS}
