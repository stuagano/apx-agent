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
])
def test_each_key_resolves_from_its_home(tmp_path: Path, body: str, key: str, value: str) -> None:
    assert _read_apx_agent_config(_write(tmp_path, body))[key] == value


def test_the_legacy_table_is_rejected_unless_migrate_config_reads_it(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx.agent]\nregistered_model = "c.s.m"\n')
    with pytest.raises(click.ClickException, match="migrate-config"):
        _read_apx_agent_config(path)
    assert _read_apx_agent_config(path, allow_agent_table=True)["registered_model"] == "c.s.m"


def test_apx_local_wins_and_a_legacy_local_table_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx]\nprofile = "committed"\n', local='[tool.apx]\nprofile = "mine"\n')
    assert _read_apx_agent_config(path)["profile"] == "mine"
    (tmp_path / ".apx.local").write_text('[tool.apx.agent]\nprofile = "legacy-local"\n')
    with pytest.raises(click.ClickException, match="migrate-config"):
        _read_apx_agent_config(path)
    assert _read_apx_agent_config(path, allow_agent_table=True)["profile"] == "legacy-local"


def test_migrate_config_reads_the_old_table_and_rejects_a_conflict_with_the_new_home(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx.agent]\nprofile = "a"\n\n[tool.apx]\nprofile = "a"\n')
    assert _read_apx_agent_config(path, allow_agent_table=True)["profile"] == "a"
    with pytest.raises(click.ClickException, match="migrate-config"):
        _read_apx_agent_config(path)
    path = _write(tmp_path, '[tool.apx.agent]\nprofile = "a"\n\n[tool.apx]\nprofile = "b"\n')
    with pytest.raises(click.ClickException,
                       match=r"profile is declared in both \[tool.apx.agent\] and \[tool.apx\] with different values; keep \[tool.apx\]."):
        _read_apx_agent_config(path, allow_agent_table=True)


def test_older_deploy_keys_never_reach_the_dict(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx.deploy]\ntarget = "apps"\nbundle_name = "b"\napp_name = "a"\nunknown = 1\n')
    assert _read_apx_agent_config(path) == {}


def test_malformed_apx_local_reads_as_empty(tmp_path: Path) -> None:
    path = _write(tmp_path, '[tool.apx]\nprofile = "dev"\n', local="not = [valid toml")
    assert _read_apx_agent_config(path)["profile"] == "dev"


def test_every_in_repo_example_reads_its_metadata_from_the_new_homes() -> None:
    from apx_agent._inspection import DEV_KEYS, REGISTRATION_KEYS

    root = Path(__file__).resolve().parents[1]
    for path in sorted(p for p in root.glob("examples/**/pyproject.toml") if ".venv" not in p.parts):
        apx = tomllib.loads(path.read_text()).get("tool", {}).get("apx", {})
        deploy = apx.get("deploy", {})
        expected = {**{k: apx[k] for k in DEV_KEYS if k in apx},
                    **{k: deploy[k] for k in REGISTRATION_KEYS if k in deploy}}
        assert _read_apx_agent_config(path) == expected, path


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


def test_saving_a_key_still_in_the_legacy_table_updates_it_there(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent.cli import _save_profile_to_pyproject, _save_to_pyproject

    _write(tmp_path, '[tool.apx.agent]\nsupervisor_id = "old"\n', local='[tool.apx.agent]\nprofile = "old"\n')
    monkeypatch.chdir(tmp_path)
    assert _save_to_pyproject("supervisor_id", "new") and _save_profile_to_pyproject("new")
    flat = _read_apx_agent_config(tmp_path / "pyproject.toml", allow_agent_table=True)  # no conflict
    assert (flat["supervisor_id"], flat["profile"]) == ("new", "new")


@pytest.mark.parametrize("header", ["[tool.apx.deploy]  # deploy", "[ tool.apx.deploy ]"])
def test_writer_recognises_commented_and_spaced_headers(tmp_path: Path, header: str) -> None:
    from apx_agent.cli import _set_toml_key

    path = _write(tmp_path, f'{header}\napp_name = "a"\n')
    _set_toml_key(path, "tool.apx.deploy", "supervisor_id", "s")
    assert tomllib.loads(path.read_text())["tool"]["apx"]["deploy"] == {"app_name": "a", "supervisor_id": "s"}


def test_writer_refuses_to_write_invalid_toml(tmp_path: Path) -> None:
    from apx_agent.cli import _set_toml_key

    body = '[tool.apx]\nnote = """\n[not a header]\n"""\nprofile = "x"\n'
    path = _write(tmp_path, body)
    original = path.read_text()
    with pytest.raises(click.ClickException, match="would not be valid TOML"):
        _set_toml_key(path, "tool.apx", "profile", "y")
    assert path.read_text() == original
