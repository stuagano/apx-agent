"""New Apps projects select native hosting without changing existing declarations."""
from pathlib import Path
import runpy
import tomllib

import pytest
import click
from click.testing import CliRunner

from apx_agent import cli
from ctk import Artifact, verify


@pytest.fixture(autouse=True)
def offline_scaffold(monkeypatch):
    monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda *_: None)
    monkeypatch.setattr(cli, "_schema_manifest_for_scaffold", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_probe_first_table", lambda *a: None)
    monkeypatch.setattr(cli, "_discover_default_data", lambda *a: None)


@pytest.mark.parametrize("template", ["base", "data", "coworker"])
@pytest.mark.parametrize("editable", [False, True])
def test_new_apps_scaffold_defaults_to_native(tmp_path, template, monkeypatch, editable):
    monkeypatch.setattr(cli, "_is_inside_framework_repo", lambda _: editable)
    result = CliRunner().invoke(cli.main, ["agents", "scaffold", "orders", "--here", "--dir", str(tmp_path),
                                        "--template", template, "--no-interactive"])
    assert result.exit_code == 0, result.output
    root = tmp_path / "orders"
    verify(Artifact(str(root / "pyproject.toml"), must_contain="durable_agent_server"),
           Artifact(str(root / "agent.py"), min_bytes=40))
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert any(dep.startswith("apx-agent[eval,agentbricks]") for dep in project["project"]["dependencies"])
    from packaging.requirements import Requirement
    dependency = Requirement(project["project"]["dependencies"][0])
    if editable:
        assert dependency.url is None
        assert project["tool"]["uv"]["sources"]["apx-agent"]["editable"] is True
    else:
        assert dependency.url.startswith("git+https://github.com/stuagano/apx-agent.git@")
    assert not any((root / name).exists() for name in ("databricks.yml", "agent_server", "scripts/quickstart.py", ".github"))
    assert project["tool"]["apx"]["agent"]["session"] == {
        "type": "managed", "store_name": "apx-orders-sessions",
        "auto_create": False, "validate_at_boot": True,
    }
    assert "memory" not in project["tool"]["apx"]["agent"]
    monkeypatch.chdir(root)
    agent = runpy.run_path(str(root / "agent.py"))["agent"]
    assert agent._name == "orders"
    from apx_agent._inspection import _load_agent_config
    assert _load_agent_config(pyproject_path=root / "pyproject.toml").target == "durable_agent_server"


@pytest.mark.parametrize("runtime", ["responses_agent", "durable_agent_server"])
def test_force_scaffold_preserves_existing_runtime(tmp_path: Path, runtime):
    args = ["agents", "scaffold", "orders", "--here", "--dir", str(tmp_path), "--template", "base", "--no-interactive"]
    first = CliRunner().invoke(cli.main, args + ["--runtime", runtime])
    assert first.exit_code == 0, first.output
    repeated = CliRunner().invoke(cli.main, args + ["--force"])
    assert repeated.exit_code == 0, repeated.output
    from apx_agent._inspection import _load_agent_config
    assert _load_agent_config(pyproject_path=tmp_path / "orders" / "pyproject.toml").target == runtime
    before = (tmp_path / "orders" / "pyproject.toml").read_bytes()
    other = "responses_agent" if runtime == "durable_agent_server" else "durable_agent_server"
    rejected = CliRunner().invoke(cli.main, args + ["--force", "--runtime", other])
    assert rejected.exit_code != 0 and "does not migrate" in rejected.output
    assert (tmp_path / "orders" / "pyproject.toml").read_bytes() == before


def test_gallery_runtime_defaults_preserve_explicit_contracts():
    from apx_agent import AgentConfig

    assert cli._scaffold_runtime_config(AgentConfig(name="orders"), None).target == "durable_agent_server"
    explicit = AgentConfig(name="orders", target="responses_agent")
    assert cli._scaffold_runtime_config(explicit, None).target == "responses_agent"
    assert cli._scaffold_runtime_config(explicit, "durable_agent_server").target == "durable_agent_server"
    incompatible = AgentConfig(name="orders", target="responses_agent", session={"type": "lakebase"})
    with pytest.raises(click.ClickException, match="incompatible with durable_agent_server"):
        cli._scaffold_runtime_config(incompatible, "durable_agent_server")


def test_force_preserves_legacy_project_without_runtime_config(tmp_path):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "agent.py").write_text("agent = None\n")
    result = CliRunner().invoke(cli.main, ["agents", "scaffold", "orders", "--here", "--dir", str(tmp_path),
                                        "--template", "base", "--no-interactive", "--force"])
    assert result.exit_code == 0, result.output
    from apx_agent._inspection import _load_agent_config
    assert _load_agent_config(pyproject_path=root / "pyproject.toml").target == "responses_agent"


@pytest.mark.parametrize("options", [["--lakebase"], ["--ci", "github"], ["--target", "model-serving", "--runtime", "durable_agent_server"]])
def test_native_scaffold_rejects_incompatible_options(tmp_path, options):
    result = CliRunner().invoke(cli.main, ["agents", "scaffold", "orders", "--here", "--dir", str(tmp_path),
                                        "--template", "base", "--no-interactive", *options])
    assert result.exit_code != 0
    assert "responses_agent" in result.output
    assert not (tmp_path / "orders" / "agent.py").exists()


# --- default model (#955) ---------------------------------------------------


def test_pick_default_model_takes_the_first_served_candidate(monkeypatch):
    first, second = cli._DEFAULT_MODEL_CANDIDATES[:2]
    monkeypatch.setattr(cli, "_make_scaffold_workspace_client", lambda profile: object())
    probed: list[str] = []

    def probe(ws, name):
        probed.append(name)
        return "served" if name == second else "403 PERMISSION_DENIED"

    monkeypatch.setattr(cli, "_probe_model", probe)
    assert cli._pick_default_model("p") == second
    assert probed == [first, second]  # stops at the first served candidate
    monkeypatch.setattr(cli, "_probe_model", lambda ws, name: "404 NOT_FOUND")
    assert cli._pick_default_model("p") == first  # nothing served: first candidate


def test_pick_default_model_without_a_workspace_is_the_first_candidate(monkeypatch):
    def unreachable(profile):
        raise RuntimeError("host does not resolve")

    monkeypatch.setattr(cli, "_make_scaffold_workspace_client", unreachable)
    assert cli._pick_default_model("p") == cli._DEFAULT_MODEL_CANDIDATES[0]


@pytest.mark.parametrize("profile", [None, "p"])
def test_native_scaffold_writes_the_picked_model(tmp_path, monkeypatch, profile):
    """Only an explicit --profile probes; without one no network call is made."""
    picked: list[str | None] = []

    def pick(p):
        picked.append(p)
        return "system.ai.claude-opus-4-7"

    monkeypatch.setattr(cli, "_pick_default_model", pick)
    args = ["agents", "scaffold", "orders", "--here", "--dir", str(tmp_path), "--template", "base",
            "--no-interactive"] + (["--profile", profile] if profile else [])
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 0, result.output
    model = tomllib.loads((tmp_path / "orders" / "pyproject.toml").read_text())["tool"]["apx"]["agent"]["model"]
    if profile:
        assert (picked, model) == (["p"], "system.ai.claude-opus-4-7")
    else:
        assert (picked, model) == ([], cli._DEFAULT_MODEL_CANDIDATES[0])
