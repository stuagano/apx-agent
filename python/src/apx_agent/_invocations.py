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

Requires the ``eval`` extra (mlflow)::

    pip install 'apx-agent[eval]'
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from ._agents import BaseAgent
from ._mlflow_tracing import safe_span

if TYPE_CHECKING:
    from ._models import AgentConfig

logger = logging.getLogger(__name__)


def _last_user_text(input_items: Any, max_len: int = 120) -> str:
    """Extract the last user message text from a /responses or /invocations input."""
    if isinstance(input_items, str):
        text = input_items
    elif isinstance(input_items, list):
        text = ""
        for item in reversed(input_items):
            if not isinstance(item, dict):
                continue
            if item.get("role") not in ("user", None):
                continue
            content = item.get("content", "")
            if isinstance(content, str):
                text = content
                break
            if isinstance(content, list):
                parts = [
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                text = " ".join(parts).strip()
                if text:
                    break
    else:
        return ""
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def mount_invocations_route(
    app: FastAPI,
    agent: BaseAgent,
    config: "AgentConfig",
    session_store: Any | None = None,
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
        session_store: Optional ``SessionStore`` for multi-turn memory. When
            provided, conversation history is persisted across requests keyed
            by the ``conversation_id`` in ``context``.

    Returns:
        ``True`` if the route was mounted; ``False`` if the ``eval`` extra
        (mlflow) is missing (warning is logged). Mounting is best-effort: a
        missing dep at startup never breaks the whole app.
    """
    try:
        from ._chat_agent import chat_agent_for
        from mlflow.types.agent import ChatAgentMessage
    except ImportError as e:
        logger.warning(
            "Cannot mount /invocations: %s. "
            "Install apx-agent[eval] to enable the MLflow ChatAgent route.",
            e,
        )
        return False

    chat_agent = chat_agent_for(agent, model=config.model, session_store=session_store)

    @app.post("/invocations", include_in_schema=False)
    async def invocations(request: Request) -> Any:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

        messages_raw = body.get("messages", []) or []
        custom_inputs: dict[str, Any] = dict(body.get("custom_inputs") or {})
        stream = bool(body.get("stream", False))

        user_text = _last_user_text(messages_raw)
        if user_text:
            logger.info("[user] %s", user_text)

        # --- context.conversation_id → custom_inputs.session_id bridge ----
        # AI Playground and Model Serving send the session key in the standard
        # MLflow ChatAgent ``context`` field. The ChatAgent reads it from
        # ``custom_inputs["session_id"]``. Bridge here so multi-turn memory
        # works without callers knowing the internal key name.
        context_raw: dict[str, Any] = body.get("context") or {}
        if context_raw.get("conversation_id"):
            custom_inputs.setdefault("session_id", context_raw["conversation_id"])

        # --- OBO header bridge ---------------------------------------------
        # Unified extractor handles both runtime conventions:
        #   - custom_inputs.user_token (caller-supplied; wins)
        #   - X-Forwarded-Access-Token header (Apps runtime injection)
        # See ``apx_agent._obo.extract_obo_headers`` for the precedence rule.
        from ._obo import extract_obo_headers

        obo = extract_obo_headers(
            custom_inputs=custom_inputs, headers=request.headers
        )
        for key, val in obo.items():
            custom_inputs.setdefault(key, val)

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
        # Stay within the data-only SSE contract (see module docstring): emit
        # the error as a ``data:`` frame rather than a named ``event: error``
        # frame, so consumers parsing only ``data:`` lines still observe it.
        err = json.dumps({"error": type(exc).__name__, "message": str(exc)})
        yield f"data: {err}\n\n"


def mount_responses_route(
    app: FastAPI,
    agent: BaseAgent,
    config: "AgentConfig",
    session_store: Any | None = None,
) -> bool:
    """Mount POST ``/responses`` (MLflow ResponsesAgent protocol) onto ``app``.

    This is the ResponsesAgent wire shape — ``{"input": [...], "stream": bool}``
    — used by AI Playground and the Apps runtime. Adding it to the model-serving
    target (``create_app``) closes the gap that broke the dev-UI playground for
    projects that deploy to model serving.

    Args:
        app: The FastAPI app to mount the route onto.
        agent: The apx-agent ``BaseAgent`` to serve.
        config: The ``AgentConfig`` — ``config.model`` is the serving endpoint.
        session_store: Optional ``SessionStore`` for multi-turn memory.

    Returns:
        ``True`` if the route was mounted; ``False`` if the ``eval`` extra
        (mlflow >= 3.x) is missing or compilation fails (warning is logged).
    """
    try:
        from ._responses_agent import compile_to_responses_agent
    except (ImportError, NotImplementedError) as exc:
        logger.warning(
            "Cannot mount /responses: %s. "
            "Install apx-agent[eval] to enable the ResponsesAgent route.",
            exc,
        )
        return False

    try:
        _invoke_fn, _stream_fn = compile_to_responses_agent(
            agent,
            model=config.model,
            session_store=session_store,
            executor=getattr(config, "executor", "langgraph"),
        )
    except Exception as exc:
        logger.warning("Cannot compile ResponsesAgent for /responses: %s", exc)
        return False

    @app.post("/responses", include_in_schema=False)
    async def responses_endpoint(request: Request) -> Any:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

        input_items = body.get("input", [])
        stream = bool(body.get("stream", False))
        custom_inputs: dict[str, Any] = dict(body.get("custom_inputs") or {})

        user_text = _last_user_text(input_items)
        if user_text:
            logger.info("[user] %s", user_text)

        from ._obo import extract_obo_headers

        obo = extract_obo_headers(custom_inputs=custom_inputs, headers=request.headers)
        for key, val in obo.items():
            custom_inputs.setdefault(key, val)

        try:
            from ._responses_agent import _import_responses_types

            req = _import_responses_types().request_cls(
                input=input_items, custom_inputs=custom_inputs or None
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid input: {exc}")

        with safe_span(
            "POST /responses",
            span_type="CHAIN",
            attributes={
                "http.route": "/responses",
                "apx.agent_name": config.name,
                "apx.streaming": stream,
                "apx.user_scoped": bool(custom_inputs.get("user_token")),
                "apx.input_items": len(input_items) if isinstance(input_items, list) else 1,
            },
        ):
            if stream:
                return StreamingResponse(
                    _stream_response_events(_stream_fn, req),
                    media_type="text/event-stream",
                )
            result = _invoke_fn(req)
            return result.model_dump() if hasattr(result, "model_dump") else result

    logger.info(
        "Mounted /responses (MLflow ResponsesAgent protocol) for agent %r",
        config.name,
    )
    return True


async def _stream_response_events(stream_fn: Any, req: Any):
    """SSE generator for the ``/responses`` endpoint.

    Wraps a sync ``ResponsesAgent`` streaming generator in an async context.
    Each ``ResponsesAgentStreamEvent`` is serialised as a ``data:`` SSE frame.
    """
    try:
        for event in stream_fn(req):
            payload = (
                event.model_dump_json()
                if hasattr(event, "model_dump_json")
                else json.dumps(event)
            )
            yield f"data: {payload}\n\n"
    except Exception as exc:
        logger.exception("Error during /responses stream")
        err = json.dumps({"type": "error", "error": type(exc).__name__, "message": str(exc)})
        yield f"data: {err}\n\n"
