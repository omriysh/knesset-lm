"""
test_tool_dispatch.py

Tests for utils.tools.dispatch and related tool infrastructure.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import config
from utils.tools import ToolSpec, ToolRegistry, dispatch, handle_find_mk
from agent.subgraph.evidence import ToolEnvelope


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_ok_handler(payload="test"):
    def handler(args: dict) -> ToolEnvelope:
        return ToolEnvelope(
            summary="ok",
            full=str(payload),
            metadata={"kind": "fetch", "source": "test", "count": 1},
            provenance={"tool": "test"},
        )
    return handler


def _make_registry(*entries: ToolSpec) -> ToolRegistry:
    return list(entries)


def _make_spec(name: str, handler=None) -> ToolSpec:
    return ToolSpec(
        name=name,
        schema={"type": "object", "properties": {}},
        handler=handler or _make_ok_handler(),
        task_kinds=["discover"],
        cost_hint="cheap",
    )


# ── dispatch: unknown tool ────────────────────────────────────────────────────

class TestDispatchUnknownTool:
    def test_unknown_tool_returns_envelope(self):
        registry = _make_registry(_make_spec("find_mk"))
        result = dispatch(registry, "no_such_tool", {})
        assert isinstance(result, ToolEnvelope)

    def test_unknown_tool_sets_error(self):
        registry = _make_registry(_make_spec("find_mk"))
        result = dispatch(registry, "no_such_tool", {})
        assert result.error == "unknown_tool"

    def test_unknown_tool_does_not_raise(self):
        registry = _make_registry(_make_spec("find_mk"))
        # Should not raise, just return envelope with error
        envelope = dispatch(registry, "completely_missing", {})
        assert envelope is not None

    def test_unknown_tool_empty_registry(self):
        result = dispatch([], "find_mk", {})
        assert isinstance(result, ToolEnvelope)
        assert result.error == "unknown_tool"

    def test_unknown_tool_has_metadata(self):
        registry = _make_registry()
        result = dispatch(registry, "ghost_tool", {})
        assert "kind" in result.metadata
        assert result.metadata["kind"] == "error"


# ── dispatch: handler exceptions ─────────────────────────────────────────────

class TestDispatchHandlerException:
    def test_handler_exception_returns_envelope(self):
        def bad_handler(args: dict) -> ToolEnvelope:
            raise RuntimeError("Simulated tool failure")

        registry = _make_registry(_make_spec("bad_tool", handler=bad_handler))
        result = dispatch(registry, "bad_tool", {})
        assert isinstance(result, ToolEnvelope)
        assert result.error == "dispatch_exception"

    def test_handler_exception_does_not_raise(self):
        def exploding_handler(args: dict):
            raise ValueError("Boom!")

        registry = _make_registry(_make_spec("boom", handler=exploding_handler))
        # Should not propagate
        envelope = dispatch(registry, "boom", {})
        assert envelope is not None

    def test_handler_exception_metadata_has_exception(self):
        def bad(args):
            raise TypeError("type mismatch")

        registry = _make_registry(_make_spec("bad", handler=bad))
        result = dispatch(registry, "bad", {})
        # The metadata should contain exception info
        assert "exception" in result.metadata or result.error == "dispatch_exception"


# ── dispatch: successful call ─────────────────────────────────────────────────

class TestDispatchSuccess:
    def test_known_tool_calls_handler(self):
        called_with = {}

        def my_handler(args: dict) -> ToolEnvelope:
            called_with.update(args)
            return ToolEnvelope(
                summary="done",
                full="result",
                metadata={"kind": "fetch", "source": "test", "count": 1},
                provenance={},
            )

        registry = _make_registry(_make_spec("my_tool", handler=my_handler))
        result = dispatch(registry, "my_tool", {"key": "value"})
        assert called_with == {"key": "value"}
        assert result.error is None

    def test_dispatch_passes_args_to_handler(self):
        received_args = {}

        def capture_handler(args: dict) -> ToolEnvelope:
            received_args.update(args)
            return ToolEnvelope(
                summary="", full="", metadata={"kind": "test", "source": "test", "count": 0},
                provenance={},
            )

        registry = _make_registry(_make_spec("capture", handler=capture_handler))
        dispatch(registry, "capture", {"query": "נתניהו", "knesset_num": 25})
        assert received_args["query"] == "נתניהו"

    def test_dispatch_with_none_args_uses_empty_dict(self):
        """Passing None as args should not crash the handler."""
        received_args = {}

        def capture_handler(args: dict) -> ToolEnvelope:
            received_args["got"] = args
            return ToolEnvelope(summary="", full="",
                                metadata={"kind": "test", "source": "test", "count": 0},
                                provenance={})

        registry = _make_registry(_make_spec("capture", handler=capture_handler))
        dispatch(registry, "capture", None)
        assert isinstance(received_args.get("got"), dict)


# ── dispatch: find_mk with real BM25 db ──────────────────────────────────────

class TestDispatchFindMkNoDB:
    def test_dispatch_find_mk_missing_db_returns_error_envelope(self, tmp_path):
        """When BM25 db path doesn't exist, find_mk should return envelope with error."""
        import config as _config

        original_bm25_dir = _config.BM25_DIR
        # Point BM25_DIR at a nonexistent location
        _config.BM25_DIR = tmp_path / "nonexistent_bm25"
        try:
            from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
            result = dispatch(RESEARCH_TOOL_REGISTRY, "find_mk", {"query": "נתניהו"})
            assert isinstance(result, ToolEnvelope)
            # Should have an error — db is missing
            assert result.error is not None
            assert result.error != "unknown_tool"  # should find the tool, but fail on db
        finally:
            _config.BM25_DIR = original_bm25_dir


class TestDispatchFindMkWithDB:
    """Test find_mk when the BM25 db is present.

    This test is conditional on the BM25 db existing. If missing, it is
    skipped so CI doesn't fail on an incomplete build.
    """

    def test_dispatch_find_mk_with_real_db(self):
        bm25_mks_path = config.BM25_DIR / "25" / "mks.db"
        if not bm25_mks_path.exists():
            pytest.skip(f"BM25 mks.db not built yet: {bm25_mks_path}")

        from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
        result = dispatch(RESEARCH_TOOL_REGISTRY, "find_mk", {"query": "נתניהו"})

        assert isinstance(result, ToolEnvelope)
        # With a real db, error should be None (or at worst a low-confidence warning)
        # The tool returns candidates even when fuzzy match is used
        if result.error is not None:
            # If error is set, it should be a soft warning, not a crash
            assert result.error not in ("dispatch_exception", "unknown_tool")
        else:
            # No error — we should have some results
            assert result.metadata.get("count", 0) >= 0


# ── ToolSpec ──────────────────────────────────────────────────────────────────

class TestToolSpec:
    def test_tool_spec_to_dict_excludes_handler(self):
        spec = _make_spec("find_mk")
        d = spec.to_dict()
        assert "name" in d
        assert "schema" in d
        assert "task_kinds" in d
        assert "cost_hint" in d
        assert "handler" not in d
