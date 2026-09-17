"""
tests/test_bullet_mk_link.py

Tests for src/indexing/bullet_mk_link.py — extracting a speaker-name prefix
from a summary opinion bullet and resolving it to an mk_id.

Bullet shapes covered here come verbatim from a full-scan profile of
Data/bm25/25/bullets.db (176,992 bullets, 2026-08): "Name (Party):",
"ח\"כ Name:", "היו\"ר Name:", ministry/role parentheticals for guests,
structural-label leaks ("תאריך:", "מזהה ישיבה:", "נוכחים:"), markdown
subheadings, and multi-person prefixes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from indexing.bullet_mk_link import extract_speaker_prefix, resolve_bullet_mk


# ── Roster fixture ────────────────────────────────────────────────────────────

ROSTER = [
    {"mk_id": "101", "name": "שמחה רוטמן"},
    {"mk_id": "102", "name": "גלעד קריב"},
    {"mk_id": "103", "name": "ינון אזולאי"},
    {"mk_id": "104", "name": "צביקה פוגל"},
    {"mk_id": "105", "name": "אביחי אברהם בוארון"},
    {"mk_id": "106", "name": "טלי גוטליב"},
    {"mk_id": "107", "name": "מתן כהנא"},
]


# ── extract_speaker_prefix ────────────────────────────────────────────────────

class TestExtractSpeakerPrefix:
    def test_title_hk_stripped(self):
        assert extract_speaker_prefix(
            "ח\"כ שמחה רוטמן: ביקש התייחסות לעמדת הממשלה"
        ) == "שמחה רוטמן"

    def test_title_chair_stripped(self):
        assert extract_speaker_prefix(
            "היו\"ר צביקה פוגל: הדגיש את חשיבות הדיון"
        ) == "צביקה פוגל"

    def test_party_parenthetical_stripped(self):
        assert extract_speaker_prefix(
            "ינון אזולאי (ש\"ס): תמך בהצעת החוק"
        ) == "ינון אזולאי"

    def test_title_and_party_both_stripped(self):
        assert extract_speaker_prefix(
            "ח\"כ גלעד קריב (העבודה): התנגד לסעיף"
        ) == "גלעד קריב"

    def test_gershayim_title_variant(self):
        # Unicode gershayim (״) instead of ASCII quote in ח"כ
        assert extract_speaker_prefix(
            "ח״כ טלי גוטליב: הדגישה את פרשנותה"
        ) == "טלי גוטליב"

    def test_guest_with_role_parenthetical_extracted(self):
        # Extraction succeeds for non-MK people too — resolution is the filter.
        assert extract_speaker_prefix(
            "גור בליי (ייעוץ משפטי): הסביר כי דרך המלך היא המתנה"
        ) == "גור בליי"

    def test_ministry_parenthetical_extracted(self):
        assert extract_speaker_prefix(
            "זיו שגיב (משטרת ישראל): הסביר את הנוהל"
        ) == "זיו שגיב"

    # ── rejections ──

    def test_no_colon_returns_none(self):
        assert extract_speaker_prefix("דיון בנושא חינוך והשכלה גבוהה") is None

    def test_date_metadata_leak_rejected(self):
        assert extract_speaker_prefix("תאריך: 2025-01-06") is None

    def test_session_id_metadata_leak_rejected(self):
        assert extract_speaker_prefix("מזהה ישיבה: 2218120") is None

    def test_attendance_labels_rejected(self):
        assert extract_speaker_prefix("נוכחים: כל חברי הוועדה") is None
        assert extract_speaker_prefix("נעדרים: שר האוצר") is None

    def test_structural_labels_rejected(self):
        assert extract_speaker_prefix("עמדות מרכזיות: להלן העמדות") is None
        assert extract_speaker_prefix("סדר היום: הצעת חוק") is None

    def test_single_word_prefix_rejected(self):
        # A person prefix needs at least first + last name.
        assert extract_speaker_prefix("חדש: נוסח מעודכן") is None

    def test_digits_in_prefix_rejected(self):
        assert extract_speaker_prefix("סעיף 12: דיון בתקציב") is None

    def test_long_sentence_prefix_rejected(self):
        assert extract_speaker_prefix(
            "הוועדה דנה בהצעת החוק והחליטה כי יש להמשיך את הדיון בשבוע הבא: כך נקבע"
        ) is None

    def test_empty_and_whitespace(self):
        assert extract_speaker_prefix("") is None
        assert extract_speaker_prefix("   ") is None

    def test_title_only_prefix_rejected(self):
        assert extract_speaker_prefix("ח\"כ: אמר משהו") is None


# ── resolve_bullet_mk ────────────────────────────────────────────────────────

class TestResolveBulletMk:
    def test_exact_roster_name(self):
        hit = resolve_bullet_mk("ח\"כ שמחה רוטמן: ביקש התייחסות", ROSTER)
        assert hit is not None
        assert hit["mk_id"] == "101"
        assert hit["mk_name"] == "שמחה רוטמן"

    def test_party_form_resolves(self):
        hit = resolve_bullet_mk("גלעד קריב (העבודה): התנגד לסעיף", ROSTER)
        assert hit is not None and hit["mk_id"] == "102"

    def test_middle_name_variant_resolves(self):
        # Bullet says "אביחי בוארון", roster stores full legal name.
        hit = resolve_bullet_mk("אביחי בוארון (הליכוד): תמך בתיקון", ROSTER)
        assert hit is not None and hit["mk_id"] == "105"

    def test_guest_not_in_roster_returns_none(self):
        assert resolve_bullet_mk(
            "גור בליי (ייעוץ משפטי): הסביר את הסעיף", ROSTER
        ) is None

    def test_unrelated_name_returns_none(self):
        assert resolve_bullet_mk(
            "תומר רוזנר (ייעוץ משפטי): הציג את הנוסח", ROSTER
        ) is None

    def test_multi_person_prefix_returns_none(self):
        # Four names glued in one prefix — must not resolve to any single MK.
        assert resolve_bullet_mk(
            "גיל לימון, עמית מררי, אלעזר כהנא, רעות גורדון (משרד המשפטים): דוחים את הטענה",
            ROSTER,
        ) is None

    def test_no_prefix_returns_none(self):
        assert resolve_bullet_mk("דיון כללי בנושא תקציב הביטחון", ROSTER) is None

    def test_empty_roster_returns_none(self):
        assert resolve_bullet_mk("ח\"כ שמחה רוטמן: ביקש התייחסות", []) is None

    def test_similar_but_different_mk_not_confused(self):
        # "מתן כהנא" must not fuzzy-match a different roster MK.
        hit = resolve_bullet_mk("מתן כהנא: התנגד לקישור", ROSTER)
        assert hit is not None and hit["mk_id"] == "107"

    def test_speaker_field_carries_cleaned_prefix(self):
        hit = resolve_bullet_mk("ח\"כ אביחי בוארון (הליכוד): תמך", ROSTER)
        assert hit is not None
        assert hit["speaker"] == "אביחי בוארון"
