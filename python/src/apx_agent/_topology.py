"""Topology visualization — render the multi-agent endpoint graph.

Scans Unity Catalog for registered models tagged with ``apx.agent.name``
(the tags ``set_uc_tags_for_agent`` writes after deploy), parses the
``apx.agent.metadata`` blob to extract declared sub-agent endpoints,
and produces a directed graph in either Mermaid or Graphviz syntax.

The graph shows agent-to-agent edges so you can answer "what calls
what" without inspecting individual agent code or stitching together
the answer from system tables. Combined with the cost report, this is
the production view of the multi-agent estate.

Typical usage::

    from databricks.sdk import WorkspaceClient
    from apx_agent import discover_topology, render_topology

    topo = discover_topology(WorkspaceClient())
    print(render_topology(topo, format="mermaid"))

or via the CLI::

    apx topology --format mermaid > topology.mmd
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentNode:
    """One agent in the topology graph."""

    name: str
    uc_name: str
    model_endpoint: str | None = None
    tool_count: int | None = None
    resource_kinds: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TopologyEdge:
    """A directed edge from one agent to another it can call."""

    source: str  # agent name
    target: str  # sub-agent identifier (endpoint name or URL)
    target_kind: str  # "endpoint" | "app_url" | "unresolved"


@dataclass(frozen=True)
class Topology:
    """Collected agents + edges discovered from UC tags."""

    nodes: tuple[AgentNode, ...]
    edges: tuple[TopologyEdge, ...]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_topology(
    ws: "WorkspaceClient",
    *,
    catalog: str | None = None,
    schema: str | None = None,
) -> Topology:
    """Walk UC registered models to build the agent-to-agent graph.

    Args:
        ws: ``WorkspaceClient`` used to list registered models.
        catalog: Optional UC catalog filter.
        schema: Optional UC schema filter (requires ``catalog``).

    Returns:
        ``Topology`` with one ``AgentNode`` per discovered agent and one
        ``TopologyEdge`` per declared sub-agent link. Sub-agents that
        appear as edge targets but don't have their own
        ``apx.agent.name`` tag stay in the graph as targets without a
        corresponding node — useful for spotting external dependencies
        (Apps URLs, ad-hoc endpoint references).
    """
    try:
        models_iter = ws.registered_models.list(
            catalog_name=catalog,
            schema_name=schema,
            include_browse=False,
        )
        models = list(models_iter)
    except TypeError:
        models = list(ws.registered_models.list())  # type: ignore[call-arg]

    nodes: list[AgentNode] = []
    edges: list[TopologyEdge] = []
    seen_names: set[str] = set()

    for m in models:
        tags = {t.key: t.value for t in (getattr(m, "tags", None) or [])}
        agent_name = tags.get("apx.agent.name")
        if not agent_name:
            continue
        uc_name = (
            getattr(m, "full_name", None)
            or f"{getattr(m, 'catalog_name', '')}.{getattr(m, 'schema_name', '')}.{getattr(m, 'name', '')}"
        )
        tool_count = _coerce_int(tags.get("apx.agent.tool_count"))
        resource_kinds = _resource_kinds_from_metadata(tags.get("apx.agent.metadata"))
        nodes.append(AgentNode(
            name=agent_name,
            uc_name=uc_name,
            model_endpoint=tags.get("apx.agent.model"),
            tool_count=tool_count,
            resource_kinds=tuple(sorted(resource_kinds)),
        ))
        seen_names.add(agent_name)

        # Sub-agent links from the metadata blob (preferred — full URL/name)
        # or from apx.agent.sub_agents (comma-separated fallback).
        sub_agents = _sub_agents_from_metadata(tags.get("apx.agent.metadata"))
        if not sub_agents:
            csv = tags.get("apx.agent.sub_agents") or ""
            sub_agents = [s.strip() for s in csv.split(",") if s.strip()]
        for raw in sub_agents:
            target, kind = _classify_sub_agent(raw)
            edges.append(TopologyEdge(
                source=agent_name, target=target, target_kind=kind,
            ))

    return Topology(nodes=tuple(nodes), edges=tuple(edges))


def _resource_kinds_from_metadata(metadata_json: str | None) -> list[str]:
    if not metadata_json:
        return []
    try:
        parsed = json.loads(metadata_json)
    except Exception:
        return []
    resources = parsed.get("resources") or []
    return sorted({r.get("kind", "") for r in resources if r.get("kind")})


def _sub_agents_from_metadata(metadata_json: str | None) -> list[str]:
    if not metadata_json:
        return []
    try:
        parsed = json.loads(metadata_json)
    except Exception:
        return []
    return [str(s) for s in (parsed.get("sub_agents") or [])]


def _classify_sub_agent(raw: str) -> tuple[str, str]:
    """Split a sub_agent reference into ``(display_name, kind)``.

    Strips ``endpoints/`` / ``serving-endpoints/`` prefixes; treats
    URLs containing ``databricksapps.com`` as Apps deployments; falls
    back to a bare name otherwise.
    """
    raw = raw.strip()
    if "databricksapps.com" in raw:
        # Apps URL — keep the URL but tag accordingly
        return raw, "app_url"
    if raw.startswith(("endpoints/", "serving-endpoints/")):
        _, _, name = raw.partition("/")
        return name, "endpoint"
    if "://" in raw:
        return raw, "unresolved"
    # Bare endpoint name
    return raw, "endpoint"


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_topology(
    topology: Topology,
    *,
    format: Literal["mermaid", "graphviz"] = "mermaid",
) -> str:
    """Render a ``Topology`` as a graph diagram.

    Args:
        topology: Output of ``discover_topology``.
        format: ``"mermaid"`` (Markdown-friendly, default) or
            ``"graphviz"`` (DOT for ``dot``-tooling).
    """
    if format == "mermaid":
        return _render_mermaid(topology)
    if format == "graphviz":
        return _render_graphviz(topology)
    raise ValueError(f"Unknown format {format!r}; expected 'mermaid' or 'graphviz'.")


def _render_mermaid(topology: Topology) -> str:
    lines = ["graph LR"]
    # Nodes — IDs must be Mermaid-safe, but names usually are; sanitise.
    node_ids: dict[str, str] = {}
    for node in topology.nodes:
        node_id = _safe_id(node.name)
        node_ids[node.name] = node_id
        label = node.name
        if node.model_endpoint:
            label += f"<br/><small>{node.model_endpoint}</small>"
        lines.append(f"  {node_id}[\"{label}\"]")
    # Edges — declare external targets as nodes too
    seen_external: set[str] = set()
    for edge in topology.edges:
        src_id = node_ids.get(edge.source) or _safe_id(edge.source)
        if edge.target not in node_ids and edge.target not in seen_external:
            tgt_id = _safe_id(edge.target)
            shape_open, shape_close = ("(", ")") if edge.target_kind == "endpoint" else ("[", "]")
            lines.append(f"  {tgt_id}{shape_open}\"{edge.target}\"{shape_close}")
            seen_external.add(edge.target)
        tgt_id = node_ids.get(edge.target) or _safe_id(edge.target)
        lines.append(f"  {src_id} --> {tgt_id}")
    return "\n".join(lines)


def _render_graphviz(topology: Topology) -> str:
    lines = ["digraph apx_topology {", "  rankdir=LR;", "  node [shape=box];"]
    for node in topology.nodes:
        label = node.name
        if node.model_endpoint:
            label += f"\\n{node.model_endpoint}"
        lines.append(f'  "{node.name}" [label="{label}"];')
    seen_external: set[str] = set()
    for edge in topology.edges:
        is_external = edge.target not in {n.name for n in topology.nodes}
        if is_external and edge.target not in seen_external:
            shape = "ellipse" if edge.target_kind == "endpoint" else "note"
            lines.append(f'  "{edge.target}" [shape={shape}];')
            seen_external.add(edge.target)
        lines.append(f'  "{edge.source}" -> "{edge.target}";')
    lines.append("}")
    return "\n".join(lines)


def _safe_id(name: str) -> str:
    import re

    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_")
    return cleaned or "n"
