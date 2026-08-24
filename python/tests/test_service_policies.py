"""Contract tests for the portable Service Policy declaration."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from apx_agent._service_policies import (
    NativePolicyMode,
    ServicePolicy,
    ServicePolicyAction,
    ServicePolicyDecision,
    ServicePolicyEvent,
    ServicePolicyMode,
    ServicePolicyPhase,
    ServicePoliciesConfig,
    evaluate_ordered_service_policies,
    ordered_policies,
)


def _config(**overrides: Any) -> ServicePoliciesConfig:
    value: dict[str, Any] = {
        "local_mode": "mirror",
        "native_mode": "plan",
        "attachments": [{
            "name": "mcp-guardrails",
            "target_type": "mcp_service",
            "target": "main.tools.github",
            "mode": "enforce",
            "policies": [{
                "name": "delete-approval",
                "kind": "llm_judge",
                "classifier": "databricks-claude-haiku-4-5",
                "prompt": "Require approval for destructive writes.",
                "phase": "on_call",
                "rank": 100,
            }],
        }],
    }
    value.update(overrides)
    return ServicePoliciesConfig.model_validate(value)


def test_valid_service_policy_declaration() -> None:
    config = _config()

    assert config.attachments[0].policies[0].rank == 100
    assert config.attachments[0].target_type.value == "mcp_service"
    assert config.native_mode is NativePolicyMode.PLAN


def test_unknown_fields_fail_closed() -> None:
    with pytest.raises(ValidationError):
        ServicePoliciesConfig.model_validate({"unexpected": True})


@pytest.mark.parametrize("policy", [
    {"name": "missing-classifier", "kind": "llm_judge", "phase": "on_call"},
    {"name": "missing-prompt", "kind": "llm_judge", "classifier": "judge", "phase": "on_call"},
    {"name": "missing-function", "kind": "sql", "phase": "on_call"},
    {"name": "missing-builtin", "kind": "builtin", "phase": "on_call"},
])
def test_policy_kind_requires_its_reference(policy: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _config(attachments=[{
            "name": "invalid",
            "target_type": "mcp_service",
            "target": "main.tools.github",
            "policies": [policy],
        }])


def test_unknown_target_and_mode_fail_closed() -> None:
    with pytest.raises(ValidationError):
        _config(attachments=[{
            "name": "invalid-target",
            "target_type": "unknown_service",
            "target": "x",
            "policies": [],
        }])
    with pytest.raises(ValidationError):
        _config(attachments=[{
            "name": "invalid-mode",
            "target_type": "mcp_service",
            "target": "x",
            "mode": "observe",
            "policies": [],
        }])


def test_invalid_rank_and_builtin_phase_fail_closed() -> None:
    with pytest.raises(ValidationError):
        _config(attachments=[{
            "name": "negative-rank",
            "target_type": "mcp_service",
            "target": "x",
            "policies": [{
                "name": "bad-rank",
                "kind": "builtin",
                "builtin": "sensitive_data",
                "phase": "on_call",
                "rank": -1,
            }],
        }])
    with pytest.raises(ValidationError):
        _config(attachments=[{
            "name": "bad-jailbreak-phase",
            "target_type": "mcp_service",
            "target": "x",
            "policies": [{
                "name": "jailbreak",
                "kind": "builtin",
                "builtin": "jailbreak",
                "phase": "on_result",
            }],
        }])


def test_both_phase_is_serializable_and_abac_is_desired_state() -> None:
    config = _config(
        abac={"tags": {"service_class": "customer-facing"}},
        attachments=[{
            "name": "sensitive",
            "target_type": "mcp_service",
            "target": "main.tools.github",
            "policies": [{
                "name": "sensitive",
                "kind": "builtin",
                "builtin": "sensitive_data",
                "phase": "both",
            }],
        }],
    )

    dumped = config.model_dump(mode="json", exclude_none=True)
    assert dumped["abac"] == {"tags": {"service_class": "customer-facing"}}
    assert dumped["attachments"][0]["policies"][0]["phase"] == "both"


def _policy(name: str, rank: int, phase: str = "on_call") -> ServicePolicy:
    return ServicePolicy(
        name=name,
        kind="builtin",
        builtin="sensitive_data" if phase == "both" else "jailbreak",
        phase=phase,
        rank=rank,
    )


def test_ordered_policies_ascend_on_call_and_descend_on_result() -> None:
    policies = [_policy("low", 10), _policy("high", 100), _policy("both", 50, "both")]

    assert [p.name for p in ordered_policies(policies, ServicePolicyPhase.ON_CALL)] == [
        "low", "both", "high",
    ]
    assert [p.name for p in ordered_policies(
        [p.model_copy(update={"phase": "on_result"}) for p in policies],
        ServicePolicyPhase.ON_RESULT,
    )] == ["high", "both", "low"]


def test_ordered_evaluation_stops_at_first_deny() -> None:
    policies = [_policy("first", 10), _policy("blocked", 20), _policy("never", 30)]
    seen: list[str] = []

    def evaluate(policy: ServicePolicy, event: ServicePolicyEvent) -> ServicePolicyAction:
        seen.append(policy.name)
        return ServicePolicyAction.DENY if policy.name == "blocked" else ServicePolicyAction.ALLOW

    decision = evaluate_ordered_service_policies(
        policies,
        ServicePolicyEvent(
            phase="on_call",
            target_type="mcp_service",
            target="main.tools.github",
        ),
        mode=ServicePolicyMode.ENFORCE,
        adapter="local",
        evaluator=evaluate,
    )

    assert seen == ["first", "blocked"]
    assert decision == ServicePolicyDecision(
        action="DENY",
        reason=None,
        policy_name="blocked",
        phase="on_call",
        rank=20,
        mode=ServicePolicyMode.ENFORCE,
        adapter="local",
    )


def test_ordered_evaluation_fails_closed_and_marks_dry_run() -> None:
    policy = _policy("broken", 10)
    event = ServicePolicyEvent(
        phase="on_call",
        target_type="model_service",
        target="main.models.chat",
    )

    def broken(policy: ServicePolicy, event: ServicePolicyEvent) -> None:
        raise RuntimeError("evaluator unavailable")

    enforced = evaluate_ordered_service_policies(
        [policy], event, mode=ServicePolicyMode.ENFORCE, adapter="local", evaluator=broken,
    )
    dry_run = evaluate_ordered_service_policies(
        [policy], event, mode=ServicePolicyMode.DRY_RUN, adapter="local", evaluator=broken,
    )

    assert enforced.action == "DENY"
    assert dry_run.action == "UNAVAILABLE"
    assert dry_run.reason is not None
    assert "evaluator unavailable" in dry_run.reason
