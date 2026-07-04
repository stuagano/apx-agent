"""Tests for ``agent_tool`` — the agent-as-tool composition primitive."""

from __future__ import annotations

import json

from unittest.mock import AsyncMock

import httpx
import pytest

from apx_agent import Agent, LlmAgent, agent_tool
from apx_agent._agent_tool import remote_agent_tool
from apx_agent._inspection import _inspect_tool_fn, _make_input_model
from apx_agent._models import Message, structured_input_schema


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


# ---------------------------------------------------------------------------
# remote_agent_tool — structured schema propagation (#442)
# ---------------------------------------------------------------------------

STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {"type": "string", "description": "Fully qualified UC table"},
        "limit": {"type": "integer"},
    },
    "required": ["table"],
}


def _assert_fmapi_accepts(schema) -> None:
    """Every additionalProperties in the schema must be absent or False (#439)."""
    if isinstance(schema, dict):
        if "additionalProperties" in schema:
            assert schema["additionalProperties"] is False
        for value in schema.values():
            _assert_fmapi_accepts(value)
    elif isinstance(schema, list):
        for item in schema:
            _assert_fmapi_accepts(item)


def test_structured_schema_exposes_typed_parameters():
    """#442: the delegate built from a card's structured inputSchema exposes
    the card's properties to the framework pipeline — not one message blob."""
    fn = remote_agent_tool(
        "http://peer.internal",
        name="lookup",
        description="Look up a table",
        input_schema=dict(STRUCTURED_SCHEMA),
    )

    plain_params, dep_names = _inspect_tool_fn(fn)
    assert list(plain_params) == ["table", "limit"]
    assert dep_names == ["headers"]

    model = _make_input_model(fn, plain_params)
    assert model is not None
    schema = model.model_json_schema()
    assert set(schema["properties"]) == {"table", "limit"}
    assert schema["required"] == ["table"]
    # The exact schema that reaches FMAPI through the compile pipeline.
    _assert_fmapi_accepts(schema)


def test_hostile_card_schema_is_sanitized_before_the_delegate():
    """structured_input_schema (the vetting seam both fetch paths use) strips
    additionalProperties: true so a remote card can't 500 the parent's LLM."""
    hostile = {
        "type": "object",
        "properties": {
            "filters": {
                "type": "object",
                "properties": {"region": {"type": "string"}},
                "additionalProperties": True,
            },
        },
        "additionalProperties": True,
    }
    vetted = structured_input_schema(hostile)
    assert vetted is not None
    _assert_fmapi_accepts(vetted)

    fn = remote_agent_tool(
        "http://peer.internal", name="search", description="d", input_schema=vetted
    )
    plain_params, _ = _inspect_tool_fn(fn)
    model = _make_input_model(fn, plain_params)
    assert model is not None
    _assert_fmapi_accepts(model.model_json_schema())


def test_no_schema_keeps_the_message_wrapper():
    """Regression: without a structured card schema the delegate is unchanged."""
    fn = remote_agent_tool("http://peer.internal", name="chat", description="d")
    plain_params, dep_names = _inspect_tool_fn(fn)
    assert list(plain_params) == ["message"]
    assert dep_names == ["headers"]


@pytest.mark.asyncio
async def test_structured_invocation_transmits_json_args(monkeypatch):
    """#442 wire contract: structured kwargs cross the wire as a JSON object
    in the user message content — named fields, not prose. The transport
    payload shape (Responses `input`) is unchanged."""
    posted: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(
                200, json={"name": "peer", "description": "d", "skills": []}
            )
        posted.append(request)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "42 rows"}],
                    }
                ]
            },
        )

    transport = httpx.MockTransport(_handler)
    real = httpx.AsyncClient

    class _Routed(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs) -> None:
            kwargs.setdefault("transport", transport)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Routed)

    fn = remote_agent_tool(
        "http://peer.internal",
        name="lookup",
        description="d",
        input_schema=dict(STRUCTURED_SCHEMA),
    )
    # headers=None is the compile-path resolution outside Databricks Apps;
    # limit=None is an optional the LLM left unset — it must be dropped.
    result = await fn(table="main.sales.orders", limit=None, headers=None)

    assert result == "42 rows"
    (request,) = posted
    payload = json.loads(request.content)
    (message,) = payload["input"]
    assert message["role"] == "user"
    # The content IS the JSON args object — the peer's LLM sees named fields.
    assert json.loads(message["content"]) == {"table": "main.sales.orders"}
