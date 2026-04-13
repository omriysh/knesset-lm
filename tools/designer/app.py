"""
Agent State-Machine Designer — Flask backend.

Usage
-----
    cd knesset-lm
    pip install flask
    python tools/designer/app.py [--port PORT]

Opens a visual graph editor at http://localhost:5001/
Machines are saved to knesset-lm/machines/.
"""

import argparse
import json
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__, template_folder="templates", static_folder="static")

# Machines live in the repository's machines/ directory
MACHINES_DIR = Path(__file__).parent.parent.parent / "machines"
MACHINES_DIR.mkdir(exist_ok=True)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _new_machine(name: str = "New Machine") -> dict:
    """Return a blank v2 state-machine with only the Begin node."""
    begin_id = "begin_" + uuid.uuid4().hex[:8]
    return {
        "id":           uuid.uuid4().hex[:12],
        "name":         name,
        "version":      2,
        "global_rules": "",
        "nodes": [
            {
                "id":       begin_id,
                "type":     "begin",
                "label":    "Begin",
                "position": {"x": 400, "y": 80},
                "data":     {},
            }
        ],
        "edges": [],
    }


def _machine_path(machine_id: str) -> Path:
    # Sanitise: only alphanumeric + underscore + hyphen
    safe = "".join(c for c in machine_id if c.isalnum() or c in "_-")
    return MACHINES_DIR / f"{safe}.json"


def _find_machine_file(machine_id: str) -> Path | None:
    """Find a machine JSON file by its internal 'id' field (not by filename)."""
    for p in MACHINES_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text("utf-8"))
            if data.get("id") == machine_id:
                return p
        except Exception:
            continue
    return None


# ── API routes ───────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/machines")
def list_machines():
    """List saved machines (id + name)."""
    results = []
    for p in sorted(MACHINES_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text("utf-8"))
            results.append({"id": data["id"], "name": data.get("name", p.stem)})
        except Exception:
            continue
    return jsonify(results)


@app.post("/api/machines")
def create_machine():
    """Create a new blank machine and return it."""
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "New Machine").strip()
    machine = _new_machine(name)
    _machine_path(machine["id"]).write_text(
        json.dumps(machine, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify(machine), 201


@app.get("/api/machines/<machine_id>")
def get_machine(machine_id: str):
    p = _find_machine_file(machine_id)
    if p is None:
        return {"error": "not found"}, 404
    return jsonify(json.loads(p.read_text("utf-8")))


@app.put("/api/machines/<machine_id>")
def save_machine(machine_id: str):
    """Overwrite a machine's full JSON."""
    body = request.get_json(force=True)
    if not body:
        return {"error": "empty body"}, 400
    body["id"] = machine_id
    # Write to the existing file if found, otherwise fall back to id-as-filename
    p = _find_machine_file(machine_id) or _machine_path(machine_id)
    p.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


@app.delete("/api/machines/<machine_id>")
def delete_machine(machine_id: str):
    p = _find_machine_file(machine_id)
    if p is not None:
        p.unlink()
    return jsonify({"ok": True})


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Agent State-Machine Designer")
    ap.add_argument("--port", type=int, default=5001, help="HTTP port (default 5001)")
    args = ap.parse_args()

    print(f"[agent-designer] Open  http://localhost:{args.port}/  in your browser\n", flush=True)
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
