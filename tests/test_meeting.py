"""
tests/test_meeting.py

Tests for src/utils/meeting.py::extract_attendance() — the נכחו: (attendance)
header parser for full_text-format (OData PDF/Word extraction) meeting
protocols.

The primary fixture below is a hand-normalized transcription of the real
attendance header verified byte-for-byte against
Data/raw_transcriptions/25/ועדת_החוץ_והביטחון/09_12_2025_2237521.json
(inlined so these tests don't depend on Data/ being present). A second,
skip-if-missing test exercises the actual file directly as a real-data
smoke test, following this repo's existing skip-if-missing convention
(see tests/test_tool_dispatch.py).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from utils.meeting import extract_attendance


# ── Ground-truth fixture ──────────────────────────────────────────────────────
# Verbatim (already LF-normalized / \x07-stripped) transcription of the real
# נכחו: header from meeting 2237521 (ועדת החוץ והביטחון, 09/12/2025).

_ATTENDANCE_HEADER = """נכחו:
חברי הוועדה:
בועז ביסמוט – היו"ר
ינון אזולאי
יעקב אשר
מירב בן ארי
רם בן ברק
שלום דנינו
עמית הלוי
ניסים ואטורי
משה טור פז
מאיר כהן
שרון ניר
צבי ידידיה סוכות
לימור סון הר מלך
מאיר פרוש
מטי צרפתי הרכבי
אליהו רביבו
יעל רון בן משה
אפרת רייטן מרום
אלעזר שטרן


חברי הכנסת:
יולי יואל אדלשטיין
ולדימיר בליאק
מירב כהן
יוראי להב הרצנו
משה סולומון
אורית פרקש הכהן
גלעד קריב

מוזמנים:
יוסי פוקס
–
מזכיר הממשלה, משרד ראש הממשלה

שי טייב
–
רח"ט תומכ"א, צה"ל, משרד הביטחון

רום בר-אב
–
רכז תעסוקה, אגף התקציבים, משרד האוצר

חגי לובר
–
משפחות שכולות

יובל זאושניצר
–
אב שכול, משפחות שכולות



ייעוץ משפטי:
מירי פרנקל-שור
איילת לוי נחום

מנהל הוועדה:
אסף פרידמן
יוני בן הרוש

רישום פרלמנטרי:
סמדר לביא, חבר תרגומים


רשימת הנוכחים על תואריהם מבוססת על המידע שהוזן במערכת המוזמנים הממוחשבת. ייתכנו אי-דיוקים והשמטות.

הצעת חוק לדוגמה, התשפ"ו-2025

היו"ר בועז ביסמוט:
דברי פתיחה.
"""

_FULL_TEXT_MEETING = {
    "meeting_id": "2237521",
    "committee": "ועדת החוץ והביטחון",
    "date": "2025-12-09",
    "knesset_num": 25,
    "full_text": _ATTENDANCE_HEADER,
}


class TestExtractAttendanceFullTextFormat:
    def test_committee_members_present_dash_suffix_stripped(self):
        names = extract_attendance(_FULL_TEXT_MEETING)
        # Chair's inline "– היו"ר" role suffix must be stripped.
        assert "בועז ביסמוט" in names
        assert "בועז ביסמוט – היו\"ר" not in names
        for n in ("ינון אזולאי", "יעקב אשר", "מירב בן ארי", "רם בן ברק",
                  "שלום דנינו", "עמית הלוי", "ניסים ואטורי", "משה טור פז",
                  "מאיר כהן", "שרון ניר", "צבי ידידיה סוכות",
                  "לימור סון הר מלך", "מאיר פרוש", "מטי צרפתי הרכבי",
                  "אליהו רביבו", "יעל רון בן משה", "אפרת רייטן מרום",
                  "אלעזר שטרן"):
            assert n in names, f"missing committee member: {n}"

    def test_other_mks_present(self):
        names = extract_attendance(_FULL_TEXT_MEETING)
        for n in ("יולי יואל אדלשטיין", "ולדימיר בליאק", "מירב כהן",
                   "יוראי להב הרצנו", "משה סולומון", "אורית פרקש הכהן",
                   "גלעד קריב"):
            assert n in names, f"missing other MK: {n}"

    def test_guests_present(self):
        names = extract_attendance(_FULL_TEXT_MEETING)
        assert "יוסי פוקס" in names
        assert "שי טייב" in names
        assert "יובל זאושניצר" in names
        assert "חגי לובר" in names

    def test_plain_hyphen_in_name_not_mangled(self):
        """רום בר-אב's surname contains a plain hyphen ('-', U+002D) — must
        NOT be split there. Only the en-dash ('–', U+2013) role separator
        may split a line."""
        names = extract_attendance(_FULL_TEXT_MEETING)
        assert "רום בר-אב" in names
        assert "רום בר" not in names

    def test_plain_hyphen_in_roster_name_not_mangled(self):
        """מירי פרנקל-שור (ייעוץ משפטי roster, not a guest block) — same
        plain-hyphen rule applies to the roster parser too."""
        names = extract_attendance(_FULL_TEXT_MEETING)
        assert "מירי פרנקל-שור" in names
        assert "מירי פרנקל" not in names

    def test_roster_style_sections_included(self):
        names = extract_attendance(_FULL_TEXT_MEETING)
        assert "אסף פרידמן" in names
        assert "יוני בן הרוש" in names
        # "סמדר לביא, חבר תרגומים" — trailing role after comma stripped.
        assert "סמדר לביא" in names
        assert "סמדר לביא, חבר תרגומים" not in names

    def test_dialogue_after_boilerplate_not_included(self):
        """Content after the רשימת הנוכחים... boilerplate (bill title,
        actual speaker turns) must not leak into the attendance list."""
        names = extract_attendance(_FULL_TEXT_MEETING)
        assert not any("היו\"ר בועז ביסמוט" == n for n in names)
        assert "הצעת חוק לדוגמה, התשפ\"ו-2025" not in names

    def test_deduplicated_and_ordered(self):
        names = extract_attendance(_FULL_TEXT_MEETING)
        assert len(names) == len(set(names))
        # Committee members appear before other MKs appear before guests.
        assert names.index("בועז ביסמוט") < names.index("יולי יואל אדלשטיין")
        assert names.index("יולי יואל אדלשטיין") < names.index("יוסי פוקס")

    def test_no_names_found_returns_empty_list(self):
        meeting = {"full_text": "פרוטוקול ללא נוכחים כלל, רק טקסט חופשי."}
        assert extract_attendance(meeting) == []


class TestExtractAttendanceSpeechesFormatUnchanged:
    """The 'speeches' branch must remain exactly as before (dedup, order)."""

    def test_speeches_format(self):
        meeting = {
            "speeches": [
                {"speaker": "בועז ביסמוט", "text_he": "פתיחה."},
                {"speaker": "מירב בן ארי", "text_he": "תגובה."},
                {"speaker": "בועז ביסמוט", "text_he": "המשך."},
            ]
        }
        assert extract_attendance(meeting) == ["בועז ביסמוט", "מירב בן ארי"]


class TestExtractAttendanceRealDataSmoke:
    """Real-data smoke test against the actual ground-truth file, skipped
    if Data/ isn't present (this repo's convention — see
    tests/test_tool_dispatch.py)."""

    _REAL_FILE = Path(
        "C:/Work/Projects/KnessetLM/Data/raw_transcriptions/25/"
        "ועדת_החוץ_והביטחון/09_12_2025_2237521.json"
    )

    def test_real_file_matches_fixture_expectations(self):
        if not self._REAL_FILE.exists():
            pytest.skip(f"real data file not present: {self._REAL_FILE}")

        from utils.meeting import load_meeting
        meeting = load_meeting(self._REAL_FILE)
        names = extract_attendance(meeting)

        assert len(names) >= 60, (
            f"expected at least 60 names (19 committee + 7 MK + ~40 guests "
            f"+ staff), got {len(names)}"
        )
        assert "בועז ביסמוט" in names
        assert "יובל זאושניצר" in names
        assert "חגי לובר" in names
        assert "רום בר-אב" in names
        assert "מירי פרנקל-שור" in names
