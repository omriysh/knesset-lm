"""Hybrid scoring helpers for the plan-and-execute retrieval layer.

Currently exposes a single primitive — :func:`rrf_fuse` — used by hybrid
tools (``search_topics``, optional rerank in ``search_protocols_keyword``)
to combine multiple ranked id lists into one fused ordering.

Pure functions only; no I/O, no Chroma / BM25 dependencies. The caller is
responsible for producing the per-signal rankings and deciding what the
ids mean (bullet ids, speech ids, meeting ids, ...).
"""

from __future__ import annotations

import config


def rrf_fuse(
    rankings: list[list[str]],
    k: int = config.RRF_K,
    top_k: int = 10,
) -> list[str]:
    """Reciprocal Rank Fusion over multiple ranked id lists.

    For each id ``d``, the fused score is::

        score(d) = sum( 1 / (k + rank_i(d)) for each list i containing d )

    where ``rank_i(d)`` is the 1-based position of ``d`` in list ``i``.
    Ids missing from a given list contribute nothing for that list.

    Parameters
    ----------
    rankings:
        One ranked list of ids per signal (e.g. ``[bm25_ids, embed_ids]``).
        Earlier-positioned ids are stronger. Lists may contain duplicates;
        only the first occurrence within a single list counts.
    k:
        RRF damping constant. Defaults to :data:`config.RRF_K` (60), the
        commonly cited setting; larger ``k`` flattens the per-rank weight
        curve, smaller ``k`` sharpens it.
    top_k:
        Maximum number of fused ids to return.

    Returns
    -------
    list[str]
        Ids sorted by descending fused score, capped at ``top_k``.
        Ties are broken by insertion order (stable Python ``sorted``).
    """
    if top_k <= 0 or not rankings:
        return []

    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    counter = 0

    for ranking in rankings:
        if not ranking:
            continue
        seen_in_list: set[str] = set()
        for rank, doc_id in enumerate(ranking, start=1):
            if doc_id in seen_in_list:
                # Duplicates within a single ranking shouldn't compound the
                # score — only the highest (first) rank counts for that list.
                continue
            seen_in_list.add(doc_id)
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            if doc_id not in first_seen:
                first_seen[doc_id] = counter
                counter += 1

    ordered = sorted(
        scores.items(),
        key=lambda item: (-item[1], first_seen.get(item[0], 0)),
    )
    return [doc_id for doc_id, _ in ordered[:top_k]]


__all__ = ["rrf_fuse"]
