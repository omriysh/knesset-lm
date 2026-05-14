"""Legacy MachineRunner view of the tool registry.

Per design §5.2 / §12 (migration plan), the legacy ``knesset_agent.json``
state machine consumes tools through two contracts:

  1. an OpenAI-style JSON-schema list (``[{"type": "function", "function":
     {...}}, ...]``) describing the callable surface, and
  2. tool-call results expressed as JSON *strings* (the legacy runner
     stuffs them straight into ``role="tool"`` messages).

The new tool layer (``utils/tools.py``) speaks ``ToolEnvelope`` instead.
This module is the §12 migration shim that bridges the two:

  * :func:`list_tools_for_machine_runner` renders the registry as the
    JSON-schema list.
  * :func:`call_for_machine_runner` invokes :func:`utils.tools.dispatch`
    and serialises the returned :class:`ToolEnvelope` back to the JSON
    string the legacy runner expects.

This module is registry-agnostic; the caller (typically
``scripts/run_web.py`` when ``KNESSET_MACHINE`` points at the legacy
machine) chooses which registry to hand it.
"""

from __future__ import annotations

import json

from utils.tools import ToolRegistry, dispatch


# ---------------------------------------------------------------------------
# View builder
# ---------------------------------------------------------------------------


def list_tools_for_machine_runner(registry: ToolRegistry) -> list[dict]:
    """Render *registry* as an OpenAI-style tool-schema list.

    Each entry has the shape::

        {
          "type": "function",
          "function": {
            "name":        <spec.name>,
            "description": <spec.schema["description"]>,
            "parameters":  <spec.schema minus "description">,
          },
        }
    """
    out: list[dict] = []
    for spec in registry or []:
        schema = spec.schema or {}
        out.append({
            "type": "function",
            "function": {
                "name":        spec.name,
                "description": schema.get("description", "") or "",
                "parameters":  {k: v for k, v in schema.items() if k != "description"},
            },
        })
    return out


# ---------------------------------------------------------------------------
# Dispatch shim
# ---------------------------------------------------------------------------


def call_for_machine_runner(
    registry: ToolRegistry,
    name: str,
    args: dict,
) -> str:
    """Invoke a tool and return the legacy JSON-string contract.

    The legacy runner shoves the return value of a tool call straight into
    a ``role="tool"`` message, which the OpenAI tool-call API requires to
    be a string.  We ``json.dumps`` the envelope's ``to_dict()`` shape so
    the LLM sees every field (summary, full, metadata, provenance,
    truncated, error) and can reason about errors / warnings the same way
    the plan-execute executor does.

    Never raises: :func:`utils.tools.dispatch` already routes every error
    path through the envelope's ``error`` field, and the JSON serialisation
    falls back to ``default=str`` for any stragglers.
    """
    envelope = dispatch(registry, name, args or {})
    try:
        return json.dumps(envelope.to_dict(), ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 — final safety net for the legacy contract
        return json.dumps(
            {
                "summary":    "",
                "full":       "",
                "metadata":   {"kind": "error", "source": "shim", "count": 0},
                "provenance": {"tool_name": name},
                "truncated":  False,
                "error":      "envelope_serialisation_failed",
            },
            ensure_ascii=False,
        )


__all__ = [
    "list_tools_for_machine_runner",
    "call_for_machine_runner",
]
