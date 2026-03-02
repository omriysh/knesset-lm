"""
summarize_committee.py

CLI: download and summarize all sessions for a Knesset committee.

Usage:
    python scripts/summarize_committee.py <committee_name> [--knesset 25] [--dry-run]

For each session:
  1. Checks if a local protocol JSON already exists (skip download if so).
  2. If missing, downloads the official protocol PDF via OData and saves as JSON.
  3. Checks if a summary .txt already exists (skip summarization if so).
  4. If missing, runs the summarization pipeline and saves the result.

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

from utils.knesset_db import get_committee_sessions_by_name, get_session_protocol_text
from summarization.pipeline import summarize_meeting, save_summary
from config import transcriptions_dir, summaries_dir

_WIN_UNSAFE = re.compile(r'[\\/:*?"<>|]')


def _safe_dirname(name: str) -> str:
    """Replace Windows-unsafe characters in a committee name for use as a directory name."""
    return _WIN_UNSAFE.sub("_", name).strip()


def _session_filename(date_iso: str, session_id: int) -> str:
    """
    Convert ISO date (YYYY-MM-DD) and session ID to the local filename convention.
    Matches the existing raw_transcriptions format: DD_MM_YYYY_<session_id>.json
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
    parser.add_argument("--force-summarize", action="store_true", help="Re-summarize even if summary exists")
    args = parser.parse_args()

    print(f"🔍 Looking up sessions for: {args.committee!r} (Knesset {args.knesset})\n")
    sessions = get_committee_sessions_by_name(args.committee, args.knesset)

    if not sessions:
        print("❌ No sessions found. Check the committee name and Knesset number.")
        sys.exit(1)

    print(f"   Found {len(sessions)} session(s).\n")

    # Use the first session's committee_id to derive the committee name for directories.
    # We'll use the search string sanitized as the dir name (it's what the user passed).
    committee_dirname = _safe_dirname(args.committee)
    proto_dir = transcriptions_dir(args.knesset) / committee_dirname
    summ_dir  = summaries_dir(args.knesset) / committee_dirname

    if not args.dry_run:
        proto_dir.mkdir(parents=True, exist_ok=True)
        summ_dir.mkdir(parents=True, exist_ok=True)

    stats = {"total": len(sessions), "downloaded": 0, "skipped_dl": 0, "summarized": 0, "skipped_summ": 0, "failed": 0}

    for session in sessions:
        session_id = session["session_id"]
        date_iso   = session["date"]
        stem       = _session_filename(date_iso, session_id)
        proto_path = proto_dir / f"{stem}.json"
        summ_path  = summ_dir  / f"{stem}.txt"

        print(f"📅 {date_iso} | session {session_id}")

        # ── Step 1: protocol download ─────────────────────────────────────────
        if proto_path.exists():
            print(f"   ✅ Protocol cached: {proto_path.name}")
            stats["skipped_dl"] += 1
        else:
            if args.dry_run:
                print(f"   ⬇️  Would download protocol")
            else:
                print(f"   ⬇️  Downloading protocol...")
                result = get_session_protocol_text(session_id)
                if result and result.get("text"):
                    payload = {
                        "meeting_id":  str(session_id),
                        "date":        date_iso,
                        "committee":   args.committee,
                        "knesset_num": args.knesset,
                        "source_url":  result["url"],
                        "full_text":   result["text"],
                    }
                    proto_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"   ✅ Saved: {proto_path.name} ({len(result['text']):,} chars)")
                    stats["downloaded"] += 1
                else:
                    print(f"   ⚠️  No protocol document found — skipping session.")
                    stats["failed"] += 1
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
                    print(f"   ❌ Summarization failed.")
                    stats["failed"] += 1

        print()

    # ── Final report ──────────────────────────────────────────────────────────
    print("=" * 50)
    print(f"📊 Done — {args.committee}")
    print(f"   Total sessions    : {stats['total']}")
    print(f"   Already cached    : {stats['skipped_dl']}")
    print(f"   Newly downloaded  : {stats['downloaded']}")
    print(f"   Already summarized: {stats['skipped_summ']}")
    print(f"   Newly summarized  : {stats['summarized']}")
    if stats["failed"]:
        print(f"   Failed            : {stats['failed']}")


if __name__ == "__main__":
    main()
