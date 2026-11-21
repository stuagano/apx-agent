"""Disposable apx-pzd trace probe. Never install this on a customer App.

A user message containing the worker-loss marker records the active MLflow
trace, ends that open span, flushes the exporter, then terminates the worker
with os._exit(86). Recovery stamps a second trace and returns. This process
does not continue an inbound traceparent.
"""
from __future__ import annotations

import contextvars
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
import platform
import time
from typing import Any

from databricks.sdk.service.workspace import ImportFormat
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

CONTEXT: contextvars.ContextVar[Any] = contextvars.ContextVar("pzd_attempt")
AGENT_NAME = "pzd-proof"
KILL = "pzd-worker-loss"
TOOL_CALL_ID = "pzd-call"


def upload_json(ws: Any, path: str, value: Any) -> None:
    """Create only, so a later attempt cannot erase the first boundary."""
    ws.workspace.upload(
        path, json.dumps(value, sort_keys=True).encode(), format=ImportFormat.AUTO, overwrite=False,
    )


def checkpoint_summary(saver: Any, session_id: str) -> dict[str, Any]:
    from apx_agent._durable_agent import session_thread_config

    saved = saver.get_tuple(session_thread_config(AGENT_NAME, session_id, None))
    if saved is None:
        raise AssertionError("Expected a persisted pre-tool checkpoint")
    messages = saved.checkpoint["channel_values"].get("messages", [])
    return {
        "checkpoint_id": saved.checkpoint["id"],
        "message_types": [message.type for message in messages],
        "tool_results": [message.content for message in messages if isinstance(message, ToolMessage)],
    }


def _versions() -> dict[str, str]:
    names = (
        "apx-agent", "databricks-agentbricks", "databricks-sdk", "mlflow",
        "langgraph", "langgraph-checkpoint", "langchain", "langchain-core",
    )
    found: dict[str, str] = {}
    for name in names:
        try:
            found[name] = version(name)
        except Exception:
            continue
    return found


def export_open_trace() -> dict[str, Any]:
    """End and flush the span that is still open inside the tool.

    ``safe_span`` stays open until the tool returns. ``os._exit`` skips that
    return, so the exporter would not see the first attempt without this.
    """
    import mlflow

    recorded: dict[str, Any] = {"trace_id": None, "ended": False, "flushed": False}
    recorded["trace_id"] = mlflow.get_active_trace_id()
    span = mlflow.get_current_active_span()
    if span is not None:
        span.end()
        recorded["ended"] = True
    mlflow.flush_trace_async_logging()
    recorded["flushed"] = True
    return recorded


def make_mark(ws: Any, saver: Any, root: str) -> Any:
    def mark(value: str) -> str:
        """Record the attempt, export the open trace, and lose the worker once."""
        if value != KILL:
            raise ValueError("Only the disposable worker-loss marker is allowed")
        context = CONTEXT.get()
        attempt: dict[str, Any] = {
            "invocation_id": context.invocation_id,
            "attempt": context.attempt,
            "recovery": context.is_recovery,
            "session_id": context.session_id,
            "utc": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "versions": _versions(),
        }
        try:
            import mlflow

            attempt["active_trace_id"] = mlflow.get_active_trace_id()
        except Exception as exc:
            attempt["active_trace_error"] = type(exc).__name__
        upload_json(ws, f"{root}/attempt-{context.attempt}.json", attempt)
        boundary = {**attempt, "checkpoint": checkpoint_summary(saver, context.session_id)}
        upload_json(ws, f"{root}/boundary-{context.attempt}.json", boundary)
        if not context.is_recovery:
            summary = boundary["checkpoint"]
            if summary["tool_results"] or summary["message_types"] != ["human", "ai"]:
                raise AssertionError("Not a fresh pre-result worker-loss boundary")
            exported = export_open_trace()
            upload_json(ws, f"{root}/export-{context.attempt}.json", {**attempt, "export": exported})
            time.sleep(3)
            os._exit(86)
        return json.dumps({
            "invocation_id": context.invocation_id,
            "session_id": context.session_id,
            "attempt": context.attempt,
            "recovery": True,
            "continued": True,
            "active_trace_id": attempt.get("active_trace_id"),
        }, sort_keys=True)

    return mark


class ProbeModel(BaseChatModel):
    """Deterministic tool caller: no model endpoint and no inference cost."""

    @property
    def _llm_type(self) -> str:
        return "pzd-deterministic-probe"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ProbeModel:
        return self

    def _generate(self, messages: Any, **kwargs: Any) -> ChatResult:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            message = AIMessage(content=f"Continued {last.content}")
        elif KILL in last.content:
            message = AIMessage(content="", tool_calls=[{
                "name": "mark", "args": {"value": KILL}, "id": TOOL_CALL_ID,
            }])
        else:
            message = AIMessage(content="pong")
        message.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        return ChatResult(generations=[ChatGeneration(message=message)])


def create_app() -> Any:
    if os.environ["APX_PZD_ENABLE_CRASH"] != "disposable-only":
        raise ValueError("Explicit disposable fault-injection opt-in required")
    root = os.environ["APX_PZD_DIAG_ROOT"]
    from databricks_agentkit import DurableAgentServer
    from databricks_agentkit.langgraph.session_store import DatabricksSessionStoreSaver
    from databricks_agentkit.runtime.workspace import workspace_client
    from apx_agent import LlmAgent
    from apx_agent import _compile
    from apx_agent._durable_agent import compile_durable_handlers

    ws = workspace_client()
    saver = DatabricksSessionStoreSaver(os.environ["AGENT_SESSION_STORE"], workspace_client=ws)
    _compile._build_chat_databricks = lambda *a, **k: ProbeModel()
    agent = LlmAgent(name=AGENT_NAME, tools=[make_mark(ws, saver, root)])
    handlers = compile_durable_handlers(
        agent, model="deterministic-probe", service_ws=ws, checkpointer=saver, recovery=True,
    )
    app = DurableAgentServer()

    async def run(value: Any, context: Any) -> Any:
        token = CONTEXT.set(context)
        try:
            await context.emit({
                "type": "pzd.attempt",
                "attempt": context.attempt,
                "recovery": context.is_recovery,
                "invocation_id": context.invocation_id,
                "session_id": context.session_id,
            })
            return await handlers.invoke(value, context)
        finally:
            CONTEXT.reset(token)

    app.invoke(run)
    app.recover(run)
    return app
