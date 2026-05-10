"""Research-domain tool registry.

This is the *only* module that knows which tools the research agent
exposes. Per design §5.2 / §5.3 the registry is a flat list of
:class:`ToolSpec` entries; the planner consumes it via the view-builders
in :mod:`agent.plan_execute.tools` (Phase 4c, out of scope for Phase 3b).

The pseudo-tool ``expand`` is **not** in this list — it is dispatched by
the plan-execute graph itself, not via :func:`utils.tools.dispatch`.

Schema policy
-------------
Numeric defaults / minima / maxima are sourced from :mod:`config` (per
design §5.3 note: "the schema is built from those constants at startup,
not as a second source of truth"). When config and the design text
disagree (e.g. ``BILL_TEXT_DEFAULT_MAX_CHARS`` is 1000 in both, but the
schema example shows 1000 as well — they currently agree), we always
follow config.
"""

from __future__ import annotations

import config
from utils.tools import (
    ToolSpec,
    handle_deep_dive_meeting,
    handle_find_bill,
    handle_find_committee,
    handle_find_mk,
    handle_find_party,
    handle_find_vote,
    handle_get_bill_details,
    handle_get_bill_text,
    handle_get_committee_sessions,
    handle_get_meeting_summary,
    handle_query_voting_records,
    handle_search_protocols_keyword,
    handle_search_topics,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
#
# Order: discovery → fetch → votes → deep. Matches the §5.3 inventory's
# layout so the planner prompt rendering is reading the list in the same
# logical order a human would.
#
# Each entry's ``schema`` is the JSON Schema fragment that goes under the
# ``parameters`` key in an OpenAI-style tool definition. View-builders in
# :mod:`agent.plan_execute.tools` are expected to wrap it with
# ``{"name": <spec.name>, "description": <…>, "parameters": <spec.schema>}``
# and prepend a ``"type": "function"`` envelope where required.

RESEARCH_TOOL_REGISTRY: list[ToolSpec] = [

    # ── Discovery / search ────────────────────────────────────────────────
    ToolSpec(
        name="search_topics",
        schema={
            "type": "object",
            "description": (
                "Discover meetings whose topical bullets match the query. "
                "Hybrid BM25 + embedding scored via Reciprocal Rank Fusion. "
                "Return up to top_k bullets with their meeting IDs."
            ),
            "properties": {
                "query":       {"type": "string"},
                "top_k": {
                    "type":    "integer",
                    "default": config.SEARCH_TOPICS_DEFAULT_TOP_K,
                    "minimum": 1,
                    "maximum": config.SEARCH_TOPICS_MAX_TOP_K,
                },
                "knesset_num": {"type": "integer", "default": 25},
            },
            "required": ["query"],
        },
        handler=handle_search_topics,
        task_kinds=["discover"],
        cost_hint="cheap",
        ui={
            "meta_note": "שורה מסיכום AI של ישיבת הוועדה",
            "enrich_fields": ["meeting_id"],
        },
    ),

    ToolSpec(
        name="search_protocols_keyword",
        schema={
            "type": "object",
            "description": (
                "BM25 keyword search over protocol speech text with optional "
                "filters by committee, meeting, speaker, or date range. "
                "Returns top_k speech-anchor hits with meeting+speaker context.\n"
                "IMPORTANT — query syntax rules:\n"
                "  • Write a plain Hebrew phrase (a few key words). All tokens "
                "are required to appear in the same speech — they are AND-ed.\n"
                "  • No boolean operators: do NOT write OR / AND / NOT. Those "
                "words are treated as literal tokens and will kill recall.\n"
                "  • Do NOT combine multiple questions into one query string. "
                "Use separate tool calls for each distinct sub-topic.\n"
                "  • Prefer the single most discriminative Hebrew term rather "
                "than a long sentence.\n"
                "Cost scales with top_k: top_k > 100 becomes expensive (BM25 "
                "over many MB of speeches); prefer narrowing via committee_ids "
                "/ speaker / date range before raising top_k."
            ),
            "properties": {
                "query":         {"type": "string"},
                "committee_ids": {"type": "array", "items": {"type": "string"}},
                "meeting_ids":   {"type": "array", "items": {"type": "string"}},
                "speaker":       {"type": "string"},
                "date_from":     {"type": "string", "format": "date"},
                "date_to":       {"type": "string", "format": "date"},
                "sort": {
                    "type":    "string",
                    "enum":    ["relevance", "recency"],
                    "default": "relevance",
                },
                "top_k": {
                    "type":    "integer",
                    "default": config.SEARCH_PROTOCOLS_DEFAULT_TOP_K,
                    "minimum": 1,
                    "maximum": config.SEARCH_PROTOCOLS_MAX_TOP_K,
                },
                "knesset_num": {"type": "integer", "default": 25},
            },
            "required": ["query"],
        },
        handler=handle_search_protocols_keyword,
        task_kinds=["filter", "discover"],
        cost_hint="cheap",
        ui={
            "meta_note": "קטע מפרוטוקול ישיבת הוועדה",
            "enrich_fields": ["meeting_id"],
        },
    ),

    ToolSpec(
        name="find_mk",
        schema={
            "type": "object",
            "description": (
                "Resolve an MK name to one or more candidate records with "
                "stable mk_id. Each result includes a full profile: party and "
                "faction history, committee positions, ministerial roles. "
                "Returns top BM25 matches sorted by score. "
                "No separate profile or committee-list fetch is needed after this call."
            ),
            "properties": {
                "query":       {"type": "string"},
                "knesset_num": {"type": "integer", "default": 25},
                "top_k":       {"type": "integer", "default": 5, "minimum": 1},
            },
            "required": ["query"],
        },
        handler=handle_find_mk,
        task_kinds=["discover", "fetch"],
        cost_hint="cheap",
        ui={"meta_note": "פרופיל חבר הכנסת (oknesset.org)"},
    ),

    ToolSpec(
        name="find_committee",
        schema={
            "type": "object",
            "description": (
                "Resolve a committee name to candidate committee_id values. "
                "Each result includes the full committee record with its active "
                "member list (mk_id, name, role). "
                "No separate member-list fetch is needed after this call."
            ),
            "properties": {
                "query":       {"type": "string"},
                "knesset_num": {"type": "integer", "default": 25},
                "top_k":       {"type": "integer", "default": 5, "minimum": 1},
            },
            "required": ["query"],
        },
        handler=handle_find_committee,
        task_kinds=["discover", "fetch"],
        cost_hint="cheap",
        ui={"meta_note": "נתוני ועדת הכנסת"},
    ),

    ToolSpec(
        name="find_bill",
        schema={
            "type": "object",
            "description": (
                "Resolve a bill name to candidate bill records by Hebrew "
                "title BM25 match."
            ),
            "properties": {
                "query":       {"type": "string"},
                "knesset_num": {"type": "integer", "default": 25},
                "top_k":       {"type": "integer", "default": 5, "minimum": 1},
            },
            "required": ["query"],
        },
        handler=handle_find_bill,
        task_kinds=["discover", "fetch"],
        cost_hint="cheap",
        ui={"meta_note": "נתוני הצעת חוק"},
    ),

    ToolSpec(
        name="find_vote",
        schema={
            "type": "object",
            "description": "Resolve a vote by title or topic to candidate vote_id values.",
            "properties": {
                "query":       {"type": "string"},
                "knesset_num": {"type": "integer", "default": 25},
                "top_k":       {"type": "integer", "default": 10, "minimum": 1},
            },
            "required": ["query"],
        },
        handler=handle_find_vote,
        task_kinds=["discover", "fetch"],
        cost_hint="cheap",
        ui={"meta_note": "נתוני הצבעה"},
    ),

    ToolSpec(
        name="find_party",
        schema={
            "type": "object",
            "description": (
                "Fuzzy-match a party/faction name and return all its members "
                "for a given Knesset. Returns up to top_k party matches, each "
                "with party name, seat count, and a list of {mk_id, full_name, "
                "is_current} members. Use when a question involves party composition "
                "or party-level analysis."
            ),
            "properties": {
                "query":       {"type": "string", "description": "Party or faction name (Hebrew)"},
                "knesset_num": {"type": "integer", "default": 25},
                "top_k":       {"type": "integer", "default": 3, "minimum": 1, "maximum": 5},
            },
            "required": ["query"],
        },
        handler=handle_find_party,
        task_kinds=["discover", "fetch"],
        cost_hint="cheap",
        ui={"meta_note": "הרכב סיעה"},
    ),

    # ── Fetch / data tools ────────────────────────────────────────────────

    # NOTE: get_mk_profile and get_mk_committees are intentionally absent —
    # find_mk already returns the full profile (party/faction history,
    # committee positions, govministries) in each candidate's `profile` field.
    # get_committee_members is also absent — find_committee already includes
    # the active member list in each candidate's `record` field.

    ToolSpec(
        name="get_meeting_summary",
        schema={
            "type": "object",
            "description": "Return the raw text summary for a single meeting.",
            "properties": {
                "meeting_id":  {"type": "string"},
                "section_num": {
                    "type":        "integer",
                    "description": "Optional 1-indexed section to return only one section.",
                    "minimum":     1,
                },
            },
            "required": ["meeting_id"],
        },
        handler=handle_get_meeting_summary,
        task_kinds=["fetch"],
        cost_hint="cheap",
        ui={
            "meta_note": "סיכום AI של ישיבת הוועדה",
            "enrich_fields": ["meeting_id"],
        },
    ),

    ToolSpec(
        name="get_committee_sessions",
        schema={
            "type": "object",
            "description": (
                "List sessions for a committee in a Knesset. Optional date "
                "range. Returns metadata only — no transcripts."
            ),
            "properties": {
                "committee_id": {"type": "string"},
                "knesset_num":  {"type": "integer", "default": 25},
                "date_from":    {"type": "string", "format": "date"},
                "date_to":      {"type": "string", "format": "date"},
            },
            "required": ["committee_id"],
        },
        handler=handle_get_committee_sessions,
        task_kinds=["fetch", "discover"],
        cost_hint="cheap",
        ui={
            "meta_note": "רשימת ישיבות ועדה",
            "enrich_fields": ["meeting_id"],
        },
    ),

    ToolSpec(
        name="get_bill_details",
        schema={
            "type": "object",
            "description": (
                "Fetch metadata for a bill. "
                "Returns status, type, initiators, document links. "
                "Use find_bill first to get bill_id."
            ),
            "properties": {
                "bill_id":     {"type": "string"},
                "knesset_num": {"type": "integer", "default": 25},
            },
            "required": ["bill_id"],
        },
        handler=handle_get_bill_details,
        task_kinds=["fetch"],
        cost_hint="cheap",
        ui={"meta_note": "פרטי הצעת חוק"},
    ),

    ToolSpec(
        name="get_bill_text",
        schema={
            "type": "object",
            "description": (
                "Fetch the extracted text of a bill PDF, capped at "
                "max_chars characters. The cap keeps this tool usable inside "
                "the plan-execute loop without blowing the executor's "
                "context; raise max_chars only when the bill text itself is "
                "the answer the user wants. Use find_bill first to get bill_id."
            ),
            "properties": {
                "bill_id":     {"type": "string"},
                "knesset_num": {"type": "integer", "default": 25},
                "max_chars": {
                    "type":    "integer",
                    "default": config.BILL_TEXT_DEFAULT_MAX_CHARS,
                    "minimum": config.BILL_TEXT_MIN_MAX_CHARS,
                    "maximum": config.BILL_TEXT_MAX_MAX_CHARS,
                },
            },
            "required": ["bill_id"],
        },
        handler=handle_get_bill_text,
        task_kinds=["fetch"],
        cost_hint="medium",
        ui={"meta_note": "טקסט הצעת חוק"},
    ),

    # ── Voting tools ──────────────────────────────────────────────────────
    ToolSpec(
        name="query_voting_records",
        schema={
            "type": "object",
            "description": (
                "Unified plenum vote query. Behaviour depends on which params are supplied:\n"
                "  topic + mk_id → how that MK voted on each matching vote\n"
                "  mk_id only   → recent votes cast by the MK\n"
                "  topic only   → vote metadata for votes matching the keyword\n"
                "  neither      → most recent votes overall\n"
                "Use find_mk first to obtain mk_id."
            ),
            "properties": {
                "topic":       {"type": "string"},
                "mk_id":       {"type": "string"},
                "knesset_num": {"type": "integer", "default": 25},
                "top_n":       {"type": "integer", "default": 20, "minimum": 1},
            },
        },
        handler=handle_query_voting_records,
        task_kinds=["discover", "fetch", "filter"],
        cost_hint="cheap",
        ui={"meta_note": "רשומות הצבעה"},
    ),

    # ── Deep-dive (planner-only) ──────────────────────────────────────────
    ToolSpec(
        name="deep_dive_meeting",
        schema={
            "type": "object",
            "description": (
                "Heavy analysis of a single meeting. mode='rerank' returns "
                "top reranked pass-1/pass-2 chunks for focus_query. "
                "mode='full' runs an LLM pass over the entire meeting "
                "(approximately 5 LLM calls budget). Allocated only by the "
                "planner — see §5.3.1."
            ),
            "properties": {
                "meeting_id":  {"type": "string"},
                "focus_query": {"type": "string"},
                "mode": {
                    "type":    "string",
                    "enum":    ["rerank", "full"],
                    "default": "rerank",
                },
            },
            "required": ["meeting_id", "focus_query"],
        },
        handler=handle_deep_dive_meeting,
        task_kinds=["deep_dive"],
        cost_hint="expensive",
        planner_only=True,
        ui={
            "meta_note": "ניתוח מעמיק של ישיבת הוועדה",
            "enrich_fields": ["meeting_id"],
        },
    ),
]


__all__ = ["RESEARCH_TOOL_REGISTRY"]
