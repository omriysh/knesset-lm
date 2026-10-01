"""
Character-budget pages of whole rows (public query_protocols).

`offset` counts rows of a scope's ranked stream. A page takes whole rows from `offset` in order while
the running total of their served JSON is below the budget; the row that crosses the budget is still
included, so a page exceeds the budget by at most one row and an oversize row is served whole. Rows are
never split, cut or deferred to the next page.
"""

import json
from collections.abc import Iterable

JSON_LIST_ITEM_SEPARATOR_CHARS = len(", ")


def served_json_chars(row: dict) -> int:
    """The row's share of its page's JSON list: the row plus one item separator (the list's two
    brackets take the place of the last row's separator)."""
    return len(json.dumps(row, ensure_ascii=False)) + JSON_LIST_ITEM_SEPARATOR_CHARS


def char_budget_page(rows_from_offset: Iterable[dict], budget: int) -> tuple[list[dict], bool]:
    """(page rows, whether more rows follow the page) for the rows starting at the page's offset."""
    page: list[dict] = []
    used_chars = 0
    for row in rows_from_offset:
        if page and used_chars >= budget:
            return page, True
        page.append(row)
        used_chars += served_json_chars(row)
    return page, False
