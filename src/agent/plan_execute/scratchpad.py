"""
scratchpad.py

Per-step transient buffer for the executor. Lives only during the executor's
run of one step. The executor reads from it freely; nobody else does.

EvidenceEntry objects always live in the EvidenceStore — the scratchpad
holds only the *ids* of evidence entries it has pushed there.

See: Documentation/KnessetLM/Development/Claude/plan-and-execute-design.md §4.4
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.plan_execute.plan import Step


@dataclass
class Scratchpad:
    """Per-step transient buffer. Discarded when the step ends.

    Fields per design §4.4:
      step:              the Step being executed
      executor_messages: the running LLM conversation for this step
      tool_calls_made:   raw call records (name, args, envelope)
      decision:          "skip" | "produced" | "abort_step" | None
      evidence_ids:      ids of EvidenceEntry rows this step pushed to the store
                         (list — supports the future relaxation of the
                         one-call-per-step rule; v1 holds 0 or 1)
      ref_evidence:      when decision == "skip", the previous-entry id the
                         executor decided already satisfies the step
    """

    step: Step
    executor_messages: list[dict] = field(default_factory=list)
    tool_calls_made: list[dict] = field(default_factory=list)
    decision: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
    ref_evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step.to_dict(),
            "executor_messages": list(self.executor_messages),
            "tool_calls_made": list(self.tool_calls_made),
            "decision": self.decision,
            "evidence_ids": list(self.evidence_ids),
            "ref_evidence": self.ref_evidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Scratchpad":
        return cls(
            step=Step.from_dict(d["step"]),
            executor_messages=list(d.get("executor_messages") or []),
            tool_calls_made=list(d.get("tool_calls_made") or []),
            decision=d.get("decision"),
            evidence_ids=list(d.get("evidence_ids") or []),
            ref_evidence=d.get("ref_evidence"),
        )
