"""
profiles/mk_activity.py

An MK's committee activity in one Knesset, from knesset.db, for the candidate profile page:
themes (mk_themes) with evidence opinions, the paged opinion list with theme and committee facets,
and committee-meeting attendance against the meetings of the committees they belonged to.
"""

import sqlite3
from collections import Counter

EVIDENCE_PER_THEME = 5
EVIDENCE_QUOTE_CHARS = (30, 230)
OTHER_THEME = "other"
_ATTENDANCE_POSITIONS = ('חבר ועדה', 'יו"ר ועדה')


def _quarter(date: str) -> str:
    return f"{date[:4]}Q{(int(date[5:7]) - 1) // 3 + 1}"


def _quarters_between(first: str, last: str) -> list[str]:
    year, quarter = int(first[:4]), (int(first[5:7]) - 1) // 3 + 1
    quarters = []
    last_quarter = _quarter(last)
    while f"{year}Q{quarter}" <= last_quarter:
        quarters.append(f"{year}Q{quarter}")
        year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    return quarters


_OPINION_COLUMNS = """o.id, o.meeting_id, o.idx, m.date, m.committee, o.opinion, o.quote, o.quote_verified,
                      o.speech_idx, o.quote_offset, o.quote_length"""


def _opinion_row(row) -> dict:
    (opinion_id, meeting_id, idx, date, committee, opinion, quote, verified,
     speech_idx, quote_offset, quote_length) = row
    return {"id": opinion_id, "meeting_id": meeting_id, "idx": idx, "date": date, "committee": committee,
            "opinion": opinion, "quote": quote, "quote_verified": bool(verified), "speech_idx": speech_idx,
            "quote_offset": quote_offset, "quote_length": quote_length}


def _theme_ids_by_opinion(conn: sqlite3.Connection, mk_id: str, knesset_num: int) -> dict[tuple[str, int], list[int]]:
    themes_of: dict[tuple[str, int], list[int]] = {}
    for theme_id, meeting_id, idx in conn.execute("""
            SELECT l.theme_id, l.meeting_id, l.opinion_idx FROM mk_theme_opinions l
            JOIN mk_themes t ON t.id = l.theme_id
            WHERE t.mk_id = ? AND t.knesset_num = ? ORDER BY t.rank""", (mk_id, knesset_num)):
        themes_of.setdefault((meeting_id, idx), []).append(theme_id)
    return themes_of


def _spread_evidence(opinions: list[dict]) -> list[dict]:
    """EVIDENCE_PER_THEME opinions spread evenly over the theme's time range, preferring verified short quotes."""
    low, high = EVIDENCE_QUOTE_CHARS
    quotable = [o for o in opinions if o["quote_verified"] and low < len(o["quote"] or "") < high] or opinions
    if len(quotable) <= EVIDENCE_PER_THEME:
        return quotable
    step = (len(quotable) - 1) / (EVIDENCE_PER_THEME - 1)
    return [quotable[round(k * step)] for k in range(EVIDENCE_PER_THEME)]


def mk_themes(conn: sqlite3.Connection, mk_id: str, knesset_num: int) -> dict:
    """Themes in rank order, each with counts, top committees, a per-quarter opinion count and evidence."""
    themes = [dict(zip(("id", "rank", "title", "summary", "first_date", "last_date", "meeting_count",
                        "opinion_count"), row))
              for row in conn.execute("""
                  SELECT id, rank, title, summary, first_date, last_date, meeting_count, opinion_count
                  FROM mk_themes WHERE mk_id = ? AND knesset_num = ? ORDER BY rank""", (mk_id, knesset_num))]
    total_opinions, first_date, last_date = conn.execute(
        """SELECT COUNT(*), MIN(m.date), MAX(m.date) FROM opinions o JOIN meetings m ON m.meeting_id = o.meeting_id
           WHERE o.mk_id = ? AND o.knesset_num = ?""", (mk_id, knesset_num)).fetchone()
    in_a_theme = conn.execute("""
        SELECT COUNT(*) FROM (SELECT DISTINCT l.meeting_id, l.opinion_idx FROM mk_theme_opinions l
                              JOIN mk_themes t ON t.id = l.theme_id WHERE t.mk_id = ? AND t.knesset_num = ?)""",
                              (mk_id, knesset_num)).fetchone()[0]
    quarters = _quarters_between(first_date, last_date) if first_date else []
    for theme in themes:
        opinions = [_opinion_row(row) for row in conn.execute(f"""
            SELECT {_OPINION_COLUMNS} FROM mk_theme_opinions l
            JOIN opinions o ON o.meeting_id = l.meeting_id AND o.idx = l.opinion_idx
            JOIN meetings m ON m.meeting_id = o.meeting_id
            WHERE l.theme_id = ? ORDER BY m.date, o.meeting_id, o.idx""", (theme["id"],))]
        per_quarter = Counter(_quarter(o["date"]) for o in opinions)
        theme["quarter_counts"] = [per_quarter.get(q, 0) for q in quarters]
        theme["top_committees"] = Counter(o["committee"] for o in opinions).most_common(3)
        theme["evidence"] = _spread_evidence(opinions)
    return {"themes": themes, "quarters": quarters, "total_opinions": total_opinions,
            "opinions_in_a_theme": in_a_theme}


def mk_opinions(conn: sqlite3.Connection, mk_id: str, knesset_num: int, *, fts_match: str | None = None,
                theme: str | None = None, committee: str | None = None, date_from: str | None = None,
                date_to: str | None = None, offset: int = 0, limit: int = 30) -> dict:
    """
    One page of the MK's opinions, newest first, each with its theme ids and the meeting's first topic.
    fts_match is an opinions_fts MATCH expression; theme is a theme id or OTHER_THEME (opinions in no theme);
    date_from / date_to are inclusive ISO dates of the meeting.
    facets count every opinion of the MK (not only the matches) per theme and per committee.
    """
    themes_of = _theme_ids_by_opinion(conn, mk_id, knesset_num)
    where, params = ["o.mk_id = ?", "o.knesset_num = ?"], [mk_id, knesset_num]
    if fts_match:
        where.append("o.id IN (SELECT rowid FROM opinions_fts WHERE opinions_fts MATCH ?)")
        params.append(fts_match)
    if committee:
        where.append("m.committee = ?")
        params.append(committee)
    if date_from:
        where.append("m.date >= ?")
        params.append(date_from)
    if date_to:
        where.append("m.date <= ?")
        params.append(date_to)
    rows = [_opinion_row(row) for row in conn.execute(f"""
        SELECT {_OPINION_COLUMNS} FROM opinions o JOIN meetings m ON m.meeting_id = o.meeting_id
        WHERE {' AND '.join(where)} ORDER BY m.date DESC, o.meeting_id DESC, o.idx""", params)]
    for row in rows:
        row["theme_ids"] = themes_of.get((row["meeting_id"], row["idx"]), [])
    if theme == OTHER_THEME:
        rows = [row for row in rows if not row["theme_ids"]]
    elif theme:
        rows = [row for row in rows if int(theme) in row["theme_ids"]]
    page = rows[offset:offset + limit]
    topics = dict(conn.execute(
        f"SELECT meeting_id, text FROM topics WHERE idx = 0 AND meeting_id IN ({','.join('?' * len(page))})",
        [row["meeting_id"] for row in page]).fetchall()) if page else {}
    for row in page:
        row["meeting_topic"] = topics.get(row["meeting_id"], "")

    all_rows = conn.execute("""SELECT o.meeting_id, o.idx, m.committee FROM opinions o
                               JOIN meetings m ON m.meeting_id = o.meeting_id
                               WHERE o.mk_id = ? AND o.knesset_num = ?""", (mk_id, knesset_num)).fetchall()
    theme_counts = Counter()
    for meeting_id, idx, _ in all_rows:
        theme_counts.update(themes_of.get((meeting_id, idx)) or [OTHER_THEME])
    return {"opinions": page, "total": len(rows), "offset": offset,
            "facets": {"themes": {str(key): count for key, count in theme_counts.items()},
                       "committees": Counter(row[2] for row in all_rows).most_common()}}


def committee_attendance(conn: sqlite3.Connection, mk_id: str, knesset_num: int, committee_positions: list[dict]) -> dict:
    """
    Meetings the MK attended out of the meetings held by committees they were a member or chair of,
    while they were (substitute memberships are left out), plus every meeting they attended.
    committee_positions are knesset_db.get_mk_positions rows.
    """
    committee_names = dict(conn.execute("SELECT committee_id, name FROM committees WHERE knesset_num = ?",
                                        (knesset_num,)).fetchall())
    member_meetings: set[str] = set()
    for position in committee_positions:
        name = committee_names.get(str(position.get("committee_id")))
        if not name or position.get("position") not in _ATTENDANCE_POSITIONS:
            continue
        member_meetings.update(meeting_id for (meeting_id,) in conn.execute(
            "SELECT meeting_id FROM meetings WHERE knesset_num = ? AND committee = ? AND date >= ? AND date <= ?",
            (knesset_num, name, (position.get("start_date") or "")[:10], (position.get("finish_date") or "9999")[:10])))
    attended = {meeting_id for (meeting_id,) in conn.execute(
        "SELECT DISTINCT meeting_id FROM attendance WHERE mk_id = ? AND knesset_num = ?", (mk_id, knesset_num))}
    per_committee = [[committee, count] for committee, count in conn.execute("""
        SELECT m.committee, COUNT(DISTINCT a.meeting_id) FROM attendance a JOIN meetings m ON m.meeting_id = a.meeting_id
        WHERE a.mk_id = ? AND a.knesset_num = ? GROUP BY m.committee ORDER BY 2 DESC""", (mk_id, knesset_num))]
    return {"meetings_attended": len(attended), "member_meetings": len(member_meetings),
            "member_meetings_attended": len(attended & member_meetings), "per_committee": per_committee}


def opinion_counts(conn: sqlite3.Connection, mk_id: str, knesset_num: int) -> dict:
    total, verified, meetings = conn.execute(
        "SELECT COUNT(*), SUM(quote_verified), COUNT(DISTINCT meeting_id) FROM opinions WHERE mk_id = ? AND knesset_num = ?",
        (mk_id, knesset_num)).fetchone()
    return {"opinions": total, "verified": verified or 0, "meetings_spoke": meetings}
