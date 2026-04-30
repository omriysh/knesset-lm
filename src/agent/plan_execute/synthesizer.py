"""Final-answer synthesizer (design §6.7).

Single LLM call against ``config.SYNTHESIZER_MODEL`` that turns the
executed plan + evidence store into a Hebrew, footnoted answer.

Public surface:
  * :func:`synthesize` — returns the Hebrew answer string.

The function accepts an injected ``llm_call`` so it can be unit-tested
without a real API key.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import config
from agent.plan_execute.plan import Plan
from agent.subgraph.evidence import EvidenceStore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPT_CACHE: dict[str, str] = {}

# How many evidence entries we eagerly expand into ``expanded_payloads``.
# Defaults to a small number; the synthesizer prompt explains the field.
_DEFAULT_EXPAND_TOP_N = 5


def _load_prompt(name: str) -> str:
    if name not in _PROMPT_CACHE:
        _PROMPT_CACHE[name] = (_PROMPTS_DIR / name).read_text(encoding="utf-8")
    return _PROMPT_CACHE[name]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary_view(store: EvidenceStore | None) -> list[dict]:
    return store.summary_view() if store is not None else []


def _expanded_payloads(
    store: EvidenceStore | None,
    expand_top_n: int,
) -> list[dict]:
    """Pre-expand the most-cited evidence entries.

    "Most cited" is approximated as the first N non-error entries in
    insertion order (the executor adds them in step-completion order; v1
    leaves a smarter ranking for future work). Each returned dict contains
    the entry id and its full payload so the synthesizer can quote
    verbatim if needed.
    """
    if store is None or expand_top_n <= 0:
        return []
    out: list[dict] = []
    for entry in store.iter():
        if entry.envelope.error:
            continue
        full = store.get(entry.id).envelope.full if store.get(entry.id) else ""
        out.append({
            "id":   entry.id,
            "full": full or "",
        })
        if len(out) >= expand_top_n:
            break
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def synthesize(
    query: str,
    plan: Plan,
    store: EvidenceStore,
    llm_call: Callable,
    *,
    expand_top_n: int = _DEFAULT_EXPAND_TOP_N,
) -> str:
    """Produce the final Hebrew answer string.

    Args:
        query: the user's original question.
        plan: the Plan as executed.
        store: the EvidenceStore populated by the executor loop.
        llm_call: callable used to invoke the synthesizer model.
            Signature: ``llm_call(model: str, prompt: str, ...) -> str | dict``.
        expand_top_n: how many entries to pre-expand. Defaults to a small
            number to keep the prompt within budget.

    Returns:
        The Hebrew answer string. ``[ev_xxx]`` markers are inline; the
        UI renders them as clickable footnotes.
    """
    template = _load_prompt("synthesizer.md")

    # Use the user's literal query for the goal slot if the plan's goal is
    # missing or empty — defensive against half-built Plan objects.
    goal = (plan.goal if plan and plan.goal else query) or ""
    plan_json = json.dumps(plan.to_dict() if plan else {}, ensure_ascii=False, indent=2)
    view_json = json.dumps(_summary_view(store), ensure_ascii=False, indent=2)
    expanded_json = json.dumps(
        _expanded_payloads(store, expand_top_n),
        ensure_ascii=False,
        indent=2,
    )

    prompt = template.format(
        goal=goal,
        plan=plan_json,
        evidence_view=view_json,
        expanded_payloads=expanded_json,
    )

    try:
        raw = llm_call(
            model=config.SYNTHESIZER_MODEL,
            prompt=prompt,
        )
    except Exception as exc:  # noqa: BLE001 — produce a Hebrew failure message
        return (
            "אירעה שגיאה ביצירת התשובה הסופית. "
            f"(synthesizer LLM error: {exc})"
        )

    return _coerce_to_text(raw)


def _coerce_to_text(raw: object) -> str:
    """LLM responses come in a few shapes; pull out the prose."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        # OpenAI-style: {"content": "..."} or {"choices": [{"message": {"content": "..."}}]}
        if "content" in raw and isinstance(raw["content"], str):
            return raw["content"]
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return msg["content"]
        # Last-ditch: serialise the dict.
        try:
            return json.dumps(raw, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[synthesizer] json.dumps failed on LLM response: {exc}", file=sys.stderr, flush=True)
            return str(raw)
    return str(raw) if raw is not None else ""


__all__ = ["synthesize"]


# Suppress unused-import lint for Any — kept available for external typing.
_ = Any
