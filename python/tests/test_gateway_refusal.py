"""A Unity AI Gateway DENY or MCP ASK is a typed outcome plus a service-policy audit.

The payloads are the shapes the chat and MCP clients already see. An MCP ASK
is the URL-mode elicitation error from MCP 2025-11-25 (code -32042), which
Databricks uses for an external agent. Nothing here calls a gateway or builds
a vendor request. Attachment stays UI-only.
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


def _fingerprint(policy: dict[str, str | int], *, action: str = "DENY", phase: str = "on_call") -> str:
    return hash_for_audit(
        json.dumps(
            {
                "name": policy["name"],
                "kind": policy["kind"],
                "phase": phase,
                "rank": policy["rank"],
                "mode": policy["mode"],
                "action": action,
            },
            sort_keys=True,
        )
    )


def _recorded(span: MagicMock) -> dict[str, str | int]:
    return {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}


def _assert_gateway_audit(
    span: MagicMock,
    policy: dict[str, str | int],
    *,
    action: str = "DENY",
    phase: str = "on_call",
) -> None:
    recorded = _recorded(span)
    assert recorded[AuditAttrs.SERVICE_POLICY_NAME] == policy["name"]
    assert recorded[AuditAttrs.SERVICE_POLICY_KIND] == policy["kind"]
    assert recorded[AuditAttrs.SERVICE_POLICY_PHASE] == phase
    assert recorded[AuditAttrs.SERVICE_POLICY_RANK] == policy["rank"]
    assert recorded[AuditAttrs.SERVICE_POLICY_MODE] == policy["mode"]
    assert recorded[AuditAttrs.SERVICE_POLICY_ACTION] == action
    assert recorded[AuditAttrs.SERVICE_POLICY_ADAPTER] == "gateway"
    assert recorded[AuditAttrs.SERVICE_POLICY_FINGERPRINT] == _fingerprint(
        policy, action=action, phase=phase,
    )
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


def test_allow_is_not_a_refusal() -> None:
    span = MagicMock()
    with patch("apx_agent._gateway_refusal.current_active_span", return_value=span):
        assert gateway_refusal(
            {"databricks_service_policy": _policy(action="ALLOW")},
            surface="model",
        ) is None
        assert gateway_refusal(
            {"databricks_service_policy": _policy(action="ASK", phase="on_result")},
            surface="mcp",
        ) is None
    assert span.set_attribute.call_args_list == []


_APPROVAL_URL = "https://mcp.example.com/approve?elicitationId=550e8400-e29b-41d4-a716-446655440000"


def _elicitation(
    *,
    message: str = _REASON,
    url: str | None = _APPROVAL_URL,
    code: int = -32042,
) -> dict[str, Any]:
    elicitations: list[dict[str, str]] = []
    if url is not None:
        elicitations.append({
            "mode": "url",
            "elicitationId": "550e8400-e29b-41d4-a716-446655440000",
            "url": url,
            "message": "Approve this tool call",
        })
    return {
        "error": {
            "code": code,
            "message": message,
            "data": {"elicitations": elicitations},
        }
    }


def test_mcp_url_elicitation_is_a_typed_hold() -> None:
    """MCP 2025-11-25 URLElicitationRequiredError is the documented ASK delivery."""
    span = MagicMock()
    with patch("apx_agent._gateway_refusal.current_active_span", return_value=span):
        refusal = gateway_refusal(_elicitation(), surface="mcp")
    assert refusal is not None
    assert isinstance(refusal, PermissionError)
    assert refusal.action == "ASK"
    assert refusal.surface == "mcp"
    assert str(refusal) == (
        "The Unity AI Gateway is holding this MCP call for approval: "
        f"{_REASON}. Tell the user this action needs their approval "
        f"and to open {_APPROVAL_URL}. "
        "Retry the same call only after they approve it."
    )
    recorded = _recorded(span)
    assert recorded[AuditAttrs.SERVICE_POLICY_ACTION] == "ASK"
    assert recorded[AuditAttrs.SERVICE_POLICY_ADAPTER] == "gateway"
    for value in recorded.values():
        assert _REASON not in str(value)
        assert _APPROVAL_URL not in str(value)


def test_mcp_policy_object_ask_is_a_hold_and_not_allow() -> None:
    """action ASK on the DENY object is not a documented delivery, and is not success."""
    policy = _policy(action="ask", kind="mcp")
    span = MagicMock()
    with patch("apx_agent._gateway_refusal.current_active_span", return_value=span):
        refusal = gateway_refusal(
            {"databricks_service_policy": policy},
            surface="mcp",
        )
    assert refusal is not None
    assert refusal.action == "ASK"
    assert "holding this MCP call" in str(refusal)
    _assert_gateway_audit(span, policy, action="ASK")


def test_model_ask_is_a_block_not_a_completion() -> None:
    """A model service cannot pause, so a caller-visible ASK is not the assistant text."""
    policy = _policy(action="ASK", phase="on_result")
    llm = get_llm("databricks-claude-sonnet-4-6")
    span = MagicMock()
    with patch("apx_agent._gateway_refusal.current_active_span", return_value=span):
        with pytest.raises(GatewayRefusal) as raised:
            llm._convert_response_to_chat_result(_completion(policy))
    refusal = raised.value
    assert refusal.action == "ASK"
    assert refusal.surface == "model"
    message = str(refusal)
    assert message == (
        "The Unity AI Gateway refused this model call "
        f"(policy {_POLICY_NAME}): {_REASON}. "
        "A model service cannot pause for approval."
    )
    assert "The policy blocked this request." not in message
    _assert_gateway_audit(span, policy, action="ASK", phase="on_result")


def test_model_ask_is_contained_and_not_a_generic_500() -> None:
    refusal = GatewayRefusal(
        f"The Unity AI Gateway refused this model call (policy {_POLICY_NAME}): {_REASON}. "
        "A model service cannot pause for approval.",
        policy_name=_POLICY_NAME,
        kind="sql",
        phase="on_call",
        rank=100,
        mode="enforce",
        action="ASK",
        surface="model",
    )
    middleware = _gateway_refusal_middleware()

    def handler(request: SimpleNamespace) -> AIMessage:
        del request
        raise refusal

    message = middleware.wrap_model_call(SimpleNamespace(), handler)
    assert isinstance(message, AIMessage)
    assert message.content == str(refusal)


def test_elicitation_omits_credential_and_non_http_urls() -> None:
    payload = _elicitation(url="https://user:secret@mcp.example.com/approve")
    payload["error"]["data"]["elicitations"].append({
        "mode": "url",
        "elicitationId": "other",
        "url": "javascript:alert(1)",
        "message": "no",
    })
    payload["error"]["data"]["elicitations"].append({
        "mode": "url",
        "elicitationId": "ok",
        "url": _APPROVAL_URL,
        "message": "Approve this tool call",
    })
    refusal = gateway_refusal(payload, surface="mcp")
    assert refusal is not None
    assert refusal.action == "ASK"
    text = str(refusal)
    assert "secret" not in text
    assert "javascript:" not in text
    assert _APPROVAL_URL in text


def test_mcp_ask_is_contained_as_a_tool_error() -> None:
    """A hold is an error tool message, not an escaped failure and not a success."""
    refusal = gateway_refusal(_elicitation(), surface="mcp")
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
    assert _APPROVAL_URL in out.content


def test_elicitation_without_a_url_still_holds() -> None:
    span = MagicMock()
    with patch("apx_agent._gateway_refusal.current_active_span", return_value=span):
        refusal = gateway_refusal(_elicitation(message="  ", url=None), surface="mcp")
    assert refusal is not None
    assert refusal.action == "ASK"
    assert "https://" not in str(refusal)
    assert "paused until a person approves it" in str(refusal)
    assert span.set_attribute.call_args_list != []


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
async def test_mcp_elicitation_from_the_client_is_a_typed_hold() -> None:
    """The MCP client raises the elicitation error; it must not become a tool success."""
    error = McpError(ErrorData(
        code=-32042,
        message=_REASON,
        data={"elicitations": [{
            "mode": "url",
            "elicitationId": "550e8400-e29b-41d4-a716-446655440000",
            "url": _APPROVAL_URL,
            "message": "Approve this tool call",
        }]},
    ))
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=error)

    @asynccontextmanager
    async def _open(server_url: str, headers: dict[str, str], transport: str, *, timeout_s: float = 30.0):
        del server_url, headers, transport, timeout_s
        yield session

    with patch("apx_agent.mcp_consume._open_session", _open):
        tool = mcp_tool("https://srv/mcp", "search", description="d", name="search")
        with pytest.raises(GatewayRefusal) as raised:
            await tool(arguments={"q": "secret-prompt"}, ws=MagicMock(config=None))
    assert raised.value.action == "ASK"
    assert _APPROVAL_URL in str(raised.value)
    assert "secret-prompt" not in str(raised.value)


@pytest.mark.asyncio
async def test_unrecognized_ask_text_is_not_allow_and_not_a_500() -> None:
    """A bare ASK word is not the elicitation error and not a policy verdict."""
    error = McpError(ErrorData(code=-32603, message="ASK"))
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=error)

    @asynccontextmanager
    async def _open(server_url: str, headers: dict[str, str], transport: str, *, timeout_s: float = 30.0):
        del server_url, headers, transport, timeout_s
        yield session

    with patch("apx_agent.mcp_consume._open_session", _open):
        tool = mcp_tool("https://srv/mcp", "search", description="d", name="search")
        result = await tool(arguments={"q": "secret-prompt"}, ws=MagicMock(config=None))
    assert result == {"error": str(error)}
    assert "text" not in result
    assert "secret-prompt" not in result["error"]


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
