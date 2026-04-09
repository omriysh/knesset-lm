"""
copy_chroma_db.py

Copy embeddings from the experimental ChromaDB store (Data/exp3_chroma) into
the production store (Data/chroma/<model>/), translating collection names as
needed.

Collection name mapping (Exp3/Exp4 → production):
  knesset_bullets_l1       → knesset_bullets        (renamed — cleaner)
  knesset_dialogs_pass2    → knesset_dialogs_pass2   (unchanged)
  knesset_dialogs_pass1    → knesset_dialogs_pass1   (unchanged)
  knesset_speeches         → knesset_speeches        (unchanged)

Usage
-----
    cd knesset-lm

    # Dry run — show what would be copied
    python scripts/copy_chroma_db.py --dry-run

    # Copy (skips entries already present in target)
    python scripts/copy_chroma_db.py

    # Force overwrite all entries in target
    python scripts/copy_chroma_db.py --force

    # Custom source/target paths
    python scripts/copy_chroma_db.py \\
        --src  ../Data/exp3_chroma \\
        --dest ../Data/chroma/qwen3-vl-embedding-8b
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import chromadb

import config

# Mapping: source collection name → target collection name.
# Only collections listed here are copied; others are ignored.
COLLECTION_MAP = {
    "knesset_bullets_l1":    config.BULLETS_COLLECTION,   # "knesset_bullets"
    "knesset_dialogs_pass2": config.PASS2_COLLECTION,
    "knesset_dialogs_pass1": config.PASS1_COLLECTION,
    "knesset_speeches":      config.SPEECHES_COLLECTION,
}

_BATCH_SIZE = 1000   # ChromaDB upsert limit per call


def _copy_collection(
    src_coll: chromadb.Collection,
    dst_coll,                           # chromadb.Collection | None (None = dry run)
    force: bool,
    dry_run: bool,
) -> dict:
    """Copy all entries from src_coll into dst_coll. Returns stats dict."""
    stats = {"total": 0, "copied": 0, "skipped": 0}

    all_ids = src_coll.get(include=[])["ids"]
    stats["total"] = len(all_ids)

    if not all_ids:
        return stats

    if dry_run:
        stats["copied"] = len(all_ids)
        return stats

    if not force:
        existing: set[str] = set()
        for batch_start in range(0, len(all_ids), _BATCH_SIZE):
            batch = all_ids[batch_start : batch_start + _BATCH_SIZE]
            existing.update(dst_coll.get(ids=batch, include=[])["ids"])
        ids_to_copy = [i for i in all_ids if i not in existing]
        stats["skipped"] = len(existing)
    else:
        ids_to_copy = all_ids

    stats["copied"] = len(ids_to_copy)

    if not ids_to_copy:
        return stats

    # Copy in batches to stay within ChromaDB limits
    for batch_start in range(0, len(ids_to_copy), _BATCH_SIZE):
        batch_ids = ids_to_copy[batch_start : batch_start + _BATCH_SIZE]
        rows = src_coll.get(
            ids=batch_ids,
            include=["embeddings", "documents", "metadatas"],
        )
        dst_coll.upsert(
            ids        = rows["ids"],
            embeddings = rows["embeddings"],
            documents  = rows["documents"],
            metadatas  = rows["metadatas"],
        )
        pct = min(100, int((batch_start + len(batch_ids)) / len(ids_to_copy) * 100))
        print(f"      {pct:3d}%  ({batch_start + len(batch_ids)}/{len(ids_to_copy)})",
              end="\r", flush=True)

    print()   # newline after progress
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path,
                    default=Path(__file__).parent.parent.parent / "Data" / "exp3_chroma",
                    help="Source ChromaDB directory (default: Data/exp3_chroma)")
    ap.add_argument("--dest", type=Path, default=config.CHROMA_DIR,
                    help=f"Target ChromaDB directory (default: {config.CHROMA_DIR})")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite entries already present in the target")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be copied without writing anything")
    args = ap.parse_args()

    if not args.src.exists():
        print(f"ERROR: source DB not found: {args.src}")
        sys.exit(1)

    print(f"Source : {args.src}")
    print(f"Target : {args.dest}")
    if args.dry_run:
        print("(dry run — nothing will be written)")
    print()

    src_client = chromadb.PersistentClient(path=str(args.src))

    # Discover source collections
    src_collection_names = {c.name for c in src_client.list_collections()}
    print(f"Collections found in source: {sorted(src_collection_names)}\n")

    if not args.dry_run:
        args.dest.mkdir(parents=True, exist_ok=True)

    dst_client = chromadb.PersistentClient(path=str(args.dest))

    grand_total = grand_copied = grand_skipped = 0
    t0 = time.perf_counter()

    for src_name, dst_name in COLLECTION_MAP.items():
        if src_name not in src_collection_names:
            print(f"  {src_name!r}  →  not found in source, skipping")
            continue

        renamed = f" → {dst_name!r}" if dst_name != src_name else ""
        print(f"  {src_name!r}{renamed}")

        src_coll = src_client.get_collection(src_name)
        if args.dry_run:
            dst_coll = None
        else:
            dst_coll = dst_client.get_or_create_collection(
                dst_name, metadata={"hnsw:space": "cosine"}
            )

        stats = _copy_collection(src_coll, dst_coll, force=args.force, dry_run=args.dry_run)

        print(f"    total={stats['total']}  "
              f"{'would copy' if args.dry_run else 'copied'}={stats['copied']}  "
              f"skipped={stats['skipped']}")

        grand_total   += stats["total"]
        grand_copied  += stats["copied"]
        grand_skipped += stats["skipped"]

    elapsed = time.perf_counter() - t0
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Total entries : {grand_total}")
    print(f"  Copied        : {grand_copied}")
    print(f"  Skipped       : {grand_skipped}")

    if args.dry_run:
        print("\nRe-run without --dry-run to perform the copy.")


if __name__ == "__main__":
    main()
