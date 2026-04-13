"""Tests for agent.context — Context and evaluate_condition."""

import pytest

from agent.context import Context, evaluate_condition


# ── Context ───────────────────────────────────────────────────────────────────

class TestContext:
    def test_init_sets_question_vars(self):
        ctx = Context("מה קורה?")
        assert ctx.get("question") == "מה קורה?"
        assert ctx.get("original_question") == "מה קורה?"

    def test_set_and_get(self):
        ctx = Context("q")
        ctx.set("agent", "פרוטוקולים")
        assert ctx.get("agent") == "פרוטוקולים"

    def test_get_missing_returns_default(self):
        ctx = Context("q")
        assert ctx.get("nonexistent") is None
        assert ctx.get("nonexistent", "fallback") == "fallback"

    def test_update_merges_dict(self):
        ctx = Context("q")
        ctx.update({"a": "1", "b": "2"})
        assert ctx.get("a") == "1"
        assert ctx.get("b") == "2"

    def test_node_output_tracking(self):
        ctx = Context("q")
        ctx.set_node_output("llm_001", "Router", "output text")
        assert ctx.get_node_output("llm_001") == "output text"
        assert ctx.get_node_label("llm_001") == "Router"

    def test_get_node_output_missing_returns_empty(self):
        ctx = Context("q")
        assert ctx.get_node_output("nonexistent") == ""
        assert ctx.get_node_label("nonexistent") == "nonexistent"

    def test_render_template_replaces_vars(self):
        ctx = Context("q")
        ctx.set("rag_context", "some passages")
        result = ctx.render_template("Context: {{rag_context}}")
        assert result == "Context: some passages"

    def test_render_template_missing_var_shows_placeholder(self):
        ctx = Context("q")
        result = ctx.render_template("{{missing_var}}")
        assert result == "[missing_var]"

    def test_render_template_no_placeholders(self):
        ctx = Context("q")
        assert ctx.render_template("plain text") == "plain text"

    def test_as_dict_returns_copy(self):
        ctx = Context("q")
        ctx.set("x", "1")
        d = ctx.as_dict()
        assert d["x"] == "1"
        d["x"] = "mutated"
        assert ctx.get("x") == "1"   # original not affected

    def test_reset_for_loop_clears_per_pass_vars(self):
        ctx = Context("original q")
        ctx.set("question", "follow-up q")
        ctx.set("agent", "פרוטוקולים")
        ctx.set("rag_context", "passages")
        ctx.set("meeting_paths", {"m1": "/path/1"})
        ctx.set_node_output("llm_001", "Router", "content")

        ctx.reset_for_loop()

        assert ctx.get("original_question") == "original q"
        assert ctx.get("question") == "follow-up q"
        assert ctx.get("meeting_paths") == {"m1": "/path/1"}
        assert ctx.get("agent") is None
        assert ctx.get("rag_context") is None
        assert ctx.get_node_output("llm_001") == ""   # node outputs cleared

    def test_reset_for_loop_no_meeting_paths_ok(self):
        ctx = Context("q")
        ctx.reset_for_loop()   # should not raise


# ── evaluate_condition ────────────────────────────────────────────────────────

class TestEvaluateCondition:
    def _ctx(self, **kwargs):
        ctx = Context("q")
        for k, v in kwargs.items():
            ctx.set(k, v)
        return ctx

    def test_empty_condition_always_true(self):
        assert evaluate_condition("", self._ctx()) is True
        assert evaluate_condition("  ", self._ctx()) is True

    def test_equals_match(self):
        ctx = self._ctx(agent="פרוטוקולים")
        assert evaluate_condition("agent == 'פרוטוקולים'", ctx) is True

    def test_equals_no_match(self):
        ctx = self._ctx(agent="עובדתי")
        assert evaluate_condition("agent == 'פרוטוקולים'", ctx) is False

    def test_not_equals_match(self):
        ctx = self._ctx(agent="עובדתי")
        assert evaluate_condition("agent != 'פרוטוקולים'", ctx) is True

    def test_not_equals_no_match(self):
        ctx = self._ctx(agent="פרוטוקולים")
        assert evaluate_condition("agent != 'פרוטוקולים'", ctx) is False

    def test_not_empty_truthy(self):
        ctx = self._ctx(follow_up="האם...")
        assert evaluate_condition("follow_up != ''", ctx) is True

    def test_not_empty_falsy(self):
        ctx = self._ctx(follow_up="")
        assert evaluate_condition("follow_up != ''", ctx) is False

    def test_missing_var_treated_as_empty(self):
        ctx = self._ctx()
        assert evaluate_condition("agent != ''", ctx) is False

    def test_in_list_match(self):
        ctx = self._ctx(agent="פרוטוקולים")
        assert evaluate_condition("agent in ['פרוטוקולים', 'שניהם']", ctx) is True

    def test_in_list_no_match(self):
        ctx = self._ctx(agent="עובדתי")
        assert evaluate_condition("agent in ['פרוטוקולים', 'שניהם']", ctx) is False

    def test_in_list_single_value(self):
        ctx = self._ctx(agent="לא רלוונטי")
        assert evaluate_condition("agent in ['לא רלוונטי']", ctx) is True

    def test_unrecognised_form_passes_through(self):
        # Unknown condition syntax → True (pass-through)
        ctx = self._ctx()
        assert evaluate_condition("something_weird", ctx) is True
