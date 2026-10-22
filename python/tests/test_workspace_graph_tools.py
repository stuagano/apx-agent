"""The six workspace-graph agent tools."""

from __future__ import annotations

import typing

import pytest

from apx_agent import workspace_graph_tools
from apx_agent._defaults import UserClientDependency
from apx_agent.workspace_graph import tools as tools_mod
from apx_agent.workspace_graph.builder import Graph
from apx_agent.workspace_graph.schema import Edge, Fragment, Node

WS = object()  # injected client; the stubbed get_graph ignores it


def _graph() -> Graph:
    tables = [
        Node(f"table:main.s.t{i:03d}", "Table", f"t{i:03d}", f"main.s.t{i:03d}")
        for i in range(60)
    ]
    view = Node("table:main.s.v", "View", "v", "main.s.v")
    return Graph(
        [
            Fragment(
                [*tables, view],
                [Edge(view.id, tables[0].id, "derivedFrom")],
                ["genie: denied"],
            )
        ]
    )


@pytest.fixture
def tools(monkeypatch):
    seen: dict[str, typing.Any] = {}

    def _get(ws, catalogs):
        seen["catalogs"] = catalogs
        return _graph()

    monkeypatch.setattr(tools_mod, "get_graph", _get)
    return {t.__name__: t for t in workspace_graph_tools(catalogs=["main"])}, seen


def test_six_named_tools_with_injected_ws(tools):
    by_name, _ = tools
    assert set(by_name) == {
        "graph_summary",
        "search_assets",
        "get_asset",
        "analyze_impact",
        "get_lineage",
        "find_orphans",
    }
    for t in by_name.values():
        assert t.__doc__
        hints = typing.get_type_hints(t, include_extras=True)
        assert hints["ws"] == UserClientDependency


@pytest.mark.asyncio
async def test_summary_passes_scope_and_shows_warnings(tools):
    by_name, seen = tools
    out = await by_name["graph_summary"](ws=WS)
    assert out["node_count"] == 61
    assert out["warnings"] == ["genie: denied"]
    assert seen["catalogs"] == ["main"]


@pytest.mark.asyncio
async def test_search_truncates_at_cap(tools):
    by_name, _ = tools
    out = await by_name["search_assets"](ws=WS, query="t")
    assert out["count"] == 61
    assert len(out["results"]) == 50
    assert out["truncated"] is True
    view_only = await by_name["search_assets"](ws=WS, query="", asset_type="View")
    assert view_only == {
        "count": 1,
        "results": [
            {"id": "table:main.s.v", "type": "View", "name": "v", "fqn": "main.s.v"}
        ],
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_get_asset_and_not_found(tools):
    by_name, _ = tools
    got = await by_name["get_asset"](ws=WS, identifier="MAIN.S.V")
    assert got["node"]["id"] == "table:main.s.v"
    assert "outgoing_count" in got
    assert "incoming_count" in got
    assert "truncated" in got
    miss = await by_name["get_asset"](ws=WS, identifier="main.s.vv")
    assert miss["error"] == "not_found"
    assert miss["identifier"] == "main.s.vv"
    assert miss["suggestions"][0] == "table:main.s.v"


@pytest.mark.asyncio
async def test_impact_lineage_orphans(tools):
    by_name, _ = tools
    impact = await by_name["analyze_impact"](ws=WS, identifier="main.s.t000")
    assert [r["id"] for r in impact["results"]] == ["table:main.s.v"]
    assert impact["asset"]["id"] == "table:main.s.t000"
    lin = await by_name["get_lineage"](ws=WS, identifier="main.s.v")
    assert [r["id"] for r in lin["upstream"]] == ["table:main.s.t000"]
    assert lin["downstream"] == []
    assert "upstream_count" in lin
    assert "downstream_count" in lin
    assert "truncated" in lin
    orphans = await by_name["find_orphans"](ws=WS)
    assert orphans["count"] == 59 and orphans["truncated"] is True
    assert (await by_name["analyze_impact"](ws=WS, identifier="zzz"))[
        "error"
    ] == "not_found"


def _hub_graph() -> Graph:
    """Graph with a hub node having >50 incoming and outgoing connections."""
    # Create a hub node
    hub = Node("table:main.s.hub", "Table", "hub", "main.s.hub")

    # Create 60 source tables that feed into the hub
    sources = [
        Node(f"table:main.s.src{i:03d}", "Table", f"src{i:03d}", f"main.s.src{i:03d}")
        for i in range(60)
    ]

    # Create 60 downstream tables that the hub feeds into
    downstreams = [
        Node(f"table:main.s.dst{i:03d}", "Table", f"dst{i:03d}", f"main.s.dst{i:03d}")
        for i in range(60)
    ]

    nodes = [hub, *sources, *downstreams]

    # Edges: sources -> hub, hub -> downstreams
    edges = [Edge(s.id, hub.id, "feedsInto") for s in sources] + [
        Edge(hub.id, d.id, "feedsInto") for d in downstreams
    ]

    return Graph([Fragment(nodes, edges, [])])


@pytest.fixture
def hub_tools(monkeypatch):
    """Fixture that provides tools with a hub graph for testing truncation."""

    def _get(ws, catalogs):
        return _hub_graph()

    monkeypatch.setattr(tools_mod, "get_graph", _get)
    return {t.__name__: t for t in workspace_graph_tools()}


@pytest.mark.asyncio
async def test_get_asset_caps_and_reports_hub_connections(hub_tools):
    """Test that get_asset caps incoming/outgoing at 50 and reports truncation."""
    by_name = hub_tools
    got = await by_name["get_asset"](ws=WS, identifier="main.s.hub")
    assert got["node"]["id"] == "table:main.s.hub"
    # Both incoming and outgoing should have 60 total connections
    assert got["incoming_count"] == 60
    assert got["outgoing_count"] == 60
    # But results should be capped at 50
    assert len(got["incoming"]) == 50
    assert len(got["outgoing"]) == 50
    # Truncated flag should be True
    assert got["truncated"] is True


@pytest.mark.asyncio
async def test_get_lineage_reports_upstream_downstream_counts(hub_tools):
    """Test that get_lineage reports upstream/downstream counts even when capped."""
    by_name = hub_tools
    # Pick a downstream node - it should have the hub as upstream
    lin = await by_name["get_lineage"](ws=WS, identifier="main.s.dst000")
    assert lin["asset"]["id"] == "table:main.s.dst000"
    # Upstream should contain the hub (and potentially others)
    assert lin["upstream_count"] >= 1
    # The upstream list should be capped at 50
    assert len(lin["upstream"]) <= 50
    # Downstream should be empty or capped at 50
    assert len(lin["downstream"]) <= 50
    # Truncated flag should reflect whether any list was cut
    if lin["upstream_count"] > 50 or lin["downstream_count"] > 50:
        assert lin["truncated"] is True
    else:
        assert lin["truncated"] is False


def test_tools_declare_obo_scopes(tools):
    from apx_agent._apps_authorization import infer_operation_authorization

    by_name, _ = tools
    assert len(by_name) == 6
    for t in by_name.values():
        op = infer_operation_authorization(t)
        assert op.execution_identity == "user"
        assert set(op.user_api_scopes) == set(tools_mod.SCOPES)
    assert "genie" in tools_mod.SCOPES and "apps" in tools_mod.SCOPES
