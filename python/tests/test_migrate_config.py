"""migrate-config: move a project off [tool.apx.agent] (sub-project 3a)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from apx_agent import Agent
from apx_agent._migrate_config import cut_sections, plan_migration, snippet

_PYPROJECT = '''[project]
name = "inspector"
version = "0"

[tool.apx.agent]
name = "inspector"
host = "apps"
model = "system.ai.claude-sonnet-4-6"
instructions = "Inspect tables."
examples = [
    "Show tables",
    "Count rows",
]

[tool.apx.agent.deploy]
space = "shared"

[tool.other]
keep = true
'''


def _write(tmp_path: Path, text: str = _PYPROJECT) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(text)
    return path


def test_cut_sections_removes_table_and_subtables_including_multiline_arrays() -> None:
    out = cut_sections(_PYPROJECT, "tool.apx.agent")
    data = tomllib.loads(out)
    assert "agent" not in data["tool"].get("apx", {})
    assert data["tool"]["other"] == {"keep": True}
    assert data["project"]["name"] == "inspector"


def test_first_run_moves_deploy_fields_and_lists_missing_runtime_fields(tmp_path: Path) -> None:
    path = _write(tmp_path)
    migration = plan_migration(path, Agent(name="inspector", tools=[]))
    data = tomllib.loads(migration.text)
    assert data["tool"]["apx"]["deploy"] == {"name": "inspector", "host": "apps", "space": "shared"}
    assert "deploy" not in data["tool"]["apx"]["agent"]
    assert "host" not in data["tool"]["apx"]["agent"]
    assert migration.removed_agent_table is False
    assert migration.missing == {
        "model": "system.ai.claude-sonnet-4-6", "instructions": "Inspect tables.",
        "examples": ["Show tables", "Count rows"],
    }


def test_table_is_removed_once_python_declares_everything(tmp_path: Path) -> None:
    path = _write(tmp_path)
    agent = Agent(name="inspector", tools=[], model="system.ai.claude-sonnet-4-6",
                  instructions="Inspect tables.", examples=["Show tables", "Count rows"])
    migration = plan_migration(path, agent)
    data = tomllib.loads(migration.text)
    assert migration.removed_agent_table is True and migration.missing == {}
    assert "agent" not in data["tool"]["apx"]
    assert data["tool"]["apx"]["deploy"]["space"] == "shared"


def test_fully_migrated_project_is_a_no_op(tmp_path: Path) -> None:
    text = '[project]\nname = "inspector"\nversion = "0"\n\n[tool.apx.deploy]\nspace = "shared"\n'
    path = _write(tmp_path, text)
    migration = plan_migration(path, Agent(name="inspector", tools=[]))
    assert migration.text == text and migration.missing == {} and migration.removed_agent_table is False


@pytest.mark.parametrize(("extra", "message"), [
    ('module = "bot:assistant"\n', r"module = 'bot:assistant' is not supported; rename it so the agent is agent:agent."),
    ('template = { name = "x" }\n', r"template = \{...\} is not supported; generate an agent.py from the template first."),
    ('executor = "openai-compat"\n', r"executor belongs in the YAML spec, not \[tool.apx.agent\]."),
])
def test_unsupported_keys_fail(tmp_path: Path, extra: str, message: str) -> None:
    text = '[project]\nname = "p"\nversion = "0"\n\n[tool.apx.agent]\nname = "p"\n' + extra
    with pytest.raises(ValueError, match=message):
        plan_migration(_write(tmp_path, text), Agent(name="p", tools=[]))


def test_snippet_is_paste_ready_keyword_arguments() -> None:
    assert snippet({"model": "m", "examples": ["a"]}) == "Agent(..., model='m', examples=['a'])"
