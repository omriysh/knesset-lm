"""
tests/test_citation_backfill.py

The synthesizer LLM copies citation quotes by hand and sometimes drops fields the UI
needs (an opinion's verbatim `quote`, `speech_idx`). backfill_protocol_citations fills
them from the cited evidence row and tags each protocol row with its source_kind
(opinion / topic / speech) so the source bubble can say what kind of source it is.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from agent.plan_execute.citation_backfill import backfill_protocol_citations
from agent.plan_execute.executor import _combine_envelopes
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from agent.subgraph.evidence import EvidenceEntry, EvidenceStore
from utils.tools import dispatch

ROTMAN_PALESTINIAN_STATE_ARGS = {"query": "מדינה פלסטינית", "mk_id": "30812", "top_k": 3}


def evidence_store_with(tmp_path, *tool_calls):
    collected = [(name, dispatch(RESEARCH_TOOL_REGISTRY, name, args)) for name, args in tool_calls]
    for name, envelope in collected:
        if envelope.error:
            pytest.skip(f"{name} unavailable: {envelope.error}")
    envelope = _combine_envelopes(collected, "s1", [{"name": n, "args": a} for n, a in tool_calls])
    store = EvidenceStore(spill_dir=str(tmp_path))
    store.add(EvidenceEntry(id="ev_1", tool_name=tool_calls[-1][0], step_id="s1", envelope=envelope))
    return store, [json.loads(env.full) for _, env in collected]


@pytest.fixture
def rotman_store(tmp_path):
    store, (rows_by_scope,) = evidence_store_with(tmp_path, ("query_protocols", ROTMAN_PALESTINIAN_STATE_ARGS))
    if not rows_by_scope.get("opinions"):
        pytest.skip("no opinion rows for the fixture query")
    return store, rows_by_scope


class TestBackfillProtocolCitations:
    def test_opinion_without_quote_gets_the_verbatim_quote_and_kind(self, rotman_store):
        store, rows_by_scope = rotman_store
        opinion_row = rows_by_scope["opinions"][0]
        citations = [{"n": 1, "ev_id": "ev_1", "quote": {
            "meeting_id": opinion_row["meeting_id"], "idx": opinion_row["idx"], "opinion": opinion_row["opinion"]}}]
        backfill_protocol_citations(citations, store)
        quote = citations[0]["quote"]
        assert quote["quote"] == opinion_row["quote"]
        assert quote["speech_idx"] == opinion_row["speech_idx"]
        assert quote["committee"] == opinion_row["committee"] and quote["date"] == opinion_row["date"]
        assert quote["source_kind"] == "opinion"

    def test_llm_values_are_kept(self, rotman_store):
        store, rows_by_scope = rotman_store
        opinion_row = rows_by_scope["opinions"][0]
        citations = [{"n": 1, "ev_id": "ev_1", "quote": {
            "meeting_id": opinion_row["meeting_id"], "idx": opinion_row["idx"], "opinion": "paraphrased"}}]
        backfill_protocol_citations(citations, store)
        assert citations[0]["quote"]["opinion"] == "paraphrased"

    def test_topic_and_speech_rows_are_tagged(self, rotman_store):
        store, rows_by_scope = rotman_store
        quotes = []
        if rows_by_scope.get("topics"):
            topic_row = rows_by_scope["topics"][0]
            quotes.append(({"meeting_id": topic_row["meeting_id"], "topic": topic_row["topic"]}, "topic"))
        if rows_by_scope.get("speeches"):
            speech_row = rows_by_scope["speeches"][0]
            quotes.append(({"meeting_id": speech_row["meeting_id"], "speech_idx": speech_row["speech_idx"],
                            "text": speech_row["text"][:50]}, "speech"))
        if not quotes:
            pytest.skip("no topic or speech rows for the fixture query")
        citations = [{"n": i, "ev_id": "ev_1", "quote": [quote]} for i, (quote, _) in enumerate(quotes)]
        backfill_protocol_citations(citations, store)
        assert [c["quote"][0]["source_kind"] for c in citations] == [kind for _, kind in quotes]

    def test_multi_call_evidence_is_searched(self, tmp_path):
        store, results = evidence_store_with(
            tmp_path, ("find_mk", {"query": "שמחה רוטמן"}), ("query_protocols", ROTMAN_PALESTINIAN_STATE_ARGS))
        opinions = results[1].get("opinions") or []
        if not opinions:
            pytest.skip("no opinion rows for the fixture query")
        citations = [{"n": 1, "ev_id": "ev_1", "quote": {"meeting_id": opinions[0]["meeting_id"],
                                                         "speech_idx": opinions[0]["speech_idx"],
                                                         "opinion": opinions[0]["opinion"]}}]
        backfill_protocol_citations(citations, store)
        assert citations[0]["quote"]["quote"] == opinions[0]["quote"]

    def test_unknown_evidence_and_non_protocol_quotes_are_left_alone(self, rotman_store):
        store, _ = rotman_store
        citations = [{"n": 1, "ev_id": "ev_missing", "quote": {"meeting_id": "1", "opinion": "x"}},
                     {"n": 2, "ev_id": "ev_1", "quote": {"vote_title": "x", "result": "y"}},
                     {"n": 3, "ev_id": "ev_1", "quote": "plain text"},
                     {"n": 4, "ev_id": "ev_1", "quote": {"meeting_id": "0", "opinion": "no such row"}}]
        before = json.loads(json.dumps(citations))
        backfill_protocol_citations(citations, store)
        assert citations == before
