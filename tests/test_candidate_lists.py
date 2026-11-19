"""
tests/test_candidate_lists.py

Parsing and MK matching of scripts/build_candidate_lists.py, on page texts in the layouts gov.il uses.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import build_candidate_lists as lists

PAGE_HEAD = "הבחירות לכנסת ה-26\nרשימה\nשתפו:\n\nתמונת פתק אמת רשימה\n\n"
PAGE_TAIL = "\nקבצים מצורפים\nפתק\npng\n"


def page(body: str) -> str:
    return PAGE_HEAD + body + PAGE_TAIL


class TestParseCandidates:
    def test_one_per_line_with_party(self):
        text = page("1. גולן יאיר\n\nמטעם מפלגת הדמוקרטים\n\t\n\n2. לזימי נעמה\n\nמטעם מפלגת מרצ\n")
        assert lists.parse_candidates(text) == [
            {"position": 1, "name_raw": "גולן יאיר", "from_party": "הדמוקרטים"},
            {"position": 2, "name_raw": "לזימי נעמה", "from_party": "מרצ"}]

    def test_tab_separated_rows(self):
        text = page("1. שקד אבי\t2. עטאונה נעים\t3. עאטללה חאתם\n4. סלימאן האלה\t \t \n")
        assert [c["name_raw"] for c in lists.parse_candidates(text)] == ["שקד אבי", "עטאונה נעים", "עאטללה חאתם", "סלימאן האלה"]

    def test_party_and_next_candidate_share_a_line(self):
        text = page("1. הנדל יועז\nמטעם מפלגת המילואימניקים\t2. זליכה ירון\nמטעם מפלגת הכלכלית החדשה\n")
        assert [(c["name_raw"], c["from_party"]) for c in lists.parse_candidates(text)] == [
            ("הנדל יועז", "המילואימניקים"), ("זליכה ירון", "הכלכלית החדשה")]

    def test_comma_and_nbsp(self):
        text = page("1. אלקרינאוי, טלאל\n\xa0\t2. אבו עאנם עבדאללה\n")
        assert [c["name_raw"] for c in lists.parse_candidates(text)] == ["אלקרינאוי טלאל", "אבו עאנם עבדאללה"]

    def test_unnumbered_single_candidate(self):
        assert lists.parse_candidates(page("גולדמן נפתלי בורוך\n")) == [
            {"position": 1, "name_raw": "גולדמן נפתלי בורוך", "from_party": ""}]

    def test_footnotes_are_not_candidates(self):
        text = page("1. כהן דנה\t2. לוי משה\n"
                    "בהחלטת הוועדה מיום 9.2026, בפני המשנה ליועצת המשפטית, הוחלט כי עופר כסיף רשאי להשתתף.\n")
        assert [c["position"] for c in lists.parse_candidates(text)] == [1, 2]


def person(first, last, knessets, aliases=()):
    return {"person_id": hash((first, last)) % 100000, "first_name": first, "last_name": last,
            "knessets": knessets, "aliases": list(aliases)}


class TestMatchMk:
    def test_exact_name_in_either_order(self):
        kariv = person("גלעד", "קריב", [24, 25])
        assert lists.match_mk("קריב גלעד", [kariv])[0] is kariv

    def test_extra_first_name_of_a_recent_mk(self):
        gottlieb = person("טלי", "גוטליב", [25])
        assert lists.match_mk("גוטליב רויטל טלי", [gottlieb])[0] is gottlieb

    def test_spelling_variants_of_a_recent_mk(self):
        eisenkot = person("גדי", "איזנקוט", [25])
        assert lists.match_mk("איזנקוט גד", [eisenkot])[0] is eisenkot
        alhuashla = person("ואליד", "אלהואשלה", [25])
        assert lists.match_mk("אל הואשלה ואליד", [alhuashla])[0] is alhuashla
        bismuth = person("בועז", "ביסמוט", [25])
        assert lists.match_mk("ביסמוט בעז", [bismuth])[0] is bismuth

    def test_similar_names_of_different_people(self):
        piron = person("שי", "פירון", [19, 20])
        assert lists.match_mk("ירון שי", [piron])[0] is None
        asher = person("יעקב", "אשר", [24, 25])
        assert lists.match_mk("גוטרמן יעקב אשר", [asher])[0] is None

    def test_spelling_variants_not_for_old_knessets(self):
        shitrit = person("שמעון", "שטרית", [12, 13])
        assert lists.match_mk("שיטרית שמעון", [shitrit])[0] is None

    def test_partial_match_needs_the_last_name_first(self):
        yehuda = person("חיים", "יהודה", [23, 24])
        assert lists.match_mk("רייכמן חיים יהודה", [yehuda])[0] is None

    def test_partial_match_not_for_old_knessets(self):
        stern = person("אברהם", "שטרן", [14])
        assert lists.match_mk("שטרן אברהם פנחס", [stern])[0] is None

    def test_repeated_token_is_not_two_names(self):
        mula = person("פטין", "מולא", [21, 23, 24])
        assert lists.match_mk("מולא מולא", [mula])[0] is None

    def test_latest_serving_wins_and_ties_are_unmatched(self):
        old, new = person("דוד", "כהן", [14, 15]), person("דוד", "כהן", [24, 25])
        assert lists.match_mk("כהן דוד", [old, new])[0] is new
        twin = person("דוד", "כהן", [25])
        match, note = lists.match_mk("כהן דוד", [new, twin])
        assert match is None and note.startswith("ambiguous")


@pytest.mark.parametrize("name_raw,first_names,expected", [
    ("גולן יאיר", {"יאיר"}, "יאיר גולן"),
    ("לסקי שוץ גבריאלה", set(), "גבריאלה לסקי שוץ"),
    ("מואטי אמילי חיה", {"אמילי", "חיה"}, "אמילי חיה מואטי"),
    ("סון הר-מלך לימור", {"לימור"}, "לימור סון הר-מלך"),
])
def test_display_name_order(name_raw, first_names, expected):
    assert lists._name_splits(name_raw, first_names)[0] == expected


SITE_NAMES = {1001: "יצחק זאב פינדרוס", 1002: "מאי גולן", 1027: "יאיר גולן", 958: "ג'מעה  אזברגה",
              873: "מרדכי יוגב", 1047: "מטי יוגב", 1092: "גדי איזנקוט", 1200: "דוד כהן", 1201: "דוד יעקב כהן"}


@pytest.mark.parametrize("name,expected", [
    ("יאיר גולן", 1027),
    ("יצחק פינדרוס", 1001),
    ("ג'מעה אזברגה", 958),
    ("מטי יוגב", 1047),
    ("גד איזנקוט", 1092),
    ("דוד כהן", 1200),
    ("משה גולן", None),
    ("יוגב", None),
])
def test_knesset_site_id(name, expected):
    assert lists._site_id_of(name, SITE_NAMES) == expected
