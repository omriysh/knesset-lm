"""
protocol_rag.py

3-level RAG retrieval over Knesset committee protocols.

Retrieval flow
--------------
1. Embed the query.
2. L1 search (knesset_bullets) → top summary bullets → deduplicate to TOP_K_MEETINGS
   meeting IDs.  Record per-meeting which bullet indices matched and at what similarity.
3. For each retrieved meeting, fetch all pass-2 chunks (knesset_dialogs_pass2).
   Score each chunk by dot-product of its topic_scores_vec with the per-bullet sims.
   Select top-N pass-2 chunks overall.
4. For each selected pass-2 chunk, cosine-rerank its pass-1 children
   (knesset_dialogs_pass1) and take the single best match.
5. Assemble context from the selected pass-1 chunks (budget-capped).

Why this design
---------------
- L1 (summary bullets) surfaces relevant meetings even when the query phrasing
  doesn't appear verbatim in dialog text.
- Pass-2 chunks are ranked via bullet association — their topic_scores_vec lives
  in the same embedding space as L1, so no separate re-embedding of large dialogs.
- Pass-1 reranking gives fine-grained, token-budget-friendly context slices.

All dependencies (chroma client, embedder, collection names) are injected at
call time — no global state, no config reads at query time.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

import config
from indexing.embedder import ProtocolEmbedder


def _score_pass2_chunk(
    topic_scores_vec: list[float],
    bullet_sims: dict[int, float],
) -> float:
    """
    Dot-product of a chunk's bullet-association vector with query bullet sims.

    topic_scores_vec : ReLU+L1-normalised scores over the meeting's bullet list.
    bullet_sims      : {bullet_idx → cosine similarity to query} for this meeting.
    """
    return sum(
        topic_scores_vec[b] * sim
        for b, sim in bullet_sims.items()
        if b < len(topic_scores_vec)
    )


def query_retrieve(
    question: str,
    chroma_client,
    embedder: ProtocolEmbedder,
    *,
    top_k: int = config.TOP_K_MEETINGS,
    top_n: int = config.TOP_N_DIALOGS,
    max_chars: int = config.MAX_CONTEXT_CHARS,
    bullets_collection: str = config.BULLETS_COLLECTION,
    pass2_collection:   str = config.PASS2_COLLECTION,
    pass1_collection:   str = config.PASS1_COLLECTION,
) -> tuple[str, dict[str, Any]]:
    """
    3-level ChromaDB retrieval.  Returns (context_str, debug_dict).

    context_str is ready to inject into the LLM prompt.

    debug_dict keys:
        meetings        : list[str]   — retrieved meeting IDs
        selected_pass1  : list[dict]  — final context chunks (pass-1 dicts)
        context_chars   : int
        meeting_paths   : dict[str, str] — meeting_id → summary .txt path
    """
    # ── 1. Embed question ─────────────────────────────────────────────────────
    q_emb = embedder.embed([question], ProtocolEmbedder.INSTR_QUERY)  # (1, D)

    # ── 2. L1: bullets → meeting IDs ─────────────────────────────────────────
    l1_coll = chroma_client.get_collection(bullets_collection)
    l1_results = l1_coll.query(
        query_embeddings=q_emb.tolist(),
        n_results=top_k * 6,
        include=["metadatas", "distances"],
    )

    meeting_bullet_sims: dict[str, dict[int, float]] = {}
    for meta, dist in zip(
        l1_results["metadatas"][0], l1_results["distances"][0]
    ):
        mid  = meta["meeting_id"]
        bidx = meta["bullet_idx"]
        sim  = 1.0 - dist   # ChromaDB cosine distance = 1 − similarity
        meeting_bullet_sims.setdefault(mid, {})
        if sim > meeting_bullet_sims[mid].get(bidx, -1.0):
            meeting_bullet_sims[mid][bidx] = sim

    meeting_ids = sorted(
        meeting_bullet_sims,
        key=lambda m: max(meeting_bullet_sims[m].values()),
        reverse=True,
    )[:top_k]

    # ── 3. Rank pass-2 chunks by bullet association ───────────────────────────
    l2_coll = chroma_client.get_collection(pass2_collection)
    pass2_candidates: list[dict] = []

    for mid in meeting_ids:
        bullet_sims = meeting_bullet_sims[mid]
        try:
            rows = l2_coll.get(
                where={"meeting_id": {"$eq": mid}},
                include=["metadatas", "documents"],
            )
        except Exception as exc:
            print(f"[protocol_rag] L2 chroma query failed for meeting_id={mid!r}: {exc}", flush=True)
            continue

        for pid, doc, meta in zip(rows["ids"], rows["documents"], rows["metadatas"]):
            tsv   = json.loads(meta.get("topic_scores_vec", "[]"))
            score = _score_pass2_chunk(tsv, bullet_sims)
            pass2_candidates.append({
                "pass2_id": pid,
                "meta":     meta,
                "doc":      doc,
                "score":    score,
            })

    pass2_candidates.sort(key=lambda x: x["score"], reverse=True)
    selected_pass2 = pass2_candidates[:top_n]

    # ── 4. Rerank pass-1 children within each pass-2 chunk ───────────────────
    p1_coll = chroma_client.get_collection(pass1_collection)
    pass1_candidates: list[dict] = []

    for p2 in selected_pass2:
        pass2_id = p2["pass2_id"]
        try:
            rows = p1_coll.query(
                query_embeddings=q_emb.tolist(),
                n_results=1,
                where={"pass2_id": {"$eq": pass2_id}},
                include=["documents", "metadatas", "distances"],
            )
            if rows["ids"][0]:
                pass1_candidates.append({
                    "meta":             rows["metadatas"][0][0],
                    "doc":              rows["documents"][0][0],
                    "p1_sim":           1.0 - rows["distances"][0][0],
                    "p2_score":         p2["score"],
                    "pass2_id":         pass2_id,
                    "topic_scores_vec": json.loads(p2["meta"].get("topic_scores_vec", "[]")),
                })
        except Exception as exc:
            print(f"[protocol_rag] pass-1 chroma query failed for pass2_id={pass2_id!r}: {exc}", flush=True)
            # Fallback: use pass-2 text directly
            pass1_candidates.append({
                "meta":            p2["meta"],
                "doc":             p2["doc"],
                "p1_sim":          p2["score"],
                "p2_score":        p2["score"],
                "pass2_id":        pass2_id,
                "topic_scores_vec": json.loads(p2["meta"].get("topic_scores_vec", "[]")),
            })

    pass1_candidates.sort(key=lambda x: x["p1_sim"], reverse=True)

    # ── 5. Assemble context ───────────────────────────────────────────────────
    parts: list[str] = []
    used  = 0
    seen: set[str] = set()

    for item in pass1_candidates:
        meta = item["meta"]
        key  = f"{meta['meeting_id']}_{meta.get('start_speech_idx', '')}"
        if key in seen:
            continue
        section = (
            f"### ישיבה {meta['meeting_id']}  ({meta['date']}, {meta['committee']})\n"
            f"נושא: {meta['topic_text']}\n"
            f"{item['doc']}\n"
        )
        if used + len(section) > max_chars:
            break
        parts.append(section)
        seen.add(key)
        used += len(section)

    context = "\n---\n".join(parts)

    # Build meeting_id → summary_path and basic meta from L1
    # L1 metadata always has committee+date even for meetings with no pass-1 chunks selected.
    meeting_paths: dict[str, str] = {}
    l1_meeting_meta: dict[str, dict] = {}
    for meta in l1_results["metadatas"][0]:
        mid = meta.get("meeting_id")
        if not mid:
            continue
        sp = meta.get("summary_path")
        if sp and mid not in meeting_paths:
            meeting_paths[mid] = sp
        if mid not in l1_meeting_meta:
            l1_meeting_meta[mid] = {
                "committee": meta.get("committee", ""),
                "date":      meta.get("date", ""),
            }

    meeting_scores: dict[str, float] = {
        mid: max(meeting_bullet_sims[mid].values())
        for mid in meeting_ids
    }

    debug: dict[str, Any] = {
        "meetings":         meeting_ids,
        "meeting_scores":   meeting_scores,
        "selected_pass1":   pass1_candidates,
        "all_pass2":        pass2_candidates,   # all meetings, pre-top-N cut
        "context_chars":    used,
        "meeting_paths":    meeting_paths,
        "l1_meeting_meta":  l1_meeting_meta,
    }
    return context, debug
