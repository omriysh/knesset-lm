"""
tests/test_query_protocols_real_db.py

Behavioural invariants of query_protocols / get_meeting_attendance on the real
Data/knesset.db. Skipped when the db is missing, locked, or its speeches table
is still empty (it is rebuilt in the background).
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config


def _ro():
    return sqlite3.connect(f"file:{Path(config.KNESSET_DB).as_posix()}?mode=ro", uri=True, timeout=30)


@pytest.fixture(scope="module")
def real_db():
    if not Path(config.KNESSET_DB).exists():
        pytest.skip(f"{config.KNESSET_DB} missing")
    try:
        conn = _ro()
        has_speeches = conn.execute("SELECT 1 FROM speeches LIMIT 1").fetchone() is not None
    except sqlite3.Error as exc:
        pytest.skip(f"knesset.db unreadable: {exc}")
    if not has_speeches:
        conn.close()
        pytest.skip("speeches table empty (build in progress)")
    yield conn
    conn.close()


def qp(**args):
    from utils.tools import handle_query_protocols
    env = handle_query_protocols(args)
    assert env.error is None, env.error
    return json.loads(env.full)


class TestRealDb:
    def test_mk_filter_returns_only_that_mks_verified_opinions(self, real_db):
        mk_id = real_db.execute(
            "SELECT mk_id FROM opinions WHERE knesset_num = 25 AND mk_id IS NOT NULL AND quote_verified = 1 "
            "GROUP BY mk_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
        rows = qp(mk_id=mk_id, search_in=["opinions"], top_k=30)["opinions"]
        assert rows and all(r["mk_id"] == mk_id for r in rows)
        assert [r["date"] for r in rows] == sorted((r["date"] for r in rows), reverse=True)

    def test_meeting_topics_in_idx_order(self, real_db):
        mid = real_db.execute(
            "SELECT t.meeting_id FROM topics t JOIN meetings m ON m.meeting_id = t.meeting_id "
            "WHERE m.is_protocol = 1 AND m.knesset_num = 25 GROUP BY t.meeting_id HAVING COUNT(*) >= 3 "
            "ORDER BY t.meeting_id LIMIT 1").fetchone()[0]
        expected = [r[0] for r in real_db.execute("SELECT text FROM topics WHERE meeting_id = ? ORDER BY idx", (mid,))]
        rows = qp(meeting_ids=[mid], search_in=["topics"])["topics"]
        assert [r["topic"] for r in rows] == expected[:50]

    def test_transcript_paging(self, real_db):
        row = real_db.execute(
            "SELECT s.meeting_id FROM speeches s JOIN meetings m ON m.meeting_id = s.meeting_id "
            "WHERE m.knesset_num = 25 AND (m.is_protocol IS NULL OR m.is_protocol = 1) "
            "GROUP BY s.meeting_id HAVING COUNT(*) >= 10 LIMIT 1").fetchone()
        if row is None:
            pytest.skip("no meeting with 10 speeches yet")
        mid = row[0]
        idxs = [r[0] for r in real_db.execute("SELECT idx FROM speeches WHERE meeting_id = ? ORDER BY idx", (mid,))]
        page1 = qp(meeting_ids=[mid], search_in=["speeches"], top_k=5)["speeches"]
        page2 = qp(meeting_ids=[mid], search_in=["speeches"], top_k=5, offset=5)["speeches"]
        assert [r["speech_idx"] for r in page1 + page2] == idxs[:10]

    def test_results_never_from_non_protocol_meetings(self, real_db):
        res = qp(query="תקציב", top_k=100)
        found = {r["meeting_id"] for rows in res.values() for r in rows}
        assert found
        placeholders = ",".join("?" * len(found))
        bad = real_db.execute(f"SELECT meeting_id FROM meetings WHERE is_protocol = 0 AND meeting_id IN ({placeholders})",
                              list(found)).fetchall()
        assert bad == []
        assert all(r["meeting_id"] for r in res["opinions"])

    def test_attendance_matches_table(self, real_db):
        from utils.tools import handle_get_meeting_attendance
        mid, n = real_db.execute(
            "SELECT meeting_id, COUNT(*) FROM attendance WHERE knesset_num = 25 GROUP BY meeting_id LIMIT 1").fetchone()
        full = json.loads(handle_get_meeting_attendance({"meeting_id": mid}).full)
        assert full["meeting_id"] == mid and len(full["attendance"]) == n
