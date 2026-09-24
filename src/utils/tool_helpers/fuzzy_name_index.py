"""In-memory fuzzy name index for entity resolution (design §5.4).

Replaces BM25 phrase-matching for entity name lookup (MKs, committees,
bills, votes). Loads all index entries into memory and scores each with
RapidFuzz, combining a label score (WRatio — handles typos and word-order
swaps on short strings) and a weighted body score (partial_token_set_ratio
— handles queries shorter than the description).

On top of the fuzzy score, a token-containment rule accepts middle-name
variants ("אביחי בוארון" vs "אביחי אברהם בוארון") that WRatio scores at
0.85 — below the precision-preserving participant threshold of 0.90.

Public surface:
  * :class:`FuzzyNameIndex`
"""

from __future__ import annotations

import re

import config
from rapidfuzz import fuzz

# label match is higher confidence than description match
_BODY_WEIGHT: float = getattr(config, "FUZZY_BODY_SCORE_WEIGHT", 0.85)

# score assigned to a token-containment match (see _is_middle_name_variant)
_CONTAINMENT_SCORE: float = getattr(config, "FUZZY_TOKEN_CONTAINMENT_SCORE", 95.0)

_TRAILING_PARENTHETICAL_RE = re.compile(r"\s*[\(\[][^()\[\]]*[\)\]]\s*$")

_QUOTE_TRANSLATION = str.maketrans({
    "״": '"',   # ״ gershayim
    "“": '"',   # “
    "”": '"',   # ”
    "׳": "'",   # ׳ geresh
    "‘": "'",   # ‘
    "’": "'",   # ’
    "`": "'",   # `
})


def _normalize_name(text: str) -> str:
    """Strip a trailing (party) parenthetical, unify quotes, collapse whitespace."""
    cleaned = _TRAILING_PARENTHETICAL_RE.sub("", text.translate(_QUOTE_TRANSLATION))
    return " ".join(cleaned.split())


def _name_tokens(text: str) -> list[str]:
    """Normalized name split into tokens, dropping punctuation-only tokens."""
    return [t for t in _normalize_name(text).split(" ") if any(c.isalnum() for c in t)]


def _is_middle_name_variant(query_tokens: list[str], label_tokens: list[str]) -> bool:
    """True when the two names differ only by inserted interior (middle) tokens.

    Requires one token set to be a proper subset of the other, at least two
    shared tokens, and agreement on both the first (given name) and last
    (surname) token — so "אביחי בוארון" ⊂ "אביחי אברהם בוארון" is accepted
    while "אברהם בצלאל" vs "אביחי אברהם בוארון" is not.
    """
    if len(query_tokens) < 2 or len(label_tokens) < 2:
        return False
    query_set, label_set = set(query_tokens), set(label_tokens)
    if not (query_set < label_set or label_set < query_set):
        return False
    if len(query_set & label_set) < 2:
        return False
    return (query_tokens[0] == label_tokens[0]
            and query_tokens[-1] == label_tokens[-1])


class FuzzyNameIndex:
    """In-memory fuzzy index over ``[{id, label, body, extra}, ...]`` entries
    (see retrieval.knesset_db_store.name_entries)."""

    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries  # [{id, label, body, extra}]
        self._normalized_labels = [_normalize_name(e["label"]) for e in entries]
        self._label_tokens = [_name_tokens(e["label"]) for e in entries]

    def search(
        self,
        query: str,
        top_k: int = 5,
        threshold: float = 55.0,
    ) -> list[dict]:
        """Return top-k fuzzy matches for query.

        Each entry is scored as max(WRatio(query, label),
        WRatio(normalized query, normalized label),
        partial_token_set_ratio(query, body) * BODY_WEIGHT). Names that
        differ only by an interior middle token score _CONTAINMENT_SCORE.
        Only entries scoring >= threshold are returned.

        Returns dicts: {id, label, score (0–1), extra, fetched: False}.
        """
        if not query or not self._entries:
            return []

        normalized_query = _normalize_name(query)
        query_tokens = _name_tokens(query)

        scored: list[tuple[float, dict]] = []
        for position, entry in enumerate(self._entries):
            label_score = max(
                fuzz.WRatio(query, entry["label"]),
                fuzz.WRatio(normalized_query, self._normalized_labels[position]),
            )
            body_score = fuzz.partial_token_set_ratio(query, entry["body"]) * _BODY_WEIGHT
            score = max(label_score, body_score)
            if _is_middle_name_variant(query_tokens, self._label_tokens[position]):
                score = max(score, _CONTAINMENT_SCORE)
            if score >= threshold:
                scored.append((score, entry))

        scored.sort(key=lambda t: t[0], reverse=True)

        return [
            {
                "id":      e["id"],
                "label":   e["label"],
                "score":   s / 100.0,
                "extra":   e["extra"],
                "fetched": False,
            }
            for s, e in scored[:top_k]
        ]


__all__ = ["FuzzyNameIndex"]
