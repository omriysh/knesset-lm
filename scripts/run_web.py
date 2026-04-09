"""
run_web.py

CLI launcher for the KnessetLM FastAPI web server.

Translates CLI arguments to environment variables and launches uvicorn.
The web app reads its configuration from web/settings.py, which reads
from the environment — so all CLI args flow through cleanly.

Usage
-----
    cd knesset-lm
    python scripts/run_web.py --cuda --quantize int4
    python scripts/run_web.py --cuda --quantize int4 --machine machines/knesset_agent.json
    python scripts/run_web.py --port 5000 --top-k 7 --top-n 20
    python scripts/run_web.py --db ../Data/exp3_chroma --cuda --quantize int4

Prerequisites
-------------
  - llama-server running (see CLAUDE.md for the command)
  - ChromaDB indexes built (see scripts/process_knesset.py)
"""

import argparse
import os
import sys
from pathlib import Path

# Ensure src/ is importable for the config import below
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config as _cfg


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Launch the KnessetLM FastAPI web server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--cuda",       action="store_true",
                    help="Use GPU for embedding")
    ap.add_argument("--quantize",   default=None, choices=["int8", "int4"],
                    help="Quantize embedding model (int4 recommended when llama-server is running)")
    ap.add_argument("--embed-model", default=None,
                    help=f"Embedding model path (default: {_cfg.EMBED_MODEL_PATH})")
    ap.add_argument("--db",          type=Path, default=None,
                    help=f"ChromaDB directory (default: {_cfg.CHROMA_DIR})")
    ap.add_argument("--machine",     type=Path, default=None,
                    help="Path to machine JSON (default: machines/knesset_agent.json)")
    ap.add_argument("--llama-server", default=None,
                    help=f"llama-server URL (default: {_cfg.LLAMA_SERVER})")
    ap.add_argument("--top-k",       dest="top_k", type=int, default=None,
                    help=f"Meetings to retrieve via L1 (default: {_cfg.TOP_K_MEETINGS})")
    ap.add_argument("--top-n",       dest="top_n", type=int, default=None,
                    help=f"Pass-2 chunks to rank (default: {_cfg.TOP_N_DIALOGS})")
    ap.add_argument("--port",        type=int, default=5000,
                    help="HTTP port (default: 5000)")
    ap.add_argument("--reload",      action="store_true",
                    help="Enable uvicorn hot-reload (development only)")
    args = ap.parse_args()

    # ── Translate args → environment variables ────────────────────────────────
    env = os.environ.copy()

    if args.cuda:
        env["KNESSET_CUDA"] = "1"
    if args.quantize:
        env["KNESSET_QUANTIZE"] = args.quantize
    if args.embed_model:
        env["KNESSET_EMBED_MODEL"] = args.embed_model
    if args.db:
        env["KNESSET_CHROMA_DIR"] = str(args.db.resolve())
    if args.machine:
        env["KNESSET_MACHINE_PATH"] = str(args.machine.resolve())
    if args.llama_server:
        env["KNESSET_LLAMA_SERVER"] = args.llama_server
    if args.top_k is not None:
        env["KNESSET_TOP_K"] = str(args.top_k)
    if args.top_n is not None:
        env["KNESSET_TOP_N"] = str(args.top_n)
    env["KNESSET_PORT"] = str(args.port)

    # ── Launch uvicorn ────────────────────────────────────────────────────────
    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn is not installed. Run: pip install uvicorn[standard]")
        sys.exit(1)

    # Ensure uvicorn can find `web.app`.  When running a script file, Python
    # sets sys.path[0] to the script directory — CWD is NOT added automatically,
    # so chdir alone is not enough.  Insert knesset-lm root explicitly.
    knesset_lm_root = Path(__file__).parent.parent.resolve()
    knesset_lm_root_str = str(knesset_lm_root)
    if knesset_lm_root_str not in sys.path:
        sys.path.insert(0, knesset_lm_root_str)

    print(f"[run_web] Starting KnessetLM on http://0.0.0.0:{args.port}/", flush=True)

    uvicorn.run(
        "web.app:app",
        host="0.0.0.0",
        port=args.port,
        reload=args.reload,
        env_file=None,
    )


if __name__ == "__main__":
    main()
