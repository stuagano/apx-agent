"""Direct model calls outside get_llm go to Unity Gateway, not Model Serving (#1275).

RouterAgent._select_route and the dev-UI tool scaffolder post to
``/ai-gateway/mlflow/v1/chat/completions`` with the ``system.ai.<id>`` rewrite
of the chat model. The gateway is mocked; live inference is a manual check.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute

from apx_agent import Agent, RouterAgent
from apx_agent._llm import AI_GATEWAY_CHAT_PATH

HOST = "https://ws.example.com"


class _FakeAsyncClient:
    """httpx.AsyncClient stand-in that records the POST and returns ``payload``."""

    calls: list[tuple[str, dict[str, Any]]] = []
    payload: dict[str, Any] = {}

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any], **_kw: Any) -> Any:
        type(self).calls.append((url, json))
        payload = type(self).payload
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


def _request(model: str) -> Any:
    ws = MagicMock()
    ws.config.host = HOST + "/"
    ws.config.authenticate.return_value = {"Authorization": "Bearer t"}
    ctx = SimpleNamespace(config=SimpleNamespace(model=model, instructions="Help."))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent_context=ctx, workspace_client=ws)))


@pytest.fixture
def fake_client() -> Iterator[type[_FakeAsyncClient]]:
    _FakeAsyncClient.calls = []
    with patch("httpx.AsyncClient", _FakeAsyncClient):
        yield _FakeAsyncClient


@pytest.mark.asyncio
async def test_router_select_route_posts_to_unity_gateway(fake_client: type[_FakeAsyncClient]) -> None:
    billing = Agent(name="billing", description="Billing questions.")
    router = RouterAgent(agents=[billing])
    fake_client.payload = {
        "choices": [{"message": {"tool_calls": [{"function": {"name": "transfer_to_billing"}}]}}]
    }

    picked = await router._select_route([], _request("databricks-claude-sonnet-4-6"))

    assert picked == "billing"
    [(url, body)] = fake_client.calls
    assert url == HOST + AI_GATEWAY_CHAT_PATH
    assert "serving-endpoints" not in url
    assert body["model"] == "system.ai.claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_tool_scaffolder_posts_to_unity_gateway(fake_client: type[_FakeAsyncClient]) -> None:
    from apx_agent._apx_models import ToolSuggestRequest
    from apx_agent._dev_routes.context import DevRouteContext
    from apx_agent._dev_routes.tools import register

    api = APIRouter()
    register(api, DevRouteContext(api_prefix="/api", instruction_source_path=None, instruction_source=None))
    [suggest] = [r.endpoint for r in api.routes if isinstance(r, APIRoute) and r.path == "/_apx/tools/suggest"]
    fake_client.payload = {"choices": [{"message": {"content": '{"name": "t", "body": ""}'}}]}

    with patch("apx_agent._dev_routes.tools._find_agent_router_path", return_value=None):
        result = await suggest(_request("main.agents.chat"), ToolSuggestRequest(prompt="count rows"))

    assert result == {"ok": True, "spec": {"name": "t", "body": ""}}
    [(url, body)] = fake_client.calls
    assert url == HOST + AI_GATEWAY_CHAT_PATH
    assert body["model"] == "main.agents.chat"
