"""
indexing/speaker_link.py

Resolve a protocol speaker label ("ח\"כ שם (מפלגה)", "היו\"ר שם",
"סגן שר החקלאות ופיתוח הכפר שם") to an mk_id against the MK roster, using the
same fuzzy machinery and threshold as attendance resolution.

Used by scripts/build_knesset_db.py when loading summary opinions; guests and
officials simply fail resolution and keep mk_id NULL.
"""

from __future__ import annotations

import re

import config
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex, _normalize_name

# Leading tokens that are titles/roles, not name parts. Quote characters are
# normalized (״→", ׳→') by _normalize_name before this set is consulted.
_TITLE_TOKENS = {
    'ח"כ', 'חה"כ', 'היו"ר', 'יו"ר', 'מ"מ', 'הוועדה', 'הועדה',
    'השר', 'השרה', 'שר', 'שרת', 'סגן', 'סגנית', 'עו"ד', 'ד"ר', "פרופ'", 'תא"ל', 'תנ"צ',
}

# Labels that are never a person.
_STRUCTURAL_STOPLIST = {
    "קריאה", "קריאות", "תאריך", "מזהה ישיבה", "נוכחים", "נעדרים", "מוזמנים", "משתתפים",
    "חברי הוועדה", "חברי הועדה", "חברי הכנסת", "ייעוץ משפטי",
}

_MIN_NAME_WORDS = 2
_MAX_NAME_WORDS = 5
_TRAILING_WINDOWS = (2, 3, 4)
_LETTERS_RE = re.compile(r"[א-תA-Za-z]{2,}")


def clean_speaker_label(label: str) -> str | None:
    """
    Speaker label -> name candidate: quotes normalized, trailing parenthetical
    dropped, leading titles stripped. None for structural labels, digits, or a
    single word. The result may still be a non-MK person.
    """
    normalized = _normalize_name((label or "").strip())
    if not normalized or normalized in _STRUCTURAL_STOPLIST:
        return None
    if any(ch.isdigit() for ch in normalized):
        return None
    tokens = normalized.split()
    while tokens and tokens[0] in _TITLE_TOKENS:
        tokens.pop(0)
    if len(tokens) < _MIN_NAME_WORDS:
        return None
    name = " ".join(tokens)
    if name in _STRUCTURAL_STOPLIST or not _LETTERS_RE.search(name):
        return None
    return name


def build_roster_index(roster: list[dict]) -> FuzzyNameIndex:
    """[{mk_id, name, aliases?}] -> reusable fuzzy index (build once per roster)."""
    return FuzzyNameIndex([
        {"id": str(mk["mk_id"]), "label": mk["name"], "body": mk.get("aliases") or "", "extra": {}}
        for mk in roster
    ])


def _search(name: str, roster_index: FuzzyNameIndex, participant_mk_ids: set[str] | None) -> dict | None:
    hits = roster_index.search(name, top_k=5, threshold=config.PARTICIPANT_FUZZY_THRESHOLD)
    if not hits:
        return None
    chosen = hits[0]
    if participant_mk_ids:
        for hit in hits:
            if hit["id"] in participant_mk_ids:
                chosen = hit
                break
    return {"mk_id": chosen["id"], "mk_name": chosen["label"]}


def resolve_speaker(
    label: str,
    roster_index: FuzzyNameIndex,
    participant_mk_ids: set[str] | None = None,
) -> dict | None:
    """
    Label -> {"mk_id", "mk_name", "speaker_name"} or None.

    Names of up to _MAX_NAME_WORDS tokens are matched whole. Longer labels are
    role prefixes we do not know ("סגן שר החקלאות ופיתוח הכפר משה אבוטבול"), so
    the trailing 2..4 tokens are tried instead, shortest first. When
    participant_mk_ids is given, an attendee outranks a non-attendee among the
    candidates above the threshold (disambiguation, not a threshold bypass).
    """
    name = clean_speaker_label(label)
    if name is None:
        return None
    tokens = name.split()
    if len(tokens) <= _MAX_NAME_WORDS:
        candidates = [name]
    else:
        candidates = [" ".join(tokens[-n:]) for n in _TRAILING_WINDOWS]
    for candidate in candidates:
        hit = _search(candidate, roster_index, participant_mk_ids)
        if hit:
            return {**hit, "speaker_name": name}
    return None
