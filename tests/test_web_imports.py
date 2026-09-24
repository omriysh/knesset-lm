"""
tests/test_web_imports.py

The web server must import without pulling in the embedding / vector-store
stack. Runs in a subprocess so modules already imported by other tests do not
leak into the check.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent

_PROBE = """
import json, sys
sys.path.insert(0, {repo!r})
sys.path.insert(0, {src!r})
import web.app
heavy = ["torch", "chromadb", "transformers", "indexing.embedder",
         "retrieval.protocol_rag", "retrieval.deep_dive"]
print(json.dumps(sorted(m for m in heavy if m in sys.modules)))
"""


def test_import_web_app_is_lightweight():
    code = _PROBE.format(repo=str(REPO), src=str(REPO / "src"))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(REPO), env=env, timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])
    assert loaded == []
