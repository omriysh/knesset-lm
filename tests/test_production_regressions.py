"""
tests/test_production_regressions.py

Real cases from the production MCP logs and the code review, replayed end to end the way a
chatbot sends them: JSON-RPC tools/call on POST /mcp (or the matching /v1 route) against the real
Data/knesset.db and, for the upstream tools (`network`), the live Knesset APIs.

Each test failed on the code that produced the logs (commit d41547f) and passes since the fixes;
its docstring names the bug it guards against.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fastapi.testclient import TestClient

import config

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}
LOGGED_MK_ORIT_FARKASH_HACOHEN = "30685"
SCIENCE_COMMITTEE_SINGLE_SPACED = "ועדת המדע והטכנולוגיה"
SCIENCE_COMMITTEE_DOUBLE_SPACED = "ועדת  המדע  והטכנולוגיה"
NONSENSE_WORD = "קשקשתאינהקיימתבשוםמקום"


class ChatbotMcpClient:
    """JSON-RPC tools/call over POST /mcp, the transport the production chatbots use."""

    def __init__(self, http_client: TestClient):
        self.http = http_client

    def call_tool(self, name: str, arguments: dict) -> tuple[bool, dict]:
        response = self.http.post("/mcp", headers=MCP_HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        return result["isError"], json.loads(result["content"][0]["text"])


@pytest.fixture()
def mcp(real_db, monkeypatch):
    from api.app import app, rate_limiter
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", (*config.MCP_ALLOWED_HOSTS, "testserver"))
    monkeypatch.setattr(config, "MCP_LOCAL_PEER_HOSTS", ("testclient",))
    rate_limiter.reset()
    with TestClient(app) as http_client:
        yield ChatbotMcpClient(http_client)
    rate_limiter.reset()


def protocol_rows(mcp, arguments: dict) -> tuple[dict, list[dict]]:
    is_error, body = mcp.call_tool("query_protocols", arguments)
    assert not is_error, body
    rows = [row for scope_rows in body["results"].values() for row in scope_rows]
    return body, rows


def guidance_text(body: dict) -> str:
    """Everything a response tells the caller besides the rows: hint, warnings, diagnostics."""
    parts = [str(body.get("hint") or "")]
    parts += [str(w) for w in body.get("warnings") or []]
    parts += [str(d) for d in body.get("diagnostics") or []]
    return " ".join(parts)


def mk_id_by_name(real_conn, name_fragment: str) -> str:
    row = real_conn.execute("SELECT mk_id FROM mks WHERE full_name LIKE ?", (f"%{name_fragment}%",)).fetchone()
    assert row, f"no MK named like {name_fragment} in knesset.db"
    return str(row[0])


# ── FTS recall on real Hebrew ─────────────────────────────────────────────────

def test_cost_of_living_topics_of_farkash_hacohen(mcp, real_conn):
    """Was: FTS recall: Hebrew prefixes (ב/ה/ל...) not matched, 'יוקר מחיה' misses 'ביוקר המחיה'."""
    assert real_conn.execute("SELECT 1 FROM topics WHERE text LIKE '%ביוקר המחיה%' LIMIT 1").fetchone()
    body, rows = protocol_rows(mcp, {"query": "יוקר מחיה", "mk_id": LOGGED_MK_ORIT_FARKASH_HACOHEN,
                                     "search_in": ["topics"], "knesset_num": 25})
    assert rows, guidance_text(body)


def test_haredi_draft_opinions_of_eisenkot(mcp, real_conn):
    """Was: FTS recall: prefixed forms ('בגיוס', 'החרדים') missed for 'גיוס חרדים' in Eisenkot's opinions."""
    eisenkot = mk_id_by_name(real_conn, "איזנקוט")
    assert real_conn.execute(
        "SELECT 1 FROM opinions WHERE mk_id = ? AND quote_verified = 1 AND opinion LIKE '%גיוס%' "
        "AND (opinion LIKE '%חרד%' OR quote LIKE '%חרד%') LIMIT 1", (eisenkot,)).fetchone()
    body, rows = protocol_rows(mcp, {"query": "גיוס חרדים", "mk_id": eisenkot, "search_in": ["opinions"]})
    assert rows, guidance_text(body)


def test_idf_with_gershayim(mcp, real_conn):
    """Was: gershayim: 'צה"ל' is split/stripped by the FTS query builder and matches nothing."""
    assert real_conn.execute("SELECT 1 FROM topics WHERE text LIKE '%צה\"ל%' LIMIT 1").fetchone()
    body, rows = protocol_rows(mcp, {"query": 'צה"ל', "search_in": ["topics"]})
    assert rows, guidance_text(body)


# ── filter name resolution ────────────────────────────────────────────────────

def test_party_without_definite_article(mcp):
    """Was: party filter is exact-match: 'ליכוד' is not resolved to the roster name 'הליכוד'."""
    body, rows = protocol_rows(mcp, {"party": "ליכוד", "search_in": ["topics", "opinions"]})
    assert rows or "הליכוד" in guidance_text(body)


def test_single_spaced_committee_covers_double_spaced_meetings(mcp, real_conn):
    """Was: committee filter is exact-match: single-spaced name misses the double-spaced 'ועדת  המדע  והטכנולוגיה' meetings."""
    meetings = [r[0] for r in real_conn.execute(
        "SELECT m.meeting_id FROM meetings m WHERE m.committee = ? AND (m.is_protocol IS NULL OR m.is_protocol != 0) "
        "AND (EXISTS (SELECT 1 FROM topics t WHERE t.meeting_id = m.meeting_id) "
        "     OR EXISTS (SELECT 1 FROM speeches s WHERE s.meeting_id = m.meeting_id)) ORDER BY m.meeting_id",
        (SCIENCE_COMMITTEE_DOUBLE_SPACED,))]
    assert len(meetings) >= 70
    missed = []
    for meeting_id in meetings:
        _, rows = protocol_rows(mcp, {"committees": [SCIENCE_COMMITTEE_SINGLE_SPACED], "meeting_ids": [meeting_id],
                                      "search_in": ["topics", "speeches"]})
        if not rows:
            missed.append(meeting_id)
    assert not missed, f"{len(missed)}/{len(meetings)} meetings missed, e.g. {missed[:5]}"


def test_long_joint_committee_name_is_accepted(mcp, client, real_conn):
    """Was: committee names are capped at 100 chars, but real joint-committee names reach 183."""
    name, = real_conn.execute("SELECT committee FROM meetings WHERE length(committee) > 100 "
                              "GROUP BY committee ORDER BY COUNT(*) DESC").fetchone()
    body, rows = protocol_rows(mcp, {"committees": [name], "search_in": ["topics"]})
    assert rows, guidance_text(body)
    assert client.get("/v1/protocols", params={"committee": name}).status_code == 200


@pytest.mark.network
def test_find_mk_does_not_confuse_golan_with_lapid(mcp):
    """Was: find_mk: 'יאיר גולן' (not a K25 MK) fuzzy-matched to יאיר לפיד with a confident score."""
    is_error, body = mcp.call_tool("find_mk", {"query": "יאיר גולן"})
    assert not is_error, body
    top = (body["results"] or [{}])[0]
    assert not (top.get("full_name") == "יאיר לפיד" and top.get("score", 0) >= 0.85), top


@pytest.mark.network
def test_find_party_shas_acronym(mcp):
    """Was: find_party: the common acronym 'ש"ס' does not resolve to the Shas roster name."""
    is_error, body = mcp.call_tool("find_party", {"query": 'ש"ס'})
    assert not is_error, body
    assert body["results"] and body["results"][0]["party"].startswith("התאחדות הספרדים"), body["results"][:1]


# ── empty results must explain themselves ─────────────────────────────────────

def test_unknown_party_listing_names_the_filter(mcp):
    """Was: empty query_protocols result gives no diagnostic naming the filter that matched nothing."""
    body, rows = protocol_rows(mcp, {"party": "מפלגתשאינהקיימתבכנסת"})
    assert rows == []
    guidance = guidance_text(body)
    assert "party" in guidance or "מפלגה" in guidance, guidance


@pytest.mark.network
def test_vote_search_without_hits_is_an_empty_200(client):
    """Was: a vote search without hits is 404 instead of 200 with an empty list."""
    response = client.get("/v1/votes", params={"q": NONSENSE_WORD})
    assert response.status_code == 200, response.text
    assert response.json()["results"] == []


@pytest.mark.network
def test_votes_without_knesset_num_search_all_knessets(mcp, real_conn):
    """Was: query_votes defaults knesset_num to 25 instead of searching all Knessets."""
    deri = mk_id_by_name(real_conn, "דרעי")
    is_error, body = mcp.call_tool("query_votes", {"query": "ההתנתקות", "mk_id": deri})
    assert not is_error, body
    assert body["args"].get("knesset_num") in (None, ""), body["args"]
    knessets = {str(vote.get("knesset_num")) for vote in body["results"]}
    assert knessets - {"25", "None"}, knessets


# ── hints and argument types ──────────────────────────────────────────────────

def test_find_committee_hint_at_max_top_k(client):
    """Was: find_committee hint says 'raise top_k' even when top_k is already at its max."""
    max_top_k = getattr(config, "API_FIND_MAX_TOP_K", 5)
    response = client.get("/v1/committees", params={"q": "ועדה", "top_k": max_top_k})
    assert response.status_code == 200, response.text
    assert "raise top_k" not in (response.json().get("hint") or "")


def test_float_top_k_over_mcp(mcp, real_db):
    """Was: MCP rejects an integral float top_k (5.0), the way JSON chatbots send numbers."""
    is_error, body = mcp.call_tool("query_protocols", {"query": real_db.topic_word, "top_k": 5.0})
    assert not is_error, body
    is_error, body = mcp.call_tool("find_committee", {"query": real_db.other_committee, "top_k": 5.0})
    assert not is_error, body
