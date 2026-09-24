"""
tests/test_web_browse.py

Keyword-only reading-tab endpoints of web.app against a temp knesset.db seeded
with real rows (see conftest.build_sample_db):
  POST /api/browse/search, GET /api/research/{sid}/meeting/{mid}/hits,
  GET /api/health (db row counts), removed RAG routes → 404.

Assumptions (post-refactor state per spec): the app lifespan is NOT run
(TestClient is used without a context manager); the tests set the app.state
attributes the routes read — settings (web.settings), sessions_dir (tmp dir),
machine (a stub with .name). Browse sessions are created by /api/browse/search
itself; /hits is called with a session saved via web.session.save_session.
"""

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ROLES, SAMPLE, SYN_MEETING, synthetic_rows

M1, M2, M3, M4, M5, M6 = (ROLES[k] for k in ("M1", "M2", "M3", "M4", "M5", "M6"))
SYN = SYN_MEETING
C1 = SAMPLE["committees"]["C1"]
C2 = SAMPLE["committees"]["C2"]


@pytest.fixture()
def client(sample_db, tmp_path):
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


class TestBrowseEmptyQuery:
    def test_newest_meetings_without_filters(self, client):
        ids = mids(search(client))
        assert ids[:2] == [SYN, M6]
        assert set(ids[2:4]) == {M3, M4}
        assert ids[4:] == [M2, M1]

    def test_query_field_may_be_omitted_or_blank(self, client):
        assert mids(search(client, query="")) == mids(search(client)) == mids(search(client, query="   "))

    def test_meeting_shape_and_excerpt_from_topics(self, client):
        meetings = {m["meeting_id"]: m for m in search(client)["meetings"]}
        for m in meetings.values():
            assert {"meeting_id", "title", "date", "committee", "score", "excerpt"} <= set(m)
        assert synthetic_rows()["topics"][0]["text"] in meetings[SYN]["excerpt"]
        first_m2_topic = next(t["text"] for t in SAMPLE["topics"] if t["meeting_id"] == M2 and t["idx"] == 0)
        assert first_m2_topic in meetings[M2]["excerpt"]
        assert meetings[M2]["committee"] == C1

    def test_committee_filter_with_underscores(self, client):
        data = search(client, filters={"committees": [C2.replace(" ", "_")]})
        assert mids(data) == [SYN, M6, M3]

    def test_date_filter(self, client):
        data = search(client, filters={"date_from": "2023-01-05", "date_to": "2023-02-08"})
        assert mids(data)[0] in (M3, M4) and set(mids(data)) == {M2, M3, M4}
        assert mids(data)[-1] == M2

    def test_party_filter(self, client):
        assert mids(search(client, filters={"parties": ["העבודה"]})) == [M3]

    def test_mk_name_filter(self, client):
        assert mids(search(client, filters={"mks": ["גלעד קריב"]})) == [M3]

    def test_guest_filter(self, client):
        assert set(mids(search(client, filters={"guest": "איל קופמן"}))) == {SYN, M6, M3}

    def test_filters_matching_nothing(self, client):
        assert search(client, filters={"committees": ["ועדה שאינה קיימת"]})["meetings"] == []

    def test_top_k(self, client):
        assert mids(search(client, top_k=2)) == [SYN, M6]


class TestBrowseQuery:
    def test_ranked_by_best_speech(self, client):
        ids = mids(search(client, query="העלייה"))
        assert ids[0] == M2
        assert set(ids[1:3]) == {M1, M4}
        assert ids[3:] == [SYN]

    def test_sort_date(self, client):
        assert mids(search(client, query="העלייה", sort="date")) == [SYN, M4, M2, M1]

    def test_excerpt_is_matching_speech(self, client):
        meetings = search(client, query="העלייה")["meetings"]
        assert all("העלייה" in m["excerpt"] for m in meetings)

    def test_query_with_filters(self, client):
        ids = mids(search(client, query="העלייה", filters={"committees": [C1]}))
        assert ids[0] == M2 and set(ids) == {M1, M2, M4}

    def test_not_protocol_meetings_excluded(self, client):
        ids = mids(search(client, query="פרוטוקול"))
        assert M5 not in ids
        assert set(ids) == {M1, M2, M3, M4, M6}
        assert M5 not in mids(search(client, filters={"committees": ["ועדת החינוך התרבות והספורט"]}))

    def test_ktiv_expansion(self, client):
        assert mids(search(client, query="בטחון")) == [SYN]

    def test_no_match(self, client):
        assert search(client, query="רופאים")["meetings"] == []

    def test_session_usable_by_summary_route(self, client):
        data = search(client, query="העלייה")
        r = client.get(f"/api/research/{data['session_id']}/meeting/{M2}/summary")
        assert r.status_code == 200 and r.json()["meeting_id"] == M2


class TestHits:
    def test_hits_for_query(self, client, session_id):
        r = client.get(f"/api/research/{session_id}/meeting/{M2}/hits", params={"q": "העלייה"})
        assert r.status_code == 200
        hits = r.json()["hits"]
        assert {h["speech_idx"] for h in hits} == {0, 1, 4, 7, 8}
        scores = {h["speech_idx"]: h["score"] for h in hits}
        assert all(0 < s <= 1 for s in scores.values())
        assert max(scores.values()) == pytest.approx(1.0)
        assert scores[1] == pytest.approx(1.0)
        assert scores[8] < scores[1]

    def test_ktiv_query(self, client, session_id):
        hits = client.get(f"/api/research/{session_id}/meeting/{SYN}/hits", params={"q": "בטחון"}).json()["hits"]
        assert {h["speech_idx"] for h in hits} == {0, 1, 2}

    def test_empty_q(self, client, session_id):
        r = client.get(f"/api/research/{session_id}/meeting/{M2}/hits", params={"q": ""})
        assert r.status_code == 200 and r.json() == {"hits": []}

    def test_no_match_in_meeting(self, client, session_id):
        r = client.get(f"/api/research/{session_id}/meeting/{M3}/hits", params={"q": "העלייה"})
        assert r.json() == {"hits": []}

    def test_topic_text_as_query(self, client, session_id):
        topic = next(t["text"] for t in SAMPLE["topics"] if t["meeting_id"] == M2 and t["idx"] == 2)
        r = client.get(f"/api/research/{session_id}/meeting/{M2}/hits", params={"q": topic})
        assert r.status_code == 200 and isinstance(r.json()["hits"], list)


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
    def test_reports_db_row_counts(self, client, sample_db):
        import sqlite3
        conn = sqlite3.connect(str(sample_db))
        expected = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("meetings", "topics", "opinions", "speeches")}
        conn.close()
        r = client.get("/api/health")
        assert r.status_code == 200
        counts = _find_counts(r.json())
        assert counts is not None, r.json()
        assert {k: counts[k] for k in expected} == expected
        assert "collections" not in r.json()


class TestRemovedRoutes:
    def test_browse_rag_gone(self, client):
        assert client.post("/api/browse/rag", json={"query": "העלייה"}).status_code == 404

    def test_research_rag_gone(self, client, session_id):
        assert client.get(f"/api/research/{session_id}/rag", params={"query": "העלייה"}).status_code == 404

    def test_pass2_chunks_gone(self, client, session_id):
        assert client.get(f"/api/research/{session_id}/meeting/{M2}/pass2_chunks").status_code == 404

    def test_score_pass2_gone(self, client, session_id):
        r = client.post(f"/api/research/{session_id}/meeting/{M2}/score_pass2", json={"query": "העלייה"})
        assert r.status_code == 404
