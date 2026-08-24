"""Credential-free tests for the verified native Service Policy boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from apx_agent._service_policies import (
    AbacSelector,
    ServicePolicyCapabilityError,
    ServicePoliciesConfig,
    ServicePolicyTargetType,
)
from apx_agent._service_policies_native import (
    NativePolicyCapability,
    NativePolicyTransport,
    apply_native_policy_plan,
    build_native_policy_plan,
    default_native_capabilities,
    verify_native_policy_plan,
)
from apx_agent.cli import main


FIXTURE = Path(__file__).parent / "fixtures" / "service_policy_capabilities.json"
EXPECTED_TARGETS = {"mcp_service", "model_service", "model_provider_service"}
EXPECTED_KINDS = {"builtin", "llm_judge", "sql"}
EXPECTED_PHASES = {"on_call", "on_result"}


def _capabilities() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def test_capability_fixture_is_workspace_independent() -> None:
    capabilities = _capabilities()

    assert capabilities["schema_version"] == 1
    assert capabilities["native_apply"] is False
    assert capabilities["native_verify"] is False
    assert capabilities["abac_attachment"] is False
    assert set(capabilities["targets"]) == EXPECTED_TARGETS


def test_capability_fixture_describes_every_beta_target() -> None:
    capabilities = _capabilities()

    for target in capabilities["targets"].values():
        assert set(target["kinds"]) == EXPECTED_KINDS
        assert set(target["phases"]) == EXPECTED_PHASES
        assert set(target["ask_phases"]).issubset(set(target["phases"]))


def test_capability_fixture_contains_no_credentials_or_payloads() -> None:
    serialized = FIXTURE.read_text()

    assert "token" not in serialized.lower()
    assert "password" not in serialized.lower()
    assert "prompt" not in serialized.lower()
    assert "sql_body" not in serialized.lower()


def _policy_config(*, native_mode: str = "plan", target_type: str = "mcp_service") -> ServicePoliciesConfig:
    return ServicePoliciesConfig.model_validate({
        "native_mode": native_mode,
        "attachments": [{
            "name": "github-guardrails",
            "target_type": target_type,
            "target": "main.tools.github",
            "policies": [{
                "name": "review-write",
                "kind": "llm_judge",
                "classifier": "judge",
                "prompt": "never print this prompt in a plan",
                "phase": "on_call",
                "rank": 100,
            }],
        }],
    })


class _RecordingTransport(NativePolicyTransport):
    def __init__(self, *, mismatch: bool = False) -> None:
        self.applied: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        self.mismatch = mismatch

    def apply(self, operation: dict[str, Any], *, profile: str) -> dict[str, Any]:
        self.applied.append({"operation": operation, "profile": profile})
        return {"status": "accepted"}

    def read(self, operation: dict[str, Any], *, profile: str) -> dict[str, Any]:
        self.reads.append({"operation": operation, "profile": profile})
        return {"policy_name": "other" if self.mismatch else operation["policy_name"]}


def _fully_supported_capabilities() -> dict[ServicePolicyTargetType, NativePolicyCapability]:
    capabilities = default_native_capabilities()
    return {
        target: NativePolicyCapability(
            target_type=capability.target_type,
            kinds=capability.kinds,
            phases=capability.phases,
            ask_phases=capability.ask_phases,
            supports_apply=True,
            supports_verify=True,
        )
        for target, capability in capabilities.items()
    }


def test_native_plan_is_deterministic_sanitized_and_side_effect_free() -> None:
    config = _policy_config()
    first = build_native_policy_plan(config)
    second = build_native_policy_plan(config)

    assert first == second
    assert first.operations
    serialized = json.dumps(first.operations)
    assert "never print this prompt" not in serialized
    assert first.declaration_fingerprint


def test_native_plan_reports_unsupported_target_and_abac() -> None:
    config = _policy_config(target_type="agent_service")
    config = config.model_copy(update={
        "abac": AbacSelector(tags={"service_class": "customer-facing"}),
    })

    plan = build_native_policy_plan(config)

    assert any("target type agent_service" in reason for reason in plan.unsupported)
    assert any("ABAC" in reason for reason in plan.unsupported)


def test_required_mode_fails_when_capability_is_not_verified() -> None:
    with pytest.raises(ServicePolicyCapabilityError, match="apply"):
        build_native_policy_plan(_policy_config(native_mode="required"))


def test_fake_native_transport_applies_and_verifies() -> None:
    config = _policy_config(native_mode="apply")
    plan = build_native_policy_plan(config, capabilities=_fully_supported_capabilities())
    transport = _RecordingTransport()

    receipt = apply_native_policy_plan(plan, transport=transport, profile="test-profile")
    verification = verify_native_policy_plan(plan, transport=transport, profile="test-profile")

    assert receipt.applied[0]["status"] == "accepted"
    assert verification.complete is True
    assert transport.applied[0]["profile"] == "test-profile"
    assert transport.reads[0]["profile"] == "test-profile"


def test_native_apply_and_verify_require_explicit_profile() -> None:
    plan = build_native_policy_plan(
        _policy_config(native_mode="apply"),
        capabilities=_fully_supported_capabilities(),
    )
    transport = _RecordingTransport()

    with pytest.raises(ValueError, match="explicit Databricks profile"):
        apply_native_policy_plan(plan, transport=transport, profile="")
    with pytest.raises(ValueError, match="explicit Databricks profile"):
        verify_native_policy_plan(plan, transport=transport, profile="")
    assert transport.applied == []
    assert transport.reads == []


def test_native_verification_reports_mismatch() -> None:
    plan = build_native_policy_plan(
        _policy_config(native_mode="apply"),
        capabilities=_fully_supported_capabilities(),
    )

    verification = verify_native_policy_plan(
        plan,
        transport=_RecordingTransport(mismatch=True),
        profile="test-profile",
    )

    assert verification.complete is False
    assert verification.statuses[0]["status"] == "mismatch"


def test_cli_apply_requires_explicit_profile(tmp_path: Path) -> None:
    spec = tmp_path / "agent.yaml"
    spec.write_text(
        """name: cli-agent
service_policies:
  native_mode: plan
  attachments:
    - name: guard
      target_type: mcp_service
      target: main.tools.github
      policies: []
"""
    )

    result = CliRunner().invoke(main, ["agents", "policies", str(spec), "apply"])

    assert result.exit_code != 0
    assert "--profile" in result.output
