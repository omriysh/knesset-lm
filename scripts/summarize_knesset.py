"""
summarize_knesset.py

Summarize all committees in a given Knesset number.

Fetches the full committee list from the API and runs the same
download-and-summarize pipeline as summarize_committee.py on each one.

Usage
-----
    cd knesset-lm
    python scripts/summarize_knesset.py --knesset 25
    python scripts/summarize_knesset.py --knesset 25 --dry-run
    python scripts/summarize_knesset.py --knesset 25 --force-summarize
    python scripts/summarize_knesset.py --knesset 25 --skip "ועדת הכנסת"
"""

import argparse
import json
import re
import sys
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.knesset_db import (
    get_all_committees,
    get_committee_sessions,
    get_session_transcript,
    SESSION_TYPE_CLASSIFIED,
)
from summarization.pipeline import summarize_meeting, save_summary
from config import transcriptions_dir, summaries_dir

_WIN_UNSAFE = re.compile(r'[\\/:*?"<>|]')
_CANCELLED_STATUS_IDS = {193}

MEETING_SUMMARY_RETRIES = 10
MEETING_SUMMARY_RETRY_DELAY = 600  # seconds


def _safe_dirname(name: str) -> str:
    return re.sub(r"[\s_]+", "_", _WIN_UNSAFE.sub("_", name)).strip("_")


def _session_filename(date_iso: str, session_id: int) -> str:
    if date_iso and len(date_iso) >= 10:
        y, m, d = date_iso[:10].split("-")
        return f"{d}_{m}_{y}_{session_id}"
    return f"00_00_0000_{session_id}"


def _process_committee(
    committee: dict,
    knesset_num: int,
    dry_run: bool,
    force_summarize: bool,
) -> dict:
    """
    Download and summarize all sessions for one committee.
    Drives its own inner session progress bar (position=1, transient).
    Returns a stats dict.
    """
    name         = committee["Name"]
    committee_id = committee["CommitteeID"]
    dirname      = _safe_dirname(name)
    proto_dir    = transcriptions_dir(knesset_num) / dirname
    summ_dir     = summaries_dir(knesset_num)      / dirname

    sessions = get_committee_sessions(committee_id, knesset_num)

    stats = {
        "total":         len(sessions),
        "classified":    0, "cancelled":     0,
        "skipped_dl":    0, "downloaded_ok": 0, "no_transcript": 0,
        "skipped_summ":  0, "summarized":    0, "too_long":      0, "failed_summ": 0,
    }

    if not sessions:
        return stats

    if not dry_run:
        proto_dir.mkdir(parents=True, exist_ok=True)
        summ_dir.mkdir(parents=True, exist_ok=True)

    with tqdm(
        total   = len(sessions),
        desc    = "  Sessions",
        unit    = "sess",
        leave   = False,
        position= 1,
        dynamic_ncols = True,
    ) as sbar:
        for session in sessions:
            session_id = session["session_id"]
            date_iso   = session["date"]
            type_id    = session.get("type_id")
            status_id  = session.get("status_id")
            stem       = _session_filename(date_iso, session_id)
            proto_path = proto_dir / f"{stem}.json"
            summ_path  = summ_dir  / f"{stem}.txt"

            if type_id == SESSION_TYPE_CLASSIFIED:
                stats["classified"] += 1
                sbar.update(1)
                continue

            if status_id in _CANCELLED_STATUS_IDS:
                stats["cancelled"] += 1
                sbar.update(1)
                continue

            # ── Protocol download ──────────────────────────────────────────────
            if proto_path.exists():
                stats["skipped_dl"] += 1
            elif dry_run:
                pass
            else:
                sbar.set_description("  Fetching")
                transcript = get_session_transcript(session_id)
                if transcript:
                    payload = {
                        "meeting_id":  str(session_id),
                        "date":        date_iso,
                        "committee":   name,
                        "knesset_num": knesset_num,
                        **transcript,
                    }
                    proto_path.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    stats["downloaded_ok"] += 1
                else:
                    stats["no_transcript"] += 1
                    sbar.update(1)
                    continue

            # ── Summarization ──────────────────────────────────────────────────
            if summ_path.exists() and not force_summarize:
                stats["skipped_summ"] += 1
            elif dry_run:
                pass
            else:
                sbar.set_description("  Summarizing")
                # summarize_meeting() prints its own progress to stdout;
                # tqdm will redraw the bars after each line automatically.
                for i in range(MEETING_SUMMARY_RETRIES):
                    try:
                        summary = summarize_meeting(proto_path)
                        break
                    except Exception as e:
                        stats["failed_summ"] += 1
                        tqdm.write(f"  [ERROR] summarization exception (attempt {i+1}/{MEETING_SUMMARY_RETRIES}): {proto_path.name}\n    {e}")
                        tqdm.write(f"sleeping for {MEETING_SUMMARY_RETRY_DELAY} seconds before retrying …")
                        time.sleep(MEETING_SUMMARY_RETRY_DELAY)
                if summary is None:
                    stats["too_long"] += 1
                    tqdm.write(f"  [skip-long] {proto_path.name}")
                elif summary:
                    save_summary(summary, proto_path, knesset_num)
                    stats["summarized"] += 1
                else:
                    stats["failed_summ"] += 1
                    tqdm.write(f"  [ERROR] summarization failed: {proto_path.name}")

            sbar.set_description("  Sessions")
            sbar.update(1)
            sbar.set_postfix(
                dl   = stats["downloaded_ok"],
                summ = stats["summarized"],
                skip = stats["skipped_summ"],
                err  = stats["failed_summ"] + stats["no_transcript"],
                refresh = False,
            )

    return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Download and summarize all committees for a given Knesset."
    )
    ap.add_argument("--knesset", type=int, default=25, help="Knesset number (default: 25)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be done without writing any files")
    ap.add_argument("--force-summarize", action="store_true",
                    help="Re-summarize even if a summary file already exists")
    ap.add_argument("--skip", nargs="*", default=[],
                    help="Committee names to skip (substring match)")
    args = ap.parse_args()

    tqdm.write(f"Fetching committee list for Knesset {args.knesset} …")
    committees = get_all_committees(args.knesset)
    if not committees:
        tqdm.write("No committees found.")
        sys.exit(1)
    tqdm.write(f"Found {len(committees)} committees.\n")

    skip_patterns = [s.strip() for s in args.skip if s.strip()]
    if skip_patterns:
        before = len(committees)
        committees = [c for c in committees if not any(p in c["Name"] for p in skip_patterns)]
        tqdm.write(f"Skipping {before - len(committees)} committee(s) by name pattern.")

    grand: dict[str, int] = {
        "total":         0, "classified":    0, "cancelled":     0,
        "skipped_dl":    0, "downloaded_ok": 0, "no_transcript": 0,
        "skipped_summ":  0, "summarized":    0, "too_long":      0, "failed_summ":   0,
    }

    with tqdm(
        total    = len(committees),
        desc     = "Committees",
        unit     = "committee",
        position = 0,
        dynamic_ncols = True,
    ) as cbar:
        for committee in committees:
            name = committee["Name"]
            cbar.set_postfix_str(name[:30], refresh=True)

            stats = _process_committee(
                committee, args.knesset, args.dry_run, args.force_summarize
            )
            for k, v in stats.items():
                grand[k] += v

            # Print per-committee summary above the bars
            tqdm.write(_format_stats(name, stats))
            cbar.update(1)
            cbar.set_postfix(
                summ  = grand["summarized"],
                cache = grand["skipped_summ"],
                err   = grand["failed_summ"],
                refresh = False,
            )

    # ── Grand totals ──────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"GRAND TOTAL — Knesset {args.knesset}  ({len(committees)} committees)")
    print(f"{'='*60}")
    _print_stats(grand)


def _format_stats(label: str, stats: dict) -> str:
    """One-line committee summary for tqdm.write."""
    parts = []
    if stats["summarized"]:
        parts.append(f"+{stats['summarized']} summ")
    if stats["downloaded_ok"]:
        parts.append(f"+{stats['downloaded_ok']} dl")
    if stats["skipped_summ"]:
        parts.append(f"{stats['skipped_summ']} cached")
    if stats["too_long"]:
        parts.append(f"{stats['too_long']} long")
    if stats["no_transcript"]:
        parts.append(f"{stats['no_transcript']} no-transcript")
    if stats["failed_summ"]:
        parts.append(f"{stats['failed_summ']} ERR")
    summary = ", ".join(parts) if parts else "nothing new"
    return f"  {label[:40]}: {summary}"


def _print_stats(stats: dict) -> None:
    print(f"  Sessions          : {stats['total']}")
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
