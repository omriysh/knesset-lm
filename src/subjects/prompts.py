"""Prompts and response schemas of the MK subjects pipeline (subjects/steps.py). Themes appear as
"[number] title" + summary, where the number is the theme's mk_themes.id in the db the run read."""

SUBJECT_GRANULARITY_RULES = """- A subject is one public debate or policy area, e.g. "גיוס חרדים לצה\"ל", "יוקר המחיה", "הרפורמה המשפטית".
  Not so broad that unrelated debates mix ("ביטחון", "כלכלה"), not so narrow that it is one bill or event.
- Prefer subjects where MKs disagree or take different angles; that is what the reader wants to compare."""

BACKGROUND_RULES = """- background: 1-3 sentences telling a reader who does not follow Israeli politics what they need to know to
  understand the subject (what the law, body, plan or event is). Use ONLY facts stated in the theme
  summaries you received; no outside knowledge, even well-known facts. If the summaries do not contain
  enough to understand the subject, leave the subject out.
- Names, descriptions and backgrounds are neutral: no stance, no charged terms, and nothing about which MK
  holds which position."""

SHARD_SYSTEM_PROMPT = f"""You map the political agenda of the Israeli Knesset.
You receive the themes of some of the Knesset members (MKs) of one Knesset: the issues and positions each MK
keeps coming back to, extracted earlier from committee and plenum protocols. MKs are anonymous numbers;
each theme is: [theme id] title, then its summary. The other MKs are processed separately, and the
subjects of all groups are merged afterwards.

Propose the subjects these themes are about, so a reader can later compare different MKs' approaches to
one subject.
{SUBJECT_GRANULARITY_RULES}
- Propose a subject even if only one or two MKs here hold a theme about it: after merging, it may turn
  out that many MKs do.
- For each subject write, in Hebrew:
  - umbrella: a short name of the broader area the subject falls under. Choose it yourself; several
    subjects should share an umbrella.
  - name: a short name.
  - description: 1-2 sentences saying what is inside the subject and what is not, so another reader can
    decide for any theme whether it belongs.
{BACKGROUND_RULES}
- background_theme_ids: the themes whose summaries the background's facts come from.
- theme_ids: every theme here that belongs to the subject. A theme often belongs to more than one subject;
  list it under each of them.
"""

EXISTING_SUBJECTS_NOTE = """
These subjects already exist; propose only subjects that none of them covers:
"""

SHARD_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "subjects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "umbrella":             {"type": "string"},
                    "name":                 {"type": "string"},
                    "description":          {"type": "string"},
                    "background":           {"type": "string"},
                    "background_theme_ids": {"type": "array", "items": {"type": "integer"}},
                    "theme_ids":            {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["umbrella", "name", "description", "background", "background_theme_ids", "theme_ids"],
            },
        },
    },
    "required": ["subjects"],
}

MERGE_SYSTEM_PROMPT = f"""You map the political agenda of the Israeli Knesset.
Candidate subjects were proposed separately for different groups of Knesset members, so many candidates
describe the same subject in different words. Each candidate is: [candidate id] umbrella > name
(number of themes): description, then its background.

Merge the candidates into one list of subjects.
- Candidates that describe the same subject become one subject. Keep different subjects separate, even
  when they are related: do not merge them into a broad area.
{SUBJECT_GRANULARITY_RULES}
- Every candidate goes to exactly one subject, unless it duplicates one of the existing subjects listed
  (when such a list is given): then put its id in duplicate_candidate_ids instead.
- For each subject write, in Hebrew:
  - umbrella: the broader area. Unify the umbrellas: related subjects must use exactly the same umbrella
    string. Choose the umbrella names yourself.
  - name and description (1-2 sentences: what is inside the subject and what is not).
  - background: combine the backgrounds of its candidates, using only facts stated in them; add nothing.
    Keep it 1-3 sentences, neutral.
  - candidate_ids: the candidates merged into it.
"""

MERGE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "subjects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "umbrella":      {"type": "string"},
                    "name":          {"type": "string"},
                    "description":   {"type": "string"},
                    "background":    {"type": "string"},
                    "candidate_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["umbrella", "name", "description", "background", "candidate_ids"],
            },
        },
        "duplicate_candidate_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["subjects", "duplicate_candidate_ids"],
}

VERIFY_SYSTEM_PROMPT = """You check the background text of a subject on the Israeli Knesset agenda.
You receive the subject (name, description, background) and the theme summaries the background was
written from. Each theme is: [theme id] title, then its summary.

- unsupported_claims: every fact in the background that the summaries do not state (quote it). Facts
  that are common knowledge but not in the summaries are unsupported too.
- understandable: true if a reader who does not follow Israeli politics can understand what the subject
  is about from the name, description and background alone.
- reason: one sentence explaining your answer.
"""

VERIFY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "understandable":     {"type": "boolean"},
        "reason":             {"type": "string"},
    },
    "required": ["unsupported_claims", "understandable", "reason"],
}

REPAIR_SYSTEM_PROMPT = """You fix the background text of a subject on the Israeli Knesset agenda.
You receive the subject (name, description, background), the claims in the background that a checker
found unsupported by the theme summaries, and the theme summaries themselves. Each theme is: [theme id]
title, then its summary.

Rewrite the background, in Hebrew, so that it contains none of the unsupported claims:
- Remove or rephrase the parts that contain them. Keep everything else as it is.
- You may use other facts, but ONLY facts stated in the summaries; no outside knowledge, even well-known facts.
- Keep it 1-3 sentences, neutral, telling a reader who does not follow Israeli politics what they need to
  know to understand the subject.
"""

REPAIR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"background": {"type": "string"}},
    "required": ["background"],
}

CONSOLIDATE_SYSTEM_PROMPT = f"""You map the political agenda of the Israeli Knesset.
You receive subjects proposed in separate rounds, so some describe the same subject in different words, and
the umbrella names (the broader area each subject falls under) were chosen separately. Each subject is:
[subject id] umbrella > name (round, number of themes): description.

Return the final list of subjects.
- Subjects that describe the same subject become one. Keep different subjects separate, even when they are
  related: do not merge them into a broad area.
{SUBJECT_GRANULARITY_RULES}
- Every subject id goes to exactly one final subject; most final subjects have a single id.
- For each final subject write, in Hebrew:
  - umbrella: unify the umbrellas, so that related subjects use exactly the same umbrella string and two
    umbrellas never cover the same area. Choose the umbrella names yourself.
  - name and description (1-2 sentences: what is inside the subject and what is not). For a single id, keep
    its name and description unless they need a fix.
  - subject_ids: the subjects merged into it.
- Copy Hebrew abbreviations exactly as written, with gershayim (צה"ל, not צה'ל).
"""

CONSOLIDATE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "subjects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "umbrella":    {"type": "string"},
                    "name":        {"type": "string"},
                    "description": {"type": "string"},
                    "subject_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["umbrella", "name", "description", "subject_ids"],
            },
        },
    },
    "required": ["subjects"],
}

ASSIGN_SYSTEM_PROMPT = """You sort themes of Israeli Knesset members (MKs) into subjects.
You receive a numbered list of subjects, each with a description of what it includes, and then a list of
themes. Each theme is: [theme id] title, then its summary.

For every theme, list the numbers of all the subjects it belongs to.
- Many themes belong to more than one subject: a theme about two issues, or about an issue that two
  subjects cover from different sides, goes into every one of them. Check each theme against the whole list.
- A theme belongs to a subject when one of its core issues is inside the subject's description, not when it
  only touches it in passing.
- A theme may also belong to no subject. An empty list is a valid answer.
- Return every theme id you received, exactly once.
"""

ASSIGN_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "theme_id":    {"type": "integer"},
                    "subject_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["theme_id", "subject_ids"],
            },
        },
    },
    "required": ["assignments"],
}

APPROACHES_SYSTEM_PROMPT = """You compare the positions of Israeli Knesset members (MKs) on one subject.
You receive a subject with its description, and the themes of different MKs that were filed under it. Each
theme is: [theme id] title, then its summary. Themes were extracted earlier from committee and plenum
protocols; each one is a single MK's recurring position. A theme may also be filed under other subjects;
here, judge it only by what it says about this subject.

1. Group the themes into approaches: themes that take the same side or the same angle on the subject belong
   to one approach, themes that disagree belong to different approaches.
   - Usually 2-6 approaches. A single approach is fine when everyone agrees.
   - Split by the stance taken on the subject, not by the side issues each MK adds.
   - Every theme about the subject goes to exactly one approach: the one closest to its main stance on this
     subject.
   - Themes that are not really about this subject go to off_topic_theme_ids, not into an approach.
2. For each approach write, in Hebrew: a short neutral name (e.g. "תמיכה ב...", "התנגדות ל...",
   "דרישה ל..."), and a summary of 2-4 sentences of what MKs taking it argue, based only on its themes.
   Attribute every claim to the MKs ("לטענתם", "חברי הכנסת בגישה זו סבורים ש..."); never state their
   claims in your own voice. Charged terms appear only in quotation marks. Do not assume gender.
Order approaches from the one with the most themes to the one with the fewest.
"""

APPROACHES_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "approaches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":      {"type": "string"},
                    "summary":   {"type": "string"},
                    "theme_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["name", "summary", "theme_ids"],
            },
        },
        "off_topic_theme_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["approaches", "off_topic_theme_ids"],
}


def theme_block(theme: dict) -> str:
    return f"[{theme['theme_id']}] {theme['title']}\n{theme['summary']}"


def theme_blocks(themes: list[dict]) -> str:
    return "\n\n".join(theme_block(theme) for theme in themes)


def existing_subject_lines(existing_subjects: list[dict]) -> str:
    return "\n".join(f"- {subject['umbrella']} > {subject['name']}: {subject['description']}"
                     for subject in existing_subjects)


def build_shard_text(themes_by_mk: list[list[dict]], existing_subjects: list[dict]) -> str:
    sections = [f"## MK {number}\n" + theme_blocks(mk_themes) for number, mk_themes in enumerate(themes_by_mk, start=1)]
    text = (f"{len(themes_by_mk)} MKs, {sum(len(mk_themes) for mk_themes in themes_by_mk)} themes\n\n"
            + "\n\n".join(sections))
    if existing_subjects:
        text += "\n\n" + EXISTING_SUBJECTS_NOTE + existing_subject_lines(existing_subjects)
    return text


def build_merge_text(candidates: list[dict], existing_subjects: list[dict]) -> str:
    lines = [f"[{candidate['candidate_id']}] {candidate['umbrella']} > {candidate['name']} "
             f"({len(candidate['theme_refs'])} themes): {candidate['description']}\n{candidate['background']}"
             for candidate in candidates]
    text = "# Candidates\n" + "\n\n".join(lines)
    if existing_subjects:
        text += "\n\n# Existing subjects\n" + existing_subject_lines(existing_subjects)
    return text


def build_verify_text(subject: dict, background_themes: list[dict]) -> str:
    return (f"# Subject: {subject['name']}\n{subject['description']}\n\n# Background\n{subject['background']}\n\n"
            "# Theme summaries\n" + theme_blocks(background_themes))


def build_repair_text(subject: dict, background_themes: list[dict]) -> str:
    unsupported_lines = "\n".join(f"- {claim}" for claim in subject["verification"]["unsupported_claims"])
    return (f"# Subject: {subject['name']}\n{subject['description']}\n\n# Background\n{subject['background']}\n\n"
            f"# Unsupported claims\n{unsupported_lines}\n\n"
            "# Theme summaries\n" + theme_blocks(background_themes))


def build_consolidate_text(subjects: list[dict], theme_count_by_subject: dict[int, int]) -> str:
    return "\n".join(f"[{subject['subject_id']}] {subject['umbrella']} > {subject['name']} "
                     f"({subject['round']}, {theme_count_by_subject.get(subject['subject_id'], 0)} themes): "
                     f"{subject['description']}" for subject in subjects)


def build_assign_text(subjects: list[dict], chunk: list[dict]) -> str:
    subject_lines = [f"{number}. {subject['name']}: {subject['description']}"
                     for number, subject in enumerate(subjects, start=1)]
    return "# Subjects\n" + "\n".join(subject_lines) + "\n\n# Themes\n" + theme_blocks(chunk)


def build_approaches_text(subject: dict, subject_themes: list[dict]) -> str:
    return (f"# Subject: {subject['name']}\n{subject['description']}\n\n"
            f"# Themes ({len(subject_themes)} themes of {len({t['mk_id'] for t in subject_themes})} MKs)\n"
            + theme_blocks(subject_themes))
