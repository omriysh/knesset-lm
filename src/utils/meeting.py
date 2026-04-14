"""
meeting.py

Helpers for loading and preparing meeting protocol data.
"""

import json
import re
from pathlib import Path

from config import MAX_CHUNK_CHARS

# Matches "ח"כ" (with various quote chars) followed by 1–4 Hebrew words.
# Used to extract MK names from the attendance section of raw protocol text.
_MK_TITLE_RE = re.compile(
    r'ח["\u05f3\u05f4\u2019\u201d]כ\s+([\u05d0-\u05ea]+(?:\s+[\u05d0-\u05ea]+){0,3})',
    re.MULTILINE,
)

# Matches speaker-turn headers in OData full_text protocols.
# Handles:  "היו"ר שם:"  "ח"כ שם:"  "שם (מפלגה):"  "שם:"
# Requires colon at end of line (no body text after it on same line).
# Uses [ \t]+ (not \s+) between name tokens to avoid crossing line boundaries.
# NOTE: full_text must be LF-normalised before use — CR-only PDFs fool re.MULTILINE.
_SPEAKER_TURN_RE = re.compile(
    r'^('
    r'(?:היו["\u05f3\u05f4\u2019\u201d]ר[ \t]+|ח["\u05f3\u05f4\u2019\u201d]כ[ \t]+|'
    r'(?:סגן[ \t]+)?שר(?:ת)?[ \t]+|ממלא[ \t]+מקום[ \t]+)?'   # optional title prefix
    r'[\u05d0-\u05ea][\u05d0-\u05ea\-\u05f3\u05f4"\']{0,20}'  # first name token
    r'(?:[ \t]+[\u05d0-\u05ea][\u05d0-\u05ea\-\u05f3\u05f4"\']{0,20}){0,3}'  # up to 3 more
    r')'
    r'(?:[ \t]*\([^)\n]{1,40}\))?'   # optional (party / role)
    r'[ \t]*:[ \t]*$',               # colon at end of line only
    re.MULTILINE,
)


def load_meeting(filepath: str | Path) -> dict:
    """Load a meeting JSON file and return its contents."""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def build_transcript_text(meeting: dict) -> str:
    """
    Format a meeting into a single transcript string for the LLM.

    Supports two source formats:
    - 'speeches': list of {speaker, text_he} dicts (oknesset.org scraper format)
    - 'full_text': raw protocol string (OData PDF extraction format)
    """
    if "full_text" in meeting:
        return meeting["full_text"]

    lines = []
    for speech in meeting.get("speeches", []):
        speaker = speech.get("speaker", "").strip()
        text    = speech.get("text_he", "").strip()
        if speaker or text:
            lines.append(f"{speaker}: {text}")
    return "\n\n".join(lines)


def extract_attendance(meeting: dict) -> list[str]:
    """
    Extract attendee names from a meeting protocol.

    For structured (speeches) format: returns unique speaker names in order of
    first appearance. Includes MKs, ministers, officials — whoever spoke.

    For raw-text format: regex-scans the first 5000 characters (the header section
    of most Knesset protocols) for names prefixed by standard MK title markers
    (ח"כ / חבר הכנסת / חברת הכנסת). Returns deduplicated names.

    Returns an empty list if no names are found.
    """
    if "speeches" in meeting:
        seen: list[str] = []
        seen_set: set[str] = set()
        for speech in meeting["speeches"]:
            speaker = speech.get("speaker", "").strip()
            if speaker and speaker not in seen_set:
                seen.append(speaker)
                seen_set.add(speaker)
        return seen

    if "full_text" in meeting:
        header = meeting["full_text"][:5000]
        seen = []
        seen_set = set()
        for name in _MK_TITLE_RE.findall(header):
            name = name.strip()
            if name and name not in seen_set:
                seen.append(name)
                seen_set.add(name)
        return seen

    return []


def parse_full_text_speeches(full_text: str) -> list[dict] | None:
    """
    Parse a raw OData full_text protocol into [{speaker, text_he}] entries.

    Splits on speaker-turn headers (e.g. 'היו"ר שם:' / 'שם (מפלגה):').
    Returns None if fewer than 2 speaker turns are found (triggers fallback).
    """
    # PDF extraction often produces CR-only line endings; re.MULTILINE ^ only
    # matches after \n, so normalise before applying the regex.
    full_text = full_text.replace('\r\n', '\n').replace('\r', '\n')

    matches = list(_SPEAKER_TURN_RE.finditer(full_text))
    if len(matches) < 2:
        return None

    speeches: list[dict] = []
    for i, m in enumerate(matches):
        speaker = m.group(1).strip()
        text_start = m.end()
        text_end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        text = full_text[text_start:text_end].strip()
        if text:
            speeches.append({"speaker": speaker, "text_he": text})

    return speeches if speeches else None


def chunk_transcript(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """
    Split a transcript into chunks that each fit within max_chars.
    Splits on speech boundaries (double newline). Returns a list of chunk strings.
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        cutoff = remaining.rfind("\n\n", 0, max_chars)
        if cutoff == -1:
            cutoff = max_chars
        chunks.append(remaining[:cutoff])
        remaining = remaining[cutoff:].lstrip()

    if remaining:
        chunks.append(remaining)

    return chunks
