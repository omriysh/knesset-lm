"""
conftest.py

Shared pytest fixtures and helpers.
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

# Bootstrap sys.path so tests can import from src/ without installation
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ── Machine JSON factories ────────────────────────────────────────────────────

def _minimal_machine(extra_nodes=None, extra_edges=None) -> dict:
    """Return the smallest valid v2 machine: begin → one LLM node."""
    nodes = [
        {"id": "begin_001", "type": "begin", "label": "Begin",
         "position": {"x": 0, "y": 0}, "data": {}},
        {"id": "llm_001",   "type": "llm_call", "label": "Router",
         "position": {"x": 200, "y": 0},
         "data": {"system_prompt": "You are helpful.", "stage": "router",
                  "temperature": 0.7, "max_tokens": 512}},
    ]
    edges = [
        {"id": "e_001", "source": "begin_001", "target": "llm_001",
         "type": "transition", "label": ""},
    ]
    nodes.extend(extra_nodes or [])
    edges.extend(extra_edges or [])
    return {"version": 2, "id": "test_machine", "name": "Test", "nodes": nodes, "edges": edges}


@pytest.fixture()
def minimal_machine_path(tmp_path):
    """Write a minimal machine JSON to a temp file and return its Path."""
    p = tmp_path / "machine.json"
    p.write_text(json.dumps(_minimal_machine()), encoding="utf-8")
    return p


@pytest.fixture()
def machine_with_tool_path(tmp_path):
    """Machine with one LLM node + one tool node."""
    nodes = [
        {"id": "tool_001", "type": "tool", "label": "get_profile",
         "position": {"x": 200, "y": 100},
         "data": {
             "function_name": "get_mk_profile",
             "description": "Get MK profile",
             "parameters": {"type": "object", "properties": {
                 "name": {"type": "string"}
             }},
         }},
    ]
    edges = [
        {"id": "e_tool", "source": "llm_001", "target": "tool_001",
         "type": "tool_link", "label": ""},
    ]
    data = _minimal_machine(extra_nodes=nodes, extra_edges=edges)
    p = tmp_path / "machine_tool.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p
