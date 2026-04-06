"""
summarize_committee.py

CLI: download and summarize all sessions for a Knesset committee.

Usage:
    python scripts/summarize_committee.py <committee_name> [--knesset 25] [--dry-run]

For each session:
  1. Classified (חסויה) or cancelled sessions are skipped.
  2. Checks if a local protocol JSON already exists (skip download if so).
  3. If missing, scrapes oknesset.org for the transcript. Falls back to OData
     PDF download if oknesset.org doesn't have it.
  4. Checks if a summary .txt already exists (skip summarization if so).
  5. If missing, runs the summarization pipeline and saves the result.

Protocols are saved to:   Data/raw_transcriptions/<knesset>/<committee>/DD_MM_YYYY_<id>.json
Summaries are saved to:   Data/summaries/<knesset>/<committee>/DD_MM_YYYY_<id>.txt
"""

import argparse
import json
import re
import sys
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.knesset_db import (
    get_committee_sessions_by_name,
    get_session_transcript,
    SESSION_TYPE_CLASSIFIED,
)
from summarization.pipeline import summarize_meeting, save_summary
from config import transcriptions_dir, summaries_dir

_WIN_UNSAFE = re.compile(r'[\\/:*?"<>|]')
_CANCELLED_STATUS_IDS = {193}


def _safe_dirname(name: str) -> str:
    return re.sub(r'[\s_]+', '_', _WIN_UNSAFE.sub("_", name)).strip('_')


def _session_filename(date_iso: str, session_id: int) -> str:
    if date_iso and len(date_iso) >= 10:
        y, m, d = date_iso[:10].split("-")
        return f"{d}_{m}_{y}_{session_id}"
    return f"00_00_0000_{session_id}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download and summarize all sessions for a Knesset committee."
    )
    parser.add_argument("committee", help="Committee name (Hebrew, partial match OK)")
    parser.add_argument("--knesset", type=int, default=25, help="Knesset number (default: 25)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List sessions and status without writing any files")
    parser.add_argument("--force-summarize", action="store_true",
                        help="Re-summarize even if a summary file already exists")
    args = parser.parse_args()

    tqdm.write(f"Looking up sessions for: {args.committee!r} (Knesset {args.knesset})")
    sessions = get_committee_sessions_by_name(args.committee, args.knesset)

    if not sessions:
        tqdm.write("No sessions found. Check the committee name and Knesset number.")
        sys.exit(1)

    tqdm.write(f"Found {len(sessions)} session(s).\n")

    committee_dirname = _safe_dirname(args.committee)
    proto_dir = transcriptions_dir(args.knesset) / committee_dirname
    summ_dir  = summaries_dir(args.knesset) / committee_dirname

    if not args.dry_run:
        proto_dir.mkdir(parents=True, exist_ok=True)
        summ_dir.mkdir(parents=True, exist_ok=True)

    stats = {
        "total":         len(sessions),
        "classified":    0, "cancelled":     0,
        "skipped_dl":    0, "downloaded_ok": 0, "no_transcript": 0,
        "skipped_summ":  0, "summarized":    0, "too_long":      0, "failed_summ": 0,
    }

    with tqdm(
        total    = len(sessions),
        desc     = args.committee[:30],
        unit     = "session",
        dynamic_ncols = True,
    ) as bar:
        for session in sessions:
            session_id = session["session_id"]
            date_iso   = session["date"]
            type_id    = session.get("type_id")
            status_id  = session.get("status_id")
            stem       = _session_filename(date_iso, session_id)
            proto_path = proto_dir / f"{stem}.json"
            summ_path  = summ_dir  / f"{stem}.txt"

            tqdm.write(f"\n{date_iso} | session {session_id}")

            if type_id == SESSION_TYPE_CLASSIFIED:
                tqdm.write("  [classified] skipping")
                stats["classified"] += 1
                bar.update(1)
                continue

            if status_id in _CANCELLED_STATUS_IDS:
                tqdm.write("  [cancelled] skipping")
                stats["cancelled"] += 1
                bar.update(1)
                continue

            # ── Protocol download ──────────────────────────────────────────────
            if proto_path.exists():
                tqdm.write(f"  protocol cached: {proto_path.name}")
                stats["skipped_dl"] += 1
            elif args.dry_run:
                tqdm.write("  would download protocol")
            else:
                bar.set_description("Fetching")
                transcript = get_session_transcript(session_id)
                if transcript:
                    if "speeches" in transcript:
                        tqdm.write(f"  downloaded: {len(transcript['speeches'])} speeches")
                    else:
                        tqdm.write(f"  downloaded: {len(transcript['full_text']):,} chars (PDF/Word)")
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
                    tqdm.write("  [warning] no transcript available — skipping")
                    stats["no_transcript"] += 1
                    bar.update(1)
                    bar.set_description(args.committee[:30])
                    continue

            # ── Summarization ──────────────────────────────────────────────────
            if summ_path.exists() and not args.force_summarize:
                tqdm.write(f"  summary cached: {summ_path.name}")
                stats["skipped_summ"] += 1
            elif args.dry_run:
                tqdm.write("  would summarize")
            else:
                bar.set_description("Summarizing")
                # summarize_meeting() prints its own chunk/token progress;
                # tqdm redraws the bar after each of those lines.
                summary = summarize_meeting(proto_path)
                if summary is None:
                    tqdm.write(f"  [skip-long] transcript too long")
                    stats["too_long"] += 1
                elif summary:
                    save_summary(summary, proto_path, args.knesset)
                    tqdm.write(f"  summary saved: {summ_path.name}")
                    stats["summarized"] += 1
                else:
                    tqdm.write("  [ERROR] summarization produced no output")
                    stats["failed_summ"] += 1

            bar.set_description(args.committee[:30])
            bar.update(1)
            bar.set_postfix(
                dl   = stats["downloaded_ok"],
                summ = stats["summarized"],
                skip = stats["skipped_summ"],
                refresh = False,
            )

    # ── Final report ──────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"Done — {args.committee}")
    print(f"  Total sessions    : {stats['total']}")
    if stats["classified"]:
        print(f"  Classified (skip) : {stats['classified']}")
    if stats["cancelled"]:
        print(f"  Cancelled  (skip) : {stats['cancelled']}")
    print(f"  Protocol cached   : {stats['skipped_dl']}")
    print(f"  Newly downloaded  : {stats['downloaded_ok']}")
    if stats["no_transcript"]:
        print(f"  No transcript     : {stats['no_transcript']}")
    print(f"  Summary cached    : {stats['skipped_summ']}")
    print(f"  Newly summarized  : {stats['summarized']}")
    if stats["too_long"]:
        print(f"  Too long (skip)   : {stats['too_long']}")
    if stats["failed_summ"]:
        print(f"  Summary failed    : {stats['failed_summ']}")


if __name__ == "__main__":
    main()
