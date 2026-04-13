"""Tests for utils.speech — get_mk_speeches_in_committee."""

import json
from pathlib import Path

import pytest

from utils.speech import _name_matches, get_mk_speeches_in_committee


# ── _name_matches ─────────────────────────────────────────────────────────────

class TestNameMatches:
    def test_exact_match(self):
        assert _name_matches("יצחק לוי", "יצחק לוי") is True

    def test_substring_query_in_speaker(self):
        assert _name_matches("לוי", 'ח"כ יצחק לוי') is True

    def test_substring_speaker_in_query(self):
        assert _name_matches("יצחק לוי שמעון", "יצחק לוי") is True

    def test_hck_prefix_stripped(self):
        assert _name_matches('ח"כ יצחק לוי', "יצחק לוי") is True

    def test_fuzzy_match(self):
        # Slight typo — still above 0.65 threshold
        assert _name_matches("יצחק לויי", "יצחק לוי") is True

    def test_no_match(self):
        assert _name_matches("שרה כהן", "יצחק לוי") is False

    def test_empty_query_returns_false(self):
        assert _name_matches("", "יצחק לוי") is False

    def test_empty_speaker_returns_false(self):
        assert _name_matches("יצחק לוי", "") is False


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
