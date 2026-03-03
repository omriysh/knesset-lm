"""
summarize_committee.py

CLI: download and summarize all sessions for a Knesset committee.

Usage:
    python scripts/summarize_committee.py <committee_name> [--knesset 25] [--dry-run]

For each session:
  1. Classified (חסויה) or cancelled sessions are skipped with a note.
  2. Checks if a local protocol JSON already exists (skip download if so).
  3. If missing, scrapes oknesset.org for the transcript. Falls back to OData
     PDF download if oknesset.org doesn't have it.
  4. Checks if a summary .txt already exists (skip summarization if so).
  5. If missing, runs the summarization pipeline and saves the result.

Protocols are saved to:   Data/raw_transcriptions/25/<committee>/DD_MM_YYYY_<session_id>.json
Summaries are saved to:   Data/summaries/25/<committee>/DD_MM_YYYY_<session_id>.txt
"""

import argparse
import json
import re
import sys
from pathlib import Path

# ── Bootstrap: add src/ to the import path ────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.knesset_db import (
    get_committee_sessions_by_name,
    get_session_transcript,
    SESSION_TYPE_CLASSIFIED,
)
from summarization.pipeline import summarize_meeting, save_summary
from config import transcriptions_dir, summaries_dir

_WIN_UNSAFE = re.compile(r'[\\/:*?"<>|]')


def _safe_dirname(name: str) -> str:
    """Replace Windows-unsafe characters and whitespace with underscores."""
    return re.sub(r'[\s_]+', '_', _WIN_UNSAFE.sub("_", name)).strip('_')


def _session_filename(date_iso: str, session_id: int) -> str:
    """
    Convert ISO date (YYYY-MM-DD) and session ID to the local filename convention.
    Format: DD_MM_YYYY_<session_id>
    """
    if date_iso and len(date_iso) >= 10:
        y, m, d = date_iso[:10].split("-")
        date_part = f"{d}_{m}_{y}"
    else:
        date_part = "00_00_0000"
    return f"{date_part}_{session_id}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download and summarize all sessions for a Knesset committee."
    )
    parser.add_argument("committee", help="Committee name (Hebrew, partial match OK)")
    parser.add_argument("--knesset", type=int, default=25, help="Knesset number (default: 25)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List sessions and their status without downloading or summarizing",
    )
    parser.add_argument(
        "--force-summarize", action="store_true",
        help="Re-summarize even if a summary file already exists",
    )
    args = parser.parse_args()

    print(f"🔍 Looking up sessions for: {args.committee!r} (Knesset {args.knesset})\n")
    sessions = get_committee_sessions_by_name(args.committee, args.knesset)

    if not sessions:
        print("❌ No sessions found. Check the committee name and Knesset number.")
        sys.exit(1)

    print(f"   Found {len(sessions)} session(s).\n")

    committee_dirname = _safe_dirname(args.committee)
    proto_dir = transcriptions_dir(args.knesset) / committee_dirname
    summ_dir  = summaries_dir(args.knesset) / committee_dirname

    if not args.dry_run:
        proto_dir.mkdir(parents=True, exist_ok=True)
        summ_dir.mkdir(parents=True, exist_ok=True)

    stats = {
        "total":            len(sessions),
        "classified":       0,
        "cancelled":        0,
        "skipped_dl":       0,
        "downloaded_ok":    0,
        "no_transcript":    0,
        "skipped_summ":     0,
        "summarized":       0,
        "failed_summ":      0,
    }

    for session in sessions:
        session_id = session["session_id"]
        date_iso   = session["date"]
        type_id    = session.get("type_id")
        status_id  = session.get("status_id")
        stem       = _session_filename(date_iso, session_id)
        proto_path = proto_dir / f"{stem}.json"
        summ_path  = summ_dir  / f"{stem}.txt"

        print(f"📅 {date_iso} | session {session_id}")

        # ── Skip classified sessions ──────────────────────────────────────────
        if type_id == SESSION_TYPE_CLASSIFIED:
            print(f"   🔒 Classified session — skipping\n")
            stats["classified"] += 1
            continue

        # ── Skip cancelled sessions ───────────────────────────────────────────
        # StatusID 193 = מבוטלת (cancelled); also guard against other non-active statuses
        # We keep 192 (פעילה = active) and unknown statuses; skip known cancelled ones.
        _CANCELLED_STATUS_IDS = {193}
        if status_id in _CANCELLED_STATUS_IDS:
            print(f"   ❌ Cancelled session — skipping\n")
            stats["cancelled"] += 1
            continue

        # ── Step 1: protocol download ─────────────────────────────────────────
        if proto_path.exists():
            print(f"   ✅ Protocol cached: {proto_path.name}")
            stats["skipped_dl"] += 1
        else:
            if args.dry_run:
                print(f"   ⬇️  Would download protocol (oknesset.org → OData PDF fallback)")
            else:
                print(f"   ⬇️  Fetching transcript...", end=" ", flush=True)
                transcript = get_session_transcript(session_id)

                if transcript:
                    if "speeches" in transcript:
                        print(f"✅ {len(transcript['speeches'])} speeches (oknesset.org)")
                    else:
                        print(f"✅ {len(transcript['full_text']):,} chars (OData document)")
                    payload = {
                        "meeting_id":  str(session_id),
                        "date":        date_iso,
                        "committee":   args.committee,
                        "knesset_num": args.knesset,
                        **transcript,
                    }
                    proto_path.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    stats["downloaded_ok"] += 1
                else:
                    print(f"⚠️  No transcript available")
                    stats["no_transcript"] += 1
                    print()
                    continue

        # ── Step 2: summarization ─────────────────────────────────────────────
        if summ_path.exists() and not args.force_summarize:
            print(f"   ⏭️  Summary exists: {summ_path.name}")
            stats["skipped_summ"] += 1
        else:
            if args.dry_run:
                print(f"   📝 Would summarize")
            else:
                print(f"   📝 Summarizing...")
                summary = summarize_meeting(proto_path)
                if summary:
                    save_summary(summary, proto_path, args.knesset)
                    print(f"   ✅ Summary saved: {summ_path.name}")
                    stats["summarized"] += 1
                else:
                    print(f"   ❌ Summarization produced no output")
                    stats["failed_summ"] += 1

        print()

    # ── Final report ──────────────────────────────────────────────────────────
    print("=" * 50)
    print(f"📊 Done — {args.committee}")
    print(f"   Total sessions      : {stats['total']}")
    if stats["classified"]:
        print(f"   🔒 Classified (skipped): {stats['classified']}")
    if stats["cancelled"]:
        print(f"   ❌ Cancelled (skipped) : {stats['cancelled']}")
    print(f"   ✅ Already cached    : {stats['skipped_dl']}")
    print(f"   ⬇️  Newly downloaded  : {stats['downloaded_ok']}")
    if stats["no_transcript"]:
        print(f"   ⚠️  No transcript found: {stats['no_transcript']}")
    print(f"   ⏭️  Already summarized: {stats['skipped_summ']}")
    print(f"   📝 Newly summarized  : {stats['summarized']}")
    if stats["failed_summ"]:
        print(f"   ❌ Summarization failed: {stats['failed_summ']}")


if __name__ == "__main__":
    main()
