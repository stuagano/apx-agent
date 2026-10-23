"""Workspace API discovery for the Dev UI Discover page.

Lists Model Serving endpoints, Genie spaces, and Vector Search indexes the
caller can see, and attaches Managed MCP URLs where Databricks hosts one.
List failures raise; the workspace graph turns them into warnings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ._managed_mcp import _build_url, _normalise_host

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkspaceApiInfo:
    """One discoverable API / MCP surface in the workspace."""

    kind: str  # serving_endpoint | genie_space | vector_search_index
    name: str
    state: str | None = None
    description: str | None = None
    url: str | None = None
    """Invoke or Apps URL when applicable."""
    mcp_url: str | None = None
    """Databricks Managed MCP URL when the kind is MCP-backed."""
    extra: dict[str, Any] | None = None


def _host(ws: Any) -> str:
    return _normalise_host(ws.config.host) if ws.config.host else ""


def list_serving_endpoints(ws: Any, *, limit: int = 50) -> list[WorkspaceApiInfo]:
    host = _host(ws)
    out: list[WorkspaceApiInfo] = []
    for ep in list(ws.serving_endpoints.list())[:limit]:
        name = ep.name
        if not name:
            continue
        ready = ep.state.ready if ep.state else None
        state = ready.value if ready is not None else None
        task = ep.task
        served = (
            [e.entity_name for e in (ep.config.served_entities or []) if e.entity_name]
            if ep.config
            else []
        )
        out.append(
            WorkspaceApiInfo(
                kind="serving_endpoint",
                name=name,
                state=state,
                description=str(task) if task else "Model Serving endpoint",
                url=f"{host}/serving-endpoints/{name}/invocations" if host else None,
                mcp_url=None,  # serving endpoints are not Managed MCP today
                extra={"task": str(task) if task else None, "served_entities": served},
            )
        )
    return out


def list_genie_spaces(ws: Any, *, limit: int = 50) -> list[WorkspaceApiInfo]:
    host = _host(ws)
    spaces: list[Any] = []
    page_token = None
    while len(spaces) < limit:
        kwargs: dict[str, Any] = {"page_size": min(200, limit - len(spaces))}
        if page_token:
            kwargs["page_token"] = page_token
        resp = ws.genie.list_spaces(**kwargs)
        batch = list(resp.spaces or [])
        spaces.extend(batch)
        page_token = resp.next_page_token
        if not page_token or not batch:
            break
    out: list[WorkspaceApiInfo] = []
    for space in spaces[:limit]:
        if not space.space_id:
            continue
        space_id = str(space.space_id)
        built = _build_url("genie_space", space_id, host) if host else None
        out.append(
            WorkspaceApiInfo(
                kind="genie_space",
                name=str(space.title) if space.title else space_id,
                description=space.description or f"Genie space {space_id}",
                mcp_url=built.url if built else None,
                extra={"space_id": space_id},
            )
        )
    return out


def list_vector_search_indexes(ws: Any, *, limit: int = 50) -> list[WorkspaceApiInfo]:
    from ._ui_probe import _discover_vs_indexes

    host = _host(ws)
    rows = _discover_vs_indexes(ws)
    if rows and all(r.get("error") and not r.get("index") for r in rows):
        raise RuntimeError(rows[0]["error"])
    out: list[WorkspaceApiInfo] = []
    for row in rows:
        idx = row.get("index")
        if not idx:
            continue
        if len(out) >= limit:
            break
        built = _build_url("vector_search_index", idx, host) if host else None
        out.append(
            WorkspaceApiInfo(
                kind="vector_search_index",
                name=idx,
                state="ready" if row.get("ready") else row.get("endpoint_state"),
                description=f"VS endpoint {row.get('endpoint')}",
                mcp_url=built.url if built else None,
                extra={
                    "endpoint": row.get("endpoint"),
                    "source_table": row.get("source_table"),
                    "columns": row.get("columns") or [],
                },
            )
        )
    return out
