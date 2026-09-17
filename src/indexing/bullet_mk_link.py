"""
bullet_mk_link.py

Link summary opinion bullets to MK identities.

Opinion bullets carry the speaker inside the text — "ח\"כ שם (מפלגה): עמדה…"
(the markdown ``**Name:** text`` form with bold already stripped by
parse_summary). This module extracts that prefix and resolves it to an mk_id
against a roster, using the same fuzzy machinery (and threshold) as
meeting-participant resolution.

A full-scan profile of the 176,992 bullets in Data/bm25/25/bullets.db
(2026-08) drove the rules here: 62,753 bullets have a 1–5-word colon prefix,
~27% of which are MKs; the rest are guests/officials (resolution correctly
fails those) plus structural leaks ("תאריך:", "מזהה ישיבה:", "נוכחים:")
that need an explicit stoplist. Verb-first attributions without a colon
("X ציין כי…", "לדברי X…") total only ~700 bullets and are deliberately
out of scope.

Used by scripts/backfill_bullet_mk_ids.py; the search_opinions tool then
relies purely on the mk_id metadata this produces.
"""

from __future__ import annotations

import re

import config
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex, _normalize_name

# Leading tokens that are titles/roles, not name parts. Quote characters are
# normalized (״→", ׳→') by _normalize_name before this set is consulted.
# "הוועדה" is included so "יו\"ר הוועדה" strips fully; no MK name starts
# with it.
_TITLE_TOKENS = {
    'ח"כ', 'חה"כ', 'היו"ר', 'יו"ר', 'מ"מ', 'הוועדה', 'הועדה',
    'השר', 'השרה', 'שר', 'שרת', 'עו"ד', 'ד"ר', "פרופ'", 'תא"ל', 'תנ"צ',
}

# Structural labels that satisfy the shape rules but are never speakers —
# metadata leaks and section subheadings observed in the bullet profile.
_STRUCTURAL_STOPLIST = {
    "תאריך", "מזהה ישיבה", "נוכחים", "נעדרים", "עמדות מרכזיות",
    "סדר היום", "חדש", "הצעת חוק", "ייעוץ משפטי", "מוזמנים", "משתתפים",
    "חברי הוועדה", "חברי הועדה", "חברי הכנסת", "נושאים נוספים",
}

# A colon that far into the bullet is mid-sentence, not a speaker prefix.
_MAX_RAW_PREFIX_WORDS = 8

_MIN_NAME_WORDS = 2
_MAX_NAME_WORDS = 5

_LETTERS_RE = re.compile(r"[א-תA-Za-z]{2,}")


def extract_speaker_prefix(text: str) -> str | None:
    """Return the cleaned speaker-name candidate from a bullet, or None.

    Cleaned means: quotes normalized, trailing (party/role) parenthetical
    dropped, leading titles stripped. Non-name prefixes (structural labels,
    digits, too short/long) return None. The result may still be a non-MK
    person (guest, legal advisor) — resolution against a roster is the
    caller's precision filter.
    """
    text = (text or "").strip()
    if ":" not in text:
        return None
    raw_prefix = text.split(":", 1)[0].strip()
    if not raw_prefix or len(raw_prefix.split()) > _MAX_RAW_PREFIX_WORDS:
        return None
    if any(ch.isdigit() for ch in raw_prefix):
        return None

    normalized = _normalize_name(raw_prefix)
    if not normalized or normalized in _STRUCTURAL_STOPLIST:
        return None

    tokens = normalized.split()
    while tokens and tokens[0] in _TITLE_TOKENS:
        tokens.pop(0)
    if not (_MIN_NAME_WORDS <= len(tokens) <= _MAX_NAME_WORDS):
        return None

    name = " ".join(tokens)
    if name in _STRUCTURAL_STOPLIST or not _LETTERS_RE.search(name):
        return None
    return name


def build_roster_index(roster: list[dict]) -> FuzzyNameIndex:
    """Build a reusable fuzzy index over ``[{mk_id, name}, ...]`` roster rows.

    Backfill callers should build this once (per roster) instead of paying
    index construction per bullet.
    """
    return FuzzyNameIndex([
        {"id": str(mk["mk_id"]), "label": mk["name"], "body": "", "extra": {}}
        for mk in roster
    ])


def resolve_prefix(
    prefix: str,
    roster_index: FuzzyNameIndex,
    participant_mk_ids: set[str] | None = None,
) -> dict | None:
    """Resolve an extracted prefix to ``{"mk_id", "mk_name"}`` or None.

    Matches at PARTICIPANT_FUZZY_THRESHOLD (the precision-preserving bar
    proven for attendance resolution). When ``participant_mk_ids`` is given,
    a lower-ranked candidate who actually attended the meeting wins over a
    non-attendee — disambiguation for similar names, not a threshold bypass.
    """
    hits = roster_index.search(
        prefix, top_k=5, threshold=config.PARTICIPANT_FUZZY_THRESHOLD)
    if not hits:
        return None
    chosen = hits[0]
    if participant_mk_ids:
        for hit in hits:
            if hit["id"] in participant_mk_ids:
                chosen = hit
                break
    return {"mk_id": chosen["id"], "mk_name": chosen["label"]}


def resolve_bullet_mk(
    text: str,
    roster: list[dict],
    participant_mk_ids: set[str] | None = None,
) -> dict | None:
    """Extract + resolve in one step (convenience for small rosters/tests).

    Returns ``{"mk_id", "mk_name", "speaker"}`` where ``speaker`` is the
    cleaned prefix as written in the bullet, or None when the bullet has no
    resolvable MK speaker.
    """
    prefix = extract_speaker_prefix(text)
    if not prefix or not roster:
        return None
    resolved = resolve_prefix(prefix, build_roster_index(roster), participant_mk_ids)
    if resolved is None:
        return None
    return {**resolved, "speaker": prefix}
