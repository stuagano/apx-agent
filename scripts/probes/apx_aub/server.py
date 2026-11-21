"""Disposable apx-aub experiment. Never deploy this fault injector to a real App."""
from __future__ import annotations

import contextvars
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
import platform
from typing import Any

from databricks.sdk.errors import ResourceAlreadyExists
from databricks.sdk.service.workspace import ImportFormat
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

CONTEXT: contextvars.ContextVar[Any] = contextvars.ContextVar("aub_attempt")
AGENT_NAME = "aub-proof"


def upload_json(ws: Any, path: str, value: Any) -> None:
    """Create only: a duplicate must never overwrite the first mutation."""
    ws.workspace.upload(path, json.dumps(value, sort_keys=True).encode(),
                        format=ImportFormat.AUTO, overwrite=False)


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


def make_record(ws: Any, saver: Any, root: str, effect_key: str) -> Any:
    def record(value: str) -> str:
        """Create the harmless business record once using its stable business key."""
        if value != effect_key:
            raise ValueError("Only this experiment's business key is allowed")
        context = CONTEXT.get()
        attempt = {"invocation_id": context.invocation_id, "attempt": context.attempt,
                   "recovery": context.is_recovery, "session_id": context.session_id,
                   "effect_key": effect_key, "utc": datetime.now(timezone.utc).isoformat(),
                   "python": platform.python_version(), "versions": {
                       name: version(name) for name in (
                           "apx-agent", "databricks-agentbricks", "databricks-sdk", "langgraph",
                           "langgraph-checkpoint", "langchain", "langchain-core",
                       )
                   }}
        upload_json(ws, f"{root}/attempt-{context.attempt}.json", attempt)
        path = f"{root}/effect.json"
        mutation = {"effect_key": effect_key, "value": "harmless-test-mutation"}
        try:
            upload_json(ws, path, mutation)
            created = True
        except ResourceAlreadyExists:
            created = False
        if read_json(ws, path) != mutation:
            raise ValueError("Existing effect differs: reconciliation required")
        boundary = {**attempt, "created": created, "sink_readback": read_json(ws, path),
                    "checkpoint": checkpoint_summary(saver, context.session_id)}
        upload_json(ws, f"{root}/boundary-{context.attempt}.json", boundary)
        if not context.is_recovery:
            # Inside the tool: the write has succeeded and been independently read,
            # but no ToolMessage/result has been returned to LangGraph to checkpoint.
            if not created or boundary["checkpoint"]["tool_results"]:
                raise AssertionError("Not a fresh write-before-tool-result crash boundary")
            os._exit(86)
        return json.dumps({"effect_key": effect_key, "created": created}, sort_keys=True)

    return record


class ProbeModel(BaseChatModel):
    """Deterministic tool caller: no model endpoint, inference cost, or LLM variability."""
    effect_key: str

    @property
    def _llm_type(self) -> str:
        return "aub-deterministic-probe"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ProbeModel:
        return self

    def _generate(self, messages: Any, **kwargs: Any) -> ChatResult:
        if isinstance(messages[-1], ToolMessage):
            message = AIMessage(content=f"Recorded {messages[-1].content}")
        else:
            message = AIMessage(content="", tool_calls=[{
                "name": "record", "args": {"value": self.effect_key}, "id": "aub-call",
            }])
        message.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        return ChatResult(generations=[ChatGeneration(message=message)])


def create_app() -> Any:
    from databricks_agentkit import DurableAgentServer
    from databricks_agentkit.langgraph.session_store import DatabricksSessionStoreSaver
    from databricks_agentkit.runtime.workspace import workspace_client
    from apx_agent import LlmAgent
    from apx_agent import _compile
    from apx_agent._durable_agent import compile_durable_handlers

    # Required configuration; no fallback to another workspace, sink, or store.
    root = os.environ["APX_AUB_SINK_ROOT"]
    effect_key = os.environ["APX_AUB_EFFECT_KEY"]
    if os.environ["APX_AUB_ENABLE_CRASH"] != "disposable-only":
        raise ValueError("Explicit disposable fault-injection opt-in required")
    ws = workspace_client()
    saver = DatabricksSessionStoreSaver(os.environ["AGENT_SESSION_STORE"], workspace_client=ws)
    # Probe-only injection of the same deterministic model seam used by APX tests.
    _compile._build_chat_databricks = lambda *a, **k: ProbeModel(effect_key=effect_key)
    agent = LlmAgent(name=AGENT_NAME, tools=[make_record(ws, saver, root, effect_key)])
    handlers = compile_durable_handlers(agent, model="deterministic-probe", service_ws=ws,
                                        checkpointer=saver, recovery=True)
    app = DurableAgentServer()

    async def run(value: Any, context: Any) -> Any:
        token = CONTEXT.set(context)
        try:
            await context.emit({"type": "aub.attempt", "attempt": context.attempt,
                                "recovery": context.is_recovery, "effect_key": effect_key})
            return await handlers.invoke(value, context)
        finally:
            CONTEXT.reset(token)

    app.invoke(run)
    app.recover(run)
    return app
