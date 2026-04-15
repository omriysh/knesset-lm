"""
runner.py

MachineRunner: BFS state-machine executor.

Execution model
---------------
Each call to run_stream performs one or more BFS forward passes through the
machine graph.  A Context accumulates named variables as nodes execute.

Within each pass:
  1. A BFS queue starts with the first non-imaginary LLM node after "begin".
  2. A node is dequeued only when all active predecessors (nodes that fired an
     edge toward it in this pass) have completed — the fan-in gate.
  3. After a node completes, its output_format is applied via parsers.parse_output
     and the results are merged into Context.
  4. Outgoing transition edges are evaluated; truthy ones register the source as
     a predecessor of the target and enqueue it.
  5. Back-edges (target already completed) are skipped — looping is handled by
     the outer loop in run_stream, which re-runs the BFS when the loop_control
     continue_var is set.

All dependencies are injected at construction time:
  backend  — LLMBackend implementation
  retrieve — callable(question, chroma_client, embedder, **kwargs) → (str, dict)
  tools    — {function_name: callable(args) → str}
"""

from __future__ import annotations

import json
import time
import warnings
from typing import Callable, Generator, Optional

import config
from agent.context import Context, evaluate_condition
from agent.llm.base import DoneEvent, LLMBackend, ThinkingEvent, TokenEvent, ToolCallsEvent
from agent.machine import StateMachine
from agent.parsers import get_loop_control, parse_output
from utils.meeting import register_meeting_paths


# ── Tool registry builder ─────────────────────────────────────────────────────

def build_tool_registry(
    machine: StateMachine,
    *,
    summary_executor: Optional[Callable] = None,
    speech_executor:  Optional[Callable] = None,
    knesset_dispatch: Optional[Callable] = None,
) -> dict[str, Callable]:
    """
    Walk the machine's tool nodes and build a {function_name: callable} registry.

    Raises ValueError at startup for any unknown function_name rather than
    failing silently at query time.

    Callables receive (args: dict) and return a str result.
    """
    known: dict[str, Callable] = {}

    # Summary tools (use global meeting registry from utils.meeting)
    if summary_executor:
        for name in ("get_meeting_summary", "get_meeting_summary_section"):
            known[name] = lambda args, n=name, ex=summary_executor: ex(n, args)

    # Speech tool
    if speech_executor:
        known["get_mk_speeches_in_committee"] = lambda args: speech_executor(
            "get_mk_speeches_in_committee", args
        )

    # Knesset DB tools (dispatch by name)
    if knesset_dispatch:
        for name in ("get_mk_profile", "get_committee_members",
                     "get_bill_details", "get_bill_text"):
            known[name] = lambda args, n=name: knesset_dispatch(n, args)

    # Validate every tool node in the machine
    missing = []
    for node in machine.tool_nodes_all():
        fn_name = node["data"].get("function_name", "")
        if fn_name and fn_name not in known:
            missing.append(fn_name)

    if missing:
        raise ValueError(
            f"Machine references unknown tool(s): {missing!r}. "
            f"Register them in build_tool_registry before starting the server."
        )

    return known


# ── Machine runner ────────────────────────────────────────────────────────────

class MachineRunner:
    """
    Executes a StateMachine against a user question via BFS.

    Yields (event_type, data) tuples for SSE streaming:
      ("status",      str)   — progress message
      ("node_result", dict)  — completed node output
      ("token",       str)   — final answer text
      ("done",        None)  — stream finished
    """

    def __init__(
        self,
        machine:   StateMachine,
        backend:   LLMBackend,
        retriever,              # callable matching protocol_rag.query_retrieve signature
        tool_registry: dict[str, Callable],
        top_k: int = config.TOP_K_MEETINGS,
        top_n: int = config.TOP_N_DIALOGS,
    ) -> None:
        self.machine       = machine
        self.backend       = backend
        self.retriever     = retriever
        self.tool_registry = tool_registry
        self.top_k         = top_k
        self.top_n         = top_n

    # ── Status helpers ────────────────────────────────────────────────────────

    _STATUS_BY_STAGE: dict[str, str] = {
        "router":   "מנתח שאלה",
        "rag":      "מחפש בפרוטוקולים",
        "factual":  "בודק נתונים עובדתיים",
        "reviewer": "מנסח תשובה",
    }

    # ── Public entry point ────────────────────────────────────────────────────

    def run_stream(
        self,
        question:       str,
        top_k:          Optional[int] = None,
        top_n:          Optional[int] = None,
        resume:         Optional[dict] = None,   # checkpoint dict
        user_response:  Optional[dict] = None,   # {"output_var": str, "value": any}
    ) -> Generator[tuple[str, object], None, None]:
        """
        Full agent loop: one or more BFS passes, looping when a continue_var is set.

        max_loops is read from back-edges in the machine JSON.

        Resume protocol
        ---------------
        When a BFS pass hits a user_input node it yields ("user_input_required", {...})
        and returns.  The caller should persist the checkpoint from that event and
        later call run_stream again with:
          resume        = checkpoint   # the dict from the event
          user_response = {"output_var": <var>, "value": <user's answer>}
        The resumed call restores context, injects the user's answer, and continues
        the BFS exactly from where it paused.  Existing behavior when resume=None is
        fully preserved.
        """
        eff_k = top_k if top_k is not None else self.top_k
        eff_n = top_n if top_n is not None else self.top_n
        max_loops = self.machine.max_loops_from_edges(default=3)

        if resume is not None:
            # ── Resumed execution ──────────────────────────────────────────
            ctx = Context.from_snapshot(resume["ctx_snapshot"])
            if user_response is not None:
                ctx.set(user_response["output_var"], user_response["value"])

            start_loop = resume["loop_idx"]

            # Run the resumed BFS pass (it takes over from the paused node)
            yield from self._run_bfs_pass(ctx, start_loop, eff_k, eff_n, resume=resume)

            # Check whether the resumed pass itself hit another user_input node
            # (in that case _run_bfs_pass already yielded user_input_required and we
            # must not emit "done" here — the next resume call will continue)
            # We detect this by checking if the last event was user_input_required;
            # since generators are lazy we can't look back, so instead we set a
            # sentinel on ctx if a user_input node was encountered.
            if ctx.get("_user_input_pending"):
                return

            # Continue the outer loop from start_loop + 1
            loop_range = range(start_loop + 1, max_loops)
        else:
            # ── Fresh execution ────────────────────────────────────────────
            ctx = Context(question)
            loop_range = range(max_loops)

        if resume is None:
            # Normal path — run first pass then additional loop passes
            first_idx = 0
            yield from self._run_bfs_pass(ctx, first_idx, eff_k, eff_n)
            if ctx.get("_user_input_pending"):
                return

            for loop_idx in range(1, max_loops):
                continue_question = self._find_continue_var(ctx)
                if continue_question:
                    ctx.set("question", continue_question)
                    ctx.reset_for_loop()
                    yield ("status", f"שואל שאלת המשך (סבב {loop_idx + 1}/{max_loops})…")
                    yield from self._run_bfs_pass(ctx, loop_idx, eff_k, eff_n)
                    if ctx.get("_user_input_pending"):
                        return
                else:
                    break
        else:
            # Resumed path — the first resumed pass is already done; run more if needed
            for loop_idx in loop_range:
                continue_question = self._find_continue_var(ctx)
                if continue_question:
                    ctx.set("question", continue_question)
                    ctx.reset_for_loop()
                    yield ("status", f"שואל שאלת המשך (סבב {loop_idx + 1}/{max_loops})…")
                    yield from self._run_bfs_pass(ctx, loop_idx, eff_k, eff_n)
                    if ctx.get("_user_input_pending"):
                        return
                else:
                    break

        # Emit final answer
        final_answer = self._extract_final_answer(ctx)
        if final_answer:
            yield ("token", final_answer)
        yield ("done", None)

    # ── BFS forward pass ──────────────────────────────────────────────────────

    def _run_bfs_pass(
        self,
        ctx:      Context,
        loop_idx: int,
        top_k:    int,
        top_n:    int,
        resume:   Optional[dict] = None,
    ) -> Generator:
        if resume:
            # Restore BFS state from checkpoint; mark paused node as completed and
            # fire its outgoing transitions now that ctx holds the user's answer.
            completed: set[str]          = set(resume["completed_node_ids"]) | {resume["paused_node_id"]}
            fired_to:  dict[str, set[str]] = {k: set(v) for k, v in resume.get("fired_to_snapshot", {}).items()}
            queue:    list[str] = []
            enqueued: set[str]  = set()

            for edge in self.machine.outgoing_transitions(resume["paused_node_id"]):
                tgt_id   = edge["target"]
                tgt_node = self.machine.get_node(tgt_id)
                if (tgt_node.get("imaginary")
                        or tgt_node.get("type") not in ("llm_call", "user_input")
                        or tgt_id in completed):
                    continue
                if not evaluate_condition(edge.get("condition", ""), ctx):
                    continue
                fired_to.setdefault(tgt_id, set()).add(resume["paused_node_id"])
                if tgt_id not in enqueued:
                    queue.append(tgt_id)
                    enqueued.add(tgt_id)
        else:
            first_id   = self.machine.first_llm_node_id()
            queue      = [first_id]
            enqueued   = {first_id}
            completed  = set()
            fired_to   = {}

        while queue:
            node_id = queue.pop(0)
            node    = self.machine.get_node(node_id)

            # Fan-in gate
            required = fired_to.get(node_id, set())
            if not required.issubset(completed):
                queue.append(node_id)
                continue

            # ── user_input node — pause BFS, emit checkpoint ──────────────
            node_type = node.get("type", "llm_call")
            if node_type == "user_input":
                yield from self._run_user_input_node(
                    node, node_id, loop_idx, completed, fired_to, ctx,
                    top_k=top_k, top_n=top_n,
                )
                return  # BFS ends; caller resumes via run_stream(resume=...)

            data         = node.get("data", {})
            node_prompt  = data.get("system_prompt", "")
            global_rules = self.machine.global_rules.strip()
            system_prompt = (
                global_rules + "\n\n" + node_prompt if global_rules and node_prompt
                else global_rules or node_prompt
            )
            stage         = data.get("stage", "")
            label         = node.get("label", "")

            # ── RAG retrieval ──────────────────────────────────────────────
            retrieval_info = None
            if data.get("rag") == "3level" and not ctx.get("rag_context"):
                yield ("status", "מאחזר קטעי פרוטוקולים…")
                rag_q = (
                    ctx.get("question_for_rag")
                    or ctx.get("question")
                    or ctx.get("original_question", "")
                )
                t_rag = time.monotonic()
                context_str, debug = self.retriever(
                    question=rag_q, top_k=top_k, top_n=top_n
                )
                rag_ms = round((time.monotonic() - t_rag) * 1000)
                existing_paths = ctx.get("meeting_paths") or {}
                new_paths = {**existing_paths, **debug.get("meeting_paths", {})}
                ctx.set("meeting_paths", new_paths)
                register_meeting_paths(new_paths)
                ctx.set("rag_context", context_str)

                # Build per-meeting RAG chunk index for the heatmap (pass-1 only).
                _rag_by_mtg: dict = ctx.get("rag_chunks_by_meeting") or {}
                for _item in debug.get("selected_pass1", []):
                    _meta = _item["meta"]
                    _mid  = _meta.get("meeting_id", "")
                    if not _mid:
                        continue
                    _rag_by_mtg.setdefault(_mid, []).append({
                        "start": _meta.get("start_speech_idx", 0),
                        "end":   _meta.get("end_speech_idx",   0),
                        "sim":   round(float(_item["p1_sim"]), 4),
                        "tvec":  _item.get("topic_scores_vec", []),
                    })
                ctx.set("rag_chunks_by_meeting", _rag_by_mtg)

                retrieval_info = {
                    "meetings":      debug.get("meetings", []),
                    "context_chars": debug.get("context_chars", 0),
                    "rag_ms":        rag_ms,
                    "chunks": [
                        {
                            "date":      item["meta"].get("date", ""),
                            "committee": item["meta"].get("committee", ""),
                            "topic":     item["meta"].get("topic_text", "")[:80],
                            "p1_sim":    round(float(item["p1_sim"]), 3),
                            "chars":     item["meta"].get("char_count", 0),
                        }
                        for item in debug.get("selected_pass1", [])
                    ],
                }

            # ── Build user input ──────────────────────────────────────────
            template = data.get("input_template", "")
            if template:
                user_content = ctx.render_template(template)
            else:
                question    = ctx.get("question") or ctx.get("original_question", "")
                rag_context = ctx.get("rag_context", "")
                if rag_context and data.get("rag"):
                    user_content = (
                        "להלן קטעים רלוונטיים ממספר ישיבות ועדה:\n\n"
                        f"{rag_context}\n\n---\n\nשאלה: {question}"
                    )
                else:
                    user_content = question

            # ── Execute node ──────────────────────────────────────────────
            action = self._STATUS_BY_STAGE.get(stage, "מריץ")
            yield ("status", f"{action}: {label}…")
            tool_nodes_list = self.machine.tool_nodes(node_id)

            node_start_ev: dict = {
                "label":  label,
                "stage":  stage,
                "loop":   loop_idx,
                "prompt": {"system": system_prompt, "user": user_content},
            }
            if retrieval_info:
                node_start_ev["retrieval"] = retrieval_info
            yield ("node_start", node_start_ev)

            node_result: dict = {}
            for ev, val in self._run_node(
                node, user_content, system_prompt, tool_nodes_list
            ):
                if ev == "status":
                    yield ("status", val)
                elif ev == "thinking_token":
                    yield ("thinking_token", val)
                elif ev == "node_done":
                    node_result = val

            content = node_result.get("content", "")

            # ── Apply output_format ───────────────────────────────────────
            output_format = data.get("output_format")
            updates = parse_output(content, output_format, ctx)
            ctx.update(updates)

            # ── Store output; accumulate sub-agent outputs for reviewer ───
            ctx.set_node_output(node_id, label, content)
            if stage not in ("router", "reviewer"):
                existing = ctx.get("sub_agent_outputs", "")
                new_part = f"### תשובת הסוכן ({label}):\n{content}"
                ctx.set("sub_agent_outputs",
                        (existing + "\n\n" + new_part).lstrip("\n"))

            # ── Emit node_result ──────────────────────────────────────────
            rag_ms = retrieval_info.get("rag_ms", 0) if retrieval_info else 0
            nr: dict = {
                "label":        label,
                "stage":        stage,
                "content":      content,
                "loop":         loop_idx,
                "elapsed_ms":   node_result.get("elapsed_ms", 0) + rag_ms,
                "llm_ms":       node_result.get("llm_ms", 0),
                "tool_ms":      node_result.get("tool_ms", 0),
                "thinking":     node_result.get("thinking", ""),
                "tools":        node_result.get("tools", []),
                "tool_results": node_result.get("tool_results", []),
                "prompt":       node_result.get("prompt", {}),
            }
            if retrieval_info:
                nr["retrieval"] = retrieval_info
            yield ("node_result", nr)

            completed.add(node_id)

            # ── Fire outgoing transitions ─────────────────────────────────
            for edge in self.machine.outgoing_transitions(node_id):
                tgt_id   = edge["target"]
                tgt_node = self.machine.get_node(tgt_id)

                if (tgt_node.get("imaginary")
                        or tgt_node.get("type") not in ("llm_call", "user_input")
                        or tgt_id in completed):
                    continue

                if not evaluate_condition(edge.get("condition", ""), ctx):
                    continue

                fired_to.setdefault(tgt_id, set()).add(node_id)
                if tgt_id not in enqueued:
                    queue.append(tgt_id)
                    enqueued.add(tgt_id)

    # ── User-input node (pause/checkpoint) ───────────────────────────────────

    def _run_user_input_node(
        self,
        node:      dict,
        node_id:   str,
        loop_idx:  int,
        completed: set[str],
        fired_to:  dict[str, set[str]],
        ctx:       Context,
        top_k:     int = 5,
        top_n:     int = 15,
    ) -> Generator:
        """
        Pause BFS at a user_input node and emit a checkpoint.

        Yields ("status", ...) and ("user_input_required", {...checkpoint...}).
        After yielding, the caller must return — BFS resumes on the next
        run_stream(resume=checkpoint, user_response=...) call.
        """
        data    = node.get("data", {})
        ui      = data.get("ui", "text_input")
        raw_prompt = data.get("prompt_he", "")
        # Render {{var}} placeholders in the prompt using current context
        prompt_he = ctx.render_template(raw_prompt)

        ui_event: dict = {
            "node_id":    node_id,
            "node_label": node.get("label", ""),
            "ui":         ui,
            "prompt_he":  prompt_he,
            "output_var": data.get("output_var", "user_input"),
        }

        # ── option_select payload ─────────────────────────────────────────
        if ui == "option_select":
            preselect_var = data.get("preselect_var", "")
            preselected   = ctx.get(preselect_var, "") if preselect_var else ""
            multi         = data.get("multi_select", False)

            # Map of option value → which context var holds the reformulated question
            _QUESTION_VARS: dict[str, str] = {
                "פרוטוקולים": "question_for_rag",
                "עובדתי":     "question_for_fact",
                "דובר":       "question_for_speaker",
            }
            raw_options = data.get("options", [])
            options_out = []
            for opt in raw_options:
                if isinstance(opt, str):
                    val = opt; label = opt; desc = ""
                else:
                    val   = opt.get("value", opt.get("label", ""))
                    label = opt.get("label", val)
                    desc  = opt.get("description", "")

                item: dict = {
                    "value":       val,
                    "label":       label,
                    "description": desc,
                    "selected":    (val == preselected),
                }
                # Attach reformulated question as subtitle when different from original
                q_var = _QUESTION_VARS.get(val, "")
                if q_var:
                    q_val = ctx.get(q_var, "") or ""
                    orig  = ctx.get("question", "") or ""
                    if q_val and q_val.strip() != orig.strip():
                        item["subtitle"] = q_val
                options_out.append(item)

            ui_event["options"]      = options_out
            ui_event["multi_select"] = multi
            ui_event["preselected"]  = preselected

        # ── deep_dive payload ─────────────────────────────────────────────
        elif ui == "deep_dive":
            query = (
                ctx.get("question_for_rag")
                or ctx.get("question")
                or ctx.get("original_question", "")
            )
            yield ("status", "מאחזר ישיבות רלוונטיות לעיון…")
            _DEEP_DIVE_TOP_K = 20
            context_str, debug = self.retriever(
                question=query, top_k=_DEEP_DIVE_TOP_K, top_n=top_n
            )
            # Pre-populate ctx so if the session is ever resumed the LLM
            # skips a second retrieval (RAG skip gate).
            existing_paths = ctx.get("meeting_paths") or {}
            new_paths = {**existing_paths, **debug.get("meeting_paths", {})}
            ctx.set("meeting_paths", new_paths)
            register_meeting_paths(new_paths)
            ctx.set("rag_context", context_str)

            # Build per-meeting RAG chunk index for the heatmap (pass-1 only).
            _rag_by_mtg: dict = ctx.get("rag_chunks_by_meeting") or {}
            for _item in debug.get("selected_pass1", []):
                _meta = _item["meta"]
                _mid  = _meta.get("meeting_id", "")
                if not _mid:
                    continue
                _rag_by_mtg.setdefault(_mid, []).append({
                    "start": _meta.get("start_speech_idx", 0),
                    "end":   _meta.get("end_speech_idx",   0),
                    "sim":   round(float(_item["p1_sim"]), 4),
                    "tvec":  _item.get("topic_scores_vec", []),
                })
            ctx.set("rag_chunks_by_meeting", _rag_by_mtg)

            meeting_ids = debug.get("meetings", [])
            l1_meta     = debug.get("l1_meeting_meta", {})
            meta_by_mid: dict[str, dict] = {}
            for item in debug.get("selected_pass1", []):
                mid = item["meta"].get("meeting_id", "")
                if mid and mid not in meta_by_mid:
                    meta_by_mid[mid] = item["meta"]

            meetings_out = []
            for rank, mid in enumerate(meeting_ids):
                # Prefer pass-1 meta (richer), fall back to L1 meta (always present)
                meta  = meta_by_mid.get(mid) or l1_meta.get(mid) or {}
                date  = meta.get("date", "")
                comm  = meta.get("committee", "")
                title = f"{comm} — {date}" if comm and date else mid
                meetings_out.append({
                    "meeting_id": mid,
                    "date":       date,
                    "committee":  comm,
                    "title":      title,
                    "score":      round(max(0.5, 1.0 - rank * 0.05), 2),
                })

            ui_event["meetings"]         = meetings_out
            ui_event["query"]            = query
            ui_event["original_question"] = ctx.get("original_question", query)
        checkpoint: dict = {
            "loop_idx":           loop_idx,
            "completed_node_ids": list(completed),
            "paused_node_id":     node_id,
            "fired_to_snapshot":  {k: list(v) for k, v in fired_to.items()},
            "ctx_snapshot":       ctx.to_snapshot(),
            "pending_ui_event":   ui_event,
        }
        # Signal to run_stream that BFS paused on a user_input node
        ctx.set("_user_input_pending", True)

        yield ("status", f"ממתין לקלט משתמש: {node.get('label', '')}…")
        yield ("user_input_required", {**ui_event, "checkpoint": checkpoint})

    # ── Node executor (agentic tool loop) ─────────────────────────────────────

    def _run_node(
        self,
        node:            dict,
        user_content:    str,
        system_prompt:   str,
        tool_nodes_list: list[dict],
        max_rounds:      int = 10,
    ) -> Generator:
        """
        Execute one llm_call node in an agentic tool loop.

        Yields ("status", str), ("node_done", dict).
        """
        data         = node.get("data", {})
        temperature  = float(data.get("temperature", self.backend.TEMPERATURE))
        max_tokens   = int(data.get("max_tokens",   config.MAX_TOKENS))
        tool_schemas = self.machine.build_tool_schemas(tool_nodes_list)

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ]

        buf: list[str]           = []
        tools_used: list[str]    = []
        tool_results: list[dict] = []
        thinking_blocks: list[str] = []
        t0 = time.monotonic()
        llm_ms_total  = 0
        tool_ms_total = 0

        for _round in range(max_rounds):
            content_parts: list[str]  = []
            thinking_parts: list[str] = []
            tool_calls:     list[dict] = []

            t_llm = time.monotonic()
            for event in self.backend.stream(
                self.backend.prepare_messages(messages, suppress_thinking=False),
                tools=tool_schemas or None,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                if isinstance(event, TokenEvent):
                    content_parts.append(event.text)
                elif isinstance(event, ThinkingEvent):
                    thinking_parts.append(event.text)
                    yield ("thinking_token", event.text)
                elif isinstance(event, ToolCallsEvent):
                    tool_calls = event.calls
                # DoneEvent: just continue
            llm_ms_total += round((time.monotonic() - t_llm) * 1000)

            raw_content = "".join(content_parts)

            # Primary: reasoning_content field (ThinkingEvent). Fallback: <think> XML in content.
            if thinking_parts:
                thinking_blocks.append("".join(thinking_parts))
            else:
                thinking = self.backend.extract_thinking(raw_content)
                if thinking:
                    thinking_blocks.append(thinking)

            print(
                f"[runner] round={_round} raw_len={len(raw_content)} "
                f"thinking_parts={len(thinking_parts)} has_think_xml={'<think>' in raw_content}",
                flush=True,
            )

            tool_calls, content = self.backend.extract_tool_calls(
                {}, raw_content
            ) if not tool_calls else (tool_calls, self.backend.extract_visible_content(raw_content))

            if not tool_calls:
                content = self.backend.extract_visible_content(raw_content)
                if self.backend.needs_thinking_retry(content, []):
                    yield ("status", "חושב שוב…")
                    continue
                buf.append(content)
                break

            # Tool calls present
            messages.append({
                "role":       "assistant",
                "content":    self.backend.extract_visible_content(raw_content) or None,
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, ValueError):
                    fn_args = {}

                yield ("status", f"קורא: {fn_name}…")
                if fn_name not in tools_used:
                    tools_used.append(fn_name)

                callable_fn = self.tool_registry.get(fn_name)
                t_tool = time.monotonic()
                result = callable_fn(fn_args) if callable_fn else f"כלי לא נמצא: {fn_name}"

                tool_elapsed_ms = round((time.monotonic() - t_tool) * 1000)
                tool_ms_total  += tool_elapsed_ms
                tool_results.append({
                    "name":       fn_name,
                    "args":       fn_args,
                    "result":     result,
                    "elapsed_ms": tool_elapsed_ms,
                })
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content":      result,
                })

            yield ("status", "ממשיך…")

        elapsed_ms = round((time.monotonic() - t0) * 1000)
        print(
            f"[runner] node_done elapsed={elapsed_ms}ms llm={llm_ms_total}ms "
            f"tool={tool_ms_total}ms thinking_blocks={len(thinking_blocks)}",
            flush=True,
        )
        yield ("node_done", {
            "content":      "".join(buf),
            "thinking":     "\n---\n".join(thinking_blocks),
            "elapsed_ms":   elapsed_ms,
            "llm_ms":       llm_ms_total,
            "tool_ms":      tool_ms_total,
            "tools":        tools_used,
            "tool_results": tool_results,
            "prompt":       {"system": system_prompt, "user": user_content},
        })

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _find_continue_var(self, ctx: Context) -> str:
        """
        Look at all nodes' loop_control to find an active continue_var.
        Returns the value of the continue_var if it's non-empty, else "".
        """
        from agent.parsers import get_loop_control  # avoid circular at module level
        for node in self.machine._nodes.values():
            if node.get("type") != "llm_call" or node.get("imaginary"):
                continue
            lc = get_loop_control(node.get("data", {}).get("output_format"))
            if not lc:
                continue
            continue_var = lc.get("continue_var")
            if continue_var:
                val = ctx.get(continue_var, "")
                if val:
                    return str(val)
        return ""

    def _extract_final_answer(self, ctx: Context) -> str:
        """
        Get the final answer from context or fall back to the last node's content.
        """
        # Check all nodes' loop_control done_var
        for node in self.machine._nodes.values():
            if node.get("type") != "llm_call" or node.get("imaginary"):
                continue
            lc = get_loop_control(node.get("data", {}).get("output_format"))
            if not lc:
                continue
            done_var = lc.get("done_var")
            if done_var:
                val = ctx.get(done_var, "")
                if val:
                    return str(val)

        # Fallback: last terminal node's content, or last node content
        for nid, info in reversed(list(ctx._node_outputs.items())):
            if self.machine.get_node(nid).get("terminal"):
                return info["content"]
        if ctx._node_outputs:
            return list(ctx._node_outputs.values())[-1]["content"]
        return ""
