"""
tests/test_meeting_index.py

Tests for src/retrieval/meeting_index.py (structural committee/date/participant
prefilter used by web/app.py::browse_rag) and the speaker -> mk_id resolution
logic used by scripts/build_meeting_index.py.

No live network / no real Data/ dependency: query tests use a small hand-built
fixture db; speaker-resolution tests stub the fuzzy MK index against a small
fixed MK list rather than depending on get_all_mks() or the real mks.db.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
from retrieval.bm25_index import BM25Index
from retrieval.meeting_index import (
    create_tables,
    db_path,
    insert_guests,
    insert_meetings,
    insert_participants,
    query_candidate_meeting_ids,
)
from utils.meeting import get_meeting_speakers
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex


# ── query_candidate_meeting_ids: hand-built fixture db ────────────────────────

@pytest.fixture()
def fixture_index(tmp_path, monkeypatch):
    """Build a small 4-meeting meeting_index.db and point config.BM25_DIR at it."""
    monkeypatch.setattr(config, "BM25_DIR", tmp_path)

    path = db_path(25)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    create_tables(conn)
    insert_meetings(conn, [
        {"meeting_id": "m1", "committee": "ועדת החוץ והביטחון", "date": "2025-12-01", "knesset_num": 25},
        {"meeting_id": "m2", "committee": "ועדת החוץ והביטחון", "date": "2025-06-01", "knesset_num": 25},
        {"meeting_id": "m3", "committee": "ועדת הכספים",        "date": "2025-12-05", "knesset_num": 25},
        {"meeting_id": "m4", "committee": "ועדת החוץ והביטחון", "date": "2025-12-10", "knesset_num": 25},
    ])
    insert_participants(conn, [
        {"meeting_id": "m1", "mk_id": "30811", "party": "עוצמה יהודית"},  # בן גביר
        {"meeting_id": "m2", "mk_id": "915",   "party": "יש עתיד"},       # בן ארי
        {"meeting_id": "m3", "mk_id": "30811", "party": "עוצמה יהודית"},  # בן גביר
        # m4 has no participants at all (nobody resolved) — still a valid meeting row
    ])
    insert_guests(conn, [
        {"meeting_id": "m2", "name": "יובל זאושניצר"},   # non-MK guest, only in m2
        {"meeting_id": "m4", "name": "רום בר-אב"},       # non-MK guest, plain-hyphen name
        # m1/m3 have no guest rows at all — still valid meeting rows
    ])
    conn.close()
    return tmp_path


class TestNoFilters:
    def test_no_filters_returns_sentinel_none(self, fixture_index):
        """No structural filter supplied -> None (skip the query entirely), not []."""
        result = query_candidate_meeting_ids(25)
        assert result is None

    def test_no_filters_does_not_touch_missing_db(self, tmp_path, monkeypatch):
        """With zero filters, the function must not even open the db —
        so it should not raise even when meeting_index.db doesn't exist."""
        monkeypatch.setattr(config, "BM25_DIR", tmp_path / "nonexistent")
        result = query_candidate_meeting_ids(25)
        assert result is None


class TestMissingDb:
    def test_filters_supplied_missing_db_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "BM25_DIR", tmp_path / "nonexistent")
        with pytest.raises(FileNotFoundError):
            query_candidate_meeting_ids(25, committees=["ועדת הכספים"])


class TestCommitteeFilter:
    def test_committee_filter(self, fixture_index):
        result = query_candidate_meeting_ids(25, committees=["ועדת החוץ והביטחון"])
        assert set(result) == {"m1", "m2", "m4"}

    def test_committee_no_match_returns_empty_list(self, fixture_index):
        result = query_candidate_meeting_ids(25, committees=["ועדה שלא קיימת"])
        assert result == []


class TestDateFilter:
    def test_date_range(self, fixture_index):
        result = query_candidate_meeting_ids(25, date_from="2025-12-01")
        assert set(result) == {"m1", "m3", "m4"}

    def test_date_range_both_bounds(self, fixture_index):
        result = query_candidate_meeting_ids(25, date_from="2025-12-01", date_to="2025-12-05")
        assert set(result) == {"m1", "m3"}


class TestCombinedFilters:
    def test_committee_and_date(self, fixture_index):
        result = query_candidate_meeting_ids(
            25, committees=["ועדת החוץ והביטחון"], date_from="2025-12-01"
        )
        assert set(result) == {"m1", "m4"}


class TestParticipantFilter:
    def test_mk_id_filter(self, fixture_index):
        result = query_candidate_meeting_ids(25, mk_ids=["30811"])
        assert set(result) == {"m1", "m3"}

    def test_party_filter(self, fixture_index):
        result = query_candidate_meeting_ids(25, parties=["עוצמה יהודית"])
        assert set(result) == {"m1", "m3"}

    def test_mk_id_no_match_returns_empty_list(self, fixture_index):
        result = query_candidate_meeting_ids(25, mk_ids=["99999999"])
        assert result == []

    def test_mk_or_party_are_ored_together(self, fixture_index):
        """mk_ids and parties within the participant group are OR-ed, not AND-ed —
        matches the old browse_rag semantics (any selected MK OR any selected
        party member counts as a hit)."""
        result = query_candidate_meeting_ids(25, mk_ids=["915"], parties=["עוצמה יהודית"])
        assert set(result) == {"m1", "m2", "m3"}

    def test_committee_and_mk_id_combined(self, fixture_index):
        """Combining a committee filter with a participant filter narrows further
        (this is the shape of the reported bug repro: topic/keyword + MK filter)."""
        result = query_candidate_meeting_ids(
            25, committees=["ועדת החוץ והביטחון"], mk_ids=["30811"]
        )
        assert set(result) == {"m1"}

    def test_true_negative_no_intersection(self, fixture_index):
        """MK 915 (בן ארי) never appears in ועדת הכספים in this fixture —
        regression guard against overcorrecting into always-returning-something."""
        result = query_candidate_meeting_ids(
            25, committees=["ועדת הכספים"], mk_ids=["915"]
        )
        assert result == []


class TestGuestFilter:
    """guest_name — LIKE-matched against meeting_guests.name, for filtering
    by a non-MK attendee who can't be resolved to an mk_id."""

    def test_guest_name_exact(self, fixture_index):
        result = query_candidate_meeting_ids(25, guest_name="יובל זאושניצר")
        assert set(result) == {"m2"}

    def test_guest_name_substring_match(self, fixture_index):
        """LIKE '%x%' — a partial name still matches."""
        result = query_candidate_meeting_ids(25, guest_name="זאושניצר")
        assert set(result) == {"m2"}

    def test_guest_name_no_match_returns_empty_list(self, fixture_index):
        result = query_candidate_meeting_ids(25, guest_name="לא קיים בכלל")
        assert result == []

    def test_guest_name_plain_hyphen_name(self, fixture_index):
        """רום בר-אב — plain-hyphen name, stored/matched as-is."""
        result = query_candidate_meeting_ids(25, guest_name="רום בר-אב")
        assert set(result) == {"m4"}

    def test_guest_name_meeting_with_zero_participant_rows_still_reachable(self, fixture_index):
        """m4 has zero rows in meeting_participants (see fixture) — the
        LEFT JOIN on meeting_participants must not silently exclude it when
        guest_name is the only match criterion."""
        result = query_candidate_meeting_ids(25, guest_name="בר-אב")
        assert "m4" in result

    def test_guest_name_ored_with_mk_ids(self, fixture_index):
        """guest_name joins the same OR-ed participant-match group as
        mk_ids/parties — any one of them counts as a hit."""
        result = query_candidate_meeting_ids(
            25, mk_ids=["30811"], guest_name="יובל זאושניצר"
        )
        assert set(result) == {"m1", "m2", "m3"}

    def test_no_filters_at_all_ignores_guest_name_none(self, fixture_index):
        """Sentinel behavior unaffected when guest_name is simply absent."""
        result = query_candidate_meeting_ids(25, guest_name=None)
        assert result is None


# ── speaker -> mk_id resolution (build_meeting_index.py logic) ───────────────

def _make_mks_fixture_bm25(path: Path) -> BM25Index:
    idx = BM25Index(path)
    idx.create_table()
    idx.insert_many([
        {"id": "30835", "label": "בועז ביסמוט", "label_lemmatized": "בועז ביסמוט",
         "body": "בועז ביסמוט", "body_lemmatized": "בועז ביסמוט",
         "extra": {"mk_id": "30835", "full_name": "בועז ביסמוט"}},
        {"id": "205", "label": "ישראל כץ", "label_lemmatized": "ישראל כץ",
         "body": "ישראל כץ", "body_lemmatized": "ישראל כץ",
         "extra": {"mk_id": "205", "full_name": "ישראל כץ"}},
        {"id": "30811", "label": "איתמר בן גביר", "label_lemmatized": "איתמר בן גביר",
         "body": "איתמר בן גביר", "body_lemmatized": "איתמר בן גביר",
         "extra": {"mk_id": "30811", "full_name": "איתמר בן גביר"}},
        {"id": "915", "label": "מירב בן ארי", "label_lemmatized": "מירב בן ארי",
         "body": "מירב בן ארי", "body_lemmatized": "מירב בן ארי",
         "extra": {"mk_id": "915", "full_name": "מירב בן ארי"}},
    ])
    return idx


@pytest.fixture()
def fuzzy_mk_index(tmp_path):
    db = tmp_path / "mks_fixture.db"
    bm25 = _make_mks_fixture_bm25(db)
    try:
        return FuzzyNameIndex.from_bm25(bm25)
    finally:
        bm25.close()


_SPEECHES_FORMAT_MEETING = {
    "meeting_id": "1001",
    "committee": "ועדת החוץ והביטחון",
    "date": "2025-01-01",
    "knesset_num": 25,
    "speeches": [
        {"speaker": "בועז ביסמוט", "text_he": "דברי פתיחה של יושב הראש."},
        {"speaker": "מירב בן ארי", "text_he": "התייחסות של חברת הכנסת."},
        {"speaker": "בועז ביסמוט", "text_he": "המשך הדיון."},  # duplicate speaker
    ],
}

_FULL_TEXT_FORMAT_MEETING = {
    "meeting_id": "1002",
    "committee": "ועדת החוץ והביטחון",
    "date": "2025-01-02",
    "knesset_num": 25,
    "full_text": (
        'היו"ר בועז ביסמוט:\n'
        "דברי פתיחה.\n\n"
        "שר הביטחון ישראל כץ:\n"
        "תשובת השר.\n\n"
        'ח"כ איתמר בן גביר:\n'
        "התייחסות.\n"
    ),
}


def _resolve_speakers_to_mk_ids(meeting: dict, fuzzy_index: FuzzyNameIndex) -> dict[str, str]:
    """Mirrors the per-meeting resolution loop in scripts/build_meeting_index.py::build()."""
    resolved: dict[str, str] = {}
    for speaker in get_meeting_speakers(meeting):
        matches = fuzzy_index.search(speaker, top_k=1, threshold=config.PARTICIPANT_FUZZY_THRESHOLD)
        if matches:
            resolved[speaker] = str(matches[0]["extra"].get("mk_id") or matches[0]["id"])
    return resolved


class TestSpeakerResolutionSpeechesFormat:
    def test_speeches_format_speakers_dedup_and_resolve(self, fuzzy_mk_index):
        speakers = get_meeting_speakers(_SPEECHES_FORMAT_MEETING)
        assert speakers == ["בועז ביסמוט", "מירב בן ארי"]  # deduped, order preserved

        resolved = _resolve_speakers_to_mk_ids(_SPEECHES_FORMAT_MEETING, fuzzy_mk_index)
        assert resolved["בועז ביסמוט"] == "30835"
        assert resolved["מירב בן ארי"] == "915"


class TestSpeakerResolutionFullTextFormat:
    def test_full_text_format_includes_minister_titled_speaker(self, fuzzy_mk_index):
        speakers = get_meeting_speakers(_FULL_TEXT_FORMAT_MEETING)
        assert any("ישראל כץ" in s for s in speakers), speakers

    def test_full_text_format_resolves_all_three_speakers(self, fuzzy_mk_index):
        resolved = _resolve_speakers_to_mk_ids(_FULL_TEXT_FORMAT_MEETING, fuzzy_mk_index)
        resolved_ids = set(resolved.values())
        assert resolved_ids == {"30835", "205", "30811"}


class TestMeetingIndexEndToEnd:
    """Speaker resolution feeding directly into the meeting_index tables —
    the exact pipeline build_meeting_index.py runs per meeting."""

    def test_participants_table_matches_resolved_speakers(self, tmp_path, monkeypatch, fuzzy_mk_index):
        monkeypatch.setattr(config, "BM25_DIR", tmp_path)
        path = db_path(25)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        create_tables(conn)

        party_map = {"30835": "הליכוד", "205": "הליכוד", "30811": "עוצמה יהודית", "915": "יש עתיד"}

        for meeting in (_SPEECHES_FORMAT_MEETING, _FULL_TEXT_FORMAT_MEETING):
            insert_meetings(conn, [{
                "meeting_id": meeting["meeting_id"], "committee": meeting["committee"],
                "date": meeting["date"], "knesset_num": 25,
            }])
            resolved = _resolve_speakers_to_mk_ids(meeting, fuzzy_mk_index)
            insert_participants(conn, [
                {"meeting_id": meeting["meeting_id"], "mk_id": mk_id, "party": party_map.get(mk_id, "")}
                for mk_id in set(resolved.values())
            ])
        conn.close()

        # בן גביר (30811) only spoke in the full_text meeting (1002)
        result = query_candidate_meeting_ids(25, mk_ids=["30811"])
        assert result == ["1002"]

        # בועז ביסמוט (30835) spoke in both
        result2 = query_candidate_meeting_ids(25, mk_ids=["30835"])
        assert set(result2) == {"1001", "1002"}
