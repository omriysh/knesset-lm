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

_NAME_TOKEN_SEPARATOR_RE = re.compile(r"[\s|\-־,]+")
_TOKEN_QUOTES = "\"'"

_TRAILING_PARENTHETICAL_RE = re.compile(r"\s*[\(\[][^()\[\]]*[\)\]]\s*$")

_PUNCTUATION_RE = re.compile(r"[^\w\"']")

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


def _punctuation_free_tokens(text: str) -> list[str]:
    """Normalized name tokens with commas and other punctuation removed, for exact/prefix comparison."""
    stripped_tokens = (_PUNCTUATION_RE.sub("", token) for token in _normalize_name(text).split(" "))
    return [token for token in stripped_tokens if token]


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


def _separated_name_tokens(text: str) -> list[str]:
    """Tokens split on whitespace, dashes, commas and the " | " alias separator, quotes stripped."""
    stripped = (token.strip(_TOKEN_QUOTES) for token in _NAME_TOKEN_SEPARATOR_RE.split(text.translate(_QUOTE_TRANSLATION)))
    return [token for token in stripped if token]


def _query_tokens_needed(query_token_count: int) -> int:
    return query_token_count if query_token_count <= 2 else query_token_count - 1


def _covers_enough_query_tokens(query_tokens: list[str], entry_tokens: set[str]) -> bool:
    """True when most query tokens (every token of a 1-2 token query) fuzzily equal a label/alias token."""
    matched = sum(1 for query_token in query_tokens
                  if any(fuzz.ratio(query_token, entry_token) >= config.FUZZY_NAME_TOKEN_MATCH_MIN_RATIO
                         for entry_token in entry_tokens))
    return matched >= _query_tokens_needed(len(query_tokens))


class FuzzyNameIndex:
    """In-memory fuzzy index over ``[{id, label, body, extra}, ...]`` entries
    (see retrieval.knesset_db_store.name_entries).

    require_query_token_coverage (person names): an entry matching fewer query tokens than
    _query_tokens_needed is capped at FUZZY_PARTIAL_NAME_MAX_SCORE, so one shared first
    name or surname never reads as a confident match.
    """

    def __init__(self, entries: list[dict], require_query_token_coverage: bool = False) -> None:
        self._entries = entries  # [{id, label, body, extra}]
        self._normalized_labels = [_normalize_name(e["label"]) for e in entries]
        self._label_tokens = [_name_tokens(e["label"]) for e in entries]
        self._punctuation_free_label_tokens = [_punctuation_free_tokens(e["label"]) for e in entries]
        self._require_query_token_coverage = require_query_token_coverage
        self._label_and_alias_tokens = [
            set(_separated_name_tokens(f"{e['label']} | {e.get('body') or ''}")) for e in entries
        ] if require_query_token_coverage else []

    def __len__(self) -> int:
        return len(self._entries)

    def unambiguous_label_match(self, query: str) -> str | None:
        """Id of the one entry whose label equals the query, or else of the one
        entry whose label starts with the query's (2+) tokens; None when no
        entry or several entries qualify.

        Tokens are compared without punctuation, so "ועדת החוקה" resolves to
        "ועדת החוקה, חוק ומשפט" while "ועדת המשנה" (many subcommittees) does not.
        """
        query_tokens = _punctuation_free_tokens(query)
        if not query_tokens:
            return None
        exact_ids = {entry["id"] for entry, label_tokens
                     in zip(self._entries, self._punctuation_free_label_tokens)
                     if label_tokens == query_tokens}
        if exact_ids:
            return exact_ids.pop() if len(exact_ids) == 1 else None
        if len(query_tokens) < 2:
            return None
        prefix_ids = {entry["id"] for entry, label_tokens
                      in zip(self._entries, self._punctuation_free_label_tokens)
                      if label_tokens[:len(query_tokens)] == query_tokens}
        return prefix_ids.pop() if len(prefix_ids) == 1 else None

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
        coverage_query_tokens = _separated_name_tokens(normalized_query)

        scored: list[tuple[float, dict]] = []
        for position, entry in enumerate(self._entries):
            label_score = max(
                fuzz.WRatio(query, entry["label"]),
                fuzz.WRatio(normalized_query, self._normalized_labels[position]),
            )
            body_score = fuzz.partial_token_set_ratio(query, entry["body"]) * _BODY_WEIGHT
            score = max(label_score, body_score)
            if self._require_query_token_coverage and not _covers_enough_query_tokens(
                    coverage_query_tokens, self._label_and_alias_tokens[position]):
                score = min(score, config.FUZZY_PARTIAL_NAME_MAX_SCORE)
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
