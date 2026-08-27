"""Regression tests for FuzzyNameIndex middle-name token containment.

Covers the audit FIX #3: protocol speaker names that differ from the roster
label only by an inserted middle name (WRatio 85, below the 90 participant
threshold) must resolve, while one-token-overlap near misses must not.
All rosters here are small synthetic in-memory ones — no dependency on Data/.
"""

import config
import pytest
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex, _is_middle_name_variant

THRESHOLD = config.PARTICIPANT_FUZZY_THRESHOLD


def _index(*labels: str) -> FuzzyNameIndex:
    return FuzzyNameIndex([
        {"id": str(i), "label": label, "body": label, "extra": {"full_name": label}}
        for i, label in enumerate(labels, start=1)
    ])


ROSTER = (
    "אביחי אברהם בוארון",
    "ירון לוי",
    "ששון ששי גואטה",
    "אריה מכלוף דרעי",
    "אליהו רביבו",
    "סימון דוידסון",
    "בועז ביסמוט",
    "משה אבוטבול",
    "חיים ביטון",
    "משה גפני",
)


@pytest.mark.parametrize("query, expected_label", [
    ("אביחי בוארון (הליכוד)",   "אביחי אברהם בוארון"),
    ("ירון עמוס לוי (יש עתיד)", "ירון לוי"),
    ("ששון גואטה (הליכוד )",    "ששון ששי גואטה"),
    ("אביחי בוארון",            "אביחי אברהם בוארון"),
    ("ששון גואטה",              "ששון ששי גואטה"),
])
def test_middle_name_variants_resolve(query, expected_label):
    matches = _index(*ROSTER).search(query, top_k=1, threshold=THRESHOLD)
    assert matches, f"{query!r} did not resolve"
    assert matches[0]["label"] == expected_label
    assert matches[0]["score"] >= THRESHOLD / 100.0


@pytest.mark.parametrize("query", [
    "יוסף עטאונה",
    "אברהם בצלאל",
    "אליהו ברוכי",
    "סימון מושיאשוילי",
    "בועז טופורובסקי",
    "משה רוט",
    "יפעת שאשא ביטון",
    "משה שמעון רוט",
])
def test_one_token_overlap_still_rejected(query):
    assert _index(*ROSTER).search(query, top_k=5, threshold=THRESHOLD) == []


def test_single_shared_token_never_accepted():
    index = _index("ירון לוי", "מיקי לוי")
    assert index.search("דוד לוי", top_k=5, threshold=THRESHOLD) == []
    assert not _is_middle_name_variant(["דוד", "לוי"], ["ירון", "לוי"])
    assert not _is_middle_name_variant(["לוי"], ["ירון", "לוי"])


@pytest.mark.parametrize("query_tokens, label_tokens, accepted", [
    (["אביחי", "בוארון"],           ["אביחי", "אברהם", "בוארון"], True),
    (["ירון", "עמוס", "לוי"],       ["ירון", "לוי"],              True),
    (["אביחי", "אברהם"],            ["אביחי", "אברהם", "בוארון"], False),
    (["אברהם", "בוארון"],           ["אביחי", "אברהם", "בוארון"], False),
    (["אברהם", "בצלאל"],            ["אביחי", "אברהם", "בוארון"], False),
    (["משה", "שמעון", "רוט"],       ["משה", "גפני"],              False),
    (["יפעת", "שאשא", "ביטון"],     ["חיים", "ביטון"],            False),
    (["ירון", "לוי"],               ["ירון", "לוי"],              False),
])
def test_first_and_last_token_must_both_align(query_tokens, label_tokens, accepted):
    assert _is_middle_name_variant(query_tokens, label_tokens) is accepted


def test_roster_labels_do_not_collide_with_each_other():
    index = _index(*ROSTER)
    for entry in index._entries:
        matches = index.search(entry["label"], top_k=5, threshold=THRESHOLD)
        assert [m["id"] for m in matches] == [entry["id"]], entry["label"]


def test_party_suffix_and_quote_variants_normalized():
    index = _index('בצלאל סמוטריץ׳', "מכלוף מיקי זוהר")
    matches = index.search("בצלאל סמוטריץ' (הציונות הדתית )", top_k=1, threshold=THRESHOLD)
    assert matches and matches[0]["label"] == 'בצלאל סמוטריץ׳'


def test_result_shape_unchanged():
    matches = _index("ירון לוי").search("ירון עמוס לוי", top_k=1, threshold=THRESHOLD)
    assert set(matches[0]) == {"id", "label", "score", "extra", "fetched"}
    assert matches[0]["fetched"] is False
    assert 0.0 <= matches[0]["score"] <= 1.0
    assert matches[0]["extra"] == {"full_name": "ירון לוי"}


def test_exact_match_outranks_containment_match():
    index = _index("ירון לוי", "ירון עמוס לוי")
    matches = index.search("ירון עמוס לוי", top_k=2, threshold=THRESHOLD)
    assert matches[0]["label"] == "ירון עמוס לוי"
    assert matches[0]["score"] == 1.0


def test_default_threshold_behaviour_preserved():
    index = _index(*ROSTER)
    assert index.search("משה רוט", top_k=3, threshold=config.FUZZY_SEARCH_THRESHOLD)
    assert index.search("", top_k=3) == []
