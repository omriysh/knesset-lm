"""
scripts/build_knesset_db.py

Offline builder for Data/knesset.db (schema: src/retrieval/knesset_db_store.py).

Targets, in dependency order:
    mks         roster (oknesset + OData merge), party, name aliases
    committees  committee list
    meetings    one row per transcript + attendance (who spoke / roster, MKs resolved
                to mk_id, everyone else kept by name)
    summaries   topics + opinions from Data/summaries/<k>/**/*.json, opinion speakers
                resolved to mk_id, quotes located in the transcript
    speeches    every speech (structured or parsed from full_text), speaker resolved

Bills and votes are not stored: the agent queries them live from OData.

Usage
-----
    python scripts/build_knesset_db.py --knesset-num 25 --target all --rebuild
    python scripts/build_knesset_db.py --knesset-num 25 --target summaries      # rerunnable, upserts
    python scripts/build_knesset_db.py --knesset-num 25 --target mks,meetings --rebuild

--rebuild clears the target's rows for that Knesset first; without it rows are
upserted. FTS tables are rebuilt after every write. A target that produces
zero rows is refused (an empty table silently breaks every consumer).
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import config
from indexing.speaker_link import resolve_speaker
from retrieval import knesset_db_store as store
from summarization.output_parsing import QuoteLocator
from summarization.summary_io import SUMMARY_SUFFIX, load_summary, transcript_path_for_summary
from utils.knesset_db import _most_recent_faction, get_all_committees, get_all_mks, mk_name_variants
from utils.meeting import extract_attendance, get_meeting_speakers, load_meeting, parse_full_text_speeches
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex

TARGETS = ("mks", "committees", "meetings", "summaries", "speeches")


def _meeting_id_from_stem(stem: str) -> str:
    parts = stem.rsplit("_", 1)
    return parts[-1] if len(parts) == 2 else stem


def _iso_date(stem: str) -> str:
    parts = stem.split("_")
    if len(parts) >= 3 and len(parts[2]) == 4 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return ""


def _roster_index(conn, knesset_num: int) -> FuzzyNameIndex | None:
    entries = store.name_entries(conn, "mks", knesset_num)
    if not entries:
        print("  [roster] mks table is empty for this Knesset; run --target mks first. "
              "Names will NOT be resolved.")
        return None
    return FuzzyNameIndex(entries)


def _attendance_by_meeting(conn, knesset_num: int) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for meeting_id, mk_id in conn.execute(
            "SELECT meeting_id, mk_id FROM attendance WHERE knesset_num = ? AND mk_id IS NOT NULL",
            (knesset_num,)):
        out.setdefault(meeting_id, set()).add(mk_id)
    return out


# ── mks / committees ──────────────────────────────────────────────────────────

def build_mks(conn, knesset_num: int, rebuild: bool) -> int:
    rows = []
    for mk in get_all_mks(knesset_num):
        mk_id = str(mk.get("mk_individual_id") or mk.get("PersonID") or "")
        first = (mk.get("mk_individual_first_name") or "").strip()
        last  = (mk.get("mk_individual_name") or "").strip()
        full  = f"{first} {last}".strip()
        if not mk_id or not full:
            continue
        faction = _most_recent_faction([f for f in (mk.get("factions") or []) if f], knesset_num)
        aliases = mk_name_variants(first, last) + [a for a in (mk.get("altnames") or []) if a]
        rows.append({
            "mk_id": mk_id, "knesset_num": knesset_num, "first_name": first, "last_name": last,
            "full_name": full, "party": (faction or {}).get("faction_name", "").strip() or None,
            "aliases": " | ".join(dict.fromkeys(a for a in aliases if a and a != full)),
        })
    if not rows:
        return 0
    if rebuild:
        store.clear_target(conn, "mks", knesset_num)
    return store.insert_mks(conn, rows)


def build_committees(conn, knesset_num: int, rebuild: bool) -> int:
    rows = [{"committee_id": str(c["CommitteeID"]), "knesset_num": knesset_num,
             "name": c["Name"].strip(), "is_current": c.get("IsCurrent")}
            for c in get_all_committees(knesset_num)]
    if not rows:
        return 0
    if rebuild:
        store.clear_target(conn, "committees", knesset_num)
    return store.insert_committees(conn, rows)


# ── meetings + attendance ─────────────────────────────────────────────────────

def _resolve_name(fuzzy: FuzzyNameIndex, name: str) -> str | None:
    try:
        hits = fuzzy.search(name, top_k=1, threshold=config.PARTICIPANT_FUZZY_THRESHOLD)
    except Exception as exc:
        print(f"  [meetings] fuzzy resolve failed for {name!r}: {exc}")
        return None
    return str(hits[0]["id"]) if hits else None


def build_meetings(conn, knesset_num: int, rebuild: bool) -> int:
    root = config.transcriptions_dir(knesset_num)
    if not root.exists():
        print(f"  [meetings] transcriptions dir not found: {root}")
        return 0
    fuzzy = _roster_index(conn, knesset_num)
    party_map = store.mk_party_map(conn, knesset_num)

    meeting_rows: list[dict] = []
    attendance_rows: list[dict] = []
    n_resolved = n_guests = 0
    for json_path in sorted(root.rglob("*.json")):
        try:
            meeting = load_meeting(json_path)
        except Exception as exc:
            print(f"  [meetings] skip {json_path.name}: {exc}")
            continue
        meeting_id = _meeting_id_from_stem(json_path.stem)
        meeting_rows.append({
            "meeting_id":      meeting_id,
            "knesset_num":     knesset_num,
            "committee":       str(meeting.get("committee") or json_path.parent.name.replace("_", " ")),
            "date":            _iso_date(json_path.stem),
            "format":          "structured" if "speeches" in meeting else "full_text",
            "transcript_path": str(json_path),
        })
        if fuzzy is None:
            continue
        try:
            names = list(dict.fromkeys(get_meeting_speakers(meeting) + extract_attendance(meeting)))
        except Exception as exc:
            print(f"  [meetings] name extraction failed for {json_path.name}: {exc}")
            continue
        seen_mk: set[str] = set()
        seen_guest: set[str] = set()
        for name in names:
            mk_id = _resolve_name(fuzzy, name)
            if mk_id:
                if mk_id in seen_mk:
                    continue
                seen_mk.add(mk_id)
                n_resolved += 1
                attendance_rows.append({"meeting_id": meeting_id, "knesset_num": knesset_num,
                                        "name": name, "mk_id": mk_id, "party": party_map.get(mk_id) or None})
            elif name not in seen_guest:
                seen_guest.add(name)
                n_guests += 1
                attendance_rows.append({"meeting_id": meeting_id, "knesset_num": knesset_num,
                                        "name": name, "mk_id": None, "party": None})

    if not meeting_rows:
        return 0
    if rebuild:
        store.clear_target(conn, "meetings", knesset_num)
    else:
        conn.executemany("DELETE FROM attendance WHERE meeting_id = ?",
                         [(m["meeting_id"],) for m in meeting_rows])
    n = store.insert_meetings(conn, meeting_rows)
    store.insert_attendance(conn, attendance_rows)
    print(f"  [meetings] {n} meetings, {n_resolved} MK attendance rows, {n_guests} guest rows")
    return n


# ── summaries ─────────────────────────────────────────────────────────────────

class _MeetingQuoteIndex:
    """Per-meeting locators: one for each speech (structured or parsed from full_text)
    and one for the whole full_text, built once and reused for every opinion."""

    def __init__(self, meeting: dict) -> None:
        self.structured = "speeches" in meeting
        if self.structured:
            speeches = meeting["speeches"]
            self.full = None
        else:
            full_text = meeting.get("full_text") or ""
            speeches = parse_full_text_speeches(full_text) or []
            self.full = QuoteLocator(full_text)
        self.speeches = [QuoteLocator(s.get("text_he") or "") for s in speeches]

    def locate(self, quote: str) -> tuple[int | None, int | None]:
        """(speech_idx, quote_offset) per the knesset_db_store docstring; (None, None) when not found."""
        speech_idx = next((i for i, loc in enumerate(self.speeches) if loc.find(quote) is not None), None)
        if self.structured:
            return (speech_idx, self.speeches[speech_idx].find(quote)) if speech_idx is not None else (None, None)
        offset = self.full.find(quote)
        return (speech_idx, offset) if offset is not None else (None, None)


def build_summaries(conn, knesset_num: int, rebuild: bool) -> int:
    root = config.summaries_dir(knesset_num)
    if not root.exists():
        print(f"  [summaries] summaries dir not found: {root}")
        return 0
    roster_index = _roster_index(conn, knesset_num)
    party_map = store.mk_party_map(conn, knesset_num)
    attendees = _attendance_by_meeting(conn, knesset_num)
    known_meetings = {r[0] for r in conn.execute("SELECT meeting_id FROM meetings WHERE knesset_num = ?",
                                                 (knesset_num,))}
    if rebuild:
        store.clear_target(conn, "summaries", knesset_num)

    n_meetings = n_opinions = n_resolved = n_located = 0
    unresolved: dict[str, int] = {}
    label_cache: dict[str, dict | None] = {}
    for summary_path in sorted(root.rglob(f"*{SUMMARY_SUFFIX}")):
        try:
            summary = load_summary(summary_path)
        except Exception as exc:
            print(f"  [summaries] skip {summary_path.name}: {exc}")
            continue
        meeting_id = _meeting_id_from_stem(summary_path.stem)
        transcript_path = transcript_path_for_summary(summary_path)
        if meeting_id not in known_meetings:
            store.insert_meetings(conn, [{
                "meeting_id": meeting_id, "knesset_num": knesset_num,
                "committee": summary_path.parent.name.replace("_", " "),
                "date": _iso_date(summary_path.stem), "format": None,
                "transcript_path": str(transcript_path) if transcript_path.exists() else None}])
            known_meetings.add(meeting_id)

        quote_index = None
        if any(o["quote_verified"] for o in summary["opinions"]) and transcript_path.exists():
            try:
                quote_index = _MeetingQuoteIndex(load_meeting(transcript_path))
            except Exception as exc:
                print(f"  [summaries] transcript load failed for {meeting_id}: {exc}")

        opinions = []
        for o in summary["opinions"]:
            label = o["speaker"]
            if label not in label_cache:
                label_cache[label] = (resolve_speaker(label, roster_index, attendees.get(meeting_id))
                                      if roster_index else None)
            hit = label_cache[label]
            row = {"speaker_label": label, "opinion": o["opinion"], "quote": o["quote"],
                   "quote_verified": o["quote_verified"], "mk_id": None, "party": None,
                   "speaker_name": label, "speech_idx": None, "quote_offset": None}
            if hit:
                n_resolved += 1
                row.update(mk_id=hit["mk_id"], speaker_name=hit["mk_name"], party=party_map.get(hit["mk_id"]) or None)
            else:
                unresolved[label] = unresolved.get(label, 0) + 1
            if o["quote_verified"] and quote_index is not None:
                row["speech_idx"], row["quote_offset"] = quote_index.locate(o["quote"])
                n_located += row["quote_offset"] is not None
            opinions.append(row)
            n_opinions += 1

        store.replace_meeting_summary(conn, meeting_id, knesset_num, str(summary_path),
                                      summary["is_protocol"], summary["topics"], opinions)
        n_meetings += 1
        if n_meetings % 500 == 0:
            conn.commit()
            print(f"  [summaries] {n_meetings} meetings…")

    conn.commit()
    store.rebuild_fts(conn, "summaries")
    print(f"  [summaries] {n_meetings} meetings, {n_opinions} opinions, "
          f"{n_resolved} resolved to an MK, {n_located} quotes located")
    if unresolved:
        top = sorted(unresolved.items(), key=lambda kv: -kv[1])[:50]
        print(f"  [summaries] {len(unresolved)} distinct unresolved speaker labels; top 50:")
        for label, count in top:
            print(f"      {count:>5}  {label}")
    return n_meetings


# ── speeches ──────────────────────────────────────────────────────────────────

def build_speeches(conn, knesset_num: int, rebuild: bool) -> int:
    root = config.transcriptions_dir(knesset_num)
    if not root.exists():
        print(f"  [speeches] transcriptions dir not found: {root}")
        return 0
    fuzzy = _roster_index(conn, knesset_num)
    speaker_cache: dict[str, str | None] = {}

    def _mk_id(speaker: str) -> str | None:
        if not speaker or fuzzy is None:
            return None
        if speaker not in speaker_cache:
            hit = resolve_speaker(speaker, fuzzy)
            speaker_cache[speaker] = hit["mk_id"] if hit else None
        return speaker_cache[speaker]

    if rebuild:
        store.clear_target(conn, "speeches", knesset_num)
    total = 0
    batch: list[dict] = []
    for json_path in sorted(root.rglob("*.json")):
        try:
            meeting = load_meeting(json_path)
        except Exception as exc:
            print(f"  [speeches] skip {json_path.name}: {exc}")
            continue
        meeting_id = _meeting_id_from_stem(json_path.stem)
        if "speeches" in meeting:
            speeches = meeting["speeches"]
        else:
            full = (meeting.get("full_text") or "").strip()
            speeches = parse_full_text_speeches(full) or ([{"speaker": "", "text_he": full}] if full else [])
        for idx, speech in enumerate(speeches):
            text = (speech.get("text_he") or "").strip()
            if len(text) < config.MIN_SPEECH_CHARS:
                continue
            speaker = (speech.get("speaker") or "").strip()
            batch.append({"meeting_id": meeting_id, "knesset_num": knesset_num, "idx": idx,
                          "speaker": speaker, "mk_id": _mk_id(speaker), "text": text})
        if len(batch) >= 20_000:
            total += store.insert_speeches(conn, batch)
            batch = []
            print(f"  [speeches] {total} rows…")
    if batch:
        total += store.insert_speeches(conn, batch)
    if total:
        store.rebuild_fts(conn, "speeches")
    return total


_BUILDERS = {
    "mks": build_mks, "committees": build_committees, "meetings": build_meetings,
    "summaries": build_summaries, "speeches": build_speeches,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--knesset-num", type=int, default=25)
    ap.add_argument("--target", default="all", help="comma-separated subset of: all, " + ", ".join(TARGETS))
    ap.add_argument("--rebuild", action="store_true", help="clear the target's rows for this Knesset first")
    args = ap.parse_args()

    targets = list(TARGETS) if args.target == "all" else [t.strip() for t in args.target.split(",") if t.strip()]
    unknown = [t for t in targets if t not in _BUILDERS]
    if unknown:
        print(f"ERROR: unknown target(s) {unknown}")
        sys.exit(1)

    conn = store.connect()
    print(f"db: {store.db_path()}")
    try:
        for target in targets:
            print(f"\n[{target}] knesset={args.knesset_num} rebuild={args.rebuild}")
            t0 = time.time()
            try:
                n = _BUILDERS[target](conn, args.knesset_num, args.rebuild)
            except Exception as exc:
                print(f"  ERROR building {target}: {exc}")
                continue
            if n == 0:
                print(f"  ERROR: builder produced 0 rows for '{target}', nothing written")
                continue
            store.set_meta(conn, f"{target}:{args.knesset_num}", f"{n} rows at {time.strftime('%Y-%m-%d %H:%M')}")
            print(f"  rows={n}  elapsed={time.time() - t0:.1f}s")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
