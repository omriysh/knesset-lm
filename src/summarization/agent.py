"""
agent.py

Agentic tool-call loop for meeting summarization.
Drives the model until it produces a final text answer,
dispatching Knesset tool calls along the way.
"""

import json
import re

from summarization.llm_client import call_model
from utils.tools import dispatch

_THINK_BLOCK_RE = re.compile(r'<think>.*?</think>', re.DOTALL)
_XML_TOOL_RE    = re.compile(
    r'<tool_call>\s*<function=(\w+)>\s*<parameter=(\w+)>\s*(.*?)\s*</parameter>\s*</function>\s*</tool_call>',
    re.DOTALL,
)


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from a string."""
    return _THINK_BLOCK_RE.sub('', text).strip()


def _extract_xml_tool_calls(content: str) -> list[dict]:
    """
    Fallback: parse raw <tool_call> XML from content when llama.cpp
    fails to populate message.tool_calls properly.
    """
    matches = _XML_TOOL_RE.findall(content)
    return [
        {
            "id": f"xml_fallback_{i}",
            "function": {
                "name":      fn_name,
                "arguments": json.dumps({param_name: param_value.strip()}, ensure_ascii=False),
            },
        }
        for i, (fn_name, param_name, param_value) in enumerate(matches)
    ]


def _extract_tool_calls(message: dict, content: str) -> tuple[list[dict], str]:
    """
    Extract tool calls from a model message, falling back to XML parsing.
    If XML tool calls are found in content, strips them from content.
    Returns (tool_calls, cleaned_content).
    """
    tool_calls = message.get("tool_calls") or []
    if content and not tool_calls:
        xml_calls = _extract_xml_tool_calls(content)
        if xml_calls:
            print(f"⚠️  Parsed {len(xml_calls)} tool call(s) from raw XML (llama.cpp fallback)")
            tool_calls = xml_calls
            content    = _XML_TOOL_RE.sub('', content).strip()
    return tool_calls, content


def _clean_content(raw: str) -> str:
    """Strip thinking blocks and XML tool markup from model output."""
    return _XML_TOOL_RE.sub('', _strip_thinking(raw)).strip()


def run_agent_loop(messages: list[dict], max_rounds: int = 30) -> tuple[str, int]:
    """
    Drive the tool-call loop until the model produces a final text answer.
    Returns (final_answer, total_completion_tokens).

    Handles three edge cases:
    - XML tool calls in content field (llama.cpp quirk)
    - Thinking-only responses (no visible output) → retries with /no_think
    - Max tool round limit → forces a final answer
    """
    total_tokens      = 0
    tool_call_count   = 0
    suppress_thinking = False
    max_iterations    = max_rounds + 5  # slack for thinking-only retries

    for _ in range(max_iterations):
        if suppress_thinking:
            last = messages[-1]
            if last["role"] == "user":
                messages[-1] = {**last, "content": last["content"] + " /no_think"}
            suppress_thinking = False

        response_data = call_model(messages)
        total_tokens += response_data.get("usage", {}).get("completion_tokens", 0)

        choice              = response_data["choices"][0]
        message             = choice["message"]
        finish              = choice.get("finish_reason", "")
        content             = message.get("content", "") or ""
        tool_calls, content = _extract_tool_calls(message, content)
        clean_content       = _clean_content(content)

        if not tool_calls or finish == "stop":
            if clean_content:
                print("\n" + "=" * 60)
                print("📋 OUTPUT")
                print("=" * 60)
                print(clean_content)
                return clean_content, total_tokens
            # Thinking-only response — retry with thinking disabled
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
            print(f"🔧 Tool [{tool_call_count}]: {fn_name}({json.dumps(fn_args, ensure_ascii=False)})")
            result = dispatch(fn_name, fn_args)
            print(f"   ↳ {result[:200]}{'...' if len(result) > 200 else ''}\n")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

        if tool_call_count >= max_rounds:
            print(f"⚠️  Reached max tool rounds ({max_rounds}), forcing final answer.\n")
            messages.append({
                "role":    "user",
                "content": "כעת כתוב את הסיכום הסופי בעברית, ללא קריאות לכלים נוספים.",
            })
            suppress_thinking = True

    print("⚠️  Agent loop exhausted without producing a final answer.")
    return "", total_tokens
