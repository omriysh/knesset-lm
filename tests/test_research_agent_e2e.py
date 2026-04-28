"""
test_research_agent_e2e.py

End-to-end smoke tests for ResearchAgent with mocked llm_call.

IMPORTANT: This module skips entirely if the BM25 mks.db is not built.
The tests do NOT make real LLM calls — they use fixed mock responses
that simulate a minimal plan → execute → synthesize cycle.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

# Skip the entire module if BM25 dbs are missing.
_bm25_db = Path("C:/Work/Projects/KnessetLM/Data/bm25/25/mks.db")
if not _bm25_db.exists():
    pytest.skip(
        f"BM25 dbs not built — expected {_bm25_db}",
        allow_module_level=True,
    )

import config
from agent.research_agent.agent import ResearchAgent
from agent.plan_execute.agent import PlanExecuteAgent, _LLMBridge
from agent.subgraph.base import SubgraphEvent


# ── Mock LLM factories ────────────────────────────────────────────────────────

_VALID_PLAN_JSON = json.dumps({
    "goal": "מה דעתו של אבי דיכטר על שירות חוץ",
    "notes": "Single-step plan to find MK",
    "steps": [
        {
            "id": "s1",
            "task": "Find MK Dichter",
            "task_kind": "discover",
            "allowed_tools": ["find_mk"],
            "args_hint": {"query": "דיכטר"},
            "deps": [],
            "replan_after": False,
            "expected_evidence": "MK profile for Dichter",
            "cost_hint": "cheap",
        }
    ],
})

_CRITIC_PRE_OK = json.dumps({"verdict": "ok", "reason": "Plan looks good."})
_CRITIC_POST_SYNTHESIZE = json.dumps({"verdict": "synthesize", "reason": "Enough evidence."})
_VALIDATOR_OK = json.dumps({"verdict": "ok", "reason": "Name is specific enough."})
_SYNTHESIZER_ANSWER = "אבי דיכטר תמך בשירות חוץ בנאומים שונים בוועדה."


def _make_mock_llm_bridge():
    """Create a mock _LLMBridge that returns fixed responses per role.

    The mock tracks call count to sequence the responses:
      - Planner call → returns valid plan JSON
      - Critic pre call → returns ok
      - Validator helper call → returns ok
      - Executor turn 1 → tool call for find_mk
      - Executor turn 2 → record_evidence produced
      - Critic post call → synthesize
      - Synthesizer call → Hebrew answer text
    """

    call_log = []

    class MockLLMBridge:
        def __call__(
            self,
            *,
            model: str,
            prompt: str | list | None = None,
            messages: list | None = None,
            tools: list | None = None,
            response_format: dict | None = None,
            temperature: float | None = None,
            max_tokens: int | None = None,
        ):
            call_index = len(call_log)
            call_log.append({"model": model, "has_tools": tools is not None})

            # If tools are present → executor turn (tool call or record_evidence)
            if tools:
                tool_names = [
                    (t.get("function") or {}).get("name", "")
                    for t in (tools or [])
                ]
                # If record_evidence is the ONLY tool available → turn 2
                if tool_names == ["record_evidence"]:
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "record_evidence",
                                    "arguments": json.dumps({
                                        "decision": "produced",
                                        "summary": "Found MK Dichter in BM25 index",
                                        "ref_evidence": None,
                                    }),
                                }
                            }
                        ],
                    }
                else:
                    # Turn 1 → emit a find_mk call if allowed
                    allowed = [n for n in tool_names if n not in ("record_evidence", "expand")]
                    tool_to_call = "find_mk" if "find_mk" in allowed else (allowed[0] if allowed else "record_evidence")

                    if tool_to_call == "record_evidence":
                        return {
                            "content": "",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "record_evidence",
                                        "arguments": json.dumps({
                                            "decision": "produced",
                                            "summary": "No tool available",
                                        }),
                                    }
                                }
                            ],
                        }

                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": tool_to_call,
                                    "arguments": json.dumps({"query": "דיכטר"}),
                                }
                            }
                        ],
                    }

            # No tools → text response
            # response_format=json_object → planner / critic / validator
            prompt_text = prompt if isinstance(prompt, str) else ""

            # Heuristic routing by model or prompt content
            if model == config.PLANNER_MODEL or "plan_schema" in prompt_text:
                return _VALID_PLAN_JSON

            if model == config.CRITIC_PRE_MODEL or "critic" in prompt_text.lower():
                # critic_pre or critic_post
                if "synthesize" in prompt_text.lower() or "evidence" in prompt_text.lower():
                    return _CRITIC_POST_SYNTHESIZE
                return _CRITIC_PRE_OK

            if model == config.CRITIC_POST_MODEL:
                return _CRITIC_POST_SYNTHESIZE

            if model == config.SYNTHESIZER_MODEL or "synthesizer" in prompt_text.lower():
                return _SYNTHESIZER_ANSWER

            if model == getattr(config, "INTENT_MODEL", "local"):
                return _VALIDATOR_OK

            # Default: return ok json
            return _CRITIC_PRE_OK

    return MockLLMBridge()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestResearchAgentE2E:
    def _run_agent(self, query: str):
        """Helper: run the agent and collect all events."""
        mock_bridge = _make_mock_llm_bridge()

        agent = ResearchAgent(llm_bridge=mock_bridge)
        events = list(agent.run({"query": query}))
        return events

    def test_q1_find_mk_yields_events(self):
        """Agent should yield at least one SubgraphEvent for any query."""
        events = self._run_agent("מה דעתו של אבי דיכטר על שירות חוץ?")
        assert len(events) > 0

    def test_q1_find_mk_all_events_are_subgraph_events(self):
        """All yielded objects must be SubgraphEvent instances."""
        events = self._run_agent("מה דעתו של אבי דיכטר על שירות חוץ?")
        for ev in events:
            assert isinstance(ev, SubgraphEvent), f"Got non-SubgraphEvent: {ev!r}"

    def test_q1_find_mk_terminates_with_done_or_error(self):
        """Agent must eventually yield a 'done' or 'error' event — no infinite loop."""
        events = self._run_agent("מה דעתו של אבי דיכטר על שירות חוץ?")
        final_kinds = {ev.kind for ev in events}
        # Must end with either done or error
        assert "done" in final_kinds or "error" in final_kinds, (
            f"Agent did not terminate. Final event kinds: {final_kinds}"
        )

    def test_q1_planning_started_event_present(self):
        """Agent should emit a 'planning_started' progress event early."""
        events = self._run_agent("מה דעתו של אבי דיכטר על שירות חוץ?")
        names = [ev.name for ev in events]
        assert "planning_started" in names

    def test_missing_query_returns_error_event(self):
        """Empty query should yield an error, not crash."""
        mock_bridge = _make_mock_llm_bridge()
        agent = ResearchAgent(llm_bridge=mock_bridge)
        events = list(agent.run({"query": ""}))
        kinds = [ev.kind for ev in events]
        assert "error" in kinds

    def test_done_event_has_final_answer(self):
        """If agent yields 'done', the payload must contain 'final_answer'."""
        events = self._run_agent("מה דעתו של אבי דיכטר על שירות חוץ?")
        done_events = [ev for ev in events if ev.kind == "done"]
        if done_events:
            for ev in done_events:
                assert "final_answer" in ev.payload

    def test_done_event_has_footnotes(self):
        """If agent yields 'done', the payload must contain 'footnotes'."""
        events = self._run_agent("מה דעתו של אבי דיכטר על שירות חוץ?")
        done_events = [ev for ev in events if ev.kind == "done"]
        if done_events:
            for ev in done_events:
                assert "footnotes" in ev.payload

    def test_error_event_has_error_key(self):
        """Error events must carry an 'error' key in payload."""
        events = self._run_agent("מה דעתו של אבי דיכטר על שירות חוץ?")
        error_events = [ev for ev in events if ev.kind == "error"]
        for ev in error_events:
            assert "error" in ev.payload

    def test_subgraph_event_kinds_are_valid(self):
        """All event kinds must be one of the known values."""
        valid_kinds = {"progress", "hook", "done", "error"}
        events = self._run_agent("מה דעתו של אבי דיכטר על שירות חוץ?")
        for ev in events:
            assert ev.kind in valid_kinds, (
                f"Unexpected event kind {ev.kind!r} in event {ev!r}"
            )
