"""
budget.py

Cost estimation and runtime budget tracking for plan-and-execute runs.

See: Documentation/KnessetLM/Development/Claude/plan-and-execute-design.md
     §4.1.1 (cost estimator) and §4.x (caps).
"""

from __future__ import annotations

from threading import Lock

from config import (
    COST_HINT_SECONDS,
    RESEARCH_MAX_LLM_TOKENS,
    RESEARCH_MAX_REPLANS,
    RESEARCH_MAX_TOOL_CALLS,
)

from agent.plan_execute.plan import Plan


# Re-export for callers that want the table without importing config directly.
__all__ = [
    "COST_HINT_SECONDS",
    "BudgetExceeded",
    "BudgetTracker",
    "estimate_plan_seconds",
]


def estimate_plan_seconds(plan: Plan) -> float:
    """Sum each step's `cost_hint` seconds. Pessimistic worst-case ceiling.

    Implementation matches design §4.1.1 exactly:

        return sum(COST_HINT_SECONDS.get(s.cost_hint, 30) for s in plan.steps)
    """
    return float(
        sum(COST_HINT_SECONDS.get(s.cost_hint, 30) for s in plan.steps)
    )


class BudgetExceeded(Exception):
    """Raised by BudgetTracker when any cap is hit. Carries (kind, used, cap)."""

    def __init__(self, kind: str, used: int, cap: int):
        super().__init__(
            f"BudgetExceeded: {kind} used={used} cap={cap}"
        )
        self.kind = kind
        self.used = used
        self.cap = cap


class BudgetTracker:
    """Thread-safe tracker for tokens, tool calls, and replans.

    Every charge_* method:
      1) increments its counter under a lock,
      2) raises BudgetExceeded if the counter is now > cap.

    Caps:
      tokens     -> config.RESEARCH_MAX_LLM_TOKENS
      tool_calls -> config.RESEARCH_MAX_TOOL_CALLS
      replans    -> config.RESEARCH_MAX_REPLANS
    """

    def __init__(
        self,
        max_tokens: int = RESEARCH_MAX_LLM_TOKENS,
        max_tool_calls: int = RESEARCH_MAX_TOOL_CALLS,
        max_replans: int = RESEARCH_MAX_REPLANS,
    ):
        self.max_tokens = int(max_tokens)
        self.max_tool_calls = int(max_tool_calls)
        self.max_replans = int(max_replans)

        self._tokens_used = 0
        self._tool_calls_made = 0
        self._replans_made = 0
        self._lock = Lock()

    # ── Read-only views ────────────────────────────────────────────────
    @property
    def tokens_used(self) -> int:
        return self._tokens_used

    @property
    def tool_calls_made(self) -> int:
        return self._tool_calls_made

    @property
    def replans_made(self) -> int:
        return self._replans_made

    # ── Charges ────────────────────────────────────────────────────────
    def charge_tokens(self, n: int) -> None:
        if n < 0:
            raise ValueError(f"charge_tokens: n must be >= 0, got {n}")
        with self._lock:
            self._tokens_used += int(n)
            if self._tokens_used > self.max_tokens:
                raise BudgetExceeded(
                    "tokens", self._tokens_used, self.max_tokens
                )

    def charge_tool_call(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError(f"charge_tool_call: n must be >= 0, got {n}")
        with self._lock:
            self._tool_calls_made += int(n)
            if self._tool_calls_made > self.max_tool_calls:
                raise BudgetExceeded(
                    "tool_calls", self._tool_calls_made, self.max_tool_calls
                )

    def charge_replan(self, n: int = 1) -> None:
        if n < 0:
            raise ValueError(f"charge_replan: n must be >= 0, got {n}")
        with self._lock:
            self._replans_made += int(n)
            if self._replans_made > self.max_replans:
                raise BudgetExceeded(
                    "replans", self._replans_made, self.max_replans
                )

    # ── Diagnostics ────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "tokens":     {"used": self._tokens_used,    "cap": self.max_tokens},
                "tool_calls": {"used": self._tool_calls_made, "cap": self.max_tool_calls},
                "replans":    {"used": self._replans_made,   "cap": self.max_replans},
            }
