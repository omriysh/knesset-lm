"""
Validation of research-tool arguments (RESEARCH_TOOL_REGISTRY), shared by every caller: the public
/v1 routes and MCP tools (PUBLIC_API_LIMITS) and the research agent's LLM-generated tool calls
(AGENT_LIMITS, applied in utils.tools.dispatch through ToolSpec.validate_args). Rejections raise
api.validation.ApiInputError (invalid_<arg>, or unknown_tool).
"""

from dataclasses import dataclass

import config
from api import validation as valid
from retrieval.knesset_db_store import PROTOCOL_SCOPES

DEFAULT_KNESSET_NUM = 25


@dataclass(frozen=True)
class ToolArgumentLimits:
    max_query_chars: int
    max_query_words: int
    max_name_chars: int
    max_list_items: int
    max_offset: int
    find_max_top_k: int
    list_max_top_k: int
    protocols_max_top_k: int
    protocols_default_top_k: int
    protocols_default_scopes: tuple[str, ...]


PUBLIC_API_LIMITS = ToolArgumentLimits(
    max_query_chars=config.API_MAX_QUERY_CHARS,
    max_query_words=config.API_MAX_QUERY_WORDS,
    max_name_chars=config.API_MAX_NAME_CHARS,
    max_list_items=config.API_MAX_LIST_ITEMS,
    max_offset=config.API_MAX_OFFSET,
    find_max_top_k=config.API_FIND_MAX_TOP_K,
    list_max_top_k=config.API_LIST_MAX_TOP_K,
    protocols_max_top_k=config.API_PROTOCOLS_MAX_TOP_K,
    protocols_default_top_k=config.API_PROTOCOLS_DEFAULT_TOP_K,
    protocols_default_scopes=tuple(config.API_PROTOCOLS_DEFAULT_SCOPES),
)

AGENT_LIMITS = ToolArgumentLimits(
    max_query_chars=config.AGENT_MAX_QUERY_CHARS,
    max_query_words=config.AGENT_MAX_QUERY_WORDS,
    max_name_chars=config.AGENT_MAX_NAME_CHARS,
    max_list_items=config.AGENT_MAX_LIST_ITEMS,
    max_offset=config.AGENT_MAX_OFFSET,
    find_max_top_k=config.AGENT_FIND_MAX_TOP_K,
    list_max_top_k=config.AGENT_LIST_MAX_TOP_K,
    protocols_max_top_k=config.QUERY_PROTOCOLS_MAX_TOP_K,
    protocols_default_top_k=config.QUERY_PROTOCOLS_DEFAULT_TOP_K,
    protocols_default_scopes=PROTOCOL_SCOPES,
)


def _clamp(value: int | None, maximum: int) -> int | None:
    return None if value is None else max(1, min(value, maximum))


def _positive_or_none(value: int | None) -> int | None:
    """Zero or negative sizes mean "not given", so the default applies."""
    return value if value is not None and value > 0 else None


def _knesset_num_arg(args: dict) -> int:
    requested = valid.as_int(args.get("knesset_num"), "knesset_num")
    return valid.knesset_num(DEFAULT_KNESSET_NUM if requested is None else requested)


def _top_k_arg(args: dict, maximum: int, default: int | None = None) -> int | None:
    return _clamp(_positive_or_none(valid.as_int(args.get("top_k"), "top_k")) or default, maximum)


def _search_text_arg(args: dict, limits: ToolArgumentLimits) -> str:
    return valid.search_text(valid.as_text(args.get("query"), "query"), limits.max_query_chars)


def _find_args(args: dict, limits: ToolArgumentLimits) -> dict:
    return {"query": _search_text_arg(args, limits),
            "knesset_num": _knesset_num_arg(args), "top_k": _top_k_arg(args, limits.find_max_top_k)}


def _query_protocols_args(args: dict, limits: ToolArgumentLimits) -> dict:
    max_items = limits.max_list_items
    scopes = valid.as_text_list(args.get("search_in"), "search_in", split_commas=True, max_items=max_items)
    committees = [valid.name_filter(c, "committee", limits.max_name_chars)
                  for c in valid.as_text_list(args.get("committees"), "committee", split_commas=False,
                                              max_items=max_items)]
    meeting_ids = [valid.numeric_id(m, "meeting_id")
                   for m in valid.as_text_list(args.get("meeting_ids"), "meeting_id", split_commas=True,
                                               max_items=max_items)]
    return {
        "query":       valid.keyword_query(valid.as_text(args.get("query"), "query"),
                                           limits.max_query_chars, limits.max_query_words),
        "search_in":   list(dict.fromkeys(scopes)) or list(limits.protocols_default_scopes),
        "mk_id":       valid.numeric_id(valid.as_text(args.get("mk_id"), "mk_id"), "mk_id"),
        "party":       valid.name_filter(valid.as_text(args.get("party"), "party"), "party", limits.max_name_chars),
        "committees":  [c for c in committees if c] or None,
        "meeting_ids": meeting_ids or None,
        "date_from":   valid.iso_date(valid.as_text(args.get("date_from"), "date_from"), "date_from"),
        "date_to":     valid.iso_date(valid.as_text(args.get("date_to"), "date_to"), "date_to"),
        "sort":        valid.name_filter(valid.as_text(args.get("sort"), "sort"), "sort", limits.max_name_chars),
        "top_k":       _top_k_arg(args, limits.protocols_max_top_k, limits.protocols_default_top_k),
        "offset":      valid.offset(valid.as_int(args.get("offset"), "offset") or 0, limits.max_offset),
        "knesset_num": _knesset_num_arg(args),
    }


def _meeting_attendance_args(args: dict, limits: ToolArgumentLimits) -> dict:
    return {"meeting_id": valid.numeric_id(valid.as_text(args.get("meeting_id"), "meeting_id"), "meeting_id")}


def _query_bills_args(args: dict, limits: ToolArgumentLimits) -> dict:
    return {"query": _search_text_arg(args, limits),
            "knesset_num": _knesset_num_arg(args), "top_k": _top_k_arg(args, limits.list_max_top_k)}


def _get_bill_args(args: dict, limits: ToolArgumentLimits) -> dict:
    return {"bill_id": valid.numeric_id(valid.as_text(args.get("bill_id"), "bill_id"), "bill_id"),
            "include_text": valid.as_bool(args.get("include_text"), "include_text"),
            "max_chars": _clamp(_positive_or_none(valid.as_int(args.get("max_chars"), "max_chars")),
                                config.BILL_TEXT_MAX_MAX_CHARS),
            "knesset_num": _knesset_num_arg(args)}


def _query_votes_args(args: dict, limits: ToolArgumentLimits) -> dict:
    return {"query": _search_text_arg(args, limits),
            "mk_id": valid.numeric_id(valid.as_text(args.get("mk_id"), "mk_id"), "mk_id"),
            "knesset_num": _knesset_num_arg(args), "top_k": _top_k_arg(args, limits.list_max_top_k)}


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


def validated_tool_args(tool: str, args: dict, limits: ToolArgumentLimits = PUBLIC_API_LIMITS) -> dict:
    """Raises valid.ApiInputError (invalid_<arg>, or unknown_tool) on input outside limits."""
    if tool not in TOOL_ARGUMENT_VALIDATORS:
        raise valid.ApiInputError("unknown_tool", f"no tool named {tool!r}; list the tools to see their names")
    return TOOL_ARGUMENT_VALIDATORS[tool](args, limits)


def _integral_float_as_int(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _integral_floats_as_ints(args: dict) -> dict:
    """LLM function calls (Gemini's protobuf Struct) send every number as a float: 5.0 for 5, also in lists."""
    return {key: [_integral_float_as_int(item) for item in value] if isinstance(value, list)
            else _integral_float_as_int(value)
            for key, value in args.items()}


def agent_tool_args(tool: str, args: dict) -> dict:
    """AGENT_LIMITS validation for an LLM-generated call; None values are dropped so handler defaults apply."""
    if not isinstance(args, dict):
        raise valid.ApiInputError("invalid_arguments", "tool arguments must be a JSON object")
    validated = validated_tool_args(tool, _integral_floats_as_ints(args), AGENT_LIMITS)
    return {key: value for key, value in validated.items() if value is not None}
