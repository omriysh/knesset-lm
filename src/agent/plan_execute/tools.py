"""Plan-execute tool-surface view builders.

Architecture-generic helpers that convert any ToolRegistry into the
JSON-schema list each consumer needs, plus the ``expand`` pseudo-tool
definition (design §5.2).

Three exports:
  * EXPAND_TOOL_SCHEMA  — the ``expand`` pseudo-tool's OpenAI-style schema.
  * list_tools_for_planner(registry)          — all tools + expand.
  * list_tools_for_executor(registry, allowed) — filtered subset + expand.

Pure transformation — no I/O, no LLM calls.
"""

from __future__ import annotations

from utils.tools import ToolRegistry


# ---------------------------------------------------------------------------
# expand pseudo-tool schema
# ---------------------------------------------------------------------------

EXPAND_TOOL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "expand",
        "description": (
            "Request the full payload of a spilled EvidenceEntry by its ID. "
            "Use this when the executor summary references an evidence entry "
            "whose full content was not included inline."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
            },
            "required": ["evidence_id"],
        },
    },
}


# ---------------------------------------------------------------------------
# View builders
# ---------------------------------------------------------------------------


def list_tools_for_planner(registry: ToolRegistry) -> list[dict]:
    """Return all tools from *registry* (including ``planner_only=True`` ones)
    as OpenAI-style JSON-schema dicts, with the ``expand`` pseudo-tool
    appended at the end.

    Each tool's full schema comes from ``spec.schema`` which already carries
    the JSON Schema parameters block; we wrap it with the standard
    ``{"type": "function", "function": {...}}`` envelope expected by the LLM
    API.
    """
    result: list[dict] = []
    for spec in registry or []:
        result.append({
            "type": "function",
            "function": {
                "name":        spec.name,
                "description": spec.schema.get("description", ""),
                "parameters":  _parameters_from_schema(spec.schema),
            },
        })
    result.append(EXPAND_TOOL_SCHEMA)
    return result


def list_tools_for_executor(
    registry: ToolRegistry,
    allowed: list[str],
    allow_planner_only: bool = False,
) -> list[dict]:
    """Return the subset of *registry* whose ``name`` is in *allowed* AND
    whose ``planner_only`` flag is ``False`` (or *allow_planner_only* is
    ``True``), as OpenAI-style JSON-schema dicts, with the ``expand``
    pseudo-tool appended at the end.

    The planner assigns a concrete set of tool names to each executor step
    via the plan; this function enforces that only non-planner-only tools
    are ever handed to the executor — except for ``deep_dive`` steps, where
    the caller passes ``allow_planner_only=True``.
    """
    allowed_set = set(allowed or [])
    result: list[dict] = []
    for spec in registry or []:
        if spec.planner_only and not allow_planner_only:
            continue
        if spec.name not in allowed_set:
            continue
        result.append({
            "type": "function",
            "function": {
                "name":        spec.name,
                "description": spec.schema.get("description", ""),
                "parameters":  _parameters_from_schema(spec.schema),
            },
        })
    result.append(EXPAND_TOOL_SCHEMA)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parameters_from_schema(schema: dict) -> dict:
    """Extract the parameters-level JSON Schema from a ToolSpec ``schema``
    dict.

    ToolSpec.schema is stored as the raw object-level JSON Schema (i.e. it
    has ``"type": "object"`` at the top level, plus ``"properties"`` /
    ``"required"`` etc.).  For the OpenAI tool-call envelope the same object
    goes under the ``parameters`` key, minus the ``description`` which we
    hoist to the function level.  We make a shallow copy without
    ``description`` so the original spec dict is not mutated.
    """
    out = {k: v for k, v in schema.items() if k != "description"}
    return out


__all__ = [
    "EXPAND_TOOL_SCHEMA",
    "list_tools_for_planner",
    "list_tools_for_executor",
]
