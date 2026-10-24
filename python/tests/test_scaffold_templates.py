"""Scaffold templates declare settings in agent.py (sub-project 3b-3c)."""

from __future__ import annotations

import runpy
import sys
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from apx_agent import cli
from apx_agent._inspection import _load_agent_config, static_agent_setting
from apx_agent._settings import resolve_settings


def _effective(root: Path):
    sys.path.insert(0, str(root))
    try:
        agent = runpy.run_path(str(root / "agent.py"))["agent"]
    finally:
        sys.path.remove(str(root))
    return agent, resolve_settings(agent, _load_agent_config(pyproject_path=root / "pyproject.toml"))


def test_model_serving_scaffold_has_no_agent_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_schema_manifest_for_scaffold", lambda *a, **k: None)
    root = tmp_path / "ms"
    cli._scaffold_model_serving(root, "ms", False, "main", "sales")
    assert "agent" not in tomllib.loads((root / "pyproject.toml").read_text()).get("tool", {}).get("apx", {})
    agent, config = _effective(root)
    assert (config.name, config.model, config.description) == ("ms", "databricks-claude-sonnet-4-6", "An apx-agent.")
    assert "You are a helpful assistant." in agent._instructions  # overlaid, as the TOML did
    assert "main.sales" in agent._instructions  # the schema grounding is kept
    assert config.knowledge is None
