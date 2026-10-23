"""CLI metadata homes: [tool.apx] (dev loop) and [tool.apx.deploy] (registration) — sub-project 3b-1."""

from __future__ import annotations

import tomllib
from pathlib import Path

import click
import pytest

from apx_agent.cli import _read_apx_agent_config


def _write(tmp_path: Path, body: str, local: str | None = None) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "p"\nversion = "0"\n\n' + body)
    if local is not None:
        (tmp_path / ".apx.local").write_text(local)
    return path


@pytest.mark.parametrize(("body", "key", "value"), [
    ('[tool.apx]\nprofile = "dev"\n', "profile", "dev"),
    ('[tool.apx]\nexperiment = "/Users/x/exp"\n', "experiment", "/Users/x/exp"),
    ('[tool.apx.deploy]\nregistered_model = "c.s.m"\n', "registered_model", "c.s.m"),
    ('[tool.apx.deploy]\nsupervisor_id = "sup-1"\n', "supervisor_id", "sup-1"),
    ('[tool.apx.agent]\nregistered_model = "c.s.m"\n', "registered_model", "c.s.m"),
])
def test_each_key_resolves_from_its_new_or_old_home(tmp_path: Path, body: str, key: str, value: str) -> None:
    assert _read_apx_agent_config(_write(tmp_path, body))[key] == value


def test_apx_local_wins_and_is_read_from_both_layouts(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx]\nprofile = "committed"\n', local='[tool.apx]\nprofile = "mine"\n')
    assert _read_apx_agent_config(path)["profile"] == "mine"
    (tmp_path / ".apx.local").write_text('[tool.apx.agent]\nprofile = "legacy-local"\n')
    assert _read_apx_agent_config(path)["profile"] == "legacy-local"


def test_new_home_overrides_the_old_table_only_when_equal(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx.agent]\nprofile = "a"\n\n[tool.apx]\nprofile = "a"\n')
    assert _read_apx_agent_config(path)["profile"] == "a"
    path = _write(tmp_path, '[tool.apx.agent]\nprofile = "a"\n\n[tool.apx]\nprofile = "b"\n')
    with pytest.raises(click.ClickException,
                       match=r"profile is declared in both \[tool.apx.agent\] and \[tool.apx\] with different values; keep \[tool.apx\]."):
        _read_apx_agent_config(path)


def test_older_deploy_keys_never_reach_the_dict(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx.agent]\ntarget = "durable_agent_server"\n\n'
                            '[tool.apx.deploy]\ntarget = "apps"\nbundle_name = "b"\napp_name = "a"\nunknown = 1\n')
    config = _read_apx_agent_config(path)
    assert config == {"target": "durable_agent_server"}


def test_malformed_apx_local_reads_as_empty(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx]\nprofile = "dev"\n', local="not = [valid toml")
    assert _read_apx_agent_config(path)["profile"] == "dev"


def test_every_in_repo_example_reads_the_same_as_its_agent_table() -> None:
    root = Path(__file__).resolve().parents[1]
    for path in sorted(p for p in root.glob("examples/**/pyproject.toml") if ".venv" not in p.parts):
        legacy = tomllib.loads(path.read_text()).get("tool", {}).get("apx", {}).get("agent", {})
        assert _read_apx_agent_config(path) == legacy, path


def test_profile_picker_writes_apx_local_tool_apx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent.cli import _save_profile_to_pyproject

    _write(tmp_path, "")
    monkeypatch.chdir(tmp_path)
    assert _save_profile_to_pyproject('dev "quoted"') is True
    local = tomllib.loads((tmp_path / ".apx.local").read_text())
    assert local == {"tool": {"apx": {"profile": 'dev "quoted"'}}}


def test_supervisor_id_lands_in_tool_apx_deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent.cli import _save_to_pyproject

    _write(tmp_path, '[tool.apx.deploy]\napp_name = "mcp-x"\n')
    monkeypatch.chdir(tmp_path)
    assert _save_to_pyproject("supervisor_id", "sup-1") is True
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert data["tool"]["apx"]["deploy"] == {"app_name": "mcp-x", "supervisor_id": "sup-1"}


def test_writer_never_touches_a_same_named_key_in_another_table(tmp_path: Path) -> None:
    from apx_agent.cli import _set_toml_key

    path = _write(tmp_path, '[tool.apx]\nprofile = "old"\n\n[tool.apx.metadata]\nprofile = "keep"\n\n[tool.other]\nprofile = "keep"\n')
    _set_toml_key(path, "tool.apx", "profile", "new")
    data = tomllib.loads(path.read_text())
    assert data["tool"]["apx"]["profile"] == "new"
    assert data["tool"]["apx"]["metadata"] == {"profile": "keep"}
    assert data["tool"]["other"] == {"profile": "keep"}


def test_writer_adds_to_an_existing_section_with_other_keys(tmp_path: Path) -> None:
    from apx_agent.cli import _set_toml_key

    path = _write(tmp_path, '[tool.apx]\nexperiment = "/e"\n')
    _set_toml_key(path, "tool.apx", "profile", "dev")
    assert tomllib.loads(path.read_text())["tool"]["apx"] == {"experiment": "/e", "profile": "dev"}
