"""Portable Service Policy declarations and deterministic local decisions.

The declaration is deliberately independent of a Databricks client. Native
deployment consumes the same validated models, while local execution uses the
ordered decision helper and existing policy primitives.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator


class _StringEnum(str, Enum):
    """String enum that serializes to the public configuration value."""


class ServicePolicyTargetType(_StringEnum):
    MCP_SERVICE = "mcp_service"
    MODEL_SERVICE = "model_service"
    MODEL_PROVIDER_SERVICE = "model_provider_service"
    AGENT_SERVICE = "agent_service"


class ServicePolicyKind(_StringEnum):
    BUILTIN = "builtin"
    LLM_JUDGE = "llm_judge"
    SQL = "sql"


class BuiltinServicePolicy(_StringEnum):
    SENSITIVE_DATA = "sensitive_data"
    UNSAFE_CONTENT = "unsafe_content"
    JAILBREAK = "jailbreak"
    HALLUCINATION = "hallucination"


class ServicePolicyPhase(_StringEnum):
    ON_CALL = "on_call"
    ON_RESULT = "on_result"
    BOTH = "both"


class ServicePolicyMode(_StringEnum):
    ENFORCE = "enforce"
    DRY_RUN = "dry_run"


class LocalPolicyMode(_StringEnum):
    MIRROR = "mirror"
    OFF = "off"


class NativePolicyMode(_StringEnum):
    OFF = "off"
    PLAN = "plan"
    APPLY = "apply"
    REQUIRED = "required"


class ServicePolicyAction(_StringEnum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"
    UNAVAILABLE = "UNAVAILABLE"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServicePolicy(_StrictModel):
    """One named policy definition within an attachment."""

    name: StrictStr
    kind: ServicePolicyKind
    builtin: BuiltinServicePolicy | None = None
    classifier: StrictStr | None = None
    prompt: StrictStr | None = None
    function: StrictStr | None = None
    phase: ServicePolicyPhase = ServicePolicyPhase.ON_CALL
    rank: StrictInt = Field(default=100, ge=0)

    @field_validator("name", "classifier", "prompt", "function")
    @classmethod
    def _non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("service policy string fields must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_kind(self) -> ServicePolicy:
        references = {
            "builtin": self.builtin,
            "classifier": self.classifier,
            "prompt": self.prompt,
            "function": self.function,
        }
        if self.kind is ServicePolicyKind.BUILTIN:
            if self.builtin is None:
                raise ValueError("builtin policies require builtin")
            if self.builtin is BuiltinServicePolicy.JAILBREAK and self.phase is not ServicePolicyPhase.ON_CALL:
                raise ValueError("jailbreak policies are request/on_call only")
            if self.builtin is BuiltinServicePolicy.HALLUCINATION and self.phase is not ServicePolicyPhase.ON_RESULT:
                raise ValueError("hallucination policies are result/on_result only")
            if any(references[key] is not None for key in ("classifier", "prompt", "function")):
                raise ValueError("builtin policies cannot define classifier, prompt, or function")
        elif self.kind is ServicePolicyKind.LLM_JUDGE:
            if not self.classifier or not self.prompt:
                raise ValueError("llm_judge policies require classifier and prompt")
            if self.builtin is not None or self.function is not None:
                raise ValueError("llm_judge policies cannot define builtin or function")
        elif self.kind is ServicePolicyKind.SQL:
            if not self.function:
                raise ValueError("sql policies require a Unity Catalog function")
            if any(references[key] is not None for key in ("builtin", "classifier", "prompt")):
                raise ValueError("sql policies cannot define builtin, classifier, or prompt")
        return self


class ServicePolicyTarget(_StrictModel):
    """A reusable target identity for plan and diagnostic output."""

    target_type: ServicePolicyTargetType
    target: StrictStr

    @field_validator("target")
    @classmethod
    def _target_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("service policy target must not be empty")
        return value


class ServicePolicyAttachment(_StrictModel):
    """Policies attached to one service target."""

    name: StrictStr
    target_type: ServicePolicyTargetType
    target: StrictStr
    mode: ServicePolicyMode = ServicePolicyMode.ENFORCE
    policies: list[ServicePolicy] = Field(default_factory=list)

    @field_validator("name", "target")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("service policy attachment strings must not be empty")
        return value


class AbacSelector(_StrictModel):
    """Future tag-based scope, retained as desired state until native support."""

    tags: dict[StrictStr, StrictStr] = Field(default_factory=dict)


class ServicePoliciesConfig(_StrictModel):
    """Declarative Service Policy configuration under ``AgentConfig``."""

    local_mode: LocalPolicyMode = LocalPolicyMode.MIRROR
    native_mode: NativePolicyMode = NativePolicyMode.OFF
    attachments: list[ServicePolicyAttachment] = Field(default_factory=list)
    abac: AbacSelector | None = None

    @model_validator(mode="after")
    def _validate_native_requirements(self) -> ServicePoliciesConfig:
        if self.native_mode is NativePolicyMode.REQUIRED and not self.attachments:
            raise ValueError("native_mode='required' needs at least one attachment")
        return self


@dataclass(frozen=True)
class ServicePolicyEvent:
    """Normalized event passed to a local policy evaluator."""

    phase: str
    target_type: ServicePolicyTargetType
    target: str
    content: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    result: Any = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ServicePolicyDecision:
    """A sanitized policy decision shared by local and native adapters."""

    action: Literal["ALLOW", "DENY", "ASK", "UNAVAILABLE"]
    reason: str | None
    policy_name: str | None
    phase: str
    rank: int | None
    mode: ServicePolicyMode
    adapter: Literal["local", "native"]


@dataclass(frozen=True)
class _NormalizedAction:
    action: ServicePolicyAction
    reason: str | None


class ServicePolicyCapabilityError(ValueError):
    """Raised when a requested native policy cannot be represented."""


class ServicePolicyEvaluationError(ValueError):
    """Raised for malformed or unavailable policy evaluation inputs."""


ServicePolicyEvaluator = Callable[
    [ServicePolicy, ServicePolicyEvent],
    Any,
]


def ordered_policies(
    policies: Sequence[ServicePolicy],
    phase: ServicePolicyPhase | str,
) -> list[ServicePolicy]:
    """Return policies applicable to *phase* in deterministic rank order."""

    requested = ServicePolicyPhase(phase)
    if requested is ServicePolicyPhase.BOTH:
        applicable = [
            policy for policy in policies if policy.phase in (ServicePolicyPhase.ON_CALL, ServicePolicyPhase.ON_RESULT, ServicePolicyPhase.BOTH)
        ]
        reverse = False
    else:
        applicable = [
            policy for policy in policies if policy.phase in (requested, ServicePolicyPhase.BOTH)
        ]
        reverse = requested is ServicePolicyPhase.ON_RESULT
    return sorted(applicable, key=lambda policy: policy.rank, reverse=reverse)


def _normalize_action(value: Any) -> _NormalizedAction:
    """Normalize existing ``PolicyResult``/enum/string outputs."""

    reason = getattr(value, "reason", None)
    action = getattr(value, "action", value)
    if hasattr(action, "name"):
        action = action.name
    try:
        return _NormalizedAction(ServicePolicyAction(str(action).upper()), reason)
    except ValueError as exc:
        raise ServicePolicyEvaluationError(f"unknown policy action {action!r}") from exc


def evaluate_ordered_service_policies(
    policies: Sequence[ServicePolicy],
    event: ServicePolicyEvent,
    *,
    mode: ServicePolicyMode,
    adapter: Literal["local", "native"],
    evaluator: ServicePolicyEvaluator | None = None,
) -> ServicePolicyDecision:
    """Evaluate in rank order; stop at the first ``DENY`` or ``ASK``.

    ``dry_run`` changes enforcement at the adapter boundary, not the recorded
    action: operators must be able to see what would have been blocked.
    """

    phase = ServicePolicyPhase(event.phase)
    for policy in ordered_policies(policies, phase):
        try:
            result = evaluator(policy, event) if evaluator is not None else None
            if result is None:
                continue
            normalized = _normalize_action(result)
            action = normalized.action
            reason = normalized.reason
        except Exception as exc:
            reason = f"policy evaluation unavailable ({exc})"
            action = ServicePolicyAction.UNAVAILABLE if mode is ServicePolicyMode.DRY_RUN else ServicePolicyAction.DENY
        if action is ServicePolicyAction.ASK:
            target_type = ServicePolicyTargetType(event.target_type)
            if target_type is not ServicePolicyTargetType.MCP_SERVICE or phase is not ServicePolicyPhase.ON_CALL:
                reason = reason or "ASK is supported only for MCP on_call policies"
                action = ServicePolicyAction.UNAVAILABLE if mode is ServicePolicyMode.DRY_RUN else ServicePolicyAction.DENY
        if action is not ServicePolicyAction.ALLOW:
            return ServicePolicyDecision(
                action=action.value,
                reason=reason,
                policy_name=policy.name,
                phase=event.phase,
                rank=policy.rank,
                mode=mode,
                adapter=adapter,
            )
    return ServicePolicyDecision(
        action=ServicePolicyAction.ALLOW.value,
        reason=None,
        policy_name=None,
        phase=event.phase,
        rank=None,
        mode=mode,
        adapter=adapter,
    )
