"""Critic-pre and critic-post LLM helpers (design §6.3 / §6.6).

Each critic makes a single LLM call with structured JSON output.

Public surface:
  * :class:`CriticResult` — dataclass returned by both critics.
  * :func:`critic_pre`    — pre-execution review of a Plan.
  * :func:`critic_post`   — post-execution sufficiency check of evidence.

Both functions accept an injected ``llm_call`` so they can be unit-tested
without a real API key. The model name is sourced from
``config.CRITIC_PRE_MODEL`` / ``config.CRITIC_POST_MODEL``.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import config
from agent.plan_execute.plan import Plan
from agent.subgraph.evidence import EvidenceStore


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

_VALID_VERDICTS = {"ok", "revise", "replan"}


@dataclass
class CriticResult:
    """Outcome of a critic call.

    ``verdict`` is one of ``"ok"``, ``"revise"``, ``"replan"``. ``reason``
    is free-form text the planner sees on revise/replan; empty when the
    verdict is ``"ok"``.
    """

    verdict: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CriticResult":
        v = str(d.get("verdict", "")).strip().lower()
        if v not in _VALID_VERDICTS:
            v = "revise"
        return cls(verdict=v, reason=str(d.get("reason", "") or ""))


# ---------------------------------------------------------------------------
# Prompt loaders (cached at module import time)
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPT_CACHE: dict[str, str] = {}


def _load_prompt(name: str) -> str:
    if name not in _PROMPT_CACHE:
        path = _PROMPTS_DIR / name
        _PROMPT_CACHE[name] = path.read_text(encoding="utf-8")
    return _PROMPT_CACHE[name]


# ---------------------------------------------------------------------------
# Evidence summary view
# ---------------------------------------------------------------------------


def _summary_view(store: EvidenceStore | None) -> list[dict]:
    """Render the prompt-friendly summary view of the evidence store.

    The store may not implement a dedicated ``get_summary_view`` helper
    yet (Phase 4a/4b are written in parallel), so we walk ``iter()``
    directly. Heavy ``full`` payloads are intentionally omitted.
    """
    if store is None:
        return []
    out: list[dict] = []
    for entry in store.iter():
        env = entry.envelope
        out.append({
            "id":          entry.id,
            "tool_name":   entry.tool_name,
            "step_id":     entry.step_id,
            "summary":     env.summary or "",
            "metadata":    env.metadata or {},
            # "provenance":  env.provenance or {},
            "truncated":   bool(env.truncated),
            "error":       env.error,
        })
    return out


# ---------------------------------------------------------------------------
# JSON parser
# ---------------------------------------------------------------------------


def _parse_json(raw: object) -> Any:
    """Best-effort JSON parser — accepts dicts, fenced strings, raw strings."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[critics] JSON parse failed: {exc}  text={text[:120]!r}", file=sys.stderr, flush=True)
        return None


# ---------------------------------------------------------------------------
# Public API — critic_pre
# ---------------------------------------------------------------------------


def critic_pre(plan: Plan, llm_call: Callable) -> CriticResult:
    """Pre-execution critic. Reads the plan only — no evidence.

    Single LLM call against ``config.CRITIC_PRE_MODEL``. Returns a
    :class:`CriticResult` whose ``verdict`` is one of ``"ok"`` /
    ``"revise"`` / ``"replan"``.

    Args:
        plan: the Plan to critique.
        llm_call: callable used to invoke the critic model.
            Signature: ``llm_call(model: str, prompt: str,
            response_format=...) -> str | dict``.
    """
    template = _load_prompt("critic_pre.md")
    plan_json = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)
    prompt = template.format(
        goal=plan.goal,
        plan=plan_json,
        tool_catalogue="(provided to the planner; omitted here for brevity)",
        max_steps_v1=int(getattr(config, "RESEARCH_MAX_PLAN_STEPS_V1", 8)),
        max_deep_dives=int(getattr(config, "RESEARCH_MAX_DEEP_DIVES_PER_PLAN", 3)),
    )

    try:
        raw = llm_call(
            model=config.CRITIC_PRE_MODEL,
            prompt=prompt,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001 — surface, not crash
        return CriticResult(verdict="ok", reason=f"critic_pre LLM error: {exc}")

    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        return CriticResult(
            verdict="ok",
            reason="critic_pre returned non-JSON; defaulting to ok",
        )
    return CriticResult.from_dict(parsed)


# ---------------------------------------------------------------------------
# Public API — critic_post
# ---------------------------------------------------------------------------


def critic_post(
    plan: Plan,
    store: EvidenceStore,
    llm_call: Callable,
) -> CriticResult:
    """Post-execution critic. Reads plan + evidence summary view.

    Single LLM call against ``config.CRITIC_POST_MODEL``. Returns a
    :class:`CriticResult` whose ``verdict`` is one of ``"ok"`` /
    ``"revise"`` / ``"replan"``.

    Args:
        plan: the Plan as executed.
        store: the EvidenceStore populated by the executor loop.
        llm_call: callable used to invoke the critic model.
    """
    template = _load_prompt("critic_post.md")
    plan_json = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)
    view = _summary_view(store)
    view_json = json.dumps(view, ensure_ascii=False, indent=2)

    prompt = template.format(
        goal=plan.goal,
        plan=plan_json,
        evidence_view=view_json,
    )

    try:
        raw = llm_call(
            model=config.CRITIC_POST_MODEL,
            prompt=prompt,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        # On LLM error, default to "ok" so the synthesizer at least runs;
        # the runner can decide whether to surface the failure separately.
        return CriticResult(
            verdict="ok",
            reason=f"critic_post LLM error: {exc}",
        )

    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        return CriticResult(
            verdict="ok",
            reason="critic_post returned non-JSON; defaulting to ok",
        )
    return CriticResult.from_dict(parsed)


__all__ = ["CriticResult", "critic_pre", "critic_post"]
