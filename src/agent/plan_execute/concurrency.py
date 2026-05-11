"""
concurrency.py

DAGExecutor — schedules plan steps under a fixed-size ThreadPoolExecutor,
respecting per-step DAG dependencies, with a single-slot lane reserved
for `task_kind == "deep_dive"` steps.

See: Documentation/KnessetLM/Development/Claude/plan-and-execute-design.md §9
"""

from __future__ import annotations

import threading
from concurrent.futures import (
    ALL_COMPLETED,
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    wait,
)
from typing import Any, Callable, Iterator

from config import (
    RESEARCH_DAG_MAX_WORKERS,
    RESEARCH_DEEP_DIVE_MAX_PARALLEL,
)

from agent.plan_execute.plan import Step


class DAGExecutor:
    """Topological scheduler over a fixed-size thread pool.

    Usage::

        executor = DAGExecutor()
        executor.submit(step, fn)        # submit each step
        for step_id, result in executor.results():
            ...                          # consume in completion order
        executor.shutdown()              # or use as a context manager

    Or, more commonly, drive it through `run_steps(steps, fn)` which
    handles dep-aware submission for you.

    Failure isolation: each step's worker catches its own exceptions and
    returns an envelope; if a worker raises, this scheduler still keeps
    going for steps that do not depend on the failed one. Critic-post
    sees the failures.
    """

    def __init__(
        self,
        max_workers: int = RESEARCH_DAG_MAX_WORKERS,
        deep_dive_max_parallel: int = RESEARCH_DEEP_DIVE_MAX_PARALLEL,
    ):
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        # Deep-dive lane: a Semaphore gates *any* step whose task_kind ==
        # "deep_dive" — the worker acquires the slot before doing real work
        # and releases it on the way out.
        self._deep_dive_sem = threading.Semaphore(deep_dive_max_parallel)

        self._lock = threading.Lock()
        self._futures: dict[str, Future] = {}             # step_id -> Future
        self._steps: dict[str, Step] = {}                 # step_id -> Step
        self._results: dict[str, Any] = {}                # step_id -> result/exception
        self._cancelled = False

    # ── Context manager ─────────────────────────────────────────────────
    def __enter__(self) -> "DAGExecutor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown(wait=False)

    # ── Public API ──────────────────────────────────────────────────────
    def submit(self, step: Step, fn: Callable[[Step], Any]) -> Future:
        """Submit `step` for execution. Returns a Future that resolves to
        whatever `fn(step)` returns.

        Waits for all `step.deps` to have completed before invoking `fn`.
        Deep-dive steps additionally hold the deep-dive semaphore for the
        duration of `fn`.
        """
        with self._lock:
            if self._cancelled:
                # Return a pre-cancelled future-like object.
                fut: Future = Future()
                fut.cancel()
                fut.set_exception(RuntimeError("DAGExecutor was cancelled"))
                return fut

            if step.id in self._futures:
                raise ValueError(
                    f"step id {step.id!r} already submitted to DAGExecutor"
                )
            self._steps[step.id] = step
            dep_futures = [
                self._futures[d] for d in step.deps if d in self._futures
            ]

        is_deep_dive = step.task_kind == "deep_dive"

        def _runner() -> Any:
            # Wait for dep futures first. We do this *inside* the worker
            # so we don't tie up the submitter's thread.
            if dep_futures:
                wait(dep_futures, return_when=ALL_COMPLETED)
                # Surface dep failure as a hard skip — the executor for
                # this step will see the missing inputs and decide
                # abort_step, but if any dep raised, we should not even
                # start. Re-raise the first dep exception.
                for df in dep_futures:
                    exc = df.exception()
                    if exc is not None:
                        raise RuntimeError(
                            f"step {step.id!r} skipped: dependency failed: {exc!r}"
                        )

            # Cancellation check after deps settle.
            if self._cancelled:
                raise RuntimeError(f"step {step.id!r} cancelled before start")

            if is_deep_dive:
                # Block for a deep-dive slot.
                self._deep_dive_sem.acquire()
                try:
                    return fn(step)
                finally:
                    self._deep_dive_sem.release()
            else:
                return fn(step)

        future = self._pool.submit(_runner)
        with self._lock:
            self._futures[step.id] = future

        # Plumb the eventual result/exception into self._results when done.
        def _on_done(f: Future, _sid: str = step.id) -> None:
            with self._lock:
                if f.cancelled():
                    self._results[_sid] = RuntimeError("cancelled")
                elif f.exception() is not None:
                    self._results[_sid] = f.exception()
                else:
                    self._results[_sid] = f.result()
        future.add_done_callback(_on_done)
        return future

    def cancel_all(self) -> None:
        """Best-effort cancel of all pending/in-flight steps. Safe to call
        from any thread (e.g. an SSE-close handler)."""
        with self._lock:
            self._cancelled = True
            futures = list(self._futures.values())
        for fut in futures:
            fut.cancel()

    def results(self) -> Iterator[tuple[str, Any]]:
        """Yield (step_id, result_or_exception) tuples in completion order
        for every step submitted so far. Result is whatever the worker
        returned; if the worker raised, the exception object is yielded
        instead. Iteration ends once all currently submitted futures have
        finished."""
        with self._lock:
            pending = set(self._futures.values())
            id_by_future = {f: sid for sid, f in self._futures.items()}

        while pending:
            done, pending_set = wait(pending, return_when=FIRST_COMPLETED)
            pending = pending_set
            for fut in done:
                sid = id_by_future[fut]
                if fut.cancelled():
                    yield sid, RuntimeError("cancelled")
                elif fut.exception() is not None:
                    yield sid, fut.exception()
                else:
                    yield sid, fut.result()

    def run_steps(
        self,
        steps: list[Step],
        fn: Callable[[Step], Any],
    ) -> Iterator[tuple[str, Any]]:
        """Convenience: submit every step in `steps` (the DAG dep-wait is
        handled per-future in `submit`), then yield results as they come in.
        Order of submission does not matter — deps are waited on inside
        the worker — so this is a single-pass loop."""
        for step in steps:
            self.submit(step, fn)
        yield from self.results()

    def shutdown(self, *, wait: bool = True) -> None:
        """Shut down the underlying ThreadPoolExecutor."""
        self._pool.shutdown(wait=wait)
