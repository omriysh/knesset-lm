"""
backfill_bullet_mk_ids.py

One-off (rerunnable) metadata backfill: resolve each summary bullet's speaker
prefix to an mk_id (indexing/bullet_mk_link.py) and write ``mk_id`` +
``speaker`` into BOTH bullet stores, keyed by the shared id
``{committee}__{meeting_id}__{bullet_idx}``:

  * Chroma  — collection config.BULLETS_COLLECTION (metadata update only,
              embeddings untouched; Chroma is the driver since it is a
              superset of the BM25 store)
  * BM25    — Data/bm25/<knesset_num>/bullets.db ``extra`` JSON column
              (also copies ``date`` from Chroma metadata, which build_bullets
              never wrote)

Resolution: fuzzy match against the full mks.db roster at
PARTICIPANT_FUZZY_THRESHOLD; when the bullet's meeting has attendance rows in
meeting_index.db, an attending candidate outranks a non-attending one
(disambiguation only, never a threshold bypass). Unresolved prefixes (guests,
officials, structural labels) are simply left untagged.

Usage
    python scripts/backfill_bullet_mk_ids.py --knesset-num 25
    python scripts/backfill_bullet_mk_ids.py --dry-run
    python scripts/backfill_bullet_mk_ids.py --force   # re-resolve already-tagged bullets
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config
from indexing.bullet_mk_link import extract_speaker_prefix, resolve_prefix
from retrieval.bm25_index import BM25Index
from retrieval.meeting_index import db_path as meeting_index_db_path
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex

_CHROMA_PAGE = 5000
_UPDATE_BATCH = 2000


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[2])
    p.add_argument("--knesset-num", type=int, default=25)
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve and report stats without writing anything")
    p.add_argument("--force", action="store_true",
                   help="Re-resolve bullets that already carry an mk_id")
    return p.parse_args()


def load_roster_index(knesset_num: int) -> FuzzyNameIndex:
    path = config.BM25_DIR / str(knesset_num) / "mks.db"
    if not path.exists():
        raise FileNotFoundError(
            f"mks.db not found: {path} — run build_bm25_indexes.py --target mks first")
    with BM25Index(path) as idx:
        index = FuzzyNameIndex.from_bm25(idx)
    return index


def load_participants(knesset_num: int) -> dict[str, set[str]]:
    path = meeting_index_db_path(knesset_num)
    if not path.exists():
        print(f"[backfill] meeting_index.db not found ({path}); "
              f"resolving without attendance disambiguation")
        return {}
    conn = sqlite3.connect(str(path))
    try:
        participants: dict[str, set[str]] = {}
        for meeting_id, mk_id in conn.execute(
                "SELECT meeting_id, mk_id FROM meeting_participants"):
            participants.setdefault(str(meeting_id), set()).add(str(mk_id))
        return participants
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    t0 = time.time()

    roster_index = load_roster_index(args.knesset_num)
    participants = load_participants(args.knesset_num)
    print(f"[backfill] roster loaded; participants for "
          f"{len(participants)} meetings")

    import chromadb
    client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
    coll = client.get_collection(config.BULLETS_COLLECTION)
    total = coll.count()
    print(f"[backfill] chroma collection {config.BULLETS_COLLECTION}: "
          f"{total} bullets")

    bm25_path = config.BM25_DIR / str(args.knesset_num) / "bullets.db"
    bm25_conn = sqlite3.connect(str(bm25_path)) if bm25_path.exists() else None
    if bm25_conn is None:
        print(f"[backfill] bullets.db not found ({bm25_path}); "
              f"updating Chroma only")

    stats = {"scanned": 0, "prefixed": 0, "resolved": 0, "already_tagged": 0,
             "chroma_updated": 0, "bm25_updated": 0, "bm25_missing_row": 0}
    prefix_hits_cache: dict[str, dict | None] = {}

    def resolve(prefix: str, meeting_id: str) -> dict | None:
        cache_key = f"{meeting_id}\x00{prefix}"
        if cache_key in prefix_hits_cache:
            return prefix_hits_cache[cache_key]
        resolved = resolve_prefix(
            prefix, roster_index, participants.get(meeting_id))
        prefix_hits_cache[cache_key] = resolved
        return resolved

    pending_ids: list[str] = []
    pending_metas: list[dict] = []

    def flush_chroma() -> None:
        if not pending_ids or args.dry_run:
            pending_ids.clear()
            pending_metas.clear()
            return
        coll.update(ids=list(pending_ids), metadatas=list(pending_metas))
        stats["chroma_updated"] += len(pending_ids)
        pending_ids.clear()
        pending_metas.clear()

    def update_bm25(bullet_id: str, mk_id: str, speaker: str,
                    date: str | None) -> None:
        if bm25_conn is None or args.dry_run:
            return
        row = bm25_conn.execute(
            "SELECT extra FROM entries WHERE id = ?", (bullet_id,)).fetchone()
        if row is None:
            stats["bm25_missing_row"] += 1
            return
        try:
            extra = json.loads(row[0]) if row[0] else {}
        except json.JSONDecodeError as exc:
            print(f"[backfill] bad extra JSON for {bullet_id}: {exc}")
            extra = {}
        extra["mk_id"] = mk_id
        extra["speaker"] = speaker
        if date and not extra.get("date"):
            extra["date"] = date
        bm25_conn.execute(
            "UPDATE entries SET extra = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), bullet_id))
        stats["bm25_updated"] += 1

    offset = 0
    while offset < total:
        page = coll.get(limit=_CHROMA_PAGE, offset=offset,
                        include=["documents", "metadatas"])
        ids = page.get("ids") or []
        if not ids:
            break
        documents = page.get("documents") or []
        metadatas = page.get("metadatas") or []

        for bullet_id, text, meta in zip(ids, documents, metadatas):
            stats["scanned"] += 1
            meta = dict(meta or {})
            if meta.get("mk_id") and not args.force:
                stats["already_tagged"] += 1
                continue
            prefix = extract_speaker_prefix(text or "")
            if prefix is None:
                continue
            stats["prefixed"] += 1
            meeting_id = str(meta.get("meeting_id") or "")
            resolved = resolve(prefix, meeting_id)
            if resolved is None:
                continue
            stats["resolved"] += 1
            meta["mk_id"] = resolved["mk_id"]
            meta["speaker"] = prefix
            pending_ids.append(bullet_id)
            pending_metas.append(meta)
            update_bm25(bullet_id, resolved["mk_id"], prefix, meta.get("date"))
            if len(pending_ids) >= _UPDATE_BATCH:
                flush_chroma()

        offset += len(ids)
        print(f"[backfill] {offset}/{total} scanned  "
              f"(prefixed={stats['prefixed']} resolved={stats['resolved']})")
        if bm25_conn is not None and not args.dry_run:
            bm25_conn.commit()

    flush_chroma()
    if bm25_conn is not None:
        if not args.dry_run:
            bm25_conn.commit()
        bm25_conn.close()

    mode = "DRY RUN — nothing written" if args.dry_run else "written"
    print(f"\n[backfill] done in {time.time() - t0:.0f}s ({mode})")
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
