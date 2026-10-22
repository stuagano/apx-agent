"""Merge connector fragments into a queryable Graph (adjacency dicts, no networkx)."""

from __future__ import annotations

import difflib
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from .schema import ARROW_IS_DOWNSTREAM, CONTAINER_TYPES, Edge, Fragment, Node


def _brief(n: Node) -> dict[str, str]:
    return {"id": n.id, "type": n.type, "name": n.name, "fqn": n.fqn}


class Graph:
    def __init__(
        self, fragments: list[Fragment], warnings: list[str] | None = None
    ) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.warnings: list[str] = list(warnings or [])
        self.built_at = datetime.now(timezone.utc).isoformat()
        self._out: dict[str, list[Edge]] = defaultdict(list)
        self._in: dict[str, list[Edge]] = defaultdict(list)
        for frag in fragments:
            for n in frag.nodes:
                self.nodes.setdefault(n.id, n)
        seen: set[Edge] = set()
        for frag in fragments:
            self.warnings.extend(frag.warnings)
            for e in frag.edges:
                # Edges into assets outside the loaded scope are dropped, not stubbed.
                if e in seen or e.src not in self.nodes or e.dst not in self.nodes:
                    continue
                seen.add(e)
                self.edges.append(e)
                self._out[e.src].append(e)
                self._in[e.dst].append(e)
        self._by_fqn = {n.fqn.lower(): n for n in self.nodes.values()}

    def resolve(self, identifier: str) -> Node | None:
        key = identifier.strip()
        return (
            self.nodes.get(key)
            or self.nodes.get(key.lower())
            or self._by_fqn.get(key.lower())
        )

    def search(self, query: str, asset_type: str | None = None) -> list[Node]:
        q = query.strip().lower()
        hits = [
            n
            for n in self.nodes.values()
            if (asset_type is None or n.type == asset_type)
            and (not q or q in n.name.lower() or q in n.fqn.lower() or q in n.id)
        ]
        return sorted(hits, key=lambda n: (n.name.lower(), n.id))

    def get(self, identifier: str) -> dict[str, Any] | None:
        node = self.resolve(identifier)
        if node is None:
            return None
        return {
            "node": {**_brief(node), "props": node.props},
            "outgoing": [
                {"type": e.type, "node": _brief(self.nodes[e.dst])}
                for e in self._out[node.id]
            ],
            "incoming": [
                {"type": e.type, "node": _brief(self.nodes[e.src])}
                for e in self._in[node.id]
            ],
        }

    def _step(
        self, node_id: str, *, downstream: bool, data_flow_only: bool
    ) -> Iterator[str]:
        for e in self._out[node_id]:
            if (
                not (data_flow_only and e.type == "contains")
                and ARROW_IS_DOWNSTREAM[e.type] == downstream
            ):
                yield e.dst
        for e in self._in[node_id]:
            if (
                not (data_flow_only and e.type == "contains")
                and ARROW_IS_DOWNSTREAM[e.type] != downstream
            ):
                yield e.src

    def _bfs(
        self, start: str, *, downstream: bool, data_flow_only: bool, depth: int | None
    ) -> list[Node]:
        seen, frontier, out, level = {start}, [start], [], 0
        while frontier and (depth is None or level < depth):
            nxt: list[str] = []
            for nid in frontier:
                for m in self._step(
                    nid, downstream=downstream, data_flow_only=data_flow_only
                ):
                    if m not in seen:
                        seen.add(m)
                        nxt.append(m)
                        out.append(self.nodes[m])
            frontier, level = nxt, level + 1
        return out

    def impact(self, identifier: str) -> list[Node]:
        """Everything downstream: contained assets plus data-flow dependents."""
        node = self.resolve(identifier)
        if node is None:
            return []
        return self._bfs(node.id, downstream=True, data_flow_only=False, depth=None)

    def lineage(self, identifier: str, depth: int = 3) -> dict[str, list[Node]]:
        node = self.resolve(identifier)
        if node is None:
            return {"upstream": [], "downstream": []}
        return {
            "upstream": self._bfs(
                node.id, downstream=False, data_flow_only=True, depth=depth
            ),
            "downstream": self._bfs(
                node.id, downstream=True, data_flow_only=True, depth=depth
            ),
        }

    def orphans(self) -> list[Node]:
        """Non-container assets that nothing feeds, reads, indexes, serves, or uses."""
        linked = {e.src for e in self.edges if e.type != "contains"} | {
            e.dst for e in self.edges if e.type != "contains"
        }
        return sorted(
            (
                n
                for n in self.nodes.values()
                if n.type not in CONTAINER_TYPES and n.id not in linked
            ),
            key=lambda n: n.id,
        )

    def suggest(self, identifier: str) -> list[str]:
        names = {n.fqn.lower(): n.id for n in self.nodes.values()}
        names.update({n.name.lower(): n.id for n in self.nodes.values()})
        close = difflib.get_close_matches(
            identifier.lower(), list(names), n=5, cutoff=0.6
        )
        return list(dict.fromkeys(names[c] for c in close))

    def summary(self) -> dict[str, Any]:
        return {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "node_types": dict(Counter(n.type for n in self.nodes.values())),
            "edge_types": dict(Counter(e.type for e in self.edges)),
            "warnings": self.warnings,
            "built_at": self.built_at,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "nodes": [{**_brief(n), "props": n.props} for n in self.nodes.values()],
            "edges": [{"src": e.src, "dst": e.dst, "type": e.type} for e in self.edges],
            "warnings": self.warnings,
            "built_at": self.built_at,
        }
