"""Compile an apx-agent ``BaseAgent`` to the Databricks Apps ``ResponsesAgent`` contract.

This is the **Apps-native peer** of :func:`apx_agent._chat_agent.compile_to_chat_agent`.
Same agent definition, different runtime contract:

  * **Model Serving** (ChatAgent): ``mlflow.pyfunc.log_model`` + UC + container
    build queue + ``databricks.agents.deploy``. Wire shape is ``ChatAgentRequest``
    (messages-style). See ``_chat_agent.py``.
  * **Databricks Apps** (this module): ``mlflow.genai.agent_server`` decorators
    on plain functions, deployed via ``databricks bundle deploy && bundle run``.
    Wire shape is ``ResponsesAgentRequest`` (input items + custom_inputs).

Author writes the agent once. The ``--target`` flag picks the runtime at deploy
time. The single load-bearing requirement is that **OBO header passthrough is
identical** — both runtimes route through :func:`apx_agent._obo.extract_obo_headers`,
so a tool sees the calling user's WorkspaceClient regardless of which runtime
served the request.

Decorator-vs-function tuple shape
---------------------------------

``mlflow.genai.agent_server.invoke()`` and ``stream()`` are *register-on-import*
decorators — each can be applied to one function per module, and applying them
installs the handler globally in the ``AgentServer`` registry. That makes them
unsafe to call inside this compile function (calling twice in the same process
would clobber the prior registration; calling in a test would leak global state).

So :func:`compile_to_responses_agent` returns a **tuple of plain functions**:

    ``(non_streaming_fn, streaming_fn)``

The Apps scaffold's ``agent_server/agent.py`` is responsible for module-level
application of the decorators::

    # agent_server/agent.py — generated scaffold file
    from mlflow.genai.agent_server import invoke, stream
    from apx_agent import compile_to_responses_agent
    from my_agent import root_agent

    non_streaming, streaming = compile_to_responses_agent(
        root_agent, model="databricks-claude-sonnet-4-6"
    )

    invoke()(non_streaming)   # module-level — registers once
    stream()(streaming)       # module-level — registers once

Requires the ``eval`` extra (mlflow >= 3.x for
``mlflow.genai.agent_server`` and ``mlflow.types.responses``)::

    pip install 'apx-agent[eval]'
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Generator, NamedTuple

from ._agents import BaseAgent
from ._audit import AuditAttrs
from ._compile import compile_to_langgraph
from ._mlflow_tracing import safe_span, set_span_outputs

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)


_APPS_MISSING_MSG = (
    "compile_to_responses_agent requires mlflow>=3.x (mlflow.genai.agent_server "
    "and mlflow.types.responses). Install with: pip install 'apx-agent[eval]' "
    "(ensure mlflow>=3.12 is installed)."
)


@dataclass(frozen=True)
class _ResponsesTypes:
    request_cls: Any
    response_cls: Any
    stream_event_cls: Any


@dataclass(frozen=True)
class _WsAuth:
    """Per-request WorkspaceClient and optional identity headers."""

    ws: "WorkspaceClient"
    headers: Any  # DatabricksAppsHeaders | None


class CompiledResponsesAgent(NamedTuple):
    non_streaming: Callable[..., Any]
    streaming: Callable[..., Generator[Any, None, None]]


# ---------------------------------------------------------------------------
# Lazy / best-effort imports from mlflow.genai.agent_server + mlflow.types.responses
# ---------------------------------------------------------------------------


def _import_responses_types() -> _ResponsesTypes:
    """Best-effort import of the Responses contract types.

    Returns:
        ``_ResponsesTypes`` with request_cls, response_cls, stream_event_cls.

    Raises:
        NotImplementedError: if the optional mlflow >= 3.x install is missing.
    """
    try:
        from mlflow.types.responses import (
            ResponsesAgentRequest,
            ResponsesAgentResponse,
            ResponsesAgentStreamEvent,
        )
    except ImportError as e:  # pragma: no cover
        raise NotImplementedError(_APPS_MISSING_MSG) from e
    return _ResponsesTypes(
        request_cls=ResponsesAgentRequest,
        response_cls=ResponsesAgentResponse,
        stream_event_cls=ResponsesAgentStreamEvent,
    )


def _maybe_import_request_headers() -> Callable[[], dict[str, str]] | None:
    """Return ``agent_server.get_request_headers`` if available, else ``None``.

    Tests invoke the compiled functions directly without an Apps context, in
    which case ``get_request_headers`` either isn't importable or raises.
    Treat both as "no headers" and fall back to ``custom_inputs`` only.
    """
    try:
        from mlflow.genai.agent_server import get_request_headers
    except ImportError:
        return None
    return get_request_headers


# ---------------------------------------------------------------------------
# Auth resolution
# ---------------------------------------------------------------------------


def _resolve_ws_and_headers_for_request(
    custom_inputs: dict[str, Any] | None,
) -> _WsAuth:
    """Resolve both the per-request WorkspaceClient and DatabricksAppsHeaders
    from a single :func:`extract_obo_headers` call.

    For the Apps runtime (this module), identity can come from both
    ``custom_inputs`` (beats) and ``X-Forwarded-User`` HTTP headers injected by
    the Apps proxy. Both sources are normalized by ``extract_obo_headers`` in
    precedence order so ws-identity and memory-principal are always consistent.

    Returns:
        ``(ws, headers)`` where ``headers`` is a :class:`DatabricksAppsHeaders`
        instance when a ``user_id`` is resolvable, else ``None``.
    """
    from pydantic import SecretStr

    from ._defaults import DatabricksAppsHeaders, _make_workspace_client
    from ._obo import extract_obo_headers

    http_headers: dict[str, str] = {}
    getter = _maybe_import_request_headers()
    if getter is not None:
        try:
            http_headers = getter() or {}
        except Exception:
            # Apps context not initialized (test path, or invoked outside the
            # server). Quiet best-effort — drop to custom_inputs-only.
            http_headers = {}

    obo = extract_obo_headers(custom_inputs=custom_inputs, headers=http_headers)

    if obo.get("user_token"):
        ws = _make_workspace_client(
            token=obo["user_token"],
            host=obo.get("workspace_host"),
        )
    else:
        ws = _make_workspace_client()

    # Build headers only when we have a user_id so that requests without
    # identity continue to see headers=None (principal=None → NO_PRINCIPAL).
    req_headers: Any = None
    if obo.get("user_id"):
        token_raw = obo.get("user_token")
        req_headers = DatabricksAppsHeaders(
            host=obo.get("workspace_host"),
            user_name=None,
            user_id=obo.get("user_id"),
            user_email=obo.get("user_email"),
            request_id=None,
            token=SecretStr(token_raw) if token_raw else None,
        )

    return _WsAuth(ws=ws, headers=req_headers)


# ---------------------------------------------------------------------------
# Message conversion: ResponsesAgentRequest.input ↔ langchain BaseMessage
# ---------------------------------------------------------------------------


def _input_item_role(item: Any) -> str | None:
    """Extract a role from a ResponsesAgent input item (Message or OutputItem).

    Handles the discriminated union heuristically: ``Message``s have a ``role``;
    function-call output items don't.
    """
    if hasattr(item, "model_dump"):
        d = item.model_dump()
    elif isinstance(item, dict):
        d = item
    else:
        return None
    return d.get("role")


def _input_item_content(item: Any) -> str:
    """Best-effort extraction of plain text from an input item."""
    if hasattr(item, "model_dump"):
        d = item.model_dump()
    elif isinstance(item, dict):
        d = item
    else:
        return ""
    content = d.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for piece in content:
            if isinstance(piece, dict):
                t = piece.get("text")
                if t:
                    parts.append(str(t))
        return "".join(parts)
    return ""


def _responses_input_to_langchain(input_items: list[Any]) -> list[Any]:
    """Convert ResponsesAgentRequest.input to a langchain BaseMessage list."""
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    out: list[Any] = []
    for item in input_items:
        d = _input_item_dict(item)
        item_type = d.get("type")

        # Function-call echo items carry no ``role``; map them to an assistant
        # AIMessage with reconstructed tool_calls so the following
        # function_call_output (ToolMessage) isn't orphaned.
        if item_type == "function_call":
            call_id = str(d.get("call_id") or d.get("id") or "")
            out.append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": d.get("name", ""),
                            "args": _coerce_args(d.get("arguments")),
                            "id": call_id,
                        }
                    ],
                )
            )
            continue
        if item_type == "function_call_output":
            out.append(
                ToolMessage(
                    content=_function_call_output_text(d),
                    tool_call_id=str(d.get("call_id") or d.get("tool_call_id") or ""),
                )
            )
            continue

        role = _input_item_role(item)
        content = _input_item_content(item)
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
        elif role == "tool":
            out.append(
                ToolMessage(
                    content=content,
                    tool_call_id=str(d.get("tool_call_id") or d.get("call_id") or ""),
                )
            )
        else:
            # Default to user — Responses input is human turns by convention.
            out.append(HumanMessage(content=content))
    return out


def _input_item_dict(item: Any) -> dict[str, Any]:
    """Normalize a Responses input item (pydantic model or dict) to a dict."""
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if isinstance(item, dict):
        return item
    return {}


def _function_call_output_text(d: dict[str, Any]) -> str:
    """Best-effort plain text from a function_call_output item's ``output``."""
    output = d.get("output")
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    return str(output)


def _langchain_to_output_item(msg: Any, idx: int) -> dict[str, Any]:
    """Convert a langchain BaseMessage to a Responses output item dict.

    Dicts (not subclass instances) are used because
    ``ResponsesAgentResponse.output`` validates entries via the ``OutputItem``
    base class — passing a ``ResponseOutputMessage`` instance directly fails
    pydantic validation. Dicts go through the typed dispatcher.
    """
    from langchain_core.messages import AIMessage, ToolMessage

    msg_id = getattr(msg, "id", None) or f"msg-{idx}"
    text = msg.content if isinstance(msg.content, str) else str(msg.content)

    if isinstance(msg, AIMessage) and (msg.tool_calls or []):
        # Surface tool calls as function_call items per the Responses spec.
        items: list[dict[str, Any]] = []
        if text:
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "id": msg_id,
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": text, "annotations": []}
                    ],
                }
            )
        for tc in msg.tool_calls or []:
            items.append(
                {
                    "type": "function_call",
                    "call_id": tc.get("id", "") or f"call-{idx}",
                    "name": tc.get("name", ""),
                    "arguments": _json_str(tc.get("args", {})),
                    "id": f"fc-{idx}-{tc.get('id', '')}",
                }
            )
        # When we have multiple items, return them as a single "wrapper" dict
        # under the first-item key. The caller flattens via _emit.
        return {"_multi": items}

    if isinstance(msg, ToolMessage):
        return {
            "type": "function_call_output",
            "call_id": str(msg.tool_call_id or ""),
            "output": text,
        }

    # Plain assistant / system / user / fallthrough
    role = "assistant"
    if hasattr(msg, "type"):
        if msg.type == "system":
            role = "system"
        elif msg.type == "human":
            role = "user"
    return {
        "type": "message",
        "role": role,
        "id": msg_id,
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _json_str(value: Any) -> str:
    """Render ``value`` as a JSON string; safe fallback for non-serializables."""
    import json

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def _flatten_output_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten ``_multi`` wrapper dicts, dropping non-emittable echoes.

    The Responses output contract only accepts assistant messages, function
    calls, and function-call outputs. Workflow agents (e.g. ``RouterAgent``)
    inject ``HumanMessage`` / system messages into the graph state when handing
    off to a sub-agent; those surface as ``role: user`` / ``role: system``
    message items, which ``ResponsesAgentStreamEvent`` rejects ("Invalid role:
    user. Must be 'assistant'."). Skip them — they are input echoes, not agent
    output.
    """
    out: list[dict[str, Any]] = []
    for item in items:
        candidates = item["_multi"] if isinstance(item, dict) and "_multi" in item else [item]
        for c in candidates:
            if (
                isinstance(c, dict)
                and c.get("type") == "message"
                and c.get("role") not in (None, "assistant")
            ):
                continue
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Session helpers (thread_id convention)
# ---------------------------------------------------------------------------


def _load_session(
    store: Any | None, custom_inputs: dict[str, Any] | None
) -> Any | None:
    """Load (or create) the session keyed by ``custom_inputs.thread_id``.

    Apps convention: the per-conversation key is ``thread_id`` (cf. the Model
    Serving ``session_id`` convention used by ``_chat_agent.py``). Both are
    plumbed through ``custom_inputs`` so the apx-agent SessionStore handles
    them identically.

    On ``StoreError`` (warehouse cold-start, permissions, transient failure)
    the agent degrades to sessionless rather than blocking the response.
    """
    if store is None or not custom_inputs:
        return None
    thread_id = custom_inputs.get("thread_id") or custom_inputs.get("session_id")
    if not thread_id:
        return None
    from ._session import StoreError, load_or_create_session

    try:
        return load_or_create_session(store, thread_id)
    except StoreError as exc:
        logger.warning("_load_session(%s) degraded to sessionless: %s", thread_id, exc)
        return None


def _history_to_langchain(history: list[dict[str, Any]]) -> list[Any]:
    """Convert a session.history (list of {role, content, ...} dicts) to LC msgs."""
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    out: list[Any] = []
    for m in history:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "assistant":
            # Reconstruct tool_calls onto the AIMessage so a following
            # ToolMessage isn't orphaned (Databricks-Claude rejects a
            # tool_result whose tool_call_id matches no AIMessage tool call).
            # History stamps function calls as OpenAI-shaped tool_calls — see
            # ``_item_to_history_dict`` — mirror ``_chat_agent._to_langchain_messages``.
            out.append(
                AIMessage(content=content, tool_calls=_history_tool_calls(m))
            )
        elif role == "tool":
            out.append(
                ToolMessage(
                    content=content,
                    tool_call_id=str(m.get("tool_call_id", "")),
                )
            )
        else:
            out.append(HumanMessage(content=content))
    return out


def _history_tool_calls(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract langchain-shaped tool_calls from a stored assistant history dict.

    History stores tool calls in the OpenAI wire shape (``{"id", "type",
    "function": {"name", "arguments"}}``); langchain ``AIMessage`` wants
    ``{"name", "args", "id"}``. Mirrors ``_chat_agent._to_langchain_messages``.
    """
    tool_calls: list[dict[str, Any]] = []
    for tc in m.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
        tool_calls.append(
            {
                "name": fn.get("name", ""),
                "args": _coerce_args(fn.get("arguments")),
                "id": tc.get("id", ""),
            }
        )
    return tool_calls


def _coerce_args(arguments: Any) -> dict[str, Any]:
    """Coerce a tool-call ``arguments`` value to the dict langchain requires.

    The Responses contract carries function-call ``arguments`` as a JSON
    string; langchain ``AIMessage.tool_calls[*].args`` validates as a dict and
    raises on a string. Parse JSON strings; fall back to an empty dict for
    unparseable / non-dict values rather than crashing the whole conversion.
    """
    import json

    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _persist_session(
    store: Any | None,
    session: Any | None,
    *,
    input_items: list[dict[str, Any]],
    output_items: list[dict[str, Any]],
) -> None:
    """Append the inbound items + outbound items to the session and put it back.

    On ``StoreError`` (permissions, warehouse unavailable, transient failure)
    the turn is silently dropped rather than crashing the response — the same
    degraded-to-ephemeral policy used by ``_load_session``.
    """
    if store is None or session is None:
        return
    from ._session import StoreError, append_turn

    append_turn(
        session,
        input_messages=[
            _item_to_history_dict(it) for it in input_items
        ],
        new_messages=[
            _item_to_history_dict(it) for it in output_items
        ],
    )
    try:
        store.put(session)
    except StoreError as exc:
        logger.warning(
            "_persist_session(%s) failed — session not saved (degraded to ephemeral): %s",
            getattr(session, "session_id", "?"), exc,
        )


def _item_to_history_dict(item: Any) -> dict[str, Any]:
    """Coerce any Responses input/output item to a history dict."""
    if hasattr(item, "model_dump"):
        d = item.model_dump()
    elif isinstance(item, dict):
        d = dict(item)
    else:
        d = {"content": str(item)}
    # Normalize to {role, content} for storage. Function calls / outputs use
    # the role field "tool" so the chat-agent code path can read them back.
    if d.get("type") == "function_call":
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": d.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": d.get("name", ""),
                        "arguments": d.get("arguments", ""),
                    },
                }
            ],
        }
    if d.get("type") == "function_call_output":
        return {
            "role": "tool",
            "content": str(d.get("output", "")),
            "tool_call_id": d.get("call_id", ""),
        }
    role = d.get("role", "user")
    content = d.get("content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for piece in content:
            if isinstance(piece, dict) and "text" in piece:
                parts.append(str(piece["text"]))
        content = "".join(parts)
    return {"role": role, "content": content}


# ---------------------------------------------------------------------------
# Executor-seam helpers (Phase C)
# ---------------------------------------------------------------------------


def _lc_to_openai_messages(messages: list["BaseMessage"]) -> list[dict[str, Any]]:
    """Convert LangChain ``BaseMessage`` objects to OpenAI chat-completions dicts.

    Handles the four message types that appear in apx-agent conversation history:
    ``HumanMessage`` (``type="human"``), ``AIMessage`` (``type="ai"``),
    ``ToolMessage`` (``type="tool"``), and ``SystemMessage`` (``type="system"``).
    All other types fall back to ``role="user"``.

    :param messages: A list of LangChain ``BaseMessage`` instances (or any
        objects with ``type`` and ``content`` attributes).
    :returns: A list of dicts in OpenAI chat-completions message format, e.g.
        ``[{"role": "user", "content": "Hello"}]``.
    """
    import json as _json

    result: list[dict[str, Any]] = []
    for m in messages:
        msg_type = getattr(m, "type", "human")
        content = m.content if isinstance(m.content, str) else str(m.content)
        if msg_type == "human":
            result.append({"role": "user", "content": content})
        elif msg_type == "ai":
            tool_calls = getattr(m, "tool_calls", None) or []
            if tool_calls:
                tc_list = [
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": _json.dumps(tc.get("args", {})),
                        },
                    }
                    for tc in tool_calls
                ]
                result.append(
                    {
                        "role": "assistant",
                        "content": content if content else " ",
                        "tool_calls": tc_list,
                    }
                )
            else:
                result.append({"role": "assistant", "content": content})
        elif msg_type == "tool":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": str(getattr(m, "tool_call_id", "") or ""),
                    "content": content,
                }
            )
        elif msg_type == "system":
            result.append({"role": "system", "content": content})
        else:
            result.append({"role": "user", "content": content})
    return result


def _run_executor_sync(executor: Any, messages: list[dict[str, Any]]) -> list[Any]:
    """Run *executor.run_turn()* synchronously in an isolated event loop.

    Launches a :class:`~concurrent.futures.ThreadPoolExecutor` thread that owns
    a fresh asyncio event loop via :func:`asyncio.run`.  This prevents
    ``RuntimeError: This event loop is already running`` when called from a
    synchronous function inside an async WSGI/ASGI server (e.g. MLflow's
    ``AgentServer`` backed by uvicorn).

    :param executor: An :class:`~apx_agent._executor.Executor` instance whose
        ``run_turn`` async generator will be drained.
    :param messages: Conversation messages in OpenAI chat-completions format,
        e.g. ``[{"role": "user", "content": "Hello"}]``.
    :returns: A list of :class:`~apx_agent._executor.ExecutorEvent` objects
        collected from the full turn, ending with
        :class:`~apx_agent._executor.TurnComplete` or
        :class:`~apx_agent._executor.ExecutorError`.
    """
    import asyncio
    import concurrent.futures

    async def _collect() -> list[Any]:
        return [e async for e in executor.run_turn(messages, [], "", None)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _collect()).result()


def _executor_events_to_items(events: list[Any]) -> list[dict[str, Any]]:
    """Convert :class:`~apx_agent._executor.ExecutorEvent` objects to Responses output items.

    Maps the executor's streaming events to the structured output-item dicts
    expected by ``ResponsesAgentResponse.output``:

    - :class:`~apx_agent._executor.TextChunk` — text is accumulated and emitted
      as a ``message`` item when followed by a tool call or at
      :class:`~apx_agent._executor.TurnComplete`.
    - :class:`~apx_agent._executor.ToolCallRequest` — flushes any accumulated
      text first, then emits a ``function_call`` item.
    - :class:`~apx_agent._executor.ToolCallComplete` — emits a
      ``function_call_output`` item.
    - :class:`~apx_agent._executor.TurnComplete` — flushes remaining text; if
      no :class:`~apx_agent._executor.TextChunk` events were seen in this
      round, uses ``TurnComplete.response`` as the final message text.
    - :class:`~apx_agent._executor.ExecutorError` — raises :exc:`RuntimeError`.

    :param events: A flat list of executor events from one turn, as returned by
        :func:`_run_executor_sync`.
    :returns: A list of output-item dicts in the ``message``, ``function_call``,
        and ``function_call_output`` shapes accepted by
        ``ResponsesAgentResponse.output``.
    :raises RuntimeError: If any event is an
        :class:`~apx_agent._executor.ExecutorError`.
    """
    import json as _json

    from ._executor import (
        ExecutorError,
        TextChunk,
        ToolCallComplete,
        ToolCallRequest,
        TurnComplete,
    )

    output_items: list[dict[str, Any]] = []
    text_parts: list[str] = []
    item_index = 0

    def _flush_text() -> bool:
        """Emit accumulated text as a message item; return True if text was flushed."""
        nonlocal text_parts, item_index
        text = "".join(text_parts)
        text_parts = []
        if not text:
            return False
        output_items.append(
            {
                "type": "message",
                "role": "assistant",
                "id": f"msg-{item_index}",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
        item_index += 1
        return True

    for event in events:
        if isinstance(event, TextChunk):
            if event.text:
                text_parts.append(event.text)
        elif isinstance(event, ToolCallRequest):
            _flush_text()
            args = event.args if event.args is not None else {}
            output_items.append(
                {
                    "type": "function_call",
                    "call_id": event.call_id or "",
                    "name": event.name or "",
                    "arguments": (
                        _json.dumps(args) if isinstance(args, dict) else str(args)
                    ),
                    "id": f"fc-{item_index}",
                }
            )
            item_index += 1
        elif isinstance(event, ToolCallComplete):
            result_val = event.result if event.error is None else event.error
            output_items.append(
                {
                    "type": "function_call_output",
                    "call_id": event.call_id or "",
                    "output": (
                        result_val
                        if isinstance(result_val, str)
                        else _json.dumps(result_val, default=str)
                    ),
                }
            )
        elif isinstance(event, TurnComplete):
            had_chunks = bool(text_parts)
            _flush_text()
            # Fallback: if no TextChunk events were received in this round, the full
            # response text lives only in TurnComplete.response.
            if not had_chunks and event.response:
                output_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "id": f"msg-{item_index}",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": event.response,
                                "annotations": [],
                            }
                        ],
                    }
                )
                item_index += 1
        elif isinstance(event, ExecutorError):
            raise RuntimeError(f"Executor error: {event.message}")

    return output_items


# ---------------------------------------------------------------------------
# Public compile entry point
# ---------------------------------------------------------------------------


def compile_to_responses_agent(
    agent: BaseAgent,
    *,
    model: str,
    session_store: Any | None = None,
    executor: str = "langgraph",
) -> CompiledResponsesAgent:
    """Compile an apx-agent ``BaseAgent`` to the Databricks Apps ResponsesAgent contract.

    Returns two plain (undecorated) functions; the caller is responsible for
    applying ``@invoke()`` / ``@stream()`` at module level in the scaffold's
    ``agent_server/agent.py``. See the module docstring for the rationale.

    Args:
        agent: The apx-agent root agent to wrap. Compiles to a LangGraph
            per-request, just like the ChatAgent path.
        model: Databricks serving endpoint name passed through to compile.
        session_store: Optional ``SessionStore`` for multi-turn memory. When
            set, the compiled functions read ``custom_inputs["thread_id"]``
            (Apps convention) — falling back to ``session_id`` for symmetry
            — and prepend the session history to the request input before
            running the agent. After the run, the new output is appended and
            persisted. When the key is absent the session machinery no-ops.
        executor: Inference backend selector.  ``"langgraph"`` (default) uses
            the existing :func:`~apx_agent._compile.compile_to_langgraph` path.
            ``"claude-sdk"`` uses
            :class:`~apx_agent._claude_sdk_executor.ClaudeSDKExecutor` — a
            direct OpenAI-compatible loop without LangGraph overhead.  Only
            effective when *agent* is a :class:`~apx_agent._agents.LlmAgent`;
            composite agents silently fall back to ``"langgraph"``.

    Returns:
        ``(non_streaming_fn, streaming_fn)``::

          non_streaming_fn(request: ResponsesAgentRequest) -> ResponsesAgentResponse
          streaming_fn(request: ResponsesAgentRequest)
              -> Generator[ResponsesAgentStreamEvent, None, None]

    Raises:
        NotImplementedError: if the optional mlflow >= 3.x Responses extras
            are not installed. The compile is lazy so a process that never
            calls this function won't trip the import.
    """
    _types = _import_responses_types()
    ResponsesAgentRequest = _types.request_cls
    ResponsesAgentResponse = _types.response_cls
    ResponsesAgentStreamEvent = _types.stream_event_cls

    # Snapshot for closure capture
    _agent = agent
    _model = model
    _session_store = session_store
    _executor_name = executor

    # -----------------------------------------------------------------------
    # Non-streaming
    # -----------------------------------------------------------------------

    def non_streaming(request: Any) -> Any:
        """``@invoke()`` handler — non-streaming ResponsesAgent endpoint."""
        if not isinstance(request, ResponsesAgentRequest):
            # Accept dict for easier testing; coerce via pydantic.
            request = ResponsesAgentRequest(**dict(request))

        # Idempotent, never-raises: ensure the in-process trace-capture
        # SpanProcessor is attached to the live provider so the dev-UI Trace
        # detail can serve this run from memory (FEVM blob egress is blocked).
        from ._trace_store import ensure_capture_processor
        ensure_capture_processor()

        custom_inputs: dict[str, Any] = dict(request.custom_inputs or {})
        session = _load_session(_session_store, custom_inputs)

        effective_model = _resolve_model(_model)
        user_token_provided = bool(
            _peek_user_token(custom_inputs)
        )

        with safe_span(
            "ApxResponsesAgent.invoke",
            span_type="AGENT",
            inputs={"input": [_item_to_history_dict(i) for i in request.input]},
            attributes={
                AuditAttrs.OPERATION: "invoke",
                AuditAttrs.MODEL_ENDPOINT: effective_model,
                AuditAttrs.MODEL_INPUT_MESSAGES: len(request.input),
                AuditAttrs.USER_TOKEN_PROVIDED: user_token_provided,
                AuditAttrs.SESSION_ID: (
                    session.session_id if session is not None else None
                ),
                AuditAttrs.MODEL_STREAMING: False,
            },
        ) as span:
            auth = _resolve_ws_and_headers_for_request(custom_inputs)
            ws, req_headers = auth.ws, auth.headers

            # Prepend session history (if any)
            lc_history = (
                _history_to_langchain(session.history) if session is not None else []
            )
            lc_input = _responses_input_to_langchain(list(request.input))
            graph_input = lc_history + lc_input

            _use_sdk = _executor_name == "claude-sdk"
            if _use_sdk:
                from ._agents import LlmAgent as _LlmAgent
                if not isinstance(_agent, _LlmAgent):
                    logger.warning(
                        "executor='claude-sdk' is only supported for LlmAgent; "
                        "falling back to langgraph for %s",
                        type(_agent).__name__,
                    )
                    _use_sdk = False

            if _use_sdk:
                from ._claude_sdk_executor import ClaudeSDKExecutor as _CSE
                _sdk_exec = _CSE(
                    model=effective_model,
                    tools=list(_agent._tool_fns),
                    instructions=_agent._instructions,
                    ws=ws,
                )
                events = _run_executor_sync(_sdk_exec, _lc_to_openai_messages(graph_input))
                output_items = _executor_events_to_items(events)
            else:
                with safe_span(
                    "compile_to_langgraph",
                    span_type="CHAIN",
                    attributes={AuditAttrs.MODEL_ENDPOINT: effective_model},
                ):
                    graph = compile_to_langgraph(
                        _agent, ws=ws, model=effective_model, headers=req_headers
                    )
                input_count = len(graph_input)
                with safe_span("graph.invoke", span_type="CHAIN"):
                    result = graph.invoke({"messages": graph_input})
                new_lc = result["messages"][input_count:]
                raw_items = [_langchain_to_output_item(m, i) for i, m in enumerate(new_lc)]
                output_items = _flatten_output_items(raw_items)

            response = ResponsesAgentResponse(
                id=f"resp-{uuid.uuid4().hex[:12]}",
                output=output_items,
            )
            set_span_outputs(span, response.model_dump())

            # Persist session
            _persist_session(
                _session_store,
                session,
                input_items=list(request.input),
                output_items=output_items,
            )

            return response

    non_streaming.__doc__ = (
        "ResponsesAgent non-streaming handler compiled from "
        f"apx-agent {type(agent).__name__}. Apply ``@invoke()`` at module level."
    )

    # -----------------------------------------------------------------------
    # Streaming
    # -----------------------------------------------------------------------

    def streaming(request: Any) -> Generator[Any, None, None]:
        """``@stream()`` handler — streaming ResponsesAgent endpoint.

        Emits one ``response.output_item.done`` event per langchain message
        produced by the graph, followed by a terminal ``response.completed``
        event carrying the full response. This is the contract documented in
        the MLflow Responses streaming guide.
        """
        if not isinstance(request, ResponsesAgentRequest):
            request = ResponsesAgentRequest(**dict(request))

        # See non_streaming — idempotent capture-processor install (never raises).
        from ._trace_store import ensure_capture_processor
        ensure_capture_processor()

        custom_inputs: dict[str, Any] = dict(request.custom_inputs or {})
        session = _load_session(_session_store, custom_inputs)
        effective_model = _resolve_model(_model)
        user_token_provided = bool(_peek_user_token(custom_inputs))

        with safe_span(
            "ApxResponsesAgent.stream",
            span_type="AGENT",
            inputs={"input": [_item_to_history_dict(i) for i in request.input]},
            attributes={
                AuditAttrs.OPERATION: "stream",
                AuditAttrs.MODEL_ENDPOINT: effective_model,
                AuditAttrs.MODEL_INPUT_MESSAGES: len(request.input),
                AuditAttrs.USER_TOKEN_PROVIDED: user_token_provided,
                AuditAttrs.MODEL_STREAMING: True,
            },
        ):
            auth = _resolve_ws_and_headers_for_request(custom_inputs)
            ws, req_headers = auth.ws, auth.headers

            lc_history = (
                _history_to_langchain(session.history) if session is not None else []
            )
            lc_input = _responses_input_to_langchain(list(request.input))
            graph_input = lc_history + lc_input

            _use_sdk = _executor_name == "claude-sdk"
            if _use_sdk:
                from ._agents import LlmAgent as _LlmAgent
                if not isinstance(_agent, _LlmAgent):
                    logger.warning(
                        "executor='claude-sdk' is only supported for LlmAgent; "
                        "falling back to langgraph for %s",
                        type(_agent).__name__,
                    )
                    _use_sdk = False

            output_items: list[dict[str, Any]] = []
            output_index = 0

            if _use_sdk:
                from ._claude_sdk_executor import ClaudeSDKExecutor as _CSE
                _sdk_exec = _CSE(
                    model=effective_model,
                    tools=list(_agent._tool_fns),
                    instructions=_agent._instructions,
                    ws=ws,
                )
                events = _run_executor_sync(_sdk_exec, _lc_to_openai_messages(graph_input))
                output_items = _executor_events_to_items(events)
                for item in output_items:
                    yield ResponsesAgentStreamEvent(
                        type="response.output_item.done",
                        item=item,
                        output_index=output_index,
                    )
                    output_index += 1
            else:
                graph = compile_to_langgraph(
                    _agent, ws=ws, model=effective_model, headers=req_headers
                )
                for chunk in graph.stream(
                    {"messages": graph_input}, stream_mode="updates"
                ):
                    if not isinstance(chunk, dict):
                        continue
                    for _node_name, node_output in chunk.items():
                        if not isinstance(node_output, dict):
                            continue
                        for lc_msg in node_output.get("messages", []) or []:
                            raw = _langchain_to_output_item(lc_msg, output_index)
                            for item in _flatten_output_items([raw]):
                                output_items.append(item)
                                yield ResponsesAgentStreamEvent(
                                    type="response.output_item.done",
                                    item=item,
                                    output_index=output_index,
                                )
                                output_index += 1

            # Terminal event with the assembled response
            final_response = {
                "id": f"resp-{uuid.uuid4().hex[:12]}",
                "object": "response",
                "output": output_items,
            }
            yield ResponsesAgentStreamEvent(
                type="response.completed",
                response=final_response,
            )

            _persist_session(
                _session_store,
                session,
                input_items=list(request.input),
                output_items=output_items,
            )

    streaming.__doc__ = (
        "ResponsesAgent streaming handler compiled from "
        f"apx-agent {type(agent).__name__}. Apply ``@stream()`` at module level."
    )

    return CompiledResponsesAgent(non_streaming=non_streaming, streaming=streaming)


# ---------------------------------------------------------------------------
# Internal helpers shared by both handlers
# ---------------------------------------------------------------------------


def _resolve_model(default: str) -> str:
    """Return the effective model for this request.

    Reads ``APX_AGENT_MODEL_OVERRIDE`` at runtime so operators can hot-swap
    the LLM on a deployed endpoint without re-logging an artifact.  This is a
    sanctioned exception to Design Principle 1 (Spec Self-Containment) — the
    override is endpoint-scoped config written only by
    :func:`apx_agent.hot_swap_model` via the Databricks SDK, never by ambient
    server state.  See ``designs/DESIGN_PRINCIPLES.md`` §Sanctioned Exceptions.

    :param default: The compile-time model from the agent spec.
    :returns: ``APX_AGENT_MODEL_OVERRIDE`` if set on this endpoint, else ``default``.
    """
    import os

    return os.environ.get("APX_AGENT_MODEL_OVERRIDE") or default


def _peek_user_token(custom_inputs: dict[str, Any] | None) -> str | None:
    """Quick peek for the audit attribute — doesn't need full normalization."""
    if not custom_inputs:
        return None
    tok = custom_inputs.get("user_token")
    if tok:
        return str(tok)
    # We don't read headers here — only the runtime path does — but the audit
    # bit is conservative: only "True" if we definitely saw a token.
    return None
