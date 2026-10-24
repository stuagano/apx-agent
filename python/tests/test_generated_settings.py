"""Generated agent.py settings (sub-project 3b-3b)."""

from __future__ import annotations

import pytest

from apx_agent import Agent, SequentialAgent
from apx_agent._models import AgentConfig, ServicePoliciesConfig
from apx_agent._settings import resolve_settings


def test_dict_settings_become_models() -> None:
    agent = Agent(name="a", tools=[], service_policies={"local_mode": "mirror", "native_mode": "off"})
    assert isinstance(agent.service_policies, ServicePoliciesConfig)


@pytest.mark.parametrize(("key", "value"), [
    ("workflows", [{"id": "w", "title": "W", "question": "q?", "purpose": "p", "route": ["a"]}]),
    ("executor", "langgraph"),
    ("data", {"tables": []}),
])
def test_yaml_only_fields_are_app_wide_settings(key: str, value) -> None:
    config = resolve_settings(Agent(name="a", tools=[], **{key: value}), None)
    assert getattr(config, key) == getattr(AgentConfig.model_validate({"name": "x", key: value}), key)


def test_composite_roots_take_a_description() -> None:
    root = SequentialAgent([Agent(name="leaf", tools=[])], description="Routes orders.")
    assert resolve_settings(root, None).description == "Routes orders."


def test_description_toml_wins_without_conflict() -> None:
    toml = AgentConfig.model_validate({"name": "a", "description": "from toml"})
    assert resolve_settings(Agent(name="a", tools=[], description="from python"), toml).description == "from toml"


def test_an_empty_description_is_not_a_declaration() -> None:
    assert "description" not in resolve_settings(Agent(name="a", tools=[]), None).model_fields_set


def test_loader_takes_target_from_agent_py_when_toml_omits_it(tmp_path) -> None:
    from apx_agent._inspection import _load_agent_config

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n\n[tool.apx.deploy]\nhost = "apps"\n')
    (tmp_path / "agent.py").write_text("from apx_agent import Agent\nagent = Agent(name='p', target='responses_agent', tools=[])\n")
    assert _load_agent_config(pyproject_path=tmp_path / "pyproject.toml").target == "responses_agent"


import sys
import tomllib
from pathlib import Path

from apx_agent._project_gen import generate_project

_SPECS = {
    "plain": {"name": "plain", "model": "system.ai.claude-sonnet-4-6", "instructions": "Be brief.",
              "description": "A plain agent.", "examples": ["Hi"], "identity": "obo"},
    "coworker": {"name": "cw", "model": "m", "template": {"name": "coworker", "catalog": "main", "schema": "hr"}},
    "responses": {"name": "resp", "model": "m", "target": "responses_agent",
                  "session": {"type": "lakebase", "host": "${LAKEBASE_HOST}", "database": "resp"}},
    "policies": {"name": "pol", "model": "m",
                 "service_policies": {"local_mode": "mirror", "native_mode": "off"},
                 "bindings": {"wh": "warehouse-id"}},
}


def _load_generated(root: Path, monkeypatch):
    import importlib.util

    from apx_agent._inspection import _load_agent_config

    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location("agent", root / "agent.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "agent", module)
    spec.loader.exec_module(module)
    return module.agent


@pytest.mark.parametrize("key", sorted(_SPECS))
def test_generated_project_reads_back_as_its_spec(tmp_path: Path, monkeypatch, key: str) -> None:
    from ctk import Artifact, verify

    spec = AgentConfig.model_validate(_SPECS[key])
    generate_project(spec, tmp_path)
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert "agent" not in data["tool"]["apx"], "generated pyproject must not declare [tool.apx.agent]"
    verify(Artifact(str(tmp_path / "agent.py"), min_bytes=40, must_contain=f"name={spec.name!r}"))
    from apx_agent._inspection import _load_agent_config
    from apx_agent._migrate_config import _CONSTRUCTOR

    agent = _load_generated(tmp_path, monkeypatch)
    effective = resolve_settings(agent, _load_agent_config(pyproject_path=tmp_path / "pyproject.toml"))
    for field in spec.model_fields_set - {"template", "tools", "skills", "agents", "root"}:
        # Constructor arguments live on the agent; everything else is in the effective config.
        actual = getattr(agent, _CONSTRUCTOR[field]) if field in _CONSTRUCTOR else getattr(effective, field)
        assert actual == getattr(spec, field), field


def test_graph_root_rejects_llm_only_fields(tmp_path: Path) -> None:
    spec = AgentConfig.model_validate({
        "name": "g", "model": "m", "instructions": "top-level",
        "agents": {"a": {"type": "agent", "instructions": "leaf", "description": "Leaf."}},
        "root": {"type": "sequential", "agents": ["a"]},
    })
    with pytest.raises(ValueError, match="instructions can't go on a graph root; set it on a leaf in agents:"):
        generate_project(spec, tmp_path)
