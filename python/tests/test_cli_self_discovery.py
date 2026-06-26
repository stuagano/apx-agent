"""CLI self-discovery: `describe-cli --json` (the whole command tree as one
JSON manifest) and `agents list --local` (offline project-declared agents)."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from apx_agent.cli import main


# ── describe-cli: the machine-readable manifest ──────────────────────────────


def _tree() -> dict:
    result = CliRunner().invoke(main, ["describe-cli"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_describe_cli_emits_valid_json_with_name_and_version():
    t = _tree()
    assert t["name"] == "apx"
    assert t["version"]
    assert isinstance(t["commands"], dict)


def test_describe_cli_covers_the_command_tree():
    t = _tree()
    # Top-level groups are present...
    for group in ("agents", "eval", "uc", "fleet", "memory", "traces"):
        assert group in t["commands"], f"{group} missing from manifest"
    # ...and nested subcommands are reachable.
    agents = t["commands"]["agents"]["commands"]
    assert {"list", "describe", "deploy", "scaffold"} <= set(agents)


def test_describe_cli_describes_flags_with_types_and_choices():
    params = _tree()["commands"]["agents"]["commands"]["list"]["params"]
    by_flag = {f: p for p in params for f in p["flags"]}
    fmt = by_flag["--format"]
    assert fmt["type"] == "choice"
    assert fmt["choices"] == ["text", "json"]
    assert fmt["default"] == "text"
    assert by_flag["--apps-only"]["is_flag"] is True
    # the auto-injected --help is filtered out of the manifest
    assert "--help" not in by_flag


def test_describe_cli_help_param_is_excluded_everywhere():
    t = _tree()

    def walk(node):
        for p in node.get("params", []):
            assert "--help" not in p["flags"]
        for child in node.get("commands", {}).values():
            walk(child)

    walk(t)


def test_describe_cli_manifest_cannot_drift_from_the_live_tree():
    # The manifest introspects the real click tree, so any registered flag shows
    # up automatically — proven by the offline --local flag added alongside it.
    params = _tree()["commands"]["agents"]["commands"]["list"]["params"]
    flags = {f for p in params for f in p["flags"]}
    assert "--local" in flags


# ── agents list --local: offline, project-declared discovery ─────────────────


_AGENT_PY = '''\
from apx_agent import Agent

def lookup(q: str) -> str:
    """Look something up."""
    return q

agent = Agent(tools=[lookup])
'''


def _project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (tmp_path / "agent.py").write_text(_AGENT_PY)
    return tmp_path


def test_list_local_discovers_declared_agent_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(_project(tmp_path))
    result = CliRunner().invoke(main, ["agents", "list", "--local", "--format", "json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "agent"
    assert rows[0]["tool_count"] == 1  # `lookup` parsed from tools=[...]
    assert rows[0]["source"] == "agent.py"


def test_list_local_text_render(tmp_path, monkeypatch):
    monkeypatch.chdir(_project(tmp_path))
    result = CliRunner().invoke(main, ["agents", "list", "--local"])
    assert result.exit_code == 0
    assert "AGENT" in result.output and "agent" in result.output


def test_list_local_empty_project_is_graceful(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='empty'\n")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["agents", "list", "--local"])
    assert result.exit_code == 0
    assert "No agents declared" in result.output
    assert "scaffold" in result.output


def test_list_local_rejects_workspace_flags(tmp_path, monkeypatch):
    monkeypatch.chdir(_project(tmp_path))
    result = CliRunner().invoke(main, ["agents", "list", "--local", "--catalog", "main"])
    assert result.exit_code != 0
    assert "can't combine" in result.output


def test_list_local_makes_no_databricks_calls(tmp_path, monkeypatch):
    # If --local touched the SDK, _require_sdk would run; make it explode to prove
    # the offline path never reaches it.
    import apx_agent.cli as climod

    def boom(*_a, **_k):
        raise AssertionError("--local must not call the Databricks SDK")

    monkeypatch.setattr(climod, "_require_sdk", boom)
    monkeypatch.chdir(_project(tmp_path))
    result = CliRunner().invoke(main, ["agents", "list", "--local", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["agent_name"] == "agent"


# ── universal --json alias (auto-injected by _ApxCommand) ────────────────────


def _flags_for(*path: str) -> set[str]:
    t = _tree()
    node = t
    for p in path:
        node = node["commands"][p]
    return {f for param in node.get("params", []) for f in param["flags"]}


def test_json_alias_auto_added_to_format_commands():
    # Every command exposing --format with a json choice also exposes --json.
    for path in (("agents", "list"), ("agents", "describe"), ("uc", "tables"),
                 ("fleet", "list"), ("traces", "list"), ("memory", "list"),
                 ("examples", "list")):
        flags = _flags_for(*path)
        assert "--format" in flags and "--json" in flags, f"{path} missing --json"


def test_json_alias_not_added_to_non_json_format():
    # uc topology uses --format [mermaid|graphviz] — no json choice, no --json.
    flags = _flags_for("uc", "topology")
    assert "--format" in flags
    assert "--json" not in flags


def test_json_alias_equivalent_to_format_json(tmp_path, monkeypatch):
    monkeypatch.chdir(_project(tmp_path))
    a = CliRunner().invoke(main, ["agents", "list", "--local", "--json"])
    b = CliRunner().invoke(main, ["agents", "list", "--local", "--format", "json"])
    assert a.exit_code == 0 and a.output == b.output


def test_json_alias_preserves_a_json_default(monkeypatch):
    # A command whose --format defaults to json must still default to json with
    # the injected --json alias present (no default clobber).
    import click
    from apx_agent.cli import _ApxCommand

    captured = {}
    cmd = _ApxCommand(
        "x",
        params=[click.Option(["--format", "fmt"],
                             type=click.Choice(["text", "json"]), default="json")],
        callback=lambda fmt: captured.update(fmt=fmt),
    )
    CliRunner().invoke(cmd, [])
    assert captured["fmt"] == "json"
    CliRunner().invoke(cmd, ["--format", "text"])
    assert captured["fmt"] == "text"
