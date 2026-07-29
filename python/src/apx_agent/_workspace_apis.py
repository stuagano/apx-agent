"""Workspace API discovery for the Dev UI Discover page.

Lists Model Serving endpoints, Genie spaces, and Vector Search indexes the
caller can see, and attaches Managed MCP URLs where Databricks hosts one.
Best-effort: each source fails independently and never raises to the caller.
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


def discover_workspace_apis(ws: Any, *, limit_per_kind: int = 50) -> list[WorkspaceApiInfo]:
    """Discover serving endpoints, Genie spaces, and Vector Search indexes.

    ``limit_per_kind`` caps each family so a busy workspace doesn't flood the UI.
    """
    host = ""
    try:
        host = _normalise_host(getattr(getattr(ws, "config", None), "host", "") or "")
    except Exception:
        host = ""

    found: list[WorkspaceApiInfo] = []
    found.extend(_list_serving(ws, host, limit_per_kind))
    found.extend(_list_genie(ws, host, limit_per_kind))
    found.extend(_list_vector_search(ws, host, limit_per_kind))
    found.sort(key=lambda a: (a.kind, a.name.lower()))
    return found


def _list_serving(ws: Any, host: str, limit: int) -> list[WorkspaceApiInfo]:
    out: list[WorkspaceApiInfo] = []
    try:
        endpoints = list(ws.serving_endpoints.list())
    except Exception as e:
        logger.info("serving_endpoints.list failed: %s", e)
        return out
    for ep in endpoints[:limit]:
        name = getattr(ep, "name", None) or ""
        if not name:
            continue
        state = None
        try:
            st = getattr(getattr(ep, "state", None), "ready", None)
            state = getattr(st, "value", None) or (str(st) if st is not None else None)
        except Exception:
            state = None
        task = getattr(ep, "task", None)
        url = f"{host}/serving-endpoints/{name}/invocations" if host else None
        out.append(
            WorkspaceApiInfo(
                kind="serving_endpoint",
                name=name,
                state=state,
                description=str(task) if task else "Model Serving endpoint",
                url=url,
                mcp_url=None,  # serving endpoints are not Managed MCP today
                extra={"task": str(task) if task else None},
            )
        )
    return out


def _list_genie(ws: Any, host: str, limit: int) -> list[WorkspaceApiInfo]:
    out: list[WorkspaceApiInfo] = []
    spaces: list[Any] = []
    try:
        page_token = None
        while len(spaces) < limit:
            kwargs: dict[str, Any] = {"page_size": min(200, limit - len(spaces))}
            if page_token:
                kwargs["page_token"] = page_token
            resp = ws.genie.list_spaces(**kwargs)
            batch = list(getattr(resp, "spaces", None) or [])
            spaces.extend(batch)
            page_token = getattr(resp, "next_page_token", None)
            if not page_token or not batch:
                break
    except Exception as e:
        logger.info("genie.list_spaces failed: %s", e)
        return out

    for space in spaces[:limit]:
        space_id = getattr(space, "space_id", None) or ""
        title = getattr(space, "title", None) or str(space_id)
        if not space_id:
            continue
        built = _build_url("genie_space", str(space_id), host) if host else None
        desc = getattr(space, "description", None) or f"Genie space {space_id}"
        out.append(
            WorkspaceApiInfo(
                kind="genie_space",
                name=str(title),
                state=None,
                description=desc,
                url=None,
                mcp_url=built.url if built else None,
                extra={"space_id": str(space_id)},
            )
        )
    return out


def _list_vector_search(ws: Any, host: str, limit: int) -> list[WorkspaceApiInfo]:
    out: list[WorkspaceApiInfo] = []
    try:
        from ._ui_probe import _discover_vs_indexes

        rows = _discover_vs_indexes(ws)
    except Exception as e:
        logger.info("vector search discovery failed: %s", e)
        return out

    count = 0
    for row in rows:
        if row.get("error") and not row.get("index"):
            continue
        idx = row.get("index") or ""
        if not idx:
            continue
        if count >= limit:
            break
        count += 1
        built = _build_url("vector_search_index", idx, host) if host else None
        out.append(
            WorkspaceApiInfo(
                kind="vector_search_index",
                name=idx,
                state="ready" if row.get("ready") else (row.get("endpoint_state") or "unknown"),
                description=(
                    f"VS endpoint {row.get('endpoint')}"
                    + (f" · source {row['source_table']}" if row.get("source_table") else "")
                ),
                url=None,
                mcp_url=built.url if built else None,
                extra={
                    "endpoint": row.get("endpoint"),
                    "source_table": row.get("source_table"),
                    "columns": row.get("columns") or [],
                },
            )
        )
    return out
