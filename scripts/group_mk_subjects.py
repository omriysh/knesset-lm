"""
group_mk_subjects.py

Groups the MK themes of a Knesset (mk_themes in Data/knesset.db) into shared subjects, each with the approaches
MKs take to it ("transposed" profiles). Plan: Documentation/KnessetLM/Development/Claude/mk-subjects-plan.md.

Steps (src/subjects/steps.py; prompts in src/subjects/prompts.py), each skipped when it has nothing to do:
  sync          themes that are new or whose text changed lose their assignments and are assigned again
  pass 1        (first run or --from-scratch) themes in shards of whole MKs -> candidate subjects with an umbrella,
                a description and a background written only from the theme summaries; one call merges them;
                every background is verified against its summaries, and one with unsupported claims is rewritten
                without them and verified again. Subjects that still fail are kept in the file, unused.
  repair        rejected subjects get another rewrite while under SUBJECTS_MAX_REPAIRS (--repair-again: one more
                try each, whatever their count)
  assign        changed and new themes against the subjects; every theme against subjects never assigned
  leftover      pass 1 on the themes without a subject, when at least SUBJECTS_LEFTOVER_MIN_THEMES of them never
                went through a leftover round, then every theme against the new subjects
  consolidate   when new subjects appeared (or --consolidate): one call merges duplicates and unifies umbrellas;
                assignments of merged subjects move to the subject they were merged into
  approaches    per subject with at least SUBJECTS_MIN_MKS_FOR_APPROACHES MKs whose themes changed since its
                approaches: the approaches, each with a summary; themes found off topic leave the subject
High-thinking calls that run out of output tokens are retried with medium thinking.

State: Data/subjects/<k>/subjects.json, saved after every step; the previous file is copied to history/ first.
Themes are named "<mk_id>:<rank>" (mk_themes.id changes on every themes load) with a fingerprint of their text:

    {"knesset_num", "model", "thinking_level", "generated_at", "usage": {"<step>_<stamp>": {calls, tokens, cost_usd}},
     "consolidated", "leftover_theme_refs": [ref, ...],
     "themes": {ref: {"fingerprint", "subject_ids": [subject_id, ...]}},      # [] = assigned to no subject
     "subjects": [{"subject_id", "round", "umbrella", "name", "description", "background",
                   "background_theme_refs", "proposed_theme_refs", "verification": {"verified", ...}, "assigned",
                   ["original_background", "verification_before_repair", "repair_count"],
                   ["merged_into" | "merged_subject_ids", "before_consolidation"],
                   ["approaches": [{"name", "summary", "theme_refs"}], "approaches_theme_keys"],
                   ["off_topic_theme_refs"]}]}

`build_knesset_db.py --target subjects` loads the live subjects (verified, not merged) into the subjects,
subject_approaches and subject_themes tables.

Usage
-----
    cd knesset-lm
    python scripts/group_mk_subjects.py --knesset 25 --dry-run      # what would run and its cost, no Gemini call
    python scripts/group_mk_subjects.py --knesset 25                # first run, or bring it up to date
    python scripts/group_mk_subjects.py --knesset 25 --repair-again # one more try for the rejected subjects
    python scripts/group_mk_subjects.py --knesset 25 --subject-id 12 --subject-id 40   # redo their approaches
    python scripts/group_mk_subjects.py --knesset 25 --from-scratch # everything again (after a themes regeneration)
    python scripts/build_knesset_db.py --knesset-num 25 --target subjects
    # scripts/process_knesset.py runs the update after the themes once subjects.json exists

Needs GOOGLE_API_KEY (or GEMINI_API_KEY) in the environment.
"""

import argparse
import math
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from google import genai

import config
from subjects import prompts
from subjects.gemini_calls import GeminiCaller, cost_usd, total_usage
from subjects.state import (apply_assignments, backup_state, empty_state, live_subjects, load_state,
                            load_themes, next_subject_id, save_state, state_path, sync_themes)
from subjects.steps import (assign_against_new_subjects, assign_themes, consolidate_subjects, estimated_tokens,
                            find_approaches, print_report, propose_subjects, repairable_subjects, split_into_shards,
                            verify_subjects)

THINKING_LEVELS = ("none", "minimal", "low", "medium", "high")
GUESSED_OUTPUT_TOKENS_PER_ASSIGNED_THEME = 530
GUESSED_TOKENS_PER_REPAIR = (8_000, 5_000)
GUESSED_TOKENS_PER_SUBJECT_LINE = 80


def leftover_themes(state: dict, themes: list[dict]) -> list[dict]:
    already_in_a_leftover_round = set(state["leftover_theme_refs"])
    return [theme for theme in themes if not state["themes"].get(theme["ref"], {}).get("subject_ids")
            and theme["ref"] not in already_in_a_leftover_round]


def print_full_run_estimate(themes: list[dict], model: str) -> None:
    """Guesses from the first Exp10 run (high thinking): ~$32 for 1,998 themes."""
    shard_count = len(split_into_shards(themes))
    theme_tokens = sum(estimated_tokens(prompts.theme_block(theme)) for theme in themes)
    chunk_count = -(-len(themes) // config.SUBJECTS_ASSIGN_CHUNK_SIZE)
    rows = {
        "pass 1":      (theme_tokens + shard_count * 2_000 + 60_000 + 100 * 5_000, shard_count * 45_000 + 70_000 + 100 * 3_000),
        "assign":      (theme_tokens + chunk_count * 100 * GUESSED_TOKENS_PER_SUBJECT_LINE,
                        len(themes) * GUESSED_OUTPUT_TOKENS_PER_ASSIGNED_THEME),
        "leftover":    (theme_tokens // 3 + 40_000 + theme_tokens, 120_000 + len(themes) * 250),
        "approaches":  (theme_tokens, len(themes) * 420 + 120 * 1_500),
    }
    total = 0.0
    for step, (input_tokens, output_tokens) in rows.items():
        step_cost = cost_usd(model, input_tokens, output_tokens)
        total += step_cost
        print(f"  {step:11} ~{input_tokens:>10,} in, ~{output_tokens:>9,} out -> ~${step_cost:.2f}")
    print(f"  full run: {len(themes)} themes, {shard_count} shards, ~${total:.2f}")


def print_dry_run(state: dict, themes: list[dict], max_repairs: float, first_run: bool, model: str) -> None:
    if first_run:
        print_full_run_estimate(themes, model)
        return
    to_assign = [theme for theme in themes if theme["ref"] not in state["themes"]]
    repairable = repairable_subjects(state["subjects"], max_repairs)
    assigned_subject_count = sum(1 for subject in live_subjects(state) if subject.get("assigned"))
    assign_input = (sum(estimated_tokens(prompts.theme_block(theme)) for theme in to_assign)
                    + -(-len(to_assign) // config.SUBJECTS_ASSIGN_CHUNK_SIZE) * assigned_subject_count
                    * GUESSED_TOKENS_PER_SUBJECT_LINE)
    assign_cost = cost_usd(model, assign_input, len(to_assign) * GUESSED_OUTPUT_TOKENS_PER_ASSIGNED_THEME)
    repair_cost = len(repairable) * cost_usd(model, *GUESSED_TOKENS_PER_REPAIR)
    print(f"  themes to assign: {len(to_assign)} (~${assign_cost:.2f})")
    print(f"  backgrounds to repair: {len(repairable)} (~${repair_cost:.2f}); if any passes, every theme is assigned "
          f"against it (~${cost_usd(model, sum(estimated_tokens(prompts.theme_block(t)) for t in themes), len(themes) * 60):.2f}"
          " per run of new subjects) and consolidation runs again (~$0.25)")
    print(f"  themes without a subject that no leftover round saw: {len(leftover_themes(state, themes))} "
          f"(a leftover round runs from {config.SUBJECTS_LEFTOVER_MIN_THEMES})")
    print("  approaches run for the subjects whose themes change, ~$0.03 each")


def group_mk_subjects(knesset_num: int, *, model: str = config.SUBJECTS_MODEL,
                      thinking_level: str = config.SUBJECTS_THINKING_LEVEL, from_scratch: bool = False,
                      repair_again: bool = False, force_consolidate: bool = False, skip_leftover_round: bool = False,
                      skip_approaches: bool = False, approaches_subject_ids: tuple[int, ...] = (),
                      dry_run: bool = False, subjects_file: Path | None = None) -> dict:
    """Brings the subjects state up to date with the themes in knesset.db. Returns {"changed", "cost_usd",
    "subjects"}. Raises RuntimeError when there are no themes or no Gemini API key."""
    path = subjects_file or state_path(knesset_num)
    conn = sqlite3.connect(f"file:{Path(config.KNESSET_DB).as_posix()}?mode=ro", uri=True)
    try:
        themes = load_themes(conn, knesset_num)
    finally:
        conn.close()
    if not themes:
        raise RuntimeError(f"no themes for Knesset {knesset_num} in {config.KNESSET_DB}")
    themes_by_ref = {theme["ref"]: theme for theme in themes}

    previous_state = load_state(path)
    state = (previous_state if previous_state and not from_scratch
             else empty_state(knesset_num, model, thinking_level))
    if state["knesset_num"] != knesset_num:
        raise RuntimeError(f"{path} is for Knesset {state['knesset_num']}, not {knesset_num}")
    first_run = not state["subjects"]
    max_repairs = math.inf if repair_again else config.SUBJECTS_MAX_REPAIRS
    theme_changes = sync_themes(state, themes)
    print(f"state: {path} ({'new' if first_run else f'{len(live_subjects(state))} subjects'}); themes: "
          f"{len(theme_changes['new'])} new, {len(theme_changes['changed'])} changed, {len(theme_changes['gone'])} gone")
    if dry_run:
        print_dry_run(state, themes, max_repairs, first_run, model)
        return {"changed": False, "cost_usd": 0.0, "subjects": len(live_subjects(state))}

    api_key = os.environ.get(config.GOOGLE_API_KEY_ENV) or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(f"set {config.GOOGLE_API_KEY_ENV} or GEMINI_API_KEY to group MK subjects")
    caller = GeminiCaller(genai.Client(api_key=api_key), model, thinking_level)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_usage_keys = []
    if previous_state:
        print(f"previous state copied to {backup_state(path)}")

    def record(step: str, usage_list: list[dict]) -> None:
        if usage_list:
            usage_key = f"{step}_{stamp}"
            state["usage"][usage_key] = total_usage(usage_list, model)
            run_usage_keys.append(usage_key)
        save_state(path, state)

    record("sync", [])
    live_ids_at_start = {subject["subject_id"] for subject in live_subjects(state)}

    if first_run:
        print("\npass 1: subjects")
        subjects, usage_list = propose_subjects(caller, themes, themes_by_ref, [], next_subject_id(state), "main")
        if not subjects:
            raise RuntimeError("pass 1 produced no subjects")
        state["subjects"].extend(subjects)
        record("pass_1", usage_list)

    if repairable_subjects(state["subjects"], max_repairs):
        print("\nrepairing rejected backgrounds")
        record("repair", verify_subjects(caller, state["subjects"], themes_by_ref, max_repairs))

    assigned_subjects = [subject for subject in live_subjects(state) if subject.get("assigned")]
    to_assign = [theme for theme in themes if theme["ref"] not in state["themes"]]
    if to_assign and assigned_subjects:
        print(f"\nassigning {len(to_assign)} new or changed themes")
        assignments, usage_list = assign_themes(caller, assigned_subjects, to_assign)
        apply_assignments(state, assignments, themes_by_ref)
        record("assign_changed_themes", usage_list)
    record("assign_new_subjects", assign_against_new_subjects(caller, state, themes, themes_by_ref))

    leftover = leftover_themes(state, themes)
    if not skip_leftover_round and len(leftover) >= config.SUBJECTS_LEFTOVER_MIN_THEMES:
        print(f"\nleftover round: {len(leftover)} themes without a subject")
        new_subjects, usage_list = propose_subjects(caller, leftover, themes_by_ref, live_subjects(state),
                                                    next_subject_id(state), "leftover")
        state["subjects"].extend(new_subjects)
        state["leftover_theme_refs"].extend(theme["ref"] for theme in leftover)
        record("leftover_pass_1", usage_list)
        record("leftover_assign", assign_against_new_subjects(caller, state, themes, themes_by_ref))

    new_live_ids = {subject["subject_id"] for subject in live_subjects(state)} - live_ids_at_start
    if force_consolidate or not state["consolidated"] or new_live_ids:
        print(f"\nconsolidating subjects ({len(new_live_ids)} new)")
        usage_list = consolidate_subjects(caller, state)
        state["consolidated"] = True
        record("consolidate", usage_list)
        record("assign_new_subjects_after_consolidation",
               assign_against_new_subjects(caller, state, themes, themes_by_ref))

    if not skip_approaches:
        print("\npass 3: approaches")
        record("approaches", find_approaches(caller, state, themes_by_ref, set(approaches_subject_ids)))

    print_report(state, themes_by_ref)
    run_cost = sum(state["usage"][usage_key]["cost_usd"] for usage_key in run_usage_keys)
    print(f"\nthis run: ${run_cost:.2f} " + ", ".join(f"{usage_key} ${state['usage'][usage_key]['cost_usd']:.2f}"
                                                   for usage_key in run_usage_keys))
    print(f"saved {path}")
    changed = bool(run_usage_keys) or any(theme_changes.values())
    return {"changed": changed, "cost_usd": run_cost, "subjects": len(live_subjects(state))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--knesset", type=int, default=25)
    parser.add_argument("--model", default=config.SUBJECTS_MODEL)
    parser.add_argument("--thinking", default=config.SUBJECTS_THINKING_LEVEL, choices=THINKING_LEVELS)
    parser.add_argument("--from-scratch", action="store_true",
                        help="ignore the current subjects.json (it is kept in history/) and run everything")
    parser.add_argument("--repair-again", action="store_true", help="one more repair try for every rejected subject")
    parser.add_argument("--consolidate", action="store_true", help="consolidate even when no subject is new")
    parser.add_argument("--subject-id", type=int, action="append", default=[], help="redo this subject's approaches")
    parser.add_argument("--skip-leftover-round", action="store_true")
    parser.add_argument("--skip-approaches", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="what would run and a cost estimate, no Gemini call")
    parser.add_argument("--subjects-file", type=Path, help="state file (default Data/subjects/<k>/subjects.json)")
    args = parser.parse_args()
    try:
        group_mk_subjects(args.knesset, model=args.model, thinking_level=args.thinking, from_scratch=args.from_scratch,
                          repair_again=args.repair_again, force_consolidate=args.consolidate,
                          skip_leftover_round=args.skip_leftover_round, skip_approaches=args.skip_approaches,
                          approaches_subject_ids=tuple(args.subject_id), dry_run=args.dry_run,
                          subjects_file=args.subjects_file)
    except RuntimeError as exc:
        sys.exit(f"ERROR: {exc}")


if __name__ == "__main__":
    main()
