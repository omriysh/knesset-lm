"""
tests/test_google_backend.py

Which Google models get thinking: the visitor may pick any Gemini/Gemma model per part of a run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import os
from types import SimpleNamespace

import pytest

import config
from agent.llm.base import DoneEvent
from agent.llm.google import GoogleBackend, _model_supports_thinking
from agent.subgraph.llm_bridge import LLMBridge


@pytest.mark.parametrize("model", ["gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-3-pro", "gemini-3.1-flash-lite",
                                   "gemini-3.5-flash", "gemini-3.8-flash", "gemini-10.0-flash", "gemini-flash-latest"])
def test_gemini_2_5_and_later_think(model):
    assert _model_supports_thinking(model)


@pytest.mark.parametrize("model", ["gemini-2.0-flash", "gemini-1.5-pro", "gemma-4-31b-it", "gemma-4-26b-a4b-it"])
def test_older_gemini_and_gemma_do_not_think(model):
    assert not _model_supports_thinking(model)


class FakeStreamingModels:
    def __init__(self, chunks):
        self.chunks = chunks
        self.config = None

    def generate_content_stream(self, model, contents, config):
        self.config = config
        yield from self.chunks


def text_chunk(text, finish_reason=None, usage=None):
    part = SimpleNamespace(text=text, thought=False, function_call=None)
    candidate = SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason=finish_reason)
    return SimpleNamespace(candidates=[candidate], usage_metadata=usage)


def backend_streaming(chunks, model="gemini-3.1-flash-lite"):
    backend = GoogleBackend(model=model, api_key="test-key")
    backend._client = SimpleNamespace(models=FakeStreamingModels(chunks))
    return backend


class TestFinishReason:
    def test_cut_off_output_reports_max_tokens_and_logs_it(self, capsys):
        usage = SimpleNamespace(prompt_token_count=900, candidates_token_count=1500, thoughts_token_count=500)
        backend = backend_streaming([text_chunk('{"answer": "'), text_chunk("cut", SimpleNamespace(name="MAX_TOKENS"), usage)])
        events = list(backend.stream(messages=[{"role": "user", "content": "q"}], max_tokens=2048))
        assert events[-1] == DoneEvent(finish_reason="MAX_TOKENS")
        log = capsys.readouterr().out
        assert "finish=MAX_TOKENS" in log and "thinking_tokens=500" in log and "max_output_tokens=2048" in log
        assert "WARNING" in log

    def test_complete_output_reports_stop(self, capsys):
        backend = backend_streaming([text_chunk("done", SimpleNamespace(name="STOP"))])
        events = list(backend.stream(messages=[{"role": "user", "content": "q"}]))
        assert events[-1] == DoneEvent(finish_reason="STOP")
        assert "WARNING" not in capsys.readouterr().out

    def test_bridge_passes_the_finish_reason_to_llm_done(self):
        bridge = LLMBridge(fallback_to_local=False, api_key="test-key")
        bridge._cache[("google", "gemini-3.1-flash-lite")] = backend_streaming(
            [text_chunk("cut", SimpleNamespace(name="MAX_TOKENS"))])
        done = [ev for ev in bridge.stream(model="gemini-3.1-flash-lite", prompt="q") if ev.kind == "llm_done"]
        assert done[0].payload["finish_reason"] == "MAX_TOKENS"


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get(config.GOOGLE_API_KEY_ENV), reason="no Google API key in the environment")
def test_real_gemini_reports_max_tokens_when_cut_off():
    backend = GoogleBackend(model="gemini-3.1-flash-lite")
    events = list(backend.stream(messages=[{"role": "user", "content": "Write the numbers 1 to 2000, comma separated."}],
                                 max_tokens=64))
    assert events[-1].finish_reason == "MAX_TOKENS"


class RecordingBridge:
    def __init__(self, synthesis_output):
        self.synthesis_output = synthesis_output
        self.stream_kwargs = None

    def __call__(self, **kwargs):
        return "no expand needed"

    def drain_events(self):
        return []

    def stream(self, **kwargs):
        from agent.subgraph.base import SubgraphEvent
        self.stream_kwargs = kwargs
        yield SubgraphEvent(kind="llm_token", name="synthesizer", payload={"text": self.synthesis_output})
        yield SubgraphEvent(kind="llm_done", name="synthesizer",
                            payload={"content": self.synthesis_output, "finish_reason": "STOP"})


def test_synthesizer_gets_its_own_output_token_limit():
    from agent.plan_execute.synthesizer import synthesize_gen
    bridge = RecordingBridge('{"answer": "a [1]", "citations": [{"n": 1, "ev_id": "ev_1", "quote": "q"}]}')
    generator = synthesize_gen("q", None, None, bridge, model="gemini-3.8-flash")
    try:
        while True:
            next(generator)
    except StopIteration as stop:
        answer, citations = stop.value
    assert bridge.stream_kwargs["max_tokens"] == config.SYNTHESIZER_MAX_TOKENS > config.MAX_TOKENS
    assert answer == "a [1]" and len(citations) == 1
