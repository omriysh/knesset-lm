"""Research-agent package — domain-specific tool registry.

Per design §5.2 the *enumeration* of which tools exist for the research
domain lives here, not in ``utils/tools.py`` (which is agent-agnostic) and
not in ``agent/plan_execute/tools.py`` (which is registry-generic). A
future ``FactCheckAgent`` would bring its own registry file under
``agent/<that_agent>/tools.py``; the plan-execute view-builder code in
``agent/plan_execute/tools.py`` accepts any registry without caring which
domain it belongs to.

Public surface:
  * :data:`agent.research_agent.tools.RESEARCH_TOOL_REGISTRY` — the v1 list
    of :class:`utils.tools.ToolSpec` entries (16 user-facing + 1 planner-
    only deep-dive tool).
"""

from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY

__all__ = ["RESEARCH_TOOL_REGISTRY"]
