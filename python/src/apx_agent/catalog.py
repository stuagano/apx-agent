"""catalog_tool, lineage_tool, schema_tool, uc_function_tool — Unity Catalog tool factories.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``UserClientDependency`` is resolved eagerly and ``get_type_hints()`` in
_inspection.py sees the real Annotated[...] type, not a string.
"""

import logging
import math
import re
from typing import Any

from databricks.sdk.errors.base import DatabricksError

from ._errors import ToolError
from ._sql import run_sql, sql_str_literal

logger = logging.getLogger(__name__)

# A plain finite decimal literal: optional sign, digits, optional fraction and
# exponent. Deliberately excludes ``nan``/``inf`` and Python underscore/hex forms
# that ``float()`` accepts but SQL does not.
_NUMERIC_LITERAL = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


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
        return sql_str_literal(str(value))
    # Numeric types — pass raw only if the value is a clean, finite decimal
    # literal. ``float()`` also accepts ``nan``/``inf`` and Python underscore/hex
    # forms (``1_000``, ``0x10``) that are junk or invalid as raw SQL tokens, so
    # anything that isn't a plain numeric literal falls back to a quoted string
    # (a clean type error at UC beats an unquoted token in the statement).
    raw = str(value)
    try:
        num = float(raw)
    except (ValueError, TypeError):
        return sql_str_literal(raw)
    if math.isfinite(num) and _NUMERIC_LITERAL.fullmatch(raw):
        return raw
    return sql_str_literal(raw)


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
        try:
            result = ws.api_client.do(
                "GET",
                "/api/2.1/unity-catalog/lineage-tracking/table-lineage",
                query={"table_name": table_name},
            )
        except DatabricksError as e:
            # A missing table / denied access is a finding the agent should
            # reason about, not a pipeline-fatal 500 (#562). Parsing bugs below
            # are NOT caught, so a genuine defect still fails loud.
            raise ToolError(f"get_table_lineage failed for {table_name!r}: {e}") from e
        assert isinstance(result, dict)
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
        try:
            t = ws.tables.get(full_name=table_name)
        except DatabricksError as e:
            # Missing table / denied access is a finding, not a 500 (#562).
            raise ToolError(f"describe_table failed for {table_name!r}: {e}") from e
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
    ws: Any | None = None,
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

    Description policy — in order of precedence:
      1. ``description`` argument, when set.
      2. The UC function's ``comment``, fetched at factory time when ``ws``
         is provided.
      3. A generic ``"Execute the Unity Catalog function ..."`` fallback.

    Pass ``ws`` to get the data team's documentation (the function's UC
    ``COMMENT``) surfaced as the LLM-facing tool description without the
    AI engineer having to copy it. The ``uc_function_toolkit`` helper
    does this automatically for every function in a schema.

    Args:
        function_name: Fully qualified UC function name (``catalog.schema.function``).
        name:          Tool name shown to the LLM. Defaults to the short function name.
        description:   Tool description override. When omitted and ``ws`` is set,
                       falls back to the UC function's ``comment``.
        ws:            Optional workspace client. When provided, the factory
                       eagerly fetches the function's ``comment`` to use as
                       the default description. Without ``ws``, the generic
                       fallback description is used.
    """
    from ._defaults import UserClientDependency

    _short_name = function_name.rsplit(".", 1)[-1]
    _tool_name = name or _short_name

    # Mutable cache — populated on first call using the live workspace client.
    # Keys: "parameters" (list ordered by position), "data_type" (str), "desc" (str).
    _cache: dict[str, Any] = {}

    _comment_desc: str | None = None
    if ws is not None and description is None:
        try:
            func_info = ws.functions.get(function_name)
            comment = getattr(func_info, "comment", None)
            if comment:
                _comment_desc = comment.strip()
        except Exception as e:
            logger.warning(
                "Failed to fetch comment for UC function %s: %s — "
                "falling back to generic description.",
                function_name, e,
            )

    from ._tool_factory import resolve_description
    _initial_desc = resolve_description(
        description,
        uc_comment=_comment_desc,
        fallback=(
            f"Execute the Unity Catalog function `{function_name}`. "
            f"Pass parameters as a dict with parameter names as keys, e.g. "
            f'`{{"param1": "value1", "param2": 42}}`.'
        ),
    )

    async def _call_uc_function(params: dict[str, Any], ws: UserClientDependency) -> Any:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        # Fetch and cache function definition on first call
        if not _cache:
            try:
                func_info = ws.functions.get(function_name)
            except DatabricksError as e:
                # Unknown function / denied access is a finding, not a 500 (#562).
                raise ToolError(f"UC function {function_name!r} lookup failed: {e}") from e
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
        cached_params: list[dict[str, Any]] = _cache["parameters"]
        sql_args = [
            _to_sql_literal(params.get(p["name"]), p["type_name"])
            for p in cached_params
        ]
        sql = (
            f"SELECT {function_name}()"
            if not sql_args
            else f"SELECT {function_name}({', '.join(sql_args)})"
        )

        try:
            rows = run_sql(ws, sql, warehouse_id=None)
        except (DatabricksError, RuntimeError) as e:
            # run_sql raises RuntimeError on query failure (incl. the #556
            # re-authorize hint). Contain it as a finding rather than a 500 (#562).
            raise ToolError(f"UC function {function_name!r} call failed: {e}") from e

        # Scalar function: unwrap the single cell
        if rows and len(rows) == 1 and len(rows[0]) == 1:
            return next(iter(rows[0].values()))
        return rows

    # Declare the UC function as a Mosaic AI resource so the compile path can
    # auto-derive ``resources=[DatabricksFunction(...)]`` at log time.
    from ._resources import ResourceSpec
    from ._tool_factory import build_tool
    return build_tool(
        _call_uc_function,
        name=_tool_name,
        description=_initial_desc,
        resources=[ResourceSpec("uc_function", function_name)],
    )


def uc_function_toolkit(
    catalog_schema: str,
    *,
    ws: Any | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Any]:
    """Discover Unity Catalog functions in a schema and return them as tools.

    Lists every function in ``catalog.schema``, builds a ``uc_function_tool``
    for each, and returns the list. Each returned tool carries the function's
    UC ``comment`` as its LLM-facing description and an auto-attached
    ``DatabricksFunction`` resource declaration.

    Use this to register an entire UC-governed tool surface in one line —
    the data team curates the schema; the agent picks up everything they
    publish without per-function wiring::

        from apx_agent import Agent, uc_function_toolkit
        from databricks.sdk import WorkspaceClient

        agent = Agent(
            instructions="Triage customer queries.",
            tools=uc_function_toolkit("main.agent_tools", ws=WorkspaceClient()),
        )

    Args:
        catalog_schema: ``"catalog.schema"`` identifying the UC schema to
            enumerate.
        ws: Workspace client used to list functions and fetch each
            function's comment. When omitted, a default ``WorkspaceClient()``
            is constructed (works in any environment where the
            ``databricks`` CLI / SDK auth chain is configured).
        include: Optional whitelist — only functions whose short names
            are in this list are returned. Use to bound the surface in a
            larger shared schema.
        exclude: Optional blacklist — functions whose short names are in
            this list are skipped.

    Returns:
        A list of tool callables ready to pass to ``Agent(tools=...)``.
        Empty list if the schema has no functions (or if listing fails;
        a warning is logged).
    """
    if "." not in catalog_schema or catalog_schema.count(".") != 1:
        raise ValueError(
            f"uc_function_toolkit expects a two-part catalog.schema identifier; "
            f"got {catalog_schema!r}"
        )
    catalog, schema = catalog_schema.split(".")

    if ws is None:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient()

    try:
        function_infos = list(ws.functions.list(catalog_name=catalog, schema_name=schema))
    except Exception as e:
        logger.warning(
            "uc_function_toolkit: failed to list functions in %s.%s: %s",
            catalog, schema, e,
        )
        return []

    include_set = set(include) if include else None
    exclude_set = set(exclude) if exclude else set()

    tools: list[Any] = []
    for info in function_infos:
        short_name = getattr(info, "name", None)
        if not short_name:
            continue
        if include_set is not None and short_name not in include_set:
            continue
        if short_name in exclude_set:
            continue
        full_name = getattr(info, "full_name", None) or f"{catalog}.{schema}.{short_name}"
        # Use the info we already have to set the description, avoiding a
        # second ws.functions.get round-trip per function. uc_function_tool
        # accepts a pre-resolved description.
        comment = getattr(info, "comment", None)
        tools.append(uc_function_tool(
            full_name,
            description=(comment.strip() if comment else None),
        ))

    return tools
