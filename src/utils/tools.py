"""
tools.py

LLM tool definitions for KnessetLM.
Wraps knesset_db.py functions in the OpenAI/llama.cpp tools API format.

Two things live here:
  TOOLS      — the JSON schema list to pass in the `tools` field of your API request
  dispatch() — call this when the model returns a tool_calls response

Usage:
    from utils.tools import TOOLS, dispatch

    # In your API payload:
    payload = { ..., "tools": TOOLS, "tool_choice": "auto" }

    # When the model responds with tool_calls:
    for tool_call in response["choices"][0]["message"]["tool_calls"]:
        result = dispatch(tool_call["function"]["name"],
                          json.loads(tool_call["function"]["arguments"]))
        # append result to conversation and call the model again
"""

import json
import traceback
from utils.knesset_db import (
    get_mk_profile,
    get_active_committee_members_by_name,
    get_bill_details_by_name,
    get_bill_text_by_name,
)


# ── Tool Schemas ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_mk_profile",
            "description": (
                "Look up an Israeli MK (Member of Knesset / חבר כנסת) by name. "
                "Returns their party/faction, current positions, and committee memberships. "
                "Use this whenever you need to verify or enrich information about a speaker "
                "mentioned in a protocol — especially party affiliation, which you should "
                "never guess from memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Full or partial name of the MK in Hebrew (e.g. 'איתמר בן גביר') or English.",
                    },
                    "knesset_num": {
                        "type": "integer",
                        "description": "Knesset number to look up membership for. Defaults to 25.",
                        "default": 25,
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_committee_members",
            "description": (
                "Look up the current active members of a Knesset committee by name. "
                "Returns each member's full name and role (chair, deputy, member). "
                "Use this when a protocol mentions a committee and you need to know its composition."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Full or partial committee name in Hebrew (e.g. 'ועדת הכספים', 'ועדת החוץ והביטחון').",
                    },
                    "knesset_num": {
                        "type": "integer",
                        "description": "Knesset number. Defaults to 25.",
                        "default": 25,
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bill_details",
            "description": (
                "Look up a Knesset bill or law by its Hebrew name. "
                "Returns its current legislative status, type (private/government), "
                "initiators, and links to official documents. "
                "Use this when a protocol discusses a specific bill and you need context "
                "about its progress or who proposed it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bill_name": {
                        "type": "string",
                        "description": "Partial or full Hebrew name of the bill (e.g. 'חוק הביטוח הלאומי', 'תיקון מס').",
                    },
                },
                "required": ["bill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bill_text",
            "description": (
                "Fetch and extract the full text of a bill from its official PDF. "
                "Use this when you need the actual legal language and clauses of a bill "
                "being discussed in a protocol — not just its metadata. "
                "Only call this if get_bill_details was not enough and you need the content itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bill_name": {
                        "type": "string",
                        "description": "Partial or full Hebrew name of the bill.",
                    },
                    "knesset_num": {
                        "type": "integer",
                        "description": "Knesset number to narrow the search. Defaults to 25.",
                        "default": 25,
                    },
                },
                "required": ["bill_name"],
            },
        },
    },
]


# ── Dispatch ──────────────────────────────────────────────────────────────────

def dispatch(tool_name: str, args: dict) -> str:
    """
    Execute a tool call requested by the model.
    Returns a JSON string to send back as the tool result message.
    Always returns a string — errors are returned as JSON so the model can
    handle them gracefully rather than crashing the conversation.
    """
    try:
        if tool_name == "get_mk_profile":
            result = get_mk_profile(
                name=args["name"],
                knesset_num=args.get("knesset_num", 25),
            )
            if result is None:
                return json.dumps({"error": f"No MK found matching '{args['name']}'"}, ensure_ascii=False)
            return json.dumps(result, ensure_ascii=False, default=str)

        elif tool_name == "get_committee_members":
            result = get_active_committee_members_by_name(
                name=args["name"],
                knesset_num=args.get("knesset_num", 25),
            )
            if not result:
                return json.dumps({"error": f"No committee found matching '{args['name']}'"}, ensure_ascii=False)
            return json.dumps(result, ensure_ascii=False, default=str)

        elif tool_name == "get_bill_details":
            result = get_bill_details_by_name(args["bill_name"])
            if result is None:
                return json.dumps({"error": f"No bill found matching '{args['bill_name']}'"}, ensure_ascii=False)
            return json.dumps(result, ensure_ascii=False, default=str)

        elif tool_name == "get_bill_text":
            result = get_bill_text_by_name(
                bill_name=args["bill_name"],
                knesset_num=args.get("knesset_num", 25),
            )
            if result is None:
                return json.dumps({"error": f"Could not retrieve text for bill '{args['bill_name']}'"}, ensure_ascii=False)
            return json.dumps(result, ensure_ascii=False, default=str)

        else:
            return json.dumps({"error": f"Unknown tool: '{tool_name}'"})

    except Exception as e:
        return json.dumps({
            "error":   str(e),
            "details": traceback.format_exc(),
        })
