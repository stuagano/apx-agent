"""DataAgent — an LlmAgent specialized for governed Unity Catalog data access.

A leaf agent primitive (alongside ``LlmAgent``, ``SequentialAgent``, ...) for the
most common Databricks shape: "talk to my data." It wires the governed data
tools and grounds its instructions in the actual schema, in one line::

    from apx_agent import DataAgent

    agent = DataAgent("main", "sales", warehouse_id="abc123", ws=w)

With a workspace client (``ws=``) it introspects ``catalog.schema`` at
construction: discovers the tables (declaring them as governed ``uc_table``
resources on its SQL tool), wires every UC function in the schema as a tool,
and generates schema-grounded instructions. Without ``ws`` it still produces a
working agent — a SQL tool plus generic data-assistant instructions — deferring
all introspection cost.

``DataAgent`` is a plain ``LlmAgent`` subclass, so it composes like any other
agent: use it directly, as a ``sub_agent``, or as a leaf in a
``SequentialAgent`` / ``RouterAgent``.
"""

from __future__ import annotations

from typing import Any

from ._agents import LlmAgent
from ._resources import ResourceSpec, attach_resources
from ._schema import build_instructions_from_schema, introspect_schema


class DataAgent(LlmAgent):
    """An ``LlmAgent`` wired to a Unity Catalog schema.

    Args:
        catalog: Unity Catalog catalog name.
        schema: Schema within the catalog.
        warehouse_id: SQL warehouse for the agent's ``sql_tool`` (and for schema
            introspection). When omitted, the warehouse is auto-discovered at
            call time.
        ws: Optional workspace client. When provided, the schema is introspected
            at construction — tables become governed ``uc_table`` resources and
            ground the instructions, and the schema's UC functions are wired as
            tools.
        include_functions: Wire the schema's UC functions as tools (needs ``ws``).
        genie_space: Optional Genie space id — adds a ``genie_tool``.
        vector_index: Optional Vector Search index — adds a ``vector_search_tool``.
        instructions: Override the schema-generated grounding instructions.
        name: Agent name. Defaults to ``"{schema}_data_agent"``.
        extra_tools: Additional tools to append.
        **kwargs: Forwarded to ``LlmAgent`` (``temperature``, ``sub_agents``,
            hooks, guardrails, etc.).
    """

    def __init__(
        self,
        catalog: str,
        schema: str,
        *,
        warehouse_id: str | None = None,
        ws: Any | None = None,
        include_functions: bool = True,
        genie_space: str | None = None,
        vector_index: str | None = None,
        instructions: str | None = None,
        name: str | None = None,
        extra_tools: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        from .genie import genie_tool
        from .sql_tools import sql_tool
        from .vector_search import vector_search_tool

        self.catalog = catalog
        self.schema = schema

        # Introspect once at construction (best-effort) when a client is given.
        tables = introspect_schema(ws, catalog, schema, warehouse_id) if ws else {}

        sql = sql_tool(warehouse_id=warehouse_id)
        if tables:
            # The schema's tables become governed resources, declared on the SQL
            # tool so they flow through the existing tool-based resource collection.
            attach_resources(
                sql,
                [ResourceSpec("uc_table", f"{catalog}.{schema}.{t}") for t in tables],
            )

        tools: list[Any] = [sql]
        if include_functions and ws is not None:
            from .catalog import uc_function_toolkit

            tools += uc_function_toolkit(f"{catalog}.{schema}", ws=ws)
        if genie_space:
            tools.append(genie_tool(genie_space))
        if vector_index:
            tools.append(vector_search_tool(vector_index))
        if extra_tools:
            tools += extra_tools

        super().__init__(
            tools=tools,
            instructions=instructions or build_instructions_from_schema(catalog, schema, tables),
            name=name or f"{schema}_data_agent",
            **kwargs,
        )
