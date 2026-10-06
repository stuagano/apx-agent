"""Tests for the declarative example workflow contract."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from apx_agent._models import (
    AgentConfig,
    ExampleWorkflow,
    normalize_workflows,
    workflow_prompts,
    workflows_for_context,
)


def test_managed_store_defaults_are_stable_and_do_not_mutate_shared_config() -> None:
    from apx_agent._models import MemoryBackendConfig, SessionBackendConfig, normalize_memory_knob

    memory = MemoryBackendConfig(type="managed")
    session = SessionBackendConfig(type="managed")
    orders = AgentConfig(name="orders", target="durable_agent_server", memory=memory, session=session)
    billing = AgentConfig(name="billing", target="durable_agent_server", memory=memory, session=session)
    assert orders.memory.store_name == "apx-orders-memory"
    assert orders.session.store_name == "apx-orders-sessions"
    assert billing.memory.store_name == "apx-billing-memory"
    assert billing.session.store_name == "apx-billing-sessions"
    assert memory.store_name is None and session.store_name is None
    assert AgentConfig.model_validate(orders.model_dump()) == orders
    assert normalize_memory_knob("managed", name="orders")[0].store_name == orders.memory.store_name


def test_explicit_managed_store_names_survive_agent_rename() -> None:
    config = AgentConfig(name="renamed", target="durable_agent_server",
                         memory={"type": "managed", "store_name": "shared-memory"},
                         session={"type": "managed", "store_name": "original-sessions"})
    assert config.memory.store_name == "shared-memory"
    assert config.session.store_name == "original-sessions"
    assert config.session.auto_create is False


@pytest.mark.parametrize("name", ["Orders_Team", "a" * 100])
def test_managed_store_defaults_follow_existing_slug_and_length_rules(name: str) -> None:
    from apx_agent._memory_managed import validate_memory_store_name

    config = AgentConfig(name=name, target="durable_agent_server",
                         memory={"type": "managed"}, session={"type": "managed"})
    for backend in (config.memory, config.session):
        assert validate_memory_store_name(backend.store_name) == backend.store_name
    assert config.memory.store_name.endswith("-memory")
    assert config.session.store_name.endswith("-sessions")


@pytest.mark.parametrize("store_name", ["", " "])
def test_explicit_blank_session_store_does_not_become_an_automatic_binding(store_name: str) -> None:
    with pytest.raises(ValidationError, match="store_name"):
        AgentConfig(name="orders", target="durable_agent_server",
                    session={"type": "managed", "store_name": store_name})


def _workflow(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": "pricing-review",
        "title": "Pricing review",
        "question": "Show me the pricing evidence",
        "purpose": "Move from signal to decision.",
        "route": ["intelligence", "calibrate"],
    }
    value.update(overrides)
    return value


def test_workflow_config_round_trips_and_merges_prompts() -> None:
    config = AgentConfig.model_validate(
        {
            "name": "demo",
            "examples": ["Show me the pricing evidence"],
            "workflows": [
                {
                    **_workflow(),
                    "outcome": "Reviewable pricing packet",
                }
            ],
        }
    )

    assert config.model_dump()["workflows"][0]["id"] == "pricing-review"
    assert config.workflows[0].handoffs == []
    assert workflow_prompts(config) == ["Show me the pricing evidence"]


def test_workflow_rejects_blank_route_stage() -> None:
    with pytest.raises(ValidationError, match="route"):
        AgentConfig.model_validate({"name": "demo", "workflows": [_workflow(route=[""])]})


def test_workflow_rejects_blank_tuple_route_stage() -> None:
    with pytest.raises(ValidationError, match="route"):
        AgentConfig.model_validate({"name": "demo", "workflows": [_workflow(route=(" ",))]})


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", ""), ("question", " "), ("title", "\t"), ("purpose", "\n")],
)
def test_workflow_rejects_blank_required_string(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        AgentConfig.model_validate({"name": "demo", "workflows": [_workflow(**{field: value})]})


def test_workflow_rejects_blank_handoff_string() -> None:
    with pytest.raises(ValidationError, match="source"):
        ExampleWorkflow(
            **_workflow(
                handoffs=[
                    {
                        "source": " ",
                        "target": "calibrate",
                        "input_contract": "evidence",
                        "output_contract": "packet",
                        "explanation": "Pass evidence to calibration.",
                    }
                ]
            )
        )


def test_normalize_workflows_accepts_declared_shapes() -> None:
    declared = _workflow()
    workflow = ExampleWorkflow.model_validate(declared)

    assert normalize_workflows(None) == []
    assert normalize_workflows(declared) == [workflow]
    assert normalize_workflows([declared, workflow]) == [workflow, workflow]


def test_workflows_for_context_prefers_config_then_uses_agent_hook() -> None:
    configured = ExampleWorkflow.model_validate(_workflow())
    attached = ExampleWorkflow.model_validate(_workflow(id="attached"))

    configured_ctx = SimpleNamespace(
        config=AgentConfig(name="demo", workflows=[configured]),
        agent=SimpleNamespace(__apx_workflows__=[attached]),
    )
    attached_ctx = SimpleNamespace(
        config=AgentConfig(name="demo"),
        agent=SimpleNamespace(__apx_workflows__=[attached.model_dump()]),
    )

    assert workflows_for_context(configured_ctx) == [configured]
    assert workflows_for_context(attached_ctx) == [attached]


def test_workflow_prompts_deduplicate_in_declaration_order() -> None:
    config = AgentConfig(
        name="demo",
        examples=["first", "first", "second"],
        workflows=[
            ExampleWorkflow.model_validate(_workflow(question="second")),
            ExampleWorkflow.model_validate(_workflow(id="next", question="third")),
            ExampleWorkflow.model_validate(_workflow(id="last", question="first")),
        ],
    )

    assert workflow_prompts(config) == ["first", "second", "third"]


def test_workflow_prompts_accepts_resolved_workflows() -> None:
    config = AgentConfig(
        name="demo",
        examples=["first", "attached"],
        workflows=[ExampleWorkflow.model_validate(_workflow(question="configured"))],
    )
    attached = [
        ExampleWorkflow.model_validate(_workflow(id="attached", question="attached")),
        ExampleWorkflow.model_validate(_workflow(id="next", question="next")),
    ]

    assert workflow_prompts(config, attached) == ["first", "attached", "next"]
