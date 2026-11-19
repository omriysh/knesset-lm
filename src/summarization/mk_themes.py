"""
summarization/mk_themes.py

Per-MK themes: the issues an MK keeps coming back to, each backed by the MK's opinions
(prompt: summarization.prompts.SYSTEM_PROMPT_MK_THEMES; batch runner: scripts/summarize_mk_themes_batches.py).

The request lists every opinion of the MK in knesset.db, grouped under a heading per committee (most
opinions first, then by date). Opinions carry short local ids, 1..N in that same order, so each committee's
ids are consecutive. The answer's ids are mapped back to (meeting_id, idx), which stays stable across db rebuilds
(opinions.id does not).

The model ranks themes by centrality; order_themes orders them by the average of that rank and a persistence
rank (separate meetings, then active months, then opinion count). Persistence alone favored broad themes, and
asking the model to rank by persistence made it drop real themes.
"""

import json
import sqlite3
from collections import Counter
from datetime import datetime


def opinion_counts_by_mk(conn: sqlite3.Connection, knesset_num: int) -> dict[str, int]:
    return dict(conn.execute(
        "SELECT mk_id, COUNT(*) FROM opinions WHERE knesset_num = ? AND mk_id IS NOT NULL GROUP BY mk_id",
        (knesset_num,)).fetchall())


def load_mk_name(conn: sqlite3.Connection, mk_id: str, knesset_num: int) -> str:
    row = conn.execute("SELECT full_name FROM mks WHERE mk_id = ? AND knesset_num = ?", (mk_id, knesset_num)).fetchone()
    return row[0] if row else mk_id


def load_mk_opinions(conn: sqlite3.Connection, mk_id: str, knesset_num: int) -> list[dict]:
    rows = conn.execute("""
        SELECT o.meeting_id, o.idx, m.date, m.committee, o.opinion
        FROM opinions o JOIN meetings m ON m.meeting_id = o.meeting_id
        WHERE o.mk_id = ? AND o.knesset_num = ?
        ORDER BY m.date, o.meeting_id, o.idx
    """, (mk_id, knesset_num)).fetchall()
    opinions_per_committee = Counter(row[3] for row in rows)
    rows_in_prompt_order = sorted(rows, key=lambda row: (-opinions_per_committee[row[3]], row[3]))
    return [{"local_id": local_id, "meeting_id": meeting_id, "idx": idx, "date": date, "committee": committee,
             "opinion": opinion}
            for local_id, (meeting_id, idx, date, committee, opinion) in enumerate(rows_in_prompt_order, start=1)]


def build_user_text(mk_name: str, opinions: list[dict]) -> str:
    """opinions in load_mk_opinions order; a committee heading starts every run of one committee."""
    opinions_by_committee: dict[str, list[dict]] = {}
    for opinion in opinions:
        opinions_by_committee.setdefault(opinion["committee"], []).append(opinion)
    sections = []
    for committee, committee_opinions in opinions_by_committee.items():
        lines = [f"[{o['local_id']}] {o['date']} | {o['opinion']}" for o in committee_opinions]
        sections.append(f"## {committee} ({len(committee_opinions)} עמדות)\n" + "\n".join(lines))
    return f"חבר/ת הכנסת: {mk_name}\nמספר עמדות: {len(opinions)}\n\n" + "\n\n".join(sections)


def parse_themes(answer_text: str, opinions: list[dict]) -> tuple[list[dict], list[int]]:
    """
    The model's JSON answer -> ([{title, summary, evidence: [opinion, ...]}, ...] in the model's order, unknown ids).
    opinions need local_id, meeting_id, idx and date. Ids that were not in the request are dropped and returned.
    Raises ValueError on an answer that is not the expected JSON.
    """
    try:
        raw_themes = json.loads(answer_text)["themes"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"answer is not the themes JSON: {exc}") from exc
    if not isinstance(raw_themes, list):
        raise ValueError(f"themes is a {type(raw_themes).__name__}, not a list")

    by_local_id = {o["local_id"]: o for o in opinions}
    unknown_ids = []
    themes = []
    for raw_theme in raw_themes:
        evidence = []
        for local_id in dict.fromkeys(raw_theme.get("opinion_ids") or []):
            if local_id in by_local_id:
                evidence.append(by_local_id[local_id])
            else:
                unknown_ids.append(local_id)
        themes.append({"title": raw_theme.get("title", ""), "summary": raw_theme.get("summary", ""),
                       "evidence": evidence})
    return themes, unknown_ids


def _persistence_key(theme: dict) -> tuple[int, int, int]:
    evidence = theme["evidence"]
    return (len({o["meeting_id"] for o in evidence}), len({o["date"][:7] for o in evidence}), len(evidence))


def order_themes(themes: list[dict]) -> list[dict]:
    """themes in the model's order -> ordered by the average of model rank and persistence rank (ties: model rank)."""
    for model_rank, theme in enumerate(themes, start=1):
        theme["model_rank"] = model_rank
    for persistence_rank, theme in enumerate(sorted(themes, key=_persistence_key, reverse=True), start=1):
        theme["persistence_rank"] = persistence_rank
    return sorted(themes, key=lambda theme: ((theme["model_rank"] + theme["persistence_rank"]) / 2,
                                             theme["model_rank"]))


def themes_file_payload(mk_id: str, knesset_num: int, mk_name: str, model: str, thinking_level: str,
                        opinion_count: int, ordered_themes: list[dict]) -> dict:
    """The Data/mk_themes/<knesset>/<mk_id>.json content (read by build_knesset_db.py --target mk_themes)."""
    return {
        "mk_id":          mk_id,
        "knesset_num":    knesset_num,
        "mk_name":        mk_name,
        "model":          model,
        "thinking_level": thinking_level,
        "generated_at":   datetime.now().isoformat(timespec="seconds"),
        "opinion_count":  opinion_count,
        "themes": [{
            "rank":             rank,
            "title":            theme["title"],
            "summary":          theme["summary"],
            "model_rank":       theme["model_rank"],
            "persistence_rank": theme["persistence_rank"],
            "opinions":         [[o["meeting_id"], o["idx"]] for o in theme["evidence"]],
        } for rank, theme in enumerate(ordered_themes, start=1)],
    }
