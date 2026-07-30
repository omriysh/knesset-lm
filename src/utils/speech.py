"""
speech.py

Direct speaker-filtered access to local protocol files.

Unlike the RAG pipeline (which uses embeddings + ChromaDB), this module scans
protocol JSON files on disk and returns full speech text for a named MK.
No index required — just the raw transcriptions.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher, get_close_matches
from pathlib import Path
from typing import Optional

from utils.meeting import load_meeting

_HCK_RE = re.compile(r'''ח["׳']\s*כ\s*''')


# ── Name matching ─────────────────────────────────────────────────────────────

def _clean_name(name: str) -> str:
    """Remove ח\"כ prefix, drop party/faction parentheticals, normalise space."""
    name = _HCK_RE.sub("", name)
    name = re.sub(r"\([^)]*\)", " ", name)   # e.g. "... סטרוק (הציונות הדתית)"
    return name.strip()


def name_tokens(name: str) -> list[str]:
    """Whitespace-split tokens of a name, with the ח\"כ prefix removed.

    Titles/middle names survive as their own tokens so token-subset matching
    can tolerate them (see :func:`name_query_matches`)."""
    return [t for t in re.split(r"\s+", _clean_name(name)) if t]


def _tok_match(a: str, b: str, threshold: float) -> bool:
    return a == b or SequenceMatcher(None, a, b).ratio() >= threshold


def _tokens_subset(a: list[str], b: list[str], threshold: float) -> bool:
    """True if every token in *a* matches (exact or fuzzy) some token in *b*."""
    return all(any(_tok_match(ta, tb, threshold) for tb in b) for ta in a)


def name_query_matches(query: str, speaker: str, *, threshold: float = 0.82) -> bool:
    """True if *query* and *speaker* name the same person, token-subset style.

    Matches when the smaller token set is a (fuzzy) subset of the larger, so
    extra tokens on *either* side are tolerated —

      * ministerial titles on the speaker side
        (``"אורית סטרוק"`` ⊆ ``"שרת ההתיישבות והמשימות הלאומיות אורית סטרוק"``)
      * a middle name on the speaker side
        (``"אורית סטרוק"`` ⊆ ``"אורית מלכה סטרוק"``)
      * a fuller query than the stored form
        (``"אורית מלכה סטרוק"`` ⊇ ``"אורית סטרוק"``)

    For multi-token names on both sides the surname (last token — Hebrew MK
    names carry the family name last, titles come first) must also agree. That
    stops a *different* person who merely shares a first/middle name from
    matching (e.g. query ``"אורית מלכה סטרוק"`` must NOT match ``"אורי מלכה"``).
    Single-token queries (first- or last-name only) skip the surname anchor —
    they are inherently broad.

    Replaces the old contiguous-substring test, which silently missed a
    speaker whenever a title or middle name broke up the queried name.
    """
    q = name_tokens(query)
    s = name_tokens(speaker)
    if not q or not s:
        return False
    if not (_tokens_subset(q, s, threshold) or _tokens_subset(s, q, threshold)):
        return False
    if len(q) == 1 or len(s) == 1:
        return True
    return _tok_match(q[-1], s[-1], threshold)


def _name_matches(query: str, speaker: str, threshold: float = 0.82) -> bool:
    """True if query matches the speaker field (token-subset, fuzzy-tolerant)."""
    return name_query_matches(query, speaker, threshold=threshold)


# ── Committee directory lookup ────────────────────────────────────────────────

def _find_committee_dir(transcriptions_root: Path, knesset_num: int, committee: str) -> Optional[Path]:
    """Return the closest matching committee directory."""
    base = transcriptions_root / str(knesset_num)
    if not base.exists():
        return None

    dirs = [d for d in base.iterdir() if d.is_dir()]
    if not dirs:
        return None

    normalized: dict[str, Path] = {d.name.replace("_", " "): d for d in dirs}
    query = committee.replace("_", " ").strip()

    if query in normalized:
        return normalized[query]

    for name, d in normalized.items():
        if query in name or name in query:
            return d

    hits = get_close_matches(query, normalized.keys(), n=1, cutoff=0.45)
    return normalized[hits[0]] if hits else None


# ── Public API ────────────────────────────────────────────────────────────────

def get_mk_speeches_in_committee(
    mk_name: str,
    committee: str,
    transcriptions_root: Path,
    *,
    max_meetings: int = 20,
    knesset_num: int = 25,
) -> str:
    """
    Return all speeches by a named MK from the most recent `max_meetings`
    protocol files in the given committee directory.

    Fuzzy-matches both the committee directory name and the MK name inside
    each speech.  Returns a formatted string ready for the LLM context.

    Parameters
    ----------
    mk_name             : MK name (Hebrew, full or partial)
    committee           : committee name (Hebrew, full or partial)
    transcriptions_root : path to Data/raw_transcriptions/
    max_meetings        : how many of the most recent meetings to scan
    knesset_num         : Knesset number (used as a subdirectory)
    """
    if not mk_name:
        return "נדרש שם חבר הכנסת."
    if not committee:
        return "נדרש שם הוועדה."

    committee_dir = _find_committee_dir(transcriptions_root, knesset_num, committee)
    if committee_dir is None:
        base      = transcriptions_root / str(knesset_num)
        available = (
            ", ".join(
                d.name.replace("_", " ")
                for d in sorted(base.iterdir())
                if d.is_dir()
            )
            if base.exists() else "אין"
        )
        return (
            f"לא נמצאה ועדה התואמת '{committee}'.\n"
            f"ועדות זמינות: {available}"
        )

    json_files = sorted(committee_dir.glob("*.json"), reverse=True)[:max_meetings]
    if not json_files:
        return f"לא נמצאו קבצי פרוטוקול בתיקיית '{committee_dir.name}'."

    results: list[tuple] = []   # (date, meeting_id, committee_name, list[str])

    for json_path in json_files:
        try:
            meeting = load_meeting(json_path)
        except Exception:
            continue

        speeches = meeting.get("speeches") or []
        if not speeches:
            continue

        matching_texts = [
            s.get("text_he", "").strip()
            for s in speeches
            if _name_matches(mk_name, s.get("speaker", ""))
            and s.get("text_he", "").strip()
        ]

        if matching_texts:
            results.append((
                str(meeting.get("date", "")),
                str(meeting.get("meeting_id", json_path.stem)),
                str(meeting.get("committee", committee_dir.name.replace("_", " "))),
                matching_texts,
            ))

    if not results:
        return (
            f"לא נמצאו דברי ח\"כ {mk_name} ב-{max_meetings} הישיבות האחרונות "
            f"של {committee_dir.name.replace('_', ' ')}.\n"
            f"בדוק שהשם נכון (נסה שם מלא/חלקי שונה)."
        )

    total_speeches = sum(len(r[3]) for r in results)
    header = (
        f"נמצאו {total_speeches} נאומים של ח\"כ {mk_name} "
        f"ב-{len(results)} ישיבות של "
        f"{committee_dir.name.replace('_', ' ')}:\n\n"
    )

    parts = []
    for date, meeting_id, comm_name, speech_texts in results:
        block = (
            f"### ישיבה {meeting_id}  ({date}, {comm_name})\n"
            + "\n\n".join(speech_texts)
        )
        parts.append(block)

    return header + "\n\n---\n\n".join(parts)
