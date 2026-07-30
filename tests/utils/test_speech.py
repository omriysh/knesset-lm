"""Tests for utils.speech — get_mk_speeches_in_committee."""

import json
from pathlib import Path

import pytest

from utils.speech import (
    _name_matches,
    get_mk_speeches_in_committee,
    name_query_matches,
    name_tokens,
)


# ── _name_matches ─────────────────────────────────────────────────────────────

class TestNameMatches:
    def test_exact_match(self):
        assert _name_matches("יצחק לוי", "יצחק לוי") is True

    def test_substring_query_in_speaker(self):
        assert _name_matches("לוי", 'ח"כ יצחק לוי') is True

    def test_extra_middle_name_on_query_side(self):
        # Speaker's tokens are a subset of a fuller query (added middle name);
        # same surname → still the same person.
        assert _name_matches("יצחק משה לוי", "יצחק לוי") is True

    def test_appended_different_surname_no_longer_matches(self):
        # Regression for the surname-anchor: appending a *different* family
        # name ("שמעון") must NOT match — these are not the same person.
        assert _name_matches("יצחק לוי שמעון", "יצחק לוי") is False

    def test_hck_prefix_stripped(self):
        assert _name_matches('ח"כ יצחק לוי', "יצחק לוי") is True

    def test_fuzzy_match(self):
        # Slight typo — still above the fuzzy threshold
        assert _name_matches("יצחק לויי", "יצחק לוי") is True

    def test_no_match(self):
        assert _name_matches("שרה כהן", "יצחק לוי") is False

    def test_empty_query_returns_false(self):
        assert _name_matches("", "יצחק לוי") is False

    def test_empty_speaker_returns_false(self):
        assert _name_matches("יצחק לוי", "") is False


# ── name_query_matches — real-world MK speaker forms ──────────────────────────

class TestNameQueryMatchesRealWorld:
    """Cases drawn from the actual speaker strings stored for Orit Struck in
    the k25 speeches index — the bug where search_protocols_keyword found zero
    of her speeches because her name appears under a ministerial title / with a
    middle name and the old filter only matched a contiguous substring."""

    QUERY = "אורית סטרוק"

    def test_matches_ministerial_title_form(self):
        # 79 speeches are stored under this title form.
        assert name_query_matches(
            self.QUERY, "שרת ההתיישבות והמשימות הלאומיות אורית סטרוק"
        ) is True

    def test_matches_middle_name_form(self):
        # 20 speeches: "אורית מלכה סטרוק"
        assert name_query_matches(self.QUERY, "אורית מלכה סטרוק") is True

    def test_matches_other_title_and_middle_name_form(self):
        assert name_query_matches(
            self.QUERY, "השרה למשימות לאומיות אורית מלכה סטרוק"
        ) is True

    def test_matches_party_parenthetical_form(self):
        # "אורית מלכה סטרוק (הציונות הדתית)" — party tag stripped before match.
        assert name_query_matches(self.QUERY, "אורית מלכה סטרוק (הציונות הדתית)") is True

    def test_rejects_different_person_sharing_a_token(self):
        # A fuller query must not latch onto a different speaker who only shares
        # a first/middle name but has a different (or missing) surname.
        assert name_query_matches("אורית מלכה סטרוק", "אורי מלכה") is False

    def test_rejects_same_surname_different_first_name(self):
        assert name_query_matches("משה כהן", "דוד כהן") is False

    def test_surname_only_query_matches(self):
        assert name_query_matches("סטרוק", "אורית מלכה סטרוק") is True

    def test_party_parenthetical_stripped_from_tokens(self):
        assert name_tokens("אורית מלכה סטרוק (הציונות הדתית)") == [
            "אורית", "מלכה", "סטרוק",
        ]


# ── get_mk_speeches_in_committee ──────────────────────────────────────────────

def _make_meeting(tmp_path, committee_dir: Path, filename: str, speeches: list, **kwargs):
    """Write a structured meeting JSON file to committee_dir."""
    meeting = {
        "meeting_id": filename.replace(".json", ""),
        "date":       "2024-01-01",
        "committee":  committee_dir.name.replace("_", " "),
        "knesset_num": 25,
        "speeches":   speeches,
        **kwargs,
    }
    committee_dir.mkdir(parents=True, exist_ok=True)
    (committee_dir / filename).write_text(
        json.dumps(meeting, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture()
def transcriptions_root(tmp_path):
    """Minimal transcriptions tree with one committee and two meetings."""
    root = tmp_path / "raw_transcriptions"
    committee_dir = root / "25" / "ועדת_הכלכלה"

    _make_meeting(root, committee_dir, "2024-01-15_001.json", speeches=[
        {"speaker": 'ח"כ יצחק לוי', "text_he": "דברי ח\"כ לוי בישיבה ראשונה."},
        {"speaker": "שרה כהן",       "text_he": "דברי שרה."},
    ])
    _make_meeting(root, committee_dir, "2024-01-10_002.json", speeches=[
        {"speaker": "יצחק לוי",      "text_he": "דברי לוי בישיבה שנייה."},
    ])
    return root


class TestGetMkSpeeches:
    def test_returns_speeches_for_known_mk(self, transcriptions_root):
        result = get_mk_speeches_in_committee(
            "יצחק לוי", "ועדת הכלכלה", transcriptions_root
        )
        assert "דברי ח\"כ לוי בישיבה ראשונה." in result
        assert "דברי לוי בישיבה שנייה." in result

    def test_excludes_other_speakers(self, transcriptions_root):
        result = get_mk_speeches_in_committee(
            "יצחק לוי", "ועדת הכלכלה", transcriptions_root
        )
        assert "דברי שרה" not in result

    def test_partial_mk_name(self, transcriptions_root):
        result = get_mk_speeches_in_committee(
            "לוי", "ועדת הכלכלה", transcriptions_root
        )
        assert "דברי" in result   # found something

    def test_fuzzy_committee_name(self, transcriptions_root):
        # Slight variation in committee name
        result = get_mk_speeches_in_committee(
            "יצחק לוי", "ועדת כלכלה", transcriptions_root
        )
        assert "דברי" in result

    def test_empty_mk_name_returns_error(self, transcriptions_root):
        result = get_mk_speeches_in_committee(
            "", "ועדת הכלכלה", transcriptions_root
        )
        assert "נדרש" in result

    def test_empty_committee_returns_error(self, transcriptions_root):
        result = get_mk_speeches_in_committee(
            "יצחק לוי", "", transcriptions_root
        )
        assert "נדרש" in result

    def test_unknown_committee_lists_available(self, transcriptions_root):
        result = get_mk_speeches_in_committee(
            "יצחק לוי", "ועדת xxxx_לא_קיימת", transcriptions_root
        )
        assert "ועדת" in result   # contains available committees in error msg

    def test_unknown_mk_returns_not_found_message(self, transcriptions_root):
        result = get_mk_speeches_in_committee(
            "שם_לא_קיים_בכלל", "ועדת הכלכלה", transcriptions_root
        )
        assert "לא נמצאו" in result

    def test_max_meetings_respected(self, tmp_path):
        root = tmp_path / "raw_transcriptions"
        committee_dir = root / "25" / "ועדת_מבחן"
        for i in range(5):
            _make_meeting(root, committee_dir, f"2024-01-{i+1:02d}_00{i}.json",
                          speeches=[{"speaker": "יצחק לוי",
                                     "text_he": f"נאום {i}"}])
        result = get_mk_speeches_in_committee(
            "יצחק לוי", "ועדת מבחן", root, max_meetings=2
        )
        # Only 2 meetings scanned (most recent 2)
        assert result.count("###") <= 2

    def test_result_includes_meeting_header(self, transcriptions_root):
        result = get_mk_speeches_in_committee(
            "יצחק לוי", "ועדת הכלכלה", transcriptions_root
        )
        assert "###" in result   # meeting block headers present
