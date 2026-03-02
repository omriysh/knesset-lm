"""
pipeline.py

Top-level meeting summarization pipeline.
"""

import time
from pathlib import Path

from summarization.agent import run_agent_loop
from summarization.prompts import SYSTEM_PROMPT_PASS1, SYSTEM_PROMPT_CONTINUATION
from utils.meeting import load_meeting, build_transcript_text, chunk_transcript
from config import summaries_dir, CHARS_PER_TOK


def _build_messages(
    system_prompt: str,
    committee: str,
    date: str,
    meeting_id: str,
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    partial_summary: str | None,
) -> list[dict]:
    """Construct the messages list for one agent call."""
    header = (
        f"ועדה: {committee}\n"
        f"תאריך: {date}\n"
        f"מזהה ישיבה: {meeting_id}\n\n"
    )

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


def summarize_meeting(meeting_path: str | Path) -> str:
    """
    Load a meeting protocol file and produce a Hebrew summary via the agentic loop.

    For transcripts that fit in a single chunk: one agent call.
    For longer transcripts: rolling summary — each chunk updates the running summary.

    Returns the final Hebrew summary text.
    """
    meeting_path = Path(meeting_path)
    meeting      = load_meeting(meeting_path)

    transcript_text = build_transcript_text(meeting)
    committee       = meeting.get("committee", "")
    date            = meeting.get("date", "")
    meeting_id      = meeting.get("meeting_id", "")

    char_count = len(transcript_text)
    print(f"   Committee : {committee}")
    print(f"   Date      : {date}")
    print(f"   ~Tokens   : {char_count // CHARS_PER_TOK:,} (estimated)")

    chunks = chunk_transcript(transcript_text)
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
