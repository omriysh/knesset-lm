"""
machine.py

StateMachine: loads, validates, and provides graph-traversal helpers for
a machine JSON produced by the agent designer.

Machine JSON schema (version 2)
--------------------------------
{
  "version": 2,
  "id": "...",
  "name": "...",
  "nodes": [
    {
      "id":        str,
      "type":      "begin" | "llm_call" | "tool",
      "label":     str,
      "imaginary": bool,  # skipped by the engine
      "terminal":  bool,  # no auto-transition continues after this node
      "data": {
        "system_prompt":   str,
        "input_template":  str,          # {{var}} placeholders
        "output_format":   dict | null,  # see parsers.py
        "stage":           str,          # UI colour hint
        "rag":             str,          # "3level" → triggers retrieval
        "temperature":     float,
        "max_tokens":      int,
        "function_name":   str,          # tool nodes only
        "description":     str,          # tool nodes only
        "parameters":      dict,         # tool nodes only (JSON Schema)
      }
    }
  ],
  "edges": [
    {
      "id":        str,
      "source":    str,
      "target":    str,
      "type":      "transition" | "tool_link",
      "condition": str,     # transition edges only; empty = always
      "max_loops": int,     # back-edges only
    }
  ]
}
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

_SUPPORTED_VERSIONS = {2}


class StateMachine:
    """
    Loaded from an agent-designer machine JSON file.
    Provides typed graph-traversal helpers for MachineRunner.
    """

    def __init__(self, path: str | Path) -> None:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        version = data.get("version")
        if version not in _SUPPORTED_VERSIONS:
            raise ValueError(
                f"Machine version {version!r} is not supported "
                f"(supported: {sorted(_SUPPORTED_VERSIONS)}). "
                f"File: {path}"
            )

        self.name         = data.get("name", "machine")
        self.version      = version
        self.global_rules = data.get("global_rules", "")
        self._nodes: dict[str, dict] = {n["id"]: n for n in data["nodes"]}
        self._edges: list[dict]      = data["edges"]

        self._transition_map: dict[str, list[str]] = {}
        self._tool_map:       dict[str, list[str]] = {}
        for edge in self._edges:
            src, tgt = edge["source"], edge["target"]
            if edge.get("type") == "tool_link":
                self._tool_map.setdefault(src, []).append(tgt)
            else:
                self._transition_map.setdefault(src, []).append(tgt)

        self._validate()

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate(self) -> None:
        errors: list[str] = []

        # All edge endpoints must exist
        for edge in self._edges:
            for key in ("source", "target"):
                nid = edge.get(key)
                if nid and nid not in self._nodes:
                    errors.append(
                        f"Edge {edge.get('id')!r}: {key} node {nid!r} does not exist"
                    )

        if errors:
            raise ValueError(
                "Machine validation failed:\n" + "\n".join(f"  • {e}" for e in errors)
            )

        # Warn about unreachable non-begin nodes (orphans)
        reachable: set[str] = set()
        begin = self._try_begin_id()
        if begin:
            queue = [begin]
            while queue:
                nid = queue.pop()
                if nid in reachable:
                    continue
                reachable.add(nid)
                queue.extend(self._transition_map.get(nid, []))
                queue.extend(self._tool_map.get(nid, []))

        for nid, node in self._nodes.items():
            if node.get("type") != "begin" and nid not in reachable:
                warnings.warn(
                    f"Node {nid!r} ({node.get('label')!r}) is unreachable from begin",
                    stacklevel=3,
                )

    def _try_begin_id(self) -> str | None:
        for nid, n in self._nodes.items():
            if n.get("type") == "begin":
                return nid
        return None

    # ── Graph traversal ───────────────────────────────────────────────────────

    def begin_id(self) -> str:
        nid = self._try_begin_id()
        if nid is None:
            raise ValueError("No begin node found in machine")
        return nid

    def first_llm_node_id(self) -> str:
        """First non-imaginary llm_call reachable from begin."""
        for tid in self._transition_map.get(self.begin_id(), []):
            n = self._nodes[tid]
            if n.get("type") == "llm_call" and not n.get("imaginary"):
                return tid
        raise ValueError("No non-imaginary LLM node found after begin")

    def get_node(self, nid: str) -> dict:
        return self._nodes[nid]

    def outgoing_transitions(self, nid: str) -> list[dict]:
        """Full transition edge dicts (including condition, max_loops) from nid."""
        return [
            e for e in self._edges
            if e["source"] == nid and e.get("type", "transition") == "transition"
        ]

    def tool_nodes(self, nid: str) -> list[dict]:
        """Tool node dicts linked from nid via tool_link edges."""
        return [
            self._nodes[t]
            for t in self._tool_map.get(nid, [])
            if self._nodes[t].get("type") == "tool"
        ]

    def tool_nodes_all(self) -> list[dict]:
        """All tool nodes in the machine (for building the tool registry)."""
        return [n for n in self._nodes.values() if n.get("type") == "tool"]

    def max_loops_from_edges(self, default: int = 3) -> int:
        """Read max_loops from the first back-edge that declares it."""
        for e in self._edges:
            if e.get("type", "transition") == "transition" and "max_loops" in e:
                return int(e["max_loops"])
        return default

    def build_tool_schemas(self, tool_node_list: list[dict]) -> list[dict]:
        """
        Build OpenAI-format tool schemas from a list of tool nodes.
        Node data fields used: function_name, description, parameters.
        """
        schemas: list[dict] = []
        seen: set[str] = set()
        for node in tool_node_list:
            fn_name = node["data"].get("function_name") or node.get("label", "")
            if not fn_name or fn_name in seen:
                continue
            seen.add(fn_name)
            schemas.append({
                "type": "function",
                "function": {
                    "name":        fn_name,
                    "description": node["data"].get("description", ""),
                    "parameters":  node["data"].get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                },
            })
        return schemas
