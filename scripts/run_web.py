"""
run_web.py

CLI launcher for the KnessetLM FastAPI web server.

Translates CLI arguments to environment variables and launches uvicorn.
The web app reads its configuration from web/settings.py, which reads
from the environment — so all CLI args flow through cleanly.

Usage
-----
    cd knesset-lm
    python scripts/run_web.py
    python scripts/run_web.py --machine machines/plan_execute_agent.json
    python scripts/run_web.py --port 5000 --top-k-browse 100
    python scripts/run_web.py --console-log      # also copy console output to Data/logs/console-<date>.log

Requests, visitor questions and server errors are logged under config.LOG_DIR by
api.request_log; uvicorn's own access log and proxy-header rewriting are off so the
peer stays the local cloudflared and CF-Connecting-IP gives the real client IP.

Prerequisites
-------------
  - Data/knesset.db built (scripts/build_knesset_db.py)
  - llama-server running for agent answers (not needed to start the server)
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure src/ is importable for the config import below
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config as _cfg


class Tee:
    """Writes to the real console stream and to a log file."""

    def __init__(self, console_stream, log_file):
        self.console_stream = console_stream
        self.log_file = log_file

    def write(self, text):
        self.console_stream.write(text)
        self.log_file.write(text)
        return len(text)

    def flush(self):
        self.console_stream.flush()
        self.log_file.flush()

    def __getattr__(self, name):
        return getattr(self.console_stream, name)


def tee_console_to_log_file() -> Path:
    _cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _cfg.LOG_DIR / f"console-{datetime.now(timezone.utc):%Y-%m-%d}.log"
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = Tee(sys.stdout, log_file)
    sys.stderr = Tee(sys.stderr, log_file)
    return log_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Launch the KnessetLM FastAPI web server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--machine",     type=Path, default=None,
                    help="Path to machine JSON (default: machines/knesset_agent.json)")
    ap.add_argument("--llama-server", default=None,
                    help=f"llama-server URL (default: {_cfg.LLAMA_SERVER})")
    ap.add_argument("--top-k-browse", dest="top_k_browse", type=int, default=None,
                    help=f"Meetings per reading-tab search (default: {_cfg.TOP_K_BROWSE})")
    ap.add_argument("--port",        type=int, default=5000,
                    help="HTTP port (default: 5000)")
    ap.add_argument("--host",        default="127.0.0.1",
                    help="Bind address (default: 127.0.0.1, reachable only through a local cloudflared; "
                         "0.0.0.0 exposes the server to the network)")
    ap.add_argument("--reload",      action="store_true",
                    help="Enable uvicorn hot-reload (development only)")
    ap.add_argument("--console-log", dest="console_log", action="store_true",
                    help="Also write console output to LOG_DIR/console-<UTC date>.log")
    args = ap.parse_args()

    if args.console_log:
        print(f"[run_web] Console output is also written to {tee_console_to_log_file()}", flush=True)

    # ── Translate args → environment variables ────────────────────────────────
    # uvicorn.run() runs in-process, so app.py reads os.environ directly.
    # Must mutate os.environ before the call — a copied dict has no effect.
    if args.machine:
        os.environ["KNESSET_MACHINE_PATH"] = str(args.machine.resolve())
    if args.llama_server:
        os.environ["KNESSET_LLAMA_SERVER"] = args.llama_server
    if args.top_k_browse is not None:
        os.environ["KNESSET_TOP_K_BROWSE"] = str(args.top_k_browse)
    os.environ["KNESSET_PORT"] = str(args.port)

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

    print(f"[run_web] Starting KnessetLM on http://{args.host}:{args.port}/", flush=True)

    uvicorn.run(
        "web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        env_file=None,
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
