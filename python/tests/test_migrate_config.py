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


import subprocess
import sys

from click.testing import CliRunner

from apx_agent.cli import main

_AGENT_PY_BARE = "from apx_agent import Agent\nagent = Agent(name='inspector', tools=[])\n"
_AGENT_PY_FULL = (
    "from apx_agent import Agent\n"
    "agent = Agent(name='inspector', tools=[], model='system.ai.claude-sonnet-4-6',\n"
    "              instructions='Inspect tables.', examples=['Show tables', 'Count rows'])\n"
)


def _repo(tmp_path: Path, agent_py: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
    (tmp_path / "agent.py").write_text(agent_py)
    for args in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    return tmp_path


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("agent", None)
    return CliRunner().invoke(main, ["agents", "migrate-config"])


def _commit_all(tmp_path: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"], cwd=tmp_path, check=True)


def test_two_runs_migrate_a_real_shaped_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ctk import Artifact, verify

    root = _repo(tmp_path, _AGENT_PY_BARE)
    first = _run(root, monkeypatch)
    assert first.exit_code == 0, first.output
    assert "Agent(..., model='system.ai.claude-sonnet-4-6'" in first.output
    for needle in ("[tool.apx.deploy]", "[tool.apx.agent]"):
        verify(Artifact(str(root / "pyproject.toml"), min_bytes=50, must_contain=needle))

    (root / "agent.py").write_text(_AGENT_PY_FULL)
    _commit_all(root)
    second = _run(root, monkeypatch)
    assert second.exit_code == 0, second.output
    for needle in ("[tool.apx.deploy]", 'space = "shared"'):
        verify(Artifact(str(root / "pyproject.toml"), min_bytes=50, must_contain=needle))
    data = tomllib.loads((root / "pyproject.toml").read_text())
    assert "agent" not in data["tool"]["apx"]
    assert data["tool"]["other"] == {"keep": True}

    _commit_all(root)
    third = _run(root, monkeypatch)
    assert third.exit_code == 0 and "nothing to migrate" in third.output


def test_refuses_a_dirty_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path, _AGENT_PY_BARE)
    (root / "pyproject.toml").write_text(_PYPROJECT + "\n# local edit\n")
    result = _run(root, monkeypatch)
    assert result.exit_code != 0
    assert "pyproject.toml has uncommitted changes; commit them first (git is migrate-config's undo)." in result.output


def test_a_read_back_mismatch_restores_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import _migrate_config

    root = _repo(tmp_path, _AGENT_PY_FULL)
    original = (root / "pyproject.toml").read_text()
    real = _migrate_config.plan_migration

    def lossy(pyproject: Path, agent):
        planned = real(pyproject, agent)
        return _migrate_config.Migration(planned.text.replace('space = "shared"', 'space = "other"'),
                                         planned.missing, planned.removed_agent_table)

    monkeypatch.setattr(_migrate_config, "plan_migration", lossy)
    result = _run(root, monkeypatch)
    assert result.exit_code != 0
    assert "migrate-config changed the effective config; restored pyproject.toml." in result.output
    assert (root / "pyproject.toml").read_text() == original


def test_existing_app_name_survives_the_migration(tmp_path: Path) -> None:
    text = _PYPROJECT + '\n[tool.apx.deploy]\napp_name = "mcp-inspector"\n'
    migration = plan_migration(_write(tmp_path, text), Agent(name="inspector", tools=[]))
    assert tomllib.loads(migration.text)["tool"]["apx"]["deploy"]["app_name"] == "mcp-inspector"


def test_keys_that_are_not_settings_are_listed_for_manual_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path, _AGENT_PY_FULL)
    (root / "pyproject.toml").write_text(_PYPROJECT.replace('[tool.apx.agent]\n', '[tool.apx.agent]\ncustom_flag = "x"\n'))
    _commit_all(root)
    result = _run(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "remove by hand once handled elsewhere: custom_flag" in result.output
    assert "Agent(..., )" not in result.output


def test_fully_migrated_project_needs_no_agent_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path, "raise RuntimeError('boom')\n")
    (root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n\n[tool.apx.deploy]\nspace = "s"\n')
    _commit_all(root)
    result = _run(root, monkeypatch)
    assert result.exit_code == 0 and "nothing to migrate" in result.output


@pytest.mark.parametrize(("agent_py", "message"), [
    (None, "no agent.py in"),
    ("raise RuntimeError('boom')\n", "could not import agent.py: RuntimeError: boom"),
])
def test_agent_import_problems_are_clean_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                agent_py: str | None, message: str) -> None:
    root = _repo(tmp_path, _AGENT_PY_BARE)
    if agent_py is None:
        (root / "agent.py").unlink()
    else:
        (root / "agent.py").write_text(agent_py)
    _commit_all(root)
    result = _run(root, monkeypatch)
    assert result.exit_code != 0 and message in result.output
    assert "Traceback" not in result.output


def test_metadata_keys_move_to_their_homes(tmp_path: Path) -> None:
    text = _PYPROJECT.replace('[tool.apx.agent]\n',
                              '[tool.apx.agent]\nregistered_model = "c.s.m"\nprofile = "dev"\nexperiment = "/e"\n')
    text += '\n[tool.apx]\nkeep = 1\n\n[tool.apx.metadata]\napp-name = "x"\n'
    agent = Agent(name="inspector", tools=[], model="system.ai.claude-sonnet-4-6",
                  instructions="Inspect tables.", examples=["Show tables", "Count rows"])
    migration = plan_migration(_write(tmp_path, text), agent)
    data = tomllib.loads(migration.text)
    assert migration.manual == [] and migration.removed_agent_table is True
    assert data["tool"]["apx"]["deploy"]["registered_model"] == "c.s.m"
    assert data["tool"]["apx"]["profile"] == "dev" and data["tool"]["apx"]["experiment"] == "/e"
    assert data["tool"]["apx"]["keep"] == 1
    assert data["tool"]["apx"]["metadata"] == {"app-name": "x"}


def test_data_triage_registered_model_is_not_manual(tmp_path: Path) -> None:
    real = Path(__file__).resolve().parents[1] / "examples" / "data-triage-agent" / "pyproject.toml"
    migration = plan_migration(_write(tmp_path, real.read_text()), Agent(name="data_triage_agent", tools=[]))
    data = tomllib.loads(migration.text)
    assert "registered_model" not in migration.manual
    assert data["tool"]["apx"]["deploy"]["registered_model"] == "serverless_stable_7def3z_catalog.apx.mcp_data_triage_agent"
    assert data["tool"]["apx"]["deploy"]["app_name"] == "mcp-data-triage-agent"


def test_migrate_local_moves_the_legacy_profile() -> None:
    from apx_agent._migrate_config import migrate_local

    out = migrate_local('[tool.apx.agent]\nprofile = "dev"\n')
    assert out is not None and tomllib.loads(out) == {"tool": {"apx": {"profile": "dev"}}}
    assert migrate_local('[tool.apx]\nprofile = "dev"\n') is None


def test_cli_migrates_apx_local_with_a_backup_even_when_pyproject_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path, _AGENT_PY_BARE)
    (root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n\n[tool.apx.deploy]\nspace = "s"\n')
    _commit_all(root)
    (root / ".apx.local").write_text('[tool.apx.agent]\nprofile = "dev"\n')
    result = _run(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert tomllib.loads((root / ".apx.local").read_text()) == {"tool": {"apx": {"profile": "dev"}}}
    assert (root / ".apx.local.bak").read_text() == '[tool.apx.agent]\nprofile = "dev"\n'
    assert "migrated .apx.local" in result.output


def test_migrate_local_never_overrides_a_newer_tool_apx_key() -> None:
    from apx_agent._migrate_config import migrate_local

    out = migrate_local('[tool.apx]\nprofile = "new"\n\n[tool.apx.agent]\nprofile = "stale"\n')
    assert out is not None and tomllib.loads(out) == {"tool": {"apx": {"profile": "new"}}}


def test_tool_apx_nested_values_survive_moving_dev_keys(tmp_path: Path) -> None:
    text = _PYPROJECT.replace('[tool.apx.agent]\n', '[tool.apx.agent]\nprofile = "dev"\n')
    text += '\n[tool.apx]\nmeta = { a = 1 }\nother.x = 2\n'
    migration = plan_migration(_write(tmp_path, text), Agent(name="inspector", tools=[]))
    apx = tomllib.loads(migration.text)["tool"]["apx"]
    assert (apx["profile"], apx["meta"], apx["other"]) == ("dev", {"a": 1}, {"x": 2})


def test_apx_local_is_untouched_when_the_pyproject_migration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path, _AGENT_PY_BARE)
    (root / ".apx.local").write_text('[tool.apx.agent]\nprofile = "dev"\n')
    (root / "pyproject.toml").write_text(_PYPROJECT + "\n# dirty\n")
    result = _run(root, monkeypatch)
    assert result.exit_code != 0
    assert (root / ".apx.local").read_text() == '[tool.apx.agent]\nprofile = "dev"\n'


def test_an_existing_backup_is_never_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path, _AGENT_PY_BARE)
    (root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n\n[tool.apx.deploy]\nspace = "s"\n')
    _commit_all(root)
    (root / ".apx.local").write_text('[tool.apx.agent]\nprofile = "dev"\n')
    (root / ".apx.local.bak").write_text("PRECIOUS")
    result = _run(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert (root / ".apx.local.bak").read_text() == "PRECIOUS"
    assert (root / ".apx.local.bak.2").read_text() == '[tool.apx.agent]\nprofile = "dev"\n'


def test_a_lakebase_session_table_moves_to_session_kwarg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The Apps scaffold's default: a responses_agent with a Lakebase session (no App Space).
    text = ('[project]\nname = "inspector"\nversion = "0"\n\n'
            '[tool.apx.agent]\nname = "inspector"\ntarget = "responses_agent"\n'
            'model = "system.ai.claude-sonnet-4-6"\ninstructions = "Inspect tables."\n'
            'examples = ["Show tables", "Count rows"]\n\n'
            '[tool.apx.agent.session]\ntype = "lakebase"\nhost = "${LAKEBASE_HOST}"\ndatabase = "inspector"\n')
    root = _repo(tmp_path, _AGENT_PY_FULL)
    (root / "pyproject.toml").write_text(text)
    _commit_all(root)
    first = _run(root, monkeypatch)
    assert first.exit_code == 0, first.output
    assert "session={'type': 'lakebase', 'host': '${LAKEBASE_HOST}', 'database': 'inspector'}" in first.output
    (root / "agent.py").write_text(_AGENT_PY_FULL.replace(
        "examples=['Show tables', 'Count rows'])",
        "examples=['Show tables', 'Count rows'], target='responses_agent',\n"
        "              session={'type': 'lakebase', 'host': '${LAKEBASE_HOST}', 'database': 'inspector'})"))
    _commit_all(root)
    second = _run(root, monkeypatch)
    assert second.exit_code == 0, second.output
    assert "agent" not in tomllib.loads((root / "pyproject.toml").read_text())["tool"]["apx"]


def test_a_managed_memory_table_converges(tmp_path: Path) -> None:
    text = _PYPROJECT.replace('[tool.apx.agent]\n', '[tool.apx.agent]\n') + '\n[tool.apx.agent.memory]\ntype = "managed"\n'
    agent = Agent(name="inspector", tools=[], model="system.ai.claude-sonnet-4-6", instructions="Inspect tables.",
                  examples=["Show tables", "Count rows"], memory="managed")
    migration = plan_migration(_write(tmp_path, text), agent)
    assert "memory" not in migration.missing, migration.missing


_PERSONA = '[project]\nname = "p"\nversion = "0"\n\n[tool.apx.agent]\nname = "p"\ninstructions = "You are a helpful assistant."\n'


def test_an_instructions_overlay_counts_as_declared(tmp_path: Path) -> None:
    agent = Agent(name="p", tools=[], instructions="Grounded in main.sales.")
    agent.overlay_instructions("You are a helpful assistant.")
    migration = plan_migration(_write(tmp_path, _PERSONA), agent)
    assert migration.removed_agent_table, migration.missing


def test_a_different_overlay_is_still_missing(tmp_path: Path) -> None:
    agent = Agent(name="p", tools=[], instructions="Grounded.")
    agent.overlay_instructions("Be terse.")
    assert plan_migration(_write(tmp_path, _PERSONA), agent).missing == {"instructions": "You are a helpful assistant."}
