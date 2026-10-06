"""
tests/test_web_profiles.py

The candidate-profile routes of web.app (web/profiles.py) on the real Data/knesset.db, with a small
lists.json in a temp dir: one candidate with a full profile (גלעד קריב, a 25th Knesset MK), one former MK
(bills only) and one who was never an MK. OData-backed routes are checked for input validation and for
a 502 when the Knesset API is down; their live behaviour is covered by tests/test_odata_tools.py.
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
from api.rate_limit import route_bucket

KARIV = {"position": 1, "name_raw": "קריב גלעד", "name": "גלעד קריב", "from_party": "העבודה", "mk_id": "30807",
         "person_id": 30807, "knessets": [24, 25], "profile": "full", "photo": "photos/7_1.jpg",
         "photo_source": "knesset", "wikipedia": None, "details": {"residence": "גבעתיים"}, "site_id": 1066}
FORMER = {"position": 2, "name_raw": "גולן יאיר", "name": "יאיר גולן", "from_party": "", "mk_id": "30743",
          "person_id": 30743, "knessets": [22, 23, 24], "profile": "bills", "photo": None, "photo_source": None,
          "wikipedia": None, "details": {}}
NEWCOMER = {"position": 3, "name_raw": "כהן דנה", "name": "דנה כהן", "from_party": "", "mk_id": None,
            "person_id": None, "knessets": [], "profile": "none", "photo": None, "photo_source": None,
            "wikipedia": None, "details": {}}
PARTY = {"id": 7, "letters": "אמת", "name": "רשימת בדיקה", "submitted_by": "", "gov_url": "https://www.gov.il/he/pages/x",
         "ballot": None, "logo": "logos/7.png", "website": "https://example.org.il/", "leader": "גלעד קריב",
         "candidates": [KARIV, FORMER, NEWCOMER]}
NO_MK_PARTY = {"id": 8, "letters": "א", "name": "אאא רשימה חדשה", "submitted_by": "", "gov_url": "https://www.gov.il/he/pages/y",
               "ballot": None, "leader": "דנה כהן", "candidates": [NEWCOMER]}
OTHER_MK_PARTY = {**PARTY, "id": 9, "name": "תתת רשימה", "logo": None, "website": None}
BASE = "/api/profiles/party/7/candidate"


@pytest.fixture()
def lists_dir(tmp_path, monkeypatch):
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "7_1.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (tmp_path / "logos").mkdir()
    (tmp_path / "logos" / "7.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "lists.json").write_text(json.dumps({"source": "https://www.gov.il/he/pages/candidates-lists-26",
                                                     "built_at": "2026-10-05",
                                                     "parties": [OTHER_MK_PARTY, NO_MK_PARTY, PARTY]},
                                                    ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "candidate_lists_dir", lambda election_knesset_num=26: tmp_path)
    return tmp_path


@pytest.fixture()
def client(real_db, lists_dir, tmp_path):
    import web.app as webapp
    import web.settings as settings
    webapp.app.state.settings = settings
    webapp.app.state.sessions_dir = tmp_path
    webapp.app.state.machine = SimpleNamespace(name="test_machine", version=2)
    webapp.rate_limiter.reset()
    return TestClient(webapp.app)


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


class TestLists:
    def test_parties(self, client):
        party = ok(client.get("/api/profiles/parties"))["parties"][0]
        assert party["id"] == 7 and party["candidate_count"] == 3
        assert party["logo_url"] == "/api/profiles/logo/7" and party["website"] == "https://example.org.il/"
        assert party["full_profiles"] == 1 and party["former_mks"] == 1
        assert party["leader_photo_url"] == "/api/profiles/photo/7/1"

    def test_lists_with_mks_first_then_alphabetical(self, client):
        assert [p["id"] for p in ok(client.get("/api/profiles/parties"))["parties"]] == [7, 9, 8]

    def test_logo(self, client):
        response = client.get("/api/profiles/logo/7")
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"

    def test_party_candidates_in_list_order(self, client):
        party = ok(client.get("/api/profiles/party/7"))
        assert [c["position"] for c in party["candidates"]] == [1, 2, 3]
        assert [c["profile"] for c in party["candidates"]] == ["full", "bills", "none"]
        assert "person_id" not in party["candidates"][0]

    @pytest.mark.parametrize("path", ["/api/profiles/party/10", f"{BASE}/9", "/api/profiles/photo/7/2",
                                      "/api/profiles/photo/10/1", "/api/profiles/ballot/7", "/api/profiles/logo/9"])
    def test_unknown_is_404(self, client, path):
        assert client.get(path).status_code == 404

    @pytest.mark.parametrize("path", ["/api/profiles/party/abc", "/api/profiles/photo/7/..%2F..%2Fx",
                                      "/api/profiles/party/7/candidate/1.5"])
    def test_ids_are_integers(self, client, path):
        assert client.get(path).status_code in (400, 404)

    def test_photo(self, client):
        response = client.get("/api/profiles/photo/7/1")
        assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"

    @pytest.mark.parametrize("path", ["/profiles", "/profiles/party/7", "/profiles/party/7/candidate/1"])
    def test_pages_serve_the_app(self, client, path):
        response = client.get(path)
        assert response.status_code == 200 and 'id="profiles-root"' in response.text


class TestProfileDepth:
    def test_newcomer_has_header_only(self, client):
        body = ok(client.get(f"{BASE}/3"))
        assert body["candidate"]["profile"] == "none" and "activity" not in body
        for tab in ("themes", "opinions", "votes", "bills", "roles", "cosponsors", "vote-summary"):
            assert client.get(f"{BASE}/3/{tab}").status_code == 404, tab

    def test_former_mk_has_no_committee_activity(self, client):
        for tab in ("themes", "opinions"):
            assert client.get(f"{BASE}/2/{tab}").status_code == 404, tab


class TestCommitteeActivity:
    def test_themes_with_spread_evidence(self, client):
        data = ok(client.get(f"{BASE}/1/themes"))
        assert data["themes"] and data["total_opinions"] >= data["opinions_in_a_theme"] > 0
        for theme in data["themes"]:
            assert len(theme["quarter_counts"]) == len(data["quarters"])
            assert 0 < len(theme["evidence"]) <= 5
            dates = [e["date"] for e in theme["evidence"]]
            assert dates == sorted(dates)

    def test_other_theme_is_opinions_in_no_theme(self, client):
        data = ok(client.get(f"{BASE}/1/opinions", params={"theme": "other"}))
        assert data["total"] == data["facets"]["themes"]["other"] > 0
        assert all(not o["theme_ids"] for o in data["opinions"])

    def test_theme_filter_and_facets(self, client):
        everything = ok(client.get(f"{BASE}/1/opinions"))
        theme_id, count = next((k, v) for k, v in everything["facets"]["themes"].items() if k != "other")
        page = ok(client.get(f"{BASE}/1/opinions", params={"theme": theme_id}))
        assert page["total"] == count and all(int(theme_id) in o["theme_ids"] for o in page["opinions"])
        dates = [o["date"] for o in everything["opinions"]]
        assert dates == sorted(dates, reverse=True)

    def test_search_and_paging(self, client):
        first = ok(client.get(f"{BASE}/1/opinions", params={"q": "משטרה"}))
        second = ok(client.get(f"{BASE}/1/opinions", params={"q": "משטרה", "offset": 30}))
        assert first["total"] > 30
        assert not {o["id"] for o in first["opinions"]} & {o["id"] for o in second["opinions"]}

    def test_date_range(self, client):
        params = {"date_from": "2024-01-01", "date_to": "2024-03-31"}
        page = ok(client.get(f"{BASE}/1/opinions", params=params))
        everything = ok(client.get(f"{BASE}/1/opinions"))
        assert 0 < page["total"] < everything["total"]
        assert all("2024-01-01" <= o["date"] <= "2024-03-31" for o in page["opinions"])

    def test_header_attendance(self, client, monkeypatch):
        monkeypatch.setattr("utils.knesset_db.get_mk_positions", lambda person_id, knesset_num: {
            "factions": [], "govministries": [], "knesset_roles": [], "faction_chairpersons": [],
            "committee_positions": [{"committee_id": 4191, "committee_name": "ועדת החוקה, חוק ומשפט",
                                     "position": "חבר ועדה", "start_date": "2023-01-09", "finish_date": None}]})
        activity = ok(client.get(f"{BASE}/1"))["activity"]
        attendance = activity["attendance"]
        assert 0 < attendance["member_meetings_attended"] <= attendance["member_meetings"]
        assert attendance["meetings_attended"] >= attendance["member_meetings_attended"]
        assert all(isinstance(row, list) and len(row) == 2 for row in attendance["per_committee"])
        assert activity["opinions"] > 0


class TestInputValidation:
    @pytest.mark.parametrize("params,error_code", [
        ({"theme": "1 OR 1=1"}, "invalid_theme"),
        ({"theme": "-1"}, "invalid_theme"),
        ({"q": "א" * 500}, "invalid_query"),
        ({"q": "!!!"}, "invalid_query"),
        ({"committee": "ועדה" * 100}, "invalid_committee"),
        ({"date_from": "2024-02-30"}, "invalid_date_from"),
        ({"date_to": "2024-01-01' OR 1=1"}, "invalid_date_to"),
        ({"offset": -1}, "invalid_offset"),
        ({"offset": 10_000_000}, "invalid_offset"),
    ])
    def test_opinions(self, client, params, error_code):
        response = client.get(f"{BASE}/1/opinions", params=params)
        assert response.status_code == 400 and response.json()["error_code"] == error_code

    @pytest.mark.parametrize("params,error_code", [
        ({"role": "author"}, "invalid_role"),
        ({"role": "initiator' or '1'='1"}, "invalid_role"),
        ({"stage": "third"}, "invalid_stage"),
        ({"stage": "passed) or (1 eq 1"}, "invalid_stage"),
        ({"q": "א" * 500}, "invalid_query"),
        ({"offset": -5}, "invalid_offset"),
    ])
    def test_bills(self, client, params, error_code):
        response = client.get(f"{BASE}/1/bills", params=params)
        assert response.status_code == 400 and response.json()["error_code"] == error_code

    def test_votes_query_length(self, client):
        assert client.get(f"{BASE}/1/votes", params={"q": "א" * 500}).status_code == 400


class TestUpstreamDown:
    @pytest.mark.parametrize("tab", ["votes", "bills", "cosponsors", "vote-summary"])
    def test_odata_failure_is_502(self, client, upstream_down, tab):
        response = client.get(f"{BASE}/2/{tab}")
        assert response.status_code == 502 and response.json()["error"]

    def test_roles_report_each_knesset(self, client, upstream_down):
        knessets = ok(client.get(f"{BASE}/2/roles"))["knessets"]
        assert [k["knesset_num"] for k in knessets] == [24, 23, 22] and all(k["error"] for k in knessets)

    def test_header_survives_without_roles(self, client, upstream_down):
        body = ok(client.get(f"{BASE}/1"))
        assert body["roles"] is None and body["activity"]["opinions"] > 0


class TestRateLimitBuckets:
    @pytest.mark.parametrize("path,bucket", [
        ("/profiles/party/7/candidate/1", None),
        ("/api/profiles/photo/7/1", None),
        ("/api/profiles/ballot/7", None),
        ("/api/profiles/logo/7", None),
        ("/api/profiles/parties", "web"),
        (f"{BASE}/1/themes", "db"),
        (f"{BASE}/1/opinions", "db"),
        (f"{BASE}/1", "profiles"),
        (f"{BASE}/1/bills", "profiles"),
        ("/api/profiles/bill/123/text", "profiles"),
    ])
    def test_bucket(self, path, bucket):
        assert route_bucket(path) == bucket
