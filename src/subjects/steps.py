"""The steps of the MK subjects pipeline (the flow is in scripts/group_mk_subjects.py). Themes are dicts of
subjects.state.load_themes; subjects and the state refer to themes by ref, the prompts by mk_themes.id."""

import re
from collections import Counter, defaultdict

import config
from subjects import prompts
from subjects.gemini_calls import GeminiCaller, run_parallel
from subjects.state import apply_assignments, live_subjects, refs_by_subject, subject_theme_keys

LATIN_GLUED_TO_HEBREW = re.compile(r"[א-ת][A-Za-z]|[A-Za-z][א-ת]")


def estimated_tokens(text_or_chars: str | int) -> int:
    chars = text_or_chars if isinstance(text_or_chars, int) else len(text_or_chars)
    return int(chars / config.SUBJECTS_CHARS_PER_TOKEN)


def group_by_mk(themes: list[dict]) -> list[list[dict]]:
    themes_by_mk = defaultdict(list)
    for theme in themes:
        themes_by_mk[theme["mk_id"]].append(theme)
    return list(themes_by_mk.values())


def split_into_shards(themes: list[dict]) -> list[list[list[dict]]]:
    """Whole MKs per shard, each shard up to SUBJECTS_SHARD_MAX_TOKENS of theme text, balanced in size."""
    total_tokens = sum(estimated_tokens(prompts.theme_block(theme)) for theme in themes)
    shard_count = max(1, -(-total_tokens // config.SUBJECTS_SHARD_MAX_TOKENS))
    shards = [[] for _ in range(shard_count)]
    shard_tokens = [0] * shard_count
    for mk_themes in sorted(group_by_mk(themes), key=lambda mk_themes: -len(mk_themes)):
        smallest = shard_tokens.index(min(shard_tokens))
        shards[smallest].append(mk_themes)
        shard_tokens[smallest] += sum(estimated_tokens(prompts.theme_block(theme)) for theme in mk_themes)
    return [shard for shard in shards if shard]


def refs_of_ids(theme_ids: list[int], ref_by_theme_id: dict[int, str]) -> list[str]:
    return [ref_by_theme_id[theme_id] for theme_id in dict.fromkeys(theme_ids) if theme_id in ref_by_theme_id]


# ── pass 1: shards → merge → verify ───────────────────────────────────────────

def propose_candidates(caller: GeminiCaller, themes: list[dict],
                       existing_subjects: list[dict]) -> tuple[list[dict], list[dict]]:
    shards = split_into_shards(themes)
    print(f"  {len(themes)} themes in {len(shards)} shards")

    def propose_for_shard(shard: list[list[dict]]) -> tuple[list[dict], list[dict]]:
        shard_themes = [theme for mk_themes in shard for theme in mk_themes]
        result, usage_list = caller.call(prompts.SHARD_SYSTEM_PROMPT,
                                         prompts.build_shard_text(shard, existing_subjects),
                                         prompts.SHARD_RESPONSE_SCHEMA)
        print(f"  shard of {len(shard_themes)} themes: {usage_list[-1]}")
        if result is None:
            print(f"[error] shard of {len(shard_themes)} themes from {shard_themes[0]['ref']} failed")
            return [], usage_list
        ref_by_theme_id = {theme["theme_id"]: theme["ref"] for theme in shard_themes}
        candidates = []
        for subject in result["subjects"]:
            theme_refs = refs_of_ids(subject["theme_ids"], ref_by_theme_id)
            if theme_refs:
                candidates.append({**{key: subject[key] for key in ("umbrella", "name", "description", "background")},
                                   "theme_refs": theme_refs,
                                   "background_theme_refs": refs_of_ids(subject["background_theme_ids"], ref_by_theme_id)})
        return candidates, usage_list

    results = run_parallel(propose_for_shard, shards)
    candidates = [candidate for shard_candidates, _ in results for candidate in shard_candidates]
    for candidate_id, candidate in enumerate(candidates, start=1):
        candidate["candidate_id"] = candidate_id
    return candidates, [usage for _, usage_list in results for usage in usage_list]


def merge_candidates(caller: GeminiCaller, candidates: list[dict],
                     existing_subjects: list[dict]) -> tuple[list[dict], list[dict]]:
    """The merged subjects (empty when the merge call failed) and the usage."""
    result, usage_list = caller.call(prompts.MERGE_SYSTEM_PROMPT, prompts.build_merge_text(candidates, existing_subjects),
                                     prompts.MERGE_RESPONSE_SCHEMA)
    print(f"  merge of {len(candidates)} candidates: {usage_list[-1]}")
    if result is None:
        print(f"[error] merging {len(candidates)} candidates failed: {usage_list}")
        return [], usage_list
    candidates_by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    used_candidate_ids = set()
    subjects = []
    for merged in result["subjects"]:
        merged_candidates = [candidates_by_id[candidate_id] for candidate_id in dict.fromkeys(merged["candidate_ids"])
                             if candidate_id in candidates_by_id and candidate_id not in used_candidate_ids]
        if not merged_candidates:
            continue
        used_candidate_ids.update(candidate["candidate_id"] for candidate in merged_candidates)
        subjects.append({
            **{key: merged[key] for key in ("umbrella", "name", "description", "background")},
            "background_theme_refs": list(dict.fromkeys(ref for candidate in merged_candidates
                                                        for ref in candidate["background_theme_refs"])),
            "proposed_theme_refs": list(dict.fromkeys(ref for candidate in merged_candidates
                                                      for ref in candidate["theme_refs"])),
            "candidates": [{key: candidate[key] for key in ("candidate_id", "umbrella", "name")}
                           for candidate in merged_candidates],
        })
    duplicate_count = len([candidate_id for candidate_id in result["duplicate_candidate_ids"]
                           if candidate_id in candidates_by_id and candidate_id not in used_candidate_ids])
    lost_count = len(candidates_by_id) - len(used_candidate_ids) - duplicate_count
    print(f"  {len(subjects)} subjects, {duplicate_count} candidates duplicate existing subjects, "
          f"{lost_count} candidates left out by the merge")
    return subjects, usage_list


def repairs_done(subject: dict) -> int:
    return subject.get("repair_count", 1 if "original_background" in subject else 0)


def repairable_subjects(subjects: list[dict], max_repairs: int) -> list[dict]:
    return [subject for subject in subjects
            if "verification" in subject and not subject["verification"]["verified"]
            and subject["verification"].get("unsupported_claims") and repairs_done(subject) < max_repairs]


def verify_subjects(caller: GeminiCaller, subjects: list[dict], themes_by_ref: dict[str, dict],
                    max_repairs: int) -> list[dict]:
    """Checks the subjects not checked yet; a background with unsupported claims is rewritten without them (up to
    max_repairs times per subject) and checked again. Subjects that still fail stay unverified."""
    def background_themes(subject: dict) -> list[dict]:
        found = [themes_by_ref[ref] for ref in subject["background_theme_refs"] if ref in themes_by_ref]
        return found[:config.SUBJECTS_MAX_BACKGROUND_THEMES_TO_VERIFY]

    def check(subject: dict) -> list[dict]:
        if not background_themes(subject):
            subject["verification"] = {"verified": False, "reason": "no background themes cited"}
            return []
        result, usage_list = caller.call(prompts.VERIFY_SYSTEM_PROMPT,
                                         prompts.build_verify_text(subject, background_themes(subject)),
                                         prompts.VERIFY_RESPONSE_SCHEMA)
        if result is None:
            subject["verification"] = {"verified": False, "reason": "verification call failed"}
            return usage_list
        subject["verification"] = {"verified": result["understandable"] and not result["unsupported_claims"], **result}
        return usage_list

    def repair_then_check(subject: dict) -> list[dict]:
        result, usage_list = caller.call(prompts.REPAIR_SYSTEM_PROMPT,
                                         prompts.build_repair_text(subject, background_themes(subject)),
                                         prompts.REPAIR_RESPONSE_SCHEMA)
        if result is None:
            print(f"[error] repairing the background of {subject['name']} failed")
            return usage_list
        subject["repair_count"] = repairs_done(subject) + 1
        subject.setdefault("original_background", subject["background"])
        subject.setdefault("verification_before_repair", subject["verification"])
        subject["background"] = result["background"]
        return usage_list + check(subject)

    usage_lists = run_parallel(check, [subject for subject in subjects if "verification" not in subject])
    repairable = repairable_subjects(subjects, max_repairs)
    print(f"  verify: repairing {len(repairable)} backgrounds")
    usage_lists += run_parallel(repair_then_check, repairable)
    failed = [subject for subject in subjects if not subject["verification"]["verified"]]
    repaired_count = sum(1 for subject in repairable if subject["verification"]["verified"])
    print(f"  verify: {len(subjects) - len(failed)}/{len(subjects)} subjects verified ({repaired_count} after repair)")
    for subject in failed:
        print(f"    [not verified] {subject['name']}: {subject['verification'].get('reason')} "
              f"{subject['verification'].get('unsupported_claims', '')}")
    return [usage for usage_list in usage_lists for usage in usage_list]


def propose_subjects(caller: GeminiCaller, themes: list[dict], themes_by_ref: dict[str, dict],
                     existing_subjects: list[dict], first_subject_id: int, round_name: str) -> tuple[list[dict], list[dict]]:
    """Pass 1 over themes: the new subjects (verified or not), numbered from first_subject_id, and the usage."""
    candidates, shard_usage = propose_candidates(caller, themes, existing_subjects)
    if not candidates:
        return [], shard_usage
    subjects, merge_usage = merge_candidates(caller, candidates, existing_subjects)
    for offset, subject in enumerate(subjects):
        subject["subject_id"] = first_subject_id + offset
        subject["round"] = round_name
    verify_usage = verify_subjects(caller, subjects, themes_by_ref, config.SUBJECTS_MAX_REPAIRS)
    return subjects, shard_usage + merge_usage + verify_usage


# ── pass 2: assign ────────────────────────────────────────────────────────────

def assign_chunk(caller: GeminiCaller, subjects: list[dict], chunk: list[dict]) -> tuple[list[dict], list[dict]]:
    """The assignments and the usage of every call made, splitting the chunk in half when a call fails."""
    result, usage_list = caller.call(prompts.ASSIGN_SYSTEM_PROMPT, prompts.build_assign_text(subjects, chunk),
                                     prompts.ASSIGN_RESPONSE_SCHEMA)
    print(f"  {len(chunk)} themes from {chunk[0]['ref']}: {usage_list[-1]}")
    if result is not None:
        return result["assignments"], usage_list
    if len(chunk) < config.SUBJECTS_MIN_ASSIGN_CHUNK_SIZE_TO_SPLIT:
        print(f"[error] giving up on {len(chunk)} themes from {chunk[0]['ref']}")
        return [], usage_list
    print(f"  retrying {len(chunk)} themes from {chunk[0]['ref']} in two halves")
    half = len(chunk) // 2
    first_assignments, first_usage = assign_chunk(caller, subjects, chunk[:half])
    second_assignments, second_usage = assign_chunk(caller, subjects, chunk[half:])
    return first_assignments + second_assignments, [*usage_list, *first_usage, *second_usage]


def assign_themes(caller: GeminiCaller, subjects: list[dict],
                  themes: list[dict]) -> tuple[dict[str, list[int]], list[dict]]:
    """Theme ref -> subject_ids, for the themes that got an answer. Marks the subjects as assigned."""
    chunk_size = config.SUBJECTS_ASSIGN_CHUNK_SIZE
    chunks = [themes[start:start + chunk_size] for start in range(0, len(themes), chunk_size)]
    results = run_parallel(lambda chunk: assign_chunk(caller, subjects, chunk), chunks)
    ref_by_theme_id = {theme["theme_id"]: theme["ref"] for theme in themes}
    assignments = {}
    for chunk_assignments, _ in results:
        for assignment in chunk_assignments:
            if assignment["theme_id"] in ref_by_theme_id:
                assignments[ref_by_theme_id[assignment["theme_id"]]] = [
                    subjects[number - 1]["subject_id"] for number in dict.fromkeys(assignment["subject_ids"])
                    if 1 <= number <= len(subjects)]
    print(f"  assigned {len(assignments)}/{len(themes)} themes against {len(subjects)} subjects")
    if len(assignments) < len(themes):
        print(f"[error] {len(themes) - len(assignments)} themes got no answer")
    for subject in subjects:
        subject["assigned"] = True
    return assignments, [usage for _, usage_list in results for usage in usage_list]


def assign_against_new_subjects(caller: GeminiCaller, state: dict, themes: list[dict],
                                themes_by_ref: dict[str, dict]) -> list[dict]:
    """Every theme against the live subjects that were never assigned; the usage."""
    new_subjects = [subject for subject in live_subjects(state) if not subject.get("assigned")]
    if not new_subjects:
        return []
    print(f"\nassigning all {len(themes)} themes against {len(new_subjects)} new subjects")
    assignments, usage_list = assign_themes(caller, new_subjects, themes)
    apply_assignments(state, assignments, themes_by_ref)
    return usage_list


# ── consolidate ───────────────────────────────────────────────────────────────

def same_but_quotes(first: str, second: str) -> bool:
    return first.replace("'", '"') == second.replace("'", '"')


def cleaned_consolidated_text(before: str, after: str) -> str:
    """The consolidation's rewrite, unless it only swapped gershayim for a geresh or glued Latin into Hebrew."""
    if same_but_quotes(before, after) or LATIN_GLUED_TO_HEBREW.search(after):
        return before
    return after


def consolidate_subjects(caller: GeminiCaller, state: dict) -> list[dict]:
    """Merges duplicate live subjects and unifies umbrellas, in place. A merged group keeps the subject_id of the
    member that was assigned and has the most themes; the others get merged_into, and their theme assignments
    move to it. Earlier merges and the text from before the first consolidation are kept."""
    active_subjects = live_subjects(state)
    theme_count_by_subject = Counter(subject_id for entry in state["themes"].values() for subject_id in entry["subject_ids"])
    result, usage_list = caller.call(prompts.CONSOLIDATE_SYSTEM_PROMPT,
                                     prompts.build_consolidate_text(active_subjects, theme_count_by_subject),
                                     prompts.CONSOLIDATE_RESPONSE_SCHEMA)
    print(f"  consolidate {len(active_subjects)} subjects: {usage_list[-1]}")
    if result is None:
        print("[error] consolidation failed; subjects are left as they are")
        return usage_list
    subjects_by_id = {subject["subject_id"]: subject for subject in active_subjects}
    used_subject_ids = set()
    canonical_id_by_subject_id = {}
    for final in result["subjects"]:
        members = [subjects_by_id[subject_id] for subject_id in dict.fromkeys(final["subject_ids"])
                   if subject_id in subjects_by_id and subject_id not in used_subject_ids]
        if not members:
            continue
        used_subject_ids.update(member["subject_id"] for member in members)
        canonical = max(members, key=lambda member: (member.get("assigned", False),
                                                     theme_count_by_subject[member["subject_id"]],
                                                     len(member["proposed_theme_refs"])))
        before = {key: canonical[key] for key in ("umbrella", "name", "description")}
        after = {key: cleaned_consolidated_text(before[key], final[key]) for key in before}
        if before != after:
            canonical.setdefault("before_consolidation", before)
            canonical.update(after)
        others = [member for member in members if member is not canonical]
        if others:
            canonical["merged_subject_ids"] = [*canonical.get("merged_subject_ids", []),
                                               *(member["subject_id"] for member in others)]
            canonical["proposed_theme_refs"] = list(dict.fromkeys(
                ref for member in [canonical, *others] for ref in member["proposed_theme_refs"]))
        for member in others:
            member["merged_into"] = canonical["subject_id"]
            canonical_id_by_subject_id[member["subject_id"]] = canonical["subject_id"]
    for entry in state["themes"].values():
        entry["subject_ids"] = list(dict.fromkeys(
            canonical_id_by_subject_id.get(subject_id, subject_id) for subject_id in entry["subject_ids"]))
    remaining = live_subjects(state)
    print(f"  consolidate: {len(canonical_id_by_subject_id)} subjects merged into others, {len(remaining)} left, "
          f"{len({subject['umbrella'] for subject in remaining})} umbrellas; "
          f"{len(set(subjects_by_id) - used_subject_ids)} subjects left out by the model and kept as they are")
    return usage_list


# ── pass 3: approaches ────────────────────────────────────────────────────────

def subject_approaches(caller: GeminiCaller, subject: dict, subject_themes: list[dict]) -> tuple[dict | None, list[dict]]:
    """The approaches (with theme refs) and the off-topic refs of one subject, or None when the call failed."""
    result, usage_list = caller.call(prompts.APPROACHES_SYSTEM_PROMPT,
                                     prompts.build_approaches_text(subject, subject_themes),
                                     prompts.APPROACHES_RESPONSE_SCHEMA)
    print(f"  {subject['name']}: {usage_list[-1]}")
    if result is None:
        return None, usage_list
    ref_by_theme_id = {theme["theme_id"]: theme["ref"] for theme in subject_themes}
    placed_refs = set()
    approaches = []
    for approach in result["approaches"]:
        approach_refs = [ref for ref in refs_of_ids(approach["theme_ids"], ref_by_theme_id) if ref not in placed_refs]
        placed_refs.update(approach_refs)
        if approach_refs:
            approaches.append({"name": approach["name"], "summary": approach["summary"], "theme_refs": approach_refs})
    off_topic_refs = [ref for ref in refs_of_ids(result["off_topic_theme_ids"], ref_by_theme_id) if ref not in placed_refs]
    return {"approaches": approaches, "off_topic_refs": off_topic_refs}, usage_list


def mk_count(refs: list[str], themes_by_ref: dict[str, dict]) -> int:
    return len({themes_by_ref[ref]["mk_id"] for ref in refs if ref in themes_by_ref})


def find_approaches(caller: GeminiCaller, state: dict, themes_by_ref: dict[str, dict],
                    forced_subject_ids: set[int] = frozenset()) -> list[dict]:
    """Pass 3 for every live subject with enough MKs whose themes changed since its approaches (or forced).
    Off-topic themes are removed from the subject's assignments and remembered so they are not assigned to it
    again. Subjects below the MK minimum lose any approaches they had. Returns the usage."""
    theme_refs_by_subject = refs_by_subject(state)
    to_run = []
    for subject in live_subjects(state):
        subject_refs = theme_refs_by_subject[subject["subject_id"]]
        if mk_count(subject_refs, themes_by_ref) < config.SUBJECTS_MIN_MKS_FOR_APPROACHES:
            subject.pop("approaches", None)
            subject.pop("approaches_theme_keys", None)
            continue
        unchanged = subject.get("approaches_theme_keys") == subject_theme_keys(subject_refs, themes_by_ref)
        if subject["subject_id"] in forced_subject_ids or not unchanged:
            to_run.append(subject)
    print(f"  approaches: {len(to_run)} subjects to run, the others are up to date")

    def run_subject(subject: dict) -> tuple[dict | None, list[dict]]:
        subject_themes = [themes_by_ref[ref] for ref in theme_refs_by_subject[subject["subject_id"]]]
        return subject_approaches(caller, subject, subject_themes)

    results = run_parallel(run_subject, to_run)
    failed = []
    for subject, (result, _) in zip(to_run, results):
        if result is None:
            failed.append(subject["name"])
            continue
        for ref in result["off_topic_refs"]:
            state["themes"][ref]["subject_ids"].remove(subject["subject_id"])
        subject["off_topic_theme_refs"] = list(dict.fromkeys([*subject.get("off_topic_theme_refs", []),
                                                              *result["off_topic_refs"]]))
        on_topic_refs = [ref for ref in theme_refs_by_subject[subject["subject_id"]] if ref not in result["off_topic_refs"]]
        subject["approaches"] = result["approaches"]
        subject["approaches_theme_keys"] = subject_theme_keys(on_topic_refs, themes_by_ref)
    if failed:
        print(f"[error] pass 3 failed for {len(failed)} subjects: {failed}")
    return [usage for _, usage_list in results for usage in usage_list]


def print_report(state: dict, themes_by_ref: dict[str, dict]) -> None:
    theme_refs_by_subject = refs_by_subject(state)
    subjects = sorted(live_subjects(state), key=lambda subject: -mk_count(theme_refs_by_subject[subject["subject_id"]],
                                                                            themes_by_ref))
    entries = [state["themes"][ref] for ref in themes_by_ref if ref in state["themes"]]
    subjects_per_theme = Counter(len(entry["subject_ids"]) for entry in entries)
    rejected = [subject for subject in state["subjects"] if not subject["verification"]["verified"]]
    print(f"\n{len(subjects)} subjects ({len(rejected)} rejected); "
          f"{sum(1 for entry in entries if entry['subject_ids'])}/{len(themes_by_ref)} themes in a subject; "
          f"subjects per theme: {dict(sorted(subjects_per_theme.items()))}")
    by_umbrella = defaultdict(list)
    for subject in subjects:
        by_umbrella[subject["umbrella"]].append(subject)
    for umbrella, umbrella_subjects in sorted(by_umbrella.items(), key=lambda item: -len(item[1])):
        print(f"\n# {umbrella} ({len(umbrella_subjects)} subjects)")
        for subject in umbrella_subjects:
            refs = theme_refs_by_subject[subject["subject_id"]]
            print(f"  {subject['name']}: {mk_count(refs, themes_by_ref)} MKs, {len(refs)} themes, "
                  f"{len(subject.get('approaches', []))} approaches [{subject['round']}]")

