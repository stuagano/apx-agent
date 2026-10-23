"""[tool.apx.deploy]: deploy settings outside [tool.apx.agent] (sub-project 3a)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apx_agent._inspection import _load_agent_config


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


def test_agent_table_name_is_not_overridden_by_the_default(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.agent]\nname = "legacy"\n\n[tool.apx.deploy]\nspace = "s"\n')
    config = _load_agent_config(pyproject_path=path)
    assert (config.name, config.deploy.space) == ("legacy", "s")


def test_equal_values_in_both_tables_pass(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.agent]\nname = "a"\n\n[tool.apx.agent.deploy]\nspace = "s"\n\n'
                              '[tool.apx.deploy]\nname = "a"\nspace = "s"\n')
    config = _load_agent_config(pyproject_path=path)
    assert (config.name, config.deploy.space) == ("a", "s")


@pytest.mark.parametrize(("body", "message"), [
    ('[tool.apx.agent]\nname = "a"\n\n[tool.apx.deploy]\nname = "b"\n',
     r"name is declared in both \[tool.apx.agent\] and \[tool.apx.deploy\] with different values; keep \[tool.apx.deploy\]."),
    ('[tool.apx.agent]\nname = "a"\n\n[tool.apx.agent.deploy]\nspace = "x"\n\n[tool.apx.deploy]\nspace = "y"\n',
     r"deploy.space is declared in both \[tool.apx.agent.deploy\] and \[tool.apx.deploy\] with different values; keep \[tool.apx.deploy\]."),
])
def test_different_values_fail(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _load_agent_config(pyproject_path=_project(tmp_path, body))


def test_neither_table_is_none(tmp_path: Path) -> None:
    assert _load_agent_config(pyproject_path=_project(tmp_path, "")) is None


def test_empty_agent_table_with_deploy_table_is_deploy_only(tmp_path: Path) -> None:
    path = _project(tmp_path, '[tool.apx.agent]\n\n[tool.apx.deploy]\nspace = "s"\n')
    config = _load_agent_config(pyproject_path=path)
    assert (config.name, config.deploy.space) == ("orders-agent", "s")


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
