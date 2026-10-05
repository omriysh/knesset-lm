"""
tests/test_char_paging.py

Public query_protocols pages whole rows by a character budget (utils.tool_helpers.char_paging), on the real
Data/knesset.db: offset counts rows, a page runs over the budget by at most its last row, following `next`
serves every row of the SQL row list exactly once and in order, an oversize speech (~135K characters) comes
whole, and each scope pages on its own. Plus the paging rules on synthetic rows.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
import utils.tools as tools
from tests.conftest import assert_within_character_budget, handler_payload, ok
from utils.tool_helpers.char_paging import char_budget_page, served_json_chars

COMMON_WORD = "תקציב"
BUDGET_SEARCH_PARAMS = {"q": COMMON_WORD, "committee": "ועדת הכספים", "date_from": "2024-01-01",
                        "date_to": "2024-03-31"}


@pytest.fixture(scope="module")
def longest_speech_meeting(real_conn):
    meeting_id, speech_idx, text_length = real_conn.execute(
        "SELECT s.meeting_id, s.idx, length(s.text) FROM speeches s JOIN meetings m ON m.meeting_id = s.meeting_id "
        "WHERE m.knesset_num = 25 AND (m.is_protocol IS NULL OR m.is_protocol != 0) "
        "ORDER BY length(s.text) DESC LIMIT 1").fetchone()
    assert text_length > config.API_PROTOCOLS_PAGE_CHARS
    speeches_in_order = real_conn.execute("SELECT idx, text FROM speeches WHERE meeting_id = ? ORDER BY idx",
                                          (meeting_id,)).fetchall()
    return meeting_id, speech_idx, speeches_in_order


def follow_next(client, params: dict, scope: str) -> list[list[dict]]:
    """Every page of one scope, following `next` until it is null; checks each page's budget and offset."""
    pages, offset = [], 0
    while True:
        body = ok(client.get("/v1/protocols", params={**params, "search_in": scope, "offset": offset}))
        rows = body["results"][scope]
        assert_within_character_budget(rows, config.API_PROTOCOLS_PAGE_CHARS)
        assert body["truncated"] is False
        assert not any(key.endswith("_chars") for row in rows for key in row)
        pages.append(rows)
        if not body["next"]:
            return pages
        assert body["next"][scope] == {"search_in": [scope], "offset": offset + len(rows)}
        offset += len(rows)


class TestMeetingTranscript:
    def test_following_next_serves_every_speech_once_in_order(self, client, longest_speech_meeting):
        meeting_id, _longest_idx, speeches_in_order = longest_speech_meeting
        pages = follow_next(client, {"meeting_id": meeting_id}, "speeches")
        served = [(row["speech_idx"], row["text"]) for page in pages for row in page]
        assert served == [tuple(speech) for speech in speeches_in_order]
        assert all(row["meeting_id"] == meeting_id and row["date"] for page in pages for row in page)

    def test_the_oversize_speech_is_served_whole_as_the_last_row_of_its_page(self, client, longest_speech_meeting):
        meeting_id, longest_idx, speeches_in_order = longest_speech_meeting
        longest_text = dict(speeches_in_order)[longest_idx]
        for page in follow_next(client, {"meeting_id": meeting_id}, "speeches"):
            if any(row["speech_idx"] == longest_idx for row in page):
                assert page[-1]["speech_idx"] == longest_idx and page[-1]["text"] == longest_text
                assert sum(served_json_chars(row) for row in page) > config.API_PROTOCOLS_PAGE_CHARS
                return
        pytest.fail("the longest speech was never served")


class TestSearchPaging:
    def test_following_next_serves_the_sql_row_list_exactly_once(self, client):
        pages = follow_next(client, BUDGET_SEARCH_PARAMS, "opinions")
        assert len(pages) > 1
        served_keys = [(row["meeting_id"], row["idx"]) for page in pages for row in page]
        sql_keys, row_offset = [], 0
        while True:
            rows = handler_payload(tools.handle_query_protocols, {
                "query": COMMON_WORD, "committees": ["ועדת הכספים"], "date_from": "2024-01-01",
                "date_to": "2024-03-31", "search_in": ["opinions"], "top_k": config.QUERY_PROTOCOLS_MAX_TOP_K,
                "offset": row_offset})["opinions"]
            if not rows:
                break
            sql_keys += [(row["meeting_id"], row["idx"]) for row in rows]
            row_offset += len(rows)
        assert served_keys == sql_keys

    def test_scopes_page_independently(self, client):
        params = {"q": COMMON_WORD}
        together = ok(client.get("/v1/protocols", params={**params, "search_in": "topics,opinions,speeches"}))
        for scope in ("topics", "opinions", "speeches"):
            alone = ok(client.get("/v1/protocols", params={**params, "search_in": scope}))
            assert alone["results"][scope] == together["results"][scope]
            assert alone["next"][scope] == together["next"][scope]
            second_page = ok(client.get("/v1/protocols", params={**params, **together["next"][scope]}))
            assert list(second_page["results"]) == [scope]
            first_keys = {json.dumps(row, sort_keys=True) for row in together["results"][scope]}
            assert not first_keys & {json.dumps(row, sort_keys=True) for row in second_page["results"][scope]}

    def test_offset_past_the_end_is_an_empty_last_page(self, client, real_db):
        body = ok(client.get("/v1/protocols", params={"meeting_id": real_db.meeting_id, "search_in": "topics",
                                                      "offset": config.API_PROTOCOLS_MAX_OFFSET}))
        assert body["results"]["topics"] == [] and body["next"] is None


class TestCharBudgetPageRules:
    def rows(self):
        return [{"id": 1, "text": "a" * 10}, {"id": 2, "text": ""}, {"id": 3, "text": "c" * 600},
                {"id": 4, "text": "e"}]

    def pages(self, budget: int) -> list[list[dict]]:
        pages, offset = [], 0
        while True:
            page, more_rows_follow = char_budget_page(iter(self.rows()[offset:]), budget)
            assert page, "every page advances"
            pages.append(page)
            if not more_rows_follow:
                return pages
            offset += len(page)

    def test_every_budget_serves_every_row_once_on_row_boundaries(self):
        for budget in (1, 40, 100, 700, 5000):
            pages = self.pages(budget)
            assert [row for page in pages for row in page] == self.rows()
            for page in pages:
                assert sum(served_json_chars(row) for row in page[:-1]) < budget

    def test_the_row_that_crosses_the_budget_is_included(self):
        first_row_chars = served_json_chars(self.rows()[0])
        page, more_rows_follow = char_budget_page(iter(self.rows()), first_row_chars + 1)
        assert [row["id"] for row in page] == [1, 2] and more_rows_follow

    def test_an_oversize_row_comes_whole(self):
        page, more_rows_follow = char_budget_page(iter(self.rows()[2:]), 50)
        assert page == [self.rows()[2]] and more_rows_follow

    def test_the_last_page_says_no_more_rows(self):
        assert char_budget_page(iter(self.rows()), 10_000) == (self.rows(), False)
        assert char_budget_page(iter([]), 100) == ([], False)
