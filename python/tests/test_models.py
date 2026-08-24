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
