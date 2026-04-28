"""Tool-helper subpackage for ``utils/tools.py``.

Holds reusable helpers that the per-tool handler functions in
``utils/tools.py`` lean on. Splitting these out keeps ``tools.py`` itself a
flat function bag (per design §5.2) instead of a tangle of utilities.

Submodules:
  * :mod:`utils.tool_helpers.name_search` — generic discover→BM25→fetch
    helper used by every ``find_*`` tool (per §5.4).
  * :mod:`utils.tool_helpers.adapters` — thin envelope-wrappers around
    existing :mod:`utils.knesset_db` calls.
"""
