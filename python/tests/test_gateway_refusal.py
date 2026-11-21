"""A Unity AI Gateway DENY is a typed refusal plus a service-policy audit.

The payloads are the shapes the chat and MCP clients already see. Nothing
here calls a gateway.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("databricks_langchain")
pytest.importorskip("langchain")
pytest.importorskip("mcp")

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from mcp import McpError  # noqa: E402
from mcp.types import ErrorData  # noqa: E402
from openai.types.chat import ChatCompletion  # noqa: E402

from apx_agent import GatewayRefusal, mcp_tool  # noqa: E402
from apx_agent._audit import AuditAttrs, hash_for_audit  # noqa: E402
from apx_agent._compile import (  # noqa: E402
    _gateway_refusal_middleware,
    _governance_exception_middleware,
)
from apx_agent._gateway_refusal import gateway_refusal  # noqa: E402
from apx_agent._llm import get_llm  # noqa: E402

_POLICY_NAME = "main.governance.block_confidential_codename"
_REASON = "blocked because the prompt names a confidential codename"
_DEFAULT_REASON = (
    "Access denied: this request is not permitted by a policy on this service."
)


def _policy(**overrides: str | int) -> dict[str, str | int]:
    body: dict[str, str | int] = {
        "name": _POLICY_NAME,
        "kind": "sql",
        "phase": "on_call",
        "rank": 100,
        "mode": "enforce",
        "action": "DENY",
        "reason": _REASON,
    }
    body.update(overrides)
    return body


def _completion(policy: dict[str, str | int] | None) -> ChatCompletion:
    raw: dict[str, Any] = {
        "id": "chatcmpl-deny",
        "object": "chat.completion",
        "created": 1,
        "model": "databricks-claude-sonnet-4-6",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "The policy blocked this request.",
                },
            }
        ],
    }
    if policy is not None:
        raw["databricks_service_policy"] = policy
    return ChatCompletion.model_validate(raw)


def _fingerprint(policy: dict[str, str | int]) -> str:
    return hash_for_audit(
        json.dumps(
            {
                "name": policy["name"],
                "kind": policy["kind"],
                "phase": "on_call",
                "rank": policy["rank"],
                "mode": policy["mode"],
                "action": "DENY",
            },
            sort_keys=True,
        )
    )


def _recorded(span: MagicMock) -> dict[str, str | int]:
    return {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}


def _assert_gateway_audit(span: MagicMock, policy: dict[str, str | int]) -> None:
    recorded = _recorded(span)
    assert recorded[AuditAttrs.SERVICE_POLICY_NAME] == policy["name"]
    assert recorded[AuditAttrs.SERVICE_POLICY_KIND] == policy["kind"]
    assert recorded[AuditAttrs.SERVICE_POLICY_PHASE] == "on_call"
    assert recorded[AuditAttrs.SERVICE_POLICY_RANK] == policy["rank"]
    assert recorded[AuditAttrs.SERVICE_POLICY_MODE] == policy["mode"]
    assert recorded[AuditAttrs.SERVICE_POLICY_ACTION] == "DENY"
    assert recorded[AuditAttrs.SERVICE_POLICY_ADAPTER] == "gateway"
    assert recorded[AuditAttrs.SERVICE_POLICY_FINGERPRINT] == _fingerprint(policy)
    for value in recorded.values():
        assert _REASON not in str(value)


def test_model_deny_completion_is_a_typed_refusal_with_audit() -> None:
    """The chat client sees HTTP 200 plus databricks_service_policy."""
    policy = _policy()
    completion = _completion(policy)
    assert completion.model_extra is not None
    assert "databricks_service_policy" in completion.model_extra
    llm = get_llm("databricks-claude-sonnet-4-6")
    assert type(llm).__name__ == "ChatDatabricks"
    span = MagicMock()
    with patch("apx_agent._gateway_refusal.current_active_span", return_value=span):
        with pytest.raises(GatewayRefusal) as raised:
            llm._convert_response_to_chat_result(completion)
    refusal = raised.value
    assert isinstance(refusal, PermissionError)
    assert refusal.surface == "model"
    assert refusal.action == "DENY"
    assert refusal.policy_name == _POLICY_NAME
    message = str(refusal)
    assert message == (
        "The Unity AI Gateway refused this model call "
        f"(policy {_POLICY_NAME}): {_REASON}"
    )
    _assert_gateway_audit(span, policy)


def test_model_completion_without_a_policy_stays_a_chat_result() -> None:
    llm = get_llm("databricks-claude-sonnet-4-6")
    span = MagicMock()
    with patch("apx_agent._gateway_refusal.current_active_span", return_value=span):
        result = llm._convert_response_to_chat_result(_completion(None))
    assert result.generations[0].message.content == "The policy blocked this request."
    assert span.set_attribute.call_args_list == []


def test_allow_and_ask_are_not_refusals() -> None:
    span = MagicMock()
    with patch("apx_agent._gateway_refusal.current_active_span", return_value=span):
        assert gateway_refusal(
            {"databricks_service_policy": _policy(action="ALLOW")},
            surface="model",
        ) is None
        assert gateway_refusal(
            {"databricks_service_policy": _policy(action="ASK")},
            surface="mcp",
        ) is None
    assert span.set_attribute.call_args_list == []


def test_policy_object_without_an_action_is_a_deny() -> None:
    policy = _policy()
    del policy["action"]
    span = MagicMock()
    with patch("apx_agent._gateway_refusal.current_active_span", return_value=span):
        refusal = gateway_refusal(
            {"databricks_service_policy": policy},
            surface="model",
        )
    assert refusal is not None
    assert refusal.action == "DENY"
    assert span.set_attribute.call_args_list != []


def test_responses_converter_sees_the_same_deny() -> None:
    llm = get_llm("databricks-gpt-5-mini")
    assert type(llm).__name__ == "ChatDatabricksGptReasoning"
    with pytest.raises(GatewayRefusal, match="refused this model call"):
        llm._convert_responses_api_response_to_chat_result(
            {"databricks_service_policy": _policy()}
        )


def test_model_middleware_returns_the_refusal_as_the_assistant_message() -> None:
    refusal = GatewayRefusal(
        f"The Unity AI Gateway refused this model call (policy {_POLICY_NAME}): {_REASON}",
        policy_name=_POLICY_NAME,
        kind="sql",
        phase="on_call",
        rank=100,
        mode="enforce",
        action="DENY",
        surface="model",
    )
    middleware = _gateway_refusal_middleware()

    def handler(request: SimpleNamespace) -> AIMessage:
        del request
        raise refusal

    message = middleware.wrap_model_call(SimpleNamespace(), handler)
    assert isinstance(message, AIMessage)
    assert message.content == str(refusal)


@pytest.mark.asyncio
async def test_mcp_deny_is_a_typed_refusal_with_audit() -> None:
    """MCP blocks arrive as a JSON-RPC error, which the client raises."""
    policy = _policy(kind="mcp", rank=2)
    error = McpError(
        ErrorData(
            code=-32000,
            message=_REASON,
            data={"databricks_service_policy": policy},
        )
    )
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=error)

    @asynccontextmanager
    async def _open(server_url: str, headers: dict[str, str], transport: str, *, timeout_s: float = 30.0):
        del server_url, headers, transport, timeout_s
        yield session

    span = MagicMock()
    with (
        patch("apx_agent.mcp_consume._open_session", _open),
        patch("apx_agent._gateway_refusal.current_active_span", return_value=span),
    ):
        tool = mcp_tool("https://srv/mcp", "search", description="d", name="search")
        with pytest.raises(GatewayRefusal) as raised:
            await tool(arguments={"q": "secret"}, ws=MagicMock(config=None))
    refusal = raised.value
    assert isinstance(refusal, PermissionError)
    assert refusal.surface == "mcp"
    assert str(refusal) == (
        "The Unity AI Gateway refused this MCP call "
        f"(policy {_POLICY_NAME}): {_REASON}"
    )
    _assert_gateway_audit(span, policy)


@pytest.mark.asyncio
async def test_mcp_default_reason_without_a_policy_object_is_a_refusal() -> None:
    error = McpError(ErrorData(code=-32000, message=_DEFAULT_REASON))
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=error)

    @asynccontextmanager
    async def _open(server_url: str, headers: dict[str, str], transport: str, *, timeout_s: float = 30.0):
        del server_url, headers, transport, timeout_s
        yield session

    with patch("apx_agent.mcp_consume._open_session", _open):
        tool = mcp_tool("https://srv/mcp", "search", description="d", name="search")
        with pytest.raises(GatewayRefusal, match=_DEFAULT_REASON):
            await tool(arguments={}, ws=MagicMock(config=None))


@pytest.mark.asyncio
async def test_mcp_unrelated_failures_stay_generic() -> None:
    session = MagicMock()

    @asynccontextmanager
    async def _open(server_url: str, headers: dict[str, str], transport: str, *, timeout_s: float = 30.0):
        del server_url, headers, transport, timeout_s
        yield session

    with patch("apx_agent.mcp_consume._open_session", _open):
        tool = mcp_tool("https://srv/mcp", "search", description="d", name="search")
        session.call_tool = AsyncMock(
            side_effect=McpError(ErrorData(code=-32603, message="internal failure"))
        )
        unrelated = await tool(arguments={}, ws=MagicMock(config=None))
        session.call_tool = AsyncMock(side_effect=RuntimeError("connection reset"))
        down = await tool(arguments={}, ws=MagicMock(config=None))
    assert unrelated == {"error": "internal failure"}
    assert down == {"error": "connection reset"}


def test_mcp_refusal_is_contained_as_a_tool_error() -> None:
    refusal = gateway_refusal(
        {"databricks_service_policy": _policy(kind="mcp")},
        surface="mcp",
    )
    assert refusal is not None
    middleware = _governance_exception_middleware()

    def handler(request: SimpleNamespace) -> ToolMessage:
        del request
        raise refusal

    out = middleware.wrap_tool_call(SimpleNamespace(tool_call={"id": "c1"}), handler)
    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert out.tool_call_id == "c1"
    assert out.content == f"Error: {refusal}"


def test_mapper_does_not_swallow_exceptions() -> None:
    from ctk.lint import find_swallowed_exceptions

    path = Path(__file__).resolve().parents[1] / "src" / "apx_agent" / "_gateway_refusal.py"
    assert find_swallowed_exceptions(str(path)) == []
