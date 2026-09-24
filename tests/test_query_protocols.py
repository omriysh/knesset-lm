"""
tests/test_query_protocols.py

utils.tools.handle_query_protocols / handle_get_meeting_attendance against a
temp knesset.db seeded with real Knesset-25 rows (tests/fixtures/protocols_sample.json)
plus a few synthetic edge-case rows (see conftest.synthetic_rows).

Sample layout (X = עודד פורר, 30121, ישראל ביתנו):
  SYN 2299001  C2  2023-06-15  is_protocol=1  synthetic: ktiv words, very long opinion/speech
  M6  2205111  C2  2023-05-08  is_protocol=NULL (no summary; speeches only)
  M5  2205807  education  2023-05-31  is_protocol=0 (real speeches + synthetic topic/opinion/attendance)
  M3  2200829  C2  2023-02-08  M4 2200258  C1  2023-02-08  (same date)
  M2  2199065  C1  2023-01-09  M1 2199062  C1  2023-01-04
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
from agent.subgraph.evidence import ToolEnvelope
from retrieval import knesset_db_store as store
from tests.conftest import ROLES, SAMPLE, SYN_MEETING, X_MK, synthetic_rows
import utils.tools as tools

M1, M2, M3, M4, M5, M6 = (ROLES[k] for k in ("M1", "M2", "M3", "M4", "M5", "M6"))
SYN = SYN_MEETING
X = X_MK["mk_id"]
C1 = SAMPLE["committees"]["C1"]
C2 = SAMPLE["committees"]["C2"]
KARIV = "30807"          # attended only M3
GOTLIB = "30860"         # attended M1 and M6
LIKUD_SPEAKER = "30701"  # speaks at M1 idx 8
DATE = {m["meeting_id"]: m["date"] for m in SAMPLE["meetings"] + synthetic_rows()["meetings"]}
SCOPES = ("topics", "opinions", "speeches")


def qp(**args) -> dict:
    env = tools.handle_query_protocols(args)
    assert isinstance(env, ToolEnvelope)
    assert env.error is None, env.error
    return json.loads(env.full)


def keys(rows, idx_field="idx"):
    return [(r["meeting_id"], r[idx_field]) for r in rows]


def sp(rows):
    return keys(rows, "speech_idx")


def real_rows(table, meeting_id, verified_only=False):
    rows = [r for r in SAMPLE[table] if r["meeting_id"] == meeting_id]
    if verified_only:
        rows = [r for r in rows if r["quote_verified"] == 1]
    return sorted(rows, key=lambda r: r["idx"])


def assert_list_order(rows, idx_field):
    """date DESC, rows of one meeting contiguous, in-meeting order ascending."""
    dates = [r["date"] for r in rows]
    assert dates == sorted(dates, reverse=True)
    seen, prev = [], None
    for r in rows:
        if r["meeting_id"] != prev:
            assert r["meeting_id"] not in seen, "meeting rows not contiguous"
            seen.append(r["meeting_id"])
            prev = r["meeting_id"]
    for mid in seen:
        idxs = [r[idx_field] for r in rows if r["meeting_id"] == mid]
        assert idxs == sorted(idxs)


def bm25_oracle(db_path, table, match):
    conn = sqlite3.connect(str(db_path))
    try:
        return {(m, i): s for m, i, s in conn.execute(
            f"SELECT x.meeting_id, x.idx, bm25({table}_fts) FROM {table}_fts "
            f"JOIN {table} x ON x.id = {table}_fts.rowid WHERE {table}_fts MATCH ?", (match,))}
    finally:
        conn.close()


# ── envelope / errors ─────────────────────────────────────────────────────────

class TestEnvelope:
    def test_metadata_and_count(self, sample_db):
        env = tools.handle_query_protocols({"query": "העלייה"})
        payload = json.loads(env.full)
        assert env.metadata["kind"] == "search"
        assert env.metadata["source"] == "knesset_db"
        assert env.metadata["count"] == sum(len(v) for v in payload.values()) == 6 + 7 + 12

    def test_provenance_echoes_params(self, sample_db):
        env = tools.handle_query_protocols({"query": "העלייה", "mk_id": X, "search_in": ["opinions"]})
        assert env.provenance["query"] == "העלייה"
        assert env.provenance["mk_id"] == X
        assert list(env.provenance["search_in"]) == ["opinions"]

    def test_only_requested_scopes_present(self, sample_db):
        assert set(qp(query="העלייה", search_in=["opinions"])) == {"opinions"}
        assert set(qp(query="העלייה", search_in=["topics", "speeches"])) == {"topics", "speeches"}

    def test_default_is_all_three_scopes(self, sample_db):
        assert set(qp(query="העלייה")) == set(SCOPES)
        assert set(qp()) == set(SCOPES)

    def test_invalid_search_in(self, sample_db):
        env = tools.handle_query_protocols({"query": "העלייה", "search_in": ["topics", "bills"]})
        assert env.error == "invalid_search_in"

    def test_db_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "missing.db")
        assert tools.handle_query_protocols({"query": "העלייה"}).error == "knesset_db_missing"

    def test_sqlite_error_is_db_search_failed(self, tmp_path, monkeypatch, capsys):
        broken = tmp_path / "knesset.db"
        broken.write_bytes(b"this is not an sqlite database" * 100)
        monkeypatch.setattr(config, "KNESSET_DB", broken)
        env = tools.handle_query_protocols({"query": "העלייה"})
        assert isinstance(env, ToolEnvelope)
        assert env.error == "db_search_failed"
        assert capsys.readouterr().out.strip()

    def test_fts_metacharacters_do_not_raise(self, sample_db):
        rows = qp(query='העלייה)"*', search_in=["topics"])["topics"]
        assert set(keys(rows)) == {(M2, 2), (M1, 0), (M1, 1), (M1, 2), (M2, 4), (M2, 1)}

    def test_other_knesset_is_empty(self, sample_db):
        assert qp(query="העלייה", knesset_num=24) == {s: [] for s in SCOPES}
        assert qp(knesset_num=24) == {s: [] for s in SCOPES}


# ── row shapes / no truncation ────────────────────────────────────────────────

class TestRowShapes:
    def test_topic_row(self, sample_db):
        row = next(r for r in qp(meeting_ids=[M2], search_in=["topics"])["topics"] if r["idx"] == 4)
        assert row["meeting_id"] == M2 and row["committee"] == C1 and row["date"] == DATE[M2]
        assert row["topic"] == real_rows("topics", M2)[4]["text"]

    def test_opinion_row(self, sample_db):
        expected = next(o for o in SAMPLE["opinions"] if o["meeting_id"] == M3 and o["idx"] == 11)
        row = next(r for r in qp(meeting_ids=[M3], search_in=["opinions"])["opinions"] if r["idx"] == 11)
        assert row["meeting_id"] == M3 and row["committee"] == C2 and row["date"] == DATE[M3]
        assert row["speaker"] == expected["speaker_label"]
        for f in ("speaker_name", "mk_id", "party", "opinion", "quote", "speech_idx", "quote_offset"):
            assert row[f] == expected[f], f

    def test_speech_row(self, sample_db):
        expected = next(s for s in SAMPLE["speeches"] if s["meeting_id"] == M2 and s["idx"] == 8)
        row = next(r for r in qp(meeting_ids=[M2], search_in=["speeches"])["speeches"] if r["speech_idx"] == 8)
        assert row["meeting_id"] == M2 and row["committee"] == C1 and row["date"] == DATE[M2]
        assert row["speaker"] == expected["speaker"] and row["mk_id"] == expected["mk_id"]
        assert row["text"] == expected["text"]

    def test_long_texts_not_truncated_in_list_mode(self, sample_db):
        syn = synthetic_rows()
        res = qp(meeting_ids=[SYN], search_in=["opinions", "speeches"])
        op = next(r for r in res["opinions"] if r["idx"] == 0)
        assert op["opinion"] == syn["opinions"][0]["opinion"] and len(op["opinion"]) > 3000
        assert op["quote"] == syn["opinions"][0]["quote"]
        speech = next(r for r in res["speeches"] if r["speech_idx"] == 2)
        assert speech["text"] == syn["speeches"][2]["text"] and len(speech["text"]) > 20000

    def test_long_texts_not_truncated_in_relevance_mode(self, sample_db):
        syn = synthetic_rows()
        res = qp(query="ביטחון", search_in=["opinions", "speeches"], mk_id=X)
        assert syn["opinions"][0]["opinion"] in [r["opinion"] for r in res["opinions"]]
        assert syn["speeches"][2]["text"] in [r["text"] for r in res["speeches"]]


# ── query matching / relevance ────────────────────────────────────────────────

class TestRelevance:
    def test_each_scope_searched_independently(self, sample_db):
        res = qp(query="העלייה")
        assert set(keys(res["topics"])) == {(M2, 2), (M1, 0), (M1, 1), (M1, 2), (M2, 4), (M2, 1)}
        assert set(keys(res["opinions"])) == {(M1, 3), (M1, 2), (M2, 3), (M1, 4), (M2, 19), (M1, 7), (M1, 1)}
        assert set(sp(res["speeches"])) == {
            (M2, 1), (M1, 0), (M2, 0), (M4, 0), (M2, 4), (M1, 7), (M2, 7), (M4, 7),
            (SYN, 2), (M2, 8), (M1, 9), (M4, 3)}

    @pytest.mark.parametrize("scope", SCOPES)
    def test_ranked_by_bm25_best_first(self, sample_db, scope):
        oracle = bm25_oracle(sample_db, scope, '"העלייה"')
        rows = qp(query="העלייה", search_in=[scope])[scope]
        idx_field = "speech_idx" if scope == "speeches" else "idx"
        scores = [oracle[(r["meeting_id"], r[idx_field])] for r in rows]
        assert scores == sorted(scores)

    def test_tokens_are_anded(self, sample_db):
        res = qp(query="משרד העלייה")
        assert set(keys(res["topics"])) == {(M2, 2), (M2, 4)}
        assert set(keys(res["opinions"])) == {(M2, 3), (M2, 19)}
        assert set(sp(res["speeches"])) == {(M2, 1), (M2, 4), (M4, 3), (M2, 7), (SYN, 2), (M2, 8)}

    def test_no_match_gives_empty_lists(self, sample_db):
        assert qp(query="רופאים", search_in=["opinions", "speeches"]) == {"opinions": [], "speeches": []}

    def test_ktiv_haser_query_matches_male_spelling(self, sample_db):
        res = qp(query="בטחון")
        assert set(keys(res["topics"])) == {(SYN, 0), (SYN, 1)}
        assert set(keys(res["opinions"])) == {(SYN, 0), (SYN, 1)}
        assert set(sp(res["speeches"])) == {(SYN, 0), (SYN, 1), (SYN, 2)}

    def test_niqqud_in_query_is_ignored(self, sample_db):
        res = qp(query="בִּיטָּחוֹן", search_in=["speeches"])
        assert set(sp(res["speeches"])) == {(SYN, 0), (SYN, 1), (SYN, 2)}

    def test_relevance_rows_may_carry_score(self, sample_db):
        rows = qp(query="העלייה", search_in=["topics"])["topics"]
        if "score" in rows[0]:
            assert [r["score"] for r in rows] == sorted(r["score"] for r in rows)

    def test_sort_date_with_query(self, sample_db):
        rows = qp(query="העלייה", search_in=["speeches"], sort="date")["speeches"]
        assert sp(rows) == [(SYN, 2), (M4, 0), (M4, 3), (M4, 7), (M2, 0), (M2, 1), (M2, 4), (M2, 7), (M2, 8),
                            (M1, 0), (M1, 7), (M1, 9)]


# ── list mode ─────────────────────────────────────────────────────────────────

class TestListMode:
    def test_topics_newest_first_in_meeting_order(self, sample_db):
        rows = qp(search_in=["topics"])["topics"]
        assert len(rows) == 3 + 8 + 8 + 8 + 2
        assert keys(rows)[:2] == [(SYN, 0), (SYN, 1)]
        assert_list_order(rows, "idx")
        assert keys(rows)[-3:] == [(M1, 0), (M1, 1), (M1, 2)]

    def test_opinions_list_verified_only(self, sample_db):
        rows = qp(search_in=["opinions"])["opinions"]
        assert_list_order(rows, "idx")
        expected = set()
        for mid in (M1, M2, M3, M4):
            expected |= {(mid, o["idx"]) for o in real_rows("opinions", mid, verified_only=True)}
        expected |= {(SYN, 0), (SYN, 1)}
        assert set(keys(rows)) == expected
        assert len(rows) == 8 + 9 + 6 + 0 + 2

    def test_speeches_list(self, sample_db):
        rows = qp(search_in=["speeches"])["speeches"]
        assert_list_order(rows, "speech_idx")
        assert [r["meeting_id"] for r in rows][:4] == [SYN, SYN, SYN, M6]
        assert len(rows) == 6 * 5 + 3

    def test_relevance_sort_with_empty_query_is_date(self, sample_db):
        assert qp(search_in=["speeches"], sort="relevance") == qp(search_in=["speeches"], sort="date")
        assert qp(query="", search_in=["topics"]) == qp(search_in=["topics"])

    def test_meeting_summary_via_meeting_ids(self, sample_db):
        res = qp(meeting_ids=[M2], search_in=["topics", "opinions"])
        assert [r["topic"] for r in res["topics"]] == [t["text"] for t in real_rows("topics", M2)]
        assert [r["idx"] for r in res["opinions"]] == [o["idx"] for o in real_rows("opinions", M2, True)]

    def test_transcript_reading_in_order(self, sample_db):
        rows = qp(meeting_ids=[M2], search_in=["speeches"])["speeches"]
        expected = real_rows("speeches", M2)
        assert [r["speech_idx"] for r in rows] == [s["idx"] for s in expected]
        assert [r["text"] for r in rows] == [s["text"] for s in expected]

    def test_offset_pages_through_transcript(self, sample_db):
        all_idx = [s["idx"] for s in real_rows("speeches", M2)]
        pages = [[r["speech_idx"] for r in qp(meeting_ids=[M2], search_in=["speeches"], top_k=2, offset=o)["speeches"]]
                 for o in (0, 2, 4, 6)]
        assert pages == [all_idx[0:2], all_idx[2:4], all_idx[4:6], []]

    def test_offset_is_per_scope(self, sample_db):
        res = qp(meeting_ids=[M2], search_in=["topics", "speeches"], top_k=3, offset=3)
        assert [r["idx"] for r in res["topics"]] == [3, 4, 5]
        assert [r["speech_idx"] for r in res["speeches"]] == [s["idx"] for s in real_rows("speeches", M2)][3:6]

    def test_offset_with_query(self, sample_db):
        full = sp(qp(query="העלייה", search_in=["speeches"], sort="date")["speeches"])
        page = sp(qp(query="העלייה", search_in=["speeches"], sort="date", top_k=4, offset=4)["speeches"])
        assert page == full[4:8]


# ── filters ───────────────────────────────────────────────────────────────────

class TestFilters:
    def test_mk_id_topics_means_attendance(self, sample_db):
        assert {r["meeting_id"] for r in qp(mk_id=KARIV, search_in=["topics"])["topics"]} == {M3}
        assert {r["meeting_id"] for r in qp(mk_id=GOTLIB, search_in=["topics"])["topics"]} == {M1}
        assert {r["meeting_id"] for r in qp(mk_id=X, search_in=["topics"])["topics"]} == {M1, M2, M3, M4, SYN}

    def test_mk_id_opinions_means_opinion_author(self, sample_db):
        rows = qp(mk_id=X, search_in=["opinions"])["opinions"]
        assert keys(rows) == [(SYN, 0), (M3, 11), (M2, 0), (M2, 19), (M1, 1)]
        assert all(r["mk_id"] == X for r in rows)

    def test_mk_id_speeches_means_speaker(self, sample_db):
        rows = qp(mk_id=X, search_in=["speeches"])["speeches"]
        assert sp(rows) == [(SYN, 0), (SYN, 2), (M4, 7), (M2, 8), (M1, 9)]

    def test_mk_id_with_query(self, sample_db):
        res = qp(query="העלייה", mk_id=X)
        assert set(keys(res["opinions"])) == {(M2, 19), (M1, 1)}
        assert set(sp(res["speeches"])) == {(SYN, 2), (M2, 8), (M1, 9), (M4, 7)}
        assert set(keys(res["topics"])) == {(M2, 2), (M1, 0), (M1, 1), (M1, 2), (M2, 4), (M2, 1)}
        assert set(sp(qp(query="העלייה", mk_id=KARIV, search_in=["speeches"])["speeches"])) == set()
        assert qp(query="העלייה", mk_id=KARIV, search_in=["topics"])["topics"] == []

    def test_party_topics_means_attendee_party(self, sample_db):
        assert {r["meeting_id"] for r in qp(party="העבודה", search_in=["topics"])["topics"]} == {M3}

    def test_party_opinions(self, sample_db):
        rows = qp(party="העבודה", search_in=["opinions"])["opinions"]
        assert set(keys(rows)) == {(M3, 2), (M3, 4), (M3, 6)}

    def test_party_speeches_uses_speaker_roster_party(self, sample_db):
        assert sp(qp(party="הליכוד", search_in=["speeches"])["speeches"]) == [(M1, 8)]
        assert sp(qp(party="ישראל ביתנו", search_in=["speeches"])["speeches"]) == \
            sp(qp(mk_id=X, search_in=["speeches"])["speeches"])

    def test_committees_with_underscores(self, sample_db):
        underscored = C1.replace(" ", "_")
        rows = qp(committees=[underscored], search_in=["topics"])["topics"]
        assert {r["meeting_id"] for r in rows} == {M1, M2, M4}
        assert rows == qp(committees=[C1], search_in=["topics"])["topics"]

    def test_committees_ored(self, sample_db):
        rows = qp(committees=[C1, C2], search_in=["speeches"])["speeches"]
        assert {r["meeting_id"] for r in rows} == {M1, M2, M3, M4, M6, SYN}
        assert {r["meeting_id"] for r in qp(committees=[C2], search_in=["speeches"])["speeches"]} == {M3, M6, SYN}

    def test_meeting_ids_ored(self, sample_db):
        res = qp(meeting_ids=[M1, M3])
        for scope in SCOPES:
            assert {r["meeting_id"] for r in res[scope]} == {M1, M3}

    def test_date_range_inclusive(self, sample_db):
        res = qp(date_from=DATE[M2], date_to=DATE[M3])
        assert {r["meeting_id"] for r in res["topics"]} == {M2, M3, M4}
        assert {r["meeting_id"] for r in res["speeches"]} == {M2, M3, M4}
        assert {r["meeting_id"] for r in qp(date_from="2023-05-08", search_in=["speeches"])["speeches"]} == {M6, SYN}
        assert {r["meeting_id"] for r in qp(date_to="2023-01-04", search_in=["topics"])["topics"]} == {M1}

    def test_filters_are_anded(self, sample_db):
        rows = qp(mk_id=X, committees=[C2], search_in=["opinions"])["opinions"]
        assert keys(rows) == [(SYN, 0), (M3, 11)]
        assert qp(mk_id=KARIV, committees=[C1], search_in=["topics"])["topics"] == []
        rows = qp(query="הוועדה", committees=[C1], search_in=["opinions", "topics"])
        assert {r["meeting_id"] for r in rows["opinions"]} == {M1}
        assert keys(rows["topics"]) == [(M4, 0)]

    def test_unknown_mk_is_empty_not_error(self, sample_db):
        assert qp(mk_id="99999999") == {s: [] for s in SCOPES}


# ── exclusions ────────────────────────────────────────────────────────────────

class TestExclusions:
    def test_unverified_opinions_never_returned(self, sample_db):
        unverified = {(o["meeting_id"], o["idx"]) for o in SAMPLE["opinions"] if o["quote_verified"] == 0}
        assert unverified
        for args in ({}, {"query": "העלייה"}, {"meeting_ids": [M4]}, {"mk_id": X}, {"meeting_ids": [M2, M3, M4]}):
            rows = qp(search_in=["opinions"], **args)["opinions"]
            assert not set(keys(rows)) & unverified
        assert qp(meeting_ids=[M4], search_in=["opinions"])["opinions"] == []

    def test_not_protocol_meeting_excluded_everywhere(self, sample_db):
        for args in ({}, {"query": "ביטחון"}, {"query": "פרוטוקול"}, {"meeting_ids": [M5]}, {"mk_id": X}):
            res = qp(**args)
            for scope in SCOPES:
                assert M5 not in {r["meeting_id"] for r in res[scope]}, (args, scope)

    def test_meeting_without_summary_is_included(self, sample_db):
        rows = qp(meeting_ids=[M6], search_in=["speeches"])["speeches"]
        assert [r["speech_idx"] for r in rows] == [s["idx"] for s in real_rows("speeches", M6)]
        assert M6 in {r["meeting_id"] for r in qp(query="פרוטוקול", search_in=["speeches"])["speeches"]}


# ── top_k ─────────────────────────────────────────────────────────────────────

def _add_speeches(db_path, meeting_id, start, count):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO speeches(meeting_id, knesset_num, idx, speaker, mk_id, text) VALUES (?,?,?,?,?,?)",
            [(meeting_id, 25, start + i, "איל קופמן", None, f"ביטחון הציבור, סעיף {i}") for i in range(count)])
        conn.execute("INSERT INTO speeches_fts(speeches_fts) VALUES('rebuild')")
        conn.commit()
    finally:
        conn.close()


class TestTopK:
    def test_default_is_50_per_scope(self, sample_db):
        _add_speeches(sample_db, SYN, 3, 80)
        res = qp(meeting_ids=[SYN], search_in=["speeches"])
        assert len(res["speeches"]) == 50
        assert [r["speech_idx"] for r in res["speeches"]] == list(range(50))
        assert len(qp(query="ביטחון", search_in=["speeches"])["speeches"]) == 50

    def test_top_k_is_per_scope(self, sample_db):
        res = qp(search_in=["topics", "opinions", "speeches"], top_k=2)
        assert [len(res[s]) for s in SCOPES] == [2, 2, 2]

    def test_clamped_to_max(self, sample_db):
        _add_speeches(sample_db, SYN, 3, config.QUERY_PROTOCOLS_MAX_TOP_K + 20)
        rows = qp(meeting_ids=[SYN], search_in=["speeches"], top_k=config.QUERY_PROTOCOLS_MAX_TOP_K * 10)["speeches"]
        assert len(rows) == config.QUERY_PROTOCOLS_MAX_TOP_K

    def test_clamped_to_one(self, sample_db):
        assert len(qp(search_in=["topics"], top_k=-5)["topics"]) == 1


# ── get_meeting_attendance ────────────────────────────────────────────────────

def _attendance(env):
    assert env.error is None, env.error
    return json.loads(env.full)


class TestGetMeetingAttendance:
    def test_header_and_rows(self, sample_db):
        full = _attendance(tools.handle_get_meeting_attendance({"meeting_id": M1}))
        assert full["meeting_id"] == M1 and full["committee"] == C1 and full["date"] == DATE[M1]
        rows = [a for a in SAMPLE["attendance"] if a["meeting_id"] == M1]
        expected = sorted(rows, key=lambda a: (a["mk_id"] is None, a["party"] or "", a["name"]))
        assert full["attendance"] == [{"name": a["name"], "mk_id": a["mk_id"], "party": a["party"]} for a in expected]

    def test_mks_first_guests_null(self, sample_db):
        rows = _attendance(tools.handle_get_meeting_attendance({"meeting_id": M3}))["attendance"]
        flags = [r["mk_id"] is None for r in rows]
        assert flags == sorted(flags) and any(flags) and not all(flags)
        assert all(r["party"] is None for r in rows if r["mk_id"] is None)
        assert X in {r["mk_id"] for r in rows}

    def test_meeting_without_summary(self, sample_db):
        rows = _attendance(tools.handle_get_meeting_attendance({"meeting_id": M6}))["attendance"]
        assert len(rows) == len([a for a in SAMPLE["attendance"] if a["meeting_id"] == M6])

    def test_meeting_id_as_int(self, sample_db):
        assert _attendance(tools.handle_get_meeting_attendance({"meeting_id": int(M1)}))["meeting_id"] == M1

    def test_errors(self, sample_db):
        assert tools.handle_get_meeting_attendance({}).error == "missing_meeting_id"
        assert tools.handle_get_meeting_attendance({"meeting_id": "  "}).error == "missing_meeting_id"
        assert tools.handle_get_meeting_attendance({"meeting_id": "999999999"}).error == "meeting_not_found"

    def test_db_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "missing.db")
        assert tools.handle_get_meeting_attendance({"meeting_id": M1}).error == "knesset_db_missing"
