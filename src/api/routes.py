"""
Public read-only HTTP surface over the research tools (RESEARCH_TOOL_REGISTRY).

Every /v1 route maps query-string params to tool args, runs the same `dispatch` the research
agent uses, and unwraps the ToolEnvelope. No query logic lives here: only API-side limits
(input validation in api.validation, smaller top_k, response size cap), hints for the calling
agent, and error → status mapping. 5xx bodies carry a generic message; the real exception is
printed server-side and logged to errors.log with the request id.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse

import config
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from api import validation as valid
from api.markdown import render_markdown
from api.request_log import current_request_id, log_server_error
from retrieval import knesset_db_store as store
from utils.tools import dispatch

router = APIRouter()

INSTRUCTIONS_PATH = Path(__file__).parent / "instructions.md"

TOOL_ENDPOINTS = {
    "find_mk":                "/v1/mks",
    "find_committee":         "/v1/committees",
    "find_party":             "/v1/parties",
    "query_protocols":        "/v1/protocols",
    "get_meeting_attendance": "/v1/meetings/{meeting_id}/attendance",
    "query_bills":            "/v1/bills",
    "get_bill":               "/v1/bills/{bill_id}",
    "query_votes":            "/v1/votes",
}

_STATUS_BY_ERROR = {
    "knesset_db_missing":    503,
    "odata_request_failed":  502,
    "db_search_failed":      500,
    "dispatch_exception":    500,
    "unknown_tool":          500,
    "adapter_exception":     502,
    "no_votes_found":        404,
    "query_timeout":         503,
}

_PUBLIC_MESSAGES_BY_ERROR = {
    "query_timeout": "the query took too long: use more specific key words or add a filter "
                     "(committee, meeting_id, mk_id, party, dates)",
}

_PUBLIC_5XX_MESSAGES = {
    502: "the upstream Knesset API request failed; retry later",
    503: "the database is temporarily unavailable",
}

_INTERNAL_PROVENANCE_KEYS = {"expected_path", "traceback", "exception", "args"}


def _error_status(error_code: str) -> int:
    if error_code in _STATUS_BY_ERROR:
        return _STATUS_BY_ERROR[error_code]
    if error_code.endswith("_not_found"):
        return 404
    if error_code.startswith(("missing_", "invalid_")):
        return 400
    return 500


def _clamp(value: int | None, maximum: int) -> int | None:
    return None if value is None else max(1, min(value, maximum))


def _drop_last_row(results) -> bool:
    """Remove the last row of the largest row list in results; False when nothing is left to drop."""
    if isinstance(results, list):
        if not results:
            return False
        results.pop()
        return True
    if isinstance(results, dict):
        lists = [v for v in results.values() if isinstance(v, list) and v]
        if not lists:
            return False
        max(lists, key=lambda rows: len(json.dumps(rows, ensure_ascii=False))).pop()
        return True
    return False


def _fit_to_size(results) -> bool:
    """Trim trailing rows until results serialize under API_MAX_RESPONSE_CHARS; True when trimmed."""
    trimmed = False
    while len(json.dumps(results, ensure_ascii=False)) > config.API_MAX_RESPONSE_CHARS:
        if not _drop_last_row(results):
            break
        trimmed = True
    return trimmed


def _hint(tool: str, args: dict, results, trimmed: bool) -> str:
    if tool == "query_protocols":
        offset = int(args.get("offset") or 0)
        parts = []
        for scope, rows in results.items():
            if trimmed or len(rows) >= int(args.get("top_k") or 0):
                if rows:
                    parts.append(f"{scope}: more rows may exist, repeat with offset={offset + len(rows)}")
        if all(not rows for rows in results.values()):
            parts.append("no rows: try fewer or different key words, a spelling variant, or drop a filter")
        if trimmed:
            parts.append("response trimmed to the size limit: lower top_k or narrow search_in")
        return "; ".join(parts)
    if isinstance(results, list):
        if not results:
            return "no results: try a shorter query or a different spelling"
        if trimmed:
            return "response trimmed to the size limit: lower top_k"
        top_k = args.get("top_k")
        if top_k and len(results) >= int(top_k):
            return "more results may exist: raise top_k"
    if trimmed:
        return "response trimmed to the size limit"
    return ""


def _error_status_and_body(tool: str, envelope, provenance: dict) -> tuple[int, dict]:
    status = _error_status(envelope.error)
    if status >= 500:
        metadata = envelope.metadata or {}
        print(f"[api] {tool} → {status} {envelope.error}: {metadata.get('exception')}")
        log_server_error(current_request_id.get(), f"api {tool} → {status} {envelope.error}: {metadata.get('exception')}",
                         metadata.get("traceback") or "")
        message = _PUBLIC_MESSAGES_BY_ERROR.get(envelope.error) or _PUBLIC_5XX_MESSAGES.get(status, "internal error")
    else:
        message = envelope.error.replace("_", " ")
    return status, {"error_code": envelope.error, "message": message, "tool": tool, "args": provenance}


# ── tool arguments: one set of limits for the /v1 routes and the MCP tools ──

def _knesset_num_arg(args: dict) -> int:
    requested = valid.as_int(args.get("knesset_num"), "knesset_num")
    return valid.knesset_num(DEFAULT_KNESSET_NUM if requested is None else requested)


def _top_k_arg(args: dict, maximum: int, default: int | None = None) -> int | None:
    return _clamp(valid.as_int(args.get("top_k"), "top_k") or default, maximum)


def _find_args(args: dict) -> dict:
    return {"query": valid.search_text(valid.as_text(args.get("query"), "query")),
            "knesset_num": _knesset_num_arg(args), "top_k": _top_k_arg(args, config.API_FIND_MAX_TOP_K)}


def _query_protocols_args(args: dict) -> dict:
    scopes = valid.as_text_list(args.get("search_in"), "search_in", split_commas=True)
    committees = [valid.name_filter(c, "committee")
                  for c in valid.as_text_list(args.get("committees"), "committee", split_commas=False)]
    meeting_ids = [valid.numeric_id(m, "meeting_id")
                   for m in valid.as_text_list(args.get("meeting_ids"), "meeting_id", split_commas=True)]
    return {
        "query":       valid.keyword_query(valid.as_text(args.get("query"), "query")),
        "search_in":   list(dict.fromkeys(scopes)) or list(config.API_PROTOCOLS_DEFAULT_SCOPES),
        "mk_id":       valid.numeric_id(valid.as_text(args.get("mk_id"), "mk_id"), "mk_id"),
        "party":       valid.name_filter(valid.as_text(args.get("party"), "party"), "party"),
        "committees":  [c for c in committees if c] or None,
        "meeting_ids": meeting_ids or None,
        "date_from":   valid.iso_date(valid.as_text(args.get("date_from"), "date_from"), "date_from"),
        "date_to":     valid.iso_date(valid.as_text(args.get("date_to"), "date_to"), "date_to"),
        "sort":        valid.name_filter(valid.as_text(args.get("sort"), "sort"), "sort"),
        "top_k":       _top_k_arg(args, config.API_PROTOCOLS_MAX_TOP_K, config.API_PROTOCOLS_DEFAULT_TOP_K),
        "offset":      valid.offset(valid.as_int(args.get("offset"), "offset") or 0),
        "knesset_num": _knesset_num_arg(args),
    }


def _meeting_attendance_args(args: dict) -> dict:
    return {"meeting_id": valid.numeric_id(valid.as_text(args.get("meeting_id"), "meeting_id"), "meeting_id")}


def _query_bills_args(args: dict) -> dict:
    return {"query": valid.search_text(valid.as_text(args.get("query"), "query")),
            "knesset_num": _knesset_num_arg(args), "top_k": _top_k_arg(args, config.API_LIST_MAX_TOP_K)}


def _get_bill_args(args: dict) -> dict:
    return {"bill_id": valid.numeric_id(valid.as_text(args.get("bill_id"), "bill_id"), "bill_id"),
            "include_text": valid.as_bool(args.get("include_text"), "include_text"),
            "max_chars": _clamp(valid.as_int(args.get("max_chars"), "max_chars"), config.BILL_TEXT_MAX_MAX_CHARS),
            "knesset_num": _knesset_num_arg(args)}


def _query_votes_args(args: dict) -> dict:
    return {"query": valid.search_text(valid.as_text(args.get("query"), "query")),
            "mk_id": valid.numeric_id(valid.as_text(args.get("mk_id"), "mk_id"), "mk_id"),
            "knesset_num": _knesset_num_arg(args), "top_k": _top_k_arg(args, config.API_LIST_MAX_TOP_K)}


DEFAULT_KNESSET_NUM = 25
TOOL_ARGUMENT_VALIDATORS = {
    "find_mk":                _find_args,
    "find_committee":         _find_args,
    "find_party":             _find_args,
    "query_protocols":        _query_protocols_args,
    "get_meeting_attendance": _meeting_attendance_args,
    "query_bills":            _query_bills_args,
    "get_bill":               _get_bill_args,
    "query_votes":            _query_votes_args,
}


def validated_tool_args(tool: str, args: dict) -> dict:
    """Raises valid.ApiInputError (invalid_<arg>, or unknown_tool) on input the public API refuses."""
    if tool not in TOOL_ARGUMENT_VALIDATORS:
        raise valid.ApiInputError("unknown_tool", f"no tool named {tool!r}; list the tools to see their names")
    return TOOL_ARGUMENT_VALIDATORS[tool](args)


def tool_call_outcome(tool: str, args: dict) -> tuple[int, dict]:
    """(200, body) or (error status, error body) for already validated args."""
    envelope = dispatch(RESEARCH_TOOL_REGISTRY, tool, {k: v for k, v in args.items() if v is not None})
    provenance = {k: v for k, v in (envelope.provenance or {}).items() if k not in _INTERNAL_PROVENANCE_KEYS}
    if envelope.error:
        return _error_status_and_body(tool, envelope, provenance)
    try:
        results = json.loads(envelope.full) if envelope.full else None
    except json.JSONDecodeError as exc:
        print(f"[api] {tool} returned non-JSON full: {exc}")
        results = envelope.full
    trimmed = _fit_to_size(results)
    return 200, {
        "tool":      tool,
        "args":      provenance,
        "results":   results,
        "warnings":  list((envelope.metadata or {}).get("warnings") or []),
        "truncated": bool(envelope.truncated or trimmed),
        "hint":      _hint(tool, provenance, results, trimmed),
    }


def run_tool(tool: str, raw_args: dict, response_format: str = "json"):
    response_format = valid.response_format(response_format)
    status, body = tool_call_outcome(tool, validated_tool_args(tool, raw_args))
    if status != 200:
        return JSONResponse(body, status_code=status)
    if response_format == "md":
        return PlainTextResponse(render_markdown(body), media_type="text/markdown; charset=utf-8")
    return body


# ── tool routes ──────────────────────────────────────────────────────────────

def _find(tool: str, q: str, knesset_num: int, top_k: int | None, format: str):
    return run_tool(tool, {"query": q, "knesset_num": knesset_num, "top_k": top_k}, format)


@router.get("/v1/mks")
def find_mk(q: str = "", knesset_num: int = 25, top_k: int | None = None, format: str = "json"):
    return _find("find_mk", q, knesset_num, top_k, format)


@router.get("/v1/committees")
def find_committee(q: str = "", knesset_num: int = 25, top_k: int | None = None, format: str = "json"):
    return _find("find_committee", q, knesset_num, top_k, format)


@router.get("/v1/parties")
def find_party(q: str = "", knesset_num: int = 25, top_k: int | None = None, format: str = "json"):
    return _find("find_party", q, knesset_num, top_k, format)


@router.get("/v1/protocols")
def query_protocols(
    q: str = "",
    search_in: list[str] | None = Query(None),
    mk_id: str | None = None,
    party: str | None = None,
    committee: list[str] | None = Query(None),
    meeting_id: list[str] | None = Query(None),
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str | None = None,
    top_k: int | None = None,
    offset: int = 0,
    knesset_num: int = 25,
    format: str = "json",
):
    return run_tool("query_protocols", {
        "query": q, "search_in": search_in, "mk_id": mk_id, "party": party, "committees": committee,
        "meeting_ids": meeting_id, "date_from": date_from, "date_to": date_to, "sort": sort,
        "top_k": top_k, "offset": offset, "knesset_num": knesset_num,
    }, format)


@router.get("/v1/meetings/{meeting_id}/attendance")
def get_meeting_attendance(meeting_id: str, format: str = "json"):
    return run_tool("get_meeting_attendance", {"meeting_id": meeting_id}, format)


@router.get("/v1/bills")
def query_bills(q: str = "", knesset_num: int = 25, top_k: int | None = None, format: str = "json"):
    return run_tool("query_bills", {"query": q, "knesset_num": knesset_num, "top_k": top_k}, format)


@router.get("/v1/bills/{bill_id}")
def get_bill(bill_id: str, include_text: bool = False, max_chars: int | None = None,
             knesset_num: int = 25, format: str = "json"):
    return run_tool("get_bill", {"bill_id": bill_id, "include_text": include_text, "max_chars": max_chars,
                                 "knesset_num": knesset_num}, format)


@router.get("/v1/votes")
def query_votes(q: str = "", mk_id: str | None = None, knesset_num: int = 25,
                top_k: int | None = None, format: str = "json"):
    return run_tool("query_votes", {"query": q, "mk_id": mk_id, "knesset_num": knesset_num, "top_k": top_k}, format)


# ── discovery / meta ─────────────────────────────────────────────────────────

def tool_listing() -> list[dict]:
    listing = []
    for spec in RESEARCH_TOOL_REGISTRY:
        parameters = {k: v for k, v in spec.schema.items() if k != "description"}
        listing.append({"name": spec.name, "endpoint": TOOL_ENDPOINTS[spec.name],
                        "description": spec.schema.get("description", ""), "parameters": parameters})
    return listing


@router.get("/v1/tools")
def list_tools():
    """Every tool with its endpoint, description and JSON-schema parameters."""
    return {"tools": tool_listing()}


@router.get("/v1/meta")
def meta():
    """Data coverage: row counts per table, indexed Knesset numbers, meeting date range and build times."""
    if not store.exists():
        return JSONResponse({"error_code": "knesset_db_missing", "message": _PUBLIC_5XX_MESSAGES[503]},
                            status_code=503)
    conn = store.connect()
    try:
        counts = store.table_row_counts(conn, ("mks", "committees", "meetings", "attendance",
                                               "topics", "opinions", "speeches"))
        build_meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        knessets = [r[0] for r in conn.execute("SELECT DISTINCT knesset_num FROM meetings ORDER BY 1")]
        first_date, last_date = conn.execute("SELECT MIN(date), MAX(date) FROM meetings").fetchone()
    finally:
        conn.close()
    return {"counts": counts, "knesset_nums": knessets, "meeting_dates": {"from": first_date, "to": last_date},
            "build": build_meta}


@router.get("/health")
def health():
    """Liveness check; reports whether the protocol database is available."""
    return {"status": "ok", "db": store.exists()}


def _instructions_text() -> str:
    return INSTRUCTIONS_PATH.read_text(encoding="utf-8")


@router.get("/llms.txt", response_class=PlainTextResponse)
@router.get("/agent-instructions", response_class=PlainTextResponse)
def agent_instructions():
    """Plain-text usage rules, endpoints, search tips and recipes for AI agents (llms.txt convention). Read this before calling the tools."""
    return PlainTextResponse(_instructions_text(), media_type="text/markdown; charset=utf-8")


@router.get("/llms-full.txt", response_class=PlainTextResponse)
def agent_instructions_full():
    """The agent instructions followed by every tool's full description and parameters, in one plain-text document."""
    parts = [_instructions_text(), "\n# Tool reference\n"]
    for tool in tool_listing():
        parts.append(f"## {tool['name']} — GET {tool['endpoint']}\n\n{tool['description']}\n\n"
                     f"```json\n{json.dumps(tool['parameters'], ensure_ascii=False, indent=1)}\n```\n")
    return PlainTextResponse("\n".join(parts), media_type="text/markdown; charset=utf-8")
