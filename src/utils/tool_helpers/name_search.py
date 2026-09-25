"""Generic fuzzy name-resolution helper (design §5.4).

Single call: fuzzy-score all entries in a :class:`FuzzyNameIndex`, return
top-k candidates, optionally inline the top record when its score gap is
unambiguous.

Public surface:
  * :func:`name_search` — returns ranked candidate records.
"""

from __future__ import annotations

from typing import Callable

import config
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex


def name_search(
    query: str,
    *,
    fuzzy_index: FuzzyNameIndex,
    fetch_by_id: Callable[[str], dict | None] | None = None,
    knesset_num: int = 25,
    top_k: int = 5,
    auto_resolve_threshold: float = config.NAME_RESOLUTION_AUTO_THRESHOLD,
) -> list[dict]:
    """Return ranked candidate records for query.

    Parameters
    ----------
    query
        Free-text name or description fragment in Hebrew or English.
    fuzzy_index
        An already-built :class:`FuzzyNameIndex` for the entity domain.
    fetch_by_id
        Optional callable that returns the full record for a stable id.
        Invoked at most once: for the single entry whose label equals the
        query or starts with it (see FuzzyNameIndex.unambiguous_label_match),
        else for the top candidate when its score gap to the runner-up
        exceeds auto_resolve_threshold.
    knesset_num
        Forwarded to fetch_by_id as a hint only; not used in search.
    top_k
        Maximum number of candidates to return.
    auto_resolve_threshold
        Minimum score gap (0–1 scale) between top and runner-up for
        auto-resolution.

    Returns
    -------
    list[dict]
        Up to top_k entries sorted by descending score, except that an
        auto-resolved entry is moved first. Each has at least ``id``,
        ``label``, ``score`` (0–1 float), ``extra``, and ``fetched`` (bool).
        The auto-resolved entry also carries ``record``.
    """
    if not query or not query.strip():
        return []

    candidates = fuzzy_index.search(query.strip(), top_k=max(len(fuzzy_index), 2))

    if not candidates:
        return []

    resolved_position = None
    if fetch_by_id is not None:
        resolved_position = _auto_resolved_position(
            query.strip(), candidates, fuzzy_index, auto_resolve_threshold,
        )
    if resolved_position is not None:
        candidates.insert(0, candidates.pop(resolved_position))
        candidate_id = candidates[0]["id"]
        try:
            record = fetch_by_id(candidate_id)
        except Exception as exc:
            print(f"[name_search] fetch_by_id({candidate_id!r}) failed: {exc}")
            record = None
        if record is not None:
            candidates[0]["fetched"] = True
            candidates[0]["record"] = record

    return candidates[:top_k]


def _auto_resolved_position(
    query: str,
    candidates: list[dict],
    fuzzy_index: FuzzyNameIndex,
    auto_resolve_threshold: float,
) -> int | None:
    """Position of the candidate to fetch: the unambiguous exact/prefix label
    match if any, else the top candidate when it clears the score-gap rule."""
    unambiguous_id = fuzzy_index.unambiguous_label_match(query)
    if unambiguous_id is not None:
        for position, candidate in enumerate(candidates):
            if candidate["id"] == unambiguous_id:
                return position
    if len(candidates) == 1 or candidates[0]["score"] - candidates[1]["score"] >= auto_resolve_threshold:
        return 0
    return None


__all__ = ["name_search"]
