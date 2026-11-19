"""
tests/test_web_subjects.py

The subjects tab routes (web/subjects.py, profiles/subjects.py) on the real Data/knesset.db, with a small
lists.json holding one MK of the first approach so the candidate link can be checked.
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


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def first_approach(real_conn):
    row = real_conn.execute("""SELECT a.subject_id, a.id, t.mk_id FROM subject_approaches a
                               JOIN subject_themes st ON st.approach_id = a.id JOIN mk_themes t ON t.id = st.theme_id
                               ORDER BY a.subject_id, a.rank LIMIT 1""").fetchone()
    if row is None:
        pytest.skip("the db has no subjects")
    return row


@pytest.fixture()
def client(first_approach, tmp_path, monkeypatch):
    candidate = {"position": 1, "name": "מועמד/ת", "name_raw": "", "profile": "full", "mk_id": first_approach[2],
                 "knessets": [25], "photo": None}
    lists = {"parties": [{"id": 1, "letters": "א", "name": "רשימה", "submitted_by": "", "gov_url": "", "ballot": None,
                          "leader": "", "candidates": [candidate]}]}
    (tmp_path / "lists.json").write_text(json.dumps(lists, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "candidate_lists_dir", lambda election_knesset_num=26: tmp_path)
    import web.app as webapp
    import web.settings as settings
    webapp.app.state.settings = settings
    webapp.app.state.sessions_dir = tmp_path
    webapp.app.state.machine = SimpleNamespace(name="test_machine", version=2)
    webapp.rate_limiter.reset()
    return TestClient(webapp.app)


class TestSubjectRoutes:
    def test_areas_hold_every_subject(self, client, real_conn):
        areas = ok(client.get("/api/subjects"))["areas"]
        subject_ids = [s["id"] for area in areas for s in area["subjects"]]
        assert sorted(subject_ids) == [r[0] for r in real_conn.execute(
            "SELECT id FROM subjects WHERE knesset_num = ? ORDER BY id", (config.ROSTER_DEFAULT_KNESSET_NUM,))]
        assert [a["mk_count"] for a in areas] == sorted((a["mk_count"] for a in areas), reverse=True)

    def test_subject_approaches_count_their_themes(self, client, first_approach, real_conn):
        subject = ok(client.get(f"/api/subjects/{first_approach[0]}"))
        assert [a["rank"] for a in subject["approaches"]] == sorted(a["rank"] for a in subject["approaches"])
        for approach in subject["approaches"]:
            assert approach["theme_count"] == real_conn.execute(
                "SELECT COUNT(*) FROM subject_themes WHERE approach_id = ?", (approach["id"],)).fetchone()[0]

    def test_approach_themes_link_candidates_and_share_quarters(self, client, first_approach):
        subject_id, approach_id, mk_id = first_approach
        body = ok(client.get(f"/api/subjects/{subject_id}/approach/{approach_id}"))
        assert body["themes"] and all(len(t["quarter_counts"]) == len(body["quarters"]) for t in body["themes"])
        linked = [t["mk"] for t in body["themes"] if t["mk"]["mk_id"] == mk_id]
        assert linked[0]["profile_url"] == "/profiles/party/1/candidate/1"
        assert all("profile_url" not in t["mk"] for t in body["themes"] if t["mk"]["mk_id"] != mk_id)

    def test_theme_opinions_belong_to_the_theme(self, client, first_approach):
        theme = ok(client.get(f"/api/subjects/{first_approach[0]}/approach/{first_approach[1]}"))["themes"][0]
        page = ok(client.get(f"/api/subjects/theme/{theme['id']}/opinions"))
        assert page["total"] == theme["opinion_count"]
        assert all(theme["id"] in o["theme_ids"] for o in page["opinions"])

    def test_process_counts_each_step(self, client, real_conn):
        counts = ok(client.get("/api/subjects/process"))
        areas = ok(client.get("/api/subjects"))["areas"]
        assert counts["subjects"] == sum(len(area["subjects"]) for area in areas)
        assert counts["areas"] == len(areas)
        assert counts["opinions"] >= counts["themes"] >= counts["subjects"] > 0
        assert counts["approaches"] == real_conn.execute("SELECT COUNT(*) FROM subject_approaches WHERE knesset_num = ?",
                                                         (counts["knesset_num"],)).fetchone()[0]

    def test_unknown_is_404(self, client, first_approach):
        assert client.get("/api/subjects/999999").status_code == 404
        assert client.get(f"/api/subjects/{first_approach[0]}/approach/999999").status_code == 404
        assert client.get("/api/subjects/theme/999999999/opinions").status_code == 404

    @pytest.mark.parametrize("path", ["/subjects", "/subjects/area/x", "/subjects/1", "/subjects/1/approach/2"])
    def test_pages_serve_the_app(self, client, path):
        assert "subjects.js" in client.get(path).text
