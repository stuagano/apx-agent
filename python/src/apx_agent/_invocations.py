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

  The route ALSO accepts the MLflow ResponsesAgent request shape
  (``{"input": [...]}``) — the deployed Apps runtime's contract, and what
  ``RemoteDatabricksAgent._call_via_http`` posts. Such a request is mapped
  onto ``messages``, executed identically, and answered in the Responses
  reply shape (``{"output": [{"content": [{"text": ...}]}]}``) so the poster's
  parser works unchanged (#438). A body with NEITHER key is a 422 — never a
  silently-empty conversation.

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

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from ._agents import BaseAgent
from ._async_bridge import make_async_stream
from ._audit import AuditAttrs, stamp_caller_correlation
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


def _input_items_to_chat_messages(input_items: Any) -> list[dict[str, Any]]:
    """Map a Responses-shape ``input`` payload to ChatAgent ``messages`` dicts.

    Handles the two forms clients send: a bare string (one user turn) and a
    list of role/content items where content is a string or a list of
    ``{"type": ..., "text": ...}`` parts. Items without extractable text
    (function_call / reasoning items) are skipped — the ChatAgent conversation
    is the text turns.
    """
    if isinstance(input_items, str):
        return [{"role": "user", "content": input_items}]
    if not isinstance(input_items, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in input_items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list):
            parts = [
                p["text"]
                for p in content
                if isinstance(p, dict) and isinstance(p.get("text"), str)
            ]
            content = "\n".join(parts)
        if not isinstance(content, str) or not content:
            continue
        role = item.get("role")
        if role not in ("system", "user", "assistant"):
            role = "user"
        messages.append({"role": role, "content": content})
    return messages


def _responses_shaped_reply(response: Any) -> dict[str, Any]:
    """Re-shape a ``ChatAgentResponse`` as a ResponsesAgent-style reply.

    Callers that POSTed the Responses shape (``{"input": [...]}``) — e.g.
    ``RemoteDatabricksAgent._call_via_http`` — parse
    ``data["output"][0]["content"][0]["text"]``. Hand them the final assistant
    text in exactly that shape (#438).
    """
    text = ""
    msg_id = "msg-0"
    for m in response.messages:
        if m.role == "assistant" and isinstance(m.content, str) and m.content:
            text = m.content
            if m.id:
                msg_id = m.id
    return {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "id": msg_id,
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ]
    }


def mount_invocations_route(
    app: FastAPI,
    agent: BaseAgent,
    config: "AgentConfig",
    conversation_store: Any | None = None,
    checkpointer: Any | None = None,
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
        conversation_store: Optional ``ConversationStore`` for multi-turn memory.
            When provided, conversation history is persisted across requests keyed
            by the ``conversation_id`` in ``context``.
        checkpointer: Optional durable LangGraph checkpointer (e.g. a Lakebase
            ``PostgresSaver``). When provided it owns thread state, so a pending
            mid-turn approval survives a restart; when ``None`` the ChatAgent
            defaults to an in-process ``InMemorySaver``.

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

    # agent_id binds created conversations to the name agent-filtered readers
    # (dev-UI History panel) look up.
    chat_agent = chat_agent_for(
        agent, model=config.model, conversation_store=conversation_store,
        agent_id=config.name, checkpointer=checkpointer,
    )

    @app.post("/invocations", include_in_schema=False)
    async def invocations(request: Request) -> Any:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

        # Dual-shape acceptance (#438): "messages" is the native ChatAgent
        # contract; "input" is the ResponsesAgent contract the deployed Apps
        # runtime (and RemoteDatabricksAgent) speaks. Neither present is a
        # malformed request — reject it loudly rather than silently running
        # an empty conversation.
        messages_present = body.get("messages") is not None
        if not messages_present and body.get("input") is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Request body must carry either 'messages' (MLflow "
                    "ChatAgent shape: {\"messages\": [{\"role\": ..., "
                    "\"content\": ...}]}) or 'input' (MLflow ResponsesAgent "
                    "shape: {\"input\": [...]}); got neither."
                ),
            )
        if messages_present:
            messages_raw = body["messages"]
        else:
            messages_raw = _input_items_to_chat_messages(body["input"])
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
                AuditAttrs.AGENT_NAME: config.name,
                "apx.streaming": stream,
                "apx.user_scoped": bool(custom_inputs.get("user_token")),
                "apx.message_count": len(messages),
            },
        ) as span:
            # Cross-agent correlation (#443): when the caller sent a
            # traceparent / x-apx-caller, tag this trace so it joins the
            # caller's trace on apx.outbound.trace_id. Absent headers → no-op.
            stamp_caller_correlation(span, request.headers)
            if stream:
                # Run the sync predict_stream on a dedicated worker thread (one
                # thread for the whole generator, so OTel span attach/detach stay
                # paired) instead of iterating it on the event loop. Streaming
                # always emits ChatAgentChunk frames — RemoteDatabricksAgent's
                # SSE parser understands the chunk-delta dict (#438).
                return StreamingResponse(
                    make_async_stream(
                        lambda _req: _stream_chunks(chat_agent, messages, custom_inputs)
                    )(None),
                    media_type="text/event-stream",
                )

            # Offload the sync agent turn to a worker thread so one slow tool
            # call doesn't freeze the event loop (and every other request).
            response = await asyncio.to_thread(
                chat_agent.predict, messages, custom_inputs=custom_inputs or None
            )
            if not messages_present:
                # The caller spoke Responses — answer in the shape it parses.
                return _responses_shaped_reply(response)
            return response.model_dump()

    logger.info(
        "Mounted /invocations (MLflow ChatAgent protocol) for agent %r",
        config.name,
    )
    return True


def _stream_chunks(
    chat_agent: Any,
    messages: list,
    custom_inputs: dict[str, Any],
):
    """SSE generator — yields one ``data: <ChatAgentChunk JSON>`` per chunk.

    A **sync** generator: it runs to completion on one worker thread (via
    :func:`~apx_agent._async_bridge.make_async_stream`) so ``predict_stream``
    never blocks the event loop and the OTel span context stays on one thread.
    Each chunk is its own SSE event terminated by a blank line.
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
    conversation_store: Any | None = None,
    checkpointer: Any | None = None,
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
        conversation_store: Optional ``ConversationStore`` for multi-turn memory.
        checkpointer: Optional durable LangGraph checkpointer (e.g. a Lakebase
            ``PostgresSaver``) so a pending mid-turn approval survives a restart;
            ``None`` falls back to an in-process ``InMemorySaver``.

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
            conversation_store=conversation_store,
            executor=getattr(config, "executor", "langgraph"),
            # Bind the SAME name the dev-UI History panel filters by, or
            # conversations created here stay invisible to it.
            agent_id=config.name,
            checkpointer=checkpointer,
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
                AuditAttrs.AGENT_NAME: config.name,
                "apx.streaming": stream,
                "apx.user_scoped": bool(custom_inputs.get("user_token")),
                "apx.input_items": len(input_items) if isinstance(input_items, list) else 1,
            },
        ) as span:
            # Cross-agent correlation (#443) — same stamp as /invocations.
            stamp_caller_correlation(span, request.headers)
            if stream:
                return StreamingResponse(
                    make_async_stream(lambda _req: _stream_response_events(_stream_fn, req))(None),
                    media_type="text/event-stream",
                )
            result = await asyncio.to_thread(_invoke_fn, req)
            return result.model_dump() if hasattr(result, "model_dump") else result

    logger.info(
        "Mounted /responses (MLflow ResponsesAgent protocol) for agent %r",
        config.name,
    )
    return True


def _stream_response_events(stream_fn: Any, req: Any):
    """SSE generator for the ``/responses`` endpoint.

    A **sync** generator run on one worker thread (via
    :func:`~apx_agent._async_bridge.make_async_stream`) so the ResponsesAgent
    stream never blocks the event loop. Each ``ResponsesAgentStreamEvent`` is
    serialised as a ``data:`` SSE frame.
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
