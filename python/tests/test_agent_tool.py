"""Tests for ``agent_tool`` — the agent-as-tool composition primitive."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apx_agent import Agent, LlmAgent, agent_tool
from apx_agent._inspection import _inspect_tool_fn, _make_input_model
from apx_agent._models import Message


# ---------------------------------------------------------------------------
# Schema integration
# ---------------------------------------------------------------------------

def test_wrapped_function_has_correct_name_from_explicit_arg():
    """Explicit ``name=`` wins over agent's _name and class name."""
    inner = LlmAgent(tools=[], name="some_inner")
    fn = agent_tool(inner, name="explicit_name", description="d")
    assert fn.__name__ == "explicit_name"


def test_wrapped_function_falls_back_to_agent_name():
    """When ``name=`` is omitted, use the agent's _name (snake-cased)."""
    inner = LlmAgent(tools=[], name="DataInspector")
    fn = agent_tool(inner)
    assert fn.__name__ == "data_inspector"


def test_wrapped_function_falls_back_to_class_name():
    """When agent has no _name, fall back to snake_case(class.__name__)."""
    inner = LlmAgent(tools=[])  # no name
    fn = agent_tool(inner)
    assert fn.__name__ == "llm_agent"


def test_wrapped_function_carries_description():
    inner = LlmAgent(tools=[])
    fn = agent_tool(inner, description="Handle billing questions.")
    assert fn.__doc__ == "Handle billing questions."


# ---------------------------------------------------------------------------
# Tool inspection — the framework must see only ``message`` as LLM-visible
# ---------------------------------------------------------------------------

def test_inspection_exposes_only_message_param_to_llm():
    """``request`` is a FastAPI dep; only ``message`` should be LLM-visible."""
    inner = LlmAgent(tools=[], name="inner")
    fn = agent_tool(inner)

    plain_params, dep_names = _inspect_tool_fn(fn)

    assert list(plain_params.keys()) == ["message"]
    assert dep_names == ["request"]


def test_inspection_builds_input_model_with_message_str():
    inner = LlmAgent(tools=[], name="inner")
    fn = agent_tool(inner)
    plain_params, _ = _inspect_tool_fn(fn)

    model = _make_input_model(fn, plain_params)
    assert model is not None
    instance = model.model_validate({"message": "hello"})
    assert instance.message == "hello"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Behavior — the wrapper actually delegates to agent.run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrapper_invokes_inner_agent_run():
    """The tool's body must call ``agent.run`` with the message wrapped as a Message."""
    inner = LlmAgent(tools=[], name="inner")
    inner.run = AsyncMock(return_value="inner agent's answer")  # type: ignore[method-assign]

    fn = agent_tool(inner)
    sentinel_request = object()

    result = await fn("user question", sentinel_request)  # type: ignore[arg-type]

    assert result == "inner agent's answer"
    inner.run.assert_awaited_once()
    call_args = inner.run.await_args
    messages = call_args.args[0]
    assert len(messages) == 1
    assert isinstance(messages[0], Message)
    assert messages[0].role == "user"
    assert messages[0].content == "user question"
    assert call_args.args[1] is sentinel_request


# ---------------------------------------------------------------------------
# Composition — can be added to an outer LlmAgent's tools list
# ---------------------------------------------------------------------------

def test_wrapped_function_is_installable_as_a_tool():
    """An outer LlmAgent should accept the wrapped function without complaint."""
    inner = LlmAgent(tools=[], name="specialist")
    outer = LlmAgent(tools=[agent_tool(inner, description="Talk to specialist.")])

    # Construction succeeded; the framework's pre-analysis pass should have
    # recognized the function as a valid tool.
    assert len(outer._tool_fns) == 1
    assert outer._analyzed[0][0].__name__ == "specialist"
