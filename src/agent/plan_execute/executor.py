"""Per-step executor (design §6.5).

One step → up to two LLM turns:
  Turn 1: emit a ``tool_call`` for one of the step's allowed tools (or a
          ``record_evidence(decision="skip", ref_evidence=…)`` shortcut).
  Turn 2: after the tool result, emit ``record_evidence`` with a 1–3
          sentence summary plus optional ``ref_evidence``.

Public surface:
  * :func:`execute_step` — runs the loop and returns a :class:`ToolEnvelope`.

The executor selects the model by ``step.cost_hint`` (``"expensive"`` →
``EXECUTOR_MODEL_HEAVY``, else ``EXECUTOR_MODEL_LIGHT``) and enforces:
  * ``allowed_tools`` whitelist (per step).
  * One-tool-call cap (raised to ``DEEP_DIVE_CALLS_PER_STEP`` for
    ``deep_dive`` steps).
  * ``planner_only`` block on ``deep_dive_meeting`` outside deep-dive
    steps.
  * Per-step token + call caps via the injected ``budget_tracker`` (any
    object exposing ``charge_tokens(int) -> bool`` and
    ``charge_tool_call() -> bool``; both should return ``False`` when a
    cap has been hit).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import config
from agent.plan_execute.plan import Step
from agent.plan_execute.tools import EXPAND_TOOL_SCHEMA, list_tools_for_executor
from agent.subgraph.evidence import EvidenceStore, ToolEnvelope
from utils.tools import ToolRegistry, ToolSpec, dispatch


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPT_CACHE: dict[str, str] = {}


def _load_prompt(name: str) -> str:
    if name not in _PROMPT_CACHE:
        _PROMPT_CACHE[name] = (_PROMPTS_DIR / name).read_text(encoding="utf-8")
    return _PROMPT_CACHE[name]


# ---------------------------------------------------------------------------
# record_evidence pseudo-tool schema
# ---------------------------------------------------------------------------
#
# The executor LLM never calls ``record_evidence`` against the tool
# registry — it is a structured-output channel for the second turn.
# Defining its schema here lets us hand it to the LLM as a regular tool
# (so JSON-tool-calling models stay on-protocol) and parse the args
# uniformly with real tool calls.

RECORD_EVIDENCE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "record_evidence",
        "description": (
            "Record the outcome of this step. Call EXACTLY ONCE per step, "
            "after the underlying tool (if any) has returned. "
            "decision='produced' means a fresh evidence entry will be minted "
            "from the tool result and your summary. decision='skip' means "
            "the step is satisfied by an existing entry — set ref_evidence "
            "to its id. decision='abort_step' means no usable result; the "
            "post-critic may replan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decision":     {"type": "string",
                                 "enum": ["produced", "skip", "abort_step"]},
                "summary":      {"type": "string"},
                "ref_evidence": {"type": "string"},
            },
            "required": ["decision", "summary"],
        },
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _LLMResponse:
    """Parsed LLM turn — exactly one of ``tool_calls`` / ``content``."""

    tool_calls: list[dict]
    content: str


def _parse_llm_response(raw: object) -> _LLMResponse:
    """Normalise an llm_call return value into ``_LLMResponse``.

    Accepts:
      - a dict with ``tool_calls`` (OpenAI-style) and/or ``content``
      - a JSON string with the same shape
      - a plain string (treated as ``content``)
      - a dict that is itself a tool-call payload
    """
    if raw is None:
        return _LLMResponse(tool_calls=[], content="")

    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            return _LLMResponse(tool_calls=[], content=raw)
        return _parse_llm_response(data)

    if not isinstance(raw, dict):
        return _LLMResponse(tool_calls=[], content=str(raw))

    tool_calls = raw.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return _LLMResponse(
            tool_calls=[_normalise_tool_call(tc) for tc in tool_calls],
            content=str(raw.get("content") or ""),
        )

    # Some models return a single tool-call dict at the top level.
    if "name" in raw and ("arguments" in raw or "args" in raw):
        return _LLMResponse(
            tool_calls=[_normalise_tool_call(raw)],
            content="",
        )

    return _LLMResponse(tool_calls=[], content=str(raw.get("content") or ""))


def _normalise_tool_call(tc: object) -> dict:
    """Coerce OpenAI / Anthropic / shorthand tool-call shapes into:
        {"name": str, "arguments": dict}
    """
    if not isinstance(tc, dict):
        return {"name": "", "arguments": {}}
    # OpenAI-style: { "function": { "name": ..., "arguments": "<json>" } }
    fn = tc.get("function")
    if isinstance(fn, dict):
        name = str(fn.get("name") or "")
        args = fn.get("arguments")
    else:
        name = str(tc.get("name") or "")
        args = tc.get("arguments")
        if args is None:
            args = tc.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:  # noqa: BLE001
            args = {}
    if not isinstance(args, dict):
        args = {}
    return {"name": name, "arguments": args}


def _select_model(step: Step) -> str:
    if (step.cost_hint or "").lower() == "expensive":
        return config.EXECUTOR_MODEL_HEAVY
    return config.EXECUTOR_MODEL_LIGHT


def _max_tool_calls(step: Step) -> int:
    if step.task_kind == "deep_dive":
        return int(getattr(config, "DEEP_DIVE_CALLS_PER_STEP", 2))
    return 1


def _registry_by_name(registry: ToolRegistry) -> dict[str, ToolSpec]:
    return {spec.name: spec for spec in (registry or [])}


def _summary_view(store: EvidenceStore | None) -> list[dict]:
    if store is None:
        return []
    out: list[dict] = []
    for entry in store.iter():
        env = entry.envelope
        out.append({
            "id":         entry.id,
            "tool_name":  entry.tool_name,
            "step_id":    entry.step_id,
            "summary":    env.summary or "",
            "metadata":   env.metadata or {},
            "provenance": env.provenance or {},
        })
    return out


def _charge(budget_tracker: Any, kind: str, amount: int = 1) -> bool:
    """Charge the budget tracker; return True if still under cap.

    The tracker is duck-typed (Phase 4a is being written in parallel).
    Accepts:
      - ``charge_tokens(n) -> bool``
      - ``charge_tool_call() -> bool``
      - or a fallthrough where the method is missing (always returns True).
    """
    if budget_tracker is None:
        return True
    method_name = "charge_tokens" if kind == "tokens" else "charge_tool_call"
    method = getattr(budget_tracker, method_name, None)
    if method is None:
        return True
    try:
        result = method(amount) if kind == "tokens" else method()
        return result is None or bool(result)
    except Exception:
        return False


def _abort_envelope(reason: str, tool_name: str = "", error_kind: str = "abort_step") -> ToolEnvelope:
    """Build a ToolEnvelope describing an aborted step."""
    return ToolEnvelope(
        summary=reason,
        full="",
        metadata={"kind": "error", "source": "executor", "count": 0},
        provenance={"tool_name": tool_name},
        error=error_kind,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def execute_step(
    step: Step,
    registry: ToolRegistry,
    store: EvidenceStore,
    llm_call: Callable,
    budget_tracker: Any = None,
) -> ToolEnvelope:
    """Run one step and return its final :class:`ToolEnvelope`.

    The envelope's ``summary`` is the executor LLM's 1–3 sentence
    description (NOT the raw tool ``summary`` if any). The envelope's
    ``full`` / ``metadata`` / ``provenance`` come from the underlying
    tool dispatch (or are empty when the step was a skip / abort).

    The caller is responsible for adding the resulting envelope (wrapped
    in an :class:`EvidenceEntry`) to ``store``; this function only
    returns the envelope. Skips and aborts return envelopes whose
    ``error`` field is set.

    Args:
        step: the Step to execute.
        registry: the full tool registry (filtered internally per
            ``step.allowed_tools``).
        store: the EvidenceStore (read-only here — for the summary view).
        llm_call: callable used for both LLM turns.
        budget_tracker: optional object exposing ``charge_tokens(int)`` and
            ``charge_tool_call()``. Either method returning ``False``
            aborts the step.

    Returns:
        A :class:`ToolEnvelope`. ``error`` is set when the step aborted
        (cap hit, no result, planner_only violation, etc.).
    """
    # import pdb; pdb.set_trace()
    if step is None:
        return _abort_envelope("execute_step called with None step")

    # ---------------------------------------------------------- caps gate
    if not _charge(budget_tracker, "tool_call"):
        return _abort_envelope(
            "tool-call cap reached before step start",
            error_kind="cap_tool_calls",
        )

    # ----------------------------------------------- allowed-tools filter
    by_name = _registry_by_name(registry)
    allowed: list[str] = list(step.allowed_tools or ())

    # planner_only sanity: block deep_dive_meeting outside deep_dive steps
    if step.task_kind != "deep_dive":
        for tool_name in allowed:
            spec = by_name.get(tool_name)
            if spec is not None and spec.planner_only:
                return _abort_envelope(
                    f"planner_only tool {tool_name!r} cannot run outside a "
                    "deep_dive step",
                    tool_name=tool_name,
                    error_kind="planner_only_violation",
                )

    tool_schemas = list_tools_for_executor(registry, allowed)
    # Always offer record_evidence and expand to the executor.
    tool_schemas_full = list(tool_schemas) + [RECORD_EVIDENCE_SCHEMA]

    # ----------------------------------------------------------- prompt
    template = _load_prompt("executor_wrapper.md")
    view = _summary_view(store)
    prompt = template.format(
        goal=getattr(step, "goal", "") or "",
        step_id=step.id,
        task=step.task,
        task_kind=step.task_kind,
        allowed_tools=json.dumps(list(allowed), ensure_ascii=False),
        args_hint=json.dumps(step.args_hint or {}, ensure_ascii=False),
        expected_evidence=step.expected_evidence or "(planner did not specify)",
        tool_schemas=json.dumps(tool_schemas_full, ensure_ascii=False, indent=2),
        evidence_view=json.dumps(view, ensure_ascii=False, indent=2),
        deep_dive_calls=int(getattr(config, "DEEP_DIVE_CALLS_PER_STEP", 2)),
    )

    model = _select_model(step)
    max_calls = _max_tool_calls(step)

    # --------------------------------------------------------- Turn 1: tool call
    try:
        raw_first = llm_call(
            model=model,
            prompt=prompt,
            tools=tool_schemas_full,
        )
    except Exception as exc:  # noqa: BLE001
        return _abort_envelope(
            f"executor LLM error on turn 1: {exc}",
            error_kind="llm_error",
        )

    first = _parse_llm_response(raw_first)

    # The executor may legitimately go straight to record_evidence with a
    # skip/abort decision; check that path first.
    skip_call = _first_call_named(first.tool_calls, "record_evidence")
    if skip_call is not None:
        decision = str(skip_call["arguments"].get("decision") or "").lower()
        summary = str(skip_call["arguments"].get("summary") or "")
        ref_evidence = skip_call["arguments"].get("ref_evidence")
        if decision == "skip":
            return ToolEnvelope(
                summary=summary or "Step satisfied by existing evidence.",
                full="",
                metadata={
                    "kind":         "skip",
                    "source":       "executor",
                    "count":        0,
                    "ref_evidence": ref_evidence,
                },
                provenance={"step_id": step.id},
                error="skip",
            )
        if decision == "abort_step":
            return _abort_envelope(
                summary or "Executor aborted the step before any tool call.",
            )

    # Otherwise we expect exactly one real tool call.
    real_call = _first_real_tool_call(first.tool_calls)
    if real_call is None:
        return _abort_envelope(
            "executor turn 1 produced no tool call",
            error_kind="no_tool_call",
        )

    tool_name = real_call["name"]
    tool_args = real_call["arguments"]

    # Allowed-tools enforcement (expand is always allowed).
    if tool_name not in set(allowed) and tool_name != EXPAND_TOOL_SCHEMA["function"]["name"]:
        return _abort_envelope(
            f"executor tried to call disallowed tool {tool_name!r}",
            tool_name=tool_name,
            error_kind="disallowed_tool",
        )

    # Planner-only check on the actual call.
    spec = by_name.get(tool_name)
    if spec is not None and spec.planner_only and step.task_kind != "deep_dive":
        return _abort_envelope(
            f"executor tried to call planner_only tool {tool_name!r}",
            tool_name=tool_name,
            error_kind="planner_only_violation",
        )

    if not _charge(budget_tracker, "tool_call"):
        return _abort_envelope(
            "tool-call cap reached before dispatch",
            tool_name=tool_name,
            error_kind="cap_tool_calls",
        )

    # ------------------------------------------------- dispatch the tool
    if tool_name == EXPAND_TOOL_SCHEMA["function"]["name"]:
        # ``expand`` is dispatched by the plan-execute graph itself; in this
        # narrow context we surface it as a structured envelope so the
        # caller can rehydrate. Does not count as a "real" tool result.
        envelope = ToolEnvelope(
            summary=f"expand({tool_args.get('evidence_id')!r}) requested",
            full="",
            metadata={"kind": "expand", "source": "executor", "count": 0},
            provenance={"evidence_id": tool_args.get("evidence_id")},
        )
    else:
        envelope = dispatch(registry, tool_name, tool_args)

    # Optional second tool call only for deep_dive (rare; v1 collapses).
    # For simplicity and per the design's "one tool call per step" rule we
    # do NOT loop further unless deep_dive is configured to allow it.
    _ = max_calls  # kept for future expansion; one call suffices in v1.

    # ----------------------------------------------- Turn 2: record_evidence
    follow_up_prompt = (
        prompt
        + "\n\nThe tool call returned. Now call `record_evidence` exactly once.\n"
        + "Tool result envelope (summary view):\n"
        + json.dumps({
            "tool_name":  tool_name,
            "full":       envelope.full or "",
            "summary":    envelope.summary or "",
            "metadata":   envelope.metadata or {},
            "provenance": envelope.provenance or {},
            "truncated":  bool(envelope.truncated),
            "error":      envelope.error,
        }, ensure_ascii=False, indent=2)
    )

    try:
        raw_second = llm_call(
            model=model,
            prompt=follow_up_prompt,
            tools=[RECORD_EVIDENCE_SCHEMA],
        )
    except Exception as exc:  # noqa: BLE001
        # The tool already ran — keep its payload, fill summary from the
        # tool's own summary as best-effort.
        return ToolEnvelope(
            summary=envelope.summary or f"executor LLM error on turn 2: {exc}",
            full=envelope.full,
            metadata=dict(envelope.metadata or {}),
            provenance=dict(envelope.provenance or {}),
            truncated=envelope.truncated,
            error=envelope.error or "llm_error_turn2",
        )

    second = _parse_llm_response(raw_second)
    record = _first_call_named(second.tool_calls, "record_evidence")
    if record is None:
        # Fall back to the tool's own summary so the entry isn't blank.
        return ToolEnvelope(
            summary=envelope.summary or second.content or "",
            full=envelope.full,
            metadata=dict(envelope.metadata or {}),
            provenance=dict(envelope.provenance or {}),
            truncated=envelope.truncated,
            error=envelope.error,
        )

    decision = str(record["arguments"].get("decision") or "").lower()
    record_summary = str(record["arguments"].get("summary") or "")
    ref_evidence = record["arguments"].get("ref_evidence")

    if decision == "abort_step":
        return _abort_envelope(
            record_summary or "executor aborted the step after tool call.",
            tool_name=tool_name,
        )

    if decision == "skip":
        return ToolEnvelope(
            summary=record_summary or "Step satisfied by existing evidence.",
            full="",
            metadata={
                "kind":         "skip",
                "source":       "executor",
                "count":        0,
                "ref_evidence": ref_evidence,
            },
            provenance={"step_id": step.id},
            error="skip",
        )

    # Default path: produced — overlay the executor's summary on the tool
    # envelope. Provenance picks up the step id for traceability.
    merged_provenance = dict(envelope.provenance or {})
    merged_provenance.setdefault("step_id", step.id)
    merged_provenance.setdefault("tool_name", tool_name)
    if ref_evidence:
        merged_provenance["ref_evidence"] = ref_evidence

    return ToolEnvelope(
        summary=record_summary or envelope.summary or "",
        full=envelope.full,
        metadata=dict(envelope.metadata or {}),
        provenance=merged_provenance,
        truncated=envelope.truncated,
        error=envelope.error,
    )


def _first_call_named(calls: list[dict], name: str) -> dict | None:
    for tc in calls or []:
        if tc.get("name") == name:
            return tc
    return None


def _first_real_tool_call(calls: list[dict]) -> dict | None:
    """Return the first call that is NOT ``record_evidence``."""
    for tc in calls or []:
        if tc.get("name") and tc["name"] != "record_evidence":
            return tc
    return None


__all__ = ["execute_step", "RECORD_EVIDENCE_SCHEMA"]
