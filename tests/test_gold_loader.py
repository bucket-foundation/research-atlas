from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from atlas.users.extract.gold import UIUC_GOLD, GoldError, label_counts, load_gold

SAMPLE = Path(__file__).parent / "fixtures" / "gold" / "sample"
REPO = Path(__file__).resolve().parents[1]


def test_sample_gold_loads_with_counts():
    pages = load_gold(SAMPLE)
    assert [p.page_id for p in pages] == ["aaaa000000000001", "aaaa000000000002"]
    assert label_counts(pages) == {"name": 2, "title": 2, "departments": 2, "school": 2, "email": 1}
    assert pages[0].label["email"] in pages[0].html


def copy(tmp_path):
    root = tmp_path / "g"
    shutil.copytree(SAMPLE, root)
    return root


def test_unsubstituted_address_is_rejected(tmp_path):
    root = copy(tmp_path)
    page = root / "pages" / "aaaa000000000002.html"
    page.write_text(page.read_text().replace("</body>", "<a href='mailto:bo@realuni.org'>x</a></body>"))
    with pytest.raises(GoldError, match="unsubstituted"):
        load_gold(root)


def test_label_email_must_be_printed_and_synthetic(tmp_path):
    root = copy(tmp_path)
    rows = [json.loads(line) for line in (root / "labels.jsonl").read_text().splitlines()]
    rows[1]["email"] = "person999@example.edu"
    (root / "labels.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(GoldError, match="synthetic"):
        load_gold(root)


def test_missing_page_or_labels_raise(tmp_path):
    root = copy(tmp_path)
    (root / "pages" / "aaaa000000000001.html").unlink()
    with pytest.raises(GoldError, match="page file missing"):
        load_gold(root)
    with pytest.raises(GoldError, match="labels missing"):
        load_gold(tmp_path / "absent")


def test_private_uiuc_gold_is_ignored_and_loads_when_present():
    assert subprocess.run(["git", "check-ignore", "-q", "data/private/gold/uiuc/labels.jsonl"], cwd=REPO).returncode == 0
    if not (UIUC_GOLD / "labels.jsonl").exists():
        pytest.skip("private UIUC gold not on this machine")
    pages = load_gold()
    assert len(pages) == 50 and label_counts(pages)["name"] == 50
