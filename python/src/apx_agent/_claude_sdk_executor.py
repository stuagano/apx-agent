"""ClaudeSDKExecutor — OpenAI-compatible agentic loop without LangGraph.

Uses ``openai.AsyncOpenAI`` (or the Databricks-authenticated wrapper
``AsyncDatabricksOpenAI``) to run a full tool loop directly against any
Databricks Model Serving endpoint, bypassing the LangGraph compilation
overhead.

Design note: the actual HTTP client is created lazily by :func:`_make_client`
so that tests can monkeypatch ``apx_agent._claude_sdk_executor._make_client``
with a factory that returns a fake client — no real Databricks credentials
needed for unit tests.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from ._executor import (
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    TurnComplete,
)
from ._inspection import _inspect_tool_fn, _make_input_model, _schema_for_model

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 20
"""Safety cap on tool call rounds per turn.

Prevents runaway loops when the model repeatedly calls tools without
converging on a final text answer.
"""


# ---------------------------------------------------------------------------
# Client factory — kept as a module-level function so tests can patch it
# ---------------------------------------------------------------------------


def _make_client(ws: Any = None) -> Any:
    """Return an authenticated async OpenAI-compatible client for Databricks.

    If *ws* is provided, uses :class:`~databricks_openai.AsyncDatabricksOpenAI`
    so that on-behalf-of (OBO) auth flows through the WorkspaceClient's
    credential provider.  When *ws* is ``None``, falls back to the default
    :class:`~databricks_openai.AsyncDatabricksOpenAI` which reads
    ``DATABRICKS_HOST`` / ``DATABRICKS_TOKEN`` from the environment.

    This function is a deliberately thin wrapper around the real client
    constructor.  Tests monkeypatch this name at the module level so no
    real Databricks credentials are required::

        monkeypatch.setattr(
            "apx_agent._claude_sdk_executor._make_client",
            lambda ws=None: my_fake_client,
        )

    :param ws: Optional :class:`~databricks.sdk.WorkspaceClient` for OBO
        auth.  ``None`` uses the SDK default auth chain.
    :returns: An ``AsyncOpenAI``-compatible client whose
        ``.chat.completions.create()`` accepts the standard OpenAI
        chat-completions kwargs.
    """
    from databricks_openai import AsyncDatabricksOpenAI

    if ws is not None:
        return AsyncDatabricksOpenAI(workspace_client=ws)
    return AsyncDatabricksOpenAI()


# ---------------------------------------------------------------------------
# Tool-schema helpers
# ---------------------------------------------------------------------------


def _build_tool_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    """Build an OpenAI function-calling tool schema from a Python callable.

    Inspects *fn* using the project's ``_inspect_tool_fn`` helper to
    discover non-dependency parameters, creates a Pydantic input model, and
    serialises it to a JSON Schema dict suitable for the OpenAI
    ``tools`` parameter.

    The resulting schema has the form::

        {
            "type": "function",
            "function": {
                "name": "my_tool",
                "description": "One-line docstring.",
                "parameters": {
                    "type": "object",
                    "properties": { ... },
                    "required": [ ... ],
                    "additionalProperties": False,
                },
            },
        }

    .. note::
        ``additionalProperties: false`` is NOT included when the schema is
        produced for non-GPT Databricks endpoints, because those endpoints
        do not support the OpenAI ``strict`` mode.  The ``strict`` field
        itself is intentionally omitted for the same reason.

    :param fn: The Python callable to introspect.  Must carry ``__name__``
        and (ideally) ``__doc__``.
    :returns: A dict in OpenAI ``tools`` list item format.
    """
    sig = _inspect_tool_fn(fn)
    input_model = _make_input_model(fn, sig.plain_params)
    schema: dict[str, Any]
    if input_model is not None:
        raw = _schema_for_model(input_model) or {}
        # model_json_schema() may wrap parameters in 'properties'; normalise
        # to a clean object schema for the LLM.
        schema = {
            "type": "object",
            "properties": raw.get("properties", {}),
        }
        required = raw.get("required", [])
        if required:
            schema["required"] = required
    else:
        # No plain parameters — accept an empty object
        schema = {"type": "object", "properties": {}}

    description = (fn.__doc__ or "").strip().split("\n")[0] or fn.__name__
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": description,
            "parameters": schema,
        },
    }


def _call_tool(fn: Callable[..., Any], args: dict[str, Any]) -> Any:
    """Invoke a tool callable with the given keyword arguments.

    Filters *args* to only include keys that match non-dependency parameters
    on *fn* (using :func:`~apx_agent._inspection._inspect_tool_fn`).  This
    prevents the LLM from accidentally injecting FastAPI ``Depends``
    parameters, which must come from the DI framework.

    :param fn: The tool callable to invoke.  May be sync or async; the
        caller is responsible for ``await``-ing the result when
        :func:`inspect.isawaitable` returns ``True``.
    :param args: Keyword arguments as decoded from the model's JSON string.
    :returns: The raw return value of *fn*.  Caller should ``await`` when
        the result is a coroutine.
    :raises Exception: Propagates any exception raised by *fn*.
    """
    sig = _inspect_tool_fn(fn)
    # Only pass args whose names are plain (non-dependency) parameters
    safe_args = {k: v for k, v in args.items() if k in sig.plain_params}
    return fn(**safe_args)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class ClaudeSDKExecutor:
    """Executor that runs a native OpenAI-compatible agentic loop without LangGraph.

    Uses :func:`_make_client` to obtain an authenticated async client pointed
    at Databricks Model Serving, then drives the full LLM + tool-call loop
    internally.  Emits :class:`~apx_agent._executor.ToolCallRequest` and
    :class:`~apx_agent._executor.ToolCallComplete` as *informational* events
    (the loop does not stop and wait for external tool dispatch).

    Suitable for :class:`~apx_agent._agents.LlmAgent` instances with a flat
    tool list.  Does **not** support composite agent topologies
    (SequentialAgent, LoopAgent, etc.) — use
    :class:`~apx_agent._langgraph_executor.LangGraphExecutor` for those.

    .. warning:: **Dependency-injected tools are not supported.**

        Tools that use ``fastapi.Depends`` parameters (e.g.
        ``Dependencies.UserClient``) will have those parameters stripped by
        :func:`~apx_agent._inspection._inspect_tool_fn`, which means they
        will be called *without* the required workspace client or OBO token —
        causing a ``TypeError`` at call time.  If your tool list includes DI
        parameters, use :class:`~apx_agent._langgraph_executor.LangGraphExecutor`
        instead, which resolves dependencies through FastAPI's DI framework.
        A :class:`logging.WARNING` is emitted at construction time for each
        affected tool.

    :param model: Default model endpoint name, e.g.
        ``"databricks-claude-sonnet-4-6"``.  May be overridden per-turn via
        :attr:`~apx_agent._executor.ExecutorConfig.model`.
    :param tools: Tool callables to register with the LLM.  Additional tools
        may also be passed per-turn via ``run_turn(tools=...)``.
    :param instructions: Default system prompt.  Per-turn ``system_prompt``
        overrides this when non-empty.
    :param ws: Optional :class:`~databricks.sdk.WorkspaceClient` for OBO
        auth.  ``None`` falls back to the SDK default auth chain inside
        :func:`_make_client`.
    :param thinking: Optional extended-thinking config dict, e.g.
        ``{"type": "enabled", "budget_tokens": 5000}``.  When set, passed
        as ``extra_body={"thinking": ...}`` in the
        :class:`~databricks_openai.AsyncDatabricksOpenAI` call kwargs.
        Only honoured by Claude models that support extended thinking.

    Example::

        def add(a: int, b: int) -> int:
            "Add two integers."
            return a + b

        executor = ClaudeSDKExecutor(
            model="databricks-claude-sonnet-4-6",
            tools=[add],
        )
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "What is 2+3?"}],
            tools=[],
            system_prompt="You are a calculator.",
            config=None,
        ):
            print(event)
    """

    def __init__(
        self,
        model: str | None = None,
        tools: list[Callable[..., Any]] | None = None,
        instructions: str | None = None,
        ws: Any = None,
        thinking: dict[str, Any] | None = None,
    ) -> None:
        """Initialise the executor with default model, tools, and auth.

        :param model: Default model endpoint name.
        :param tools: List of Python callables to register as tools.
        :param instructions: Default system prompt text.
        :param ws: Optional WorkspaceClient for OBO auth.
        :param thinking: Optional extended-thinking config passed to the model
            verbatim.  E.g. ``{"type": "enabled", "budget_tokens": 5000}``.
        """
        self._model = model
        self._tools: list[Callable[..., Any]] = list(tools or [])
        self._instructions = instructions or ""
        self._ws = ws
        self._thinking = thinking

        # Warn immediately if any tools have dependency-injected parameters —
        # ClaudeSDKExecutor calls tools without DI framework support, so those
        # parameters will be absent at call time (causing TypeError).
        for fn in self._tools:
            sig = _inspect_tool_fn(fn)
            if sig.dep_param_names:
                logger.warning(
                    "ClaudeSDKExecutor: tool %r has dependency-injected parameters "
                    "%r that will NOT be resolved (no FastAPI DI context). "
                    "The tool will receive only plain LLM-supplied arguments and will "
                    "raise TypeError if any dep param is required. "
                    "Use LangGraphExecutor for tools with Dependencies.* parameters.",
                    fn.__name__,
                    sorted(sig.dep_param_names),
                )

    def handles_tools_internally(self) -> bool:
        """Return ``True`` — this executor dispatches tool calls in the loop.

        The caller must **not** attempt to intercept
        :class:`~apx_agent._executor.ToolCallRequest` events from this
        executor; tool calls are executed internally and
        :class:`~apx_agent._executor.ToolCallComplete` is emitted once the
        result is available.

        :returns: Always ``True``.
        """
        return True

    def supports_streaming(self) -> bool:
        """Return ``True`` — text deltas are streamed via SSE.

        :returns: Always ``True``.
        """
        return True

    async def run_turn(
        self,
        messages: list[Any],
        tools: list[Any],
        system_prompt: str,
        config: ExecutorConfig | None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one agentic turn (LLM + tool loop) and yield events.

        The turn runs until:

        - The model returns a final text response with no tool calls — a
          :class:`~apx_agent._executor.TurnComplete` event is emitted.
        - The tool-call loop exceeds :data:`MAX_TOOL_ROUNDS` — an
          :class:`~apx_agent._executor.ExecutorError` is emitted.
        - An unrecoverable exception is raised — an
          :class:`~apx_agent._executor.ExecutorError` is emitted.

        Per-turn *tools* (callables) are merged with the executor-level
        ``self._tools``.  Non-callable items in *tools* are silently
        ignored (they may be schema dicts from other callers; we only
        handle Python callables here).

        :param messages: Conversation history as a list of dicts with
            ``"role"`` and ``"content"`` keys, or objects with those attrs.
        :param tools: Additional tool callables for this turn only.  Merged
            with executor-level tools.  Non-callable items are skipped.
        :param system_prompt: System prompt to prepend.  When non-empty,
            overrides the executor-level ``self._instructions``.
        :param config: Per-turn generation config.  ``None`` uses executor
            defaults.  :attr:`~apx_agent._executor.ExecutorConfig.model`
            overrides :attr:`self._model` when set.
        :returns: An async iterator of
            :class:`~apx_agent._executor.ExecutorEvent` objects.  The last
            event is always :class:`~apx_agent._executor.TurnComplete` or
            :class:`~apx_agent._executor.ExecutorError`.
        """
        resolved_model = (config.model if config and config.model else None) or self._model or ""
        # Merge per-turn callable tools with executor-level tools
        per_turn_callables = [t for t in tools if callable(t)]
        all_tools = list(self._tools) + per_turn_callables
        resolved_system = system_prompt or self._instructions or ""

        # Build the messages list in OpenAI format
        lm_messages: list[dict[str, Any]] = []
        if resolved_system:
            lm_messages.append({"role": "system", "content": resolved_system})
        for m in messages:
            if isinstance(m, dict):
                lm_messages.append(m)
            else:
                lm_messages.append(
                    {
                        "role": getattr(m, "role", "user"),
                        "content": getattr(m, "content", str(m)),
                    }
                )

        tool_schemas = [_build_tool_schema(fn) for fn in all_tools]
        tool_map: dict[str, Callable[..., Any]] = {fn.__name__: fn for fn in all_tools}

        client = _make_client(self._ws)

        try:
            for _round in range(MAX_TOOL_ROUNDS):
                # ----------------------------------------------------------------
                # Build call kwargs
                # ----------------------------------------------------------------
                call_kwargs: dict[str, Any] = {
                    "model": resolved_model,
                    "messages": lm_messages,
                    "stream": True,
                    "temperature": config.temperature if config else 0.0,
                    "max_tokens": config.max_tokens if config else 100_000,
                }
                if tool_schemas:
                    call_kwargs["tools"] = tool_schemas
                    call_kwargs["tool_choice"] = "auto"
                if self._thinking:
                    call_kwargs["thinking"] = self._thinking

                # ----------------------------------------------------------------
                # Stream the response, accumulating text and tool call fragments
                # ----------------------------------------------------------------
                text_parts: list[str] = []
                # tool_calls_raw[i] = {"id": ..., "type": "function",
                #                      "function": {"name": ..., "arguments": ...}}
                tool_calls_by_index: dict[int, dict[str, Any]] = {}
                usage: dict[str, Any] | None = None

                response = await client.chat.completions.create(**call_kwargs)
                async for chunk in response:
                    choices = chunk.choices if chunk.choices else []
                    delta = choices[0].delta if choices else None
                    if delta is not None:
                        # ---- text delta ----
                        content = getattr(delta, "content", None)
                        if content:
                            text_parts.append(content)
                            yield TextChunk(text=content)

                        # ---- tool call fragments ----
                        tc_deltas = getattr(delta, "tool_calls", None)
                        if tc_deltas:
                            for tc_delta in tc_deltas:
                                idx = getattr(tc_delta, "index", 0)
                                if idx not in tool_calls_by_index:
                                    tool_calls_by_index[idx] = {
                                        "id": getattr(tc_delta, "id", "") or "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                entry = tool_calls_by_index[idx]
                                # id may arrive in later chunks; take first non-empty
                                if not entry["id"] and getattr(tc_delta, "id", None):
                                    entry["id"] = tc_delta.id
                                fn_delta = getattr(tc_delta, "function", None)
                                if fn_delta:
                                    fn_name = getattr(fn_delta, "name", None)
                                    fn_args = getattr(fn_delta, "arguments", None)
                                    if fn_name:
                                        entry["function"]["name"] += fn_name
                                    if fn_args:
                                        entry["function"]["arguments"] += fn_args

                    # Usage may arrive on a trailing empty-choice chunk
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage = {
                            "input_tokens": getattr(chunk_usage, "prompt_tokens", 0),
                            "output_tokens": getattr(chunk_usage, "completion_tokens", 0),
                        }

                full_text = "".join(text_parts)
                tool_calls_raw = [
                    tool_calls_by_index[k] for k in sorted(tool_calls_by_index)
                ]
                for tool_call in tool_calls_raw:
                    if not tool_call["function"]["arguments"].strip():
                        tool_call["function"]["arguments"] = "{}"

                if not tool_calls_raw:
                    # No tool calls requested — the turn is done
                    yield TurnComplete(response=full_text or None, usage=usage)
                    return

                # ----------------------------------------------------------------
                # Append assistant message with tool calls, then execute each one
                # ----------------------------------------------------------------
                lm_messages.append(
                    {
                        "role": "assistant",
                        # Claude rejects empty content alongside tool_calls;
                        # the DatabricksOpenAI wrapper replaces "" with " ",
                        # but being explicit is cleaner.
                        "content": full_text if full_text else " ",
                        "tool_calls": tool_calls_raw,
                    }
                )

                for tc in tool_calls_raw:
                    fn_name = tc.get("function", {}).get("name", "")
                    fn_args_raw = tc.get("function", {}).get("arguments", "{}")
                    call_id = tc.get("id", "")

                    try:
                        fn_args = (
                            json.loads(fn_args_raw)
                            if isinstance(fn_args_raw, str)
                            else fn_args_raw
                        )
                    except (json.JSONDecodeError, TypeError):
                        fn_args = {}

                    yield ToolCallRequest(name=fn_name, args=fn_args, call_id=call_id)

                    fn = tool_map.get(fn_name)
                    if fn is None:
                        err = f"Unknown tool: {fn_name!r}"
                        logger.warning("ClaudeSDKExecutor: %s", err)
                        yield ToolCallComplete(name=fn_name, call_id=call_id, error=err)
                        lm_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": json.dumps({"error": err}),
                            }
                        )
                    else:
                        try:
                            result = _call_tool(fn, fn_args)
                            if inspect.isawaitable(result):
                                result = await result
                            yield ToolCallComplete(name=fn_name, call_id=call_id, result=result)
                            result_str = (
                                result
                                if isinstance(result, str)
                                else json.dumps(result, default=str)
                            )
                            lm_messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "content": result_str,
                                }
                            )
                        except Exception as exc:
                            err_msg = str(exc)
                            logger.warning(
                                "ClaudeSDKExecutor: tool %r raised: %s", fn_name, err_msg
                            )
                            yield ToolCallComplete(
                                name=fn_name, call_id=call_id, error=err_msg
                            )
                            lm_messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "content": json.dumps({"error": err_msg}),
                                }
                            )

            # Exhausted MAX_TOOL_ROUNDS without a terminal text response
            yield ExecutorError(
                message=f"Tool loop exceeded {MAX_TOOL_ROUNDS} rounds without a final response.",
                retryable=False,
            )

        except Exception as exc:
            logger.exception("ClaudeSDKExecutor.run_turn failed: %s", exc)
            yield ExecutorError(message=str(exc), retryable=False)
