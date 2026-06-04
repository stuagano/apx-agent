"""DataAgent — an LlmAgent specialized for governed Unity Catalog data access.

Also the reference implementation of the Template protocol: ``DataTemplate``
wraps the same builder behind a typed Spec + registry entry, so the data agent
can be created by name/config as well as directly.

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

from pydantic import BaseModel, ConfigDict, Field

from ._agents import LlmAgent
from ._resources import ResourceSpec, attach_resources
from ._schema import build_instructions_from_schema, introspect_schema, load_baked_schema
from ._template import template


def _build_data_tools_and_instructions(
    *,
    catalog: str,
    schema: str,
    warehouse_id: str | None,
    ws: Any | None,
    include_functions: bool,
    genie_space: str | None,
    vector_index: str | None,
    instructions: str | None,
    tables: dict | None,
    extra_tools: list[Any] | None,
) -> tuple[list[Any], str]:
    """Shared builder: returns (tools, instructions) for the data agent shape."""
    from .genie import genie_tool
    from .sql_tools import sql_tool
    from .vector_search import vector_search_tool

    # Resolve the schema (table -> columns), in priority order:
    #   1) explicit `tables=` override
    #   2) live introspection when a workspace client is given
    #   3) the baked `.apx/schema.json` manifest (scaffold-time grounding)
    #   4) {} -> generic, ungrounded instructions
    resolved_tables: dict = tables or {}
    if not resolved_tables and ws:
        resolved_tables = introspect_schema(ws, catalog, schema, warehouse_id)
    if not resolved_tables:
        baked = load_baked_schema()
        if (
            baked
            and baked.get("catalog") == catalog
            and baked.get("schema") == schema
            and isinstance(baked.get("tables"), dict)
        ):
            resolved_tables = baked["tables"]
    tables = resolved_tables

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

    resolved_instructions = instructions or build_instructions_from_schema(
        catalog, schema, tables
    )
    return tools, resolved_instructions


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
        tables: Pre-baked schema as ``{table: ["col(type)", ...]}`` (e.g. the
            ``.apx/schema.json`` manifest). Grounds the agent without a live
            workspace call. When omitted, falls back to live introspection
            (if ``ws`` given) then auto-discovery of ``.apx/schema.json``.
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
        tables: dict | None = None,
        name: str | None = None,
        extra_tools: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.catalog = catalog
        self.schema = schema

        tools, resolved_instructions = _build_data_tools_and_instructions(
            catalog=catalog,
            schema=schema,
            warehouse_id=warehouse_id,
            ws=ws,
            include_functions=include_functions,
            genie_space=genie_space,
            vector_index=vector_index,
            instructions=instructions,
            tables=tables,
            extra_tools=extra_tools,
        )
        super().__init__(
            tools=tools,
            instructions=resolved_instructions,
            name=name or f"{schema}_data_agent",
            **kwargs,
        )


@template
class DataTemplate:
    """Talks to a governed Unity Catalog schema (SQL + UC functions, optional
    Genie / Vector Search) — the reference Template implementation.

    The Spec covers only role/skill inputs. Persona (``model``, instruction
    tone, generation knobs) is layered later from ``[tool.apx.agent]`` via
    ``apply_config_knobs``; ``name``/``instructions``/``extra_tools`` from the
    direct ``DataAgent`` constructor are intentionally out of the Spec for now.
    """

    name = "data"
    title = "Data Analyst"
    description = "Talks to a governed Unity Catalog schema (SQL + UC functions, optional Genie/Vector Search)."

    class Spec(BaseModel):
        """Spec for DataTemplate. In config dicts use the key ``schema`` (the
        field is stored as ``schema_name`` to avoid shadowing Pydantic's
        BaseModel.schema(); access it as ``spec.schema_name`` in code)."""

        model_config = ConfigDict(populate_by_name=True)
        catalog: str
        # Access as spec.schema_name in code; 'schema' alias is for config dicts only.
        schema_name: str = Field(alias="schema")
        warehouse_id: str | None = None
        genie_space: str | None = None
        vector_index: str | None = None
        include_functions: bool = True

    def build(self, spec: "DataTemplate.Spec", *, ws: Any | None = None) -> DataAgent:
        return DataAgent(
            spec.catalog,
            spec.schema_name,
            warehouse_id=spec.warehouse_id,
            ws=ws,
            include_functions=spec.include_functions,
            genie_space=spec.genie_space,
            vector_index=spec.vector_index,
        )
