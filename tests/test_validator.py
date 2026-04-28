"""
test_validator.py

Tests for validate_plan and ValidationResult.
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import config
from agent.plan_execute.plan import Plan, Step
from agent.plan_execute.validator import ValidationResult, validate_plan
from utils.tools import ToolSpec, ToolRegistry
from agent.subgraph.evidence import ToolEnvelope


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ok_llm_call(**kwargs) -> str:
    """Mock llm_call that always returns a valid 'ok' verdict JSON."""
    return json.dumps({"verdict": "ok", "reason": "name is specific enough"})


def _ambiguous_llm_call(**kwargs) -> str:
    """Mock llm_call that always returns 'ambiguous' verdict."""
    return json.dumps({"verdict": "ambiguous", "reason": "could be multiple people"})


def _make_tool(name: str, planner_only: bool = False, task_kinds=None) -> ToolSpec:
    return ToolSpec(
        name=name,
        schema={"type": "object", "properties": {}},
        handler=lambda args: ToolEnvelope(
            summary="", full="", metadata={"kind": "search", "source": "test", "count": 0},
            provenance={},
        ),
        task_kinds=task_kinds or ["discover"],
        cost_hint="cheap",
        planner_only=planner_only,
    )


def _make_registry(*tool_names, planner_only_tools=None) -> ToolRegistry:
    planner_only_tools = planner_only_tools or set()
    return [
        _make_tool(name, planner_only=(name in planner_only_tools))
        for name in tool_names
    ]


def _make_step(**kwargs) -> Step:
    defaults = dict(
        id="s1",
        task="Find an MK",
        task_kind="discover",
        allowed_tools=("find_mk",),
        deps=(),
        cost_hint="cheap",
    )
    defaults.update(kwargs)
    return Step(**defaults)


def _make_plan(steps, goal="Test goal") -> Plan:
    return Plan(goal=goal, steps=steps, version=1)


# ── ValidationResult ──────────────────────────────────────────────────────────

class TestValidationResult:
    def test_ok_true_no_issues(self):
        vr = ValidationResult(ok=True, issues=[])
        assert vr.ok is True
        assert vr.issues == []

    def test_ok_false_with_issues(self):
        vr = ValidationResult(ok=False, issues=["UNKNOWN_TOOL: s1: tool 'x' not in registry"])
        assert vr.ok is False
        assert len(vr.issues) == 1

    def test_roundtrip(self):
        vr = ValidationResult(ok=False, issues=["TEST_ISSUE: detail"])
        d = vr.to_dict()
        restored = ValidationResult.from_dict(d)
        assert restored.ok == vr.ok
        assert restored.issues == vr.issues


# ── validate_plan: valid plan ─────────────────────────────────────────────────

class TestValidPlanPasses:
    def test_valid_single_step_plan(self):
        registry = _make_registry("find_mk")
        steps = [_make_step(id="s1", allowed_tools=("find_mk",))]
        plan = _make_plan(steps)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert result.ok is True
        assert result.issues == []

    def test_valid_multi_step_plan(self):
        registry = _make_registry("find_mk", "search_topics")
        steps = [
            _make_step(id="s1", allowed_tools=("find_mk",)),
            _make_step(id="s2", task="Search", task_kind="filter",
                       allowed_tools=("search_topics",), deps=("s1",)),
        ]
        plan = _make_plan(steps)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert result.ok is True

    def test_valid_no_tools_analyze_step(self):
        registry = _make_registry("find_mk")
        steps = [
            _make_step(id="s1", allowed_tools=("find_mk",)),
            _make_step(id="s2", task_kind="analyze", allowed_tools=(), deps=("s1",)),
        ]
        plan = _make_plan(steps)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert result.ok is True

    def test_valid_plan_returns_validation_result(self):
        registry = _make_registry("find_mk")
        plan = _make_plan([_make_step()])
        result = validate_plan(plan, registry, _ok_llm_call)
        assert isinstance(result, ValidationResult)


# ── validate_plan: too many steps ─────────────────────────────────────────────

class TestTooManySteps:
    def test_too_many_steps_v1(self):
        max_steps = config.RESEARCH_MAX_PLAN_STEPS_V1
        registry = _make_registry("find_mk")
        # Create one more step than the cap
        steps = [
            _make_step(id=f"s{i+1}", task=f"Step {i+1}")
            for i in range(max_steps + 1)
        ]
        plan = Plan(goal="Overreach", steps=steps, version=1)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert result.ok is False
        assert any("OVERREACH_STEPS" in issue for issue in result.issues)

    def test_exactly_at_steps_cap_passes(self):
        max_steps = config.RESEARCH_MAX_PLAN_STEPS_V1
        registry = _make_registry("find_mk")
        steps = [
            _make_step(id=f"s{i+1}", task=f"Step {i+1}")
            for i in range(max_steps)
        ]
        plan = Plan(goal="At cap", steps=steps, version=1)
        result = validate_plan(plan, registry, _ok_llm_call)
        # Should not have OVERREACH_STEPS issue
        assert not any("OVERREACH_STEPS" in issue for issue in result.issues)


# ── validate_plan: too many deep dives ────────────────────────────────────────

class TestTooManyDeepDives:
    def test_too_many_deep_dives(self):
        max_dd = config.RESEARCH_MAX_DEEP_DIVES_PER_PLAN
        registry = _make_registry(
            "find_mk", "deep_dive_meeting",
            planner_only_tools={"deep_dive_meeting"},
        )
        steps = [
            _make_step(
                id=f"s{i+1}",
                task=f"Deep dive {i+1}",
                task_kind="deep_dive",
                allowed_tools=("deep_dive_meeting",),
            )
            for i in range(max_dd + 1)
        ]
        plan = Plan(goal="Too many deep dives", steps=steps, version=1)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert result.ok is False
        assert any("OVERREACH_DEEP_DIVES" in issue for issue in result.issues)

    def test_exactly_at_deep_dive_cap_passes(self):
        max_dd = config.RESEARCH_MAX_DEEP_DIVES_PER_PLAN
        registry = _make_registry(
            "find_mk", "deep_dive_meeting",
            planner_only_tools={"deep_dive_meeting"},
        )
        steps = [
            _make_step(
                id=f"s{i+1}",
                task=f"Deep dive {i+1}",
                task_kind="deep_dive",
                allowed_tools=("deep_dive_meeting",),
            )
            for i in range(max_dd)
        ]
        plan = Plan(goal="At deep dive cap", steps=steps, version=1)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert not any("OVERREACH_DEEP_DIVES" in issue for issue in result.issues)


# ── validate_plan: unknown tool ───────────────────────────────────────────────

class TestUnknownTool:
    def test_unknown_tool_returns_not_ok(self):
        registry = _make_registry("find_mk")  # does NOT contain "no_such_tool"
        steps = [_make_step(id="s1", allowed_tools=("no_such_tool",))]
        plan = _make_plan(steps)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert result.ok is False
        assert any("UNKNOWN_TOOL" in issue for issue in result.issues)

    def test_unknown_tool_issue_contains_step_id(self):
        registry = _make_registry("find_mk")
        steps = [_make_step(id="s99", allowed_tools=("phantom_tool",))]
        plan = _make_plan(steps)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert any("s99" in issue for issue in result.issues)


# ── validate_plan: planner_only violation ────────────────────────────────────

class TestPlannerOnlyViolation:
    def test_planner_only_tool_on_non_deep_dive_step(self):
        registry = _make_registry(
            "deep_dive_meeting",
            planner_only_tools={"deep_dive_meeting"},
        )
        steps = [
            _make_step(
                id="s1",
                task_kind="discover",  # NOT deep_dive
                allowed_tools=("deep_dive_meeting",),
            )
        ]
        plan = _make_plan(steps)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert result.ok is False
        assert any("PLANNER_ONLY" in issue for issue in result.issues)

    def test_planner_only_tool_on_deep_dive_step_passes(self):
        registry = _make_registry(
            "deep_dive_meeting",
            planner_only_tools={"deep_dive_meeting"},
        )
        steps = [
            _make_step(
                id="s1",
                task_kind="deep_dive",  # matches
                allowed_tools=("deep_dive_meeting",),
            )
        ]
        plan = _make_plan(steps)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert not any("PLANNER_ONLY" in issue for issue in result.issues)


# ── validate_plan: DAG cycle ──────────────────────────────────────────────────

class TestDAGCycle:
    def test_direct_cycle(self):
        """s1 depends on s2, s2 depends on s1 — cycle."""
        registry = _make_registry("find_mk")
        steps = [
            Step(id="s1", task="A", task_kind="discover",
                 allowed_tools=("find_mk",), deps=("s2",)),
            Step(id="s2", task="B", task_kind="discover",
                 allowed_tools=("find_mk",), deps=("s1",)),
        ]
        plan = _make_plan(steps)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert result.ok is False
        assert any("DAG_CYCLE" in issue for issue in result.issues)

    def test_three_step_cycle(self):
        """s1 → s2 → s3 → s1"""
        registry = _make_registry("find_mk")
        steps = [
            Step(id="s1", task="A", task_kind="discover",
                 allowed_tools=("find_mk",), deps=("s3",)),
            Step(id="s2", task="B", task_kind="discover",
                 allowed_tools=("find_mk",), deps=("s1",)),
            Step(id="s3", task="C", task_kind="discover",
                 allowed_tools=("find_mk",), deps=("s2",)),
        ]
        plan = _make_plan(steps)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert result.ok is False
        assert any("DAG_CYCLE" in issue for issue in result.issues)

    def test_linear_dag_no_cycle(self):
        """s1 → s2 → s3 — valid DAG, no cycle."""
        registry = _make_registry("find_mk")
        steps = [
            Step(id="s1", task="A", task_kind="discover",
                 allowed_tools=("find_mk",), deps=()),
            Step(id="s2", task="B", task_kind="filter",
                 allowed_tools=("find_mk",), deps=("s1",)),
            Step(id="s3", task="C", task_kind="analyze",
                 allowed_tools=(), deps=("s2",)),
        ]
        plan = _make_plan(steps)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert not any("DAG_CYCLE" in issue for issue in result.issues)


# ── validate_plan: missing dep ────────────────────────────────────────────────

class TestMissingDep:
    def test_missing_dep_returns_not_ok(self):
        registry = _make_registry("find_mk")
        steps = [
            # s2 refers to s99 which doesn't exist
            Step(id="s2", task="Filter", task_kind="filter",
                 allowed_tools=("find_mk",), deps=("s99",)),
        ]
        plan = _make_plan(steps)
        result = validate_plan(plan, registry, _ok_llm_call)
        assert result.ok is False
        assert any("MISSING_DEP" in issue for issue in result.issues)


# ── validate_plan: None plan ──────────────────────────────────────────────────

class TestNonePlan:
    def test_none_plan_returns_not_ok(self):
        registry = _make_registry("find_mk")
        result = validate_plan(None, registry, _ok_llm_call)
        assert result.ok is False
        assert len(result.issues) > 0
