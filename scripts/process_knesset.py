"""
process_knesset.py

Full offline processing pipeline for a Knesset number:
  1. For each committee, download all session protocols and summarize them.
  2. Immediately after, index all meetings that have both a protocol and a summary.

Run with llama-server RUNNING (needed for summarization) and the embedding
model NOT yet loaded (VRAM will be shared when indexing starts).

Usage
-----
    cd knesset-lm
    python scripts/process_knesset.py --knesset 25 --cuda --quantize int4
    python scripts/process_knesset.py --knesset 25 --cuda --quantize int4 \\
        --db ../Data/exp3_chroma
    python scripts/process_knesset.py --knesset 25 --cuda --quantize int4 \\
        --force-summarize --force-index
    python scripts/process_knesset.py --knesset 25 --cuda --quantize int4 \\
        --skip "ועדת הכנסת"

Notes
-----
- The embedder is loaded lazily on first use and kept in memory for the entire
  run — no repeated model loads.
- Meetings without a summary (too long, no transcript) are silently skipped
  during indexing.
- Interrupt safely: all progress is persisted to disk (ChromaDB + .txt files).
  Re-running picks up where it left off unless --force-* flags are used.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import chromadb

import config
from utils.knesset_db import (
    get_all_committees,
    get_committee_sessions,
    get_session_transcript,
    SESSION_TYPE_CLASSIFIED,
)
from summarization.pipeline import summarize_meeting, save_summary
from indexing.embedder import KnessetEmbedder
from indexing.indexer import index_meeting, IndexResult

_WIN_UNSAFE = re.compile(r'[\\/:*?"<>|]')
_CANCELLED_STATUS_IDS = {193}


def _safe_dirname(name: str) -> str:
    return re.sub(r"[\s_]+", "_", _WIN_UNSAFE.sub("_", name)).strip("_")


def _session_filename(date_iso: str, session_id: int) -> str:
    if date_iso and len(date_iso) >= 10:
        y, m, d = date_iso[:10].split("-")
        return f"{d}_{m}_{y}_{session_id}"
    return f"00_00_0000_{session_id}"


def _infer_summary_path(json_path: Path, knesset_num: int) -> Path:
    committee_dir = json_path.parent.name
    return config.summaries_dir(knesset_num) / committee_dir / json_path.with_suffix(".txt").name


def _summarize_phase(
    committee: dict,
    knesset_num: int,
    force_summarize: bool,
) -> tuple[dict, list[Path]]:
    """
    Download + summarize all sessions for one committee.

    Returns (stats, summarized_paths) where summarized_paths lists all meetings
    that have both a protocol and a summary (ready for indexing).
    """
    name         = committee["Name"]
    committee_id = committee["CommitteeID"]
    dirname      = _safe_dirname(name)
    proto_dir    = config.transcriptions_dir(knesset_num) / dirname
    summ_dir     = config.summaries_dir(knesset_num)      / dirname

    stats = {
        "total":         0,
        "classified":    0, "cancelled":     0,
        "skipped_dl":    0, "downloaded_ok": 0, "no_transcript": 0,
        "skipped_summ":  0, "summarized":    0, "too_long":      0, "failed_summ": 0,
    }

    sessions = get_committee_sessions(committee_id, knesset_num)
    stats["total"] = len(sessions)
    if not sessions:
        return stats, []

    proto_dir.mkdir(parents=True, exist_ok=True)
    summ_dir.mkdir(parents=True, exist_ok=True)

    summarized_paths: list[Path] = []

    with tqdm(
        total    = len(sessions),
        desc     = "  Sessions",
        unit     = "sess",
        leave    = False,
        position = 1,
        dynamic_ncols = True,
    ) as sbar:
        for session in sessions:
            session_id = session["session_id"]
            date_iso   = session["date"]
            type_id    = session.get("type_id")
            status_id  = session.get("status_id")
            stem       = _session_filename(date_iso, session_id)
            proto_path = proto_dir / f"{stem}.json"
            summ_path  = summ_dir  / f"{stem}.txt"

            if type_id == SESSION_TYPE_CLASSIFIED:
                stats["classified"] += 1
                sbar.update(1)
                continue
            if status_id in _CANCELLED_STATUS_IDS:
                stats["cancelled"] += 1
                sbar.update(1)
                continue

            # Protocol
            if proto_path.exists():
                stats["skipped_dl"] += 1
            else:
                sbar.set_description("  Fetching")
                transcript = get_session_transcript(session_id)
                if transcript:
                    payload = {
                        "meeting_id":  str(session_id),
                        "date":        date_iso,
                        "committee":   name,
                        "knesset_num": knesset_num,
                        **transcript,
                    }
                    proto_path.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    stats["downloaded_ok"] += 1
                else:
                    stats["no_transcript"] += 1
                    sbar.update(1)
                    continue

            # Summary
            if summ_path.exists() and not force_summarize:
                stats["skipped_summ"] += 1
            else:
                sbar.set_description("  Summarizing")
                # summarize_meeting() prints its own progress; tqdm redraws bars after each line.
                summary = summarize_meeting(proto_path)
                if summary is None:
                    stats["too_long"] += 1
                    tqdm.write(f"  [skip-long] {proto_path.name}")
                elif summary:
                    save_summary(summary, proto_path, knesset_num)
                    stats["summarized"] += 1
                else:
                    stats["failed_summ"] += 1
                    tqdm.write(f"  [ERROR] summarization failed: {proto_path.name}")

            if proto_path.exists() and summ_path.exists():
                summarized_paths.append(proto_path)

            sbar.set_description("  Sessions")
            sbar.update(1)
            sbar.set_postfix(
                dl   = stats["downloaded_ok"],
                summ = stats["summarized"],
                skip = stats["skipped_summ"],
                refresh = False,
            )

    return stats, summarized_paths


def _index_phase(
    summarized_paths: list[Path],
    knesset_num: int,
    chroma_client: chromadb.ClientAPI,
    embedder_factory,
    force_index: bool,
) -> dict:
    """
    Index all meetings in summarized_paths into ChromaDB.
    Returns stats dict with indexed/index_skip/index_err counts.
    """
    stats = {"indexed": 0, "index_skip": 0, "index_err": 0}

    if not summarized_paths:
        return stats

    embedder = embedder_factory()

    with tqdm(
        total    = len(summarized_paths),
        desc     = "  Indexing",
        unit     = "meet",
        leave    = False,
        position = 1,
        dynamic_ncols = True,
    ) as ibar:
        for proto_path in summarized_paths:
            summ_path = _infer_summary_path(proto_path, knesset_num)
            ibar.set_postfix_str(proto_path.stem[-20:], refresh=False)
            t0 = time.perf_counter()
            try:
                result: IndexResult = index_meeting(
                    proto_path, summ_path, chroma_client, embedder,
                    force=force_index,
                )
                elapsed = time.perf_counter() - t0
                if result.status == "skip":
                    stats["index_skip"] += 1
                else:
                    stats["indexed"] += 1
                    ibar.set_postfix(
                        sp = result.speeches, bl = result.bullets,
                        p2 = result.pass2,    s  = f"{elapsed:.0f}s",
                        refresh = False,
                    )
            except Exception as exc:
                stats["index_err"] += 1
                tqdm.write(f"  [INDEX ERROR] {proto_path.name}: {exc}")
            ibar.update(1)

    return stats


def _process_committee(
    committee: dict,
    knesset_num: int,
    chroma_client: chromadb.ClientAPI,
    embedder_factory,
    force_summarize: bool,
    force_index: bool,
) -> dict:
    """Run summarize + index phases for one committee. Returns combined stats."""
    summ_stats, summarized_paths = _summarize_phase(
        committee, knesset_num, force_summarize
    )
    idx_stats = _index_phase(
        summarized_paths, knesset_num, chroma_client, embedder_factory, force_index
    )
    return {**summ_stats, **idx_stats}


def _format_committee_summary(name: str, stats: dict) -> str:
    """Compact one-line summary printed after each committee finishes."""
    parts = []
    if stats["summarized"]:
        parts.append(f"+{stats['summarized']} summ")
    if stats["downloaded_ok"]:
        parts.append(f"+{stats['downloaded_ok']} dl")
    if stats["indexed"]:
        parts.append(f"+{stats['indexed']} indexed")
    if stats["skipped_summ"] or stats["index_skip"]:
        parts.append(f"{stats['skipped_summ']}+{stats['index_skip']} cached")
    flags = []
    if stats["too_long"]:
        flags.append(f"{stats['too_long']} long")
    if stats["no_transcript"]:
        flags.append(f"{stats['no_transcript']} no-tr")
    if stats["failed_summ"]:
        flags.append(f"{stats['failed_summ']} summ-err")
    if stats["index_err"]:
        flags.append(f"{stats['index_err']} idx-err")
    if flags:
        parts.append("(" + " ".join(flags) + ")")
    body = ", ".join(parts) if parts else "nothing new"
    return f"  {name[:40]}: {body}"


def _print_stats(stats: dict) -> None:
    print(f"  Sessions          : {stats['total']}")
    if stats["classified"]:
        print(f"  Classified        : {stats['classified']}")
    if stats["cancelled"]:
        print(f"  Cancelled         : {stats['cancelled']}")
    print(f"  Protocol cached   : {stats['skipped_dl']}")
    print(f"  Downloaded        : {stats['downloaded_ok']}")
    if stats["no_transcript"]:
        print(f"  No transcript     : {stats['no_transcript']}")
    print(f"  Summary cached    : {stats['skipped_summ']}")
    print(f"  Summarized        : {stats['summarized']}")
    if stats["too_long"]:
        print(f"  Too long          : {stats['too_long']}")
    if stats["failed_summ"]:
        print(f"  Summary failed    : {stats['failed_summ']}")
    print(f"  Indexed           : {stats['indexed']}")
    if stats["index_skip"]:
        print(f"  Index skipped     : {stats['index_skip']}")
    if stats["index_err"]:
        print(f"  Index errors      : {stats['index_err']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--knesset", type=int, default=25,
                    help="Knesset number (default: 25)")
    ap.add_argument("--db", type=Path, default=config.CHROMA_DIR,
                    help=f"ChromaDB directory (default: {config.CHROMA_DIR})")
    ap.add_argument("--embed-model", default=None,
                    help="Embedding model path (default: KNESSET_EMBED_MODEL env / config)")
    ap.add_argument("--cuda",     action="store_true", help="Use GPU for embedding")
    ap.add_argument("--quantize", default=None, choices=["int8", "int4"])
    ap.add_argument("--force-summarize", action="store_true",
                    help="Re-summarize even if a summary already exists")
    ap.add_argument("--force-index", action="store_true",
                    help="Re-index even if already present in ChromaDB")
    ap.add_argument("--skip", nargs="*", default=[],
                    help="Committee name patterns to skip (substring match)")
    args = ap.parse_args()

    tqdm.write(f"Fetching committee list for Knesset {args.knesset} …")
    committees = get_all_committees(args.knesset)
    if not committees:
        tqdm.write("No committees found.")
        sys.exit(1)
    tqdm.write(f"Found {len(committees)} committees.")

    skip_patterns = [s.strip() for s in args.skip if s.strip()]
    if skip_patterns:
        before = len(committees)
        committees = [c for c in committees if not any(p in c["Name"] for p in skip_patterns)]
        tqdm.write(f"Skipping {before - len(committees)} committee(s) by name pattern.")

    args.db.mkdir(parents=True, exist_ok=True)
    chroma_client = chromadb.PersistentClient(path=str(args.db))
    tqdm.write(f"DB: {args.db}\n")

    _embedder: list[Optional[KnessetEmbedder]] = [None]

    def embedder_factory() -> KnessetEmbedder:
        if _embedder[0] is None:
            _embedder[0] = KnessetEmbedder(
                model_path=args.embed_model,
                use_cuda=args.cuda,
                quantize=args.quantize,
            )
        return _embedder[0]

    grand: dict[str, int] = {k: 0 for k in (
        "total", "classified", "cancelled",
        "skipped_dl", "downloaded_ok", "no_transcript",
        "skipped_summ", "summarized", "too_long", "failed_summ",
        "indexed", "index_skip", "index_err",
    )}

    t_run = time.perf_counter()

    with tqdm(
        total    = len(committees),
        desc     = "Committees",
        unit     = "committee",
        position = 0,
        dynamic_ncols = True,
    ) as cbar:
        for committee in committees:
            name = committee["Name"]
            cbar.set_postfix_str(name[:30], refresh=True)

            stats = _process_committee(
                committee, args.knesset, chroma_client, embedder_factory,
                args.force_summarize, args.force_index,
            )
            for k in grand:
                grand[k] += stats.get(k, 0)

            tqdm.write(_format_committee_summary(name, stats))
            cbar.update(1)
            cbar.set_postfix(
                summ    = grand["summarized"],
                indexed = grand["indexed"],
                err     = grand["failed_summ"] + grand["index_err"],
                refresh = False,
            )

    elapsed = time.perf_counter() - t_run
    print(f"\n{'='*60}")
    print(f"GRAND TOTAL — Knesset {args.knesset}  "
          f"({len(committees)} committees, {elapsed / 60:.1f} min)")
    print(f"{'='*60}")
    _print_stats(grand)


if __name__ == "__main__":
    main()
