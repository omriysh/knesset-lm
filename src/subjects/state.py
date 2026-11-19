"""The MK subjects pipeline state: Data/subjects/<k>/subjects.json (format in scripts/group_mk_subjects.py),
the themes it is computed from, and the bookkeeping between them."""

import json
import os
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import config
from subjects.theme_refs import theme_fingerprint, theme_key, theme_ref


def load_themes(conn: sqlite3.Connection, knesset_num: int) -> list[dict]:
    rows = conn.execute(
        "SELECT t.id, t.mk_id, m.full_name, m.party, t.rank, t.title, t.summary "
        "FROM mk_themes t LEFT JOIN mks m ON m.mk_id = t.mk_id AND m.knesset_num = t.knesset_num "
        "WHERE t.knesset_num = ? ORDER BY t.mk_id, t.rank", (knesset_num,)).fetchall()
    return [{"theme_id": theme_id, "ref": theme_ref(mk_id, rank), "fingerprint": theme_fingerprint(title, summary),
             "mk_id": mk_id, "mk_name": mk_name, "party": party, "rank": rank, "title": title, "summary": summary}
            for theme_id, mk_id, mk_name, party, rank, title, summary in rows]


def state_path(knesset_num: int) -> Path:
    return config.subjects_dir(knesset_num) / "subjects.json"


def empty_state(knesset_num: int, model: str, thinking_level: str) -> dict:
    return {"knesset_num": knesset_num, "model": model, "thinking_level": thinking_level, "generated_at": None,
            "usage": {}, "consolidated": False, "leftover_theme_refs": [], "themes": {}, "subjects": []}


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def backup_state(path: Path) -> Path | None:
    """Copies the current file to history/subjects_<stamp>.json before a run changes it."""
    if not path.exists():
        return None
    backup_path = path.parent / "history" / f"subjects_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_path)
    return backup_path


def save_state(path: Path, state: dict) -> None:
    state["generated_at"] = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary_path, path)


def is_live(subject: dict) -> bool:
    """Verified and not merged into another subject: the subjects that are assigned and stored in the db."""
    return subject.get("verification", {}).get("verified", False) and "merged_into" not in subject


def live_subjects(state: dict) -> list[dict]:
    return [subject for subject in state["subjects"] if is_live(subject)]


def next_subject_id(state: dict) -> int:
    return max((subject["subject_id"] for subject in state["subjects"]), default=0) + 1


def refs_by_subject(state: dict) -> dict[int, list[str]]:
    refs = defaultdict(list)
    for ref, entry in state["themes"].items():
        for subject_id in entry["subject_ids"]:
            refs[subject_id].append(ref)
    return refs


def sync_themes(state: dict, themes: list[dict]) -> dict[str, list[str]]:
    """Forgets every theme that changed or is gone (its assignments, off-topic marks and leftover round), so the
    changed and new ones are assigned again. Returns the refs of each kind."""
    current_fingerprint_by_ref = {theme["ref"]: theme["fingerprint"] for theme in themes}
    changed = [ref for ref, entry in state["themes"].items()
               if ref in current_fingerprint_by_ref and entry["fingerprint"] != current_fingerprint_by_ref[ref]]
    gone = [ref for ref in state["themes"] if ref not in current_fingerprint_by_ref]
    forgotten = set(changed) | set(gone)
    for ref in forgotten:
        del state["themes"][ref]
    for subject in state["subjects"]:
        if subject.get("off_topic_theme_refs"):
            subject["off_topic_theme_refs"] = [ref for ref in subject["off_topic_theme_refs"] if ref not in forgotten]
    state["leftover_theme_refs"] = [ref for ref in state["leftover_theme_refs"] if ref not in forgotten]
    new = [theme["ref"] for theme in themes if theme["ref"] not in state["themes"] and theme["ref"] not in forgotten]
    return {"new": new, "changed": changed, "gone": gone}


def apply_assignments(state: dict, assignments: dict[str, list[int]], themes_by_ref: dict[str, dict]) -> None:
    """Adds subject ids to the themes' assignments, except where pass 3 found the theme off topic."""
    off_topic_pairs = {(ref, subject["subject_id"]) for subject in state["subjects"]
                       for ref in subject.get("off_topic_theme_refs", [])}
    for ref, subject_ids in assignments.items():
        entry = state["themes"].setdefault(ref, {"fingerprint": themes_by_ref[ref]["fingerprint"], "subject_ids": []})
        entry["subject_ids"] = list(dict.fromkeys(
            [*entry["subject_ids"], *(subject_id for subject_id in subject_ids if (ref, subject_id) not in off_topic_pairs)]))


def subject_theme_keys(refs: list[str], themes_by_ref: dict[str, dict]) -> list[str]:
    return sorted(theme_key(ref, themes_by_ref[ref]["fingerprint"]) for ref in refs if ref in themes_by_ref)
