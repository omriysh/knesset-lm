"""
Public read-only HTTP surface over the research tools (RESEARCH_TOOL_REGISTRY).

Every /v1 route maps query-string params to tool args, runs the same `dispatch` the research
agent uses, and unwraps the ToolEnvelope. No query logic lives here: only API-side limits
(input validation in api.validation, smaller top_k, response size cap), hints for the calling
agent, and error → status mapping. 5xx bodies carry a generic message; the real exception is
printed server-side.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse

import config
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from api import validation as valid
from api.markdown import render_markdown
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


def _split_list(values: list[str] | None) -> list[str]:
    """Repeated params and comma-separated values both work: ?x=a&x=b or ?x=a,b."""
    return [part.strip() for value in values or [] for part in value.split(",") if part.strip()]


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


def _error_response(tool: str, envelope, provenance: dict) -> JSONResponse:
    status = _error_status(envelope.error)
    if status >= 500:
        print(f"[api] {tool} → {status} {envelope.error}: {(envelope.metadata or {}).get('exception')}")
        message = _PUBLIC_5XX_MESSAGES.get(status, "internal error")
    else:
        message = envelope.error.replace("_", " ")
    return JSONResponse({"error_code": envelope.error, "message": message, "tool": tool, "args": provenance},
                        status_code=status)


def run_tool(tool: str, args: dict, response_format: str = "json"):
    envelope = dispatch(RESEARCH_TOOL_REGISTRY, tool, {k: v for k, v in args.items() if v is not None})
    provenance = {k: v for k, v in (envelope.provenance or {}).items() if k not in _INTERNAL_PROVENANCE_KEYS}
    if envelope.error:
        return _error_response(tool, envelope, provenance)
    try:
        results = json.loads(envelope.full) if envelope.full else None
    except json.JSONDecodeError as exc:
        print(f"[api] {tool} returned non-JSON full: {exc}")
        results = envelope.full
    trimmed = _fit_to_size(results)
    body = {
        "tool":      tool,
        "args":      provenance,
        "results":   results,
        "warnings":  list((envelope.metadata or {}).get("warnings") or []),
        "truncated": bool(envelope.truncated or trimmed),
        "hint":      _hint(tool, provenance, results, trimmed),
    }
    if response_format == "md":
        return PlainTextResponse(render_markdown(body), media_type="text/markdown; charset=utf-8")
    return body


# ── tool routes ──────────────────────────────────────────────────────────────

def _find(tool: str, q: str, knesset_num: int, top_k: int | None, format: str):
    return run_tool(tool, {"query": valid.search_text(q), "knesset_num": valid.knesset_num(knesset_num),
                           "top_k": _clamp(top_k, config.API_FIND_MAX_TOP_K)}, valid.response_format(format))


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
    scopes = valid.list_param(_split_list(search_in), "search_in")
    committees = [valid.name_filter(c, "committee") for c in valid.list_param(committee or [], "committee")]
    meeting_ids = [valid.numeric_id(m, "meeting_id") for m in valid.list_param(_split_list(meeting_id), "meeting_id")]
    return run_tool("query_protocols", {
        "query":       valid.keyword_query(q),
        "search_in":   list(dict.fromkeys(scopes)) or list(config.API_PROTOCOLS_DEFAULT_SCOPES),
        "mk_id":       valid.numeric_id(mk_id, "mk_id"),
        "party":       valid.name_filter(party, "party"),
        "committees":  [c for c in committees if c] or None,
        "meeting_ids": meeting_ids or None,
        "date_from":   valid.iso_date(date_from, "date_from"),
        "date_to":     valid.iso_date(date_to, "date_to"),
        "sort":        valid.name_filter(sort, "sort"),
        "top_k":       _clamp(top_k or config.API_PROTOCOLS_DEFAULT_TOP_K, config.API_PROTOCOLS_MAX_TOP_K),
        "offset":      valid.offset(offset),
        "knesset_num": valid.knesset_num(knesset_num),
    }, valid.response_format(format))


@router.get("/v1/meetings/{meeting_id}/attendance")
def get_meeting_attendance(meeting_id: str, format: str = "json"):
    return run_tool("get_meeting_attendance", {"meeting_id": valid.numeric_id(meeting_id, "meeting_id")},
                    valid.response_format(format))


@router.get("/v1/bills")
def query_bills(q: str = "", knesset_num: int = 25, top_k: int | None = None, format: str = "json"):
    return run_tool("query_bills", {"query": valid.search_text(q), "knesset_num": valid.knesset_num(knesset_num),
                                    "top_k": _clamp(top_k, config.API_LIST_MAX_TOP_K)}, valid.response_format(format))


@router.get("/v1/bills/{bill_id}")
def get_bill(bill_id: str, include_text: bool = False, max_chars: int | None = None,
             knesset_num: int = 25, format: str = "json"):
    return run_tool("get_bill", {"bill_id": valid.numeric_id(bill_id, "bill_id"), "include_text": include_text,
                                 "max_chars": _clamp(max_chars, config.BILL_TEXT_MAX_MAX_CHARS),
                                 "knesset_num": valid.knesset_num(knesset_num)}, valid.response_format(format))


@router.get("/v1/votes")
def query_votes(q: str = "", mk_id: str | None = None, knesset_num: int = 25,
                top_k: int | None = None, format: str = "json"):
    return run_tool("query_votes", {"query": valid.search_text(q), "mk_id": valid.numeric_id(mk_id, "mk_id"),
                                    "knesset_num": valid.knesset_num(knesset_num),
                                    "top_k": _clamp(top_k, config.API_LIST_MAX_TOP_K)}, valid.response_format(format))


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
    return {"tools": tool_listing()}


@router.get("/v1/meta")
def meta():
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
    return {"status": "ok", "db": store.exists()}


def _instructions_text() -> str:
    return INSTRUCTIONS_PATH.read_text(encoding="utf-8")


@router.get("/llms.txt", response_class=PlainTextResponse)
@router.get("/agent-instructions", response_class=PlainTextResponse)
def agent_instructions():
    return PlainTextResponse(_instructions_text(), media_type="text/markdown; charset=utf-8")


@router.get("/llms-full.txt", response_class=PlainTextResponse)
def agent_instructions_full():
    parts = [_instructions_text(), "\n# Tool reference\n"]
    for tool in tool_listing():
        parts.append(f"## {tool['name']} — GET {tool['endpoint']}\n\n{tool['description']}\n\n"
                     f"```json\n{json.dumps(tool['parameters'], ensure_ascii=False, indent=1)}\n```\n")
    return PlainTextResponse("\n".join(parts), media_type="text/markdown; charset=utf-8")
