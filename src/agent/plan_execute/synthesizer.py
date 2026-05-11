"""Final-answer synthesizer (design §6.7).

Two entry points:

* :func:`synthesize`     — simple synchronous call (for scripts / tests).
* :func:`synthesize_gen` — generator variant that yields ``SubgraphEvent``
                           objects (llm_start / llm_done / llm_token) and
                           returns ``(answer_str, citations_list)``.  Used by
                           the agent loop so the UI can stream progress.

The generator supports an expand-then-synthesize loop:
  1. Offer the synthesizer LLM the ``expand`` pseudo-tool so it can fetch
     any evidence entry's full payload before writing the answer.
  2. After all expand calls finish (or the cap is hit), do a dedicated
     streaming synthesis call — no tools — so the final JSON answer is
     streamed token-by-token to the UI.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Generator

import config
from agent.plan_execute.plan import Plan
from agent.subgraph.compact import build_compact_view
from agent.subgraph.evidence import EvidenceStore


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
# Helpers
# ---------------------------------------------------------------------------


def _summary_view(store: EvidenceStore | None) -> list[dict]:
    return store.summary_view() if store is not None else []


def _compact_payloads(
    store: EvidenceStore | None,
    registry: Any,
) -> list[dict]:
    if store is None:
        return []
    by_name: dict = {}
    if registry:
        by_name = {spec.name: spec for spec in registry}
    out: list[dict] = []
    for entry in store.iter():
        if entry.tool_name == "expand":
            continue
        if entry.envelope.error and entry.envelope.error not in ("partial_error",):
            continue
        full_entry = store.get(entry.id)
        if full_entry is None:
            continue
        out.append({"id": entry.id, "tool_calls": build_compact_view(full_entry, by_name)})
    return out


def _build_prompt(
    query: str,
    plan: Plan,
    store: EvidenceStore | None,
    registry: Any,
) -> str:
    template = _load_prompt("synthesizer.md")
    goal = (plan.goal if plan and plan.goal else query) or ""
    plan_json = json.dumps(plan.to_dict() if plan else {}, ensure_ascii=False, indent=2)
    view_json = json.dumps(_summary_view(store), ensure_ascii=False, indent=2)
    compact_json = json.dumps(_compact_payloads(store, registry), ensure_ascii=False, indent=2)
    return (
        template
        .replace("{goal}", goal)
        .replace("{plan}", plan_json)
        .replace("{evidence_view}", view_json)
        .replace("{compact_payloads}", compact_json)
    )


def _parse_synthesizer_output(raw: str) -> tuple[str, list]:
    """Parse JSON from synthesizer LLM output → (answer, citations).

    Falls back to (raw, []) if JSON parsing fails so a broken synthesis
    still surfaces as plain text.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.rstrip()).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "answer" in obj:
            answer = str(obj.get("answer") or "")
            citations = obj.get("citations") or []
            if isinstance(citations, list):
                print(
                    f"[synthesizer] parsed ok: answer_len={len(answer)} citations={len(citations)}",
                    flush=True,
                )
                return answer, citations
            print(
                f"[synthesizer] 'citations' not a list (type={type(citations).__name__}); fallback",
                flush=True,
            )
        else:
            keys = list(obj.keys()) if isinstance(obj, dict) else type(obj).__name__
            print(f"[synthesizer] 'answer' key missing; keys={keys}; fallback", flush=True)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[synthesizer] JSON parse failed ({exc}); raw_len={len(raw)} "
            f"first_200={raw[:200]!r}",
            flush=True,
        )
    return raw, []


def _coerce_to_text(raw: object) -> str:
    """LLM responses come in a few shapes; pull out the prose (sync path)."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        if "content" in raw and isinstance(raw["content"], str):
            return raw["content"]
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return msg["content"]
        try:
            return json.dumps(raw, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[synthesizer] json.dumps failed on LLM response: {exc}", file=sys.stderr, flush=True)
            return str(raw)
    return str(raw) if raw is not None else ""


# ---------------------------------------------------------------------------
# Generator (streaming) entry point
# ---------------------------------------------------------------------------


def synthesize_gen(
    query: str,
    plan: Plan,
    store: EvidenceStore,
    llm_bridge: Any,
    *,
    registry: Any = None,
) -> Generator:
    """Yield SubgraphEvents and return (answer_str, citations_list).

    Uses an expand-then-synthesize pattern:

    1. **Expand loop** — call the LLM with ``tools=[EXPAND_TOOL_SCHEMA]`` so
       it can fetch any full evidence payloads it needs.  Up to
       ``config.SYNTHESIZER_MAX_EXPANDS`` (default 5) expand calls allowed.
       Uses ``llm_bridge.__call__`` (buffered events, drained & yielded after
       each turn).

    2. **Synthesis turn** — a dedicated ``llm_bridge.stream()`` call with NO
       tools so the model outputs the final JSON cleanly, token-by-token.
    """
    from agent.plan_execute.tools import EXPAND_TOOL_SCHEMA
    from agent.subgraph.base import SubgraphEvent  # noqa: F401 (re-exported)

    # Lazy imports to avoid circular deps at module load time.
    from agent.plan_execute.executor import (  # noqa: PLC0415
        _first_call_named,
        _parse_llm_response,
    )

    prompt = _build_prompt(query, plan, store, registry)
    n_view = len(_summary_view(store))
    n_compact = len(_compact_payloads(store, registry))
    print(
        f"[synthesizer] starting: evidence_entries={n_view} "
        f"compact_entries={n_compact} prompt_len={len(prompt)}",
        flush=True,
    )

    max_expands = int(getattr(config, "SYNTHESIZER_MAX_EXPANDS", 5))
    messages: list[dict] = [{"role": "user", "content": prompt}]
    expand_count = 0

    # ── Expand loop ──────────────────────────────────────────────────────
    while expand_count < max_expands:
        try:
            raw = llm_bridge(
                model=config.SYNTHESIZER_MODEL,
                messages=messages,
                tools=[EXPAND_TOOL_SCHEMA],
                phase="synthesizer:expand",
            )
        except Exception as exc:  # noqa: BLE001
            yield from llm_bridge.drain_events()
            yield SubgraphEvent(kind="hook", name="synthesizer_completed", payload={})
            return f"שגיאה בסינתזה (expand): {exc}", []

        yield from llm_bridge.drain_events()

        parsed = _parse_llm_response(raw)
        expand_call = _first_call_named(parsed.tool_calls, "expand")
        if expand_call is None:
            break  # model decided not to expand — proceed to synthesis

        ev_id = str(expand_call["arguments"].get("evidence_id") or "")
        full_payload = ""
        if store is not None and ev_id:
            entry = store.get(ev_id)
            if entry is not None:
                full_payload = entry.envelope.full or ""

        print(
            f"[synthesizer] expand {expand_count + 1}/{max_expands}: "
            f"ev_id={ev_id!r} payload_len={len(full_payload)}",
            flush=True,
        )

        raw_tcs: list[dict] = raw.get("tool_calls", []) if isinstance(raw, dict) else []
        asst_msg: dict = {"role": "assistant", "content": parsed.content or ""}
        if raw_tcs:
            asst_msg["tool_calls"] = raw_tcs
        messages.append(asst_msg)

        tc_id = raw_tcs[0].get("id") if raw_tcs else f"expand_{expand_count}"
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": full_payload or json.dumps({"error": f"evidence {ev_id!r} not found"}),
        })
        expand_count += 1

    # ── Synthesis turn (streaming, no tools) ─────────────────────────────
    print(f"[synthesizer] synthesis turn after {expand_count} expand(s)", flush=True)
    text_parts: list[str] = []
    error_msg: str = ""
    for sg_ev in llm_bridge.stream(
        model=config.SYNTHESIZER_MODEL,
        messages=messages,
        phase="synthesizer",
    ):
        if sg_ev.kind == "llm_token":
            text_parts.append(sg_ev.payload.get("text", ""))
        elif sg_ev.kind == "llm_done" and sg_ev.payload.get("error"):
            error_msg = sg_ev.payload["error"]
        yield sg_ev

    raw_output = "".join(text_parts)
    print(
        f"[synthesizer] done: raw_len={len(raw_output)} error={error_msg!r} "
        f"first_100={raw_output[:100]!r}",
        flush=True,
    )

    yield SubgraphEvent(kind="hook", name="synthesizer_completed", payload={"expand_count": expand_count})
    if error_msg:
        return f"שגיאה בסינתזה: {error_msg}", []
    return _parse_synthesizer_output(raw_output)


# ---------------------------------------------------------------------------
# Synchronous entry point (scripts / tests / legacy callers)
# ---------------------------------------------------------------------------


def synthesize(
    query: str,
    plan: Plan,
    store: EvidenceStore,
    llm_call: Callable,
    *,
    registry: Any = None,
) -> str:
    """Produce the final Hebrew answer string (synchronous, no streaming).

    Args:
        query:    the user's original question.
        plan:     the Plan as executed.
        store:    the EvidenceStore populated by the executor loop.
        llm_call: callable ``(model, prompt) -> str | dict``.
        registry: ToolRegistry used to look up ``compact_spec`` per tool.

    Returns:
        The Hebrew answer string.
    """
    prompt = _build_prompt(query, plan, store, registry)
    try:
        raw = llm_call(model=config.SYNTHESIZER_MODEL, prompt=prompt)
    except Exception as exc:  # noqa: BLE001
        return (
            "אירעה שגיאה ביצירת התשובה הסופית. "
            f"(synthesizer LLM error: {exc})"
        )
    answer, _citations = _parse_synthesizer_output(_coerce_to_text(raw))
    return answer


__all__ = ["synthesize", "synthesize_gen"]
