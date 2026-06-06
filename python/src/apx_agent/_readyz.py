"""``/readyz`` capability self-test for the Databricks Apps runtime.

``/health`` is a *liveness* probe — it returns 200 as soon as the server
process has booted. ``/readyz`` is a *readiness* probe: it proves the agent
can actually answer a canned prompt and that an MLflow trace was recorded for
the run. ``apx deploy`` later calls this endpoint as a deploy gate (Slice C),
so a green deploy means the agent really responds and traces.

Design:

  * ``GET /readyz`` runs a canned prompt (``"Reply with exactly: READY"``)
    through the compiled agent and returns structured JSON.
  * The agent-run step is factored into the module-global
    ``_run_canned_probe(agent, model) -> (text, trace_id)`` so tests can
    monkeypatch it without a real model call. The route handler looks the
    helper up by module-global name at call time (never captured into the
    closure at mount time) so ``monkeypatch.setattr`` resolves.
  * The handler is wrapped in try/except so it NEVER 500s — an unexpected
    error returns 503 with a short ``error`` string.

Checks:

  * ``llm`` — ``"ok"`` if the agent produced non-empty assistant text, else
    ``"fail"``.
  * ``tracing`` — ``"ok"`` if a trace id was produced for the run, else
    ``"unavailable"`` (mlflow not installed / tracing off). Tracing has only
    these two states — it never causes ``degraded`` on its own (a legitimately
    tracing-off deploy is still ready).
  * ``tools_registered`` — integer count of tools on the agent (informational,
    never fails).
  * ``tool_exec`` — best-effort, always ``"skipped"`` for now (running a real
    tool needs OBO / user data access this self-test lacks).

Contract:

  * Overall ``status`` is ``"ready"`` iff ``llm == "ok"`` AND
    ``tracing in ("ok", "unavailable")``; else ``"degraded"``.
  * HTTP 200 ``{"status":"ready","checks":{...}}`` when ready;
    HTTP 503 ``{"status":"degraded","checks":{...}}`` otherwise.
  * On unexpected error: HTTP 503
    ``{"status":"degraded","checks":{...},"error":"<short>"}``.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from fastapi import FastAPI

    from ._agents import BaseAgent

logger = logging.getLogger(__name__)

# The canned prompt the self-test runs through the agent.
_CANNED_PROMPT = "Reply with exactly: READY"

# Default serving endpoint, mirroring the scaffold's ``start_server.py``.
_DEFAULT_MODEL = "databricks-claude-sonnet-4-6"


def _count_tools(agent: "BaseAgent") -> int:
    """Number of tools registered directly on the agent — never raises."""
    try:
        from ._topology import _agent_tools

        return len(_agent_tools(agent))
    except Exception:  # pragma: no cover — defensive
        return 0


def _extract_assistant_text(response: Any) -> str:
    """Join all assistant text parts out of a ResponsesAgentResponse.

    Mirrors the output shape produced by ``compile_to_responses_agent``:
    ``response.output`` is a list of items; assistant messages have
    ``type == "message"``, ``role == "assistant"`` and a ``content`` list of
    typed parts, each carrying a ``text`` field.
    """
    parts: list[str] = []
    try:
        output = getattr(response, "output", None)
        if output is None and isinstance(response, dict):
            output = response.get("output")
        for item in output or []:
            d = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            if d.get("type") != "message" or d.get("role") != "assistant":
                continue
            for content_part in d.get("content") or []:
                text = content_part.get("text") if isinstance(content_part, dict) else None
                if text:
                    parts.append(text)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("readyz: failed to extract assistant text: %s", exc)
    return "".join(parts).strip()


def _last_trace_id() -> str | None:
    """Return the trace id of the most recent run, or ``None``.

    Uses ``mlflow.get_last_active_trace_id()``. This works without a live
    tracking server: ``compile_to_responses_agent``'s invoke path opens
    ``safe_span("ApxResponsesAgent.invoke", span_type="AGENT")``, so a trace
    id exists in-process whenever mlflow is importable — independent of
    export / autolog. Returns ``None`` if mlflow is missing or no trace ran.
    """
    try:
        import mlflow

        return mlflow.get_last_active_trace_id()
    except Exception:
        return None


def _run_canned_probe(agent: "BaseAgent", model: str | None) -> tuple[str, str | None]:
    """Run the canned prompt through the compiled agent.

    Returns ``(assistant_text, trace_id)``. Compiles the agent via
    ``compile_to_responses_agent``, invokes the non-streaming handler with the
    canned prompt, extracts the assistant text, and captures the last trace id
    immediately after the run (when the invoke span has closed).

    This is the production path. Tests monkeypatch this module-global so no
    real model / network call happens.
    """
    from ._responses_agent import compile_to_responses_agent

    effective_model = model or os.environ.get("APX_MODEL") or _DEFAULT_MODEL
    non_streaming, _stream = compile_to_responses_agent(agent, model=effective_model)
    response = non_streaming({"input": [{"role": "user", "content": _CANNED_PROMPT}]})
    text = _extract_assistant_text(response)
    trace_id = _last_trace_id()
    return text, trace_id


def mount_readyz(app: "FastAPI", agent: "BaseAgent", *, model: str | None = None) -> None:
    """Mount a ``GET /readyz`` capability self-test on an existing FastAPI app.

    Designed for the Databricks Apps target: pair with
    ``mount_mcp_endpoints`` so a deploy can self-verify that the agent answers
    and traces before declaring success.

    The route is registered synchronously (unlike ``mount_mcp_endpoints``'s
    startup-deferred MCP setup) so it is reachable immediately — including in
    a ``TestClient`` that never runs the lifespan. The blocking agent run
    lives in a plain ``def`` handler so FastAPI dispatches it on a threadpool
    rather than blocking the event loop.

    Usage::

        from apx_agent import mount_mcp_endpoints, mount_readyz

        mount_mcp_endpoints(app, agent)
        mount_readyz(app, agent)
    """
    from fastapi import Response

    @app.get("/readyz")
    def readyz() -> Response:
        import json

        checks: dict[str, Any] = {
            "llm": "fail",
            "tracing": "unavailable",
            "tools_registered": _count_tools(agent),
            "tool_exec": "skipped",
            "memory": getattr(agent, "_apx_memory_degraded", None) or "ok",
        }
        try:
            # Module-global lookup so monkeypatch.setattr resolves at call time.
            text, trace_id = _run_canned_probe(agent, model)
            checks["llm"] = "ok" if text else "fail"
            checks["tracing"] = "ok" if trace_id else "unavailable"

            mem_ok = checks["memory"] in ("ok", None)
            ready = checks["llm"] == "ok" and checks["tracing"] in ("ok", "unavailable") and mem_ok
            status = "ready" if ready else "degraded"
            return Response(
                content=json.dumps({"status": status, "checks": checks}),
                media_type="application/json",
                status_code=200 if ready else 503,
            )
        except Exception as exc:
            # Never 500 — readiness failures are a 503, not a crash.
            logger.warning("readyz self-test errored: %s", exc)
            return Response(
                content=json.dumps(
                    {"status": "degraded", "checks": checks, "error": str(exc)[:200]}
                ),
                media_type="application/json",
                status_code=503,
            )
