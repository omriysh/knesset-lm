"""
tests/test_public_surface.py

The public surface agents see (MCP tools and /v1 routes) on the real Data/knesset.db through the real app:
no top_k (fixed page sizes, a sent top_k is an unknown argument), per-scope `next` paging patches
(character offsets for query_protocols),
hints with next steps, actionable 4xx bodies, MCP titles/instructions, integral floats accepted, and
the questions.jsonl line with the result row count and error code.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fastapi.testclient import TestClient

import config
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from tests.conftest import assert_within_character_budget, ok
from tests.test_mcp_endpoint import McpTestClient, allow_test_client, mcp  # noqa: F401  (fixture)
from utils.tools import ToolEnvelope

COMMON_WORD = "תקציב"
NO_SUCH_WORD = "זזזזזזזזזז"


@pytest.fixture()
def real_app(real_db, monkeypatch):
    from api.app import app, rate_limiter
    allow_test_client(monkeypatch)
    rate_limiter.reset()
    with TestClient(app) as http_client:
        yield http_client
    rate_limiter.reset()


def row_key(scope: str, row: dict) -> tuple:
    return (row["meeting_id"], row["speech_idx"] if scope == "speeches" else row["idx"])


# ── advertised schemas ───────────────────────────────────────────────────────

class TestAdvertisedSchemas:
    def test_mcp_tools_have_no_top_k_and_real_defaults(self, mcp):
        tools = {tool["name"]: tool for tool in mcp.result("tools/list")["tools"]}
        assert list(tools) == [spec.name for spec in RESEARCH_TOOL_REGISTRY]
        for tool in tools.values():
            assert "top_k" not in tool["inputSchema"].get("properties", {}), tool["name"]
            assert "top_k" not in tool["description"], tool["name"]
            assert tool["annotations"]["title"]
        protocols = tools["query_protocols"]["inputSchema"]["properties"]
        assert protocols["search_in"]["default"] == list(config.API_PROTOCOLS_DEFAULT_SCOPES)
        assert protocols["offset"]["maximum"] == config.API_PROTOCOLS_MAX_OFFSET
        assert "next" in protocols["offset"]["description"] and "Rows" in protocols["offset"]["description"]
        assert tools["query_bills"]["inputSchema"]["properties"]["offset"]["maximum"] == config.API_MAX_OFFSET
        assert "characters of whole rows per scope" in tools["query_protocols"]["description"]
        assert protocols["query"]["examples"] and protocols["party"]["examples"]
        assert protocols["committees"]["examples"] and protocols["date_from"]["examples"]
        assert "offset" in tools["query_bills"]["inputSchema"]["properties"]
        assert tools["query_protocols"]["annotations"]["title"] == "Search committee protocols"

    def test_rest_listing_and_llms_full_have_no_top_k(self, real_app):
        for tool in ok(real_app.get("/v1/tools"))["tools"]:
            assert "top_k" not in tool["parameters"].get("properties", {}), tool["name"]
        assert "top_k" not in real_app.get("/llms-full.txt").text
        assert "top_k" not in json.dumps(real_app.get("/openapi.json").json())

    def test_mcp_instructions_are_mcp_first(self, mcp, real_conn):
        instructions = mcp.result("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "pytest", "version": "1"}})["instructions"]
        assert "/v1/" not in instructions and "top_k" not in instructions
        assert "query_protocols(" in instructions and "next" in instructions
        first_date, last_date = real_conn.execute(
            f"SELECT MIN(date), MAX(date) FROM meetings WHERE knesset_num IN "
            f"({','.join('?' * len(config.PROTOCOL_KNESSET_NUMS))})", config.PROTOCOL_KNESSET_NUMS).fetchone()
        assert first_date in instructions and last_date in instructions
        low, high = config.API_KNESSET_NUM_RANGE
        assert f"Knessets {low}-{high}" in instructions and "26" not in instructions.replace(first_date, "").replace(last_date, "")

    def test_llms_txt_has_no_sketch_line(self, real_app):
        assert "Sketch" not in real_app.get("/llms.txt").text


# ── fixed page sizes, top_k not an argument ─────────────────────────────────

class TestFixedPageSizes:
    @pytest.mark.parametrize("top_k", [5, "abc", 500])
    def test_rest_top_k_changes_nothing_and_warns_nothing(self, real_app, top_k):
        params = {"q": COMMON_WORD, "search_in": "topics"}
        body = ok(real_app.get("/v1/protocols", params={**params, "top_k": top_k}))
        assert body["results"] == ok(real_app.get("/v1/protocols", params=params))["results"]
        assert body["warnings"] == []
        assert "top_k" not in body["args"]

    def test_mcp_top_k_changes_nothing_and_warns_nothing(self, mcp, real_db):
        args = {"meeting_ids": [real_db.meeting_id], "search_in": ["speeches"]}
        is_error, body = mcp.call_tool("query_protocols", {**args, "top_k": 3})
        assert not is_error
        assert body["results"] == mcp.call_tool("query_protocols", args)[1]["results"]
        assert body["warnings"] == []

    @pytest.mark.network
    @pytest.mark.parametrize("path", ["/v1/committees", "/v1/parties"])
    def test_empty_query_lists_every_committee_or_party(self, real_app, path):
        body = ok(real_app.get(path))
        assert len(body["results"]) > config.API_FIND_PAGE_SIZE
        assert "refine the query" not in body["hint"] and "use " not in body["hint"]

    def test_listing_one_meeting_uses_the_same_page_size(self, real_app, real_db):
        speeches = ok(real_app.get("/v1/protocols", params={"meeting_id": real_db.meeting_id,
                                                            "search_in": "speeches"}))
        assert speeches["warnings"] == []
        rows = speeches["results"]["speeches"]
        assert_within_character_budget(rows, config.API_PROTOCOLS_PAGE_CHARS)
        assert speeches["next"]["speeches"]["offset"] == len(rows)

    def test_no_meeting_listing_page_size_setting_is_left(self):
        assert not hasattr(config, "API_PROTOCOLS_MEETING_LISTING_PAGE_SIZE")
        assert not hasattr(config, "API_MAX_RESPONSE_CHARS")
        assert not hasattr(config, "API_MAX_ROW_TEXT_CHARS")
        assert not hasattr(config, "API_PROTOCOLS_SEARCH_PAGE_SIZE")


# ── per-scope paging ─────────────────────────────────────────────────────────

class TestPerScopePaging:
    def test_next_is_a_patch_per_full_scope_and_following_it_gives_new_rows(self, mcp):
        first_args = {"query": COMMON_WORD, "search_in": ["topics", "opinions"]}
        is_error, first = mcp.call_tool("query_protocols", first_args)
        assert not is_error
        page = len(first["results"]["opinions"])
        assert first["next"]["opinions"] == {"search_in": ["opinions"], "offset": page}
        assert f'opinions: more rows; call again with search_in=["opinions"], offset={page}' in first["hint"]
        is_error, second = mcp.call_tool("query_protocols", {**first_args, **first["next"]["opinions"]})
        assert not is_error and list(second["results"]) == ["opinions"]
        first_keys = {row_key("opinions", row) for row in first["results"]["opinions"]}
        second_keys = {row_key("opinions", row) for row in second["results"]["opinions"]}
        assert second_keys and not first_keys & second_keys
        assert second["next"]["opinions"]["offset"] == page + len(second["results"]["opinions"])

    def test_transcript_pages_continue_exactly(self, real_app, real_db, real_conn):
        params = {"meeting_id": real_db.meeting_id, "search_in": "speeches"}
        first = ok(real_app.get("/v1/protocols", params=params))
        patch = first["next"]["speeches"]
        second = ok(real_app.get("/v1/protocols", params={**params, "search_in": patch["search_in"],
                                                         "offset": patch["offset"]}))
        last_row, next_row = first["results"]["speeches"][-1], second["results"]["speeches"][0]
        assert next_row["speech_idx"] == real_conn.execute(
            "SELECT MIN(idx) FROM speeches WHERE meeting_id = ? AND idx > ?",
            (real_db.meeting_id, last_row["speech_idx"])).fetchone()[0]

    def test_no_next_when_every_scope_is_short(self, real_app):
        body = ok(real_app.get("/v1/protocols", params={"q": NO_SUCH_WORD}))
        assert body["next"] is None
        assert body["diagnostics"] or "no rows" in body["hint"]

    def test_bills_next_comes_from_paging_metadata(self, real_app, monkeypatch):
        import api.routes as routes

        def dispatch_with_paging(registry, name, args, **kwargs):
            return ToolEnvelope(summary="", full=json.dumps([{"bill_id": str(i), "name": "x"} for i in range(3)]),
                                metadata={"paging": {"offset": 0, "returned": 3, "has_more": True, "total": 99}},
                                provenance={"query": args.get("query")})

        monkeypatch.setattr(routes, "dispatch", dispatch_with_paging)
        body = ok(real_app.get("/v1/bills", params={"q": "חינוך"}))
        assert body["next"] == {"offset": 3}
        assert "offset=3" in body["hint"]


# ── float integers over MCP ──────────────────────────────────────────────────

class TestIntegralFloats:
    def test_float_offset_and_top_k_are_accepted(self, mcp):
        is_error, body = mcp.call_tool("query_protocols", {"query": COMMON_WORD, "search_in": ["topics"],
                                                           "offset": 20.0, "top_k": 5.0})
        assert not is_error, body
        assert body["args"]["offset"] == 20


# ── hints and diagnostics ────────────────────────────────────────────────────

class TestHints:
    @pytest.mark.network
    def test_find_mk_suggests_the_next_call(self, mcp, real_db):
        is_error, body = mcp.call_tool("find_mk", {"query": real_db.mk_name})
        assert not is_error
        assert f"found MK {real_db.mk_name} (mk_id={real_db.mk_id}" in body["hint"]
        assert f'use mk_id="{real_db.mk_id}" in query_protocols / query_votes' in body["hint"]
        assert "raise top_k" not in body["hint"]

    def test_diagnostics_are_passed_through_first_in_the_hint(self, real_app, monkeypatch):
        import api.routes as routes
        real_dispatch = routes.dispatch
        diagnostic = {"filter": "committees", "value": "ועדת הכספים", "problem": "no_match",
                      "message": "committee 'ועדת הכספים' matched no rows", "suggestions": ["ועדת הכספים"]}

        def dispatch_with_diagnostics(registry, name, args, **kwargs):
            envelope = real_dispatch(registry, name, args, **kwargs)
            envelope.metadata = {**(envelope.metadata or {}), "diagnostics": [diagnostic]}
            return envelope

        monkeypatch.setattr(routes, "dispatch", dispatch_with_diagnostics)
        body = ok(real_app.get("/v1/protocols", params={"q": NO_SUCH_WORD}))
        assert body["diagnostics"] == [diagnostic]
        assert body["hint"].startswith(diagnostic["message"])
        markdown = real_app.get("/v1/protocols", params={"q": NO_SUCH_WORD, "format": "md"}).text
        assert diagnostic["message"] in markdown

    def test_markdown_renders_next_and_warnings(self, real_app):
        markdown = real_app.get("/v1/protocols", params={"q": COMMON_WORD, "party": "ליכוד", "format": "md"}).text
        assert 'party "ליכוד" → "הליכוד"' in markdown
        assert 'search_in=["opinions"]' in markdown
        assert "top_k" not in markdown

    def test_markdown_serves_the_longest_speech_whole(self, real_app, real_conn):
        meeting_id, speech_idx, text = real_conn.execute(
            "SELECT s.meeting_id, s.idx, s.text FROM speeches s JOIN meetings m ON m.meeting_id = s.meeting_id "
            "WHERE m.knesset_num = 25 AND (m.is_protocol IS NULL OR m.is_protocol != 0) "
            "ORDER BY length(s.text) DESC LIMIT 1").fetchone()
        speech_row_offset = real_conn.execute("SELECT COUNT(*) FROM speeches WHERE meeting_id = ? AND idx < ?",
                                              (meeting_id, speech_idx)).fetchone()[0]
        markdown = real_app.get("/v1/protocols", params={"meeting_id": meeting_id, "search_in": "speeches",
                                                         "offset": speech_row_offset, "format": "md"}).text
        assert text.strip() in markdown and "[characters" not in markdown


# ── 4xx bodies ───────────────────────────────────────────────────────────────

class TestErrorHints:
    def test_rest_400_has_a_hint(self, real_app):
        response = real_app.get("/v1/protocols", params={"date_from": "yesterday"})
        assert response.status_code == 400
        assert "YYYY-MM-DD" in response.json()["hint"]

    def test_too_many_words_names_no_rest_param_over_mcp(self, mcp):
        words = " ".join(["מילה"] * (config.API_MAX_QUERY_WORDS + 1))
        is_error, body = mcp.call_tool("query_protocols", {"query": words})
        assert is_error and body["error_code"] == "invalid_query"
        assert not body["message"].startswith("q ")
        assert f"at most {config.API_MAX_QUERY_WORDS} words" in body["hint"]

    def test_unresolvable_mk_is_400_with_a_hint(self, real_app):
        response = real_app.get("/v1/protocols", params={"mk_id": "abc"})
        assert response.status_code == 400
        assert "find_mk" in response.json()["hint"]

    def test_ambiguous_mk_name_is_400_with_candidates(self, real_app):
        body = real_app.get("/v1/protocols", params={"mk_id": "כהן"}).json()
        assert body["error_code"] == "mk_id_not_resolved"
        assert body["candidates"] and all(candidate["mk_id"] for candidate in body["candidates"])
        assert body["diagnostics"] and "candidates" in body["hint"]

    def test_not_found_404_has_a_hint(self, real_app):
        response = real_app.get("/v1/meetings/9999999/attendance")
        assert response.status_code == 404
        assert "meeting_id" in response.json()["hint"]


# ── question log ─────────────────────────────────────────────────────────────

def question_lines() -> list[dict]:
    path = Path(config.LOG_DIR) / "questions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestQuestionLog:
    def test_mcp_call_logs_row_count_error_code_and_user_agent(self, mcp):
        headers = {"User-Agent": "Claude-User/1.0", "X-Gemini-Api-Key": "AIzaSECRETSECRET"}
        _, found_body = mcp.call_tool("query_protocols", {"query": COMMON_WORD, "search_in": ["topics"]}, headers=headers)
        mcp.call_tool("query_protocols", {"date_from": "yesterday"}, headers=headers)
        mcp.call_tool("query_protocols", {"query": NO_SUCH_WORD}, headers=headers)
        found, refused, empty = question_lines()[-3:]
        assert found["route"] == "mcp:query_protocols"
        assert found["rows"] == len(found_body["results"]["topics"]) > 0 and found["error_code"] is None
        assert refused["error_code"] == "invalid_date_from" and refused["rows"] is None
        assert empty["rows"] == 0
        assert found["ua"] == "Claude-User/1.0"
        assert "SECRET" not in (Path(config.LOG_DIR) / "questions.jsonl").read_text(encoding="utf-8")

    def test_rest_call_is_logged_too(self, real_app):
        body = ok(real_app.get("/v1/protocols", params={"q": COMMON_WORD, "search_in": "topics"}))
        line = question_lines()[-1]
        assert line["route"] == "api:query_protocols" and line["rows"] == len(body["results"]["topics"]) > 0


# ── knesset_num range and dynamic coverage ───────────────────────────────────

class TestCoverage:
    def test_roster_knesset_range_ends_at_the_25th(self, real_app):
        assert config.API_KNESSET_NUM_RANGE == (1, 25)
        tools = {tool["name"]: tool for tool in ok(real_app.get("/v1/tools"))["tools"]}
        for tool_name in ("find_mk", "find_party"):
            assert tools[tool_name]["parameters"]["properties"]["knesset_num"]["maximum"] == 25
            assert "knesset_num 1-25" in tools[tool_name]["description"]
        response = real_app.get("/v1/mks", params={"q": "x", "knesset_num": 26})
        assert response.status_code == 400 and "1-25" in response.json()["hint"]

    def test_descriptions_and_instructions_follow_a_rebuilt_db(self, tmp_path, monkeypatch):
        import os
        import sqlite3

        from api.mcp_server import mcp_instructions
        from api.routes import public_tool_schema

        db_file = tmp_path / "knesset.db"
        from retrieval import knesset_db_store as store
        store.connect(db_file).close()
        with sqlite3.connect(db_file) as conn:
            conn.executemany("INSERT INTO meetings (meeting_id, knesset_num, date) VALUES (?, 25, ?)",
                             [("1", "2023-01-02"), ("2", "2024-05-06")])
        conn.close()
        monkeypatch.setattr(config, "KNESSET_DB", db_file)
        protocols_spec = next(spec for spec in RESEARCH_TOOL_REGISTRY if spec.name == "query_protocols")
        assert "meetings from 2023-01-02 to 2024-05-06" in public_tool_schema(protocols_spec)["description"]
        assert "meetings from 2023-01-02 to 2024-05-06" in mcp_instructions()

        with sqlite3.connect(db_file) as conn:
            conn.execute("INSERT INTO meetings (meeting_id, knesset_num, date) VALUES ('3', 25, '2026-07-08')")
        conn.close()
        rebuilt_mtime_ns = os.stat(db_file).st_mtime_ns + 10**9
        os.utime(db_file, ns=(rebuilt_mtime_ns, rebuilt_mtime_ns))
        assert "meetings from 2023-01-02 to 2026-07-08" in public_tool_schema(protocols_spec)["description"]
        assert "meetings from 2023-01-02 to 2026-07-08" in mcp_instructions()

    def test_mcp_initialize_reads_the_current_coverage(self, mcp, monkeypatch):
        import api.routes as routes
        monkeypatch.setattr(routes, "protocol_meeting_date_range", lambda: ("2001-01-01", "2002-02-02"))
        instructions = mcp.result("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "pytest", "version": "1"}})["instructions"]
        assert "meetings from 2001-01-01 to 2002-02-02" in instructions
        tools = {tool["name"]: tool for tool in mcp.result("tools/list", {})["tools"]}
        assert "meetings from 2001-01-01 to 2002-02-02" in tools["query_protocols"]["description"]

    def test_instructions_text_matches_the_page_settings(self, real_app):
        instructions = real_app.get("/llms.txt").text
        assert f"Pages are about {config.API_PROTOCOLS_PAGE_CHARS}" in instructions
        assert "`offset` counts the" in instructions and "Texts are never cut or split" in instructions
        assert "50 when listing one meeting" not in instructions


def test_find_party_description_has_no_top_k_wording(real_app):
    registry_description = next(spec for spec in RESEARCH_TOOL_REGISTRY if spec.name == "find_party").schema["description"]
    public_description = {tool["name"]: tool for tool in ok(real_app.get("/v1/tools"))["tools"]}["find_party"]["description"]
    for description in (registry_description, public_description):
        assert "top_k" not in description and "up to" not in description
        assert "not a faction of that Knesset returns no party" in description
