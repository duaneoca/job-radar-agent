"""
Langfuse observability seam (manual spans, per D5 — not the LangChain callback).

One trace per email; each LLM call is a named generation (model, input/output, token usage, latency)
nesting under it via the Langfuse v4 OTel context. The trace id is captured and propagated into the
Job Radar writes (`langfuse_trace_id`) so the business pane cross-links to the engineering pane.

Soft dependency: if LANGFUSE_* env isn't set (or the SDK is absent), `get_langfuse()` returns None
and every helper becomes a no-op — the agent runs identically without tracing, and tests need no
Langfuse. Email body DOES appear in trace input (H2): document this / redact upstream if required.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any


_CLIENT = None
_RESOLVED = False


def get_langfuse():
    """Configured Langfuse client (memoized so runner + LLM client share one + its OTel context)."""
    global _CLIENT, _RESOLVED
    if _RESOLVED:
        return _CLIENT
    _RESOLVED = True
    if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
        try:
            from langfuse import Langfuse
            _CLIENT = Langfuse()
        except Exception:
            _CLIENT = None
    return _CLIENT


@contextmanager
def email_trace(lf, *, message_id: str, subject: str):
    """Per-email root span. Yields a handle with `.trace_id` and `.update(**fields)`; no-op if lf None."""
    if lf is None:
        yield _NoHandle()
        return
    with lf.start_as_current_observation(
        as_type="span", name="process_email",
        input={"message_id": message_id, "subject": subject},
    ) as span:
        try:
            tid = lf.get_current_trace_id()
        except Exception:
            tid = None
        yield _SpanHandle(span, tid)


@contextmanager
def generation(lf, *, name: str, model: str, messages: list[dict]):
    """LLM generation span. Yields a handle whose `.finish(output, usage)` records the result."""
    if lf is None:
        yield _NoHandle()
        return
    with lf.start_as_current_observation(
        as_type="generation", name=name, model=model, input=messages,
    ) as gen:
        yield _GenHandle(gen)


class _NoHandle:
    trace_id = None
    def update(self, **_: Any) -> None: ...
    def finish(self, *_: Any, **__: Any) -> None: ...


class _SpanHandle:
    def __init__(self, span, trace_id):
        self._span = span
        self.trace_id = trace_id
    def update(self, **fields: Any) -> None:
        try:
            self._span.update(**fields)
        except Exception:
            pass


class _GenHandle:
    def __init__(self, gen):
        self._gen = gen
    def finish(self, output: Any = None, usage: dict | None = None) -> None:
        try:
            kw: dict[str, Any] = {}
            if output is not None:
                kw["output"] = output
            if usage:
                kw["usage_details"] = usage
            if kw:
                self._gen.update(**kw)
        except Exception:
            pass
