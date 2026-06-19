"""Normalize funder award identifiers so OpenAlex works link back to our grants.

The output side (OpenAlex ``works[].awards[].funder_award_id``) and the input
side (our ingested grant ``source_id``) describe the *same* award with different
string conventions. This module produces a small set of canonical **join keys**
per award id so a work can be matched to a grant by set-intersection.

The hard cases are funder-specific:

NIH
    Application ids look like ``5R01CA068377-23`` = ``<app_type><activity><IC>
    <serial>-<support_year><suffix>``. OpenAlex variously reports the full id,
    ``R01CA068377``, ``CA068377``, or ``R01 CA 068377``. The stable identifier
    is the **IC + serial** (``CA068377``); the activity code (``R01``) is also
    stable when present. We emit both ``<IC><serial>`` and
    ``<activity><IC><serial>`` so either convention links.

NSF
    Our ids are the bare award number (``2540313``). OpenAlex reports the bare
    number or a directorate-prefixed form (``DEB-1832221``). We emit the bare
    digit run.

CORDIS / EC
    Project ids are bare numbers (``645651``); OpenAlex EC awards report the same
    grant number (sometimes with an ``H2020``/``FP7`` programme prefix). We emit
    the bare digit run.

UKRI
    Reference ids are UUIDs or council references (``MR/X006123/1``). We emit the
    upper-cased compact form.

For any id, we additionally always emit the aggressively-normalized compact
string (alnum-only, upper) as a last-resort exact key, so a clean equal match
links even for funders without a bespoke rule.
"""

from __future__ import annotations

import re

# NIH activity codes are a letter-block then 2 digits (R01, U54, P30, T32, F31,
# K08, DP2, ...). IC is two letters. Serial is >=4 digits.
_NIH_RE = re.compile(
    r"""^\s*
    (?P<apptype>\d)?              # application type (1=new,2=renewal,5=noncomp,...)
    (?P<activity>[A-Z]{1,3}\d{2})  # activity code, e.g. R01, U54, DP2
    \s*
    (?P<ic>[A-Z]{2})              # institute/center, e.g. CA, AI, GM
    \s*
    (?P<serial>\d{4,7})           # serial number
    (?:[-\s]?(?P<year>\d{1,2}))?  # support year
    (?P<suffix>[A-Z]\d+)?         # amendment suffix, e.g. S1, A1
    \s*$""",
    re.VERBOSE,
)

# A bare NIH "IC+serial" (no activity), e.g. "CA068377", "MH120498".
_NIH_IC_SERIAL_RE = re.compile(r"^\s*([A-Z]{2})\s*(\d{4,7})\s*$")


def _compact(s: str) -> str:
    """Alnum-only, upper-cased -- the last-resort exact key."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def nih_core_keys(award: str) -> set[str]:
    """Canonical NIH join keys: {IC+serial, activity+IC+serial} when parseable."""
    if not award:
        return set()
    s = award.strip().upper()
    m = _NIH_RE.match(s)
    if m:
        ic = m.group("ic")
        serial = m.group("serial")
        activity = m.group("activity")
        return {f"{ic}{serial}", f"{activity}{ic}{serial}"}
    m = _NIH_IC_SERIAL_RE.match(s)
    if m:
        return {f"{m.group(1)}{m.group(2)}"}
    return set()


def _digits(s: str) -> str | None:
    """The longest run of digits in a string (NSF/EC bare award numbers)."""
    runs = re.findall(r"\d{3,}", s or "")
    return max(runs, key=len) if runs else None


def award_keys(source: str, award_id: str) -> set[str]:
    """Canonical join keys for an award id, given the funder source.

    ``source`` is one of our connector keys (``nih``, ``nsf``, ``cordis``,
    ``ukri``) or ``None``/unknown (we then emit only generic keys). Always
    includes the generic compact key so a clean exact match links regardless.
    """
    award_id = (award_id or "").strip()
    if not award_id:
        return set()
    keys: set[str] = set()
    compact = _compact(award_id)
    if compact:
        keys.add(compact)

    src = (source or "").lower()
    if src == "nih":
        keys |= {f"NIH:{k}" for k in nih_core_keys(award_id)}
    elif src in ("nsf", "cordis", "ec"):
        d = _digits(award_id)
        if d:
            keys.add(f"{src.upper()}:{d}")
            keys.add(d)  # bare digits also as a generic key
    elif src == "ukri":
        keys.add(f"UKRI:{compact}")
    return keys


def grant_keys(source: str, source_id: str) -> set[str]:
    """Canonical join keys for one of *our* grant source ids.

    Mirrors :func:`award_keys` so a grant and a work-award produce overlapping
    keys exactly when they are the same award.
    """
    source_id = (source_id or "").strip()
    if not source_id:
        return set()
    keys: set[str] = set()
    compact = _compact(source_id)
    if compact:
        keys.add(compact)

    src = (source or "").lower()
    if src == "nih":
        keys |= {f"NIH:{k}" for k in nih_core_keys(source_id)}
    elif src in ("nsf", "cordis", "ec"):
        d = _digits(source_id)
        if d:
            keys.add(f"{src.upper()}:{d}")
            keys.add(d)
    elif src == "ukri":
        keys.add(f"UKRI:{compact}")
    return keys
