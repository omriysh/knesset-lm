"""
Parsers for the two-pass summarization outputs (see summarization.prompts).

The model answers in plain text: either the NOT_PROTOCOL sentinel, a "- " bullet
list of topics, or "- speaker || opinion || quote" lines. Python turns that into

    {"is_protocol": bool, "topics": [str], "opinions": [{"speaker", "opinion", "quote", "quote_verified"}]}

and verifies every quote against the transcript.
"""

import re

from config import NOT_PROTOCOL

_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s*")
_NIQQUD_RE        = re.compile(r"[֑-ׇ]")
_PUNCT_RE         = re.compile(r"[\"'“”‘’׳״.,;:!?()\[\]{}\-–—…]")
OPINION_SEPARATOR = "||"


def _normalize_sentinel(text: str) -> str:
    return " ".join(_NIQQUD_RE.sub("", text).split()).strip("\"'.:")


def is_not_protocol(text: str) -> bool:
    return _normalize_sentinel(text) == NOT_PROTOCOL


def bullet_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        line = _BULLET_PREFIX_RE.sub("", line).strip()
        if line:
            lines.append(line)
    return lines


def parse_topics(text: str) -> list[str] | None:
    """
    Topics pass output -> list of topics, [] for the NOT_PROTOCOL sentinel,
    None when the output is empty (caller retries).
    """
    stripped = text.strip()
    if not stripped:
        print("[output_parsing] empty topics output")
        return None
    if is_not_protocol(stripped):
        return []
    topics = bullet_lines(stripped)
    if not topics:
        print(f"[output_parsing] no topics found in: {stripped[:120]!r}")
        return None
    return topics


def parse_opinions(text: str) -> list[dict] | None:
    """
    Opinions pass output -> list of {"speaker", "opinion", "quote"} dicts,
    [] for the NOT_PROTOCOL sentinel, None when nothing usable came back.
    Lines without the separator are skipped with a printed warning.
    """
    stripped = text.strip()
    if not stripped:
        print("[output_parsing] empty opinions output")
        return None
    if is_not_protocol(stripped):
        return []

    opinions: list[dict] = []
    malformed = 0
    for line in bullet_lines(stripped):
        if OPINION_SEPARATOR not in line:
            malformed += 1
            print(f"[output_parsing] opinion line without separator: {line[:100]!r}")
            continue
        parts = [p.strip() for p in line.split(OPINION_SEPARATOR)]
        speaker = parts[0]
        opinion = parts[1] if len(parts) > 1 else ""
        quote   = f" {OPINION_SEPARATOR} ".join(parts[2:]).strip("\"'“”") if len(parts) > 2 else ""
        if not speaker or not opinion:
            malformed += 1
            print(f"[output_parsing] opinion line missing speaker or opinion: {line[:100]!r}")
            continue
        opinions.append({"speaker": speaker, "opinion": opinion, "quote": quote})

    if not opinions and malformed == 0:
        print(f"[output_parsing] no opinions found in: {stripped[:120]!r}")
        return None
    return opinions


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """normalize_for_match plus, for every normalized char, the raw index it came from."""
    out: list[str] = []
    positions: list[int] = []
    for i, ch in enumerate(text):
        if _NIQQUD_RE.match(ch):
            continue
        if ch.isspace() or _PUNCT_RE.match(ch):
            if out and out[-1] != " ":
                out.append(" ")
                positions.append(i)
            continue
        out.append(ch)
        positions.append(i)
    if out and out[-1] == " ":
        out.pop()
        positions.pop()
    return "".join(out), positions


def normalize_for_match(text: str) -> str:
    return _normalize_with_map(text)[0]


class QuoteLocator:
    """Normalizes a text once so many quotes can be located in it cheaply."""

    def __init__(self, text: str) -> None:
        self._haystack, self._positions = _normalize_with_map(text)

    def find(self, quote: str) -> int | None:
        """Raw character offset of the quote's first occurrence, or None."""
        needle = normalize_for_match(quote)
        if not needle:
            return None
        pos = self._haystack.find(needle)
        return self._positions[pos] if pos >= 0 else None


def locate_quote(quote: str, text: str) -> int | None:
    return QuoteLocator(text).find(quote)


def verify_quotes(opinions: list[dict], transcript: str) -> None:
    """Set opinion["quote_verified"]: the normalized quote is a substring of the transcript."""
    haystack = normalize_for_match(transcript)
    for opinion in opinions:
        needle = normalize_for_match(opinion.get("quote", ""))
        opinion["quote_verified"] = bool(needle) and needle in haystack
