"""Tests for agent.runner — build_tool_registry."""

import pytest

from agent.machine import StateMachine
from agent.runner import build_tool_registry


# ── build_tool_registry ───────────────────────────────────────────────────────

class TestBuildToolRegistry:
    def test_empty_machine_returns_empty_registry(self, minimal_machine_path):
        sm = StateMachine(minimal_machine_path)
        registry = build_tool_registry(sm)
        assert registry == {}

    def test_known_tool_registered(self, machine_with_tool_path):
        sm = StateMachine(machine_with_tool_path)
        registry = build_tool_registry(
            sm,
            knesset_dispatch=lambda name, args: f"result:{name}",
        )
        assert "get_mk_profile" in registry

    def test_unknown_tool_raises_at_startup(self, tmp_path):
        """build_tool_registry must raise ValueError for any unregistered tool."""
        import json
        from pathlib import Path

        data = {
            "version": 2,
            "id": "t",
            "name": "T",
            "nodes": [
                {"id": "begin_1", "type": "begin", "label": "Begin",
                 "position": {"x": 0, "y": 0}, "data": {}},
                {"id": "llm_1",   "type": "llm_call", "label": "LLM",
                 "position": {"x": 200, "y": 0}, "data": {}},
                {"id": "tool_x",  "type": "tool", "label": "X",
                 "position": {"x": 200, "y": 100},
                 "data": {"function_name": "some_unknown_tool",
                          "description": "", "parameters": {}}},
            ],
            "edges": [
                {"id": "e1", "source": "begin_1", "target": "llm_1",
                 "type": "transition", "label": ""},
                {"id": "e2", "source": "llm_1", "target": "tool_x",
                 "type": "tool_link", "label": ""},
            ],
        }
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        sm = StateMachine(p)

        with pytest.raises(ValueError, match="some_unknown_tool"):
            build_tool_registry(sm)   # no executors provided → unknown

    def test_tool_callable_invoked(self, machine_with_tool_path):
        sm = StateMachine(machine_with_tool_path)
        calls = []

        def _dispatch(name, args):
            calls.append((name, args))
            return "mock result"

        registry = build_tool_registry(sm, knesset_dispatch=_dispatch)
        result = registry["get_mk_profile"]({"name": "יצחק"})
        assert result == "mock result"
        assert calls == [("get_mk_profile", {"name": "יצחק"})]

    def test_summary_executor_registered(self, machine_with_tool_path):
        """Summary tools should be callable when summary_executor is provided."""
        import json as _json
        from pathlib import Path

        # Build a machine with a summary tool
        data = {
            "version": 2,
            "id": "s",
            "name": "S",
            "nodes": [
                {"id": "begin_1", "type": "begin", "label": "Begin",
                 "position": {"x": 0, "y": 0}, "data": {}},
                {"id": "llm_1",   "type": "llm_call", "label": "LLM",
                 "position": {"x": 200, "y": 0}, "data": {}},
                {"id": "tool_s",  "type": "tool", "label": "Summary",
                 "position": {"x": 200, "y": 100},
                 "data": {"function_name": "get_meeting_summary",
                          "description": "", "parameters": {}}},
            ],
            "edges": [
                {"id": "e1", "source": "begin_1", "target": "llm_1",
                 "type": "transition", "label": ""},
                {"id": "e2", "source": "llm_1", "target": "tool_s",
                 "type": "tool_link", "label": ""},
            ],
        }
        p = machine_with_tool_path.parent / "summary_machine.json"
        p.write_text(_json.dumps(data), encoding="utf-8")
        sm = StateMachine(p)

        calls = []
        def _summary_exec(name, args, meeting_paths=None):
            calls.append(name)
            return "summary text"

        registry = build_tool_registry(sm, summary_executor=_summary_exec)
        assert "get_meeting_summary" in registry
