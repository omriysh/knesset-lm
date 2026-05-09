"""
scripts/build_bm25_indexes.py

Offline script that builds the SQLite FTS5 BM25 indexes used by the
plan-and-execute research agent.

Usage
-----
    python scripts/build_bm25_indexes.py --knesset-num 25 --target mks
    python scripts/build_bm25_indexes.py --knesset-num 25 --target all --rebuild
    python scripts/build_bm25_indexes.py --knesset-num 25 --target bullets --no-lemma

Output files: Data/bm25/<knesset_num>/<target>.db
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Absolute-import bootstrap
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config
from retrieval.bm25_index import BM25Index
from retrieval.lemmatize import lemmatize

# ── CLI ───────────────────────────────────────────────────────────────────────

TARGETS = ("bullets", "speeches", "mks", "committees", "bills", "votes")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build BM25 FTS5 indexes for KnessetLM")
    p.add_argument("--knesset-num", type=int, default=25, metavar="N",
                   help="Knesset number to index (default: 25)")
    p.add_argument("--target", choices=list(TARGETS) + ["all"], default="all",
                   help="Which index to build (default: all)")
    p.add_argument("--no-lemma", action="store_true",
                   help="Force passthrough lemmatizer (no Dicta-BERT)")
    p.add_argument("--rebuild", action="store_true",
                   help="Drop and recreate the table even if it already exists")
    return p.parse_args()


# ── helpers ───────────────────────────────────────────────────────────────────

def _db_path(knesset_num: int, target: str) -> Path:
    return config.BM25_DIR / str(knesset_num) / f"{target}.db"


def _lem(text: str) -> str:
    """Apply lemmatize() — honours the module-level USE_DICTABERT_LEMMA flag."""
    return lemmatize(text)


def _make_row(
    id_: str | int,
    label: str,
    body: str,
    extra: dict,
) -> dict:
    return {
        "id":               str(id_),
        "label":            label,
        "label_lemmatized": _lem(label),
        "body":             body,
        "body_lemmatized":  _lem(body),
        "extra":            extra,
    }


# ── per-target builders ───────────────────────────────────────────────────────

def build_mks(knesset_num: int) -> list[dict]:
    from utils.knesset_db import get_all_mks
    mks = get_all_mks(knesset_num)
    rows = []
    for mk in mks:
        mk_id    = mk.get("mk_individual_id") or mk.get("PersonID") or ""
        first    = (mk.get("mk_individual_first_name") or "").strip()
        last     = (mk.get("mk_individual_name") or "").strip()
        full     = f"{first} {last}".strip()
        altnames = mk.get("altnames") or []

        # Build faction history for body
        factions = mk.get("factions") or []
        faction_parts = []
        for f in factions:
            if f and f.get("knesset") == knesset_num:
                faction_parts.append(f.get("faction_name", "").strip())

        body_parts = [full]
        body_parts.extend(a for a in altnames if a)
        body_parts.extend(faction_parts)
        body = " | ".join(p for p in body_parts if p)

        extra = {
            "mk_id":    str(mk_id),
            "full_name": full,
            "factions": list(dict.fromkeys(faction_parts)),
        }
        rows.append(_make_row(mk_id, full, body, extra))
    return rows


def build_committees(knesset_num: int) -> list[dict]:
    from utils.knesset_db import get_all_committees
    committees = get_all_committees(knesset_num)
    rows = []
    for c in committees:
        cid  = c.get("CommitteeID", "")
        name = c.get("Name", "").strip()
        extra = {
            "committee_id": str(cid),
            "knesset_num":  knesset_num,
            "is_current":   c.get("IsCurrent"),
        }
        rows.append(_make_row(cid, name, name, extra))
    return rows


def build_bullets(knesset_num: int) -> list[dict]:
    from indexing.parse_summary import parse_summary_bullets
    summaries_root = config.summaries_dir(knesset_num)
    rows = []
    if not summaries_root.exists():
        print(f"  [bullets] summaries dir not found: {summaries_root}")
        return rows

    for summary_path in sorted(summaries_root.rglob("*.txt")):
        try:
            bullets = parse_summary_bullets(summary_path, sections_wanted=None)
        except Exception as exc:
            print(f"  [bullets] skip {summary_path.name}: {exc}")
            continue

        # Derive a meeting_id from the filename stem
        stem = summary_path.stem  # e.g. 01_01_2023_12345
        parts = stem.rsplit("_", 1)
        meeting_id = parts[-1] if len(parts) == 2 else stem

        # Prefer committee name from meeting JSON (matches Chroma metadata).
        # Directory name uses underscores; JSON field has the real name with spaces.
        json_path = config.transcriptions_dir(knesset_num) / summary_path.parent.name / (stem + ".json")
        committee = summary_path.parent.name
        if json_path.exists():
            try:
                import json as _json
                _data = _json.loads(json_path.read_text(encoding="utf-8"))
                committee = str(_data.get("committee") or committee)
            except Exception:
                pass

        for bullet in bullets:
            text = bullet.get("text", "").strip() if isinstance(bullet, dict) else str(bullet).strip()
            if not text:
                continue
            bullet_idx = bullet["idx"]
            row_id = f"{committee}__{meeting_id}__{bullet_idx}"
            extra = {
                "meeting_id":  meeting_id,
                "committee":   committee,
                "bullet_idx":  bullet_idx,
                "source_file": str(summary_path),
            }
            rows.append(_make_row(row_id, text[:120], text, extra))
    return rows


def build_speeches(knesset_num: int) -> list[dict]:
    from utils.meeting import load_meeting
    transcriptions_root = config.transcriptions_dir(knesset_num)
    rows = []
    if not transcriptions_root.exists():
        print(f"  [speeches] transcriptions dir not found: {transcriptions_root}")
        return rows

    for json_path in sorted(transcriptions_root.rglob("*.json")):
        try:
            meeting = load_meeting(json_path)
        except Exception as exc:
            print(f"  [speeches] skip {json_path.name}: {exc}")
            continue

        stem = json_path.stem
        parts = stem.rsplit("_", 1)
        meeting_id = parts[-1] if len(parts) == 2 else stem
        committee = json_path.parent.name

        # Structured format: iterate speeches directly
        if "speeches" in meeting:
            for i, speech in enumerate(meeting["speeches"]):
                speaker = (speech.get("speaker") or "").strip()
                text    = (speech.get("text_he") or "").strip()
                if len(text) < config.MIN_SPEECH_CHARS:
                    continue
                body  = f"{speaker}: {text}" if speaker else text
                label = speaker or f"speech_{i}"
                extra = {
                    "meeting_id": meeting_id,
                    "committee":  committee,
                    "speech_idx": i,
                    "speaker":    speaker,
                }
                rows.append(_make_row(f"{meeting_id}_{i}", label, body, extra))
        else:
            # full_text format — index as a single document
            full = (meeting.get("full_text") or "").strip()
            if len(full) < config.MIN_SPEECH_CHARS:
                continue
            extra = {
                "meeting_id": meeting_id,
                "committee":  committee,
                "speech_idx": 0,
                "speaker":    "",
            }
            rows.append(_make_row(f"{meeting_id}_0", committee, full, extra))

    return rows


def build_bills(knesset_num: int) -> list[dict]:
    """Paginated OData fetch of bills for the given Knesset."""
    import requests as _requests
    base_url = f"{config.OFFICIAL_KNESSET_NEW_API}/KNS_Bill"
    params = {
        "$filter": f"KnessetNum eq {knesset_num}",
        "$expand": "KNS_Status,KNS_BillInitiator($expand=KNS_Person)",
        "$top":    200,
        "$skip":   0,
        "$orderby": "Id asc",
    }
    rows = []
    while True:
        try:
            r = _requests.get(base_url, params=params, timeout=config.API_TIMEOUT)
            r.raise_for_status()
        except Exception as exc:
            print(f"  [bills] fetch error at skip={params['$skip']}: {exc}")
            break
        data  = r.json().get("value", [])
        if not data:
            break
        for bill in data:
            bid   = bill.get("Id", "")
            name  = (bill.get("Name") or "").strip()
            initiators = [
                f"{bi['KNS_Person'].get('FirstName','')} {bi['KNS_Person'].get('LastName','')}".strip()
                for bi in (bill.get("KNS_BillInitiator") or [])
                if bi.get("KNS_Person")
            ]
            status = (bill.get("KNS_Status") or {}).get("Desc", "")
            body   = " | ".join(filter(None, [name] + initiators + [status]))
            extra  = {
                "bill_id":    str(bid),
                "bill_name":  name,
                "status":     status,
                "initiators": initiators,
                "knesset_num": knesset_num,
            }
            rows.append(_make_row(bid, name, body, extra))
        params["$skip"] += 200
        if len(data) < 200:
            break
    return rows


def build_votes(knesset_num: int) -> list[dict]:
    """Paginated OData fetch of plenum votes for the given Knesset."""
    import requests as _requests
    base_url = f"{config.OFFICIAL_KNESSET_NEW_API}/KNS_PlenumVote"
    params = {
        "$filter": f"KnessetNum eq {knesset_num}",
        "$top":    200,
        "$skip":   0,
        "$orderby": "Id asc",
    }
    rows = []
    while True:
        try:
            r = _requests.get(base_url, params=params, timeout=config.API_TIMEOUT)
            r.raise_for_status()
        except Exception as exc:
            print(f"  [votes] fetch error at skip={params['$skip']}: {exc}")
            break
        data = r.json().get("value", [])
        if not data:
            break
        for vote in data:
            vid     = vote.get("Id", "")
            title   = (vote.get("VoteTitle") or vote.get("Title") or "").strip()
            subject = (vote.get("VoteSubject") or vote.get("ItemDesc") or "").strip()
            body    = " | ".join(filter(None, [title, subject]))
            extra   = {
                "vote_id":     str(vid),
                "vote_title":  title,
                "subject":     subject,
                "knesset_num": knesset_num,
            }
            rows.append(_make_row(vid, title or subject, body, extra))
        params["$skip"] += 200
        if len(data) < 200:
            break
    return rows


# ── target dispatch ───────────────────────────────────────────────────────────

_BUILDERS = {
    "mks":        build_mks,
    "committees": build_committees,
    "bullets":    build_bullets,
    "speeches":   build_speeches,
    "bills":      build_bills,
    "votes":      build_votes,
}


def run_target(target: str, knesset_num: int, rebuild: bool) -> None:
    db_path = _db_path(knesset_num, target)
    print(f"\n[{target}] -> {db_path}")
    t0 = time.time()
    try:
        rows = _BUILDERS[target](knesset_num)
    except Exception as exc:
        print(f"  ERROR building rows: {exc}")
        return

    try:
        with BM25Index(db_path) as idx:
            idx.create_table(force_rebuild=rebuild)
            count = idx.insert_many(rows)
    except Exception as exc:
        print(f"  ERROR writing index: {exc}")
        return

    elapsed = time.time() - t0
    print(f"  rows={count}  elapsed={elapsed:.1f}s")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    # Override lemmatize flag at runtime
    if args.no_lemma:
        config.USE_DICTABERT_LEMMA = False

    targets = list(TARGETS) if args.target == "all" else [args.target]

    print(f"Building BM25 indexes: knesset={args.knesset_num}  targets={targets}  "
          f"lemma={config.USE_DICTABERT_LEMMA}  rebuild={args.rebuild}")

    for target in targets:
        run_target(target, args.knesset_num, args.rebuild)

    print("\nDone.")


if __name__ == "__main__":
    main()
