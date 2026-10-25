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
    with pytest.raises(ValueError, match=r"model: agent.py sets 'a' on agent 'root'; pyproject.toml sets 'b'. Set it in one place."):
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


from apx_agent import _compile
from apx_agent._compile import compile_to_langgraph
from apx_agent._tool_scope import get_scope
from apx_agent._wiring import finalize_agent


def _record_models(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = _compile._build_chat_databricks

    def build(model: str, **kwargs):
        seen.append(model)
        return real(model, **kwargs)

    monkeypatch.setattr(_compile, "_build_chat_databricks", build)
    return seen


def test_leaf_compiles_with_its_own_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record_models(monkeypatch)
    root = SequentialAgent([_leaf("a", model="special"), _leaf("b")], model="app")
    resolve_settings(root, None)
    compile_to_langgraph(root, ws=None, model="app")
    assert sorted(seen) == ["app", "special"]


def test_apx_model_pin_wins_for_inheriting_leaves(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record_models(monkeypatch)
    root = SequentialAgent([_leaf("a"), _leaf("b", model="special")], model="app")
    resolve_settings(root, None)
    compile_to_langgraph(root, ws=None, model="pinned")
    assert sorted(seen) == ["pinned", "special"]


def test_each_leafs_tools_get_its_own_identity() -> None:
    def tool_a(q: str) -> str:
        """A."""
        return q

    def tool_b(q: str) -> str:
        """B."""
        return q

    a = Agent(name="a", tools=[tool_a], identity="obo")
    b = Agent(name="b", tools=[tool_b])
    root = SequentialAgent([a, b], identity="sp", target="durable_agent_server", model="m")
    finalize_agent(root, resolve_settings(root, None))
    assert get_scope(a._tool_fns[0]).identity == "obo"
    assert get_scope(b._tool_fns[0]).identity == "sp"


def test_an_object_without_settings_reads_as_unset() -> None:
    toml = _toml(model="m")
    assert resolve_settings(object(), toml) is toml  # type: ignore[arg-type]


def test_toml_memory_wins_over_the_agents_bound_memory() -> None:
    # finalize_agent binds [tool.apx.agent].memory onto the agent; that is not a conflict.
    toml = _toml(memory={"type": "managed", "store_name": "toml-store"})
    root = _leaf(memory="managed")
    assert resolve_settings(root, toml).memory == toml.memory


def test_shared_agent_inheriting_different_identities_fails() -> None:
    from apx_agent._settings import SettingsConflict

    shared = _leaf("shared")
    root = SequentialAgent([SequentialAgent([shared], identity="obo"), SequentialAgent([shared], identity="sp")])
    with pytest.raises(SettingsConflict, match="'shared' is reached through parents that give it different identity"):
        resolve_settings(root, None)


def test_examples_round_trip_and_are_app_wide() -> None:
    from apx_agent._settings import SettingsConflict

    assert resolve_settings(_leaf(examples=["Top 5 customers"]), None).examples == ["Top 5 customers"]
    with pytest.raises(SettingsConflict, match="examples is app-wide"):
        resolve_settings(SequentialAgent([_leaf("child", examples=["x"])]), None)


def test_dev_ui_context_carries_python_examples() -> None:
    import asyncio

    from fastapi import FastAPI

    from apx_agent._wiring import setup_agent

    agent = _leaf("demo", examples=["Show me sample customers"])
    ctx = asyncio.run(setup_agent(FastAPI(), agent, config=AgentConfig(name="demo")))
    assert ctx is not None and ctx.config.examples == ["Show me sample customers"]
