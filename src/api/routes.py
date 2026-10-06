"""
Public read-only HTTP surface over the research tools (RESEARCH_TOOL_REGISTRY).

Every /v1 route maps query-string params to tool args, runs the same `dispatch` the research
agent uses, and unwraps the ToolEnvelope. No query logic lives here: only API-side limits
(input validation in api.tool_arguments with PUBLIC_API_LIMITS), hints for the calling
agent, and error → status mapping. 5xx bodies carry a generic message; the real exception is
printed server-side and logged to errors.log with the request id.

top_k is not public: pages have fixed sizes, the tool schemas shown to callers are a public view of
the registry (public_tool_schema), and a response's `next` is the exact argument patch for the next
page. query_protocols pages whole rows by a character budget (about API_PROTOCOLS_PAGE_CHARS per
scope, offset = row count, utils.tool_helpers.char_paging), so no text is ever cut or split; get_bill
pages its text by characters (max_chars from offset); query_bills / query_votes / find_* page by rows.
"""

import copy
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

import config
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from api import validation as valid
from api.markdown import render_markdown
from api.request_log import current_request_id, log_question, log_server_error
from api.tool_arguments import public_page_size, validated_tool_args
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
_ARGUMENTS_HIDDEN_FROM_PUBLIC = {"top_k"}

_HINTS_BY_ERROR = {
    "mk_not_found":        "resolve the name with find_mk and pass its mk_id",
    "mk_id_not_resolved":  "pass one of the candidates' mk_id, or call find_mk with the name",
    "bill_not_found":      "bill_id comes from query_bills results",
    "meeting_not_found":   "meeting_id comes from query_protocols rows",
    "missing_query":       "this tool needs a query (a name or Hebrew key words); query_bills also accepts mk_id alone",
    "invalid_initiator_role": "initiator_role is initiator or joined, and needs mk_id",
    "invalid_query":       f"at most {config.API_MAX_QUERY_WORDS} words; key words are AND-ed, "
                           "use one topic per call",
    "invalid_date_from":   "dates are YYYY-MM-DD, e.g. 2024-01-31",
    "invalid_date_to":     "dates are YYYY-MM-DD, e.g. 2024-12-31",
    "invalid_search_in":   "search_in values: topics, opinions, speeches",
    "invalid_mk_id":       "mk_id is the number find_mk returns",
    "invalid_meeting_id":  "meeting_id as returned in query_protocols rows: digits, or p + digits for a plenum session",
    "invalid_bill_id":     "bill_id is the number in query_bills results",
    "invalid_knesset_num": f"knesset_num: {', '.join(map(str, config.PROTOCOL_KNESSET_NUMS))} for protocols and "
                           f"committees, {config.API_KNESSET_NUM_RANGE[0]}-{config.API_KNESSET_NUM_RANGE[1]} for "
                           "MKs and parties; omit it for bills and votes (every Knesset)",
    "invalid_offset":      f"offset is 0 to {config.API_PROTOCOLS_MAX_OFFSET} rows for query_protocols, "
                           f"0 to {config.API_MAX_OFFSET} rows for query_bills / query_votes, 0 to "
                           f"{config.BILL_TEXT_MAX_OFFSET} characters for get_bill; "
                           "copy it from the response's next",
    "unknown_tool":        "list the tools to see their names",
}


def error_hint(error_code: str) -> str:
    if error_code in _HINTS_BY_ERROR:
        return _HINTS_BY_ERROR[error_code]
    if error_code.endswith("_not_found"):
        return "check the value with find_mk, find_party or find_committee"
    return ""


def input_error_body(tool: str, exc: valid.ApiInputError) -> dict:
    return {"error_code": exc.error_code, "message": exc.message, "tool": tool, "hint": error_hint(exc.error_code)}


def _error_status(error_code: str) -> int:
    if error_code in _STATUS_BY_ERROR:
        return _STATUS_BY_ERROR[error_code]
    if error_code.endswith("_not_found"):
        return 404
    if error_code.startswith(("missing_", "invalid_")) or error_code.endswith("_not_resolved"):
        return 400
    return 500


def _result_rows(results) -> list:
    if isinstance(results, list):
        return results
    if isinstance(results, dict):
        return [row for rows in results.values() if isinstance(rows, list) for row in rows]
    return []


def result_row_count(tool: str, results) -> int:
    if results is None:
        return 0
    if isinstance(results, list):
        return len(results)
    if tool != "get_bill" and isinstance(results, dict) and any(isinstance(rows, list) for rows in results.values()):
        return len(_result_rows(results))
    return 1


def _protocol_next_pages(metadata: dict) -> dict | None:
    """Per scope with more rows: the argument patch (a row offset) that fetches its next page."""
    next_pages = {scope: {"search_in": [scope], "offset": next_offset}
                  for scope, next_offset in (metadata.get("next_offsets") or {}).items()}
    return next_pages or None


def _list_next_page(args: dict, metadata: dict, results) -> dict | None:
    paging = metadata.get("paging") or {}
    if not isinstance(results, list) or not paging.get("has_more"):
        return None
    return {"offset": int(paging.get("offset", args.get("offset") or 0)) + len(results)}


def _next_page(tool: str, args: dict, metadata: dict, results) -> dict | None:
    if tool == "query_protocols" and isinstance(results, dict):
        return _protocol_next_pages(metadata)
    if tool == "get_bill" and metadata.get("next_offset") is not None:
        return {"offset": metadata["next_offset"]}
    if tool in ("query_bills", "query_votes"):
        return _list_next_page(args, metadata, results)
    return None


def _mk_party(mk_row: dict) -> str:
    factions = [faction for faction in (mk_row.get("profile") or {}).get("factions") or [] if isinstance(faction, dict)]
    if not factions:
        return mk_row.get("party") or ""
    latest_faction = max(factions, key=lambda faction: (bool(faction.get("is_current")),
                                                         faction.get("start_date") or ""))
    return latest_faction.get("faction_name") or ""


def _find_next_step(tool: str, top_row: dict) -> str:
    if tool == "find_mk" and top_row.get("mk_id"):
        party = _mk_party(top_row)
        party_text = f", party {party}" if party else ""
        return (f"found MK {top_row.get('full_name', '')} (mk_id={top_row['mk_id']}{party_text}): "
                f'use mk_id="{top_row["mk_id"]}" in query_protocols / query_votes')
    if tool == "find_party" and top_row.get("party"):
        return f'use party="{top_row["party"]}" in query_protocols'
    if tool == "find_committee" and top_row.get("name"):
        return f'use committees=["{top_row["name"]}"] in query_protocols'
    return ""


def _single_meeting_suggestion(args: dict, results: dict) -> str:
    if not args.get("query") or args.get("meeting_ids"):
        return ""
    meeting_ids = {row.get("meeting_id") for rows in results.values() if isinstance(rows, list) for row in rows}
    if len(meeting_ids) != 1:
        return ""
    meeting_id = meeting_ids.pop()
    return f'all hits are in meeting {meeting_id}: list it with query="", meeting_ids=["{meeting_id}"]'


def _protocol_hint_parts(args: dict, results: dict, has_diagnostics: bool, next_page: dict | None) -> list[str]:
    parts = [f'{scope}: more rows; call again with search_in=["{scope}"], offset={patch["offset"]}'
             for scope, patch in (next_page or {}).items()]
    if all(not rows for rows in results.values()):
        if not has_diagnostics:
            parts.append("no rows: try fewer or different key words, a spelling variant, or drop a filter")
    elif _single_meeting_suggestion(args, results):
        parts.append(_single_meeting_suggestion(args, results))
    return parts


def _list_hint_parts(tool: str, args: dict, results: list, has_diagnostics: bool,
                     next_page: dict | None) -> list[str]:
    parts = []
    is_listing = tool.startswith("find_") and not args.get("query")
    if not results:
        if not has_diagnostics:
            parts.append("no results: try a shorter query or a different spelling")
    elif not is_listing and _find_next_step(tool, results[0]):
        parts.append(_find_next_step(tool, results[0]))
    page_size = args.get("top_k")
    if tool.startswith("find_") and not is_listing and page_size and len(results) >= page_size:
        parts.append(f"{len(results)} matches fill the page: refine the query if the one you want is missing")
    if next_page:
        parts.append(f"more rows; call again with offset={next_page['offset']}")
    return parts


def _hint(tool: str, args: dict, results, diagnostics: list, handler_hints: list, next_page: dict | None) -> str:
    parts = [diagnostic["message"] for diagnostic in diagnostics
             if isinstance(diagnostic, dict) and diagnostic.get("message")]
    parts += [str(handler_hint) for handler_hint in handler_hints if handler_hint]
    if tool == "query_protocols" and isinstance(results, dict):
        parts += _protocol_hint_parts(args, results, bool(parts), next_page)
    elif isinstance(results, list):
        parts += _list_hint_parts(tool, args, results, bool(parts), next_page)
    elif tool == "get_bill" and next_page:
        parts.append(f"the bill text continues; call again with offset={next_page['offset']} (a character position)")
    return "; ".join(parts)


def _error_status_and_body(tool: str, envelope, provenance: dict) -> tuple[int, dict]:
    status = _error_status(envelope.error)
    metadata = envelope.metadata or {}
    if status >= 500:
        print(f"[api] {tool} -> {status} {envelope.error}: {metadata.get('exception')}", flush=True)
        log_server_error(current_request_id.get(), f"api {tool} → {status} {envelope.error}: {metadata.get('exception')}",
                         metadata.get("traceback") or "")
        message = _PUBLIC_MESSAGES_BY_ERROR.get(envelope.error) or _PUBLIC_5XX_MESSAGES.get(status, "internal error")
    else:
        message = envelope.summary or envelope.error.replace("_", " ")
    body = {"error_code": envelope.error, "message": message, "tool": tool, "args": provenance,
            "hint": error_hint(envelope.error)}
    if status < 500:
        for detail_key in ("diagnostics", "candidates"):
            if metadata.get(detail_key):
                body[detail_key] = list(metadata[detail_key])
    return status, body


def tool_call_outcome(tool: str, args: dict) -> tuple[int, dict]:
    """(200, body) or (error status, error body) for already validated args."""
    envelope = dispatch(RESEARCH_TOOL_REGISTRY, tool, {k: v for k, v in args.items() if v is not None},
                        args_already_validated=True)
    metadata = envelope.metadata or {}
    provenance = {k: v for k, v in (envelope.provenance or {}).items()
                  if k not in _INTERNAL_PROVENANCE_KEYS and k not in _ARGUMENTS_HIDDEN_FROM_PUBLIC}
    if envelope.error:
        return _error_status_and_body(tool, envelope, provenance)
    try:
        results = json.loads(envelope.full) if envelope.full else None
    except json.JSONDecodeError as exc:
        print(f"[api] {tool} returned non-JSON full: {exc}")
        results = envelope.full
    diagnostics = list(metadata.get("diagnostics") or [])
    next_page = _next_page(tool, args, metadata, results)
    return 200, {
        "tool":        tool,
        "args":        provenance,
        "hint":        _hint(tool, args, results, diagnostics, list(metadata.get("hints") or []), next_page),
        "diagnostics": diagnostics,
        "next":        next_page,
        "warnings":    list(metadata.get("warnings") or []),
        "truncated":   bool(envelope.truncated),
        "results":     results,
    }


def log_tool_call(request: Request | None, route: str, tool: str, raw_args: dict, body: dict) -> None:
    if request is None:
        return
    try:
        rows = result_row_count(tool, body["results"]) if "results" in body else None
        log_question(request, route, None, {k: v for k, v in raw_args.items() if v not in (None, "", [])},
                     outcome={"rows": rows, "error_code": body.get("error_code")})
    except Exception as exc:
        print(f"[api] logging the {route} call failed: {type(exc).__name__}: {exc}", flush=True)


def run_tool(request: Request | None, tool: str, raw_args: dict, response_format: str = "json"):
    response_format = valid.response_format(response_format)
    try:
        args = validated_tool_args(tool, raw_args)
    except valid.ApiInputError as exc:
        body = input_error_body(tool, exc)
        log_tool_call(request, f"api:{tool}", tool, raw_args, body)
        return JSONResponse(body, status_code=400)
    status, body = tool_call_outcome(tool, args)
    log_tool_call(request, f"api:{tool}", tool, raw_args, body)
    if status != 200:
        return JSONResponse(body, status_code=status)
    if response_format == "md":
        return PlainTextResponse(render_markdown(body), media_type="text/markdown; charset=utf-8")
    return body


# ── public tool schemas ──────────────────────────────────────────────────────

_PUBLIC_QUERY_EXAMPLES = {
    "find_mk":         ["עודד פורר"],
    "find_committee":  ["ועדת הכספים"],
    "find_party":      ["יש עתיד"],
    "query_protocols": ["גיוס", "יוקר המחיה"],
    "query_bills":     ["שכר מינימום"],
    "query_votes":     ["תקציב"],
}
_PUBLIC_ARGUMENT_EXAMPLES = {
    "party":      ["יש עתיד"],
    "committees": [["ועדת הכספים"]],
    "date_from":  ["2024-01-01"],
    "date_to":    ["2024-12-31"],
}
_PUBLIC_OFFSET_DESCRIPTIONS = {
    "query_protocols": "Rows of the scope to skip. Copy it from the response's `next`, which pairs it with the "
                       "one scope (search_in) it pages.",
    "get_bill":        "Character position in the bill text. Copy it from the response's `next`.",
}
_PUBLIC_OFFSET_MAXIMUMS = {"query_protocols": config.API_PROTOCOLS_MAX_OFFSET, "get_bill": config.BILL_TEXT_MAX_OFFSET}
_PUBLIC_OFFSET_DESCRIPTION = "Rows to skip. Copy it from the response's `next`."
_READ_THE_RESPONSE = "Read `hint` (next step), `diagnostics` (why a filter matched nothing) and `next` (paging)."
_PROTOCOL_KNESSETS_TEXT = ", ".join(map(str, config.PROTOCOL_KNESSET_NUMS))
_ROSTER_KNESSETS_TEXT = f"{config.API_KNESSET_NUM_RANGE[0]}-{config.API_KNESSET_NUM_RANGE[1]}"
_EVERY_KNESSET_TEXT = "knesset_num is optional: omit it to search every Knesset (live Knesset API)."
_PUBLIC_USAGE_NOTES = {
    "find_mk":         (f"Returns up to {config.API_FIND_PAGE_SIZE} matches. "
                        f"knesset_num {_ROSTER_KNESSETS_TEXT}, default 25."),
    "find_committee":  (f"Returns up to {config.API_FIND_PAGE_SIZE} matches; an empty query lists every committee. "
                        f"Committees of Knesset {_PROTOCOL_KNESSETS_TEXT} only."),
    "find_party":      ("An empty query lists every party. "
                        f"knesset_num {_ROSTER_KNESSETS_TEXT}, default 25."),
    "query_protocols": (f"Pages are about {config.API_PROTOCOLS_PAGE_CHARS} characters of whole rows per scope, "
                        "and offset counts rows: follow `next` for more. Texts are never cut or split: a "
                        "long row comes whole, so a page can run over. "
                        "Query: 1-2 Hebrew key words, all must appear; one topic per call. mk_id, party and "
                        "committees accept names, the server resolves them. Protocols cover Knesset "
                        f"{_PROTOCOL_KNESSETS_TEXT} only, meetings from {{meeting_date_from}} to {{meeting_date_to}}. "
                        "Cite committee, date and meeting_id for every claim and link the row's `url` (the source "
                        "in the protocol reader: the meeting, the speech, or the quote highlighted); quote `quote` "
                        "or speech `text`, never an `opinion` paraphrase as a quote."),
    "get_meeting_attendance": "Cite the meeting_id and link the `url`.",
    "query_bills":     f"Pages of {config.API_LIST_PAGE_SIZE}; follow `next` for more. {_EVERY_KNESSET_TEXT}",
    "get_bill":        ("include_text=true returns max_chars characters of the text from offset, with "
                        f"text_chars [start, end, total]; follow `next` for more. {_EVERY_KNESSET_TEXT}"),
    "query_votes":     f"Pages of {config.API_LIST_PAGE_SIZE}; follow `next` for more. {_EVERY_KNESSET_TEXT}",
}


UNKNOWN_MEETING_DATES = ("(unknown: the database is unavailable)", "(unknown)")
_meeting_dates_by_db_version: dict[tuple, tuple[str, str]] = {}


def _db_version() -> tuple[str, int] | None:
    try:
        return str(store.db_path()), os.stat(store.db_path()).st_mtime_ns
    except OSError as exc:
        print(f"[api] cannot stat the protocol database {store.db_path()}: {exc}", flush=True)
        return None


def protocol_meeting_date_range() -> tuple[str, str]:
    """First and last meeting date of the processed protocol Knessets, re-read when knesset.db changes
    on disk (a rebuild), so descriptions and instructions follow the data without a restart."""
    db_version = _db_version()
    if db_version is None:
        return UNKNOWN_MEETING_DATES
    if db_version in _meeting_dates_by_db_version:
        return _meeting_dates_by_db_version[db_version]
    knesset_nums = list(config.PROTOCOL_KNESSET_NUMS)
    try:
        conn = store.connect()
        try:
            first_date, last_date = conn.execute(
                f"SELECT MIN(date), MAX(date) FROM meetings WHERE knesset_num IN ({','.join('?' * len(knesset_nums))})",
                knesset_nums).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[api] reading the protocol date range failed: {type(exc).__name__}: {exc}", flush=True)
        return UNKNOWN_MEETING_DATES
    _meeting_dates_by_db_version.clear()
    _meeting_dates_by_db_version[db_version] = (first_date or UNKNOWN_MEETING_DATES[0],
                                                last_date or UNKNOWN_MEETING_DATES[1])
    return _meeting_dates_by_db_version[db_version]


def with_coverage_dates(text: str) -> str:
    first_date, last_date = protocol_meeting_date_range()
    return text.replace("{meeting_date_from}", first_date).replace("{meeting_date_to}", last_date)


def _public_description(tool: str, description: str) -> str:
    search_page_size = public_page_size(tool, {"query": "x"})
    if search_page_size:
        description = re.sub(r"\btop_k\b", str(search_page_size), description)
    description = description.replace(" Texts are returned in full.", "")
    description = description.replace(" (top_k rows per scope)", " (paged by characters)")
    notes = " ".join(note for note in (_PUBLIC_USAGE_NOTES.get(tool, ""), _READ_THE_RESPONSE) if note)
    return f"{description}\n{with_coverage_dates(notes)}"


def public_tool_schema(spec) -> dict:
    """The registry schema as public callers see it: no top_k, the real public defaults and limits, examples."""
    schema = copy.deepcopy(spec.schema)
    properties = schema.setdefault("properties", {})
    for hidden_argument in _ARGUMENTS_HIDDEN_FROM_PUBLIC:
        properties.pop(hidden_argument, None)
    if spec.name in ("query_bills", "query_votes") and "offset" not in properties:
        properties["offset"] = {"type": "integer", "default": 0, "minimum": 0}
    if "offset" in properties:
        properties["offset"].update(
            maximum=_PUBLIC_OFFSET_MAXIMUMS.get(spec.name, config.API_MAX_OFFSET),
            description=_PUBLIC_OFFSET_DESCRIPTIONS.get(spec.name, _PUBLIC_OFFSET_DESCRIPTION))
    if "search_in" in properties:
        properties["search_in"].update(default=list(config.API_PROTOCOLS_DEFAULT_SCOPES),
                                       maxItems=config.API_MAX_LIST_ITEMS)
    if "query" in properties:
        properties["query"]["maxLength"] = config.API_MAX_QUERY_CHARS
        if spec.name in _PUBLIC_QUERY_EXAMPLES:
            properties["query"]["examples"] = _PUBLIC_QUERY_EXAMPLES[spec.name]
    if "party" in properties:
        properties["party"]["maxLength"] = config.API_MAX_NAME_CHARS
    if "knesset_num" in properties and spec.name in ("query_protocols", "find_committee"):
        properties["knesset_num"].update(enum=list(config.PROTOCOL_KNESSET_NUMS))
    elif "knesset_num" in properties:
        properties["knesset_num"].update(minimum=config.API_KNESSET_NUM_RANGE[0],
                                         maximum=config.API_KNESSET_NUM_RANGE[1])
    for list_argument in ("committees", "meeting_ids"):
        if list_argument in properties:
            properties[list_argument]["maxItems"] = config.API_MAX_LIST_ITEMS
    for argument, examples in _PUBLIC_ARGUMENT_EXAMPLES.items():
        if argument in properties:
            properties[argument]["examples"] = examples
    schema["required"] = [name for name in schema.get("required", []) if name in properties]
    if not schema["required"]:
        schema.pop("required")
    schema["description"] = _public_description(spec.name, schema.get("description", ""))
    return schema


# ── tool routes ──────────────────────────────────────────────────────────────

def _find(request: Request, tool: str, q: str, knesset_num: int, format: str):
    return run_tool(request, tool, {"query": q, "knesset_num": knesset_num}, format)


@router.get("/v1/mks")
def find_mk(request: Request, q: str = "", knesset_num: int = 25, format: str = "json"):
    return _find(request, "find_mk", q, knesset_num, format)


@router.get("/v1/committees")
def find_committee(request: Request, q: str = "", knesset_num: int = 25, format: str = "json"):
    return _find(request, "find_committee", q, knesset_num, format)


@router.get("/v1/parties")
def find_party(request: Request, q: str = "", knesset_num: int = 25, format: str = "json"):
    return _find(request, "find_party", q, knesset_num, format)


@router.get("/v1/protocols")
def query_protocols(
    request: Request,
    q: str = "",
    search_in: list[str] | None = Query(None),
    mk_id: str | None = None,
    party: str | None = None,
    committee: list[str] | None = Query(None),
    meeting_id: list[str] | None = Query(None),
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str | None = None,
    offset: int = 0,
    knesset_num: int = 25,
    format: str = "json",
):
    return run_tool(request, "query_protocols", {
        "query": q, "search_in": search_in, "mk_id": mk_id, "party": party, "committees": committee,
        "meeting_ids": meeting_id, "date_from": date_from, "date_to": date_to, "sort": sort,
        "offset": offset, "knesset_num": knesset_num,
    }, format)


@router.get("/v1/meetings/{meeting_id}/attendance")
def get_meeting_attendance(request: Request, meeting_id: str, format: str = "json"):
    return run_tool(request, "get_meeting_attendance", {"meeting_id": meeting_id}, format)


@router.get("/v1/bills")
def query_bills(request: Request, q: str = "", mk_id: str | None = None, initiator_role: str = "",
                knesset_num: int | None = None, offset: int = 0, format: str = "json"):
    return run_tool(request, "query_bills", {"query": q, "mk_id": mk_id, "initiator_role": initiator_role,
                                             "knesset_num": knesset_num, "offset": offset}, format)


@router.get("/v1/bills/{bill_id}")
def get_bill(request: Request, bill_id: str, include_text: bool = False, max_chars: int | None = None,
             offset: int = 0, knesset_num: int | None = None, format: str = "json"):
    return run_tool(request, "get_bill", {"bill_id": bill_id, "include_text": include_text,
                                          "max_chars": max_chars, "offset": offset,
                                          "knesset_num": knesset_num}, format)


@router.get("/v1/votes")
def query_votes(request: Request, q: str = "", mk_id: str | None = None, knesset_num: int | None = None,
                offset: int = 0, format: str = "json"):
    return run_tool(request, "query_votes", {"query": q, "mk_id": mk_id, "knesset_num": knesset_num,
                                             "offset": offset}, format)


# ── discovery / meta ─────────────────────────────────────────────────────────

def tool_listing() -> list[dict]:
    listing = []
    for spec in RESEARCH_TOOL_REGISTRY:
        schema = public_tool_schema(spec)
        parameters = {k: v for k, v in schema.items() if k != "description"}
        listing.append({"name": spec.name, "endpoint": TOOL_ENDPOINTS[spec.name],
                        "description": schema["description"], "parameters": parameters})
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
