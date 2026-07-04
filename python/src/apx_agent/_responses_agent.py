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
from ._audit import AuditAttrs, set_audit_attrs, stamp_version_correlation, user_hash
from ._chat_agent import _pending_interrupt, _resume_decision
from ._compile import compile_to_langgraph
from ._conversation import (
    ConversationItem,
    ConversationStore,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
    drop_orphaned_tool_outputs,
    synthesize_conversation_title,
)
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


@dataclass(frozen=True)
class _ConvLoad:
    """Loaded or created conversation with its stored items.

    :param conversation_id: The conversation key from ``custom_inputs``,
        e.g. ``"thread-abc123"``.
    :param items: Ascending-ordered ConversationItems already persisted for
        this conversation. Empty list for a brand-new conversation.
    :param is_new: ``True`` when the conversation row was just created (first
        turn). Used to gate title synthesis so we only set it once.
    """

    conversation_id: str
    items: list[ConversationItem]
    is_new: bool = False


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
        # G2: fail closed in the Apps multi-user runtime (unless SP fallback is
        # explicitly opted in) instead of silently running as the app SP.
        from ._obo import resolve_no_obo_or_raise
        resolve_no_obo_or_raise()
        ws = _make_workspace_client()

    # Build headers when we have a user_token OR a user_id. The token is the
    # load-bearing field for A2A forwarding, so a request with a token but no
    # user_id (a valid Model Serving shape) must still produce headers — else
    # the sub-agent hop launders the caller's privilege away. Requests with
    # neither still see headers=None (principal=None → NO_PRINCIPAL).
    req_headers: Any = None
    token_raw = obo.get("user_token")
    if token_raw or obo.get("user_id"):
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


def _approval_output_item(payload: Any, thread_id: str | None) -> dict[str, Any]:
    """A ResponsesAgent output message item stating a mid-turn approval ask.

    Reuses the ChatAgent prose builder keyed on ``thread_id`` (ResponsesAgent's
    convention) so the resume instruction names the right ``custom_inputs`` field.
    """
    from ._chat_agent import approval_prose  # noqa: PLC0415

    return {
        "type": "message",
        "role": "assistant",
        "id": f"appr-{uuid.uuid4().hex[:8]}",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": approval_prose(payload, thread_id, key="thread_id"),
                "annotations": [],
            }
        ],
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


def _token_text(chunk: Any) -> str:
    """Extract plain text from an LLM message chunk streamed in ``messages`` mode.

    ``stream_mode="messages"`` yields ``AIMessageChunk``s whose ``content`` is a
    string for most providers, or a list of content-block dicts (Anthropic-style)
    from which the ``text`` blocks are concatenated. Non-text chunks (e.g. tool-
    call argument deltas carry empty content) yield ``""`` and are not emitted as
    output-text deltas.
    """
    content = chunk.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text") for p in content if isinstance(p, dict)]
        return "".join(p for p in parts if isinstance(p, str))
    return ""


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
# ConversationStore helpers (thread_id convention)
# ---------------------------------------------------------------------------


def _load_or_create_conversation(
    store: ConversationStore | None,
    custom_inputs: dict[str, Any] | None,
    agent_id: str | None = None,
) -> _ConvLoad | None:
    """Load or create the conversation keyed by ``custom_inputs.thread_id``.

    Apps convention: the per-conversation key is ``thread_id`` (cf. the Model
    Serving ``session_id`` convention used by ``_chat_agent.py``). Both are
    plumbed through ``custom_inputs`` so the ConversationStore handles them.

    On failure the agent degrades to sessionless rather than blocking the response.

    :param store: The conversation store, or ``None`` for no-op.
    :param custom_inputs: Per-request custom inputs, e.g.
        ``{"thread_id": "my_conv_id"}``.
    :param agent_id: Agent identifier bound on conversation creation,
        e.g. ``"my-agent"``. Readers that filter by agent (the dev-UI
        History panel) only see conversations bound to their agent —
        creating without it makes the conversation invisible to them.
    :returns: A :class:`_ConvLoad` on success; ``None`` otherwise.
    """
    if store is None or not custom_inputs:
        return None
    conv_id = custom_inputs.get("thread_id") or custom_inputs.get("session_id")
    if not conv_id:
        return None
    try:
        existing = store.get_conversation(conv_id)
        is_new = existing is None
        if is_new:
            store.create_conversation(id=conv_id, agent_id=agent_id)
        page = store.list_items(conv_id, order="asc", limit=10_000)
        return _ConvLoad(conversation_id=conv_id, items=page.data, is_new=is_new)
    except Exception as exc:
        logger.warning(
            "_load_or_create_conversation(%s) degraded to sessionless: %s", conv_id, exc
        )
        return None


def _conv_items_to_lc_messages(items: list[ConversationItem]) -> list[Any]:
    """Convert ConversationStore items to langchain BaseMessages for history replay.

    Consecutive ``function_call`` items sharing the same ``response_id`` are
    coalesced into a single AIMessage so the LLM sees properly paired
    tool_call/tool_result sequences.

    :param items: Ascending-ordered ConversationItems from the store.
    :returns: A list of langchain BaseMessage objects.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    # Drop any tool result whose function_call was never stored (e.g. an approval
    # turn's tool-call lived only in a since-gone checkpointer) — an orphaned tool
    # message replays as a tool_result with no matching call and the LLM rejects it.
    items = drop_orphaned_tool_outputs(items)

    result: list[Any] = []
    i = 0
    while i < len(items):
        item = items[i]
        if item.type == "message":
            data: MessageData = item.data  # type: ignore[assignment]
            text = "".join(
                b.get("text", "") for b in data.content
                if isinstance(b, dict) and "text" in b
            )
            if data.role == "user":
                result.append(HumanMessage(content=text))
            else:
                result.append(AIMessage(content=text))
            i += 1
        elif item.type == "function_call":
            # Coalesce consecutive function_call items with the same response_id
            # into one AIMessage with tool_calls, to avoid orphaned ToolMessages.
            current_rid = item.response_id
            tool_calls: list[dict[str, Any]] = []
            while (
                i < len(items)
                and items[i].type == "function_call"
                and items[i].response_id == current_rid
            ):
                fc: FunctionCallData = items[i].data  # type: ignore[assignment]
                tool_calls.append({
                    "name": fc.name,
                    "args": _coerce_args(fc.arguments),
                    "id": fc.call_id,
                })
                i += 1
            result.append(AIMessage(content="", tool_calls=tool_calls))
        elif item.type == "function_call_output":
            fco: FunctionCallOutputData = item.data  # type: ignore[assignment]
            result.append(ToolMessage(content=fco.output, tool_call_id=fco.call_id))
            i += 1
        else:
            i += 1  # skip reasoning, compaction, native_tool
    return result


def _coerce_args(arguments: Any) -> dict[str, Any]:
    """Coerce a tool-call ``arguments`` value to the dict langchain requires.

    The Responses contract carries function-call ``arguments`` as a JSON
    string; langchain ``AIMessage.tool_calls[*].args`` validates as a dict and
    raises on a string. Parse JSON strings; fall back to an empty dict for
    unparseable / non-dict values rather than crashing the whole conversion.

    :param arguments: Raw arguments value from a function call, either a dict
        or a JSON string, e.g. ``'{"query": "hello"}'``.
    :returns: A dict of parsed arguments, e.g. ``{"query": "hello"}``.
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


def _resp_item_to_new_conv_items(
    item: Any,
    model: str,
    response_id: str,
) -> list[NewConversationItem]:
    """Convert a single Responses API item to NewConversationItem objects.

    Handles message, function_call, and function_call_output item types.
    Items that cannot be converted (unknown types) are skipped with a warning.

    :param item: A Responses API item — either a Pydantic model with
        ``model_dump()`` or a plain dict.
    :param model: The model/endpoint name, e.g. ``"databricks-claude-sonnet-4-6"``.
        Required for assistant-role and function_call items.
    :param response_id: Logical turn ID linking input and output items,
        e.g. ``"turn_abc123"``.
    :returns: A list of :class:`NewConversationItem` objects.
    """
    d: dict[str, Any] = item.model_dump() if hasattr(item, "model_dump") else dict(item)
    item_type = d.get("type", "")

    if item_type == "function_call":
        return [NewConversationItem(
            type="function_call",
            response_id=response_id,
            data=FunctionCallData(
                agent=model,
                name=d.get("name") or "",
                arguments=d.get("arguments") or "{}",
                call_id=d.get("call_id") or "",
            ),
        )]

    if item_type == "function_call_output":
        return [NewConversationItem(
            type="function_call_output",
            response_id=response_id,
            data=FunctionCallOutputData(
                call_id=d.get("call_id") or "",
                output=str(d.get("output") or ""),
            ),
        )]

    if item_type == "message":
        role = d.get("role", "user")
        if role not in ("user", "assistant"):
            logger.warning(
                "Skipping Responses message item with unsupported role %r", role
            )
            return []
        content_raw = d.get("content", "")
        if isinstance(content_raw, list):
            text = "".join(
                str(p.get("text", "")) for p in content_raw if isinstance(p, dict)
            )
        else:
            text = str(content_raw)
        content_type = "output_text" if role == "assistant" else "input_text"
        return [NewConversationItem(
            type="message",
            response_id=response_id,
            data=MessageData(
                role=role,  # type: ignore[arg-type]
                content=[{"type": content_type, "text": text}],
                agent=model if role == "assistant" else None,
            ),
        )]

    return []


def _persist_conv_turn(
    store: ConversationStore | None,
    conv_id: str | None,
    *,
    input_items: list[Any],
    output_items: list[Any],
    model: str,
    response_id: str,
    is_new: bool = False,
) -> None:
    """Append Responses API input and output items to the ConversationStore.

    On failure the turn is silently dropped rather than crashing the response.

    :param store: The conversation store, or ``None`` for no-op.
    :param conv_id: The conversation id to append to, or ``None`` for no-op.
    :param input_items: Responses API input items for this turn.
    :param output_items: Responses API output items generated this turn.
    :param model: Model/endpoint name, e.g. ``"databricks-claude-sonnet-4-6"``.
    :param response_id: Logical turn ID linking input and output items.
    :param is_new: ``True`` when the conversation was just created. When set,
        a title is synthesized from the first user message and persisted via
        ``update_conversation``.
    """
    if store is None or conv_id is None:
        return
    all_items = list(input_items) + list(output_items)
    new_items = [
        ci
        for it in all_items
        for ci in _resp_item_to_new_conv_items(it, model, response_id)
    ]
    try:
        store.append(conv_id, new_items)
        if is_new:
            user_item = next(
                (it for it in new_items
                 if it.type == "message" and isinstance(it.data, MessageData)
                 and it.data.role == "user"),
                None,
            )
            if user_item is not None and isinstance(user_item.data, MessageData):
                title = synthesize_conversation_title(user_item.data.content)
                if title:
                    store.update_conversation(conv_id, title=title)
    except Exception as exc:
        logger.warning(
            "_persist_conv_turn(%s) failed — turn not saved: %s", conv_id, exc
        )


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
    conversation_store: ConversationStore | None = None,
    executor: str = "langgraph",
    agent_id: str | None = None,
    checkpointer: Any | None = None,
) -> CompiledResponsesAgent:
    """Compile an apx-agent ``BaseAgent`` to the Databricks Apps ResponsesAgent contract.

    Returns two plain (undecorated) functions; the caller is responsible for
    applying ``@invoke()`` / ``@stream()`` at module level in the scaffold's
    ``agent_server/agent.py``. See the module docstring for the rationale.

    Args:
        agent: The apx-agent root agent to wrap. Compiles to a LangGraph
            per-request, just like the ChatAgent path.
        model: Databricks serving endpoint name passed through to compile.
        conversation_store: Optional :class:`ConversationStore` for multi-turn
            memory. When set, the compiled functions read
            ``custom_inputs["thread_id"]`` (Apps convention) — falling back to
            ``session_id`` for symmetry — and prepend prior conversation items
            before running the agent. After the run, the new output is appended
            and persisted. When the key is absent the session machinery no-ops.
        executor: Inference backend selector.  ``"langgraph"`` (default) uses
            the existing :func:`~apx_agent._compile.compile_to_langgraph` path.
            ``"claude-sdk"`` uses
            :class:`~apx_agent._claude_sdk_executor.ClaudeSDKExecutor` — a
            direct OpenAI-compatible loop without LangGraph overhead.  Only
            effective when *agent* is a :class:`~apx_agent._agents.LlmAgent`;
            composite agents silently fall back to ``"langgraph"``.
        agent_id: Identifier bound to conversations created by these handlers,
            e.g. ``"my-agent"``. Pass the SAME name the reader filters by —
            the dev-UI History panel lists ``list_conversations(agent_id=
            config.name)``, so the pyproject ``[tool.apx.agent].name`` is the
            right value. Falls back to ``agent.name`` when omitted.

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
    _conversation_store = conversation_store
    _executor_name = executor
    _agent_id = agent_id if agent_id is not None else getattr(agent, "name", None)

    # Short-term memory (mirrors chat_agent_for): default a served LlmAgent to a
    # process-scoped InMemorySaver when neither a checkpointer nor a durable
    # conversation store was wired — so it remembers across turns within a
    # thread_id, in-process. Not for the claude-sdk path (no LangGraph) and not
    # for composite agents (compile_to_langgraph would raise). See #329.
    _checkpointer = checkpointer
    if (
        _checkpointer is None
        and _conversation_store is None
        and _executor_name != "claude-sdk"
    ):
        from ._agents import LlmAgent as _LlmAgentCp  # noqa: PLC0415

        if isinstance(_agent, _LlmAgentCp):
            from langgraph.checkpoint.memory import InMemorySaver  # noqa: PLC0415

            _checkpointer = InMemorySaver()
            logger.info(
                "Short-term memory: in-process (InMemorySaver) for ResponsesAgent "
                "%r — per-replica, resets on restart. Configure "
                "[tool.apx.agent.session] for durable memory.",
                _agent_id,
            )

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
        conv = _load_or_create_conversation(_conversation_store, custom_inputs, agent_id=_agent_id)
        conv_id = conv.conversation_id if conv is not None else None
        conv_items = conv.items if conv is not None else []

        effective_model = _resolve_model(_model)
        user_token_provided = bool(
            _peek_user_token(custom_inputs)
        )

        with safe_span(
            "ApxResponsesAgent.invoke",
            span_type="AGENT",
            inputs={"input": [str(i) for i in request.input]},
            attributes={
                AuditAttrs.OPERATION: "invoke",
                AuditAttrs.MODEL_ENDPOINT: effective_model,
                AuditAttrs.MODEL_INPUT_MESSAGES: len(request.input),
                AuditAttrs.USER_TOKEN_PROVIDED: user_token_provided,
                AuditAttrs.SESSION_ID: conv_id,
                AuditAttrs.MODEL_STREAMING: False,
            },
        ) as span:
            stamp_version_correlation(span)
            auth = _resolve_ws_and_headers_for_request(custom_inputs)
            set_audit_attrs(
                span,
                user_hash=user_hash(auth.headers.user_id if auth.headers else None),
            )
            ws, req_headers = auth.ws, auth.headers

            # Short-term memory: when a checkpointer is active + a thread key is
            # present, it owns the transcript — send only the new turn (no
            # history prepend) and key state by thread_id. See #329.
            thread_id = custom_inputs.get("thread_id") or custom_inputs.get("session_id")
            cp = _checkpointer if thread_id else None
            lg_config = {"configurable": {"thread_id": thread_id}} if cp else None

            lc_history = _conv_items_to_lc_messages(conv_items)
            lc_input = _responses_input_to_langchain(list(request.input))
            graph_input = lc_input if cp else lc_history + lc_input

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
                from ._executor_factory import governance_guards_present
                if _gov := governance_guards_present(_agent):
                    logger.warning(
                        "executor='claude-sdk' cannot enforce configured "
                        "governance (%s); using the governed langgraph path so "
                        "before_tool/before_model guards and input/output "
                        "guardrails are not silently bypassed.",
                        _gov,
                    )
                    _use_sdk = False

            if _use_sdk:
                from ._agents import LlmAgent as _LlmAgent
                from ._claude_sdk_executor import ClaudeSDKExecutor as _CSE
                # Guaranteed by the guard above (_use_sdk is set False unless
                # _agent is an LlmAgent); narrows _agent for the type checker.
                assert isinstance(_agent, _LlmAgent)
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
                        _agent, ws=ws, model=effective_model, headers=req_headers,
                        **({"checkpointer": cp} if cp else {}),
                    )
                input_count = len(graph_input)
                # With a checkpointer, invoke returns the FULL thread state
                # (prior history + this turn's input + output), not just
                # input+output — so slice new output after the prior state too.
                pre_count = 0
                if cp is not None:
                    try:
                        pre_count = len(graph.get_state(lg_config).values.get("messages", []))
                    except Exception:
                        pre_count = 0
                # Approval resume: a checkpointed thread resends
                # {"resume": <decision>} to continue from the paused tool call
                # instead of new input (mirrors the ChatAgent predict path).
                resume = _resume_decision(custom_inputs) if lg_config else None
                with safe_span("graph.invoke", span_type="CHAIN"):
                    if resume is not None:
                        from langgraph.types import Command  # noqa: PLC0415
                        result = graph.invoke(Command(resume=resume), config=lg_config)
                    else:
                        result = graph.invoke(
                            {"messages": graph_input},
                            **({"config": lg_config} if lg_config else {}),
                        )

                # Mid-turn approval: a gated tool suspended the run → return an
                # approval-required response (tool NOT run) before persisting.
                paused = _pending_interrupt(graph, lg_config)
                if paused is not None:
                    response = ResponsesAgentResponse(
                        id=f"resp-{uuid.uuid4().hex[:12]}",
                        output=[_approval_output_item(paused, thread_id)],
                        custom_outputs={"approval_required": paused},
                    )
                    set_span_outputs(span, response.model_dump())
                    # Persist the user's input (+ title) on the approval turn;
                    # the tool result + answer are appended on resume. Without
                    # this the prompt is lost when the turn pauses.
                    if conv_id is not None:
                        _persist_conv_turn(
                            _conversation_store,
                            conv_id,
                            input_items=list(request.input),
                            output_items=[],
                            model=effective_model,
                            response_id=str(uuid.uuid4()),
                            is_new=conv.is_new if conv is not None else False,
                        )
                    return response

                # On a resume there is no new input in graph state (Command was
                # fed, not messages), so slice only past the prior state — else a
                # client that resent its input would drop that many new messages
                # (mirrors ChatAgent.predict's slice_start).
                slice_start = pre_count if resume is not None else pre_count + input_count
                new_lc = result["messages"][slice_start:]
                raw_items = [_langchain_to_output_item(m, i) for i, m in enumerate(new_lc)]
                output_items = _flatten_output_items(raw_items)

            response = ResponsesAgentResponse(
                id=f"resp-{uuid.uuid4().hex[:12]}",
                output=output_items,
            )
            set_span_outputs(span, response.model_dump())

            if conv_id is not None:
                response_id = str(uuid.uuid4())
                _persist_conv_turn(
                    _conversation_store,
                    conv_id,
                    # On resume the client resends the original input, but it was
                    # already persisted on the pause turn (and Command(resume) is
                    # fed, not the messages). Persist empty input here to avoid
                    # double-writing the prompt (#375).
                    input_items=[] if resume is not None else list(request.input),
                    output_items=output_items,
                    model=effective_model,
                    response_id=response_id,
                    is_new=conv.is_new if conv is not None else False,
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
        conv = _load_or_create_conversation(_conversation_store, custom_inputs, agent_id=_agent_id)
        conv_id = conv.conversation_id if conv is not None else None
        conv_items = conv.items if conv is not None else []
        effective_model = _resolve_model(_model)
        user_token_provided = bool(_peek_user_token(custom_inputs))

        with safe_span(
            "ApxResponsesAgent.stream",
            span_type="AGENT",
            inputs={"input": [str(i) for i in request.input]},
            attributes={
                AuditAttrs.OPERATION: "stream",
                AuditAttrs.MODEL_ENDPOINT: effective_model,
                AuditAttrs.MODEL_INPUT_MESSAGES: len(request.input),
                AuditAttrs.USER_TOKEN_PROVIDED: user_token_provided,
                AuditAttrs.SESSION_ID: conv_id,
                AuditAttrs.MODEL_STREAMING: True,
            },
        ) as span:
            stamp_version_correlation(span)
            auth = _resolve_ws_and_headers_for_request(custom_inputs)
            set_audit_attrs(
                span,
                user_hash=user_hash(auth.headers.user_id if auth.headers else None),
            )
            ws, req_headers = auth.ws, auth.headers

            # Short-term memory (see non-streaming): checkpointer active + thread
            # key → send only the new turn, key state by thread_id. The stream's
            # "updates" mode yields only new messages, so no slice fix is needed.
            thread_id = custom_inputs.get("thread_id") or custom_inputs.get("session_id")
            cp = _checkpointer if thread_id else None
            lg_config = {"configurable": {"thread_id": thread_id}} if cp else None

            lc_history = _conv_items_to_lc_messages(conv_items)
            lc_input = _responses_input_to_langchain(list(request.input))
            graph_input = lc_input if cp else lc_history + lc_input

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
                from ._executor_factory import governance_guards_present
                if _gov := governance_guards_present(_agent):
                    logger.warning(
                        "executor='claude-sdk' cannot enforce configured "
                        "governance (%s); using the governed langgraph path so "
                        "before_tool/before_model guards and input/output "
                        "guardrails are not silently bypassed.",
                        _gov,
                    )
                    _use_sdk = False

            output_items: list[dict[str, Any]] = []
            output_index = 0

            if _use_sdk:
                from ._agents import LlmAgent as _LlmAgent
                from ._claude_sdk_executor import ClaudeSDKExecutor as _CSE
                # Guaranteed by the guard above (_use_sdk is set False unless
                # _agent is an LlmAgent); narrows _agent for the type checker.
                assert isinstance(_agent, _LlmAgent)
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
                    _agent, ws=ws, model=effective_model, headers=req_headers,
                    **({"checkpointer": cp} if cp else {}),
                )
                # Approval resume: a checkpointed thread resends
                # {"resume": <decision>} to continue from the paused tool call.
                resume = _resume_decision(custom_inputs) if lg_config else None
                if resume is not None:
                    from langgraph.types import Command  # noqa: PLC0415
                    stream_input: Any = Command(resume=resume)
                else:
                    stream_input = {"messages": graph_input}
                # Combined stream: "messages" surfaces LLM token chunks (emitted
                # as response.output_text.delta for true word-by-word streaming),
                # "updates" surfaces completed graph steps (kept as the existing
                # response.output_item.done events for tool-call/step surfacing).
                # Clients correlate deltas to their done item via item_id (the
                # langchain message id, shared by a message's chunks and its final
                # assembled message).
                for mode, data in graph.stream(
                    stream_input,
                    stream_mode=["updates", "messages"],
                    **({"config": lg_config} if lg_config else {}),
                ):
                    if mode == "messages":
                        msg_chunk = data[0] if isinstance(data, tuple) else data
                        delta = _token_text(msg_chunk)
                        if delta:
                            # Mirror _langchain_to_output_item's id fallback so a
                            # chunk without an id still correlates to its done item.
                            item_id = msg_chunk.id or f"msg-{output_index}"
                            yield ResponsesAgentStreamEvent(
                                type="response.output_text.delta",
                                delta=delta,
                                item_id=item_id,
                                output_index=output_index,
                                content_index=0,
                            )
                        continue
                    if not isinstance(data, dict):
                        continue
                    for _node_name, node_output in data.items():
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

                # Mid-turn approval: a gated tool suspended the run → surface the
                # ask (done item + completed event carrying the payload). The tool
                # has NOT run; only the user's input is persisted here (the tool
                # result + answer are appended on resume).
                paused = _pending_interrupt(graph, lg_config)
                if paused is not None:
                    appr_item = _approval_output_item(paused, thread_id)
                    yield ResponsesAgentStreamEvent(
                        type="response.output_item.done",
                        item=appr_item,
                        output_index=output_index,
                    )
                    yield ResponsesAgentStreamEvent(
                        type="response.completed",
                        response={
                            "id": f"resp-{uuid.uuid4().hex[:12]}",
                            "object": "response",
                            "output": [appr_item],
                        },
                        custom_outputs={"approval_required": paused},
                    )
                    if conv_id is not None:
                        _persist_conv_turn(
                            _conversation_store,
                            conv_id,
                            input_items=list(request.input),
                            output_items=[],
                            model=effective_model,
                            response_id=str(uuid.uuid4()),
                            is_new=conv.is_new if conv is not None else False,
                        )
                    return

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

            if conv_id is not None:
                response_id = str(uuid.uuid4())
                _persist_conv_turn(
                    _conversation_store,
                    conv_id,
                    # On resume the client resends the original input, but it was
                    # already persisted on the pause turn (and Command(resume) is
                    # fed, not the messages). Persist empty input here to avoid
                    # double-writing the prompt (#375).
                    input_items=[] if resume is not None else list(request.input),
                    output_items=output_items,
                    model=effective_model,
                    response_id=response_id,
                    is_new=conv.is_new if conv is not None else False,
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
