"""Smoke tests for the hubspot-complaints-agent example.

Doesn't make any Databricks calls — pure import-time + introspection checks.
"""

from __future__ import annotations


def test_agent_imports() -> None:
    import agent as agent_module
    assert hasattr(agent_module, "agent")


def test_agent_is_data_agent() -> None:
    import agent as agent_module
    from apx_agent import DataAgent
    assert isinstance(agent_module.agent, DataAgent)


def test_table_env_vars_default_to_placeholders() -> None:
    import agent as agent_module
    assert agent_module.CATALOG == "placeholder_catalog"
    assert agent_module.SCHEMA == "placeholder_schema"
    assert agent_module.TICKETS_TABLE == "tickets"
    assert agent_module.TABLE == "placeholder_catalog.placeholder_schema.tickets"


def test_instructions_reference_the_tickets_table_and_month_grouping() -> None:
    import agent as agent_module
    assert agent_module.TABLE in agent_module.agent._instructions
    assert "hs_createdate" in agent_module.agent._instructions
