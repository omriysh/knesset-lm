"""
agent.plan_execute

Phase-4a building blocks for the plan-and-execute research agent:
Plan/Step dataclasses, Scratchpad, BudgetTracker, DAGExecutor.

Out of scope here: validator, critics, executor, synthesizer (Phase 4b),
and the assembled PlanExecuteAgent.run (Phase 5).
"""

from agent.plan_execute.budget import (
    BudgetExceeded,
    BudgetTracker,
    estimate_plan_seconds,
)
from agent.plan_execute.concurrency import DAGExecutor
from agent.plan_execute.plan import (
    PLAN_JSON_SCHEMA,
    VALID_TASK_KINDS,
    Plan,
    Step,
)
from agent.plan_execute.scratchpad import Scratchpad

__all__ = [
    "BudgetExceeded",
    "BudgetTracker",
    "DAGExecutor",
    "PLAN_JSON_SCHEMA",
    "Plan",
    "Scratchpad",
    "Step",
    "VALID_TASK_KINDS",
    "estimate_plan_seconds",
]
