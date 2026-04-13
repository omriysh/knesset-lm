"""
context.py

Context accumulates named variables as the BFS pass executes.
evaluate_condition() evaluates edge condition strings against a Context.
"""

from __future__ import annotations

import json
import re


class Context:
    """
    Accumulates named variables as the BFS pass executes.

    Core variables set by the runner:
      original_question   user's original question (never mutated)
      question            current question (updated to follow-up between loops)
      rag_context         retrieved protocol passages (set before RAG node runs)
      meeting_paths       {meeting_id: path} (accumulated; not cleared on reset)
      sub_agent_outputs   concatenated sub-agent outputs for the reviewer

    Variables extracted by the output_format parser depend on the machine JSON.
    """

    def __init__(self, original_question: str) -> None:
        self._vars: dict = {
            "original_question": original_question,
            "question":          original_question,
        }
        self._node_outputs: dict = {}   # node_id → {"content": str, "label": str}

    # ── Var access ────────────────────────────────────────────────────────────

    def set(self, key: str, value) -> None:
        self._vars[key] = value

    def get(self, key: str, default=None):
        return self._vars.get(key, default)

    def update(self, d: dict) -> None:
        """Merge a dict into context variables."""
        for k, v in d.items():
            self._vars[k] = v

    # ── Node output tracking ──────────────────────────────────────────────────

    def set_node_output(self, node_id: str, label: str, content: str) -> None:
        self._node_outputs[node_id] = {"content": content, "label": label}

    def get_node_output(self, node_id: str) -> str:
        return self._node_outputs.get(node_id, {}).get("content", "")

    def get_node_label(self, node_id: str) -> str:
        return self._node_outputs.get(node_id, {}).get("label", node_id)

    # ── Template rendering ────────────────────────────────────────────────────

    def render_template(self, template: str) -> str:
        """Replace {{var}} placeholders with context values."""
        def replace(m: re.Match) -> str:
            key = m.group(1).strip()
            val = self._vars.get(key)
            if val is None:
                return f"[{key}]"
            return str(val)
        return re.sub(r"\{\{(\w+)\}\}", replace, template)

    def as_dict(self) -> dict:
        return dict(self._vars)

    # ── Loop reset ────────────────────────────────────────────────────────────

    def reset_for_loop(self) -> None:
        """
        Clear per-pass state before a new loop iteration.

        Preserves: original_question, question (updated to the follow-up),
                   meeting_paths (accumulated across passes).
        Any variables declared via loop_control (done_var, continue_var) are
        cleared here along with other well-known per-pass vars.
        """
        self._node_outputs.clear()
        # Clear all per-pass variables; preserve originals
        preserved_keys = {"original_question", "question", "meeting_paths"}
        keys_to_clear = [k for k in self._vars if k not in preserved_keys]
        for k in keys_to_clear:
            del self._vars[k]


# ── Condition evaluator ───────────────────────────────────────────────────────

def evaluate_condition(condition: str, ctx: Context) -> bool:
    """
    Evaluate a simple condition string against a Context.

    Supported forms (empty string → always True):
      key == 'value'
      key != 'value'
      key != ''          (truthy check — True when non-empty)
      key in ['a', 'b']
    """
    if not condition:
        return True
    s = condition.strip()

    # key in ['a', 'b', ...]
    m = re.match(r"^(\w+)\s+in\s+\[(.+)\]$", s, re.DOTALL)
    if m:
        key, raw = m.group(1), m.group(2)
        val = str(ctx.get(key, ""))
        try:
            values = json.loads(f"[{raw}]")
        except json.JSONDecodeError:
            values = [v.strip().strip("'\"") for v in raw.split(",")]
        return val in [str(v) for v in values]

    # key == 'value'
    m = re.match(r"^(\w+)\s*==\s*['\"](.+)['\"]$", s)
    if m:
        return str(ctx.get(m.group(1), "")) == m.group(2)

    # key != ''  (truthy)
    m = re.match(r"^(\w+)\s*!=\s*['\"]['\"]$", s)
    if m:
        return bool(ctx.get(m.group(1), ""))

    # key != 'value'
    m = re.match(r"^(\w+)\s*!=\s*['\"](.+)['\"]$", s)
    if m:
        return str(ctx.get(m.group(1), "")) != m.group(2)

    return True   # unrecognised form → pass through
