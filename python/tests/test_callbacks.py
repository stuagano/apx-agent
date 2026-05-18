"""Tests for _callbacks.py — LlmAgent hooks bridged through LangChain callbacks.

Covers:
  - build_callback_handler returns None when no hooks are set, an instance
    when any one of the four is set.
  - The handler routes LangChain lifecycle events to the right hook.
  - Sync hooks invoked directly. Async hooks scheduled (we don't assert
    completion order, just that they're called).
  - Tool input/output threading: on_tool_start records the inputs by
    run_id; on_tool_end pops and passes them to after_tool.
  - on_tool_error cleans up tracked state.
  - Hook exceptions propagate (with the original traceback).

Uses an LlmAgent stub so we don't depend on the full compile path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("langchain_core")

from apx_agent._callbacks import (  # noqa: E402
    _AgentCallbackHandler,
    build_callback_handler,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _StubLlmAgent:
    """Minimal stand-in with the same hook attributes LlmAgent exposes."""

    def __init__(
        self,
        *,
        before_tool: Any = None,
        after_tool: Any = None,
        before_model: Any = None,
        after_model: Any = None,
    ) -> None:
        self._before_tool = before_tool
        self._after_tool = after_tool
        self._before_model = before_model
        self._after_model = after_model


# ---------------------------------------------------------------------------
# build_callback_handler
# ---------------------------------------------------------------------------


def test_build_returns_none_when_no_hooks() -> None:
    assert build_callback_handler(_StubLlmAgent()) is None


def test_build_returns_handler_when_any_hook_set() -> None:
    def noop(*args: Any) -> None: ...
    for kwarg in ("before_tool", "after_tool", "before_model", "after_model"):
        handler = build_callback_handler(_StubLlmAgent(**{kwarg: noop}))
        assert handler is not None, f"hook {kwarg} should produce a handler"


# ---------------------------------------------------------------------------
# Model lifecycle
# ---------------------------------------------------------------------------


def test_on_llm_start_fires_before_model() -> None:
    before = MagicMock()
    h = _AgentCallbackHandler(before_model=before)
    h.on_llm_start({}, ["prompt"])
    before.assert_called_once_with(["prompt"])


def test_on_chat_model_start_fires_before_model() -> None:
    before = MagicMock()
    h = _AgentCallbackHandler(before_model=before)
    messages = [[{"role": "user", "content": "hi"}]]
    h.on_chat_model_start({}, messages)
    before.assert_called_once_with(messages)


def test_on_llm_end_fires_after_model() -> None:
    after = MagicMock()
    h = _AgentCallbackHandler(after_model=after)
    fake_result = MagicMock(name="LLMResult")
    h.on_llm_end(fake_result)
    after.assert_called_once_with(fake_result)


def test_model_hooks_no_op_when_not_set() -> None:
    # No before/after model hooks — events fire harmlessly.
    h = _AgentCallbackHandler()
    h.on_llm_start({}, ["prompt"])
    h.on_chat_model_start({}, [[]])
    h.on_llm_end(MagicMock())


# ---------------------------------------------------------------------------
# Tool lifecycle
# ---------------------------------------------------------------------------


def test_on_tool_start_fires_before_tool_with_name_and_inputs() -> None:
    before = MagicMock()
    h = _AgentCallbackHandler(before_tool=before)
    inputs = {"query": "select 1"}
    h.on_tool_start({"name": "run_sql"}, "select 1", inputs=inputs, run_id=uuid4())
    before.assert_called_once_with("run_sql", inputs)


def test_on_tool_start_falls_back_to_input_str_when_no_inputs_kwarg() -> None:
    before = MagicMock()
    h = _AgentCallbackHandler(before_tool=before)
    h.on_tool_start({"name": "run_sql"}, "select 1", run_id=uuid4())
    before.assert_called_once_with("run_sql", {"input": "select 1"})


def test_tool_end_pops_inputs_and_passes_to_after_tool() -> None:
    after = MagicMock()
    h = _AgentCallbackHandler(after_tool=after)
    run_id = uuid4()
    inputs = {"x": 1}
    h.on_tool_start({"name": "do_thing"}, "irrelevant", inputs=inputs, run_id=run_id)
    h.on_tool_end({"rows": []}, run_id=run_id)
    after.assert_called_once_with("do_thing", inputs, {"rows": []})
    # State cleaned up
    assert run_id not in h._tool_inputs


def test_tool_error_cleans_up_state() -> None:
    h = _AgentCallbackHandler(after_tool=MagicMock())
    run_id = uuid4()
    h.on_tool_start({"name": "foo"}, "x", inputs={}, run_id=run_id)
    h.on_tool_error(RuntimeError("kaboom"), run_id=run_id)
    assert run_id not in h._tool_inputs


def test_tool_end_without_matching_start_still_calls_after_tool() -> None:
    """If on_tool_end fires for a run_id we never saw, fall back to empty inputs."""
    after = MagicMock()
    h = _AgentCallbackHandler(after_tool=after)
    h.on_tool_end({"rows": []}, run_id=uuid4())
    after.assert_called_once_with("", {}, {"rows": []})


# ---------------------------------------------------------------------------
# Async hooks
# ---------------------------------------------------------------------------


def test_async_before_model_hook_is_invoked() -> None:
    invocations: list[Any] = []

    async def before_async(prompts: Any) -> None:
        invocations.append(prompts)

    h = _AgentCallbackHandler(before_model=before_async)
    h.on_llm_start({}, ["sync-call"])

    # Async hook runs via asyncio.run when no loop is active; should have
    # completed by the time on_llm_start returns.
    assert invocations == [["sync-call"]]


# ---------------------------------------------------------------------------
# Exception propagation
# ---------------------------------------------------------------------------


def test_before_tool_exception_propagates() -> None:
    def boom(name: str, args: dict[str, Any]) -> None:
        raise RuntimeError("blocked by policy")

    h = _AgentCallbackHandler(before_tool=boom)
    with pytest.raises(RuntimeError, match="blocked by policy"):
        h.on_tool_start({"name": "foo"}, "input", inputs={}, run_id=uuid4())


def test_after_model_exception_propagates() -> None:
    def boom(result: Any) -> None:
        raise ValueError("post-hoc check failed")

    h = _AgentCallbackHandler(after_model=boom)
    with pytest.raises(ValueError, match="post-hoc check failed"):
        h.on_llm_end(MagicMock())
