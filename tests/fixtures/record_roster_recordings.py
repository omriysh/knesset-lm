"""
Record the real oknesset + OData responses behind find_mk / find_committee / find_party
into tests/fixtures/roster_recordings.json (replayed by tests/test_api.py).

Runs the handlers live against a temp knesset.db built from protocols_sample.json.
Knesset-wide lists (oknesset /members, KNS_PersonToPosition pages) are trimmed to the
persons of the sample's mks table and collapsed into one page, so the file stays small.

    python tests/fixtures/record_roster_recordings.py
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import requests

import config
import utils.knesset_db as kdb
import utils.tools as tools
from tests.conftest import SAMPLE, X_MK, build_sample_db

OUT = Path(__file__).parent / "roster_recordings.json"


def main() -> None:
    exchanges: list[dict] = []
    real_get = requests.get

    def recording_get(url, params=None, **kwargs):
        response = real_get(url, params=params, **kwargs)
        try:
            payload = response.json()
        except ValueError as exc:
            print(f"[record] non-json response from {url}: {exc}")
            payload = None
        exchanges.append({"url": url, "params": {k: str(v) for k, v in (params or {}).items()},
                          "status": response.status_code, "json": payload})
        return response

    requests.get = recording_get
    with tempfile.TemporaryDirectory() as tmp:
        config.KNESSET_DB = build_sample_db(Path(tmp) / "knesset.db")
        calls = {
            "find_mk":        (tools.handle_find_mk, {"query": X_MK["full_name"], "top_k": 1}),
            "find_committee": (tools.handle_find_committee, {"query": SAMPLE["committees"]["C1"], "top_k": 1}),
            "find_party":     (tools.handle_find_party, {"query": X_MK["party"], "top_k": 1}),
        }
        results = {}
        for name, (handler, args) in calls.items():
            env = handler(args)
            if env.error:
                raise SystemExit(f"{name} failed live: {env.error} {env.metadata}")
            results[name] = json.loads(env.full)
    requests.get = real_get

    sample_mk_ids = {str(m["mk_id"]) for m in SAMPLE["mks"]}
    for ex in exchanges:
        if ex["url"].endswith("/members") and isinstance(ex["json"], list):
            ex["json"] = [m for m in ex["json"] if str(m.get("mk_individual_id")) in sample_mk_ids]
    # mk_individual_id (knesset.db mk_id) differs from the OData PersonID for veteran MKs
    sample_person_ids = sample_mk_ids | {str(m.get("PersonID")) for ex in exchanges
                                         if ex["url"].endswith("/members") and isinstance(ex["json"], list)
                                         for m in ex["json"]}
    kept: list[dict] = []
    for ex in exchanges:
        is_page = ex["url"].endswith("/KNS_PersonToPosition") and "$skip" in ex["params"]
        if is_page and ex["params"]["$skip"] != "0":
            continue
        if is_page:
            rows = [e for e in exchanges if e["url"] == ex["url"] and e["params"].get("$filter") == ex["params"]["$filter"]]
            ex["json"] = {"value": [r for e in rows for r in (e["json"] or {}).get("value", [])
                                    if str(r.get("PersonID")) in sample_person_ids]}
        kept.append(ex)

    OUT.write_text(json.dumps({"recorded_at": datetime.now().isoformat(), "results": results,
                               "exchanges": kept}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[record] {len(kept)} exchanges → {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    kdb._fetch_members.cache_clear()
    main()
