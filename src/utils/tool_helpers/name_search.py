"""Generic BM25-based name resolution helper.

Used by every ``find_*`` tool (``find_mk``, ``find_committee``, ``find_bill``,
``find_vote``) per design §5.4. Single round-trip: BM25-search the relevant
index, return top-k candidates, optionally inline the top record if its
BM25 score is unambiguously the best (gap to the runner-up exceeds the
configured threshold).

Design principles (from §5.1 #3):
  * Names are entities, not strings: callers receive *candidate records*
    with stable ids; they are the ones who pick which id to use downstream.
  * No fallback logic, no implicit query widening — that is the planner's
    job, not the tool's.
  * The first call returns *candidates*. Only when the BM25 score gap is
    large enough do we mark the top one as ``fetched=True`` and inline the
    record from the supplied ``fetch_by_id`` callback for the caller's
    convenience.

The helper does *not* know what kind of entity it is resolving — it takes
a ``BM25Index`` and a ``fetch_by_id`` callable and produces a ranked list
of plain ``{id, label, score, fetched, record?}`` dicts. Per-entity field
shaping (e.g. mapping ``id`` to ``mk_id`` vs ``committee_id``) is the
adapter's responsibility.
"""

from __future__ import annotations

from typing import Callable

import config
from retrieval.bm25_index import BM25Index
from retrieval.lemmatize import lemmatize


def name_search(
    query: str,
    *,
    bm25_index: BM25Index,
    fetch_by_id: Callable[[str], dict | None] | None = None,
    knesset_num: int = 25,
    top_k: int = 5,
    auto_resolve_threshold: float = config.NAME_RESOLUTION_AUTO_THRESHOLD,
) -> list[dict]:
    """Return ranked candidate records for ``query`` from a BM25 index.

    Parameters
    ----------
    query
        Free-text name fragment in Hebrew or English.
    bm25_index
        An already-opened :class:`BM25Index` for the entity domain.
    fetch_by_id
        Optional callable that, given a stable id string, returns the full
        record. Invoked only for the unique top match when the score-gap
        condition is met. The fetched record is merged in under ``record``.
    knesset_num
        Forwarded to ``fetch_by_id`` only as a hint when the helper auto-
        resolves the top match. Currently unused in BM25 search itself
        (the index is already per-Knesset on disk; see
        :func:`scripts.build_bm25_indexes._db_path`).
    top_k
        Maximum number of candidates to return. The helper rounds up to
        at least 2 internally so it can always inspect the runner-up
        score for the auto-resolution gap test, then trims back to
        ``top_k`` on output.
    auto_resolve_threshold
        Minimum *normalised* score gap between the top candidate and the
        runner-up for the helper to treat the top as unique. The
        normalisation puts FTS5's negative bm25() scores onto a scale
        where 0 means "no signal" and ``+1`` is "perfect"; see
        :func:`_normalise_score` below.

    Returns
    -------
    list[dict]
        Up to ``top_k`` entries, sorted by descending normalised score.
        Each dict has at least ``id``, ``label``, ``score`` (positive
        float; higher is better) and ``fetched`` (bool). Auto-resolved
        top matches additionally carry ``record`` (the dict returned by
        ``fetch_by_id``).
    """
    if not query or not query.strip():
        return []

    fetch_top_k = max(top_k, 2)

    # Apply the same lemmatisation pass we used at index build time so the
    # match expression hits the lemmatised columns. Empty result → empty
    # list, never an exception bubbled to the caller.
    lemmatised = lemmatize(query)
    match_expr = lemmatised.strip() or query.strip()

    try:
        rows = bm25_index.search(_quote_match(match_expr), top_k=fetch_top_k)
    except Exception:
        # FTS5 syntax errors / malformed queries fall back to the raw
        # string with FTS5 metacharacters stripped.
        rows = bm25_index.search(_safe_match(query), top_k=fetch_top_k)

    if not rows:
        return []

    # Build candidate records with normalised, monotone-positive scores.
    candidates: list[dict] = []
    for row in rows:
        raw_score = float(row.get("score") or 0.0)
        candidates.append({
            "id":      str(row.get("id") or ""),
            "label":   row.get("label") or "",
            "score":   _normalise_score(raw_score),
            "extra":   row.get("extra") if isinstance(row.get("extra"), dict) else {},
            "fetched": False,
        })

    # Sort by descending normalised score; ties resolved by FTS5 order.
    candidates.sort(key=lambda c: c["score"], reverse=True)

    # Auto-resolve the top match if the score gap to the runner-up is
    # convincingly large. Gap is measured on the same normalised scale as
    # the candidate scores themselves, so the threshold is dimensionless.
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
            candidates[0]["record"]  = record

    # Trim to the caller's requested cap.
    return candidates[:top_k]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_score(raw: float) -> float:
    """Map FTS5's negative ``bm25()`` score to ``[0, +inf)``-ish positives.

    SQLite FTS5 returns BM25 as a *negative* value where smaller (more
    negative) means a stronger match. ``-raw`` flips it to "higher is
    better"; we floor at 0 so a pathological non-negative reading still
    sorts last instead of poisoning the gap calculation.
    """
    flipped = -raw
    return flipped if flipped > 0.0 else 0.0


_FTS5_META = set('"*():^-+')


def _safe_match(text: str) -> str:
    """Strip FTS5 metacharacters from ``text`` for a fallback search."""
    return "".join(ch for ch in text if ch not in _FTS5_META).strip() or text


def _quote_match(text: str) -> str:
    """Wrap each whitespace-separated token in double quotes.

    FTS5 treats Hebrew morphology hyphens / apostrophes as syntax otherwise.
    Quoting each token disables operators while still allowing AND-style
    multi-word matching.
    """
    tokens = [tok for tok in text.split() if tok.strip()]
    if not tokens:
        return text
    return " ".join(f'"{_safe_match(tok)}"' for tok in tokens if _safe_match(tok))


__all__ = ["name_search"]
