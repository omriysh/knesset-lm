"""ResearchAgent — minimal PlanExecuteAgent subclass for the Knesset domain.

Phase 5 binds three pieces to the generic PlanExecuteAgent driver:
  * the research-specific tool registry (RESEARCH_TOOL_REGISTRY)
  * prompt addenda loaded from ``research_agent/prompts/*.md``
  * EVENTS / OUTPUT_VARS class attributes per design §3.2.3.

Construction does NOT require a Gemini API key: the model clients are
created lazily inside :meth:`PlanExecuteAgent.run`.
"""

from __future__ import annotations

from pathlib import Path

from agent.plan_execute.agent import PlanExecuteAgent
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from utils.tools import ToolRegistry


# ---------------------------------------------------------------------------
# Addendum loader
# ---------------------------------------------------------------------------


_PROMPTS_DIR = Path(__file__).parent / "prompts"


# Mapping from prompt-role keys (used by PlanExecuteAgent.prompt_addenda())
# to filenames under research_agent/prompts/. Only roles for which we ship
# an addendum file appear here; missing roles fall through with no addendum.
_ADDENDUM_FILES: dict[str, str] = {
    "planner":     "planner_addendum.md",
    "critic_pre":  "critic_pre_addendum.md",
    "synthesizer": "synthesizer_addendum.md",
}


def _load_addenda() -> dict[str, str]:
    """Load every addendum file shipped under ``research_agent/prompts/``.

    Returns a ``{role: text}`` dict suitable for
    :meth:`PlanExecuteAgent.prompt_addenda`. Files that are missing or
    unreadable are silently skipped so a partial install does not crash
    the agent at import time.
    """
    out: dict[str, str] = {}
    for role, filename in _ADDENDUM_FILES.items():
        path = _PROMPTS_DIR / filename
        try:
            out[role] = path.read_text(encoding="utf-8")
        except OSError:
            # Missing file → skip; the generic prompt still works alone.
            continue
    return out


# ---------------------------------------------------------------------------
# ResearchAgent
# ---------------------------------------------------------------------------


class ResearchAgent(PlanExecuteAgent):
    """Plan-and-execute agent bound to the Israeli Knesset domain."""

    # Hook names this subgraph may yield. The outer state machine wires
    # any subset of these via the ``data.hooks`` field on its subgraph
    # node; unwired hooks fall back to default behaviour (auto-approve
    # / no-op) per design §3.2.3.
    EVENTS = {"cost_estimate_required", "plan_ready", "step_completed"}

    # Context keys the agent writes when it yields ``done``. Matches the
    # outer SM's ``data.output_vars`` for the research subgraph node.
    OUTPUT_VARS = ["final_answer", "footnotes"]

    def tool_registry(self) -> ToolRegistry:
        return RESEARCH_TOOL_REGISTRY

    def prompt_addenda(self) -> dict[str, str]:
        return _load_addenda()


__all__ = ["ResearchAgent"]
