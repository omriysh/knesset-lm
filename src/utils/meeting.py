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
