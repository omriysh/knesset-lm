"""
test_budget.py

Tests for BudgetTracker, BudgetExceeded, and estimate_plan_seconds.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from agent.plan_execute.budget import BudgetExceeded, BudgetTracker, estimate_plan_seconds
from agent.plan_execute.plan import Plan, Step


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_step(id="s1", cost_hint="cheap", task_kind="discover") -> Step:
    return Step(id=id, task="Test task", task_kind=task_kind, cost_hint=cost_hint)


def _make_plan(steps) -> Plan:
    return Plan(goal="Test plan", steps=steps)


# ── BudgetTracker: tokens ─────────────────────────────────────────────────────

class TestBudgetTrackerTokens:
    def test_charge_tokens_under_cap(self):
        bt = BudgetTracker(max_tokens=100)
        bt.charge_tokens(50)
        assert bt.tokens_used == 50

    def test_charge_tokens_exactly_at_cap(self):
        bt = BudgetTracker(max_tokens=100)
        # At exactly cap: no exception (> cap, not >=)
        bt.charge_tokens(100)
        assert bt.tokens_used == 100

    def test_charge_tokens_exceeds_cap_raises(self):
        bt = BudgetTracker(max_tokens=100)
        bt.charge_tokens(100)
        with pytest.raises(BudgetExceeded) as exc_info:
            bt.charge_tokens(1)
        assert exc_info.value.kind == "tokens"
        assert exc_info.value.used == 101
        assert exc_info.value.cap == 100

    def test_charge_tokens_cumulative(self):
        bt = BudgetTracker(max_tokens=100)
        bt.charge_tokens(40)
        bt.charge_tokens(40)
        with pytest.raises(BudgetExceeded):
            bt.charge_tokens(21)

    def test_charge_tokens_negative_raises(self):
        bt = BudgetTracker(max_tokens=1000)
        with pytest.raises(ValueError):
            bt.charge_tokens(-1)

    def test_budget_exceeded_attributes(self):
        bt = BudgetTracker(max_tokens=10)
        bt.charge_tokens(10)
        with pytest.raises(BudgetExceeded) as exc_info:
            bt.charge_tokens(5)
        exc = exc_info.value
        assert exc.kind == "tokens"
        assert exc.used > 0
        assert exc.cap == 10


# ── BudgetTracker: tool calls ─────────────────────────────────────────────────

class TestBudgetTrackerToolCalls:
    def test_charge_tool_call_under_cap(self):
        bt = BudgetTracker(max_tool_calls=10)
        bt.charge_tool_call()
        assert bt.tool_calls_made == 1

    def test_charge_tool_call_at_cap(self):
        bt = BudgetTracker(max_tool_calls=2)
        bt.charge_tool_call()
        bt.charge_tool_call()
        assert bt.tool_calls_made == 2

    def test_charge_tool_call_exceeds_cap_raises(self):
        bt = BudgetTracker(max_tool_calls=2)
        bt.charge_tool_call()
        bt.charge_tool_call()
        with pytest.raises(BudgetExceeded) as exc_info:
            bt.charge_tool_call()
        assert exc_info.value.kind == "tool_calls"

    def test_charge_tool_call_negative_raises(self):
        bt = BudgetTracker(max_tool_calls=100)
        with pytest.raises(ValueError):
            bt.charge_tool_call(-1)

    def test_tool_call_n_greater_than_1(self):
        bt = BudgetTracker(max_tool_calls=5)
        bt.charge_tool_call(3)
        assert bt.tool_calls_made == 3


# ── BudgetTracker: replans ────────────────────────────────────────────────────

class TestBudgetTrackerReplans:
    def test_charge_replan_under_cap(self):
        bt = BudgetTracker(max_replans=3)
        bt.charge_replan()
        assert bt.replans_made == 1

    def test_charge_replan_at_cap(self):
        bt = BudgetTracker(max_replans=3)
        bt.charge_replan()
        bt.charge_replan()
        bt.charge_replan()
        assert bt.replans_made == 3

    def test_charge_replan_exceeds_cap_raises(self):
        bt = BudgetTracker(max_replans=3)
        bt.charge_replan()
        bt.charge_replan()
        bt.charge_replan()
        with pytest.raises(BudgetExceeded) as exc_info:
            bt.charge_replan()
        assert exc_info.value.kind == "replans"

    def test_charge_replan_negative_raises(self):
        bt = BudgetTracker(max_replans=10)
        with pytest.raises(ValueError):
            bt.charge_replan(-1)


# ── BudgetTracker: snapshot ───────────────────────────────────────────────────

class TestBudgetTrackerSnapshot:
    def test_snapshot_initial(self):
        bt = BudgetTracker(max_tokens=100, max_tool_calls=10, max_replans=3)
        snap = bt.snapshot()
        assert snap["tokens"]["used"] == 0
        assert snap["tokens"]["cap"] == 100
        assert snap["tool_calls"]["used"] == 0
        assert snap["replans"]["used"] == 0

    def test_snapshot_after_charges(self):
        bt = BudgetTracker(max_tokens=1000, max_tool_calls=50, max_replans=5)
        bt.charge_tokens(100)
        bt.charge_tool_call()
        bt.charge_replan()
        snap = bt.snapshot()
        assert snap["tokens"]["used"] == 100
        assert snap["tool_calls"]["used"] == 1
        assert snap["replans"]["used"] == 1


# ── estimate_plan_seconds ─────────────────────────────────────────────────────

class TestEstimatePlanSeconds:
    def test_returns_positive_float(self):
        steps = [_make_step("s1", "cheap")]
        plan = _make_plan(steps)
        result = estimate_plan_seconds(plan)
        assert isinstance(result, float)
        assert result > 0

    def test_cheap_step_is_5_seconds(self):
        steps = [_make_step("s1", "cheap")]
        result = estimate_plan_seconds(_make_plan(steps))
        assert result == 5.0

    def test_medium_step_is_30_seconds(self):
        steps = [_make_step("s1", "medium")]
        result = estimate_plan_seconds(_make_plan(steps))
        assert result == 30.0

    def test_expensive_step_is_120_seconds(self):
        steps = [_make_step("s1", "expensive")]
        result = estimate_plan_seconds(_make_plan(steps))
        assert result == 120.0

    def test_multi_step_sum(self):
        steps = [
            _make_step("s1", "cheap"),      # 5
            _make_step("s2", "medium"),     # 30
            _make_step("s3", "expensive"),  # 120
        ]
        result = estimate_plan_seconds(_make_plan(steps))
        assert result == 155.0

    def test_empty_plan(self):
        result = estimate_plan_seconds(_make_plan([]))
        assert result == 0.0

    def test_unknown_cost_hint_uses_default_30(self):
        # Design says: COST_HINT_SECONDS.get(s.cost_hint, 30) for unknown keys
        steps = [_make_step("s1", "unknown_hint")]
        result = estimate_plan_seconds(_make_plan(steps))
        assert result == 30.0
