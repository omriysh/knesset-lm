"""
tests/test_research_registry.py

RESEARCH_TOOL_REGISTRY after the keyword-only transition: exactly 8 tools,
schemas/defaults per spec, removed handlers/modules/config gone.
"""

import importlib.util
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
import utils.tools as tools
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from retrieval import knesset_db_store as store


EXPECTED_TOOLS = {
    "find_mk":                "handle_find_mk",
    "find_committee":         "handle_find_committee",
    "find_party":             "handle_find_party",
    "query_protocols":        "handle_query_protocols",
    "get_meeting_attendance": "handle_get_meeting_attendance",
    "query_bills":            "handle_query_bills",
    "get_bill":               "handle_get_bill",
    "query_votes":            "handle_query_votes",
}

REMOVED_HANDLERS = [
    "handle_search_topics", "handle_search_opinions", "handle_search_protocols_keyword",
    "handle_get_meeting_summary", "handle_get_committee_sessions", "handle_deep_dive_meeting",
    "handle_find_bill", "handle_find_vote", "handle_query_voting_records",
    "handle_get_bill_details", "handle_get_bill_text",
]

REMOVED_MODULES = ["retrieval.protocol_rag", "retrieval.deep_dive", "retrieval.hybrid", "indexing.embedder"]


def _spec(name):
    return next(s for s in RESEARCH_TOOL_REGISTRY if s.name == name)


def _props(name):
    return _spec(name).schema["properties"]


def _required(name):
    return set(_spec(name).schema.get("required") or [])


class TestRoster:
    def test_exactly_the_eight_tools(self):
        names = [s.name for s in RESEARCH_TOOL_REGISTRY]
        assert len(names) == 8
        assert set(names) == set(EXPECTED_TOOLS)

    @pytest.mark.parametrize("tool,handler", sorted(EXPECTED_TOOLS.items()))
    def test_handler_lives_in_utils_tools(self, tool, handler):
        assert _spec(tool).handler is getattr(tools, handler)

    @pytest.mark.parametrize("handler", REMOVED_HANDLERS)
    def test_removed_handler_not_importable(self, handler):
        assert not hasattr(tools, handler)

    @pytest.mark.parametrize("module", REMOVED_MODULES)
    def test_removed_module_deleted(self, module):
        assert importlib.util.find_spec(module) is None


class TestFindSchemas:
    @pytest.mark.parametrize("tool,top_k", [("find_mk", 5), ("find_committee", 5), ("find_party", 3)])
    def test_unchanged(self, tool, top_k):
        assert _required(tool) == {"query"}
        assert _props(tool)["knesset_num"]["default"] == 25
        assert _props(tool)["top_k"]["default"] == top_k


class TestQueryProtocolsSchema:
    def test_nothing_required(self):
        assert _required("query_protocols") == set()

    def test_params(self):
        assert set(_props("query_protocols")) == {
            "query", "search_in", "mk_id", "party", "committees", "meeting_ids",
            "date_from", "date_to", "sort", "top_k", "offset", "knesset_num",
        }

    def test_search_in_array_of_scopes_default_all(self):
        p = _props("query_protocols")["search_in"]
        assert p["type"] == "array"
        assert set(p["items"]["enum"]) == {"topics", "opinions", "speeches"}
        assert set(p["default"]) == {"topics", "opinions", "speeches"}

    def test_list_filters_are_arrays(self):
        props = _props("query_protocols")
        assert props["committees"]["type"] == "array"
        assert props["meeting_ids"]["type"] == "array"

    def test_sort_enum(self):
        assert set(_props("query_protocols")["sort"]["enum"]) == {"relevance", "date"}

    def test_top_k_offset_knesset_defaults(self):
        props = _props("query_protocols")
        assert props["top_k"]["default"] == config.QUERY_PROTOCOLS_DEFAULT_TOP_K == 50
        assert props["top_k"]["maximum"] == config.QUERY_PROTOCOLS_MAX_TOP_K
        assert props["top_k"]["minimum"] == 1
        assert props["offset"]["default"] == 0
        assert props["knesset_num"]["default"] == 25

    def test_ui_enriches_meeting_id(self):
        assert _spec("query_protocols").ui["enrich_fields"] == ["meeting_id"]

    def test_compact_spec_does_not_truncate_text(self):
        cs = _spec("query_protocols").compact_spec or {}
        assert "max_chars" not in cs
        assert "text_fields" not in (cs.get("item_spec") or {})

    def test_description_teaches_usage(self):
        desc = _spec("query_protocols").schema["description"]
        for needle in ("meeting_ids", "search_in", "speeches", "find_mk"):
            assert needle in desc


class TestOtherSchemas:
    def test_get_meeting_attendance(self):
        assert _required("get_meeting_attendance") == {"meeting_id"}
        assert "meeting_id" in _props("get_meeting_attendance")

    def test_query_bills(self):
        props = _props("query_bills")
        assert _required("query_bills") == {"query"}
        assert props["knesset_num"]["default"] == 25
        assert props["top_k"]["default"] == 10

    def test_get_bill(self):
        props = _props("get_bill")
        assert _required("get_bill") == {"bill_id"}
        assert props["include_text"]["default"] is False
        assert props["max_chars"]["default"] == config.BILL_TEXT_DEFAULT_MAX_CHARS
        assert props["max_chars"]["minimum"] == config.BILL_TEXT_MIN_MAX_CHARS
        assert props["max_chars"]["maximum"] == config.BILL_TEXT_MAX_MAX_CHARS
        assert props["knesset_num"]["default"] == 25

    def test_query_votes(self):
        props = _props("query_votes")
        assert _required("query_votes") == set()
        assert {"query", "mk_id", "knesset_num", "top_k"} <= set(props)
        assert "topic" not in props and "top_n" not in props
        assert props["knesset_num"]["default"] == 25
        assert props["top_k"]["default"] == 20


class TestConfigAndStore:
    def test_query_protocols_constants(self):
        assert config.QUERY_PROTOCOLS_DEFAULT_TOP_K == 50
        assert config.QUERY_PROTOCOLS_MAX_TOP_K >= config.QUERY_PROTOCOLS_DEFAULT_TOP_K

    @pytest.mark.parametrize("name", [
        "KEYWORD_RERANK_TOP_K", "HYBRID_FIRST_STAGE_TOP_K", "CHROMA_DIR",
        "BULLETS_COLLECTION", "PASS1_COLLECTION", "PASS2_COLLECTION",
    ])
    def test_rag_constants_removed(self, name):
        assert not hasattr(config, name)

    def test_bills_votes_targets_removed(self):
        assert "bills" not in store.TARGET_TABLES and "votes" not in store.TARGET_TABLES
        assert not hasattr(store, "insert_bills") and not hasattr(store, "insert_votes")

    def test_schema_has_no_bills_votes_tables(self, tmp_path):
        conn = store.connect(tmp_path / "k.db")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        assert not names & {"bills", "bills_fts", "votes", "votes_fts"}

    @pytest.mark.parametrize("target", ["bills", "votes"])
    def test_name_entries_has_no_bill_vote_targets(self, tmp_path, target):
        conn = store.connect(tmp_path / "k.db")
        try:
            with pytest.raises(ValueError):
                store.name_entries(conn, target, 25)
        finally:
            conn.close()


class TestRunnerHasNoRetriever:
    def test_machine_runner_takes_no_retriever(self):
        from agent.runner import MachineRunner
        assert "retriever" not in inspect.signature(MachineRunner.__init__).parameters
