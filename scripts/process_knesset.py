"""
process_knesset.py

Offline data refresh for a Knesset number:
  1. For each committee, download every session protocol that is not on disk yet
     into Data/raw_transcriptions/<knesset>/<committee>/<DD_MM_YYYY_<session_id>>.json.
  2. Rebuild Data/knesset.db (scripts/build_knesset_db.py, all targets).

Summaries are produced separately by scripts/summarize_knesset_batches.py
(Gemini Batch API); run it between steps 1 and 2 when new protocols landed, or
rerun `build_knesset_db.py --target summaries` afterwards.

Usage
-----
    cd knesset-lm
    python scripts/process_knesset.py --knesset 25
    python scripts/process_knesset.py --knesset 25 --skip "ועדת הכנסת"
    python scripts/process_knesset.py --knesset 25 --skip-db
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config
from utils.knesset_db import (
    get_all_committees,
    get_committee_sessions,
    get_session_transcript,
    SESSION_TYPE_CLASSIFIED,
)

_WIN_UNSAFE = re.compile(r'[\\/:*?"<>|]')
_CANCELLED_STATUS_IDS = {193}


def _safe_dirname(name: str) -> str:
    return _WIN_UNSAFE.sub("_", name).replace(" ", "_")


def _session_filename(date_iso: str, session_id: int) -> str:
    """'2023-07-14', 12345 -> '14_07_2023_12345'."""
    y, m, d = date_iso[:10].split("-")
    return f"{d}_{m}_{y}_{session_id}"


def _download_committee(committee: dict, knesset_num: int) -> dict:
    name = committee["Name"]
    proto_dir = config.transcriptions_dir(knesset_num) / _safe_dirname(name)
    stats = {"total": 0, "classified": 0, "cancelled": 0, "cached": 0, "downloaded": 0, "no_transcript": 0}

    sessions = get_committee_sessions(committee["CommitteeID"], knesset_num)
    stats["total"] = len(sessions)
    if not sessions:
        return stats
    proto_dir.mkdir(parents=True, exist_ok=True)

    with tqdm(total=len(sessions), desc="  Sessions", unit="sess", leave=False, position=1,
              dynamic_ncols=True) as sbar:
        for session in sessions:
            sbar.update(1)
            if session.get("type_id") == SESSION_TYPE_CLASSIFIED:
                stats["classified"] += 1
                continue
            if session.get("status_id") in _CANCELLED_STATUS_IDS:
                stats["cancelled"] += 1
                continue
            session_id = session["session_id"]
            proto_path = proto_dir / f"{_session_filename(session['date'], session_id)}.json"
            if proto_path.exists():
                stats["cached"] += 1
                continue
            transcript = get_session_transcript(session_id)
            if not transcript:
                stats["no_transcript"] += 1
                continue
            payload = {"meeting_id": str(session_id), "date": session["date"], "committee": name,
                       "knesset_num": knesset_num, **transcript}
            proto_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            stats["downloaded"] += 1
            sbar.set_postfix(dl=stats["downloaded"], cached=stats["cached"], refresh=False)
    return stats


def _build_db(knesset_num: int) -> None:
    script = Path(__file__).parent / "build_knesset_db.py"
    tqdm.write(f"\nRebuilding {config.KNESSET_DB} …")
    result = subprocess.run(
        [sys.executable, str(script), "--knesset-num", str(knesset_num), "--target", "all", "--rebuild"],
        text=True,
    )
    if result.returncode != 0:
        tqdm.write(f"  [build_knesset_db ERROR] exit code {result.returncode}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--knesset", type=int, default=25, help="Knesset number (default: 25)")
    ap.add_argument("--skip", nargs="*", default=[], help="Committee name patterns to skip (substring match)")
    ap.add_argument("--skip-db", action="store_true", help="Download only; do not rebuild knesset.db")
    args = ap.parse_args()

    tqdm.write(f"Fetching committee list for Knesset {args.knesset} …")
    committees = get_all_committees(args.knesset)
    if not committees:
        tqdm.write("No committees found.")
        sys.exit(1)
    skip_patterns = [s.strip() for s in args.skip if s.strip()]
    if skip_patterns:
        before = len(committees)
        committees = [c for c in committees if not any(p in c["Name"] for p in skip_patterns)]
        tqdm.write(f"Skipping {before - len(committees)} committee(s) by name pattern.")
    tqdm.write(f"Found {len(committees)} committees.\n")

    grand = {"total": 0, "classified": 0, "cancelled": 0, "cached": 0, "downloaded": 0, "no_transcript": 0}
    t_run = time.perf_counter()
    with tqdm(total=len(committees), desc="Committees", unit="committee", position=0, dynamic_ncols=True) as cbar:
        for committee in committees:
            cbar.set_postfix_str(committee["Name"][:30], refresh=False)
            try:
                stats = _download_committee(committee, args.knesset)
            except Exception as exc:
                tqdm.write(f"  [ERROR] {committee['Name']}: {exc}")
                cbar.update(1)
                continue
            for key in grand:
                grand[key] += stats[key]
            if stats["downloaded"] or stats["no_transcript"]:
                tqdm.write(f"  {committee['Name'][:40]}: +{stats['downloaded']} dl, "
                           f"{stats['cached']} cached, {stats['no_transcript']} no-tr")
            cbar.update(1)

    print(f"\nDone in {(time.perf_counter() - t_run) / 60:.1f} min")
    for key, value in grand.items():
        print(f"  {key:<14}: {value}")

    if not args.skip_db:
        _build_db(args.knesset)


if __name__ == "__main__":
    main()
