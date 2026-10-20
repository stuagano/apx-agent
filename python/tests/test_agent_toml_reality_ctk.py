from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from ctk import Artifact, verify

from apx_agent._agent_toml import load_agent_toml, write_agent_toml
from apx_agent._agents import LlmAgent
from apx_agent._apps_authorization import AuthorizationPlan
from apx_agent._durable_agent import build_native_manifest
from apx_agent._models import AgentConfig


def _plan(scopes: tuple[str, ...]) -> AuthorizationPlan:
    return AuthorizationPlan(operations=(), user_resources=(), service_resources=(),
                             user_api_scopes=scopes, app_dependencies=())


def test_migrate_round_trips_through_the_sdk(tmp_path: Path) -> None:
    config = AgentConfig.model_validate({
        "name": "p", "model": "m", "target": "durable_agent_server",
        "memory": {"type": "managed", "store_name": "mem"},
    })
    path = write_agent_toml(tmp_path, config, _plan(("sql",)))
    verify(Artifact(str(path), min_bytes=60, must_contain="[memory_store]"))
    project = load_agent_toml(tmp_path)
    assert project is not None
    assert project.memory_store == "mem"
    assert project.user_auth.additional_api_scopes == ("sql",)


def test_second_run_refuses_and_leaves_file_untouched(tmp_path: Path) -> None:
    config = AgentConfig.model_validate({"name": "p", "model": "m", "target": "durable_agent_server"})
    path = write_agent_toml(tmp_path, config, _plan(()))
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_agent_toml(tmp_path, config, _plan(("sql",)))
    assert path.read_bytes() == before


def test_migrated_auth_user_matches_the_generated_manifest(tmp_path: Path) -> None:
    config = AgentConfig.model_validate({
        "name": "p", "model": "m", "target": "durable_agent_server",
        "memory": {"type": "managed", "store_name": "mem"},
    })
    plan = _plan(("sql", "ai-gateway"))
    generated = build_native_manifest(config=config, authorization_plan=plan,
                                      agent=LlmAgent())
    migrated = write_agent_toml(tmp_path, config, plan)
    assert tomllib.loads(migrated.read_text())["auth"]["user"] == tomllib.loads(generated)["auth"]["user"]
