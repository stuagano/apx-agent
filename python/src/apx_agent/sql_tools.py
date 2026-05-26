"""sql_tool — expose a Databricks SQL warehouse as a tool.

A general-purpose SQL execution tool the LLM can call to query data. For
production tools that should only query specific tables, prefer wrapping
the SQL in a UC function and registering it via ``@tool(uc=...)``
(governed, auditable, narrowly scoped). Use ``sql_tool`` when the agent
genuinely needs to author SQL on the fly.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``UserClientDependency`` is resolved eagerly at function definition time
and ``get_type_hints()`` in ``_inspection.py`` sees the real ``Annotated[...]``
type, not a string.
"""

import logging
from typing import Any

from ._sql import run_sql

logger = logging.getLogger(__name__)


def sql_tool(
    *,
    warehouse_id: str | None = None,
    name: str = "run_sql",
    description: str | None = None,
    max_rows: int = 1000,
) -> Any:
    """Return a tool that runs a SQL query and returns the rows.

    The query runs against a Databricks SQL warehouse. When ``warehouse_id``
    is provided, that warehouse is used and declared as a Mosaic AI resource;
    when omitted, the framework auto-discovers a warehouse at call time
    (handy for dev, less suitable for production where you want explicit
    governance).

    The workspace client is injected automatically and runs as the calling
    user, so UC table grants apply.

    Usage::

        from apx_agent import Agent, sql_tool

        agent = Agent(tools=[
            sql_tool(warehouse_id="abc123",
                     description="Run SQL against the analytics warehouse."),
        ])

    Args:
        warehouse_id: Fixed SQL warehouse ID to use. When set, declared as
            a ``DatabricksSQLWarehouse`` resource at log time. When omitted,
            the warehouse is auto-discovered on each call (no resource
            declaration).
        name: Tool name shown to the LLM. Defaults to ``"run_sql"``.
        description: Tool description shown to the LLM. When omitted,
            generated from the warehouse_id (or a generic description).
        max_rows: Maximum number of rows returned to the LLM. Truncated
            with a warning if the query produces more. Default 1000.
    """
    from ._defaults import UserClientDependency
    from ._resources import ResourceSpec
    from ._tool_factory import build_tool

    if description is None:
        if warehouse_id:
            _desc = (
                f"Run a SQL query against warehouse `{warehouse_id}` and return the rows. "
                f"Returns up to {max_rows} rows. Auth is per-call user identity — "
                f"the query sees only what the calling user has UC grants on."
            )
        else:
            _desc = (
                f"Run a SQL query and return the rows. Returns up to {max_rows} rows. "
                f"Auth is per-call user identity — the query sees only what the "
                f"calling user has UC grants on."
            )
    else:
        _desc = description

    async def _run_sql(query: str, ws: UserClientDependency) -> dict[str, Any]:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        try:
            rows = run_sql(ws, query, warehouse_id=warehouse_id)
        except Exception as e:
            logger.warning("SQL query failed: %s", e)
            return {"error": str(e), "rows": []}

        truncated = len(rows) > max_rows
        if truncated:
            logger.info("SQL result truncated from %d to %d rows", len(rows), max_rows)
            rows = rows[:max_rows]
        return {
            "row_count": len(rows),
            "truncated": truncated,
            "rows": rows,
        }

    return build_tool(
        _run_sql,
        name=name,
        description=_desc,
        resources=[ResourceSpec("sql_warehouse", warehouse_id)] if warehouse_id else (),
    )
