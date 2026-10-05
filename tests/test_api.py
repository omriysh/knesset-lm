"""
tests/test_api.py

Public read-only API (src/api) over the research tools, end to end:
  * knesset.db routes run against the real Data/knesset.db (conftest.real_db);
  * roster, bill and vote routes (tests marked `network`) query the live oknesset / Knesset
    OData APIs through the production disk cache.

The API is a thin wrapper: for the same arguments its `results` must equal the handler's
payload, so most assertions compare against a direct handler call.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fastapi.testclient import TestClient

import config
import utils.tools as tools
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from api.routes import public_tool_schema
from tests.conftest import assert_within_character_budget, handler_payload, ok

# Knesset-25 bill with a published PDF (the Knesset basic-law override bill).
REAL_BILL_ID = 2209870
MISSING_BILL_ID = 999999999
NONSENSE_WORD = "קשקשתאינהקיימתבשוםפרוטוקול"


# ── tool listing / docs ──────────────────────────────────────────────────────

class TestToolListing:
    def test_tools_mirror_the_research_registry(self, client):
        tools_out = ok(client.get("/v1/tools"))["tools"]
        assert [t["name"] for t in tools_out] == [s.name for s in RESEARCH_TOOL_REGISTRY]
        for t, spec in zip(tools_out, RESEARCH_TOOL_REGISTRY):
            assert t["description"] == public_tool_schema(spec)["description"]
            assert t["parameters"]["properties"] == public_tool_schema(spec)["properties"]
            assert "top_k" not in t["parameters"]["properties"]
            assert "description" not in t["parameters"]
            assert t["endpoint"].startswith("/v1/")

    @pytest.mark.parametrize("path", ["/llms.txt", "/agent-instructions"])
    def test_instructions_list_every_endpoint(self, client, path):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/")
        for endpoint in ("/v1/mks", "/v1/committees", "/v1/parties", "/v1/protocols",
                         "/v1/meetings/", "/v1/bills", "/v1/votes", "/v1/tools"):
            assert endpoint in r.text

    def test_llms_full_has_every_tool_description(self, client):
        text = client.get("/llms-full.txt").text
        for spec in RESEARCH_TOOL_REGISTRY:
            assert spec.name in text
            assert public_tool_schema(spec)["description"].splitlines()[0] in text

    def test_health_and_meta_counts_match_the_db(self, client, real_db):
        assert ok(client.get("/health"))["db"] is True
        counts = ok(client.get("/v1/meta"))["counts"]
        for table in ("meetings", "speeches", "mks"):
            assert counts[table] == real_db.table_counts[table], table
        assert counts["meetings"] >= 9000 and counts["speeches"] >= 1_500_000 and counts["mks"] >= 120


# ── query_protocols ──────────────────────────────────────────────────────────

class TestProtocols:
    def test_defaults_are_topics_and_opinions_without_top_k(self, client, real_db):
        body = ok(client.get("/v1/protocols", params={"meeting_id": real_db.meeting_id}))
        assert body["tool"] == "query_protocols"
        assert set(body["results"]) == {"topics", "opinions"}
        assert "top_k" not in body["args"]
        assert body["args"]["meeting_ids"] == [real_db.meeting_id]

    def test_meeting_listing_equals_handler(self, client, real_db):
        body = ok(client.get("/v1/protocols", params={"meeting_id": real_db.meeting_id,
                                                      "search_in": "topics,opinions,speeches"}))
        expected = handler_payload(tools.handle_query_protocols, {
            "meeting_ids": [real_db.meeting_id], "search_in": ["topics", "opinions", "speeches"],
            "page_chars": config.API_PROTOCOLS_PAGE_CHARS})
        assert body["results"] == expected
        assert all(body["results"][scope] for scope in ("topics", "opinions", "speeches"))

    def test_keyword_search_equals_handler(self, client, real_db):
        body = ok(client.get("/v1/protocols", params={"q": real_db.topic_word}))
        expected = handler_payload(tools.handle_query_protocols, {
            "query": real_db.topic_word, "search_in": ["topics", "opinions"],
            "page_chars": config.API_PROTOCOLS_PAGE_CHARS})
        assert body["results"] == expected
        assert body["results"]["topics"]

    def test_repeated_params(self, client, real_db):
        committees = [real_db.meeting_committee, real_db.other_committee]
        params = [("search_in", "speeches"), ("search_in", "topics"),
                  ("committee", committees[0]), ("committee", committees[1]), ("top_k", "50")]
        body = ok(client.get("/v1/protocols", params=params))
        assert set(body["results"]) == {"speeches", "topics"}
        assert body["args"]["committees"] == committees
        rows = body["results"]["speeches"] + body["results"]["topics"]
        assert rows and {r["committee"] for r in rows} <= set(committees)

    def test_mk_filter(self, client, real_db):
        rows = ok(client.get("/v1/protocols", params={"mk_id": real_db.mk_id,
                                                      "search_in": "opinions"}))["results"]["opinions"]
        assert rows and all(r["mk_id"] == real_db.mk_id for r in rows)

    def test_a_sent_top_k_is_an_unknown_parameter(self, client, real_db):
        params = {"meeting_id": real_db.meeting_id, "search_in": "speeches"}
        body = ok(client.get("/v1/protocols", params={**params, "top_k": 500}))
        assert "top_k" not in body["args"]
        assert body["results"] == ok(client.get("/v1/protocols", params=params))["results"]
        assert not any("top_k" in warning for warning in body["warnings"])

    def test_full_page_hints_next_row_offset(self, client, real_db, real_conn):
        params = {"meeting_id": real_db.meeting_id, "search_in": "speeches"}
        body = ok(client.get("/v1/protocols", params=params))
        next_offset = len(body["results"]["speeches"])
        assert body["next"]["speeches"] == {"search_in": ["speeches"], "offset": next_offset}
        assert f'more rows; call again with search_in=["speeches"], offset={next_offset}' in body["hint"]
        page2 = ok(client.get("/v1/protocols", params={**params, "offset": next_offset}))["results"]["speeches"]
        last_row = body["results"]["speeches"][-1]
        assert page2[0]["speech_idx"] == real_conn.execute(
            "SELECT MIN(idx) FROM speeches WHERE meeting_id = ? AND idx > ?",
            (real_db.meeting_id, last_row["speech_idx"])).fetchone()[0]

    def test_no_rows_hint(self, client, real_db):
        body = ok(client.get("/v1/protocols", params={"q": NONSENSE_WORD}))
        assert all(rows == [] for rows in body["results"].values())
        assert body["hint"]

    def test_invalid_scope_and_sort_are_400(self, client):
        r = client.get("/v1/protocols", params={"search_in": "bullets"})
        assert r.status_code == 400 and r.json()["error_code"] == "invalid_search_in"
        r = client.get("/v1/protocols", params={"sort": "random"})
        assert r.status_code == 400 and r.json()["error_code"] == "invalid_sort"

    def test_no_text_is_cut(self, client, real_conn):
        meeting_id = real_conn.execute(
            "SELECT s.meeting_id FROM speeches s JOIN meetings m ON m.meeting_id = s.meeting_id "
            "WHERE m.knesset_num = 25 AND (m.is_protocol IS NULL OR m.is_protocol != 0) AND length(s.text) > 5000 "
            "GROUP BY s.meeting_id HAVING COUNT(*) >= 3 LIMIT 1").fetchone()[0]
        texts_by_idx = dict(real_conn.execute("SELECT idx, text FROM speeches WHERE meeting_id = ?", (meeting_id,)))
        body = ok(client.get("/v1/protocols", params={"meeting_id": meeting_id, "search_in": "speeches"}))
        assert body["truncated"] is False
        for row in body["results"]["speeches"]:
            assert not any(key.endswith(("_truncated", "_chars")) for key in row)
            assert row["text"] == texts_by_idx[row["speech_idx"]]

    def test_three_scopes_stay_under_the_response_budget(self, client, real_db):
        body = ok(client.get("/v1/protocols", params={"q": "תקציב", "search_in": "topics,opinions,speeches"}))
        assert all(body["results"].values()) and set(body["next"]) == {"topics", "opinions", "speeches"}
        for rows in body["results"].values():
            assert_within_character_budget(rows, config.API_PROTOCOLS_PAGE_CHARS)

    def test_markdown_format(self, client, real_db):
        r = client.get("/v1/protocols", params={"meeting_id": real_db.meeting_id, "format": "md"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
        assert real_db.meeting_id in r.text and real_db.meeting_first_topic in r.text

    def test_missing_db_is_503(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "nope.db")
        r = client.get("/v1/protocols", params={"q": "תקציב"})
        assert r.status_code == 503 and r.json()["error_code"] == "knesset_db_missing"


# ── attendance ───────────────────────────────────────────────────────────────

class TestAttendance:
    def test_equals_handler(self, client, real_db):
        body = ok(client.get(f"/v1/meetings/{real_db.other_meeting_id}/attendance"))
        assert body["results"] == handler_payload(tools.handle_get_meeting_attendance,
                                                  {"meeting_id": real_db.other_meeting_id})
        attendance = body["results"]["attendance"]
        mk_flags = [a["mk_id"] is not None for a in attendance]
        assert attendance and mk_flags == sorted(mk_flags, reverse=True), "MKs first"

    def test_unknown_meeting_is_404(self, client):
        r = client.get("/v1/meetings/9999999/attendance")
        assert r.status_code == 404 and r.json()["error_code"] == "meeting_not_found"


# ── find_mk / find_committee / find_party ────────────────────────────────────

class TestFind:
    @pytest.mark.network
    def test_mk_with_live_profile(self, client, real_db):
        body = ok(client.get("/v1/mks", params={"q": real_db.mk_name}))
        top = body["results"][0]
        assert top["mk_id"] == real_db.mk_id and top["full_name"] == real_db.mk_name
        assert top["profile"]["mk_id"] == real_db.mk_id
        assert real_db.mk_party in [f["faction_name"] for f in top["profile"]["factions"]]
        assert top["profile"]["committee_positions"]

    @pytest.mark.network
    def test_mk_sent_top_k_does_not_change_the_page(self, client):
        body = ok(client.get("/v1/mks", params={"q": "עודד", "top_k": 50}))
        assert len(body["results"]) <= config.API_FIND_PAGE_SIZE and "top_k" not in body["args"]
        assert not any("top_k" in warning for warning in body["warnings"])

    def test_mk_missing_query(self, client):
        r = client.get("/v1/mks")
        assert r.status_code == 400 and r.json()["error_code"] == "missing_query"

    @pytest.mark.network
    def test_committee(self, client, real_db):
        body = ok(client.get("/v1/committees", params={"q": real_db.other_committee}))
        assert body["results"][0]["committee_id"] == real_db.other_committee_id
        assert body["results"][0]["name"] == real_db.other_committee

    @pytest.mark.network
    def test_party_with_live_members(self, client, real_db):
        body = ok(client.get("/v1/parties", params={"q": real_db.mk_party, "top_k": 1}))
        party = body["results"][0]
        assert party["party"] == real_db.mk_party
        assert real_db.mk_id in {m["mk_id"] for m in party["members"]}
        assert party["mk_count"] == len(party["members"]) >= 2


# ── bills / votes (live OData) ───────────────────────────────────────────────

@pytest.mark.network
class TestBillsAndVotes:
    def test_bill_search(self, client):
        body = ok(client.get("/v1/bills", params={"q": "חינוך"}))
        assert body["results"] and all("חינוך" in b["name"] for b in body["results"])

    def test_get_bill_details_and_text(self, client):
        body = ok(client.get(f"/v1/bills/{REAL_BILL_ID}"))
        assert body["results"]["bill_name"] and "text" not in body["results"]
        body = ok(client.get(f"/v1/bills/{REAL_BILL_ID}", params={"include_text": "true", "max_chars": 3000}))
        assert 0 < len(body["results"]["text"]) <= 3000

    def test_missing_bill_is_404(self, client):
        r = client.get(f"/v1/bills/{MISSING_BILL_ID}")
        assert r.status_code == 404 and r.json()["error_code"] == "bill_not_found"

    def test_votes_on_topic(self, client):
        votes = ok(client.get("/v1/votes", params={"q": "תקציב"}))["results"]
        assert 1 <= len(votes) <= config.API_LIST_PAGE_SIZE and all(v["vote_id"] for v in votes)

    def test_votes_page_has_a_fixed_size(self, client):
        body = ok(client.get("/v1/votes", params={"q": "תקציב", "top_k": 2}))
        assert len(body["results"]) == config.API_LIST_PAGE_SIZE and "top_k" not in body["args"]
        assert "top_k" not in body["hint"]
        if body["next"]:
            assert f"offset={body['next']['offset']}" in body["hint"]

    def test_unknown_mk_is_404(self, client):
        r = client.get("/v1/votes", params={"mk_id": "99999999"})
        assert r.status_code == 404 and r.json()["error_code"] == "mk_not_found"


def test_bill_id_format_is_400_offline(client, upstream_down):
    r = client.get("/v1/bills/abc")
    assert r.status_code == 400 and r.json()["error_code"] == "invalid_bill_id"
    assert not upstream_down.calls


def test_odata_down_is_502(client, upstream_down):
    r = client.get("/v1/bills", params={"q": "חינוך"})
    assert r.status_code == 502 and r.json()["error_code"] == "odata_request_failed"
    assert upstream_down.calls


# ── mounting in the local web UI server ──────────────────────────────────────

def test_web_app_serves_the_api(real_db, tmp_path):
    import web.app as webapp
    import web.settings as settings
    webapp.app.state.settings = settings
    webapp.app.state.sessions_dir = tmp_path
    webapp.app.state.machine = SimpleNamespace(name="test_machine", version=2)
    web_client = TestClient(webapp.app)
    assert [t["name"] for t in ok(web_client.get("/v1/tools"))["tools"]] == [s.name for s in RESEARCH_TOOL_REGISTRY]
    attendance = ok(web_client.get(f"/v1/meetings/{real_db.meeting_id}/attendance"))["results"]
    assert attendance["meeting_id"] == real_db.meeting_id and attendance["attendance"]
    protocols = ok(web_client.get("/v1/protocols", params={"q": real_db.topic_word}))
    assert protocols["results"]["topics"]
