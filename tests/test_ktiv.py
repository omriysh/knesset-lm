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

from retrieval.bm25_index import BM25Index
from retrieval.ktiv import clear_cache, expand_token, skeleton
from utils.tools import _expand_match, search_speeches_bm25


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

def _build_vocab_db(path, term_counts: dict) -> BM25Index:
    """Build a speeches-shaped index where each term appears in `count` rows
    (so its fts5vocab doc-frequency equals `count`)."""
    idx = BM25Index(path)
    idx.create_table()
    rows, rid = [], 0
    for term, count in term_counts.items():
        for _ in range(count):
            rows.append({
                "id": f"r{rid}", "label": "", "label_lemmatized": "",
                "body": "", "body_lemmatized": f"דיון בנושא {term} בוועדה",
                "extra": {"meeting_id": f"m{rid}", "committee": "ועדה",
                          "speech_idx": 0, "speaker": "דובר"},
            })
            rid += 1
    idx.insert_many(rows)
    return idx


@pytest.fixture(autouse=True)
def _clear_ktiv_cache():
    clear_cache()
    yield
    clear_cache()


class TestExpandToken:
    def test_haser_expands_to_male(self, tmp_path):
        db = tmp_path / "speeches.db"
        idx = _build_vocab_db(db, {"ביטחון": 5, "בטחון": 3})
        idx.close()
        variants = expand_token("בטחון", db)
        assert variants[0] == "בטחון"          # original always first
        assert "ביטחון" in variants

    def test_male_expands_to_haser(self, tmp_path):
        db = tmp_path / "speeches.db"
        idx = _build_vocab_db(db, {"ביטחון": 5, "בטחון": 3})
        idx.close()
        assert "בטחון" in expand_token("ביטחון", db)

    def test_rare_collision_variant_is_dropped(self, tmp_path):
        # המודנה shares a skeleton with המדינה but is a hapax typo — must not
        # be OR-ed into the query.
        db = tmp_path / "speeches.db"
        idx = _build_vocab_db(db, {"המדינה": 6, "המודנה": 1})
        idx.close()
        assert expand_token("המדינה", db) == ["המדינה"]

    def test_short_token_not_expanded(self, tmp_path):
        db = tmp_path / "speeches.db"
        idx = _build_vocab_db(db, {"ביטחון": 5})
        idx.close()
        assert expand_token("של", db) == ["של"]

    def test_non_hebrew_token_not_expanded(self, tmp_path):
        db = tmp_path / "speeches.db"
        idx = _build_vocab_db(db, {"ביטחון": 5})
        idx.close()
        assert expand_token("budget2024", db) == ["budget2024"]

    def test_original_kept_when_absent_from_corpus(self, tmp_path):
        # Corpus has only the male form; a haser query must still return the
        # original plus the male variant.
        db = tmp_path / "speeches.db"
        idx = _build_vocab_db(db, {"ביטחון": 5})
        idx.close()
        variants = expand_token("בטחון", db)
        assert variants[0] == "בטחון" and "ביטחון" in variants


# ── _expand_match (MATCH expression shape) ────────────────────────────────────

class TestExpandMatch:
    def test_builds_or_slot_for_expanded_token(self, tmp_path):
        db = tmp_path / "speeches.db"
        idx = _build_vocab_db(db, {"ביטחון": 5, "בטחון": 3})
        idx.close()
        expr = _expand_match("בטחון המדינה", db)
        assert '"בטחון"' in expr and '"ביטחון"' in expr
        assert " OR " in expr

    def test_single_variant_token_has_no_or(self, tmp_path):
        db = tmp_path / "speeches.db"
        idx = _build_vocab_db(db, {"תקציב": 4})
        idx.close()
        assert _expand_match("תקציב", db) == '"תקציב"'


# ── end-to-end via search_speeches_bm25 ───────────────────────────────────────

class TestSearchWithKtivExpansion:
    def test_haser_query_finds_male_spelled_speeches(self, tmp_path):
        db = tmp_path / "speeches.db"
        idx = _build_vocab_db(db, {"ביטחון": 5, "בטחון": 3})
        try:
            # Query in ktiv haser must find the 5 male-spelled rows too.
            rows = search_speeches_bm25(idx, "בטחון", top_k=200)
        finally:
            idx.close()
        bodies = [r["body_lemmatized"] for r in rows]
        assert any("ביטחון" in b for b in bodies)
        assert len(rows) == 8   # 5 male + 3 haser
