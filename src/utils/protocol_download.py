"""
utils/protocol_download.py

The one download path for protocol transcripts, used by scripts/process_knesset.py:

    Data/raw_transcriptions/<knesset>/<safe_dirname(committee)>/<DD_MM_YYYY>_<meeting_id>.json

Committee meetings come from OData KNS_CommitteeSession (transcript from oknesset.org or the
session's protocol document); plenum sessions from the "דברי הכנסת" document of
KNS_DocumentPlenumSession, stored as meetings of config.PLENUM_COMMITTEE_NAME with meeting_id
config.PLENUM_MEETING_ID_PREFIX + session id. A meeting already on disk under any committee
folder (committees get renamed) is not downloaded again.
"""

import json
import re
from datetime import date, timedelta
from pathlib import Path

from tqdm import tqdm

import config
from summarization.summary_io import summary_path_for_transcript
from utils.cache import SESSION as HTTP_SESSION
from utils.knesset_db import (
    SESSION_TYPE_CLASSIFIED,
    get_all_committees,
    get_committee_sessions,
    get_plenum_protocol_documents,
    get_plenum_session_transcript,
    get_session_transcript,
)

NOT_MEETINGS_DIR = "not_meetings"
CANCELLED_SESSION_STATUS_IDS = {193}
# Sessions this recent skip the week-long HTTP cache, so a protocol published since the
# last run is not hidden behind a cached "no documents" answer.
RECENT_SESSION_DAYS = 45
_WINDOWS_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|]')

DOWNLOAD_COUNTERS = ("sessions", "classified", "cancelled", "on_disk", "downloaded", "no_transcript", "failed")


def safe_dirname(name: str) -> str:
    """'ועדת  החוקה, חוק ומשפט ' -> 'ועדת_החוקה,_חוק_ומשפט'."""
    return re.sub(r"[\s_]+", "_", _WINDOWS_UNSAFE_CHARS.sub("_", name)).strip("_")


def meeting_file_stem(date_iso: str, meeting_id: str) -> str:
    """'2023-07-14', '12345' -> '14_07_2023_12345'."""
    if date_iso and len(date_iso) >= 10:
        year, month, day = date_iso[:10].split("-")
        return f"{day}_{month}_{year}_{meeting_id}"
    return f"00_00_0000_{meeting_id}"


def meeting_id_from_stem(stem: str) -> str:
    parts = stem.rsplit("_", 1)
    return parts[-1] if len(parts) == 2 else stem


def plenum_meeting_id(plenum_session_id: int | str) -> str:
    return f"{config.PLENUM_MEETING_ID_PREFIX}{plenum_session_id}"


def json_files_by_meeting(root: Path, label: str) -> dict[str, list[Path]]:
    """All *.json under root grouped by meeting_id, skipping NOT_MEETINGS_DIR folders."""
    by_meeting: dict[str, list[Path]] = {}
    if not root.exists():
        return by_meeting
    for json_path in sorted(root.rglob("*.json")):
        if NOT_MEETINGS_DIR in json_path.relative_to(root).parts:
            continue
        by_meeting.setdefault(meeting_id_from_stem(json_path.stem), []).append(json_path)
    n_duplicated = sum(1 for paths in by_meeting.values() if len(paths) > 1)
    if n_duplicated:
        print(f"  [{label}] {n_duplicated} meeting_ids have more than one file; keeping one each")
    return by_meeting


def _newest(paths: list[Path]) -> Path:
    return max(paths, key=lambda path: path.stat().st_mtime)


def choose_transcript(paths: list[Path]) -> Path:
    """Among copies of one meeting (saved under a renamed committee folder) prefer the one that
    has a summary, then the newest file."""
    with_summary = [path for path in paths if summary_path_for_transcript(path).exists()]
    return _newest(with_summary or paths)


def transcripts_by_meeting(knesset_num: int, label: str = "transcripts") -> dict[str, Path]:
    """One transcript per meeting_id (see choose_transcript)."""
    return {meeting_id: choose_transcript(paths)
            for meeting_id, paths in json_files_by_meeting(config.transcriptions_dir(knesset_num), label).items()}


def duplicate_transcripts(knesset_num: int) -> list[tuple[str, Path, list[Path]]]:
    """[(meeting_id, kept copy, other copies)] for meetings saved in more than one folder."""
    duplicates = []
    for meeting_id, paths in json_files_by_meeting(config.transcriptions_dir(knesset_num), "duplicates").items():
        if len(paths) > 1:
            kept = choose_transcript(paths)
            duplicates.append((meeting_id, kept, [path for path in paths if path != kept]))
    return duplicates


def _write_transcript(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".json.part")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(path)


def _is_recent(date_iso: str) -> bool:
    return bool(date_iso) and date_iso >= (date.today() - timedelta(days=RECENT_SESSION_DAYS)).isoformat()


def _fetch_session_transcript(session_id: int, date_iso: str) -> dict | None:
    if _is_recent(date_iso):
        with HTTP_SESSION.cache_disabled():
            return get_session_transcript(session_id)
    return get_session_transcript(session_id)


def download_committee_protocols(committee: dict, knesset_num: int, meetings_on_disk: set[str],
                                 failures: list[str]) -> dict:
    """Download every public session protocol of one committee that is not on disk yet."""
    name = committee["Name"]
    proto_dir = config.transcriptions_dir(knesset_num) / safe_dirname(name)
    stats = dict.fromkeys(DOWNLOAD_COUNTERS, 0)
    with HTTP_SESSION.cache_disabled():
        sessions = get_committee_sessions(committee["CommitteeID"], knesset_num)
    stats["sessions"] = len(sessions)

    for session in tqdm(sessions, desc=f"  {name[:30]}", unit="sess", leave=False, dynamic_ncols=True):
        if session.get("type_id") == SESSION_TYPE_CLASSIFIED:
            stats["classified"] += 1
            continue
        if session.get("status_id") in CANCELLED_SESSION_STATUS_IDS:
            stats["cancelled"] += 1
            continue
        meeting_id = str(session["session_id"])
        if meeting_id in meetings_on_disk:
            stats["on_disk"] += 1
            continue
        try:
            transcript = _fetch_session_transcript(session["session_id"], session["date"])
        except Exception as exc:
            print(f"  [download] {name} session {meeting_id} failed: {exc}")
            failures.append(f"{name} session {meeting_id}: {exc}")
            stats["failed"] += 1
            continue
        if not transcript:
            stats["no_transcript"] += 1
            continue
        _write_transcript(proto_dir / f"{meeting_file_stem(session['date'], meeting_id)}.json",
                          {"meeting_id": meeting_id, "date": session["date"], "committee": name,
                           "knesset_num": knesset_num, **transcript})
        meetings_on_disk.add(meeting_id)
        stats["downloaded"] += 1
    return stats


def download_plenum_protocols(knesset_num: int, meetings_on_disk: set[str], failures: list[str]) -> dict:
    """Download the "דברי הכנסת" protocol of every plenum session that is not on disk yet."""
    proto_dir = config.transcriptions_dir(knesset_num) / safe_dirname(config.PLENUM_COMMITTEE_NAME)
    stats = dict.fromkeys(DOWNLOAD_COUNTERS, 0)
    with HTTP_SESSION.cache_disabled():
        documents = get_plenum_protocol_documents(knesset_num)
    stats["sessions"] = len(documents)

    for document in tqdm(documents, desc="  plenum", unit="sess", leave=False, dynamic_ncols=True):
        meeting_id = plenum_meeting_id(document["session_id"])
        if meeting_id in meetings_on_disk:
            stats["on_disk"] += 1
            continue
        try:
            transcript = get_plenum_session_transcript(document)
        except Exception as exc:
            print(f"  [download] plenum session {meeting_id} ({document['url']}) failed: {exc}")
            transcript, error = None, exc
        else:
            error = "no text extracted (the .doc needs Microsoft Word through pywin32)"
        if not transcript:
            failures.append(f"plenum session {meeting_id} ({document['url']}): {error}")
            stats["failed"] += 1
            continue
        _write_transcript(proto_dir / f"{meeting_file_stem(document['date'], meeting_id)}.json", {
            "meeting_id": meeting_id, "date": document["date"], "committee": config.PLENUM_COMMITTEE_NAME,
            "knesset_num": knesset_num, "plenum_session_number": document["session_number"],
            "source_document_id": document["document_id"], "source_updated": document["updated"],
            **transcript})
        meetings_on_disk.add(meeting_id)
        stats["downloaded"] += 1
    return stats


def download_missing_protocols(knesset_num: int, skip_patterns: list[str] = (),
                               include_committees: bool = True, include_plenum: bool = True) -> dict:
    """
    Download every committee and plenum protocol of a Knesset that is not on disk yet.
    Returns the summed counters of DOWNLOAD_COUNTERS plus "failures": [message, ...], one per
    committee whose session list failed and per session whose download failed.
    """
    totals: dict = dict.fromkeys(DOWNLOAD_COUNTERS, 0)
    failures: list[str] = []
    meetings_on_disk = set(json_files_by_meeting(config.transcriptions_dir(knesset_num), "transcripts"))

    def add(stats: dict) -> None:
        for key in DOWNLOAD_COUNTERS:
            totals[key] += stats[key]

    if include_committees:
        with HTTP_SESSION.cache_disabled():
            committees = get_all_committees(knesset_num)
        if not committees:
            failures.append(f"no committees listed for Knesset {knesset_num}")
        committees = [c for c in committees if not any(p in c["Name"] for p in skip_patterns)]
        tqdm.write(f"Downloading protocols of {len(committees)} committees (Knesset {knesset_num}) …")
        for committee in tqdm(committees, desc="Committees", unit="committee", dynamic_ncols=True):
            try:
                stats = download_committee_protocols(committee, knesset_num, meetings_on_disk, failures)
            except Exception as exc:
                print(f"  [download] committee {committee['Name']} failed: {exc}")
                failures.append(f"committee {committee['Name']}: {exc}")
                continue
            add(stats)
            if stats["downloaded"] or stats["failed"]:
                tqdm.write(f"  {committee['Name'][:50]}: +{stats['downloaded']} downloaded, "
                           f"{stats['failed']} failed, {stats['no_transcript']} without transcript")

    if include_plenum:
        tqdm.write(f"Downloading plenum protocols (Knesset {knesset_num}) …")
        try:
            stats = download_plenum_protocols(knesset_num, meetings_on_disk, failures)
        except Exception as exc:
            print(f"  [download] plenum session list failed: {exc}")
            failures.append(f"plenum session list: {exc}")
        else:
            add(stats)
            tqdm.write(f"  plenum: +{stats['downloaded']} downloaded, {stats['on_disk']} on disk, "
                       f"{stats['failed']} failed")

    totals["failures"] = failures
    return totals
