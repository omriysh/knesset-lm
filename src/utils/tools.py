"""Tool implementations + dispatch helpers — agent-agnostic.

This module is the function-bag layer of the tool surface. It owns:

  * the :class:`ToolSpec` dataclass that ``research_agent/tools.py`` uses to
    enumerate the registry,
  * a generic :func:`dispatch` that looks a tool up in any registry and
    invokes its handler with raw kwargs,
  * one ``handle_*`` function per tool.

This module deliberately holds *no* registry — registry construction lives
in the agent-specific module that knows which subset of tools to expose.
Imports flow upward only: ``utils/`` may not import from ``agent/`` (project
CLAUDE.md import convention), so the registry has to live one layer up.

ToolEnvelope contract: every handler returns a
:class:`agent.subgraph.evidence.ToolEnvelope` — never raises, never returns
``None``. Argument-validation failures and infrastructure errors (missing
knesset.db, network exception, etc.) are reported via the envelope's ``error``
field.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Callable

import config
from agent.subgraph.evidence import ToolEnvelope
from retrieval import knesset_db_store as store
from retrieval.ktiv import expand_token
from retrieval.lemmatize import lemmatize
from utils.speech import name_query_matches, name_tokens
from utils.knesset_db import (
    ODATA_PAGE_SIZE,
    _bill_record_to_dict,
    _fetch_members,
    _get_bill_details_by_id,
    _get_bill_text_by_id,
    _sanitize_odata_search,
    get_mk_positions,
    mk_full_name,
    _search_bills_by_term,
    get_party_members,
)
from utils.tool_helpers.adapters import (
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

    ``description`` lives inside ``schema`` (it is part of the JSON schema);
    the dataclass uses ``to_dict`` explicitly — Pydantic is forbidden.
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


def _as_list(value) -> list:
    """Tool args may carry a scalar where the schema asks for an array."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _fts_match(query: str, fts_table: str) -> str:
    """FTS5 MATCH expression for a free-text query: lemmatized, ktiv-expanded, tokens AND-ed."""
    normalized = lemmatize(query)
    return _expand_match(normalized, fts_table) or _quote_match(normalized) or query


# ---------------------------------------------------------------------------
# query_protocols — topics / opinions / speeches over knesset.db
# ---------------------------------------------------------------------------


def handle_query_protocols(args: dict) -> ToolEnvelope:
    """Keyword search (or listing, with an empty query) over the protocol scopes.

    Each requested scope is queried independently with the same query and
    filters; ``full`` is a JSON object with one row list per scope.
    """
    query = (args.get("query") or "").strip()
    search_in = [str(s) for s in _as_list(args.get("search_in"))] or list(store.PROTOCOL_SCOPES)
    mk_id = str(args.get("mk_id") or "").strip() or None
    party = (args.get("party") or "").strip() or None
    committees = [str(c).replace("_", " ") for c in _as_list(args.get("committees"))]
    meeting_ids = [str(m).strip() for m in _as_list(args.get("meeting_ids")) if str(m).strip()]
    date_from = (args.get("date_from") or "").strip() or None
    date_to = (args.get("date_to") or "").strip() or None
    sort = (args.get("sort") or ("relevance" if query else "date")).strip().lower()
    top_k = int(args.get("top_k") or config.QUERY_PROTOCOLS_DEFAULT_TOP_K)
    top_k = max(1, min(top_k, config.QUERY_PROTOCOLS_MAX_TOP_K))
    offset = max(0, int(args.get("offset") or 0))
    knesset_num = int(args.get("knesset_num") or 25)

    provenance = {
        "query": query, "search_in": search_in, "mk_id": mk_id, "party": party,
        "committees": committees, "meeting_ids": meeting_ids, "date_from": date_from,
        "date_to": date_to, "sort": sort, "top_k": top_k, "offset": offset, "knesset_num": knesset_num,
    }
    unknown_scopes = [s for s in search_in if s not in store.PROTOCOL_SCOPES]
    if unknown_scopes:
        return _validation_error("invalid_search_in", kind="search", source="knesset_db",
                                 unknown_scopes=unknown_scopes, **provenance)
    if sort not in ("relevance", "date"):
        return _validation_error("invalid_sort", kind="search", source="knesset_db", **provenance)

    if not store.exists():
        return _db_missing_envelope("protocols", knesset_num)
    results: dict[str, list[dict]] = {}
    conn = None
    try:
        conn = store.connect()
        for scope in search_in:
            results[scope] = store.query_protocol_rows(
                conn, scope, knesset_num,
                match=_fts_match(query, f"{scope}_fts") if query else None,
                mk_id=mk_id, party=party, committees=committees or None,
                meeting_ids=meeting_ids or None, date_from=date_from, date_to=date_to,
                sort=sort, top_k=top_k, offset=offset,
            )
    except Exception as exc:
        return _db_error_envelope(exc, "knesset_db", **provenance)
    finally:
        if conn is not None:
            conn.close()

    return ToolEnvelope(
        summary="",
        full=json.dumps(results, ensure_ascii=False),
        metadata={"kind": "search", "source": "knesset_db",
                  "count": sum(len(rows) for rows in results.values())},
        provenance=provenance,
    )


def search_speeches(
    conn,
    query: str,
    *,
    knesset_num: int = 25,
    committees: list | None = None,
    meeting_ids: list | None = None,
    speaker: str | None = None,
    top_k: int = config.QUERY_PROTOCOLS_DEFAULT_TOP_K,
    sort: str = "relevance",
) -> list[dict]:
    """FTS search over speeches for the web protocol browser.

    The SQL speaker filter is a coarse OR over name tokens; rows are then
    refined with name_query_matches so a speech sharing one token with the
    requested name (a different MK called "אורית") is dropped. Rows carry
    id, meeting_id, speech_idx, speaker, mk_id, text, committee, date, score
    (sqlite bm25: lower = more relevant). sort="date" reorders the top_k
    best matches newest first.
    """
    rows = store.search_speeches(
        conn, _fts_match(query, "speeches_fts"), knesset_num,
        top_k=top_k,
        meeting_ids=[str(m) for m in meeting_ids] if meeting_ids else None,
        committees=[str(c).replace("_", " ") for c in committees] if committees else None,
        speaker_tokens=name_tokens(speaker) if speaker else None,
    )
    if speaker:
        rows = [r for r in rows if name_query_matches(speaker, r.get("speaker") or "")]
    if sort == "date":
        rows.sort(key=lambda r: (r.get("date") or ""), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# get_meeting_attendance
# ---------------------------------------------------------------------------


def handle_get_meeting_attendance(args: dict) -> ToolEnvelope:
    """Attendance list of one meeting: MKs first (with roster party), then guests."""
    meeting_id = str(args.get("meeting_id") or "").strip()
    if not meeting_id:
        return _validation_error("missing_meeting_id", kind="fetch", source="knesset_db")
    if not store.exists():
        return _db_missing_envelope("attendance", 0)
    conn = None
    try:
        conn = store.connect()
        meeting = store.get_meeting(conn, meeting_id)
        attendance = store.get_attendance(conn, meeting_id) if meeting is not None else []
    except Exception as exc:
        return _db_error_envelope(exc, "knesset_db", meeting_id=meeting_id)
    finally:
        if conn is not None:
            conn.close()
    if meeting is None:
        return _validation_error("meeting_not_found", kind="fetch", source="knesset_db",
                                 meeting_id=meeting_id)

    payload = {
        "meeting_id": meeting_id,
        "committee":  meeting.get("committee"),
        "date":       meeting.get("date"),
        "attendance": attendance,
    }
    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False),
        metadata={"kind": "fetch", "source": "knesset_db", "count": len(attendance)},
        provenance={"meeting_id": meeting_id, "committee": meeting.get("committee"),
                    "date": meeting.get("date")},
    )


# ---------------------------------------------------------------------------
# Find-* tools — fuzzy name index → candidate records
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
    """MK identity from the oknesset record plus positions in the given Knesset from OData."""
    mk_id = str(record.get("mk_individual_id") or record.get("PersonID") or "")
    profile = {"mk_id": mk_id, "full_name": mk_full_name(record), "is_current": record.get("IsCurrent", False)}
    try:
        profile.update(get_mk_positions(record.get("PersonID") or mk_id, knesset_num))
    except Exception as exc:
        print(f"[tools] OData positions fetch failed for mk {mk_id}: {exc}")
        profile["positions_error"] = str(exc)
    return profile


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
    """Look up an MK by id by walking the cached oknesset members lists."""
    target = str(mk_id)
    for is_current in (True, False):
        try:
            members = _fetch_members(is_current)
        except Exception as exc:
            print(f"[tools] _fetch_mk_record: members list (is_current={is_current}) failed: {exc}")
            continue
        for mk in members:
            if str(mk.get("mk_individual_id") or "") == target or \
               str(mk.get("PersonID") or "") == target:
                return mk
    return None


# ---------------------------------------------------------------------------
# Bills (live OData)
# ---------------------------------------------------------------------------


def handle_query_bills(args: dict) -> ToolEnvelope:
    """Bill title search (OData ``contains(Name, ...)``) within one Knesset, newest update first."""
    query = (args.get("query") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_k = max(1, min(int(args.get("top_k") or 10), ODATA_PAGE_SIZE))
    provenance = {"query": query, "knesset_num": knesset_num, "top_k": top_k}

    if not query:
        return _validation_error("missing_query", kind="search", source="odata", **provenance)
    try:
        bills = _search_bills_by_term(_sanitize_odata_search(query), knesset_num, top=top_k)
    except Exception as exc:
        return _odata_error_envelope(exc, "search", **provenance)

    payload = [_bill_record_to_dict(b) for b in bills]
    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False, default=str),
        metadata={"kind": "search", "source": "odata", "count": len(payload)},
        provenance=provenance,
    )


def handle_get_bill(args: dict) -> ToolEnvelope:
    """Bill metadata (status, initiators, documents); ``include_text`` adds the extracted bill text."""
    bill_id = str(args.get("bill_id") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    include_text = bool(args.get("include_text"))
    max_chars = int(args.get("max_chars") or config.BILL_TEXT_DEFAULT_MAX_CHARS)
    max_chars = max(config.BILL_TEXT_MIN_MAX_CHARS, min(max_chars, config.BILL_TEXT_MAX_MAX_CHARS))
    provenance = {"bill_id": bill_id, "knesset_num": knesset_num,
                  "include_text": include_text, "max_chars": max_chars}

    if not bill_id:
        return _validation_error("missing_bill_id", kind="fetch", source="odata", **provenance)
    if not bill_id.isdigit():
        return _validation_error("invalid_bill_id", kind="fetch", source="odata", **provenance)

    text_record = None
    try:
        record = _get_bill_details_by_id(int(bill_id))
        if record is not None and include_text:
            text_record = _get_bill_text_by_id(int(bill_id), max_chars=max_chars)
    except Exception as exc:
        return _odata_error_envelope(exc, "fetch", **provenance)
    if record is None:
        return _validation_error("bill_not_found", kind="fetch", source="odata", **provenance)

    text_truncated = bool(text_record and text_record.get("truncated"))
    if include_text:
        record["text"] = text_record["text"] if text_record else None
        record["text_truncated"] = text_truncated
    metadata: dict = {"kind": "fetch", "source": "odata", "count": 1}
    if include_text and text_record is None:
        metadata["warnings"] = ["bill_text_not_found"]
    elif text_truncated:
        metadata["warnings"] = [f"result_truncated_to_{max_chars}_chars"]
    return ToolEnvelope(
        summary="",
        full=json.dumps(record, ensure_ascii=False, default=str),
        metadata=metadata,
        provenance=provenance,
        truncated=text_truncated,
    )


def _odata_error_envelope(exc: Exception, kind: str, **prov) -> ToolEnvelope:
    print(f"[tools] OData request failed: {exc}")
    return ToolEnvelope(
        summary="",
        full="",
        metadata={"kind": "error", "source": "odata", "count": 0, "exception": str(exc)},
        provenance=prov,
        error="odata_request_failed",
    )


# ---------------------------------------------------------------------------
# Votes (live OData)
# ---------------------------------------------------------------------------


def handle_query_votes(args: dict) -> ToolEnvelope:
    """Plenum votes — behaviour determined by which params are supplied:
      query + mk_id → how that MK voted on matching votes
      mk_id only    → recent votes cast by the MK
      query only    → votes matching the keyword
      neither       → most recent votes overall
    """
    query = (args.get("query") or "").strip()
    mk_id = str(args.get("mk_id") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_k = max(1, int(args.get("top_k") or 20))

    if mk_id:
        record = _fetch_mk_record(mk_id)
        if record is None:
            return _validation_error(
                "mk_not_found", kind="fetch", source="odata",
                mk_id=mk_id, knesset_num=knesset_num,
            )
        name = mk_full_name(record)
        if query:
            return adapt_get_votes_on_topic_by_mk(
                topic=query, name=name, knesset_num=knesset_num, top_n=top_k,
            )
        return adapt_get_mk_votes(name=name, knesset_num=knesset_num, top_n=top_k)

    if query:
        return adapt_get_votes_on_topic(topic=query, top_n=top_k)

    return adapt_get_recent_votes(top_n=top_k, knesset_num=knesset_num)


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
    (``("בטחון" OR "ביטחון")``) so a query in one ktiv spelling matches text
    written in the other. Slots are AND-ed. Falls back to the bare token when
    it has no extra variants.
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


__all__ = [
    "ToolSpec",
    "ToolRegistry",
    "dispatch",
    "search_speeches",
    "handle_query_protocols",
    "handle_get_meeting_attendance",
    "handle_find_mk",
    "handle_find_committee",
    "handle_find_party",
    "handle_query_bills",
    "handle_get_bill",
    "handle_query_votes",
]
