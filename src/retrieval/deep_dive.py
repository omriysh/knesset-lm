"""Deep dive over a single committee meeting.

Two operating modes:

* ``mode="rerank"`` — reuse the existing 3-level RAG
  (``retrieval/protocol_rag.py``) but scope the L1→pass-2→pass-1 walk to a
  single ``meeting_id``. Returns the top reranked pass-1 chunks.

* ``mode="full"`` — walk every pass-2 chunk of the meeting from ChromaDB
  and run a batched LLM pass via the injected ``llm_call`` callable. The
  production caller wires the Gemini client at composition time so this
  module stays free of provider imports.

Both modes wrap their result in a :class:`agent.subgraph.evidence.ToolEnvelope`.

Per the Phase 3a brief:
    Do NOT import anything from ``agent/plan_execute/``. Retrieval lives
    below that layer; the plan-execute graph imports retrieval, never the
    other way round.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import chromadb
import numpy as np

import config
from agent.subgraph.evidence import ToolEnvelope
from indexing.embedder import ProtocolEmbedder


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def deep_dive_meeting(
    meeting_id: str,
    query: str,
    mode: str = "rerank",
    llm_call: Callable[[list[str]], list[str]] | None = None,
    top_k: int = 5,
    *,
    chroma_client: "chromadb.ClientAPI | None" = None,
    embedder: ProtocolEmbedder | None = None,
    bullets_collection: str = config.BULLETS_COLLECTION,
    pass2_collection: str = config.PASS2_COLLECTION,
    pass1_collection: str = config.PASS1_COLLECTION,
) -> ToolEnvelope:
    """Run a query against a single meeting and return a ToolEnvelope.

    Parameters
    ----------
    meeting_id:
        The meeting to scope retrieval to. All Chroma queries are filtered
        by ``meta["meeting_id"] == meeting_id``.
    query:
        The user/agent question, in any language. Embedded with
        ``ProtocolEmbedder.INSTR_QUERY`` for ``mode="rerank"``.
    mode:
        ``"rerank"`` (default) — return top-``top_k`` pass-1 chunks ranked
        by the 3-level RAG within the meeting. ``"full"`` — fan all pass-2
        chunks through ``llm_call`` in a single batch and return its outputs.
    llm_call:
        Required for ``mode="full"``. Called as ``llm_call(prompts)`` where
        ``prompts`` is a list of pass-2 chunk-aware prompt strings; must
        return one string per prompt (the LLM's reading of that chunk).
        Ignored in ``mode="rerank"``.
    top_k:
        Cap on the number of chunks returned.
    chroma_client / embedder:
        Lazily constructed if not supplied — production callers are
        expected to inject already-warmed handles.
    bullets_collection / pass2_collection / pass1_collection:
        Collection name overrides. Defaults follow ``config``.

    Returns
    -------
    ToolEnvelope
        ``full`` is a JSON-encoded payload of the selected chunks (rerank)
        or per-chunk LLM outputs (full). ``summary`` is left empty — the
        executor LLM fills it after the call, per design §4.3. ``metadata``
        records ``kind``, ``count``, and ``source``. ``provenance`` carries
        the meeting id and per-chunk ids.

    Raises
    ------
    ValueError
        If ``mode`` is unknown, or ``mode="full"`` is requested without
        ``llm_call``.
    """
    if mode == "rerank":
        return _deep_dive_rerank(
            meeting_id=meeting_id,
            query=query,
            top_k=top_k,
            chroma_client=chroma_client,
            embedder=embedder,
            bullets_collection=bullets_collection,
            pass2_collection=pass2_collection,
            pass1_collection=pass1_collection,
        )
    if mode == "full":
        if llm_call is None:
            raise ValueError("llm_call required for mode='full'")
        return _deep_dive_full(
            meeting_id=meeting_id,
            query=query,
            llm_call=llm_call,
            top_k=top_k,
            chroma_client=chroma_client,
            pass2_collection=pass2_collection,
        )
    raise ValueError(f"deep_dive_meeting: unknown mode {mode!r}")


# ---------------------------------------------------------------------------
# Mode: rerank — single-meeting 3-level RAG
# ---------------------------------------------------------------------------


def _deep_dive_rerank(
    *,
    meeting_id: str,
    query: str,
    top_k: int,
    chroma_client,
    embedder: ProtocolEmbedder | None,
    bullets_collection: str,
    pass2_collection: str,
    pass1_collection: str,
) -> ToolEnvelope:
    """Replicate the 3-level RAG walk but filtered to one meeting.

    Implementation notes:
      * Re-uses the same ``topic_scores_vec · bullet_sims`` scoring as
        :func:`retrieval.protocol_rag.query_retrieve`. We don't import
        ``query_retrieve`` directly because it ranks meetings via L1 first,
        and we already know which meeting we want. Instead we:

          1. Pull this meeting's bullets from L1 with their cosine to the
             query (single ``query()`` call, ``where`` filters on
             ``meeting_id``).
          2. Score every pass-2 chunk in the meeting via dot product.
          3. For each top pass-2 chunk, cosine-rerank pass-1 children.
    """
    chroma_client = chroma_client or _default_chroma_client()
    embedder = embedder or ProtocolEmbedder()

    # 1. Embed query, fetch this meeting's bullets with their cosine sims.
    q_emb = embedder.embed([query], ProtocolEmbedder.INSTR_QUERY)
    l1_coll = chroma_client.get_collection(bullets_collection)
    try:
        l1_results = l1_coll.query(
            query_embeddings=q_emb.tolist(),
            n_results=200,
            where={"meeting_id": {"$eq": meeting_id}},
            include=["metadatas", "distances"],
        )
    except Exception as exc:  # noqa: BLE001 — surface to envelope
        return _error_envelope(
            "deep_dive_rerank_l1_failed",
            meeting_id=meeting_id,
            error=str(exc),
        )

    bullet_sims: dict[int, float] = {}
    if l1_results["metadatas"] and l1_results["metadatas"][0]:
        for meta, dist in zip(
            l1_results["metadatas"][0], l1_results["distances"][0]
        ):
            bidx = meta.get("bullet_idx")
            if bidx is None:
                continue
            sim = 1.0 - float(dist)
            if sim > bullet_sims.get(bidx, -1.0):
                bullet_sims[bidx] = sim

    # 2. Score pass-2 chunks.
    l2_coll = chroma_client.get_collection(pass2_collection)
    try:
        rows = l2_coll.get(
            where={"meeting_id": {"$eq": meeting_id}},
            include=["metadatas", "documents"],
        )
    except Exception as exc:  # noqa: BLE001
        return _error_envelope(
            "deep_dive_rerank_pass2_failed",
            meeting_id=meeting_id,
            error=str(exc),
        )

    pass2_scored: list[dict] = []
    for pid, doc, meta in zip(
        rows.get("ids", []) or [],
        rows.get("documents", []) or [],
        rows.get("metadatas", []) or [],
    ):
        tsv = json.loads(meta.get("topic_scores_vec", "[]") or "[]")
        score = sum(
            tsv[b] * sim
            for b, sim in bullet_sims.items()
            if b < len(tsv)
        )
        pass2_scored.append({
            "pass2_id": pid,
            "meta": meta,
            "doc": doc,
            "score": float(score),
        })
    pass2_scored.sort(key=lambda x: x["score"], reverse=True)
    selected_pass2 = pass2_scored[: max(top_k, 1)]

    # 3. Rerank pass-1 children of each selected pass-2 chunk.
    p1_coll = chroma_client.get_collection(pass1_collection)
    pass1_picks: list[dict] = []
    for p2 in selected_pass2:
        try:
            r = p1_coll.query(
                query_embeddings=q_emb.tolist(),
                n_results=1,
                where={"pass2_id": {"$eq": p2["pass2_id"]}},
                include=["documents", "metadatas", "distances"],
            )
            if r["ids"] and r["ids"][0]:
                pass1_picks.append({
                    "pass1_id": r["ids"][0][0],
                    "pass2_id": p2["pass2_id"],
                    "meta": r["metadatas"][0][0],
                    "doc": r["documents"][0][0],
                    "p1_sim": 1.0 - float(r["distances"][0][0]),
                    "p2_score": p2["score"],
                })
                continue
        except Exception:
            pass
        # Fallback: keep the pass-2 text directly.
        pass1_picks.append({
            "pass1_id": None,
            "pass2_id": p2["pass2_id"],
            "meta": p2["meta"],
            "doc": p2["doc"],
            "p1_sim": p2["score"],
            "p2_score": p2["score"],
        })

    pass1_picks.sort(key=lambda x: x["p1_sim"], reverse=True)
    pass1_picks = pass1_picks[:top_k]

    full_payload = {
        "meeting_id": meeting_id,
        "query": query,
        "mode": "rerank",
        "chunks": [
            {
                "pass1_id": p["pass1_id"],
                "pass2_id": p["pass2_id"],
                "topic_text": p["meta"].get("topic_text", ""),
                "date": p["meta"].get("date", ""),
                "committee": p["meta"].get("committee", ""),
                "text": p["doc"],
                "p1_sim": p["p1_sim"],
                "p2_score": p["p2_score"],
            }
            for p in pass1_picks
        ],
    }
    provenance = {
        "meeting_id": meeting_id,
        "pass1_ids": [p["pass1_id"] for p in pass1_picks if p["pass1_id"]],
        "pass2_ids": [p["pass2_id"] for p in pass1_picks if p["pass2_id"]],
    }
    metadata = {
        "kind": "analysis",
        "count": len(pass1_picks),
        "total_match": len(pass2_scored),
        "source": "deep_dive_meeting:rerank",
        "warnings": [],
    }
    return ToolEnvelope(
        summary="",
        full=json.dumps(full_payload, ensure_ascii=False),
        metadata=metadata,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Mode: full — fan all pass-2 chunks through an injected LLM
# ---------------------------------------------------------------------------


def _deep_dive_full(
    *,
    meeting_id: str,
    query: str,
    llm_call: Callable[[list[str]], list[str]],
    top_k: int,
    chroma_client,
    pass2_collection: str,
) -> ToolEnvelope:
    """Run every pass-2 chunk of the meeting through ``llm_call``.

    The injected callable must accept ``list[str]`` (one prompt per chunk)
    and return ``list[str]`` of equal length. Order is preserved.
    """
    chroma_client = chroma_client or _default_chroma_client()
    l2_coll = chroma_client.get_collection(pass2_collection)
    try:
        rows = l2_coll.get(
            where={"meeting_id": {"$eq": meeting_id}},
            include=["metadatas", "documents"],
        )
    except Exception as exc:  # noqa: BLE001
        return _error_envelope(
            "deep_dive_full_pass2_failed",
            meeting_id=meeting_id,
            error=str(exc),
        )

    chunks: list[dict] = []
    for pid, doc, meta in zip(
        rows.get("ids", []) or [],
        rows.get("documents", []) or [],
        rows.get("metadatas", []) or [],
    ):
        chunks.append({
            "pass2_id": pid,
            "meta": meta,
            "doc": doc,
        })

    if not chunks:
        return ToolEnvelope(
            summary="",
            full=json.dumps(
                {
                    "meeting_id": meeting_id,
                    "query": query,
                    "mode": "full",
                    "responses": [],
                },
                ensure_ascii=False,
            ),
            metadata={
                "kind": "analysis",
                "count": 0,
                "source": "deep_dive_meeting:full",
                "warnings": ["no_pass2_chunks"],
            },
            provenance={"meeting_id": meeting_id},
        )

    prompts = [_render_full_prompt(query, chunk) for chunk in chunks]
    try:
        outputs = llm_call(prompts)
    except Exception as exc:  # noqa: BLE001
        return _error_envelope(
            "deep_dive_full_llm_failed",
            meeting_id=meeting_id,
            error=str(exc),
        )

    if not isinstance(outputs, list) or len(outputs) != len(prompts):
        return _error_envelope(
            "deep_dive_full_llm_shape_mismatch",
            meeting_id=meeting_id,
            error=(
                f"llm_call returned {type(outputs).__name__} "
                f"len={len(outputs) if hasattr(outputs, '__len__') else '?'} "
                f"for {len(prompts)} prompts"
            ),
        )

    responses: list[dict] = []
    for chunk, out in zip(chunks, outputs):
        responses.append({
            "pass2_id": chunk["pass2_id"],
            "topic_text": chunk["meta"].get("topic_text", ""),
            "date": chunk["meta"].get("date", ""),
            "committee": chunk["meta"].get("committee", ""),
            "response": out,
        })

    truncated = False
    if top_k and top_k > 0 and len(responses) > top_k:
        responses = responses[:top_k]
        truncated = True

    full_payload = {
        "meeting_id": meeting_id,
        "query": query,
        "mode": "full",
        "responses": responses,
    }
    provenance = {
        "meeting_id": meeting_id,
        "pass2_ids": [r["pass2_id"] for r in responses],
    }
    metadata = {
        "kind": "analysis",
        "count": len(responses),
        "total_match": len(chunks),
        "source": "deep_dive_meeting:full",
        "warnings": ["result_truncated_to_%d" % top_k] if truncated else [],
    }
    return ToolEnvelope(
        summary="",
        full=json.dumps(full_payload, ensure_ascii=False),
        metadata=metadata,
        provenance=provenance,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_chroma_client():
    """Open the project's persistent Chroma directory.

    Imported lazily and only built when no client is injected — keeps the
    module importable in environments without Chroma installed (the smoke
    test in Phase 3a only exercises rrf_fuse + the import path).
    """
    return chromadb.PersistentClient(path=str(config.CHROMA_DIR))


def _render_full_prompt(query: str, chunk: dict) -> str:
    """Build the per-chunk prompt for ``mode='full'``.

    Kept minimal and provider-agnostic — the production caller is free to
    wrap this output in whatever system / role envelope its LLM expects.
    """
    meta = chunk.get("meta") or {}
    header = (
        f"Meeting: {meta.get('meeting_id', '')} "
        f"({meta.get('date', '')}, {meta.get('committee', '')})\n"
        f"Topic: {meta.get('topic_text', '')}\n"
    )
    return (
        f"{header}\n"
        f"---\n"
        f"{chunk.get('doc', '')}\n"
        f"---\n"
        f"Question: {query}\n"
        "Answer briefly using only the chunk above; if the chunk is irrelevant, say so."
    )


def _error_envelope(code: str, *, meeting_id: str, error: str) -> ToolEnvelope:
    """Wrap a hard failure as a ToolEnvelope with ``error`` populated."""
    return ToolEnvelope(
        summary="",
        full="",
        metadata={
            "kind": "analysis",
            "count": 0,
            "source": "deep_dive_meeting",
            "warnings": [code],
        },
        provenance={"meeting_id": meeting_id},
        error=error,
    )


__all__ = ["deep_dive_meeting"]


# Suppress unused-import lint for numpy — kept for symmetry with the rest of
# the retrieval layer (protocol_rag.py imports it for vector ops); the code
# path here doesn't need numpy directly but future extensions almost certainly
# will. Safe to drop if a linter complains.
_ = np
