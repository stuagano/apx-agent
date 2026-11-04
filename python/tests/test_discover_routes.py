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
    assert set(body) == {"nodes", "edges", "warnings", "built_at", "agent"}
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


@pytest.mark.asyncio
@pytest.mark.parametrize("env_catalog, expected", [("runtime_catalog", "runtime_catalog"), ("${var.catalog}", "project_catalog"), ("", "project_catalog")])
async def test_context_exposes_project_catalog_and_verified_identity(app, monkeypatch, tmp_path, env_catalog, expected):
    from types import SimpleNamespace

    (tmp_path / "pyproject.toml").write_text('[tool.apx.deploy]\ncatalog = "project_catalog"\n')
    monkeypatch.setattr("apx_agent._ui_grounding.resolve_project_root", lambda: tmp_path)
    monkeypatch.setenv("APX_CATALOG", env_catalog)
    ws = MagicMock()
    ws.config.host = "https://workspace.example"
    ws.current_user.me.return_value = SimpleNamespace(id="scim-123", user_name="reader@example.com")
    monkeypatch.setattr("apx_agent._defaults._ws_prefer_obo", lambda request: ws)
    r = await _get(app, "/_apx/workspace-context")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["project_catalog"] == expected
    assert r.json()["user_id"] == "scim-123"
    import hashlib

    assert r.json()["project_id"] == hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()
    assert str(tmp_path) not in r.text  # Stable project namespace without exposing a server path.
    assert r.json()["used_catalogs"] == []  # Default is not a fabricated resource footprint.
    ws.current_user.me.side_effect = RuntimeError("identity unavailable")
    unknown = (await _get(app, "/_apx/workspace-context")).json()
    assert unknown["user_id"] is None and unknown["user"] == "unknown"
    ws.current_user.me.side_effect = None
    ws.current_user.me.return_value = SimpleNamespace(display_name="Not a verified ID")
    assert (await _get(app, "/_apx/workspace-context")).json()["user_id"] is None


def _declare(monkeypatch, *specs):
    from apx_agent._resources import ResourceSpec

    monkeypatch.setattr("apx_agent._resources.collect_resource_specs",
                        lambda agent: [ResourceSpec(k, i) for k, i in specs])


@pytest.mark.asyncio
async def test_graph_overlays_the_local_agent_and_its_footprint(app, stub_graph, monkeypatch):
    # Local run: the agent isn't an App node, so Discover gets a synthesized Agent node.
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    _declare(monkeypatch, ("uc_table", "Main.S.T"), ("genie_space", "g1"), ("sql_warehouse", "wh-1"))
    body = (await _get(app, "/_apx/discover/graph.json")).json()
    assert {"id": "agent:self", "type": "Agent", "name": "disc", "fqn": "disc", "props": {}} in body["nodes"]
    assert {"src": "agent:self", "dst": "table:main.s.t", "type": "uses"} in body["edges"]
    assert body["agent"] == {
        "id": "agent:self",
        "footprint": ["table:main.s.t"],
        # Not in this graph: outside the loaded scope, or a kind the graph doesn't hold.
        "unplaced": [{"kind": "genie_space", "identifier": "g1", "node_id": "genie:g1"},
                     {"kind": "sql_warehouse", "identifier": "wh-1", "node_id": None}],
    }


@pytest.mark.asyncio
async def test_graph_uses_the_deployed_app_node_as_the_agent(app, monkeypatch):
    # Deployed: the apps connector already drew this App and its resource edges; reuse them.
    t = Node("table:main.s.t", "Table", "t", "main.s.t")
    a = Node("app:my-agent", "App", "disc", "my-agent")
    graph = Graph([Fragment([t, a], [Edge(a.id, t.id, "uses")], [])])
    monkeypatch.setattr(builder, "get_graph", lambda ws, catalogs, *, refresh=False: graph)
    monkeypatch.setattr("apx_agent._defaults._ws_prefer_obo", lambda request: "obo-client")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "my-agent")
    _declare(monkeypatch, ("uc_table", "main.s.t"))
    body = (await _get(app, "/_apx/discover/graph.json")).json()
    assert [n["id"] for n in body["nodes"] if n["type"] in ("Agent", "App")] == ["app:my-agent"]
    assert body["edges"] == [{"src": "app:my-agent", "dst": "table:main.s.t", "type": "uses"}]
    assert body["agent"] == {"id": "app:my-agent", "footprint": ["table:main.s.t"], "unplaced": []}
