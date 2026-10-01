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
import sqlite3
import sys
import traceback
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

import config
from agent.subgraph.evidence import ToolEnvelope
from api.validation import ApiInputError
from retrieval import knesset_db_store as store
from retrieval.ktiv import expand_token, prefixed_variants, stripped_prefix_bases
from retrieval.lemmatize import lemmatize
from utils.speech import name_query_matches, name_tokens
from utils.knesset_db import (
    ODATA_PAGE_SIZE,
    _bill_record_to_dict,
    _fetch_members,
    _get_bill_details_by_id,
    _get_bill_text_by_id,
    _sanitize_odata_search,
    get_all_committees,
    get_mk_positions,
    get_person_by_id,
    mk_full_name,
    mk_roster_rows,
    search_bills_page,
    get_all_parties,
    get_party_members,
)
from utils.tool_helpers.adapters import (
    adapt_query_votes,
    fetch_committee_record,
    paging_metadata,
)
from utils.tool_helpers.filter_diagnostics import (
    diagnose_empty_protocol_query,
    is_latin_only_query,
    unknown_party_message,
    unresolved_mk_name_diagnostic,
)
from utils.tool_helpers.filter_resolution import (
    filter_vocabulary,
    normalized_name_key,
    resolve_committee,
    resolve_mk_name,
    resolve_party,
)
from utils.tool_helpers.char_paging import char_budget_page
from utils.source_links import protocol_row_url, protocol_url
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
    validate_args: Callable[[dict], dict] | None = None

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


def dispatch(registry: ToolRegistry, name: str, args: dict, args_already_validated: bool = False) -> ToolEnvelope:
    """Look up a tool by name in ``registry`` and invoke its handler.

    Never raises. Unknown names → ``error="unknown_tool"``. Arguments the
    spec's ``validate_args`` refuses (ApiInputError) → its ``error_code``
    (e.g. ``invalid_offset``) with the message as summary; callers that
    validated with their own limits (the /v1 routes, MCP) pass
    ``args_already_validated``. Handler or validator exceptions →
    ``error="dispatch_exception"`` with the traceback in metadata.
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
    try:
        args_preview = _printable(json.dumps(args_safe, ensure_ascii=False)[:300])
        print(f"[tools] → {name}  args={args_preview}", flush=True)
        if spec.validate_args is not None and not args_already_validated:
            try:
                args = spec.validate_args(args or {})
            except ApiInputError as exc:
                return _invalid_args_envelope(name, args_safe, exc)
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
    return result


def _printable(text: str) -> str:
    """Lone surrogates (from LLM or visitor JSON) become backslash escapes, so printing never raises."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _invalid_args_envelope(name: str, args_safe: dict, exc: ApiInputError) -> ToolEnvelope:
    print(f"[tools] ✗ {name} refused arguments: {exc.error_code}: {exc}", flush=True)
    return ToolEnvelope(
        summary=f"{name}: invalid arguments: {exc.message}",
        full="",
        metadata={"kind": "error", "source": "dispatch", "count": 0, "message": exc.message},
        provenance={"tool_name": name, "args": args_safe},
        error=exc.error_code,
    )


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


def _connect_for_query():
    return store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)


def _open_db():
    """Open knesset.db (built offline by scripts/build_knesset_db.py) or None when missing."""
    if not store.exists():
        return None
    return _connect_for_query()


def _db_missing_envelope(target: str, knesset_num: int) -> ToolEnvelope:
    print(f"[tools] {store.db_path()} missing; {target} lookup skipped", flush=True)
    return ToolEnvelope(
        summary="",
        full="",
        metadata={"kind": "error", "source": "knesset_db", "count": 0, "target": target},
        provenance={"target": target, "knesset_num": knesset_num},
        error="knesset_db_missing",
    )


def _db_error_envelope(exc: Exception, source: str, conn=None, **prov) -> ToolEnvelope:
    print(f"[tools] {source} query failed: {exc}")
    if isinstance(exc, sqlite3.OperationalError) and store.deadline_passed(conn):
        return ToolEnvelope(
            summary="",
            full="",
            metadata={"kind": "error", "source": source, "count": 0, "exception": str(exc)},
            provenance=prov,
            error="query_timeout",
        )
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
    """FTS5 MATCH expression for a free-text query: lemmatized, ktiv-expanded, tokens AND-ed.
    Empty when no token survives metacharacter stripping (the raw query never reaches MATCH)."""
    normalized = lemmatize(query)
    return _expand_match(normalized, fts_table) or _quote_match(normalized)


def _fts_exact_match(query: str) -> str:
    """FTS5 MATCH expression for the query words as typed (after punctuation cleanup), without
    spelling or prefix variants: ranks rows that match it above rows found only through variants."""
    return _quote_match(lemmatize(query))


# ---------------------------------------------------------------------------
# query_protocols — topics / opinions / speeches over knesset.db
# ---------------------------------------------------------------------------


def handle_query_protocols(args: dict) -> ToolEnvelope:
    """Keyword search (or listing, with an empty query) over the protocol scopes.

    Each requested scope is queried independently with the same query and
    filters; ``full`` is a JSON object with one row list per scope.
    With ``page_chars`` (the public API) a scope's page is whole rows from row ``offset`` up to about
    page_chars characters (utils.tool_helpers.char_paging) instead of top_k rows, and
    metadata["next_offsets"] holds, per scope with more rows, the row offset of its next page.
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
    page_chars = int(args.get("page_chars") or 0)

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
    requested_filters = dict(provenance)
    results: dict[str, list[dict]] = {}
    next_offsets: dict[str, int] = {}
    diagnostics: list[dict] = []
    conn = None
    try:
        conn = _connect_for_query()
        filters = _resolve_protocol_filters(conn, knesset_num, mk_id, party, committees)
        if filters.unresolved_mk_name is not None:
            return _unresolved_mk_name_envelope(filters, provenance)
        provenance.update(mk_id=filters.mk_id, party=filters.party, committees=filters.committees)
        for scope in search_in:
            match = _fts_match(query, f"{scope}_fts") if query else None
            if (query and not match) or not filters.resolved:
                results[scope] = []
                continue
            row_filters = dict(
                match=match, exact_match=_fts_exact_match(query) if query else None,
                mk_id=filters.mk_id, party=filters.party, committees=filters.committees or None,
                meeting_ids=meeting_ids or None, date_from=date_from, date_to=date_to, sort=sort,
            )
            if page_chars:
                ranked_rows = store.iter_protocol_rows(conn, scope, knesset_num, **row_filters, offset=offset)
                try:
                    page_rows, more_rows_follow = char_budget_page(ranked_rows, page_chars)
                finally:
                    ranked_rows.close()
                results[scope] = [_public_protocol_row(scope, row) for row in page_rows]
                if more_rows_follow:
                    next_offsets[scope] = offset + len(results[scope])
            else:
                results[scope] = store.query_protocol_rows(conn, scope, knesset_num, **row_filters,
                                                           top_k=top_k, offset=offset)
        if not any(results.values()):
            diagnostics = diagnose_empty_protocol_query(conn, requested_filters)
    except Exception as exc:
        return _db_error_envelope(exc, "knesset_db", conn, **provenance)
    finally:
        if conn is not None:
            conn.close()

    metadata: dict = {"kind": "search", "source": "knesset_db",
                      "count": sum(len(rows) for rows in results.values())}
    if next_offsets:
        metadata["next_offsets"] = next_offsets
    if filters.warnings:
        metadata["warnings"] = filters.warnings
    if diagnostics:
        metadata["diagnostics"] = diagnostics
    return ToolEnvelope(
        summary=" ".join(d["message"] for d in diagnostics),
        full=json.dumps(results, ensure_ascii=False),
        metadata=metadata,
        provenance=provenance,
    )


_PUBLIC_ROW_HIDDEN_FIELDS = ("quote_offset", "quote_length")


def _public_protocol_row(scope: str, row: dict) -> dict:
    """The row with a link to its source in the reading tab instead of the raw quote location."""
    public_row = {key: value for key, value in row.items() if key not in _PUBLIC_ROW_HIDDEN_FIELDS}
    public_row["url"] = protocol_row_url(scope, row)
    return public_row


@dataclass
class _ProtocolFilters:
    mk_id: str | None
    party: str | None
    committees: list[str]
    resolved: bool = True
    warnings: list[str] = field(default_factory=list)
    unresolved_mk_name: dict | None = None
    mk_name_candidates: list[dict] = field(default_factory=list)


def _resolve_protocol_filters(conn, knesset_num: int, mk_id: str | None, party: str | None,
                              committees: list[str]) -> _ProtocolFilters:
    """mk_id given as a name, party aliases and committee names / ids → the exact DB values.
    An unresolvable party or committee list leaves resolved=False (no guess, 0 rows + diagnostics)."""
    vocabulary = filter_vocabulary(conn, knesset_num)
    filters = _ProtocolFilters(mk_id=mk_id, party=party, committees=[])

    if mk_id and not mk_id.isdigit():
        mk_resolution = resolve_mk_name(mk_id, vocabulary)
        if mk_resolution.mk_id is None:
            filters.unresolved_mk_name = unresolved_mk_name_diagnostic(mk_id, knesset_num, mk_resolution.candidates)
            filters.mk_name_candidates = mk_resolution.candidates
            return filters
        filters.mk_id = mk_resolution.mk_id
        filters.warnings.append(f'mk_id "{mk_id}" → {mk_resolution.mk_id} ({mk_resolution.full_name})')

    if party:
        party_resolution = resolve_party(party, list(vocabulary.party_member_counts))
        if party_resolution.party is None:
            filters.resolved = False
        elif party_resolution.party != party:
            filters.party = party_resolution.party
            filters.warnings.append(f'party "{party}" → "{party_resolution.party}"')

    for committee in committees:
        committee_resolution = resolve_committee(committee, vocabulary)
        if not committee_resolution.db_names:
            filters.warnings.append(f'committee "{committee}" matches no committee with meetings')
            continue
        filters.committees += [n for n in committee_resolution.db_names if n not in filters.committees]
        if committee_resolution.method not in ("exact", "normalized"):
            filters.warnings.append(f'committee "{committee}" → "{committee_resolution.db_names[0].strip()}"')
    if committees and not filters.committees:
        filters.resolved = False
    return filters


def _unresolved_mk_name_envelope(filters: _ProtocolFilters, provenance: dict) -> ToolEnvelope:
    return ToolEnvelope(
        summary=filters.unresolved_mk_name["message"],
        full="",
        metadata={"kind": "search", "source": "knesset_db", "count": 0, "candidates": filters.mk_name_candidates,
                  "diagnostics": [filters.unresolved_mk_name]},
        provenance=provenance,
        error="mk_id_not_resolved",
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
    match = _fts_match(query, "speeches_fts")
    if not match:
        return []
    rows = store.search_speeches(
        conn, match, knesset_num,
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
        conn = _connect_for_query()
        meeting = store.get_meeting(conn, meeting_id)
        attendance = store.get_attendance(conn, meeting_id) if meeting is not None else []
    except Exception as exc:
        return _db_error_envelope(exc, "knesset_db", conn, meeting_id=meeting_id)
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
        "url":        protocol_url(meeting_id),
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
    """In-memory fuzzy index over one of the knesset.db name tables, or None when the db is missing.
    MKs and committees of a Knesset whose protocols are not processed come from the live Knesset lists."""
    if target == "committees" and knesset_num not in config.PROTOCOL_KNESSET_NUMS:
        return FuzzyNameIndex([{"id": str(committee["CommitteeID"]), "label": committee["Name"], "body": committee["Name"],
                                "extra": {"committee_id": str(committee["CommitteeID"]), "knesset_num": knesset_num,
                                          "is_current": committee["IsCurrent"]}}
                               for committee in get_all_committees(knesset_num)])
    if target == "mks" and knesset_num not in config.PROTOCOL_KNESSET_NUMS:
        return FuzzyNameIndex([{"id": row["mk_id"], "label": row["full_name"], "body": row["aliases"],
                                "extra": {"mk_id": row["mk_id"], "full_name": row["full_name"], "party": row["party"]}}
                               for row in mk_roster_rows(knesset_num)], require_query_token_coverage=True)
    conn = _open_db()
    if conn is None:
        return None
    try:
        return FuzzyNameIndex(store.name_entries(conn, target, knesset_num),
                              require_query_token_coverage=target == "mks")
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
        profile["positions_error"] = "positions unavailable: the Knesset OData request failed"
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
    hints = _find_mk_hints(query, knesset_num, payload)
    if hints:
        metadata["hints"] = hints

    return ToolEnvelope(
        summary=" ".join(hints),
        full=json.dumps(payload, ensure_ascii=False),
        metadata=metadata,
        provenance={"query": query, "knesset_num": knesset_num, "top_k": top_k},
    )


def _find_mk_hints(query: str, knesset_num: int, payload: list[dict]) -> list[str]:
    if query.isdigit():
        return [f"'{query}' looks like an mk_id; find_mk searches MK names. Pass the number as the mk_id "
                f"filter of query_protocols, or search by name here."]
    hints: list[str] = []
    if is_latin_only_query(query):
        hints.append(f"'{query}' has no Hebrew letters; MK names are stored in Hebrew, so search with the Hebrew name.")
    if not payload or payload[0]["score"] < config.FIND_MK_CONFIDENT_SCORE:
        closest = ", ".join(f"{c['full_name']} ({c['score']:.2f})" for c in payload[:3])
        weak_note = f"; the candidates below share only part of the name ({closest})" if closest else ""
        hints.append(f"No MK of Knesset {knesset_num} named '{query}'{weak_note}. The DB covers the MKs who "
                     f"served in Knesset {knesset_num} only; the person may not be an MK of that Knesset.")
    return hints


def handle_find_committee(args: dict) -> ToolEnvelope:
    """Fuzzy committee lookup; an empty query lists the committees that have meetings, with meeting counts."""
    if not (args.get("query") or "").strip():
        return _list_committees_with_meetings(int(args.get("knesset_num") or 25))
    knesset_num = int(args.get("knesset_num") or 25)
    return _generic_find(
        args,
        target="committees",
        kind="search",
        source="committees",
        id_key="committee_id",
        label_key="name",
        fetch_record=lambda committee_id: fetch_committee_record(committee_id, knesset_num=knesset_num),
        default_top_k=5,
    )


def handle_find_party(args: dict) -> ToolEnvelope:
    """Match a party name (aliases such as ש"ס / Likud included) and return its members for a given
    Knesset; an empty query lists every party with its member count."""
    query       = (args.get("query") or "").strip()
    knesset_num = int(args.get("knesset_num") or 25)
    top_k       = int(args.get("top_k") or 3)

    if not query:
        parties = get_all_parties(knesset_num)
        return ToolEnvelope(
            summary=f"{len(parties)} parties of Knesset {knesset_num}.",
            full=json.dumps(parties, ensure_ascii=False),
            metadata={"kind": "search", "source": "parties", "count": len(parties)},
            provenance={"query": query, "knesset_num": knesset_num},
        )

    results = get_party_members(party_query=query, knesset_num=knesset_num, top_k=top_k)

    if not results:
        hint = unknown_party_message(query, knesset_num)
        return ToolEnvelope(
            summary=hint,
            full="[]",
            metadata={"kind": "search", "source": "parties", "count": 0, "hints": [hint]},
            provenance={"query": query, "knesset_num": knesset_num},
        )

    summary_parts = [f"{r['party']} ({r['mk_count']} ח\"כ)" for r in results]
    return ToolEnvelope(
        summary=f"מפלגות: {', '.join(summary_parts)}",
        full=json.dumps(results, ensure_ascii=False),
        metadata={"kind": "search", "source": "parties", "count": len(results)},
        provenance={"query": query, "knesset_num": knesset_num},
    )


def _list_committees_with_meetings(knesset_num: int) -> ToolEnvelope:
    conn = _open_db()
    if conn is None:
        return _db_missing_envelope("committees", knesset_num)
    try:
        vocabulary = filter_vocabulary(conn, knesset_num)
    except Exception as exc:
        return _db_error_envelope(exc, "committees", conn, query="", knesset_num=knesset_num)
    finally:
        conn.close()
    committee_id_by_key = {normalized_name_key(name): committee_id
                           for committee_id, name in vocabulary.committee_name_by_id.items()}
    committees = []
    for key, db_names in vocabulary.committee_names_by_key.items():
        meeting_count = sum(vocabulary.committee_meeting_counts.get(name, 0) for name in db_names)
        if meeting_count:
            committees.append({"committee_id": committee_id_by_key.get(key), "name": key,
                               "meeting_count": meeting_count})
    committees.sort(key=lambda c: c["meeting_count"], reverse=True)
    return ToolEnvelope(
        summary=f"{len(committees)} committees of Knesset {knesset_num} have meetings in the DB.",
        full=json.dumps(committees, ensure_ascii=False),
        metadata={"kind": "search", "source": "committees", "count": len(committees)},
        provenance={"query": "", "knesset_num": knesset_num},
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


def _optional_knesset_num(args: dict) -> int | None:
    requested = args.get("knesset_num")
    return None if requested in (None, "") else int(requested)


def handle_query_bills(args: dict) -> ToolEnvelope:
    """Bill title search (OData ``contains(Name, ...)``), newest update first, paged by offset/top_k;
    knesset_num None = every Knesset."""
    query = (args.get("query") or "").strip()
    knesset_num = _optional_knesset_num(args)
    top_k = max(1, min(int(args.get("top_k") or 10), ODATA_PAGE_SIZE))
    offset = max(0, int(args.get("offset") or 0))
    provenance = {"query": query, "knesset_num": knesset_num, "top_k": top_k, "offset": offset}

    if not query:
        return _validation_error("missing_query", kind="search", source="odata", **provenance)
    try:
        bills, total = search_bills_page(_sanitize_odata_search(query), knesset_num, offset, top_k)
    except Exception as exc:
        return _odata_error_envelope(exc, "search", **provenance)

    payload = [_bill_record_to_dict(b) for b in bills]
    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False, default=str),
        metadata={"kind": "search", "source": "odata", "count": len(payload),
                  "paging": paging_metadata(offset=offset, returned=len(payload), page_size=top_k, total=total)},
        provenance=provenance,
    )


def handle_get_bill(args: dict) -> ToolEnvelope:
    """Bill metadata (status, initiators, documents); ``include_text`` adds the extracted bill text.
    Bill ids are unique across Knessets, so knesset_num is not needed."""
    bill_id = str(args.get("bill_id") or "").strip()
    include_text = bool(args.get("include_text"))
    max_chars = int(args.get("max_chars") or config.BILL_TEXT_DEFAULT_MAX_CHARS)
    max_chars = max(config.BILL_TEXT_MIN_MAX_CHARS, min(max_chars, config.BILL_TEXT_MAX_MAX_CHARS))
    text_offset = max(0, int(args.get("offset") or 0))
    provenance = {"bill_id": bill_id, "include_text": include_text, "max_chars": max_chars, "offset": text_offset}

    if not bill_id:
        return _validation_error("missing_bill_id", kind="fetch", source="odata", **provenance)
    if not (bill_id.isascii() and bill_id.isdigit()):
        return _validation_error("invalid_bill_id", kind="fetch", source="odata", **provenance)

    text_record = None
    try:
        record = _get_bill_details_by_id(int(bill_id))
        if record is not None and include_text:
            text_record = _get_bill_text_by_id(int(bill_id), max_chars=max_chars, text_offset=text_offset)
    except Exception as exc:
        return _odata_error_envelope(exc, "fetch", **provenance)
    if record is None:
        return _validation_error("bill_not_found", kind="fetch", source="odata", **provenance)

    text_continues = bool(text_record and text_record.get("truncated"))
    if include_text:
        record["text"] = text_record["text"] if text_record else None
        record["text_chars"] = text_record.get("text_chars") if text_record else None
    metadata: dict = {"kind": "fetch", "source": "odata", "count": 1}
    if include_text and text_record is None:
        metadata["warnings"] = ["bill_text_not_found"]
    elif text_continues:
        metadata["next_offset"] = text_record["text_chars"][1]
    return ToolEnvelope(
        summary="",
        full=json.dumps(record, ensure_ascii=False, default=str),
        metadata=metadata,
        provenance=provenance,
        truncated=text_continues,
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


def _vote_person_id(mk_id: str) -> int | None:
    """KNS_Person id behind an mk_id (oknesset mk_individual_id or PersonID); None when unknown."""
    record = _fetch_mk_record(mk_id)
    if record is not None and record.get("PersonID"):
        return int(record["PersonID"])
    if mk_id.isascii() and mk_id.isdigit() and get_person_by_id(int(mk_id)) is not None:
        return int(mk_id)
    return None


def handle_query_votes(args: dict) -> ToolEnvelope:
    """Plenum votes, newest first, paged by offset/top_k; knesset_num None = every Knesset.
      query + mk_id → how that MK voted on matching votes
      mk_id only    → votes cast by the MK
      query only    → votes matching the keyword
      neither       → most recent votes overall
    """
    query = (args.get("query") or "").strip()
    mk_id = str(args.get("mk_id") or "").strip()
    knesset_num = _optional_knesset_num(args)
    top_k = max(1, int(args.get("top_k") or 20))
    offset = max(0, int(args.get("offset") or 0))
    provenance = {"query": query, "mk_id": mk_id, "knesset_num": knesset_num, "top_k": top_k, "offset": offset}

    person_id = None
    if mk_id:
        try:
            person_id = _vote_person_id(mk_id)
        except Exception as exc:
            return _odata_error_envelope(exc, "fetch", **provenance)
        if person_id is None:
            return _validation_error("mk_not_found", kind="fetch", source="odata", **provenance)

    envelope = adapt_query_votes(topic=query, person_id=person_id, knesset_num=knesset_num,
                                 offset=offset, page_size=top_k)
    envelope.provenance = {**(envelope.provenance or {}), **provenance}
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


def _fts_token_parts(text: str) -> list[str]:
    """The pieces the unicode61 tokenizer indexes: letters, digits and marks. Every other
    character (FTS5 metacharacters, gershayim, hyphen, commas) separates tokens, so
    צה"ל -> ["צה", "ל"], matching how the protocol text was indexed."""
    token_chars = [
        ch if unicodedata.category(ch)[0] in "LNM" or unicodedata.category(ch) == "Co" else " "
        for ch in text
    ]
    return "".join(token_chars).split()


def _safe_match(text: str) -> str:
    """Text with every token separator turned into a single space; empty when no token is left."""
    return " ".join(_fts_token_parts(text))


def _quote_match(text: str) -> str:
    """Each whitespace-separated token as an FTS5 phrase (צה"ל -> "צה ל")."""
    phrases = [_safe_match(tok) for tok in text.split()]
    return " ".join(f'"{phrase}"' for phrase in phrases if phrase)


def _fts_token_variants(token_parts: list[str], fts_table: str) -> list[str]:
    """FTS5 phrases one query token may appear as: its ktiv spellings plus their indexed
    Hebrew-prefixed forms (יוקר -> ביוקר, היוקר). A token that already carries a prefix
    also gets its common bare base and the base's forms (ביוקר -> יוקר, ליוקר). A multi-part
    token (צה ל) is not ktiv-expanded or stripped; the prefix goes on its first part (בצה ל)."""
    db_path = store.db_path()
    first_part, remaining_parts = token_parts[0], token_parts[1:]
    spellings = [first_part] if remaining_parts else expand_token(first_part, db_path, fts_table)
    extra_forms: list[str] = []
    if remaining_parts or len(first_part) >= config.FTS_MIN_PREFIXED_WORD_CHARS:
        base_spellings = [] if remaining_parts else [
            base_spelling
            for base in stripped_prefix_bases(first_part, db_path, fts_table)
            for base_spelling in expand_token(base, db_path, fts_table)
        ]
        extra_forms.extend(base_spellings)
        for spelling in base_spellings + spellings:
            extra_forms.extend(prefixed_variants(spelling, db_path, fts_table))
        extra_forms = [form for form in dict.fromkeys(extra_forms) if form not in spellings]
        extra_forms = extra_forms[:config.FTS_MAX_PREFIXED_VARIANTS_PER_WORD]
    phrase_tail = "".join(" " + part for part in remaining_parts)
    return list(dict.fromkeys(head + phrase_tail for head in spellings + extra_forms))


def _expand_match(text: str, fts_table: str) -> str:
    """Build an FTS5 MATCH expression with query-side ktiv and Hebrew-prefix expansion.

    Each whitespace token becomes an OR-slot of its corpus variants
    (``("בטחון" OR "ביטחון" OR "הביטחון" ...)``) so a query matches text written
    in the other ktiv spelling or behind a prefix letter. Slots are AND-ed.
    """
    slots: list[str] = []
    for tok in text.split():
        token_parts = _fts_token_parts(tok)
        if not token_parts:
            continue
        variants = _fts_token_variants(token_parts, fts_table)
        slots.append(
            f'"{variants[0]}"' if len(variants) == 1
            else "(" + " OR ".join(f'"{v}"' for v in variants) + ")"
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
