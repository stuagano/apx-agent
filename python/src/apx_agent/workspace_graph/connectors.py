"""Workspace graph connectors — one per source, each ``(ws, catalogs) -> Fragment``.

Reuses the existing caller-scoped listers; ports Lattice's cross-asset edge
logic (server/connectors/*.py, server/graph/builder.py). Soft problems (caps hit,
one schema denied) go to ``Fragment.warnings``; a connector that cannot run at
all raises, and the builder turns that into a warning.
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from typing import Any

from databricks.sdk import WorkspaceClient

from .._discover_data import _MAX_TABLES, list_discover_tables
from .schema import Edge, Fragment, Node, node_id

MAX_CATALOGS = 10
MAX_SCHEMAS = 50
_SKIP_CATALOGS = frozenset({"system"})


def _merge(into: Fragment, part: Fragment) -> None:
    into.nodes.extend(part.nodes)
    into.edges.extend(part.edges)
    into.warnings.extend(part.warnings)


def _listed(frag: Fragment, label: str, fn: Callable[[], Any]) -> list[Any]:
    """Run one SDK list; a failure becomes a warning and an empty result."""
    try:
        return list(fn())
    except Exception as exc:  # noqa: BLE001 — per-listing isolation, surfaced as a warning
        frag.warnings.append(f"{label}: {exc}")
        return []


def _default_catalogs(ws: WorkspaceClient, frag: Fragment) -> list[str]:
    names = [
        c.name
        for c in ws.catalogs.list()
        if c.name and not c.name.startswith("__") and c.name not in _SKIP_CATALOGS
    ]
    if len(names) > MAX_CATALOGS:
        frag.warnings.append(
            f"{len(names)} catalogs visible; loaded first {MAX_CATALOGS} — pass catalogs= to choose"
        )
    return names[:MAX_CATALOGS]


def _schema_fragment(ws: WorkspaceClient, catalog: str, schema: str) -> Fragment:
    fqn = f"{catalog}.{schema}"
    sid = node_id("schema", fqn)
    frag = Fragment(
        nodes=[Node(sid, "Schema", schema, fqn)],
        edges=[Edge(node_id("catalog", catalog), sid, "contains")],
    )

    def _child(
        prefix: str, type_: str, full_name: str, name: str, props: dict[str, Any]
    ) -> str:
        cid = node_id(prefix, full_name)
        frag.nodes.append(Node(cid, type_, name, full_name, props))
        frag.edges.append(Edge(sid, cid, "contains"))
        return cid

    tables = _listed(
        frag, f"{fqn} tables", lambda: list_discover_tables(ws, catalog, schema)
    )
    if len(tables) == _MAX_TABLES:
        frag.warnings.append(f"{fqn}: showing first {_MAX_TABLES} tables")
    for t in tables:
        kind = "View" if "VIEW" in t["table_type"].upper() else "Table"
        props = {k: t[k] for k in ("table_type", "comment", "columns", "row_count")}
        tid = _child("table", kind, t["full_name"], t["name"], props)
        frag.edges.extend(
            Edge(tid, node_id("table", dep), "derivedFrom")
            for dep in t["view_dependencies"]
        )

    for v in _listed(
        frag,
        f"{fqn} volumes",
        lambda: ws.volumes.list(catalog_name=catalog, schema_name=schema),
    ):
        _child(
            "volume",
            "Volume",
            v.full_name,
            v.name,
            {
                "volume_type": v.volume_type.value if v.volume_type else None,
                "comment": v.comment,
            },
        )
    for f in _listed(
        frag,
        f"{fqn} functions",
        lambda: ws.functions.list(catalog_name=catalog, schema_name=schema),
    ):
        _child("function", "Function", f.full_name, f.name, {"comment": f.comment})
    for m in _listed(
        frag,
        f"{fqn} models",
        lambda: ws.registered_models.list(catalog_name=catalog, schema_name=schema),
    ):
        _child("model", "Model", m.full_name, m.name, {"comment": m.comment})
    return frag


def unity_catalog(ws: WorkspaceClient, catalogs: list[str] | None) -> Fragment:
    """Catalog ⊃ schema ⊃ table/view/volume/function/model, plus view→table derivedFrom."""
    frag = Fragment()
    for catalog in catalogs if catalogs else _default_catalogs(ws, frag):
        try:
            schemas = [
                s.name
                for s in ws.schemas.list(catalog_name=catalog)
                if s.name and s.name != "information_schema"
            ]
        except Exception as exc:  # noqa: BLE001 — unknown/denied catalog: warn, skip it
            frag.warnings.append(f"{catalog}: {exc}")
            continue
        frag.nodes.append(
            Node(node_id("catalog", catalog), "Catalog", catalog, catalog)
        )
        if len(schemas) > MAX_SCHEMAS:
            frag.warnings.append(
                f"{catalog}: {len(schemas)} schemas; loaded first {MAX_SCHEMAS}"
            )
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for part in ex.map(
                lambda s, c=catalog: _schema_fragment(ws, c, s), schemas[:MAX_SCHEMAS]
            ):
                _merge(frag, part)
    return frag
