"""
tests/test_search_opinions.py

Tests for utils.tools.handle_search_opinions — FTS over the opinions table of
knesset.db, hard-filtered to one MK, verified quotes only.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
from utils.tools import handle_search_opinions
from agent.subgraph.evidence import ToolEnvelope
from retrieval import knesset_db_store as store


MK_A = "101"   # שמחה רוטמן — three opinions, two about חינוך, one unverified
MK_B = "102"   # גלעד קריב — one opinion about חינוך

MEETINGS = [
    {"meeting_id": "m1", "knesset_num": 25, "committee": "ועדת החינוך", "date": "2025-01-06"},
    {"meeting_id": "m2", "knesset_num": 25, "committee": "ועדת החינוך", "date": "2025-02-10"},
    {"meeting_id": "m3", "knesset_num": 25, "committee": "ועדת הכספים", "date": "2025-03-01"},
]

def _op(label, name, mk_id, opinion, quote, verified=True):
    party = {MK_A: "הציונות הדתית", MK_B: "העבודה"}.get(mk_id)
    return {"speaker_label": label, "speaker_name": name, "mk_id": mk_id, "party": party,
            "opinion": opinion, "quote": quote, "quote_verified": verified}

OPINIONS = {
    "m1": [
        _op("שמחה רוטמן (הציונות הדתית)", "שמחה רוטמן", MK_A, "תמך ברפורמה בנושא חינוך", "אני תומך ברפורמה"),
        _op("גלעד קריב (העבודה)", "גלעד קריב", MK_B, "התנגד לרפורמה בנושא חינוך", "הרפורמה הזו מסוכנת"),
        _op("גור בליי", "גור בליי", None, "הסביר את הסעיף בנושא חינוך", "הסעיף קובע"),
    ],
    "m2": [
        _op("שמחה רוטמן (הציונות הדתית)", "שמחה רוטמן", MK_A, "התנגד לקיצוץ בתקציב מערכת חינוך", "אסור לקצץ בחינוך"),
    ],
    "m3": [
        _op("שמחה רוטמן (הציונות הדתית)", "שמחה רוטמן", MK_A, "הציג עמדה בנושא ביטחון ותקציבו", "ציטוט לא מאומת", verified=False),
    ],
}


@pytest.fixture()
def opinions_db(tmp_path, monkeypatch):
    path = tmp_path / "knesset.db"
    monkeypatch.setattr(config, "KNESSET_DB", path)
    conn = store.connect(path)
    store.insert_meetings(conn, MEETINGS)
    for mid, ops in OPINIONS.items():
        store.replace_meeting_summary(conn, mid, 25, f"{mid}.json", True, ["נושא"], ops)
    conn.commit()
    store.rebuild_fts(conn, "summaries")
    conn.close()
    return path


def _payload(env: ToolEnvelope) -> list[dict]:
    return json.loads(env.full)


class TestValidation:
    def test_missing_mk_and_party_is_error(self, opinions_db):
        env = handle_search_opinions({"query": "חינוך"})
        assert env.error == "missing_mk_id"

    def test_missing_db_returns_missing_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "nope.db")
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        assert env.error == "knesset_db_missing"

    def test_never_raises_returns_envelope(self, opinions_db):
        assert isinstance(handle_search_opinions({}), ToolEnvelope)


class TestMkFilter:
    def test_returns_only_opinions_of_requested_mk(self, opinions_db):
        rows = _payload(handle_search_opinions({"query": "חינוך", "mk_id": MK_A}))
        assert rows and all(r["mk_id"] == MK_A for r in rows)
        assert {r["meeting_id"] for r in rows} == {"m1", "m2"}

    def test_other_mk_gets_their_own(self, opinions_db):
        rows = _payload(handle_search_opinions({"query": "חינוך", "mk_id": MK_B}))
        assert [r["meeting_id"] for r in rows] == ["m1"]
        assert rows[0]["speaker"] == "גלעד קריב"

    def test_unverified_quotes_never_returned(self, opinions_db):
        rows = _payload(handle_search_opinions({"query": "ביטחון", "mk_id": MK_A}))
        assert rows == []

    def test_unknown_mk_returns_empty_not_error(self, opinions_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": "999"})
        assert env.error is None and _payload(env) == []

    def test_empty_query_lists_newest_first(self, opinions_db):
        rows = _payload(handle_search_opinions({"mk_id": MK_A}))
        assert [r["meeting_id"] for r in rows] == ["m2", "m1"]

    def test_party_filter(self, opinions_db):
        rows = _payload(handle_search_opinions({"query": "חינוך", "party": "העבודה"}))
        assert [r["mk_id"] for r in rows] == [MK_B]


class TestPayload:
    def test_hit_fields(self, opinions_db):
        row = _payload(handle_search_opinions({"query": "מערכת", "mk_id": MK_A}))[0]
        assert row["opinion"] == "התנגד לקיצוץ בתקציב מערכת חינוך"
        assert row["quote"] == "אסור לקצץ בחינוך"
        assert row["committee"] == "ועדת החינוך" and row["date"] == "2025-02-10"
        assert row["party"] == "הציונות הדתית"

    def test_metadata_and_provenance(self, opinions_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A, "top_k": 1})
        assert env.metadata["kind"] == "search" and env.metadata["count"] == 1
        assert env.provenance["mk_id"] == MK_A and env.provenance["top_k"] == 1

    def test_top_k_caps_results(self, opinions_db):
        assert len(_payload(handle_search_opinions({"query": "חינוך", "mk_id": MK_A, "top_k": 1}))) == 1


class TestRegistry:
    def test_search_opinions_registered(self):
        from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
        spec = next((s for s in RESEARCH_TOOL_REGISTRY if s.name == "search_opinions"), None)
        assert spec is not None
        assert set(spec.schema["required"]) == {"mk_id"}
        assert spec.handler is handle_search_opinions
