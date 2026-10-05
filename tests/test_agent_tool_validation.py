"""
tests/test_agent_tool_validation.py

Research-agent tool calls (plan-execute executor and the web machine-runner shim) go through the
same argument validators as the public /v1 API and MCP, with the agent's own, larger limits.
Invalid LLM-generated arguments become an error ToolEnvelope (invalid_<arg>), never an exception.
Real data: the real knesset.db (conftest.real_db).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from agent.subgraph.evidence import ToolEnvelope
from agent.tools import call_for_machine_runner
from api.routes import tool_call_outcome
from api.tool_arguments import AGENT_LIMITS, PUBLIC_API_LIMITS, agent_tool_args, validated_tool_args
from api import validation as valid
from retrieval.knesset_db_store import PROTOCOL_SCOPES
from utils.tools import ToolSpec, dispatch

ANY_MEETING_ID = "2199065"


def research_call(tool: str, args: dict):
    return dispatch(RESEARCH_TOOL_REGISTRY, tool, args)


class TestInvalidAgentArgumentsBecomeErrorEnvelopes:
    @pytest.mark.parametrize("args,error_code", [
        ({"query": "ביטחון", "offset": 10 ** 30}, "invalid_offset"),
        ({"query": "ביטחון", "offset": -1}, "invalid_offset"),
        ({"query": "ביטחון", "top_k": "abc"}, "invalid_top_k"),
        ({"query": "ביטחון", "meeting_ids": [str(i) for i in range(40_000)]}, "invalid_meeting_id"),
        ({"query": "ביטחון", "meeting_ids": ["1 OR 1=1"]}, "invalid_meeting_id"),
        ({"query": "ביטחון", "date_from": "yesterday"}, "invalid_date_from"),
        ({"query": "ביטחון", "knesset_num": 10 ** 12}, "invalid_knesset_num"),
        ({"query": "\"*\""}, "invalid_query"),
        ({"query": "abc \ud800"}, "invalid_query"),
        ({"query": "ביטחון", "party": {"nested": "dict"}}, "invalid_party"),
    ])
    def test_query_protocols(self, real_db, args, error_code):
        envelope = research_call("query_protocols", args)
        assert envelope.error == error_code
        assert envelope.summary
        assert envelope.metadata["kind"] == "error"

    @pytest.mark.parametrize("tool,args,error_code", [
        ("find_mk", {"query": "x", "top_k": "many"}, "invalid_top_k"),
        ("query_votes", {"query": "x", "mk_id": "../1"}, "invalid_mk_id"),
        ("get_bill", {"bill_id": "12a"}, "invalid_bill_id"),
        ("get_meeting_attendance", {"meeting_id": "٣"}, "invalid_meeting_id"),
        ("get_meeting_attendance", {"meeting_id": "1" * 200}, "invalid_meeting_id"),
    ])
    def test_other_tools(self, real_db, tool, args, error_code):
        assert research_call(tool, args).error == error_code

    def test_machine_runner_shim_validates_too(self, real_db):
        payload = json.loads(call_for_machine_runner(RESEARCH_TOOL_REGISTRY, "query_protocols",
                                                     {"query": "ביטחון", "offset": 10 ** 30}))
        assert payload["error"] == "invalid_offset"


class TestAgentLimitsKeepCurrentBehaviour:
    def test_agent_limits_are_at_least_the_public_limits(self):
        for field_name, public_value in vars(PUBLIC_API_LIMITS).items():
            if field_name == "protocols_page_chars":
                continue
            agent_value = getattr(AGENT_LIMITS, field_name)
            if isinstance(public_value, int) and not isinstance(public_value, bool):
                assert agent_value >= public_value, field_name

    def test_query_protocols_keeps_the_handler_defaults(self, real_db):
        envelope = research_call("query_protocols", {"meeting_ids": [real_db.meeting_id]})
        assert envelope.error is None
        assert envelope.provenance["search_in"] == list(PROTOCOL_SCOPES)
        assert envelope.provenance["top_k"] == config.QUERY_PROTOCOLS_DEFAULT_TOP_K

    def test_query_protocols_accepts_more_than_the_public_caps(self, real_db):
        long_query = " ".join(["ביטחון"] * (config.API_MAX_QUERY_WORDS + 1))
        envelope = research_call("query_protocols", {
            "query": long_query, "top_k": config.QUERY_PROTOCOLS_MAX_TOP_K,
            "offset": config.API_MAX_OFFSET + 1,
            "meeting_ids": [real_db.meeting_id] + [str(1000 + i) for i in range(config.API_MAX_LIST_ITEMS)],
        })
        assert envelope.error is None
        assert envelope.provenance["top_k"] == config.QUERY_PROTOCOLS_MAX_TOP_K
        assert envelope.provenance["offset"] == config.API_MAX_OFFSET + 1

    def test_transcript_listing_returns_the_meeting_rows(self, real_db):
        envelope = research_call("query_protocols", {"meeting_ids": [int(real_db.meeting_id)], "search_in": ["speeches"],
                                                     "top_k": 5.0})
        assert envelope.error is None
        speeches = json.loads(envelope.full)["speeches"]
        assert speeches and all(row["meeting_id"] == real_db.meeting_id for row in speeches)

    def test_find_top_k_is_capped_not_refused(self):
        args = validated_tool_args("find_mk", {"query": "x", "top_k": 10 ** 6}, AGENT_LIMITS)
        assert args["top_k"] == config.AGENT_FIND_MAX_TOP_K
        assert validated_tool_args("find_mk", {"query": "x"}, AGENT_LIMITS)["top_k"] is None

    def test_vote_and_bill_top_k_are_capped(self):
        for tool in ("query_votes", "query_bills"):
            args = validated_tool_args(tool, {"query": "x", "top_k": 10 ** 9}, AGENT_LIMITS)
            assert args["top_k"] == config.AGENT_LIST_MAX_TOP_K

    def test_public_protocol_pages_are_a_character_budget_of_rows(self):
        args = validated_tool_args("query_protocols", {"query": "ביטחון", "offset": config.API_MAX_OFFSET + 1},
                                   PUBLIC_API_LIMITS)
        assert args["page_chars"] == config.API_PROTOCOLS_PAGE_CHARS and "top_k" not in args
        assert args["offset"] == config.API_MAX_OFFSET + 1
        assert args["search_in"] == list(config.API_PROTOCOLS_DEFAULT_SCOPES)
        with pytest.raises(valid.ApiInputError):
            validated_tool_args("query_protocols", {"query": "ביטחון", "offset": config.API_PROTOCOLS_MAX_OFFSET + 1},
                                PUBLIC_API_LIMITS)
        with pytest.raises(valid.ApiInputError):
            validated_tool_args("query_bills", {"query": "x", "offset": config.API_MAX_OFFSET + 1}, PUBLIC_API_LIMITS)

    def test_agent_protocol_pages_stay_rows(self):
        args = validated_tool_args("query_protocols", {"query": "ביטחון"}, AGENT_LIMITS)
        assert args["top_k"] == config.QUERY_PROTOCOLS_DEFAULT_TOP_K and "page_chars" not in args


class TestLlmNumberArguments:
    def test_integral_float_meeting_ids_list_the_meeting(self, real_db):
        envelope = research_call("query_protocols", {"meeting_ids": [float(real_db.meeting_id)], "search_in": ["speeches"]})
        assert envelope.error is None
        speeches = json.loads(envelope.full)["speeches"]
        assert speeches and all(row["meeting_id"] == real_db.meeting_id for row in speeches)

    def test_fractional_float_meeting_id_is_refused(self, real_db):
        assert research_call("query_protocols", {"meeting_ids": [2.5]}).error == "invalid_meeting_id"

    def test_mixed_float_and_string_ids_are_equal(self):
        assert agent_tool_args("query_protocols", {"meeting_ids": [float(ANY_MEETING_ID), ANY_MEETING_ID]})["meeting_ids"] == [ANY_MEETING_ID, ANY_MEETING_ID]


class TestNonPositiveSizesMeanDefault:
    @pytest.mark.parametrize("max_chars", [0, -1, "0", "-5"])
    def test_bill_max_chars(self, max_chars):
        assert validated_tool_args("get_bill", {"bill_id": "1", "max_chars": max_chars}, AGENT_LIMITS)["max_chars"] is None
        assert "max_chars" not in agent_tool_args("get_bill", {"bill_id": "1", "max_chars": max_chars})

    @pytest.mark.parametrize("top_k", [0, -3])
    def test_top_k(self, top_k):
        assert validated_tool_args("find_mk", {"query": "x", "top_k": top_k}, AGENT_LIMITS)["top_k"] is None
        protocols = validated_tool_args("query_protocols", {"query": "x", "top_k": top_k}, AGENT_LIMITS)
        assert protocols["top_k"] == config.QUERY_PROTOCOLS_DEFAULT_TOP_K

    def test_positive_values_are_kept(self):
        assert validated_tool_args("get_bill", {"bill_id": "1", "max_chars": 500}, AGENT_LIMITS)["max_chars"] == 500


def _echo_registry(validator):
    def echo(args):
        return ToolEnvelope(summary="ok", full=json.dumps(args), metadata={}, provenance={})
    return [ToolSpec(name="echo", schema={}, handler=echo, validate_args=validator)]


class TestDispatchValidatorFailures:
    def test_refused_arguments_become_their_error_code(self):
        def refuse(args):
            raise valid.ApiInputError("invalid_offset", "offset too large")
        envelope = dispatch(_echo_registry(refuse), "echo", {"offset": 1})
        assert envelope.error == "invalid_offset"
        assert "offset too large" in envelope.summary

    def test_validator_bug_is_a_dispatch_exception(self):
        def broken(args):
            raise TypeError("validator bug")
        envelope = dispatch(_echo_registry(broken), "echo", {"offset": 1})
        assert envelope.error == "dispatch_exception"
        assert "validator bug" in envelope.metadata["exception"]
        assert "TypeError" in envelope.metadata["traceback"]


class TestValidationRunsOncePerCall:
    @pytest.fixture()
    def agent_validator_calls(self, monkeypatch):
        calls = []
        spec = next(s for s in RESEARCH_TOOL_REGISTRY if s.name == "query_protocols")
        original = spec.validate_args

        def spy(args):
            calls.append(args)
            return original(args)
        monkeypatch.setattr(spec, "validate_args", spy)
        return calls

    def test_already_validated_args_skip_the_validator(self, real_db, agent_validator_calls):
        envelope = dispatch(RESEARCH_TOOL_REGISTRY, "query_protocols", {"meeting_ids": [real_db.meeting_id]},
                            args_already_validated=True)
        assert envelope.error is None and agent_validator_calls == []

    def test_agent_path_validates(self, real_db, agent_validator_calls):
        assert research_call("query_protocols", {"meeting_ids": [real_db.meeting_id]}).error is None
        assert len(agent_validator_calls) == 1

    def test_public_route_does_not_run_the_agent_validator(self, client, real_db, agent_validator_calls):
        response = client.get("/v1/protocols", params={"meeting_id": real_db.meeting_id})
        assert response.status_code == 200
        assert agent_validator_calls == []

    def test_public_tool_call_outcome_does_not_run_the_agent_validator(self, real_db, agent_validator_calls):
        status, _ = tool_call_outcome("query_protocols",
                                      validated_tool_args("query_protocols", {"meeting_ids": [real_db.meeting_id]}))
        assert status == 200 and agent_validator_calls == []


class TestPlenumMeetingIds:
    """Plenum sessions are meetings with id "p" + PlenumSessionID; every other id stays digits only."""

    @pytest.mark.parametrize("value", ["p2245272", "2245272", "p" + "1" * config.API_MAX_ID_DIGITS])
    def test_meeting_id_accepts_committee_and_plenum_ids(self, value):
        assert valid.meeting_id(value) == value

    @pytest.mark.parametrize("value", ["p", "pp1", "P1", "p²", "1p", "p-1", "p" + "1" * (config.API_MAX_ID_DIGITS + 1)])
    def test_meeting_id_rejects_malformed_ids(self, value):
        with pytest.raises(valid.ApiInputError):
            valid.meeting_id(value)

    def test_tool_arguments_accept_plenum_meeting_ids(self):
        assert validated_tool_args("get_meeting_attendance", {"meeting_id": "p2245272"}) == {"meeting_id": "p2245272"}
        assert validated_tool_args("query_protocols", {"meeting_ids": ["p2245272", "2199065"]})["meeting_ids"] == \
            ["p2245272", "2199065"]

    @pytest.mark.parametrize("tool,args,error_code", [
        ("query_votes", {"query": "x", "mk_id": "p1"}, "invalid_mk_id"),
        ("get_bill", {"bill_id": "p1"}, "invalid_bill_id"),
    ])
    def test_other_ids_stay_numeric(self, real_db, tool, args, error_code):
        assert research_call(tool, args).error == error_code
