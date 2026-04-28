"""Hook routing helper for subgraph nodes.

Per design §3.2.2, a subgraph node carries a ``data.hooks`` JSON config that
maps hook names (declared by the subgraph in ``EVENTS``) to sibling node ids
in the outer state machine. When the subgraph yields a ``kind="hook"`` event,
the runner pauses the subgraph, routes execution to the target node, and
later feeds the target node's output back via ``resume(...)``.

This module is the small, pure helper the runner uses to do that routing.
The runner itself (Phase 6) orchestrates the pause/resume; here we provide:

  - ``parse_hook_config``: normalise the JSON into a name → target_node mapping.
  - ``HookRouter``: given a stream of ``SubgraphEvent``, classify each into
    one of: progress (relay to UI), hook-to-route (pause and route), hook-
    unwired (default behaviour, no route), done (final outputs), error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from agent.subgraph.base import SubgraphEvent


@dataclass
class HookRoute:
    """A resolved routing decision for a single SubgraphEvent."""

    # "progress" | "route" | "unwired_hook" | "done" | "error"
    action: str
    event: SubgraphEvent
    target_node: str | None = None


def parse_hook_config(raw: object) -> dict[str, str]:
    """Normalise a node's ``data.hooks`` field into ``{event_name: node_id}``.

    Two shapes are accepted (per design + designer round-trip notes):

      1. Flat mapping: ``{"plan_ready": "plan_review_llm_node", ...}``.
      2. List of entries: ``[{"event": "plan_ready",
                               "target_node": "plan_review_llm_node", ...}, ...]``
         Extra keys on each entry (e.g. ``label``) are ignored here; the
         designer may use them but routing only needs ``event`` and
         ``target_node``.

    Returns ``{}`` for missing / null / unrecognised configs — an unwired
    hooks block is a valid state (default behaviour applies for every event).
    """
    if raw is None:
        return {}
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, str) and v:
                out[k] = v
        return out
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("event")
            target = entry.get("target_node") or entry.get("target")
            if isinstance(name, str) and isinstance(target, str) and name and target:
                out[name] = target
        return out
    return {}


class HookRouter:
    """Classify SubgraphEvent objects against a parsed hooks config.

    The runner owns the actual transition into the target node and the
    subsequent ``resume(...)`` call; this class only decides *what* should
    happen for each event.
    """

    def __init__(self, hooks: dict[str, str] | None):
        self.hooks: dict[str, str] = dict(hooks or {})

    def route(self, event: SubgraphEvent) -> HookRoute:
        """Map a single event to a HookRoute decision."""
        kind = event.kind
        if kind == "hook":
            target = self.hooks.get(event.name)
            if target:
                return HookRoute(action="route", event=event, target_node=target)
            return HookRoute(action="unwired_hook", event=event, target_node=None)
        if kind == "done":
            return HookRoute(action="done", event=event, target_node=None)
        if kind == "error":
            return HookRoute(action="error", event=event, target_node=None)
        # Anything else (progress, custom relay events, ...) flows through.
        return HookRoute(action="progress", event=event, target_node=None)

    def classify_stream(
        self, events: Iterable[SubgraphEvent]
    ) -> Iterator[HookRoute]:
        """Lazy convenience: classify each event in a stream in turn.

        The runner typically pulls events one at a time from the subgraph
        generator, so it can call ``route`` directly. This helper exists for
        tests and offline tooling that already have a list of events.
        """
        for ev in events:
            yield self.route(ev)
