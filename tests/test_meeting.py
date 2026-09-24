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


# ── "speeches"-shape roster regression ────────────────────────────────────────
# Verbatim transcription of the pseudo-speech header of meeting 2215520
# (ועדת החוץ והביטחון, 26/02/2024). This file is NOT a speech-by-speech scrape:
# it is a converted full_text document whose נכחו: header became pseudo-speeches
# and whose roster lost every separator ("טלי גוטליבשלום דנינו"). Before the fix
# extract_attendance() returned only the speakers, losing the 5 committee
# members who attended without taking the floor.

_STRUCTURED_MEETING = {
    "meeting_id": 2215520,
    "knesset_num": 25,
    "speeches": [
        {"speaker": "", "text_he": "פרוטוקול של ישיבת ועדה"},
        {"speaker": "סדר היום", "text_he": "הצעת חוק שירות ביטחון"},
        {"speaker": "נכחו", "text_he":
            'חברי הוועדה: יולי יואל אדלשטיין – היו"ררם בן ברק – מ"מ היו"ר'
            "טלי גוטליבשלום דנינומשה טור פזמאיר כהןשרון ניריבגני סובה"
            "צבי ידידיה סוכותלימור סון הר מלךעידן רולאלעזר שטרןאושר שקלים"},
        {"speaker": "חברי הכנסת", "text_he":
            "יאיר לפידמשה סולומוןאלון שוסטרנאור שירי"},
        {"speaker": "מוזמנים", "text_he":
            "פזית תדהר - ממונה משפטית לרגולציה וחירום, משרד הביטחון"
            'תא"ל שי טייב - רח"ט תומכ"א, משרד הביטחון'},
        {"speaker": "משתתפים באמצעים מקוונים", "text_he": "דני רון - יועץ, משרד הביטחון"},
        {"speaker": "ייעוץ משפטי", "text_he": "מירי פרנקל-שור"},
        {"speaker": "מנהל הוועדה", "text_he": "אסף פרידמן"},
        {"speaker": "רישום פרלמנטרי", "text_he":
            "אלון דמלהרשימת הנוכחים על תואריהם מבוססת על המידע שהוזן במערכת "
            "המוזמנים הממוחשבת. ייתכנו אי-דיוקים והשמטות."},
        {"speaker": 'היו"ר יולי יואל אדלשטיין', "text_he": "צוהריים טובים לכולם."},
        {"speaker": "(מוקרן סרטון, להלן התמלול)", "text_he": "טקסט הסרטון."},
        {"speaker": "טלי גוטליב (הליכוד)", "text_he": "תודה."},
        {"speaker": "קריאה", "text_he": "לא נכון."},
    ],
}

# The 132-name mks.db lexicon reduced to the names this fixture needs.
_LEXICON = (
    "אלון שוסטר", "אלעזר שטרן", "אושר שקלים", "טלי גוטליב", "יאיר לפיד",
    "יבגני סובה", "יולי יואל אדלשטיין", "לימור סון הר מלך", "מאיר כהן",
    "משה טור פז", "משה סולומון", "נאור שירי", "צבי ידידיה סוכות",
    "רם בן ברק", "שלום דנינו", "שרון ניר",
)

_ROSTER_MKS = [
    "יולי יואל אדלשטיין", "רם בן ברק", "טלי גוטליב", "שלום דנינו",
    "משה טור פז", "מאיר כהן", "שרון ניר", "יבגני סובה", "צבי ידידיה סוכות",
    "לימור סון הר מלך", "אלעזר שטרן", "אושר שקלים",
    "יאיר לפיד", "משה סולומון", "אלון שוסטר", "נאור שירי",
]


@pytest.fixture
def patched_lexicon(monkeypatch):
    from utils import meeting as meeting_module
    monkeypatch.setattr(meeting_module, "_mk_name_lexicon", lambda knesset_num=25: _LEXICON)


class TestExtractAttendanceStructuredRoster:
    """Regression: the נכחו: roster of a 'speeches'-shape protocol must be
    harvested, not just the speakers (audit: structured recall 0.804)."""

    def test_all_roster_mks_recovered(self, patched_lexicon):
        names = extract_attendance(_STRUCTURED_MEETING)
        missing = [mk for mk in _ROSTER_MKS if mk not in names]
        assert not missing, f"roster names lost: {missing}"

    def test_silent_attendees_recovered(self, patched_lexicon):
        """These five never speak in the real protocol — speaker-only
        extraction dropped them entirely."""
        names = extract_attendance(_STRUCTURED_MEETING)
        for mk in ("שלום דנינו", "צבי ידידיה סוכות", "לימור סון הר מלך",
                   "אושר שקלים", "משה סולומון"):
            assert mk in names

    def test_staff_roster_lines_kept_whole(self, patched_lexicon):
        names = extract_attendance(_STRUCTURED_MEETING)
        assert "מירי פרנקל-שור" in names   # hyphenated surname not split
        assert "אסף פרידמן" in names
        assert "אלון דמלה" in names        # end-of-block boilerplate stripped

    def test_speakers_still_included(self, patched_lexicon):
        names = extract_attendance(_STRUCTURED_MEETING)
        assert 'היו"ר יולי יואל אדלשטיין' in names
        assert "טלי גוטליב (הליכוד)" in names

    def test_header_and_stage_direction_artifacts_dropped(self, patched_lexicon):
        names = extract_attendance(_STRUCTURED_MEETING)
        for artifact in ("נכחו", "סדר היום", "מוזמנים", "ייעוץ משפטי",
                         "מנהל הוועדה", "רישום פרלמנטרי", "חברי הכנסת",
                         "משתתפים באמצעים מקוונים", "קריאה",
                         "(מוקרן סרטון, להלן התמלול)"):
            assert artifact not in names

    def test_guest_block_not_mined(self, patched_lexicon):
        """Guest bodies are 'name - role, org' runs glued end-to-end with no
        separator, so they're deliberately skipped rather than guessed at."""
        names = extract_attendance(_STRUCTURED_MEETING)
        assert not any(n.startswith("פזית תדהר -") for n in names)

    def test_no_duplicates_and_roster_precedes_speakers(self, patched_lexicon):
        names = extract_attendance(_STRUCTURED_MEETING)
        assert len(names) == len(set(names))
        assert names.index("שלום דנינו") < names.index('היו"ר יולי יואל אדלשטיין')

    def test_degrades_to_speakers_without_lexicon(self, monkeypatch):
        from utils import meeting as meeting_module
        monkeypatch.setattr(meeting_module, "_mk_name_lexicon", lambda knesset_num=25: ())
        names = extract_attendance(_STRUCTURED_MEETING)
        assert 'היו"ר יולי יואל אדלשטיין' in names
        assert "קריאה" not in names


class TestSplitGluedRosterLine:
    def test_role_suffixes_and_glue_skipped(self):
        from utils.meeting import _split_glued_roster_line
        line = ('יולי יואל אדלשטיין – היו"ררם בן ברק – מ"מ היו"ר'
                "טלי גוטליבשלום דנינו")
        assert _split_glued_roster_line(line, _LEXICON) == [
            "יולי יואל אדלשטיין", "רם בן ברק", "טלי גוטליב", "שלום דנינו",
        ]

    def test_space_and_tab_separated_variants(self):
        from utils.meeting import _split_glued_roster_line
        assert _split_glued_roster_line("רם בן ברק\tאלון שוסטר נאור שירי", _LEXICON) == [
            "רם בן ברק", "אלון שוסטר", "נאור שירי",
        ]

    def test_no_match_returns_empty(self):
        from utils.meeting import _split_glued_roster_line
        assert _split_glued_roster_line("מירי פרנקל-שור", _LEXICON) == []


class TestIsPersonName:
    def test_rejects_headers_and_stage_directions(self):
        from utils.meeting import _is_person_name
        for artifact in ("משתתפים באמצעים מקוונים", "משתתפים באמצעים דיגיטליים",
                         "מוזמנים באמצעים מקוונים", "השתתפו",
                         "השתתפו באמצעים מקוונים", "נוכחים", "חברי כנסת",
                         "(מוקרן סרטון, להלן התמלול)",
                         "(מושמעת הקלטה, להלן התמלול)",
                         "(תרגום חופשי מהשפה האנגלית)",
                         "(להלן הצגת הסרטון)",
                         "(אומר דברים בשפה האנגלית, להלן תרגומם)"):
            assert not _is_person_name(artifact), artifact

    def test_accepts_real_names(self):
        from utils.meeting import _is_person_name
        for name in ("מיכל מרים וולדיגר (הציונות הדתית)", "רום בר-אב",
                     'היו"ר עודד פורר', "Dr. Gautam nand Allahbadia"):
            assert _is_person_name(name), name


class TestSpacedHyphenRoleSuffix:
    def test_role_after_spaced_hyphen_stripped(self):
        from utils.meeting import _parse_attendance_section
        section = 'נכחו:\nחברי הוועדה:\nאליהו רביבו- היו"ר\nאריאל צרפתי - מתמחה'
        assert _parse_attendance_section(section) == ["אליהו רביבו", "אריאל צרפתי"]

    def test_hyphenated_surname_survives(self):
        from utils.meeting import _parse_attendance_section
        section = "נכחו:\nייעוץ משפטי:\nמירי פרנקל-שור\nרום בר-אב"
        assert _parse_attendance_section(section) == ["מירי פרנקל-שור", "רום בר-אב"]
