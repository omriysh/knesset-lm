"""
index_meeting.py

Index a single meeting into ChromaDB: speeches, summary bullets, and
coherence-based dialog chunks (pass-1 fine + pass-2 coarse).

The summary file is inferred automatically from the meeting JSON path
(same stem, .txt extension, under Data/summaries/), or supplied explicitly.
If no summary exists, only speeches are indexed.

Usage
-----
    cd knesset-lm

    # Infer summary path automatically
    python scripts/index_meeting.py path/to/meeting.json

    # Point at a specific summary
    python scripts/index_meeting.py path/to/meeting.json \\
        --summary path/to/summary.txt

    # Custom DB path (override the default Data/chroma/)
    python scripts/index_meeting.py path/to/meeting.json \\
        --db ../Data/exp3_chroma

    # GPU options (defaults: no CUDA, no quantization)
    python scripts/index_meeting.py path/to/meeting.json \\
        --cuda --quantize int4

    # Force re-index even if already present
    python scripts/index_meeting.py path/to/meeting.json --force
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import chromadb

import config
from indexing.embedder import ProtocolEmbedder
from indexing.indexer import index_meeting


def _infer_summary_path(json_path: Path, knesset_num: int) -> Path:
    """
    Derive the summary .txt path from the meeting JSON path.
    Convention: Data/summaries/<knesset_num>/<committee>/<stem>.txt
    """
    committee_dir = json_path.parent.name
    return config.summaries_dir(knesset_num) / committee_dir / json_path.with_suffix(".txt").name


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json_path", type=Path, help="Meeting JSON file to index")
    ap.add_argument("--summary",  type=Path, default=None,
                    help="Summary .txt path (inferred automatically if omitted)")
    ap.add_argument("--knesset",  type=int, default=25,
                    help="Knesset number — used to locate the summary (default: 25)")
    ap.add_argument("--db",       type=Path, default=config.CHROMA_DIR,
                    help=f"ChromaDB directory (default: {config.CHROMA_DIR})")
    ap.add_argument("--embed-model", default=None,
                    help="Embedding model path (default: KNESSET_EMBED_MODEL env / config)")
    ap.add_argument("--cuda",     action="store_true", help="Use GPU for embedding")
    ap.add_argument("--quantize", default=None, choices=["int8", "int4"],
                    help="Quantize the embedding model")
    ap.add_argument("--force",    action="store_true",
                    help="Re-index even if already present")
    args = ap.parse_args()

    json_path = args.json_path.resolve()
    if not json_path.exists():
        print(f"ERROR: file not found: {json_path}")
        sys.exit(1)

    summary_path = args.summary
    if summary_path is None:
        summary_path = _infer_summary_path(json_path, args.knesset)

    has_summary = summary_path.exists()
    print(f"Meeting  : {json_path.name}")
    print(f"Summary  : {summary_path.name}  ({'found' if has_summary else 'not found — speeches only'})")
    print(f"DB       : {args.db}")
    print()

    args.db.mkdir(parents=True, exist_ok=True)
    chroma_client = chromadb.PersistentClient(path=str(args.db))

    embedder = ProtocolEmbedder(
        model_path=args.embed_model,
        use_cuda=args.cuda,
        quantize=args.quantize,
    )

    t0 = time.perf_counter()
    result = index_meeting(
        json_path,
        summary_path if has_summary else None,
        chroma_client,
        embedder,
        force=args.force,
    )
    elapsed = time.perf_counter() - t0

    if result.status == "skip":
        print(f"Skipped: {result.reason}")
        sys.exit(0)

    if result.status == "error":
        print(f"ERROR: {result.reason}")
        for e in result.errors:
            print(f"  {e}")
        sys.exit(1)

    print(f"Done in {elapsed:.1f}s")
    print(f"  speeches indexed : {result.speeches}")
    if has_summary:
        print(f"  bullets  indexed : {result.bullets}")
        print(f"  pass-1 chunks    : {result.pass1}")
        print(f"  pass-2 chunks    : {result.pass2}")


if __name__ == "__main__":
    main()
