"""Tests for _doctor._data_source_from_agent_py — reading catalog/schema from a
Python-canonical agent.py (DataAgent/CoworkerAgent constructor) when the
[tool.apx.agent.template] section is absent."""

from __future__ import annotations

from pathlib import Path

import apx_agent._doctor as doctor


def test_data_source_from_agent_py_reads_positional(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text(
        'from apx_agent.coworker import CoworkerAgent\n'
        'agent = CoworkerAgent("main", "payroll", persona="x")\n'
    )
    assert doctor._data_source_from_agent_py(tmp_path) == ("main", "payroll")


def test_data_source_from_agent_py_reads_kwargs(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text(
        'from apx_agent.data_agent import DataAgent\n'
        'agent = DataAgent(catalog="c", schema="s")\n'
    )
    assert doctor._data_source_from_agent_py(tmp_path) == ("c", "s")


def test_data_source_from_agent_py_none_when_absent(tmp_path: Path) -> None:
    assert doctor._data_source_from_agent_py(tmp_path) is None
