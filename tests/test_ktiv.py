"""
test_ktiv.py

Tests for retrieval.ktiv — query-side ktiv male/haser expansion, and its
wiring into search_speeches_bm25 (a query in one spelling matches a corpus
written in the other, with no change to the index/content).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from retrieval.ktiv import clear_cache, expand_token, skeleton
import config
from retrieval import knesset_db_store as store
from utils.tools import _expand_match, search_speeches


# ── skeleton (no db) ──────────────────────────────────────────────────────────

class TestSkeleton:
    def test_male_and_haser_share_skeleton(self):
        assert skeleton("ביטחון") == skeleton("בטחון")

    def test_first_letter_kept(self):
        # Leading yod/vav is usually consonantal — must not be dropped.
        assert skeleton("ירושלים").startswith("י")

    def test_interior_maters_dropped(self):
        assert skeleton("ביטחון") == "בטחן"

    def test_short_token_returned_asis(self):
        assert skeleton("א") == "א"


# ── expand_token (fixture db) ─────────────────────────────────────────────────

def _build_vocab_db(path, term_counts: dict) -> Path:
    """Build a knesset.db where each term appears in `count` speeches (so its
    fts5vocab doc-frequency equals `count`)."""
    conn = store.connect(path)
    rows, rid = [], 0
    for term, count in term_counts.items():
        for _ in range(count):
            rows.append({"meeting_id": f"m{rid}", "knesset_num": 25, "idx": 0, "speaker": "דובר",
                         "mk_id": None, "text": f"דיון בנושא {term} בוועדה"})
            rid += 1
    store.insert_meetings(conn, [{"meeting_id": r["meeting_id"], "knesset_num": 25} for r in rows])
    store.insert_speeches(conn, rows)
    store.rebuild_fts(conn, "speeches")
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _clear_ktiv_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "knesset.db"
    monkeypatch.setattr(config, "KNESSET_DB", path)
    return path


class TestExpandToken:
    def test_haser_expands_to_male(self, db):
        _build_vocab_db(db, {"ביטחון": 5, "בטחון": 3})
        variants = expand_token("בטחון", db, "speeches_fts")
        assert variants[0] == "בטחון"          # original always first
        assert "ביטחון" in variants

    def test_male_expands_to_haser(self, db):
        _build_vocab_db(db, {"ביטחון": 5, "בטחון": 3})
        assert "בטחון" in expand_token("ביטחון", db, "speeches_fts")

    def test_rare_collision_variant_is_dropped(self, db):
        # המודנה shares a skeleton with המדינה but is a hapax typo — must not
        # be OR-ed into the query.
        _build_vocab_db(db, {"המדינה": 6, "המודנה": 1})
        assert expand_token("המדינה", db, "speeches_fts") == ["המדינה"]

    def test_short_token_not_expanded(self, db):
        _build_vocab_db(db, {"ביטחון": 5})
        assert expand_token("של", db, "speeches_fts") == ["של"]

    def test_non_hebrew_token_not_expanded(self, db):
        _build_vocab_db(db, {"ביטחון": 5})
        assert expand_token("budget2024", db, "speeches_fts") == ["budget2024"]

    def test_original_kept_when_absent_from_corpus(self, db):
        # Corpus has only the male form; a haser query must still return the
        # original plus the male variant.
        _build_vocab_db(db, {"ביטחון": 5})
        variants = expand_token("בטחון", db, "speeches_fts")
        assert variants[0] == "בטחון" and "ביטחון" in variants


# ── _expand_match (MATCH expression shape) ────────────────────────────────────

class TestExpandMatch:
    def test_builds_or_slot_for_expanded_token(self, db):
        _build_vocab_db(db, {"ביטחון": 5, "בטחון": 3})
        expr = _expand_match("בטחון המדינה", "speeches_fts")
        assert '"בטחון"' in expr and '"ביטחון"' in expr
        assert " OR " in expr

    def test_single_variant_token_has_no_or(self, db):
        _build_vocab_db(db, {"תקציב": 4})
        assert _expand_match("תקציב", "speeches_fts") == '"תקציב"'


# ── end-to-end via search_speeches ────────────────────────────────────────────

class TestSearchWithKtivExpansion:
    def test_haser_query_finds_male_spelled_speeches(self, db):
        _build_vocab_db(db, {"ביטחון": 5, "בטחון": 3})
        conn = store.connect(db)
        try:
            # Query in ktiv haser must find the 5 male-spelled rows too.
            rows = search_speeches(conn, "בטחון", top_k=200)
        finally:
            conn.close()
        texts = [r["text"] for r in rows]
        assert any("ביטחון" in t for t in texts)
        assert len(rows) == 8   # 5 male + 3 haser
