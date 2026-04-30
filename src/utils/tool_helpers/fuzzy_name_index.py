"""In-memory fuzzy name index for entity resolution (design §5.4).

Replaces BM25 phrase-matching for entity name lookup (MKs, committees,
bills, votes). Loads all index entries into memory and scores each with
RapidFuzz, combining a label score (WRatio — handles typos and word-order
swaps on short strings) and a weighted body score (partial_token_set_ratio
— handles queries shorter than the description).

Public surface:
  * :class:`FuzzyNameIndex`
"""

from __future__ import annotations

import json

import config
from rapidfuzz import fuzz

# label match is higher confidence than description match
_BODY_WEIGHT: float = getattr(config, "FUZZY_BODY_SCORE_WEIGHT", 0.85)


class FuzzyNameIndex:
    """In-memory fuzzy index built from a :class:`BM25Index` data store."""

    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries  # [{id, label, body, extra}]

    @classmethod
    def from_bm25(cls, bm25_index) -> "FuzzyNameIndex":
        """Scan all rows of an open BM25Index into memory."""
        con = bm25_index._connect()
        rows = con.execute("SELECT id, label, body, extra FROM entries").fetchall()
        entries: list[dict] = []
        for row in rows:
            extra = row["extra"] or "{}"
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except Exception:
                    extra = {}
            entries.append({
                "id":    str(row["id"] or ""),
                "label": str(row["label"] or ""),
                "body":  str(row["body"] or ""),
                "extra": extra,
            })
        return cls(entries)

    def search(
        self,
        query: str,
        top_k: int = 5,
        threshold: float = 55.0,
    ) -> list[dict]:
        """Return top-k fuzzy matches for query.

        Each entry is scored as max(WRatio(query, label),
        partial_token_set_ratio(query, body) * BODY_WEIGHT).
        Only entries scoring >= threshold are returned.

        Returns dicts: {id, label, score (0–1), extra, fetched: False}.
        """
        if not query or not self._entries:
            return []

        scored: list[tuple[float, dict]] = []
        for entry in self._entries:
            label_score = fuzz.WRatio(query, entry["label"])
            body_score  = fuzz.partial_token_set_ratio(query, entry["body"]) * _BODY_WEIGHT
            score = max(label_score, body_score)
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
