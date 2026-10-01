"""
tests/test_exact_match_ranking.py

Ranking of protocol FTS results on the real Data/knesset.db: the OR-slot per query word also matches
ktiv spellings, prefixed forms and stripped bases, so "ביוקר" ("expensively") also finds "ביקר"
("visited"). Rows matching the words as typed must rank above rows found only through those
variants, with bm25 inside each tier: in query_protocols (agent rows and public character pages),
the web reading tab's meeting search and its per-meeting heatmap.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import utils.tools as tools
from retrieval import knesset_db_store as store
from tests.conftest import handler_payload, ok

TYPED_WORD = "ביוקר"
VARIANT_ONLY_WORD = "ביקר"
SEARCHED_TEXT_FIELDS_BY_SCOPE = {"topics": ("topic",), "opinions": ("opinion", "quote"), "speeches": ("text",)}


def row_tokens(scope: str, row: dict) -> set[str]:
    text = " ".join(row.get(field_name) or "" for field_name in SEARCHED_TEXT_FIELDS_BY_SCOPE[scope])
    return set(tools._fts_token_parts(text))


def assert_typed_word_rows_first(scope: str, rows: list[dict]) -> None:
    has_typed_word = [TYPED_WORD in row_tokens(scope, row) for row in rows]
    variant_only_positions = [position for position, row in enumerate(rows)
                              if not has_typed_word[position] and VARIANT_ONLY_WORD in row_tokens(scope, row)]
    typed_word_positions = [position for position, typed in enumerate(has_typed_word) if typed]
    assert typed_word_positions and variant_only_positions
    assert max(typed_word_positions) < min(variant_only_positions)


@pytest.mark.parametrize("scope", ["opinions", "speeches"])
def test_every_typed_word_row_ranks_before_variant_only_rows(real_db, real_conn, scope):
    fts_table = f"{scope}_fts"
    rows = list(store.iter_protocol_rows(real_conn, scope, 25, match=tools._fts_match(TYPED_WORD, fts_table),
                                         exact_match=tools._fts_exact_match(TYPED_WORD)))
    assert VARIANT_ONLY_WORD in tools._fts_match(TYPED_WORD, fts_table)
    assert_typed_word_rows_first(scope, rows)


def test_bm25_orders_rows_within_each_tier(real_db, real_conn):
    rows = list(store.iter_protocol_rows(real_conn, "opinions", 25, match=tools._fts_match(TYPED_WORD, "opinions_fts"),
                                         exact_match=tools._fts_exact_match(TYPED_WORD)))
    typed_word_count = sum(TYPED_WORD in row_tokens("opinions", row) for row in rows)
    for tier_rows in (rows[:typed_word_count], rows[typed_word_count:]):
        scores = [row["score"] for row in tier_rows]
        assert scores == sorted(scores)


def test_agent_rows_rank_the_typed_word_first(real_db, real_conn):
    ranked = store.iter_protocol_rows(real_conn, "speeches", 25, match=tools._fts_match(TYPED_WORD, "speeches_fts"),
                                      exact_match=tools._fts_exact_match(TYPED_WORD))
    typed_word_rows = sum(TYPED_WORD in row_tokens("speeches", row) for row in ranked)
    rows = handler_payload(tools.handle_query_protocols, {"query": TYPED_WORD, "search_in": ["speeches"], "top_k": 200,
                                                          "offset": max(0, typed_word_rows - 100)})["speeches"]
    assert_typed_word_rows_first("speeches", rows)


def test_public_pages_rank_the_typed_word_first(client):
    first_page = ok(client.get("/v1/protocols", params={"q": TYPED_WORD, "search_in": "opinions"}))
    assert all(TYPED_WORD in row_tokens("opinions", row) for row in first_page["results"]["opinions"])


def test_listing_order_is_unchanged_by_tiers(real_db, real_conn):
    listed = store.query_protocol_rows(real_conn, "speeches", 25, meeting_ids=[real_db.meeting_id], top_k=30,
                                       exact_match=tools._fts_exact_match(TYPED_WORD))
    assert [row["speech_idx"] for row in listed] == sorted(row["speech_idx"] for row in listed)
