"""Tests for _topology.py — discover + render the multi-agent graph.

Covers:
  - discover_topology pulls AgentNodes for apx-tagged models and edges
    from apx.agent.metadata.sub_agents
  - Apps URLs vs endpoint refs vs bare names get classified correctly
  - Models without apx.agent.name tags are skipped
  - render_topology mermaid and graphviz outputs include all nodes + edges
  - Sub-agent targets that don't have their own node show up as external
    targets in the rendered graph
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apx_agent import (
    AgentNode,
    Topology,
    TopologyEdge,
    discover_topology,
    render_topology,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _model(name: str, *, tags: dict[str, str], catalog: str = "main", schema: str = "agents") -> object:
    return SimpleNamespace(
        name=name,
        catalog_name=catalog,
        schema_name=schema,
        full_name=f"{catalog}.{schema}.{name}",
        tags=[SimpleNamespace(key=k, value=v) for k, v in tags.items()],
    )


def _make_ws(models: list[object]) -> MagicMock:
    ws = MagicMock()
    ws.registered_models.list.return_value = iter(models)
    return ws


# ---------------------------------------------------------------------------
# discover_topology
# ---------------------------------------------------------------------------


def test_discover_pulls_apx_tagged_models_as_nodes() -> None:
    ws = _make_ws([
        _model("triage", tags={
            "apx.agent.name": "triage",
            "apx.agent.model": "databricks-claude-sonnet-4-6",
            "apx.agent.tool_count": "3",
            "apx.agent.metadata": json.dumps({
                "sub_agents": [],
                "resources": [{"kind": "uc_function", "identifier": "main.tools.x"}],
            }),
        }),
        _model("untagged", tags={"other.tag": "x"}),  # should not appear
    ])

    topo = discover_topology(ws)

    assert len(topo.nodes) == 1
    node = topo.nodes[0]
    assert node.name == "triage"
    assert node.model_endpoint == "databricks-claude-sonnet-4-6"
    assert node.tool_count == 3
    assert "uc_function" in node.resource_kinds


def test_discover_extracts_edges_from_metadata_sub_agents() -> None:
    ws = _make_ws([
        _model("triage", tags={
            "apx.agent.name": "triage",
            "apx.agent.metadata": json.dumps({
                "sub_agents": [
                    "endpoints/billing",
                    "serving-endpoints/technical",
                    "data-triage",  # bare name
                ],
            }),
        }),
    ])

    topo = discover_topology(ws)

    assert len(topo.edges) == 3
    target_names = {e.target for e in topo.edges}
    assert target_names == {"billing", "technical", "data-triage"}
    for e in topo.edges:
        assert e.source == "triage"
        assert e.target_kind == "endpoint"


def test_discover_classifies_apps_urls_separately() -> None:
    ws = _make_ws([
        _model("triage", tags={
            "apx.agent.name": "triage",
            "apx.agent.metadata": json.dumps({
                "sub_agents": [
                    "https://billing.workspace.databricksapps.com",
                ],
            }),
        }),
    ])

    topo = discover_topology(ws)

    assert len(topo.edges) == 1
    e = topo.edges[0]
    assert e.target_kind == "app_url"
    assert "billing.workspace.databricksapps.com" in e.target


def test_discover_falls_back_to_csv_sub_agents_tag() -> None:
    """When metadata doesn't have sub_agents, the comma-separated tag is read."""
    ws = _make_ws([
        _model("triage", tags={
            "apx.agent.name": "triage",
            "apx.agent.sub_agents": "endpoints/billing,endpoints/technical",
        }),
    ])

    topo = discover_topology(ws)

    target_names = {e.target for e in topo.edges}
    assert target_names == {"billing", "technical"}


def test_discover_handles_malformed_metadata_json() -> None:
    ws = _make_ws([
        _model("triage", tags={
            "apx.agent.name": "triage",
            "apx.agent.metadata": "not valid json[",
        }),
    ])

    topo = discover_topology(ws)
    assert len(topo.nodes) == 1
    assert topo.edges == ()


# ---------------------------------------------------------------------------
# render_topology
# ---------------------------------------------------------------------------


def _sample_topology() -> Topology:
    return Topology(
        nodes=(
            AgentNode(
                name="triage",
                uc_name="main.agents.triage",
                model_endpoint="databricks-claude-sonnet-4-6",
                tool_count=3,
                resource_kinds=("uc_function", "genie_space"),
            ),
            AgentNode(
                name="billing",
                uc_name="main.agents.billing",
                model_endpoint="databricks-claude-sonnet-4-6",
            ),
        ),
        edges=(
            TopologyEdge(source="triage", target="billing", target_kind="endpoint"),
            TopologyEdge(source="triage", target="external_lookup", target_kind="endpoint"),
        ),
    )


def test_render_mermaid_includes_nodes_and_edges() -> None:
    text = render_topology(_sample_topology(), format="mermaid")
    assert text.startswith("graph LR")
    assert "triage" in text
    assert "billing" in text
    assert "-->" in text
    # External (not in nodes) shows up too
    assert "external_lookup" in text


def test_render_mermaid_embeds_model_endpoint_as_subtext() -> None:
    text = render_topology(_sample_topology(), format="mermaid")
    assert "databricks-claude-sonnet-4-6" in text


def test_render_graphviz_emits_digraph() -> None:
    text = render_topology(_sample_topology(), format="graphviz")
    assert text.startswith("digraph apx_topology")
    assert '"triage"' in text
    assert '"billing"' in text
    assert '"triage" -> "billing"' in text


def test_render_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="Unknown format"):
        render_topology(_sample_topology(), format="svg")  # type: ignore[arg-type]


def test_render_handles_empty_topology() -> None:
    topo = Topology(nodes=(), edges=())
    mermaid = render_topology(topo, format="mermaid")
    assert mermaid.startswith("graph LR")
    graphviz = render_topology(topo, format="graphviz")
    assert graphviz.startswith("digraph apx_topology")
