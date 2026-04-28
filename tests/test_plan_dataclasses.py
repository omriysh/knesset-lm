"""
test_plan_dataclasses.py

Tests for Plan, Step dataclasses and VALID_TASK_KINDS.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from agent.plan_execute.plan import Plan, Step, VALID_TASK_KINDS


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_step(**kwargs) -> Step:
    defaults = dict(
        id="s1",
        task="Find MK Netanyahu",
        task_kind="discover",
        allowed_tools=("find_mk",),
        args_hint={"query": "נתניהו"},
        deps=(),
        replan_after=False,
        expected_evidence="MK record for Netanyahu",
        cost_hint="cheap",
    )
    defaults.update(kwargs)
    return Step(**defaults)


def _make_plan(**kwargs) -> Plan:
    steps = [_make_step()]
    defaults = dict(goal="Test goal", steps=steps, version=1, notes="")
    defaults.update(kwargs)
    return Plan(**defaults)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestStepRoundtrip:
    def test_step_roundtrip(self):
        s = _make_step()
        restored = Step.from_dict(s.to_dict())
        assert restored == s

    def test_step_roundtrip_minimal(self):
        s = Step(id="s2", task="Analyze", task_kind="analyze")
        restored = Step.from_dict(s.to_dict())
        assert restored == s

    def test_step_roundtrip_with_all_fields(self):
        s = Step(
            id="s3",
            task="Deep dive",
            task_kind="deep_dive",
            allowed_tools=("deep_dive_meeting",),
            args_hint={"meeting_id": "42", "focus_query": "welfare"},
            deps=("s1", "s2"),
            replan_after=True,
            expected_evidence="Key speeches about welfare",
            cost_hint="expensive",
        )
        restored = Step.from_dict(s.to_dict())
        assert restored == s

    def test_step_to_dict_types(self):
        s = _make_step()
        d = s.to_dict()
        assert isinstance(d["allowed_tools"], list)
        assert isinstance(d["deps"], list)
        assert isinstance(d["replan_after"], bool)

    def test_step_from_dict_converts_tuples(self):
        d = {
            "id": "s1",
            "task": "Find MK",
            "task_kind": "discover",
            "allowed_tools": ["find_mk"],
            "deps": ["s0"],
        }
        s = Step.from_dict(d)
        assert isinstance(s.allowed_tools, tuple)
        assert isinstance(s.deps, tuple)

    def test_step_args_hint_none_roundtrip(self):
        s = Step(id="s1", task="Analyze", task_kind="analyze", args_hint=None)
        restored = Step.from_dict(s.to_dict())
        assert restored.args_hint is None


class TestPlanRoundtrip:
    def test_plan_roundtrip(self):
        p = _make_plan()
        restored = Plan.from_dict(p.to_dict())
        assert restored.goal == p.goal
        assert restored.version == p.version
        assert restored.notes == p.notes
        assert len(restored.steps) == len(p.steps)
        assert restored.steps[0] == p.steps[0]

    def test_plan_roundtrip_multi_step(self):
        steps = [
            _make_step(id="s1"),
            _make_step(id="s2", task="Keyword search", task_kind="filter",
                       allowed_tools=("search_protocols_keyword",), deps=("s1",),
                       cost_hint="medium"),
        ]
        p = Plan(goal="Multi step goal", steps=steps, version=2, notes="some notes")
        restored = Plan.from_dict(p.to_dict())
        assert len(restored.steps) == 2
        assert restored.steps[1].deps == ("s1",)

    def test_plan_roundtrip_empty_steps(self):
        p = Plan(goal="Empty plan", steps=[], version=1, notes="")
        restored = Plan.from_dict(p.to_dict())
        assert restored.goal == "Empty plan"
        assert restored.steps == []

    def test_plan_from_dict_defaults(self):
        d = {"goal": "Minimal plan"}
        p = Plan.from_dict(d)
        assert p.goal == "Minimal plan"
        assert p.version == 1
        assert p.notes == ""
        assert p.steps == []


class TestPlanReplan:
    def test_replan_append_only(self):
        p = _make_plan()
        original_len = len(p.steps)
        new_step = _make_step(id="s2", task="Second step", task_kind="filter")
        p.replan([new_step])
        assert len(p.steps) == original_len + 1
        assert p.version == 2
        assert p.steps[-1].id == "s2"

    def test_replan_bumps_version(self):
        p = _make_plan(version=1)
        p.replan([_make_step(id="s2", task="Extra step", task_kind="analyze")])
        assert p.version == 2
        p.replan([_make_step(id="s3", task="Another step", task_kind="fetch")])
        assert p.version == 3

    def test_replan_duplicate_id_raises(self):
        p = _make_plan()
        # s1 already exists
        duplicate = _make_step(id="s1", task="Duplicate", task_kind="fetch")
        with pytest.raises(ValueError, match="s1"):
            p.replan([duplicate])

    def test_replan_preserves_old_steps(self):
        p = _make_plan()
        original_step = p.steps[0]
        new_step = _make_step(id="s2", task="New", task_kind="analyze")
        p.replan([new_step])
        assert p.steps[0] == original_step

    def test_replan_multiple_new_steps(self):
        p = _make_plan()
        new_steps = [
            _make_step(id="s2", task="Step 2", task_kind="filter"),
            _make_step(id="s3", task="Step 3", task_kind="analyze"),
        ]
        p.replan(new_steps)
        assert len(p.steps) == 3
        assert p.version == 2


class TestValidTaskKinds:
    def test_valid_task_kinds_non_empty(self):
        assert len(VALID_TASK_KINDS) > 0

    def test_valid_task_kinds_contains_discover(self):
        assert "discover" in VALID_TASK_KINDS

    def test_valid_task_kinds_contains_filter(self):
        assert "filter" in VALID_TASK_KINDS

    def test_valid_task_kinds_contains_fetch(self):
        assert "fetch" in VALID_TASK_KINDS

    def test_valid_task_kinds_contains_deep_dive(self):
        assert "deep_dive" in VALID_TASK_KINDS

    def test_valid_task_kinds_contains_analyze(self):
        assert "analyze" in VALID_TASK_KINDS

    def test_valid_task_kinds_is_set(self):
        assert isinstance(VALID_TASK_KINDS, set)
