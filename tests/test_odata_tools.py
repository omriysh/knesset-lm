"""
tests/test_odata_tools.py

query_bills / get_bill / query_votes replayed offline against real Knesset
OData + oknesset responses recorded in tests/fixtures/odata_recordings.json.

requests.get is replaced by a replayer: an exchange matches on url + params
(ignoring $top, which is applied by slicing the recorded "value" list). Bill
title searches match semantically on the contains(Name,'...') term, so an
implementation may choose its own $expand/$orderby. Unrecorded requests raise
ConnectionError (retry sleep is zeroed).
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import requests

import config
import utils.knesset_db as kdb
from agent.subgraph.evidence import ToolEnvelope
import utils.tools as tools

_REC = json.loads((Path(__file__).parent / "fixtures" / "odata_recordings.json").read_text(encoding="utf-8"))
META = _REC["meta"]
RESULTS = _REC["results"]
BILL_ID = META["bill_with_pdf"]
VOTE_MK_ID = META["vote_mk"]["mk_id"]
BILL_POOL_IDS = {b["Id"] for b in RESULTS["search_bills"]}
REAL_BILL_TEXT = RESULTS["bill_text"]["text"]
LONG_BILL_TEXT = (REAL_BILL_TEXT + "\n") * 30


class _FakeResponse:
    def __init__(self, status: int, payload=None, content: bytes = b"", content_type: str = "application/json"):
        self.status_code = status
        self._payload = payload
        self.content = content or (json.dumps(payload).encode() if payload is not None else b"")
        self.headers = {"Content-Type": content_type}
        self.text = self.content.decode("utf-8", errors="ignore")

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


def _strip_top(params: dict) -> dict:
    return {k: str(v) for k, v in (params or {}).items() if k != "$top"}


def _slice(payload, params):
    top = (params or {}).get("$top")
    if top is not None and isinstance(payload, dict) and isinstance(payload.get("value"), list):
        return {**payload, "value": payload["value"][: int(top)]}
    return payload


_CONTAINS_NAME = re.compile(r"contains\(Name,\s*'([^']*)'\)")
_ID_LIST = re.compile(r"\bId eq (\d+)")


class ODataReplayer:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params=None, **kwargs):
        params = dict(params or {})
        self.calls.append((url, params))
        flt = str(params.get("$filter", ""))

        if url.rstrip("/").endswith("/KNS_Bill") and _CONTAINS_NAME.search(flt):
            term = _CONTAINS_NAME.search(flt).group(1)
            pool = RESULTS["search_bills"] if term and term in META["bill_query"] else []
            m = re.search(r"KnessetNum eq (\d+)", flt)
            if m:
                pool = [b for b in pool if b["KnessetNum"] == int(m.group(1))]
            return _FakeResponse(200, _slice({"value": pool}, params))

        for ex in _REC["exchanges"]:
            if ex["url"] == url and _strip_top(ex["params"]) == _strip_top(params):
                if (ex.get("content_type") or "").startswith("application/pdf"):
                    return _FakeResponse(ex["status"], None, content=b"%PDF-1.4 recorded", content_type="application/pdf")
                return _FakeResponse(ex["status"], _slice(ex["json"], params))

        if url.rstrip("/").endswith("/KNS_PlenumVote") and flt.startswith("Id eq"):
            wanted = {int(i) for i in _ID_LIST.findall(flt)}
            pool = {}
            for ex in _REC["exchanges"]:
                if ex["url"].endswith("/KNS_PlenumVote") and isinstance(ex.get("json"), dict):
                    for v in ex["json"].get("value", []):
                        pool[v["Id"]] = v
            return _FakeResponse(200, {"value": [pool[i] for i in sorted(wanted) if i in pool]})

        raise requests.exceptions.ConnectionError(f"unrecorded request: {url} {params}")


@pytest.fixture()
def odata(monkeypatch):
    replayer = ODataReplayer()
    monkeypatch.setattr(requests, "get", replayer)
    monkeypatch.setattr(config, "API_RETRY_SLEEP", 0, raising=False)
    monkeypatch.setattr(kdb, "_extract_pdf_text", lambda content: LONG_BILL_TEXT)
    kdb._fetch_members.cache_clear()
    yield replayer
    kdb._fetch_members.cache_clear()


def _rows(env: ToolEnvelope):
    assert env.error is None, env.error
    return json.loads(env.full)


class TestQueryBills:
    def test_missing_query(self, odata):
        assert tools.handle_query_bills({"query": "  "}).error == "missing_query"
        assert tools.handle_query_bills({}).error == "missing_query"

    def test_title_search_returns_real_bills(self, odata):
        rows = _rows(tools.handle_query_bills({"query": "חינוך"}))
        assert rows and len(rows) <= 10
        for r in rows:
            assert int(r["bill_id"]) in BILL_POOL_IDS
            assert "חינוך" in r["name"]
            assert r["knesset_num"] == 25
            assert "status" in r
        first = next(b for b in RESULTS["search_bills"] if b["Id"] == int(rows[0]["bill_id"]))
        assert rows[0]["status"] == (first.get("KNS_Status") or {}).get("Desc")

    def test_one_live_odata_title_search_scoped_to_knesset(self, odata):
        tools.handle_query_bills({"query": "חינוך", "knesset_num": 25})
        searches = [p for u, p in odata.calls if u.rstrip("/").endswith("/KNS_Bill")]
        assert searches
        flt = searches[0]["$filter"]
        assert "contains(Name,'חינוך')" in flt and "KnessetNum eq 25" in flt

    def test_top_k_caps(self, odata):
        assert len(_rows(tools.handle_query_bills({"query": "חינוך", "top_k": 3}))) <= 3

    def test_default_top_k_is_ten(self, odata):
        tools.handle_query_bills({"query": "חינוך"})
        searches = [p for u, p in odata.calls if u.rstrip("/").endswith("/KNS_Bill")]
        assert int(searches[0]["$top"]) == 10

    def test_no_match_is_empty_or_error_never_raises(self, odata):
        env = tools.handle_query_bills({"query": "מילהשאינהקיימת"})
        assert isinstance(env, ToolEnvelope)
        assert env.error or json.loads(env.full) == []

    def test_network_failure_returns_error_envelope(self, monkeypatch):
        def boom(*a, **k):
            raise requests.exceptions.ConnectionError("offline")
        monkeypatch.setattr(requests, "get", boom)
        monkeypatch.setattr(config, "API_RETRY_SLEEP", 0, raising=False)
        env = tools.handle_query_bills({"query": "חינוך"})
        assert isinstance(env, ToolEnvelope) and env.error


class TestGetBill:
    def test_missing_bill_id(self, odata):
        assert tools.handle_get_bill({}).error == "missing_bill_id"

    def test_details_without_text(self, odata):
        row = _rows(tools.handle_get_bill({"bill_id": str(BILL_ID)}))
        expected = RESULTS["bill_details"]
        assert row["bill_id"] == expected["bill_id"] == BILL_ID
        assert row["bill_name"] == expected["bill_name"]
        assert row["initiators"] == expected["initiators"]
        assert [d["doc_id"] for d in row["documents"]] == [d["doc_id"] for d in expected["documents"]]
        assert "text" not in row

    def test_include_text_adds_capped_text(self, odata):
        row = _rows(tools.handle_get_bill({"bill_id": str(BILL_ID), "include_text": True}))
        text = row["text"] if isinstance(row["text"], str) else row["text"]["text"]
        assert text == LONG_BILL_TEXT[: config.BILL_TEXT_DEFAULT_MAX_CHARS]
        assert row["bill_name"] == RESULTS["bill_details"]["bill_name"]

    def test_max_chars_respected_and_clamped(self, odata):
        def text_len(max_chars):
            row = _rows(tools.handle_get_bill({"bill_id": str(BILL_ID), "include_text": True, "max_chars": max_chars}))
            t = row["text"]
            return len(t if isinstance(t, str) else t["text"])
        assert text_len(3000) == 3000
        assert text_len(10) == config.BILL_TEXT_MIN_MAX_CHARS
        assert text_len(10**7) == config.BILL_TEXT_MAX_MAX_CHARS

    def test_unknown_bill(self, odata):
        assert tools.handle_get_bill({"bill_id": str(META["missing_bill_id"])}).error == "bill_not_found"

    def test_non_numeric_bill_id_is_error_not_raise(self, odata):
        env = tools.handle_get_bill({"bill_id": "abc"})
        assert isinstance(env, ToolEnvelope) and env.error


def _vote_ids(rows):
    return [r["vote_id"] for r in rows]


class TestQueryVotes:
    def test_query_and_mk(self, odata):
        rows = _rows(tools.handle_query_votes({"query": META["vote_topic"], "mk_id": VOTE_MK_ID, "top_k": 5}))
        assert _vote_ids(rows) == _vote_ids(RESULTS["votes_on_topic_by_mk"])
        assert [r["result"] for r in rows] == [r["result"] for r in RESULTS["votes_on_topic_by_mk"]]

    def test_mk_only(self, odata):
        rows = _rows(tools.handle_query_votes({"mk_id": VOTE_MK_ID, "top_k": 5}))
        assert _vote_ids(rows) == _vote_ids(RESULTS["mk_votes"])
        assert all(r["result"] for r in rows)

    def test_query_only(self, odata):
        rows = _rows(tools.handle_query_votes({"query": META["vote_topic"], "top_k": 5}))
        assert _vote_ids(rows) == _vote_ids(RESULTS["votes_on_topic"])

    def test_neither_is_recent_votes(self, odata):
        rows = _rows(tools.handle_query_votes({"top_k": 5}))
        assert _vote_ids(rows) == _vote_ids(RESULTS["recent_votes"])

    def test_top_k_passed_through(self, odata):
        rows = _rows(tools.handle_query_votes({"query": META["vote_topic"], "top_k": 2}))
        assert _vote_ids(rows) == _vote_ids(RESULTS["votes_on_topic"])[:2]

    def test_default_top_k_is_twenty(self, odata):
        tools.handle_query_votes({"query": META["vote_topic"]})
        searches = [p for u, p in odata.calls if u.rstrip("/").endswith("/KNS_PlenumVote") and "$top" in p]
        assert searches and int(searches[0]["$top"]) == 20

    def test_old_param_names_ignored(self, odata):
        tools.handle_query_votes({"topic": META["vote_topic"], "top_n": 2})
        filters = [p.get("$filter", "") for u, p in odata.calls]
        assert not any("תקציב" in f for f in filters)

    def test_unknown_mk(self, odata):
        assert tools.handle_query_votes({"mk_id": "99999999"}).error == "mk_not_found"
