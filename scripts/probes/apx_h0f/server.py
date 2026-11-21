"""Disposable apx-h0f worker-loss probe. Never install this on a customer App.

The first call whose user text contains MARKER terminates the worker with
os._exit(86) after the managed checkpoint has the tool call and before the tool
returns. Recovery of that same invocation is the only path that returns.
"""
from __future__ import annotations

import contextvars
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
import platform
from typing import Any

from databricks.sdk.service.workspace import ImportFormat
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

CONTEXT: contextvars.ContextVar[Any] = contextvars.ContextVar("h0f_attempt")
AGENT_NAME = "h0f-proof"
MARKER = "h0f-worker-loss"
TOOL_CALL_ID = "h0f-call"


def upload_json(ws: Any, path: str, value: Any) -> None:
    """Create only, so a retry cannot overwrite the first attempt's diagnostic."""
    ws.workspace.upload(
        path, json.dumps(value, sort_keys=True).encode(), format=ImportFormat.AUTO, overwrite=False,
    )


def read_json(ws: Any, path: str) -> Any:
    with ws.workspace.download(path) as stream:
        return json.load(stream)


def checkpoint_summary(saver: Any, session_id: str) -> dict[str, Any]:
    from apx_agent._durable_agent import session_thread_config

    saved = saver.get_tuple(session_thread_config(AGENT_NAME, session_id, None))
    if saved is None:
        raise AssertionError("Expected a persisted pre-tool checkpoint")
    messages = saved.checkpoint["channel_values"].get("messages", [])
    return {
        "checkpoint_id": saved.checkpoint["id"],
        "metadata": saved.metadata,
        "message_types": [m.type for m in messages],
        "tool_results": [m.content for m in messages if isinstance(m, ToolMessage)],
        "pending_write_channels": [write[1] for write in saved.pending_writes or []],
    }


def _versions() -> dict[str, str]:
    return {
        name: version(name) for name in (
            "apx-agent", "databricks-agentbricks", "databricks-sdk", "langgraph",
            "langgraph-checkpoint", "langchain", "langchain-core",
        )
    }


def make_mark(ws: Any, saver: Any, root: str) -> Any:
    def mark(value: str) -> str:
        """Record the attempt, then lose the worker before the first result commits."""
        if value != MARKER:
            raise ValueError("Only the disposable worker-loss marker is allowed")
        context = CONTEXT.get()
        attempt = {
            "invocation_id": context.invocation_id,
            "attempt": context.attempt,
            "recovery": context.is_recovery,
            "session_id": context.session_id,
            "marker": MARKER,
            "utc": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "versions": _versions(),
        }
        upload_json(ws, f"{root}/attempt-{context.attempt}.json", attempt)
        boundary = {
            **attempt,
            "checkpoint": checkpoint_summary(saver, context.session_id),
        }
        upload_json(ws, f"{root}/boundary-{context.attempt}.json", boundary)
        if not context.is_recovery:
            # The tool call is checkpointed; its result is not, and this return never happens.
            summary = boundary["checkpoint"]
            if summary["tool_results"] or summary["message_types"] != ["human", "ai"]:
                raise AssertionError("Not a fresh pre-result worker-loss boundary")
            os._exit(86)
        return json.dumps({
            "marker": MARKER,
            "invocation_id": context.invocation_id,
            "session_id": context.session_id,
            "attempt": context.attempt,
            "recovery": True,
        }, sort_keys=True)

    return mark


class ProbeModel(BaseChatModel):
    """Deterministic tool caller: no model endpoint and no inference cost."""

    @property
    def _llm_type(self) -> str:
        return "h0f-deterministic-probe"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ProbeModel:
        return self

    def _generate(self, messages: Any, **kwargs: Any) -> ChatResult:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            message = AIMessage(content=f"Continued {last.content}")
        elif MARKER in last.content:
            message = AIMessage(content="", tool_calls=[{
                "name": "mark", "args": {"value": MARKER}, "id": TOOL_CALL_ID,
            }])
        else:
            message = AIMessage(content="pong")
        message.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        return ChatResult(generations=[ChatGeneration(message=message)])


def create_app() -> Any:
    if os.environ["APX_H0F_ENABLE_CRASH"] != "disposable-only":
        raise ValueError("Explicit disposable fault-injection opt-in required")
    root = os.environ["APX_H0F_DIAG_ROOT"]
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
                "type": "h0f.attempt",
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
