"""
agent.py

Agentic tool-call loop for meeting summarization.
Drives the model until it produces a final text answer,
dispatching Knesset tool calls along the way.
"""

import json

from agent.llm.base import DoneEvent, LLMBackend, ThinkingEvent, TokenEvent, ToolCallsEvent
from agent.llm.google import GoogleBackend
from utils.tools import TOOLS, dispatch


def run_agent_loop(
    messages:   list[dict],
    max_rounds: int        = 30,
    backend:    LLMBackend | None = None,
    quiet:      bool       = False,
) -> tuple[str, int]:
    """
    Drive the tool-call loop until the model produces a final text answer.
    Returns (final_answer, total_completion_tokens).

    Handles three edge cases:
    - XML tool calls in content field (llama.cpp quirk)
    - Thinking-only responses (no visible output) → retries with /no_think
    - Max tool round limit → forces a final answer
    """
    if backend is None:
        backend = GoogleBackend()

    total_tokens      = 0
    tool_call_count   = 0
    suppress_thinking = False
    max_iterations    = max_rounds + 5

    for _ in range(max_iterations):
        send_messages = backend.prepare_messages(messages, suppress_thinking=suppress_thinking)
        suppress_thinking = False

        raw_content = ""
        tool_calls: list[dict] = []
        token_count = 0

        for event in backend.stream(send_messages, tools=TOOLS, temperature=0.7):
            if isinstance(event, TokenEvent):
                raw_content += event.text
                token_count += 1
            elif isinstance(event, ThinkingEvent):
                pass
            elif isinstance(event, ToolCallsEvent):
                tool_calls = event.calls
            elif isinstance(event, DoneEvent):
                break

        total_tokens += token_count

        tool_calls, clean_content = backend.extract_tool_calls({}, raw_content)
        clean_content = backend.extract_visible_content(clean_content)

        if not tool_calls:
            if not backend.needs_thinking_retry(clean_content, tool_calls):
                if not quiet:
                    print("\n" + "=" * 60)
                    print("📋 OUTPUT")
                    print("=" * 60)
                    print(clean_content)
                return clean_content, total_tokens
            if not quiet:
                print("\n⚠️  Thinking-only response. Re-requesting with thinking disabled.\n")
            messages.append({
                "role":    "user",
                "content": "כעת כתוב את הסיכום הסופי בעברית, ללא קריאות לכלים נוספים.",
            })
            suppress_thinking = True
            continue

        messages.append({"role": "assistant", "content": clean_content, "tool_calls": tool_calls})
        for tc in tool_calls:
            tool_call_count += 1
            fn_name = tc["function"]["name"]
            fn_args = json.loads(tc["function"]["arguments"])
            if not quiet:
                print(f"🔧 Tool [{tool_call_count}]: {fn_name}({json.dumps(fn_args, ensure_ascii=False)})")
            result = dispatch(fn_name, fn_args)
            if not quiet:
                print(f"   ↳ {result[:200]}{'...' if len(result) > 200 else ''}\n")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

        if tool_call_count >= max_rounds:
            if not quiet:
                print(f"⚠️  Reached max tool rounds ({max_rounds}), forcing final answer.\n")
            messages.append({
                "role":    "user",
                "content": "כעת כתוב את הסיכום הסופי בעברית, ללא קריאות לכלים נוספים.",
            })
            suppress_thinking = True

    if not quiet:
        print("⚠️  Agent loop exhausted without producing a final answer.")
    return "", total_tokens
