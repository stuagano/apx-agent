"""/_apx/discover graph routes: shape, OBO fail-closed, impact path ids."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, AgentContext
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard
from apx_agent.workspace_graph import builder
from apx_agent.workspace_graph.builder import Graph
from apx_agent.workspace_graph.schema import Edge, Fragment, Node


@pytest.fixture
def app() -> FastAPI:
    config = AgentConfig(name="disc", model="claude-fake")
    ctx = AgentContext(config=config, tools=[], card=AgentCard(name="disc", description="", skills=[]), agent=None)  # type: ignore[arg-type]
    a = FastAPI()
    a.state.agent_context = ctx
    a.state.workspace_client = MagicMock()
    a.include_router(build_dev_ui_router())
    return a


@pytest.fixture
def stub_graph(monkeypatch):
    seen: dict[str, Any] = {}
    t = Node("table:main.s.t", "Table", "t", "main.s.t")
    v = Node("table:main.s.v", "View", "v", "main.s.v")
    graph = Graph([Fragment([t, v], [Edge(v.id, t.id, "derivedFrom")], ["genie: denied"])])

    def _get(ws, catalogs, *, refresh=False):
        seen.update(ws=ws, catalogs=catalogs, refresh=refresh)
        return graph

    monkeypatch.setattr(builder, "get_graph", _get)
    monkeypatch.setattr("apx_agent._defaults._ws_prefer_obo", lambda request: "obo-client")
    return seen


async def _get(app, path, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        return await ac.get(path, **kw)


@pytest.mark.asyncio
async def test_graph_json_shape_and_params(app, stub_graph):
    r = await _get(app, "/_apx/discover/graph.json", params={"catalogs": "main, other ,", "refresh": "1"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"nodes", "edges", "warnings", "built_at"}
    assert body["warnings"] == ["genie: denied"]
    assert body["edges"] == [{"src": "table:main.s.v", "dst": "table:main.s.t", "type": "derivedFrom"}]
    assert stub_graph == {"ws": "obo-client", "catalogs": ["main", "other"], "refresh": True}


@pytest.mark.asyncio
async def test_graph_json_default_scope(app, stub_graph):
    r = await _get(app, "/_apx/discover/graph.json")
    assert r.status_code == 200
    assert stub_graph["catalogs"] is None and stub_graph["refresh"] is False


@pytest.mark.asyncio
async def test_impact_route_accepts_colon_ids(app, stub_graph):
    r = await _get(app, "/_apx/discover/impact/table%3Amain.s.t")
    assert r.status_code == 200
    assert r.json() == {"id": "table:main.s.t", "impacted": ["table:main.s.v"]}


@pytest.mark.asyncio
async def test_impact_unknown_is_404_with_suggestions(app, stub_graph):
    r = await _get(app, "/_apx/discover/impact/main.s.tt")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"
    assert "table:main.s.t" in r.json()["detail"]["suggestions"]


@pytest.mark.asyncio
async def test_graph_json_apps_without_obo_is_401(app, monkeypatch):
    """#612: never build the graph under the App SP."""
    monkeypatch.setenv("DATABRICKS_APP_NAME", "my-app")
    monkeypatch.delenv("APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK", raising=False)
    monkeypatch.setattr(builder, "get_graph", lambda *a, **k: pytest.fail("must not build without OBO"))
    r = await _get(app, "/_apx/discover/graph.json")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_impact_apps_without_obo_is_401(app, monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_NAME", "my-app")
    monkeypatch.delenv("APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK", raising=False)
    monkeypatch.setattr(builder, "get_graph", lambda *a, **k: pytest.fail("must not build without OBO"))
    r = await _get(app, "/_apx/discover/impact/table:main.s.t")
    assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/_apx/workspace-agents", "/_apx/workspace-functions?catalog=a&schema=b", "/_apx/workspace-apis",
    "/_apx/discover/tables?catalog=a&schema=b", "/_apx/discover/sample?catalog=a&schema=b&table=c",
    "/_apx/discover/targets",
])
async def test_old_discover_routes_are_gone(app, path):
    assert (await _get(app, path)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["wire-agent", "unwire-agent", "wire-tool", "unwire-tool"])
async def test_old_wire_routes_are_gone(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(f"/_apx/discover/{path}", json={})
    assert r.status_code in (404, 405)


@pytest.mark.asyncio
async def test_discover_page_serves_built_spa(app):
    r = await _get(app, "/_apx/discover")
    assert r.status_code == 200
    assert '<div id="root">' in r.text
    assert "apx-agent · discover" in r.text
