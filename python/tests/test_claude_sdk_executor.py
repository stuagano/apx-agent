"""Tests for ``apx_agent._claude_sdk_executor.ClaudeSDKExecutor``.

All tests run without real Databricks credentials.  The
``_make_client`` factory at the module level is monkeypatched to return
a fake async client whose ``.chat.completions.create()`` method yields
pre-built streaming chunks.

Mock shapes use plain objects (``SimpleNamespace``) rather than
``MagicMock`` for the OpenAI streaming chunk types so that attribute
access in the executor code works exactly as it would on the real SDK
types — ``MagicMock`` attribute reads are always truthy and would mask
missing fields.

Covers:

1. ``handles_tools_internally()`` returns ``True``.
2. ``supports_streaming()`` returns ``True``.
3. No-tool turn: yields ``TextChunk`` deltas then ``TurnComplete``.
4. Tool-call turn: model requests a tool, executor calls it, re-queries
   and returns final text — events in correct order.
5. Unknown tool emits ``ToolCallComplete`` with an ``error`` field.
6. Tool-loop cap: always-tool model yields ``ExecutorError`` after
   ``MAX_TOOL_ROUNDS`` rounds.
7. ``_build_tool_schema`` produces the expected OpenAI schema shape.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated, Any

import pytest

from apx_agent._claude_sdk_executor import (
    MAX_TOOL_ROUNDS,
    ClaudeSDKExecutor,
    _build_tool_schema,
)
from apx_agent._executor import (
    ExecutorError,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    TurnComplete,
)


# ---------------------------------------------------------------------------
# Helpers — fake streaming chunks
# ---------------------------------------------------------------------------


def _text_chunk(text: str) -> SimpleNamespace:
    """Build a fake OpenAI streaming chunk that carries a text delta.

    :param text: The text content of the delta.
    :returns: A ``SimpleNamespace`` shaped like an OpenAI
        ``ChatCompletionChunk`` with a text delta and no tool calls.
    """
    delta = SimpleNamespace(content=text, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=None)
    return SimpleNamespace(choices=[choice], usage=None)


def _tool_call_chunk(
    index: int,
    call_id: str,
    fn_name: str,
    fn_args: str,
) -> SimpleNamespace:
    """Build a fake OpenAI streaming chunk carrying a tool-call fragment.

    Simulates the *first* fragment for a tool call (carries both ``id`` and
    full ``name`` / ``arguments`` in one go, matching the pattern where the
    entire tool call arrives in a single chunk rather than fragmented).

    :param index: The integer index of this tool call (used to correlate
        fragments across chunks in the real SDK; here always 0 for simplicity).
    :param call_id: The opaque call identifier, e.g. ``"call_abc123"``.
    :param fn_name: The function name string.
    :param fn_args: The JSON-serialised function arguments string.
    :returns: A ``SimpleNamespace`` shaped like an OpenAI
        ``ChatCompletionChunk`` with a tool-call delta and no text content.
    """
    fn_delta = SimpleNamespace(name=fn_name, arguments=fn_args)
    tc_delta = SimpleNamespace(index=index, id=call_id, function=fn_delta)
    delta = SimpleNamespace(content=None, tool_calls=[tc_delta])
    choice = SimpleNamespace(delta=delta, finish_reason="tool_calls")
    return SimpleNamespace(choices=[choice], usage=None)


def _finish_chunk() -> SimpleNamespace:
    """Build a fake terminal streaming chunk with no delta content.

    :returns: A ``SimpleNamespace`` with ``choices=[]`` and no usage,
        representing the final empty chunk the real SDK emits after
        ``finish_reason="stop"`` or ``finish_reason="tool_calls"``.
    """
    return SimpleNamespace(choices=[], usage=None)


async def _async_chunks(chunks: list[Any]):
    """Yield items from *chunks* one by one as an async generator.

    Used to simulate the ``async for chunk in response:`` streaming loop
    inside the executor.

    :param chunks: Pre-built chunk objects to yield.
    """
    for chunk in chunks:
        yield chunk


class FakeCompletions:
    """Fake ``AsyncCompletions`` resource.

    Stores a queue of per-call chunk lists.  Each call to ``create()``
    pops the next list from the queue and returns an async generator over
    those chunks.

    :param call_chunks: List of chunk-lists.  Each inner list is the full
        sequence of chunks returned for one ``create()`` invocation.
    """

    def __init__(self, call_chunks: list[list[Any]]) -> None:
        """Initialise the fake completions with a call queue.

        :param call_chunks: Queue of chunk lists, consumed FIFO.
        """
        self._queue: list[list[Any]] = list(call_chunks)
        self.call_count: int = 0
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any):
        """Return an async generator for the next queued chunk list.

        :param kwargs: Forwarded call kwargs (ignored; retained for
            signature compatibility).
        :returns: An async generator yielding the next queued chunks.
        :raises IndexError: If called more times than there are queued
            chunk lists.
        """
        self.call_count += 1
        self.calls.append(kwargs)
        chunks = self._queue.pop(0)
        return _async_chunks(chunks)


class FakeChatClient:
    """Fake chat client that exposes a ``completions`` resource.

    :param completions: A :class:`FakeCompletions` instance.
    """

    def __init__(self, completions: FakeCompletions) -> None:
        """Wrap *completions* as ``self.completions``.

        :param completions: The fake completions resource.
        """
        self.completions = completions


class FakeClient:
    """Top-level fake client returned by a patched ``_make_client``.

    :param chat: A :class:`FakeChatClient` instance.
    """

    def __init__(self, chat: FakeChatClient) -> None:
        """Wrap *chat* as ``self.chat``.

        :param chat: The fake chat resource.
        """
        self.chat = chat


def _make_fake_client(call_chunks: list[list[Any]]) -> FakeClient:
    """Build a :class:`FakeClient` pre-loaded with *call_chunks*.

    :param call_chunks: List of per-call chunk lists (consumed in order).
    :returns: A :class:`FakeClient` whose ``.chat.completions.create()``
        returns async generators over the queued chunks.
    """
    completions = FakeCompletions(call_chunks)
    return FakeClient(FakeChatClient(completions))


async def _drain(executor: ClaudeSDKExecutor, **kwargs: Any) -> list[Any]:
    """Exhaust ``executor.run_turn(**kwargs)`` and return all events.

    :param executor: The executor whose ``run_turn`` to drain.
    :param kwargs: Forwarded to ``run_turn``.
    :returns: A list of all yielded :class:`~apx_agent._executor.ExecutorEvent`
        objects.
    """
    return [e async for e in executor.run_turn(**kwargs)]


# ---------------------------------------------------------------------------
# 1. handles_tools_internally
# ---------------------------------------------------------------------------


class TestHandlesToolsInternally:
    def test_handles_tools_internally_true(self) -> None:
        """ClaudeSDKExecutor.handles_tools_internally() always returns True.

        The executor dispatches tool calls itself inside the agentic loop;
        callers must not try to intercept ToolCallRequest events.
        """
        executor = ClaudeSDKExecutor()
        assert executor.handles_tools_internally() is True


# ---------------------------------------------------------------------------
# 2. supports_streaming
# ---------------------------------------------------------------------------


class TestSupportsStreaming:
    def test_supports_streaming_true(self) -> None:
        """ClaudeSDKExecutor.supports_streaming() always returns True.

        Text deltas are streamed via SSE so the caller can render them
        incrementally.
        """
        executor = ClaudeSDKExecutor()
        assert executor.supports_streaming() is True


# ---------------------------------------------------------------------------
# 3. No-tool turn — yields TextChunks then TurnComplete
# ---------------------------------------------------------------------------


class TestNoToolTurn:
    async def test_run_turn_no_tools_yields_turn_complete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain text response with no tool calls yields TextChunks + TurnComplete.

        The mock client returns two text delta chunks followed by a terminal
        empty chunk.  The executor should:
        - yield one TextChunk per non-empty delta
        - yield a TurnComplete whose ``response`` is the joined text

        :param monkeypatch: pytest fixture for patching module attributes.
        """
        chunks = [
            _text_chunk("Hello, "),
            _text_chunk("world!"),
            _finish_chunk(),
        ]
        fake_client = _make_fake_client([chunks])
        monkeypatch.setattr(
            "apx_agent._claude_sdk_executor._make_client",
            lambda ws=None: fake_client,
        )

        executor = ClaudeSDKExecutor(model="test-model")
        events = await _drain(
            executor,
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=None,
        )

        text_chunks = [e for e in events if isinstance(e, TextChunk)]
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]

        assert len(text_chunks) == 2, f"Expected 2 TextChunks, got {text_chunks}"
        assert text_chunks[0].text == "Hello, "
        assert text_chunks[1].text == "world!"

        assert len(turn_completes) == 1, f"Expected 1 TurnComplete, got {turn_completes}"
        assert turn_completes[0].response == "Hello, world!"


# ---------------------------------------------------------------------------
# 4. Tool-call turn — tool executed, final answer returned
# ---------------------------------------------------------------------------


def add(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


def ping() -> str:
    """Return a pong without accepting arguments."""
    return "pong"


# Module-level DI tool — must be at module scope so that
# ``typing.get_type_hints`` can resolve the ``Annotated[...]``
# annotation via this module's ``__globals__`` dict.  A locally-scoped
# type alias inside a test method would NOT be visible to get_type_hints.
try:
    from fastapi import Depends as _Depends

    def _fake_ws_dep() -> str:
        return "ws"

    _FakeWS = Annotated[str, _Depends(_fake_ws_dep)]

    def _di_tool_for_warning_test(question: str, ws: _FakeWS) -> str:  # type: ignore[valid-type]
        """Query something with a workspace client (DI test fixture)."""
        return question

except ImportError:
    # fastapi not installed — define a dummy so the name exists
    def _di_tool_for_warning_test(question: str) -> str:  # type: ignore[misc]
        """Fallback: no-op DI test fixture."""
        return question


class TestToolCallTurn:
    async def test_run_turn_with_tool_call_executes_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the model requests a tool, the executor calls it and loops.

        Round 1: model returns a tool call for ``add(a=1, b=2)``.
        Round 2: model returns the plain text "The answer is 3".

        Expected event sequence:
        - ToolCallRequest(name="add", args={"a": 1, "b": 2})
        - ToolCallComplete(name="add", result=3, error=None)
        - TextChunk(text="The answer is 3")
        - TurnComplete(response="The answer is 3")

        :param monkeypatch: pytest fixture for patching module attributes.
        """
        import json

        round1 = [
            _tool_call_chunk(0, "call_001", "add", json.dumps({"a": 1, "b": 2})),
            _finish_chunk(),
        ]
        round2 = [
            _text_chunk("The answer is 3"),
            _finish_chunk(),
        ]
        fake_client = _make_fake_client([round1, round2])
        monkeypatch.setattr(
            "apx_agent._claude_sdk_executor._make_client",
            lambda ws=None: fake_client,
        )

        executor = ClaudeSDKExecutor(model="test-model", tools=[add])
        events = await _drain(
            executor,
            messages=[{"role": "user", "content": "What is 1+2?"}],
            tools=[],
            system_prompt="",
            config=None,
        )

        tool_reqs = [e for e in events if isinstance(e, ToolCallRequest)]
        tool_completes = [e for e in events if isinstance(e, ToolCallComplete)]
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]

        assert len(tool_reqs) == 1
        assert tool_reqs[0].name == "add"
        assert tool_reqs[0].args == {"a": 1, "b": 2}
        assert tool_reqs[0].call_id == "call_001"

        assert len(tool_completes) == 1
        assert tool_completes[0].name == "add"
        assert tool_completes[0].result == 3
        assert tool_completes[0].error is None

        assert len(turn_completes) == 1
        assert turn_completes[0].response == "The answer is 3"

    async def test_no_argument_tool_call_echoes_json_object_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The follow-up assistant message uses valid JSON for an empty call."""
        round1 = [
            _tool_call_fragment(0, call_id="call_ping", fn_name="ping"),
            _finish_chunk(),
        ]
        round2 = [_text_chunk("pong"), _finish_chunk()]
        fake_client = _make_fake_client([round1, round2])
        monkeypatch.setattr(
            "apx_agent._claude_sdk_executor._make_client",
            lambda ws=None: fake_client,
        )

        executor = ClaudeSDKExecutor(model="test-model", tools=[ping])
        events = await _drain(
            executor,
            messages=[{"role": "user", "content": "Ping."}],
            tools=[],
            system_prompt="",
            config=None,
        )

        second_call_messages = fake_client.chat.completions.calls[1]["messages"]
        assistant_tool_call = second_call_messages[1]["tool_calls"][0]
        assert assistant_tool_call["function"]["arguments"] == "{}"
        assert any(
            isinstance(event, TurnComplete) and event.response == "pong"
            for event in events
        )


# ---------------------------------------------------------------------------
# 5. Unknown tool — ToolCallComplete.error is set
# ---------------------------------------------------------------------------


class TestUnknownTool:
    async def test_unknown_tool_emits_error_and_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tool call for an unknown function sets ToolCallComplete.error.

        The executor logs a warning, emits a ``ToolCallComplete`` with
        ``error`` set to a description of the unknown tool, appends a tool
        result message indicating the error, and continues the loop.

        In this test the model then returns a plain text response, so the
        turn completes normally.

        :param monkeypatch: pytest fixture for patching module attributes.
        """
        import json

        round1 = [
            _tool_call_chunk(0, "call_999", "nonexistent_tool", json.dumps({"x": 1})),
            _finish_chunk(),
        ]
        round2 = [
            _text_chunk("I could not call the tool."),
            _finish_chunk(),
        ]
        fake_client = _make_fake_client([round1, round2])
        monkeypatch.setattr(
            "apx_agent._claude_sdk_executor._make_client",
            lambda ws=None: fake_client,
        )

        executor = ClaudeSDKExecutor(model="test-model", tools=[])
        events = await _drain(
            executor,
            messages=[{"role": "user", "content": "Do something."}],
            tools=[],
            system_prompt="",
            config=None,
        )

        tool_completes = [e for e in events if isinstance(e, ToolCallComplete)]
        assert len(tool_completes) == 1
        assert tool_completes[0].name == "nonexistent_tool"
        assert tool_completes[0].error is not None
        assert "nonexistent_tool" in tool_completes[0].error

        turn_completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_completes) == 1
        assert turn_completes[0].response == "I could not call the tool."


# ---------------------------------------------------------------------------
# 6. Tool-loop cap — ExecutorError after MAX_TOOL_ROUNDS
# ---------------------------------------------------------------------------


class TestToolLoopCap:
    async def test_tool_loop_cap_yields_executor_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ExecutorError is yielded when the tool loop exceeds MAX_TOOL_ROUNDS.

        The fake client always returns a tool call so the loop never
        terminates naturally.  After exactly MAX_TOOL_ROUNDS rounds the
        executor should yield an ``ExecutorError`` and stop.

        :param monkeypatch: pytest fixture for patching module attributes.
        """
        import json

        # Supply MAX_TOOL_ROUNDS + 1 rounds of tool-call chunks (the extra one
        # ensures we don't accidentally stop one round early).
        call_chunks = [
            [
                _tool_call_chunk(0, f"call_{i}", "add", json.dumps({"a": i, "b": 0})),
                _finish_chunk(),
            ]
            for i in range(MAX_TOOL_ROUNDS + 1)
        ]
        fake_client = _make_fake_client(call_chunks)
        monkeypatch.setattr(
            "apx_agent._claude_sdk_executor._make_client",
            lambda ws=None: fake_client,
        )

        executor = ClaudeSDKExecutor(model="test-model", tools=[add])
        events = await _drain(
            executor,
            messages=[{"role": "user", "content": "loop"}],
            tools=[],
            system_prompt="",
            config=None,
        )

        executor_errors = [e for e in events if isinstance(e, ExecutorError)]
        assert len(executor_errors) >= 1, f"No ExecutorError in events: {events}"
        assert not executor_errors[0].retryable
        # The loop should have consumed exactly MAX_TOOL_ROUNDS rounds
        assert fake_client.chat.completions.call_count == MAX_TOOL_ROUNDS


# ---------------------------------------------------------------------------
# 7. _build_tool_schema — correct OpenAI schema shape
# ---------------------------------------------------------------------------


class TestBuildToolSchema:
    def test_build_tool_schema_from_callable(self) -> None:
        """_build_tool_schema returns a well-formed OpenAI tool dict.

        For a function ``greet(name: str) -> str``, the schema must:
        - have ``type == "function"``
        - carry ``function.name == "greet"``
        - include ``"name"`` in ``function.parameters.properties``
        - include the one-line docstring as ``function.description``
        """

        def greet(name: str) -> str:
            """Say hello to someone by name."""
            return f"Hello {name}"

        schema = _build_tool_schema(greet)
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == "greet"
        assert "greet" in fn["description"] or "name" in fn["description"] or "hello" in fn["description"].lower()
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "name" in params["properties"]

    def test_build_tool_schema_no_params(self) -> None:
        """_build_tool_schema handles a zero-argument function gracefully.

        The schema ``parameters.properties`` should be an empty dict, not
        absent or ``None``.

        :returns: None
        """

        def ping() -> str:
            """Return a ping."""
            return "pong"

        schema = _build_tool_schema(ping)
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "ping"
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)


# ---------------------------------------------------------------------------
# 8. Fragmented tool-call accumulation
# ---------------------------------------------------------------------------


def _tool_call_fragment(
    index: int,
    *,
    call_id: str | None = None,
    fn_name: str | None = None,
    fn_args: str | None = None,
) -> SimpleNamespace:
    """Build a streaming chunk carrying a *partial* tool-call delta.

    Unlike :func:`_tool_call_chunk`, this helper lets callers supply only some
    fields (name/args/id) to simulate the real OpenAI behaviour where each is
    spread across multiple chunks.

    :param index: Tool-call index.
    :param call_id: Opaque call ID (``None``/absent means this fragment carries
        no call-id — the executor must take the id from the first fragment that
        does carry it).
    :param fn_name: Partial function name (``None`` means absent in this
        fragment).
    :param fn_args: Partial JSON arguments string (``None`` means absent).
    :returns: A ``SimpleNamespace`` streaming chunk.
    """
    fn_delta = SimpleNamespace(name=fn_name, arguments=fn_args)
    tc_delta = SimpleNamespace(index=index, id=call_id, function=fn_delta)
    delta = SimpleNamespace(content=None, tool_calls=[tc_delta])
    choice = SimpleNamespace(delta=delta, finish_reason="tool_calls")
    return SimpleNamespace(choices=[choice], usage=None)


class TestFragmentedToolCallAccumulation:
    async def test_arguments_spread_across_multiple_chunks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tool-call arguments split across several chunks are reassembled correctly.

        Simulates the real OpenAI streaming pattern where:
        - Chunk 1: id + name, arguments=""
        - Chunk 2: id=None, name="", arguments='{"a": 1'
        - Chunk 3: id=None, name="", arguments=', "b": 2}'

        The executor must concatenate the ``arguments`` fragments and parse the
        result as a single JSON object ``{"a": 1, "b": 2}`` before calling the
        tool.  A second LLM call then returns a plain text response.

        :param monkeypatch: pytest fixture for patching module attributes.
        """
        import json

        round1 = [
            # Fragment 1: id + name arrive first, no arguments yet
            _tool_call_fragment(0, call_id="call_frag", fn_name="add", fn_args=""),
            # Fragment 2: first half of arguments
            _tool_call_fragment(0, call_id="", fn_name="", fn_args='{"a": 1'),
            # Fragment 3: second half of arguments (completes the JSON)
            _tool_call_fragment(0, call_id="", fn_name="", fn_args=', "b": 2}'),
            _finish_chunk(),
        ]
        round2 = [
            _text_chunk("The answer is 3"),
            _finish_chunk(),
        ]
        fake_client = _make_fake_client([round1, round2])
        monkeypatch.setattr(
            "apx_agent._claude_sdk_executor._make_client",
            lambda ws=None: fake_client,
        )

        executor = ClaudeSDKExecutor(model="test-model", tools=[add])
        events = await _drain(
            executor,
            messages=[{"role": "user", "content": "What is 1+2?"}],
            tools=[],
            system_prompt="",
            config=None,
        )

        tool_reqs = [e for e in events if isinstance(e, ToolCallRequest)]
        tool_completes = [e for e in events if isinstance(e, ToolCallComplete)]
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]

        assert len(tool_reqs) == 1
        # The call id must be the one from the *first* fragment
        assert tool_reqs[0].call_id == "call_frag"
        assert tool_reqs[0].name == "add"
        # Arguments must be the fully reassembled dict
        assert tool_reqs[0].args == {"a": 1, "b": 2}

        assert len(tool_completes) == 1
        assert tool_completes[0].result == 3
        assert tool_completes[0].error is None

        assert len(turn_completes) == 1
        assert turn_completes[0].response == "The answer is 3"


# ---------------------------------------------------------------------------
# 9. create_executor factory
# ---------------------------------------------------------------------------


class TestCreateExecutor:
    def test_create_executor_returns_claude_sdk_for_llm_agent(self) -> None:
        """create_executor returns ClaudeSDKExecutor when executor='claude-sdk' for LlmAgent.

        This locks the factory dispatch so that a rename of ``agent._tool_fns``
        or ``agent._instructions`` is caught immediately.

        :returns: None
        """
        from apx_agent import LlmAgent
        from apx_agent._executor_factory import create_executor
        from apx_agent._models import AgentConfig

        agent = LlmAgent(tools=[])
        config = AgentConfig(
            name="test-agent",
            model="databricks-claude-sonnet-4-6",
            executor="claude-sdk",
        )
        executor = create_executor(agent, config, ws=None)
        assert isinstance(executor, ClaudeSDKExecutor)

    def test_create_executor_falls_back_for_non_llm_agent(self) -> None:
        """create_executor falls back to LangGraphExecutor for non-LlmAgent types.

        SequentialAgent (and other composite topologies) are not supported by
        ClaudeSDKExecutor.  The factory must fall back to LangGraphExecutor and
        log a warning rather than raise.

        :returns: None
        """
        from apx_agent import LlmAgent, SequentialAgent
        from apx_agent._executor_factory import create_executor
        from apx_agent._langgraph_executor import LangGraphExecutor
        from apx_agent._models import AgentConfig

        inner = LlmAgent(tools=[])
        agent = SequentialAgent(agents=[inner])
        config = AgentConfig(
            name="test-seq",
            model="databricks-claude-sonnet-4-6",
            executor="claude-sdk",
        )
        executor = create_executor(agent, config, ws=None)
        assert isinstance(executor, LangGraphExecutor)

    def test_create_executor_default_returns_langgraph(self) -> None:
        """create_executor with default executor returns LangGraphExecutor.

        AgentConfig.executor defaults to 'langgraph'.  This test ensures the
        default path (no explicit executor field) does not accidentally route
        to ClaudeSDKExecutor.

        :returns: None
        """
        from apx_agent import LlmAgent
        from apx_agent._executor_factory import create_executor
        from apx_agent._langgraph_executor import LangGraphExecutor
        from apx_agent._models import AgentConfig

        agent = LlmAgent(tools=[])
        config = AgentConfig(name="default-agent")
        executor = create_executor(agent, config, ws=None)
        assert isinstance(executor, LangGraphExecutor)


# ---------------------------------------------------------------------------
# 10. DI tool warning at construction time
# ---------------------------------------------------------------------------


class TestDIToolWarning:
    def test_di_tool_emits_warning(self) -> None:
        """ClaudeSDKExecutor logs a WARNING for tools with DI parameters.

        A tool function whose signature uses ``Annotated[..., Depends(...)]``
        — the same pattern as ``Dependencies.UserClient`` — will not work
        correctly under ClaudeSDKExecutor because the FastAPI DI framework is
        not present.  The executor must warn at construction time so the
        developer sees the problem immediately rather than at runtime.

        Uses :meth:`unittest.TestCase.assertLogs` to reliably capture log
        records regardless of pytest's root-logger configuration.

        The DI tool (``_di_tool_for_warning_test``) is defined at module level
        so that ``typing.get_type_hints`` can resolve its ``Annotated[...]``
        annotation via the module's ``__globals__`` dict.  Locally-defined type
        aliases inside a test method are not visible to ``get_type_hints``.
        """
        import logging
        from unittest import TestCase

        try:
            import fastapi  # noqa: F401
        except ImportError:
            pytest.skip("fastapi not installed; DI detection test requires it")

        tc = TestCase()
        tc.maxDiff = None
        with tc.assertLogs("apx_agent._claude_sdk_executor", level=logging.WARNING) as cm:
            ClaudeSDKExecutor(tools=[_di_tool_for_warning_test])

        assert any("_di_tool_for_warning_test" in msg for msg in cm.output), (
            f"Expected warning about '_di_tool_for_warning_test'; got: {cm.output}"
        )
