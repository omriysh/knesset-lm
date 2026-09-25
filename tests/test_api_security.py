"""
tests/test_api_security.py

Hostile inputs against the public read-only API (src/api), on the same real sampled db and
recorded OData / oknesset responses as tests/test_api.py. Every rejection must be a 400 in the
{"error_code", "message"} shape, nothing may reach SQLite / FTS5 / OData unescaped, and 5xx
bodies must not carry exception text, paths or tracebacks.
"""

import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import requests

import config
import utils.knesset_db as kdb
import utils.tools as tools
from retrieval import knesset_db_store as store
from tests.conftest import ROLES, SAMPLE
from tests.test_api import client, network, ok  # noqa: F401 — fixtures
from tests.test_odata_tools import BILL_ID, _FakeResponse

M2 = ROLES["M2"]

FTS_PAYLOADS = [
    '"', '""', "'", "*", "ביטחון*", "^ביטחון", "NEAR(ביטחון תקציב, 2)", "ביטחון NEAR תקציב",
    "topic:ביטחון", "{topic text}: ביטחון", "(((", ")", "ביטחון OR 1", "ביטחון AND NOT תקציב",
    "- ביטחון", "+", '"ביטחון', 'ביטחון"', '"" OR ""', "ביטחון\" OR \"x", "AND", "OR", "NOT",
    "\"; DROP TABLE speeches; --", "' OR 1=1 --", "1; SELECT * FROM mks", "ביטחון\\", "%", "_",
    "ביטחון ‏תקציב", "🙂 ביטחון", "ביטחון\u200b", "ﭏ",
]

SQL_PAYLOADS = ["' OR '1'='1", "\" OR 1=1 --", "x'); DROP TABLE mks; --", "%' OR party LIKE '%",
                "1 UNION SELECT sql FROM sqlite_master"]


def bad_request(response, error_code: str | None = None) -> dict:
    assert response.status_code == 400, response.text
    body = response.json()
    assert set(body) >= {"error_code", "message"} and body["message"]
    if error_code:
        assert body["error_code"] == error_code, body
    return body


def not_leaking(body_text: str, *secrets: str):
    lowered = body_text.lower()
    for secret in secrets:
        assert secret.lower() not in lowered
    assert "traceback" not in lowered
    assert "file \"" not in lowered


def db_counts(client) -> dict:
    return ok(client.get("/v1/meta"))["counts"]


# ── FTS5 MATCH ───────────────────────────────────────────────────────────────

class TestFtsInjection:
    @pytest.mark.parametrize("payload", FTS_PAYLOADS)
    def test_match_expression_always_parses(self, sample_db, payload):
        conn = sqlite3.connect(str(sample_db))
        try:
            for scope in store.PROTOCOL_SCOPES:
                match = tools._fts_match(payload, f"{scope}_fts")
                if match:
                    conn.execute(f"SELECT count(*) FROM {scope}_fts WHERE {scope}_fts MATCH ?", (match,)).fetchone()
        finally:
            conn.close()

    @pytest.mark.parametrize("payload", FTS_PAYLOADS)
    def test_handler_never_errors(self, sample_db, payload):
        env = tools.handle_query_protocols({"query": payload, "search_in": list(store.PROTOCOL_SCOPES)})
        assert env.error is None, (env.error, env.metadata)

    @pytest.mark.parametrize("payload", FTS_PAYLOADS + SQL_PAYLOADS)
    def test_api_is_200_or_400(self, client, payload):
        before = db_counts(client)
        r = client.get("/v1/protocols", params={"q": payload, "search_in": "topics,opinions,speeches"})
        if r.status_code == 400:
            assert bad_request(r)["error_code"] == "invalid_query"
        else:
            assert set(ok(r)["results"]) == {"topics", "opinions", "speeches"}
        assert db_counts(client) == before

    def test_operators_are_literal_words(self, client):
        plain = ok(client.get("/v1/protocols", params={"q": "ביטחון"}))["results"]
        assert plain["topics"]
        prefixed = ok(client.get("/v1/protocols", params={"q": "ביטחון*"}))["results"]
        assert prefixed == plain
        with_or = ok(client.get("/v1/protocols", params={"q": "ביטחון OR מילהשאינהקיימת"}))["results"]
        assert all(rows == [] for rows in with_or.values()), "OR must not widen the search"

    def test_punctuation_only_query_is_400(self, client):
        for payload in ('"', "***", "()", "^:-+"):
            bad_request(client.get("/v1/protocols", params={"q": payload}), "invalid_query")

    def test_punctuation_only_query_has_no_rows_in_handler(self, sample_db):
        env = tools.handle_query_protocols({"query": '"', "search_in": ["speeches"]})
        assert env.error is None and '"speeches": []' in env.full


# ── SQL-ish values in filters ────────────────────────────────────────────────

class TestSqlFilters:
    @pytest.mark.parametrize("payload", SQL_PAYLOADS)
    def test_party_and_committee_are_bound_values(self, client, payload):
        before = db_counts(client)
        for field in ("party", "committee"):
            body = ok(client.get("/v1/protocols", params={field: payload, "search_in": "topics,opinions,speeches"}))
            assert all(rows == [] for rows in body["results"].values())
        assert db_counts(client) == before

    @pytest.mark.parametrize("field,value", [
        ("mk_id", "1 OR 1=1"), ("mk_id", "30121'--"), ("mk_id", "abc"), ("mk_id", "٣٠١٢١"), ("mk_id", "1" * 40),
        ("meeting_id", "1 OR 1=1"), ("meeting_id", f"{M2},2 OR 1=1"), ("meeting_id", "²"),
        ("date_from", "2023-01-01' OR 1=1 --"), ("date_from", "2023-13-01"), ("date_from", "2023-1-1"),
        ("date_to", "yesterday"), ("date_to", "2023-02-30"), ("date_to", "20230101"),
    ])
    def test_strict_formats(self, client, field, value):
        bad_request(client.get("/v1/protocols", params={field: value}), f"invalid_{field}")

    def test_valid_dates_pass(self, client):
        ok(client.get("/v1/protocols", params={"date_from": "2023-01-01", "date_to": "2023-12-31"}))

    def test_votes_mk_id_format(self, client):
        bad_request(client.get("/v1/votes", params={"mk_id": "1' or '1'='1"}), "invalid_mk_id")


# ── OData $filter ────────────────────────────────────────────────────────────

_ODATA_LITERAL = re.compile(r"'(?:[^']|'')*'")


def filter_shape(flt: str) -> str:
    """The $filter with every well-formed string literal replaced by S: an injection changes the shape."""
    return _ODATA_LITERAL.sub("S", flt)


def outgoing_filters(network, suffix: str) -> list[str]:
    return [p.get("$filter", "") for url, p in network.calls if url.rstrip("/").endswith(suffix)]


ODATA_PAYLOADS = [
    "x') or (1 eq 1", "x' or '1' eq '1", "חוק'", "'", "''", "x') or true or contains(Name,'",
    "%27) or (1 eq 1", "x%2527", "חוק\u0000' or 1 eq 1", "x\n' or 1 eq 1", "a&$top=1000", "a#b",
    "חוק (תיקון') or (1 eq 1",
]


class TestODataInjection:
    @pytest.mark.parametrize("payload", ODATA_PAYLOADS)
    def test_bill_search_filter_is_escaped(self, client, network, payload):
        r = client.get("/v1/bills", params={"q": payload})
        assert r.status_code in (200, 400, 502), r.text
        filters = outgoing_filters(network, "/KNS_Bill")
        assert filters or r.status_code == 400
        for flt in filters:
            assert filter_shape(flt) == "contains(Name,S) and KnessetNum eq 25", flt
            assert not any(ord(ch) < 32 for ch in flt)

    @pytest.mark.parametrize("payload", ODATA_PAYLOADS)
    def test_vote_topic_filter_is_escaped(self, client, network, payload):
        r = client.get("/v1/votes", params={"q": payload})
        assert r.status_code in (200, 400, 404, 502), r.text
        filters = outgoing_filters(network, "/KNS_PlenumVote")
        assert filters or r.status_code == 400
        for flt in filters:
            assert filter_shape(flt) == "contains(VoteTitle,S) or contains(VoteSubject,S)", flt
            assert not any(ord(ch) < 32 for ch in flt)

    def test_bill_filter_keeps_the_literal_text(self, client, network):
        client.get("/v1/bills", params={"q": "x' or '1' eq '1"})
        flt = outgoing_filters(network, "/KNS_Bill")[0]
        assert "'x'' or ''1'' eq ''1'" in flt

    @pytest.mark.parametrize("bill_id", ["²", "٣", "123abc", "-1", "1 or 1 eq 1", "1)", "9" * 40, "0x10"])
    def test_bill_id_digits_only(self, client, network, bill_id):
        body = bad_request(client.get(f"/v1/bills/{bill_id}"), "invalid_bill_id")
        assert not network.calls, "nothing may reach OData"
        assert body["error_code"] == "invalid_bill_id"

    def test_odata_down_hides_exception_text(self, client, monkeypatch):
        def offline(url, *a, **k):
            raise requests.exceptions.ConnectionError(f"secret-host refused {url}")
        monkeypatch.setattr(requests, "get", offline)
        for path, params in (("/v1/bills", {"q": "חינוך"}), (f"/v1/bills/{BILL_ID}", {}),
                             ("/v1/votes", {"q": "חינוך"})):
            r = client.get(path, params=params)
            assert r.status_code == 502, (path, r.text)
            assert r.json()["error_code"] in ("odata_request_failed", "adapter_exception")
            not_leaking(r.text, "secret-host", "knesset.gov.il", "ConnectionError")


# ── bill documents: SSRF / size / pages ──────────────────────────────────────

class _StreamResponse(_FakeResponse):
    def __init__(self, content: bytes, content_length: str | None = None):
        super().__init__(200, None, content=content, content_type="application/pdf")
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self):
        self.closed = True


class TestBillDocuments:
    def _docs(self, *urls):
        return [{"doc_id": i, "bill_id": 1, "group": "g", "format": "PDF", "url": u} for i, u in enumerate(urls)]

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data", "http://localhost:8080/admin", "file:///etc/passwd",
        "https://fs.knesset.gov.il.evil.com/x.pdf", "https://evil.com/fs.knesset.gov.il/x.pdf",
        "https://user@evil.com/x.pdf", "http://fs.knesset.gov.il/x.pdf", "ftp://fs.knesset.gov.il/x.pdf",
    ])
    def test_only_knesset_https_documents_are_downloaded(self, monkeypatch, url):
        calls = []
        monkeypatch.setattr(kdb, "_get_bill_documents", lambda bill_id: self._docs(url))
        monkeypatch.setattr(requests, "get", lambda u, **k: calls.append(u) or _StreamResponse(b"%PDF"))
        assert kdb._get_bill_text_by_id(1) is None
        assert calls == []

    def test_knesset_document_is_downloaded(self, monkeypatch):
        url = "https://fs.knesset.gov.il/25/law/25_lst_1.pdf"
        monkeypatch.setattr(kdb, "_get_bill_documents", lambda bill_id: self._docs(url))
        monkeypatch.setattr(requests, "get", lambda u, **k: _StreamResponse(b"%PDF-1.4 real"))
        monkeypatch.setattr(kdb, "_extract_pdf_text", lambda content: "טקסט החוק")
        assert kdb._get_bill_text_by_id(1)["text"] == "טקסט החוק"

    @pytest.mark.parametrize("content,length", [(b"x" * 64, None), (b"x" * 8, "999999999")])
    def test_oversized_pdf_is_skipped(self, monkeypatch, content, length):
        url = "https://fs.knesset.gov.il/25/law/big.pdf"
        monkeypatch.setattr(config, "BILL_PDF_MAX_BYTES", 32)
        monkeypatch.setattr(kdb, "_get_bill_documents", lambda bill_id: self._docs(url))
        response = _StreamResponse(content, length)
        monkeypatch.setattr(requests, "get", lambda u, **k: response)
        extracted = []
        monkeypatch.setattr(kdb, "_extract_pdf_text", lambda c: extracted.append(c) or "text")
        assert kdb._get_bill_text_by_id(1) is None
        assert extracted == [] and response.closed

    def test_pdf_page_cap(self, monkeypatch):
        fitz = pytest.importorskip("fitz")
        doc = fitz.open()
        for i in range(12):
            doc.new_page().insert_text((72, 72), f"page {i}")
        pdf_bytes = doc.tobytes()
        monkeypatch.setattr(config, "BILL_PDF_MAX_PAGES", 3)
        text = kdb._extract_pdf_text_pymupdf(pdf_bytes)
        assert "page 2" in text and "page 3" not in text
        text = kdb._extract_pdf_text_pdfplumber(pdf_bytes)
        assert "page 2" in text and "page 3" not in text


# ── sizes / counts / ranges ──────────────────────────────────────────────────

class TestLimits:
    def test_query_length_cap(self, client):
        long_q = "ביטחון " * (config.API_MAX_QUERY_CHARS // 7 + 5)
        for path in ("/v1/protocols", "/v1/mks", "/v1/committees", "/v1/parties", "/v1/bills", "/v1/votes"):
            bad_request(client.get(path, params={"q": long_q}), "invalid_query")

    def test_query_word_cap(self, client):
        many = " ".join(f"מילה{i}" for i in range(config.API_MAX_QUERY_WORDS + 1))
        assert len(many) <= config.API_MAX_QUERY_CHARS
        bad_request(client.get("/v1/protocols", params={"q": many}), "invalid_query")
        ok(client.get("/v1/protocols", params={"q": " ".join(many.split()[:config.API_MAX_QUERY_WORDS])}))

    def test_party_and_committee_length_cap(self, client):
        long_text = "א" * (config.API_MAX_NAME_CHARS + 1)
        bad_request(client.get("/v1/protocols", params={"party": long_text}), "invalid_party")
        bad_request(client.get("/v1/protocols", params={"committee": long_text}), "invalid_committee")

    def test_list_param_count_caps(self, client):
        n = config.API_MAX_LIST_ITEMS + 1
        bad_request(client.get("/v1/protocols", params={"meeting_id": ",".join(["1"] * n)}), "invalid_meeting_id")
        bad_request(client.get("/v1/protocols", params=[("committee", f"c{i}") for i in range(n)]), "invalid_committee")
        bad_request(client.get("/v1/protocols", params={"search_in": ",".join(["topics"] * n)}), "invalid_search_in")

    @pytest.mark.parametrize("offset", ["-1", str(10**6), str(10**30)])
    def test_offset_range(self, client, offset):
        bad_request(client.get("/v1/protocols", params={"offset": offset}), "invalid_offset")

    def test_offset_max_is_allowed(self, client):
        ok(client.get("/v1/protocols", params={"meeting_id": M2, "offset": config.API_MAX_OFFSET}))

    @pytest.mark.parametrize("path", ["/v1/protocols", "/v1/mks?q=x", "/v1/committees?q=x", "/v1/parties?q=x",
                                      "/v1/bills?q=x", f"/v1/bills/{BILL_ID}", "/v1/votes"])
    @pytest.mark.parametrize("knesset_num", ["0", "-1", "999", str(10**20)])
    def test_knesset_num_range(self, client, network, path, knesset_num):
        sep = "&" if "?" in path else "?"
        bad_request(client.get(f"{path}{sep}knesset_num={knesset_num}"), "invalid_knesset_num")
        assert not network.calls

    @pytest.mark.parametrize("top_k", ["-5", "0", str(10**30)])
    def test_top_k_clamped_not_error(self, client, top_k):
        body = ok(client.get("/v1/protocols", params={"meeting_id": M2, "top_k": top_k}))
        assert 1 <= body["args"]["top_k"] <= config.API_PROTOCOLS_MAX_TOP_K

    def test_max_chars_huge_is_clamped(self, client):
        body = ok(client.get(f"/v1/bills/{BILL_ID}", params={"include_text": "true", "max_chars": str(10**30)}))
        assert body["args"]["max_chars"] == config.BILL_TEXT_MAX_MAX_CHARS

    @pytest.mark.parametrize("params", [{"top_k": "abc"}, {"offset": "1.5"}, {"knesset_num": "x"},
                                        {"include_text": "maybe"}])
    def test_type_errors_are_400_in_api_shape(self, client, params):
        path = f"/v1/bills/{BILL_ID}" if "include_text" in params else "/v1/protocols"
        body = bad_request(client.get(path, params=params), "invalid_parameter")
        assert "detail" not in body

    @pytest.mark.parametrize("fmt", ["xml", "MD ", "../json", ""])
    def test_format_enum(self, client, fmt):
        bad_request(client.get("/v1/protocols", params={"meeting_id": M2, "format": fmt}), "invalid_format")


# ── path params ──────────────────────────────────────────────────────────────

class TestPathParams:
    @pytest.mark.parametrize("meeting_id", ["abc", "1 OR 1=1", "²", "1" * 40, "-1"])
    def test_meeting_id_digits_only(self, client, meeting_id):
        bad_request(client.get(f"/v1/meetings/{meeting_id}/attendance"), "invalid_meeting_id")

    @pytest.mark.parametrize("path", ["/v1/meetings/../../etc/passwd/attendance", "/v1/bills/..%2f..%2fetc%2fpasswd",
                                      "/v1/meetings/%2e%2e/attendance", "/v1/meetings/%2e%2e%2fetc%2fpasswd/attendance"])
    def test_traversal_never_500(self, client, path):
        r = client.get(path)
        assert r.status_code in (400, 404), r.text
        not_leaking(r.text, "root:", "passwd")


# ── control characters / unicode ─────────────────────────────────────────────

class TestUnicode:
    def test_control_chars_are_stripped(self, client):
        body = ok(client.get("/v1/protocols", params={"q": "ביטחון\x00\x1b[31m\x7f", "party": "\x00"}))
        assert body["args"]["query"] == "ביטחון [31m"
        assert body["args"]["party"] is None

    def test_null_byte_in_path(self, client):
        bad_request(client.get("/v1/meetings/2199065%00/attendance"), "invalid_meeting_id")

    def test_hebrew_marks_and_emoji_ok(self, client):
        ok(client.get("/v1/protocols", params={"q": "ביטחון\u200f 🙂"}))
        ok(client.get("/v1/mks", params={"q": "\u202eעודד"}))

    def test_invalid_utf8_percent_escapes(self, client):
        r = client.get("/v1/protocols?q=%ff%fe%00")
        assert r.status_code in (200, 400), r.text


# ── error bodies ─────────────────────────────────────────────────────────────

class TestErrorBodies:
    def test_db_failure_is_generic_500(self, client, monkeypatch):
        def broken(*a, **k):
            raise sqlite3.OperationalError("fts5: syntax error near C:\\secret\\knesset.db")
        monkeypatch.setattr(store, "query_protocol_rows", broken)
        r = client.get("/v1/protocols", params={"q": "ביטחון"})
        assert r.status_code == 500 and r.json()["error_code"] == "db_search_failed"
        not_leaking(r.text, "secret", "fts5", "syntax error")

    def test_handler_crash_is_generic_500(self, client, monkeypatch):
        def crash(*a, **k):
            raise RuntimeError("boom at C:\\secret\\path")
        monkeypatch.setattr(tools, "_name_index", crash)
        r = client.get("/v1/committees", params={"q": "חינוך"})
        assert r.status_code == 500 and r.json()["error_code"] == "dispatch_exception"
        not_leaking(r.text, "secret", "boom", "RuntimeError")

    def test_missing_db_hides_path(self, client, monkeypatch, tmp_path):
        missing = tmp_path / "hidden-dir" / "nope.db"
        monkeypatch.setattr(config, "KNESSET_DB", missing)
        for path in ("/v1/protocols?q=x", "/v1/mks?q=x", "/v1/meta"):
            r = client.get(path)
            assert r.status_code == 503, r.text
            not_leaking(r.text, "hidden-dir", "nope.db", str(tmp_path))

    def test_mk_positions_error_is_generic(self, client, monkeypatch):
        def fail(*a, **k):
            raise requests.exceptions.ConnectionError("secret-host down")
        monkeypatch.setattr(tools, "get_mk_positions", fail)
        from tests.conftest import X_MK
        body = ok(client.get("/v1/mks", params={"q": X_MK["full_name"]}))
        profile = body["results"][0]["profile"]
        assert profile["positions_error"]
        not_leaking(str(body), "secret-host")

    def test_404_route_and_method(self, client):
        assert client.get("/v1/nope").status_code == 404
        assert client.post("/v1/protocols").status_code == 405


# ── CORS ─────────────────────────────────────────────────────────────────────

def test_cors_is_public_read_only_without_credentials(client):
    r = client.get("/v1/tools", headers={"Origin": "https://evil.example"})
    assert r.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in r.headers
    pre = client.options("/v1/tools", headers={"Origin": "https://evil.example",
                                               "Access-Control-Request-Method": "DELETE"})
    assert pre.status_code == 400 or "DELETE" not in pre.headers.get("access-control-allow-methods", "")
