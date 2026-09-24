"""
settings.py

Runtime configuration for the web app, loaded from environment variables.
All settings have sensible defaults so the app works out-of-the-box.

Override any value by setting the corresponding environment variable before
launching the server (or via a .env file if you add python-dotenv support).
"""

from __future__ import annotations

import os
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config as _cfg

# ── LLM server ────────────────────────────────────────────────────────────────

LLAMA_SERVER: str = os.environ.get("KNESSET_LLAMA_SERVER", _cfg.LLAMA_SERVER)

# ── State machine ─────────────────────────────────────────────────────────────

_MACHINES_DIR = Path(__file__).parent.parent / "machines"
MACHINE_PATH: Path = Path(
    os.environ.get(
        "KNESSET_MACHINE_PATH",
        str(_MACHINES_DIR / "knesset_agent.json"),
    )
)

# ── Data paths ────────────────────────────────────────────────────────────────

TRANSCRIPTIONS_ROOT: Path = Path(
    os.environ.get("KNESSET_TRANSCRIPTIONS_DIR", str(_cfg.DATA_DIR / "raw_transcriptions"))
)

# ── Reading-tab browser ───────────────────────────────────────────────────────

TOP_K_BROWSE: int = int(os.environ.get("KNESSET_TOP_K_BROWSE", str(_cfg.TOP_K_BROWSE)))

# ── Sessions ─────────────────────────────────────────────────────────────────

SESSIONS_DIR: Path = Path(
    os.environ.get(
        "KNESSET_SESSIONS_DIR",
        str(Path(__file__).parent.parent / "sessions"),
    )
)

# ── Server ────────────────────────────────────────────────────────────────────

PORT: int = int(os.environ.get("KNESSET_PORT", "5000"))
