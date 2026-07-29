"""Tests for Dev UI workspace discovery routes."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, AgentContext
from apx_agent._apps_discovery import AppAgentInfo
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard
from apx_agent._topology import AgentNode, Topology


def _make_ctx() -> AgentContext:
    config = AgentConfig(name="disc-test", model="claude-fake")
    card = AgentCard(name="disc-test", description="", skills=[])
    return AgentContext(config=config, tools=[], card=card, agent=None)  # type: ignore[arg-type]


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.state.agent_context = _make_ctx()
    a.state.workspace_client = MagicMock()
    a.include_router(build_dev_ui_router())
    return a


@pytest.mark.asyncio
async def test_discover_page_renders(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/_apx/discover")
    assert r.status_code == 200
    assert "Refresh" in r.text
    assert "UC functions" in r.text
    assert "Scanning workspace" in r.text
    assert "workspace-apis" in r.text
    assert "APIs" in r.text


@pytest.mark.asyncio
async def test_workspace_agents_merges_apps_and_uc(app: FastAPI, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "apx_agent._apps_discovery.discover_app_agents",
        lambda ws, **k: [
            AppAgentInfo(
                name="triage",
                app_name="triage-app",
                url="https://triage.example",
                description="triage agent",
                tool_count=2,
                state="RUNNING",
                tools=("lookup", "escalate"),
            )
        ],
    )
    monkeypatch.setattr(
        "apx_agent._topology.discover_topology",
        lambda ws, **k: Topology(
            nodes=(
                AgentNode(name="triage", uc_name="main.agents.triage", model_endpoint="triage-ep", tool_count=2),
                AgentNode(name="billing", uc_name="main.agents.billing", model_endpoint="billing-ep", tool_count=1),
            ),
            edges=(),
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/_apx/workspace-agents")
    assert r.status_code == 200
    body = r.json()
    names = {a["name"] for a in body["agents"]}
    assert names == {"triage", "billing"}
    triage = next(a for a in body["agents"] if a["name"] == "triage")
    assert triage["source"] == "app"
    assert triage["url"] == "https://triage.example"
    assert triage["uc_name"] == "main.agents.triage"
    assert triage["tools"] == ["lookup", "escalate"]
    billing = next(a for a in body["agents"] if a["name"] == "billing")
    assert billing["source"] == "uc"
    assert billing["model_endpoint"] == "billing-ep"


@pytest.mark.asyncio
async def test_workspace_functions_lists_uc(app: FastAPI):
    fn = MagicMock()
    fn.name = "score_lead"
    fn.full_name = "main.ml.score_lead"
    fn.comment = "scores a lead"
    app.state.workspace_client.functions.list.return_value = [fn]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/_apx/workspace-functions", params={"catalog": "main", "schema": "ml"})
    assert r.status_code == 200
    body = r.json()
    assert body["catalog"] == "main"
    assert body["functions"][0]["full_name"] == "main.ml.score_lead"
    assert body["functions"][0]["comment"] == "scores a lead"


@pytest.mark.asyncio
async def test_workspace_functions_requires_params(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/_apx/workspace-functions", params={"catalog": "", "schema": "ml"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_workspace_apis_lists_serving_genie_vs(app: FastAPI, monkeypatch: pytest.MonkeyPatch):
    ws = app.state.workspace_client
    ws.config.host = "https://dbc.example.com"

    ep = MagicMock()
    ep.name = "databricks-meta-llama"
    ep.task = "llm/v1/chat"
    ep.state.ready = MagicMock(value="READY")
    ws.serving_endpoints.list.return_value = [ep]

    space = MagicMock()
    space.space_id = "space-abc"
    space.title = "Sales Genie"
    space.description = "sales Q&A"
    spaces_resp = MagicMock()
    spaces_resp.spaces = [space]
    spaces_resp.next_page_token = None
    ws.genie.list_spaces.return_value = spaces_resp

    monkeypatch.setattr(
        "apx_agent._ui_probe._discover_vs_indexes",
        lambda _ws: [
            {
                "endpoint": "vs-ep",
                "endpoint_state": "ONLINE",
                "index": "main.rag.docs_idx",
                "source_table": "main.rag.docs",
                "ready": True,
                "columns": ["content"],
            }
        ],
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/_apx/workspace-apis")
    assert r.status_code == 200
    body = r.json()
    by_kind = {a["kind"]: a for a in body["apis"]}
    assert set(by_kind) == {"serving_endpoint", "genie_space", "vector_search_index"}

    serving = by_kind["serving_endpoint"]
    assert serving["name"] == "databricks-meta-llama"
    assert serving["url"] == "https://dbc.example.com/serving-endpoints/databricks-meta-llama/invocations"
    assert serving["mcp_url"] is None

    genie = by_kind["genie_space"]
    assert genie["name"] == "Sales Genie"
    assert genie["mcp_url"] == "https://dbc.example.com/api/2.0/mcp/genie/space-abc"
    assert genie["extra"]["space_id"] == "space-abc"

    vs = by_kind["vector_search_index"]
    assert vs["name"] == "main.rag.docs_idx"
    assert vs["mcp_url"] == (
        "https://dbc.example.com/api/2.0/mcp/vector-search/main/rag/docs_idx"
    )


@pytest.mark.asyncio
async def test_workspace_apis_survives_partial_failures(app: FastAPI, monkeypatch: pytest.MonkeyPatch):
    ws = app.state.workspace_client
    ws.config.host = "https://dbc.example.com"
    ws.serving_endpoints.list.side_effect = RuntimeError("no serving perms")
    ws.genie.list_spaces.side_effect = RuntimeError("no genie")
    monkeypatch.setattr(
        "apx_agent._ui_probe._discover_vs_indexes",
        lambda _ws: [{"error": "Could not list endpoints: boom"}],
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/_apx/workspace-apis")
    assert r.status_code == 200
    assert r.json() == {"apis": []}
