"""
indexer.py

Core meeting-indexing logic.

One meeting produces entries in up to four ChromaDB collections:

  knesset_speeches    — one entry per valid speech (for speaker queries and
                        as a pre-computation cache for dialog extraction)
  knesset_bullets     — one entry per summary bullet (L1 meeting-level retrieval)
  knesset_dialogs_pass1 — one entry per fine-grained (coherence-boundary) chunk
  knesset_dialogs_pass2 — one entry per coarse (same-topic merged) chunk

All collection names default to the constants in config.py so they are shared
automatically with the query web app.

Public API
----------
index_meeting(json_path, summary_path, chroma_client, embedder, ...)
    → IndexResult

The summary_path is optional: if absent, only speeches are indexed.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import chromadb
import numpy as np

import config
from indexing.embedder import ProtocolEmbedder
from indexing.extract_dialogs import extract_dialogs_coherence
from indexing.parse_summary import parse_summary_bullets
from utils.meeting import load_meeting, parse_full_text_speeches


@dataclass
class IndexResult:
    status: str           # "ok" | "skip" | "error"
    reason: str = ""
    speeches:  int = 0
    bullets:   int = 0
    pass1:     int = 0
    pass2:     int = 0
    errors:    list = field(default_factory=list)


def _get_or_create(
    client: chromadb.ClientAPI,
    name: str,
) -> chromadb.Collection:
    return client.get_or_create_collection(
        name, metadata={"hnsw:space": "cosine"}
    )


def _get_meeting_id(data: dict, json_path: Path) -> str:
    return str(data.get("meeting_id") or json_path.stem)


def _valid_speeches(speeches: list) -> list[tuple[int, dict]]:
    return [
        (i, s) for i, s in enumerate(speeches)
        if s.get("speaker", "").strip()
        and len(s.get("text_he", "")) >= config.MIN_SPEECH_CHARS
    ]


# ── Speech indexing ────────────────────────────────────────────────────────────

def _index_speeches(
    data: dict,
    json_path: Path,
    coll: chromadb.Collection,
    embedder: ProtocolEmbedder,
    force: bool,
) -> tuple[int, Optional[np.ndarray]]:
    """
    Upsert speech embeddings into *coll*.  Returns (n_indexed, embedding_array).
    embedding_array is returned so the caller can reuse it for dialog extraction.
    Returns (0, None) if already indexed and not force.
    """
    speeches = data.get("speeches") or []
    if not speeches:
        return 0, None

    meeting_id = _get_meeting_id(data, json_path)
    valid      = _valid_speeches(speeches)
    if not valid:
        return 0, None

    if not force:
        probe = coll.get(ids=[f"{meeting_id}_{valid[0][0]}"], include=[])
        if probe["ids"]:
            # Already indexed — load embeddings for reuse in dialog extraction
            ids  = [f"{meeting_id}_{i}" for i, _ in valid]
            rows = coll.get(ids=ids, include=["embeddings"])
            if len(rows["ids"]) == len(ids) and rows["embeddings"] is not None and len(rows["embeddings"]) > 0:
                idx_map = {rid: emb for rid, emb in zip(rows["ids"], rows["embeddings"])}
                embs    = [idx_map.get(id_) for id_ in ids]
                if all(e is not None for e in embs):
                    return 0, np.array(embs, dtype=np.float32)
            return 0, None

    texts = [f"{s['speaker']}: {s['text_he']}" for _, s in valid]
    embs  = embedder.embed(texts, ProtocolEmbedder.INSTR_ASSIGN)

    coll.upsert(
        ids        = [f"{meeting_id}_{i}" for i, _ in valid],
        embeddings = embs.tolist(),
        documents  = [s.get("text_he", "")[:500] for _, s in valid],
        metadatas  = [
            {
                "meeting_id":  meeting_id,
                "speech_idx":  i,
                "speaker":     s.get("speaker", ""),
                "committee":   str(data.get("committee", "")),
                "knesset_num": int(data.get("knesset_num", 25)),
                "date":        str(data.get("date", "")),
            }
            for i, s in valid
        ],
    )
    return len(valid), embs


# ── Bullet (L1) indexing ───────────────────────────────────────────────────────

def _index_bullets(
    summary_path: Path,
    committee: str,
    meeting_id: str,
    date: str,
    coll: chromadb.Collection,
    embedder: ProtocolEmbedder,
    force: bool,
) -> tuple[int, list]:
    """
    Upsert summary bullets into *coll*.
    Returns (n_new, bullets_list) so the caller can reuse the bullet list.
    """
    bullets = parse_summary_bullets(summary_path, sections_wanted=None)
    if not bullets:
        return 0, []

    if not force:
        # Check whether the first bullet is already present
        first_id = f"{committee}__{meeting_id}__0"
        if coll.get(ids=[first_id], include=[])["ids"]:
            return 0, bullets   # already indexed; return bullets for reuse

    ids   = [f"{committee}__{meeting_id}__{b['idx']}" for b in bullets]
    texts = [b["text"] for b in bullets]
    embs  = embedder.embed(texts, ProtocolEmbedder.INSTR_BULLET_DOC)

    coll.upsert(
        ids        = ids,
        embeddings = embs.tolist(),
        documents  = texts,
        metadatas  = [
            {
                "committee":    committee,
                "date":         date,
                "meeting_id":   meeting_id,
                "section":      b["section"],
                "bullet_idx":   b["idx"],
                "summary_path": str(summary_path),
            }
            for b in bullets
        ],
    )
    return len(bullets), bullets


# ── Dialog (pass-1 / pass-2) indexing ─────────────────────────────────────────

def _find_pass2_idx(pass2_dialogs: list, p1_start: int, p1_end: int) -> int:
    for k, p2 in enumerate(pass2_dialogs):
        if p2["start_speech_idx"] <= p1_start and p2["end_speech_idx"] >= p1_end:
            return k
    return 0


def _index_dialogs(
    data: dict,
    summary_path: Path,
    committee: str,
    meeting_id: str,
    date: str,
    pass1_coll: chromadb.Collection,
    pass2_coll: chromadb.Collection,
    embedder: ProtocolEmbedder,
    precomputed_speech_embs: Optional[np.ndarray],
    force: bool,
) -> tuple[int, int]:
    """
    Extract and upsert pass-1 and pass-2 dialog chunks.
    Returns (n_pass1, n_pass2).
    """
    speeches = data.get("speeches") or []
    if not speeches:
        return 0, 0

    mk = f"{committee}__{meeting_id}"

    if not force:
        # Skip if the first pass-2 chunk is already present
        probe = pass2_coll.get(ids=[f"{mk}__p2_0"], include=[])
        if probe["ids"]:
            return 0, 0

    bullets = parse_summary_bullets(summary_path, sections_wanted=None)
    if not bullets:
        return 0, 0

    cr = extract_dialogs_coherence(
        speeches, bullets, embedder,
        precomputed_speech_embs=precomputed_speech_embs,
    )

    pass2_dialogs = cr["dialogs"]
    pass1_dialogs = cr["raw_dialogs"]
    if not pass2_dialogs:
        return 0, 0

    # ── Pass-2 ────────────────────────────────────────────────────────────────
    p2_texts = [d["full_dialog_text"] for d in pass2_dialogs]
    p2_embs  = embedder.embed(p2_texts, ProtocolEmbedder.INSTR_DIALOG_DOC)

    pass2_coll.upsert(
        ids        = [f"{mk}__p2_{k}" for k in range(len(pass2_dialogs))],
        embeddings = [e.tolist() for e in p2_embs],
        documents  = p2_texts,
        metadatas  = [
            {
                "committee":        committee,
                "date":             date,
                "meeting_id":       meeting_id,
                "summary_path":     str(summary_path),
                "topic_idx":        d["topic_idx"],
                "topic_text":       d["topic_text"],
                "topic_scores_vec": json.dumps(d["topic_scores_vec"]),
                "char_count":       d["char_count"],
                "speech_count":     d["speech_count"],
                "start_speech_idx": d["start_speech_idx"],
                "end_speech_idx":   d["end_speech_idx"],
                "speakers":         ", ".join(d["speakers"]),
                "pass2_chunk_idx":  k,
            }
            for k, d in enumerate(pass2_dialogs)
        ],
    )

    # ── Pass-1 ────────────────────────────────────────────────────────────────
    p1_texts = [d["full_dialog_text"] for d in pass1_dialogs]
    p1_embs  = embedder.embed(p1_texts, ProtocolEmbedder.INSTR_DIALOG_DOC)

    pass1_coll.upsert(
        ids        = [f"{mk}__p1_{i}" for i in range(len(pass1_dialogs))],
        embeddings = [e.tolist() for e in p1_embs],
        documents  = p1_texts,
        metadatas  = [
            {
                "committee":        committee,
                "date":             date,
                "meeting_id":       meeting_id,
                "pass2_id":         f"{mk}__p2_{_find_pass2_idx(pass2_dialogs, d['start_speech_idx'], d['end_speech_idx'])}",
                "topic_idx":        d["topic_idx"],
                "topic_text":       d["topic_text"],
                "topic_scores_vec": json.dumps(d["topic_scores_vec"]),
                "char_count":       d["char_count"],
                "speech_count":     d["speech_count"],
                "start_speech_idx": d["start_speech_idx"],
                "end_speech_idx":   d["end_speech_idx"],
                "speakers":         ", ".join(d["speakers"]),
            }
            for i, d in enumerate(pass1_dialogs)
        ],
    )

    return len(pass1_dialogs), len(pass2_dialogs)


# ── Public API ────────────────────────────────────────────────────────────────

def index_meeting(
    json_path: Path,
    summary_path: Optional[Path],
    chroma_client: chromadb.ClientAPI,
    embedder: ProtocolEmbedder,
    *,
    force: bool = False,
    speeches_coll_name: str = config.SPEECHES_COLLECTION,
    bullets_coll_name:  str = config.BULLETS_COLLECTION,
    pass1_coll_name:    str = config.PASS1_COLLECTION,
    pass2_coll_name:    str = config.PASS2_COLLECTION,
) -> IndexResult:
    """
    Index a single meeting into ChromaDB.

    If summary_path is provided and exists, also indexes bullets and dialogs.
    If summary_path is None or missing, only speeches are indexed.

    Parameters
    ----------
    json_path     : path to the meeting JSON (structured speeches format)
    summary_path  : path to the .txt summary, or None
    chroma_client : open ChromaDB client (PersistentClient or EphemeralClient)
    embedder      : loaded ProtocolEmbedder instance
    force         : re-index even if already present
    *_coll_name   : override collection names (defaults from config.py)
    """
    data = load_meeting(json_path)

    speeches = data.get("speeches") or []
    if not speeches and data.get("full_text"):
        parsed = parse_full_text_speeches(data["full_text"])
        if parsed:
            data["speeches"] = parsed
            speeches = parsed

    if not speeches:
        return IndexResult(status="skip", reason="no structured speeches")

    meeting_id = _get_meeting_id(data, json_path)
    stem       = json_path.stem                   # DD_MM_YYYY_<session_id>
    parts      = stem.split("_")
    date       = "_".join(parts[:3])
    committee  = str(data.get("committee", json_path.parent.name))

    result = IndexResult(status="ok")

    speeches_coll = _get_or_create(chroma_client, speeches_coll_name)
    n_sp, speech_embs = _index_speeches(data, json_path, speeches_coll, embedder, force)
    result.speeches = n_sp

    if summary_path is not None and summary_path.exists():
        bullets_coll = _get_or_create(chroma_client, bullets_coll_name)
        n_bl, _ = _index_bullets(
            summary_path, committee, meeting_id, date,
            bullets_coll, embedder, force,
        )
        result.bullets = n_bl

        pass1_coll = _get_or_create(chroma_client, pass1_coll_name)
        pass2_coll = _get_or_create(chroma_client, pass2_coll_name)
        n_p1, n_p2 = _index_dialogs(
            data, summary_path, committee, meeting_id, date,
            pass1_coll, pass2_coll, embedder, speech_embs, force,
        )
        result.pass1 = n_p1
        result.pass2 = n_p2

    return result
