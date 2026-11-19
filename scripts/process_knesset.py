"""
process_knesset.py

The complete offline data refresh for a Knesset number, in one command:
  1. Download every committee session protocol and every plenum session protocol that is not on
     disk yet into Data/raw_transcriptions/<knesset>/<committee>/<DD_MM_YYYY>_<meeting_id>.json
     (utils/protocol_download.py; plenum sessions go to מליאת_הכנסת/ with meeting_id p<id>).
  2. Summarize every transcript that has no summary yet (scripts/summarize_knesset_batches.py,
     Gemini Batch API; needs GOOGLE_API_KEY or GEMINI_API_KEY). Resumes an interrupted run.
  3. Rebuild Data/knesset.db (scripts/build_knesset_db.py, all targets).
  4. Summarize the themes of every MK whose opinions changed (scripts/summarize_mk_themes_batches.py,
     Gemini Batch API, reads the rebuilt knesset.db), then load them (build_knesset_db.py --target mk_themes).
  5. Bring the MK subjects up to date with the themes (scripts/group_mk_subjects.py, interactive Gemini calls;
     only once Data/subjects/<k>/subjects.json exists, the first run is manual), then load them
     (build_knesset_db.py --target subjects).

Exits 1 when anything failed: a committee or session download, a meeting or MK that could not be
summarized, or the database build. Plenum .doc files need Microsoft Word (pywin32).

Usage
-----
    cd knesset-lm
    python scripts/process_knesset.py --knesset 25
    python scripts/process_knesset.py --knesset 25 --skip "ועדת הכנסת"
    python scripts/process_knesset.py --knesset 25 --summaries-dry-run     # cost estimates, submits nothing
    python scripts/process_knesset.py --knesset 25 --skip-download --skip-summaries --skip-db   # MK themes only
    python scripts/process_knesset.py --knesset 25 --skip-subjects
    python scripts/process_knesset.py --knesset 25 --skip-summaries --skip-db
    python scripts/process_knesset.py --knesset 25 --move-duplicate-transcripts
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from tqdm import tqdm

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import config
from utils.protocol_download import download_missing_protocols, duplicate_transcripts

DUPLICATE_TRANSCRIPTS_DIR_NAME = "duplicate_transcripts"


def _summarize(args) -> dict:
    import summarize_knesset_batches
    return summarize_knesset_batches.summarize_knesset(
        args.knesset, skip_patterns=args.skip, rescan=args.rescan, dry_run=args.summaries_dry_run)


def _summarize_mk_themes(args) -> dict:
    import summarize_mk_themes_batches
    return summarize_mk_themes_batches.summarize_mk_themes(args.knesset, dry_run=args.summaries_dry_run)


def _group_mk_subjects(args) -> dict:
    import group_mk_subjects
    return group_mk_subjects.group_mk_subjects(args.knesset, dry_run=args.summaries_dry_run)


def _build_db(knesset_num: int, target: str = "all") -> int:
    script = Path(__file__).parent / "build_knesset_db.py"
    tqdm.write(f"\nRebuilding {config.KNESSET_DB} ({target}) …")
    result = subprocess.run(
        [sys.executable, str(script), "--knesset-num", str(knesset_num), "--target", target, "--rebuild"],
        text=True,
    )
    if result.returncode != 0:
        tqdm.write(f"  [build_knesset_db ERROR] exit code {result.returncode}")
    return result.returncode


def _report_duplicates(knesset_num: int, move: bool) -> int:
    """Print meetings saved under more than one committee folder (renamed committees); with move,
    move the copies the pipeline does not use to Data/duplicate_transcripts/<knesset>/."""
    duplicates = duplicate_transcripts(knesset_num)
    if not duplicates:
        return 0
    folder_pairs: dict[tuple[str, str], int] = {}
    for _, kept, others in duplicates:
        for other in others:
            pair = (kept.parent.name, other.parent.name)
            folder_pairs[pair] = folder_pairs.get(pair, 0) + 1
    print(f"\n{len(duplicates)} meetings are saved in more than one folder "
          f"(the pipeline uses one copy of each; kept folder <- unused copies' folder):")
    for (kept_folder, other_folder), count in sorted(folder_pairs.items(), key=lambda kv: -kv[1]):
        print(f"  {count:5}  {kept_folder}  <-  {other_folder}")
    if not move:
        print("  To move the unused copies out of raw_transcriptions/ (nothing is deleted):\n"
              f"    python scripts/process_knesset.py --knesset {knesset_num} --move-duplicate-transcripts "
              "--skip-download --skip-summaries --skip-db")
        return len(duplicates)
    target_root = config.DATA_DIR / DUPLICATE_TRANSCRIPTS_DIR_NAME / str(knesset_num)
    moved = 0
    for _, _, others in duplicates:
        for other in others:
            destination = target_root / other.parent.name / other.name
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                other.replace(destination)
                moved += 1
            except Exception as exc:
                print(f"  [duplicates] could not move {other}: {exc}")
    print(f"  moved {moved} unused copies to {target_root}")
    return len(duplicates)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--knesset", type=int, default=25, help="Knesset number (default: 25)")
    ap.add_argument("--skip", nargs="*", default=[], help="Committee name patterns to skip (substring match)")
    ap.add_argument("--no-plenum", action="store_true", help="Do not download plenum session protocols")
    ap.add_argument("--skip-download", action="store_true", help="Do not download protocols")
    ap.add_argument("--skip-summaries", action="store_true", help="Do not summarize new transcripts")
    ap.add_argument("--summaries-dry-run", action="store_true",
                    help="Build the summarization requests and print their cost estimate; submit nothing")
    ap.add_argument("--rescan", action="store_true",
                    help="Summarizer: rescan the disk before finishing a leftover queue")
    ap.add_argument("--skip-db", action="store_true", help="Do not rebuild knesset.db")
    ap.add_argument("--skip-themes", action="store_true", help="Do not summarize MK themes")
    ap.add_argument("--skip-subjects", action="store_true", help="Do not update the MK subjects")
    ap.add_argument("--move-duplicate-transcripts", action="store_true",
                    help=f"Move transcript copies the pipeline does not use to Data/{DUPLICATE_TRANSCRIPTS_DIR_NAME}/")
    args = ap.parse_args()
    args.skip = [s.strip() for s in args.skip if s.strip()]

    started = time.perf_counter()
    failures: list[str] = []
    download = summaries = themes = subjects = None
    db_exit_code = themes_db_exit_code = subjects_db_exit_code = None

    if not args.skip_download:
        download = download_missing_protocols(args.knesset, args.skip, include_plenum=not args.no_plenum)
        failures += download["failures"]

    if not args.skip_summaries:
        try:
            summaries = _summarize(args)
        except Exception as exc:
            print(f"\n[summaries ERROR] {exc}")
            failures.append(f"summarization: {exc}")
        else:
            if summaries["failed"]:
                failures.append(f"summarization: {summaries['failed']} meeting(s) failed every attempt")

    if not args.skip_db and not args.summaries_dry_run:
        db_exit_code = _build_db(args.knesset)
        if db_exit_code != 0:
            failures.append(f"build_knesset_db exited with {db_exit_code}")

    if not args.skip_themes and db_exit_code in (None, 0):
        try:
            themes = _summarize_mk_themes(args)
        except Exception as exc:
            print(f"\n[MK themes ERROR] {exc}")
            failures.append(f"MK themes: {exc}")
        else:
            if themes["failed"]:
                failures.append(f"MK themes: {themes['failed']} MK(s) failed every attempt")
            if themes["summarized"]:
                themes_db_exit_code = _build_db(args.knesset, "mk_themes")
                if themes_db_exit_code != 0:
                    failures.append(f"build_knesset_db --target mk_themes exited with {themes_db_exit_code}")

    subjects_state_exists = (config.subjects_dir(args.knesset) / "subjects.json").exists()
    if not args.skip_subjects and not subjects_state_exists:
        print(f"\n[MK subjects] no subjects.json yet: run scripts/group_mk_subjects.py --knesset {args.knesset} once")
    if not args.skip_subjects and subjects_state_exists and db_exit_code in (None, 0) and themes_db_exit_code in (None, 0):
        try:
            subjects = _group_mk_subjects(args)
        except Exception as exc:
            print(f"\n[MK subjects ERROR] {exc}")
            failures.append(f"MK subjects: {exc}")
        else:
            if subjects["changed"]:
                subjects_db_exit_code = _build_db(args.knesset, "subjects")
                if subjects_db_exit_code != 0:
                    failures.append(f"build_knesset_db --target subjects exited with {subjects_db_exit_code}")

    n_duplicates = _report_duplicates(args.knesset, args.move_duplicate_transcripts)

    print(f"\n{'=' * 60}\nKnesset {args.knesset} — done in {(time.perf_counter() - started) / 60:.1f} min\n{'=' * 60}")
    if download:
        print(f"  downloaded            : {download['downloaded']}  "
              f"(sessions listed {download['sessions']}, already on disk {download['on_disk']}, "
              f"no transcript published {download['no_transcript']}, "
              f"classified/cancelled {download['classified'] + download['cancelled']})")
        print(f"  download failures     : {len(download['failures'])}")
    if summaries:
        print(f"  summarized            : {summaries['summarized']}  (not a protocol: {summaries['not_protocol']})")
        print(f"  summarization failed  : {summaries['failed']}")
        print(f"  truncated answers     : {summaries['truncated_resplit']} re-split, "
              f"{summaries['truncated_accepted']} kept without their cut-off line")
        if summaries["queued"]:
            print(f"  still queued          : {summaries['queued']}")
    if db_exit_code is not None:
        print(f"  knesset.db rebuild    : {'ok' if db_exit_code == 0 else f'FAILED (exit {db_exit_code})'}")
    if themes:
        print(f"  MK themes summarized  : {themes['summarized']}  (up to date {themes['up_to_date']}, "
              f"failed {themes['failed']})")
    if themes_db_exit_code is not None:
        print(f"  MK themes db load     : {'ok' if themes_db_exit_code == 0 else f'FAILED (exit {themes_db_exit_code})'}")
    if subjects:
        print(f"  MK subjects           : {subjects['subjects']}  (this run ${subjects['cost_usd']:.2f})")
    if subjects_db_exit_code is not None:
        print(f"  MK subjects db load   : {'ok' if subjects_db_exit_code == 0 else f'FAILED (exit {subjects_db_exit_code})'}")
    print(f"  duplicate transcripts : {n_duplicates}")
    if failures:
        print(f"\n{len(failures)} failure(s):")
        for failure in failures[:50]:
            print(f"  - {failure}")
        if len(failures) > 50:
            print(f"  … and {len(failures) - 50} more")
        sys.exit(1)


if __name__ == "__main__":
    main()
