"""Plan validator (design §6.4).

Hybrid: a deterministic pass first, with a *targeted* helper LLM call only
when an entity name in a step's ``args_hint`` looks ambiguous.

Public surface:
  * :class:`ValidationResult` — dataclass returned by :func:`validate_plan`.
  * :func:`validate_plan` — runs the deterministic checks plus the helper
    LLM disambiguation call.

The validator never raises on a malformed plan; it always returns a
:class:`ValidationResult`. Issues are tagged strings of the form
``"<KIND>: <step_id?>: <human-readable detail>"``.

Per §11 of the design, a validator failure always produces an actionable
hint for the planner (caller decides whether to replan or surface the
issue to the user).
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import config
from agent.plan_execute.plan import Plan, Step
from utils.tools import ToolRegistry, ToolSpec


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of :func:`validate_plan`.

    ``ok`` is ``True`` iff ``issues`` is empty. ``issues`` is a list of
    short strings the planner / caller can surface verbatim.
    """

    ok: bool
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": bool(self.ok), "issues": list(self.issues)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ValidationResult":
        return cls(ok=bool(d.get("ok", False)), issues=list(d.get("issues") or []))


# ---------------------------------------------------------------------------
# Issue kinds (string tags surfaced inside ``issues``)
# ---------------------------------------------------------------------------

_KIND_PHANTOM = "PHANTOM_ENTITY"
_KIND_AMBIGUOUS = "AMBIGUOUS_ENTITY"
_KIND_COVERAGE = "COVERAGE_GAP"
_KIND_OVERREACH_STEPS = "OVERREACH_STEPS"
_KIND_OVERREACH_DEEP_DIVES = "OVERREACH_DEEP_DIVES"
_KIND_MISSING_DEP = "MISSING_DEP"
_KIND_UNKNOWN_TOOL = "UNKNOWN_TOOL"
_KIND_DAG_CYCLE = "DAG_CYCLE"
_KIND_BAD_TASK_KIND = "BAD_TASK_KIND"


# ---------------------------------------------------------------------------
# Helpers — entity hints
# ---------------------------------------------------------------------------

# Tokens in ``args_hint`` that we treat as candidates for entity-resolution
# checks. Other keys (numeric IDs, booleans, free-form topic queries) are
# left alone.
_ENTITY_HINT_KEYS = {
    "mk_name",
    "mk", "name",
    "committee_name", "committee",
    "bill_name", "bill",
    "vote_name", "vote",
    "speaker", "speaker_name",
}

# Crude Hebrew/Latin name detector — a non-empty string with no digits and
# at least one letter character. Numeric IDs slip through unflagged.
_LIKELY_NAME_RE = re.compile(r"^[^\d]*[A-Za-z֐-׿][^\d]*$")


def _looks_like_entity_name(value: object) -> bool:
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    return bool(_LIKELY_NAME_RE.match(s))


def _find_resolvers(deps: Iterable[str], steps_by_id: dict[str, Step]) -> list[str]:
    """Return the names of `find_*` tools used by any step in ``deps``."""
    out: list[str] = []
    for dep_id in deps:
        dep = steps_by_id.get(dep_id)
        if dep is None:
            continue
        for t in dep.allowed_tools or ():
            if isinstance(t, str) and t.startswith("find_"):
                out.append(t)
    return out


# ---------------------------------------------------------------------------
# Helper LLM disambiguation
# ---------------------------------------------------------------------------


def _disambiguate(
    step: Step,
    entity_key: str,
    entity_value: str,
    llm_call: Callable,
) -> str | None:
    """Ask the helper LLM whether ``entity_value`` is concrete enough.

    Returns ``None`` if the LLM judges the value resolvable in context
    (no issue to flag), or a short human-readable issue string otherwise.

    The call is single-turn, JSON-only, and uses the local llama-server
    (``config.INTENT_MODEL``) by convention. The caller-injected
    ``llm_call`` handles model selection — this function only crafts the
    request and parses the JSON.
    """
    prompt = (
        "You are a helper that decides if a named entity in a research-plan "
        "step is specific enough to be used as-is, or if the planner needs "
        "a `find_*` resolver step first.\n\n"
        f"Step task: {step.task}\n"
        f"Step task_kind: {step.task_kind}\n"
        f"Field: {entity_key}\n"
        f"Value: {entity_value!r}\n\n"
        "Output ONE JSON object (no prose, no markdown fences):\n"
        '  { "verdict": "ok" | "ambiguous", "reason": "..." }\n'
        "Use 'ambiguous' if the value could plausibly match more than one "
        "real-world entity and a resolver step is needed."
    )
    try:
        raw = llm_call(
            model=getattr(config, "INTENT_MODEL", "local"),
            prompt=prompt,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001 — surface as an issue, not a crash
        return f"{_KIND_AMBIGUOUS}: {step.id}: helper LLM error ({exc})"

    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        # Couldn't parse — be conservative and flag.
        return f"{_KIND_AMBIGUOUS}: {step.id}: {entity_key}={entity_value!r} (helper returned non-JSON)"
    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict == "ok":
        return None
    reason = str(parsed.get("reason", "")).strip() or "helper marked ambiguous"
    return f"{_KIND_AMBIGUOUS}: {step.id}: {entity_key}={entity_value!r} ({reason})"


def _parse_json(raw: object) -> Any:
    """Best-effort JSON parse — accepts strings or dicts; tolerates fences."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    # Strip ```json ... ``` fences if the model emitted them.
    if text.startswith("```"):
        # remove leading fence line
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[validator] JSON parse failed: {exc}  text={text[:120]!r}", file=sys.stderr, flush=True)
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_plan(
    plan: Plan,
    registry: ToolRegistry,
    llm_call: Callable,
) -> ValidationResult:
    """Run deterministic validation, then a targeted helper LLM call for any
    ambiguous entity hints.

    Args:
        plan: the Plan to validate.
        registry: the tool registry the plan's ``allowed_tools`` reference.
        llm_call: callable used for the helper LLM disambiguation step.
            Signature: ``llm_call(model: str, prompt: str, response_format=...)
            -> str | dict``. Only invoked on ambiguous entity-name hits.

    Returns:
        :class:`ValidationResult`. Always returns; never raises on a
        malformed plan.
    """
    issues: list[str] = []

    if plan is None:
        return ValidationResult(ok=False, issues=["NO_PLAN: validate_plan called with None"])

    steps: list[Step] = list(plan.steps or [])
    steps_by_id: dict[str, Step] = {s.id: s for s in steps}

    # ------------------------------------------------------------------ caps
    max_steps_v1 = int(getattr(config, "RESEARCH_MAX_PLAN_STEPS_V1", 8))
    max_deep_dives = int(getattr(config, "RESEARCH_MAX_DEEP_DIVES_PER_PLAN", 3))

    if int(getattr(plan, "version", 1)) == 1 and len(steps) > max_steps_v1:
        issues.append(
            f"{_KIND_OVERREACH_STEPS}: plan v1 has {len(steps)} steps > cap {max_steps_v1}"
        )

    deep_dive_count = sum(
        1
        for s in steps
        if s.task_kind == "deep_dive"
        or "deep_dive_meeting" in (s.allowed_tools or ())
    )
    if deep_dive_count > max_deep_dives:
        issues.append(
            f"{_KIND_OVERREACH_DEEP_DIVES}: {deep_dive_count} deep-dive steps > cap {max_deep_dives}"
        )

    # ----------------------------------------------------- registry lookups
    registry_by_name: dict[str, ToolSpec] = {
        spec.name: spec for spec in (registry or [])
    }

    # ------------------------------------------------------- per-step checks
    for step in steps:
        # task_kind sanity
        from agent.plan_execute.plan import VALID_TASK_KINDS  # local to avoid cycles
        if step.task_kind not in VALID_TASK_KINDS:
            issues.append(
                f"{_KIND_BAD_TASK_KIND}: {step.id}: task_kind={step.task_kind!r}"
            )

        # analyze steps are LLM-only — no tool calls allowed
        if step.task_kind == "analyze" and step.allowed_tools:
            issues.append(
                f"{_KIND_BAD_TASK_KIND}: {step.id}: task_kind='analyze' must have "
                f"empty allowed_tools (got {list(step.allowed_tools)!r})"
            )

        # tools must exist in the registry
        for tool_name in (step.allowed_tools or ()):
            # expand is a pseudo-tool dispatched by the graph, not in the registry
            if tool_name == "expand":
                issues.append(
                    f"{_KIND_UNKNOWN_TOOL}: {step.id}: 'expand' must not appear in "
                    "allowed_tools — it is executor-internal and always available"
                )
                continue
            spec = registry_by_name.get(tool_name)
            if spec is None:
                issues.append(
                    f"{_KIND_UNKNOWN_TOOL}: {step.id}: tool {tool_name!r} not in registry"
                )

        # deps must reference existing step ids
        for dep_id in (step.deps or ()):
            if dep_id not in steps_by_id:
                issues.append(
                    f"{_KIND_MISSING_DEP}: {step.id}: dep {dep_id!r} not in plan"
                )

    # ---------------------------------------------------------- DAG cycle
    if _has_cycle(steps_by_id):
        issues.append(f"{_KIND_DAG_CYCLE}: plan steps form a cycle")

    # ------------------------------------------------------- coverage gap
    # Coverage gap: there are steps with no dependents AND no expected
    # evidence (i.e. dead ends). The post-critic does the heavy lifting,
    # but we can flag the obvious case where a plan has zero steps.
    if not steps:
        issues.append(f"{_KIND_COVERAGE}: plan has zero steps")

    # ---------------------------------------------------- entity-hint pass
    for step in steps:
        hint = step.args_hint or {}
        for key, value in hint.items():
            if key not in _ENTITY_HINT_KEYS:
                continue
            if not _looks_like_entity_name(value):
                continue

            resolvers = _find_resolvers(step.deps or (), steps_by_id)
            if resolvers:
                # A `find_*` resolver runs upstream — accept without LLM call.
                continue

            # No upstream resolver. Check whether the step's own task_kind
            # implies it IS a resolver itself.
            self_resolves = any(
                isinstance(t, str) and t.startswith("find_")
                for t in (step.allowed_tools or ())
            )
            if self_resolves:
                continue

            # Ambiguity decision delegated to helper LLM.
            issue = _disambiguate(step, key, str(value), llm_call)
            if issue is None:
                continue
            # If helper marks it ambiguous AND no resolver in deps, this is
            # also a phantom-entity flag.
            issues.append(issue)
            issues.append(
                f"{_KIND_PHANTOM}: {step.id}: {key}={str(value)!r} has no "
                f"upstream find_* resolver"
            )

    return ValidationResult(ok=(not issues), issues=issues)


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def _has_cycle(steps_by_id: dict[str, Step]) -> bool:
    """Standard DFS cycle detection over the dep graph."""
    WHITE, GRAY, BLACK = 0, 1, 2
    colour: dict[str, int] = {sid: WHITE for sid in steps_by_id}

    def visit(sid: str) -> bool:
        if colour.get(sid, WHITE) == GRAY:
            return True
        if colour.get(sid, WHITE) == BLACK:
            return False
        colour[sid] = GRAY
        step = steps_by_id.get(sid)
        for dep in (step.deps if step is not None else ()):
            if dep in steps_by_id and visit(dep):
                return True
        colour[sid] = BLACK
        return False

    for sid in list(steps_by_id):
        if visit(sid):
            return True
    return False


__all__ = ["ValidationResult", "validate_plan"]
