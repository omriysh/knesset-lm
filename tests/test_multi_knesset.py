"""
tests/test_multi_knesset.py

Protocols of several Knessets in one knesset.db: the store queries, the filter vocabulary, the tools'
Knesset defaults (protocol tools, find_mk and find_committee span every processed Knesset; find_party
stays on one), the public API / MCP knesset_num handling, the reading tab (/api/meta and browse search
with a knesset filter) and the per-Knesset activity of a candidate profile.

Most tests run on a small scratch db of two Knessets (the same person serving in both under different
parties, one committee name in both, a committee and an MK of one Knesset only). The real-data tests
open Data/knesset.db read-only and derive every expectation from SQL.
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
from api import validation as valid
from api.tool_arguments import AGENT_LIMITS, agent_tool_args, validated_tool_args
from retrieval import knesset_db_store as store
import utils.tools as tools
from utils.tool_helpers.filter_resolution import filter_vocabulary

OLDER, NEWER = 24, 25
BOTH = (OLDER, NEWER)
UNPROCESSED_KNESSET = 23
SHARED_COMMITTEE = "ועדת הכספים"
OLDER_ONLY_COMMITTEE = "ועדת הבריאות"
NEWER_ONLY_COMMITTEE = "ועדת החינוך"
OLDER_PARTY, NEWER_PARTY = "ימינה", "הציונות הדתית"
WORD = "תקציב"

SERVED_BOTH = {"mk_id": "1", "first_name": "אבי", "last_name": "כהן", "full_name": "אבי כהן"}
NEWER_ONLY_MK = {"mk_id": "2", "first_name": "דנה", "last_name": "לוי", "full_name": "דנה לוי"}
OLDER_ONLY_MK = {"mk_id": "3", "first_name": "רון", "last_name": "מזרחי", "full_name": "רון מזרחי"}

MEETINGS = [
    # meeting_id, knesset, committee, date, speaker (mk dict), party of the speaker in that Knesset
    ("a24", OLDER, SHARED_COMMITTEE, "2022-03-01", SERVED_BOTH, OLDER_PARTY),
    ("b24", OLDER, OLDER_ONLY_COMMITTEE, "2022-05-01", OLDER_ONLY_MK, "כחול לבן"),
    ("a25", NEWER, SHARED_COMMITTEE, "2024-01-10", SERVED_BOTH, NEWER_PARTY),
    ("b25", NEWER, NEWER_ONLY_COMMITTEE, "2025-02-01", NEWER_ONLY_MK, "העבודה"),
]
OLDER_MEETINGS = {m[0] for m in MEETINGS if m[1] == OLDER}
NEWER_MEETINGS = {m[0] for m in MEETINGS if m[1] == NEWER}
ALL_MEETINGS = OLDER_MEETINGS | NEWER_MEETINGS


def build_two_knesset_db(path: Path) -> None:
    conn = store.connect(path)
    store.insert_mks(conn, [
        {**SERVED_BOTH, "knesset_num": OLDER, "party": OLDER_PARTY, "aliases": ""},
        {**SERVED_BOTH, "knesset_num": NEWER, "party": NEWER_PARTY, "aliases": ""},
        {**NEWER_ONLY_MK, "knesset_num": NEWER, "party": "העבודה", "aliases": ""},
        {**OLDER_ONLY_MK, "knesset_num": OLDER, "party": "כחול לבן", "aliases": ""},
    ])
    store.insert_committees(conn, [
        {"committee_id": "100", "knesset_num": OLDER, "name": SHARED_COMMITTEE, "is_current": 0},
        {"committee_id": "101", "knesset_num": OLDER, "name": OLDER_ONLY_COMMITTEE, "is_current": 0},
        {"committee_id": "200", "knesset_num": NEWER, "name": SHARED_COMMITTEE, "is_current": 1},
        {"committee_id": "201", "knesset_num": NEWER, "name": NEWER_ONLY_COMMITTEE, "is_current": 1},
    ])
    store.insert_meetings(conn, [{"meeting_id": mid, "knesset_num": k, "committee": committee, "date": date,
                                  "format": "structured"} for mid, k, committee, date, _, _ in MEETINGS])
    store.insert_attendance(conn, [{"meeting_id": mid, "knesset_num": k, "name": mk["full_name"],
                                    "mk_id": mk["mk_id"], "party": party}
                                   for mid, k, _, _, mk, party in MEETINGS])
    store.insert_speeches(conn, [{"meeting_id": mid, "knesset_num": k, "idx": 0, "speaker": mk["full_name"],
                                  "mk_id": mk["mk_id"], "text": f"דיון על {WORD} המדינה בישיבה {mid}"}
                                 for mid, k, _, _, mk, _ in MEETINGS])
    for mid, k, _, _, mk, party in MEETINGS:
        store.replace_meeting_summary(conn, mid, k, f"{mid}.json", True, [f"{WORD} המדינה בישיבה {mid}"], [
            {"speaker_label": mk["full_name"], "speaker_name": mk["full_name"], "mk_id": mk["mk_id"],
             "party": party, "opinion": f"תומך ב{WORD} בישיבה {mid}", "quote": f"{WORD} המדינה",
             "quote_verified": True, "speech_idx": 0, "quote_offset": 0, "quote_length": 5}])
    conn.commit()
    store.rebuild_fts(conn, "summaries")
    store.rebuild_fts(conn, "speeches")
    for mid, k, _, _, mk, _ in MEETINGS:
        store.replace_mk_themes(conn, {"mk_id": mk["mk_id"], "knesset_num": k, "model": "test",
                                       "generated_at": "2026-10-01", "themes": [
                                           {"rank": 0, "title": f"נושא {mid}", "summary": f"סיכום {mid}",
                                            "opinions": [[mid, 0]]}]})
    store.rebuild_fts(conn, "mk_themes")
    conn.close()


@pytest.fixture()
def two_knesset_db(tmp_path, monkeypatch):
    path = tmp_path / "knesset.db"
    build_two_knesset_db(path)
    monkeypatch.setattr(config, "KNESSET_DB", path)
    monkeypatch.setattr(config, "PROTOCOL_KNESSET_NUMS", BOTH)
    return path


@pytest.fixture()
def conn(two_knesset_db):
    c = store.connect(two_knesset_db)
    yield c
    c.close()


def meeting_ids(rows) -> set[str]:
    return {r["meeting_id"] for r in rows}


# ── store ─────────────────────────────────────────────────────────────────────

class TestStoreQueries:
    @pytest.mark.parametrize("scope", store.PROTOCOL_SCOPES)
    @pytest.mark.parametrize("knesset_nums, expected", [(BOTH, ALL_MEETINGS), (OLDER, OLDER_MEETINGS),
                                                         ((NEWER,), NEWER_MEETINGS)])
    def test_protocol_rows_per_knesset_selection(self, conn, scope, knesset_nums, expected):
        rows = store.query_protocol_rows(conn, scope, knesset_nums, top_k=50)
        assert meeting_ids(rows) == expected
        match = tools._fts_match(WORD, f"{scope}_fts")
        assert meeting_ids(store.query_protocol_rows(conn, scope, knesset_nums, match=match, top_k=50)) == expected
        assert store.count_protocol_rows(conn, scope, knesset_nums, cap=50) == len(expected)

    def test_rows_carry_their_knesset(self, conn):
        rows = store.query_protocol_rows(conn, "topics", BOTH, top_k=50)
        assert {r["meeting_id"]: r["knesset_num"] for r in rows} == {m[0]: m[1] for m in MEETINGS}

    def test_speech_party_filter_uses_the_party_of_that_knesset(self, conn):
        rows = store.query_protocol_rows(conn, "speeches", BOTH, party=OLDER_PARTY, top_k=50)
        assert meeting_ids(rows) == {"a24"}
        rows = store.query_protocol_rows(conn, "speeches", BOTH, party=NEWER_PARTY, top_k=50)
        assert meeting_ids(rows) == {"a25"}

    def test_mk_filter_spans_knessets(self, conn):
        rows = store.query_protocol_rows(conn, "opinions", BOTH, mk_id=SERVED_BOTH["mk_id"], top_k=50)
        assert meeting_ids(rows) == {"a24", "a25"}

    def test_search_speeches(self, conn):
        match = tools._fts_match(WORD, "speeches_fts")
        assert meeting_ids(store.search_speeches(conn, match, BOTH, top_k=50)) == ALL_MEETINGS
        assert meeting_ids(store.search_speeches(conn, match, OLDER, top_k=50)) == OLDER_MEETINGS

    @pytest.mark.parametrize("knesset_nums, expected", [(BOTH, {"a24", "a25"}), (NEWER, {"a25"})])
    def test_candidate_meeting_ids(self, conn, knesset_nums, expected):
        assert set(store.query_candidate_meeting_ids(conn, knesset_nums, committees=[SHARED_COMMITTEE])) == expected

    def test_candidate_meetings_by_party_of_either_knesset(self, conn):
        assert set(store.query_candidate_meeting_ids(conn, BOTH, parties=[OLDER_PARTY, NEWER_PARTY])) == {"a24", "a25"}

    @pytest.mark.parametrize("knesset_nums, expected", [(BOTH, ALL_MEETINGS), (OLDER, OLDER_MEETINGS)])
    def test_recent_and_best_speech_meetings(self, conn, knesset_nums, expected):
        recent = store.recent_meetings(conn, knesset_nums, limit=50)
        assert meeting_ids(recent) == expected
        assert [r["date"] for r in recent] == sorted((r["date"] for r in recent), reverse=True)
        match = tools._fts_match(WORD, "speeches_fts")
        assert meeting_ids(store.meetings_by_best_speech(conn, match, knesset_nums, limit=50)) == expected

    def test_mk_name_entries_one_per_person_latest_knesset_wins(self, conn):
        entries = store.name_entries(conn, "mks", BOTH)
        assert sorted(e["id"] for e in entries) == ["1", "2", "3"]
        served_both = next(e for e in entries if e["id"] == SERVED_BOTH["mk_id"])
        assert served_both["extra"]["party"] == NEWER_PARTY and served_both["extra"]["knesset_num"] == NEWER
        assert next(e for e in entries if e["id"] == "3")["extra"]["knesset_num"] == OLDER
        assert sorted(e["id"] for e in store.name_entries(conn, "mks", OLDER)) == ["1", "3"]

    def test_committee_name_entries_of_every_knesset(self, conn):
        entries = store.name_entries(conn, "committees", BOTH)
        assert {(e["id"], e["extra"]["knesset_num"]) for e in entries} == {
            ("100", OLDER), ("101", OLDER), ("200", NEWER), ("201", NEWER)}

    def test_mk_party_map_latest_knesset_wins(self, conn):
        assert store.mk_party_map(conn, BOTH)[SERVED_BOTH["mk_id"]] == NEWER_PARTY
        assert store.mk_party_map(conn, OLDER)[SERVED_BOTH["mk_id"]] == OLDER_PARTY

    def test_single_int_still_accepted(self, conn):
        assert store.query_protocol_rows(conn, "topics", OLDER, top_k=50) == \
            store.query_protocol_rows(conn, "topics", (OLDER,), top_k=50)


class TestFilterVocabulary:
    def test_vocabulary_of_every_knesset(self, conn):
        vocabulary = filter_vocabulary(conn, BOTH)
        assert vocabulary.knesset_nums == BOTH
        assert {OLDER_PARTY, NEWER_PARTY} <= set(vocabulary.party_member_counts)
        assert vocabulary.meeting_date_range == ("2022-03-01", "2025-02-01")
        assert vocabulary.committee_meeting_counts[SHARED_COMMITTEE] == 2
        assert set(vocabulary.mk_name_by_id) == {"1", "2", "3"}

    def test_vocabulary_of_one_knesset(self, conn):
        vocabulary = filter_vocabulary(conn, (OLDER,))
        assert OLDER_PARTY in vocabulary.party_member_counts and NEWER_PARTY not in vocabulary.party_member_counts
        assert NEWER_ONLY_COMMITTEE not in vocabulary.committee_meeting_counts
        assert vocabulary.meeting_date_range == ("2022-03-01", "2022-05-01")


# ── tools ─────────────────────────────────────────────────────────────────────

def query_protocols(**args):
    envelope = tools.handle_query_protocols(args)
    assert envelope.error is None, envelope.error
    return envelope, json.loads(envelope.full)


class TestQueryProtocolsKnessets:
    def test_default_searches_every_processed_knesset(self, two_knesset_db):
        envelope, payload = query_protocols(query=WORD)
        for scope in store.PROTOCOL_SCOPES:
            assert meeting_ids(payload[scope]) == ALL_MEETINGS
        assert envelope.provenance["knesset_num"] is None
        assert envelope.provenance["knesset_nums"] == list(BOTH)

    def test_one_knesset_on_request(self, two_knesset_db):
        envelope, payload = query_protocols(query=WORD, knesset_num=OLDER)
        assert all(meeting_ids(rows) == OLDER_MEETINGS for rows in payload.values())
        assert envelope.provenance["knesset_nums"] == [OLDER]

    def test_party_and_committee_of_the_older_knesset_resolve_by_default(self, two_knesset_db):
        _, payload = query_protocols(party=OLDER_PARTY, committees=[OLDER_ONLY_COMMITTEE], search_in=["topics"])
        assert payload["topics"] == []
        _, payload = query_protocols(party=OLDER_PARTY, search_in=["opinions"])
        assert meeting_ids(payload["opinions"]) == {"a24"}
        _, payload = query_protocols(committees=[OLDER_ONLY_COMMITTEE], search_in=["topics"])
        assert meeting_ids(payload["topics"]) == {"b24"}

    def test_mk_name_of_an_older_knesset_only_mk_resolves_by_default(self, two_knesset_db):
        _, payload = query_protocols(mk_id=OLDER_ONLY_MK["full_name"], search_in=["opinions"])
        assert meeting_ids(payload["opinions"]) == {"b24"}

    def test_party_of_the_other_knesset_is_unknown_in_one_knesset(self, two_knesset_db):
        envelope, payload = query_protocols(party=OLDER_PARTY, knesset_num=NEWER, search_in=["opinions"])
        assert payload["opinions"] == []
        assert any(d.get("problem") == "unknown_party" for d in envelope.metadata["diagnostics"])

    def test_meeting_of_another_knesset_is_diagnosed(self, two_knesset_db):
        envelope, payload = query_protocols(meeting_ids=["a24"], knesset_num=NEWER, search_in=["topics"])
        assert payload["topics"] == []
        assert any(d.get("problem") == "meeting_in_other_knesset" for d in envelope.metadata["diagnostics"])

    def test_meeting_of_any_knesset_lists_by_default(self, two_knesset_db):
        _, payload = query_protocols(meeting_ids=["a24", "b25"], search_in=["topics"])
        assert meeting_ids(payload["topics"]) == {"a24", "b25"}


class TestRosterToolDefaults:
    def test_find_mk_default_finds_an_older_knesset_only_mk(self, two_knesset_db, monkeypatch):
        positions_knessets = []
        monkeypatch.setattr(tools, "_fetch_mk_record", lambda mk_id: {"mk_individual_id": mk_id, "PersonID": mk_id})
        monkeypatch.setattr(tools, "get_mk_positions", lambda person_id, knesset_num: positions_knessets.append(knesset_num) or {})
        envelope = tools.handle_find_mk({"query": OLDER_ONLY_MK["full_name"]})
        payload = json.loads(envelope.full)
        assert payload[0]["mk_id"] == OLDER_ONLY_MK["mk_id"]
        assert positions_knessets[0] == OLDER
        assert envelope.provenance["knesset_num"] is None

    def test_find_mk_positions_from_the_latest_knesset_served(self, two_knesset_db, monkeypatch):
        positions_knessets = []
        monkeypatch.setattr(tools, "_fetch_mk_record", lambda mk_id: {"mk_individual_id": mk_id, "PersonID": mk_id})
        monkeypatch.setattr(tools, "get_mk_positions", lambda person_id, knesset_num: positions_knessets.append(knesset_num) or {})
        payload = json.loads(tools.handle_find_mk({"query": SERVED_BOTH["full_name"]}).full)
        assert payload[0]["mk_id"] == SERVED_BOTH["mk_id"] and positions_knessets[0] == NEWER

    def test_find_mk_one_knesset(self, two_knesset_db, monkeypatch):
        monkeypatch.setattr(tools, "_fetch_mk_record", lambda mk_id: None)
        payload = json.loads(tools.handle_find_mk({"query": OLDER_ONLY_MK["full_name"], "knesset_num": NEWER}).full)
        assert OLDER_ONLY_MK["mk_id"] not in [c["mk_id"] for c in payload if c["score"] >= config.FIND_MK_CONFIDENT_SCORE]

    def test_committee_listing_default_spans_knessets(self, two_knesset_db):
        envelope = tools.handle_find_committee({"query": ""})
        listing = json.loads(envelope.full)
        assert {(c["name"], c["knesset_num"]) for c in listing} == {
            (SHARED_COMMITTEE, OLDER), (OLDER_ONLY_COMMITTEE, OLDER), (SHARED_COMMITTEE, NEWER),
            (NEWER_ONLY_COMMITTEE, NEWER)}
        assert all(c["meeting_count"] == 1 for c in listing)

    def test_committee_listing_of_one_knesset(self, two_knesset_db):
        listing = json.loads(tools.handle_find_committee({"query": "", "knesset_num": OLDER}).full)
        assert {c["name"] for c in listing} == {SHARED_COMMITTEE, OLDER_ONLY_COMMITTEE}

    def test_find_committee_default_matches_every_knesset(self, two_knesset_db, monkeypatch):
        fetched = []
        monkeypatch.setattr(tools, "fetch_committee_record",
                            lambda committee_id, knesset_num=None: fetched.append((committee_id, knesset_num)))
        payload = json.loads(tools.handle_find_committee({"query": OLDER_ONLY_COMMITTEE}).full)
        assert payload[0]["committee_id"] == "101"
        assert all(knesset_num == OLDER for committee_id, knesset_num in fetched if committee_id == "101")

    def test_find_committee_name_of_several_knessets_is_the_latest(self, two_knesset_db, monkeypatch):
        fetched = []
        monkeypatch.setattr(tools, "fetch_committee_record",
                            lambda committee_id, knesset_num=None: fetched.append((committee_id, knesset_num)) or {})
        payload = json.loads(tools.handle_find_committee({"query": SHARED_COMMITTEE}).full)
        assert payload[0]["committee_id"] == "200" and fetched == [("200", NEWER)]
        payload = json.loads(tools.handle_find_committee({"query": SHARED_COMMITTEE, "knesset_num": OLDER}).full)
        assert payload[0]["committee_id"] == "100"

    def test_find_party_default_stays_on_one_knesset(self, monkeypatch):
        asked = []
        monkeypatch.setattr(tools, "get_party_members",
                            lambda party_query, knesset_num, top_k: asked.append(knesset_num) or [])
        envelope = tools.handle_find_party({"query": "הליכוד"})
        assert asked == [max(config.PROTOCOL_KNESSET_NUMS)] and envelope.provenance["knesset_num"] == asked[0]


# ── public API / MCP / agent arguments ────────────────────────────────────────

class TestKnessetNumArgument:
    @pytest.mark.parametrize("tool", ["query_protocols", "find_mk", "find_committee"])
    def test_omitted_means_every_processed_knesset(self, two_knesset_db, tool):
        assert validated_tool_args(tool, {"query": "א"} if tool != "query_protocols" else {})["knesset_num"] is None
        assert "knesset_num" not in agent_tool_args(tool, {"query": "אבי"})

    def test_find_party_defaults_to_the_latest_knesset(self, two_knesset_db):
        assert validated_tool_args("find_party", {"query": "א"})["knesset_num"] == max(config.PROTOCOL_KNESSET_NUMS)

    def test_one_processed_knesset(self, two_knesset_db):
        assert validated_tool_args("query_protocols", {"knesset_num": OLDER})["knesset_num"] == OLDER

    def test_unprocessed_knesset_rejected_for_protocols(self, two_knesset_db):
        with pytest.raises(valid.ApiInputError) as error:
            validated_tool_args("query_protocols", {"knesset_num": UNPROCESSED_KNESSET})
        assert error.value.error_code == "invalid_knesset_num"

    def test_agent_schemas_have_no_protocol_knesset_default(self):
        from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
        properties = {spec.name: spec.schema["properties"] for spec in RESEARCH_TOOL_REGISTRY}
        for tool in ("query_protocols", "find_mk", "find_committee"):
            assert "default" not in properties[tool]["knesset_num"], tool
            assert properties[tool]["knesset_num"].get("description"), tool
        assert properties["find_party"]["knesset_num"]["default"] == max(config.PROTOCOL_KNESSET_NUMS)

    def test_public_schema_describes_the_default(self, two_knesset_db):
        from api.routes import public_tool_schema
        from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
        spec = next(s for s in RESEARCH_TOOL_REGISTRY if s.name == "query_protocols")
        schema = public_tool_schema(spec)
        knesset_property = schema["properties"]["knesset_num"]
        assert knesset_property["enum"] == list(BOTH) and "default" not in knesset_property
        assert "omit" in knesset_property["description"].lower()
        assert "24, 25" in schema["description"]

    @pytest.mark.parametrize("tool", ["find_mk", "find_committee"])
    def test_public_schema_of_roster_tools_allows_every_knesset(self, two_knesset_db, tool):
        from api.routes import public_tool_schema
        from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
        knesset_property = public_tool_schema(next(s for s in RESEARCH_TOOL_REGISTRY if s.name == tool))[
            "properties"]["knesset_num"]
        assert "enum" not in knesset_property
        assert (knesset_property["minimum"], knesset_property["maximum"]) == config.API_KNESSET_NUM_RANGE

    def test_mcp_instructions_cover_every_processed_knesset(self, two_knesset_db):
        from api.mcp_server import mcp_instructions
        text = mcp_instructions()
        assert "25th Knesset only" not in text and "{" not in text
        assert "24, 25" in text


@pytest.fixture()
def api_client(two_knesset_db):
    from api.app import app
    return TestClient(app)


class TestPublicRoutes:
    def test_protocols_route_default_and_one_knesset(self, api_client):
        body = api_client.get("/v1/protocols", params={"q": WORD, "search_in": "topics"}).json()
        assert meeting_ids(body["results"]["topics"]) == ALL_MEETINGS
        body = api_client.get("/v1/protocols", params={"q": WORD, "search_in": "topics", "knesset_num": OLDER}).json()
        assert meeting_ids(body["results"]["topics"]) == OLDER_MEETINGS

    def test_protocols_route_rejects_unprocessed_knesset(self, api_client):
        response = api_client.get("/v1/protocols", params={"q": WORD, "knesset_num": UNPROCESSED_KNESSET})
        assert response.status_code == 400 and response.json()["error_code"] == "invalid_knesset_num"

    def test_committees_route_lists_every_knesset_by_default(self, api_client):
        body = api_client.get("/v1/committees").json()
        assert {(c["name"], c["knesset_num"]) for c in body["results"]} >= {(OLDER_ONLY_COMMITTEE, OLDER),
                                                                             (NEWER_ONLY_COMMITTEE, NEWER)}

    def test_openapi_describes_the_default(self, api_client):
        parameters = api_client.get("/openapi.json").json()["paths"]["/v1/protocols"]["get"]["parameters"]
        knesset_parameter = next(p for p in parameters if p["name"] == "knesset_num")
        assert "default 25" not in knesset_parameter.get("description", "").lower()
        assert "omit" in knesset_parameter.get("description", "").lower()


# ── web: reading tab ──────────────────────────────────────────────────────────

@pytest.fixture()
def web_client(two_knesset_db, tmp_path, monkeypatch):
    import web.app as webapp
    import web.settings as settings
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    webapp.app.state.settings = settings
    webapp.app.state.sessions_dir = sessions
    webapp.app.state.machine = SimpleNamespace(name="test_machine", version=2)
    webapp.forget_meta_cache()
    monkeypatch.setattr(webapp, "_mk_fuzzy_index", None)
    monkeypatch.setattr(webapp, "_mk_fuzzy_loaded", False)
    yield TestClient(webapp.app)
    webapp.forget_meta_cache()


def browse(client, **filters):
    response = client.post("/api/browse/search", json={"query": "", "filters": filters})
    assert response.status_code == 200, response.text
    return {m["meeting_id"] for m in response.json()["meetings"]}


class TestReadingTabMeta:
    def test_meta_of_every_knesset(self, web_client):
        meta = web_client.get("/api/meta").json()
        assert meta["knessets"] == list(BOTH)
        assert set(meta["committees"]) == {SHARED_COMMITTEE, OLDER_ONLY_COMMITTEE, NEWER_ONLY_COMMITTEE}
        assert set(meta["mks"]) == {SERVED_BOTH["full_name"], NEWER_ONLY_MK["full_name"], OLDER_ONLY_MK["full_name"]}
        assert {OLDER_PARTY, NEWER_PARTY} <= set(meta["parties"])

    def test_meta_of_one_knesset(self, web_client):
        meta = web_client.get("/api/meta", params={"knesset": OLDER}).json()
        assert set(meta["committees"]) == {SHARED_COMMITTEE, OLDER_ONLY_COMMITTEE}
        assert set(meta["mks"]) == {SERVED_BOTH["full_name"], OLDER_ONLY_MK["full_name"]}
        assert OLDER_PARTY in meta["parties"] and NEWER_PARTY not in meta["parties"]
        assert meta["knessets"] == list(BOTH)

    def test_meta_rejects_unprocessed_knesset(self, web_client):
        assert web_client.get("/api/meta", params={"knesset": UNPROCESSED_KNESSET}).status_code == 400


class TestReadingTabBrowse:
    def test_no_knesset_filter_is_every_knesset(self, web_client):
        assert browse(web_client) == ALL_MEETINGS

    def test_knesset_filter(self, web_client):
        assert browse(web_client, knesset=OLDER) == OLDER_MEETINGS
        assert browse(web_client, knesset=NEWER, committees=[SHARED_COMMITTEE]) == {"a25"}

    def test_mk_of_the_older_knesset_resolves(self, web_client):
        assert browse(web_client, mks=[OLDER_ONLY_MK["full_name"]]) == {"b24"}

    def test_ranked_search_with_knesset_filter(self, web_client):
        response = web_client.post("/api/browse/search", json={"query": WORD, "filters": {"knesset": OLDER}})
        assert {m["meeting_id"] for m in response.json()["meetings"]} == OLDER_MEETINGS

    def test_unprocessed_knesset_rejected(self, web_client):
        response = web_client.post("/api/browse/search", json={"query": "", "filters": {"knesset": UNPROCESSED_KNESSET}})
        assert response.status_code == 400


# ── web: candidate profile activity per Knesset ───────────────────────────────

def candidate(position, mk, knessets):
    return {"position": position, "name_raw": mk["full_name"], "name": mk["full_name"], "from_party": "",
            "mk_id": mk["mk_id"], "person_id": int(mk["mk_id"]), "knessets": knessets, "profile": "full",
            "photo": None, "photo_source": None, "wikipedia": None, "details": {}, "site_id": None}


PROFILE_PARTY = {"id": 5, "letters": "בד", "name": "רשימת בדיקה", "submitted_by": "", "gov_url": "https://www.gov.il/x",
                 "ballot": None, "logo": None, "website": None, "leader": SERVED_BOTH["full_name"],
                 "candidates": [candidate(1, SERVED_BOTH, [OLDER, NEWER]), candidate(2, OLDER_ONLY_MK, [OLDER])]}
PROFILE_BASE = "/api/profiles/party/5/candidate"


@pytest.fixture()
def profile_client(web_client, tmp_path, monkeypatch):
    lists_dir = tmp_path / "lists"
    lists_dir.mkdir()
    (lists_dir / "lists.json").write_text(json.dumps({"source": "", "built_at": "2026-10-09", "parties": [PROFILE_PARTY]},
                                                     ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "candidate_lists_dir", lambda election_knesset_num=26: lists_dir)
    positions_asked = []

    def positions(person_id, knesset_num):
        positions_asked.append(knesset_num)
        committee_id = {OLDER: 100, NEWER: 200}[knesset_num]
        return {"factions": [], "govministries": [], "knesset_roles": [], "faction_chairpersons": [],
                "committee_positions": [{"committee_id": committee_id, "committee_name": SHARED_COMMITTEE,
                                         "position": "חבר ועדה", "start_date": "2021-01-01", "finish_date": None}]}
    monkeypatch.setattr("utils.knesset_db.get_mk_positions", positions)
    web_client.positions_asked = positions_asked
    return web_client


def get_ok(client, url, **params):
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


class TestProfileActivityKnessets:
    def test_themes_start_on_the_latest_knesset_with_data(self, profile_client):
        themes = get_ok(profile_client, f"{PROFILE_BASE}/1/themes")
        assert themes["knesset_num"] == NEWER and themes["knessets"] == [NEWER, OLDER]
        assert [t["title"] for t in themes["themes"]] == ["נושא a25"]

    def test_themes_of_a_chosen_knesset(self, profile_client):
        themes = get_ok(profile_client, f"{PROFILE_BASE}/1/themes", knesset=OLDER)
        assert themes["knesset_num"] == OLDER and [t["title"] for t in themes["themes"]] == ["נושא a24"]

    def test_older_knesset_only_mk_starts_on_its_knesset(self, profile_client):
        themes = get_ok(profile_client, f"{PROFILE_BASE}/2/themes")
        assert themes["knesset_num"] == OLDER and themes["knessets"] == [OLDER]

    @pytest.mark.parametrize("tab", ["themes", "opinions"])
    def test_knesset_without_activity_rejected(self, profile_client, tab):
        assert profile_client.get(f"{PROFILE_BASE}/2/{tab}", params={"knesset": NEWER}).status_code == 400
        assert profile_client.get(f"{PROFILE_BASE}/1/{tab}", params={"knesset": "x"}).status_code in (400, 422)

    def test_opinions_per_knesset(self, profile_client):
        latest = get_ok(profile_client, f"{PROFILE_BASE}/1/opinions")
        assert latest["knesset_num"] == NEWER and meeting_ids(latest["opinions"]) == {"a25"}
        older = get_ok(profile_client, f"{PROFILE_BASE}/1/opinions", knesset=OLDER)
        assert older["knesset_num"] == OLDER and meeting_ids(older["opinions"]) == {"a24"} and older["total"] == 1

    def test_theme_filter_follows_the_theme_knesset(self, profile_client):
        older_theme = get_ok(profile_client, f"{PROFILE_BASE}/1/themes", knesset=OLDER)["themes"][0]
        page = get_ok(profile_client, f"{PROFILE_BASE}/1/opinions", theme=str(older_theme["id"]))
        assert meeting_ids(page["opinions"]) == {"a24"}

    def test_header_stats_latest_attendance_per_knesset(self, profile_client):
        activity = get_ok(profile_client, f"{PROFILE_BASE}/1")["activity"]
        assert activity["knesset_num"] == NEWER and activity["opinions"] == 1
        assert activity["attendance"]["meetings_attended"] == 1
        blocks = activity["attendance_by_knesset"]
        assert [b["knesset_num"] for b in blocks] == [NEWER, OLDER]
        assert all(b["member_meetings"] == 1 and b["member_meetings_attended"] == 1 for b in blocks)
        assert all(b["per_committee"] == [[SHARED_COMMITTEE, 1]] for b in blocks)
        assert sorted(profile_client.positions_asked) == [OLDER, NEWER]

    def test_parties_list_names_the_activity_knessets(self, profile_client):
        assert get_ok(profile_client, "/api/profiles/parties")["activity_knessets"] == list(BOTH)


# ── candidate lists: a full profile for an MK of any processed Knesset ────────

def test_candidate_profile_depth_counts_every_processed_knesset(two_knesset_db):
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    import build_candidate_lists as lists
    db_mk_ids = lists.profile_mk_ids()
    assert db_mk_ids == {"1", "2", "3"}
    assert lists.profile_depth({"mk_id": "3", "knessets": [OLDER]}, db_mk_ids) == "full"
    assert lists.profile_depth({"mk_id": "999", "knessets": [20]}, db_mk_ids) == "bills"


# ── the real knesset.db (read-only) ───────────────────────────────────────────

class TestRealDb:
    def test_config_lists_every_knesset_with_meetings(self, real_conn):
        with_meetings = [r[0] for r in real_conn.execute("SELECT DISTINCT knesset_num FROM meetings ORDER BY 1")]
        assert list(config.PROTOCOL_KNESSET_NUMS) == with_meetings

    def test_default_query_reaches_every_knesset(self, real_db, real_conn):
        oldest = min(config.PROTOCOL_KNESSET_NUMS)
        oldest_meeting = real_conn.execute(
            "SELECT meeting_id FROM topics WHERE knesset_num = ? ORDER BY meeting_id LIMIT 1", (oldest,)).fetchone()
        if oldest_meeting is None:
            pytest.skip(f"no Knesset {oldest} topics yet (summaries rebuild in progress?)")
        _, payload = query_protocols(meeting_ids=[oldest_meeting[0]], search_in=["topics"])
        expected = real_conn.execute("SELECT COUNT(*) FROM topics WHERE meeting_id = ?", (oldest_meeting[0],)).fetchone()[0]
        assert len(payload["topics"]) == min(expected, config.QUERY_PROTOCOLS_DEFAULT_TOP_K)
        assert {r["knesset_num"] for r in payload["topics"]} == {oldest}

    def test_one_knesset_listing_matches_sql(self, real_db, real_conn):
        for knesset_num in config.PROTOCOL_KNESSET_NUMS:
            _, payload = query_protocols(search_in=["topics"], knesset_num=knesset_num, top_k=5)
            newest = real_conn.execute(
                "SELECT MAX(m.date) FROM meetings m WHERE m.knesset_num = ? AND (m.is_protocol IS NULL OR m.is_protocol != 0) "
                "AND EXISTS (SELECT 1 FROM topics t WHERE t.meeting_id = m.meeting_id)", (knesset_num,)).fetchone()[0]
            assert payload["topics"] and payload["topics"][0]["date"] == newest
            assert {r["knesset_num"] for r in payload["topics"]} == {knesset_num}
