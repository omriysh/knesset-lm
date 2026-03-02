"""
summarize_meeting.py

CLI: summarize a single Knesset meeting protocol file.

Usage:
    python scripts/summarize_meeting.py <path/to/meeting.json>

The summary is printed to stdout and saved to:
    Data/summaries/25/<committee>/<filename>.txt

If the summary file already exists it is skipped (use --force to overwrite).
"""

import argparse
import sys
from pathlib import Path

# ── Bootstrap: add src/ to the import path ────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from summarization.pipeline import summarize_meeting, save_summary
from config import summaries_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize a Knesset meeting protocol.")
    parser.add_argument("meeting_file", type=Path, help="Path to meeting JSON file")
    parser.add_argument("--knesset", type=int, default=25, help="Knesset number (default: 25)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing summary")
    args = parser.parse_args()

    meeting_path = args.meeting_file.resolve()
    if not meeting_path.exists():
        print(f"❌ File not found: {meeting_path}")
        sys.exit(1)

    # Determine output path and check for existing summary
    committee_dir = meeting_path.parent.name
    out_path = summaries_dir(args.knesset) / committee_dir / meeting_path.with_suffix(".txt").name
    if out_path.exists() and not args.force:
        print(f"⏭️  Summary already exists: {out_path}")
        print("   Use --force to regenerate.")
        sys.exit(0)

    print(f"📄 Meeting : {meeting_path.name}\n")
    summary = summarize_meeting(meeting_path)

    if summary:
        saved = save_summary(summary, meeting_path, args.knesset)
        print(f"\n✅ Summary saved to: {saved}")
    else:
        print("\n❌ No summary was produced.")
        sys.exit(1)


if __name__ == "__main__":
    main()
