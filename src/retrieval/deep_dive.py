"""Deep dive over a single committee meeting.

Two operating modes:

* ``mode="rerank"`` — reuse the existing 3-level RAG
  (``retrieval/protocol_rag.py``) but scope the L1→pass-2→pass-1 walk to a
  single ``meeting_id``. Returns the top reranked pass-1 chunks.

* ``mode="full"`` — walk every pass-2 chunk of the meeting from ChromaDB,
  pack them into LLM calls sized to the model's context window, and return
  per-chunk LLM answers.  If all chunks fit in one call they are sent
  together; otherwise they are batched into groups.  Use
  ``make_gemini_llm_call()`` to obtain a ready-made callable, or inject your
  own.

Both modes wrap their result in a :class:`agent.subgraph.evidence.ToolEnvelope`.

Per the Phase 3a brief:
    Do NOT import anything from ``agent/plan_execute/``. Retrieval lives
    below that layer; the plan-execute graph imports retrieval, never the
    other way round.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import chromadb
import numpy as np

import config
from agent.subgraph.evidence import ToolEnvelope
from indexing.embedder import ProtocolEmbedder

# llm_call type: receives the raw chunk dicts and the query string so the
# implementation can decide how to batch them.  Returns one response string
# per chunk, in the same order.
LLMCallable = Callable[[list[dict], str], list[str]]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def deep_dive_meeting(
    meeting_id: str,
    query: str,
    mode: str = "rerank",
    llm_call: LLMCallable | None = None,
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
        Required for ``mode="full"``.  Signature:
        ``(chunks: list[dict], query: str) -> list[str]``.
        Receives the raw pass-2 chunk dicts and the question; must return
        one answer string per chunk in the same order.  If ``None`` a
        default Gemini callable is created via ``make_gemini_llm_call()``.
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
        If ``mode`` is unknown.
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
            llm_call = make_gemini_llm_call()
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
        except Exception as exc:
            print(f"[deep_dive] pass-1 chroma query failed for pass2_id={p2['pass2_id']!r}: {exc}", flush=True)
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
    llm_call: LLMCallable,
    top_k: int,
    chroma_client,
    pass2_collection: str,
) -> ToolEnvelope:
    """Run every pass-2 chunk of the meeting through ``llm_call``.

    ``llm_call(chunks, query)`` receives raw chunk dicts and the question and
    returns one answer string per chunk. Order is preserved.
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

    try:
        outputs = llm_call(chunks, query)
    except Exception as exc:  # noqa: BLE001
        return _error_envelope(
            "deep_dive_full_llm_failed",
            meeting_id=meeting_id,
            error=str(exc),
        )

    if not isinstance(outputs, list) or len(outputs) != len(chunks):
        return _error_envelope(
            "deep_dive_full_llm_shape_mismatch",
            meeting_id=meeting_id,
            error=(
                f"llm_call returned {type(outputs).__name__} "
                f"len={len(outputs) if hasattr(outputs, '__len__') else '?'} "
                f"for {len(chunks)} chunks"
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


# ---------------------------------------------------------------------------
# Gemini llm_call factory
# ---------------------------------------------------------------------------

_PER_CHUNK_OUTPUT_CHARS = 600   # ~300 tok × CHARS_PER_TOK — output budget per chunk


def make_gemini_llm_call(model: str | None = None) -> LLMCallable:
    """Return an ``llm_call`` backed by Gemini.

    The callable packs as many pass-2 chunks as fit within the model's
    context window into a single API call.  If all chunks fit, one call is
    made; otherwise the chunks are split into batches that each stay within
    the budget.  Each call asks the model to return a JSON array so responses
    can be reassembled in order regardless of batching.

    Parameters
    ----------
    model:
        Gemini model ID.  Defaults to ``config.DEEP_DIVE_FULL_MODEL``.
    """
    from agent.llm.google import GoogleBackend

    backend = GoogleBackend(model=model or config.DEEP_DIVE_FULL_MODEL)
    # Characters available for input after reserving headroom for output.
    input_char_budget = int(
        backend.ctx_size * config.CHARS_PER_TOK * config.DEEP_DIVE_FULL_BATCH_HEADROOM
    )

    def llm_call(chunks: list[dict], query: str) -> list[str]:
        if not chunks:
            return []
        results: list[tuple[int, str]] = []   # (original_idx, response)

        # Group chunks into batches that fit the input budget.
        batch: list[tuple[int, dict]] = []
        batch_chars = 0
        for orig_idx, chunk in enumerate(chunks):
            chunk_chars = len(chunk.get("doc") or "")
            if batch and (batch_chars + chunk_chars) > input_char_budget:
                results.extend(_call_batch(backend, batch, query))
                batch, batch_chars = [], 0
            batch.append((orig_idx, chunk))
            batch_chars += chunk_chars
        if batch:
            results.extend(_call_batch(backend, batch, query))

        # Sort by original index and return responses in chunk order.
        results.sort(key=lambda x: x[0])
        return [r for _, r in results]

    return llm_call


def _call_batch(
    backend: "GoogleBackend",  # noqa: F821 — forward ref ok, imported inside factory
    indexed_chunks: list[tuple[int, dict]],
    query: str,
) -> list[tuple[int, str]]:
    """Send one batch of chunks to the backend, return (original_idx, response) pairs."""
    prompt = _render_batch_prompt(indexed_chunks, query)
    max_tokens = min(len(indexed_chunks) * 400, config.MAX_TOKENS)
    raw = backend.generate_text(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    parsed = _parse_batch_response(raw, len(indexed_chunks))
    # Map local (batch-relative) idx back to the original chunk indices.
    return [(indexed_chunks[local_idx][0], resp) for local_idx, resp in parsed]


def _render_batch_prompt(indexed_chunks: list[tuple[int, dict]], query: str) -> str:
    """Render multiple chunks into one prompt asking for a JSON array response."""
    header = (
        "You are analyzing a parliamentary committee meeting transcript.\n"
        "For each numbered chunk below, answer the question.\n"
        "Return ONLY a JSON array with one object per chunk, in order:\n"
        '[{"idx": 0, "response": "..."}, {"idx": 1, "response": "..."}, ...]\n'
        "For irrelevant chunks use an empty string as the response.\n\n"
        f"QUESTION: {query}\n\n"
    )
    parts = [header]
    for local_idx, (_, chunk) in enumerate(indexed_chunks):
        meta = chunk.get("meta") or {}
        parts.append(
            f"--- CHUNK {local_idx} ---\n"
            f"Meeting: {meta.get('meeting_id', '')} "
            f"({meta.get('date', '')}, {meta.get('committee', '')})\n"
            f"Topic: {meta.get('topic_text', '')}\n\n"
            f"{chunk.get('doc', '')}\n\n"
        )
    return "".join(parts)


def _parse_batch_response(raw: str, n: int) -> list[tuple[int, str]]:
    """Parse the model's JSON-array response into (idx, response) pairs.

    Falls back to positional splitting if JSON parsing fails.
    """
    # Strip markdown fences if present.
    text = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            out = {}
            for item in data:
                if isinstance(item, dict):
                    idx = item.get("idx")
                    resp = str(item.get("response") or "")
                    if isinstance(idx, int) and 0 <= idx < n:
                        out[idx] = resp
            # Fill any missing indices with empty string.
            return [(i, out.get(i, "")) for i in range(n)]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"[deep_dive] _parse_batch_response JSON parse failed ({exc}); trying marker fallback", flush=True)

    # Fallback: split on "--- CHUNK N ---" markers that appear in the response.
    segments = re.split(r"(?:idx[\"']?\s*:\s*(\d+)|CHUNK\s+(\d+))", text)
    if len(segments) > 1:
        result: dict[int, str] = {}
        i = 0
        while i < len(segments):
            m1, m2 = (segments[i + 1] if i + 1 < len(segments) else None,
                      segments[i + 2] if i + 2 < len(segments) else None)
            idx_str = m1 or m2
            if idx_str is not None:
                try:
                    idx = int(idx_str)
                    resp = segments[i + 3].strip() if i + 3 < len(segments) else ""
                    if 0 <= idx < n:
                        result[idx] = resp
                    i += 3
                    continue
                except (ValueError, IndexError):
                    pass
            i += 1
        if result:
            return [(i, result.get(i, "")) for i in range(n)]

    # Last resort: treat entire response as answer for the only chunk.
    if n == 1:
        return [(0, raw.strip())]
    return [(i, "") for i in range(n)]


# ---------------------------------------------------------------------------
# Legacy per-chunk prompt (kept for reference; not used in production path)
# ---------------------------------------------------------------------------


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


__all__ = ["deep_dive_meeting", "make_gemini_llm_call"]


# Suppress unused-import lint for numpy — kept for symmetry with the rest of
# the retrieval layer (protocol_rag.py imports it for vector ops); the code
# path here doesn't need numpy directly but future extensions almost certainly
# will. Safe to drop if a linter complains.
_ = np
