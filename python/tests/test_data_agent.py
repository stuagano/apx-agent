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


class TestLateWorkspaceBinding:
    """Python-canonical agents construct at import time with ws=None (render_agent_py
    emits no ws); finalize_agent later calls bind_workspace with the live ws so UC
    functions wire — making the import path tool-identical to a build-with-ws."""

    @staticmethod
    def _names(agent: LlmAgent) -> set[str]:
        return {t.__name__ for t in agent._tool_fns}

    def test_uc_functions_absent_without_ws(self):
        # include_functions defaults True; with no ws the UC functions can't wire yet.
        assert "classify" not in self._names(DataAgent("main", "sales"))

    def test_bind_workspace_wires_uc_functions(self):
        a = DataAgent("main", "sales")
        a.bind_workspace(_ws_with_schema({"orders": ["id(INT)"]}, functions=["classify", "score"]))
        assert {"classify", "score"} <= self._names(a)

    def test_bind_workspace_idempotent(self):
        a = DataAgent("main", "sales")
        ws = _ws_with_schema({}, functions=["classify"])
        a.bind_workspace(ws)
        a.bind_workspace(ws)
        assert sum(1 for t in a._tool_fns if t.__name__ == "classify") == 1

    def test_no_op_when_include_functions_false(self):
        a = DataAgent("main", "sales", include_functions=False)
        a.bind_workspace(_ws_with_schema({}, functions=["classify"]))
        assert "classify" not in self._names(a)

    def test_finalize_agent_triggers_bind(self):
        from apx_agent import AgentConfig
        from apx_agent._wiring import finalize_agent

        a = DataAgent("main", "sales")
        finalize_agent(a, AgentConfig(name="x"), ws=_ws_with_schema({}, functions=["classify"]))
        assert "classify" in self._names(a)

    def test_late_bind_matches_build_with_ws(self):
        # The discriminator: import-path (ws=None) + late bind == live-ws registry build.
        ws = _ws_with_schema({"orders": ["id(INT)"]}, functions=["classify", "score"])
        built = template_registry.build("data", {"catalog": "main", "schema": "sales"}, ws=ws)
        coded = DataAgent("main", "sales")  # exactly what render_agent_py emits
        coded.bind_workspace(ws)
        assert isinstance(built, LlmAgent)
        assert self._names(coded) == self._names(built)


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


class TestDataAgentKnowledgeKnob:
    def _enriched_bundle(self, root):
        from apx_agent._okf import write_okf_bundle
        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))"]}}
        write_okf_bundle(m, root, timestamp="z")
        (root / "tables" / "pay_runs.md").write_text(
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\ndescription: d\ntimestamp: z\n---\n\n"
            "# Overview\nDeclared-bundle narrative.\n\n# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) |  |\n"
        )
        return root

    def test_knowledge_bundle_grounds_even_without_cwd_apx(self, tmp_path, monkeypatch):
        from apx_agent.data_agent import _build_data_tools_and_instructions
        okf = self._enriched_bundle(tmp_path / "kb")
        monkeypatch.chdir(tmp_path)
        comp = _build_data_tools_and_instructions(
            catalog="c", schema="s", warehouse_id=None, ws=None, include_functions=False,
            genie_space=None, vector_index=None, instructions=None, persona=None,
            objective=None, tables=None, extra_tools=None, knowledge=str(okf),
        )
        assert "Declared-bundle narrative." in comp.instructions
        assert "gross_pay(decimal(6,2))" in comp.instructions

    def test_tables_override_still_wins_over_knowledge(self, tmp_path, monkeypatch):
        from apx_agent.data_agent import _build_data_tools_and_instructions
        okf = self._enriched_bundle(tmp_path / "kb")
        monkeypatch.chdir(tmp_path)
        comp = _build_data_tools_and_instructions(
            catalog="c", schema="s", warehouse_id=None, ws=None, include_functions=False,
            genie_space=None, vector_index=None, instructions=None, persona=None,
            objective=None, tables={"pay_runs": ["gross_pay(decimal(6,2))"]}, extra_tools=None,
            knowledge=str(okf),
        )
        assert "Declared-bundle narrative." not in comp.instructions

    def test_missing_knowledge_path_falls_through_no_raise(self, tmp_path, monkeypatch):
        from apx_agent.data_agent import _build_data_tools_and_instructions
        monkeypatch.chdir(tmp_path)
        comp = _build_data_tools_and_instructions(
            catalog="c", schema="s", warehouse_id=None, ws=None, include_functions=False,
            genie_space=None, vector_index=None, instructions=None, persona=None,
            objective=None, tables=None, extra_tools=None, knowledge=str(tmp_path / "nope"),
        )
        assert isinstance(comp.instructions, str)

    def test_knowledge_catalog_mismatch_degrades(self, tmp_path, monkeypatch):
        from apx_agent.data_agent import _build_data_tools_and_instructions
        okf = self._enriched_bundle(tmp_path / "kb")
        monkeypatch.chdir(tmp_path)
        comp = _build_data_tools_and_instructions(
            catalog="OTHER", schema="s", warehouse_id=None, ws=None, include_functions=False,
            genie_space=None, vector_index=None, instructions=None, persona=None,
            objective=None, tables=None, extra_tools=None, knowledge=str(okf),
        )
        assert "Declared-bundle narrative." not in comp.instructions


class TestStartupWarning:
    """Startup warehouse check warns early when no warehouse is available."""

    def test_no_warehouse_logs_warning(self, caplog):
        import logging
        from unittest.mock import MagicMock, patch
        from apx_agent import DataAgent

        ws = MagicMock()
        ws.warehouses.list.return_value = []
        ws.config.host = "https://my-workspace.azuredatabricks.net"

        with caplog.at_level(logging.WARNING, logger="apx_agent.data_agent"):
            DataAgent("main", "sales", ws=ws)

        assert any("SQL queries will fail" in r.message for r in caplog.records)
        assert any("my-workspace.azuredatabricks.net" in r.message for r in caplog.records)

    def test_warehouse_found_no_warning(self, caplog):
        import logging
        from unittest.mock import MagicMock
        from apx_agent import DataAgent

        wh = MagicMock()
        wh.id = "abc123"
        wh.warehouse_type = None
        ws = MagicMock()
        ws.warehouses.list.return_value = [wh]

        with caplog.at_level(logging.WARNING, logger="apx_agent.data_agent"):
            DataAgent("main", "sales", ws=ws)

        assert not any("SQL queries will fail" in r.message for r in caplog.records)

    def test_explicit_warehouse_id_skips_check(self, caplog):
        import logging
        from unittest.mock import MagicMock
        from apx_agent import DataAgent

        ws = MagicMock()
        ws.warehouses.list.return_value = []

        with caplog.at_level(logging.WARNING, logger="apx_agent.data_agent"):
            DataAgent("main", "sales", warehouse_id="explicit-id", ws=ws)

        # ws.warehouses.list should never be called when warehouse_id is explicit
        ws.warehouses.list.assert_not_called()
        assert not any("SQL queries will fail" in r.message for r in caplog.records)


class TestDataAgentOKFGrounding:
    def test_baked_grounding_reaches_instructions(self, tmp_path, monkeypatch):
        from apx_agent._okf import write_okf_bundle
        from apx_agent.data_agent import _build_data_tools_and_instructions

        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))"]}}
        okf = tmp_path / ".apx" / "okf"
        write_okf_bundle(m, okf, timestamp="z")
        (okf / "tables" / "pay_runs.md").write_text(
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\ndescription: d\ntimestamp: z\n---\n\n"
            "# Overview\nPay records narrative.\n\n# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) |  |\n"
        )
        monkeypatch.chdir(tmp_path)
        comp = _build_data_tools_and_instructions(
            catalog="c", schema="s", warehouse_id=None, ws=None, include_functions=False,
            genie_space=None, vector_index=None, instructions=None, persona=None,
            objective=None, tables=None, extra_tools=None,
        )
        assert "Pay records narrative." in comp.instructions

    def test_tables_override_does_not_pull_grounding(self, tmp_path, monkeypatch):
        from apx_agent._okf import write_okf_bundle
        from apx_agent.data_agent import _build_data_tools_and_instructions

        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))"]}}
        okf = tmp_path / ".apx" / "okf"
        write_okf_bundle(m, okf, timestamp="z")
        (okf / "tables" / "pay_runs.md").write_text(
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\ndescription: d\ntimestamp: z\n---\n\n"
            "# Overview\nShould not appear.\n\n# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) |  |\n"
        )
        monkeypatch.chdir(tmp_path)
        comp = _build_data_tools_and_instructions(
            catalog="c", schema="s", warehouse_id=None, ws=None, include_functions=False,
            genie_space=None, vector_index=None, instructions=None, persona=None,
            objective=None, tables={"pay_runs": ["gross_pay(decimal(6,2))"]}, extra_tools=None,
        )
        assert "Should not appear." not in comp.instructions

    def test_live_introspect_does_not_pull_grounding(self, tmp_path, monkeypatch):
        import apx_agent.data_agent as da
        from apx_agent._okf import write_okf_bundle
        from apx_agent.data_agent import _build_data_tools_and_instructions

        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))"]}}
        okf = tmp_path / ".apx" / "okf"
        write_okf_bundle(m, okf, timestamp="z")
        (okf / "tables" / "pay_runs.md").write_text(
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\ndescription: d\ntimestamp: z\n---\n\n"
            "# Overview\nShould not appear via introspect.\n\n# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) |  |\n"
        )
        monkeypatch.chdir(tmp_path)
        # Fake live introspection: non-None ws + introspect returns tables -> baked path skipped.
        ws = MagicMock()
        ws.warehouses.list.return_value = [MagicMock(id="wh1", warehouse_type=None)]
        monkeypatch.setattr(da, "introspect_schema", lambda ws, c, s, w: {"pay_runs": ["gross_pay(decimal(6,2))"]})
        comp = _build_data_tools_and_instructions(
            catalog="c", schema="s", warehouse_id=None, ws=ws, include_functions=False,
            genie_space=None, vector_index=None, instructions=None, persona=None,
            objective=None, tables=None, extra_tools=None,
        )
        assert "Should not appear via introspect." not in comp.instructions
