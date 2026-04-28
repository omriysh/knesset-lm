"""PlanExecuteAgent — the assembled plan-and-execute subgraph driver.

Composes Phase 4 building blocks (plan, scratchpad, budget, concurrency,
validator, critics, executor, synthesizer, tools-view) into a single
SubgraphAgent generator.

Subclasses (notably ResearchAgent) bind:
  * a domain-specific tool registry via :meth:`tool_registry`,
  * prompt addenda (planner / critic_pre / critic_post / synthesizer)
    via :meth:`prompt_addenda`,
  * the EVENTS / OUTPUT_VARS class attributes from SubgraphAgent.

This module is otherwise generic — it knows about Plan/Step/EvidenceStore
but nothing about the Knesset domain.

The agent yields ``SubgraphEvent`` values per design §3.2:
  * ``progress`` — informational beats (planning_started, executing, ...)
  * ``hook``     — interruptable points: cost_estimate_required, plan_ready,
                   step_completed
  * ``done``     — successful completion; payload contains OUTPUT_VARS
  * ``error``    — fatal failure; payload contains ``error`` / ``kind``

Per design §11 / §3.2.3 unwired hooks fall back to a default behaviour
(auto-approve cost gate, auto-ok plan_ready, no-op step_completed). The
runner signals "not wired" by sending ``resume`` with payload
``{"_unwired": True}`` or by simply not calling resume; v1 treats both as
the default.
"""

from __future__ import annotations

import json
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Generator

import config
from agent.llm.base import DoneEvent, TokenEvent, ToolCallsEvent
from agent.plan_execute.budget import BudgetExceeded, BudgetTracker, estimate_plan_seconds
from agent.plan_execute.concurrency import DAGExecutor
from agent.plan_execute.critics import CriticResult, critic_post, critic_pre
from agent.plan_execute.executor import execute_step
from agent.plan_execute.plan import PLAN_JSON_SCHEMA, Plan, Step
from agent.plan_execute.scratchpad import Scratchpad
from agent.plan_execute.synthesizer import synthesize
from agent.plan_execute.tools import (
    EXPAND_TOOL_SCHEMA,
    list_tools_for_executor,
    list_tools_for_planner,
)
from agent.plan_execute.validator import ValidationResult, validate_plan
from agent.subgraph.base import SubgraphAgent, SubgraphEvent
from agent.subgraph.evidence import (
    EvidenceCapExceeded,
    EvidenceEntry,
    EvidenceStore,
    ToolEnvelope,
)
from utils.tools import ToolRegistry


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


_GENERIC_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_generic_prompt(name: str) -> str:
    """Load one of plan_execute's generic prompts."""
    return (_GENERIC_PROMPTS_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# LLM adapter — turn an LLMBackend.stream() into the synchronous llm_call
# shape the validator/critics/executor/synthesizer expect.
# ---------------------------------------------------------------------------


def _drain_stream(stream) -> tuple[str, list[dict]]:
    """Consume an LLMBackend.stream() generator and return (text, tool_calls).

    Tool calls are returned in OpenAI-style shape:
        {"id": str, "type": "function",
         "function": {"name": str, "arguments": "<json string>"}}
    """
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for ev in stream:
        if isinstance(ev, TokenEvent):
            text_parts.append(ev.text)
        elif isinstance(ev, ToolCallsEvent):
            tool_calls = list(ev.calls or [])
        elif isinstance(ev, DoneEvent):
            break
        # ThinkingEvent: ignored for non-streaming consumers.
    return ("".join(text_parts), tool_calls)


def _normalize_tool_calls_for_executor(tcs: list[dict]) -> list[dict]:
    """Pre-parse OpenAI-style stringified arguments for executor.parse paths.

    ``executor._normalise_tool_call`` already handles the same shape; this
    helper is a no-op pass-through for now and exists so future LLM tweaks
    can land in one place.
    """
    return tcs


class _LLMBridge:
    """Wraps an LLMBackend factory into the plain ``llm_call`` callable that
    Phase 4 sub-modules already speak.

    Why a class rather than a closure: each model name maps to its own
    backend instance (Gemini-side caches its client; local-side uses the
    same llama-server URL). We lazily build the right backend per model and
    hold it for re-use within one agent run.

    Construction does NOT require any network or API key — the underlying
    backend constructors only fail when the user actually attempts a call.
    """

    def __init__(self, fallback_to_local: bool = True):
        self._fallback_to_local = bool(fallback_to_local)
        self._cache: dict[tuple[str, str], Any] = {}

    # -- public --------------------------------------------------------------

    def __call__(
        self,
        *,
        model: str,
        prompt: str | list[dict] | None = None,
        messages: list[dict] | None = None,
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """Call ``model`` with ``prompt`` (or messages) and return either a
        plain text string (for response_format=None paths) or a dict with
        ``tool_calls`` and ``content`` (for tool-using paths).

        The signature is a superset of every Phase 4 sub-module's expected
        ``llm_call``: synthesizer passes only ``model`` + ``prompt``;
        critics pass ``response_format``; executor passes ``tools``.
        """
        backend = self._backend_for(model)
        msgs = self._build_messages(prompt, messages, response_format)

        kwargs: dict[str, Any] = {"messages": msgs}
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        try:
            text, tool_calls = _drain_stream(backend.stream(**kwargs))
        except Exception as exc:  # noqa: BLE001 — surface as a structured response
            # The downstream consumers (critics / executor) wrap this back
            # into their own error envelopes; raising here would break the
            # outer SubgraphEvent contract.
            raise RuntimeError(f"llm_call({model!r}) failed: {exc}") from exc

        # Tool-using path: keep the OpenAI-style shape the executor expects.
        if tools:
            return {
                "content": text,
                "tool_calls": _normalize_tool_calls_for_executor(tool_calls),
            }

        # JSON-response_format path: return raw text; the consumer parses.
        # critic_pre / critic_post / validator helper LLM all do their own
        # ```json``` fence stripping + json.loads.
        return text

    # -- internal ------------------------------------------------------------

    def _build_messages(
        self,
        prompt: str | list[dict] | None,
        messages: list[dict] | None,
        response_format: dict | None,
    ) -> list[dict]:
        if messages is not None:
            out = list(messages)
        elif isinstance(prompt, list):
            out = list(prompt)
        elif isinstance(prompt, str):
            out = [{"role": "user", "content": prompt}]
        else:
            out = []

        # Hint to the model when the consumer asked for a JSON object.
        if response_format and response_format.get("type") == "json_object":
            # If the last message is the user's, append a one-line nudge so
            # models that don't honour response_format still get the message.
            if out and out[-1].get("role") == "user":
                hint = (
                    "\n\nReply with ONE JSON object (no markdown fences, no prose)."
                )
                out[-1] = {
                    "role": out[-1].get("role"),
                    "content": (out[-1].get("content") or "") + hint,
                }
        return out

    def _backend_for(self, model: str):
        """Return (and cache) the right backend instance for ``model``.

        Routing rules (per design §10 model registry):
          * "local" → llama-server (used by INTENT_MODEL).
          * "gemma-3-*" / "gemini-*" → GoogleBackend (cloud).
          * anything else → GoogleBackend by default.

        On `model == "local"` we skip the cloud entirely. Otherwise we
        instantiate ``GoogleBackend(model=...)``; that backend has its own
        retry-and-fall-back-to-local logic (see google.py).
        """
        kind = "local" if model == "local" else "google"
        cache_key = (kind, model)
        if cache_key in self._cache:
            return self._cache[cache_key]

        if kind == "local":
            from agent.llm.gemma import GemmaLlamaBackend
            backend = GemmaLlamaBackend()
        else:
            from agent.llm.google import GoogleBackend
            backend = GoogleBackend(model=model)

        self._cache[cache_key] = backend
        return backend


# ---------------------------------------------------------------------------
# Helpers for plan parsing
# ---------------------------------------------------------------------------


def _parse_plan_json(raw: object) -> dict | None:
    """Best-effort JSON parse — accepts dict or fenced/plain string."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _make_plan_from_dict(d: dict, *, version: int = 1) -> Plan | None:
    """Tolerant Plan-from-dict; accepts missing fields with sensible defaults."""
    if not isinstance(d, dict):
        return None
    try:
        plan = Plan.from_dict(d)
    except Exception:  # noqa: BLE001 — we'll synthesise minimal plan below
        # Fall through to manual construction.
        plan = None
    if plan is None:
        steps_in = d.get("steps") or []
        steps: list[Step] = []
        for s in steps_in:
            try:
                steps.append(Step.from_dict(s))
            except Exception:  # noqa: BLE001
                continue
        plan = Plan(
            goal=str(d.get("goal") or ""),
            steps=steps,
            version=version,
            notes=str(d.get("notes") or ""),
        )
    if version is not None:
        plan.version = int(version)
    return plan


# ---------------------------------------------------------------------------
# PlanExecuteAgent
# ---------------------------------------------------------------------------


class PlanExecuteAgent(SubgraphAgent):
    """Subgraph agent that drives the full plan-and-execute loop.

    Concrete subclasses bind a domain by overriding :meth:`tool_registry`,
    :meth:`prompt_addenda`, and the class attributes
    :data:`EVENTS` / :data:`OUTPUT_VARS` from :class:`SubgraphAgent`.

    Construction never makes network or API calls — the LLM bridge is
    instantiated immediately, but its underlying backends are built
    lazily on first use. This means the constructor works without
    ``GOOGLE_API_KEY`` set (the failure surfaces inside :meth:`run` when
    the planner LLM is actually invoked).
    """

    # Subclasses override; default values keep ``PlanExecuteAgent()`` safe to
    # instantiate (the test harness uses bare PlanExecuteAgent in some
    # paths).
    EVENTS: set[str] = set()
    OUTPUT_VARS: list[str] = ["final_answer", "footnotes"]

    # ── Subclass surface ────────────────────────────────────────────────

    def tool_registry(self) -> ToolRegistry:
        """Return the domain tool registry. Subclasses MUST override."""
        raise NotImplementedError(
            "PlanExecuteAgent subclasses must override tool_registry()"
        )

    def prompt_addenda(self) -> dict[str, str]:
        """Return a {role: text} dict appended onto generic prompts.

        Roles: ``"planner"``, ``"critic_pre"``, ``"critic_post"``,
        ``"synthesizer"``. Missing keys mean "no addendum".
        """
        return {}

    # ── Construction ────────────────────────────────────────────────────

    def __init__(self, *, llm_bridge: _LLMBridge | None = None):
        # Lazily-resolved fields — populated inside :meth:`run`.
        self._plan: Plan | None = None
        self._store: EvidenceStore | None = None
        self._budget: BudgetTracker | None = None

        self._inputs: dict | None = None
        self._llm: _LLMBridge = llm_bridge or _LLMBridge(
            fallback_to_local=getattr(config, "GOOGLE_API_FALLBACK_TO_LOCAL", True)
        )

        # Cached resolved prompts. Built on first use.
        self._resolved_prompts: dict[str, str] = {}

    # ── Public SubgraphAgent contract ───────────────────────────────────

    def run(self, inputs: dict) -> Generator[SubgraphEvent, Any, None]:
        """Drive the full pipeline. Yields SubgraphEvents.

        Inputs:
          * ``query`` (required): the user's question.
          * ``intent_hint`` (optional): free-form planner steering text.

        Yields, in order:
          progress(planning_started) →
          [hook(plan_ready)?] →
          [progress(replanning?)] →
          [hook(cost_estimate_required)?] →
          progress(executing) →
          hook(step_completed) per step →
          progress(synthesizing) →
          done(payload={"final_answer": ..., "footnotes": [...]})
        """
        try:
            yield from self._run_inner(inputs)
        except BudgetExceeded as exc:
            yield SubgraphEvent(
                kind="error",
                name="budget_exceeded",
                payload={
                    "error":  str(exc),
                    "kind":   getattr(exc, "kind", ""),
                    "used":   getattr(exc, "used", 0),
                    "cap":    getattr(exc, "cap", 0),
                },
            )
        except EvidenceCapExceeded as exc:
            yield SubgraphEvent(
                kind="error",
                name="evidence_cap_exceeded",
                payload={"error": str(exc), "kind": "evidence_entries"},
            )
        except Exception as exc:  # noqa: BLE001 — never let the SM crash
            yield SubgraphEvent(
                kind="error",
                name="unexpected",
                payload={
                    "error":     str(exc),
                    "kind":      type(exc).__name__,
                    "traceback": traceback.format_exc(),
                },
            )

    def resume(
        self, inputs: dict, event: SubgraphEvent
    ) -> Generator[SubgraphEvent, Any, None]:
        """Resume after a hook target node completes.

        v1 only supports re-running ``run`` from scratch when the runner
        chooses to (cost-gate denial, etc.). The framework itself preserves
        generator state for in-process resumption; this method exists for
        cross-process resumption from persisted inputs and is currently
        treated as a fresh run with the original inputs unless the hook
        result says otherwise.
        """
        # v1: simplest re-entry — defer to the runner's saved generator
        # if it exists, otherwise behave like a fresh run.
        if event.kind == "hook" and event.name == "cost_estimate_required":
            payload = event.payload or {}
            if not payload.get("approve", True):
                yield SubgraphEvent(
                    kind="error",
                    name="cost_gate_denied",
                    payload={"error": "user denied cost estimate"},
                )
                return
        # Default: re-drive from scratch. Phase 6 hardens this to true
        # mid-flight resumption.
        yield from self.run(inputs)

    # ── Internal driver ─────────────────────────────────────────────────

    def _run_inner(self, inputs: dict) -> Generator[SubgraphEvent, Any, None]:
        self._inputs = dict(inputs or {})
        query = str(self._inputs.get("query") or "").strip()
        if not query:
            yield SubgraphEvent(
                kind="error",
                name="missing_query",
                payload={"error": "PlanExecuteAgent.run requires inputs['query']"},
            )
            return

        # Lazily provision per-run state so a single agent instance can be
        # re-used across queries without bleed.
        self._plan = None
        self._store = EvidenceStore()
        self._budget = BudgetTracker()

        registry = self.tool_registry()

        # ─── Plan v1 ────────────────────────────────────────────────────
        yield SubgraphEvent(
            kind="progress",
            name="planning_started",
            payload={"query": query, "version": 1},
        )

        plan = self._call_planner(query=query, registry=registry, replan_hint="")
        if plan is None or not plan.steps:
            yield SubgraphEvent(
                kind="error",
                name="planner_no_plan",
                payload={"error": "planner returned no plan or zero steps"},
            )
            return
        self._plan = plan

        # ─── Critic-pre on v1 ───────────────────────────────────────────
        cp = critic_pre(plan, self._llm)
        if cp.verdict in ("revise", "replan"):
            yield SubgraphEvent(
                kind="progress",
                name="critic_pre_revise",
                payload={"reason": cp.reason},
            )
            self._budget.charge_replan()
            plan = self._call_planner(
                query=query, registry=registry, replan_hint=cp.reason
            )
            if plan is None or not plan.steps:
                yield SubgraphEvent(
                    kind="error",
                    name="planner_no_plan_after_critic_pre",
                    payload={"error": "planner returned empty plan after critic_pre"},
                )
                return
            self._plan = plan

        # ─── Validator ──────────────────────────────────────────────────
        vr = validate_plan(plan, registry, self._llm)
        replan_attempts = 0
        while not vr.ok and replan_attempts < int(getattr(config, "RESEARCH_MAX_REPLANS", 3)):
            yield SubgraphEvent(
                kind="progress",
                name="validator_revise",
                payload={"issues": list(vr.issues)},
            )
            self._budget.charge_replan()
            replan_attempts += 1
            plan = self._call_planner(
                query=query,
                registry=registry,
                replan_hint="; ".join(vr.issues),
            )
            if plan is None or not plan.steps:
                yield SubgraphEvent(
                    kind="error",
                    name="planner_no_plan_after_validator",
                    payload={"error": "planner returned empty plan after validator"},
                )
                return
            self._plan = plan
            vr = validate_plan(plan, registry, self._llm)

        if not vr.ok:
            yield SubgraphEvent(
                kind="error",
                name="validator_failed",
                payload={"issues": list(vr.issues)},
            )
            return

        # ─── plan_ready hook (default = ok) ─────────────────────────────
        plan_ready_response = yield from self._maybe_hook(
            "plan_ready",
            payload={
                "plan_summary": plan.notes or plan.goal,
                "step_count":   len(plan.steps),
            },
        )
        if isinstance(plan_ready_response, dict):
            decision = str(plan_ready_response.get("decision") or "ok").lower()
            if decision == "revise":
                yield SubgraphEvent(
                    kind="error",
                    name="plan_ready_revise",
                    payload={"error": "user/reviewer asked to revise the plan"},
                )
                return

        # ─── Cost gate ──────────────────────────────────────────────────
        est_seconds = estimate_plan_seconds(plan)
        threshold = float(getattr(config, "RESEARCH_LONG_LATENCY_THRESHOLD_SECONDS", 600))
        if est_seconds > threshold:
            deep_dives = sum(1 for s in plan.steps if s.task_kind == "deep_dive")
            cost_response = yield from self._maybe_hook(
                "cost_estimate_required",
                payload={
                    "estimated_minutes": int(est_seconds // 60),
                    "step_count":        len(plan.steps),
                    "deep_dives":        deep_dives,
                },
            )
            if isinstance(cost_response, dict) and not cost_response.get("approve", True):
                yield SubgraphEvent(
                    kind="error",
                    name="cost_gate_denied",
                    payload={"error": "user denied cost estimate"},
                )
                return

        # ─── Execute → critic_post → maybe replan ───────────────────────
        max_replans = int(getattr(config, "RESEARCH_MAX_REPLANS", 3))
        post_replans = 0
        executed_step_ids: set[str] = set()

        while True:
            yield SubgraphEvent(
                kind="progress",
                name="executing",
                payload={"plan_version": plan.version, "step_count": len(plan.steps)},
            )

            # Run only the steps not yet executed (replans append-only).
            pending_steps = [s for s in plan.steps if s.id not in executed_step_ids]
            yield from self._execute_steps(pending_steps, registry)
            for s in pending_steps:
                executed_step_ids.add(s.id)

            yield SubgraphEvent(
                kind="progress",
                name="critic_post_started",
                payload={"plan_version": plan.version},
            )
            cpost: CriticResult = critic_post(plan, self._store, self._llm)

            if cpost.verdict != "replan":
                break

            if post_replans >= max_replans:
                # Force-synthesize anyway per §11.
                yield SubgraphEvent(
                    kind="progress",
                    name="critic_post_replan_capped",
                    payload={"reason": cpost.reason, "replans": post_replans},
                )
                break

            try:
                self._budget.charge_replan()
            except BudgetExceeded:
                yield SubgraphEvent(
                    kind="progress",
                    name="critic_post_replan_capped",
                    payload={"reason": cpost.reason, "replans": post_replans},
                )
                break

            yield SubgraphEvent(
                kind="progress",
                name="replanning",
                payload={"reason": cpost.reason, "replan_attempt": post_replans + 1},
            )
            new_plan = self._call_planner(
                query=query,
                registry=registry,
                replan_hint=cpost.reason,
                prior_plan=plan,
            )
            if new_plan is None or not new_plan.steps:
                # Couldn't extend — synthesize what we have.
                break

            # Append-only: keep prior steps, only add brand-new step IDs.
            delta = [s for s in new_plan.steps if s.id not in {p.id for p in plan.steps}]
            if not delta:
                break
            try:
                plan.replan(delta)
            except ValueError:
                # Append-only contract violated by the planner — skip the
                # replan rather than crash.
                break
            self._plan = plan
            post_replans += 1

        # ─── Synthesize ─────────────────────────────────────────────────
        yield SubgraphEvent(
            kind="progress",
            name="synthesizing",
            payload={"plan_version": plan.version},
        )
        final_answer = synthesize(query, plan, self._store, self._llm)

        footnotes = self._collect_footnotes()

        yield SubgraphEvent(
            kind="done",
            name="done",
            payload={
                "final_answer": final_answer,
                "footnotes":    footnotes,
            },
        )

    # ── Step execution ──────────────────────────────────────────────────

    def _execute_steps(
        self, steps: list[Step], registry: ToolRegistry
    ) -> Generator[SubgraphEvent, Any, None]:
        """Run *steps* through the DAGExecutor, push envelopes into store,
        emit step_completed hook per step.
        """
        if not steps:
            return

        # The worker function: build a Scratchpad, call execute_step,
        # return the envelope. The DAGExecutor handles dep-waiting + the
        # deep-dive single-slot lane.
        def _worker(step: Step) -> ToolEnvelope:
            envelope = self._dispatch_step(step, registry)
            return envelope

        # Run the DAG and collect results in completion order.
        with DAGExecutor() as dag:
            for step in steps:
                dag.submit(step, _worker)
            # Drain results synchronously; surface step_completed hooks as
            # SubgraphEvents on the way out.
            for step_id, result in dag.results():
                if isinstance(result, BaseException):
                    # Worker raised — translate to an error envelope and
                    # store it so the post-critic can react.
                    error_env = ToolEnvelope(
                        summary=f"step {step_id} failed: {result!r}",
                        full="",
                        metadata={"kind": "error", "source": "dag_executor", "count": 0},
                        provenance={"step_id": step_id},
                        error="dag_worker_exception",
                    )
                    entry_id = self._add_evidence(step_id, "internal:error", error_env)
                else:
                    entry_id = self._add_evidence(step_id, _tool_name_from_envelope(result), result)

                yield SubgraphEvent(
                    kind="hook",
                    name="step_completed",
                    payload={
                        "step_id":      step_id,
                        "evidence_ids": [entry_id] if entry_id else [],
                    },
                )

    def _dispatch_step(self, step: Step, registry: ToolRegistry) -> ToolEnvelope:
        """Run one step through ``execute_step`` with the right wiring.

        The ``expand`` pseudo-tool branch is intercepted here: when the
        executor LLM picks ``expand(evidence_id=...)``, we read the full
        payload from the EvidenceStore and surface it as a fresh envelope
        whose ``full`` carries the rehydrated payload, so the executor's
        second turn can summarise it.
        """
        envelope = execute_step(
            step=step,
            registry=registry,
            store=self._store,
            llm_call=self._llm,
            budget_tracker=self._budget,
        )

        # If the executor invoked `expand`, the envelope's metadata says so.
        # Phase 5 contract: the agent itself dispatches expand by reading
        # from the store and substituting the full payload.
        if (
            isinstance(envelope.metadata, dict)
            and envelope.metadata.get("kind") == "expand"
        ):
            ev_id = envelope.metadata.get("evidence_id") or envelope.provenance.get("evidence_id")
            if isinstance(ev_id, str) and self._store is not None:
                rehydrated = self._dispatch_expand(ev_id)
                # Replace the envelope's body with the rehydrated payload;
                # keep the executor LLM's summary so the evidence entry
                # stays meaningful.
                envelope = ToolEnvelope(
                    summary=envelope.summary,
                    full=rehydrated,
                    metadata={
                        "kind":       "expand",
                        "source":     "evidence_store",
                        "evidence_id": ev_id,
                    },
                    provenance={"evidence_id": ev_id, "step_id": step.id},
                )
        return envelope

    def _dispatch_expand(self, evidence_id: str) -> str:
        """Implementation of the ``expand`` pseudo-tool dispatch.

        Reads the entry from the EvidenceStore (transparently rehydrating
        spilled payloads from disk) and returns the ``full`` field as a
        string. Returns an empty string when the entry is missing.
        """
        if self._store is None:
            return ""
        entry = self._store.get(evidence_id)
        if entry is None:
            return ""
        return entry.envelope.full or ""

    # ── Evidence helpers ────────────────────────────────────────────────

    def _add_evidence(
        self, step_id: str, tool_name: str, envelope: ToolEnvelope
    ) -> str:
        """Wrap an envelope in an EvidenceEntry and add it to the store."""
        if self._store is None:
            return ""
        entry = EvidenceEntry(
            id="",
            tool_name=tool_name or "",
            step_id=step_id or "",
            envelope=envelope,
        )
        try:
            return self._store.add(entry)
        except EvidenceCapExceeded:
            # Re-raise so the outer try/except in run() can yield a clean
            # error event. Otherwise we'd swallow the cap silently.
            raise

    def _collect_footnotes(self) -> list[dict]:
        """Build the list of evidence references the synthesizer cited.

        v1 returns *all* entries; the UI side filters by which ``[ev_xxx]``
        markers actually appear in the answer text. Phase 6 will tighten
        this to "only the cited ones".
        """
        if self._store is None:
            return []
        out: list[dict] = []
        for entry in self._store.iter():
            env = entry.envelope
            out.append({
                "id":         entry.id,
                "tool_name":  entry.tool_name,
                "step_id":    entry.step_id,
                "summary":    env.summary or "",
                "metadata":   env.metadata or {},
                "provenance": env.provenance or {},
                "truncated":  bool(env.truncated),
                "error":      env.error,
            })
        return out

    # ── Planner LLM call ───────────────────────────────────────────────

    def _call_planner(
        self,
        *,
        query: str,
        registry: ToolRegistry,
        replan_hint: str = "",
        prior_plan: Plan | None = None,
    ) -> Plan | None:
        """Invoke the planner LLM and parse the resulting JSON into a Plan."""
        prompt = self._render_prompt(
            "planner",
            "planner.md",
            params={
                "goal":           query,
                "max_steps_v1":   int(getattr(config, "RESEARCH_MAX_PLAN_STEPS_V1", 8)),
                "max_deep_dives": int(getattr(config, "RESEARCH_MAX_DEEP_DIVES_PER_PLAN", 3)),
                "plan_schema":    json.dumps(PLAN_JSON_SCHEMA, ensure_ascii=False, indent=2),
                "tool_catalogue": json.dumps(
                    list_tools_for_planner(registry), ensure_ascii=False, indent=2
                ),
                "evidence_view":  json.dumps(
                    self._summary_view_dict(), ensure_ascii=False, indent=2
                ),
                "replan_hint":    f"\nReplan hint: {replan_hint}" if replan_hint else "",
            },
        )

        try:
            raw = self._llm(
                model=config.PLANNER_MODEL,
                prompt=prompt,
                response_format={"type": "json_object"},
            )
        except Exception:  # noqa: BLE001 — return None and let caller emit error
            return None

        parsed = _parse_plan_json(raw)
        if not isinstance(parsed, dict):
            return None
        new_version = (prior_plan.version + 1) if prior_plan is not None else 1
        return _make_plan_from_dict(parsed, version=new_version)

    def _summary_view_dict(self) -> list[dict]:
        if self._store is None:
            return []
        out: list[dict] = []
        for entry in self._store.iter():
            env = entry.envelope
            out.append({
                "id":         entry.id,
                "tool_name":  entry.tool_name,
                "step_id":    entry.step_id,
                "summary":    env.summary or "",
                "metadata":   env.metadata or {},
                "provenance": env.provenance or {},
            })
        return out

    # ── Prompt rendering ────────────────────────────────────────────────

    def _render_prompt(
        self,
        role: str,
        filename: str,
        params: dict,
    ) -> str:
        """Load the generic prompt for *role*, append the subclass addendum
        (if any), and ``.format(**params)`` it.

        Cached per role so repeated planner calls don't re-read disk.
        """
        if role not in self._resolved_prompts:
            generic = _load_generic_prompt(filename)
            addendum = (self.prompt_addenda() or {}).get(role, "")
            if addendum:
                # Two blank lines between the generic prompt body and the
                # addendum so the addendum reads as a separate section.
                merged = f"{generic.rstrip()}\n\n{addendum.lstrip()}"
            else:
                merged = generic
            self._resolved_prompts[role] = merged
        # ``str.format`` complains about stray ``{}`` in JSON examples; we
        # use a defensive variant that leaves unknown braces alone.
        return _safe_format(self._resolved_prompts[role], params)

    # ── Hook helper ─────────────────────────────────────────────────────

    def _maybe_hook(
        self,
        name: str,
        payload: dict,
    ) -> Generator[SubgraphEvent, Any, dict | None]:
        """Yield a hook event ONLY if ``name`` is wired in EVENTS.

        Returns the hook target's response payload via the generator's
        send/return protocol — Phase 6 wires the runner to drive this.
        For v1 / pre-Phase-6, we yield the event and treat any pre-defined
        EVENTS as auto-approved (return None).
        """
        if name not in self.EVENTS:
            return None
        yield SubgraphEvent(kind="hook", name=name, payload=dict(payload))
        # The runner is expected to either call ``resume`` or, for fire-
        # and-forget hooks like step_completed, do nothing. We can't know
        # the answer from here without bidirectional generator semantics
        # the runner would need to wire up; v1 returns None and the caller
        # treats it as "default behaviour" per §3.2.3.
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FORMAT_TOKEN_RE = re.compile(r"\{(\w+)\}")


def _safe_format(template: str, params: dict) -> str:
    """``str.format`` substitute that ignores unknown placeholders.

    Plain ``template.format(**params)`` will KeyError on any ``{foo}``
    that isn't in *params* — including stray braces in inlined JSON
    examples. This walks named placeholders only, leaving everything else
    (including bare ``{}`` and ``{{...}}`` sequences) untouched.
    """
    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key in params:
            return str(params[key])
        return m.group(0)
    return _FORMAT_TOKEN_RE.sub(repl, template)


def _tool_name_from_envelope(envelope: ToolEnvelope) -> str:
    """Best-effort tool-name extraction from a returned envelope."""
    if envelope is None:
        return ""
    prov = envelope.provenance or {}
    name = prov.get("tool_name") if isinstance(prov, dict) else ""
    return str(name or "")


__all__ = ["PlanExecuteAgent"]
