"""catalog_tool, lineage_tool, schema_tool — Unity Catalog tool factories.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``UserClientDependency`` is resolved eagerly and ``get_type_hints()`` in
_inspection.py sees the real Annotated[...] type, not a string.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def catalog_tool(
    catalog: str,
    schema: str,
    *,
    name: str = "list_tables",
    description: str | None = None,
) -> Any:
    """Return a tool that lists tables in a Unity Catalog schema.

    Usage::

        from apx_agent import Agent, catalog_tool

        agent = Agent(tools=[catalog_tool("main", "sales")])

    Args:
        catalog: Unity Catalog catalog name.
        schema:  Schema name within the catalog.
        name:    Tool name shown to the LLM. Defaults to ``"list_tables"``.
        description: Tool description shown to the LLM.
    """
    from ._defaults import UserClientDependency

    _desc = description or f"List all tables in {catalog}.{schema} with their names and descriptions."

    async def _list_tables(ws: UserClientDependency) -> list[dict[str, Any]]:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        tables = []
        try:
            for t in ws.tables.list(catalog_name=catalog, schema_name=schema):
                tables.append({
                    "name": t.name,
                    "full_name": t.full_name,
                    "table_type": str(getattr(t, "table_type", "") or ""),
                    "comment": getattr(t, "comment", None) or "",
                })
        except Exception as e:
            logger.warning("Failed to list tables in %s.%s: %s", catalog, schema, e)
        return tables

    _list_tables.__name__ = name
    _list_tables.__qualname__ = name
    _list_tables.__doc__ = _desc
    return _list_tables


def lineage_tool(
    *,
    name: str = "get_table_lineage",
    description: str | None = None,
) -> Any:
    """Return a tool that fetches upstream/downstream lineage for a table.

    Usage::

        from apx_agent import Agent, lineage_tool

        agent = Agent(tools=[lineage_tool()])

    The returned tool accepts ``table_name: str`` — the fully qualified name
    (``catalog.schema.table_name``).

    Args:
        name:        Tool name shown to the LLM. Defaults to ``"get_table_lineage"``.
        description: Tool description shown to the LLM.
    """
    from ._defaults import UserClientDependency

    _desc = description or (
        "Get the upstream sources and downstream consumers for a Unity Catalog table. "
        "Pass the full table name as catalog.schema.table_name."
    )

    async def _get_lineage(table_name: str, ws: UserClientDependency) -> dict[str, Any]:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        result: dict[str, Any] = ws.api_client.do(
            "GET",
            "/api/2.1/unity-catalog/lineage-tracking/table-lineage",
            query={"table_name": table_name},
        )
        upstreams = [
            {
                "full_name": u.get("tableInfo", {}).get("name", ""),
                "table_type": u.get("tableInfo", {}).get("table_type", ""),
            }
            for u in result.get("upstreams", [])
            if u.get("tableInfo")
        ]
        downstreams = [
            {
                "full_name": d.get("tableInfo", {}).get("name", ""),
                "table_type": d.get("tableInfo", {}).get("table_type", ""),
            }
            for d in result.get("downstreams", [])
            if d.get("tableInfo")
        ]
        return {"table": table_name, "upstreams": upstreams, "downstreams": downstreams}

    _get_lineage.__name__ = name
    _get_lineage.__qualname__ = name
    _get_lineage.__doc__ = _desc
    return _get_lineage


def schema_tool(
    *,
    name: str = "describe_table",
    description: str | None = None,
) -> Any:
    """Return a tool that describes the columns of a Unity Catalog table.

    Usage::

        from apx_agent import Agent, schema_tool

        agent = Agent(tools=[schema_tool()])

    The returned tool accepts ``table_name: str`` — the fully qualified name
    (``catalog.schema.table_name``).

    Args:
        name:        Tool name shown to the LLM. Defaults to ``"describe_table"``.
        description: Tool description shown to the LLM.
    """
    from ._defaults import UserClientDependency

    _desc = description or (
        "Describe the columns of a Unity Catalog table — names, types, and descriptions. "
        "Pass the full table name as catalog.schema.table_name."
    )

    async def _describe_table(table_name: str, ws: UserClientDependency) -> list[dict[str, Any]]:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        t = ws.tables.get(full_name=table_name)
        return [
            {
                "name": col.name,
                "type": str(col.type_name or ""),
                "type_text": col.type_text or "",
                "comment": col.comment or "",
                "nullable": col.nullable if col.nullable is not None else True,
                "position": col.position if col.position is not None else 0,
            }
            for col in (t.columns or [])
        ]

    _describe_table.__name__ = name
    _describe_table.__qualname__ = name
    _describe_table.__doc__ = _desc
    return _describe_table
