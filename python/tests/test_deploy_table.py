"""[tool.apx.deploy]: deploy settings outside the retired [tool.apx.agent] table (sub-project 3a)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apx_agent._inspection import _load_agent_config
from apx_agent._tool_config import ToolConfigError


def _project(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "orders-agent"\nversion = "0"\n\n' + body)
    return path


def test_deploy_table_alone_builds_a_config(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.deploy]\nhost = "apps"\ninstances = 2\n')
    config = _load_agent_config(pyproject_path=path)
    assert config is not None
    assert (config.name, config.host) == ("orders-agent", "apps")
    assert config.deploy is not None and config.deploy.instances == 2


def test_deploy_table_name_wins_over_project_name(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.deploy]\nname = "custom"\n')
    assert _load_agent_config(pyproject_path=path).name == "custom"


@pytest.mark.parametrize("body", [
    '[tool.apx.agent]\nname = "legacy"\n\n[tool.apx.deploy]\nspace = "s"\n',
    '[tool.apx.agent]\nname = "a"\n\n[tool.apx.agent.deploy]\nspace = "s"\n\n[tool.apx.deploy]\nname = "a"\nspace = "s"\n',
    '[tool.apx.agent]\nname = "a"\n\n[tool.apx.deploy]\nname = "b"\n',
    '[tool.apx.agent]\nname = "a"\n\n[tool.apx.agent.deploy]\nspace = "x"\n\n[tool.apx.deploy]\nspace = "y"\n',
    '[tool.apx.agent]\n\n[tool.apx.deploy]\nspace = "s"\n',
])
def test_the_agent_table_is_rejected_alongside_the_deploy_table(tmp_path: Path, body: str) -> None:
    with pytest.raises(ToolConfigError, match="migrate-config"):
        _load_agent_config(pyproject_path=_project(tmp_path, body))


def test_neither_table_is_none(tmp_path: Path) -> None:
    assert _load_agent_config(pyproject_path=_project(tmp_path, "")) is None


def test_deploy_validation_fires_from_the_new_table(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.deploy]\ninstances = 2\n\n[tool.apx.deploy.autoscale]\nmin = 1\nmax = 3\n')
    with pytest.raises(ValueError):
        _load_agent_config(pyproject_path=path)


def test_runtime_name_env_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APX_AGENT_NAME", "from-env")
    path = _project(tmp_path, '[tool.apx.deploy]\nname = "custom"\n')
    assert _load_agent_config(pyproject_path=path).name == "from-env"


def test_durable_doctor_reads_a_deploy_only_project(tmp_path: Path) -> None:
    from apx_agent._doctor import Status
    from apx_agent._doctor_durable import check_durable_readiness

    root = tmp_path / "p"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0.1.0"\n\n[tool.apx.deploy]\nspace = "s"\n')
    (root / "agent.py").write_text("from apx_agent import LlmAgent\nagent = LlmAgent(name='p', tools=[])\n")
    checks = {c.name: c for c in check_durable_readiness(root)}
    assert checks["Config"].status is Status.OK


def test_app_name_is_left_to_its_own_reader(tmp_path: Path) -> None:
    # [tool.apx.deploy] app_name (#986) names the Apps API App; it isn't an AgentConfig field.
    path = _project(tmp_path, '[tool.apx.deploy]\napp_name = "mcp-orders"\nhost = "apps"\n')
    config = _load_agent_config(pyproject_path=path)
    assert (config.name, config.host) == ("orders-agent", "apps")


def test_existing_apps_target_metadata_is_left_alone(tmp_path: Path) -> None:
    # Pre-3a [tool.apx.deploy] carries Apps-target metadata (target, bundle_name) read elsewhere.
    path = _project(tmp_path, '[tool.apx.deploy]\nname = "a"\ntarget = "apps"\n'
                              'bundle_name = "mcp-a"\napp_name = "mcp-a"\n')
    config = _load_agent_config(pyproject_path=path)
    assert config.name == "a" and config.deploy is None


def test_a_table_of_only_foreign_keys_declares_no_config(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.deploy]\napp_name = "mcp-x"\ntarget = "apps"\n')
    assert _load_agent_config(pyproject_path=path) is None


def test_every_in_repo_project_still_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    projects = sorted(p for p in root.glob("examples/**/pyproject.toml") if ".venv" not in p.parts)
    assert projects
    for path in projects:
        _load_agent_config(pyproject_path=path)  # raises on a regression
