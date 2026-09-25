"""
tests/test_api.py

Public read-only API (src/api) over the 8 research tools, on real data:
  * knesset.db routes run against the temp db of real sampled rows (conftest.sample_db);
  * roster routes (find_mk / find_party) replay real oknesset + OData responses recorded by
    tests/fixtures/record_roster_recordings.py;
  * bill / vote routes replay tests/fixtures/odata_recordings.json (test_odata_tools.ODataReplayer).

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
import requests
from fastapi.testclient import TestClient

import config
import utils.knesset_db as kdb
import utils.tools as tools
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from tests.conftest import ROLES, SAMPLE, SYN_MEETING, X_MK
from tests.test_odata_tools import (
    _REC as ODATA_REC, BILL_ID, BILL_POOL_IDS, LONG_BILL_TEXT, META, RESULTS, ODataReplayer, _FakeResponse,
)

ROSTER = json.loads((Path(__file__).parent / "fixtures" / "roster_recordings.json").read_text(encoding="utf-8"))
M1, M2 = ROLES["M1"], ROLES["M2"]
C1, C2 = SAMPLE["committees"]["C1"], SAMPLE["committees"]["C2"]
C1_ID = next(c["committee_id"] for c in SAMPLE["committee_rows"] if c["name"] == C1)


def _odata_recorded_exchanges(url: str, params: dict) -> list[dict]:
    return [ex for ex in ODATA_REC["exchanges"] if ex["url"] == url and ex["params"] == params]


class NetworkReplayer(ODataReplayer):
    """Roster exchanges first (KNS_PersonToPosition pages collapsed into $skip=0; oknesset
    /members = union of both recordings), then the bill/vote recordings."""

    def __call__(self, url, params=None, **kwargs):
        params = {k: str(v) for k, v in (params or {}).items()}
        if url.endswith("/KNS_PersonToPosition") and params.get("$skip", "0") != "0":
            self.calls.append((url, params))
            return _FakeResponse(200, {"value": []})
        roster_hits = [ex for ex in ROSTER["exchanges"] if ex["url"] == url and ex["params"] == params]
        if not roster_hits:
            return super().__call__(url, params, **kwargs)
        self.calls.append((url, params))
        payload = roster_hits[0]["json"]
        if url.endswith("/members"):
            members = {str(m.get("mk_individual_id")): m for m in payload}
            for ex in _odata_recorded_exchanges(url, params):
                for m in ex["json"]:
                    members.setdefault(str(m.get("mk_individual_id")), m)
            payload = list(members.values())
        return _FakeResponse(200, payload)


def _clear_network_caches():
    kdb._fetch_members.cache_clear()
    kdb._fetch_person_positions.cache_clear()
    kdb._position_names.cache_clear()


@pytest.fixture()
def network(monkeypatch):
    replayer = NetworkReplayer()
    monkeypatch.setattr(requests, "get", replayer)
    monkeypatch.setattr(config, "API_RETRY_SLEEP", 0, raising=False)
    monkeypatch.setattr(kdb, "_extract_pdf_text", lambda content: LONG_BILL_TEXT)
    _clear_network_caches()
    yield replayer
    _clear_network_caches()


@pytest.fixture()
def client(sample_db, network):
    from api.app import app
    return TestClient(app)


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def handler_payload(handler, args: dict):
    env = handler(args)
    assert env.error is None, env.error
    return json.loads(env.full)


# ── tool listing / docs ──────────────────────────────────────────────────────

class TestToolListing:
    def test_tools_mirror_the_research_registry(self, client):
        tools_out = ok(client.get("/v1/tools"))["tools"]
        assert [t["name"] for t in tools_out] == [s.name for s in RESEARCH_TOOL_REGISTRY]
        for t, spec in zip(tools_out, RESEARCH_TOOL_REGISTRY):
            assert t["description"] == spec.schema["description"]
            assert t["parameters"]["properties"] == spec.schema["properties"]
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
            assert spec.schema["description"].splitlines()[0] in text

    def test_health_and_meta_counts_match_sample(self, client):
        assert ok(client.get("/health"))["db"] is True
        counts = ok(client.get("/v1/meta"))["counts"]
        assert counts["meetings"] == len(SAMPLE["meetings"]) + 1
        assert counts["speeches"] == len(SAMPLE["speeches"]) + 3
        assert counts["mks"] == len(SAMPLE["mks"])


# ── query_protocols ──────────────────────────────────────────────────────────

class TestProtocols:
    def test_defaults_are_topics_and_opinions_top_10(self, client):
        body = ok(client.get("/v1/protocols", params={"meeting_id": M2}))
        assert body["tool"] == "query_protocols"
        assert set(body["results"]) == {"topics", "opinions"}
        assert body["args"]["top_k"] == config.API_PROTOCOLS_DEFAULT_TOP_K == 10
        assert body["args"]["meeting_ids"] == [M2]

    def test_meeting_listing_equals_handler(self, client):
        body = ok(client.get("/v1/protocols", params={"meeting_id": M2, "search_in": "topics,opinions,speeches",
                                                      "top_k": 50}))
        expected = handler_payload(tools.handle_query_protocols, {
            "meeting_ids": [M2], "search_in": ["topics", "opinions", "speeches"], "top_k": 50})
        assert body["results"] == expected
        assert body["results"]["speeches"], "real meeting M2 has speeches"

    def test_keyword_search_equals_handler(self, client):
        word = max((w for w in SAMPLE["topics"][0]["text"].split() if w.isalpha()), key=len)
        body = ok(client.get("/v1/protocols", params={"q": word}))
        expected = handler_payload(tools.handle_query_protocols, {
            "query": word, "search_in": ["topics", "opinions"], "top_k": 10})
        assert body["results"] == expected
        assert body["results"]["topics"]

    def test_repeated_params(self, client):
        params = [("search_in", "speeches"), ("search_in", "topics"),
                  ("committee", C1), ("committee", C2), ("top_k", "50")]
        body = ok(client.get("/v1/protocols", params=params))
        assert set(body["results"]) == {"speeches", "topics"}
        assert body["args"]["committees"] == [C1, C2]
        rows = body["results"]["speeches"] + body["results"]["topics"]
        assert rows and {r["committee"] for r in rows} <= {C1, C2}

    def test_mk_filter(self, client):
        rows = ok(client.get("/v1/protocols", params={"mk_id": X_MK["mk_id"], "search_in": "opinions"}))["results"]["opinions"]
        assert rows and all(r["mk_id"] == X_MK["mk_id"] for r in rows)

    def test_top_k_clamped(self, client):
        body = ok(client.get("/v1/protocols", params={"meeting_id": M2, "top_k": 500}))
        assert body["args"]["top_k"] == config.API_PROTOCOLS_MAX_TOP_K == 50

    def test_full_page_hints_next_offset(self, client):
        body = ok(client.get("/v1/protocols", params={"meeting_id": M2, "search_in": "speeches", "top_k": 2}))
        assert len(body["results"]["speeches"]) == 2
        assert "offset=2" in body["hint"]
        page2 = ok(client.get("/v1/protocols", params={"meeting_id": M2, "search_in": "speeches",
                                                       "top_k": 2, "offset": 2}))["results"]["speeches"]
        assert page2[0]["speech_idx"] > body["results"]["speeches"][-1]["speech_idx"]

    def test_no_rows_hint(self, client):
        body = ok(client.get("/v1/protocols", params={"q": "מילהשאינהקיימתבכלל"}))
        assert all(rows == [] for rows in body["results"].values())
        assert body["hint"]

    def test_invalid_scope_and_sort_are_400(self, client):
        r = client.get("/v1/protocols", params={"search_in": "bullets"})
        assert r.status_code == 400 and r.json()["error_code"] == "invalid_search_in"
        r = client.get("/v1/protocols", params={"sort": "random"})
        assert r.status_code == 400 and r.json()["error_code"] == "invalid_sort"

    def test_size_cap_truncates_rows_keeping_prefix(self, client, monkeypatch):
        params = {"meeting_id": [SYN_MEETING, M2], "search_in": "speeches", "top_k": 50}
        full = ok(client.get("/v1/protocols", params=params))
        assert full["truncated"] is False
        monkeypatch.setattr(config, "API_MAX_RESPONSE_CHARS", 20_000)
        capped = ok(client.get("/v1/protocols", params=params))
        assert capped["truncated"] is True
        rows = capped["results"]["speeches"]
        assert rows and rows == full["results"]["speeches"][: len(rows)]
        assert len(json.dumps(capped["results"], ensure_ascii=False)) <= 20_000
        assert "offset" in capped["hint"]

    def test_markdown_format(self, client):
        r = client.get("/v1/protocols", params={"meeting_id": M2, "format": "md"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
        topic = next(t["text"] for t in SAMPLE["topics"] if t["meeting_id"] == M2)
        assert M2 in r.text and topic in r.text

    def test_missing_db_is_503(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "nope.db")
        r = client.get("/v1/protocols", params={"q": "תקציב"})
        assert r.status_code == 503 and r.json()["error_code"] == "knesset_db_missing"


# ── attendance ───────────────────────────────────────────────────────────────

class TestAttendance:
    def test_equals_handler(self, client):
        body = ok(client.get(f"/v1/meetings/{M1}/attendance"))
        assert body["results"] == handler_payload(tools.handle_get_meeting_attendance, {"meeting_id": M1})
        attendance = body["results"]["attendance"]
        assert attendance
        mk_flags = [a["mk_id"] is not None for a in attendance]
        assert mk_flags == sorted(mk_flags, reverse=True), "MKs first"

    def test_unknown_meeting_is_404(self, client):
        r = client.get("/v1/meetings/9999999/attendance")
        assert r.status_code == 404 and r.json()["error_code"] == "meeting_not_found"


# ── find_mk / find_committee / find_party ────────────────────────────────────

class TestFind:
    def test_mk_with_real_profile(self, client):
        body = ok(client.get("/v1/mks", params={"q": X_MK["full_name"]}))
        top = body["results"][0]
        assert top["mk_id"] == X_MK["mk_id"]
        recorded = ROSTER["results"]["find_mk"][0]["profile"]
        assert top["profile"]["factions"] == recorded["factions"]
        assert top["profile"]["factions"][-1]["faction_name"] == X_MK["party"]
        assert top["profile"]["committee_positions"]

    def test_mk_top_k_clamped_and_missing_query(self, client):
        assert ok(client.get("/v1/mks", params={"q": "עודד", "top_k": 50}))["args"]["top_k"] == config.API_FIND_MAX_TOP_K
        r = client.get("/v1/mks")
        assert r.status_code == 400 and r.json()["error_code"] == "missing_query"

    def test_committee(self, client):
        body = ok(client.get("/v1/committees", params={"q": C1}))
        assert body["results"][0]["committee_id"] == C1_ID
        assert body["results"][0]["name"] == C1

    def test_party_with_real_members(self, client):
        body = ok(client.get("/v1/parties", params={"q": X_MK["party"], "top_k": 1}))
        recorded = ROSTER["results"]["find_party"][0]
        sample_mk_ids = {str(m["mk_id"]) for m in SAMPLE["mks"]}
        assert body["results"][0]["party"] == recorded["party"]
        assert body["results"][0]["members"] == [m for m in recorded["members"] if m["mk_id"] in sample_mk_ids]
        assert X_MK["mk_id"] in {m["mk_id"] for m in body["results"][0]["members"]}


# ── bills / votes (OData) ────────────────────────────────────────────────────

class TestBillsAndVotes:
    def test_bill_search(self, client):
        body = ok(client.get("/v1/bills", params={"q": META["bill_query"]}))
        assert body["results"] and all(int(b["bill_id"]) in BILL_POOL_IDS for b in body["results"])

    def test_get_bill_details_and_text(self, client):
        body = ok(client.get(f"/v1/bills/{BILL_ID}"))
        assert body["results"]["bill_name"] == RESULTS["bill_details"]["bill_name"]
        assert "text" not in body["results"]
        body = ok(client.get(f"/v1/bills/{BILL_ID}", params={"include_text": "true", "max_chars": 3000}))
        assert body["results"]["text"] == LONG_BILL_TEXT[:3000]
        assert body["truncated"] is True

    def test_bill_errors(self, client):
        r = client.get("/v1/bills/abc")
        assert r.status_code == 400 and r.json()["error_code"] == "invalid_bill_id"
        r = client.get(f"/v1/bills/{META['missing_bill_id']}")
        assert r.status_code == 404 and r.json()["error_code"] == "bill_not_found"

    def test_odata_down_is_502(self, client, monkeypatch):
        def offline(*a, **k):
            raise requests.exceptions.ConnectionError("offline")
        monkeypatch.setattr(requests, "get", offline)
        r = client.get("/v1/bills", params={"q": "חינוך"})
        assert r.status_code == 502 and r.json()["error_code"] == "odata_request_failed"

    def test_votes_on_topic(self, client):
        body = ok(client.get("/v1/votes", params={"q": META["vote_topic"], "top_k": 5}))
        assert [v["vote_id"] for v in body["results"]] == [v["vote_id"] for v in RESULTS["votes_on_topic"]]

    def test_votes_of_mk_on_topic(self, client):
        body = ok(client.get("/v1/votes", params={"q": META["vote_topic"], "mk_id": META["vote_mk"]["mk_id"], "top_k": 5}))
        assert [v["vote_id"] for v in body["results"]] == [v["vote_id"] for v in RESULTS["votes_on_topic_by_mk"]]

    def test_votes_full_page_hints_top_k(self, client):
        body = ok(client.get("/v1/votes", params={"q": META["vote_topic"], "top_k": 2}))
        assert len(body["results"]) == 2
        assert body["args"]["top_k"] == 2
        assert "top_k" in body["hint"]

    def test_unknown_mk_is_404(self, client):
        r = client.get("/v1/votes", params={"mk_id": "99999999"})
        assert r.status_code == 404 and r.json()["error_code"] == "mk_not_found"


# ── mounting in the local web UI server ──────────────────────────────────────

def test_web_app_serves_the_api(sample_db, network, tmp_path):
    import web.app as webapp
    import web.settings as settings
    webapp.app.state.settings = settings
    webapp.app.state.sessions_dir = tmp_path
    webapp.app.state.machine = SimpleNamespace(name="test_machine", version=2)
    web_client = TestClient(webapp.app)
    assert [t["name"] for t in ok(web_client.get("/v1/tools"))["tools"]] == [s.name for s in RESEARCH_TOOL_REGISTRY]
    assert ok(web_client.get(f"/v1/meetings/{M1}/attendance"))["results"]["meeting_id"] == M1
