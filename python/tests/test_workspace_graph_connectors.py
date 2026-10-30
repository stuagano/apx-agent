"""Workspace graph connectors over fake SDK clients."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apx_agent._apps_discovery import AppAgentInfo
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


def test_one_cause_across_schemas_is_one_readable_warning():
    # A scope gap fails functions/models/volumes in every schema; the SDK appends its whole Config.
    ws = _uc_ws()
    ws.schemas.list.return_value = [SimpleNamespace(name="sales"), SimpleNamespace(name="ops")]
    denied = ("Provided OAuth token does not have required scopes: unity-catalog [ReqId: 8d8ea5c9]. "
              "Config: host=https://example.cloud.databricks.com, client_id=abc, client_secret=***")
    for lister in (ws.functions.list, ws.registered_models.list, ws.volumes.list):
        lister.side_effect = PermissionError(denied)
    frag = connectors.unity_catalog(ws, ["main"])
    assert frag.warnings == [
        "functions, models, volumes in 2 schemas (main.ops, main.sales): "
        "Provided OAuth token does not have required scopes: unity-catalog"
    ]
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


def test_serving_connector_links_served_models():
    ws = MagicMock()
    ws.config.host = "https://h.cloud.databricks.com"
    ws.serving_endpoints.list.return_value = [
        SimpleNamespace(
            name="churn-ep",
            state=SimpleNamespace(ready=SimpleNamespace(value="READY")),
            task=None,
            config=SimpleNamespace(
                served_entities=[SimpleNamespace(entity_name="main.sales.churn")]
            ),
        )
    ]
    frag = connectors.serving(ws, None)
    assert [(n.id, n.type) for n in frag.nodes] == [
        ("serving:churn-ep", "ServingEndpoint")
    ]
    assert [(e.dst, e.type) for e in frag.edges] == [
        ("model:main.sales.churn", "serves")
    ]


def test_serving_connector_raises_on_denied_list():
    ws = MagicMock()
    ws.config.host = "https://h"
    ws.serving_endpoints.list.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        connectors.serving(ws, None)


def test_genie_connector_uses_serialized_space_tables():
    ws = MagicMock()
    ws.config.host = "https://h"
    ws.genie.list_spaces.return_value = SimpleNamespace(
        spaces=[SimpleNamespace(space_id="g1", title="Sales", description=None)],
        next_page_token=None,
    )
    ws.genie.get_space.return_value = SimpleNamespace(
        serialized_space=json.dumps(
            {"data_sources": {"tables": [{"identifier": "Main.Sales.Orders"}]}}
        )
    )
    frag = connectors.genie(ws, None)
    assert [n.id for n in frag.nodes] == ["genie:g1"]
    assert [(e.dst, e.type) for e in frag.edges] == [
        ("table:main.sales.orders", "uses")
    ]
    ws.genie.get_space.assert_called_once_with("g1", include_serialized_space=True)


def test_genie_space_detail_failure_is_a_warning():
    ws = MagicMock()
    ws.config.host = "https://h"
    ws.genie.list_spaces.return_value = SimpleNamespace(
        spaces=[SimpleNamespace(space_id="g1", title="Sales", description=None)],
        next_page_token=None,
    )
    ws.genie.get_space.side_effect = PermissionError("no CAN_VIEW")
    frag = connectors.genie(ws, None)
    assert [n.id for n in frag.nodes] == ["genie:g1"]
    assert frag.warnings == ["Sales tables: no CAN_VIEW"]


def test_vector_search_connector(monkeypatch):
    monkeypatch.setattr(
        "apx_agent._ui_probe._discover_vs_indexes",
        lambda ws: [
            {
                "endpoint": "vs-ep",
                "endpoint_state": "ONLINE",
                "index": "main.sales.orders_idx",
                "source_table": "main.sales.orders",
                "ready": True,
                "columns": ["content"],
            }
        ],
    )
    ws = MagicMock()
    ws.config.host = "https://h"
    frag = connectors.vector_search(ws, None)
    assert [n.id for n in frag.nodes] == ["vsindex:main.sales.orders_idx"]
    assert [(e.dst, e.type) for e in frag.edges] == [
        ("table:main.sales.orders", "indexesFrom")
    ]


def test_vector_search_connector_raises_when_endpoints_unlistable(monkeypatch):
    monkeypatch.setattr(
        "apx_agent._ui_probe._discover_vs_indexes",
        lambda ws: [{"error": "Could not list endpoints: denied"}],
    )
    ws = MagicMock()
    ws.config.host = "https://h"
    with pytest.raises(RuntimeError, match="denied"):
        connectors.vector_search(ws, None)


def test_apps_connector_uses_declared_resources(monkeypatch):
    from databricks.sdk.service.apps import (
        AppResource,
        AppResourceGenieSpace,
        AppResourceGenieSpaceGenieSpacePermission,
        AppResourceServingEndpoint,
        AppResourceServingEndpointServingEndpointPermission,
        AppResourceUcSecurable,
        AppResourceUcSecurableUcSecurablePermission,
        AppResourceUcSecurableUcSecurableType,
    )

    monkeypatch.setattr(
        "apx_agent.workspace_graph.connectors.discover_app_agents",
        lambda ws: [
            AppAgentInfo(
                name="triage",
                app_name="triage-app",
                url="https://t.databricksapps.com",
                description=None,
                tool_count=1,
                state="RUNNING",
                tools=("lookup",),
            )
        ],
    )
    ws = MagicMock()
    ws.apps.get.return_value = SimpleNamespace(
        resources=[
            AppResource(
                name="t",
                uc_securable=AppResourceUcSecurable(
                    securable_full_name="main.sales.orders",
                    securable_type=AppResourceUcSecurableUcSecurableType.TABLE,
                    permission=AppResourceUcSecurableUcSecurablePermission.SELECT,
                ),
            ),
            AppResource(
                name="g",
                genie_space=AppResourceGenieSpace(
                    name="g",
                    space_id="g1",
                    permission=AppResourceGenieSpaceGenieSpacePermission.CAN_VIEW,
                ),
            ),
            AppResource(
                name="e",
                serving_endpoint=AppResourceServingEndpoint(
                    name="churn-ep",
                    permission=AppResourceServingEndpointServingEndpointPermission.CAN_QUERY,
                ),
            ),
        ]
    )
    frag = connectors.apps(ws, None)
    assert [n.id for n in frag.nodes] == ["app:triage-app"]
    assert {(e.dst, e.type) for e in frag.edges} == {
        ("table:main.sales.orders", "uses"),
        ("genie:g1", "uses"),
        ("serving:churn-ep", "uses"),
    }


def test_lineage_connector_feeds_into_and_caps(monkeypatch):
    # The REST path is the fallback when system.access.table_lineage can't be queried.
    monkeypatch.setattr(connectors, "MAX_LINEAGE_TABLES", 2)
    def _no_sql(*a, **k):
        raise PermissionError("no system.access")
    monkeypatch.setattr(connectors, "run_sql", _no_sql)
    monkeypatch.setenv("MLFLOW_TRACING_SQL_WAREHOUSE_ID", "wh-app")
    ws = MagicMock()

    def _do(method, path, query):
        # The real endpoint; /api/2.1/unity-catalog/... 404s ("No API found").
        assert (method, path) == ("GET", "/api/2.0/lineage-tracking/table-lineage")
        if query["table_name"] == "main.sales.bad":
            raise PermissionError("no lineage access")
        return {
            "downstreams": [
                {
                    "tableInfo": {
                        "catalog_name": "main",
                        "schema_name": "sales",
                        "name": "daily",
                    }
                }
            ]
        }

    ws.api_client.do.side_effect = _do
    frag = connectors.lineage(
        ws, ["main.sales.orders", "main.sales.bad", "main.sales.ignored"]
    )
    assert [(e.src, e.dst, e.type) for e in frag.edges] == [
        ("table:main.sales.orders", "table:main.sales.daily", "feedsInto")
    ]
    assert "system.access.table_lineage unavailable" in frag.warnings[0]
    assert "PermissionError" in frag.warnings[0]
    assert "checked first 2 of 3 tables" in frag.warnings[1]
    assert "1 of 2 tables failed" in frag.warnings[2]


def test_lineage_reads_system_table_in_one_bound_query(monkeypatch):
    # Apps user tokens can't reach the lineage REST API; one SQL query over the
    # caller's own system.access.table_lineage covers every discovered table.
    calls = []

    def _run_sql(ws, sql, *, parameters, warehouse_id, poll_timeout_s):
        calls.append((sql, parameters, warehouse_id, poll_timeout_s))
        return [{"src": "main.sales.orders", "dst": "main.sales.daily"},
                {"src": "Main.Sales.Orders", "dst": "main.gold.revenue"}]

    monkeypatch.setattr(connectors, "run_sql", _run_sql)
    # The deployed App's own warehouse (apx writes it into app.yaml), not the first one listed.
    monkeypatch.setenv("MLFLOW_TRACING_SQL_WAREHOUSE_ID", "wh-app")
    ws = MagicMock()
    tables = [f"main.sales.t{i}" for i in range(300)] + ["main.sales.orders", "main.sales.daily", "Main.Gold.Revenue"]
    frag = connectors.lineage(ws, tables)
    [(sql, params, warehouse_id, poll_timeout_s)] = calls
    assert warehouse_id == "wh-app"
    assert poll_timeout_s <= 30  # a user-facing request: bounded, then fall back
    assert "365" in sql and "lower(target_table_full_name)" in sql  # a year; both ends in the graph
    assert "system.access.table_lineage" in sql and ":tables" in sql
    # Bound, not interpolated; joined on '/', which UC names can't contain (commas they can).
    assert params == [{"name": "tables", "value": "/".join(tables), "type": "STRING"}]
    assert [(e.src, e.dst, e.type) for e in frag.edges] == [
        ("table:main.sales.orders", "table:main.sales.daily", "feedsInto"),
        ("table:main.sales.orders", "table:main.gold.revenue", "feedsInto"),  # ids are case-insensitive
    ]
    assert frag.warnings == []  # no 200-table cap on this path
    ws.api_client.do.assert_not_called()


def test_lineage_warns_only_when_edges_were_cut(monkeypatch):
    monkeypatch.setattr(connectors, "MAX_LINEAGE_EDGES", 1)
    monkeypatch.setenv("MLFLOW_TRACING_SQL_WAREHOUSE_ID", "wh-app")
    rows = [{"src": "a.b.c", "dst": "a.b.d"}]
    monkeypatch.setattr(connectors, "run_sql", lambda ws, sql, **k: list(rows))
    assert connectors.lineage(MagicMock(), ["a.b.c", "a.b.d"]).warnings == []  # exactly at the cap
    rows.append({"src": "a.b.c", "dst": "a.b.e"})
    frag = connectors.lineage(MagicMock(), ["a.b.c", "a.b.d"])
    assert len(frag.edges) == 1 and "first 1 lineage edges" in frag.warnings[0]


def test_lineage_without_app_warehouse_uses_the_api_and_starts_nothing(monkeypatch):
    # Locally there's no App warehouse: never auto-pick (and start) one for a graph refresh.
    monkeypatch.delenv("MLFLOW_TRACING_SQL_WAREHOUSE_ID", raising=False)
    def _fail(*a, **k):
        raise AssertionError("no SQL without the App warehouse")
    monkeypatch.setattr(connectors, "run_sql", _fail)
    ws = MagicMock()
    ws.api_client.do.return_value = {"downstreams": []}
    frag = connectors.lineage(ws, ["main.s.t"])
    ws.api_client.do.assert_called_once()
    assert frag.warnings == []


def test_lineage_without_tables_runs_nothing(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("no query without tables")
    monkeypatch.setattr(connectors, "run_sql", _fail)
    ws = MagicMock()
    assert connectors.lineage(ws, []).edges == []
    ws.api_client.do.assert_not_called()


def test_connector_registry_names():
    assert set(connectors.CONNECTORS) == {
        "unity_catalog",
        "serving",
        "genie",
        "vector_search",
        "apps",
    }


def test_serving_warns_when_listing_cap_hit():
    ws = MagicMock()
    ws.config.host = "https://h"
    ws.serving_endpoints.list.return_value = [
        SimpleNamespace(name=f"ep{i}", state=None, task=None, config=None)
        for i in range(60)
    ]
    frag = connectors.serving(ws, None)
    assert len(frag.nodes) == 50
    assert frag.warnings == ["showing first 50 serving endpoints"]


def test_genie_warns_when_listing_cap_hit():
    ws = MagicMock()
    ws.config.host = "https://h"
    ws.genie.list_spaces.return_value = SimpleNamespace(
        spaces=[
            SimpleNamespace(space_id=f"g{i}", title=f"S{i}", description=None)
            for i in range(60)
        ],
        next_page_token="more",
    )
    ws.genie.get_space.return_value = SimpleNamespace(serialized_space=None)
    frag = connectors.genie(ws, None)
    assert len(frag.nodes) == 50
    assert frag.warnings == ["showing first 50 genie spaces"]


def test_vector_search_warns_when_listing_cap_hit(monkeypatch):
    monkeypatch.setattr(
        "apx_agent._ui_probe._discover_vs_indexes",
        lambda ws: [
            {
                "endpoint": "vs-ep",
                "endpoint_state": "ONLINE",
                "index": f"main.s.idx{i}",
                "source_table": None,
                "ready": True,
                "columns": [],
            }
            for i in range(60)
        ],
    )
    ws = MagicMock()
    ws.config.host = "https://h"
    frag = connectors.vector_search(ws, None)
    assert len(frag.nodes) == 50
    assert frag.warnings == ["showing first 50 vector search indexes"]


def test_genie_details_fetched_concurrently_in_order():
    import threading

    barrier = threading.Barrier(2, timeout=3)

    def get_space(space_id, include_serialized_space):
        barrier.wait()  # serial fetching would never reach 2 waiters
        return SimpleNamespace(
            serialized_space=json.dumps(
                {"data_sources": {"tables": [{"identifier": f"m.s.{space_id}"}]}}
            )
        )

    ws = MagicMock()
    ws.config.host = "https://h"
    ws.genie.list_spaces.return_value = SimpleNamespace(
        spaces=[
            SimpleNamespace(space_id=f"g{i}", title=f"S{i}", description=None)
            for i in range(2)
        ],
        next_page_token=None,
    )
    ws.genie.get_space.side_effect = get_space
    frag = connectors.genie(ws, None)
    assert frag.warnings == []
    assert [n.id for n in frag.nodes] == ["genie:g0", "genie:g1"]
    assert [(e.src, e.dst) for e in frag.edges] == [
        ("genie:g0", "table:m.s.g0"),
        ("genie:g1", "table:m.s.g1"),
    ]


def test_apps_details_fetched_concurrently_in_order(monkeypatch):
    import threading

    barrier = threading.Barrier(2, timeout=3)
    agents = [
        AppAgentInfo(
            name=f"a{i}",
            app_name=f"app{i}",
            url=None,
            description=None,
            tool_count=0,
            state="RUNNING",
            tools=(),
        )
        for i in range(2)
    ]
    monkeypatch.setattr(
        "apx_agent.workspace_graph.connectors.discover_app_agents", lambda ws: agents
    )

    def get(name):
        barrier.wait()
        if name == "app0":
            raise PermissionError("denied")
        return SimpleNamespace(resources=[])

    ws = MagicMock()
    ws.apps.get.side_effect = get
    frag = connectors.apps(ws, None)
    assert [n.id for n in frag.nodes] == ["app:app0", "app:app1"]
    assert frag.warnings == ["app0 resources: denied"]
