"""Workspace graph: merge, queries, and per-identity cache."""

from __future__ import annotations

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
