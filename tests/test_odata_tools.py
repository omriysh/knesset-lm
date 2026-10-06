"""
tests/test_odata_tools.py

query_bills / get_bill / query_votes against the live Knesset OData + oknesset services
(marked `network`, skipped when offline). Assertions are on stable real facts (a known
bill's name, initiator and documents), not on exact result sets.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import requests

import config
import utils.knesset_db as kdb
from agent.subgraph.evidence import ToolEnvelope
import utils.tools as tools

KNOWN_BILL_ID = 2209870
KNOWN_BILL_NAME = 'הצעת חוק שוברים להשלמת לימודי בסיס בחינוך הבלתי פורמלי, התשפ"ד-2023'
KNOWN_BILL_INITIATOR = {"person_id": 30839, "full_name": "דן אילוז"}
KNOWN_BILL_DOC_IDS = {3414599, 4295186}
MISSING_BILL_ID = 999999999


def _rows(env: ToolEnvelope):
    assert env.error is None, env.error
    return json.loads(env.full)


def _bill_text(row: dict) -> str:
    return row["text"] if isinstance(row["text"], str) else row["text"]["text"]


class TestQueryBillsArguments:
    def test_missing_query(self):
        assert tools.handle_query_bills({"query": "  "}).error == "missing_query"
        assert tools.handle_query_bills({}).error == "missing_query"

    def test_network_failure_returns_error_envelope(self, upstream_down):
        env = tools.handle_query_bills({"query": "חינוך"})
        assert isinstance(env, ToolEnvelope) and env.error


@pytest.mark.network
class TestQueryBills:
    def test_title_search_returns_real_bills(self, live_odata):
        rows = _rows(tools.handle_query_bills({"query": "חינוך"}))
        assert rows and len(rows) <= 10
        for r in rows:
            assert "חינוך" in r["name"]
            assert isinstance(r["knesset_num"], int) and "status" in r

    def test_known_bill_is_found_by_its_title_in_knesset_25(self, live_odata):
        rows = _rows(tools.handle_query_bills({"query": "שוברים להשלמת לימודי בסיס", "knesset_num": 25}))
        known = next(r for r in rows if int(r["bill_id"]) == KNOWN_BILL_ID)
        assert known["name"] == KNOWN_BILL_NAME
        assert all(r["knesset_num"] == 25 for r in rows)

    def test_one_live_odata_title_search_scoped_to_knesset(self, live_odata, odata_requests):
        tools.handle_query_bills({"query": "חינוך", "knesset_num": 25})
        bill_filters = [params["$filter"] for url, params in odata_requests.calls if url.rstrip("/").endswith("/KNS_Bill")]
        assert bill_filters
        assert "contains(Name,'חינוך')" in bill_filters[0] and "KnessetNum eq 25" in bill_filters[0]

    def test_top_k_caps(self, live_odata):
        assert 0 < len(_rows(tools.handle_query_bills({"query": "חינוך", "top_k": 3}))) <= 3

    def test_default_top_k_is_ten(self, live_odata, odata_requests):
        tools.handle_query_bills({"query": "חינוך"})
        bill_searches = [params for url, params in odata_requests.calls if url.rstrip("/").endswith("/KNS_Bill")]
        assert int(bill_searches[0]["$top"]) == 10

    def test_no_match_is_empty_or_error_never_raises(self, live_odata):
        env = tools.handle_query_bills({"query": "מילהשאינהקיימת"})
        assert isinstance(env, ToolEnvelope)
        assert env.error or json.loads(env.full) == []


class TestGetBillArguments:
    def test_missing_bill_id(self):
        assert tools.handle_get_bill({}).error == "missing_bill_id"

    def test_non_numeric_bill_id_is_error_not_raise(self):
        env = tools.handle_get_bill({"bill_id": "abc"})
        assert isinstance(env, ToolEnvelope) and env.error


@pytest.mark.network
class TestGetBill:
    def test_details_without_text(self, live_odata):
        row = _rows(tools.handle_get_bill({"bill_id": str(KNOWN_BILL_ID)}))
        assert row["bill_id"] == KNOWN_BILL_ID
        assert row["bill_name"] == KNOWN_BILL_NAME
        assert row["knesset_num"] == 25
        assert KNOWN_BILL_INITIATOR.items() <= next(
            initiator for initiator in row["initiators"]
            if initiator["person_id"] == KNOWN_BILL_INITIATOR["person_id"]).items()
        assert row["status"]
        assert KNOWN_BILL_DOC_IDS <= {d["doc_id"] for d in row["documents"]}
        assert "text" not in row

    def test_include_text_adds_capped_text(self, live_odata):
        row = _rows(tools.handle_get_bill({"bill_id": str(KNOWN_BILL_ID), "include_text": True}))
        text = _bill_text(row)
        assert len(text) == config.BILL_TEXT_DEFAULT_MAX_CHARS
        assert "שוברים" in text
        assert row["bill_name"] == KNOWN_BILL_NAME

    def test_max_chars_respected_and_clamped(self, live_odata):
        def text_length(max_chars):
            return len(_bill_text(_rows(tools.handle_get_bill(
                {"bill_id": str(KNOWN_BILL_ID), "include_text": True, "max_chars": max_chars}))))
        full_length = text_length(10**7)
        assert config.BILL_TEXT_MIN_MAX_CHARS < full_length <= config.BILL_TEXT_MAX_MAX_CHARS
        assert text_length(3000) == min(3000, full_length)
        assert text_length(10) == config.BILL_TEXT_MIN_MAX_CHARS

    def test_unknown_bill(self, live_odata):
        assert tools.handle_get_bill({"bill_id": str(MISSING_BILL_ID)}).error == "bill_not_found"


def _live_row_count(entity: str, odata_filter: str) -> int:
    response = requests.get(f"{kdb.OFFICIAL_KNESSET_NEW_API}/{entity}",
                            params={"$filter": odata_filter, "$count": "true", "$top": 0}, timeout=30)
    response.raise_for_status()
    return response.json()["@odata.count"]


BILL_WITH_41_INITIATORS = 2230015
BILL_WITH_OVER_20_DOCUMENTS = 2226845


@pytest.mark.network
class TestBillListsAreNotCappedAt20:
    def test_every_initiator_is_returned(self, live_odata):
        expected = _live_row_count("KNS_BillInitiator", f"BillID eq {BILL_WITH_41_INITIATORS}")
        assert expected > 20
        initiators = kdb._get_bill_details_by_id(BILL_WITH_41_INITIATORS)["initiators"]
        assert len(initiators) == expected

    def test_every_document_is_returned(self, live_odata):
        expected = _live_row_count("KNS_DocumentBill", f"BillID eq {BILL_WITH_OVER_20_DOCUMENTS}")
        assert expected > 20
        documents = kdb._get_bill_documents(BILL_WITH_OVER_20_DOCUMENTS)
        assert len({d["doc_id"] for d in documents}) == expected


COMMITTEE_WITH_OVER_100_ROSTER_ROWS = 4190   # Knesset 25; 112 KNS_PersonToPosition rows as of 2026-09


@pytest.mark.network
class TestCommitteeRosterIsNotCappedAt100:
    def test_every_roster_row_is_fetched(self, live_odata, monkeypatch):
        expected = _live_row_count("KNS_PersonToPosition",
                                   f"CommitteeID eq {COMMITTEE_WITH_OVER_100_ROSTER_ROWS} and KnessetNum eq 25")
        assert expected > 100
        fetched_row_counts = []
        real_odata_all_rows = kdb.odata_all_rows

        def counting_odata_all_rows(entity, params):
            rows = real_odata_all_rows(entity, params)
            fetched_row_counts.append(len(rows))
            return rows

        monkeypatch.setattr(kdb, "odata_all_rows", counting_odata_all_rows)
        members = kdb._get_active_committee_members_by_id(COMMITTEE_WITH_OVER_100_ROSTER_ROWS, 25, current_only=False)
        assert fetched_row_counts == [expected]
        assert members


SESSIONS_WITH_PROTOCOL_PAST_ROW_20 = {2240023: 11444329, 2234680: 8720428}
SESSION_WITH_DOCUMENTS_BUT_NO_PROTOCOL = 2240887


@pytest.mark.network
class TestCommitteeSessionProtocolDocuments:
    @pytest.mark.parametrize("session_id,protocol_doc_id", SESSIONS_WITH_PROTOCOL_PAST_ROW_20.items())
    def test_protocol_is_found_among_more_than_20_attachments(self, live_odata, session_id, protocol_doc_id):
        assert _live_row_count("KNS_DocumentCommitteeSession", f"CommitteeSessionID eq {session_id}") > 20
        protocols = kdb._get_session_protocol_documents(session_id)
        assert [d["doc_id"] for d in protocols] == [protocol_doc_id]
        assert protocols[0]["url"].startswith("https://fs.knesset.gov.il/25/Committees/")

    def test_session_without_protocol_has_none(self, live_odata):
        assert kdb._get_session_documents(SESSION_WITH_DOCUMENTS_BUT_NO_PROTOCOL)
        assert kdb._get_session_protocol_documents(SESSION_WITH_DOCUMENTS_BUT_NO_PROTOCOL) == []
        assert kdb._get_session_protocol_text(SESSION_WITH_DOCUMENTS_BUT_NO_PROTOCOL) is None


# ── Live Knesset OData (skipped when offline) ────────────────────────────────

_NO_MATCH_QUERY = "מילהשאינהקיימתבשוםהצבעה"
_TOPIC_IN_KNESSET_20 = "גז"
_MK_OF_KNESSETS_19_TO_24 = "23601"   # KNS_Person 23601, יואל רזבוזוב


@pytest.fixture(scope="module")
def live_odata():
    try:
        response = requests.get(f"{kdb.OFFICIAL_KNESSET_NEW_API}/KNS_KnessetDates", params={"$top": 1}, timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        print(f"[test_odata_tools] Knesset OData unreachable: {exc}")
        pytest.skip(f"Knesset OData unreachable: {exc}")


def _paging(env: ToolEnvelope) -> dict:
    return env.metadata["paging"]


class _SpyingSession:
    """Wraps knesset_db's HTTP session: records (url, params) of every request, then sends it."""

    def __init__(self, session):
        self.session = session
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, {k: str(v) for k, v in (params or {}).items()}))
        return self.session.get(url, params=params, **kwargs)

    def filters(self) -> list[str]:
        return [params.get("$filter", "") for _, params in self.calls]


@pytest.fixture()
def odata_requests(monkeypatch):
    spy = _SpyingSession(kdb.HTTP_SESSION)
    monkeypatch.setattr(kdb, "HTTP_SESSION", spy)
    return spy


class TestLiveVoteArguments:
    @pytest.mark.parametrize("args", [
        {"query": "תקציב", "mk_id": _MK_OF_KNESSETS_19_TO_24, "top_k": 5},
        {"mk_id": _MK_OF_KNESSETS_19_TO_24, "top_k": 5},
        {"query": "תקציב", "top_k": 5},
        {"top_k": 5},
    ], ids=["query_and_mk", "mk_only", "query_only", "neither"])
    def test_every_mode_returns_rows_and_normalized_provenance(self, live_odata, args):
        env = tools.handle_query_votes(args)
        rows = _rows(env)
        assert len(rows) == 5 and all(r["vote_id"] and r["knesset_num"] for r in rows)
        if "mk_id" in args:
            assert all(r["result"] for r in rows)
        assert env.provenance["query"] == args.get("query", "")
        assert env.provenance["mk_id"] == args.get("mk_id", "")
        assert env.provenance["knesset_num"] is None
        assert env.provenance["top_k"] == 5 and env.provenance["offset"] == 0

    def test_default_top_k_is_twenty(self, live_odata, odata_requests):
        assert len(_rows(tools.handle_query_votes({"query": "תקציב"}))) == 20
        searches = [p for u, p in odata_requests.calls if u.endswith("/KNS_PlenumVote")]
        assert searches and int(searches[0]["$top"]) == 20

    def test_old_param_names_ignored(self, live_odata, odata_requests):
        tools.handle_query_votes({"topic": "תקציב", "top_n": 2, "top_k": 2})
        assert not any("תקציב" in f for f in odata_requests.filters())

    def test_newest_first(self, live_odata):
        dates = [r["vote_datetime"] for r in _rows(tools.handle_query_votes({"query": "תקציב", "top_k": 10}))]
        assert dates == sorted(dates, reverse=True)


class TestLiveVotes:
    def test_all_knessets_is_wider_than_knesset_25(self, live_odata):
        everywhere = tools.handle_query_votes({"query": "תקציב", "top_k": 20})
        only_25 = tools.handle_query_votes({"query": "תקציב", "top_k": 20, "knesset_num": 25})
        assert {r["knesset_num"] for r in _rows(only_25)} == {25}
        assert _paging(everywhere)["total"] > _paging(only_25)["total"] > 0
        assert everywhere.provenance["knesset_num"] is None
        assert only_25.provenance["knesset_num"] == 25

    def test_topic_in_knesset_20(self, live_odata):
        rows = _rows(tools.handle_query_votes({"query": _TOPIC_IN_KNESSET_20, "knesset_num": 20, "top_k": 10}))
        assert rows and {r["knesset_num"] for r in rows} == {20}
        assert all("2015" <= r["vote_datetime"][:4] <= "2019" for r in rows)

    def test_recent_votes_of_one_knesset(self, live_odata):
        rows = _rows(tools.handle_query_votes({"knesset_num": 20, "top_k": 5}))
        assert len(rows) == 5 and {r["knesset_num"] for r in rows} == {20}

    def test_mk_votes_filter_by_resolved_person_id(self, live_odata, odata_requests):
        in_20 = tools.handle_query_votes({"mk_id": _MK_OF_KNESSETS_19_TO_24, "knesset_num": 20, "top_k": 10})
        rows = _rows(in_20)
        assert len(rows) == 10 and {r["knesset_num"] for r in rows} == {20}
        assert all(r["result"] for r in rows)
        assert any(f"MkId eq {_MK_OF_KNESSETS_19_TO_24}" in f for f in odata_requests.filters())
        assert not any("LastName eq" in f for f in odata_requests.filters())

        everywhere = tools.handle_query_votes({"mk_id": _MK_OF_KNESSETS_19_TO_24, "top_k": 10})
        assert len({r["knesset_num"] for r in _rows(everywhere)}) >= 1
        assert _paging(everywhere)["total"] > _paging(in_20)["total"] > 0

    def test_mk_votes_on_topic(self, live_odata):
        rows = _rows(tools.handle_query_votes({"mk_id": _MK_OF_KNESSETS_19_TO_24, "query": "תקציב",
                                               "knesset_num": 20, "top_k": 10}))
        assert rows and {r["knesset_num"] for r in rows} == {20}
        assert all(r["result"] for r in rows)
        assert any(r["result"] != "לא הצביע" for r in rows)

    def test_votes_paging_does_not_overlap(self, live_odata):
        first = tools.handle_query_votes({"query": "תקציב", "top_k": 20, "offset": 0})
        second = tools.handle_query_votes({"query": "תקציב", "top_k": 20, "offset": 20})
        assert not {r["vote_id"] for r in _rows(first)} & {r["vote_id"] for r in _rows(second)}
        assert _paging(first) == {"offset": 0, "returned": 20, "has_more": True, "total": _paging(first)["total"]}

    def test_empty_search_is_success_with_no_rows(self, live_odata):
        env = tools.handle_query_votes({"query": _NO_MATCH_QUERY})
        assert env.error is None and json.loads(env.full) == []
        assert _paging(env)["has_more"] is False

    def test_empty_search_over_the_api_is_200(self, live_odata):
        from fastapi.testclient import TestClient
        from api.app import app
        response = TestClient(app).get("/v1/votes", params={"q": _NO_MATCH_QUERY})
        assert response.status_code == 200, response.text
        assert response.json()["results"] == []

    def test_unknown_mk(self, live_odata):
        assert tools.handle_query_votes({"mk_id": "99999999"}).error == "mk_not_found"


class TestLiveBills:
    def test_offset_pages_do_not_overlap(self, live_odata):
        first = tools.handle_query_bills({"query": "חינוך", "top_k": 20, "offset": 0})
        second = tools.handle_query_bills({"query": "חינוך", "top_k": 20, "offset": 20})
        first_ids = {r["bill_id"] for r in _rows(first)}
        second_ids = {r["bill_id"] for r in _rows(second)}
        assert len(first_ids) == len(second_ids) == 20 and not first_ids & second_ids
        assert _paging(first)["has_more"] is True and _paging(first)["total"] >= 40
        assert _paging(second)["offset"] == 20

    def test_all_knessets_is_wider_than_knesset_25(self, live_odata):
        everywhere = tools.handle_query_bills({"query": "חינוך", "top_k": 20})
        only_25 = tools.handle_query_bills({"query": "חינוך", "top_k": 20, "knesset_num": 25})
        assert {r["knesset_num"] for r in _rows(only_25)} == {25}
        assert _paging(everywhere)["total"] > _paging(only_25)["total"] > 0
        assert everywhere.provenance["knesset_num"] is None

    def test_bills_of_knesset_20(self, live_odata):
        rows = _rows(tools.handle_query_bills({"query": "חינוך", "knesset_num": 20, "top_k": 5}))
        assert rows and {r["knesset_num"] for r in rows} == {20}


class TestLiveBillsOfMk:
    def test_initiated_and_joined_add_up_to_every_bill(self, live_odata):
        totals = {role: _paging(tools.handle_query_bills({"mk_id": "30807", "initiator_role": role, "top_k": 1}))["total"]
                  for role in ("", "initiator", "joined")}
        assert totals["initiator"] > 0 and totals["joined"] > 0
        assert totals[""] == totals["initiator"] + totals["joined"]

    def test_rows_are_the_mks_bills_with_named_initiators(self, live_odata):
        rows = _rows(tools.handle_query_bills({"mk_id": "30807", "initiator_role": "joined", "top_k": 5}))
        assert rows
        for row in rows:
            mk_row = next(i for i in row["initiators"] if i["person_id"] == 30807)
            assert row["mk_is_initiator"] is False and mk_row["is_initiator"] is False
            assert all(i["full_name"] for i in row["initiators"])
            assert row["status"]

    def test_query_and_knesset_narrow_the_mks_bills(self, live_odata):
        rows = _rows(tools.handle_query_bills({"mk_id": "30839", "query": "שוברים", "knesset_num": 25}))
        assert KNOWN_BILL_ID in {r["bill_id"] for r in rows}
        assert {r["knesset_num"] for r in rows} == {25}
        known = next(r for r in rows if r["bill_id"] == KNOWN_BILL_ID)
        assert known["first_document_date"] and known["first_document_date"] <= known["last_updated"][:10]

    def test_unknown_mk(self, live_odata):
        assert tools.handle_query_bills({"mk_id": "999999999"}).error == "mk_not_found"

    def test_role_needs_an_mk(self):
        assert tools.handle_query_bills({"query": "חינוך", "initiator_role": "joined"}).error == "invalid_initiator_role"
        assert tools.handle_query_bills({"mk_id": "30807", "initiator_role": "author"}).error == "invalid_initiator_role"


class TestLivePersonVoteSummary:
    def test_counts_add_up_and_fit_the_plenum_votes(self, live_odata):
        summary = kdb.get_person_vote_summary(30807, 25)
        assert summary["votes_cast"] == sum(summary["by_result"].values()) > 0
        assert summary["plenum_votes"] >= summary["votes_cast"]
        assert summary["first_vote"] <= summary["last_vote"]

    def test_person_without_votes(self, live_odata):
        summary = kdb.get_person_vote_summary(30807, 20)
        assert summary["votes_cast"] == summary["plenum_votes"] == 0


class TestLiveFindMkOlderKnesset:
    def test_find_mk_in_knesset_20_uses_the_odata_roster(self, live_odata):
        rows = _rows(tools.handle_find_mk({"query": "יהודה גליק", "knesset_num": 20, "top_k": 3}))
        assert rows and "גליק" in rows[0]["full_name"]


# ── knesset_num validation per tool ──────────────────────────────────────────

class TestKnessetNumRules:
    def _validate(self, tool, args):
        from api.tool_arguments import validated_tool_args
        return validated_tool_args(tool, args)

    @pytest.mark.parametrize("tool,args", [("query_votes", {}), ("query_bills", {"query": "חינוך"})])
    def test_odata_searches_default_to_every_knesset(self, tool, args):
        validated = self._validate(tool, args)
        assert validated["knesset_num"] is None and validated["offset"] == 0

    @pytest.mark.parametrize("tool,args", [("query_votes", {}), ("query_bills", {"query": "חינוך"}),
                                           ("find_party", {"query": "ליכוד"}), ("find_mk", {"query": "גליק"}),
                                           ("find_committee", {"query": "כספים"})])
    @pytest.mark.parametrize("knesset_num", [1, 20, 25])
    def test_odata_tools_accept_older_knessets(self, tool, args, knesset_num):
        assert self._validate(tool, {**args, "knesset_num": knesset_num})["knesset_num"] == knesset_num

    @pytest.mark.parametrize("tool,args", [("query_protocols", {})])
    def test_protocol_tools_are_limited_to_processed_knessets(self, tool, args):
        from api.validation import ApiInputError
        assert self._validate(tool, args)["knesset_num"] == max(config.PROTOCOL_KNESSET_NUMS)
        with pytest.raises(ApiInputError) as raised:
            self._validate(tool, {**args, "knesset_num": 20})
        assert raised.value.error_code == "invalid_knesset_num"

    def test_offset_is_validated(self):
        from api.validation import ApiInputError
        assert self._validate("query_votes", {"offset": 40})["offset"] == 40
        with pytest.raises(ApiInputError):
            self._validate("query_bills", {"query": "חינוך", "offset": -1})
