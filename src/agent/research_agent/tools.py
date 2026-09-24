"""Research-domain tool registry.

This is the *only* module that knows which tools the research agent
exposes: a flat list of :class:`ToolSpec` entries whose handlers live in
:mod:`utils.tools`. The planner consumes it via the view-builders in
:mod:`agent.plan_execute.tools`.

The pseudo-tool ``expand`` is **not** in this list — it is dispatched by
the plan-execute graph itself, not via :func:`utils.tools.dispatch`.

Numeric defaults / minima / maxima are sourced from :mod:`config` so the
schema is never a second source of truth.
"""

from __future__ import annotations

import config
from retrieval.knesset_db_store import PROTOCOL_SCOPES
from utils.tools import (
    ToolSpec,
    handle_find_committee,
    handle_find_mk,
    handle_find_party,
    handle_get_bill,
    handle_get_meeting_attendance,
    handle_query_bills,
    handle_query_protocols,
    handle_query_votes,
)


# Each entry's ``schema`` is the JSON Schema fragment that goes under the
# ``parameters`` key in an OpenAI-style tool definition.

RESEARCH_TOOL_REGISTRY: list[ToolSpec] = [

    # ── Name resolution ───────────────────────────────────────────────────
    ToolSpec(
        name="find_mk",
        schema={
            "type": "object",
            "description": (
                "Resolve an MK name to one or more candidate records with "
                "stable mk_id. Each result includes a full profile: party and "
                "faction history, committee positions, ministerial roles. "
                "Returns the best fuzzy name matches sorted by score. "
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
        ui={"meta_note": "נתונים מאתר הכנסת"},
        compact_spec={
            "kind": "list",
            "max_items": 1,
            "item_spec": {
                "drop_fields": ["score"],
                "nested": {"profile": {"drop_fields": ["faction_chairpersons"]}},
            },
        },
    ),

    ToolSpec(
        name="find_committee",
        schema={
            "type": "object",
            "description": (
                "Resolve a committee name to candidate committee_id values. "
                "Each result includes the full committee record (its exact "
                "name, usable in query_protocols.committees) with its active "
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
        ui={"meta_note": "נתונים מאתר הכנסת"},
        compact_spec={
            "kind": "list",
            "max_items": 5,
            "item_spec": {
                "drop_fields": ["score", "extra", "fetched"],
                "nested": {"record": {"max_items_fields": {"members": 15}}},
            },
        },
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
                "or party-level analysis; the returned party name is the `party` "
                "filter of query_protocols."
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
        ui={"meta_note": "הרכב סיעה, מתוך אתר הכנסת"},
        compact_spec={
            "kind": "list",
            "max_items": 3,
            "item_spec": {"max_items_fields": {"members": 20}},
        },
    ),

    # ── Committee protocols (knesset.db) ──────────────────────────────────
    ToolSpec(
        name="query_protocols",
        schema={
            "type": "object",
            "description": (
                "Keyword search and listing over committee meeting protocols. Three "
                "scopes, each searched independently with the same query and filters "
                "and returned as its own list (top_k rows per scope):\n"
                "  • topics   — discussion topics from the meeting's AI summary\n"
                "  • opinions — positions a speaker stated, each with a verbatim "
                "quote from the protocol (mk_id / party of the speaker)\n"
                "  • speeches — the protocol transcript itself, one row per speech\n"
                "Query rules: a few plain Hebrew key words; all words must appear "
                "(AND); no OR/AND/NOT operators; one sub-topic per call. Spelling "
                "variants (ktiv male/haser) are matched automatically.\n"
                "An empty query lists rows instead of ranking them (newest meeting "
                "first, in-meeting order), so filters alone are a listing. Recipes:\n"
                "  • a meeting's summary: meeting_ids=[id], search_in=[\"topics\",\"opinions\"]\n"
                "  • read a meeting's transcript: meeting_ids=[id], search_in=[\"speeches\"]; "
                "page on with offset\n"
                "  • what does MK X think about Y: find_mk (or find_party) first, then "
                "query=Y, mk_id (or party), search_in=[\"opinions\"] (add \"speeches\" "
                "for more)\n"
                "  • which meetings discussed Y: query=Y, search_in=[\"topics\"]\n"
                "Filters are AND-ed; list filters (committees, meeting_ids) OR within "
                "themselves. mk_id/party mean: topics → meetings the MK/party attended; "
                "opinions → opinion author; speeches → speaker. Dates are YYYY-MM-DD, "
                "inclusive. Texts are returned in full."
            ),
            "properties": {
                "query": {
                    "type":        "string",
                    "description": "Hebrew key words; empty = list mode",
                },
                "search_in": {
                    "type":    "array",
                    "items":   {"type": "string", "enum": list(PROTOCOL_SCOPES)},
                    "default": list(PROTOCOL_SCOPES),
                },
                "mk_id":       {"type": "string", "description": "From find_mk"},
                "party":       {"type": "string", "description": "Party name as returned by find_party"},
                "committees":  {"type": "array", "items": {"type": "string"},
                                "description": "Committee names as returned by find_committee"},
                "meeting_ids": {"type": "array", "items": {"type": "string"}},
                "date_from":   {"type": "string", "format": "date"},
                "date_to":     {"type": "string", "format": "date"},
                "sort": {
                    "type":        "string",
                    "enum":        ["relevance", "date"],
                    "description": "Default: relevance with a query, date (newest first) without",
                },
                "top_k": {
                    "type":    "integer",
                    "default": config.QUERY_PROTOCOLS_DEFAULT_TOP_K,
                    "minimum": 1,
                    "maximum": config.QUERY_PROTOCOLS_MAX_TOP_K,
                },
                "offset":      {"type": "integer", "default": 0, "minimum": 0},
                "knesset_num": {"type": "integer", "default": 25},
            },
        },
        handler=handle_query_protocols,
        task_kinds=["discover", "filter", "fetch"],
        cost_hint="cheap",
        ui={
            "meta_note": "מתוך פרוטוקולי ועדות הכנסת וסיכומי AI שלהם",
            "enrich_fields": ["meeting_id"],
        },
        compact_spec={"kind": "dict"},
    ),

    ToolSpec(
        name="get_meeting_attendance",
        schema={
            "type": "object",
            "description": (
                "List who attended a committee meeting: MKs first (with mk_id and "
                "party), then guests (mk_id/party null), plus the meeting's "
                "committee and date."
            ),
            "properties": {
                "meeting_id": {"type": "string"},
            },
            "required": ["meeting_id"],
        },
        handler=handle_get_meeting_attendance,
        task_kinds=["fetch"],
        cost_hint="cheap",
        ui={
            "meta_note": "רשימת נוכחים מפרוטוקול ישיבת הוועדה",
            "enrich_fields": ["meeting_id"],
        },
        compact_spec={"kind": "dict"},
    ),

    # ── Bills (live Knesset OData) ────────────────────────────────────────
    ToolSpec(
        name="query_bills",
        schema={
            "type": "object",
            "description": (
                "Search bills by Hebrew title words (the whole query must appear in "
                "the bill name) within one Knesset, most recently updated first. "
                "Returns bill_id, name, status, type, initiators."
            ),
            "properties": {
                "query":       {"type": "string"},
                "knesset_num": {"type": "integer", "default": 25},
                "top_k":       {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
        },
        handler=handle_query_bills,
        task_kinds=["discover"],
        cost_hint="cheap",
        ui={"meta_note": "הצעות חוק, מנתוני אתר הכנסת"},
        compact_spec={
            "kind": "list",
            "max_items": 10,
            "item_spec": {"drop_fields": ["committee_id", "sub_type"]},
        },
    ),

    ToolSpec(
        name="get_bill",
        schema={
            "type": "object",
            "description": (
                "Fetch a bill by bill_id (from query_bills): status, type, "
                "initiators, document links. include_text=true also returns the "
                "extracted bill text, capped at max_chars — raise max_chars only "
                "when the bill text itself is the answer."
            ),
            "properties": {
                "bill_id":      {"type": "string"},
                "include_text": {"type": "boolean", "default": False},
                "max_chars": {
                    "type":    "integer",
                    "default": config.BILL_TEXT_DEFAULT_MAX_CHARS,
                    "minimum": config.BILL_TEXT_MIN_MAX_CHARS,
                    "maximum": config.BILL_TEXT_MAX_MAX_CHARS,
                },
                "knesset_num":  {"type": "integer", "default": 25},
            },
            "required": ["bill_id"],
        },
        handler=handle_get_bill,
        task_kinds=["fetch"],
        cost_hint="medium",
        ui={"meta_note": "פרטי הצעת חוק, מנתוני אתר הכנסת"},
        compact_spec={
            "kind": "dict",
            "drop_fields": ["documents"],
        },
    ),

    # ── Votes (live Knesset OData) ────────────────────────────────────────
    ToolSpec(
        name="query_votes",
        schema={
            "type": "object",
            "description": (
                "Plenum votes. Behaviour depends on which params are supplied:\n"
                "  query + mk_id → how that MK voted on each matching vote\n"
                "  mk_id only    → recent votes cast by the MK\n"
                "  query only    → votes whose title matches the keyword\n"
                "  neither       → most recent votes overall\n"
                "Use find_mk first to obtain mk_id."
            ),
            "properties": {
                "query":       {"type": "string"},
                "mk_id":       {"type": "string"},
                "knesset_num": {"type": "integer", "default": 25},
                "top_k":       {"type": "integer", "default": 20, "minimum": 1},
            },
        },
        handler=handle_query_votes,
        task_kinds=["discover", "fetch", "filter"],
        cost_hint="cheap",
        ui={"meta_note": "רשומות הצבעה ממאגרי הכנסת"},
        compact_spec={
            "kind": "list",
            "max_items": 20,
            "executor_selects": True,
            "item_spec": {"drop_fields": ["vote_id"]},
        },
    ),
]


__all__ = ["RESEARCH_TOOL_REGISTRY"]
