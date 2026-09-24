"""
tests/test_knesset_db_store.py

Tests for retrieval.knesset_db_store (schema, upserts, structural candidate
filter) and for summarization.summary_io / output_parsing.locate_quote.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
from retrieval import knesset_db_store as store
from summarization.output_parsing import locate_quote
from summarization.summary_io import load_summary, render_summary_text


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    path = tmp_path / "knesset.db"
    monkeypatch.setattr(config, "KNESSET_DB", path)
    c = store.connect()
    store.insert_mks(c, [
        {"mk_id": "1", "knesset_num": 25, "first_name": "שמחה", "last_name": "רוטמן", "full_name": "שמחה רוטמן",
         "party": "הציונות הדתית", "aliases": ""},
        {"mk_id": "2", "knesset_num": 25, "first_name": "גלעד", "last_name": "קריב", "full_name": "גלעד קריב",
         "party": "העבודה", "aliases": ""},
    ])
    store.insert_meetings(c, [
        {"meeting_id": "m1", "knesset_num": 25, "committee": "ועדת החינוך", "date": "2025-01-06", "format": "structured"},
        {"meeting_id": "m2", "knesset_num": 25, "committee": "ועדת הכספים", "date": "2025-02-10", "format": "full_text"},
        {"meeting_id": "m3", "knesset_num": 25, "committee": "ועדת הכספים", "date": "2025-03-01", "format": "structured"},
    ])
    store.insert_attendance(c, [
        {"meeting_id": "m1", "knesset_num": 25, "name": "שמחה רוטמן", "mk_id": "1", "party": "הציונות הדתית"},
        {"meeting_id": "m1", "knesset_num": 25, "name": "גור בליי", "mk_id": None, "party": None},
        {"meeting_id": "m2", "knesset_num": 25, "name": "גלעד קריב", "mk_id": "2", "party": "העבודה"},
    ])
    store.replace_meeting_summary(c, "m1", 25, "m1.json", True, ["תקציב החינוך", "מעונות יום"], [
        {"speaker_label": "שמחה רוטמן (הציונות הדתית)", "speaker_name": "שמחה רוטמן", "mk_id": "1",
         "party": "הציונות הדתית", "opinion": "תומך בתקציב", "quote": "אני תומך", "quote_verified": True,
         "speech_idx": 3, "quote_offset": 10},
    ])
    store.replace_meeting_summary(c, "m2", 25, "m2.json", True, ["תקציב הביטחון"], [])
    store.replace_meeting_summary(c, "m3", 25, "m3.json", False, [], [])
    c.commit()
    store.rebuild_fts(c, "summaries")
    yield c
    c.close()


class TestMeetings:
    def test_meeting_upsert_keeps_summary_columns(self, conn):
        store.insert_meetings(conn, [{"meeting_id": "m1", "knesset_num": 25, "committee": "ועדה חדשה"}])
        row = store.get_meeting(conn, "m1")
        assert row["committee"] == "ועדה חדשה" and row["summary_path"] == "m1.json" and row["is_protocol"] == 1

    def test_not_protocol_marked(self, conn):
        assert store.get_meeting(conn, "m3")["is_protocol"] == 0
        assert store.meeting_ids_with_summary(conn, 25) == {"m1", "m2", "m3"}

    def test_attendance_order_mks_first(self, conn):
        assert [a["name"] for a in store.get_attendance(conn, "m1")] == ["שמחה רוטמן", "גור בליי"]

    def test_name_entries_shape(self, conn):
        entries = store.name_entries(conn, "mks", 25)
        assert {e["id"] for e in entries} == {"1", "2"}
        assert entries[0]["extra"]["mk_id"] == entries[0]["id"]
        assert store.mk_party_map(conn, 25)["2"] == "העבודה"


class TestSummaryRows:
    def test_replace_is_idempotent(self, conn):
        store.replace_meeting_summary(conn, "m1", 25, "m1.json", True, ["נושא אחד"], [])
        conn.commit()
        store.rebuild_fts(conn, "summaries")
        assert [t["text"] for t in store.get_topics(conn, "m1")] == ["נושא אחד"]
        assert store.get_opinions(conn, "m1") == []

    def test_get_opinions_fields(self, conn):
        op = store.get_opinions(conn, "m1")[0]
        assert op["speaker"] == "שמחה רוטמן (הציונות הדתית)" and op["speaker_name"] == "שמחה רוטמן"
        assert op["speech_idx"] == 3 and op["quote_offset"] == 10 and op["quote_verified"] == 1


class TestCandidateMeetingIds:
    def test_no_filters_returns_none(self, conn):
        assert store.query_candidate_meeting_ids(conn, 25) is None

    def test_committee_and_date(self, conn):
        assert store.query_candidate_meeting_ids(conn, 25, committees=["ועדת הכספים"]) == ["m3", "m2"]
        assert store.query_candidate_meeting_ids(conn, 25, committees=["ועדת הכספים"], date_to="2025-02-28") == ["m2"]
        assert store.query_candidate_meeting_ids(conn, 25, committees=["אין"]) == []

    def test_mk_party_guest_ored(self, conn):
        assert store.query_candidate_meeting_ids(conn, 25, mk_ids=["1"]) == ["m1"]
        assert store.query_candidate_meeting_ids(conn, 25, parties=["העבודה"]) == ["m2"]
        assert store.query_candidate_meeting_ids(conn, 25, mk_ids=["1"], parties=["העבודה"]) == ["m2", "m1"]
        assert store.query_candidate_meeting_ids(conn, 25, guest_name="בליי") == ["m1"]

    def test_committee_and_mk_combined(self, conn):
        assert store.query_candidate_meeting_ids(conn, 25, committees=["ועדת הכספים"], mk_ids=["1"]) == []


class TestSummaryIo:
    def test_load_summary_defaults(self, tmp_path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps({"is_protocol": True, "topics": ["a"], "opinions": [{"speaker": "s", "opinion": "o"}]},
                                ensure_ascii=False), encoding="utf-8")
        assert load_summary(p) == {"is_protocol": True, "topics": ["a"],
                                   "opinions": [{"speaker": "s", "opinion": "o", "quote": "", "quote_verified": False}]}

    def test_render_sections(self):
        text = render_summary_text(["נושא"], [{"speaker": "דובר", "opinion": "עמדה", "quote": "ציטוט"}],
                                   [{"name": "שמחה רוטמן", "party": "הציונות הדתית"}, {"name": "אורח"}])
        assert "## נוכחים\nשמחה רוטמן (הציונות הדתית), אורח" in text
        assert "- נושא" in text and '### דובר\n- עמדה — "ציטוט"' in text
        assert render_summary_text(["נושא"], [], [], section="topics") == "## נושאים\n- נושא"


class TestLocateQuote:
    def test_offset_points_at_raw_text(self):
        text = 'ח"כ יעל: הַחוק מסוכן, לדעתי.\nתודה.'
        offset = locate_quote('"החוק מסוכן, לדעתי"', text)
        assert text[offset:offset + 5] == "הַחוק"
        assert locate_quote("לא קיים", text) is None
        assert locate_quote("", text) is None
