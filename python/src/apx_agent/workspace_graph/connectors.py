"""Workspace graph connectors — one per source, each ``(ws, catalogs) -> Fragment``.

Reuses the existing caller-scoped listers; ports Lattice's cross-asset edge
logic (server/connectors/*.py, server/graph/builder.py). Soft problems (caps hit,
one schema denied) go to ``Fragment.warnings``; a connector that cannot run at
all raises, and the builder turns that into a warning.
"""

from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Callable
from typing import Any, cast

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import AppResource

from .._apps_discovery import discover_app_agents
from .._discover_data import _MAX_TABLES, list_discover_tables
from .._workspace_apis import (
    list_genie_spaces,
    list_serving_endpoints,
    list_vector_search_indexes,
)
from .schema import Edge, Fragment, Node, node_id

MAX_CATALOGS = 10
MAX_SCHEMAS = 50
MAX_LISTED = 50  # per-kind cap passed to the serving / genie / vector-search listers
MAX_DETAIL_WORKERS = 8
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


MAX_LINEAGE_TABLES = 200
_LINEAGE_PATH = "/api/2.1/unity-catalog/lineage-tracking/table-lineage"
_SECURABLE_PREFIX = {"TABLE": "table", "VOLUME": "volume", "FUNCTION": "function"}


def serving(ws: WorkspaceClient, catalogs: list[str] | None) -> Fragment:
    """Serving endpoints; ``serves`` edges to the UC models they host."""
    frag = Fragment()
    listed = list_serving_endpoints(ws, limit=MAX_LISTED)
    if len(listed) == MAX_LISTED:
        frag.warnings.append(f"showing first {MAX_LISTED} serving endpoints")
    for api in listed:
        nid = node_id("serving", api.name)
        extra = api.extra or {}
        frag.nodes.append(
            Node(
                nid,
                "ServingEndpoint",
                api.name,
                api.name,
                {"state": api.state, "task": extra["task"], "url": api.url},
            )
        )
        frag.edges.extend(
            Edge(nid, node_id("model", m), "serves") for m in extra["served_entities"]
        )
    return frag


def _genie_tables(ws: WorkspaceClient, space_id: str) -> list[str]:
    space = ws.genie.get_space(space_id, include_serialized_space=True)
    if not space.serialized_space:
        return []
    data = json.loads(space.serialized_space)
    return [t["identifier"] for t in data.get("data_sources", {}).get("tables", [])]


def genie(ws: WorkspaceClient, catalogs: list[str] | None) -> Fragment:
    """Genie spaces; ``uses`` edges to the tables each space is configured with."""
    frag = Fragment()
    listed = list_genie_spaces(ws, limit=MAX_LISTED)
    if len(listed) == MAX_LISTED:
        frag.warnings.append(f"showing first {MAX_LISTED} genie spaces")
    space_ids = [(api.extra or {})["space_id"] for api in listed]

    def _detail(space_id: str) -> list[str] | Exception:
        try:
            return _genie_tables(ws, space_id)
        except Exception as exc:  # noqa: BLE001 — one space's detail denied: warn, keep the node
            return exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_DETAIL_WORKERS) as ex:
        details = list(ex.map(_detail, space_ids))
    for api, space_id, tables in zip(listed, space_ids, details):
        nid = node_id("genie", space_id)
        frag.nodes.append(
            Node(
                nid,
                "GenieSpace",
                api.name,
                space_id,
                {"description": api.description, "mcp_url": api.mcp_url},
            )
        )
        if isinstance(tables, Exception):
            frag.warnings.append(f"{api.name} tables: {tables}")
            continue
        frag.edges.extend(Edge(nid, node_id("table", t), "uses") for t in tables)
    return frag


def vector_search(ws: WorkspaceClient, catalogs: list[str] | None) -> Fragment:
    """Vector Search indexes; ``indexesFrom`` edges to their source tables."""
    frag = Fragment()
    listed = list_vector_search_indexes(ws, limit=MAX_LISTED)
    if len(listed) == MAX_LISTED:
        frag.warnings.append(f"showing first {MAX_LISTED} vector search indexes")
    for api in listed:
        extra = api.extra or {}
        nid = node_id("vsindex", api.name)
        frag.nodes.append(
            Node(
                nid,
                "VectorSearchIndex",
                api.name.split(".")[-1],
                api.name,
                {
                    "endpoint": extra["endpoint"],
                    "state": api.state,
                    "mcp_url": api.mcp_url,
                },
            )
        )
        if extra["source_table"]:
            frag.edges.append(
                Edge(nid, node_id("table", extra["source_table"]), "indexesFrom")
            )
    return frag


def _app_resource_target(r: AppResource) -> str | None:
    if (
        r.uc_securable
        and r.uc_securable.securable_full_name
        and r.uc_securable.securable_type
    ):
        prefix = _SECURABLE_PREFIX.get(r.uc_securable.securable_type.value)
        return node_id(prefix, r.uc_securable.securable_full_name) if prefix else None
    if r.genie_space and r.genie_space.space_id:
        return node_id("genie", r.genie_space.space_id)
    if r.serving_endpoint and r.serving_endpoint.name:
        return node_id("serving", r.serving_endpoint.name)
    return None


def apps(ws: WorkspaceClient, catalogs: list[str] | None) -> Fragment:
    """apx agents running as Apps; ``uses`` edges to their declared resources.

    ponytail: discover_app_agents never raises (the CLI relies on that), so an
    apps.list denial reads as zero apps rather than a warning.
    """
    frag = Fragment()
    agents = list(discover_app_agents(ws))

    def _detail(app_name: str) -> list[AppResource] | Exception:
        try:
            return ws.apps.get(app_name).resources or []
        except Exception as exc:  # noqa: BLE001 — one app's detail denied: warn, keep the node
            return exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_DETAIL_WORKERS) as ex:
        details = list(ex.map(_detail, [a.app_name for a in agents]))
    for agent, resources in zip(agents, details):
        nid = node_id("app", agent.app_name)
        frag.nodes.append(
            Node(
                nid,
                "App",
                agent.name,
                agent.app_name,
                {
                    "url": agent.url,
                    "description": agent.description,
                    "tools": list(agent.tools),
                },
            )
        )
        if isinstance(resources, Exception):
            frag.warnings.append(f"{agent.app_name} resources: {resources}")
            continue
        for r in resources:
            target = _app_resource_target(r)
            if target:
                frag.edges.append(Edge(nid, target, "uses"))
    return frag


def lineage(ws: WorkspaceClient, table_fqns: list[str]) -> Fragment:
    """Table→table ``feedsInto`` from the UC lineage API (no system tables in v1)."""
    frag = Fragment()
    targets = table_fqns[:MAX_LINEAGE_TABLES]
    if len(table_fqns) > MAX_LINEAGE_TABLES:
        frag.warnings.append(
            f"checked first {MAX_LINEAGE_TABLES} of {len(table_fqns)} tables"
        )

    def _one(fqn: str) -> list[Edge]:
        resp = cast(
            dict[str, Any],
            ws.api_client.do("GET", _LINEAGE_PATH, query={"table_name": fqn}),
        )
        return [
            Edge(
                node_id("table", fqn),
                node_id(
                    "table",
                    f"{d['tableInfo']['catalog_name']}.{d['tableInfo']['schema_name']}.{d['tableInfo']['name']}",
                ),
                "feedsInto",
            )
            for d in resp.get("downstreams", [])
            if "tableInfo" in d
        ]

    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for fqn, fut in [(f, ex.submit(_one, f)) for f in targets]:
            try:
                frag.edges.extend(fut.result())
            except Exception as exc:  # noqa: BLE001 — per-table isolation, summarized below
                failures.append(f"{fqn}: {exc}")
    if failures:
        frag.warnings.append(
            f"{len(failures)} of {len(targets)} tables failed (first: {failures[0]})"
        )
    return frag


CONNECTORS: dict[str, Callable[[WorkspaceClient, list[str] | None], Fragment]] = {
    "unity_catalog": unity_catalog,
    "serving": serving,
    "genie": genie,
    "vector_search": vector_search,
    "apps": apps,
}
