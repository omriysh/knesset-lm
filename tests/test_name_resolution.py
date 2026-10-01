"""
Name resolution for the research tools against the real Data/knesset.db: find_mk scoring,
find_party aliases / listing, find_committee listing, and the server-side resolution of the
query_protocols party / committees / mk_id filters. Cases are taken from production MCP logs.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config
from api.tool_arguments import AGENT_LIMITS, PUBLIC_API_LIMITS, validated_tool_args
from retrieval import knesset_db_store as store
from utils import knesset_db, tools
from utils.tool_helpers import filter_resolution
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex

pytestmark = pytest.mark.skipif(not store.exists(), reason="Data/knesset.db not built")

SHAS = 'התאחדות הספרדים שומרי תורה תנועתו של מרן הרב עובדיה יוסף זצ"ל'
SCIENCE_COMMITTEE_SINGLE_SPACE = "ועדת המדע והטכנולוגיה"
SCIENCE_COMMITTEE_ID = "4195"
YAIR_LAPID_MK_ID = "878"
CONFIDENT_SCORE = 0.75


@pytest.fixture(scope="module")
def conn():
    connection = store.connect()
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def db_party_names(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT party FROM mks WHERE knesset_num = 25 AND party IS NOT NULL")]


@pytest.fixture(scope="module")
def mk_index(conn) -> FuzzyNameIndex:
    return FuzzyNameIndex(store.name_entries(conn, "mks", 25), require_query_token_coverage=True)


def _rows(envelope) -> dict:
    return json.loads(envelope.full) if envelope.full else {}


# ── find_mk scoring ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("query, wrong_mk", [
    ("יאיר גולן", "יאיר לפיד"),
    ("יאיר גולן", "מאי גולן"),
    ("יורם כהן", "מאיר כהן"),
    ("לירן אבישר בן חורין", "מירב בן ארי"),
])
def test_single_shared_name_token_is_not_a_confident_match(mk_index, query, wrong_mk):
    hits = {hit["label"]: hit["score"] for hit in mk_index.search(query, top_k=10)}
    assert hits.get(wrong_mk, 0.0) < CONFIDENT_SCORE


@pytest.mark.parametrize("query, expected_mk", [
    ("יאיר לפיד", "יאיר לפיד"),
    ("לפיד", "יאיר לפיד"),
    ("בן גביר", "איתמר בן גביר"),
    ("סמוטריץ", "בצלאל סמוטריץ'"),
    ("מרב בן ארי", "מירב בן ארי"),
])
def test_real_mk_names_still_match_confidently(mk_index, query, expected_mk):
    top = mk_index.search(query, top_k=1)[0]
    assert top["label"] == expected_mk and top["score"] >= 0.85


def test_find_mk_weak_match_carries_hint(monkeypatch):
    monkeypatch.setattr(tools, "_fetch_mk_record", lambda mk_id: None)
    envelope = tools.handle_find_mk({"query": "יאיר גולן"})
    payload = _rows(envelope)
    assert all(not (c["full_name"] == "יאיר לפיד" and c["score"] >= CONFIDENT_SCORE) for c in payload)
    assert "No MK of Knesset 25" in envelope.summary and "יאיר גולן" in envelope.summary
    assert envelope.metadata["hints"]


def test_find_mk_numeric_query_is_flagged_as_mk_id(monkeypatch):
    monkeypatch.setattr(tools, "_fetch_mk_record", lambda mk_id: None)
    envelope = tools.handle_find_mk({"query": YAIR_LAPID_MK_ID})
    assert "looks like an mk_id" in envelope.summary


def test_find_mk_confident_match_has_no_hint(monkeypatch):
    monkeypatch.setattr(tools, "_fetch_mk_record", lambda mk_id: None)
    envelope = tools.handle_find_mk({"query": "יאיר לפיד"})
    assert _rows(envelope)[0]["mk_id"] == YAIR_LAPID_MK_ID
    assert "hints" not in envelope.metadata


# ── party aliases and scoring ────────────────────────────────────────────────

def test_every_party_alias_points_to_a_real_party(db_party_names):
    assert len(db_party_names) == 15
    unknown_targets = {canonical for canonical in config.PARTY_ALIASES.values()
                       if canonical not in db_party_names}
    assert not unknown_targets


@pytest.mark.parametrize("query, expected_party", [
    ("ליכוד", "הליכוד"),
    ("Likud", "הליכוד"),
    ('ש"ס', SHAS),
    ("שס", SHAS),
    ("ש״ס", SHAS),
    ("עוצמה יהודית", "עוצמה יהודית בראשות איתמר בן גביר"),
    ("הציונות הדתית", "הציונות הדתית בראשות בצלאל סמוטריץ'"),
    ("כחול לבן", "כחול לבן - המחנה הממלכתי"),
    ("המחנה הממלכתי", "המחנה הממלכתי"),
    ("יש עתיד", "יש עתיד"),
    ("ישראל ביתנו", "ישראל ביתנו"),
    ("העבודה", "העבודה"),
    ("רעם", 'רע"ם'),
    ('חד"ש', 'חד"ש-תע"ל'),
    ("הליכוד", "הליכוד"),
    ("likud", "הליכוד"),
    ("raam", 'רע"ם'),
    ("hadash", 'חד"ש-תע"ל'),
    ("utj", "יהדות התורה"),
    ("יהדות התורה המאוחדת", "יהדות התורה"),
    ("labor", "העבודה"),
    ("מפלגת העבודה", "העבודה"),
    ("נועם", "נעם - בראשות אבי מעוז"),
    ("yesh atid", "יש עתיד"),
    ("otzma yehudit", "עוצמה יהודית בראשות איתמר בן גביר"),
    ("religious zionism", "הציונות הדתית בראשות בצלאל סמוטריץ'"),
    ("yisrael beiteinu", "ישראל ביתנו"),
    ("blue and white", "כחול לבן - המחנה הממלכתי"),
])
def test_resolve_party(db_party_names, query, expected_party):
    assert filter_resolution.resolve_party(query, db_party_names).party == expected_party


def test_resolve_party_does_not_guess_on_a_word_fragment(db_party_names):
    resolution = filter_resolution.resolve_party("ישר", db_party_names)
    assert resolution.party is None
    assert not hasattr(resolution, "candidates")


@pytest.mark.parametrize("removed_alias", ["הדמוקרטים", "תקווה חדשה", "new hope", "אגודת ישראל", "דגל התורה"])
def test_renames_mergers_and_component_parties_are_not_aliases(db_party_names, removed_alias):
    assert removed_alias not in config.PARTY_ALIASES
    assert filter_resolution.resolve_party(removed_alias, db_party_names).party is None
    assert filter_resolution.matching_party_names(removed_alias, db_party_names) == []


def _fake_oknesset_members(conn) -> list[dict]:
    return [{"mk_individual_id": r["mk_id"], "mk_individual_first_name": r["first_name"],
             "mk_individual_name": r["last_name"], "IsCurrent": True,
             "factions": [{"knesset": 25, "faction_name": r["party"], "start_date": "2022-11-15"}]
             if r["party"] else []}
            for r in conn.execute("SELECT * FROM mks WHERE knesset_num = 25")]


@pytest.fixture()
def offline_roster(conn, monkeypatch):
    members = _fake_oknesset_members(conn)
    monkeypatch.setattr(knesset_db, "_get_all_members_raw", lambda knesset_num=25: members)


@pytest.mark.parametrize("query, expected_party", [('ש"ס', SHAS), ("ליכוד", "הליכוד"), ("Likud", "הליכוד")])
def test_find_party_alias_with_score(offline_roster, query, expected_party):
    payload = _rows(tools.handle_find_party({"query": query}))
    assert payload[0]["party"] == expected_party
    assert payload[0]["score"] == 1.0
    assert payload[0]["members"]


def test_find_party_unknown_name_returns_no_party_and_states_the_fact(offline_roster):
    envelope = tools.handle_find_party({"query": "ישר"})
    assert _rows(envelope) == []
    assert envelope.metadata["hints"] == ["Party 'ישר' is not a faction of the 25th Knesset."]


def test_find_party_unknown_name_uses_the_queried_knesset(offline_roster):
    envelope = tools.handle_find_party({"query": "ישר", "knesset_num": 23})
    assert envelope.metadata["hints"] == ["Party 'ישר' is not a faction of the 23rd Knesset."]


def test_find_party_resolved_name_returns_only_that_party(offline_roster):
    assert [p["party"] for p in _rows(tools.handle_find_party({"query": "ליכוד"}))] == ["הליכוד"]


@pytest.mark.parametrize("number, expected", [(1, "1st"), (2, "2nd"), (3, "3rd"), (11, "11th"), (12, "12th"),
                                              (13, "13th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (25, "25th")])
def test_ordinal(number, expected):
    from utils.tool_helpers.filter_diagnostics import ordinal
    assert ordinal(number) == expected


def test_find_party_empty_query_lists_all_parties(offline_roster, db_party_names):
    payload = _rows(tools.handle_find_party({"query": ""}))
    assert {p["party"] for p in payload} == set(db_party_names)
    assert all(p["mk_count"] > 0 for p in payload)


def test_find_committee_empty_query_lists_committees_with_meetings():
    envelope = tools.handle_find_committee({"query": ""})
    payload = _rows(envelope)
    by_name = {c["name"]: c["meeting_count"] for c in payload}
    assert by_name[SCIENCE_COMMITTEE_SINGLE_SPACE] == 77 + 38
    assert all(count > 0 for count in by_name.values())


def test_find_party_and_committee_schemas_do_not_require_query():
    from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
    schemas = {spec.name: spec.schema for spec in RESEARCH_TOOL_REGISTRY}
    assert "query" not in schemas["find_party"].get("required", [])
    assert "query" not in schemas["find_committee"].get("required", [])


# ── query_protocols party filter ─────────────────────────────────────────────

def test_query_protocols_resolves_party_alias():
    envelope = tools.handle_query_protocols({"party": "ליכוד", "search_in": ["opinions"], "top_k": 5})
    rows = _rows(envelope)["opinions"]
    assert rows and all(r["party"] == "הליכוד" for r in rows)
    assert envelope.provenance["party"] == "הליכוד"
    assert 'party "ליכוד" → "הליכוד"' in envelope.metadata["warnings"]


def test_query_protocols_resolves_shas():
    envelope = tools.handle_query_protocols({"party": 'ש"ס', "search_in": ["opinions"], "top_k": 3})
    assert _rows(envelope)["opinions"]
    assert envelope.provenance["party"] == SHAS


def test_query_protocols_unknown_party_returns_nothing_without_suggestions():
    envelope = tools.handle_query_protocols({"party": "ישר", "search_in": ["opinions"], "top_k": 3})
    assert _rows(envelope)["opinions"] == []
    party_diagnostics = [d for d in envelope.metadata["diagnostics"] if d["filter"] == "party"]
    assert party_diagnostics and party_diagnostics[0]["suggestions"] == []
    assert party_diagnostics[0]["message"] == "Party 'ישר' is not a faction of the 25th Knesset."


# ── committee filter ─────────────────────────────────────────────────────────

def test_committee_names_are_stored_whitespace_normalized(conn):
    """Was: 'ועדת  המדע  והטכנולוגיה' (double spaces) kept 38 meetings apart from the single-spaced name."""
    assert conn.execute("SELECT COUNT(*) FROM meetings WHERE committee LIKE '%  %' OR committee <> TRIM(committee)"
                        ).fetchone()[0] == 0
    vocabulary = filter_resolution.filter_vocabulary(conn, 25)
    resolution = filter_resolution.resolve_committee(SCIENCE_COMMITTEE_SINGLE_SPACE, vocabulary)
    assert resolution.db_names == [SCIENCE_COMMITTEE_SINGLE_SPACE]
    assert vocabulary.committee_meeting_counts[SCIENCE_COMMITTEE_SINGLE_SPACE] >= 77 + 38


def test_query_protocols_committee_filter_returns_all_science_meetings(conn):
    science_meetings = {r[0] for r in conn.execute(
        "SELECT meeting_id FROM meetings WHERE committee = ? AND is_protocol != 0",
        (SCIENCE_COMMITTEE_SINGLE_SPACE,))}
    assert len(science_meetings) >= 70 + 38
    listed_meetings: set[str] = set()
    offset = 0
    while True:
        page = _rows(tools.handle_query_protocols({
            "committees": [SCIENCE_COMMITTEE_SINGLE_SPACE], "search_in": ["topics"],
            "meeting_ids": sorted(science_meetings), "top_k": config.QUERY_PROTOCOLS_MAX_TOP_K,
            "offset": offset}))["topics"]
        if not page:
            break
        listed_meetings |= {r["meeting_id"] for r in page}
        offset += len(page)
    assert listed_meetings == science_meetings


def test_query_protocols_accepts_committee_id():
    envelope = tools.handle_query_protocols({"committees": [SCIENCE_COMMITTEE_ID], "search_in": ["topics"],
                                             "top_k": 50})
    committees_seen = {r["committee"] for r in _rows(envelope)["topics"]}
    assert committees_seen and committees_seen == {SCIENCE_COMMITTEE_SINGLE_SPACE}


def test_committee_with_trailing_space_and_long_committee_name_are_reachable(conn):
    trailing = "ועדת משנה לסיוע בדיור ודיור ציבורי לעולים"
    envelope = tools.handle_query_protocols({"committees": [trailing], "search_in": ["speeches"], "top_k": 1})
    assert _rows(envelope)["speeches"]

    longest = conn.execute("SELECT committee FROM meetings ORDER BY length(committee) DESC LIMIT 1").fetchone()[0]
    assert len(longest) > config.API_MAX_NAME_CHARS
    for limits in (PUBLIC_API_LIMITS, AGENT_LIMITS):
        validated = validated_tool_args("query_protocols", {"committees": [longest]}, limits)
        envelope = tools.handle_query_protocols({**validated, "search_in": ["speeches"], "top_k": 1})
        assert _rows(envelope)["speeches"]


def test_missing_committees_table_rows_are_still_listed(conn):
    vocabulary = filter_resolution.filter_vocabulary(conn, 25)
    assert filter_resolution.resolve_committee(
        "ועדת משנה לסיוע בדיור ודיור ציבורי לעולים", vocabulary).db_names


# ── mk_id given as a name ────────────────────────────────────────────────────

def test_mk_id_name_passes_validation():
    for limits in (PUBLIC_API_LIMITS, AGENT_LIMITS):
        assert validated_tool_args("query_protocols", {"mk_id": "יאיר לפיד"}, limits)["mk_id"] == "יאיר לפיד"
        assert validated_tool_args("query_protocols", {"mk_id": "878"}, limits)["mk_id"] == "878"


def test_mk_id_given_as_name_is_resolved():
    envelope = tools.handle_query_protocols({"mk_id": "יאיר לפיד", "search_in": ["opinions"], "top_k": 3})
    rows = _rows(envelope)["opinions"]
    assert rows and all(r["mk_id"] == YAIR_LAPID_MK_ID for r in rows)
    assert envelope.provenance["mk_id"] == YAIR_LAPID_MK_ID
    assert any("יאיר לפיד" in w for w in envelope.metadata["warnings"])


def test_mk_id_unresolvable_name_is_an_error_with_candidates():
    envelope = tools.handle_query_protocols({"mk_id": "יאיר גולן", "search_in": ["opinions"]})
    assert envelope.error == "mk_id_not_resolved"
    assert envelope.metadata["candidates"]
    assert "יאיר גולן" in envelope.summary
