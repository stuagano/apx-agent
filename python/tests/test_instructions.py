"""Tests for deterministic instruction generation and persistence."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apx_agent._ui_probe import _build_instructions_from_schema
from apx_agent._dev import _persist_instructions
from apx_agent import AgentConfig, AgentContext
from apx_agent._models import AgentCard


def _make_ctx(instructions: str = "") -> AgentContext:
    config = AgentConfig(name="test", model="claude-fake", instructions=instructions)
    card = AgentCard(name="test", description="", skills=[])
    return AgentContext(config=config, tools=[], card=card, agent=None)  # type: ignore[arg-type]


class TestBuildInstructionsFromSchema:
    def test_single_table_contains_table_name(self):
        result = _build_instructions_from_schema(
            "main", "sales", {"transactions": ["id", "amount"]}
        )
        assert "transactions" in result

    def test_multiple_tables_all_listed(self):
        result = _build_instructions_from_schema(
            "main", "sales", {"transactions": ["id"], "customers": ["id", "name"]}
        )
        assert "transactions" in result
        assert "customers" in result

    def test_empty_tables_uses_fqn(self):
        result = _build_instructions_from_schema("main", "sales", {})
        assert "main.sales" in result

    def test_no_catalog_uses_schema(self):
        result = _build_instructions_from_schema("", "sales", {})
        assert "sales" in result

    def test_no_catalog_no_schema_fallback(self):
        result = _build_instructions_from_schema("", "", {})
        assert "the data" in result

    def test_result_is_string(self):
        result = _build_instructions_from_schema("c", "s", {"t": ["col"]})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_instructs_sql_tool_usage(self):
        result = _build_instructions_from_schema("c", "s", {"orders": ["id"]})
        assert "SQL" in result or "sql" in result.lower()

    def test_deterministic(self):
        tables = {"a": ["x", "y"], "b": ["z"]}
        r1 = _build_instructions_from_schema("c", "s", tables)
        r2 = _build_instructions_from_schema("c", "s", tables)
        assert r1 == r2


class TestPersistInstructions:
    """_persist_instructions writes the root agent's instructions= in agent.py
    (the editor's + runtime's source of truth), not the ignored pyproject field."""

    def test_updates_ctx_instructions(self):
        ctx = _make_ctx("old instructions")
        with patch("apx_agent._ui_edit._find_agent_router_path", return_value=None):
            _persist_instructions(ctx, "new instructions")
        assert ctx.config.instructions == "new instructions"

    def test_preserves_dev_mode_addendum(self):
        ctx = _make_ctx("base\n\n[DEV MODE] You have create_tool")
        with patch("apx_agent._ui_edit._find_agent_router_path", return_value=None):
            _persist_instructions(ctx, "updated base")
        assert ctx.config.instructions.startswith("updated base")
        assert "[DEV MODE]" in ctx.config.instructions
        assert "create_tool" in ctx.config.instructions

    def test_ctx_none_does_not_raise(self):
        with patch("apx_agent._ui_edit._find_agent_router_path", return_value=None):
            _persist_instructions(None, "some instructions")

    def test_writes_instructions_into_agent_py(self, tmp_path: Path):
        agent_py = tmp_path / "agent.py"
        agent_py.write_text(
            'from apx_agent import Agent\n'
            'agent = Agent(name="x", instructions="old", tools=[t])\n'
        )
        with patch("apx_agent._ui_edit._find_agent_router_path", return_value=agent_py):
            _persist_instructions(None, "brand new instructions")
        updated = agent_py.read_text()
        assert "brand new instructions" in updated
        assert '"old"' not in updated
        # other args preserved
        assert 'name="x"' in updated and "tools=[t]" in updated
        compile(updated, "agent.py", "exec")  # never leaves broken source

    def test_skips_when_no_agent_file(self, tmp_path: Path):
        with patch("apx_agent._ui_edit._find_agent_router_path", return_value=None):
            _persist_instructions(None, "instructions")
        # No agent.py — should not raise

    def test_does_not_corrupt_unparseable_agent_py(self, tmp_path: Path):
        agent_py = tmp_path / "agent.py"
        broken = "agent = Agent(instructions=\n"  # syntax error
        agent_py.write_text(broken)
        with patch("apx_agent._ui_edit._find_agent_router_path", return_value=agent_py):
            _persist_instructions(None, "new")
        # Left untouched rather than corrupted further
        assert agent_py.read_text() == broken
