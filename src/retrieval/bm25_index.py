"""
retrieval/bm25_index.py

Thin SQLite FTS5 wrapper used by the BM25 retrieval layer.

All six index databases (bullets, speeches, mks, committees, bills, votes)
share the same virtual-table schema defined in §8.1 of the design.

Usage
-----
    from retrieval.bm25_index import BM25Index
    idx = BM25Index(path)
    idx.create_table(force_rebuild=False)
    idx.insert_many(rows)           # each row is a dict with the schema keys
    results = idx.search("query", top_k=20)
    idx.close()
"""

import json
import sqlite3
from pathlib import Path
from typing import Iterable

# ── Schema ────────────────────────────────────────────────────────────────────

_CREATE_FTS5 = """
CREATE VIRTUAL TABLE entries USING fts5(
    id UNINDEXED,
    label,
    label_lemmatized,
    body,
    body_lemmatized,
    extra UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
"""

_DROP_FTS5 = "DROP TABLE IF EXISTS entries;"

# All searchable (non-UNINDEXED) columns for the default MATCH target
_SEARCH_COLUMNS = ("label_lemmatized", "body_lemmatized")

# ── BM25Index ─────────────────────────────────────────────────────────────────


class BM25Index:
    """SQLite FTS5-backed BM25 index for a single target corpus."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._con: sqlite3.Connection | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        if self._con is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._con = sqlite3.connect(str(self._path))
            self._con.row_factory = sqlite3.Row
        return self._con

    def create_table(self, force_rebuild: bool = False) -> None:
        """
        Create the FTS5 virtual table.

        Parameters
        ----------
        force_rebuild
            If True, drop the existing table first (full rebuild).
            If False, the operation is idempotent — the table is only
            created when it does not exist yet.
        """
        con = self._connect()
        if force_rebuild:
            con.execute(_DROP_FTS5)
        # FTS5 virtual tables don't support IF NOT EXISTS — check manually
        exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entries';"
        ).fetchone()
        if not exists:
            con.execute(_CREATE_FTS5)
        con.commit()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # ── write ─────────────────────────────────────────────────────────────────

    def insert_many(self, rows: Iterable[dict]) -> int:
        """
        Batch-insert rows into the index.

        Each row must be a dict with keys:
            id, label, label_lemmatized, body, body_lemmatized, extra

        *extra* may be a dict (it is JSON-serialized automatically) or a
        string.  Missing keys default to empty string / empty JSON object.

        Returns the number of rows inserted.
        """
        con = self._connect()
        count = 0
        batch: list[tuple] = []
        BATCH = 500

        for row in rows:
            extra = row.get("extra", {})
            if isinstance(extra, dict):
                extra = json.dumps(extra, ensure_ascii=False)
            batch.append((
                str(row.get("id", "")),
                str(row.get("label", "")),
                str(row.get("label_lemmatized", "")),
                str(row.get("body", "")),
                str(row.get("body_lemmatized", "")),
                extra,
            ))
            if len(batch) >= BATCH:
                con.executemany(
                    "INSERT INTO entries(id,label,label_lemmatized,body,body_lemmatized,extra) VALUES (?,?,?,?,?,?)",
                    batch,
                )
                count += len(batch)
                batch = []

        if batch:
            con.executemany(
                "INSERT INTO entries(id,label,label_lemmatized,body,body_lemmatized,extra) VALUES (?,?,?,?,?,?)",
                batch,
            )
            count += len(batch)

        con.commit()
        return count

    # ── read ──────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 20,
        where: str | None = None,
    ) -> list[dict]:
        """
        Full-text search using FTS5 MATCH.

        Parameters
        ----------
        query
            FTS5 match expression (plain text is fine; the caller may also
            pass quoted phrases or column filters like ``label:word``).
        top_k
            Maximum number of results to return, ranked by BM25 score.
        where
            Optional extra SQL WHERE clause fragment applied after the MATCH
            filter (e.g. ``"extra LIKE '%committee_id: 5%'"``).  The fragment
            must not start with ``WHERE``.

        Returns a list of dicts with keys:
            id, label, label_lemmatized, body, body_lemmatized, extra, score
        where *extra* is already parsed back to a dict (if valid JSON).
        """
        con = self._connect()
        sql = (
            "SELECT id, label, label_lemmatized, body, body_lemmatized, extra,"
            "       bm25(entries) AS score"
            "  FROM entries"
            " WHERE entries MATCH ?"
        )
        params: list = [query]
        if where:
            sql += f" AND ({where})"
        sql += " ORDER BY score LIMIT ?"
        params.append(top_k)

        cursor = con.execute(sql, params)
        results = []
        for row in cursor.fetchall():
            d = dict(row)
            # Parse extra back to dict when possible
            try:
                d["extra"] = json.loads(d["extra"])
            except (json.JSONDecodeError, TypeError):
                pass
            results.append(d)
        return results

    # ── context manager support ───────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
