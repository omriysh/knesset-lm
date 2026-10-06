"""
Validation of research-tool arguments (RESEARCH_TOOL_REGISTRY), shared by every caller: the public
/v1 routes and MCP tools (PUBLIC_API_LIMITS) and the research agent's LLM-generated tool calls
(AGENT_LIMITS, applied in utils.tools.dispatch through ToolSpec.validate_args). Rejections raise
api.validation.ApiInputError (invalid_<arg>, or unknown_tool).
"""

import re
from dataclasses import dataclass

import config
from api import validation as valid
from retrieval.knesset_db_store import PROTOCOL_SCOPES

DEFAULT_KNESSET_NUM = max(config.PROTOCOL_KNESSET_NUMS)
_HEBREW_LETTER_RE = re.compile(r"[א-ת]")


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
    fixed_page_sizes: bool = False
    protocols_page_chars: int | None = None


PUBLIC_API_LIMITS = ToolArgumentLimits(
    max_query_chars=config.API_MAX_QUERY_CHARS,
    max_query_words=config.API_MAX_QUERY_WORDS,
    max_name_chars=config.API_MAX_NAME_CHARS,
    max_list_items=config.API_MAX_LIST_ITEMS,
    max_offset=config.API_MAX_OFFSET,
    find_max_top_k=config.API_FIND_PAGE_SIZE,
    list_max_top_k=config.API_LIST_PAGE_SIZE,
    protocols_max_top_k=config.QUERY_PROTOCOLS_MAX_TOP_K,
    protocols_default_top_k=config.QUERY_PROTOCOLS_DEFAULT_TOP_K,
    protocols_default_scopes=tuple(config.API_PROTOCOLS_DEFAULT_SCOPES),
    fixed_page_sizes=True,
    protocols_page_chars=config.API_PROTOCOLS_PAGE_CHARS,
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


def _optional_knesset_num_arg(args: dict) -> int | None:
    """OData searches: None searches every Knesset."""
    requested = valid.as_int(args.get("knesset_num"), "knesset_num")
    return None if requested is None else valid.knesset_num(requested)


def _protocol_knesset_num_arg(args: dict) -> int:
    knesset_num = _knesset_num_arg(args)
    if knesset_num not in config.PROTOCOL_KNESSET_NUMS:
        processed = ", ".join(str(k) for k in config.PROTOCOL_KNESSET_NUMS)
        raise valid.ApiInputError("invalid_knesset_num",
                                  f"only Knesset {processed} protocols are processed; knesset_num must be one of them")
    return knesset_num


def _top_k_arg(args: dict, maximum: int, default: int | None = None) -> int | None:
    return _clamp(_positive_or_none(valid.as_int(args.get("top_k"), "top_k")) or default, maximum)


def _search_text_arg(args: dict, limits: ToolArgumentLimits) -> str:
    return valid.search_text(valid.as_text(args.get("query"), "query"), limits.max_query_chars)


def _find_args(args: dict, limits: ToolArgumentLimits) -> dict:
    """find_mk / find_party read the OData roster of any Knesset."""
    return {"query": _search_text_arg(args, limits),
            "knesset_num": _knesset_num_arg(args), "top_k": _top_k_arg(args, limits.find_max_top_k)}


def _mk_id_or_name_arg(args: dict, limits: ToolArgumentLimits) -> str | None:
    """A Hebrew MK name without digits is resolved server-side (utils.tools._resolve_protocol_filters);
    anything else must be a numeric id."""
    mk_id = valid.as_text(args.get("mk_id"), "mk_id").strip()
    if _HEBREW_LETTER_RE.search(mk_id) and not any(ch.isdigit() for ch in mk_id):
        return valid.name_filter(mk_id, "mk_id", limits.max_name_chars)
    return valid.numeric_id(mk_id, "mk_id")


def _query_protocols_args(args: dict, limits: ToolArgumentLimits) -> dict:
    max_items = limits.max_list_items
    scopes = valid.as_text_list(args.get("search_in"), "search_in", split_commas=True, max_items=max_items)
    committees = [valid.name_filter(c, "committee", max(limits.max_name_chars, config.MAX_COMMITTEE_NAME_CHARS))
                  for c in valid.as_text_list(args.get("committees"), "committee", split_commas=False,
                                              max_items=max_items)]
    meeting_ids = [valid.meeting_id(m, "meeting_id")
                   for m in valid.as_text_list(args.get("meeting_ids"), "meeting_id", split_commas=True,
                                               max_items=max_items)]
    return {
        "query":       valid.keyword_query(valid.as_text(args.get("query"), "query"),
                                           limits.max_query_chars, limits.max_query_words),
        "search_in":   list(dict.fromkeys(scopes)) or list(limits.protocols_default_scopes),
        "mk_id":       _mk_id_or_name_arg(args, limits),
        "party":       valid.name_filter(valid.as_text(args.get("party"), "party"), "party", limits.max_name_chars),
        "committees":  [c for c in committees if c] or None,
        "meeting_ids": meeting_ids or None,
        "date_from":   valid.iso_date(valid.as_text(args.get("date_from"), "date_from"), "date_from"),
        "date_to":     valid.iso_date(valid.as_text(args.get("date_to"), "date_to"), "date_to"),
        "sort":        valid.name_filter(valid.as_text(args.get("sort"), "sort"), "sort", limits.max_name_chars),
        **_protocol_page_args(args, limits),
        "knesset_num": _protocol_knesset_num_arg(args),
    }


def _protocol_page_args(args: dict, limits: ToolArgumentLimits) -> dict:
    """Row offset for both; the page is top_k rows for the research agent and a character budget
    (page_chars) of whole rows for the public API, where top_k is not an argument."""
    if limits.protocols_page_chars:
        return {"page_chars": limits.protocols_page_chars,
                "offset": _offset_arg(args, config.API_PROTOCOLS_MAX_OFFSET)}
    return {"top_k": _top_k_arg(args, limits.protocols_max_top_k, limits.protocols_default_top_k),
            "offset": _offset_arg(args, limits.max_offset)}


def _meeting_attendance_args(args: dict, limits: ToolArgumentLimits) -> dict:
    return {"meeting_id": valid.meeting_id(valid.as_text(args.get("meeting_id"), "meeting_id"), "meeting_id")}


def _offset_arg(args: dict, maximum: int) -> int:
    return valid.offset(valid.as_int(args.get("offset"), "offset") or 0, maximum)


def _query_bills_args(args: dict, limits: ToolArgumentLimits) -> dict:
    initiator_role = valid.as_text(args.get("initiator_role"), "initiator_role").strip()
    return {"query": _search_text_arg(args, limits),
            "mk_id": valid.numeric_id(valid.as_text(args.get("mk_id"), "mk_id"), "mk_id"),
            "initiator_role": valid.one_of(initiator_role, ("initiator", "joined"), "initiator_role")
                              if initiator_role else None,
            "knesset_num": _optional_knesset_num_arg(args), "top_k": _top_k_arg(args, limits.list_max_top_k),
            "offset": _offset_arg(args, limits.max_offset)}


def _get_bill_args(args: dict, limits: ToolArgumentLimits) -> dict:
    """knesset_num is accepted but unused: bill ids are unique across Knessets."""
    return {"bill_id": valid.numeric_id(valid.as_text(args.get("bill_id"), "bill_id"), "bill_id"),
            "include_text": valid.as_bool(args.get("include_text"), "include_text"),
            "max_chars": _clamp(_positive_or_none(valid.as_int(args.get("max_chars"), "max_chars")),
                                config.BILL_TEXT_MAX_MAX_CHARS),
            "offset": _offset_arg(args, config.BILL_TEXT_MAX_OFFSET),
            "knesset_num": _optional_knesset_num_arg(args)}


def _query_votes_args(args: dict, limits: ToolArgumentLimits) -> dict:
    return {"query": _search_text_arg(args, limits),
            "mk_id": valid.numeric_id(valid.as_text(args.get("mk_id"), "mk_id"), "mk_id"),
            "knesset_num": _optional_knesset_num_arg(args), "top_k": _top_k_arg(args, limits.list_max_top_k),
            "offset": _offset_arg(args, limits.max_offset)}


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


def public_page_size(tool: str, validated_args: dict) -> int | None:
    """The fixed public page size (top_k) for a call; None for tools that return one record and for
    query_protocols, whose pages are a character budget of whole rows (page_chars)."""
    if tool in ("find_party", "find_committee") and not validated_args.get("query"):
        return config.API_FIND_LISTING_PAGE_SIZE
    if tool in ("find_mk", "find_committee"):
        return config.API_FIND_PAGE_SIZE
    if tool == "find_party":
        return config.API_FIND_PARTY_PAGE_SIZE
    if tool in ("query_bills", "query_votes"):
        return config.API_LIST_PAGE_SIZE
    return None


def validated_tool_args(tool: str, args: dict, limits: ToolArgumentLimits = PUBLIC_API_LIMITS) -> dict:
    """Raises valid.ApiInputError (invalid_<arg>, or unknown_tool) on input outside limits.
    With fixed_page_sizes (the public API) top_k is not an argument: the page size is set per call."""
    if tool not in TOOL_ARGUMENT_VALIDATORS:
        raise valid.ApiInputError("unknown_tool", f"no tool named {tool!r}; list the tools to see their names")
    if not limits.fixed_page_sizes:
        return TOOL_ARGUMENT_VALIDATORS[tool](args, limits)
    validated = TOOL_ARGUMENT_VALIDATORS[tool]({key: value for key, value in args.items() if key != "top_k"}, limits)
    if "top_k" in validated:
        validated["top_k"] = public_page_size(tool, validated)
    return validated


def _integral_float_as_int(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


def integral_floats_as_ints(args: dict) -> dict:
    """LLM function calls (Gemini's protobuf Struct) send every number as a float: 5.0 for 5, also in lists."""
    return {key: [_integral_float_as_int(item) for item in value] if isinstance(value, list)
            else _integral_float_as_int(value)
            for key, value in args.items()}


def agent_tool_args(tool: str, args: dict) -> dict:
    """AGENT_LIMITS validation for an LLM-generated call; None values are dropped so handler defaults apply."""
    if not isinstance(args, dict):
        raise valid.ApiInputError("invalid_arguments", "tool arguments must be a JSON object")
    validated = validated_tool_args(tool, integral_floats_as_ints(args), AGENT_LIMITS)
    return {key: value for key, value in validated.items() if value is not None}
