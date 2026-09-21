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
knesset.db, network exception, etc.) are reported via the envelope's ``error``
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
from retrieval import knesset_db_store as store
from retrieval.ktiv import expand_token
from retrieval.lemmatize import lemmatize
from utils.speech import name_query_matches, name_tokens
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
    ui: dict = field(default_factory=dict)
    compact_spec: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialise the registry-visible fields. Handler is omitted."""
        return {
            "name":         self.name,
            "schema":       dict(self.schema or {}),
            "task_kinds":   list(self.task_kinds),
            "cost_hint":    self.cost_hint,
            "ui":           dict(self.ui or {}),
            "compact_spec": dict(self.compact_spec or {}),
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
# Shared infra: knesset.db
# ---------------------------------------------------------------------------


def _open_db():
    """Open knesset.db (built offline by scripts/build_knesset_db.py) or None when missing."""
    if not store.exists():
        return None
    return store.connect()


def _db_missing_envelope(target: str, knesset_num: int) -> ToolEnvelope:
    return ToolEnvelope(
        summary="",
        full="",
        metadata={"kind": "error", "source": "knesset_db", "count": 0, "target": target},
        provenance={"target": target, "knesset_num": knesset_num,
                    "expected_path": str(store.db_path())},
        error="knesset_db_missing",
    )


def _db_error_envelope(exc: Exception, source: str, **prov) -> ToolEnvelope:
    print(f"[tools] {source} query failed: {exc}")
    return ToolEnvelope(
        summary="",
        full="",
        metadata={"kind": "error", "source": source, "count": 0, "exception": str(exc)},
        provenance=prov,
        error="db_search_failed",
    )


# ---------------------------------------------------------------------------
# search_topics — FTS over summary topics
# ---------------------------------------------------------------------------


def handle_search_topics(args: dict) -> ToolEnvelope:
    """FTS5 (BM25-ranked) search over the summary topics table."""
    query = (args.get("query") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_k = int(args.get("top_k") or config.SEARCH_TOPICS_DEFAULT_TOP_K)
    top_k = max(1, min(top_k, config.SEARCH_TOPICS_MAX_TOP_K))
    committees = [str(c).replace("_", " ") for c in (args.get("committees") or [])]
    date_from = (args.get("date_from") or "").strip() or None
    date_to = (args.get("date_to") or "").strip() or None

    if not query:
        return _validation_error("missing_query", kind="search", source="topics",
                                 query=query, knesset_num=knesset_num)

    conn = _open_db()
    if conn is None:
        return _db_missing_envelope("topics", knesset_num)
    try:
        rows = store.search_topics(
            conn, _expand_match(lemmatize(query), "topics_fts") or query, knesset_num,
            top_k=top_k, committees=committees or None, date_from=date_from, date_to=date_to)
    except Exception as exc:
        return _db_error_envelope(exc, "topics", query=query, knesset_num=knesset_num)
    finally:
        conn.close()

    payload = [{
        "topic_id":   r["id"],
        "text":       r["text"],
        "meeting_id": r["meeting_id"],
        "committee":  r["committee"],
        "date":       r["date"],
        "topic_idx":  r["idx"],
    } for r in rows]
    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False),
        metadata={"kind": "search", "source": "topics", "count": len(payload)},
        provenance={"query": query, "knesset_num": knesset_num, "top_k": top_k},
    )


# ---------------------------------------------------------------------------
# search_opinions — MK-scoped FTS over opinions (verified quotes only)
# ---------------------------------------------------------------------------


def handle_search_opinions(args: dict) -> ToolEnvelope:
    """FTS5 search over opinion + quote text, hard-filtered to one MK (or party).

    Only opinions whose quote was verified verbatim against the transcript are
    returned. An empty query lists the MK's opinions newest first.
    """
    query = (args.get("query") or "").strip()
    mk_id = str(args.get("mk_id") or "").strip()
    party = (args.get("party") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_k = int(args.get("top_k") or config.SEARCH_OPINIONS_DEFAULT_TOP_K)
    top_k = max(1, min(top_k, config.SEARCH_OPINIONS_MAX_TOP_K))

    if not mk_id and not party:
        return _validation_error("missing_mk_id", kind="search", source="opinions",
                                 query=query, mk_id=mk_id, knesset_num=knesset_num)

    conn = _open_db()
    if conn is None:
        return _db_missing_envelope("opinions", knesset_num)
    try:
        rows = store.search_opinions(
            conn, _expand_match(lemmatize(query), "opinions_fts") if query else None, knesset_num,
            top_k=top_k, mk_id=mk_id or None, party=party or None, verified_only=True)
    except Exception as exc:
        return _db_error_envelope(exc, "opinions", query=query, mk_id=mk_id, knesset_num=knesset_num)
    finally:
        conn.close()

    payload = [{
        "opinion_id":    r["id"],
        "speaker":       r["speaker_name"],
        "speaker_label": r["speaker"],
        "mk_id":         r["mk_id"],
        "party":         r["party"],
        "opinion":       r["opinion"],
        "quote":         r["quote"],
        "meeting_id":    r["meeting_id"],
        "committee":     r["committee"],
        "date":          r["date"],
        "speech_idx":    r["speech_idx"],
        "quote_offset":  r["quote_offset"],
    } for r in rows]
    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False),
        metadata={"kind": "search", "source": "opinions", "count": len(payload)},
        provenance={"query": query, "mk_id": mk_id, "party": party,
                    "knesset_num": knesset_num, "top_k": top_k},
    )


# ---------------------------------------------------------------------------
# search_protocols_keyword — BM25 over speech text
# ---------------------------------------------------------------------------


def search_speeches(
    conn,
    query: str,
    *,
    knesset_num: int = 25,
    committees: list | None = None,
    meeting_ids: list | None = None,
    speaker: str | None = None,
    top_k: int = config.SEARCH_PROTOCOLS_DEFAULT_TOP_K,
    sort: str = "relevance",
) -> list[dict]:
    """FTS search over speeches, shared by the agent tool and web.app.browse_rag.

    The SQL speaker filter is a coarse OR over name tokens; rows are then
    refined with name_query_matches so a speech sharing one token with the
    requested name (a different MK called "אורית") is dropped. Rows carry
    id, meeting_id, speech_idx, speaker, mk_id, text, committee, date, score
    (sqlite bm25: lower = more relevant, already sorted).
    """
    normalized = lemmatize(query)
    match_expr = _expand_match(normalized, "speeches_fts") or _quote_match(normalized) or query
    rows = store.search_speeches(
        conn, match_expr, knesset_num,
        top_k=max(top_k, config.KEYWORD_RERANK_TOP_K) if sort == "relevance" else top_k,
        meeting_ids=[str(m) for m in meeting_ids] if meeting_ids else None,
        committees=[str(c).replace("_", " ") for c in committees] if committees else None,
        speaker_tokens=name_tokens(speaker) if speaker else None,
    )
    if speaker:
        rows = [r for r in rows if name_query_matches(speaker, r.get("speaker") or "")]
    return rows


def handle_search_protocols_keyword(args: dict) -> ToolEnvelope:
    """FTS over speeches with optional committee / meeting / speaker / date filters."""
    query = (args.get("query") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_k = int(args.get("top_k") or config.SEARCH_PROTOCOLS_DEFAULT_TOP_K)
    top_k = max(1, min(top_k, config.SEARCH_PROTOCOLS_MAX_TOP_K))
    sort = (args.get("sort") or "relevance").lower()

    if not query:
        return _validation_error("missing_query", kind="search", source="speeches",
                                 query=query, knesset_num=knesset_num)

    committee_ids = args.get("committee_ids") or []
    meeting_ids = args.get("meeting_ids") or []
    speaker = (args.get("speaker") or "").strip()
    date_from = (args.get("date_from") or "").strip()
    date_to = (args.get("date_to") or "").strip()
    filters = {"committee_ids": list(committee_ids), "meeting_ids": list(meeting_ids),
               "speaker": speaker or None, "date_from": date_from or None, "date_to": date_to or None}

    conn = _open_db()
    if conn is None:
        return _db_missing_envelope("speeches", knesset_num)
    try:
        rows = search_speeches(
            conn, query, knesset_num=knesset_num,
            committees=committee_ids, meeting_ids=meeting_ids, speaker=speaker,
            top_k=top_k, sort=sort,
        )
    except Exception as exc:
        return _db_error_envelope(exc, "speeches", query=query, knesset_num=knesset_num, filters=filters)
    finally:
        conn.close()

    if date_from or date_to:
        rows = [r for r in rows if (not date_from or (r.get("date") or "") >= date_from)
                and (not date_to or (r.get("date") or "") <= date_to)]

    payload = [{
        "speech_id":  f"{r['meeting_id']}_{r['speech_idx']}",
        "label":      r.get("speaker") or "",
        "text":       r["text"],
        "meeting_id": r["meeting_id"],
        "committee":  r.get("committee"),
        "date":       r.get("date"),
        "speaker":    r.get("speaker"),
        "mk_id":      r.get("mk_id"),
        "speech_idx": r["speech_idx"],
    } for r in rows[:top_k]]

    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False),
        metadata={"kind": "search", "source": "speeches", "count": len(payload), "total_match": len(rows)},
        provenance={"query": query, "knesset_num": knesset_num, "top_k": top_k, "sort": sort,
                    "filters": filters},
    )


# ---------------------------------------------------------------------------
# Find-* tools — BM25 → candidate records
# ---------------------------------------------------------------------------


def _name_index(target: str, knesset_num: int) -> FuzzyNameIndex | None:
    """In-memory fuzzy index over one of the knesset.db name tables, or None when the db is missing."""
    conn = _open_db()
    if conn is None:
        return None
    try:
        return FuzzyNameIndex(store.name_entries(conn, target, knesset_num))
    finally:
        conn.close()


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
        return _validation_error("missing_query", kind="search", source="mks",
                                 knesset_num=knesset_num)

    fuzzy = _name_index("mks", knesset_num)
    if fuzzy is None:
        return _db_missing_envelope("mks", knesset_num)

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

    metadata: dict = {"kind": "search", "source": "mks", "count": len(payload)}
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
        source="committees",
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
        source="bills",
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
        source="votes",
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

    fuzzy = _name_index(target, knesset_num)
    if fuzzy is None:
        return _db_missing_envelope(target, knesset_num)

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
    """Render a meeting's topics, opinions (grouped by speaker) and attendance from knesset.db.

    ``section`` limits the output to topics | opinions | attendance.
    """
    from summarization.summary_io import render_summary_text

    meeting_id = str(args.get("meeting_id") or "").strip()
    section = (args.get("section") or "").strip().lower() or None
    if not meeting_id:
        return _validation_error("missing_meeting_id", kind="fetch", source="summaries")
    if section not in (None, "topics", "opinions", "attendance"):
        return _validation_error("invalid_section", kind="fetch", source="summaries",
                                 meeting_id=meeting_id, section=section)

    conn = _open_db()
    if conn is None:
        return _db_missing_envelope("summaries", 0)
    try:
        meeting = store.get_meeting(conn, meeting_id)
        if meeting is None or meeting.get("is_protocol") is None:
            return ToolEnvelope(
                summary="",
                full="",
                metadata={"kind": "fetch", "source": "summaries", "count": 0},
                provenance={"meeting_id": meeting_id},
                error="summary_not_found",
            )
        topics = [t["text"] for t in store.get_topics(conn, meeting_id)]
        opinions = store.get_opinions(conn, meeting_id)
        attendance = store.get_attendance(conn, meeting_id)
    except Exception as exc:
        return _db_error_envelope(exc, "summaries", meeting_id=meeting_id)
    finally:
        conn.close()

    text = render_summary_text(topics, opinions, attendance, section=section)
    if not meeting["is_protocol"]:
        text = f"{config.NOT_PROTOCOL}\n\n{text}"
    return ToolEnvelope(
        summary="",
        full=text,
        metadata={"kind": "fetch", "source": "summaries", "count": 1,
                  "topics": len(topics), "opinions": len(opinions), "attendance": len(attendance),
                  "is_protocol": bool(meeting["is_protocol"])},
        provenance={"meeting_id": meeting_id, "section": section,
                    "committee": meeting.get("committee"), "date": meeting.get("date")},
    )


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


def _expand_match(text: str, fts_table: str) -> str:
    """Build an FTS5 MATCH expression with query-side ktiv male/haser expansion.

    Each whitespace token becomes an OR-slot of its corpus spelling variants
    (``("בטחון" OR "ביטחון")``) so a query in one ktiv spelling matches speeches
    written in the other. Slots are AND-ed (space) exactly as ``_quote_match``.
    Falls back to the bare token when it has no extra variants.
    """
    slots: list[str] = []
    for tok in text.split():
        if not tok.strip():
            continue
        safe = [s for s in (_safe_match(v) for v in expand_token(tok, store.db_path(), fts_table)) if s]
        # de-dup while preserving order (safe_match can collapse two variants)
        seen: list[str] = []
        for s in safe:
            if s not in seen:
                seen.append(s)
        if not seen:
            continue
        slots.append(
            f'"{seen[0]}"' if len(seen) == 1
            else "(" + " OR ".join(f'"{v}"' for v in seen) + ")"
        )
    # Join with explicit AND: FTS5 accepts implicit-AND between bare phrases
    # ("a" "b") but NOT between a phrase and a parenthesised OR-group
    # ("a" (...)), which is exactly what expansion produces.
    return " AND ".join(slots)


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
    "search_speeches",
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
