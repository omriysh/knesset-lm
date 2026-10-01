"""
Empty-result diagnostics of query_protocols against the real Data/knesset.db: each check of
utils.tool_helpers.filter_diagnostics on a real failing call, and the handler wiring
(metadata["diagnostics"] + summary, only when every requested scope is empty).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from retrieval import knesset_db_store as store
from utils import tools
from utils.tool_helpers.filter_diagnostics import diagnose_empty_protocol_query

pytestmark = pytest.mark.skipif(not store.exists(), reason="Data/knesset.db not built")

NONEXISTENT_WORD = "מילהשאינהקיימתבכלל"
YAIR_LAPID_MK_ID = "878"
DIAGNOSTIC_KEYS = {"filter", "value", "problem", "message", "suggestions"}


@pytest.fixture(scope="module")
def conn():
    connection = store.connect()
    yield connection
    connection.close()


def _problems(conn, **args) -> dict[str, dict]:
    diagnostics = diagnose_empty_protocol_query(conn, {"search_in": list(store.PROTOCOL_SCOPES), **args})
    assert all(set(d) == DIAGNOSTIC_KEYS for d in diagnostics)
    assert all(d["message"] and d["message"].endswith(".") for d in diagnostics)
    return {d["problem"]: d for d in diagnostics}


def test_knesset_without_protocols(conn):
    problems = _problems(conn, knesset_num=24, query="תקציב")
    assert "Knesset 24" in problems["knesset_no_data"]["message"]
    assert "25" in problems["knesset_no_data"]["message"]


def test_date_range_outside_coverage(conn):
    problem = _problems(conn, date_from="2020-01-01", date_to="2020-12-31")["date_out_of_coverage"]
    assert "2022-11-16" in problem["message"]


def test_date_range_inverted(conn):
    assert "date_range_inverted" in _problems(conn, date_from="2024-05-01", date_to="2024-01-01")


def test_unknown_meeting_id(conn):
    problem = _problems(conn, meeting_ids=["999999999"])["meeting_not_found"]
    assert problem["value"] == "999999999"


def test_meeting_without_summary_points_to_speeches(conn):
    meeting_id = conn.execute("SELECT meeting_id FROM meetings WHERE summary_path IS NULL LIMIT 1").fetchone()[0]
    problem = _problems(conn, meeting_ids=[meeting_id], search_in=["topics", "opinions"])["meeting_has_no_summary"]
    assert 'search_in=["speeches"]' in problem["message"]


def test_mk_not_in_knesset(conn):
    assert "mk_not_in_knesset" in _problems(conn, mk_id="999999")


def test_unknown_party_states_the_fact_without_suggestions(conn):
    problem = _problems(conn, party="מפלגת הפיראטים")["unknown_party"]
    assert problem["suggestions"] == []
    assert problem["message"] == "Party 'מפלגת הפיראטים' is not a faction of the 25th Knesset."


def test_unknown_committee_with_three_suggestions(conn):
    problem = _problems(conn, committees=["ועדת הקסמים והכשפים"])["unknown_committee"]
    assert len(problem["suggestions"]) == 3


def test_latin_query(conn):
    problem = _problems(conn, query="budget")["non_hebrew_query"]
    assert "Hebrew" in problem["message"]


def test_filters_match_but_key_words_do_not(conn):
    problem = _problems(conn, query=f"תקציב {NONEXISTENT_WORD}", mk_id=YAIR_LAPID_MK_ID)["keywords_no_match"]
    assert "AND-ed" in problem["message"]
    assert "יאיר לפיד" in problem["message"]


def test_key_words_without_filters(conn):
    problem = _problems(conn, query=NONEXISTENT_WORD)["keywords_no_match"]
    assert "AND-ed" in problem["message"]


def test_handler_attaches_diagnostics_to_empty_result():
    envelope = tools.handle_query_protocols({"query": NONEXISTENT_WORD, "mk_id": YAIR_LAPID_MK_ID})
    assert envelope.metadata["count"] == 0
    diagnostics = envelope.metadata["diagnostics"]
    assert diagnostics and all(set(d) == DIAGNOSTIC_KEYS for d in diagnostics)
    assert all(d["message"] in envelope.summary for d in diagnostics)


def test_handler_has_no_diagnostics_when_a_scope_has_rows():
    envelope = tools.handle_query_protocols({"query": "תקציב", "search_in": ["topics"], "top_k": 1})
    assert json.loads(envelope.full)["topics"]
    assert "diagnostics" not in envelope.metadata
