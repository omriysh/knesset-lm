"""Generic SubgraphAgent contract.

Defines the abstract base class every subgraph implementation inherits from,
plus the SubgraphEvent type both sides of the contract speak. This module is
intentionally generic: it knows nothing about plans, tools, or evidence.

Per design doc §3.2.1, the three-tier hierarchy is:

    SubgraphAgent (here)
       └─► PlanExecuteAgent  (agent/plan_execute/agent.py — Phase 4)
              └─► ResearchAgent  (agent/research_agent/agent.py — Phase 5)

This module corresponds to the top tier only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generator


@dataclass
class SubgraphEvent:
    """A single event yielded by a subgraph generator.

    Per §3.2.1:
      - kind: "progress" | "hook" | "done" | "error"
      - name: hook name when kind == "hook" (may be empty otherwise)
      - payload: progress / hook payload, or final outputs when kind == "done"
    """

    kind: str
    name: str
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "payload": dict(self.payload) if self.payload is not None else {},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SubgraphEvent":
        return cls(
            kind=data.get("kind", ""),
            name=data.get("name", ""),
            payload=dict(data.get("payload") or {}),
        )


class SubgraphAgent:
    """Abstract contract every subgraph implementation must obey.

    Subclasses declare the hook names they may yield in ``EVENTS`` and the
    context keys they write on completion in ``OUTPUT_VARS``. They implement
    ``run`` (initial entry) and ``resume`` (continuation after a hook target
    node returns) as generators that yield SubgraphEvent objects.

    The generator protocol — not return values — is how a subgraph
    communicates with the runner. The final event of a successful run is
    ``SubgraphEvent(kind="done", ...)`` whose payload contains the output
    variables; on failure the generator yields ``kind="error"``.
    """

    # Hook names this subgraph may yield. Subclasses override.
    EVENTS: set[str] = set()

    # Context keys this subgraph writes when it yields a "done" event.
    OUTPUT_VARS: list[str] = []

    def run(self, inputs: dict) -> Generator[SubgraphEvent, Any, None]:
        """Start a fresh subgraph run.

        Args:
            inputs: dict of input variables, keyed by names declared in the
                outer state-machine node's ``data.input_vars``.

        Yields:
            SubgraphEvent values describing progress, hooks, the final
            ``done`` event, or an ``error`` event.
        """
        raise NotImplementedError

    def resume(
        self, inputs: dict, event: SubgraphEvent
    ) -> Generator[SubgraphEvent, Any, None]:
        """Continue a paused subgraph run after a hook target node returned.

        Args:
            inputs: the original inputs the run was started with (the runner
                passes them through so a subgraph can be re-hydrated from
                persisted state without keeping its generator alive).
            event: the event describing the hook result. ``event.name`` is
                the hook name; ``event.payload`` is the target node's output.

        Yields:
            SubgraphEvent values, same protocol as ``run``.
        """
        raise NotImplementedError
