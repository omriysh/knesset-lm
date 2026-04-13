"""Tests for agent.machine — StateMachine loading and graph traversal."""

import json
import warnings

import pytest

from agent.machine import StateMachine


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_machine(tmp_path, data: dict) -> "Path":
    p = tmp_path / "m.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _base_machine(**kwargs) -> dict:
    m = {
        "version": 2,
        "id":      "test",
        "name":    "Test Machine",
        "nodes": [
            {"id": "begin_1", "type": "begin", "label": "Begin",
             "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "llm_1",   "type": "llm_call", "label": "Router",
             "position": {"x": 200, "y": 0},
             "data": {"system_prompt": "You are helpful.", "stage": "router"}},
        ],
        "edges": [
            {"id": "e1", "source": "begin_1", "target": "llm_1",
             "type": "transition", "label": ""},
        ],
    }
    m.update(kwargs)
    return m


# ── Version validation ────────────────────────────────────────────────────────

def test_loads_valid_v2_machine(minimal_machine_path):
    sm = StateMachine(minimal_machine_path)
    assert sm.version == 2
    assert sm.name == "Test"

def test_rejects_unsupported_version(tmp_path):
    data = _base_machine(version=1)
    p = _write_machine(tmp_path, data)
    with pytest.raises(ValueError, match="not supported"):
        StateMachine(p)

def test_rejects_missing_version(tmp_path):
    data = _base_machine()
    del data["version"]
    p = _write_machine(tmp_path, data)
    with pytest.raises(ValueError, match="not supported"):
        StateMachine(p)


# ── Edge validation ───────────────────────────────────────────────────────────

def test_rejects_dangling_edge(tmp_path):
    data = _base_machine()
    data["edges"].append(
        {"id": "bad", "source": "llm_1", "target": "nonexistent",
         "type": "transition", "label": ""}
    )
    p = _write_machine(tmp_path, data)
    with pytest.raises(ValueError, match="nonexistent"):
        StateMachine(p)

def test_warns_on_unreachable_node(tmp_path):
    data = _base_machine()
    data["nodes"].append(
        {"id": "orphan", "type": "llm_call", "label": "Orphan",
         "position": {"x": 400, "y": 0}, "data": {}}
    )
    p = _write_machine(tmp_path, data)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        StateMachine(p)
    assert any("orphan" in str(warning.message).lower() or
               "Orphan" in str(warning.message) for warning in w)


# ── Graph traversal ───────────────────────────────────────────────────────────

def test_begin_id(minimal_machine_path):
    sm = StateMachine(minimal_machine_path)
    assert sm.begin_id() == "begin_001"

def test_first_llm_node_id(minimal_machine_path):
    sm = StateMachine(minimal_machine_path)
    assert sm.first_llm_node_id() == "llm_001"

def test_get_node(minimal_machine_path):
    sm = StateMachine(minimal_machine_path)
    node = sm.get_node("llm_001")
    assert node["label"] == "Router"
    assert node["type"] == "llm_call"

def test_outgoing_transitions(tmp_path):
    data = _base_machine()
    # Add a second LLM node + transition from llm_1 to it
    data["nodes"].append(
        {"id": "llm_2", "type": "llm_call", "label": "Reviewer",
         "position": {"x": 400, "y": 0}, "data": {}}
    )
    data["edges"].append(
        {"id": "e2", "source": "llm_1", "target": "llm_2",
         "type": "transition", "label": "done",
         "condition": "agent != ''"}
    )
    p = _write_machine(tmp_path, data)
    sm = StateMachine(p)

    transitions = sm.outgoing_transitions("llm_1")
    assert len(transitions) == 1
    assert transitions[0]["target"] == "llm_2"
    assert transitions[0]["condition"] == "agent != ''"

def test_tool_nodes(machine_with_tool_path):
    sm = StateMachine(machine_with_tool_path)
    tools = sm.tool_nodes("llm_001")
    assert len(tools) == 1
    assert tools[0]["data"]["function_name"] == "get_mk_profile"

def test_tool_nodes_all(machine_with_tool_path):
    sm = StateMachine(machine_with_tool_path)
    all_tools = sm.tool_nodes_all()
    assert len(all_tools) == 1
    assert all_tools[0]["id"] == "tool_001"

def test_tool_nodes_empty_when_none(minimal_machine_path):
    sm = StateMachine(minimal_machine_path)
    assert sm.tool_nodes("llm_001") == []
    assert sm.tool_nodes_all() == []

def test_max_loops_from_edges_default(minimal_machine_path):
    sm = StateMachine(minimal_machine_path)
    assert sm.max_loops_from_edges(default=3) == 3

def test_max_loops_from_edges_from_json(tmp_path):
    data = _base_machine()
    # Add a back-edge with max_loops
    data["nodes"].append(
        {"id": "llm_2", "type": "llm_call", "label": "Rev",
         "position": {"x": 400, "y": 0}, "data": {}}
    )
    data["edges"].extend([
        {"id": "e2", "source": "llm_1", "target": "llm_2",
         "type": "transition", "label": ""},
        {"id": "e_back", "source": "llm_2", "target": "llm_1",
         "type": "transition", "label": "", "max_loops": 5},
    ])
    p = _write_machine(tmp_path, data)
    sm = StateMachine(p)
    assert sm.max_loops_from_edges() == 5


# ── build_tool_schemas ────────────────────────────────────────────────────────

def test_build_tool_schemas(machine_with_tool_path):
    sm = StateMachine(machine_with_tool_path)
    schemas = sm.build_tool_schemas(sm.tool_nodes_all())
    assert len(schemas) == 1
    fn = schemas[0]["function"]
    assert fn["name"] == "get_mk_profile"
    assert "name" in fn["parameters"]["properties"]

def test_build_tool_schemas_deduplicates(tmp_path):
    data = _base_machine()
    for i in range(3):
        data["nodes"].append(
            {"id": f"tool_{i}", "type": "tool", "label": f"t{i}",
             "position": {"x": 0, "y": i * 50},
             "data": {"function_name": "get_mk_profile",
                      "description": "", "parameters": {}}}
        )
        data["edges"].append(
            {"id": f"et_{i}", "source": "llm_1", "target": f"tool_{i}",
             "type": "tool_link", "label": ""}
        )
    p = _write_machine(tmp_path, data)
    sm = StateMachine(p)
    schemas = sm.build_tool_schemas(sm.tool_nodes_all())
    names = [s["function"]["name"] for s in schemas]
    assert len(names) == len(set(names))   # no duplicates
