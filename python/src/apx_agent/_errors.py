"""Public tool-facing exceptions.

The one exception here, :class:`ToolError`, is how a tool author says *"this
failure is a legible finding — contain it as a tool result the agent can reason
about, don't crash the turn."*

Motivation (#562): in a multi-step ``SequentialAgent`` a single tool that raises
propagates out of its node, out of the pipeline ``StateGraph``, and out of
``graph.invoke`` — a plain ``RuntimeError`` from a denied query aborts all the
downstream steps and returns HTTP 500 from ``/invocations`` (an opaque
"sub-agent returned 500" across an A2A hop). But for an investigation agent a
denied query, a missing table, or a failed job is exactly the kind of thing it
should *reason about*, not die on.

``ToolError`` draws the bug-vs-finding line by type, so containment never
becomes blanket-swallow: the governance middleware
(``_compile._governance_exception_middleware``) converts a raised ``ToolError``
into an error ``ToolMessage`` the LLM reads and works around — the same path it
already uses for ``PermissionError`` / ``ToolCancelled``. Genuine bugs
(``TypeError``, ``KeyError``, a bare ``RuntimeError``) are NOT ``ToolError`` and
still propagate and fail loud, so a real defect isn't hidden behind a plausible
tool message.

Usage::

    from apx_agent import ToolError

    def run_sql_query(sql: str, ws) -> dict:
        try:
            return {"rows": execute_sql(sql, client=ws)}
        except SQLExecutionError as e:
            # A denied/failed query is a finding, not a crash — the agent reads
            # this and can try a different table or explain the failure.
            raise ToolError(f"Query failed: {e}") from e

Returning a structured error dict (``return {"error": ...}``) is still valid and
equivalent at the tool boundary; ``ToolError`` is the raise-side ergonomic for
aborting from deep inside a helper without threading an error value back up
through every caller.
"""

from __future__ import annotations


class ToolError(Exception):
    """A tool failed in an expected, legible way — contain it, don't crash.

    Raise this from a tool (or a helper a tool calls) to signal an operational
    failure the agent should treat as a finding: a denied query, a missing
    table, a failed downstream call. The governance middleware turns it into an
    error ``ToolMessage`` so the agent loop stays alive and can reason about the
    failure, instead of the exception aborting the whole turn with an HTTP 500.

    Deliberately a direct :class:`Exception` subclass (not ``RuntimeError``) so
    it never collides with a bare ``RuntimeError`` raised by a genuine bug —
    only an *explicit* ``ToolError`` is contained; everything else fails loud.
    """
