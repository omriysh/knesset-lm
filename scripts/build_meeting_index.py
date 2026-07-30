"""
scripts/build_meeting_index.py

Offline builder for the structural (committee / date / participant) meeting
index used by the reading-tab prefilter in web/app.py::browse_rag().

Unlike the BM25 FTS5 indexes (scripts/build_bm25_indexes.py), this is a
plain relational SQLite db — see src/retrieval/meeting_index.py for schema
and query helpers.

Participants (meeting_participants, mk_id rows) are derived from two
unioned sources, each fuzzy-resolved against the "mks" BM25 index to a
canonical mk_id (utils.tool_helpers.fuzzy_name_index):
  1. who *spoke* in the meeting (utils.meeting.get_meeting_speakers()).
  2. the full נכחו: attendance roster (utils.meeting.extract_attendance())
     — catches attendees who were silently present but never spoke, the
     originally-known limitation of speaker-only derivation. (Earlier
     versions of this builder didn't use extract_attendance() at all,
     because its full_text parsing was broken — see git history / that
     function's docstring for the real-data-backed rewrite.)
Names from either source that DON'T resolve to a known mk_id (ministry
officials, private citizens, etc.) go into meeting_guests (name rows)
instead — see insert_guests() / meeting_guests schema in meeting_index.py.

Requires Data/bm25/<knesset_num>/mks.db to already exist (build it first
via build_bm25_indexes.py --target mks) — without it, participants can't be
resolved and only the `meetings` table (committee/date) is populated.

Usage
-----
    python scripts/build_meeting_index.py --knesset-num 25
    python scripts/build_meeting_index.py --knesset-num 25 --rebuild

Output: Data/bm25/<knesset_num>/meeting_index.db
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Absolute-import bootstrap
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config
from retrieval.bm25_index import BM25Index
from retrieval.meeting_index import (
    create_tables, db_path, insert_guests, insert_meetings, insert_participants,
)
from utils.knesset_db import get_all_mks, _most_recent_faction
from utils.meeting import extract_attendance, get_meeting_speakers, load_meeting
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build the structural meeting index for KnessetLM")
    p.add_argument("--knesset-num", type=int, default=25, metavar="N",
                   help="Knesset number to index (default: 25)")
    p.add_argument("--rebuild", action="store_true",
                   help="Drop and recreate tables even if they already exist")
    return p.parse_args()


# ── helpers ───────────────────────────────────────────────────────────────────

def _iso_date(stem_parts: list[str]) -> str:
    """['DD','MM','YYYY', ...] -> 'YYYY-MM-DD'. Returns '' if not parseable."""
    if len(stem_parts) < 3:
        return ""
    d, m, y = stem_parts[0], stem_parts[1], stem_parts[2]
    if len(y) == 4 and d.isdigit() and m.isdigit():
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return ""


def _load_party_map(knesset_num: int) -> dict[str, str]:
    """mk_id -> current faction name (same lookup app.py's old party filter used)."""
    party_map: dict[str, str] = {}
    try:
        mks = get_all_mks(knesset_num)
    except Exception as exc:
        print(f"  [meeting_index] get_all_mks failed: {exc}")
        return party_map
    for mk in mks:
        mk_id = str(mk.get("mk_individual_id") or mk.get("PersonID") or "")
        if not mk_id:
            continue
        faction = _most_recent_faction(
            [f for f in (mk.get("factions") or []) if f], knesset_num
        )
        if faction:
            party_map[mk_id] = (faction.get("faction_name") or "").strip()
    return party_map


def _load_fuzzy_mk_index(knesset_num: int) -> FuzzyNameIndex | None:
    mks_db_path = config.BM25_DIR / str(knesset_num) / "mks.db"
    if not mks_db_path.exists():
        print(f"  [meeting_index] mks.db not built yet ({mks_db_path}); "
              f"run build_bm25_indexes.py --target mks first. "
              f"Participants will NOT be resolved this run.")
        return None
    bm25_mks = BM25Index(mks_db_path)
    try:
        return FuzzyNameIndex.from_bm25(bm25_mks)
    except Exception as exc:
        print(f"  [meeting_index] failed to load mks fuzzy index: {exc}")
        return None
    finally:
        bm25_mks.close()


# ── build ─────────────────────────────────────────────────────────────────────

def build(knesset_num: int, rebuild: bool) -> None:
    transcriptions_root = config.transcriptions_dir(knesset_num)
    if not transcriptions_root.exists():
        print(f"  [meeting_index] transcriptions dir not found: {transcriptions_root}")
        return

    fuzzy_index = _load_fuzzy_mk_index(knesset_num)
    party_map = _load_party_map(knesset_num)

    meeting_rows: list[dict] = []
    participant_rows: list[dict] = []
    guest_rows: list[dict] = []
    n_files = 0
    n_resolved = 0
    n_unresolved_speakers = 0
    n_guests = 0

    for json_path in sorted(transcriptions_root.rglob("*.json")):
        try:
            meeting = load_meeting(json_path)
        except Exception as exc:
            print(f"  [meeting_index] skip {json_path.name}: {exc}")
            continue

        n_files += 1
        stem = json_path.stem  # DD_MM_YYYY_<session_id>
        parts = stem.rsplit("_", 1)
        meeting_id = parts[-1] if len(parts) == 2 else stem
        # Fallback (folder name) uses underscores (ועדת_החוץ_והביטחון);
        # meeting["committee"] (when present) uses spaces. Normalize the
        # fallback the same way web/app.py::browse_rag's _norm() normalizes
        # filter input (replace "_" with " "), so a meeting that hits this
        # fallback never silently becomes unmatchable by committee filter.
        # Scanned all 8600 files under Data/raw_transcriptions/25/ — 0 are
        # missing "committee", so this fallback is currently unreachable in
        # practice; normalized defensively anyway for future/other knessets.
        committee = str(meeting.get("committee") or json_path.parent.name.replace("_", " "))
        date_iso = _iso_date(stem.split("_"))

        meeting_rows.append({
            "meeting_id": meeting_id,
            "committee": committee,
            "date": date_iso,
            "knesset_num": knesset_num,
        })

        if fuzzy_index is None:
            continue

        try:
            speakers = get_meeting_speakers(meeting)
        except Exception as exc:
            print(f"  [meeting_index] speaker extraction failed for {json_path.name}: {exc}")
            continue

        seen_mk_ids: set[str] = set()
        seen_guest_names: set[str] = set()
        speaker_names_resolved: set[str] = set()
        unresolved_speaker_names: set[str] = set()
        for speaker in speakers:
            try:
                matches = fuzzy_index.search(
                    speaker, top_k=1, threshold=config.PARTICIPANT_FUZZY_THRESHOLD
                )
            except Exception as exc:
                print(f"  [meeting_index] fuzzy resolve failed for speaker "
                      f"{speaker!r} in {json_path.name}: {exc}")
                continue
            if not matches:
                n_unresolved_speakers += 1
                unresolved_speaker_names.add(speaker)
                continue
            mk_id = str(matches[0]["extra"].get("mk_id") or matches[0]["id"] or "")
            if not mk_id:
                continue
            speaker_names_resolved.add(speaker)
            if mk_id in seen_mk_ids:
                continue
            seen_mk_ids.add(mk_id)
            n_resolved += 1
            participant_rows.append({
                "meeting_id": meeting_id,
                "mk_id": mk_id,
                "party": party_map.get(mk_id, ""),
            })

        # Full נכחו: roster (extract_attendance) — catches silently-present
        # attendees who never spoke, the known limitation of speaker-only
        # derivation. Names already resolved via the speaker loop above are
        # skipped (redundant fuzzy work, same deterministic outcome); names
        # already known-unresolved as speakers go straight to guest_rows
        # without re-querying (also deterministic — same outcome either way).
        try:
            attendance_names = extract_attendance(meeting)
        except Exception as exc:
            print(f"  [meeting_index] attendance extraction failed for {json_path.name}: {exc}")
            attendance_names = []

        for name in unresolved_speaker_names:
            if name in seen_guest_names:
                continue
            seen_guest_names.add(name)
            n_guests += 1
            guest_rows.append({"meeting_id": meeting_id, "name": name})

        to_resolve = set(attendance_names) - speaker_names_resolved - unresolved_speaker_names
        for name in to_resolve:
            try:
                matches = fuzzy_index.search(
                    name, top_k=1, threshold=config.PARTICIPANT_FUZZY_THRESHOLD
                )
            except Exception as exc:
                print(f"  [meeting_index] fuzzy resolve failed for attendee "
                      f"{name!r} in {json_path.name}: {exc}")
                continue
            if matches:
                mk_id = str(matches[0]["extra"].get("mk_id") or matches[0]["id"] or "")
                if mk_id and mk_id not in seen_mk_ids:
                    seen_mk_ids.add(mk_id)
                    n_resolved += 1
                    participant_rows.append({
                        "meeting_id": meeting_id,
                        "mk_id": mk_id,
                        "party": party_map.get(mk_id, ""),
                    })
            elif name not in seen_guest_names:
                seen_guest_names.add(name)
                n_guests += 1
                guest_rows.append({"meeting_id": meeting_id, "name": name})

    print(f"  [meeting_index] scanned {n_files} meetings — "
          f"{n_resolved} name->mk resolutions "
          f"({n_unresolved_speakers} unresolved speakers), "
          f"{n_guests} guest rows (non-MK attendees)")

    path = db_path(knesset_num)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        create_tables(conn, force_rebuild=rebuild)
        n_m = insert_meetings(conn, meeting_rows)
        n_p = insert_participants(conn, participant_rows)
        n_g = insert_guests(conn, guest_rows)
        print(f"  [meeting_index] wrote {n_m} meetings, {n_p} participant rows, "
              f"{n_g} guest rows -> {path}")
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    print(f"Building meeting_index: knesset={args.knesset_num}  rebuild={args.rebuild}")
    t0 = time.time()
    build(args.knesset_num, args.rebuild)
    print(f"Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
