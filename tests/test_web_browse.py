"""
tests/test_web_browse.py

Keyword-only reading-tab endpoints of web.app on the real Data/knesset.db:
  POST /api/browse/search, GET /api/research/{sid}/meeting/{mid}/hits,
  GET /api/health (db row counts), removed RAG routes → 404.

The app lifespan is not run (TestClient without a context manager); the tests set the app.state
attributes the routes read. Expected meetings come from SQL over the same db (conftest.real_conn).
"""

import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fastapi.testclient import TestClient

import utils.tools as tools

NONSENSE_WORD = "קשקשתאינהקיימתבשוםפרוטוקול"


@pytest.fixture()
def client(real_db, tmp_path):
    import web.app as webapp
    import web.settings as settings
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    webapp.app.state.settings = settings
    webapp.app.state.sessions_dir = sessions
    webapp.app.state.machine = SimpleNamespace(name="test_machine", version=2)
    return TestClient(webapp.app)


@pytest.fixture()
def session_id(client):
    from web.session import ResearchSession, save_session
    import web.app as webapp
    sid = str(uuid.uuid4())
    save_session(ResearchSession(session_id=sid, status="done", original_question="",
                                 created_at="2026-09-24T00:00:00.000Z", updated_at="2026-09-24T00:00:00.000Z",
                                 workspace_data={}), webapp.app.state.sessions_dir)
    return sid


def search(client, **body):
    r = client.post("/api/browse/search", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("session_id")
    return data


def mids(data):
    return [m["meeting_id"] for m in data["meetings"]]


def only_meeting_filters(real_db):
    return {"committees": [real_db.meeting_committee], "date_from": real_db.meeting_date,
            "date_to": real_db.meeting_date}


def iso_date(browse_date: str) -> str:
    """The reading tab shows dates as DD/MM/YYYY; the db stores YYYY-MM-DD."""
    day, month, year = browse_date.split("/")
    return f"{year}-{month}-{day}"


def assert_newest_first(meetings):
    dates = [iso_date(m["date"]) for m in meetings]
    assert dates == sorted(dates, reverse=True)


def meeting_set(conn, sql, *params) -> set[str]:
    return {r[0] for r in conn.execute(sql, params)}


class TestBrowseEmptyQuery:
    def test_newest_meetings_first(self, client, real_conn):
        meetings = search(client)["meetings"]
        assert meetings
        newest = real_conn.execute("SELECT MAX(date) FROM meetings WHERE is_protocol IS NULL OR is_protocol != 0"
                                   ).fetchone()[0]
        assert iso_date(meetings[0]["date"]) == newest
        assert_newest_first(meetings)

    def test_query_field_may_be_omitted_or_blank(self, client):
        assert mids(search(client, query="")) == mids(search(client)) == mids(search(client, query="   "))

    def test_meeting_shape_and_excerpt_from_topics(self, client, real_db):
        meetings = {m["meeting_id"]: m for m in search(client, filters=only_meeting_filters(real_db))["meetings"]}
        for m in meetings.values():
            assert {"meeting_id", "title", "date", "committee", "score", "excerpt"} <= set(m)
        assert real_db.meeting_first_topic in meetings[real_db.meeting_id]["excerpt"]
        assert meetings[real_db.meeting_id]["committee"] == real_db.meeting_committee

    def test_committee_filter_with_underscores(self, client, real_db):
        data = search(client, filters={"committees": [real_db.other_committee.replace(" ", "_")]})
        assert data["meetings"] and {m["committee"] for m in data["meetings"]} == {real_db.other_committee}
        assert_newest_first(data["meetings"])

    def test_date_filter(self, client, real_db):
        meetings = search(client, filters={"date_from": real_db.meeting_date, "date_to": real_db.meeting_date})["meetings"]
        assert real_db.meeting_id in mids({"meetings": meetings})
        assert {iso_date(m["date"]) for m in meetings} == {real_db.meeting_date}

    def test_party_filter(self, client, real_db, real_conn):
        ids = set(mids(search(client, filters={"parties": [real_db.mk_party]})))
        assert ids and ids <= meeting_set(real_conn, "SELECT meeting_id FROM attendance WHERE party = ?",
                                          real_db.mk_party)

    def test_mk_name_filter(self, client, real_db, real_conn):
        ids = set(mids(search(client, filters={"mks": [real_db.mk_name]})))
        assert ids and ids <= meeting_set(real_conn, "SELECT meeting_id FROM attendance WHERE mk_id = ?", real_db.mk_id)

    def test_guest_filter(self, client, real_db, real_conn):
        guest = real_conn.execute("SELECT name FROM attendance WHERE meeting_id = ? AND mk_id IS NULL ORDER BY name",
                                  (real_db.meeting_id,)).fetchone()[0]
        filters = {**only_meeting_filters(real_db), "guest": guest}
        assert real_db.meeting_id in mids(search(client, filters=filters))

    def test_filters_matching_nothing(self, client, real_db):
        assert search(client, filters={"committees": ["ועדה שאינה קיימת"]})["meetings"] == []

    def test_top_k(self, client):
        assert mids(search(client, top_k=2)) == mids(search(client))[:2]


class TestBrowseQuery:
    def test_ranked_query_finds_the_meeting(self, client, real_db):
        ids = mids(search(client, query=real_db.topic_word, filters={"committees": [real_db.meeting_committee]}))
        assert real_db.meeting_id in ids

    def test_sort_date(self, client, real_db):
        meetings = search(client, query=real_db.topic_word, sort="date")["meetings"]
        assert meetings
        assert_newest_first(meetings)

    def test_excerpt_is_a_matching_speech(self, client):
        meetings = search(client, query="תקציב")["meetings"]
        assert meetings and sum("תקציב" in m["excerpt"] for m in meetings) >= len(meetings) // 2

    def test_query_with_filters(self, client, real_db):
        meetings = search(client, query="תקציב", filters={"committees": [real_db.other_committee]})["meetings"]
        assert meetings and {m["committee"] for m in meetings} == {real_db.other_committee}

    def test_not_protocol_meetings_excluded(self, client, real_db, real_conn):
        committee, date = real_conn.execute("SELECT committee, date FROM meetings WHERE meeting_id = ?",
                                            (real_db.non_protocol_meeting_id,)).fetchone()
        filters = {"committees": [committee], "date_from": date, "date_to": date}
        assert real_db.non_protocol_meeting_id not in mids(search(client, filters=filters))

    def test_ktiv_expansion(self, client):
        assert search(client, query="בטחון")["meetings"]

    def test_no_match(self, client):
        assert search(client, query=NONSENSE_WORD)["meetings"] == []

    def test_session_usable_by_summary_route(self, client, real_db):
        data = search(client, query=real_db.topic_word)
        r = client.get(f"/api/research/{data['session_id']}/meeting/{real_db.meeting_id}/summary")
        assert r.status_code == 200 and r.json()["meeting_id"] == real_db.meeting_id


class TestHits:
    def test_hits_are_the_matching_speeches(self, client, session_id, real_db, real_conn):
        word = "הוועדה"
        r = client.get(f"/api/research/{session_id}/meeting/{real_db.meeting_id}/hits", params={"q": word})
        assert r.status_code == 200
        hits = r.json()["hits"]
        match = tools._fts_match(word, "speeches_fts")
        expected = meeting_set(real_conn, "SELECT x.idx FROM speeches_fts JOIN speeches x ON x.id = speeches_fts.rowid "
                                          "WHERE speeches_fts MATCH ? AND x.meeting_id = ?", match, real_db.meeting_id)
        assert hits and {h["speech_idx"] for h in hits} == expected
        scores = [h["score"] for h in hits]
        assert all(0 < s <= 1 for s in scores) and max(scores) == pytest.approx(1.0)

    def test_hits_carry_the_keyword_ranges_in_the_speech(self, client, session_id, real_db, real_conn):
        word = "הוועדה"
        hits = client.get(f"/api/research/{session_id}/meeting/{real_db.meeting_id}/hits",
                          params={"q": word}).json()["hits"]
        assert hits
        text_by_idx = {r["idx"]: r["text"] for r in real_conn.execute(
            "SELECT idx, text FROM speeches WHERE meeting_id = ?", (real_db.meeting_id,))}
        for hit in hits:
            text = text_by_idx[hit["speech_idx"]]
            assert hit["ranges"] and hit["matched_words"] == 1 and hit["query_words"] == 1
            for offset, length in hit["ranges"]:
                assert 0 <= offset and length > 0 and offset + length <= len(text)
                assert "ועד" in text[offset:offset + length]
            assert hit["ranges"] == sorted(hit["ranges"])

    def test_every_query_word_is_marked(self, client, session_id, real_db, real_conn):
        query = "הוועדה חבר"
        hits = client.get(f"/api/research/{session_id}/meeting/{real_db.meeting_id}/hits",
                          params={"q": query}).json()["hits"]
        both = [h for h in hits if h["matched_words"] == 2]
        if not both:
            pytest.skip("no speech with both words")
        text = real_conn.execute("SELECT text FROM speeches WHERE meeting_id = ? AND idx = ?",
                                 (real_db.meeting_id, both[0]["speech_idx"])).fetchone()[0]
        marked = [text[o:o + n] for o, n in both[0]["ranges"]]
        assert any("ועד" in m for m in marked) and any("חבר" in m for m in marked)
        assert all(h["query_words"] == 2 for h in hits)

    def test_empty_q(self, client, session_id, real_db):
        r = client.get(f"/api/research/{session_id}/meeting/{real_db.meeting_id}/hits", params={"q": ""})
        assert r.status_code == 200 and r.json() == {"hits": []}

    def test_no_match_in_meeting(self, client, session_id, real_db):
        r = client.get(f"/api/research/{session_id}/meeting/{real_db.meeting_id}/hits", params={"q": NONSENSE_WORD})
        assert r.json() == {"hits": []}

    def test_topic_text_as_query(self, client, session_id, real_db):
        r = client.get(f"/api/research/{session_id}/meeting/{real_db.meeting_id}/hits",
                       params={"q": real_db.meeting_first_topic[:150]})
        assert r.status_code == 200 and isinstance(r.json()["hits"], list)

    def test_topic_query_on_the_biggest_meeting_is_fast(self, client, session_id, real_conn):
        meeting_id, speech_count = real_conn.execute(
            "SELECT meeting_id, COUNT(*) AS speech_count FROM speeches GROUP BY meeting_id "
            "HAVING meeting_id IN (SELECT meeting_id FROM topics) ORDER BY speech_count DESC LIMIT 1").fetchone()
        topic = real_conn.execute("SELECT text FROM topics WHERE meeting_id = ? ORDER BY idx LIMIT 1",
                                  (meeting_id,)).fetchone()[0]
        started = time.monotonic()
        r = client.get(f"/api/research/{session_id}/meeting/{meeting_id}/hits", params={"q": topic})
        elapsed = time.monotonic() - started
        assert r.status_code == 200, r.text
        hits = r.json()["hits"]
        assert hits and all(h["ranges"] for h in hits)
        assert elapsed < 3, f"{elapsed:.1f}s for a topic of meeting {meeting_id} ({speech_count} speeches)"


def _find_counts(obj):
    if isinstance(obj, dict):
        if {"meetings", "topics", "opinions", "speeches"} <= set(obj) and all(
                isinstance(obj[k], int) for k in ("meetings", "topics", "opinions", "speeches")):
            return obj
        for v in obj.values():
            found = _find_counts(v)
            if found:
                return found
    return None


class TestHealth:
    def test_reports_db_row_counts(self, client, real_db):
        r = client.get("/api/health")
        assert r.status_code == 200
        counts = _find_counts(r.json())
        assert counts is not None, r.json()
        for table in ("meetings", "topics", "opinions", "speeches"):
            assert counts[table] == real_db.table_counts[table], table
        assert "collections" not in r.json()


class TestRemovedRoutes:
    def test_browse_rag_gone(self, client):
        assert client.post("/api/browse/rag", json={"query": "תקציב"}).status_code == 404

    def test_research_rag_gone(self, client, session_id):
        assert client.get(f"/api/research/{session_id}/rag", params={"query": "תקציב"}).status_code == 404

    def test_pass2_chunks_gone(self, client, session_id, real_db):
        assert client.get(f"/api/research/{session_id}/meeting/{real_db.meeting_id}/pass2_chunks").status_code == 404

    def test_score_pass2_gone(self, client, session_id, real_db):
        r = client.post(f"/api/research/{session_id}/meeting/{real_db.meeting_id}/score_pass2", json={"query": "תקציב"})
        assert r.status_code == 404


class TestTypedWordsRankFirst:
    """"ביוקר" also matches its variant "ביקר" ("visited"); meetings and speeches with the word as typed come first."""
    TYPED_WORD = "ביוקר"

    def meetings_with_typed_word(self, real_conn) -> set[str]:
        return meeting_set(real_conn, "SELECT DISTINCT x.meeting_id FROM speeches_fts "
                                      "JOIN speeches x ON x.id = speeches_fts.rowid WHERE speeches_fts MATCH ?",
                           tools._fts_exact_match(self.TYPED_WORD))

    def test_search_lists_meetings_with_the_typed_word_first(self, client, real_conn):
        meetings = search(client, query=self.TYPED_WORD, top_k=500,
                          filters={"date_from": "2024-03-01", "date_to": "2024-03-31"})["meetings"]
        assert len(meetings) < 500
        with_typed_word = self.meetings_with_typed_word(real_conn)
        typed_flags = [m["meeting_id"] in with_typed_word for m in meetings]
        assert True in typed_flags and False in typed_flags
        assert typed_flags == sorted(typed_flags, reverse=True)
        scores = [m["score"] for m in meetings]
        assert scores == sorted(scores, reverse=True) and scores[0] == pytest.approx(1.0)

    def test_heatmap_scores_speeches_with_the_typed_word_higher(self, client, session_id, real_conn):
        meeting_id = real_conn.execute(
            "SELECT x.meeting_id FROM speeches_fts JOIN speeches x ON x.id = speeches_fts.rowid "
            "WHERE speeches_fts MATCH ? AND x.meeting_id IN (SELECT x2.meeting_id FROM speeches_fts "
            "JOIN speeches x2 ON x2.id = speeches_fts.rowid WHERE speeches_fts MATCH '\"ביקר\"') LIMIT 1",
            (tools._fts_exact_match(self.TYPED_WORD),)).fetchone()[0]
        hits = client.get(f"/api/research/{session_id}/meeting/{meeting_id}/hits",
                          params={"q": self.TYPED_WORD}).json()["hits"]
        typed_idxs = meeting_set(real_conn, "SELECT x.idx FROM speeches_fts JOIN speeches x ON x.id = speeches_fts.rowid "
                                            "WHERE speeches_fts MATCH ? AND x.meeting_id = ?",
                                 tools._fts_exact_match(self.TYPED_WORD), meeting_id)
        typed_scores = [h["score"] for h in hits if h["speech_idx"] in typed_idxs]
        variant_scores = [h["score"] for h in hits if h["speech_idx"] not in typed_idxs]
        assert typed_scores and variant_scores and min(typed_scores) > max(variant_scores)
