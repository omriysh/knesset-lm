"""
tests/test_google_backend.py

Which Google models get thinking: the visitor may pick any Gemini/Gemma model per part of a run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from agent.llm.google import _model_supports_thinking


@pytest.mark.parametrize("model", ["gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-3-pro", "gemini-3.1-flash-lite",
                                   "gemini-3.5-flash", "gemini-3.8-flash", "gemini-10.0-flash", "gemini-flash-latest"])
def test_gemini_2_5_and_later_think(model):
    assert _model_supports_thinking(model)


@pytest.mark.parametrize("model", ["gemini-2.0-flash", "gemini-1.5-pro", "gemma-4-31b-it", "gemma-4-26b-a4b-it"])
def test_older_gemini_and_gemma_do_not_think(model):
    assert not _model_supports_thinking(model)
