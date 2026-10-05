"""
tests/test_query_protocols.py

utils.tools.handle_query_protocols / handle_get_meeting_attendance on the real Data/knesset.db.
Expected rows come from SQL oracles over the same db (conftest.real_conn), and the meetings,
committees and MK come from conftest.RealDbFacts, so nothing depends on a data snapshot.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
from agent.subgraph.evidence import ToolEnvelope
import utils.tools as tools

SCOPES = ("topics", "opinions", "speeches")
NONSENSE_WORD = "קשקשתאינהקיימתבשוםפרוטוקול"
UNUSED_KNESSET = 1


def qp(**args) -> dict:
    env = tools.handle_query_protocols(args)
    assert isinstance(env, ToolEnvelope)
    assert env.error is None, env.error
    return json.loads(env.full)


def keys(rows, idx_field="idx"):
    return [(r["meeting_id"], r[idx_field]) for r in rows]


def speech_keys(rows):
    return keys(rows, "speech_idx")


def assert_list_order(rows, idx_field):
    """date DESC, rows of one meeting contiguous, in-meeting order ascending."""
    dates = [r["date"] for r in rows]
    assert dates == sorted(dates, reverse=True)
    seen, previous = [], None
    for r in rows:
        if r["meeting_id"] != previous:
            assert r["meeting_id"] not in seen, "meeting rows not contiguous"
            seen.append(r["meeting_id"])
            previous = r["meeting_id"]
    for meeting_id in seen:
        idxs = [r[idx_field] for r in rows if r["meeting_id"] == meeting_id]
        assert idxs == sorted(idxs)


def column(conn, sql, *params) -> list:
    return [r[0] for r in conn.execute(sql, params)]


def meeting_ids_of(rows) -> set[str]:
    return {r["meeting_id"] for r in rows}


def speech_position(conn, meeting_id: str, speech_idx: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM speeches WHERE meeting_id = ? AND idx < ?",
                        (meeting_id, speech_idx)).fetchone()[0]


# ── envelope / errors ─────────────────────────────────────────────────────────

class TestEnvelope:
    def test_metadata_and_count(self, real_db):
        env = tools.handle_query_protocols({"query": real_db.topic_word})
        payload = json.loads(env.full)
        assert env.metadata["kind"] == "search" and env.metadata["source"] == "knesset_db"
        assert env.metadata["count"] == sum(len(v) for v in payload.values()) > 0

    def test_provenance_echoes_params(self, real_db):
        env = tools.handle_query_protocols({"query": real_db.topic_word, "mk_id": real_db.mk_id,
                                            "search_in": ["opinions"]})
        assert env.provenance["query"] == real_db.topic_word
        assert env.provenance["mk_id"] == real_db.mk_id
        assert list(env.provenance["search_in"]) == ["opinions"]

    def test_only_requested_scopes_present(self, real_db):
        assert set(qp(query=real_db.topic_word, search_in=["opinions"])) == {"opinions"}
        assert set(qp(query=real_db.topic_word, search_in=["topics", "speeches"])) == {"topics", "speeches"}

    def test_default_is_all_three_scopes(self, real_db):
        assert set(qp(query=real_db.topic_word)) == set(SCOPES)
        assert set(qp(meeting_ids=[real_db.meeting_id])) == set(SCOPES)

    def test_invalid_search_in(self, real_db):
        env = tools.handle_query_protocols({"query": real_db.topic_word, "search_in": ["topics", "bills"]})
        assert env.error == "invalid_search_in"

    def test_db_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "missing.db")
        assert tools.handle_query_protocols({"query": "תקציב"}).error == "knesset_db_missing"

    def test_sqlite_error_is_db_search_failed(self, tmp_path, monkeypatch, capsys):
        broken = tmp_path / "knesset.db"
        broken.write_bytes(b"this is not an sqlite database" * 100)
        monkeypatch.setattr(config, "KNESSET_DB", broken)
        env = tools.handle_query_protocols({"query": "תקציב"})
        assert env.error == "db_search_failed"
        assert capsys.readouterr().out.strip()

    def test_fts_metacharacters_are_literal(self, real_db):
        plain = qp(query=real_db.topic_word, search_in=["topics"])["topics"]
        decorated = qp(query=f'{real_db.topic_word})"*', search_in=["topics"])["topics"]
        assert plain and keys(decorated) == keys(plain)

    def test_knesset_without_data_is_empty(self, real_db):
        assert qp(query=real_db.topic_word, knesset_num=UNUSED_KNESSET) == {s: [] for s in SCOPES}
        assert qp(knesset_num=UNUSED_KNESSET) == {s: [] for s in SCOPES}


# ── row shapes / no truncation ────────────────────────────────────────────────

class TestRowShapes:
    def test_topic_row(self, real_db, real_conn):
        expected = real_conn.execute("SELECT idx, text FROM topics WHERE meeting_id = ? ORDER BY idx DESC",
                                     (real_db.meeting_id,)).fetchone()
        rows = qp(meeting_ids=[real_db.meeting_id], search_in=["topics"], top_k=config.QUERY_PROTOCOLS_MAX_TOP_K)
        row = next(r for r in rows["topics"] if r["idx"] == expected["idx"])
        assert (row["committee"], row["date"]) == (real_db.meeting_committee, real_db.meeting_date)
        assert row["topic"] == expected["text"]

    def test_opinion_row(self, real_db, real_conn):
        expected = real_conn.execute(
            "SELECT * FROM opinions WHERE meeting_id = ? AND quote_verified = 1 AND mk_id IS NOT NULL "
            "ORDER BY idx", (real_db.meeting_id,)).fetchone()
        row = next(r for r in qp(meeting_ids=[real_db.meeting_id], search_in=["opinions"])["opinions"]
                   if r["idx"] == expected["idx"])
        assert row["speaker"] == expected["speaker_label"]
        for name in ("speaker_name", "mk_id", "party", "opinion", "quote", "speech_idx", "quote_offset"):
            assert row[name] == expected[name], name

    def test_speech_row(self, real_db, real_conn):
        expected = real_conn.execute("SELECT * FROM speeches WHERE meeting_id = ? AND mk_id IS NOT NULL ORDER BY idx",
                                     (real_db.meeting_id,)).fetchone()
        rows = qp(meeting_ids=[real_db.meeting_id], search_in=["speeches"], top_k=1,
                  offset=speech_position(real_conn, real_db.meeting_id, expected["idx"]))["speeches"]
        assert rows[0]["speech_idx"] == expected["idx"]
        assert (rows[0]["speaker"], rows[0]["mk_id"], rows[0]["text"]) == (
            expected["speaker"], expected["mk_id"], expected["text"])

    def test_longest_texts_are_not_truncated(self, real_db, real_conn):
        longest_speech = real_conn.execute(
            "SELECT idx, text FROM speeches WHERE meeting_id = ? ORDER BY length(text) DESC",
            (real_db.meeting_id,)).fetchone()
        rows = qp(meeting_ids=[real_db.meeting_id], search_in=["speeches"], top_k=1,
                  offset=speech_position(real_conn, real_db.meeting_id, longest_speech["idx"]))["speeches"]
        assert rows[0]["text"] == longest_speech["text"]
        longest_opinion = real_conn.execute(
            "SELECT opinion FROM (SELECT opinion FROM opinions WHERE quote_verified = 1 AND meeting_id = ? "
            "ORDER BY idx LIMIT ?) ORDER BY length(opinion) DESC",
            (real_db.meeting_id, config.QUERY_PROTOCOLS_MAX_TOP_K)).fetchone()
        rows = qp(meeting_ids=[real_db.meeting_id], search_in=["opinions"], top_k=config.QUERY_PROTOCOLS_MAX_TOP_K)
        assert longest_opinion["opinion"] in [r["opinion"] for r in rows["opinions"]]


# ── query matching / relevance ────────────────────────────────────────────────

class TestRelevance:
    @pytest.mark.parametrize("scope", SCOPES)
    def test_ranked_exact_words_first_then_bm25(self, real_db, real_conn, scope):
        rows = qp(query=real_db.topic_word, search_in=[scope], top_k=20)[scope]
        assert rows
        idx_field = "speech_idx" if scope == "speeches" else "idx"
        match = tools._fts_match(real_db.topic_word, f"{scope}_fts")
        exact_match = tools._fts_exact_match(real_db.topic_word)
        meeting_ids = sorted(meeting_ids_of(rows))
        oracle = {(r[0], r[1]): (r[2], r[3]) for r in real_conn.execute(
            f"SELECT x.meeting_id, x.idx, x.id NOT IN (SELECT rowid FROM {scope}_fts WHERE {scope}_fts MATCH ?), "
            f"bm25({scope}_fts) FROM {scope}_fts JOIN {scope} x ON x.id = {scope}_fts.rowid "
            f"WHERE {scope}_fts MATCH ? AND x.meeting_id IN ({','.join('?' * len(meeting_ids))})",
            (exact_match, match, *meeting_ids))}
        tier_and_scores = [oracle[(r["meeting_id"], r[idx_field])] for r in rows]
        assert tier_and_scores == sorted(tier_and_scores)

    def test_tokens_are_anded(self, real_db, real_conn):
        first_word, second_word = "תקציב", "החינוך"
        both = set(keys(qp(query=f"{first_word} {second_word}", search_in=["topics"], top_k=50)["topics"]))
        assert both
        for word in (first_word, second_word):
            match = tools._fts_match(word, "topics_fts")
            with_word = {(r[0], r[1]) for r in real_conn.execute(
                "SELECT x.meeting_id, x.idx FROM topics_fts JOIN topics x ON x.id = topics_fts.rowid "
                "WHERE topics_fts MATCH ?", (match,))}
            assert both <= with_word, word

    def test_no_match_gives_empty_lists(self, real_db):
        assert qp(query=NONSENSE_WORD, search_in=["opinions", "speeches"]) == {"opinions": [], "speeches": []}

    def test_ktiv_haser_query_finds_male_spelling(self, real_db):
        rows = qp(query="בטחון", search_in=["topics"], top_k=50)["topics"]
        assert any("ביטחון" in r["topic"] for r in rows)

    def test_niqqud_in_query_is_ignored(self, real_db):
        assert qp(query="בִּיטָּחוֹן", search_in=["speeches"], top_k=10) == qp(query="ביטחון", search_in=["speeches"],
                                                                             top_k=10)

    def test_sort_date_with_query(self, real_db):
        rows = qp(query=real_db.topic_word, search_in=["speeches"], sort="date", top_k=50)["speeches"]
        assert rows
        assert_list_order(rows, "speech_idx")

    def test_offset_with_query(self, real_db):
        full = speech_keys(qp(query=real_db.topic_word, search_in=["speeches"], sort="date", top_k=12)["speeches"])
        page = speech_keys(qp(query=real_db.topic_word, search_in=["speeches"], sort="date", top_k=4,
                              offset=4)["speeches"])
        assert page == full[4:8]

    def test_results_never_from_non_protocol_meetings(self, real_db, real_conn):
        rows = qp(query="תקציב", top_k=config.QUERY_PROTOCOLS_MAX_TOP_K)
        meeting_ids = sorted(set().union(*(meeting_ids_of(r) for r in rows.values())))
        assert meeting_ids
        flags = column(real_conn, f"SELECT is_protocol FROM meetings WHERE meeting_id IN "
                                  f"({','.join('?' * len(meeting_ids))})", *meeting_ids)
        assert 0 not in flags


# ── list mode ─────────────────────────────────────────────────────────────────

class TestListMode:
    @pytest.mark.parametrize("scope, idx_field", [("topics", "idx"), ("opinions", "idx"), ("speeches", "speech_idx")])
    def test_newest_first_in_meeting_order(self, real_db, real_conn, scope, idx_field):
        rows = qp(search_in=[scope], top_k=config.QUERY_PROTOCOLS_MAX_TOP_K)[scope]
        assert len(rows) == config.QUERY_PROTOCOLS_MAX_TOP_K
        assert_list_order(rows, idx_field)
        newest = real_conn.execute(
            f"SELECT MAX(m.date) FROM meetings m WHERE (m.is_protocol IS NULL OR m.is_protocol != 0) AND m.knesset_num = 25 "
            f"AND EXISTS (SELECT 1 FROM {scope} x WHERE x.meeting_id = m.meeting_id)").fetchone()[0]
        if scope != "opinions":
            assert rows[0]["date"] == newest

    def test_listed_opinions_are_verified(self, real_db, real_conn):
        rows = qp(search_in=["opinions"], top_k=config.QUERY_PROTOCOLS_MAX_TOP_K)["opinions"]
        verified = {(r[0], r[1]): r[2] for r in real_conn.execute(
            f"SELECT meeting_id, idx, quote_verified FROM opinions WHERE meeting_id IN "
            f"({','.join('?' * len(meeting_ids_of(rows)))})", tuple(meeting_ids_of(rows)))}
        assert all(verified[key] == 1 for key in keys(rows))

    def test_relevance_sort_with_empty_query_is_date(self, real_db):
        assert qp(search_in=["speeches"], sort="relevance") == qp(search_in=["speeches"], sort="date")
        assert qp(query="", search_in=["topics"]) == qp(search_in=["topics"])

    def test_meeting_summary_via_meeting_ids(self, real_db, real_conn):
        res = qp(meeting_ids=[real_db.meeting_id], search_in=["topics", "opinions"],
                 top_k=config.QUERY_PROTOCOLS_MAX_TOP_K)
        assert [r["topic"] for r in res["topics"]] == column(
            real_conn, "SELECT text FROM topics WHERE meeting_id = ? ORDER BY idx", real_db.meeting_id)
        assert [r["idx"] for r in res["opinions"]] == column(
            real_conn, "SELECT idx FROM opinions WHERE meeting_id = ? AND quote_verified = 1 ORDER BY idx",
            real_db.meeting_id)[:config.QUERY_PROTOCOLS_MAX_TOP_K]

    def test_transcript_pages_in_order(self, real_db, real_conn):
        all_idx = column(real_conn, "SELECT idx FROM speeches WHERE meeting_id = ? ORDER BY idx", real_db.meeting_id)
        pages = [[r["speech_idx"] for r in qp(meeting_ids=[real_db.meeting_id], search_in=["speeches"],
                                              top_k=5, offset=o)["speeches"]] for o in (0, 5, 10)]
        assert pages == [all_idx[0:5], all_idx[5:10], all_idx[10:15]]
        past_end = qp(meeting_ids=[real_db.meeting_id], search_in=["speeches"], offset=len(all_idx))["speeches"]
        assert past_end == []

    def test_offset_is_per_scope(self, real_db, real_conn):
        res = qp(meeting_ids=[real_db.meeting_id], search_in=["topics", "speeches"], top_k=2, offset=2)
        topic_idx = column(real_conn, "SELECT idx FROM topics WHERE meeting_id = ? ORDER BY idx", real_db.meeting_id)
        speech_idx = column(real_conn, "SELECT idx FROM speeches WHERE meeting_id = ? ORDER BY idx", real_db.meeting_id)
        assert [r["idx"] for r in res["topics"]] == topic_idx[2:4]
        assert [r["speech_idx"] for r in res["speeches"]] == speech_idx[2:4]


# ── filters ───────────────────────────────────────────────────────────────────

class TestFilters:
    def test_mk_id_topics_means_attendance(self, real_db, real_conn):
        rows = qp(mk_id=real_db.mk_id, search_in=["topics"], top_k=config.QUERY_PROTOCOLS_MAX_TOP_K)["topics"]
        attended = set(column(real_conn, "SELECT meeting_id FROM attendance WHERE mk_id = ?", real_db.mk_id))
        assert rows and meeting_ids_of(rows) <= attended

    def test_mk_id_opinions_means_opinion_author(self, real_db):
        rows = qp(mk_id=real_db.mk_id, search_in=["opinions"], top_k=30)["opinions"]
        assert rows and all(r["mk_id"] == real_db.mk_id for r in rows)
        assert_list_order(rows, "idx")

    def test_mk_id_speeches_means_speaker(self, real_db):
        rows = qp(mk_id=real_db.mk_id, search_in=["speeches"], top_k=30)["speeches"]
        assert rows and all(r["mk_id"] == real_db.mk_id for r in rows)

    def test_mk_id_numeric_equals_string(self, real_db):
        assert qp(mk_id=int(real_db.mk_id), search_in=["opinions"]) == qp(mk_id=real_db.mk_id, search_in=["opinions"])

    def test_party_topics_means_attendee_party(self, real_db, real_conn):
        rows = qp(party=real_db.mk_party, search_in=["topics"], top_k=30)["topics"]
        with_party = set(column(real_conn, "SELECT DISTINCT meeting_id FROM attendance WHERE party = ?",
                                real_db.mk_party))
        assert rows and meeting_ids_of(rows) <= with_party

    def test_party_opinions_and_speeches(self, real_db, real_conn):
        opinions = qp(party=real_db.mk_party, search_in=["opinions"], top_k=30)["opinions"]
        assert opinions and all(r["party"] == real_db.mk_party for r in opinions)
        party_members = set(column(real_conn, "SELECT mk_id FROM mks WHERE party = ?", real_db.mk_party))
        speeches = qp(party=real_db.mk_party, search_in=["speeches"], top_k=30)["speeches"]
        assert speeches and {r["mk_id"] for r in speeches} <= party_members

    def test_committees_with_underscores(self, real_db):
        underscored = real_db.meeting_committee.replace(" ", "_")
        rows = qp(committees=[underscored], search_in=["topics"])["topics"]
        assert rows and {r["committee"] for r in rows} == {real_db.meeting_committee}
        assert rows == qp(committees=[real_db.meeting_committee], search_in=["topics"])["topics"]

    def test_committees_ored(self, real_db):
        both = [real_db.meeting_committee, real_db.other_committee]
        rows = qp(committees=both, search_in=["speeches"], top_k=config.QUERY_PROTOCOLS_MAX_TOP_K)["speeches"]
        assert rows and {r["committee"] for r in rows} <= set(both)
        for committee in both:
            assert qp(committees=[committee], meeting_ids=[real_db.meeting_id, real_db.other_meeting_id],
                      search_in=["topics"])["topics"]

    def test_meeting_ids_ored(self, real_db):
        res = qp(meeting_ids=[real_db.meeting_id, real_db.other_meeting_id], top_k=config.QUERY_PROTOCOLS_MAX_TOP_K)
        for scope in ("topics", "opinions"):
            assert meeting_ids_of(res[scope]) == {real_db.meeting_id, real_db.other_meeting_id}, scope

    def test_date_range_inclusive(self, real_db):
        rows = qp(date_from=real_db.meeting_date, date_to=real_db.meeting_date, search_in=["topics"],
                  top_k=config.QUERY_PROTOCOLS_MAX_TOP_K)["topics"]
        assert rows and {r["date"] for r in rows} == {real_db.meeting_date}
        later = qp(date_from=real_db.meeting_date, search_in=["topics"])["topics"]
        earlier = qp(date_to=real_db.meeting_date, search_in=["topics"])["topics"]
        assert all(r["date"] >= real_db.meeting_date for r in later)
        assert all(r["date"] <= real_db.meeting_date for r in earlier)

    def test_filters_are_anded(self, real_db):
        rows = qp(mk_id=real_db.mk_id, committees=[real_db.meeting_committee], search_in=["opinions"])["opinions"]
        assert all(r["mk_id"] == real_db.mk_id and r["committee"] == real_db.meeting_committee for r in rows)
        assert qp(meeting_ids=[real_db.meeting_id], committees=[real_db.other_committee]) == {s: [] for s in SCOPES}

    def test_unknown_mk_is_empty_not_error(self, real_db):
        assert qp(mk_id="99999999") == {s: [] for s in SCOPES}


# ── exclusions ────────────────────────────────────────────────────────────────

class TestExclusions:
    def test_unverified_opinions_never_returned(self, real_db, real_conn):
        meeting_id = real_conn.execute(
            "SELECT meeting_id FROM opinions WHERE quote_verified = 0 AND knesset_num = 25 ORDER BY meeting_id").fetchone()[0]
        unverified = set(column(real_conn, "SELECT idx FROM opinions WHERE meeting_id = ? AND quote_verified = 0",
                                meeting_id))
        rows = qp(meeting_ids=[meeting_id], search_in=["opinions"], top_k=config.QUERY_PROTOCOLS_MAX_TOP_K)["opinions"]
        assert not {r["idx"] for r in rows} & unverified

    def test_not_protocol_meeting_excluded(self, real_db):
        assert qp(meeting_ids=[real_db.non_protocol_meeting_id]) == {s: [] for s in SCOPES}


# ── top_k ─────────────────────────────────────────────────────────────────────

class TestTopK:
    def test_default_per_scope(self, real_db):
        rows = qp(meeting_ids=[real_db.meeting_id], search_in=["speeches"])["speeches"]
        assert len(rows) == config.QUERY_PROTOCOLS_DEFAULT_TOP_K

    def test_top_k_is_per_scope(self, real_db):
        res = qp(search_in=list(SCOPES), top_k=2)
        assert [len(res[s]) for s in SCOPES] == [2, 2, 2]

    def test_clamped_to_max(self, real_db):
        rows = qp(search_in=["speeches"], top_k=config.QUERY_PROTOCOLS_MAX_TOP_K * 10)["speeches"]
        assert len(rows) == config.QUERY_PROTOCOLS_MAX_TOP_K

    def test_clamped_to_one(self, real_db):
        assert len(qp(search_in=["topics"], top_k=-5)["topics"]) == 1


# ── get_meeting_attendance ────────────────────────────────────────────────────

def attendance(env):
    assert env.error is None, env.error
    return json.loads(env.full)


class TestGetMeetingAttendance:
    def test_header_and_rows_equal_the_db(self, real_db, real_conn):
        full = attendance(tools.handle_get_meeting_attendance({"meeting_id": real_db.meeting_id}))
        assert (full["meeting_id"], full["committee"], full["date"]) == (
            real_db.meeting_id, real_db.meeting_committee, real_db.meeting_date)
        expected = [dict(r) for r in real_conn.execute(
            "SELECT name, mk_id, party FROM attendance WHERE meeting_id = ?", (real_db.meeting_id,))]
        assert sorted(full["attendance"], key=json.dumps) == sorted(expected, key=json.dumps)

    def test_mks_first_guests_have_no_party(self, real_db):
        rows = attendance(tools.handle_get_meeting_attendance({"meeting_id": real_db.meeting_id}))["attendance"]
        guest_flags = [r["mk_id"] is None for r in rows]
        assert guest_flags == sorted(guest_flags) and any(guest_flags) and not all(guest_flags)

    def test_meeting_id_as_int(self, real_db):
        env = tools.handle_get_meeting_attendance({"meeting_id": int(real_db.meeting_id)})
        assert attendance(env)["meeting_id"] == real_db.meeting_id

    def test_errors(self, real_db):
        assert tools.handle_get_meeting_attendance({}).error == "missing_meeting_id"
        assert tools.handle_get_meeting_attendance({"meeting_id": "  "}).error == "missing_meeting_id"
        assert tools.handle_get_meeting_attendance({"meeting_id": "999999999"}).error == "meeting_not_found"

    def test_db_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "missing.db")
        assert tools.handle_get_meeting_attendance({"meeting_id": "1"}).error == "knesset_db_missing"
