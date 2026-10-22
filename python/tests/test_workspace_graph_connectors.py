"""Workspace graph connectors over fake SDK clients."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from apx_agent._discover_data import list_discover_tables
from apx_agent.workspace_graph import connectors


def _uc_ws() -> MagicMock:
    ws = MagicMock()
    ws.schemas.list.return_value = [
        SimpleNamespace(name="sales"),
        SimpleNamespace(name="information_schema"),
    ]
    orders = SimpleNamespace(name="orders", full_name="main.sales.orders")
    view = SimpleNamespace(name="orders_v", full_name="main.sales.orders_v")
    ws.tables.list.return_value = [orders, view]
    view_detail = SimpleNamespace(
        name="orders_v",
        full_name="main.sales.orders_v",
        table_type=SimpleNamespace(value="VIEW"),
        comment=None,
        columns=[],
        properties={},
        view_dependencies=SimpleNamespace(
            dependencies=[
                SimpleNamespace(
                    table=SimpleNamespace(table_full_name="main.sales.orders")
                )
            ]
        ),
    )
    orders_detail = SimpleNamespace(
        name="orders",
        full_name="main.sales.orders",
        table_type=SimpleNamespace(value="MANAGED"),
        comment="orders",
        columns=[
            SimpleNamespace(name="id", type_text="BIGINT", nullable=False, comment=None)
        ],
        properties={"numRows": "7"},
        view_dependencies=None,
    )
    ws.tables.get.side_effect = (
        lambda fqn: view_detail if fqn.endswith("_v") else orders_detail
    )
    ws.volumes.list.return_value = [
        SimpleNamespace(
            name="raw",
            full_name="main.sales.raw",
            volume_type=SimpleNamespace(value="MANAGED"),
            comment=None,
        )
    ]
    ws.functions.list.return_value = [
        SimpleNamespace(name="mask", full_name="main.sales.mask", comment="masks pii")
    ]
    ws.registered_models.list.return_value = [
        SimpleNamespace(name="churn", full_name="main.sales.churn", comment=None)
    ]
    return ws


def test_list_discover_tables_reports_view_dependencies():
    rows = {r["name"]: r for r in list_discover_tables(_uc_ws(), "main", "sales")}
    assert rows["orders_v"]["view_dependencies"] == ["main.sales.orders"]
    assert rows["orders"]["view_dependencies"] == []


def test_unity_catalog_nodes_and_edges():
    frag = connectors.unity_catalog(_uc_ws(), ["main"])
    types = {n.id: n.type for n in frag.nodes}
    assert types == {
        "catalog:main": "Catalog",
        "schema:main.sales": "Schema",
        "table:main.sales.orders": "Table",
        "table:main.sales.orders_v": "View",
        "volume:main.sales.raw": "Volume",
        "function:main.sales.mask": "Function",
        "model:main.sales.churn": "Model",
    }
    edges = {(e.src, e.dst, e.type) for e in frag.edges}
    assert ("catalog:main", "schema:main.sales", "contains") in edges
    assert ("schema:main.sales", "model:main.sales.churn", "contains") in edges
    assert (
        "table:main.sales.orders_v",
        "table:main.sales.orders",
        "derivedFrom",
    ) in edges
    assert frag.warnings == []
    orders = next(n for n in frag.nodes if n.id == "table:main.sales.orders")
    assert orders.props["row_count"] == 7
    assert orders.props["columns"][0]["name"] == "id"


def test_schema_listing_failure_is_a_warning():
    ws = _uc_ws()
    ws.volumes.list.side_effect = PermissionError("no USE on volumes")
    frag = connectors.unity_catalog(ws, ["main"])
    assert any("main.sales volumes: no USE on volumes" in w for w in frag.warnings)
    assert any(n.type == "Table" for n in frag.nodes)


def test_missing_catalog_is_a_warning():
    ws = _uc_ws()
    ws.schemas.list.side_effect = ValueError("catalog nope does not exist")
    frag = connectors.unity_catalog(ws, ["nope"])
    assert frag.warnings == ["nope: catalog nope does not exist"]


def test_default_catalogs_capped_with_warning():
    ws = _uc_ws()
    ws.schemas.list.return_value = []
    ws.catalogs.list.return_value = [
        SimpleNamespace(name="__internal"),
        SimpleNamespace(name="system"),
    ] + [SimpleNamespace(name=f"c{i:02d}") for i in range(12)]
    frag = connectors.unity_catalog(ws, None)
    cats = [n.id for n in frag.nodes if n.type == "Catalog"]
    assert len(cats) == connectors.MAX_CATALOGS
    assert "catalog:system" not in cats and "catalog:__internal" not in cats
    assert any("12 catalogs" in w for w in frag.warnings)
