"""Disposable apx-4ag disconnect probe. Never install this on a customer App.

A user message containing KILL sleeps, records the pre-result checkpoint, then
terminates the worker with os._exit(86). A HOLD message sleeps and returns.
Closing an SSE client is not a cancel, and this process does not register one.
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

CONTEXT: contextvars.ContextVar[Any] = contextvars.ContextVar("ag4_attempt")
AGENT_NAME = "4ag-proof"
HOLD = "4ag-hold"
KILL = "4ag-worker-loss"
TOOL_CALL_ID = "4ag-call"


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


def _required_seconds(name: str) -> int:
    raw = os.environ[name]
    seconds = int(raw)
    if seconds < 1:
        raise ValueError(f"{name} must be a positive number of seconds")
    return seconds


def make_pace(ws: Any, saver: Any, root: str) -> Any:
    def pace(value: str) -> str:
        """Record the attempt, hold, and lose the worker only for KILL before return."""
        if value not in (HOLD, KILL):
            raise ValueError("Only the disposable hold or worker-loss marker is allowed")
        context = CONTEXT.get()
        attempt = {
            "invocation_id": context.invocation_id,
            "attempt": context.attempt,
            "recovery": context.is_recovery,
            "session_id": context.session_id,
            "marker": value,
            "utc": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "versions": _versions(),
        }
        upload_json(ws, f"{root}/attempt-{context.attempt}-{value}.json", attempt)
        boundary = {**attempt, "checkpoint": checkpoint_summary(saver, context.session_id)}
        upload_json(ws, f"{root}/boundary-{context.attempt}-{value}.json", boundary)
        if value == KILL and not context.is_recovery:
            summary = boundary["checkpoint"]
            if summary["tool_results"] or summary["message_types"] != ["human", "ai"]:
                raise AssertionError("Not a fresh pre-result worker-loss boundary")
            time.sleep(_required_seconds("APX_4AG_KILL_DELAY_SECONDS"))
            os._exit(86)
        time.sleep(_required_seconds("APX_4AG_HOLD_SECONDS"))
        return json.dumps({
            "marker": value,
            "invocation_id": context.invocation_id,
            "session_id": context.session_id,
            "attempt": context.attempt,
            "recovery": bool(context.is_recovery),
            "continued": True,
        }, sort_keys=True)

    return pace


class ProbeModel(BaseChatModel):
    """Deterministic tool caller: no model endpoint and no inference cost."""

    @property
    def _llm_type(self) -> str:
        return "4ag-deterministic-probe"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ProbeModel:
        return self

    def _generate(self, messages: Any, **kwargs: Any) -> ChatResult:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            message = AIMessage(content=f"Continued {last.content}")
        elif KILL in last.content:
            message = AIMessage(content="", tool_calls=[{
                "name": "pace", "args": {"value": KILL}, "id": TOOL_CALL_ID,
            }])
        elif HOLD in last.content:
            message = AIMessage(content="", tool_calls=[{
                "name": "pace", "args": {"value": HOLD}, "id": TOOL_CALL_ID,
            }])
        else:
            message = AIMessage(content="pong")
        message.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        return ChatResult(generations=[ChatGeneration(message=message)])


def create_app() -> Any:
    if os.environ["APX_4AG_ENABLE_CRASH"] != "disposable-only":
        raise ValueError("Explicit disposable fault-injection opt-in required")
    root = os.environ["APX_4AG_DIAG_ROOT"]
    _required_seconds("APX_4AG_HOLD_SECONDS")
    _required_seconds("APX_4AG_KILL_DELAY_SECONDS")
    from databricks_agentkit import DurableAgentServer
    from databricks_agentkit.langgraph.session_store import DatabricksSessionStoreSaver
    from databricks_agentkit.runtime.workspace import workspace_client
    from apx_agent import LlmAgent
    from apx_agent import _compile
    from apx_agent._durable_agent import compile_durable_handlers

    ws = workspace_client()
    saver = DatabricksSessionStoreSaver(os.environ["AGENT_SESSION_STORE"], workspace_client=ws)
    _compile._build_chat_databricks = lambda *a, **k: ProbeModel()
    agent = LlmAgent(name=AGENT_NAME, tools=[make_pace(ws, saver, root)])
    handlers = compile_durable_handlers(
        agent, model="deterministic-probe", service_ws=ws, checkpointer=saver, recovery=True,
    )
    app = DurableAgentServer()

    async def run(value: Any, context: Any) -> Any:
        token = CONTEXT.set(context)
        try:
            await context.emit({
                "type": "4ag.attempt",
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
