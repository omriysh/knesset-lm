"""
conftest.py

Shared pytest fixtures. Tests run against the real Data/knesset.db (read-only) and, for the
tests marked `network`, the live oknesset / Knesset OData APIs through the production disk
cache. Facts the tests assert on (meeting ids, committees, an MK) are discovered with small
SQL queries at session start, never hardcoded from a snapshot.

    python -m pytest tests -q                 # everything; network tests skip when offline
    python -m pytest tests -q -m "not network"
    KNESSETLM_OFFLINE=1 python -m pytest ...  # force the network tests to skip
"""

import json
import os
import socket
import sqlite3
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config  # noqa: E402

UPSTREAM_HOSTS = ("knesset.gov.il", "backend.oknesset.org")


def pytest_configure(config):  # noqa: F811 (pytest passes its Config by this parameter name)
    config.addinivalue_line(
        "markers", "network: hits the live Knesset OData / oknesset APIs; skipped when offline")
    config.addinivalue_line(
        "markers", "word: drives a real Microsoft Word through pywin32; skipped when Word is unavailable")


@lru_cache(maxsize=1)
def upstream_unreachable_reason() -> str | None:
    if os.environ.get("KNESSETLM_OFFLINE"):
        return "KNESSETLM_OFFLINE is set"
    for host in UPSTREAM_HOSTS:
        try:
            socket.create_connection((host, 443), timeout=4).close()
        except OSError as exc:
            print(f"[conftest] {host}:443 unreachable: {exc}")
            return f"offline: cannot reach {host} ({exc})"
    return None


def pytest_runtest_setup(item):
    if item.get_closest_marker("network") is not None:
        reason = upstream_unreachable_reason()
        if reason:
            pytest.skip(f"network test skipped, {reason}")


# ── Machine JSON factories ────────────────────────────────────────────────────

def _minimal_machine(extra_nodes=None, extra_edges=None) -> dict:
    """Return the smallest valid v2 machine: begin → one LLM node."""
    nodes = [
        {"id": "begin_001", "type": "begin", "label": "Begin",
         "position": {"x": 0, "y": 0}, "data": {}},
        {"id": "llm_001",   "type": "llm_call", "label": "Router",
         "position": {"x": 200, "y": 0},
         "data": {"system_prompt": "You are helpful.", "stage": "router",
                  "temperature": 0.7, "max_tokens": 512}},
    ]
    edges = [
        {"id": "e_001", "source": "begin_001", "target": "llm_001",
         "type": "transition", "label": ""},
    ]
    nodes.extend(extra_nodes or [])
    edges.extend(extra_edges or [])
    return {"version": 2, "id": "test_machine", "name": "Test", "nodes": nodes, "edges": edges}


@pytest.fixture()
def minimal_machine_path(tmp_path):
    p = tmp_path / "machine.json"
    p.write_text(json.dumps(_minimal_machine()), encoding="utf-8")
    return p


@pytest.fixture()
def machine_with_tool_path(tmp_path):
    nodes = [
        {"id": "tool_001", "type": "tool", "label": "get_profile",
         "position": {"x": 200, "y": 100},
         "data": {
             "function_name": "find_mk",
             "description": "Find MK",
             "parameters": {"type": "object", "properties": {
                 "name": {"type": "string"}
             }},
         }},
    ]
    edges = [
        {"id": "e_tool", "source": "llm_001", "target": "tool_001",
         "type": "tool_link", "label": ""},
    ]
    data = _minimal_machine(extra_nodes=nodes, extra_edges=edges)
    p = tmp_path / "machine_tool.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ── The real knesset.db ───────────────────────────────────────────────────────

REAL_DB_PATH = Path(config.KNESSET_DB)


def open_real_db_readonly() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{REAL_DB_PATH.as_posix()}?mode=ro", uri=True, timeout=30,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class RealDbFacts:
    """Stable facts of the real knesset.db, discovered by SQL (deterministic ORDER BY)."""
    db_path: Path
    meeting_id: str
    meeting_committee: str
    meeting_date: str
    meeting_first_topic: str
    other_meeting_id: str
    other_committee: str
    other_committee_id: str
    non_protocol_meeting_id: str
    mk_id: str
    mk_name: str
    mk_party: str
    topic_word: str
    table_counts: dict = field(default_factory=dict)


def _one(conn, sql: str, *params):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise LookupError(f"no row for: {sql.split()[0:12]}")
    return row


def discover_real_facts(conn: sqlite3.Connection) -> RealDbFacts:
    """A rich K25 protocol meeting (topics, verified opinions, a long transcript, MK and guest
    attendance), a meeting of another committee, a non-protocol meeting and the MK with the most
    verified opinions."""
    rich_meeting_sql = """
        SELECT m.meeting_id, m.committee, m.date FROM meetings m
        WHERE m.knesset_num = 25 AND m.is_protocol = 1 AND m.format = 'structured'
          AND (SELECT COUNT(*) FROM topics t WHERE t.meeting_id = m.meeting_id) >= 5
          AND (SELECT COUNT(*) FROM opinions o WHERE o.meeting_id = m.meeting_id AND o.quote_verified = 1
               AND o.mk_id IS NOT NULL) >= 5
          AND (SELECT COUNT(*) FROM speeches s WHERE s.meeting_id = m.meeting_id) >= 60
          AND (SELECT COUNT(*) FROM attendance a WHERE a.meeting_id = m.meeting_id AND a.mk_id IS NOT NULL) >= 3
          AND (SELECT COUNT(*) FROM attendance a WHERE a.meeting_id = m.meeting_id AND a.mk_id IS NULL) >= 1
          {extra}
        ORDER BY m.meeting_id LIMIT 1"""
    meeting = _one(conn, rich_meeting_sql.format(extra=""))
    other = _one(conn, rich_meeting_sql.format(extra="AND m.committee <> ?"), meeting["committee"])
    other_committee_id = _one(conn, "SELECT committee_id FROM committees WHERE name = ? AND knesset_num = 25",
                              other["committee"])[0]
    non_protocol = _one(conn, "SELECT m.meeting_id FROM meetings m WHERE m.is_protocol = 0 AND EXISTS "
                              "(SELECT 1 FROM speeches s WHERE s.meeting_id = m.meeting_id) ORDER BY m.meeting_id")
    mk = _one(conn, """
        SELECT k.mk_id, k.full_name, k.party FROM opinions o JOIN mks k ON k.mk_id = o.mk_id
        WHERE o.quote_verified = 1 AND k.party IS NOT NULL AND o.knesset_num = 25
        GROUP BY k.mk_id ORDER BY COUNT(*) DESC, k.mk_id LIMIT 1""")
    first_topic = _one(conn, "SELECT text FROM topics WHERE meeting_id = ? ORDER BY idx", meeting["meeting_id"])[0]
    topic_word = max((w for w in first_topic.split() if w.isalpha() and len(w) >= 4), key=len)
    counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("mks", "committees", "meetings", "attendance", "topics", "opinions", "speeches")}
    return RealDbFacts(
        db_path=REAL_DB_PATH,
        meeting_id=meeting["meeting_id"], meeting_committee=meeting["committee"], meeting_date=meeting["date"],
        meeting_first_topic=first_topic,
        other_meeting_id=other["meeting_id"], other_committee=other["committee"],
        other_committee_id=str(other_committee_id),
        non_protocol_meeting_id=non_protocol["meeting_id"],
        mk_id=str(mk["mk_id"]), mk_name=mk["full_name"], mk_party=mk["party"],
        topic_word=topic_word, table_counts=counts,
    )


@lru_cache(maxsize=1)
def _real_facts_or_skip_reason() -> tuple[RealDbFacts | None, str | None]:
    if not REAL_DB_PATH.exists():
        return None, f"real knesset.db missing at {REAL_DB_PATH}: build it with scripts/build_knesset_db.py"
    try:
        conn = open_real_db_readonly()
        try:
            return discover_real_facts(conn), None
        finally:
            conn.close()
    except (sqlite3.Error, LookupError, ValueError) as exc:
        print(f"[conftest] knesset.db unusable for tests: {exc}")
        return None, f"real knesset.db unusable for tests: {exc}"


@pytest.fixture(scope="session")
def real_db() -> RealDbFacts:
    """Facts of the real Data/knesset.db; skips the test cleanly when the db is missing."""
    facts, reason = _real_facts_or_skip_reason()
    if facts is None:
        pytest.skip(reason)
    return facts


@pytest.fixture(scope="session")
def real_conn(real_db):
    """Read-only connection to the real knesset.db, for SQL oracles."""
    conn = open_real_db_readonly()
    yield conn
    conn.close()


# ── Clients over the real db ──────────────────────────────────────────────────

@pytest.fixture()
def client(real_db):
    """TestClient on the public API (api.app:app) over the real db and live upstream APIs."""
    from fastapi.testclient import TestClient
    from api.app import app
    return TestClient(app)


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def handler_payload(handler, args: dict):
    env = handler(args)
    assert env.error is None, env.error
    return json.loads(env.full)


def assert_within_character_budget(rows: list[dict], budget: int) -> None:
    """A query_protocols page (utils.tool_helpers.char_paging): the rows before the last one stay under the
    budget, so the page runs over it by at most its last row."""
    from utils.tool_helpers.char_paging import served_json_chars
    assert rows
    assert sum(served_json_chars(row) for row in rows[:-1]) < budget


# ── Upstream failure / spy (no canned payloads) ───────────────────────────────

def _clear_upstream_caches():
    import utils.knesset_db as kdb
    kdb._fetch_members.cache_clear()
    kdb._fetch_person_positions.cache_clear()
    kdb._position_names.cache_clear()


class UpstreamSpy:
    """Stands in for requests.get and the cached HTTP session: records every outgoing request
    and fails it with a ConnectionError, so nothing reaches the network."""

    def __init__(self, message: str = "upstream unreachable (test)"):
        self.calls: list[tuple[str, dict]] = []
        self.message = message

    def get(self, url, params=None, **kwargs):
        import requests
        self.calls.append((url, {k: str(v) for k, v in (params or {}).items()}))
        raise requests.exceptions.ConnectionError(f"{self.message}: {url}")

    __call__ = get


@pytest.fixture()
def upstream_down(monkeypatch):
    import requests
    import utils.knesset_db as kdb
    spy = UpstreamSpy()
    monkeypatch.setattr(requests, "get", spy)
    monkeypatch.setattr(kdb, "HTTP_SESSION", spy)
    monkeypatch.setattr(config, "API_RETRY_SLEEP", 0, raising=False)
    _clear_upstream_caches()
    yield spy
    _clear_upstream_caches()


# ── Autouse ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """The public API rate limiter is enabled only by the tests that exercise it."""
    monkeypatch.setattr(config, "API_RATE_LIMIT_ENABLED", False, raising=False)


@pytest.fixture(autouse=True)
def _server_logs_in_tmp(tmp_path, monkeypatch):
    """Server log files go to the test's tmp dir, never Data/logs; handlers are closed after each test."""
    from api import request_log
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    yield
    request_log.close_file_logging()
