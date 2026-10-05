"""Native DurableAgentServer target, sharing graph execution with other hosts."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

from ._agents import BaseAgent, LlmAgent
from ._compile import compile_to_langgraph
from ._graph_turn import run_graph_turn


@dataclass(frozen=True)
class DurableHandlers:
    invoke: Callable[[Any, Any], Awaitable[dict[str, Any]]]


def _pending(graph: Any, config: dict[str, Any] | None) -> Any | None:
    if config is None:
        return None
    interrupts = graph.get_state(config).interrupts
    return interrupts[0].value if interrupts else None


def compile_durable_handlers(
    agent: BaseAgent,
    *,
    model: str,
    service_ws: Any,
    checkpointer: Any | None = None,
) -> DurableHandlers:
    """Build native message handlers; no MLflow request/response dependency.

    This first target is app-auth only. Its session keys belong to the app's
    agent, not individual users. The service client never fills a user slot.
    Recovery and token streaming are intentionally absent until implemented.
    """
    if service_ws is None:
        raise ValueError("durable_agent_server requires an explicit service_ws runtime binding")
    from ._runtime_targets import inspect_target

    inspect_target(agent, target="durable_agent_server", checkpointer=checkpointer).require_compatible()
    if checkpointer is not None and not isinstance(agent, LlmAgent):
        raise ValueError("Checkpoint sessions currently require LlmAgent")
    if getattr(agent, "memory_config", None) is not None or getattr(agent, "_apx_memory_store", None) is not None:
        raise ValueError("long_term_memory is not wired for durable_agent_server")

    def execute(value: Any, context: Any) -> dict[str, Any]:
        if getattr(context, "request_auth", None) is not None:
            raise ValueError("user_identity is not wired for durable_agent_server; use responses_agent")
        if getattr(context, "is_recovery", False):
            raise ValueError("recovery is not wired for durable_agent_server")
        if isinstance(value, list):
            value = {"messages": value}
        if not isinstance(value, dict) or set(value) - {"messages", "resume"}:
            raise ValueError("Native input must contain messages and optional resume; session_id belongs in the top-level invocation envelope")
        messages = value.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        # Durable invocation input is fresh chat input. Tool results and state
        # are produced by execution/checkpoints, never supplied by a caller.
        for message in messages:
            if not isinstance(message, dict) or set(message) - {"role", "content"}:
                raise ValueError("Each message must contain only role and content")
            if message.get("role") != "user" or not isinstance(message.get("content"), str):
                raise ValueError("Native input accepts user text messages only")
        resume = value.get("resume")
        if resume is not None and checkpointer is None:
            raise ValueError("Approval resume requires a checkpointed session")
        config = None
        if checkpointer is not None:
            session = context.session_id
            if not isinstance(session, str) or not session:
                raise ValueError("A checkpointed invocation requires a non-empty session_id")
            key = hashlib.sha256(json.dumps([getattr(agent, "_name", None), session]).encode()).hexdigest()
            config = {"configurable": {"thread_id": key, "actor_id": key}}
        from langchain_core.messages import HumanMessage

        graph = compile_to_langgraph(
            agent, ws=None, service_ws=service_ws, model=model,
            **({"checkpointer": checkpointer} if checkpointer is not None else {}),
        )
        turn = run_graph_turn(
            graph, agent, [HumanMessage(content=m["content"]) for m in messages],
            config=config, resume=resume, read_interrupt=_pending,
        )
        if turn.approval_required is not None:
            return {"status": "interrupted", "messages": [], "approval_required": turn.approval_required}
        return {"status": "completed", "messages": [m.model_dump(mode="json") for m in turn.messages]}

    async def invoke(value: Any, context: Any) -> dict[str, Any]:
        # Sync graph/checkpoint tools run outside the server's event loop.
        return await asyncio.to_thread(execute, value, context)

    return DurableHandlers(invoke=invoke)


def compile_to_durable_agent_server(
    agent: BaseAgent,
    *,
    model: str,
    service_ws: Any,
    checkpointer: Any | None = None,
    session_store: str | None = None,
) -> Any:
    """Create the optional SDK server without importing ResponsesAgent."""
    from ._runtime_targets import inspect_target

    inspect_target(agent, target="durable_agent_server", checkpointer=checkpointer,
                   session_store=session_store).require_compatible()
    try:
        from databricks_agentkit.runtime.app import DurableAgentServer
    except ImportError as exc:
        raise ImportError("durable_agent_server requires the optional SDK; install apx-agent[agentbricks] in the host project") from exc
    if session_store is not None:
        if service_ws is None:
            raise ValueError("session_store requires an explicit service_ws runtime binding")
        from databricks_agentkit.langgraph.session_store import DatabricksSessionStoreSaver

        checkpointer = DatabricksSessionStoreSaver(session_store, workspace_client=service_ws)
    handlers = compile_durable_handlers(agent, model=model, service_ws=service_ws, checkpointer=checkpointer)
    app = DurableAgentServer()
    if app.auth_policy.requires_user:
        raise ValueError("user_identity: the selected Agent Bricks manifest requires request-user auth, which this APX target does not wire yet")
    app.invoke(handlers.invoke)

    @app.get("/readyz")
    async def readyz() -> Any:
        import uuid
        from fastapi.responses import JSONResponse

        try:
            await app._runtime.runtime_store.get(invocation_id=str(uuid.uuid4()))
        except Exception:
            return JSONResponse(status_code=503, content={
                "status": "degraded", "checks": {"runtime_store": "unreachable"},
            })
        return {"status": "ready", "checks": {
            "runtime_store": "ok", "durable": app._runtime.is_durable,
            "agent_execution": "not_probed",
        }}

    return app
