"""resolve_settings: the one merge of agent-declared settings with [tool.apx.agent]."""

from __future__ import annotations

import pytest

from apx_agent import Agent, SequentialAgent
from apx_agent._models import AgentConfig
from apx_agent._settings import resolve_settings


def _noop(q: str) -> str:
    """Echo."""
    return q


def _leaf(name: str = "leaf", **settings) -> Agent:
    return Agent(name=name, tools=[_noop], **settings)


def _toml(**fields) -> AgentConfig:
    return AgentConfig.model_validate({"name": "app", **fields})


def test_python_only_builds_config_from_root() -> None:
    root = _leaf("triage", model="m", identity="obo", recovery=True, memory="managed")
    config = resolve_settings(root, None)
    assert (config.name, config.model, config.identity, config.recovery) == ("triage", "m", "obo", True)
    assert config.target == "durable_agent_server"
    assert config.memory is not None and config.memory.store_name == root.memory_config.store_name


def test_toml_only_is_unchanged() -> None:
    toml = _toml(model="m", identity="obo")
    assert resolve_settings(_leaf(), toml) == toml


def test_equal_values_in_both_places_pass() -> None:
    assert resolve_settings(_leaf(model="m"), _toml(model="m")).model == "m"


def test_python_fills_fields_toml_leaves_unset() -> None:
    config = resolve_settings(_leaf(identity="obo"), _toml(model="m"))
    assert (config.model, config.identity, config.name) == ("m", "obo", "app")


def test_conflict_with_toml_fails() -> None:
    with pytest.raises(ValueError, match=r"model: agent.py sets 'a' on agent 'root'; \[tool.apx.agent\] sets 'b'. Set it in one place."):
        resolve_settings(_leaf("root", model="a"), _toml(model="b"))


def test_app_wide_field_below_root_must_match() -> None:
    same = SequentialAgent([_leaf("researcher", target="durable_agent_server")], target="durable_agent_server")
    resolve_settings(same, None)
    with pytest.raises(ValueError, match=r"target is app-wide; set it on the root agent \(found on 'researcher'\)."):
        resolve_settings(SequentialAgent([_leaf("researcher", target="responses_agent")]), None)


def test_per_agent_values_inherit_and_only_differences_are_overrides() -> None:
    inner = _leaf("inner")
    middle = SequentialAgent([inner], model="special", identity="obo")
    plain = _leaf("plain")
    root = SequentialAgent([middle, plain], model="app")
    config = resolve_settings(root, None)
    assert config.model == "app"
    assert dict(inner._overrides) == {"model": "special", "identity": "obo"}
    assert dict(plain._overrides) == {}
    assert dict(root._overrides) == {}


def test_resolve_twice_is_identical() -> None:
    inner = _leaf("inner", model="special")
    root = SequentialAgent([inner], model="app", target="durable_agent_server")
    first = resolve_settings(root, _toml(model="app"))
    assert resolve_settings(root, _toml(model="app")) == first
    assert dict(inner._overrides) == {"model": "special"}


def test_shared_leaf_is_visited_once() -> None:
    shared = _leaf("shared", target="durable_agent_server")
    root = SequentialAgent([shared, shared], target="durable_agent_server")
    assert resolve_settings(root, None).target == "durable_agent_server"
