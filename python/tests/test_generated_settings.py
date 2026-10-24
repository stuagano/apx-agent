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
