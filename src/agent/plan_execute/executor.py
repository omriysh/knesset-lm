"""Per-step executor (design §6.5).

Each step runs a multi-turn LLM loop:
  - The executor may call up to MAX_TOOL_CALLS_PER_STEP real tools,
    one per turn, building a growing message history.
  - At any point the executor may call ``record_evidence`` to summarise
    and finish. After the loop cap is reached, a final turn forces
    ``record_evidence``.
  - A ``record_evidence(decision="skip")`` shortcut is allowed on the
    first turn when prior evidence already satisfies the step.

Public surface:
  * :func:`execute_step` — runs the loop and returns a :class:`ToolEnvelope`.
"""

from __future__ import annotations

import json
import re
import sys
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


def _truncate_tool_result(text: str, max_chars: int) -> str:
    """Truncate a tool result string without corrupting JSON.

    If the text fits, returns it unchanged.
    If it's a JSON array, keeps as many items as fit within max_chars and
    appends a truncation sentinel so the result stays valid JSON.
    Otherwise truncates the string and appends a [TRUNCATED] marker.
    """
    if len(text) <= max_chars:
        return text
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None

    if isinstance(parsed, list):
        kept: list = []
        sentinel_len = len('{"_truncated":true,"items_removed":99999}') + 2  # comma + item
        budget = max_chars - 2 - sentinel_len  # reserve for [] and sentinel
        running = 0
        for item in parsed:
            s = json.dumps(item, ensure_ascii=False)
            comma = 2 if kept else 0  # ", " between items
            if running + comma + len(s) <= budget:
                kept.append(item)
                running += comma + len(s)
            else:
                break
        removed = len(parsed) - len(kept)
        if removed > 0:
            kept.append({"_truncated": True, "items_removed": removed})
        return json.dumps(kept, ensure_ascii=False)

    # Non-list JSON or plain text: safe string truncation
    marker = " [TRUNCATED]"
    return text[: max_chars - len(marker)] + marker


# ---------------------------------------------------------------------------
# record_evidence pseudo-tool schema
# ---------------------------------------------------------------------------

RECORD_EVIDENCE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "record_evidence",
        "description": (
            "Early-exit only. Use ONLY when you can decide immediately, "
            "before calling any tool: "
            "decision='skip' — a prior evidence entry already satisfies the "
            "step (set ref_evidence to its id). "
            "decision='abort_step' — the step is impossible (no useful tool "
            "to call). "
            "Do NOT call this with decision='produced' — that is handled "
            "automatically after your tool calls finish."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decision":     {"type": "string",
                                 "enum": ["skip", "abort_step"]},
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
    tool_calls: list[dict]
    content: str


def _parse_llm_response(raw: object) -> _LLMResponse:
    if raw is None:
        return _LLMResponse(tool_calls=[], content="")

    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] JSON parse failed on LLM response: {exc}  text={text[:120]!r}", file=sys.stderr, flush=True)
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

    if "name" in raw and ("arguments" in raw or "args" in raw):
        return _LLMResponse(
            tool_calls=[_normalise_tool_call(raw)],
            content="",
        )

    return _LLMResponse(tool_calls=[], content=str(raw.get("content") or ""))


def _normalise_tool_call(tc: object) -> dict:
    if not isinstance(tc, dict):
        return {"name": "", "arguments": {}}
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
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] tool args JSON parse failed: {exc}  args={args[:120]!r}", file=sys.stderr, flush=True)
            args = {}
    if not isinstance(args, dict):
        args = {}
    return {"name": name, "arguments": args}


def _select_model(step: Step) -> str:
    if (step.cost_hint or "").lower() == "expensive":
        return config.EXECUTOR_MODEL_HEAVY
    return config.EXECUTOR_MODEL_LIGHT


def _max_tool_calls(step: Step) -> int:  # noqa: ARG001
    return int(getattr(config, "MAX_TOOL_CALLS_PER_STEP", 20))


def _registry_by_name(registry: ToolRegistry) -> dict[str, ToolSpec]:
    return {spec.name: spec for spec in (registry or [])}


def _summary_view(store: EvidenceStore | None) -> list[dict]:
    return store.summary_view() if store is not None else []


def _charge(budget_tracker: Any, kind: str, amount: int = 1) -> bool:
    if budget_tracker is None:
        return True
    method_name = "charge_tokens" if kind == "tokens" else "charge_tool_call"
    method = getattr(budget_tracker, method_name, None)
    if method is None:
        return True
    try:
        result = method(amount) if kind == "tokens" else method()
        return result is None or bool(result)
    except Exception as exc:
        print(f"[executor] budget charge ({kind}) raised: {exc}", file=sys.stderr, flush=True)
        return False


def _abort_envelope(reason: str, tool_name: str = "", error_kind: str = "abort_step") -> ToolEnvelope:
    return ToolEnvelope(
        summary=reason,
        full="",
        metadata={"kind": "error", "source": "executor", "count": 0},
        provenance={"tool_name": tool_name},
        error=error_kind,
    )


def _find_tc_id(raw_tool_calls: list[dict], tool_name: str) -> str:
    """Return the tool_call_id matching tool_name from the raw OpenAI-style list."""
    for tc in raw_tool_calls:
        fn = tc.get("function", {})
        if fn.get("name") == tool_name:
            return tc.get("id") or f"call_{tool_name}"
    return f"call_{tool_name}"


def _combine_envelopes(
    collected: list[tuple[str, ToolEnvelope]],
    step_id: str,
    call_args: list[dict] | None = None,
) -> ToolEnvelope:
    """Merge one or more (tool_name, envelope) pairs into a single ToolEnvelope."""
    if not collected:
        return _abort_envelope("no envelopes to combine", error_kind="no_tool_call")

    tool_calls_prov = list(call_args or [])

    if len(collected) == 1:
        name, env = collected[0]
        merged_prov = dict(env.provenance or {})
        merged_prov.setdefault("step_id", step_id)
        merged_prov.setdefault("tool_name", name)
        merged_prov["tool_calls"] = tool_calls_prov
        merged_prov["tool_call_results"] = [{
            "name":    name,
            "args":    call_args[0]["args"] if call_args else {},
            "summary": env.summary or "",
            "full":    env.full or "",
        }]
        return ToolEnvelope(
            summary=env.summary,
            full=env.full,
            metadata=dict(env.metadata or {}),
            provenance=merged_prov,
            truncated=env.truncated,
            error=env.error,
        )

    summaries = [f"[{name}] {env.summary}" for name, env in collected if env.summary]
    fulls = [f"=== {name} ===\n{env.full}" for name, env in collected if env.full]
    has_error = any(
        env.error and env.error not in ("skip",) for _, env in collected
    )

    return ToolEnvelope(
        summary="; ".join(summaries) if summaries else "",
        full="\n\n".join(fulls) if fulls else "",
        metadata={
            "kind":       "multi_tool",
            "source":     "executor",
            "count":      len(collected),
            "tool_names": [name for name, _ in collected],
        },
        provenance={
            "step_id":          step_id,
            "tool_names":       [name for name, _ in collected],
            "tool_name":        collected[-1][0] if collected else "",
            "tool_calls":       tool_calls_prov,
            "tool_call_results": [
                {
                    "name":    tool_name,
                    "args":    (call_args[i]["args"] if i < len(call_args) else {}),
                    "summary": env.summary or "",
                    "full":    env.full or "",
                }
                for i, (tool_name, env) in enumerate(collected)
            ],
        },
        truncated=any(env.truncated for _, env in collected),
        error="partial_error" if has_error else None,
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
    phase_prefix: str = "executor",
) -> ToolEnvelope:
    """Run one step and return its final :class:`ToolEnvelope`.

    Runs a multi-turn tool-call loop (up to MAX_TOOL_CALLS_PER_STEP), then
    a final ``record_evidence`` turn. Returns a merged envelope whose
    ``summary`` comes from the executor LLM and whose ``full`` / ``metadata``
    / ``provenance`` are combined from all dispatched tools.

    ``error`` is set when the step aborted (cap hit, no result, violation).
    """
    if step is None:
        return _abort_envelope("execute_step called with None step")

    # ----------------------------------------------- allowed-tools filter
    by_name = _registry_by_name(registry)
    allowed: list[str] = list(step.allowed_tools or ())

    tool_schemas = list_tools_for_executor(registry, allowed)
    tool_schemas_full = list(tool_schemas) + [RECORD_EVIDENCE_SCHEMA]

    # ----------------------------------------------------------- prompt
    template = _load_prompt("executor_wrapper.md")
    view = _summary_view(store)
    max_calls = _max_tool_calls(step)
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
        max_tool_calls=max_calls,
    )

    model = _select_model(step)
    messages: list[dict] = [{"role": "user", "content": prompt}]
    collected: list[tuple[str, ToolEnvelope]] = []
    call_args: list[dict] = []   # [{name, args}] — tracked in parallel with collected
    _turn = 0

    # ─── Tool call loop ──────────────────────────────────────────────────
    while len(collected) < max_calls:
        _turn += 1
        _phase = f"{phase_prefix}:{step.id}:t{_turn}"
        try:
            raw = llm_call(model=model, messages=messages, tools=tool_schemas_full,
                           phase=_phase)
        except Exception as exc:  # noqa: BLE001
            if not collected:
                return _abort_envelope(
                    f"executor LLM error: {exc}", error_kind="llm_error"
                )
            break  # have some results — proceed to record_evidence

        parsed = _parse_llm_response(raw)
        raw_tool_calls: list[dict] = (
            raw.get("tool_calls", []) if isinstance(raw, dict) else []
        )

        # Append assistant turn to history
        asst_msg: dict = {"role": "assistant", "content": parsed.content or ""}
        if raw_tool_calls:
            asst_msg["tool_calls"] = raw_tool_calls
        messages.append(asst_msg)

        # Check for record_evidence (skip / abort / produced early)
        skip_call = _first_call_named(parsed.tool_calls, "record_evidence")
        if skip_call is not None:
            decision = str(skip_call["arguments"].get("decision") or "").lower()
            summary = str(skip_call["arguments"].get("summary") or "")
            ref_evidence = skip_call["arguments"].get("ref_evidence")

            if decision == "skip":
                if not collected:
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
                # Tools already collected — fall through to record turn.

            if decision == "abort_step":
                if collected:
                    combined = _combine_envelopes(collected, step.id, call_args)
                    return ToolEnvelope(
                        summary=summary or "executor aborted after tool calls.",
                        full=combined.full,
                        metadata=combined.metadata,
                        provenance=combined.provenance,
                        truncated=combined.truncated,
                        error="abort_step",
                    )
                return _abort_envelope(summary or "Executor aborted the step.")

            if decision == "produced":
                # 'produced' is no longer a valid early-exit decision;
                # treat it as a no-op and fall through to normal tool loop.
                pass

        # Find the first real tool call
        real_call = _first_real_tool_call(parsed.tool_calls)
        if real_call is None:
            break  # no tool, no record_evidence — proceed to record turn

        tool_name = real_call["name"]
        tool_args = real_call["arguments"]

        # Allowed-tools enforcement
        expand_name = EXPAND_TOOL_SCHEMA["function"]["name"]
        if tool_name not in set(allowed) and tool_name != expand_name:
            tc_id = _find_tc_id(raw_tool_calls, tool_name)
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": json.dumps({"error": f"tool {tool_name!r} not in allowed list"}),
            })
            continue

        if not _charge(budget_tracker, "tool_call"):
            if not collected:
                return _abort_envelope(
                    "tool-call cap reached", error_kind="cap_tool_calls"
                )
            break

        # Dispatch
        if tool_name == expand_name:
            ev_id = tool_args.get("evidence_id", "")
            full_payload = ""
            if store is not None and isinstance(ev_id, str):
                entry = store.get(ev_id)
                if entry is not None:
                    full_payload = entry.envelope.full or ""
            envelope = ToolEnvelope(
                summary=f"Expanded evidence {ev_id!r}: {len(full_payload)} chars",
                full=full_payload,
                metadata={"kind": "expand", "source": "evidence_store",
                          "count": 1, "evidence_id": ev_id},
                provenance={"evidence_id": ev_id},
            )
        else:
            envelope = dispatch(registry, tool_name, tool_args)

        collected.append((tool_name, envelope))
        call_args.append({"name": tool_name, "args": dict(tool_args or {})})

        # Add tool result to messages (summary only — full stays in collected)
        tc_id = _find_tc_id(raw_tool_calls, tool_name)
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": json.dumps({
                "summary":  envelope.summary or "",
                "full":     _truncate_tool_result(envelope.full or "", config.EXECUTOR_TOOL_RESULT_CHARS),
                "error":    envelope.error,
            }, ensure_ascii=False),
        })

    # ─── End of tool loop ────────────────────────────────────────────────
    if not collected:
        return _abort_envelope(
            "executor produced no tool calls", error_kind="no_tool_call"
        )

    # ─── Record turn: structured JSON output (no tool schemas) ──────────
    record_prompt = _build_record_prompt(collected, by_name)
    messages_for_record = messages + [{"role": "user", "content": record_prompt}]

    try:
        raw_second = llm_call(
            model=model,
            messages=messages_for_record,
            tools=None,
            response_format={"type": "json_object"},
            phase=f"{phase_prefix}:{step.id}:record",
        )
    except Exception as exc:  # noqa: BLE001
        combined = _combine_envelopes(collected, step.id, call_args)
        return ToolEnvelope(
            summary=combined.summary or f"executor LLM error on record turn: {exc}",
            full=combined.full,
            metadata=combined.metadata,
            provenance=combined.provenance,
            truncated=combined.truncated,
            error=combined.error or "llm_error_turn2",
        )

    parsed_record = _parse_record_json(raw_second, len(collected))
    combined = _combine_envelopes(collected, step.id, call_args)

    if parsed_record["decision"] == "abort_step":
        return ToolEnvelope(
            summary=parsed_record["summary"] or "executor aborted after tool calls.",
            full=combined.full,
            metadata=combined.metadata,
            provenance=combined.provenance,
            truncated=combined.truncated,
            error="abort_step",
        )

    # produced
    merged_provenance = dict(combined.provenance or {})
    merged_provenance.setdefault("step_id", step.id)

    return ToolEnvelope(
        summary=parsed_record["summary"] or combined.summary or "",
        full=combined.full,
        metadata=combined.metadata,
        provenance=merged_provenance,
        truncated=combined.truncated,
        error=combined.error,
        compact_keys=parsed_record["tool_results"] or None,
    )


def _build_record_prompt(
    collected: list[tuple[str, ToolEnvelope]],
    by_name: dict[str, Any],
) -> str:
    """Build the record-turn user message requesting structured JSON output."""
    lines = [
        f"All {len(collected)} tool call(s) complete.",
        "Output ONLY a JSON object (no tool call). Structure:",
        "",
        "{",
        '  "decision": "produced",',
        '  "summary": "<1–3 sentence overall summary of all findings>",',
        '  "tool_results": [',
    ]
    for i, (tool_name, _) in enumerate(collected):
        spec = by_name.get(tool_name)
        cs = (spec.compact_spec if spec is not None else {}) or {}
        needs_indices = cs.get("executor_selects") and cs.get("kind") != "text"
        needs_quotes = cs.get("executor_selects") and cs.get("kind") == "text"
        comma = "," if i < len(collected) - 1 else ""
        list_path = cs.get("list_path")
        where = f'"{list_path}" array' if list_path else "result array"
        lines += [
            "    {",
            f'      "call_index": {i},',
            f'      "summary": "<what call {i} ({tool_name}) found>",',
            f'      "key_indices": {("[<0-based positions of the most relevant items in the " + where + ">]") if needs_indices else "null"},',
            f'      "key_quotes": {("[<verbatim passages most relevant to the task>]") if needs_quotes else "null"}',
            "    }" + comma,
        ]
    lines += [
        "  ]",
        "}",
        "",
        'If no useful result was obtained output: {"decision": "abort_step", "summary": "<reason>", "tool_results": []}',
    ]
    return "\n".join(lines)


def _parse_record_json(raw: object, n_calls: int) -> dict:
    """Parse the executor's structured JSON from the record turn.

    Returns a validated dict with keys: decision, summary, tool_results.
    Falls back gracefully on parse errors.
    """
    text: str = ""
    if isinstance(raw, str):
        text = raw.strip()
    elif isinstance(raw, dict):
        # Some backends return {"content": "..."} wrapper
        content = raw.get("content") or ""
        text = content if isinstance(content, str) else json.dumps(raw)

    # Strip markdown fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except Exception:
        return {"decision": "produced", "summary": str(raw)[:500], "tool_results": []}

    if not isinstance(data, dict):
        return {"decision": "produced", "summary": str(data)[:500], "tool_results": []}

    validated: list[dict] = []
    for item in (data.get("tool_results") or []):
        if not isinstance(item, dict):
            continue
        ci = item.get("call_index")
        if not isinstance(ci, int) or not (0 <= ci < n_calls):
            continue
        ki = item.get("key_indices")
        kq = item.get("key_quotes")
        validated.append({
            "call_index":  ci,
            "summary":     str(item.get("summary") or ""),
            "key_indices": [x for x in ki if isinstance(x, int)] if isinstance(ki, list) else None,
            "key_quotes":  [str(q) for q in kq] if isinstance(kq, list) else None,
        })

    return {
        "decision":     str(data.get("decision") or "produced").lower(),
        "summary":      str(data.get("summary") or "")[:1000],
        "tool_results": validated,
    }


def _first_call_named(calls: list[dict], name: str) -> dict | None:
    for tc in calls or []:
        if tc.get("name") == name:
            return tc
    return None


def _first_real_tool_call(calls: list[dict]) -> dict | None:
    for tc in calls or []:
        if tc.get("name") and tc["name"] != "record_evidence":
            return tc
    return None


__all__ = ["execute_step", "RECORD_EVIDENCE_SCHEMA"]
