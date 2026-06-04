"""Tests for DataAgent — an LlmAgent wired to a Unity Catalog schema."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apx_agent import Agent, DataAgent, DataTemplate, LlmAgent, template_registry
from apx_agent._resources import collect_resource_specs


def _ws_with_schema(tables: dict[str, list[str]], functions: list[str] | None = None):
    """Fake workspace client: information_schema query + UC function listing."""
    ws = MagicMock()

    # information_schema.columns result
    rows = [[t, col.split("(")[0], col.split("(")[1].rstrip(")")]
            for t, cols in tables.items() for col in cols]
    ws.statement_execution.execute_statement.return_value = SimpleNamespace(
        result=SimpleNamespace(data_array=rows) if rows else SimpleNamespace(data_array=[]),
        manifest=SimpleNamespace(schema=SimpleNamespace(columns=[
            SimpleNamespace(name="table_name"),
            SimpleNamespace(name="column_name"),
            SimpleNamespace(name="data_type"),
        ])),
    )
    # uc_function_toolkit lists functions in the schema
    ws.functions.list.return_value = [
        SimpleNamespace(name=f, full_name=f"main.sales.{f}", comment="") for f in (functions or [])
    ]
    return ws


class TestNoWorkspace:
    def test_is_an_llm_agent(self):
        a = DataAgent("main", "sales")
        assert isinstance(a, LlmAgent) and isinstance(a, Agent)

    def test_has_sql_tool_and_generic_instructions(self):
        a = DataAgent("main", "sales")
        assert a._instructions and "main.sales" in a._instructions
        assert any(getattr(t, "__name__", "") == "run_sql" for t in a._tool_fns)

    def test_default_name(self):
        assert DataAgent("main", "sales")._name == "sales_data_agent"
        assert DataAgent("main", "sales", name="biz")._name == "biz"

    def test_carries_catalog_schema(self):
        a = DataAgent("main", "sales")
        assert a.catalog == "main" and a.schema == "sales"

    def test_no_table_resources_without_ws(self):
        specs = collect_resource_specs(DataAgent("main", "sales"))
        assert not [s for s in specs if s.kind == "uc_table"]


class TestWithWorkspace:
    def test_instructions_grounded_in_tables(self):
        ws = _ws_with_schema({"orders": ["id(INT)", "total(DOUBLE)"], "customers": ["id(INT)"]})
        a = DataAgent("main", "sales", warehouse_id="wh", ws=ws)
        assert "orders" in a._instructions and "customers" in a._instructions

    def test_declares_tables_as_uc_table_resources(self):
        ws = _ws_with_schema({"orders": ["id(INT)"], "customers": ["id(INT)"]})
        a = DataAgent("main", "sales", warehouse_id="wh", ws=ws)
        tables = {s.identifier for s in collect_resource_specs(a) if s.kind == "uc_table"}
        assert tables == {"main.sales.orders", "main.sales.customers"}

    def test_wires_uc_functions(self):
        ws = _ws_with_schema({"orders": ["id(INT)"]}, functions=["classify", "score"])
        a = DataAgent("main", "sales", warehouse_id="wh", ws=ws)
        names = {getattr(t, "__name__", "") for t in a._tool_fns}
        assert "classify" in names and "score" in names

    def test_warehouse_declared(self):
        ws = _ws_with_schema({"orders": ["id(INT)"]})
        a = DataAgent("main", "sales", warehouse_id="wh-7", ws=ws)
        wh = {s.identifier for s in collect_resource_specs(a) if s.kind == "sql_warehouse"}
        assert "wh-7" in wh

    def test_introspection_failure_degrades_gracefully(self):
        ws = MagicMock()
        ws.statement_execution.execute_statement.side_effect = RuntimeError("no perms")
        a = DataAgent("main", "sales", warehouse_id="wh", ws=ws)  # must not raise
        assert isinstance(a, LlmAgent)
        assert not [s for s in collect_resource_specs(a) if s.kind == "uc_table"]


class TestOptions:
    def test_genie_and_vector_tools_added(self):
        a = DataAgent("main", "sales", genie_space="sp-1", vector_index="main.sales.idx")
        specs = {s.kind for s in collect_resource_specs(a)}
        assert "genie_space" in specs and "vector_search_index" in specs

    def test_instructions_override(self):
        a = DataAgent("main", "sales", instructions="Custom data agent.")
        assert a._instructions == "Custom data agent."

    def test_extra_tools_appended(self):
        from apx_agent import tool

        @tool
        def helper(x: str) -> str:
            """Help."""
            return x

        a = DataAgent("main", "sales", extra_tools=[helper])
        assert any(getattr(t, "__name__", "") == "helper" for t in a._tool_fns)


class TestTopology:
    def test_node_type_is_dataagent(self):
        from apx_agent._topology import _agent_class_to_node_type

        # Recognized as its own type (not falling back to the generic "Agent").
        assert _agent_class_to_node_type(DataAgent("main", "sales")) == "DataAgent"


class TestDataTemplate:
    def test_registered_in_global_registry(self):
        assert template_registry.get("data").name == "data"

    def test_build_returns_dataagent_equivalent_to_constructor(self):
        ws = _ws_with_schema({"orders": ["id(INT)", "total(DOUBLE)"]})
        spec = DataTemplate.Spec(catalog="main", schema="sales")
        built = DataTemplate().build(spec, ws=ws)
        direct = DataAgent("main", "sales", ws=ws)
        assert type(built) is DataAgent
        assert built._instructions == direct._instructions
        assert [t.__name__ for t in built._tool_fns] == [t.__name__ for t in direct._tool_fns]

    def test_build_from_dict_via_registry_alias(self):
        agent = template_registry.build("data", {"catalog": "main", "schema": "sales"})
        assert type(agent) is DataAgent
        assert agent.schema == "sales"

    def test_topology_node_type_still_dataagent_for_built(self):
        from apx_agent._topology import _agent_class_to_node_type
        agent = DataTemplate().build(DataTemplate.Spec(catalog="main", schema="sales"))
        assert _agent_class_to_node_type(agent) == "DataAgent"


class TestDataAgentBakedSchema:
    def test_explicit_tables_ground_instructions(self):
        from apx_agent import DataAgent
        agent = DataAgent(
            "samples", "tpch",
            tables={"customer": ["c_custkey(bigint)", "c_name(string)"]},
        )
        instr = agent._instructions
        assert "customer" in instr and "c_custkey(bigint)" in instr
        assert "call the SQL tool to confirm what tables" not in instr

    def test_auto_discovers_manifest(self, tmp_path, monkeypatch):
        import json
        from apx_agent._schema import APX_DIR, SCHEMA_MANIFEST_NAME
        from apx_agent import DataAgent
        d = tmp_path / APX_DIR
        d.mkdir()
        (d / SCHEMA_MANIFEST_NAME).write_text(json.dumps({
            "catalog": "samples", "schema": "tpch",
            "tables": {"orders": ["o_orderkey(bigint)"]},
        }))
        monkeypatch.chdir(tmp_path)
        agent = DataAgent("samples", "tpch")
        assert "orders" in agent._instructions and "o_orderkey(bigint)" in agent._instructions
        assert "call the SQL tool to confirm what tables" not in agent._instructions

    def test_manifest_for_other_schema_ignored(self, tmp_path, monkeypatch):
        import json
        from apx_agent._schema import APX_DIR, SCHEMA_MANIFEST_NAME
        from apx_agent import DataAgent
        d = tmp_path / APX_DIR
        d.mkdir()
        (d / SCHEMA_MANIFEST_NAME).write_text(json.dumps({
            "catalog": "other", "schema": "elsewhere",
            "tables": {"x": ["a(int)"]},
        }))
        monkeypatch.chdir(tmp_path)
        agent = DataAgent("samples", "tpch")  # different schema → ignore manifest
        assert "call the SQL tool to confirm what tables" in agent._instructions

    def test_no_manifest_falls_back(self, tmp_path, monkeypatch):
        from apx_agent import DataAgent
        monkeypatch.chdir(tmp_path)
        agent = DataAgent("samples", "tpch")
        assert "call the SQL tool to confirm what tables" in agent._instructions
