"""name_search auto-resolution (record fetch) on committee names.

The fuzzy scores of real committee names plateau (exact 1.0, containing
labels 0.9, shared "ועדת ה..." prefix 0.855), so a score-gap rule alone
never fetches. The labels below are copied from the real Data/knesset.db
committees table (Knesset 25).
"""

import json
import sqlite3
from pathlib import Path

import pytest

import config
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex
from utils.tool_helpers.name_search import name_search

REAL_COMMITTEES_25 = {
    "4186": "ועדת הכספים",
    "4190": "ועדת החוץ והביטחון",
    "4191": "ועדת החוקה, חוק ומשפט",
    "4192": "ועדת החינוך התרבות והספורט",
    "4194": "ועדת הכנסת",
    "4196": "ועדת העבודה והרווחה",
    "4187": "הוועדה לביטחון לאומי",
    "4209": "הוועדה המיוחדת לזכויות הילד",
    "4208": "הוועדה המיוחדת לעובדים זרים",
    "4202": "הוועדה המשותפת של ועדת החוץ והביטחון וועדת הכספים לתקציב הביטחון",
    "4238": "הוועדה המשותפת של ועדת הכנסת וועדת הכספים לתקציב הכנסת לפי חוק יסודות התקציב, התשמ\"ה-1985",
    "4272": "הוועדה המשותפת לוועדת החוקה, חוק ומשפט ולוועדת החוץ והביטחון לפי חוק המידע הפלילי ותקנת שבים, התשע\"ט-2019",
    "4286": "הוועדה המשותפת לוועדת החוץ והביטחון ולוועדת העבודה והרווחה, לפי חוק שירות הקבע בצה\"ל (גמלאות)",
    "4242": "ועדת הכנסת המשותפת ליישומים ביומטריים, לפי חוק הכללת אמצעי זיהוי ביומטריים ונתוני זיהוי ביומטריים במסמכי זיהוי ובמאגר מידע, התש\"ע-2009",
    "4274": "ועדת המשנה להגות יהודית במערכת החינוך",
    "4280": "ועדת המשנה לעניין אבטחת חברי הכנסת וסמכויות קצין הכנסת",
    "4265": "ועדת המשנה לספורט",
    "4283": "ועדה משותפת לוועדת החינוך, התרבות והספורט, ועדת הכנסת וועדת הפנים והגנת הסביבה, לפי חוק מוזאון הכנסת, התש\"ע-2010",
    "4287": "ועדה משותפת של ועדת החינוך, התרבות והספורט וועדת הפנים והגנת הסביבה לפי חוק בית הספר החקלאי מקווה ישראל, תשל\"ו-1976",
}


def _committee_index(labels_by_id: dict[str, str]) -> FuzzyNameIndex:
    return FuzzyNameIndex([
        {"id": committee_id, "label": label, "body": label, "extra": {}}
        for committee_id, label in labels_by_id.items()
    ])


class _RecordingFetcher:
    def __init__(self):
        self.requested_ids: list[str] = []

    def __call__(self, committee_id: str) -> dict:
        self.requested_ids.append(committee_id)
        return {"committee_id": committee_id, "members": [{"mk_id": "1", "name": "x"}]}


RESOLVING_QUERIES = [
    ("ועדת הכספים",                  "4186"),
    ("ועדת החוקה",                   "4191"),
    ("ועדת החוקה, חוק ומשפט",        "4191"),
    ("ועדת החינוך",                  "4192"),
    ("ועדת החינוך, התרבות והספורט",  "4192"),
    ("ועדת החוץ והביטחון",           "4190"),
    ("ועדת הכנסת",                   "4194"),
    ("ועדת העבודה",                  "4196"),
    ("הוועדה לביטחון לאומי",         "4187"),
]


@pytest.mark.parametrize("query, expected_id", RESOLVING_QUERIES)
def test_exact_and_prefix_names_fetch_the_intended_committee(query, expected_id):
    fetcher = _RecordingFetcher()
    candidates = name_search(query, fuzzy_index=_committee_index(REAL_COMMITTEES_25),
                             fetch_by_id=fetcher, top_k=5)
    assert fetcher.requested_ids == [expected_id]
    assert candidates[0]["id"] == expected_id
    assert candidates[0]["fetched"] is True
    assert candidates[0]["record"]["members"]
    assert all(not c["fetched"] for c in candidates[1:])


@pytest.mark.parametrize("query", [
    "ועדה",                  # prefix of several "ועדה משותפת ..." labels
    "ועדת המשנה",            # prefix of many subcommittees
    "הוועדה המיוחדת",        # prefix of several special committees
    "כספים",                 # substring only, no label starts with it
])
def test_ambiguous_queries_fetch_nothing(query):
    fetcher = _RecordingFetcher()
    candidates = name_search(query, fuzzy_index=_committee_index(REAL_COMMITTEES_25),
                             fetch_by_id=fetcher, top_k=5)
    assert fetcher.requested_ids == []
    assert candidates
    assert all(not c["fetched"] for c in candidates)


def test_resolved_candidate_outside_score_top_k_is_still_returned_first():
    fetcher = _RecordingFetcher()
    candidates = name_search("ועדת החינוך", fuzzy_index=_committee_index(REAL_COMMITTEES_25),
                             fetch_by_id=fetcher, top_k=1)
    assert [c["id"] for c in candidates] == ["4192"]
    assert candidates[0]["fetched"] is True


def test_without_fetcher_order_is_pure_score():
    candidates = name_search("ועדת החינוך", fuzzy_index=_committee_index(REAL_COMMITTEES_25), top_k=3)
    scores = [c["score"] for c in candidates]
    assert scores == sorted(scores, reverse=True)
    assert all(not c["fetched"] for c in candidates)


def test_failing_fetcher_leaves_candidate_unfetched():
    def failing_fetcher(committee_id):
        raise RuntimeError("network down")
    candidates = name_search("ועדת הכספים", fuzzy_index=_committee_index(REAL_COMMITTEES_25),
                             fetch_by_id=failing_fetcher)
    assert candidates[0]["id"] == "4186"
    assert candidates[0]["fetched"] is False


def test_find_committee_tool_inlines_record(sample_db, monkeypatch):
    from utils import tools
    fetcher = _RecordingFetcher()
    monkeypatch.setattr(tools, "fetch_committee_record", fetcher)
    envelope = tools.handle_find_committee({"query": "ועדת החוקה"})
    payload = json.loads(envelope.full)
    assert fetcher.requested_ids == ["4191"]
    assert payload[0]["committee_id"] == "4191"
    assert payload[0]["record"]["members"]
    assert "low_confidence_match" not in envelope.metadata.get("warnings", [])


@pytest.fixture(scope="module")
def real_committee_index():
    if not Path(config.KNESSET_DB).exists():
        pytest.skip(f"{config.KNESSET_DB} missing")
    from retrieval import knesset_db_store as store
    try:
        conn = sqlite3.connect(f"file:{Path(config.KNESSET_DB).as_posix()}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        entries = store.name_entries(conn, "committees", 25)
        conn.close()
    except sqlite3.Error as exc:
        pytest.skip(f"knesset.db unreadable: {exc}")
    if not entries:
        pytest.skip("committees table empty")
    return FuzzyNameIndex(entries)


@pytest.mark.parametrize("query, expected_id", RESOLVING_QUERIES)
def test_real_db_committee_names_resolve(real_committee_index, query, expected_id):
    fetcher = _RecordingFetcher()
    candidates = name_search(query, fuzzy_index=real_committee_index, fetch_by_id=fetcher)
    assert fetcher.requested_ids == [expected_id]
    assert candidates[0]["id"] == expected_id


@pytest.mark.parametrize("query", ["ועדה", "ועדת המשנה", "הוועדה המיוחדת", "הוועדה המשותפת"])
def test_real_db_ambiguous_names_fetch_nothing(real_committee_index, query):
    fetcher = _RecordingFetcher()
    name_search(query, fuzzy_index=real_committee_index, fetch_by_id=fetcher)
    assert fetcher.requested_ids == []
