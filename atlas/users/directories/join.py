from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from atlas.users.directories.base import JOIN_MATCH_TIER, FacultyRecord
from atlas.users.directories.domains import normalize_ror
from atlas.users.directories.optout import Suppression, norm_name


def name_key(name: str | None, ror_id: str | None) -> tuple[str, str, str] | None:
    tokens = norm_name(name).split()
    if len(tokens) < 2 or not ror_id:
        return None
    return tokens[-1], tokens[0][0], normalize_ror(ror_id)


def join_records(records: Iterable[FacultyRecord], people: Iterable[dict],
                 suppression: Suppression) -> list[tuple[FacultyRecord, str]]:
    index: dict[tuple, list[dict]] = defaultdict(list)
    for p in people:
        if suppression.blocks_record(p):
            continue
        if (k := name_key(p.get("name"), p.get("ror_id"))):
            index[k].append(p)
    joined = []
    for rec in records:
        if suppression.blocks_record(rec):
            continue
        k = name_key(rec.name, rec.ror_id)
        cands = index.get(k, []) if k else []
        if len(cands) == 1:
            rec.match_tier = JOIN_MATCH_TIER
            joined.append((rec, cands[0]["atlas_id"]))
    return joined
