"""
retrieval/knesset_db_store.py

Schema, writers and query helpers for the one SQLite file behind every
structured lookup: config.KNESSET_DB (Data/knesset.db). All Knessets live in
the same tables (knesset_num column); text search is FTS5 external-content
over the plain tables, so text is stored once.

Built by scripts/build_knesset_db.py. Read by utils/tools.py, web/app.py and
utils/meeting.py. One connection per query call; nothing is cached across
requests.

Quote location in opinions: speech_idx indexes utils.meeting.meeting_display_speeches(),
the speech list the viewer shows and the speeches table stores; quote_offset and
quote_length are the character range of the quote inside that speech's text. All
three are NULL when the quote was not located.
"""

from __future__ import annotations

import sqlite3
import time
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
    quote_length   INTEGER,
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

CREATE TABLE IF NOT EXISTS mk_themes (
    id               INTEGER PRIMARY KEY,
    mk_id            TEXT NOT NULL,
    knesset_num      INTEGER NOT NULL,
    rank             INTEGER NOT NULL,
    title            TEXT NOT NULL,
    summary          TEXT NOT NULL,
    model_rank       INTEGER,
    persistence_rank INTEGER,
    first_date       TEXT,
    last_date        TEXT,
    meeting_count    INTEGER NOT NULL,
    opinion_count    INTEGER NOT NULL,
    model            TEXT,
    generated_at     TEXT,
    UNIQUE (mk_id, knesset_num, rank)
);
CREATE VIRTUAL TABLE IF NOT EXISTS mk_themes_fts USING fts5(
    title, summary, content='mk_themes', content_rowid='id', {_FTS_TOKENIZE}
);

CREATE TABLE IF NOT EXISTS mk_theme_opinions (
    theme_id    INTEGER NOT NULL,
    knesset_num INTEGER NOT NULL,
    meeting_id  TEXT NOT NULL,
    opinion_idx INTEGER NOT NULL,
    PRIMARY KEY (theme_id, meeting_id, opinion_idx)
);
CREATE INDEX IF NOT EXISTS idx_mk_theme_opinions_opinion ON mk_theme_opinions(meeting_id, opinion_idx);

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
    "mk_themes":  (("mk_theme_opinions", "mk_themes"), ("mk_themes_fts",)),
}

_BATCH = 1000


_PROGRESS_HANDLER_OPCODES = 10_000


class _DeadlineConnection(sqlite3.Connection):
    interrupt_deadline: float | None = None


def deadline_passed(conn) -> bool:
    """True once a connection opened with interrupt_after_seconds is past its deadline
    (its running statement is then aborted, surfacing as some sqlite3.OperationalError)."""
    deadline = getattr(conn, "interrupt_deadline", None)
    return deadline is not None and time.monotonic() > deadline


def db_path() -> Path:
    return config.KNESSET_DB


def connect(path: Path | None = None, *, interrupt_after_seconds: float | None = None) -> sqlite3.Connection:
    """Open the db, creating the file and schema when missing (readers check exists() first).
    interrupt_after_seconds: statements still running that long after connect raise
    sqlite3.OperationalError("interrupted")."""
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), factory=_DeadlineConnection)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _add_missing_columns(conn)
    except sqlite3.Error:
        conn.close()
        raise
    if interrupt_after_seconds is not None:
        conn.interrupt_deadline = time.monotonic() + interrupt_after_seconds
        conn.set_progress_handler(lambda: deadline_passed(conn), _PROGRESS_HANDLER_OPCODES)
    return conn


_COLUMNS_ADDED_AFTER_TABLE_CREATION = {"opinions": {"quote_length": "INTEGER"}}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS leaves tables of an older db as they were; add the newer columns."""
    for table, columns in _COLUMNS_ADDED_AFTER_TABLE_CREATION.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, column_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                conn.commit()


LIKE_ESCAPE_CHAR = "!"


def like_substring_pattern(text: str) -> str:
    """LIKE pattern for text as a literal substring; use it with `LIKE ? ESCAPE '{LIKE_ESCAPE_CHAR}'`."""
    escaped = "".join(LIKE_ESCAPE_CHAR + ch if ch in "%_" + LIKE_ESCAPE_CHAR else ch for ch in text)
    return f"%{escaped}%"


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


def replace_mk_themes(conn, theme_file: dict) -> tuple[int, int]:
    """
    Replace one MK's themes with those of a Data/mk_themes/<k>/<mk_id>.json payload
    (summarization.mk_themes.themes_file_payload). Opinions are linked by (meeting_id, idx), which survives
    an opinions rebuild; links to opinions missing from the db are dropped. Dates and counts are derived
    from the linked opinions. FTS is rebuilt by the caller. Returns (themes written, links dropped).
    """
    mk_id, knesset_num = theme_file["mk_id"], theme_file["knesset_num"]
    conn.execute("DELETE FROM mk_theme_opinions WHERE theme_id IN "
                 "(SELECT id FROM mk_themes WHERE mk_id = ? AND knesset_num = ?)", (mk_id, knesset_num))
    conn.execute("DELETE FROM mk_themes WHERE mk_id = ? AND knesset_num = ?", (mk_id, knesset_num))
    links_dropped = 0
    for theme in theme_file["themes"]:
        theme_id = conn.execute(
            "INSERT INTO mk_themes(mk_id, knesset_num, rank, title, summary, model_rank, persistence_rank, "
            "meeting_count, opinion_count, model, generated_at) VALUES (?,?,?,?,?,?,?,0,0,?,?)",
            (mk_id, knesset_num, theme["rank"], theme["title"], theme["summary"], theme.get("model_rank"),
             theme.get("persistence_rank"), theme_file.get("model"), theme_file.get("generated_at"))).lastrowid
        refs = list(dict.fromkeys((meeting_id, idx) for meeting_id, idx in theme["opinions"]))
        existing = [ref for ref in refs if conn.execute(
            "SELECT 1 FROM opinions WHERE meeting_id = ? AND idx = ?", ref).fetchone()]
        links_dropped += len(refs) - len(existing)
        conn.executemany("INSERT INTO mk_theme_opinions(theme_id, knesset_num, meeting_id, opinion_idx) VALUES (?,?,?,?)",
                         [(theme_id, knesset_num, meeting_id, idx) for meeting_id, idx in existing])
        conn.execute("""
            UPDATE mk_themes SET (first_date, last_date, meeting_count, opinion_count) = (
                SELECT MIN(m.date), MAX(m.date), COUNT(DISTINCT l.meeting_id), COUNT(*)
                FROM mk_theme_opinions l JOIN meetings m ON m.meeting_id = l.meeting_id
                WHERE l.theme_id = ?)
            WHERE id = ?""", (theme_id, theme_id))
    conn.commit()
    return len(theme_file["themes"]), links_dropped


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
        "opinion, quote, quote_verified, speech_idx, quote_offset, quote_length) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(meeting_id, knesset_num, i, o["speaker_label"], o["speaker_name"], o.get("mk_id"), o.get("party"),
          o["opinion"], o.get("quote") or "", int(bool(o.get("quote_verified"))),
          o.get("speech_idx"), o.get("quote_offset"), o.get("quote_length"))
         for i, o in enumerate(opinions)])
    conn.execute("UPDATE meetings SET summary_path = ?, is_protocol = ? WHERE meeting_id = ?",
                 (summary_path, int(is_protocol), meeting_id))


def insert_speeches(conn, rows):
    return _insert_rows(conn, "speeches", ("meeting_id", "knesset_num", "idx", "speaker", "mk_id", "text"), rows)


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
    conn.commit()


# ── Knesset selection ─────────────────────────────────────────────────────────

def knesset_num_list(knesset_nums: int | Iterable[int]) -> list[int]:
    """One Knesset number or several → a sorted list of distinct ints."""
    if isinstance(knesset_nums, int):
        return [knesset_nums]
    return sorted({int(k) for k in knesset_nums})


def knesset_condition(column: str, knesset_nums: int | Iterable[int]) -> tuple[str, list[int]]:
    """(SQL condition, params) restricting column to the given Knesset numbers."""
    nums = knesset_num_list(knesset_nums)
    if len(nums) == 1:
        return f"{column} = ?", nums
    return f"{column} IN ({','.join('?' * len(nums))})", nums


# ── name lookups (fuzzy index input) ──────────────────────────────────────────

def name_entries(conn, target: str, knesset_nums: int | Iterable[int]) -> list[dict]:
    """
    Rows shaped for FuzzyNameIndex: {id, label, body, extra}. target is one of
    mks | committees. An MK of several of the Knessets is one entry (mk_id is the person) with the
    name and party of the latest of them, extra.knesset_num; a committee is one entry per Knesset.
    """
    condition, params = knesset_condition("knesset_num", knesset_nums)
    if target == "mks":
        sql = (f"SELECT mk_id AS id, full_name AS label, aliases AS body, party, knesset_num FROM mks "
               f"WHERE {condition} ORDER BY knesset_num")
        latest_row_by_mk_id = {r["id"]: r for r in conn.execute(sql, params)}
        return [{"id": r["id"], "label": r["label"], "body": r["body"] or "",
                 "extra": {"mk_id": r["id"], "full_name": r["label"], "party": r["party"],
                           "knesset_num": r["knesset_num"]}}
                for r in latest_row_by_mk_id.values()]
    if target == "committees":
        sql = f"SELECT committee_id AS id, name AS label, is_current, knesset_num FROM committees WHERE {condition}"
        return [{"id": r["id"], "label": r["label"], "body": r["label"],
                 "extra": {"committee_id": r["id"], "knesset_num": r["knesset_num"], "is_current": r["is_current"]}}
                for r in conn.execute(sql, params)]
    raise ValueError(f"unknown name target {target!r}")


def mk_names(conn, knesset_nums: int | Iterable[int]) -> list[str]:
    condition, params = knesset_condition("knesset_num", knesset_nums)
    return [r[0] for r in conn.execute(f"SELECT DISTINCT full_name FROM mks WHERE {condition}", params)]


def mk_party_map(conn, knesset_nums: int | Iterable[int]) -> dict[str, str]:
    """mk_id → roster party; for an MK of several of the Knessets, the party in the latest."""
    condition, params = knesset_condition("knesset_num", knesset_nums)
    return {r[0]: r[1] or "" for r in conn.execute(
        f"SELECT mk_id, party FROM mks WHERE {condition} ORDER BY knesset_num", params)}


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
        "speech_idx, quote_offset, quote_length FROM opinions WHERE meeting_id = ? ORDER BY idx", (str(meeting_id),))]


def get_attendance(conn, meeting_id: str) -> list[dict]:
    """MKs first (with party), then guests; each row name/mk_id/party."""
    return [dict(r) for r in conn.execute(
        "SELECT name, mk_id, party FROM attendance WHERE meeting_id = ? "
        "ORDER BY mk_id IS NULL, party, name", (str(meeting_id),))]


def meeting_ids_with_summary(conn, knesset_num: int) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT meeting_id FROM meetings WHERE knesset_num = ? AND is_protocol IS NOT NULL", (knesset_num,))}


# ── searches ──────────────────────────────────────────────────────────────────

PROTOCOL_SCOPES = ("topics", "opinions", "speeches")

_SCOPE_COLUMNS = {
    "topics":   "x.idx, x.text AS topic",
    "opinions": ("x.idx, x.speaker_label AS speaker, x.speaker_name, x.mk_id, x.party, x.opinion, x.quote, "
                 "x.speech_idx, x.quote_offset, x.quote_length"),
    "speeches": "x.idx AS speech_idx, x.speaker, x.mk_id, x.text",
}



def _exact_tier_order(fts: str) -> str:
    """ORDER BY term: 0 for rows matching the query words as typed, 1 for rows matched only through variants."""
    return f"(CASE WHEN {fts}.rowid IN (SELECT rowid FROM {fts} WHERE {fts} MATCH ?) THEN 0 ELSE 1 END)"


def _protocol_rows_sql(scope: str, knesset_nums: int | Iterable[int], *, match: str | None, exact_match: str | None,
                       mk_id: str | None, party: str | list[str] | None, committees: list[str] | None,
                       meeting_ids: list[str] | None, date_from: str | None, date_to: str | None,
                       sort: str) -> tuple[str, list]:
    where, params = _protocol_filter_where(scope, knesset_nums, mk_id=mk_id, party=party, committees=committees,
                                           meeting_ids=meeting_ids, date_from=date_from, date_to=date_to)
    fts = f"{scope}_fts"
    select = f"SELECT x.meeting_id, m.knesset_num, m.committee, m.date, {_SCOPE_COLUMNS[scope]}"
    if match:
        where.insert(0, f"{fts} MATCH ?")
        params.insert(0, match)
        source = (f"FROM {fts} JOIN {scope} x ON x.id = {fts}.rowid "
                  f"JOIN meetings m ON m.meeting_id = x.meeting_id")
        select += f", bm25({fts}) AS score"
    elif mk_id or party:
        source = f"FROM {scope} x JOIN meetings m ON m.meeting_id = x.meeting_id"
    else:
        # Walk meetings newest-first via idx_meetings_date so LIMIT stops early;
        # left to itself the planner scans and sorts the whole scope table.
        source = f"FROM meetings m CROSS JOIN {scope} x ON x.meeting_id = m.meeting_id"
    if match and sort == "relevance":
        order = "score, x.id"
        if exact_match and exact_match != match:
            order = f"{_exact_tier_order(fts)}, {order}"
            params.append(exact_match)
    else:
        order = "m.date DESC, m.meeting_id, x.idx"
    return f"{select} {source} WHERE {' AND '.join(where)} ORDER BY {order}", params


def query_protocol_rows(conn, scope: str, knesset_nums: int | Iterable[int], *, match: str | None = None,
                        exact_match: str | None = None,
                        mk_id: str | None = None, party: str | list[str] | None = None,
                        committees: list[str] | None = None, meeting_ids: list[str] | None = None,
                        date_from: str | None = None, date_to: str | None = None,
                        sort: str = "relevance", top_k: int, offset: int = 0) -> list[dict]:
    """
    Rows of one protocol scope (topics | opinions | speeches) with meeting_id,
    knesset_num, committee and date. match=None lists instead of ranking. Filters AND
    together; mk_id / party mean attendance for topics, the opinion author for
    opinions and the speaker (roster party) for speeches; party may be a list of names
    (the same party under its name in each Knesset). Opinions are always
    verified-only and is_protocol = 0 meetings are always excluded.
    Order: sort="relevance" with a match ranks rows matching exact_match (the query
    words as typed) first, then rows matched only through spelling / prefix variants,
    bm25 within each tier; otherwise date DESC, meeting_id, in-meeting idx.
    """
    sql, params = _protocol_rows_sql(scope, knesset_nums, match=match, exact_match=exact_match, mk_id=mk_id,
                                     party=party, committees=committees, meeting_ids=meeting_ids,
                                     date_from=date_from, date_to=date_to, sort=sort)
    return [dict(r) for r in conn.execute(f"{sql} LIMIT ? OFFSET ?", params + [top_k, offset])]


def iter_protocol_rows(conn, scope: str, knesset_nums: int | Iterable[int], *, match: str | None = None,
                       exact_match: str | None = None, mk_id: str | None = None, party: str | list[str] | None = None,
                       committees: list[str] | None = None, meeting_ids: list[str] | None = None,
                       date_from: str | None = None, date_to: str | None = None, sort: str = "relevance",
                       offset: int = 0, batch_size: int = 50):
    """query_protocol_rows in the same order from row offset on, without a page limit, fetched lazily
    in batches: the caller stops iterating once it has what it needs."""
    sql, params = _protocol_rows_sql(scope, knesset_nums, match=match, exact_match=exact_match, mk_id=mk_id,
                                     party=party, committees=committees, meeting_ids=meeting_ids,
                                     date_from=date_from, date_to=date_to, sort=sort)
    cursor = conn.execute(f"{sql} LIMIT -1 OFFSET ?", params + [offset])
    while batch := cursor.fetchmany(batch_size):
        for row in batch:
            yield dict(row)


def count_protocol_rows(conn, scope: str, knesset_nums: int | Iterable[int], *, cap: int, mk_id: str | None = None,
                        party: str | list[str] | None = None, committees: list[str] | None = None,
                        meeting_ids: list[str] | None = None, date_from: str | None = None,
                        date_to: str | None = None) -> int:
    """Rows of one protocol scope passing the query_protocol_rows filters (no match), counted up to cap."""
    where, params = _protocol_filter_where(scope, knesset_nums, mk_id=mk_id, party=party, committees=committees,
                                           meeting_ids=meeting_ids, date_from=date_from, date_to=date_to)
    sql = (f"SELECT COUNT(*) FROM (SELECT 1 FROM {scope} x JOIN meetings m ON m.meeting_id = x.meeting_id "
           f"WHERE {' AND '.join(where)} LIMIT ?)")
    return conn.execute(sql, params + [cap]).fetchone()[0]


def _protocol_filter_where(scope: str, knesset_nums: int | Iterable[int], *, mk_id: str | None,
                           party: str | list[str] | None,
                           committees: list[str] | None, meeting_ids: list[str] | None,
                           date_from: str | None, date_to: str | None) -> tuple[list[str], list]:
    if scope not in PROTOCOL_SCOPES:
        raise ValueError(f"unknown protocol scope {scope!r}")
    knesset_sql, params = knesset_condition("x.knesset_num", knesset_nums)
    where = [knesset_sql, "(m.is_protocol IS NULL OR m.is_protocol != 0)"]
    _meeting_filters(where, params, "m", committees, date_from, date_to)
    if meeting_ids:
        where.append(f"m.meeting_id IN ({','.join('?' * len(meeting_ids))})")
        params.extend(str(m) for m in meeting_ids)
    parties = [party] if isinstance(party, str) else list(party or [])
    party_in = f"party IN ({','.join('?' * len(parties))})"
    if scope == "topics":
        if mk_id:
            where.append("x.meeting_id IN (SELECT meeting_id FROM attendance WHERE mk_id = ?)")
            params.append(str(mk_id))
        if parties:
            where.append(f"x.meeting_id IN (SELECT meeting_id FROM attendance WHERE {party_in})")
            params.extend(parties)
    elif scope == "opinions":
        where.append("x.quote_verified = 1")
        if mk_id:
            where.append("x.mk_id = ?")
            params.append(str(mk_id))
        if parties:
            where.append(f"x.{party_in}")
            params.extend(parties)
    else:
        if mk_id:
            where.append("x.mk_id = ?")
            params.append(str(mk_id))
        if parties:
            where.append(f"(x.mk_id, x.knesset_num) IN (SELECT mk_id, knesset_num FROM mks WHERE {party_in})")
            params.extend(parties)
    return where, params


def search_speeches(conn, match: str, knesset_nums: int | Iterable[int], *, top_k: int,
                    meeting_ids: list[str] | None = None,
                    committees: list[str] | None = None,
                    speaker_tokens: list[str] | None = None) -> list[dict]:
    """
    FTS over speech text. speaker_tokens is a coarse OR pre-filter (any token
    inside speeches.speaker); callers refine with name_query_matches.
    """
    knesset_sql, knesset_params = knesset_condition("s.knesset_num", knesset_nums)
    where = ["speeches_fts MATCH ?", knesset_sql]
    params: list = [match, *knesset_params]
    if meeting_ids:
        where.append(f"s.meeting_id IN ({','.join('?' * len(meeting_ids))})")
        params.extend(str(m) for m in meeting_ids)
    if speaker_tokens:
        where.append("(" + " OR ".join(f"s.speaker LIKE ? ESCAPE '{LIKE_ESCAPE_CHAR}'" for _ in speaker_tokens) + ")")
        params.extend(like_substring_pattern(t) for t in speaker_tokens)
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
    knesset_nums: int | Iterable[int],
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
    knesset_sql, params = knesset_condition("m.knesset_num", knesset_nums)
    where = [knesset_sql]
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
        attendance_parts.append(f"a.name LIKE ? ESCAPE '{LIKE_ESCAPE_CHAR}'")
        attendance_params.append(like_substring_pattern(guest_name))
    if attendance_parts:
        where.append("m.meeting_id IN (SELECT a.meeting_id FROM attendance a WHERE "
                     + " OR ".join(attendance_parts) + ")")
        params.extend(attendance_params)

    sql = f"SELECT m.meeting_id FROM meetings m WHERE {' AND '.join(where)} ORDER BY m.date DESC"
    return [r[0] for r in conn.execute(sql, params)]


# ── web browser queries ───────────────────────────────────────────────────────

def _browsable_meetings_where(knesset_nums: int | Iterable[int],
                              candidate_meeting_ids: list[str] | None) -> tuple[list[str], list]:
    """Meetings of the Knessets that are protocols (or not summarized yet), optionally limited to candidates."""
    knesset_sql, params = knesset_condition("m.knesset_num", knesset_nums)
    where = [knesset_sql, "(m.is_protocol IS NULL OR m.is_protocol != 0)"]
    if candidate_meeting_ids is not None:
        if not candidate_meeting_ids:
            where.append("0")
        else:
            where.append(f"m.meeting_id IN ({','.join('?' * len(candidate_meeting_ids))})")
            params.extend(str(m) for m in candidate_meeting_ids)
    return where, params


def browse_filter_lists(conn, knesset_nums: int | Iterable[int]) -> dict[str, list[str]]:
    """The reading tab's filter choices for the Knessets: committees with browsable meetings, MK names
    and roster parties, each distinct and sorted."""
    meetings_where, meetings_params = _browsable_meetings_where(knesset_nums, None)
    mks_condition, mks_params = knesset_condition("knesset_num", knesset_nums)
    return {
        "committees": [r[0] for r in conn.execute(
            f"SELECT DISTINCT m.committee FROM meetings m WHERE {' AND '.join(meetings_where)} "
            f"AND m.committee IS NOT NULL ORDER BY m.committee", meetings_params)],
        "mks": [r[0] for r in conn.execute(
            f"SELECT DISTINCT full_name FROM mks WHERE {mks_condition} ORDER BY full_name", mks_params)],
        "parties": [r[0] for r in conn.execute(
            f"SELECT DISTINCT party FROM mks WHERE {mks_condition} AND party IS NOT NULL AND party != '' "
            f"ORDER BY party", mks_params)],
    }


def recent_meetings(conn,knesset_nums: int | Iterable[int], *, limit: int,
                    candidate_meeting_ids: list[str] | None = None) -> list[dict]:
    """Newest browsable meetings: rows meeting_id/committee/date."""
    where, params = _browsable_meetings_where(knesset_nums, candidate_meeting_ids)
    sql = (f"SELECT m.meeting_id, m.committee, m.date FROM meetings m WHERE {' AND '.join(where)} "
           f"ORDER BY m.date DESC, m.meeting_id DESC LIMIT ?")
    return [dict(r) for r in conn.execute(sql, params + [limit])]


def meetings_by_best_speech(conn, match: str, knesset_nums: int | Iterable[int], *, limit: int, sort: str = "relevance",
                            candidate_meeting_ids: list[str] | None = None,
                            exact_match: str | None = None) -> list[dict]:
    """
    Browsable meetings with at least one speech matching the FTS expression, one row per meeting:
    meeting_id/committee/date, best_speech_rowid, score (bm25 of the best speech, lower = better) and
    tier (0 when a speech matches exact_match, the query words as typed; 1 when the meeting matches
    only through spelling / prefix variants). The best speech is the lowest (tier, score).
    sort="relevance" orders by tier, then score; anything else by date DESC.
    """
    where, params = _browsable_meetings_where(knesset_nums, candidate_meeting_ids)
    order_by = "tier, score, meeting_id" if sort == "relevance" else "date DESC, meeting_id DESC"
    use_tiers = bool(exact_match) and exact_match != match
    tier_sql = _exact_tier_order("speeches_fts") if use_tiers else "0"
    sql = (f"WITH matching_speeches AS MATERIALIZED ("
           f"  SELECT s.meeting_id, m.committee, m.date, speeches_fts.rowid AS speech_rowid, "
           f"         bm25(speeches_fts) AS speech_score, {tier_sql} AS speech_tier "
           f"  FROM speeches_fts JOIN speeches s ON s.id = speeches_fts.rowid "
           f"  JOIN meetings m ON m.meeting_id = s.meeting_id "
           f"  WHERE speeches_fts MATCH ? AND {' AND '.join(where)}), "
           f"ranked_speeches AS ("
           f"  SELECT *, ROW_NUMBER() OVER (PARTITION BY meeting_id ORDER BY speech_tier, speech_score) AS rank_in_meeting "
           f"  FROM matching_speeches) "
           f"SELECT meeting_id, committee, date, speech_rowid AS best_speech_rowid, speech_score AS score, "
           f"       speech_tier AS tier "
           f"FROM ranked_speeches WHERE rank_in_meeting = 1 ORDER BY {order_by} LIMIT ?")
    tier_params = [exact_match] if use_tiers else []
    return [dict(r) for r in conn.execute(sql, tier_params + [match] + params + [limit])]


def speech_snippet(conn, match: str, speech_rowid: int, *, tokens: int = 24) -> str:
    row = conn.execute(
        "SELECT snippet(speeches_fts, 0, '', '', '…', ?) FROM speeches_fts "
        "WHERE speeches_fts MATCH ? AND rowid = ?", (tokens, match, speech_rowid)).fetchone()
    return row[0] if row else ""


def first_topic_by_meeting(conn, meeting_ids: list[str]) -> dict[str, str]:
    if not meeting_ids:
        return {}
    sql = (f"SELECT meeting_id, text, MIN(idx) FROM topics "
           f"WHERE meeting_id IN ({','.join('?' * len(meeting_ids))}) GROUP BY meeting_id")
    return {r[0]: r[1] for r in conn.execute(sql, [str(m) for m in meeting_ids])}


def meeting_speech_hits(conn, word_matches: list[str], meeting_id: str,
                        exact_word_matches: list[str] | None = None) -> list[dict]:
    """Speeches of one meeting matching at least one of the per-word FTS expressions.

    Rows: speech_idx, matched_words (how many of word_matches the speech contains), exact_words
    (how many of exact_word_matches, the words as typed, it contains), relevance (summed -bm25 over
    the matched words, higher = better), ordered by speech_idx.
    """
    speech_idx_by_rowid = _meeting_speech_idx_by_rowid(conn, meeting_id)
    if not speech_idx_by_rowid:
        return []
    sql = "SELECT rowid, -bm25(speeches_fts) FROM speeches_fts WHERE speeches_fts MATCH ? AND rowid BETWEEN ? AND ?"
    rowid_range = (min(speech_idx_by_rowid), max(speech_idx_by_rowid))
    hits_by_speech: dict[int, dict] = {}
    for word_match in word_matches:
        for rowid, relevance in conn.execute(sql, (word_match, *rowid_range)):
            if rowid not in speech_idx_by_rowid:
                continue
            speech_idx = speech_idx_by_rowid[rowid]
            hit = hits_by_speech.setdefault(speech_idx, {"speech_idx": speech_idx, "matched_words": 0,
                                                         "exact_words": 0, "relevance": 0.0})
            hit["matched_words"] += 1
            hit["relevance"] += relevance
    for exact_word_match in exact_word_matches or []:
        for rowid, _relevance in conn.execute(sql, (exact_word_match, *rowid_range)):
            if speech_idx_by_rowid.get(rowid) in hits_by_speech:
                hits_by_speech[speech_idx_by_rowid[rowid]]["exact_words"] += 1
    return [hits_by_speech[idx] for idx in sorted(hits_by_speech)]


def _meeting_speech_idx_by_rowid(conn, meeting_id: str) -> dict[int, int]:
    """speeches_fts rowid -> speech idx of one meeting. The FTS queries filter by this meeting's rowid range
    instead of joining speeches: with the join SQLite runs the MATCH once per speech of the meeting."""
    return dict(conn.execute("SELECT id, idx FROM speeches WHERE meeting_id = ?", (str(meeting_id),)).fetchall())


_HIGHLIGHT_OPEN, _HIGHLIGHT_CLOSE = "", ""


def _ranges_between_markers(highlighted: str) -> list[list[int]]:
    ranges, plain_length, open_at = [], 0, None
    for char in highlighted:
        if char == _HIGHLIGHT_OPEN:
            open_at = plain_length
        elif char == _HIGHLIGHT_CLOSE:
            if open_at is not None and plain_length > open_at:
                ranges.append([open_at, plain_length - open_at])
            open_at = None
        else:
            plain_length += 1
    return ranges


def meeting_speech_keyword_ranges(conn, word_matches: list[str], meeting_id: str) -> dict[int, list[list[int]]]:
    """speech_idx -> [[offset, length], ...] of the tokens any of word_matches matches, in the stored
    speech text (the text the reading tab shows), ordered by offset."""
    if not word_matches:
        return {}
    speech_idx_by_rowid = _meeting_speech_idx_by_rowid(conn, meeting_id)
    if not speech_idx_by_rowid:
        return {}
    any_word_match = " OR ".join(f"({word_match})" for word_match in word_matches)
    sql = ("SELECT rowid, highlight(speeches_fts, 0, ?, ?) FROM speeches_fts "
           "WHERE speeches_fts MATCH ? AND rowid BETWEEN ? AND ?")
    return {speech_idx_by_rowid[rowid]: _ranges_between_markers(highlighted)
            for rowid, highlighted in conn.execute(
                sql, (_HIGHLIGHT_OPEN, _HIGHLIGHT_CLOSE, any_word_match,
                      min(speech_idx_by_rowid), max(speech_idx_by_rowid)))
            if rowid in speech_idx_by_rowid}


def table_row_counts(conn, tables: tuple[str, ...] = ("meetings", "topics", "opinions", "speeches")) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
