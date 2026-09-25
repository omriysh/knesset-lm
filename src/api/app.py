"""
Standalone public API server (no sessions, no LLM): uvicorn api.app:app
Run from src/ or with src/ on sys.path.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from api.validation import install_error_handlers

app = FastAPI(title="KnessetLM public API",
              description="Read-only access to Knesset committee protocols, MKs, bills and votes. "
                          "Start with /agent-instructions.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
app.include_router(router)
install_error_handlers(app)
