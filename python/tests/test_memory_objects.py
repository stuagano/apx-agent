"""memory= / session= accept backend objects (sub-project 3b-3a)."""

from __future__ import annotations

import pydantic
import pytest

from apx_agent import Agent, DataAgent, MemoryBackendConfig, SessionBackendConfig
from apx_agent._models import AgentConfig
from apx_agent._settings import resolve_settings

_SESSION = {"type": "lakebase", "host": "${LAKEBASE_HOST}", "database": "orders"}
_MEMORY = {"type": "lakebase", "host": "${LAKEBASE_HOST}", "database": "orders",
           "table_name": "main.orders.apx_memory", "embedding_model": "databricks-bge-large-en",
           "embedding_dim": 1024}


def test_session_object_and_dict_give_the_same_config() -> None:
    by_model = Agent(name="o", tools=[], session=SessionBackendConfig(**_SESSION))
    by_dict = Agent(name="o", tools=[], session=_SESSION)
    assert by_model.session_config == by_dict.session_config == SessionBackendConfig(**_SESSION)


def test_memory_object_and_dict_give_the_same_config() -> None:
    assert (Agent(name="o", tools=[], memory=_MEMORY).memory_config
            == Agent(name="o", tools=[], memory=MemoryBackendConfig(**_MEMORY)).memory_config
            == MemoryBackendConfig(**_MEMORY))


def test_an_object_memory_invents_no_session() -> None:
    assert Agent(name="o", tools=[], memory=_MEMORY).session_config is None


def test_session_replaces_the_knobs_session_only() -> None:
    knob_only = Agent(name="o", tools=[], memory="managed")
    agent = Agent(name="o", tools=[], memory="managed", session=_SESSION)
    assert agent.memory_config == knob_only.memory_config
    assert agent.session_config == SessionBackendConfig(**_SESSION)


def test_a_typo_in_a_backend_dict_fails_at_construction() -> None:
    with pytest.raises(pydantic.ValidationError):
        Agent(name="o", tools=[], session={**_SESSION, "hots": "x"})


def test_python_declared_session_reaches_the_effective_config() -> None:
    config = resolve_settings(Agent(name="o", tools=[], target="responses_agent", session=_SESSION), None)
    assert config.session == SessionBackendConfig(**_SESSION)


def test_the_toml_session_wins_the_gap() -> None:
    toml = AgentConfig.model_validate({"name": "o", "target": "responses_agent",
                                       "session": {"type": "lakebase", "host": "h", "database": "toml"}})
    config = resolve_settings(Agent(name="o", tools=[], target="responses_agent", session=_SESSION), toml)
    assert config.session is not None and config.session.database == "toml"


def test_data_agent_passes_backends_through() -> None:
    agent = DataAgent("main", "orders", name="d", session=_SESSION)
    assert agent.session_config == SessionBackendConfig(**_SESSION)


def test_lakebase_knob_error_points_at_session_and_memory_objects() -> None:
    with pytest.raises(ValueError, match="session=SessionBackendConfig"):
        Agent(name="o", tools=[], memory="lakebase")


def test_an_auto_attached_managed_session_is_not_a_declaration() -> None:
    # A durable-by-default TOML gets a managed session filled in; that default must not
    # beat a session the agent declares (the deploy-only TOML of a migrated project).
    toml = AgentConfig.model_validate({"name": "o"})
    assert toml.session is not None and toml.session.type == "managed"
    assert "session" not in toml.model_fields_set
    agent = Agent(name="o", tools=[], target="responses_agent", session=_SESSION)
    assert resolve_settings(agent, toml).session == SessionBackendConfig(**_SESSION)


def test_a_knob_invented_session_never_beats_the_tomls_default() -> None:
    # memory="persistent" invents a Lakebase session; on a durable project the TOML's
    # auto-filled managed session must still win, as before 3b-3a.
    toml = AgentConfig.model_validate({"name": "o"})
    config = resolve_settings(Agent(name="o", tools=[], memory="persistent"), toml)
    assert config.session is not None and config.session.type == "managed"
