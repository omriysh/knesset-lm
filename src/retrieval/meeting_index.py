"""
retrieval/meeting_index.py

Plain relational SQLite index for structural (committee / date / participant)
meeting filtering — the reading-tab prefilter used by web/app.py::browse_rag().

Built offline by scripts/build_meeting_index.py into
``Data/bm25/<knesset_num>/meeting_index.db`` (same directory convention as
the BM25 FTS5 indexes — see config.BM25_DIR / scripts/build_bm25_indexes.py).

This is intentionally NOT an FTS5 table: the filters here need exact /
range matching (committee name, ISO date range, mk_id/party membership),
not text ranking, so a plain relational schema is the right tool.

One connection = one query per search call (mirrors how BM25Index is used
today — no in-memory index kept across requests).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import config

_CREATE_MEETINGS = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id  TEXT PRIMARY KEY,
    committee   TEXT,
    date        TEXT,       -- ISO yyyy-mm-dd
    knesset_num INTEGER
);
"""

_CREATE_PARTICIPANTS = """
CREATE TABLE IF NOT EXISTS meeting_participants (
    meeting_id TEXT,
    mk_id      TEXT,
    party      TEXT,
    FOREIGN KEY(meeting_id) REFERENCES meetings(meeting_id)
);
"""

_CREATE_GUESTS = """
CREATE TABLE IF NOT EXISTS meeting_guests (
    meeting_id TEXT,
    name       TEXT,
    FOREIGN KEY(meeting_id) REFERENCES meetings(meeting_id)
);
"""

_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_meetings_committee ON meetings(committee);",
    "CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(date);",
    "CREATE INDEX IF NOT EXISTS idx_participants_mk ON meeting_participants(mk_id);",
    "CREATE INDEX IF NOT EXISTS idx_participants_party ON meeting_participants(party);",
    "CREATE INDEX IF NOT EXISTS idx_participants_meeting ON meeting_participants(meeting_id);",
    # No index on meeting_guests.name: SQLite LIKE '%x%' (substring match, used
    # by the guest-name filter below) can't use a plain index anyway.
    "CREATE INDEX IF NOT EXISTS idx_guests_meeting ON meeting_guests(meeting_id);",
)

_BATCH = 500


def db_path(knesset_num: int) -> Path:
    """Path layout matches scripts/build_bm25_indexes.py::_db_path()."""
    return config.BM25_DIR / str(knesset_num) / "meeting_index.db"


def create_tables(conn: sqlite3.Connection, force_rebuild: bool = False) -> None:
    """Create the three tables (+ indexes) if missing. force_rebuild drops first."""
    if force_rebuild:
        conn.execute("DROP TABLE IF EXISTS meeting_guests;")
        conn.execute("DROP TABLE IF EXISTS meeting_participants;")
        conn.execute("DROP TABLE IF EXISTS meetings;")
    conn.execute(_CREATE_MEETINGS)
    conn.execute(_CREATE_PARTICIPANTS)
    conn.execute(_CREATE_GUESTS)
    for stmt in _CREATE_INDEXES:
        conn.execute(stmt)
    conn.commit()


def insert_meetings(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Batch-insert into ``meetings``. Each row: {meeting_id, committee, date, knesset_num}."""
    count = 0
    batch: list[tuple] = []
    for row in rows:
        batch.append((
            str(row["meeting_id"]),
            row.get("committee", "") or "",
            row.get("date", "") or "",
            int(row.get("knesset_num") or 0),
        ))
        if len(batch) >= _BATCH:
            conn.executemany(
                "INSERT OR REPLACE INTO meetings(meeting_id,committee,date,knesset_num) "
                "VALUES (?,?,?,?)",
                batch,
            )
            count += len(batch)
            batch = []
    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO meetings(meeting_id,committee,date,knesset_num) "
            "VALUES (?,?,?,?)",
            batch,
        )
        count += len(batch)
    conn.commit()
    return count


def insert_participants(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Batch-insert into ``meeting_participants``. Each row: {meeting_id, mk_id, party}."""
    count = 0
    batch: list[tuple] = []
    for row in rows:
        batch.append((str(row["meeting_id"]), str(row["mk_id"]), row.get("party") or ""))
        if len(batch) >= _BATCH:
            conn.executemany(
                "INSERT INTO meeting_participants(meeting_id,mk_id,party) VALUES (?,?,?)",
                batch,
            )
            count += len(batch)
            batch = []
    if batch:
        conn.executemany(
            "INSERT INTO meeting_participants(meeting_id,mk_id,party) VALUES (?,?,?)",
            batch,
        )
        count += len(batch)
    conn.commit()
    return count


def insert_guests(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Batch-insert into ``meeting_guests``. Each row: {meeting_id, name}."""
    count = 0
    batch: list[tuple] = []
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        batch.append((str(row["meeting_id"]), name))
        if len(batch) >= _BATCH:
            conn.executemany(
                "INSERT INTO meeting_guests(meeting_id,name) VALUES (?,?)", batch
            )
            count += len(batch)
            batch = []
    if batch:
        conn.executemany(
            "INSERT INTO meeting_guests(meeting_id,name) VALUES (?,?)", batch
        )
        count += len(batch)
    conn.commit()
    return count


def query_candidate_meeting_ids(
    knesset_num: int,
    *,
    committees: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    mk_ids: list[str] | None = None,
    parties: list[str] | None = None,
    guest_name: str | None = None,
) -> list[str] | None:
    """
    Return the meeting_ids satisfying every supplied structural filter.

    Returns ``None`` (a sentinel, not an empty list) if *no* filter at all
    was supplied — callers should treat that as "every meeting is a
    candidate" and skip running this query entirely rather than issuing a
    needless ``SELECT *``.

    Returns an empty list (not None) when filters were supplied but nothing
    matched — a legitimate "zero results" case.

    Filter combination: committee / date-range / (mk_id-or-party-or-guest)
    groups are AND-ed together (each group narrows the result further);
    mk_ids, parties, and guest_name are OR-ed *within* the participant
    group (matches the old browse_rag behavior where any selected MK, any
    selected party's member, or a guest-name substring match — whichever —
    counted as a hit).

    guest_name
        Optional substring (case-insensitive per SQLite's default LIKE
        collation) to match against meeting_guests.name — for filtering by
        a non-MK attendee (ministry official, private citizen, etc.) who
        can't be resolved to an mk_id. Parameterized (bound as
        f"%{guest_name}%"); never string-interpolated into the SQL itself.

    Raises FileNotFoundError if meeting_index.db for this knesset_num
    hasn't been built yet — callers must check for this explicitly
    (mirrors the fail-loud convention of utils.tools._open_bm25 /
    _bm25_missing_envelope) rather than let it crash unexplained.
    """
    if not (committees or date_from or date_to or mk_ids or parties or guest_name):
        return None

    path = db_path(knesset_num)
    if not path.exists():
        raise FileNotFoundError(f"meeting_index.db not built yet: {path}")

    conn = sqlite3.connect(str(path))
    try:
        where_parts: list[str] = []
        params: list = []

        if committees:
            placeholders = ",".join("?" * len(committees))
            where_parts.append(f"m.committee IN ({placeholders})")
            params.extend(committees)
        if date_from:
            where_parts.append("m.date >= ?")
            params.append(date_from)
        if date_to:
            where_parts.append("m.date <= ?")
            params.append(date_to)

        needs_participant_join = bool(mk_ids or parties)
        needs_guest_join = bool(guest_name)
        participant_parts: list[str] = []
        participant_params: list = []
        if mk_ids:
            placeholders = ",".join("?" * len(mk_ids))
            participant_parts.append(f"p.mk_id IN ({placeholders})")
            participant_params.extend(mk_ids)
        if parties:
            placeholders = ",".join("?" * len(parties))
            participant_parts.append(f"p.party IN ({placeholders})")
            participant_params.extend(parties)
        if guest_name:
            participant_parts.append("g.name LIKE ?")
            participant_params.append(f"%{guest_name}%")

        sql = "SELECT DISTINCT m.meeting_id FROM meetings m"
        # LEFT (not INNER) JOIN: a meeting with zero participant rows (or
        # zero guest rows) must still be reachable via the *other* side of
        # the OR-ed participant_parts group below — an INNER JOIN would
        # silently drop it before the WHERE clause even runs.
        if needs_participant_join:
            sql += " LEFT JOIN meeting_participants p ON p.meeting_id = m.meeting_id"
        if needs_guest_join:
            sql += " LEFT JOIN meeting_guests g ON g.meeting_id = m.meeting_id"

        if participant_parts:
            where_parts.append("(" + " OR ".join(participant_parts) + ")")
            params.extend(participant_params)

        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)

        rows = conn.execute(sql, params).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


__all__ = [
    "db_path",
    "create_tables",
    "insert_meetings",
    "insert_participants",
    "insert_guests",
    "query_candidate_meeting_ids",
]
