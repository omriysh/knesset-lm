"""
pipeline.py

Top-level meeting summarization pipeline.
"""

import time
from pathlib import Path

from summarization.agent import run_agent_loop
from summarization.prompts import SYSTEM_PROMPT_PASS1, SYSTEM_PROMPT_CONTINUATION
from utils.meeting import load_meeting, build_transcript_text, chunk_transcript, extract_attendance
from utils.knesset_db import get_mk_profile, get_active_committee_members_by_name
from config import summaries_dir, CHARS_PER_TOK, MAX_SUMMARIZATION_CHUNKS


def _mk_line(profile: dict, knesset_num: int, duty_desc: str = "") -> str:
    """Format a single MK profile as a bulleted attendance line with party and optional role."""
    factions = [
        f for f in (profile.get("factions") or [])
        if f and f.get("knesset") == knesset_num
    ]
    faction = max(factions, key=lambda f: f.get("start_date") or "", default=None)
    party = faction["faction_name"] if faction else ""
    first = profile.get("mk_individual_first_name", "")
    last  = profile.get("mk_individual_name", "")
    full  = f"{first} {last}".strip()
    line  = f'- ח"כ {full} ({party})' if party else f'- ח"כ {full}'
    if duty_desc:
        line += f" — {duty_desc}"
    return line


def _build_attendance_block(
    raw_names: list[str],
    committee_members: list[dict],
    knesset_num: int,
) -> str:
    """
    Build a formatted attendance block for injection into the LLM prompt.

    Two sections:
    - נוכחים: names extracted from the transcript, MKs enriched with party affiliation.
    - נעדרים (חברי הוועדה): committee members whose name does not appear in any
      raw_name (substring match), also enriched with party affiliation.

    Returns an empty string if raw_names is empty (can't distinguish present/absent).
    """
    if not raw_names:
        return ""

    raw_lower = [n.lower() for n in raw_names]

    def _is_present(full_name: str) -> bool:
        nl = full_name.lower()
        return any(nl in r or r in nl for r in raw_lower)

    def _find_member(name: str) -> dict | None:
        """Return the committee member record whose name matches a speaker name."""
        nl = name.lower()
        for member in committee_members:
            ml = member["full_name"].lower()
            if ml in nl or nl in ml:
                return member
        return None

    # ── Present ───────────────────────────────────────────────────────────────
    present_lines = []
    for name in raw_names:
        member    = _find_member(name)
        duty_desc = member["duty_desc"] if member else ""
        profile   = get_mk_profile(name, knesset_num)
        if profile:
            present_lines.append(_mk_line(profile, knesset_num, duty_desc))
        else:
            line = f"- {name}"
            if duty_desc:
                line += f" — {duty_desc}"
            present_lines.append(line)

    # ── Absent committee members ───────────────────────────────────────────────
    absent_lines = []
    for member in committee_members:
        if _is_present(member["full_name"]):
            continue
        duty_desc = member.get("duty_desc", "")
        profile   = get_mk_profile(member["full_name"], knesset_num)
        if profile:
            absent_lines.append(_mk_line(profile, knesset_num, duty_desc))
        else:
            line = f"- {member['full_name']}"
            if duty_desc:
                line += f" — {duty_desc}"
            absent_lines.append(line)

    # ── Assemble ──────────────────────────────────────────────────────────────
    parts = []
    if present_lines:
        parts.append("נוכחים:\n" + "\n".join(present_lines))
    if absent_lines:
        parts.append("נעדרים (חברי הוועדה):\n" + "\n".join(absent_lines))
    return "\n\n".join(parts)


def _build_messages(
    system_prompt: str,
    committee: str,
    date: str,
    meeting_id: str,
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    partial_summary: str | None,
    attendance_block: str | None = None,
) -> list[dict]:
    """Construct the messages list for one agent call."""
    header = (
        f"ועדה: {committee}\n"
        f"תאריך: {date}\n"
        f"מזהה ישיבה: {meeting_id}\n\n"
    )

    if attendance_block:
        header += f"נוכחים ונעדרים (מחושב מהפרוטוקול):\n{attendance_block}\n\n"

    if partial_summary is None:
        user_content = (
            header
            + f"פרוטוקול הישיבה (חלק {chunk_index} מתוך {total_chunks}):\n\n{chunk}"
        )
    else:
        user_content = (
            header
            + f"סיכום חלקי עד כה:\n{partial_summary}\n\n"
            + "---\n\n"
            + f"המשך הפרוטוקול (חלק {chunk_index} מתוך {total_chunks}):\n\n{chunk}"
        )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content},
    ]


def summarize_meeting(meeting_path: str | Path) -> str | None:
    """
    Load a meeting protocol file and produce a Hebrew summary via the agentic loop.

    For transcripts that fit in a single chunk: one agent call.
    For longer transcripts: rolling summary — each chunk updates the running summary.

    Returns the final Hebrew summary text, or None if the transcript is too long
    (more than MAX_SUMMARIZATION_CHUNKS chunks) and was skipped.
    """
    meeting_path = Path(meeting_path)
    meeting      = load_meeting(meeting_path)

    transcript_text = build_transcript_text(meeting)
    committee       = meeting.get("committee", "")
    date            = meeting.get("date", "")
    meeting_id      = meeting.get("meeting_id", "")
    knesset_num     = meeting.get("knesset_num", 25)

    char_count = len(transcript_text)
    print(f"   Committee : {committee}")
    print(f"   Date      : {date}")
    print(f"   ~Tokens   : {char_count // CHARS_PER_TOK:,} (estimated)")

    chunks = chunk_transcript(transcript_text)

    if len(chunks) > MAX_SUMMARIZATION_CHUNKS:
        print(
            f"\n⏩ Transcript too long ({len(chunks)} chunks > {MAX_SUMMARIZATION_CHUNKS} max) — skipping.\n"
        )
        return None

    # Pre-compute attendance once, before the chunk loop.
    # Looks up party affiliation for all present speakers and absent committee members.
    raw_names         = extract_attendance(meeting)
    committee_members = get_active_committee_members_by_name(committee, knesset_num)
    attendance_block  = _build_attendance_block(raw_names, committee_members, knesset_num)
    if attendance_block:
        absent_count = sum(
            1 for m in committee_members
            if not any(m["full_name"].lower() in n.lower() or n.lower() in m["full_name"].lower()
                       for n in raw_names)
        )
        print(f"   Attendance: {len(raw_names)} present, {absent_count} absent committee member(s)")

    t_start = time.time()

    if len(chunks) == 1:
        print("\n📄 Transcript fits in one chunk — single pass.\n")
    else:
        print(f"\n📄 Transcript split into {len(chunks)} chunks.\n")

    partial_summary: str | None = None
    total_tokens = 0

    for i, chunk in enumerate(chunks, 1):
        is_last = (i == len(chunks))
        if len(chunks) > 1:
            print(f"\n{'='*60}")
            print(f"🔄 Chunk {i}/{len(chunks)} ({len(chunk):,} chars){' [FINAL]' if is_last else ''}")
            print(f"{'='*60}\n")

        system_prompt = SYSTEM_PROMPT_PASS1 if partial_summary is None else SYSTEM_PROMPT_CONTINUATION
        messages = _build_messages(
            system_prompt, committee, date, meeting_id,
            chunk, i, len(chunks), partial_summary,
            attendance_block=attendance_block,
        )

        print("⏳ Sending to llama-server...\n")
        partial_summary, tokens = run_agent_loop(messages)
        total_tokens += tokens

    elapsed = time.time() - t_start
    print(f"\n⏱️  Done in {elapsed:.1f}s | {total_tokens} tokens | {total_tokens / elapsed:.1f} tok/s")

    return partial_summary or ""


def save_summary(summary: str, meeting_path: Path, knesset_num: int = 25) -> Path:
    """
    Save a summary to Data/summaries/<knesset_num>/<committee>/<filename>.txt.
    Derives the committee subdirectory from the meeting file's parent directory name.
    Creates the output directory if it doesn't exist.
    Returns the path to the saved file.
    """
    committee_dir = meeting_path.parent.name
    out_dir = summaries_dir(knesset_num) / committee_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / meeting_path.with_suffix(".txt").name
    out_path.write_text(summary, encoding="utf-8")
    return out_path
