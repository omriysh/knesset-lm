"""
index_committee.py

Index all meetings for a Knesset committee into ChromaDB.

For each meeting JSON found in Data/raw_transcriptions/<knesset>/<committee>/:
  - Infers the matching summary .txt from Data/summaries/<knesset>/<committee>/
  - Calls index_meeting() (speeches + bullets + pass-1 + pass-2)
  - Skips already-indexed meetings unless --force is given

Usage
-----
    cd knesset-lm

    # Index committee (skip already-indexed)
    python scripts/index_committee.py "ועדת הכספים"

    # Force re-index everything
    python scripts/index_committee.py "ועדת הכספים" --force

    # Different knesset / DB / GPU options
    python scripts/index_committee.py "ועדת הכספים" --knesset 25 \\
        --db ../Data/chroma --cuda --quantize int4
"""

import argparse
import re
import sys
import time
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import chromadb

import config
from indexing.embedder import ProtocolEmbedder
from indexing.indexer import index_meeting

_WIN_UNSAFE = re.compile(r'[\\/:*?"<>|]')


def _safe_dirname(name: str) -> str:
    return re.sub(r'[\s_]+', '_', _WIN_UNSAFE.sub("_", name)).strip('_')


def _find_committee_dir(base: Path, name: str) -> Path | None:
    """Exact match first, then case-insensitive, then substring."""
    safe = _safe_dirname(name)
    exact = base / safe
    if exact.is_dir():
        return exact
    low = safe.lower()
    for d in base.iterdir():
        if d.is_dir() and d.name.lower() == low:
            return d
    for d in base.iterdir():
        if d.is_dir() and low in d.name.lower():
            return d
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("committee",
                    help="Committee name (Hebrew). Matched against directory names.")
    ap.add_argument("--knesset",     type=int, default=25,
                    help="Knesset number (default: 25)")
    ap.add_argument("--db",          type=Path, default=config.CHROMA_DIR,
                    help=f"ChromaDB directory (default: {config.CHROMA_DIR})")
    ap.add_argument("--embed-model", default=None,
                    help="Embedding model path (overrides config / KNESSET_EMBED_MODEL env)")
    ap.add_argument("--cuda",        action="store_true",
                    help="Use GPU for embedding")
    ap.add_argument("--quantize",    default=None, choices=["int8", "int4"],
                    help="Quantize the embedding model")
    ap.add_argument("--force",       action="store_true",
                    help="Re-index even if already present in ChromaDB")
    args = ap.parse_args()

    proto_base = config.transcriptions_dir(args.knesset)
    summ_base  = config.summaries_dir(args.knesset)

    proto_dir = _find_committee_dir(proto_base, args.committee)
    if proto_dir is None:
        print(f"ERROR: no transcription directory found for {args.committee!r}")
        print(f"       looked in: {proto_base}")
        available = sorted(d.name for d in proto_base.iterdir() if d.is_dir())
        if available:
            print("Available committees:")
            for d in available:
                print(f"  {d}")
        sys.exit(1)

    summ_dir = _find_committee_dir(summ_base, args.committee) or (summ_base / proto_dir.name)

    json_files = sorted(proto_dir.glob("*.json"))
    if not json_files:
        print(f"No JSON files found in {proto_dir}")
        sys.exit(0)

    print(f"Committee : {proto_dir.name}")
    print(f"Meetings  : {len(json_files)}")
    print(f"Summaries : {summ_dir}  ({'found' if summ_dir.is_dir() else 'not found'})")
    print(f"DB        : {args.db}")
    print(f"Force     : {args.force}")
    print()

    args.db.mkdir(parents=True, exist_ok=True)
    chroma_client = chromadb.PersistentClient(path=str(args.db))

    embedder = ProtocolEmbedder(
        model_path=args.embed_model,
        use_cuda=args.cuda,
        quantize=args.quantize,
    )

    stats = {
        "ok": 0, "skip": 0, "error": 0,
        "speeches": 0, "bullets": 0, "pass1": 0, "pass2": 0,
    }
    t_total = time.perf_counter()

    with tqdm(total=len(json_files), unit="meeting", dynamic_ncols=True) as bar:
        for json_path in json_files:
            bar.set_description(json_path.stem[-30:])

            summ_path = summ_dir / json_path.with_suffix(".txt").name
            has_summary = summ_path.exists()

            t0 = time.perf_counter()
            result = index_meeting(
                json_path,
                summ_path if has_summary else None,
                chroma_client,
                embedder,
                force=args.force,
            )
            elapsed = time.perf_counter() - t0

            if result.status == "skip":
                tqdm.write(f"  skip  {json_path.name}  ({result.reason})")
                stats["skip"] += 1
            elif result.status == "error":
                tqdm.write(f"  ERROR {json_path.name}  {result.reason}")
                for e in result.errors:
                    tqdm.write(f"        {e}")
                stats["error"] += 1
            else:
                summary_note = (
                    f"  b={result.bullets} p1={result.pass1} p2={result.pass2}"
                    if has_summary else "  (no summary)"
                )
                tqdm.write(
                    f"  ok    {json_path.name}  sp={result.speeches}"
                    f"{summary_note}  {elapsed:.1f}s"
                )
                stats["ok"]       += 1
                stats["speeches"] += result.speeches
                stats["bullets"]  += result.bullets
                stats["pass1"]    += result.pass1
                stats["pass2"]    += result.pass2

            bar.update(1)
            bar.set_postfix(ok=stats["ok"], skip=stats["skip"], err=stats["error"],
                            refresh=False)

    total_s = time.perf_counter() - t_total
    print(f"\n{'='*55}")
    print(f"Done — {proto_dir.name}  ({total_s:.0f}s total)")
    print(f"  Indexed OK   : {stats['ok']}")
    if stats["skip"]:
        print(f"  Skipped      : {stats['skip']}")
    if stats["error"]:
        print(f"  Errors       : {stats['error']}")
    if stats["ok"]:
        print(f"  Speeches     : {stats['speeches']}")
        print(f"  Bullets      : {stats['bullets']}")
        print(f"  Pass-1 chunks: {stats['pass1']}")
        print(f"  Pass-2 chunks: {stats['pass2']}")


if __name__ == "__main__":
    main()
