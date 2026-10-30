"""Merge connector fragments into a queryable Graph (adjacency dicts, no networkx)."""

from __future__ import annotations

import concurrent.futures
import difflib
import hashlib
import logging
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from databricks.sdk import WorkspaceClient

from .schema import ARROW_IS_DOWNSTREAM, CONTAINER_TYPES, Edge, Fragment, Node
from . import connectors

logger = logging.getLogger(__name__)


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
        kind = asset_type.lower() if asset_type else None
        hits = [
            n
            for n in self.nodes.values()
            if (kind is None or n.type.lower() == kind)
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


CACHE_TTL_S = 600.0
PARTIAL_TTL_S = 60.0  # graphs with connector warnings are retried sooner


def build_graph(ws: WorkspaceClient, catalogs: list[str] | None = None) -> Graph:
    """Run every connector in parallel, then lineage over the found tables.

    A connector that raises contributes nothing and leaves one warning — a
    partial graph, never a 500.
    """
    fragments: list[Fragment] = []
    warnings: list[str] = []

    def _collect(name: str, run: Callable[[], Fragment]) -> None:
        try:
            frag = run()
        except Exception as exc:  # noqa: BLE001 — per-connector isolation, surfaced as a warning
            logger.warning("workspace graph connector %s failed: %s", name, exc)
            warnings.append(f"{name}: {connectors._reason(exc)}")
            return
        warnings.extend(f"{name}: {w}" for w in frag.warnings)
        fragments.append(Fragment(frag.nodes, frag.edges))

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(connectors.CONNECTORS)
    ) as ex:
        futs = {
            name: ex.submit(fn, ws, catalogs)
            for name, fn in connectors.CONNECTORS.items()
        }
        for name, fut in futs.items():
            _collect(name, fut.result)
    tables = [n.fqn for f in fragments for n in f.nodes if n.type in ("Table", "View")]
    _collect("lineage", lambda: connectors.lineage(ws, tables))
    return Graph(fragments, warnings=sorted(warnings))


@dataclass
class _Cached:
    graph: Graph
    at: float
    ttl: float


# ponytail: in-process cache — each replica builds its own; move to a shared
# cache if Discover runs multi-replica. Misses for the same key share one build.
_CACHE: dict[str, _Cached] = {}
_INFLIGHT: dict[str, concurrent.futures.Future[Graph]] = {}
_LOCK = threading.Lock()


def _identity(ws: WorkspaceClient) -> str:
    """Hash of the caller's credential: never cross-user, token never stored raw."""
    return hashlib.sha256(
        ws.config.authenticate()["Authorization"].encode()
    ).hexdigest()


def get_graph(
    ws: WorkspaceClient, catalogs: list[str] | None = None, *, refresh: bool = False
) -> Graph:
    now = time.monotonic()
    key = f"{_identity(ws)}|{','.join(sorted(catalogs or []))}"
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and not refresh and now - hit.at < hit.ttl:
            return hit.graph
        for stale in [k for k, v in _CACHE.items() if now - v.at >= v.ttl]:
            _CACHE.pop(stale, None)
        future = _INFLIGHT.get(key)
        is_builder = future is None
        if future is None:
            future = _INFLIGHT[key] = concurrent.futures.Future()
    if not is_builder:
        return future.result()
    try:
        graph = build_graph(ws, catalogs)
    except BaseException as exc:
        with _LOCK:
            _INFLIGHT.pop(key, None)
        future.set_exception(exc)
        raise
    with _LOCK:
        _CACHE[key] = _Cached(
            graph, now, PARTIAL_TTL_S if graph.warnings else CACHE_TTL_S
        )
        _INFLIGHT.pop(key, None)
    future.set_result(graph)
    return graph
