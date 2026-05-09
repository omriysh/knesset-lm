"""Tool implementations + dispatch helpers — agent-agnostic.

This module is the function-bag layer of the tool surface (per design §5.2).
It owns:

  * the :class:`ToolSpec` dataclass that ``research_agent/tools.py`` uses to
    enumerate the registry,
  * a generic :func:`dispatch` that looks a tool up in any registry and
    invokes its handler with raw kwargs,
  * one ``handle_*`` function per tool in the v1 inventory (§5.3).

This module deliberately holds *no* registry — registry construction lives
in the agent-specific module that knows which subset of tools to expose
(per §5.1 #5: the planner drives the surface, not the SM). Imports flow
upward only: ``utils/`` may not import from ``agent/`` (project CLAUDE.md
import convention), so the registry has to live one layer up.

ToolEnvelope contract: every handler returns a
:class:`agent.subgraph.evidence.ToolEnvelope` — never raises, never returns
``None``. Argument-validation failures and infrastructure errors (missing
BM25 db, network exception, etc.) are reported via the envelope's ``error``
field per §4.3.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import config
from agent.subgraph.evidence import ToolEnvelope
from retrieval.bm25_index import BM25Index
from retrieval.lemmatize import lemmatize
from utils.knesset_db import (
    _get_bill_details_by_id,
    _get_bill_text_by_id,
    _resolve_bill_by_name,
    get_bill_details,
    get_party_members,
    get_session_transcript,
)
from utils.tool_helpers.adapters import (
    adapt_get_committee_members,
    adapt_get_committee_sessions,
    adapt_get_mk_votes,
    adapt_get_recent_votes,
    adapt_get_votes_on_topic,
    adapt_get_votes_on_topic_by_mk,
    fetch_committee_record,
)
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex
from utils.tool_helpers.name_search import name_search


# ---------------------------------------------------------------------------
# ToolSpec dataclass
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    """Registry entry for a single tool.

    Field set follows design §5.2 with one minor addition: ``description``
    is exposed as a top-level field (it is part of the JSON schema in
    practice, but planner-prompt rendering treats it as a header so it is
    handy to keep it indexable).

    Per project CLAUDE.md, the dataclass uses ``to_dict`` / ``from_dict``
    explicitly — Pydantic is forbidden.
    """

    name: str
    schema: dict
    handler: Callable[..., ToolEnvelope]
    task_kinds: list[str] = field(default_factory=list)
    cost_hint: str = "cheap"
    planner_only: bool = False

    def to_dict(self) -> dict:
        """Serialise the registry-visible fields. Handler is omitted."""
        return {
            "name":         self.name,
            "schema":       dict(self.schema or {}),
            "task_kinds":   list(self.task_kinds),
            "cost_hint":    self.cost_hint,
            "planner_only": bool(self.planner_only),
        }


ToolRegistry = list[ToolSpec]


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def dispatch(registry: ToolRegistry, name: str, args: dict) -> ToolEnvelope:
    """Look up a tool by name in ``registry`` and invoke its handler.

    Never raises. Unknown names → ``error="unknown_tool"``. Handler
    exceptions → ``error="dispatch_exception"`` with the traceback in
    metadata.
    """
    spec = _find_spec(registry, name)
    if spec is None:
        print(f"[tools] unknown tool: {name!r}", file=sys.stderr, flush=True)
        return ToolEnvelope(
            summary="",
            full="",
            metadata={"kind": "error", "source": "dispatch", "count": 0},
            provenance={"tool_name": name},
            error="unknown_tool",
        )

    args_safe = _safe_args(args)
    args_preview = json.dumps(args_safe, ensure_ascii=False)[:300]
    print(f"[tools] → {name}  args={args_preview}", flush=True)

    try:
        result = spec.handler(args or {})
    except Exception as exc:  # noqa: BLE001 — surface to envelope
        print(
            f"[tools] ✗ {name} EXCEPTION {type(exc).__name__}: {exc}\n"
            + traceback.format_exc(),
            file=sys.stderr, flush=True,
        )
        return ToolEnvelope(
            summary="",
            full="",
            metadata={
                "kind":      "error",
                "source":    "dispatch",
                "count":     0,
                "exception": str(exc),
                "traceback": traceback.format_exc(),
            },
            provenance={"tool_name": name, "args": args_safe},
            error="dispatch_exception",
        )

    if not isinstance(result, ToolEnvelope):
        print(f"[tools] ← {name}  (non-envelope result)", flush=True)
        return ToolEnvelope(
            summary="",
            full=json.dumps(result, ensure_ascii=False, default=str)
                 if result is not None else "",
            metadata={"kind": "fetch", "source": "dispatch", "count": 0},
            provenance={"tool_name": name},
            error="handler_returned_non_envelope",
        )

    status = f"error={result.error}" if result.error else "ok"
    summary_preview = (result.summary or "")[:120]
    print(f"[tools] ← {name}  {status}  summary={summary_preview!r}", flush=True)
    print(f"[tools] ← {name}  {status}  full:", flush=True)
    print(result.full or "", flush=True)
    return result


def _find_spec(registry: ToolRegistry, name: str) -> ToolSpec | None:
    for spec in registry or []:
        if spec.name == name:
            return spec
    return None


def _safe_args(args: dict) -> dict:
    """Return a JSON-roundtrippable copy of ``args`` for provenance."""
    try:
        return json.loads(json.dumps(args, ensure_ascii=False, default=str))
    except Exception as exc:
        print(f"[tools] _safe_args serialisation failed: {exc}", file=sys.stderr, flush=True)
        return {"_repr": repr(args)}


# ---------------------------------------------------------------------------
# Shared infra: BM25 index resolution
# ---------------------------------------------------------------------------


def _bm25_path(target: str, knesset_num: int) -> Path:
    """Path layout matches ``scripts/build_bm25_indexes.py``."""
    return config.BM25_DIR / str(knesset_num) / f"{target}.db"


def _open_bm25(target: str, knesset_num: int) -> BM25Index | None:
    """Return an opened :class:`BM25Index` or ``None`` if the db file is missing.

    The ``.db`` file is built offline by ``scripts/build_bm25_indexes.py``;
    if a deployment hasn't run that script yet, the find_* tools must fail
    with a clean ``bm25_db_missing`` error rather than crashing.
    """
    path = _bm25_path(target, knesset_num)
    if not path.exists():
        return None
    return BM25Index(path)


def _bm25_missing_envelope(target: str, knesset_num: int) -> ToolEnvelope:
    return ToolEnvelope(
        summary="",
        full="",
        metadata={"kind": "error", "source": "bm25", "count": 0, "target": target},
        provenance={"target": target, "knesset_num": knesset_num,
                    "expected_path": str(_bm25_path(target, knesset_num))},
        error="bm25_db_missing",
    )


# ---------------------------------------------------------------------------
# search_topics — bullets-only L1 hybrid search
# ---------------------------------------------------------------------------


def handle_search_topics(args: dict) -> ToolEnvelope:
    """Hybrid BM25 (FTS5) + dense (Chroma) search over summary bullets.

    Returns the top-``top_k`` bullets after RRF fusion. Each result carries
    enough metadata for the executor to know which meeting and section the
    bullet came from. Embedding-side failures degrade to BM25-only with a
    ``embedding_unavailable`` warning instead of erroring out — keeps the
    tool useful when llama-server / GPU isn't up.
    """
    query = (args.get("query") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_k = int(args.get("top_k") or config.SEARCH_TOPICS_DEFAULT_TOP_K)
    top_k = max(1, min(top_k, config.SEARCH_TOPICS_MAX_TOP_K))

    if not query:
        return _validation_error("missing_query", kind="search", source="hybrid",
                                 query=query, knesset_num=knesset_num)

    bm25 = _open_bm25("bullets", knesset_num)
    if bm25 is None:
        return _bm25_missing_envelope("bullets", knesset_num)

    warnings: list[str] = []
    bm25_ranking: list[str] = []
    try:
        rows = bm25.search(
            _quote_match(lemmatize(query)) or query,
            top_k=config.HYBRID_FIRST_STAGE_TOP_K,
        )
        bm25_rows: dict[str, dict] = {}
        for r in rows:
            rid = str(r.get("id") or "")
            if rid:
                bm25_rows[rid] = r
                bm25_ranking.append(rid)
    except Exception as exc:
        bm25.close()
        return ToolEnvelope(
            summary="",
            full="",
            metadata={"kind": "error", "source": "bm25", "count": 0,
                      "exception": str(exc)},
            provenance={"query": query, "knesset_num": knesset_num},
            error="bm25_search_failed",
        )

    # Optional dense rerank via Chroma. We *try* it but don't make the tool
    # depend on it — a fresh checkout without an indexed Chroma collection
    # should still get BM25-only results.
    embed_ranking: list[str] = []
    try:
        embed_ranking = _embed_bullet_ranking(
            query=query,
            top_k=config.HYBRID_FIRST_STAGE_TOP_K,
        )
    except Exception:
        warnings.append("embedding_unavailable")

    # RRF fuse — only when both sides produced something.
    if embed_ranking:
        from retrieval.hybrid import rrf_fuse  # local import: optional dep
        fused = rrf_fuse([bm25_ranking, embed_ranking], top_k=top_k)
    else:
        fused = bm25_ranking[:top_k]

    # Fetch any Chroma-only IDs (not in BM25 search results) before closing.
    missing = [rid for rid in fused if rid not in bm25_rows]
    if missing:
        bm25_rows.update(bm25.fetch_by_ids(missing))

    bm25.close()

    if not fused:
        return ToolEnvelope(
            summary="",
            full=json.dumps([], ensure_ascii=False),
            metadata={"kind": "search", "source": "hybrid", "count": 0,
                      "total_match": len(bm25_ranking),
                      **({"warnings": warnings} if warnings else {})},
            provenance={"query": query, "knesset_num": knesset_num, "top_k": top_k},
        )

    # Build full result list from BM25 rows (richer metadata than embed
    # rankings, which only have ids).
    payload: list[dict] = []
    for rid in fused:
        row = bm25_rows.get(rid)
        extra = row.get("extra") if (row and isinstance(row.get("extra"), dict)) else {}
        payload.append({
            "bullet_id":  rid,
            "label":      (row or {}).get("label") or "",
            "text":       (row or {}).get("body") or "",
            "meeting_id": extra.get("meeting_id"),
            "committee":  extra.get("committee"),
            "bullet_idx": extra.get("bullet_idx"),
        })

    metadata = {
        "kind":        "search",
        "source":      "hybrid",
        "count":       len(payload),
        "total_match": len(bm25_ranking),
    }
    if warnings:
        metadata["warnings"] = warnings

    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False),
        metadata=metadata,
        provenance={"query": query, "knesset_num": knesset_num, "top_k": top_k},
    )


def _embed_bullet_ranking(*, query: str, top_k: int) -> list[str]:
    """Return Chroma's id ranking for the bullets collection.

    Imports are local: this is the only path that pulls in chromadb /
    transformers, and we don't want to pay that import cost when the
    embedding side is unavailable.
    """
    import chromadb

    client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
    coll = client.get_collection(config.BULLETS_COLLECTION)
    from indexing.embedder import ProtocolEmbedder
    embedder = ProtocolEmbedder()
    q_emb = embedder.embed([query], ProtocolEmbedder.INSTR_QUERY)
    res = coll.query(
        query_embeddings=q_emb.tolist(),
        n_results=top_k,
        include=["metadatas"],
    )
    return [str(i) for i in (res.get("ids") or [[]])[0]]


# ---------------------------------------------------------------------------
# search_protocols_keyword — BM25 over speech text
# ---------------------------------------------------------------------------


def handle_search_protocols_keyword(args: dict) -> ToolEnvelope:
    """BM25 over indexed speeches with optional axis filters.

    Filters (committee, meeting, speaker, date range) are applied via the
    FTS5 WHERE clause against the row's ``extra`` JSON column; this is
    cheap because FTS5 evaluates the MATCH first and only filters the
    candidate set.
    """
    query = (args.get("query") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_k = int(args.get("top_k") or config.SEARCH_PROTOCOLS_DEFAULT_TOP_K)
    top_k = max(1, min(top_k, config.SEARCH_PROTOCOLS_MAX_TOP_K))
    sort = (args.get("sort") or "relevance").lower()

    if not query:
        return _validation_error("missing_query", kind="search", source="bm25",
                                 query=query, knesset_num=knesset_num)

    bm25 = _open_bm25("speeches", knesset_num)
    if bm25 is None:
        return _bm25_missing_envelope("speeches", knesset_num)

    where_parts: list[str] = []
    committee_ids = args.get("committee_ids") or []
    meeting_ids = args.get("meeting_ids") or []
    speaker = (args.get("speaker") or "").strip()
    date_from = (args.get("date_from") or "").strip()
    date_to = (args.get("date_to") or "").strip()

    for cid in committee_ids:
        where_parts.append(f"extra LIKE '%\"committee\": \"{_sql_safe(str(cid))}\"%'")
    for mid in meeting_ids:
        where_parts.append(f"extra LIKE '%\"meeting_id\": \"{_sql_safe(str(mid))}\"%'")
    if speaker:
        where_parts.append(f"extra LIKE '%\"speaker\": \"%{_sql_safe(speaker)}%\"%'")

    where = " AND ".join(where_parts) if where_parts else None

    try:
        rows = bm25.search(
            _quote_match(lemmatize(query)) or query,
            top_k=max(top_k, config.KEYWORD_RERANK_TOP_K) if sort == "relevance" else top_k,
            where=where,
        )
    except Exception as exc:
        bm25.close()
        return ToolEnvelope(
            summary="",
            full="",
            metadata={"kind": "error", "source": "bm25", "count": 0,
                      "exception": str(exc)},
            provenance={"query": query, "knesset_num": knesset_num,
                        "filters": {"committee_ids": committee_ids,
                                    "meeting_ids": meeting_ids,
                                    "speaker": speaker,
                                    "date_from": date_from,
                                    "date_to": date_to}},
            error="bm25_search_failed",
        )
    finally:
        bm25.close()

    # Date filters: applied post-fetch since the FTS5 row's ``extra`` carries
    # ``meeting_id`` (a numeric session id), not a literal date. The caller
    # who needs date scoping has typically narrowed to a committee already.
    if date_from or date_to:
        # We don't have date metadata on the speech row itself; this is a
        # known v1 limitation. Surface it as a warning instead of silently
        # dropping the filter.
        rows = rows  # noqa: PLW0127 — intentional no-op; warning below
        warnings_extra = ["date_filter_unsupported_v1"]
    else:
        warnings_extra = []

    payload: list[dict] = []
    for r in rows[:top_k]:
        extra = r.get("extra") if isinstance(r.get("extra"), dict) else {}
        payload.append({
            "speech_id":  str(r.get("id") or ""),
            "label":      r.get("label") or "",
            "text":       r.get("body") or "",
            "meeting_id": extra.get("meeting_id"),
            "committee":  extra.get("committee"),
            "speaker":    extra.get("speaker"),
            "speech_idx": extra.get("speech_idx"),
        })

    metadata = {
        "kind":        "search",
        "source":      "bm25",
        "count":       len(payload),
        "total_match": len(rows),
    }
    if warnings_extra:
        metadata["warnings"] = warnings_extra

    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False),
        metadata=metadata,
        provenance={
            "query":       query,
            "knesset_num": knesset_num,
            "top_k":       top_k,
            "sort":        sort,
            "filters": {
                "committee_ids": list(committee_ids),
                "meeting_ids":   list(meeting_ids),
                "speaker":       speaker or None,
                "date_from":     date_from or None,
                "date_to":       date_to or None,
            },
        },
    )


def _sql_safe(s: str) -> str:
    """Strip characters that would break the LIKE-clause string."""
    return s.replace("'", "").replace("%", "").replace("_", "")


# ---------------------------------------------------------------------------
# Find-* tools — BM25 → candidate records
# ---------------------------------------------------------------------------


def _build_mk_full_profile(record: dict, knesset_num: int) -> dict:
    """Return a clean MK profile dict filtered to the given Knesset."""
    def _kn_filter(items: list, key: str = "knesset") -> list:
        return [x for x in (items or []) if not isinstance(x, dict) or x.get(key) in (None, knesset_num)]

    return {
        "mk_id":               str(record.get("mk_individual_id") or record.get("PersonID") or ""),
        "full_name":           record.get("full_name") or record.get("mk_individual_name") or "",
        "is_current":          record.get("IsCurrent", False),
        "factions":            _kn_filter(record.get("factions")),
        "committee_positions": _kn_filter(record.get("committee_positions")),
        "govministries":       _kn_filter(record.get("govministries")),
        "faction_chairpersons": _kn_filter(record.get("faction_chairpersons")),
    }


def handle_find_mk(args: dict) -> ToolEnvelope:
    query       = (args.get("query") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_k       = max(1, int(args.get("top_k") or 5))

    if not query:
        return _validation_error("missing_query", kind="search", source="bm25_mks",
                                 knesset_num=knesset_num)

    bm25 = _open_bm25("mks", knesset_num)
    if bm25 is None:
        return _bm25_missing_envelope("mks", knesset_num)

    try:
        fuzzy = FuzzyNameIndex.from_bm25(bm25)
    finally:
        bm25.close()

    candidates = name_search(query, fuzzy_index=fuzzy, knesset_num=knesset_num, top_k=top_k)

    payload: list[dict] = []
    for c in candidates:
        raw = _fetch_mk_record(c["id"])
        item: dict = {
            "mk_id":     c["id"],
            "full_name": c["label"],
            "score":     c["score"],
        }
        if raw is not None:
            item["profile"] = _build_mk_full_profile(raw, knesset_num)
        payload.append(item)

    warnings: list[str] = []
    if payload and not payload[0].get("profile"):
        warnings.append("low_confidence_match")

    metadata: dict = {"kind": "search", "source": "bm25_mks", "count": len(payload)}
    if warnings:
        metadata["warnings"] = warnings

    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False),
        metadata=metadata,
        provenance={"query": query, "knesset_num": knesset_num, "top_k": top_k},
    )


def handle_find_committee(args: dict) -> ToolEnvelope:
    return _generic_find(
        args,
        target="committees",
        kind="search",
        source="bm25_committees",
        id_key="committee_id",
        label_key="name",
        fetch_record=fetch_committee_record,
        default_top_k=5,
    )


def handle_find_bill(args: dict) -> ToolEnvelope:
    return _generic_find(
        args,
        target="bills",
        kind="search",
        source="bm25_bills",
        id_key="bill_id",
        label_key="bill_name",
        fetch_record=lambda eid: _fetch_bill_record(eid),
        default_top_k=5,
    )


def handle_find_vote(args: dict) -> ToolEnvelope:
    return _generic_find(
        args,
        target="votes",
        kind="search",
        source="bm25_votes",
        id_key="vote_id",
        label_key="title",
        fetch_record=lambda eid: _fetch_vote_record(eid),
        default_top_k=10,
    )


def handle_find_party(args: dict) -> ToolEnvelope:
    """Fuzzy-match a party name and return all its members for a given Knesset."""
    query       = (args.get("query") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_k       = int(args.get("top_k") or 3)

    if not query:
        return _validation_error("missing_query", kind="search", source="parties",
                                 knesset_num=knesset_num)

    results = get_party_members(party_query=query, knesset_num=knesset_num, top_k=top_k)

    if not results:
        return ToolEnvelope(
            summary=f"לא נמצאו מפלגות לשאילתה '{query}'",
            full="[]",
            metadata={"kind": "search", "source": "parties", "count": 0},
            provenance={"query": query, "knesset_num": knesset_num},
        )

    summary_parts = [f"{r['party']} ({r['mk_count']} ח\"כ)" for r in results]
    return ToolEnvelope(
        summary=f"מפלגות: {', '.join(summary_parts)}",
        full=json.dumps(results, ensure_ascii=False),
        metadata={"kind": "search", "source": "parties", "count": len(results)},
        provenance={"query": query, "knesset_num": knesset_num},
    )


def _generic_find(
    args: dict,
    *,
    target: str,
    kind: str,
    source: str,
    id_key: str,
    label_key: str,
    fetch_record: Callable[[str], dict | None],
    default_top_k: int,
) -> ToolEnvelope:
    query = (args.get("query") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_k = int(args.get("top_k") or default_top_k)
    top_k = max(1, top_k)

    if not query:
        return _validation_error(
            "missing_query", kind=kind, source=source,
            query=query, knesset_num=knesset_num,
        )

    bm25 = _open_bm25(target, knesset_num)
    if bm25 is None:
        return _bm25_missing_envelope(target, knesset_num)

    try:
        fuzzy = FuzzyNameIndex.from_bm25(bm25)
    finally:
        bm25.close()

    candidates = name_search(
        query,
        fuzzy_index=fuzzy,
        fetch_by_id=fetch_record,
        knesset_num=knesset_num,
        top_k=top_k,
    )

    payload: list[dict] = []
    for c in candidates:
        item: dict = {
            id_key:    c["id"],
            label_key: c["label"],
            "score":   c["score"],
            "fetched": c.get("fetched", False),
        }
        if c.get("record"):
            item["record"] = c["record"]
        if c.get("extra"):
            item["extra"] = c["extra"]
        payload.append(item)

    warnings: list[str] = []
    if payload and not payload[0]["fetched"]:
        warnings.append("low_confidence_match")

    metadata = {"kind": kind, "source": source, "count": len(payload)}
    if warnings:
        metadata["warnings"] = warnings

    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False),
        metadata=metadata,
        provenance={"query": query, "knesset_num": knesset_num, "top_k": top_k},
    )


def _fetch_mk_record(mk_id: str) -> dict | None:
    """Look up an MK by id by walking the cached members lists."""
    from utils.knesset_db import _fetch_members
    target = str(mk_id)
    for is_current in (True, False):
        try:
            members = _fetch_members(is_current)
        except Exception:
            continue
        for mk in members:
            if str(mk.get("mk_individual_id") or "") == target or \
               str(mk.get("PersonID") or "") == target:
                return mk
    return None


def _fetch_bill_record(bill_id: str) -> dict | None:
    try:
        bid = int(bill_id)
    except (TypeError, ValueError):
        return None
    return _get_bill_details_by_id(bid)


def _fetch_vote_record(vote_id: str) -> dict | None:
    """Best-effort vote-by-id fetch via OData KNS_PlenumVote."""
    try:
        import requests
        r = requests.get(
            f"{config.OFFICIAL_KNESSET_NEW_API}/KNS_PlenumVote({int(vote_id)})",
            timeout=config.API_TIMEOUT,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Profile / fetch passthroughs (delegate to adapters)
# ---------------------------------------------------------------------------


def handle_get_mk_profile(args: dict) -> ToolEnvelope:
    mk_id = (args.get("mk_id") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)

    if not mk_id:
        return _validation_error(
            "missing_mk_id", kind="fetch", source="oknesset",
            knesset_num=knesset_num,
        )
    record = _fetch_mk_record(mk_id)
    if record is None:
        return _validation_error(
            "mk_not_found", kind="fetch", source="oknesset",
            mk_id=mk_id, knesset_num=knesset_num,
        )
    return ToolEnvelope(
        summary="",
        full=json.dumps(record, ensure_ascii=False, default=str),
        metadata={"kind": "fetch", "source": "oknesset", "count": 1},
        provenance={"mk_id": mk_id, "knesset_num": knesset_num},
    )


def handle_get_mk_committees(args: dict) -> ToolEnvelope:
    mk_id = (args.get("mk_id") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)

    if not mk_id:
        return _validation_error(
            "missing_mk_id", kind="fetch", source="oknesset",
            knesset_num=knesset_num,
        )
    record = _fetch_mk_record(mk_id)
    if record is None:
        return _validation_error(
            "mk_not_found", kind="fetch", source="oknesset",
            mk_id=mk_id, knesset_num=knesset_num,
        )
    positions = record.get("committee_positions") or []
    filtered = [
        p for p in positions
        if not isinstance(p, dict) or p.get("knesset") in (None, knesset_num)
    ]
    payload = {
        "mk_id":               mk_id,
        "full_name":           record.get("full_name") or record.get("mk_individual_name") or "",
        "knesset_num":         knesset_num,
        "committee_positions": filtered,
    }
    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False, default=str),
        metadata={"kind": "fetch", "source": "oknesset", "count": 1},
        provenance={"mk_id": mk_id, "knesset_num": knesset_num},
    )


def handle_get_committee_members(args: dict) -> ToolEnvelope:
    committee_id = (args.get("committee_id") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)

    if not committee_id:
        return _validation_error(
            "missing_committee_id", kind="fetch", source="oknesset",
            knesset_num=knesset_num,
        )
    record = fetch_committee_record(committee_id)
    if record is None:
        return _validation_error(
            "committee_not_found", kind="fetch", source="oknesset",
            committee_id=committee_id, knesset_num=knesset_num,
        )
    return adapt_get_committee_members(
        name=record.get("name") or "",
        knesset_num=knesset_num,
    )


def handle_get_committee_sessions(args: dict) -> ToolEnvelope:
    committee_id = (args.get("committee_id") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    date_from = args.get("date_from")
    date_to = args.get("date_to")

    if not committee_id:
        return _validation_error(
            "missing_committee_id", kind="fetch", source="odata",
            knesset_num=knesset_num,
        )

    return adapt_get_committee_sessions(
        committee_id=committee_id,
        knesset_num=knesset_num,
        date_from=date_from,
        date_to=date_to,
    )


def handle_get_bill_details(args: dict) -> ToolEnvelope:
    bill_id = (args.get("bill_id") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)

    if not bill_id:
        return _validation_error(
            "missing_bill_id", kind="fetch", source="odata",
            knesset_num=knesset_num,
        )
    record = _fetch_bill_record(bill_id)
    if record is None:
        return _validation_error(
            "bill_not_found", kind="fetch", source="odata",
            bill_id=bill_id, knesset_num=knesset_num,
        )
    return ToolEnvelope(
        summary="",
        full=json.dumps(record, ensure_ascii=False, default=str),
        metadata={"kind": "fetch", "source": "odata", "count": 1},
        provenance={"bill_id": bill_id, "knesset_num": knesset_num},
    )


def handle_get_bill_text(args: dict) -> ToolEnvelope:
    bill_id = (args.get("bill_id") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    max_chars = int(args.get("max_chars") or config.BILL_TEXT_DEFAULT_MAX_CHARS)
    max_chars = max(
        config.BILL_TEXT_MIN_MAX_CHARS,
        min(max_chars, config.BILL_TEXT_MAX_MAX_CHARS),
    )

    if not bill_id:
        return _validation_error(
            "missing_bill_id", kind="fetch", source="odata",
            knesset_num=knesset_num,
        )
    try:
        record = _get_bill_text_by_id(int(bill_id), max_chars=max_chars)
    except Exception as exc:
        return ToolEnvelope(
            summary="",
            full="",
            metadata={"kind": "error", "source": "odata", "count": 0,
                      "exception": str(exc)},
            provenance={"bill_id": bill_id, "knesset_num": knesset_num},
            error="bill_text_fetch_failed",
        )
    if record is None:
        return _validation_error(
            "bill_text_not_found", kind="fetch", source="odata",
            bill_id=bill_id, knesset_num=knesset_num,
        )
    warnings = ["result_truncated_to_%d_chars" % max_chars] if record.get("truncated") else []
    return ToolEnvelope(
        summary="",
        full=json.dumps(record, ensure_ascii=False, default=str),
        metadata={
            "kind":   "fetch",
            "source": "odata",
            "count":  1,
            **({"warnings": warnings} if warnings else {}),
        },
        provenance={"bill_id": bill_id, "knesset_num": knesset_num},
        truncated=bool(record.get("truncated")),
    )


# ---------------------------------------------------------------------------
# Voting tools (merged)
# ---------------------------------------------------------------------------


def handle_query_voting_records(args: dict) -> ToolEnvelope:
    """Unified voting query — behaviour determined by which params are supplied:
      topic + mk_id → how that MK voted on matching votes
      mk_id only    → recent votes cast by the MK
      topic only    → votes matching the topic keyword
      neither       → most recent votes overall
    """
    topic = (args.get("topic") or "").strip()
    mk_id = (args.get("mk_id") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_n = int(args.get("top_n") or 20)

    if mk_id:
        record = _fetch_mk_record(mk_id)
        if record is None:
            return _validation_error(
                "mk_not_found", kind="fetch", source="odata",
                mk_id=mk_id, knesset_num=knesset_num,
            )
        name = record.get("full_name") or record.get("mk_individual_name") or ""
        if topic:
            return adapt_get_votes_on_topic_by_mk(
                topic=topic, name=name, knesset_num=knesset_num, top_n=top_n,
            )
        return adapt_get_mk_votes(name=name, knesset_num=knesset_num, top_n=top_n)

    if topic:
        return adapt_get_votes_on_topic(topic=topic, top_n=top_n)

    return adapt_get_recent_votes(top_n=top_n, knesset_num=knesset_num)


# ---------------------------------------------------------------------------
# get_meeting_summary
# ---------------------------------------------------------------------------


def handle_get_meeting_summary(args: dict) -> ToolEnvelope:
    """Return the raw .txt summary of a meeting; optional 1-indexed section."""
    meeting_id = (args.get("meeting_id") or "").strip()
    section_num = args.get("section_num")
    if section_num is not None:
        try:
            section_num = int(section_num)
        except (TypeError, ValueError):
            return _validation_error(
                "invalid_section_num", kind="fetch", source="summaries",
                meeting_id=meeting_id, section_num=section_num,
            )

    if not meeting_id:
        return _validation_error(
            "missing_meeting_id", kind="fetch", source="summaries",
        )

    summary_path = _find_summary_path(meeting_id)
    if summary_path is None:
        return ToolEnvelope(
            summary="",
            full="",
            metadata={"kind": "fetch", "source": "summaries", "count": 0},
            provenance={"meeting_id": meeting_id},
            error="summary_not_found",
        )

    try:
        text = summary_path.read_text(encoding="utf-8")
    except Exception as exc:
        return ToolEnvelope(
            summary="",
            full="",
            metadata={"kind": "error", "source": "summaries", "count": 0,
                      "exception": str(exc)},
            provenance={"meeting_id": meeting_id, "path": str(summary_path)},
            error="summary_read_failed",
        )

    payload: Any = text
    if section_num is not None:
        sections = _split_summary_sections(text)
        if 1 <= section_num <= len(sections):
            payload = sections[section_num - 1]
        else:
            return ToolEnvelope(
                summary="",
                full="",
                metadata={"kind": "fetch", "source": "summaries", "count": 0,
                          "total_sections": len(sections)},
                provenance={"meeting_id": meeting_id, "section_num": section_num},
                error="section_out_of_range",
            )

    return ToolEnvelope(
        summary="",
        full=payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
        metadata={"kind": "fetch", "source": "summaries", "count": 1},
        provenance={
            "meeting_id":  meeting_id,
            "path":        str(summary_path),
            "section_num": section_num,
            "committee":   summary_path.parent.name,
        },
    )


def _find_summary_path(meeting_id: str) -> Path | None:
    """Locate the .txt summary file matching ``meeting_id`` across all
    Knessets / committees. Filenames look like ``DD_MM_YYYY_<session_id>.txt``;
    we glob for the trailing session id.
    """
    target = str(meeting_id)
    root = config.DATA_DIR / "summaries"
    if not root.exists():
        return None
    matches = list(root.rglob(f"*_{target}.txt"))
    if matches:
        return matches[0]
    # Fall back to exact-stem match in case the filename layout shifts.
    matches = list(root.rglob(f"{target}.txt"))
    return matches[0] if matches else None


def _split_summary_sections(text: str) -> list[str]:
    """Split a Hebrew summary by ``##``-level headings; each section keeps
    its leading heading line. The pre-amble (before the first heading) is
    NOT counted as a section, matching the design's 1-indexed semantics.
    """
    lines = text.splitlines()
    sections: list[list[str]] = []
    current: list[str] | None = None
    for ln in lines:
        if ln.startswith("## ") or ln.startswith("# "):
            if current is not None:
                sections.append(current)
            current = [ln]
        elif current is not None:
            current.append(ln)
    if current is not None:
        sections.append(current)
    return ["\n".join(s).rstrip() for s in sections]


# ---------------------------------------------------------------------------
# deep_dive_meeting (planner-only handler)
# ---------------------------------------------------------------------------


def handle_deep_dive_meeting(args: dict) -> ToolEnvelope:
    """Delegate to :func:`retrieval.deep_dive.deep_dive_meeting`.

    Imports of the heavy retrieval module are deferred so simply *loading*
    the registry (e.g. for schema introspection) doesn't spin up
    chromadb/transformers.
    """
    meeting_id = (args.get("meeting_id") or "").strip()
    focus_query = (args.get("focus_query") or "").strip()
    mode = (args.get("mode") or "rerank").lower()

    if not meeting_id:
        return _validation_error(
            "missing_meeting_id", kind="analysis", source="deep_dive",
        )
    if not focus_query:
        return _validation_error(
            "missing_focus_query", kind="analysis", source="deep_dive",
            meeting_id=meeting_id,
        )
    if mode not in ("rerank", "full"):
        return _validation_error(
            "invalid_mode", kind="analysis", source="deep_dive",
            meeting_id=meeting_id, mode=mode,
        )

    try:
        from retrieval.deep_dive import deep_dive_meeting as _dd
        envelope = _dd(meeting_id=meeting_id, query=focus_query, mode=mode)
    except Exception as exc:
        return ToolEnvelope(
            summary="",
            full="",
            metadata={"kind": "error", "source": "deep_dive", "count": 0,
                      "exception": str(exc),
                      "traceback": traceback.format_exc()},
            provenance={"meeting_id": meeting_id, "mode": mode},
            error="deep_dive_failed",
        )

    if not isinstance(envelope, ToolEnvelope):
        return ToolEnvelope(
            summary="",
            full=json.dumps(envelope, ensure_ascii=False, default=str)
                 if envelope is not None else "",
            metadata={"kind": "analysis", "source": "deep_dive", "count": 0},
            provenance={"meeting_id": meeting_id, "mode": mode},
            error="deep_dive_returned_non_envelope",
        )
    return envelope


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _validation_error(error_code: str, *, kind: str, source: str, **prov) -> ToolEnvelope:
    return ToolEnvelope(
        summary="",
        full="",
        metadata={"kind": kind, "source": source, "count": 0},
        provenance=dict(prov),
        error=error_code,
    )


_FTS5_META = set('"*():^-+')


def _safe_match(text: str) -> str:
    """Strip FTS5 metacharacters."""
    return "".join(ch for ch in text if ch not in _FTS5_META).strip() or text


def _quote_match(text: str) -> str:
    """Wrap each whitespace-separated token in double quotes (FTS5)."""
    tokens = [tok for tok in text.split() if tok.strip()]
    if not tokens:
        return text
    return " ".join(f'"{_safe_match(tok)}"' for tok in tokens if _safe_match(tok))


# Suppress unused-import warnings — these are part of the public dispatch
# path even if some IDEs don't resolve indirect uses.
_ = (get_bill_details, get_session_transcript, _resolve_bill_by_name)


__all__ = [
    "ToolSpec",
    "ToolRegistry",
    "dispatch",
    # search
    "handle_search_topics",
    "handle_search_protocols_keyword",
    # find
    "handle_find_mk",
    "handle_find_committee",
    "handle_find_bill",
    "handle_find_vote",
    "handle_find_party",
    # fetch
    "handle_get_mk_profile",
    "handle_get_mk_committees",
    "handle_get_committee_members",
    "handle_get_committee_sessions",
    "handle_get_bill_details",
    "handle_get_bill_text",
    "handle_get_meeting_summary",
    # votes
    "handle_query_voting_records",
    # deep
    "handle_deep_dive_meeting",
]
