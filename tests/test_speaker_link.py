"""
tests/test_speaker_link.py

Tests for indexing.speaker_link: protocol speaker label -> cleaned name,
party, and fuzzy mk_id resolution against a roster.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from indexing.speaker_link import build_roster_index, clean_speaker_label, resolve_speaker

ROSTER = [
    {"mk_id": "101", "name": "שמחה רוטמן"},
    {"mk_id": "102", "name": "גלעד קריב"},
    {"mk_id": "103", "name": "ינון אזולאי"},
    {"mk_id": "104", "name": "צביקה פוגל"},
    {"mk_id": "105", "name": "אביחי אברהם בוארון", "aliases": "אביחי בוארון | אברהם בוארון"},
    {"mk_id": "106", "name": "טלי גוטליב"},
    {"mk_id": "107", "name": "מתן כהנא"},
    {"mk_id": "108", "name": "משה אבוטבול"},
]


@pytest.fixture(scope="module")
def roster():
    return build_roster_index(ROSTER)


class TestCleanSpeakerLabel:
    def test_title_stripped(self):
        assert clean_speaker_label('ח"כ שמחה רוטמן') == "שמחה רוטמן"
        assert clean_speaker_label('היו"ר צביקה פוגל') == "צביקה פוגל"

    def test_party_parenthetical_stripped(self):
        assert clean_speaker_label('ח"כ גלעד קריב (העבודה)') == "גלעד קריב"

    def test_gershayim_title_variant(self):
        assert clean_speaker_label("ח״כ טלי גוטליב") == "טלי גוטליב"

    def test_guest_with_role_kept_as_name(self):
        assert clean_speaker_label("גור בליי (ייעוץ משפטי)") == "גור בליי"

    def test_long_role_prefix_kept(self):
        assert clean_speaker_label("סגן שר החקלאות ופיתוח הכפר משה אבוטבול") == "החקלאות ופיתוח הכפר משה אבוטבול"

    def test_rejects_structural_single_word_digits(self):
        assert clean_speaker_label("קריאה") is None
        assert clean_speaker_label("נוכחים") is None
        assert clean_speaker_label("רוטמן") is None
        assert clean_speaker_label("דובר 3") is None
        assert clean_speaker_label("") is None
        assert clean_speaker_label('ח"כ') is None


class TestResolveSpeaker:
    def test_exact_roster_name(self, roster):
        hit = resolve_speaker('ח"כ שמחה רוטמן', roster)
        assert hit == {"mk_id": "101", "mk_name": "שמחה רוטמן", "speaker_name": "שמחה רוטמן"}

    def test_party_form_resolves(self, roster):
        assert resolve_speaker("גלעד קריב (העבודה)", roster)["mk_id"] == "102"

    def test_middle_name_variant_resolves(self, roster):
        assert resolve_speaker("אביחי בוארון (הליכוד)", roster)["mk_id"] == "105"

    def test_long_role_prefix_resolves(self, roster):
        hit = resolve_speaker("סגן שר החקלאות ופיתוח הכפר משה אבוטבול", roster)
        assert hit["mk_id"] == "108"
        assert hit["mk_name"] == "משה אבוטבול"

    def test_guest_returns_none(self, roster):
        assert resolve_speaker("גור בליי (ייעוץ משפטי)", roster) is None
        assert resolve_speaker("תומר רוזנר", roster) is None

    def test_unknown_role_prefix_resolves_via_trailing_window(self, roster):
        hit = resolve_speaker("מנהל אגף תקציבים במשרד הבריאות שמחה רוטמן", roster)
        assert hit is not None and hit["mk_id"] == "101"

    def test_multi_person_label_returns_none(self, roster):
        assert resolve_speaker("גיל לימון, עמית מררי, אלעזר כהנא, רעות גורדון (משרד המשפטים)", roster) is None

    def test_similar_names_not_confused(self, roster):
        assert resolve_speaker("מתן כהנא", roster)["mk_id"] == "107"

    def test_attendee_preferred_among_candidates(self, roster):
        both = build_roster_index(ROSTER + [{"mk_id": "201", "name": "שמחה רוטמן"}])
        assert resolve_speaker("שמחה רוטמן", both, participant_mk_ids={"201"})["mk_id"] == "201"

    def test_empty_roster(self):
        assert resolve_speaker("שמחה רוטמן", build_roster_index([])) is None
