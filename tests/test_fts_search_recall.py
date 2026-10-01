"""
tests/test_fts_search_recall.py

Recall / precision of the protocol FTS5 query builder (utils.tools._fts_match and
retrieval.ktiv) on the real Data/knesset.db: FTS metacharacters become token
separators, punctuation does not block ktiv expansion, Hebrew prefix letters are
matched, ktiv expansion only adds/removes a mater lectionis, and the vocab cache
follows the db file. Real-db tests skip when the db is missing or empty.
"""

import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
from retrieval import knesset_db_store as store
from retrieval.ktiv import (
    clear_cache, expand_token, is_ktiv_alternation, prefixed_variants, stripped_prefix_bases,
)
from utils.tools import _expand_match, _fts_match, _quote_match, _safe_match


@pytest.fixture(scope="module")
def real_db():
    if not Path(config.KNESSET_DB).exists():
        pytest.skip(f"{config.KNESSET_DB} missing")
    try:
        conn = sqlite3.connect(f"file:{Path(config.KNESSET_DB).as_posix()}?mode=ro", uri=True, timeout=30)
        has_opinions = conn.execute("SELECT 1 FROM opinions LIMIT 1").fetchone() is not None
    except sqlite3.Error as exc:
        pytest.skip(f"knesset.db unreadable: {exc}")
    if not has_opinions:
        conn.close()
        pytest.skip("opinions table empty (build in progress)")
    yield conn
    conn.close()


def _hit_count(conn, query: str, table: str) -> int:
    match = _fts_match(query, f"{table}_fts")
    assert match, f"empty MATCH expression for {query!r}"
    return conn.execute(f"SELECT count(*) FROM {table}_fts WHERE {table}_fts MATCH ?", (match,)).fetchone()[0]


# ── 1. FTS metacharacters split tokens instead of gluing them ─────────────────

class TestMetacharactersSplitTokens:
    def test_quote_match_turns_gershayim_into_phrase(self):
        assert _quote_match('צה"ל') == '"צה ל"'

    def test_safe_match_turns_hyphen_into_space(self):
        assert _safe_match("חוק-יסוד") == "חוק יסוד"

    def test_only_metacharacters_is_empty(self):
        assert _quote_match('"*()') == ""

    @pytest.mark.parametrize("acronym, table, minimum_hits", [
        ('צה"ל', "topics", 1600),
        ('ש"ס', "opinions", 20),
        ('רה"מ', "opinions", 20),
        ('ח"כ', "opinions", 150),
        ("חוק-יסוד", "topics", 200),
    ])
    def test_acronyms_and_hyphenated_terms_find_rows(self, real_db, acronym, table, minimum_hits):
        assert _hit_count(real_db, acronym, table) >= minimum_hits

    def test_ascii_and_hebrew_gershayim_match_the_same_rows(self, real_db):
        assert _hit_count(real_db, 'צה"ל', "topics") == _hit_count(real_db, "צה״ל", "topics")


# ── 2. punctuation attached to a token does not disable ktiv expansion ───────

class TestPunctuationDoesNotBlockExpansion:
    def test_trailing_comma_still_expands(self, real_db):
        assert '"ביטחון"' in _expand_match("בטחון,", "topics_fts")

    def test_trailing_comma_same_hits_as_bare_token(self, real_db):
        assert _hit_count(real_db, "בטחון,", "opinions") == _hit_count(real_db, "בטחון", "opinions")


# ── 3. Hebrew prefix letters ──────────────────────────────────────────────────

class TestHebrewPrefixRecall:
    @pytest.mark.parametrize("query, table, hits_before_fix, minimum_hits", [
        ("יוקר מחיה", "opinions", 68, 800),
        ("יוקר מחיה", "topics", 2, 250),
        ("גיוס חרדים", "opinions", 273, 600),
        ("גיוס חרדים", "topics", 124, 220),
        ("הפרדת רשויות", "opinions", 53, 120),
        ("הפרדת רשויות", "topics", 2, 35),
        ("חינוך ממלכתי", "opinions", 44, 180),
        ("חינוך ממלכתי", "topics", 17, 75),
    ])
    def test_prefixed_forms_are_found(self, real_db, query, table, hits_before_fix, minimum_hits):
        assert _hit_count(real_db, query, table) >= minimum_hits

    def test_prefixed_variants_exist_in_the_index(self, real_db):
        variants = prefixed_variants("יוקר", config.KNESSET_DB, "opinions_fts")
        assert "ביוקר" in variants
        assert all(v.endswith("יוקר") for v in variants)

    def test_initial_vav_is_doubled_after_a_prefix(self, real_db):
        assert "הוועדה" in prefixed_variants("ועדה", config.KNESSET_DB, "speeches_fts")

    def test_one_and_slot_per_query_word(self, real_db):
        expression = _expand_match("יוקר מחיה", "opinions_fts")
        assert expression.count(" AND ") == 1
        assert '"ביוקר"' in expression and '"המחיה"' in expression
        assert "*" not in expression

    def test_acronym_with_prefix_is_found(self, real_db):
        assert '"בצה ל"' in _fts_match('צה"ל', "speeches_fts")

    def test_two_word_speech_queries_stay_fast(self, real_db):
        expand_token("חינוך", config.KNESSET_DB, "speeches_fts")
        for query in ("יוקר מחיה", "גיוס חרדים", "הפרדת רשויות", "חינוך ממלכתי"):
            started = time.perf_counter()
            match = _fts_match(query, "speeches_fts")
            real_db.execute(
                "SELECT rowid FROM speeches_fts WHERE speeches_fts MATCH ? ORDER BY bm25(speeches_fts) LIMIT 20",
                (match,),
            ).fetchall()
            assert time.perf_counter() - started < 5, query


class TestPrefixedQueryWordRecall:
    def _row_ids(self, conn, query: str, table: str) -> set[int]:
        match = _fts_match(query, f"{table}_fts")
        return {row[0] for row in conn.execute(f"SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ?", (match,))}

    @pytest.mark.parametrize("prefixed_query, bare_query, table", [
        ("המחיה", "מחיה", "opinions"), ("המחיה", "מחיה", "speeches"),
        ("ביוקר המחיה", "יוקר מחיה", "opinions"), ("ביוקר המחיה", "יוקר מחיה", "topics"),
    ])
    def test_prefixed_query_reaches_the_rows_of_the_bare_words(self, real_db, prefixed_query, bare_query, table):
        assert self._row_ids(real_db, bare_query, table) <= self._row_ids(real_db, prefixed_query, table)

    def test_stripped_base_is_added_and_original_kept(self, real_db):
        bases = stripped_prefix_bases("ביוקר", config.KNESSET_DB, "speeches_fts")
        assert bases == ["יוקר"]
        variants = _expand_match("ביוקר", "speeches_fts")
        assert '"ביוקר"' in variants and '"יוקר"' in variants and '"ליוקר"' in variants

    def test_hakneset_still_finds_its_rows(self, real_db):
        assert _hit_count(real_db, "הכנסת", "opinions") >= 9434

    @pytest.mark.parametrize("word, not_a_base", [
        ("מדינה", "דינה"), ("ממשלה", "משלה"), ("משפט", "שפט"), ("בחירות", "חירות"),
        ("מחיה", "חיה"), ("מכתב", "כתב"), ("שמירה", "מירה"), ("ברית", "רית"),
    ])
    def test_root_letters_are_not_stripped(self, real_db, word, not_a_base):
        assert not_a_base not in stripped_prefix_bases(word, config.KNESSET_DB, "speeches_fts")

    def test_short_remainder_is_not_stripped(self, real_db):
        assert stripped_prefix_bases("לחץ", config.KNESSET_DB, "speeches_fts") == []


# ── 4. ktiv expansion precision ───────────────────────────────────────────────

class TestKtivAlternation:
    @pytest.mark.parametrize("first, second", [
        ("בטחון", "ביטחון"), ("חנוך", "חינוך"), ("תכנית", "תוכנית"), ("ענין", "עניין"),
        ("מחיה", "מחייה"), ("תקוה", "תקווה"),
    ])
    def test_true_male_haser_pairs(self, first, second):
        assert is_ktiv_alternation(first, second) and is_ktiv_alternation(second, first)

    @pytest.mark.parametrize("first, second", [
        ("עזה", "עוזיהו"), ("מחיה", "מחה"), ("מחיה", "מחווה"), ("יוקר", "יקר"), ("יוקר", "יקיר"),
        ("גיוס", "גיסי"), ("רשויות", "רשת"), ("רשויות", "רשות"), ("הפרדת", "הפרדות"), ("חינוך", "חניך"),
        ("ממלכתי", "ממלכת"),
    ])
    def test_different_words_are_not_alternations(self, first, second):
        assert not is_ktiv_alternation(first, second)

    @pytest.mark.parametrize("token, unwanted", [
        ("עזה", {"עוזיהו"}), ("מחיה", {"מחה", "מחווה", "מוחה"}), ("יוקר", {"יקר", "יקיר", "יקרו"}),
        ("גיוס", {"גיסי", "גויס"}), ("רשויות", {"רשת", "רשות"}), ("חינוך", {"חניך"}),
    ])
    def test_real_db_expansion_drops_different_words(self, real_db, token, unwanted):
        assert not unwanted & set(expand_token(token, config.KNESSET_DB, "speeches_fts"))

    @pytest.mark.parametrize("token, wanted", [
        ("בטחון", "ביטחון"), ("ביטחון", "בטחון"), ("חינוך", "חנוך"), ("תכנית", "תוכנית"),
    ])
    def test_real_db_expansion_keeps_true_variants(self, real_db, token, wanted):
        assert wanted in expand_token(token, config.KNESSET_DB, "speeches_fts")


# ── 5. vocab cache ────────────────────────────────────────────────────────────

def _write_speeches(path: Path, words: list[str], first_meeting_number: int = 0) -> None:
    conn = store.connect(path)
    rows = [{"meeting_id": f"m{first_meeting_number + i}", "knesset_num": 25, "idx": 0, "speaker": "דובר", "mk_id": None,
             "text": f"דיון {word}"} for i, word in enumerate(words)]
    store.insert_meetings(conn, [{"meeting_id": r["meeting_id"], "knesset_num": 25} for r in rows])
    store.insert_speeches(conn, rows)
    store.rebuild_fts(conn, "speeches")
    conn.close()


class TestVocabCache:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        clear_cache()
        yield
        clear_cache()

    def test_cache_refreshes_after_db_rebuild(self, tmp_path):
        path = tmp_path / "knesset.db"
        _write_speeches(path, ["בטחון"] * 3)
        assert expand_token("בטחון", path, "speeches_fts") == ["בטחון"]
        _write_speeches(path, ["ביטחון"] * 5, first_meeting_number=100)
        later = path.stat().st_mtime + 10
        os.utime(path, (later, later))
        assert "ביטחון" in expand_token("בטחון", path, "speeches_fts")

    def test_load_failure_is_not_cached(self, tmp_path):
        path = tmp_path / "knesset.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE placeholder (x)")
        conn.close()
        mtime_without_fts = path.stat().st_mtime
        assert expand_token("בטחון", path, "speeches_fts") == ["בטחון"]
        _write_speeches(path, ["בטחון"] * 3 + ["ביטחון"] * 5)
        os.utime(path, (mtime_without_fts, mtime_without_fts))
        assert "ביטחון" in expand_token("בטחון", path, "speeches_fts")

    def test_missing_db_is_not_created(self, tmp_path):
        path = tmp_path / "absent.db"
        assert expand_token("בטחון", path, "speeches_fts") == ["בטחון"]
        assert not path.exists()


# ── web reading tab uses the same builder ─────────────────────────────────────

def test_reading_tab_search_uses_the_shared_builder(real_db):
    from web.app import _speech_match_expression
    assert _speech_match_expression("יוקר מחיה") == _fts_match("יוקר מחיה", "speeches_fts")
