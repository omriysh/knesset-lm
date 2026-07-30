"""
retrieval/ktiv.py

Query-side ktiv male/haser expansion — a pure query rewrite. Nothing about the
stored protocols or the FTS index is modified.

A Hebrew word's ktiv male ("full" spelling) and ktiv haser ("defective"
spelling) differ only by optional *mater lectionis* letters — yod (י) and vav
(ו) — that mark vowels. Stripping those yields a shared consonantal *skeleton*:

    בטחון   → drop interior י/ו → בטחן
    ביטחון  → drop interior י/ו → בטחן      ← same skeleton ⇒ genuine variant

We bucket every token actually present in an FTS index by its skeleton, so at
query time a token can be expanded to exactly the spelling variants that exist
in the corpus (via ``fts5vocab``). Building generates only real, corpus-present
variants — no candidate explosion, no invented tokens.

Usage
-----
    from retrieval.ktiv import expand_token
    expand_token("בטחון", speeches_db_path)   # -> ["בטחון", "ביטחון", ...]
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

# Mater lectionis letters folded to form the skeleton.
_MATER = frozenset("יו")

_MAX_VARIANTS     = 6      # cap the OR-slot width per query token
_MIN_TOKEN_LEN    = 3      # shorter tokens are too ambiguous to expand
_MIN_VARIANT_DOCS = 2      # absolute floor: drop hapax variants (likely typos)
_RARE_RATIO       = 0.01   # drop a variant far rarer than its bucket's dominant
                           # spelling — kills yod↔vav minimal-pair collisions
                           # (e.g. המדינה vs the typo המודנה) that share a skeleton
_HEB_RE = re.compile(r"[א-ת]")

# db path (str) -> {skeleton: [(token, doc_freq), ...]}
_bucket_cache: dict[str, dict[str, list[tuple[str, int]]]] = {}


def skeleton(token: str) -> str:
    """Consonantal skeleton: drop interior mater letters (yod/vav).

    The first character is kept untouched so an initial yod/vav — which is
    usually consonantal, not a mater — does not collapse the word.
    """
    if len(token) <= 1:
        return token
    return token[0] + "".join(ch for ch in token[1:] if ch not in _MATER)


def _load_vocab(db_path: Path) -> list[tuple[str, int]]:
    """Return ``(term, doc_freq)`` for every indexed term via fts5vocab."""
    try:
        con = sqlite3.connect(str(db_path))
    except Exception as exc:
        print(f"[ktiv] cannot open {db_path}: {exc}")
        return []
    try:
        # 3-arg form: the vocab table lives in `temp` but its source `entries`
        # table is in `main`, so the source schema must be named explicitly.
        con.execute(
            "CREATE VIRTUAL TABLE temp.ktiv_vocab USING fts5vocab('main', 'entries', 'row')"
        )
        return list(con.execute("SELECT term, doc FROM temp.ktiv_vocab"))
    except Exception as exc:
        print(f"[ktiv] vocab load failed for {db_path}: {exc}")
        return []
    finally:
        con.close()


def _buckets(db_path: Path) -> dict[str, list[tuple[str, int]]]:
    """Skeleton -> ``[(token, doc_freq), ...]`` for an index, built once + cached."""
    key = str(db_path)
    cached = _bucket_cache.get(key)
    if cached is not None:
        return cached
    buckets: dict[str, list[tuple[str, int]]] = {}
    for term, doc in _load_vocab(db_path):
        if len(term) < _MIN_TOKEN_LEN or not _HEB_RE.search(term):
            continue
        buckets.setdefault(skeleton(term), []).append((term, doc))
    _bucket_cache[key] = buckets
    return buckets


def expand_token(token: str, db_path: Path) -> list[str]:
    """Return corpus spelling-variants of *token* (the original always first).

    Only Hebrew tokens of length >= _MIN_TOKEN_LEN are expanded; everything
    else is returned unchanged. Variants are the other real index tokens that
    share *token*'s consonantal skeleton, ordered by corpus frequency and capped
    at _MAX_VARIANTS. A variant is dropped when it is far rarer than the bucket's
    dominant spelling (a hapax / yod↔vav minimal-pair collision, not a real ktiv
    variant). The original is always kept even when absent from the corpus — the
    whole point of expansion: a query in ktiv haser can match a corpus in male.
    """
    if len(token) < _MIN_TOKEN_LEN or not _HEB_RE.search(token):
        return [token]
    bucket = _buckets(db_path).get(skeleton(token))
    if not bucket:
        return [token]
    bucket_max = max(doc for _, doc in bucket)
    floor = max(_MIN_VARIANT_DOCS, _RARE_RATIO * bucket_max)
    variants = [token]
    for term, doc in sorted(bucket, key=lambda td: td[1], reverse=True):
        if term == token or doc < floor:
            continue
        variants.append(term)
        if len(variants) >= _MAX_VARIANTS:
            break
    return variants


def clear_cache() -> None:
    """Drop cached skeleton buckets (e.g. after an index rebuild)."""
    _bucket_cache.clear()
