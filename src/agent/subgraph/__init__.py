"""Generic subgraph contract package.

Phase 2 of the plan-and-execute agent project. Defines the abstract
``SubgraphAgent`` every research / browse / fact-check agent inherits from,
the ``SubgraphEvent`` protocol they communicate with the runner over, and
the generic ``EvidenceStore`` / ``ToolEnvelope`` types used to persist tool
outputs. No research-specific code lives here.
"""

from agent.subgraph.base import SubgraphAgent, SubgraphEvent
from agent.subgraph.evidence import (
    EvidenceCapExceeded,
    EvidenceEntry,
    EvidenceStore,
    ToolEnvelope,
)

__all__ = [
    "SubgraphAgent",
    "SubgraphEvent",
    "EvidenceEntry",
    "EvidenceStore",
    "ToolEnvelope",
    "EvidenceCapExceeded",
]
