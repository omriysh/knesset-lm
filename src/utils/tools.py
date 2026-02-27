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
    get_committee_by_name,
    get_committee_members,
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
                        "description": "Full or partial name of the MK in Hebrew (e.g. 'איתמר בן גביר') or English."
                    },
                    "knesset_num": {
                        "type": "integer",
                        "description": "Knesset number to look up membership for. Defaults to 25.",
                        "default": 25
                    }
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_committee_info",
            "description": (
                "Look up a Knesset committee by name and return its members. "
                "Use this to understand who participates in a given committee."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Full or partial committee name in Hebrew (e.g. 'ועדת החוץ והביטחון')."
                    },
                    "knesset_num": {
                        "type": "integer",
                        "description": "Knesset number. Defaults to 25.",
                        "default": 25
                    }
                },
                "required": ["name"]
            }
        }
    },
]


# ── Dispatch ──────────────────────────────────────────────────────────────────

def dispatch(tool_name: str, args: dict) -> str:
    """
    Execute a tool call requested by the model.
    Returns a JSON string to send back as the tool result message.

    The model receives this string as the content of a { role: "tool" } message.
    Always returns a string — errors are returned as JSON so the model can
    handle them gracefully rather than crashing the conversation.
    """
    try:
        if tool_name == "get_mk_profile":
            result = get_mk_profile(
                name=args["name"],
                knesset_num=args.get("knesset_num", 25)
            )
            if result is None:
                return json.dumps({"error": f"No MK found matching '{args['name']}'"}, ensure_ascii=False)
            return json.dumps(result, ensure_ascii=False, default=str)

        elif tool_name == "get_committee_info":
            knesset_num = args.get("knesset_num", 25)
            committees = get_committee_by_name(args["name"], knesset_num)
            if not committees:
                return json.dumps({"error": f"No committee found matching '{args['name']}'"}, ensure_ascii=False)
            # Return members of the first (best) match
            committee = committees[0]
            members = get_committee_members(committee["CommitteeID"], knesset_num)
            return json.dumps({
                "committee_id":   committee["CommitteeID"],
                "committee_name": committee.get("Name", ""),
                "members":        members,
            }, ensure_ascii=False, default=str)

        else:
            return json.dumps({"error": f"Unknown tool: '{tool_name}'"})

    except Exception as e:
        return json.dumps({
            "error":   str(e),
            "details": traceback.format_exc()
        })
