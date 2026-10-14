"""#831 — per-caller allowlist for MCP tools: list_tools hides, call_tool/REST refuse."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

from apx_agent import AgentConfig, LlmAgent, setup_agent
from apx_agent._mcp import _build_mcp_components, set_mcp_auth
from apx_agent._tool_config import ToolConfigError
from apx_agent._tool_scope import ToolScope, attach_scope, parse_tool_scope

ALICE = "alice@example.com"
BOB = "bob@example.com"


def refund_create(amount: int) -> str:
    """Issue a refund."""
    return f"refunded {amount}"


def lookup_account(account_id: str) -> str:
    """Look up an account."""
    return f"account {account_id}"


attach_scope(refund_create, ToolScope(callers=(ALICE,)))


@pytest.fixture
async def served():
    app = FastAPI()
    ctx = await setup_agent(
        app,
        LlmAgent(tools=[refund_create, lookup_account]),
        AgentConfig(name="callers", api_prefix="/api"),
    )
    assert ctx is not None
    return ctx, app, _build_mcp_components(ctx, app, "/api").server


async def _list(server, caller: str) -> set[str]:
    set_mcp_auth("", "", caller)
    result = await server.request_handlers[ListToolsRequest](MagicMock())
    return {t.name for t in result.root.tools}


async def _call(server, caller: str, name: str, args: dict) -> str:
    set_mcp_auth("", "", caller)
    request = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=name, arguments=args)
    )
    result = await server.request_handlers[CallToolRequest](request)
    return result.root.content[0].text


@pytest.mark.asyncio
async def test_list_tools_hides_restricted_tool_from_other_callers(served):
    _, _, server = served
    assert {"refund_create", "lookup_account"} <= await _list(server, ALICE)
    bob_sees = await _list(server, BOB)
    assert "refund_create" not in bob_sees
    assert "lookup_account" in bob_sees
    # No caller identity → restricted tool is hidden (fail closed).
    assert "refund_create" not in await _list(server, "")


@pytest.mark.asyncio
async def test_call_tool_refuses_caller_outside_allowlist(served):
    _, _, server = served
    assert "refunded 5" in await _call(server, ALICE.upper(), "refund_create", {"amount": 5})
    denied = await _call(server, BOB, "refund_create", {"amount": 5})
    assert "403" in denied and "caller_denied" in denied
    assert "account 7" in await _call(server, BOB, "lookup_account", {"account_id": "7"})


@pytest.mark.asyncio
async def test_rest_tool_route_enforces_same_allowlist(served):
    _, app, _ = served
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        bob = await client.post(
            "/api/tools/refund_create", json={"amount": 1}, headers={"X-Forwarded-Email": BOB}
        )
        anon = await client.post("/api/tools/refund_create", json={"amount": 1})
        alice = await client.post(
            "/api/tools/refund_create", json={"amount": 1}, headers={"X-Forwarded-Email": ALICE}
        )
    assert bob.status_code == 403
    assert anon.status_code == 403
    assert alice.status_code == 200


@pytest.mark.asyncio
async def test_card_does_not_publish_callers(served):
    ctx, _, _ = served
    assert ALICE not in ctx.card.model_dump_json()
    assert ALICE not in ctx.get_tool("refund_create").model_dump_json()


def test_parse_callers_from_config():
    assert parse_tool_scope(callers=[ALICE]) == ToolScope(callers=(ALICE,))
    with pytest.raises(ToolConfigError, match="at least one"):
        parse_tool_scope(callers=[])
    with pytest.raises(ToolConfigError, match="callers"):
        parse_tool_scope(callers="alice@example.com")
