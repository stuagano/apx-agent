"""Workspace graph types — typed assets and the edges between them.

Ported from Lattice's ontology (server/graph/schema.py, mcp_server.py), cut to
the v1 Data + AI core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NODE_TYPES = (
    "Catalog",
    "Schema",
    "Table",
    "View",
    "Volume",
    "Function",
    "Model",
    "GenieSpace",
    "VectorSearchIndex",
    "ServingEndpoint",
    "App",
)
EDGE_TYPES = ("contains", "feedsInto", "derivedFrom", "indexesFrom", "serves", "uses")
CONTAINER_TYPES = frozenset({"Catalog", "Schema"})

# Whether the dependent asset sits at the arrow's head (src -> dst). derivedFrom,
# indexesFrom, serves and uses point AT what they depend on, so their dependents
# are behind the arrow (Lattice mcp_server._ARROW_IS_DATA_FLOW).
ARROW_IS_DOWNSTREAM = {
    "contains": True,
    "feedsInto": True,
    "derivedFrom": False,
    "indexesFrom": False,
    "serves": False,
    "uses": False,
}


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    name: str
    fqn: str
    props: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    type: str


@dataclass
class Fragment:
    """One connector's contribution: nodes, edges, and soft (non-fatal) warnings."""

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def node_id(prefix: str, fqn: str) -> str:
    """Stable id; UC names are case-insensitive, so ids are lower-cased."""
    return f"{prefix}:{fqn.lower()}"


# Declared ResourceSpec kind -> graph node prefix. Warehouses, connections, Lakebase
# instances and jobs are not graph node types.
_RESOURCE_PREFIX = {
    "uc_table": "table", "uc_function": "function", "genie_space": "genie",
    "serving_endpoint": "serving", "vector_search_index": "vsindex", "app": "app",
}


def resource_node_id(kind: str, identifier: str) -> str | None:
    """The graph node a declared agent resource is, or None for kinds the graph doesn't hold."""
    prefix = _RESOURCE_PREFIX.get(kind)
    return node_id(prefix, identifier) if prefix else None
