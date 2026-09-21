"""
retrieval/knesset_db_store.py

Schema, writers and query helpers for the one SQLite file behind every
structured lookup: config.KNESSET_DB (Data/knesset.db). All Knessets live in
the same tables (knesset_num column); text search is FTS5 external-content
over the plain tables, so text is stored once.

Built by scripts/build_knesset_db.py. Read by utils/tools.py, web/app.py and
utils/meeting.py. One connection per query call; nothing is cached across
requests.

Quote location in opinions: speech_idx indexes the speech list the viewer
shows (meeting["speeches"] for structured files, parse_full_text_speeches()
for full_text files); quote_offset is a raw character offset inside
speeches[speech_idx].text_he for structured files and inside full_text for
full_text files. Both are NULL when the quote was not verified.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import config

_FTS_TOKENIZE = "tokenize='unicode61 remove_diacritics 2'"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS mks (
    mk_id       TEXT NOT NULL,
    knesset_num INTEGER NOT NULL,
    first_name  TEXT,
    last_name   TEXT,
    full_name   TEXT NOT NULL,
    party       TEXT,
    aliases     TEXT,
    PRIMARY KEY (mk_id, knesset_num)
);

CREATE TABLE IF NOT EXISTS committees (
    committee_id TEXT NOT NULL,
    knesset_num  INTEGER NOT NULL,
    name         TEXT NOT NULL,
    is_current   INTEGER,
    PRIMARY KEY (committee_id, knesset_num)
);

CREATE TABLE IF NOT EXISTS meetings (
    meeting_id      TEXT PRIMARY KEY,
    knesset_num     INTEGER NOT NULL,
    committee       TEXT,
    date            TEXT,
    format          TEXT,
    transcript_path TEXT,
    summary_path    TEXT,
    is_protocol     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_meetings_committee ON meetings(committee);
CREATE INDEX IF NOT EXISTS idx_meetings_date      ON meetings(date);
CREATE INDEX IF NOT EXISTS idx_meetings_knesset   ON meetings(knesset_num);

CREATE TABLE IF NOT EXISTS attendance (
    meeting_id  TEXT NOT NULL,
    knesset_num INTEGER NOT NULL,
    name        TEXT NOT NULL,
    mk_id       TEXT,
    party       TEXT
);
CREATE INDEX IF NOT EXISTS idx_attendance_meeting ON attendance(meeting_id);
CREATE INDEX IF NOT EXISTS idx_attendance_mk      ON attendance(mk_id);
CREATE INDEX IF NOT EXISTS idx_attendance_party   ON attendance(party);

CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY,
    meeting_id  TEXT NOT NULL,
    knesset_num INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    UNIQUE (meeting_id, idx)
);
CREATE VIRTUAL TABLE IF NOT EXISTS topics_fts USING fts5(
    text, content='topics', content_rowid='id', {_FTS_TOKENIZE}
);

CREATE TABLE IF NOT EXISTS opinions (
    id             INTEGER PRIMARY KEY,
    meeting_id     TEXT NOT NULL,
    knesset_num    INTEGER NOT NULL,
    idx            INTEGER NOT NULL,
    speaker_label  TEXT NOT NULL,
    speaker_name   TEXT NOT NULL,
    mk_id          TEXT,
    party          TEXT,
    opinion        TEXT NOT NULL,
    quote          TEXT,
    quote_verified INTEGER NOT NULL,
    speech_idx     INTEGER,
    quote_offset   INTEGER,
    UNIQUE (meeting_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_opinions_meeting ON opinions(meeting_id);
CREATE INDEX IF NOT EXISTS idx_opinions_mk      ON opinions(mk_id);
CREATE INDEX IF NOT EXISTS idx_opinions_party   ON opinions(party);
CREATE INDEX IF NOT EXISTS idx_opinions_speaker ON opinions(speaker_name);
CREATE VIRTUAL TABLE IF NOT EXISTS opinions_fts USING fts5(
    opinion, quote, content='opinions', content_rowid='id', {_FTS_TOKENIZE}
);

CREATE TABLE IF NOT EXISTS speeches (
    id          INTEGER PRIMARY KEY,
    meeting_id  TEXT NOT NULL,
    knesset_num INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    speaker     TEXT,
    mk_id       TEXT,
    text        TEXT NOT NULL,
    UNIQUE (meeting_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_speeches_meeting ON speeches(meeting_id);
CREATE INDEX IF NOT EXISTS idx_speeches_mk      ON speeches(mk_id);
CREATE VIRTUAL TABLE IF NOT EXISTS speeches_fts USING fts5(
    text, content='speeches', content_rowid='id', {_FTS_TOKENIZE}
);

CREATE TABLE IF NOT EXISTS bills (
    id          INTEGER PRIMARY KEY,
    bill_id     TEXT NOT NULL,
    knesset_num INTEGER NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT,
    initiators  TEXT,
    UNIQUE (bill_id, knesset_num)
);
CREATE VIRTUAL TABLE IF NOT EXISTS bills_fts USING fts5(
    name, initiators, content='bills', content_rowid='id', {_FTS_TOKENIZE}
);

CREATE TABLE IF NOT EXISTS votes (
    id          INTEGER PRIMARY KEY,
    vote_id     TEXT NOT NULL,
    knesset_num INTEGER NOT NULL,
    title       TEXT NOT NULL,
    subject     TEXT,
    UNIQUE (vote_id, knesset_num)
);
CREATE VIRTUAL TABLE IF NOT EXISTS votes_fts USING fts5(
    title, subject, content='votes', content_rowid='id', {_FTS_TOKENIZE}
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# target name -> (tables cleared per knesset on --rebuild, FTS tables to rebuild after writes).
# meetings rows are upserted, never cleared: the summaries target stores its columns there.
TARGET_TABLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "mks":        (("mks",), ()),
    "committees": (("committees",), ()),
    "meetings":   (("attendance",), ()),
    "summaries":  (("topics", "opinions"), ("topics_fts", "opinions_fts")),
    "speeches":   (("speeches",), ("speeches_fts",)),
    "bills":      (("bills",), ("bills_fts",)),
    "votes":      (("votes",), ("votes_fts",)),
}

_BATCH = 1000


def db_path() -> Path:
    return config.KNESSET_DB


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the db, creating the file and schema when missing (readers check exists() first)."""
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def exists(path: Path | None = None) -> bool:
    return Path(path or db_path()).exists()


# ── writers ───────────────────────────────────────────────────────────────────

def clear_target(conn: sqlite3.Connection, target: str, knesset_num: int) -> None:
    for table in TARGET_TABLES[target][0]:
        conn.execute(f"DELETE FROM {table} WHERE knesset_num = ?", (knesset_num,))
    conn.commit()


def rebuild_fts(conn: sqlite3.Connection, target: str) -> None:
    for fts in TARGET_TABLES[target][1]:
        conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
    conn.commit()


def _insert_rows(conn: sqlite3.Connection, table: str, columns: tuple[str, ...], rows: Iterable[dict]) -> int:
    sql = (f"INSERT OR REPLACE INTO {table}({','.join(columns)}) "
           f"VALUES ({','.join('?' * len(columns))})")
    count = 0
    batch: list[tuple] = []
    for row in rows:
        batch.append(tuple(row.get(c) for c in columns))
        if len(batch) >= _BATCH:
            conn.executemany(sql, batch)
            count += len(batch)
            batch = []
    if batch:
        conn.executemany(sql, batch)
        count += len(batch)
    conn.commit()
    return count


def insert_mks(conn, rows):
    return _insert_rows(conn, "mks", ("mk_id", "knesset_num", "first_name", "last_name", "full_name", "party", "aliases"), rows)


def insert_committees(conn, rows):
    return _insert_rows(conn, "committees", ("committee_id", "knesset_num", "name", "is_current"), rows)


def insert_meetings(conn, rows):
    """Upsert meeting rows; summary_path / is_protocol belong to the summaries target and are kept."""
    sql = ("INSERT INTO meetings(meeting_id, knesset_num, committee, date, format, transcript_path) "
           "VALUES (?,?,?,?,?,?) ON CONFLICT(meeting_id) DO UPDATE SET knesset_num=excluded.knesset_num, "
           "committee=excluded.committee, date=excluded.date, format=excluded.format, "
           "transcript_path=excluded.transcript_path")
    rows = list(rows)
    conn.executemany(sql, [(r["meeting_id"], r["knesset_num"], r.get("committee"), r.get("date"),
                            r.get("format"), r.get("transcript_path")) for r in rows])
    conn.commit()
    return len(rows)


def insert_attendance(conn, rows):
    return _insert_rows(conn, "attendance", ("meeting_id", "knesset_num", "name", "mk_id", "party"), rows)


def replace_meeting_summary(conn, meeting_id: str, knesset_num: int, summary_path: str,
                            is_protocol: bool, topics: list[str], opinions: list[dict]) -> None:
    """Upsert one meeting's topics/opinions rows; FTS is rebuilt by the caller at the end."""
    conn.execute("DELETE FROM topics WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM opinions WHERE meeting_id = ?", (meeting_id,))
    conn.executemany(
        "INSERT INTO topics(meeting_id, knesset_num, idx, text) VALUES (?,?,?,?)",
        [(meeting_id, knesset_num, i, t) for i, t in enumerate(topics)])
    conn.executemany(
        "INSERT INTO opinions(meeting_id, knesset_num, idx, speaker_label, speaker_name, mk_id, party, "
        "opinion, quote, quote_verified, speech_idx, quote_offset) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(meeting_id, knesset_num, i, o["speaker_label"], o["speaker_name"], o.get("mk_id"), o.get("party"),
          o["opinion"], o.get("quote") or "", int(bool(o.get("quote_verified"))),
          o.get("speech_idx"), o.get("quote_offset"))
         for i, o in enumerate(opinions)])
    conn.execute("UPDATE meetings SET summary_path = ?, is_protocol = ? WHERE meeting_id = ?",
                 (summary_path, int(is_protocol), meeting_id))


def insert_speeches(conn, rows):
    return _insert_rows(conn, "speeches", ("meeting_id", "knesset_num", "idx", "speaker", "mk_id", "text"), rows)


def insert_bills(conn, rows):
    return _insert_rows(conn, "bills", ("bill_id", "knesset_num", "name", "status", "initiators"), rows)


def insert_votes(conn, rows):
    return _insert_rows(conn, "votes", ("vote_id", "knesset_num", "title", "subject"), rows)


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
    conn.commit()


# ── name lookups (fuzzy index input) ──────────────────────────────────────────

def name_entries(conn, target: str, knesset_num: int) -> list[dict]:
    """
    Rows shaped for FuzzyNameIndex: {id, label, body, extra}. target is one of
    mks | committees | bills | votes.
    """
    if target == "mks":
        sql = ("SELECT mk_id AS id, full_name AS label, aliases AS body, party FROM mks "
               "WHERE knesset_num = ?")
        return [{"id": r["id"], "label": r["label"], "body": r["body"] or "",
                 "extra": {"mk_id": r["id"], "full_name": r["label"], "party": r["party"]}}
                for r in conn.execute(sql, (knesset_num,))]
    if target == "committees":
        sql = "SELECT committee_id AS id, name AS label, is_current FROM committees WHERE knesset_num = ?"
        return [{"id": r["id"], "label": r["label"], "body": r["label"],
                 "extra": {"committee_id": r["id"], "knesset_num": knesset_num, "is_current": r["is_current"]}}
                for r in conn.execute(sql, (knesset_num,))]
    if target == "bills":
        sql = "SELECT bill_id AS id, name AS label, status, initiators FROM bills WHERE knesset_num = ?"
        return [{"id": r["id"], "label": r["label"],
                 "body": " | ".join(filter(None, [r["label"], r["initiators"], r["status"]])),
                 "extra": {"bill_id": r["id"], "bill_name": r["label"], "status": r["status"],
                           "initiators": (r["initiators"] or "").split(" | ") if r["initiators"] else [],
                           "knesset_num": knesset_num}}
                for r in conn.execute(sql, (knesset_num,))]
    if target == "votes":
        sql = "SELECT vote_id AS id, title AS label, subject FROM votes WHERE knesset_num = ?"
        return [{"id": r["id"], "label": r["label"] or r["subject"] or "",
                 "body": " | ".join(filter(None, [r["label"], r["subject"]])),
                 "extra": {"vote_id": r["id"], "vote_title": r["label"], "subject": r["subject"],
                           "knesset_num": knesset_num}}
                for r in conn.execute(sql, (knesset_num,))]
    raise ValueError(f"unknown name target {target!r}")


def mk_names(conn, knesset_num: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT full_name FROM mks WHERE knesset_num = ?", (knesset_num,))]


def mk_party_map(conn, knesset_num: int) -> dict[str, str]:
    return {r[0]: r[1] or "" for r in conn.execute(
        "SELECT mk_id, party FROM mks WHERE knesset_num = ?", (knesset_num,))}


# ── meeting reads ─────────────────────────────────────────────────────────────

def get_meeting(conn, meeting_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM meetings WHERE meeting_id = ?", (str(meeting_id),)).fetchone()
    return dict(row) if row else None


def get_topics(conn, meeting_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT idx, text FROM topics WHERE meeting_id = ? ORDER BY idx", (str(meeting_id),))]


def get_opinions(conn, meeting_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT idx, speaker_label AS speaker, speaker_name, mk_id, party, opinion, quote, quote_verified, "
        "speech_idx, quote_offset FROM opinions WHERE meeting_id = ? ORDER BY idx", (str(meeting_id),))]


def get_attendance(conn, meeting_id: str) -> list[dict]:
    """MKs first (with party), then guests; each row name/mk_id/party."""
    return [dict(r) for r in conn.execute(
        "SELECT name, mk_id, party FROM attendance WHERE meeting_id = ? "
        "ORDER BY mk_id IS NULL, party, name", (str(meeting_id),))]


def meeting_ids_with_summary(conn, knesset_num: int) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT meeting_id FROM meetings WHERE knesset_num = ? AND is_protocol IS NOT NULL", (knesset_num,))}


# ── searches ──────────────────────────────────────────────────────────────────

def search_topics(conn, match: str, knesset_num: int, *, top_k: int,
                  committees: list[str] | None = None,
                  date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    where = ["topics_fts MATCH ?", "t.knesset_num = ?"]
    params: list = [match, knesset_num]
    _meeting_filters(where, params, "m", committees, date_from, date_to)
    sql = (f"SELECT t.id, t.meeting_id, t.idx, t.text, m.committee, m.date, bm25(topics_fts) AS score "
           f"FROM topics_fts JOIN topics t ON t.id = topics_fts.rowid "
           f"JOIN meetings m ON m.meeting_id = t.meeting_id "
           f"WHERE {' AND '.join(where)} ORDER BY score LIMIT ?")
    params.append(top_k)
    return [dict(r) for r in conn.execute(sql, params)]


def search_opinions(conn, match: str | None, knesset_num: int, *, top_k: int,
                    mk_id: str | None = None, party: str | None = None,
                    verified_only: bool = True,
                    committees: list[str] | None = None,
                    date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """FTS over opinion+quote; match=None lists (newest first) instead of ranking."""
    where = ["o.knesset_num = ?"]
    params: list = [knesset_num]
    if match:
        where.insert(0, "opinions_fts MATCH ?")
        params.insert(0, match)
    if mk_id:
        where.append("o.mk_id = ?")
        params.append(str(mk_id))
    if party:
        where.append("o.party = ?")
        params.append(party)
    if verified_only:
        where.append("o.quote_verified = 1")
    _meeting_filters(where, params, "m", committees, date_from, date_to)
    select = ("SELECT o.id, o.meeting_id, o.idx, o.speaker_label AS speaker, o.speaker_name, o.mk_id, "
              "o.party, o.opinion, o.quote, o.quote_verified, o.speech_idx, o.quote_offset, "
              "m.committee, m.date")
    if match:
        sql = (f"{select}, bm25(opinions_fts) AS score FROM opinions_fts "
               f"JOIN opinions o ON o.id = opinions_fts.rowid "
               f"JOIN meetings m ON m.meeting_id = o.meeting_id "
               f"WHERE {' AND '.join(where)} ORDER BY score LIMIT ?")
    else:
        sql = (f"{select}, 0.0 AS score FROM opinions o "
               f"JOIN meetings m ON m.meeting_id = o.meeting_id "
               f"WHERE {' AND '.join(where)} ORDER BY m.date DESC, o.idx LIMIT ?")
    params.append(top_k)
    return [dict(r) for r in conn.execute(sql, params)]


def search_speeches(conn, match: str, knesset_num: int, *, top_k: int,
                    meeting_ids: list[str] | None = None,
                    committees: list[str] | None = None,
                    speaker_tokens: list[str] | None = None) -> list[dict]:
    """
    FTS over speech text. speaker_tokens is a coarse OR pre-filter (any token
    inside speeches.speaker); callers refine with name_query_matches.
    """
    where = ["speeches_fts MATCH ?", "s.knesset_num = ?"]
    params: list = [match, knesset_num]
    if meeting_ids:
        where.append(f"s.meeting_id IN ({','.join('?' * len(meeting_ids))})")
        params.extend(str(m) for m in meeting_ids)
    if speaker_tokens:
        where.append("(" + " OR ".join("s.speaker LIKE ?" for _ in speaker_tokens) + ")")
        params.extend(f"%{t}%" for t in speaker_tokens)
    _meeting_filters(where, params, "m", committees, None, None)
    sql = (f"SELECT s.id, s.meeting_id, s.idx AS speech_idx, s.speaker, s.mk_id, s.text, "
           f"m.committee, m.date, bm25(speeches_fts) AS score "
           f"FROM speeches_fts JOIN speeches s ON s.id = speeches_fts.rowid "
           f"JOIN meetings m ON m.meeting_id = s.meeting_id "
           f"WHERE {' AND '.join(where)} ORDER BY score LIMIT ?")
    params.append(top_k)
    return [dict(r) for r in conn.execute(sql, params)]


def _meeting_filters(where: list[str], params: list, alias: str,
                     committees: list[str] | None, date_from: str | None, date_to: str | None) -> None:
    if committees:
        where.append(f"{alias}.committee IN ({','.join('?' * len(committees))})")
        params.extend(committees)
    if date_from:
        where.append(f"{alias}.date >= ?")
        params.append(date_from)
    if date_to:
        where.append(f"{alias}.date <= ?")
        params.append(date_to)


def query_candidate_meeting_ids(
    conn,
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
    Meeting ids satisfying every supplied structural filter, or None when no
    filter was supplied (caller treats None as "unrestricted"). Committee and
    date filters AND together; mk_ids / parties / guest_name OR together inside
    one attendance group. guest_name is a substring match on attendance.name.
    """
    if not (committees or date_from or date_to or mk_ids or parties or guest_name):
        return None
    where = ["m.knesset_num = ?"]
    params: list = [knesset_num]
    _meeting_filters(where, params, "m", committees, date_from, date_to)

    attendance_parts: list[str] = []
    attendance_params: list = []
    if mk_ids:
        attendance_parts.append(f"a.mk_id IN ({','.join('?' * len(mk_ids))})")
        attendance_params.extend(str(m) for m in mk_ids)
    if parties:
        attendance_parts.append(f"a.party IN ({','.join('?' * len(parties))})")
        attendance_params.extend(parties)
    if guest_name:
        attendance_parts.append("a.name LIKE ?")
        attendance_params.append(f"%{guest_name}%")
    if attendance_parts:
        where.append("m.meeting_id IN (SELECT a.meeting_id FROM attendance a WHERE "
                     + " OR ".join(attendance_parts) + ")")
        params.extend(attendance_params)

    sql = f"SELECT m.meeting_id FROM meetings m WHERE {' AND '.join(where)} ORDER BY m.date DESC"
    return [r[0] for r in conn.execute(sql, params)]
