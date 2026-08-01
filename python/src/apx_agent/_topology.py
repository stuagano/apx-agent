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

    apx-agent topology --format mermaid > topology.mmd
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from ._agents import BaseAgent
    from ._models import AgentContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SubAgentRef:
    display_name: str
    kind: str


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
            _ref = _classify_sub_agent(raw)
            edges.append(TopologyEdge(
                source=agent_name, target=_ref.display_name, target_kind=_ref.kind,
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


def _classify_sub_agent(raw: str) -> _SubAgentRef:
    """Split a sub_agent reference into ``(display_name, kind)``.

    Strips ``endpoints/`` / ``serving-endpoints/`` prefixes; treats
    URLs containing ``databricksapps.com`` as Apps deployments; resolves
    ``$APX_PEER_*_URL`` env refs to a friendly peer slug; falls back to a
    bare name otherwise.
    """
    raw = raw.strip()
    if raw.startswith("$"):
        key = raw[1:].strip("{}")
        if key.startswith("APX_PEER_") and key.endswith("_URL"):
            slug = key[len("APX_PEER_") : -len("_URL")]
            label = slug.replace("_", "-").lower() or key
            return _SubAgentRef(display_name=label, kind="env_ref")
        return _SubAgentRef(display_name=key or raw, kind="env_ref")
    if "databricksapps.com" in raw:
        host = raw.split("//", 1)[-1].split("/", 1)[0]
        app = host.split(".", 1)[0]
        m = re.match(r"^(.+)-(\d{10,})$", app)
        label = m.group(1) if m else (app or raw)
        return _SubAgentRef(display_name=label, kind="app_url")
    if raw.startswith(("endpoints/", "serving-endpoints/")):
        _, _, name = raw.partition("/")
        return _SubAgentRef(display_name=name, kind="endpoint")
    if "://" in raw:
        return _SubAgentRef(display_name=raw, kind="unresolved")
    # Bare endpoint name
    return _SubAgentRef(display_name=raw, kind="endpoint")


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


# ---------------------------------------------------------------------------
# In-process agent-tree topology — powers the /_apx/topology dev UI.
#
# Distinct from the UC-discovery functions above: ``build_topology`` walks the
# *live* ``BaseAgent`` tree (root agent + sub-agents + tools + declared
# resources) and produces the JSON shape consumed by the React graph at
# ``/_apx/topology``. ``inspect_node`` returns details for a single node by
# its stable ID. Both are pure functions on the running ``AgentContext``.
# ---------------------------------------------------------------------------


# Map ResourceSpec.kind -> (NodeType, id_prefix)
_RESOURCE_KIND_TO_TYPE: dict[str, tuple[str, str]] = {
    "uc_function": ("UCFunction", "uc"),
    "genie_space": ("GenieSpace", "genie"),
    "vector_search_index": ("VectorIndex", "vs"),
    "serving_endpoint": ("ServingEndpoint", "endpoint"),
    "sql_warehouse": ("WarehouseSQL", "warehouse"),
    "uc_table": ("UCFunction", "uc"),
    "uc_connection": ("UCFunction", "uc"),
    "lakebase_instance": ("ServingEndpoint", "endpoint"),
}

_RESOURCE_KIND_TO_DETAIL_KIND: dict[str, str] = {
    "uc_function": "uc_function",
    "genie_space": "genie_space",
    "vector_search_index": "vector_index",
    "serving_endpoint": "serving_endpoint",
    "sql_warehouse": "sql_warehouse",
    "uc_table": "uc_function",
    "uc_connection": "uc_function",
    "lakebase_instance": "serving_endpoint",
}


def _resource_node_for(kind: str, identifier: str) -> tuple[str, str, str] | None:
    """Return ``(node_id, node_type, label)`` for a ResourceSpec, or ``None``."""
    mapping = _RESOURCE_KIND_TO_TYPE.get(kind)
    if mapping is None:
        return None
    node_type, prefix = mapping
    return f"{prefix}:{identifier}", node_type, identifier


def _agent_class_to_node_type(agent: "BaseAgent") -> str:
    """Map an agent instance to its NodeType string.

    Uses ``type(agent).__name__`` directly so the spec's exact NodeType values
    are preserved (``LlmAgent`` for the canonical Agent class; ``Agent`` alias
    resolves to the same class so ``__name__`` is ``LlmAgent``).
    """
    cls_name = type(agent).__name__
    known = {
        "LlmAgent",
        "Agent",
        "DataAgent",
        "CoworkerAgent",
        "SequentialAgent",
        "ParallelAgent",
        "LoopAgent",
        "RouterAgent",
        "HandoffAgent",
        "KeywordRouter",
    }
    return cls_name if cls_name in known else "Agent"


def _agent_label(agent: "BaseAgent", fallback: str) -> str:
    name = getattr(agent, "_name", None)
    if name:
        return str(name)
    return fallback


def _instructions_for(agent: "BaseAgent") -> str:
    """Pull instructions string from any agent type; empty if not present."""
    instr = getattr(agent, "_instructions", "")
    if not instr and hasattr(agent, "_inner"):
        instr = getattr(getattr(agent, "_inner"), "_instructions", "")
    return str(instr or "")


def _iter_child_agents(agent: "BaseAgent") -> list[tuple[str, "BaseAgent"]]:
    """Return ``[(child_name, child_agent), ...]`` for any composition agent.

    Used both for building topology edges and for walking the tree. The child
    name is the dict key for HandoffAgent (where ordering is by insertion) and
    a positional ``step{i}`` / ``branch{i}`` for the list-based composers.
    """
    from ._agents import (
        HandoffAgent,
        KeywordRouter,
        LoopAgent,
        ParallelAgent,
        RouterAgent,
        SequentialAgent,
    )

    if isinstance(agent, HandoffAgent):
        return [(str(n), a) for n, a in agent._agents.items()]
    if isinstance(agent, RouterAgent):
        return [(str(n), a) for n, _, a in agent._routes]
    if isinstance(agent, KeywordRouter):
        out = [(str(n), a) for n, a, _ in agent._branches]
        out.append(("__default__", agent._default))
        return out
    if isinstance(agent, SequentialAgent):
        return [(f"step{i}", a) for i, a in enumerate(agent._agents)]
    if isinstance(agent, ParallelAgent):
        return [(f"branch{i}", a) for i, a in enumerate(agent._agents)]
    if isinstance(agent, LoopAgent):
        return [("inner", agent._inner)]
    return []


def _edge_kind_for_parent(parent: "BaseAgent") -> str:
    """The EdgeKind to use for edges from ``parent`` to its sub-agents."""
    from ._agents import (
        HandoffAgent,
        KeywordRouter,
        LoopAgent,
        ParallelAgent,
        RouterAgent,
        SequentialAgent,
    )

    if isinstance(parent, SequentialAgent):
        return "next-step"
    if isinstance(parent, LoopAgent):
        return "iterates"
    if isinstance(parent, (RouterAgent, HandoffAgent, KeywordRouter)):
        return "branch"
    if isinstance(parent, ParallelAgent):
        return "branch"
    return "delegates-to"


def _agent_tools(agent: "BaseAgent") -> list[Any]:
    """Yield raw tool callables registered directly on this agent (no recursion)."""
    from ._agents import LlmAgent

    if isinstance(agent, LlmAgent):
        return list(agent._tool_fns)
    return []


def _agent_sub_agent_urls(agent: "BaseAgent") -> list[str]:
    from ._agents import LlmAgent

    if isinstance(agent, LlmAgent):
        return list(agent._sub_agent_urls)
    return []


def _is_dependency_param(annotation: Any) -> bool:
    """True if a parameter annotation is a FastAPI ``Depends(...)`` injection."""
    try:
        from ._inspection import _is_fastapi_dependency

        return _is_fastapi_dependency(annotation)
    except Exception:
        return False


def _tool_input_schema(fn: Any) -> dict[str, Any] | None:
    try:
        from ._inspection import (
            _inspect_tool_fn,
            _make_input_model,
            _schema_for_model,
        )

        plain_params, _ = _inspect_tool_fn(fn)
        input_model = _make_input_model(fn, plain_params)
        return _schema_for_model(input_model)
    except Exception:
        return None


def _tool_has_obo_dep(fn: Any) -> bool:
    """True if any parameter is annotated as a FastAPI dependency (workspace/obo)."""
    try:
        from typing import get_type_hints

        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        return False
    return any(_is_dependency_param(annotation) for annotation in hints.values())


def _resource_url_for(kind: str, identifier: str, workspace_host: str | None) -> str | None:
    """Best-effort Managed MCP URL for a resource — None when not applicable.

    ``workspace_host`` is the Databricks workspace host (e.g. from
    ``DATABRICKS_HOST``); without it the URL can't be built, so return None.
    """
    if not workspace_host:
        return None
    try:
        from ._managed_mcp import managed_mcp_url_for_resource

        return managed_mcp_url_for_resource(kind, identifier, workspace_host=workspace_host)
    except Exception:
        return None


def build_topology(ctx: "AgentContext") -> dict[str, Any]:
    """Walk ``ctx.agent`` and return the ``TopologyResponse``-shaped dict.

    Returns ``{rootId, agentName, nodes, edges}``. Each node has ``{id, type,
    label, description?}``; each edge has ``{id, source, target, kind}``. See
    ``docs/superpowers/specs/2026-05-22-topology-ui.md`` for the schema.
    """
    from ._resources import get_resources

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_node_ids: set[str] = set()
    seen_edge_ids: set[str] = set()

    def _add_node(node: dict[str, Any]) -> None:
        if node["id"] in seen_node_ids:
            return
        seen_node_ids.add(node["id"])
        nodes.append(node)

    def _add_edge(source: str, target: str, kind: str) -> None:
        edge_id = f"{source}->{target}:{kind}"
        if edge_id in seen_edge_ids:
            return
        seen_edge_ids.add(edge_id)
        edges.append({"id": edge_id, "source": source, "target": target, "kind": kind})

    def _walk(agent: "BaseAgent", agent_id: str, label: str) -> None:
        node_type = _agent_class_to_node_type(agent)
        _add_node({
            "id": agent_id,
            "type": node_type,
            "label": label,
            "description": (_instructions_for(agent) or "").strip()[:280] or None,
        })

        # Tool nodes + their resource nodes. Skip callables that are the
        # materialization of a ``sub_agents=`` peer — those already appear as
        # SubAgent / delegates-to nodes below (otherwise Topology double-draws
        # the same remote as uses-tool + delegates-to).
        for fn in _agent_tools(agent):
            if getattr(fn, "__apx_sub_agent_url__", None):
                continue
            tool_name = getattr(fn, "__name__", "tool")
            tool_id = f"tool:{agent_id}:{tool_name}"
            specs = get_resources(fn)
            # Decide tool NodeType — if it has exactly one resource, use that
            # kind's NodeType so the icon/colour matches the resource. Pure
            # functions stay as ``Tool``.
            if len(specs) == 1:
                mapped = _RESOURCE_KIND_TO_TYPE.get(specs[0].kind)
                tool_node_type = mapped[0] if mapped else "Tool"
            else:
                tool_node_type = "Tool"
            _add_node({
                "id": tool_id,
                "type": tool_node_type,
                "label": tool_name,
                "description": (getattr(fn, "__doc__", "") or "").strip()[:280] or None,
            })
            _add_edge(agent_id, tool_id, "uses-tool")

            for spec in specs:
                resource = _resource_node_for(spec.kind, spec.identifier)
                if resource is None:
                    continue
                res_id, res_type, res_label = resource
                _add_node({
                    "id": res_id,
                    "type": res_type,
                    "label": res_label,
                    "description": f"{spec.kind}: {spec.identifier}",
                })
                _add_edge(tool_id, res_id, "delegates-to")

        # Sub-agent URLs declared on this agent (remote A2A)
        for raw_url in _agent_sub_agent_urls(agent):
            ref = _classify_sub_agent(raw_url)
            resolved = raw_url
            if raw_url.startswith("$"):
                env_key = raw_url[1:].strip("{}")
                resolved = os.environ.get(env_key) or raw_url
            sub_id = f"endpoint:{raw_url}"
            desc = f"Remote sub-agent ({raw_url}"
            if resolved != raw_url:
                desc += f" → {resolved}"
            desc += ")"
            _add_node({
                "id": sub_id,
                "type": "SubAgent",
                "label": ref.display_name,
                "description": desc,
            })
            _add_edge(agent_id, sub_id, "delegates-to")

        # Model endpoint reference (calls-model) — root-level only, derived from
        # ctx.config so we don't have to peek at compile state. Done in the
        # outer scope after the walk completes.

        # Composition children
        kind = _edge_kind_for_parent(agent)
        for child_name, child_agent in _iter_child_agents(agent):
            child_id = f"{agent_id}.{child_name}"
            _walk(child_agent, child_id, child_name)
            _add_edge(agent_id, child_id, kind)

    root_id = "agent:root"
    root_label = ctx.config.name if ctx and ctx.config else "agent"
    _walk(ctx.agent, root_id, root_label)

    # Add the model serving endpoint (calls-model edge) for the root
    model = getattr(ctx.config, "model", None)
    if model:
        endpoint_id = f"endpoint:{model}"
        _add_node({
            "id": endpoint_id,
            "type": "ServingEndpoint",
            "label": model,
            "description": f"LLM serving endpoint: {model}",
        })
        _add_edge(root_id, endpoint_id, "calls-model")

    return {
        "rootId": root_id,
        "agentName": ctx.config.name if ctx and ctx.config else "agent",
        "nodes": nodes,
        "edges": edges,
    }


def route_highlight_from_spans(
    topology: dict[str, Any],
    spans: list[dict[str, Any]],
) -> dict[str, Any]:
    """Map serialized MLflow spans onto topology node/edge ids for last-turn highlight.

    Matches TOOL (and similarly named) span ``name`` values against topology
    node labels and id suffixes (``tool:…:run_sql``, ``endpoint:$APX_PEER_…``).
    Then walks edges so the path from the root through each hit lights up.
    """
    nodes = list(topology.get("nodes") or [])
    edges = list(topology.get("edges") or [])
    root_id = topology.get("rootId") or "agent:root"

    # Prefer TOOL spans; also accept CHAIN/AGENT names that match a node label
    # (remote peer materializations often land as non-TOOL spans).
    candidate_names: list[str] = []
    for span in spans:
        name = (span.get("name") or "").strip()
        if not name:
            continue
        st = str(span.get("span_type") or "").upper()
        if st == "TOOL" or name in {n.get("label") for n in nodes}:
            candidate_names.append(name)

    hit_ids: set[str] = set()
    matched_names: list[str] = []
    for name in candidate_names:
        for node in nodes:
            nid = str(node.get("id") or "")
            label = str(node.get("label") or "")
            if label == name or nid.endswith(f":{name}"):
                hit_ids.add(nid)
                if name not in matched_names:
                    matched_names.append(name)

    if not hit_ids:
        return {
            "node_ids": [],
            "edge_ids": [],
            "tool_names": [],
        }

    # Include ancestors: any edge whose target is already highlighted pulls
    # its source in, iterating until closure (root → tool path).
    parent_of: dict[str, list[str]] = {}
    for edge in edges:
        parent_of.setdefault(str(edge["target"]), []).append(str(edge["source"]))

    frontier = set(hit_ids)
    while frontier:
        nxt: set[str] = set()
        for nid in frontier:
            for parent in parent_of.get(nid, []):
                if parent not in hit_ids:
                    hit_ids.add(parent)
                    nxt.add(parent)
        frontier = nxt

    if root_id in {str(n.get("id")) for n in nodes}:
        hit_ids.add(root_id)

    edge_ids = [
        str(e["id"])
        for e in edges
        if str(e["source"]) in hit_ids and str(e["target"]) in hit_ids
    ]
    return {
        "node_ids": sorted(hit_ids),
        "edge_ids": edge_ids,
        "tool_names": matched_names,
    }


def _discover_target_for_agent_id(agent_id: str) -> str:
    """Map a topology agent node id to the Discover wire ``target`` name."""
    if agent_id == "agent:root":
        return "agent"
    if agent_id.startswith("agent:root."):
        return agent_id.rsplit(".", 1)[-1]
    return "agent"


def _leaf_editable(agent: "BaseAgent") -> bool:
    """True when inspector may edit instructions on this agent class."""
    from ._agents import LlmAgent
    from .data_agent import DataAgent

    return isinstance(agent, (LlmAgent, DataAgent))


def inspect_node(ctx: "AgentContext", node_id: str) -> dict[str, Any] | None:
    """Return the ``InspectResponse`` dict for ``node_id``, or ``None`` if unknown."""
    from ._resources import get_resources
    from ._ui_edit import _find_agent_router_path, _is_factory_tool_binding

    if not node_id:
        return None

    source = ""
    path = _find_agent_router_path()
    if path is not None and path.exists():
        try:
            source = path.read_text()
        except Exception:
            source = ""

    # Agent IDs: ``agent:root`` (root) or ``agent:root.<child_name>...`` (nested)
    if node_id == "agent:root" or node_id.startswith("agent:root."):
        target = _resolve_agent(ctx, node_id)
        if target is None:
            return None
        agent_obj, label = target
        instr = _instructions_for(agent_obj)
        tools = _agent_tools(agent_obj)
        sub_count = len(_iter_child_agents(agent_obj)) + len(_agent_sub_agent_urls(agent_obj))
        wire_target = _discover_target_for_agent_id(node_id)
        agent_details: dict[str, Any] = {
            "className": type(agent_obj).__name__,
            "instructions": instr[:4096] if instr else None,
            "model": getattr(ctx.config, "model", None) if node_id == "agent:root" else None,
            "toolCount": len(tools),
            "subAgentCount": sub_count,
        }
        max_iter = getattr(agent_obj, "_max_iterations", None)
        if max_iter is not None:
            agent_details["maxIterations"] = max_iter
        actions: dict[str, Any] = {
            "canEditInstructions": _leaf_editable(agent_obj),
            "canUnwire": False,
            "wireTarget": wire_target,
        }
        return {
            "id": node_id,
            "type": _agent_class_to_node_type(agent_obj),
            "label": label,
            "description": (instr or "").strip()[:280] or None,
            "agent": agent_details,
            "actions": actions,
        }

    # Tool IDs: ``tool:<agent_id>:<tool_name>`` where <agent_id> contains a colon
    # (it always starts with ``agent:``). Strip leading ``tool:`` then split on
    # the LAST colon to recover (agent_id, tool_name).
    if node_id.startswith("tool:"):
        rest = node_id[len("tool:"):]
        if ":" not in rest:
            return None
        owner_agent_id, _, tool_name = rest.rpartition(":")
        target = _resolve_agent(ctx, owner_agent_id)
        if target is None:
            return None
        agent_obj, _ = target
        fn = next(
            (f for f in _agent_tools(agent_obj) if getattr(f, "__name__", "") == tool_name),
            None,
        )
        if fn is None:
            return None
        specs = get_resources(fn)
        if len(specs) == 1:
            mapped = _RESOURCE_KIND_TO_TYPE.get(specs[0].kind)
            tool_node_type = mapped[0] if mapped else "Tool"
        else:
            tool_node_type = "Tool"
        wire_target = _discover_target_for_agent_id(owner_agent_id)
        can_unwire = bool(source) and _is_factory_tool_binding(source, tool_name)
        tool_actions: dict[str, Any] = {
            "canEditInstructions": False,
            "canUnwire": can_unwire,
            "wireTarget": wire_target,
        }
        if can_unwire:
            tool_actions["unwire"] = {
                "kind": "tool",
                "target": wire_target,
                "binding_name": tool_name,
            }
        return {
            "id": node_id,
            "type": tool_node_type,
            "label": tool_name,
            "description": (getattr(fn, "__doc__", "") or "").strip()[:280] or None,
            "tool": {
                "name": tool_name,
                "description": (getattr(fn, "__doc__", "") or "").strip() or None,
                "inputSchema": _tool_input_schema(fn),
                "isSync": not inspect.iscoroutinefunction(fn),
                "hasObOTokenDep": _tool_has_obo_dep(fn),
            },
            "actions": tool_actions,
        }

    # Resource IDs: ``<prefix>:<identifier>``
    prefix, _, identifier = node_id.partition(":")
    if not identifier:
        return None

    # Map prefix back to kind + NodeType
    prefix_to_kind: dict[str, tuple[str, str]] = {
        "uc": ("uc_function", "UCFunction"),
        "genie": ("genie_space", "GenieSpace"),
        "vs": ("vector_search_index", "VectorIndex"),
        "endpoint": ("serving_endpoint", "ServingEndpoint"),
        "warehouse": ("sql_warehouse", "WarehouseSQL"),
    }
    mapping = prefix_to_kind.get(prefix)
    if mapping is None:
        return None
    kind, node_type = mapping

    # Differentiate ServingEndpoint vs SubAgent: SubAgent if any LlmAgent in the
    # tree lists this URL as a sub_agent. (Only matters for endpoint: prefix.)
    if prefix == "endpoint":
        for agent_obj, agent_id in _walk_all_agents(ctx):
            urls = _agent_sub_agent_urls(agent_obj)
            if identifier in urls:
                ref = _classify_sub_agent(identifier)
                wire_target = _discover_target_for_agent_id(agent_id)
                return {
                    "id": node_id,
                    "type": "SubAgent",
                    "label": ref.display_name,
                    "description": f"Remote sub-agent ({identifier})",
                    "subAgent": {
                        "url": identifier,
                        "cardSource": "config",
                        "resolvedName": ref.display_name,
                        "ref": identifier,
                    },
                    "actions": {
                        "canEditInstructions": False,
                        "canUnwire": True,
                        "wireTarget": wire_target,
                        "unwire": {
                            "kind": "agent",
                            "target": wire_target,
                            "ref": identifier,
                        },
                    },
                }

    detail_kind = _RESOURCE_KIND_TO_DETAIL_KIND.get(kind, kind)
    resource_details: dict[str, Any] = {
        "resourceKind": detail_kind,
        "identifier": identifier,
    }
    url = _resource_url_for(kind, identifier, os.environ.get("DATABRICKS_HOST"))
    if url:
        resource_details["url"] = url
    return {
        "id": node_id,
        "type": node_type,
        "label": identifier,
        "description": f"{kind}: {identifier}",
        "resource": resource_details,
        "actions": {"canEditInstructions": False, "canUnwire": False},
    }


def _resolve_agent(
    ctx: "AgentContext", agent_id: str
) -> tuple["BaseAgent", str] | None:
    """Walk ``agent:root.a.b.c`` to find the leaf agent + its display label."""
    if agent_id == "agent:root":
        return ctx.agent, ctx.config.name if ctx and ctx.config else "agent"
    if not agent_id.startswith("agent:root."):
        return None
    parts = agent_id[len("agent:root."):].split(".")
    current: "BaseAgent" = ctx.agent
    label = ctx.config.name if ctx and ctx.config else "agent"
    for part in parts:
        children = dict(_iter_child_agents(current))
        if part not in children:
            return None
        current = children[part]
        label = part
    return current, label


def _walk_all_agents(ctx: "AgentContext"):
    """Yield ``(agent, agent_id)`` for the root and every nested sub-agent."""
    stack: list[tuple["BaseAgent", str]] = [(ctx.agent, "agent:root")]
    while stack:
        agent, agent_id = stack.pop()
        yield agent, agent_id
        for child_name, child_agent in _iter_child_agents(agent):
            stack.append((child_agent, f"{agent_id}.{child_name}"))
