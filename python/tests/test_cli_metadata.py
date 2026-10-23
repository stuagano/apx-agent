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
