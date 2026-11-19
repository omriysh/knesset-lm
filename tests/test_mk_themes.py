"""
tests/test_mk_themes.py

Tests for summarization.mk_themes (per-MK theme requests and answers, against the real
knesset.db opened read-only) and for the mk_themes tables of retrieval.knesset_db_store.
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
from retrieval import knesset_db_store as store
from summarization import mk_themes

KNESSET_NUM = 25
MK_WITH_FEW_OPINIONS = "30893"
MK_WITH_MANY_OPINIONS = "30807"


@pytest.fixture(scope="module")
def real_db():
    if not Path(config.KNESSET_DB).exists():
        pytest.skip(f"{config.KNESSET_DB} missing")
    conn = sqlite3.connect(f"file:{Path(config.KNESSET_DB).as_posix()}?mode=ro", uri=True, timeout=30)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def few_opinions(real_db):
    opinions = mk_themes.load_mk_opinions(real_db, MK_WITH_FEW_OPINIONS, KNESSET_NUM)
    assert opinions, f"MK {MK_WITH_FEW_OPINIONS} has no opinions in knesset.db"
    return opinions


# ── request building (real data) ──────────────────────────────────────────────

@pytest.mark.parametrize("mk_id", [MK_WITH_FEW_OPINIONS, MK_WITH_MANY_OPINIONS])
def test_local_ids_run_in_prompt_order(real_db, mk_id):
    opinions = mk_themes.load_mk_opinions(real_db, mk_id, KNESSET_NUM)
    assert [o["local_id"] for o in opinions] == list(range(1, len(opinions) + 1))

    committee_blocks = []
    for opinion in opinions:
        if not committee_blocks or committee_blocks[-1][0] != opinion["committee"]:
            committee_blocks.append((opinion["committee"], []))
        committee_blocks[-1][1].append(opinion)
    committees_in_order = [committee for committee, _ in committee_blocks]
    assert len(committees_in_order) == len(set(committees_in_order)), "a committee's opinions are not contiguous"
    block_sizes = [len(block) for _, block in committee_blocks]
    assert block_sizes == sorted(block_sizes, reverse=True)
    for _, block in committee_blocks:
        dates = [o["date"] for o in block]
        assert dates == sorted(dates)


def test_user_text_lists_ids_in_ascending_order(real_db, few_opinions):
    user_text = mk_themes.build_user_text("חבר כנסת לבדיקה", few_opinions)
    listed_ids = [int(match) for match in re.findall(r"^\[(\d+)\]", user_text, flags=re.M)]
    assert listed_ids == list(range(1, len(few_opinions) + 1))
    assert len(re.findall(r"^## ", user_text, flags=re.M)) == len({o["committee"] for o in few_opinions})


def test_opinion_counts_match_loaded_opinions(real_db, few_opinions):
    counts = mk_themes.opinion_counts_by_mk(real_db, KNESSET_NUM)
    assert counts[MK_WITH_FEW_OPINIONS] == len(few_opinions)
    assert None not in counts


def test_mk_name_comes_from_the_roster(real_db):
    name = mk_themes.load_mk_name(real_db, MK_WITH_MANY_OPINIONS, KNESSET_NUM)
    assert name and name != MK_WITH_MANY_OPINIONS


# ── answer parsing ────────────────────────────────────────────────────────────

def test_parse_themes_maps_local_ids_and_drops_unknown(few_opinions):
    answer = json.dumps({"themes": [
        {"title": "נושא א", "summary": "לטענתו...", "opinion_ids": [2, 1, 2, 9999]},
        {"title": "נושא ב", "summary": "לדבריו...", "opinion_ids": []},
    ]})
    themes, unknown_ids = mk_themes.parse_themes(answer, few_opinions)
    assert unknown_ids == [9999]
    assert [o["local_id"] for o in themes[0]["evidence"]] == [2, 1]
    assert themes[1]["evidence"] == []


@pytest.mark.parametrize("answer", ["not json", "{\"no_themes\": []}", "{\"themes\": 5}", ""])
def test_parse_themes_rejects_malformed_answers(answer, few_opinions):
    with pytest.raises(ValueError):
        mk_themes.parse_themes(answer, few_opinions)


def _opinion(local_id: int, meeting_id: str, date: str) -> dict:
    return {"local_id": local_id, "meeting_id": meeting_id, "idx": local_id, "date": date}


def test_order_themes_averages_model_and_persistence_ranks():
    central_but_rare = {"title": "central", "evidence": [_opinion(1, "m1", "2024-01-01")]}
    broad_and_persistent = {"title": "broad", "evidence": [_opinion(i, f"m{i}", f"2024-{i:02d}-01") for i in range(2, 12)]}
    middle = {"title": "middle", "evidence": [_opinion(i, f"m{i}", "2024-05-01") for i in range(12, 16)]}
    ordered = mk_themes.order_themes([central_but_rare, broad_and_persistent, middle])
    assert [t["title"] for t in ordered] == ["broad", "central", "middle"]
    assert {t["title"]: (t["model_rank"], t["persistence_rank"]) for t in ordered} == {
        "central": (1, 3), "broad": (2, 1), "middle": (3, 2)}


def test_order_themes_breaks_ties_by_model_rank():
    first = {"title": "first", "evidence": [_opinion(1, "m1", "2024-01-01")]}
    second = {"title": "second", "evidence": [_opinion(2, "m2", "2024-01-01"), _opinion(3, "m3", "2024-02-01")]}
    assert [t["title"] for t in mk_themes.order_themes([first, second])] == ["first", "second"]


def test_themes_file_payload_keeps_stable_opinion_refs(few_opinions):
    themes, _ = mk_themes.parse_themes(json.dumps({"themes": [
        {"title": "נושא", "summary": "לטענתו...", "opinion_ids": [1, 2]}]}), few_opinions)
    payload = mk_themes.themes_file_payload("1", KNESSET_NUM, "שם", "gemini-x", "low", len(few_opinions),
                                            mk_themes.order_themes(themes))
    theme = payload["themes"][0]
    assert theme["rank"] == 1 and theme["model_rank"] == 1 and theme["persistence_rank"] == 1
    assert theme["opinions"] == [[o["meeting_id"], o["idx"]] for o in few_opinions[:2]]
    assert payload["opinion_count"] == len(few_opinions)
    assert "evidence" not in theme
    json.dumps(payload, ensure_ascii=False)


# ── mk_themes tables ──────────────────────────────────────────────────────────

@pytest.fixture()
def scratch_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "knesset.db")
    conn = store.connect()
    store.insert_meetings(conn, [
        {"meeting_id": "m1", "knesset_num": 25, "committee": "ועדת החוקה", "date": "2024-01-10", "format": "structured"},
        {"meeting_id": "m2", "knesset_num": 25, "committee": "ועדת הכספים", "date": "2024-06-02", "format": "structured"},
    ])
    for meeting_id in ("m1", "m2"):
        store.replace_meeting_summary(conn, meeting_id, 25, "", True, [], [
            {"speaker_label": "קריב", "speaker_name": "גלעד קריב", "mk_id": "1", "opinion": f"עמדה {meeting_id}-{i}"}
            for i in range(2)])
    conn.commit()
    yield conn
    conn.close()


THEME_FILE = {
    "mk_id": "1", "knesset_num": 25, "model": "gemini-x", "generated_at": "2026-10-05T10:00:00",
    "themes": [
        {"rank": 1, "title": "התנגדות לרפורמה המשפטית", "summary": "לטענת קריב...", "model_rank": 1,
         "persistence_rank": 1, "opinions": [["m1", 0], ["m2", 1], ["m9", 0]]},
        {"rank": 2, "title": "קידום תחבורה ציבורית", "summary": "לדברי קריב...", "model_rank": 2,
         "persistence_rank": 2, "opinions": [["m1", 1]]},
    ],
}


def test_replace_mk_themes_derives_dates_and_counts(scratch_db):
    themes_written, links_dropped = store.replace_mk_themes(scratch_db, THEME_FILE)
    assert (themes_written, links_dropped) == (2, 1)
    row = scratch_db.execute("SELECT first_date, last_date, meeting_count, opinion_count, model "
                             "FROM mk_themes WHERE mk_id = '1' AND rank = 1").fetchone()
    assert tuple(row) == ("2024-01-10", "2024-06-02", 2, 2, "gemini-x")


def test_theme_links_survive_an_opinions_rebuild(scratch_db):
    store.replace_mk_themes(scratch_db, THEME_FILE)
    store.replace_meeting_summary(scratch_db, "m1", 25, "", True, [], [
        {"speaker_label": "קריב", "speaker_name": "גלעד קריב", "mk_id": "1", "opinion": f"עמדה חדשה {i}"}
        for i in range(2)])
    linked = scratch_db.execute("""
        SELECT o.opinion FROM mk_themes t
        JOIN mk_theme_opinions l ON l.theme_id = t.id
        JOIN opinions o ON o.meeting_id = l.meeting_id AND o.idx = l.opinion_idx
        WHERE t.mk_id = '1' AND t.rank = 1 ORDER BY o.meeting_id""").fetchall()
    assert [r[0] for r in linked] == ["עמדה חדשה 0", "עמדה m2-1"]


def test_replace_mk_themes_replaces_the_mks_previous_themes(scratch_db):
    store.replace_mk_themes(scratch_db, THEME_FILE)
    store.replace_mk_themes(scratch_db, {**THEME_FILE, "themes": THEME_FILE["themes"][1:]})
    assert scratch_db.execute("SELECT COUNT(*) FROM mk_themes").fetchone()[0] == 1
    assert scratch_db.execute("SELECT COUNT(*) FROM mk_theme_opinions").fetchone()[0] == 1


def test_theme_fts_finds_titles_and_summaries(scratch_db):
    store.replace_mk_themes(scratch_db, THEME_FILE)
    store.rebuild_fts(scratch_db, "mk_themes")
    hits = scratch_db.execute("SELECT rowid FROM mk_themes_fts WHERE mk_themes_fts MATCH ?", ("תחבורה",)).fetchall()
    assert len(hits) == 1


def test_clear_target_removes_themes_of_that_knesset(scratch_db):
    store.replace_mk_themes(scratch_db, THEME_FILE)
    store.clear_target(scratch_db, "mk_themes", 25)
    assert scratch_db.execute("SELECT COUNT(*) FROM mk_themes").fetchone()[0] == 0
    assert scratch_db.execute("SELECT COUNT(*) FROM mk_theme_opinions").fetchone()[0] == 0


def test_build_target_loads_theme_files(scratch_db, tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    import build_knesset_db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    themes_dir = config.mk_themes_dir(25)
    themes_dir.mkdir(parents=True)
    (themes_dir / "1.json").write_text(json.dumps(THEME_FILE, ensure_ascii=False), encoding="utf-8")
    (themes_dir / "broken.json").write_text("{", encoding="utf-8")
    assert build_knesset_db.build_mk_themes(scratch_db, 25, rebuild=True) == 2
    assert scratch_db.execute("SELECT COUNT(*) FROM mk_themes_fts WHERE mk_themes_fts MATCH 'תחבורה'").fetchone()[0] == 1


def test_build_target_without_theme_files_builds_nothing(scratch_db, tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    import build_knesset_db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert build_knesset_db.build_mk_themes(scratch_db, 25, rebuild=True) == 0
    assert "mk_themes" in build_knesset_db.TARGETS_ALLOWED_EMPTY
