"""
meeting.py

Helpers for loading and preparing meeting protocol data.
"""

import json
from pathlib import Path

from config import MAX_CHUNK_CHARS


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
