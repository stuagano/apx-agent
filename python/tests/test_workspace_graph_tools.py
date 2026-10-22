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
    orphans = await by_name["find_orphans"](ws=WS)
    assert orphans["count"] == 59 and orphans["truncated"] is True
    assert (await by_name["analyze_impact"](ws=WS, identifier="zzz"))[
        "error"
    ] == "not_found"
