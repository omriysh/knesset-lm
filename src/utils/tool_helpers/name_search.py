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
        Invoked only when the top candidate's score gap to the runner-up
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
        Up to top_k entries sorted by descending score. Each has at least
        ``id``, ``label``, ``score`` (0–1 float), ``extra``, and
        ``fetched`` (bool). Auto-resolved top entries also carry ``record``.
    """
    if not query or not query.strip():
        return []

    fetch_top_k = max(top_k, 2)
    candidates = fuzzy_index.search(query.strip(), top_k=fetch_top_k)

    if not candidates:
        return []

    if (
        fetch_by_id is not None
        and len(candidates) >= 1
        and (
            len(candidates) == 1
            or (candidates[0]["score"] - candidates[1]["score"]) >= auto_resolve_threshold
        )
    ):
        try:
            record = fetch_by_id(candidates[0]["id"])
        except Exception:
            record = None
        if record is not None:
            candidates[0]["fetched"] = True
            candidates[0]["record"] = record

    return candidates[:top_k]


__all__ = ["name_search"]
