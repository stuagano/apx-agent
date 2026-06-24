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

from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

import logging

from ._agents import LlmAgent
from ._resources import ResourceSpec, attach_resources
from ._schema import build_instructions_from_schema, introspect_schema, load_baked_schema, load_okf_grounding
from ._template import template

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataAgentHealth:
    """Structured discovery/wiring health for a ``DataAgent``.

    Captured at construction and refreshed by ``bind_workspace`` so day-two ops
    can read it without re-introspecting. ``/readyz`` surfaces the degraded
    reason (see ``data_degraded_reason``) as its ``data`` check.

    Fields:
        grounded: True when the schema resolved from a real source (not the
            ungrounded fallback).
        schema_source: which resolution branch won —
            ``"tables"`` | ``"introspect"`` | ``"knowledge"`` | ``"baked"`` |
            ``"ungrounded"``.
        table_count: number of tables grounding the agent.
        uc_functions: ``"wired"`` (functions bound) | ``"deferred"`` (enabled
            but waiting on a live ws via ``bind_workspace``) | ``"disabled"``
            (``include_functions=False``) | ``"none"`` (enabled, ws present, but
            the schema has no functions).
        warehouse: ``"declared"`` (explicit ``warehouse_id``) | ``"auto"``
            (resolved/deferred to call time) | ``"unavailable"`` (no warehouse
            reachable — SQL will fail until one exists).
    """

    grounded: bool
    schema_source: str
    table_count: int
    uc_functions: str
    warehouse: str


def data_degraded_reason(health: "DataAgentHealth") -> str | None:
    """The single most important degraded reason, or ``None`` when healthy.

    Used to set ``agent._apx_data_degraded`` (the string ``/readyz`` reports).
    An unreachable warehouse outranks ungrounded, since it breaks every query;
    ungrounded is a working-but-generic state, not a failure.
    """
    if health.warehouse == "unavailable":
        return "no SQL warehouse available — queries will fail until one is created"
    if not health.grounded:
        return (
            "no schema grounding — running as a generic SQL assistant "
            "(pass ws= or bake .apx/schema.json)"
        )
    return None


@dataclass(frozen=True)
class DataAgentProbe:
    """Result of an active day-two health check (``DataAgent.probe(ws)``).

    Unlike ``DataAgentHealth`` (captured at construction), this is a *live* check
    against the workspace — meant to run off the hot path, from an ops job /
    notebook / CLI, not per request.

    Fields:
        ok: True when the warehouse is reachable and the live schema still
            matches what the agent was grounded on.
        warehouse: ``"reachable"`` | ``"unavailable"``.
        schema: ``"match"`` (live == grounded) | ``"drift"`` (tables added or
            removed) | ``"unreachable"`` (couldn't read the live schema) |
            ``"ungrounded"`` (agent has no grounded tables to compare).
        missing_tables: grounded tables no longer present in the live schema.
        new_tables: live tables not in the agent's grounding.
        detail: short human reason when ``ok`` is False, else ``None``.
    """

    ok: bool
    warehouse: str
    schema: str
    missing_tables: tuple[str, ...]
    new_tables: tuple[str, ...]
    detail: str | None


@dataclass(frozen=True)
class _DataAgentComponents:
    tools: list[Any]
    instructions: str
    health: "DataAgentHealth"


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
    persona: str | None,
    objective: str | None,
    tables: dict | None,
    extra_tools: list[Any] | None,
    knowledge: str | None = None,
) -> _DataAgentComponents:
    """Shared builder: returns tools and instructions for the data agent shape."""
    from .genie import genie_tool
    from .sql_tools import sql_tool
    from .vector_search import vector_search_tool

    # Resolve the schema (table -> columns), in priority order:
    #   1) explicit `tables=` override
    #   2) live introspection when a workspace client is given
    #   3) explicit `knowledge=` bundle path (OKF grounding)
    #   4) the baked `.apx/schema.json` manifest (scaffold-time grounding)
    #   5) {} -> generic, ungrounded instructions
    resolved_tables: dict = tables or {}
    baked_was_source = False
    knowledge_was_source = False
    explicit_grounding = None
    # Which resolution branch won — captured for day-two health (see DataAgentHealth).
    schema_source = "tables" if resolved_tables else "ungrounded"
    if not resolved_tables and ws:
        resolved_tables = introspect_schema(ws, catalog, schema, warehouse_id)
        if resolved_tables:
            schema_source = "introspect"
    if not resolved_tables and knowledge:
        from ._schema import load_grounding_from_path
        km, kg = load_grounding_from_path(knowledge)
        if (
            km
            and km.get("catalog") == catalog
            and km.get("schema") == schema
            and isinstance(km.get("tables"), dict)
        ):
            resolved_tables = km["tables"]
            # knowledge_was_source takes priority in the grounding-selection if-elif below,
            # so baked_was_source is never reached for this branch — kept only as a marker
            # meaning "a baked source won" for any future consumer that inspects that flag.
            baked_was_source = True
            knowledge_was_source = True
            explicit_grounding = kg
            schema_source = "knowledge"
    if not resolved_tables:
        baked = load_baked_schema()
        if (
            baked
            and baked.get("catalog") == catalog
            and baked.get("schema") == schema
            and isinstance(baked.get("tables"), dict)
        ):
            resolved_tables = baked["tables"]
            baked_was_source = True
            schema_source = "baked"
    if not resolved_tables:
        logger.warning(
            "DataAgent(%r, %r): no schema found via tables=, ws=, or .apx/schema.json — "
            "running ungrounded (generic SQL assistant). Pass ws= or run "
            "`apx-agent scaffold` to bake the schema.",
            catalog,
            schema,
        )
    tables = resolved_tables

    sql = sql_tool(warehouse_id=warehouse_id)

    # Startup warehouse check — surface missing warehouse in logs before any
    # user query, not silently on the first SQL call. Outcome is captured in
    # health.warehouse for day-two ops.
    if warehouse_id is not None:
        warehouse_state = "declared"
    elif ws is not None:
        from ._sql import get_warehouse_id as _get_wh
        try:
            _get_wh(ws)
            warehouse_state = "auto"
        except RuntimeError as _wh_err:
            warehouse_state = "unavailable"
            logger.warning(
                "DataAgent(%r, %r): %s — SQL queries will fail until a warehouse "
                "is created. Create one in your workspace then re-deploy.",
                catalog, schema, _wh_err,
            )
    else:
        # No explicit id and no ws to check now — resolved at call time.
        warehouse_state = "auto"

    if tables:
        # The schema's tables become governed resources, declared on the SQL
        # tool so they flow through the existing tool-based resource collection.
        attach_resources(
            sql,
            [ResourceSpec("uc_table", f"{catalog}.{schema}.{t}") for t in tables],
        )

    tools: list[Any] = [sql]
    if not include_functions:
        uc_functions_state = "disabled"
    elif ws is None:
        # Enabled but no live ws yet — wired later by bind_workspace().
        uc_functions_state = "deferred"
    else:
        from .catalog import uc_function_toolkit

        fns = uc_function_toolkit(f"{catalog}.{schema}", ws=ws)
        tools += fns
        uc_functions_state = "wired" if fns else "none"
    if genie_space:
        tools.append(genie_tool(genie_space))
    if vector_index:
        tools.append(vector_search_tool(vector_index))
    if extra_tools:
        tools += extra_tools

    if knowledge_was_source:
        grounding = explicit_grounding
    elif baked_was_source:
        grounding = load_okf_grounding()
    else:
        grounding = None

    # Verified/golden queries (#288 phase 2): when the grounding bundle carries
    # any, expose a verified_query tool that matches the user's question to a
    # golden query and runs its TRUSTED SQL with safe named-parameter binding —
    # the SQL is the verified template, so the LLM only supplies param values.
    golden = [
        gq for tg in (grounding or {}).values() for gq in (tg.get("golden_queries") or [])
    ]
    if golden:
        from ._verified_query import verified_query_tool
        tools.append(verified_query_tool(
            golden, warehouse_id=warehouse_id, catalog=catalog, schema=schema,
        ))

    resolved_instructions = instructions or build_instructions_from_schema(
        catalog, schema, tables, persona=persona, objective=objective, grounding=grounding
    )
    health = DataAgentHealth(
        grounded=schema_source != "ungrounded",
        schema_source=schema_source,
        table_count=len(tables),
        uc_functions=uc_functions_state,
        warehouse=warehouse_state,
    )
    return _DataAgentComponents(
        tools=tools, instructions=resolved_instructions, health=health
    )


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
        persona: Optional role phrase ("a payroll analyst"). Woven into
            schema-generated instructions. Ignored when ``instructions`` is given.
        objective: Optional mission phrase ("surface mismatches between hours
            worked and paychecks issued"). When both persona and objective are
            given, the lead becomes "You are {persona} designed to {objective}."
        tables: Pre-baked schema as ``{table: ["col(type)", ...]}`` (e.g. the
            ``.apx/schema.json`` manifest). Grounds the agent without a live
            workspace call. When omitted, falls back to live introspection
            (if ``ws`` given) then auto-discovery of ``.apx/schema.json``.
        knowledge: Path to an OKF bundle directory. When provided, used as the
            grounding source after live introspection but before the baked
            ``.apx/schema.json`` manifest. Catalog/schema must match.
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
        persona: str | None = None,
        objective: str | None = None,
        tables: dict | None = None,
        knowledge: str | None = None,
        name: str | None = None,
        extra_tools: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.catalog = catalog
        self.schema = schema
        # Stored for the active day-two probe() — the warehouse to introspect.
        self._warehouse_id = warehouse_id

        _components = _build_data_tools_and_instructions(
            catalog=catalog,
            schema=schema,
            warehouse_id=warehouse_id,
            ws=ws,
            include_functions=include_functions,
            genie_space=genie_space,
            vector_index=vector_index,
            instructions=instructions,
            persona=persona,
            objective=objective,
            tables=tables,
            extra_tools=extra_tools,
            knowledge=knowledge,
        )
        # Late ws-binding state: when the agent is constructed without a ws (the
        # Python-canonical import path — agent.py runs at import time before any
        # workspace client exists), the schema's UC functions can't be wired yet.
        # bind_workspace() wires them once finalize_agent supplies the live ws.
        self._include_functions = include_functions
        self._uc_functions_bound = include_functions and ws is not None

        # Day-two ops state (mirrors the _apx_memory_degraded convention):
        # _apx_data is the structured discovery/wiring health; _apx_data_degraded
        # is the short reason /readyz reports (None when healthy).
        self._apx_data = _components.health
        self._apx_data_degraded = data_degraded_reason(_components.health)

        super().__init__(
            tools=_components.tools,
            instructions=_components.instructions,
            name=name or f"{schema}_data_agent",
            **kwargs,
        )

    def bind_workspace(self, ws: Any) -> None:
        """Wire ws-dependent tools (UC functions) once a live ws is available.

        Idempotent: a no-op if functions are disabled or already bound. Called by
        ``finalize_agent`` on every runtime, so an agent constructed at import with
        ``ws=None`` ends up with the same UC-function tools as one built with a live
        ws at construction. Must run before the A2A/MCP card snapshot so the tools
        are advertised, not just callable — ``finalize_agent`` guarantees that order.
        """
        if not self._include_functions or self._uc_functions_bound or ws is None:
            return
        from .catalog import uc_function_toolkit

        fns = uc_function_toolkit(f"{self.catalog}.{self.schema}", ws=ws)
        for fn in fns:
            self._register_tool(fn)
        self._uc_functions_bound = True

        # Refresh day-two health: deferred functions are now wired (or the schema
        # genuinely has none), so /readyz and ops reflect the live state.
        self._apx_data = replace(
            self._apx_data, uc_functions="wired" if fns else "none"
        )
        self._apx_data_degraded = data_degraded_reason(self._apx_data)

    def probe(self, ws: Any) -> "DataAgentProbe":
        """Active day-two health check against a live workspace.

        Off the hot path — call from a scheduled ops job, notebook, or CLI, not
        per request (``/readyz`` is the cheap, per-request readiness signal).
        Best-effort: never raises; every failure comes back as a structured
        field. Checks two things that drift after deploy:

          1. the SQL warehouse is reachable, and
          2. the live schema still matches the tables the agent is grounded on.

        Grounded tables are read back from the agent's own ``uc_table`` resources
        (no separate state to keep in sync). An ungrounded agent has nothing to
        compare, so its schema check is reported as ``"ungrounded"``, not drift.
        """
        from ._resources import collect_resource_specs
        from ._schema import introspect_schema

        prefix = f"{self.catalog}.{self.schema}."
        grounded = {
            s.identifier[len(prefix):]
            for s in collect_resource_specs(self)
            if s.kind == "uc_table" and s.identifier.startswith(prefix)
        }

        # Warehouse reachability. An explicit id is trusted; otherwise resolve a
        # warehouse live and treat any failure as unavailable (best-effort —
        # state is recorded, not swallowed).
        warehouse = "reachable"
        if self._warehouse_id is None:
            from ._sql import get_warehouse_id

            try:
                get_warehouse_id(ws)
            except Exception:
                warehouse = "unavailable"

        live_names = set(introspect_schema(ws, self.catalog, self.schema, self._warehouse_id))

        missing: tuple[str, ...] = ()
        new: tuple[str, ...] = ()
        if not grounded:
            schema_state = "ungrounded"
        elif not live_names:
            # Couldn't read the schema — don't claim every table vanished.
            schema_state = "unreachable"
        else:
            missing = tuple(sorted(grounded - live_names))
            new = tuple(sorted(live_names - grounded))
            schema_state = "match" if not missing and not new else "drift"

        ok = warehouse == "reachable" and schema_state in ("match", "ungrounded")
        detail = None
        if not ok:
            parts: list[str] = []
            if warehouse != "reachable":
                parts.append("warehouse unavailable")
            if schema_state == "unreachable":
                parts.append("schema unreadable (check warehouse/grants)")
            if schema_state == "drift":
                bits: list[str] = []
                if missing:
                    bits.append(f"missing {list(missing)}")
                if new:
                    bits.append(f"new {list(new)}")
                parts.append("schema drift: " + ", ".join(bits))
            detail = "; ".join(parts)

        return DataAgentProbe(
            ok=ok,
            warehouse=warehouse,
            schema=schema_state,
            missing_tables=missing,
            new_tables=new,
            detail=detail,
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
        knowledge: str | None = None

    def build(self, spec: "DataTemplate.Spec", *, ws: Any | None = None) -> DataAgent:
        return DataAgent(
            spec.catalog,
            spec.schema_name,
            warehouse_id=spec.warehouse_id,
            ws=ws,
            include_functions=spec.include_functions,
            genie_space=spec.genie_space,
            vector_index=spec.vector_index,
            knowledge=spec.knowledge,
        )
