"""Tests for the coworker template — memory knob, CoworkerAgent, CoworkerTemplate."""
from __future__ import annotations

import pytest

from apx_agent.coworker import normalize_memory_knob


class TestNormalizeMemoryKnob:
    def test_off_disables_both(self):
        assert normalize_memory_knob("off") == (None, None)

    def test_inmemory_and_alias_local(self):
        for v in ("inmemory", "local", "InMemory", " LOCAL "):
            mem, sess = normalize_memory_knob(v)
            assert mem.type == "inmemory" and sess.type == "inmemory"

    def test_persistent_and_alias_delta_default_tier(self):
        for v in ("persistent", "delta"):
            mem, sess = normalize_memory_knob(v)
            assert mem.type == "delta" and sess.type == "delta"
            # No catalog → falls back to main.default so bare LlmAgent still works
            assert mem.table_name == "main.default.apx_memories"
            assert sess.table_name == "main.default.apx_sessions"

    def test_persistent_with_catalog_derives_uc_table_names(self):
        mem, sess = normalize_memory_knob("persistent", catalog="acme", schema="hr", name="hr_coworker")
        assert mem.type == "delta"
        assert mem.table_name == "acme.hr.apx_hr_coworker_memory"
        assert sess.table_name == "acme.hr.apx_hr_coworker_sessions"

    def test_persistent_with_catalog_slugifies_name(self):
        # Non-alphanumeric chars → underscores; trailing stripped
        mem, sess = normalize_memory_knob("persistent", catalog="cat", schema="sch", name="my-agent name!")
        assert mem.table_name == "cat.sch.apx_my_agent_name_memory"

    def test_persistent_with_catalog_falls_back_to_schema_when_no_name(self):
        mem, sess = normalize_memory_knob("persistent", catalog="cat", schema="sales")
        assert mem.table_name == "cat.sales.apx_sales_memory"
        assert sess.table_name == "cat.sales.apx_sales_sessions"

    def test_lakebase_errors_to_explicit_block(self):
        with pytest.raises(ValueError, match="lakebase"):
            normalize_memory_knob("lakebase")

    def test_unknown_value_errors_with_valid_rungs(self):
        with pytest.raises(ValueError, match="off|inmemory|persistent"):
            normalize_memory_knob("sometimes")


class TestCoworkerAgent:
    def test_is_data_agent_with_persona_and_memory_config(self):
        from apx_agent.coworker import CoworkerAgent
        from apx_agent import DataAgent
        cw = CoworkerAgent(
            "samples", "tpch",
            persona="a revenue analyst",
            memory="persistent",
            tables={"customer": ["c_custkey(bigint)"]},
        )
        assert isinstance(cw, DataAgent)
        # persona + grounding in the instructions
        assert cw._instructions.startswith("You are a revenue analyst.")
        assert "c_custkey(bigint)" in cw._instructions
        # memory declared (not yet built — needs ws at wiring time)
        assert cw.memory_config is not None and cw.memory_config.type == "delta"
        assert cw.session_config is not None and cw.session_config.type == "delta"
        # table names must be scoped to the coworker's own catalog.schema, not main.default
        assert cw.memory_config.table_name is not None
        assert cw.memory_config.table_name.startswith("samples.tpch.")
        assert cw.session_config.table_name is not None
        assert cw.session_config.table_name.startswith("samples.tpch.")

    def test_memory_off_declares_nothing(self):
        from apx_agent.coworker import CoworkerAgent
        cw = CoworkerAgent("samples", "tpch", memory="off",
                           tables={"t": ["a(int)"]})
        assert cw.memory_config is None and cw.session_config is None

    def test_default_memory_is_off(self):
        from apx_agent.coworker import CoworkerAgent
        cw = CoworkerAgent("samples", "tpch", tables={"t": ["a(int)"]})
        assert cw.memory_config is None

    def test_join_key_woven_into_instructions(self):
        from apx_agent.coworker import CoworkerAgent
        cw = CoworkerAgent(
            "main", "payroll",
            persona="a payroll analyst",
            join_key="employee ID",
            objective="surface mismatches between hours worked and paychecks issued",
            tables={"timesheet": ["emp_id(bigint)", "hours(float)"]},
        )
        assert "employee ID" in cw._instructions
        assert "You are a payroll analyst designed to" in cw._instructions

    def test_join_key_without_objective(self):
        from apx_agent.coworker import CoworkerAgent
        cw = CoworkerAgent(
            "main", "payroll",
            join_key="employee ID",
            tables={"timesheet": ["emp_id(bigint)"]},
        )
        assert "employee ID" in cw._instructions


class TestCoworkerTemplate:
    def test_registered_and_builds_coworker_agent(self):
        from apx_agent._template import template_registry
        from apx_agent.coworker import CoworkerAgent
        tmpl = template_registry.get("coworker")
        spec = tmpl.Spec(catalog="samples", schema="tpch",
                         persona="a revenue analyst", memory="persistent")
        agent = tmpl.build(spec, ws=None)
        assert isinstance(agent, CoworkerAgent)
        assert agent.memory_config.type == "delta"
        assert agent._instructions.startswith("You are a revenue analyst.")

    def test_data_template_still_resolves(self):
        from apx_agent._template import template_registry
        assert template_registry.get("data") is not None

    def test_exported_from_package(self):
        import apx_agent
        assert hasattr(apx_agent, "CoworkerAgent")
        assert hasattr(apx_agent, "CoworkerTemplate")
