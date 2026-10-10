"""
tests/test_subjects.py

The MK subjects pipeline (scripts/group_mk_subjects.py, src/subjects/) against a fake Gemini on a scratch
knesset.db, and the subjects tables of retrieval.knesset_db_store.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import pytest

import config
import group_mk_subjects
from retrieval import knesset_db_store as store
from subjects import prompts
from subjects.state import live_subjects, load_state
from subjects.steps import cleaned_consolidated_text, consolidate_subjects

KNESSET_NUM = 25
FIRST_ROUND_KEYWORDS = ("חינוך", "תחבורה")
LEFTOVER_KEYWORD = "דיור"
OFF_TOPIC_MARK = "צדדי"
UNSUPPORTED_MARK = "טענה-לא-נתמכת"

MKS = [("1", "מפלגה א"), ("2", "מפלגה ב"), ("3", "מפלגה א"), ("4", "מפלגה ב")]
THEME_TITLES = {
    "1": ["חינוך 1", "תחבורה 1"],
    "2": ["חינוך 2", "תחבורה 2"],
    "3": ["חינוך 3", "דיור 3"],
    "4": ["תחבורה 4", f"חינוך {OFF_TOPIC_MARK} 4"],
}


def theme_ids_and_titles(text: str) -> list[tuple[int, str]]:
    return [(int(theme_id), title) for theme_id, title in re.findall(r"^\[(\d+)\] (.+)$", text, re.MULTILINE)]


class FakeGemini:
    """Answers each prompt of the pipeline from the keywords in the theme titles."""

    def __init__(self):
        self.calls: list[str] = []
        self.repair_works = False
        self.merge_on_consolidate: list[list[int]] = []

    def call(self, system_prompt: str, user_text: str, response_schema: dict):
        usage = [{"seconds": 0, "finish_reason": "STOP", "input_tokens": 100, "output_tokens": 10, "thinking_tokens": 0}]
        handler = {
            prompts.SHARD_SYSTEM_PROMPT: self.shard, prompts.MERGE_SYSTEM_PROMPT: self.merge,
            prompts.VERIFY_SYSTEM_PROMPT: self.verify, prompts.REPAIR_SYSTEM_PROMPT: self.repair,
            prompts.ASSIGN_SYSTEM_PROMPT: self.assign, prompts.CONSOLIDATE_SYSTEM_PROMPT: self.consolidate,
            prompts.APPROACHES_SYSTEM_PROMPT: self.approaches,
        }[system_prompt]
        self.calls.append(handler.__name__)
        return handler(user_text), usage

    def shard(self, text: str) -> dict:
        themes_text, _, existing_text = text.partition(prompts.EXISTING_SUBJECTS_NOTE)
        keywords = ([keyword for keyword in (*FIRST_ROUND_KEYWORDS, LEFTOVER_KEYWORD) if keyword not in existing_text]
                    if existing_text else FIRST_ROUND_KEYWORDS)
        subjects = []
        for keyword in keywords:
            theme_ids = [theme_id for theme_id, title in theme_ids_and_titles(themes_text) if keyword in title]
            if theme_ids:
                background = f"רקע על {keyword}" + (f" {UNSUPPORTED_MARK}" if keyword == "תחבורה" else "")
                subjects.append({"umbrella": "כללי", "name": keyword, "description": f"כל מה שעל {keyword}",
                                 "background": background, "background_theme_ids": theme_ids, "theme_ids": theme_ids})
        return {"subjects": subjects}

    def merge(self, text: str) -> dict:
        ids_by_name = {}
        background_by_name = {}
        for candidate_id, name, background in re.findall(r"^\[(\d+)\] כללי > (\S+) .*\n(.+)$", text, re.MULTILINE):
            ids_by_name.setdefault(name, []).append(int(candidate_id))
            background_by_name[name] = background
        return {"subjects": [{"umbrella": "כללי", "name": name, "description": f"כל מה שעל {name}",
                              "background": background_by_name[name], "candidate_ids": ids}
                             for name, ids in ids_by_name.items()],
                "duplicate_candidate_ids": []}

    def verify(self, text: str) -> dict:
        background = text.split("# Background\n")[1].split("\n")[0]
        claims = [UNSUPPORTED_MARK] if UNSUPPORTED_MARK in background else []
        return {"unsupported_claims": claims, "understandable": True, "reason": "בדיקה"}

    def repair(self, text: str) -> dict:
        background = text.split("# Background\n")[1].split("\n")[0]
        return {"background": background.replace(f" {UNSUPPORTED_MARK}", "") if self.repair_works else background}

    def assign(self, text: str) -> dict:
        subjects_text, _, themes_text = text.partition("# Themes\n")
        names = re.findall(r"^(\d+)\. (\S+):", subjects_text, re.MULTILINE)
        return {"assignments": [{"theme_id": theme_id,
                                 "subject_ids": [int(number) for number, name in names if name in title]}
                                for theme_id, title in theme_ids_and_titles(themes_text)]}

    def consolidate(self, text: str) -> dict:
        lines = re.findall(r"^\[(\d+)\] (\S+) > (\S+) .*: (.+)$", text, re.MULTILINE)
        merged_ids = {subject_id for group in self.merge_on_consolidate for subject_id in group}
        subjects = [{"umbrella": umbrella, "name": name, "description": description, "subject_ids": [int(subject_id)]}
                    for subject_id, umbrella, name, description in lines if int(subject_id) not in merged_ids]
        subjects += [{"umbrella": "כללי", "name": "מאוחד", "description": "מאוחד", "subject_ids": group}
                     for group in self.merge_on_consolidate]
        return {"subjects": subjects}

    def approaches(self, text: str) -> dict:
        themes = theme_ids_and_titles(text)
        return {"approaches": [{"name": "תמיכה", "summary": "לטענתם...",
                                "theme_ids": [theme_id for theme_id, title in themes if OFF_TOPIC_MARK not in title]}],
                "off_topic_theme_ids": [theme_id for theme_id, title in themes if OFF_TOPIC_MARK in title]}


def theme_file(mk_id: str, titles: list[str], summary_suffix: str = "") -> dict:
    return {"mk_id": mk_id, "knesset_num": KNESSET_NUM, "model": "gemini-x", "generated_at": "2026-10-01T10:00:00",
            "themes": [{"rank": rank, "title": title, "summary": f"סיכום {title}{summary_suffix}", "opinions": []}
                       for rank, title in enumerate(titles, start=1)]}


@pytest.fixture()
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "knesset.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SUBJECTS_LEFTOVER_MIN_THEMES", 1)
    monkeypatch.setattr(config, "SUBJECTS_PARALLEL_CALLS", 1)
    monkeypatch.setenv(config.GOOGLE_API_KEY_ENV, "fake-key")
    fake = FakeGemini()
    monkeypatch.setattr(group_mk_subjects.genai, "Client", lambda **kwargs: None)
    monkeypatch.setattr(group_mk_subjects, "GeminiCaller", lambda client, model, thinking_level: fake)
    conn = store.connect()
    store.insert_mks(conn, [{"mk_id": mk_id, "knesset_num": KNESSET_NUM, "full_name": f"ח\"כ {mk_id}", "party": party}
                            for mk_id, party in MKS])
    for mk_id, titles in THEME_TITLES.items():
        store.replace_mk_themes(conn, theme_file(mk_id, titles))
    yield conn, fake
    conn.close()


def run(**kwargs) -> dict:
    return group_mk_subjects.group_mk_subjects(KNESSET_NUM, **kwargs)


def current_state() -> dict:
    return load_state(config.subjects_dir(KNESSET_NUM) / "subjects.json")


def subject_named(state: dict, name: str) -> dict:
    return next(subject for subject in state["subjects"] if subject["name"] == name)


def refs_of(state: dict, subject: dict) -> set[str]:
    return {ref for ref, entry in state["themes"].items() if subject["subject_id"] in entry["subject_ids"]}


def test_first_run_builds_subjects_and_a_second_run_with_nothing_changed_calls_nothing(scratch):
    conn, fake = scratch
    result = run()
    state = current_state()
    assert result["changed"]
    assert {subject["name"] for subject in live_subjects(state)} == {"חינוך", LEFTOVER_KEYWORD}
    rejected = subject_named(state, "תחבורה")
    assert not rejected["verification"]["verified"] and rejected["repair_count"] == 1
    education = subject_named(state, "חינוך")
    assert refs_of(state, education) == {"1:1", "2:1", "3:1"}
    assert education["off_topic_theme_refs"] == ["4:2"]
    assert [approach["theme_refs"] for approach in education["approaches"]] == [["1:1", "2:1", "3:1"]]
    assert refs_of(state, subject_named(state, LEFTOVER_KEYWORD)) == {"3:2"}
    assert state["consolidated"]

    fake.calls.clear()
    run()
    assert fake.calls == ["shard"], "the off-topic theme lost its only subject after the leftover round: it gets one"
    assert "4:2" in current_state()["leftover_theme_refs"]

    fake.calls.clear()
    third = run()
    assert fake.calls == []
    assert not third["changed"]


def test_repair_again_gives_rejected_subjects_one_more_try(scratch):
    conn, fake = scratch
    run()
    fake.repair_works = True
    fake.calls.clear()
    run()
    assert "repair" not in fake.calls, "a subject at SUBJECTS_MAX_REPAIRS is not repaired without --repair-again"

    run(repair_again=True)
    state = current_state()
    transport = subject_named(state, "תחבורה")
    assert transport["verification"]["verified"] and transport["repair_count"] == 2
    assert UNSUPPORTED_MARK in transport["original_background"]
    assert UNSUPPORTED_MARK in " ".join(transport["verification_before_repair"]["unsupported_claims"])
    assert refs_of(state, transport) == {"1:2", "2:2", "4:1"}
    assert "consolidate" in fake.calls


def test_a_changed_theme_is_assigned_again_and_only_its_subjects_redo_approaches(scratch):
    conn, fake = scratch
    run()
    store.replace_mk_themes(conn, theme_file("2", THEME_TITLES["2"], summary_suffix=" (חדש)"))
    fake.calls.clear()
    run()
    assert fake.calls.count("assign") == 1
    assert fake.calls.count("approaches") == 1, "only the education subject holds a changed theme"
    state = current_state()
    assert "2:1" in refs_of(state, subject_named(state, "חינוך"))


def test_an_off_topic_theme_is_not_assigned_back_to_its_subject(scratch):
    conn, fake = scratch
    run()
    store.replace_mk_themes(conn, theme_file("1", THEME_TITLES["1"], summary_suffix=" (חדש)"))
    run()
    state = current_state()
    assert "4:2" not in refs_of(state, subject_named(state, "חינוך"))


def test_replace_subjects_stores_live_subjects_with_counts(scratch):
    conn, fake = scratch
    run()
    counts = store.replace_subjects(conn, current_state())
    assert counts["subjects"] == 2 and counts["stale_refs"] == 0
    row = conn.execute("SELECT id, mk_count, theme_count, approach_count, party_counts FROM subjects "
                       "WHERE name = 'חינוך'").fetchone()
    assert (row["mk_count"], row["theme_count"], row["approach_count"]) == (3, 3, 1)
    assert json.loads(row["party_counts"]) == {"מפלגה א": 2, "מפלגה ב": 1}
    approach = conn.execute("SELECT id, mk_count, party_counts FROM subject_approaches WHERE subject_id = ?",
                            (row["id"],)).fetchone()
    assert approach["mk_count"] == 3
    linked = conn.execute("SELECT t.mk_id || ':' || t.rank AS ref, l.approach_id FROM subject_themes l "
                          "JOIN mk_themes t ON t.id = l.theme_id WHERE l.subject_id = ? ORDER BY ref", (row["id"],)).fetchall()
    assert [(link["ref"], link["approach_id"]) for link in linked] == [("1:1", approach["id"]), ("2:1", approach["id"]),
                                                                       ("3:1", approach["id"])]
    leftover = conn.execute("SELECT approach_count FROM subjects WHERE name = ?", (LEFTOVER_KEYWORD,)).fetchone()
    assert leftover["approach_count"] == 0, "a subject below SUBJECTS_MIN_MKS_FOR_APPROACHES has no approaches"
    assert conn.execute("SELECT COUNT(*) FROM subjects WHERE name = 'תחבורה'").fetchone()[0] == 0

    store.replace_subjects(conn, current_state())
    assert conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 2, "rebuilding replaces, never duplicates"


def test_replace_subjects_skips_themes_whose_text_changed_since_the_run(scratch):
    conn, fake = scratch
    run()
    store.replace_mk_themes(conn, theme_file("1", THEME_TITLES["1"], summary_suffix=" (חדש)"))
    counts = store.replace_subjects(conn, current_state())
    assert counts["stale_refs"] == 2
    assert conn.execute("SELECT theme_count FROM subjects WHERE name = 'חינוך'").fetchone()[0] == 2


def test_consolidation_remaps_assignments_and_keeps_earlier_merges(scratch):
    conn, fake = scratch
    run()
    state = current_state()
    education, housing = subject_named(state, "חינוך"), subject_named(state, LEFTOVER_KEYWORD)
    education["merged_subject_ids"] = [99]
    fake.merge_on_consolidate = [[education["subject_id"], housing["subject_id"]]]
    consolidate_subjects(fake, state)
    assert housing["merged_into"] == education["subject_id"]
    assert education["merged_subject_ids"] == [99, housing["subject_id"]]
    assert state["themes"]["3:2"]["subject_ids"] == [education["subject_id"]]
    assert education["before_consolidation"]["name"] == "חינוך" and education["name"] == "מאוחד"


@pytest.mark.parametrize("before, after, expected", [
    ('גיוס לצה"ל', "גיוס לצה'ל", 'גיוס לצה"ל'),
    ("גישה ישירה", "המDirect", "גישה ישירה"),
    ("טיפול בפוסט טראומה", "טיפול ב-PTSD", "טיפול ב-PTSD"),
    ("בינה מלאכותית", "AI בממשלה", "AI בממשלה"),
])
def test_cleaned_consolidated_text(before, after, expected):
    assert cleaned_consolidated_text(before, after) == expected
