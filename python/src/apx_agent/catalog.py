"""catalog_tool, lineage_tool, schema_tool, uc_function_tool — Unity Catalog tool factories.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``UserClientDependency`` is resolved eagerly and ``get_type_hints()`` in
_inspection.py sees the real Annotated[...] type, not a string.
"""

import logging
from typing import Any

from ._sql import get_warehouse_id, run_sql

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL literal helper (used by uc_function_tool)
# ---------------------------------------------------------------------------

def _to_sql_literal(value: Any, type_name: str) -> str:
    """Convert a Python value to a safe SQL literal for a UC function call."""
    if value is None:
        return "NULL"
    type_upper = type_name.upper()
    if type_upper in ("BOOLEAN",):
        return "TRUE" if value else "FALSE"
    if type_upper in ("STRING", "CHAR", "VARCHAR", "TEXT"):
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"
    # Numeric types — validate then pass raw
    try:
        float(str(value))
        return str(value)
    except (ValueError, TypeError):
        # Unknown type — fall back to quoted string
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"


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


def uc_function_tool(
    function_name: str,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Any:
    """Return a tool that executes a Unity Catalog function via SQL.

    The function definition is fetched from UC on the first call and cached —
    parameter names, types, and order are derived automatically so the SQL
    call is always correct.

    Usage::

        from apx_agent import Agent, uc_function_tool

        agent = Agent(tools=[
            uc_function_tool("main.tools.classify_intent"),
        ])

    The returned tool accepts ``params: dict[str, Any]`` — a mapping of
    parameter name to value. Values are safely quoted for SQL.

    Args:
        function_name: Fully qualified UC function name (``catalog.schema.function``).
        name:          Tool name shown to the LLM. Defaults to the short function name.
        description:   Tool description. Defaults to the UC function's ``comment``.
    """
    from ._defaults import UserClientDependency

    _short_name = function_name.rsplit(".", 1)[-1]
    _tool_name = name or _short_name

    # Mutable cache — populated on first call using the live workspace client.
    # Keys: "parameters" (list ordered by position), "data_type" (str), "desc" (str).
    _cache: dict[str, Any] = {}

    _initial_desc = description or (
        f"Execute the Unity Catalog function `{function_name}`. "
        f"Pass parameters as a dict with parameter names as keys, e.g. "
        f'`{{"param1": "value1", "param2": 42}}`.'
    )

    async def _call_uc_function(params: dict[str, Any], ws: UserClientDependency) -> Any:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        # Fetch and cache function definition on first call
        if not _cache:
            func_info = ws.functions.get(full_name=function_name)
            raw_params = getattr(getattr(func_info, "input_params", None), "parameters", None) or []
            _cache["parameters"] = sorted(
                [
                    {
                        "name": p.name,
                        "position": p.position if p.position is not None else 0,
                        "type_name": str(p.type_name or "STRING"),
                    }
                    for p in raw_params
                ],
                key=lambda p: p["position"],
            )
            _cache["data_type"] = str(getattr(func_info, "data_type", "") or "")

        # Build positional SQL args from param dict
        sql_args = [
            _to_sql_literal(params.get(p["name"]), p["type_name"])
            for p in _cache["parameters"]
        ]
        sql = (
            f"SELECT {function_name}()"
            if not sql_args
            else f"SELECT {function_name}({', '.join(sql_args)})"
        )

        rows = run_sql(ws, sql, warehouse_id=None)

        # Scalar function: unwrap the single cell
        if rows and len(rows) == 1 and len(rows[0]) == 1:
            return next(iter(rows[0].values()))
        return rows

    _call_uc_function.__name__ = _tool_name
    _call_uc_function.__qualname__ = _tool_name
    _call_uc_function.__doc__ = _initial_desc
    return _call_uc_function
