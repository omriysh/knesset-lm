"""
llm_client.py

HTTP client for the local llama-server (OpenAI-compatible /v1/chat/completions).
"""

import sys
import requests

from config import LLAMA_SERVER, MAX_TOKENS, MAX_THINKING_TOKENS
from utils.tools import TOOLS


def call_model(messages: list[dict]) -> dict:
    """
    Send a chat completion request to llama-server with tool use enabled.
    Returns the full response dict.
    Exits with a clear error message if the server is unreachable.
    """
    payload = {
        "messages":            messages,
        "tools":               TOOLS,
        "tool_choice":         "auto",
        "max_tokens":          MAX_TOKENS,
        "temperature":         0.7,
        "top_p":               0.95,
        "top_k":               20,
        "stream":              False,
        "max_thinking_tokens": MAX_THINKING_TOKENS,
    }

    try:
        response = requests.post(
            f"{LLAMA_SERVER}/v1/chat/completions",
            json=payload,
            timeout=600,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        print(f"\n❌ Could not connect to llama-server at {LLAMA_SERVER}")
        print("   Make sure llama-server is running before starting summarization.")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"\n❌ llama-server returned an error: {e}")
        sys.exit(1)

    return response.json()
