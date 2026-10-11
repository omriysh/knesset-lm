"""
profiles/subjects.py

The shared subjects of a Knesset, from knesset.db (subjects / subject_approaches / subject_themes), for the
subjects tab: umbrella areas with their subjects, a subject with its approaches, and an approach with the MK
themes it groups.
"""

import json
import sqlite3

from profiles.mk_activity import _THEME_FIELDS, _add_theme_details, _quarters_between

_SUBJECT_FIELDS = ("id", "umbrella", "name", "description", "mk_count", "theme_count", "approach_count")
_APPROACH_FIELDS = ("id", "rank", "name", "summary", "mk_count")


def _party_counts(raw: str) -> list[list]:
    return sorted(([party, count] for party, count in json.loads(raw).items()), key=lambda pair: -pair[1])


def subject_areas(conn: sqlite3.Connection, knesset_num: int) -> list[dict]:
    """Umbrella areas, most MKs first, each with its distinct MKs and themes and its subjects (most MKs first)."""
    areas: dict[str, dict] = {}
    for row in conn.execute(f"""SELECT {', '.join(_SUBJECT_FIELDS)} FROM subjects WHERE knesset_num = ?
                                ORDER BY mk_count DESC""", (knesset_num,)):
        subject = dict(zip(_SUBJECT_FIELDS, row))
        areas.setdefault(subject["umbrella"], {"name": subject["umbrella"], "subjects": []})["subjects"].append(subject)
    for umbrella, mk_count, theme_count in conn.execute("""
            SELECT s.umbrella, COUNT(DISTINCT t.mk_id), COUNT(DISTINCT t.id) FROM subject_themes st
            JOIN subjects s ON s.id = st.subject_id JOIN mk_themes t ON t.id = st.theme_id
            WHERE s.knesset_num = ? GROUP BY s.umbrella""", (knesset_num,)):
        areas[umbrella].update(mk_count=mk_count, theme_count=theme_count)
    return sorted(areas.values(), key=lambda area: -area.get("mk_count", 0))


def subject(conn: sqlite3.Connection, subject_id: int) -> dict | None:
    """A subject with its background, its party counts and its approaches in rank order. None when unknown."""
    row = conn.execute(f"SELECT {', '.join(_SUBJECT_FIELDS)}, background, party_counts, knesset_num FROM subjects WHERE id = ?",
                       (subject_id,)).fetchone()
    if row is None:
        return None
    result = dict(zip(_SUBJECT_FIELDS, row))
    result["background"], result["party_counts"], result["knesset_num"] = row[-3], _party_counts(row[-2]), row[-1]
    theme_counts = dict(conn.execute("""SELECT approach_id, COUNT(*) FROM subject_themes WHERE subject_id = ?
                                        GROUP BY approach_id""", (subject_id,)).fetchall())
    result["approaches"] = [
        {**dict(zip(_APPROACH_FIELDS, approach[:-1])), "party_counts": _party_counts(approach[-1]),
         "theme_count": theme_counts.get(approach[0], 0)}
        for approach in conn.execute(f"""SELECT {', '.join(_APPROACH_FIELDS)}, party_counts FROM subject_approaches
                                         WHERE subject_id = ? ORDER BY rank""", (subject_id,))]
    return result


def approach_themes(conn: sqlite3.Connection, subject_id: int, approach_id: int) -> dict | None:
    """
    The MK themes grouped under one approach of a subject, most opinions first, each with its MK (mk_id,
    name, roster party) and per-quarter opinion counts over the quarters every one of the themes spans.
    None when the approach is not one of the subject's.
    """
    if conn.execute("SELECT 1 FROM subject_approaches WHERE id = ? AND subject_id = ?",
                    (approach_id, subject_id)).fetchone() is None:
        return None
    rows = conn.execute(f"""
        SELECT {', '.join('t.' + field for field in _THEME_FIELDS)}, t.mk_id, mk.full_name, mk.party
        FROM subject_themes st JOIN mk_themes t ON t.id = st.theme_id
        LEFT JOIN mks mk ON mk.mk_id = t.mk_id AND mk.knesset_num = t.knesset_num
        WHERE st.subject_id = ? AND st.approach_id = ? ORDER BY t.opinion_count DESC""",
                        (subject_id, approach_id)).fetchall()
    themes = [{**dict(zip(_THEME_FIELDS, row)), "mk": {"mk_id": row[-3], "name": row[-2] or "", "party": row[-1] or ""}}
              for row in rows]
    first_dates = [theme["first_date"] for theme in themes if theme["first_date"]]
    last_dates = [theme["last_date"] for theme in themes if theme["last_date"]]
    quarters = _quarters_between(min(first_dates), max(last_dates)) if first_dates and last_dates else []
    for theme in themes:
        _add_theme_details(conn, theme, quarters)
    return {"themes": themes, "quarters": quarters}


def theme_owner(conn: sqlite3.Connection, theme_id: int) -> tuple[str, int] | None:
    """(mk_id, knesset_num) of a theme in some subject, else None."""
    return conn.execute("""SELECT t.mk_id, t.knesset_num FROM mk_themes t
                           WHERE t.id = ? AND EXISTS (SELECT 1 FROM subject_themes st WHERE st.theme_id = t.id)""",
                        (theme_id,)).fetchone()


def process_counts(conn: sqlite3.Connection, knesset_num: int) -> dict:
    """How many protocols, MK opinions, MK themes, subjects, approaches and areas each step of the subjects made."""
    protocols, opinions = conn.execute("""SELECT COUNT(DISTINCT meeting_id), COUNT(*) FROM opinions
                                          WHERE knesset_num = ? AND mk_id IS NOT NULL""", (knesset_num,)).fetchone()
    themes, mks = conn.execute("SELECT COUNT(*), COUNT(DISTINCT mk_id) FROM mk_themes WHERE knesset_num = ?",
                               (knesset_num,)).fetchone()
    subject_count, areas = conn.execute("SELECT COUNT(*), COUNT(DISTINCT umbrella) FROM subjects WHERE knesset_num = ?",
                                        (knesset_num,)).fetchone()
    approaches = conn.execute("SELECT COUNT(*) FROM subject_approaches WHERE knesset_num = ?", (knesset_num,)).fetchone()[0]
    return {"protocols": protocols, "opinions": opinions, "themes": themes, "mks": mks, "subjects": subject_count,
            "approaches": approaches, "areas": areas}


def game_subjects(conn: sqlite3.Connection, knesset_num: int) -> dict[int, tuple[int, int | None]]:
    """theme id → (subject id, approach id) of the theme's subject with the most MKs, its one subject in the game."""
    chosen: dict[int, tuple[int, int | None]] = {}
    for theme_id, subject_id, approach_id in conn.execute("""
            SELECT st.theme_id, st.subject_id, st.approach_id FROM subject_themes st JOIN subjects s ON s.id = st.subject_id
            WHERE s.knesset_num = ? ORDER BY st.theme_id, s.mk_count DESC, s.id""", (knesset_num,)):
        chosen.setdefault(theme_id, (subject_id, approach_id))
    return chosen


def subject_names(conn: sqlite3.Connection, subject_ids: list[int]) -> dict[int, dict]:
    """subject id → {id, name, umbrella}."""
    if not subject_ids:
        return {}
    rows = conn.execute(f"SELECT id, name, umbrella FROM subjects WHERE id IN ({','.join('?' * len(subject_ids))})", subject_ids)
    return {row[0]: {"id": row[0], "name": row[1], "umbrella": row[2]} for row in rows}


def approach_names(conn: sqlite3.Connection, approach_ids: list[int]) -> dict[int, str]:
    """approach id → name."""
    if not approach_ids:
        return {}
    return dict(conn.execute(f"SELECT id, name FROM subject_approaches WHERE id IN ({','.join('?' * len(approach_ids))})", approach_ids))
