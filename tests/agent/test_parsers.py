"""Tests for agent.parsers — parse_output and get_loop_control."""

import warnings

import pytest

from agent.context import Context
from agent.parsers import get_loop_control, parse_output


def _ctx(**kwargs):
    ctx = Context("test question")
    for k, v in kwargs.items():
        ctx.set(k, v)
    return ctx


# ── No output_format ──────────────────────────────────────────────────────────

def test_none_output_format_returns_empty():
    assert parse_output("some content", None, _ctx()) == {}

def test_empty_dict_output_format_returns_empty():
    assert parse_output("some content", {}, _ctx()) == {}


# ── Basic labeled field extraction ───────────────────────────────────────────

ROUTER_FORMAT = {
    "type": "labeled_fields",
    "fields": [
        {"label": "סוכן בשימוש", "var": "agent", "required": True},
        {"label": "שאלה",         "var": "question", "optional": True},
        {"label": "הערות",         "var": "notes_block", "optional": True,
         "format": "\nהערות: {value}", "default": ""},
    ],
}

def test_extracts_simple_fields():
    content = "סוכן בשימוש: פרוטוקולים\nשאלה: מה עמדת חברי הכנסת?"
    result = parse_output(content, ROUTER_FORMAT, _ctx())
    assert result["agent"] == "פרוטוקולים"
    assert result["question"] == "מה עמדת חברי הכנסת?"

def test_format_template_applied():
    content = "סוכן בשימוש: עובדתי\nהערות: חשוב לבדוק"
    result = parse_output(content, ROUTER_FORMAT, _ctx())
    assert result["notes_block"] == "\nהערות: חשוב לבדוק"

def test_default_when_field_absent():
    content = "סוכן בשימוש: עובדתי"
    result = parse_output(content, ROUTER_FORMAT, _ctx())
    # notes_block absent → default is ""
    assert result.get("notes_block") == ""

def test_required_field_missing_warns():
    content = "שאלה: מה?"
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        parse_output(content, ROUTER_FORMAT, _ctx())
    assert any("סוכן בשימוש" in str(warning.message) for warning in w)

def test_multiline_field_value():
    content = "סוכן בשימוש: פרוטוקולים\nשאלה: שורה ראשונה\nשורה שנייה"
    result = parse_output(content, ROUTER_FORMAT, _ctx())
    assert "שורה ראשונה" in result["question"]
    assert "שורה שנייה" in result["question"]


# ── Fallback field ────────────────────────────────────────────────────────────

FALLBACK_FORMAT = {
    "type": "labeled_fields",
    "fields": [
        {"label": "שאלה (פרוטוקולים)", "var": "question_for_rag",
         "optional": True, "fallback": "question"},
    ],
}

def test_fallback_uses_context_var_when_field_absent():
    ctx = _ctx(question="שאלה כללית")
    content = "סוכן בשימוש: פרוטוקולים"   # no "שאלה (פרוטוקולים)" label
    result = parse_output(content, FALLBACK_FORMAT, ctx)
    assert result["question_for_rag"] == "שאלה כללית"

def test_fallback_not_used_when_field_present():
    ctx = _ctx(question="שאלה כללית")
    content = "שאלה (פרוטוקולים): שאלה ספציפית"
    result = parse_output(content, FALLBACK_FORMAT, ctx)
    assert result["question_for_rag"] == "שאלה ספציפית"


# ── fallback_content ──────────────────────────────────────────────────────────

FALLBACK_CONTENT_FORMAT = {
    "type": "labeled_fields",
    "fields": [
        {"label": "שאלת המשך", "var": "follow_up", "optional": True},
        {"label": "תשובה",     "var": "final_answer", "optional": True},
    ],
    "fallback_content": "final_answer",
}

def test_fallback_content_used_when_no_fields_match():
    content = "כאן תשובה ללא פורמט מיוחד."
    result = parse_output(content, FALLBACK_CONTENT_FORMAT, _ctx())
    assert result["final_answer"] == content.strip()

def test_fallback_content_not_used_when_field_matched():
    content = "תשובה: כאן תשובה מפורמטת."
    result = parse_output(content, FALLBACK_CONTENT_FORMAT, _ctx())
    assert result["final_answer"] == "כאן תשובה מפורמטת."
    # fallback_content should NOT override the extracted value
    assert result["final_answer"] != content.strip()


# ── Conditions ────────────────────────────────────────────────────────────────

CONDITION_FORMAT = {
    "type": "labeled_fields",
    "fields": [
        {"label": "סוכן בשימוש", "var": "agent", "required": True},
        {"label": "תשובה",       "var": "answer", "optional": True},
    ],
    "conditions": [
        {
            "when_var":   "agent",
            "when_value": "לא רלוונטי",
            "set": {"final_answer": "{{answer}}", "follow_up": ""},
        }
    ],
}

def test_condition_fires_when_match():
    content = "סוכן בשימוש: לא רלוונטי\nתשובה: שאלה אינה רלוונטית."
    result = parse_output(content, CONDITION_FORMAT, _ctx())
    assert result["final_answer"] == "שאלה אינה רלוונטית."
    assert result["follow_up"] == ""

def test_condition_does_not_fire_when_no_match():
    content = "סוכן בשימוש: פרוטוקולים"
    result = parse_output(content, CONDITION_FORMAT, _ctx())
    assert "final_answer" not in result
    assert "follow_up" not in result


# ── get_loop_control ──────────────────────────────────────────────────────────

def test_get_loop_control_returns_dict():
    fmt = {
        "type": "labeled_fields",
        "loop_control": {"done_var": "final_answer", "continue_var": "follow_up"},
    }
    lc = get_loop_control(fmt)
    assert lc == {"done_var": "final_answer", "continue_var": "follow_up"}

def test_get_loop_control_none_when_absent():
    assert get_loop_control({"type": "labeled_fields"}) is None

def test_get_loop_control_none_for_no_format():
    assert get_loop_control(None) is None


# ── Unknown format type ───────────────────────────────────────────────────────

def test_unknown_format_type_warns_and_returns_empty():
    fmt = {"type": "unknown_future_format"}
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        result = parse_output("content", fmt, _ctx())
    assert result == {}
    assert any("unknown_future_format" in str(warning.message) for warning in w)
