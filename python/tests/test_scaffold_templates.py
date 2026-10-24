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


# Effective settings each responses_agent scaffold produced on main before 3b-3c (captured, not guessed).
_BEFORE = {
 "base-False": {
  "catalog": None,
  "description": "An apx-agent on Databricks Apps.",
  "examples": [
   "Show me a few sample rows from the data you can access",
   "What questions can you answer?"
  ],
  "knowledge": None,
  "model": "databricks-claude-sonnet-4-6",
  "name": "orders",
  "registered_model": None,
  "schema": None,
  "session": None,
  "target": "responses_agent"
 },
 "base-True": {
  "catalog": None,
  "description": "An apx-agent on Databricks Apps.",
  "examples": [
   "Show me a few sample rows from the data you can access",
   "What questions can you answer?"
  ],
  "knowledge": None,
  "model": "databricks-claude-sonnet-4-6",
  "name": "orders",
  "registered_model": None,
  "schema": None,
  "session": {
   "auto_create": True,
   "database": "orders",
   "host": "${LAKEBASE_HOST}",
   "type": "lakebase",
   "validate_at_boot": True
  },
  "target": "responses_agent"
 },
 "coworker-False": {
  "catalog": "main",
  "description": "An apx-agent on Databricks Apps.",
  "examples": [
   "Show me a few sample rows from the data you can access",
   "What questions can you answer?"
  ],
  "knowledge": None,
  "model": "databricks-claude-sonnet-4-6",
  "name": "orders",
  "registered_model": "main.sales.orders",
  "schema": "sales",
  "session": None,
  "target": "responses_agent"
 },
 "coworker-True": {
  "catalog": "main",
  "description": "An apx-agent on Databricks Apps.",
  "examples": [
   "Show me a few sample rows from the data you can access",
   "What questions can you answer?"
  ],
  "knowledge": None,
  "model": "databricks-claude-sonnet-4-6",
  "name": "orders",
  "registered_model": "main.sales.orders",
  "schema": "sales",
  "session": {
   "auto_create": True,
   "database": "orders",
   "host": "${LAKEBASE_HOST}",
   "type": "lakebase",
   "validate_at_boot": True
  },
  "target": "responses_agent"
 },
 "data-False": {
  "catalog": "main",
  "description": "An apx-agent on Databricks Apps.",
  "examples": [
   "Show me a few sample rows from the data you can access",
   "What questions can you answer?"
  ],
  "knowledge": None,
  "model": "databricks-claude-sonnet-4-6",
  "name": "orders",
  "registered_model": "main.sales.orders",
  "schema": "sales",
  "session": None,
  "target": "responses_agent"
 },
 "data-True": {
  "catalog": "main",
  "description": "An apx-agent on Databricks Apps.",
  "examples": [
   "Show me a few sample rows from the data you can access",
   "What questions can you answer?"
  ],
  "knowledge": None,
  "model": "databricks-claude-sonnet-4-6",
  "name": "orders",
  "registered_model": "main.sales.orders",
  "schema": "sales",
  "session": {
   "auto_create": True,
   "database": "orders",
   "host": "${LAKEBASE_HOST}",
   "type": "lakebase",
   "validate_at_boot": True
  },
  "target": "responses_agent"
 }
}


@pytest.mark.parametrize("variant", sorted(_BEFORE))
def test_apps_responses_scaffold_keeps_its_effective_settings(tmp_path: Path, monkeypatch, variant: str) -> None:
    template, lakebase = variant.split("-")
    monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda profile: None)
    monkeypatch.setattr(cli, "_discover_default_data", lambda profile: None)
    args = ["agents", "scaffold", "--runtime", "responses_agent", "orders", "--target", "apps",
            "--template", template, "--dir", str(tmp_path), "--no-interactive"]
    if template != "base":
        args += ["--catalog", "main", "--schema", "sales"]
    args += [] if lakebase == "True" else ["--no-lakebase"]
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 0, result.output
    root = tmp_path / "orders"
    data = tomllib.loads((root / "pyproject.toml").read_text())
    assert "agent" not in data.get("tool", {}).get("apx", {}), "no scaffold writes [tool.apx.agent] (3b-3c)"
    assert static_agent_setting(root / "agent.py", "target") == "responses_agent"  # visible before import
    agent, config = _effective(root)
    flat = cli._read_apx_agent_config(root / "pyproject.toml")
    actual = {
        "name": config.name, "model": config.model, "target": config.target, "description": config.description,
        "examples": config.examples, "knowledge": config.knowledge,
        "session": config.session.model_dump(exclude_none=True) if config.session else None,
        "registered_model": flat.get("registered_model"), "catalog": flat.get("catalog"), "schema": flat.get("schema"),
    }
    assert actual == _BEFORE[variant]
    if template != "base":
        # The TOML's generic overlay used to land above the grounding at finalize; now agent.py does it.
        assert agent._instructions.startswith("You are a helpful assistant.")
        assert "main.sales" in agent._instructions
        assert "# memory=" in (root / "agent.py").read_text()


def test_force_rescaffold_preserves_the_responses_runtime(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda profile: None)
    args = ["agents", "scaffold", "orders", "--target", "apps", "--template", "base", "--dir", str(tmp_path),
            "--no-interactive"]
    assert CliRunner().invoke(cli.main, args + ["--runtime", "responses_agent"]).exit_code == 0
    assert CliRunner().invoke(cli.main, args + ["--force"]).exit_code == 0
    assert static_agent_setting(tmp_path / "orders" / "agent.py", "target") == "responses_agent"
