"""Mount the MLflow ChatAgent ``/invocations`` route onto a FastAPI app.

This bridges apx-agent's declarative DSL to the **supported** Mosaic AI agent
serving protocol. The ``/invocations`` endpoint is what AI Playground, the
Review App, and Model Serving expect. Mounting it makes any apx-agent app a
first-class Mosaic AI Agent at the wire level — no need to log to MLflow
first, no detour through Model Serving.

Wire shape (the contract):

  POST /invocations
  {
    "messages": [{"role": "user", "content": "..."}],
    "custom_inputs": {},
    "context": {"conversation_id": "...", "user_id": "..."},
    "stream": false
  }

  Response (200, application/json, non-streaming):
  {
    "messages": [{"role": "assistant", "content": "...", "id": "..."}],
    "finish_reason": null,
    "custom_outputs": {},
    "usage": {}
  }

  Response (200, text/event-stream, streaming):
  data: {<ChatAgentChunk JSON>}

  data: {<ChatAgentChunk JSON>}

  ...

OBO auth bridge:

  When running inside Databricks Apps, the proxy injects
  ``X-Forwarded-Access-Token``. If present, this route forwards it as
  ``custom_inputs["user_token"]`` (and ``DATABRICKS_HOST`` as
  ``workspace_host``), so the ChatAgent's per-request compile builds a
  user-scoped ``WorkspaceClient``. Every tool then runs as the calling user.
  This is THE seam that preserves enterprise user-scope auth across the move
  to the supported runtime.

Requires the ``langgraph`` and ``eval`` extras::

    pip install 'apx-agent[langgraph,eval]'
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from ._agents import BaseAgent
from ._mlflow_tracing import safe_span

if TYPE_CHECKING:
    from ._models import AgentConfig

logger = logging.getLogger(__name__)


def mount_invocations_route(
    app: FastAPI,
    agent: BaseAgent,
    config: "AgentConfig",
) -> bool:
    """Mount POST ``/invocations`` (MLflow ChatAgent protocol) onto ``app``.

    Args:
        app: The FastAPI app to mount the route onto.
        agent: The apx-agent ``BaseAgent`` to serve. Currently must be a type
            ``compile_to_langgraph`` supports (``LlmAgent``,
            ``SequentialAgent``); unsupported types raise on first request,
            not at mount.
        config: The ``AgentConfig`` — ``config.model`` is the serving endpoint
            the compiled graph uses for LLM calls.

    Returns:
        ``True`` if the route was mounted; ``False`` if the optional
        ``langgraph`` / ``eval`` extras are missing (warning is logged).
        Mounting is best-effort: a missing dep at startup never breaks the
        whole app.
    """
    try:
        from ._chat_agent import chat_agent_for
        from mlflow.types.agent import ChatAgentMessage
    except ImportError as e:
        logger.warning(
            "Cannot mount /invocations: %s. "
            "Install apx-agent[langgraph,eval] to enable the MLflow ChatAgent route.",
            e,
        )
        return False

    chat_agent = chat_agent_for(agent, model=config.model)

    @app.post("/invocations", include_in_schema=False)
    async def invocations(request: Request) -> Any:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

        messages_raw = body.get("messages", []) or []
        custom_inputs: dict[str, Any] = dict(body.get("custom_inputs") or {})
        stream = bool(body.get("stream", False))

        # --- OBO header bridge ---------------------------------------------
        # Databricks Apps injects X-Forwarded-Access-Token. Forward it as
        # custom_inputs["user_token"] so the per-request compile builds a
        # user-scoped WorkspaceClient. Caller-provided user_token wins.
        forwarded_token = request.headers.get("X-Forwarded-Access-Token")
        if forwarded_token and "user_token" not in custom_inputs:
            custom_inputs["user_token"] = forwarded_token
            host = os.environ.get("DATABRICKS_HOST")
            if host and "workspace_host" not in custom_inputs:
                custom_inputs["workspace_host"] = host

        try:
            messages = [ChatAgentMessage(**m) for m in messages_raw]
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid ChatAgentMessage in messages: {exc}",
            )

        with safe_span(
            "POST /invocations",
            span_type="CHAIN",
            attributes={
                "http.route": "/invocations",
                "apx.agent_name": config.name,
                "apx.streaming": stream,
                "apx.user_scoped": bool(custom_inputs.get("user_token")),
                "apx.message_count": len(messages),
            },
        ):
            if stream:
                return StreamingResponse(
                    _stream_chunks(chat_agent, messages, custom_inputs),
                    media_type="text/event-stream",
                )

            response = chat_agent.predict(
                messages, custom_inputs=custom_inputs or None
            )
            return response.model_dump()

    logger.info(
        "Mounted /invocations (MLflow ChatAgent protocol) for agent %r",
        config.name,
    )
    return True


async def _stream_chunks(
    chat_agent: Any,
    messages: list,
    custom_inputs: dict[str, Any],
):
    """SSE generator — yields one ``data: <ChatAgentChunk JSON>`` per chunk.

    ``predict_stream`` is a sync generator, but FastAPI's ``StreamingResponse``
    accepts an async one. We bridge by yielding from a sync iterator inside an
    async function. Each chunk is its own SSE event terminated by a blank line.
    """
    custom = custom_inputs or None
    try:
        for chunk in chat_agent.predict_stream(messages, custom_inputs=custom):
            payload = chunk.model_dump_json()
            yield f"data: {payload}\n\n"
    except Exception as exc:
        logger.exception("Error during /invocations stream")
        err = json.dumps({"error": type(exc).__name__, "message": str(exc)})
        yield f"event: error\ndata: {err}\n\n"
