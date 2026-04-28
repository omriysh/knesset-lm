"""
plan.py

Plan / Step dataclasses and JSON schema for the plan-and-execute agent.

See: Documentation/KnessetLM/Development/Claude/plan-and-execute-design.md §4.1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Valid task kinds — see design §4.1.
VALID_TASK_KINDS: set[str] = {
    "discover",   # broad search (search_topics, find_*)
    "filter",     # keyword search across protocols
    "fetch",      # single-record fetch (get_mk_profile, get_meeting_summary)
    "deep_dive",  # planner-only: deep_dive_meeting
    "analyze",    # LLM-only step over already-collected evidence (no tool)
}


# Aliases accepted by Step.__init__ in addition to the canonical field names.
# The design uses (task, allowed_tools, args_hint, deps); the verification
# snippet uses (description, tool, args, depends_on). Both work.
_STEP_KWARG_ALIASES = {
    "description": "task",
    "args":        "args_hint",
    "depends_on":  "deps",
}


@dataclass(frozen=True)
class Step:
    """A single step in a Plan. Frozen — once emitted by the planner it is
    immutable. See design §4.1."""

    id: str                                       # "s1", "s2", ...
    task: str                                     # natural-language description
    task_kind: str                                # one of VALID_TASK_KINDS
    allowed_tools: tuple[str, ...] = ()           # whitelist; executor enforces
    args_hint: dict | None = None                 # optional pre-fill for executor
    deps: tuple[str, ...] = ()                    # ids of prerequisite steps
    replan_after: bool = False                    # planner-marked checkpoint
    expected_evidence: str | None = None          # post-critic input
    cost_hint: str = "cheap"                      # "cheap" | "medium" | "expensive"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "task_kind": self.task_kind,
            "allowed_tools": list(self.allowed_tools),
            "args_hint": dict(self.args_hint) if self.args_hint is not None else None,
            "deps": list(self.deps),
            "replan_after": bool(self.replan_after),
            "expected_evidence": self.expected_evidence,
            "cost_hint": self.cost_hint,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Step":
        args_hint = d.get("args_hint")
        return cls(
            id=d["id"],
            task=d["task"],
            task_kind=d["task_kind"],
            allowed_tools=tuple(d.get("allowed_tools") or ()),
            args_hint=dict(args_hint) if args_hint is not None else None,
            deps=tuple(d.get("deps") or ()),
            replan_after=bool(d.get("replan_after", False)),
            expected_evidence=d.get("expected_evidence"),
            cost_hint=d.get("cost_hint", "cheap"),
        )


# Save the dataclass-generated __init__ before our custom one shadows it.
Step.__dataclass_init__ = Step.__init__  # type: ignore[attr-defined]
# Re-apply our wrapper as the actual __init__. The dataclass decorator already
# ran by the time we reach this line, so __dataclass_init__ holds its work.
def _step_init(self, *args, **kwargs):
    for alias, canonical in _STEP_KWARG_ALIASES.items():
        if alias in kwargs:
            if canonical in kwargs:
                raise TypeError(f"Step() got both {alias!r} and {canonical!r}")
            kwargs[canonical] = kwargs.pop(alias)
    if "tool" in kwargs:
        tool = kwargs.pop("tool")
        if "allowed_tools" not in kwargs:
            kwargs["allowed_tools"] = (tool,) if tool else ()
    if "allowed_tools" in kwargs and not isinstance(kwargs["allowed_tools"], tuple):
        kwargs["allowed_tools"] = tuple(kwargs["allowed_tools"])
    if "deps" in kwargs and not isinstance(kwargs["deps"], tuple):
        kwargs["deps"] = tuple(kwargs["deps"])
    Step.__dataclass_init__(self, *args, **kwargs)
Step.__init__ = _step_init  # type: ignore[assignment]


@dataclass
class Plan:
    """A mutable Plan. Append-only across replans (design §4.1)."""

    goal: str
    steps: list[Step] = field(default_factory=list)
    version: int = 1
    notes: str = ""

    def replan(self, delta_steps: list[Step]) -> None:
        """Append-only replan. Bumps version, appends new steps to the end.

        Raises ValueError if any new step's id collides with an existing one.
        Existing step IDs remain stable so footnote IDs survive replans.
        """
        existing_ids = {s.id for s in self.steps}
        for new_step in delta_steps:
            if new_step.id in existing_ids:
                raise ValueError(
                    f"replan() is append-only — step id {new_step.id!r} "
                    "already exists; cannot replace or modify it."
                )
        self.steps.extend(delta_steps)
        self.version += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "version": self.version,
            "notes": self.notes,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Plan":
        return cls(
            goal=d["goal"],
            version=int(d.get("version", 1)),
            notes=d.get("notes", "") or "",
            steps=[Step.from_dict(s) for s in d.get("steps", [])],
        )


# Plan accepts `query=` as alias for `goal=` (verification snippet).
Plan.__dataclass_init__ = Plan.__init__  # type: ignore[attr-defined]
def _plan_init(self, *args, **kwargs):
    if "query" in kwargs:
        if "goal" in kwargs:
            raise TypeError("Plan() got both 'query' and 'goal'")
        kwargs["goal"] = kwargs.pop("query")
    Plan.__dataclass_init__(self, *args, **kwargs)
Plan.__init__ = _plan_init  # type: ignore[assignment]


# ── JSON schema for the planner output ─────────────────────────────────────
# Shown to the planner LLM in its system prompt. See design §4.1.
PLAN_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["goal", "steps"],
    "properties": {
        "goal": {
            "type": "string",
            "description": "Restates the user's question in one sentence.",
        },
        "notes": {
            "type": "string",
            "description": "Optional reasoning shown to the user.",
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "task", "task_kind", "allowed_tools"],
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": r"^s\d+$",
                        "description": "Stable step identifier, e.g. 's1', 's2'.",
                    },
                    "task": {
                        "type": "string",
                        "description": "Natural-language task description.",
                    },
                    "task_kind": {
                        "type": "string",
                        "enum": sorted(VALID_TASK_KINDS),
                    },
                    "allowed_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "args_hint": {
                        "type": ["object", "null"],
                        "description": "Optional pre-fill the executor may use.",
                    },
                    "deps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IDs of prerequisite steps.",
                    },
                    "replan_after": {
                        "type": "boolean",
                        "default": False,
                    },
                    "expected_evidence": {
                        "type": ["string", "null"],
                        "description": "One sentence — what evidence this step should produce.",
                    },
                    "cost_hint": {
                        "type": "string",
                        "enum": ["cheap", "medium", "expensive"],
                        "default": "cheap",
                    },
                },
            },
        },
    },
}
