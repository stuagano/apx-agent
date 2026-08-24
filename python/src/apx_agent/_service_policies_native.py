"""Deterministic native Service Policy plans and explicit transport boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ._service_policies import (
    NativePolicyMode,
    ServicePolicyCapabilityError,
    ServicePolicyKind,
    ServicePolicyPhase,
    ServicePoliciesConfig,
    ServicePolicyTargetType,
)


@dataclass(frozen=True)
class NativePolicyCapability:
    target_type: ServicePolicyTargetType
    kinds: frozenset[ServicePolicyKind]
    phases: frozenset[ServicePolicyPhase]
    ask_phases: frozenset[ServicePolicyPhase]
    supports_apply: bool = False
    supports_verify: bool = False


@dataclass(frozen=True)
class NativePolicyPlan:
    native_mode: NativePolicyMode
    operations: list[dict[str, Any]]
    unsupported: list[str]
    declaration_fingerprint: str


@dataclass(frozen=True)
class NativePolicyApplyReceipt:
    declaration_fingerprint: str
    applied: list[dict[str, Any]]


@dataclass(frozen=True)
class NativePolicyVerification:
    declaration_fingerprint: str
    statuses: list[dict[str, Any]]
    complete: bool


class NativePolicyTransport(Protocol):
    def apply(self, operation: dict[str, Any], *, profile: str) -> dict[str, Any]: ...

    def read(self, operation: dict[str, Any], *, profile: str) -> dict[str, Any]: ...


class UnavailableNativePolicyTransport:
    """Transport used until a supported Databricks Service Policy API exists."""

    def apply(self, operation: dict[str, Any], *, profile: str) -> dict[str, Any]:
        raise ServicePolicyCapabilityError(
            "Databricks Service Policy apply is unavailable: the installed SDK "
            "has no Service Policy attachment client or verified REST endpoint."
        )

    def read(self, operation: dict[str, Any], *, profile: str) -> dict[str, Any]:
        raise ServicePolicyCapabilityError(
            "Databricks Service Policy verification is unavailable: no verified "
            "attachment read-back surface exists."
        )


def default_native_capabilities() -> dict[ServicePolicyTargetType, NativePolicyCapability]:
    """Return the credential-free capability record verified for this release."""

    kinds = frozenset(ServicePolicyKind)
    phases = frozenset({ServicePolicyPhase.ON_CALL, ServicePolicyPhase.ON_RESULT})
    return {
        target: NativePolicyCapability(
            target_type=target,
            kinds=kinds,
            phases=phases,
            ask_phases=(frozenset({ServicePolicyPhase.ON_CALL}) if target is ServicePolicyTargetType.MCP_SERVICE else frozenset()),
        )
        for target in (
            ServicePolicyTargetType.MCP_SERVICE,
            ServicePolicyTargetType.MODEL_SERVICE,
            ServicePolicyTargetType.MODEL_PROVIDER_SERVICE,
        )
    }


def _fingerprint(config: ServicePoliciesConfig) -> str:
    payload = json.dumps(
        config.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _phases(phase: ServicePolicyPhase) -> list[ServicePolicyPhase]:
    if phase is ServicePolicyPhase.BOTH:
        return [ServicePolicyPhase.ON_CALL, ServicePolicyPhase.ON_RESULT]
    return [phase]


def _safe_operation(attachment: Any, policy: Any, phase: ServicePolicyPhase) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "attachment": attachment.name,
        "target_type": attachment.target_type.value,
        "target": attachment.target,
        "policy_name": policy.name,
        "kind": policy.kind.value,
        "phase": phase.value,
        "rank": policy.rank,
        "mode": attachment.mode.value,
    }
    if policy.builtin is not None:
        operation["builtin"] = policy.builtin.value
    if policy.classifier is not None:
        operation["classifier"] = policy.classifier
    if policy.function is not None:
        operation["function"] = policy.function
    return operation


def build_native_policy_plan(
    config: ServicePoliciesConfig,
    *,
    capabilities: Mapping[ServicePolicyTargetType, NativePolicyCapability] | None = None,
) -> NativePolicyPlan:
    """Validate and emit a deterministic, side-effect-free native plan."""

    fingerprint = _fingerprint(config)
    if config.native_mode is NativePolicyMode.OFF:
        return NativePolicyPlan(config.native_mode, [], [], fingerprint)

    available = capabilities or default_native_capabilities()
    operations: list[dict[str, Any]] = []
    unsupported: list[str] = []
    for attachment in config.attachments:
        capability = available.get(attachment.target_type)
        if capability is None:
            unsupported.append(f"{attachment.name}: target type {attachment.target_type.value} is unsupported")
            continue
        for policy in attachment.policies:
            if policy.kind not in capability.kinds:
                unsupported.append(f"{attachment.name}/{policy.name}: kind {policy.kind.value} is unsupported")
                continue
            for phase in _phases(policy.phase):
                if phase not in capability.phases:
                    unsupported.append(f"{attachment.name}/{policy.name}: phase {phase.value} is unsupported")
                    continue
                operations.append(_safe_operation(attachment, policy, phase))
    if config.abac is not None:
        unsupported.append("ABAC attachment selectors are not supported")
    if config.native_mode in (NativePolicyMode.APPLY, NativePolicyMode.REQUIRED):
        if not all(capability.supports_apply for capability in available.values()):
            unsupported.append("native Service Policy apply is not supported by the verified platform surface")
        if config.native_mode is NativePolicyMode.REQUIRED and not all(
            capability.supports_verify for capability in available.values()
        ):
            unsupported.append("native Service Policy verification is not supported by the verified platform surface")
    if config.native_mode is NativePolicyMode.REQUIRED and unsupported:
        raise ServicePolicyCapabilityError("; ".join(unsupported))
    return NativePolicyPlan(config.native_mode, operations, sorted(set(unsupported)), fingerprint)


def _require_profile(profile: str) -> None:
    if not profile.strip():
        raise ValueError("an explicit Databricks profile is required; pass --profile <name>")


def apply_native_policy_plan(
    plan: NativePolicyPlan,
    *,
    transport: NativePolicyTransport,
    profile: str,
) -> NativePolicyApplyReceipt:
    """Apply supported operations and fail closed on any transport error."""

    _require_profile(profile)
    if plan.unsupported:
        raise ServicePolicyCapabilityError("; ".join(plan.unsupported))
    applied: list[dict[str, Any]] = []
    for operation in plan.operations:
        response = transport.apply(operation, profile=profile)
        if not isinstance(response, dict):
            raise ServicePolicyCapabilityError("native apply returned a malformed response")
        applied.append({
            "policy_name": operation["policy_name"],
            "target": operation["target"],
            "status": str(response.get("status", "accepted")),
        })
    return NativePolicyApplyReceipt(plan.declaration_fingerprint, applied)


def verify_native_policy_plan(
    plan: NativePolicyPlan,
    *,
    transport: NativePolicyTransport,
    profile: str,
) -> NativePolicyVerification:
    """Read back each operation and distinguish observed/mismatch state."""

    _require_profile(profile)
    if plan.unsupported:
        raise ServicePolicyCapabilityError("; ".join(plan.unsupported))
    statuses: list[dict[str, Any]] = []
    for operation in plan.operations:
        response = transport.read(operation, profile=profile)
        if not isinstance(response, dict):
            raise ServicePolicyCapabilityError("native verification returned a malformed response")
        matches = all(response.get(key, operation[key]) == operation[key] for key in ("target", "policy_name", "phase", "rank", "mode"))
        statuses.append({
            "policy_name": operation["policy_name"],
            "target": operation["target"],
            "status": "observed" if matches else "mismatch",
        })
    complete = all(status["status"] == "observed" for status in statuses)
    return NativePolicyVerification(plan.declaration_fingerprint, statuses, complete)
