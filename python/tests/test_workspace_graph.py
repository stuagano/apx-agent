"""Workspace graph: merge, queries, and per-identity cache."""

from __future__ import annotations

import time

import pytest
from unittest.mock import MagicMock

from apx_agent.workspace_graph import builder, connectors
from apx_agent.workspace_graph.builder import Graph
from apx_agent.workspace_graph.schema import Edge, Fragment, Node, node_id


def _n(prefix: str, fqn: str, type_: str) -> Node:
    return Node(node_id(prefix, fqn), type_, fqn.split(".")[-1], fqn)


def _small_graph() -> Graph:
    """catalog main ⊃ schema sales ⊃ {orders, orders_v (view of orders), unused};
    a VS index over orders, a Genie space using orders_v, and an endpoint serving a model."""
    cat = _n("catalog", "main", "Catalog")
    sch = _n("schema", "main.sales", "Schema")
    orders = _n("table", "main.sales.orders", "Table")
    view = _n("table", "main.sales.orders_v", "View")
    unused = _n("table", "main.sales.unused", "Table")
    agg = _n("table", "main.sales.daily", "Table")
    vs = _n("vsindex", "main.sales.orders_idx", "VectorSearchIndex")
    genie = Node("genie:g1", "GenieSpace", "Sales Genie", "g1")
    model = _n("model", "main.sales.churn", "Model")
    ep = Node("serving:churn-ep", "ServingEndpoint", "churn-ep", "churn-ep")
    uc = Fragment(
        nodes=[cat, sch, orders, view, unused, agg, model],
        edges=[
            Edge(cat.id, sch.id, "contains"),
            *(
                Edge(sch.id, t.id, "contains")
                for t in (orders, view, unused, agg, model)
            ),
            Edge(view.id, orders.id, "derivedFrom"),
            Edge(orders.id, agg.id, "feedsInto"),
        ],
        warnings=[],
    )
    ai = Fragment(
        nodes=[vs, genie, ep],
        edges=[
            Edge(vs.id, orders.id, "indexesFrom"),
            Edge(genie.id, view.id, "uses"),
            Edge(ep.id, model.id, "serves"),
        ],
        warnings=["genie: partial"],
    )
    return Graph([uc, ai])


def test_merge_counts_and_summary():
    g = _small_graph()
    s = g.summary()
    assert s["node_count"] == 10
    assert (
        s["edge_count"] == 11
    )  # 6 contains + derivedFrom + feedsInto + indexesFrom + uses + serves
    assert s["node_types"]["Table"] == 3
    assert s["edge_types"]["contains"] == 6
    assert s["warnings"] == ["genie: partial"]
    assert s["built_at"]


def test_dangling_edges_dropped():
    frag = Fragment(
        nodes=[Node("genie:g1", "GenieSpace", "G", "g1")],
        edges=[Edge("genie:g1", "table:other.x.y", "uses")],
        warnings=[],
    )
    g = Graph([frag])
    assert g.edges == []
    assert g.resolve("genie:g1") is not None


def test_duplicate_edges_collapse():
    a, b = Node("a:1", "Table", "1", "1"), Node("a:2", "Table", "2", "2")
    g = Graph([Fragment([a, b], [Edge(a.id, b.id, "feedsInto")] * 2, [])])
    assert len(g.edges) == 1


def test_resolve_is_case_insensitive():
    g = _small_graph()
    assert g.resolve("Main.Sales.Orders").id == "table:main.sales.orders"
    assert g.resolve("TABLE:main.sales.orders").id == "table:main.sales.orders"
    assert g.resolve("nope") is None


def test_impact_follows_data_flow_and_containment():
    g = _small_graph()
    ids = {n.id for n in g.impact("main.sales.orders")}
    # view derived from it, index built from it, table it feeds, genie using the view
    assert ids == {
        "table:main.sales.orders_v",
        "vsindex:main.sales.orders_idx",
        "table:main.sales.daily",
        "genie:g1",
    }
    # containment: dropping the schema affects everything in it and downstream
    assert "serving:churn-ep" in {n.id for n in g.impact("schema:main.sales")}


def test_lineage_is_data_flow_only_and_depth_capped():
    g = _small_graph()
    lin = g.lineage("table:main.sales.orders_v")
    assert [n.id for n in lin["upstream"]] == ["table:main.sales.orders"]
    assert [n.id for n in lin["downstream"]] == ["genie:g1"]
    assert all(n.type != "Schema" for n in lin["upstream"])
    one = g.lineage("genie:g1", depth=1)
    assert [n.id for n in one["upstream"]] == ["table:main.sales.orders_v"]


def test_orphans_ignore_containment():
    g = _small_graph()
    assert [n.id for n in g.orphans()] == ["table:main.sales.unused"]


def test_search_and_type_filter():
    g = _small_graph()
    assert {n.id for n in g.search("orders")} >= {
        "table:main.sales.orders",
        "table:main.sales.orders_v",
        "vsindex:main.sales.orders_idx",
    }
    assert [n.type for n in g.search("orders", asset_type="View")] == ["View"]
    assert len(g.search("")) == 10


def test_get_returns_connections():
    g = _small_graph()
    got = g.get("main.sales.orders")
    assert got["node"]["id"] == "table:main.sales.orders"
    assert {(e["type"], e["node"]["id"]) for e in got["incoming"]} >= {
        ("derivedFrom", "table:main.sales.orders_v"),
        ("contains", "schema:main.sales"),
    }
    assert ("feedsInto", "table:main.sales.daily") in {
        (e["type"], e["node"]["id"]) for e in got["outgoing"]
    }
    assert g.get("missing") is None


def test_suggest_close_names():
    assert "table:main.sales.orders" in _small_graph().suggest("ordrs")


def test_to_json_shape():
    body = _small_graph().to_json()
    assert set(body) == {"nodes", "edges", "warnings", "built_at"}
    assert set(body["nodes"][0]) == {"id", "type", "name", "fqn", "props"}
    assert set(body["edges"][0]) == {"src", "dst", "type"}


@pytest.fixture(autouse=True)
def _clear_cache():
    builder._CACHE.clear()
    yield
    builder._CACHE.clear()


def _ws(token: str = "u1") -> MagicMock:
    ws = MagicMock()
    ws.config.authenticate.return_value = {"Authorization": f"Bearer {token}"}
    return ws


def _fake_connectors(monkeypatch, calls: list[str]):
    def ok(ws, catalogs):
        calls.append("ok")
        return Fragment(
            [Node("table:main.s.t", "Table", "t", "main.s.t")], [], ["capped"]
        )

    def boom(ws, catalogs):
        raise PermissionError("no Genie permission")

    monkeypatch.setattr(connectors, "CONNECTORS", {"unity_catalog": ok, "genie": boom})
    monkeypatch.setattr(
        connectors,
        "lineage",
        lambda ws, fqns: Fragment(warnings=[f"lineage over {fqns}"]),
    )


def test_build_graph_isolates_connector_failure(monkeypatch):
    _fake_connectors(monkeypatch, [])
    g = builder.build_graph(_ws(), ["main"])
    assert list(g.nodes) == ["table:main.s.t"]
    assert g.warnings == [
        "genie: no Genie permission",
        "lineage: lineage over ['main.s.t']",
        "unity_catalog: capped",
    ]


def test_get_graph_caches_and_refresh_bypasses(monkeypatch):
    calls: list[str] = []
    _fake_connectors(monkeypatch, calls)
    ws = _ws()
    first = builder.get_graph(ws, ["main"])
    assert builder.get_graph(ws, ["main"]) is first
    assert builder.get_graph(ws, ["main"], refresh=True) is not first
    assert calls == ["ok", "ok"]


def test_cache_isolated_per_identity(monkeypatch):
    calls: list[str] = []
    _fake_connectors(monkeypatch, calls)
    a = builder.get_graph(_ws("user-a"), ["main"])
    b = builder.get_graph(_ws("user-b"), ["main"])
    assert a is not b
    assert calls == ["ok", "ok"]
    assert all("user-a" not in k for k in builder._CACHE)  # token never stored raw


def test_cache_keyed_by_scope_and_expires(monkeypatch):
    calls: list[str] = []
    _fake_connectors(monkeypatch, calls)
    ws = _ws()
    builder.get_graph(ws, ["main"])
    builder.get_graph(ws, ["other"])
    assert len(calls) == 2
    builder.get_graph(ws, ["main", "other"])
    builder.get_graph(ws, ["other", "main"])  # order-insensitive scope
    assert len(calls) == 3
    clock = [1000.0]
    monkeypatch.setattr(builder.time, "monotonic", lambda: clock[0])
    builder.get_graph(ws, ["x"])
    clock[0] += builder.CACHE_TTL_S + 1
    builder.get_graph(ws, ["x"])
    assert len(calls) == 5


def test_concurrent_get_graph_thread_safe(monkeypatch):
    import concurrent.futures

    _fake_connectors(monkeypatch, [])
    clock = [1000.0]
    monkeypatch.setattr(builder.time, "monotonic", lambda: clock[0])

    def call_get_graph(scope_idx: int) -> Graph:
        # Each thread gets a slightly different scope to create independent cache entries
        return builder.get_graph(_ws(), [f"scope_{scope_idx}"])

    # First: warm the cache with a few entries
    for i in range(4):
        call_get_graph(i)

    # Then: make prior entries stale and run concurrent requests
    clock[0] += builder.CACHE_TTL_S + 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futures = [ex.submit(call_get_graph, i) for i in range(16)]
        results = [f.result(timeout=5) for f in futures]
    assert all(isinstance(g, Graph) for g in results)
    assert len(results) == 16


def test_concurrent_misses_share_one_build(monkeypatch):
    import threading

    gate = threading.Event()
    started = threading.Event()
    runs: list[int] = []

    def slow(ws, catalogs):
        runs.append(1)
        started.set()
        gate.wait(5)
        return Fragment([Node("table:main.s.t", "Table", "t", "main.s.t")])

    monkeypatch.setattr(connectors, "CONNECTORS", {"unity_catalog": slow})
    monkeypatch.setattr(connectors, "lineage", lambda ws, fqns: Fragment())
    ws = _ws()
    results: list[Graph] = []

    def call() -> None:
        results.append(builder.get_graph(ws, ["main"]))

    threads = [threading.Thread(target=call) for _ in range(5)]
    for t in threads:
        t.start()
    assert started.wait(5)
    time.sleep(0.2)  # let the other callers reach the in-flight build
    gate.set()
    for t in threads:
        t.join(5)
    assert len(runs) == 1
    assert len(results) == 5 and all(g is results[0] for g in results)
    assert builder._INFLIGHT == {}


def test_failed_build_raises_in_all_waiters_and_retries(monkeypatch):
    import threading

    gate = threading.Event()
    started = threading.Event()
    runs: list[int] = []

    def boom_build(ws, catalogs):
        runs.append(1)
        started.set()
        gate.wait(5)
        raise RuntimeError("build exploded")

    monkeypatch.setattr(builder, "build_graph", boom_build)
    ws = _ws()
    errors: list[BaseException] = []

    def call() -> None:
        try:
            builder.get_graph(ws, ["main"])
        except RuntimeError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    assert started.wait(5)
    time.sleep(0.2)  # let the other callers reach the in-flight build
    gate.set()
    for t in threads:
        t.join(5)
    assert len(runs) == 1 and len(errors) == 4
    assert builder._INFLIGHT == {}
    monkeypatch.setattr(builder, "build_graph", lambda ws, c: Graph([]))
    assert isinstance(builder.get_graph(ws, ["main"]), Graph)


def test_partial_graph_expires_after_short_ttl(monkeypatch):
    calls: list[str] = []
    _fake_connectors(monkeypatch, calls)  # genie raises -> warnings -> partial
    clock = [1000.0]
    monkeypatch.setattr(builder.time, "monotonic", lambda: clock[0])
    ws = _ws()
    first = builder.get_graph(ws, ["main"])
    assert first.warnings
    clock[0] += builder.PARTIAL_TTL_S - 1
    assert builder.get_graph(ws, ["main"]) is first
    clock[0] += 2
    assert builder.get_graph(ws, ["main"]) is not first
    assert len(calls) == 2


def test_clean_graph_keeps_full_ttl(monkeypatch):
    monkeypatch.setattr(builder, "build_graph", lambda ws, c: Graph([]))
    clock = [1000.0]
    monkeypatch.setattr(builder.time, "monotonic", lambda: clock[0])
    ws = _ws()
    first = builder.get_graph(ws, ["main"])
    clock[0] += builder.PARTIAL_TTL_S + 1
    assert builder.get_graph(ws, ["main"]) is first
    clock[0] += builder.CACHE_TTL_S
    assert builder.get_graph(ws, ["main"]) is not first
