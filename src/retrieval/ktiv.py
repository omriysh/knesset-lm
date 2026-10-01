"""
retrieval/ktiv.py

Query-side ktiv male/haser expansion — a pure query rewrite. Nothing about the
stored protocols or the FTS index is modified.

A Hebrew word's ktiv male ("full" spelling) and ktiv haser ("defective"
spelling) differ only by optional *mater lectionis* letters — yod (י) and vav
(ו) — that mark vowels. Stripping those yields a shared consonantal *skeleton*:

    בטחון   → drop interior י/ו → בטחן
    ביטחון  → drop interior י/ו → בטחן      ← same skeleton ⇒ candidate variant

A candidate is kept only when it differs by a single mater (is_ktiv_alternation),
so words that merely share consonants (יוקר/יקיר, מחיה/מחווה) are not OR-ed.

We bucket every token actually present in an FTS index by its skeleton, so at
query time a token can be expanded to exactly the spelling variants that exist
in the corpus (via ``fts5vocab``). Building generates only real, corpus-present
variants — no candidate explosion, no invented tokens.

Usage
-----
    from retrieval.ktiv import expand_token
    expand_token("בטחון", config.KNESSET_DB, "speeches_fts")   # -> ["בטחון", "ביטחון", ...]
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import config

# Mater lectionis letters folded to form the skeleton.
_MATER = frozenset("יו")

_MAX_VARIANTS     = 6      # cap the OR-slot width per query token
_MIN_TOKEN_LEN    = 3      # shorter tokens are too ambiguous to expand
_MIN_VARIANT_DOCS = 2      # absolute floor: drop hapax variants (likely typos)
_RARE_RATIO       = 0.01   # drop a variant far rarer than its bucket's dominant
                           # spelling — kills yod↔vav minimal-pair collisions
                           # (e.g. המדינה vs the typo המודנה) that share a skeleton
_HEB_RE = re.compile(r"[א-ת]")

_MIN_HASER_LEN    = 4      # 3-letter haser forms (יקר, מחה, דין) are other words too often


@dataclass
class _VocabIndex:
    db_mtime_ns: int
    doc_frequency_by_term: dict[str, int]
    terms_by_skeleton: dict[str, list[tuple[str, int]]]


# (db path, fts table) -> vocab of that index as of db_mtime_ns
_vocab_cache: dict[tuple[str, str], _VocabIndex] = {}


def skeleton(token: str) -> str:
    """Consonantal skeleton: drop interior mater letters (yod/vav).

    The first and last characters are kept: an initial yod/vav is usually
    consonantal, and a final one is a suffix (גיסי, עוזיהו), not a mater.
    Tokens sharing a skeleton are only candidates; is_ktiv_alternation decides.
    """
    if len(token) <= 2:
        return token
    return token[0] + "".join(ch for ch in token[1:-1] if ch not in _MATER) + token[-1]


def is_ktiv_alternation(first: str, second: str) -> bool:
    """True when one spelling is the other plus a single mater (י or ו) between two
    letters that are neither the first nor the last one: בטחון/ביטחון, ענין/עניין.
    Not before the last letter, where it marks a different form (הפרדת/הפרדות,
    תיקן/תיקון), and never when the shorter spelling has under _MIN_HASER_LEN letters."""
    shorter, longer = sorted((first, second), key=len)
    if len(longer) != len(shorter) + 1 or len(shorter) < _MIN_HASER_LEN:
        return False
    return any(
        longer == shorter[:insert_at] + mater + shorter[insert_at:]
        for insert_at in range(1, len(shorter) - 1)
        for mater in _MATER
    )


def _load_vocab(db_path: Path, fts_table: str) -> list[tuple[str, int]] | None:
    """``(term, doc_freq)`` for every term indexed in fts_table via fts5vocab; None on failure."""
    try:
        con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    except Exception as exc:
        print(f"[ktiv] cannot open {db_path}: {exc}")
        return None
    try:
        # 3-arg form: the vocab table lives in `temp` but its source table is
        # in `main`, so the source schema must be named explicitly.
        con.execute(
            f"CREATE VIRTUAL TABLE temp.ktiv_vocab USING fts5vocab('main', '{fts_table}', 'row')"
        )
        return list(con.execute("SELECT term, doc FROM temp.ktiv_vocab"))
    except Exception as exc:
        print(f"[ktiv] vocab load failed for {db_path}: {exc}")
        return None
    finally:
        con.close()


def _vocab_index(db_path: Path, fts_table: str) -> _VocabIndex | None:
    """The index's vocab, reloaded whenever the db file's mtime changes. Failures are not cached."""
    try:
        db_mtime_ns = Path(db_path).stat().st_mtime_ns
    except OSError as exc:
        print(f"[ktiv] no vocab for {db_path}: {exc}")
        return None
    key = (str(db_path), fts_table)
    cached = _vocab_cache.get(key)
    if cached is not None and cached.db_mtime_ns == db_mtime_ns:
        return cached
    vocab = _load_vocab(db_path, fts_table)
    if vocab is None:
        return None
    terms_by_skeleton: dict[str, list[tuple[str, int]]] = {}
    for term, doc in vocab:
        if len(term) >= _MIN_TOKEN_LEN and _HEB_RE.search(term):
            terms_by_skeleton.setdefault(skeleton(term), []).append((term, doc))
    index = _VocabIndex(db_mtime_ns, dict(vocab), terms_by_skeleton)
    _vocab_cache[key] = index
    return index


def expand_token(token: str, db_path: Path, fts_table: str) -> list[str]:
    """Return corpus spelling-variants of *token* (the original always first).

    Only Hebrew tokens of length >= _MIN_TOKEN_LEN are expanded; everything
    else is returned unchanged. Variants are the other real index tokens that
    differ from *token* by one mater lectionis (is_ktiv_alternation), ordered by
    corpus frequency and capped at _MAX_VARIANTS. A variant is dropped when it is
    far rarer than the most frequent spelling (a hapax / typo). The original is
    always kept even when absent from the corpus — the whole point of expansion:
    a query in ktiv haser can match a corpus in male.
    """
    if len(token) < _MIN_TOKEN_LEN or not _HEB_RE.search(token):
        return [token]
    index = _vocab_index(db_path, fts_table)
    if index is None:
        return [token]
    bucket = index.terms_by_skeleton.get(skeleton(token))
    if not bucket:
        return [token]
    bucket_max = max(doc for _, doc in bucket)
    floor = max(_MIN_VARIANT_DOCS, _RARE_RATIO * bucket_max)
    variants = [token]
    for term, doc in sorted(bucket, key=lambda td: td[1], reverse=True):
        if term == token or doc < floor or not is_ktiv_alternation(token, term):
            continue
        variants.append(term)
        if len(variants) >= _MAX_VARIANTS:
            break
    return variants


def prefixed_variants(word: str, db_path: Path, fts_table: str,
                      prefixes: tuple[str, ...] | None = None, limit: int | None = None) -> list[str]:
    """Index tokens that are *word* behind a Hebrew prefix (ביוקר, הוועדה), most frequent first.

    FTS5 prefix queries match the end of a token, so the prefixed forms are listed
    explicitly, keeping only ones indexed in at least _MIN_VARIANT_DOCS rows. A word
    starting with vav also gets the doubled-vav form (ועדה -> הוועדה).
    """
    prefixes = config.FTS_HEBREW_PREFIXES if prefixes is None else prefixes
    limit = config.FTS_MAX_PREFIXED_VARIANTS_PER_WORD if limit is None else limit
    if not word or not _HEB_RE.search(word):
        return []
    index = _vocab_index(db_path, fts_table)
    if index is None:
        return []
    word_forms = [word, "ו" + word] if word.startswith("ו") else [word]
    doc_frequency_by_variant = {
        prefix + form: index.doc_frequency_by_term.get(prefix + form, 0)
        for prefix in prefixes for form in word_forms
    }
    found = [(variant, doc) for variant, doc in doc_frequency_by_variant.items() if doc >= _MIN_VARIANT_DOCS]
    found.sort(key=lambda variant_doc: variant_doc[1], reverse=True)
    return [variant for variant, _ in found[:limit]]


def stripped_prefix_bases(word: str, db_path: Path, fts_table: str) -> list[str]:
    """Bases of *word* with a leading Hebrew prefix removed (המחיה -> מחיה, והמחיה -> המחיה, מחיה).

    A base is kept only when it has at least FTS_MIN_PREFIXED_WORD_CHARS letters and is
    a common index token: at least FTS_MIN_STRIPPED_BASE_DOCS rows and
    FTS_MIN_STRIPPED_BASE_DOC_RATIO of *word*'s own rows. That rejects roots whose first
    letter only looks like a prefix (מדינה -> דינה, משפט -> שפט). A word that is itself
    common with a definite article (מחיה, since המחיה is common) is a bare word and is not
    stripped (מחיה -> חיה). Shortest base first.
    """
    if not word or not _HEB_RE.search(word):
        return []
    index = _vocab_index(db_path, fts_table)
    if index is None:
        return []
    word_doc_frequency = index.doc_frequency_by_term.get(word, 0)
    minimum_base_docs = max(config.FTS_MIN_STRIPPED_BASE_DOCS,
                            config.FTS_MIN_STRIPPED_BASE_DOC_RATIO * word_doc_frequency)
    if index.doc_frequency_by_term.get("ה" + word, 0) >= minimum_base_docs:
        return []
    bases = {
        word[len(prefix):]
        for prefix in config.FTS_HEBREW_PREFIXES
        if word.startswith(prefix) and len(word) - len(prefix) >= config.FTS_MIN_PREFIXED_WORD_CHARS
    }
    return sorted(
        (base for base in bases if index.doc_frequency_by_term.get(base, 0) >= minimum_base_docs),
        key=len,
    )


def clear_cache() -> None:
    """Drop the cached vocab (it also reloads by itself when the db file changes)."""
    _vocab_cache.clear()
