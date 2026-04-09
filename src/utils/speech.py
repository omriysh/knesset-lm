"""
speech.py

Direct speaker-filtered access to local protocol files.

Unlike the RAG pipeline (which uses embeddings + ChromaDB), this module scans
protocol JSON files on disk and returns full speech text for a named MK.
No index required — just the raw transcriptions.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher, get_close_matches
from pathlib import Path
from typing import Optional

_HCK_RE = re.compile(r'''ח["׳']\s*כ\s*''')


# ── Name matching ─────────────────────────────────────────────────────────────

def _clean_name(name: str) -> str:
    """Remove ח\"כ prefix and normalise whitespace."""
    return _HCK_RE.sub("", name).strip()


def _name_matches(query: str, speaker: str, threshold: float = 0.65) -> bool:
    """True if query fuzzy-matches the speaker field."""
    q = _clean_name(query)
    s = _clean_name(speaker)
    if not q or not s:
        return False
    if q in s or s in q:
        return True
    return SequenceMatcher(None, q, s).ratio() >= threshold


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
            with open(json_path, encoding="utf-8") as f:
                meeting = json.load(f)
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
