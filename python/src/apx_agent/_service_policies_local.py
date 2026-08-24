"""Local projection of the portable Service Policy declaration."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ._guards import prompt_injection_heuristic
from ._policy import (
    ApprovalStore,
    PolicyAction,
    PolicyEvent,
    PolicyGate,
    PolicyResult,
    PromptPolicy,
)
from ._service_policies import (
    ServicePolicy,
    ServicePolicyAction,
    ServicePolicyAttachment,
    ServicePolicyDecision,
    ServicePolicyEvent,
    ServicePolicyPhase,
    ServicePoliciesConfig,
    ServicePolicyEvaluationError,
    evaluate_ordered_service_policies,
)


LocalEvaluator = Callable[[ServicePolicyEvent], Any]


def _policy_event(event: ServicePolicyEvent) -> PolicyEvent:
    """Translate the portable phase into the existing policy phase names."""

    if event.tool_name:
        phase = "tool_call" if event.phase == ServicePolicyPhase.ON_CALL.value else "tool_result"
    else:
        phase = "request" if event.phase == ServicePolicyPhase.ON_CALL.value else "response"
    return PolicyEvent(
        phase=phase,
        tool_name=event.tool_name,
        arguments=event.arguments,
        content=event.content,
        context=event.context,
    )


def _watchdog_action(watchdog: Any, policy: ServicePolicy, event: ServicePolicyEvent) -> ServicePolicyAction:
    decision = watchdog.evaluate(
        operation=f"service_policy:{policy.name}",
        context={
            "policy_name": policy.name,
            "policy_kind": policy.kind.value,
            "phase": event.phase,
            "target_type": str(event.target_type),
            "target": event.target,
        },
    )
    return ServicePolicyAction.DENY if getattr(decision, "action", "reject") == "reject" else ServicePolicyAction.ALLOW


def build_local_policy_evaluators(
    config: ServicePoliciesConfig,
    *,
    watchdog: Any | None = None,
    local_evaluators: Mapping[str, LocalEvaluator] | None = None,
) -> dict[str, LocalEvaluator]:
    """Build local evaluators keyed by stable policy name.

    Vendor-managed detectors and SQL functions require an injected evaluator
    or Watchdog transport. They are never silently treated as local ALLOW.
    """

    injected = dict(local_evaluators or {})
    jailbreak = prompt_injection_heuristic()
    evaluators: dict[str, LocalEvaluator] = {}

    for attachment in config.attachments:
        for policy in attachment.policies:
            if policy.name in injected:
                evaluators[policy.name] = injected[policy.name]
                continue

            if policy.kind.value == "builtin" and policy.builtin is not None:
                if policy.builtin.value == "jailbreak":
                    def _jailbreak(event: ServicePolicyEvent, check: Any = jailbreak) -> ServicePolicyAction:
                        content = event.content
                        if content is None and event.arguments is not None:
                            content = json.dumps(event.arguments, default=str)
                        return ServicePolicyAction.DENY if check([{"content": content or ""}]) else ServicePolicyAction.ALLOW

                    evaluators[policy.name] = _jailbreak
                elif watchdog is not None:
                    evaluators[policy.name] = lambda event, p=policy: _watchdog_action(watchdog, p, event)
                else:
                    def _unavailable(event: ServicePolicyEvent, p: ServicePolicy = policy) -> ServicePolicyAction:
                        raise ServicePolicyEvaluationError(
                            f"local evaluator unavailable for built-in {p.builtin.value}"
                        )

                    evaluators[policy.name] = _unavailable
                continue

            if policy.kind.value == "llm_judge":
                classifier = PromptPolicy(
                    policy.prompt or "",
                    model=policy.classifier or "",
                    phases=("request", "response", "tool_call", "tool_result"),
                    name=policy.name,
                )
                evaluators[policy.name] = lambda event, p=classifier: p.evaluate(_policy_event(event))
                continue

            def _sql_unavailable(event: ServicePolicyEvent, p=policy) -> ServicePolicyAction:
                raise ServicePolicyEvaluationError(
                    f"local SQL evaluator unavailable for {p.function}"
                )

            evaluators[policy.name] = _sql_unavailable

    return evaluators


class LocalServicePolicyAdapter:
    """Attach Service Policy decisions to existing local lifecycle hooks."""

    def __init__(
        self,
        config: ServicePoliciesConfig,
        *,
        watchdog: Any | None = None,
        local_evaluators: Mapping[str, LocalEvaluator] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self._enabled = config.local_mode.value == "mirror"
        self.approvals = ApprovalStore()
        self.decisions: list[ServicePolicyDecision] = []
        self._context = dict(context) if context else {}
        self._evaluators = build_local_policy_evaluators(
            config,
            watchdog=watchdog,
            local_evaluators=local_evaluators,
        )
        self._gates = {
            attachment.name: PolicyGate(
                attachment.policies,
                approval_store=self.approvals,
                context=self._context,
                evaluator=self._make_gate_evaluator(attachment),
            )
            for attachment in config.attachments
            if attachment.mode.value == "enforce"
        }
        self._watchdog = watchdog

    def _make_event(
        self,
        attachment: ServicePolicyAttachment,
        phase: ServicePolicyPhase,
        *,
        content: str | None = None,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
        result: Any = None,
    ) -> ServicePolicyEvent:
        return ServicePolicyEvent(
            phase=phase.value,
            target_type=attachment.target_type,
            target=attachment.target,
            content=content,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            context=self._context,
        )

    def _evaluate(self, attachment: ServicePolicyAttachment, event: ServicePolicyEvent) -> ServicePolicyDecision:
        decision = evaluate_ordered_service_policies(
            attachment.policies,
            event,
            mode=attachment.mode,
            adapter="local",
            evaluator=lambda policy, current: self._evaluators[policy.name](current),
        )
        self.decisions.append(decision)
        return decision

    def _make_gate_evaluator(
        self,
        attachment: ServicePolicyAttachment,
    ) -> Callable[[Sequence[Any], PolicyEvent], PolicyResult]:
        def _evaluate(policies: Sequence[Any], event: PolicyEvent) -> PolicyResult:
            current = self._make_event(
                attachment,
                ServicePolicyPhase.ON_CALL,
                tool_name=event.tool_name,
                arguments=event.arguments,
            )
            decision = evaluate_ordered_service_policies(
                list(policies),
                current,
                mode=attachment.mode,
                adapter="local",
                evaluator=lambda policy, service_event: self._evaluators[policy.name](service_event),
            )
            self.decisions.append(decision)
            action = PolicyAction[decision.action]
            return PolicyResult(action=action, reason=decision.reason)

        return _evaluate

    def _deny_message(self, decision: ServicePolicyDecision, default: str) -> str | None:
        if decision.action == ServicePolicyAction.ALLOW.value:
            return None
        return decision.reason or default

    def for_input(self) -> Callable[[list[Any]], str | None]:
        def _check(messages: list[Any]) -> str | None:
            if not self._enabled:
                return None
            content = json.dumps(messages, default=str)
            for attachment in self.config.attachments:
                decision = self._evaluate(
                    attachment,
                    self._make_event(attachment, ServicePolicyPhase.ON_CALL, content=content),
                )
                if attachment.mode.value == "enforce":
                    message = self._deny_message(decision, "Request blocked by Service Policy.")
                    if message is not None:
                        return message
            return None

        return _check

    def for_output(self) -> Callable[[str], str | None]:
        def _check(text: str) -> str | None:
            if not self._enabled:
                return None
            for attachment in self.config.attachments:
                decision = self._evaluate(
                    attachment,
                    self._make_event(attachment, ServicePolicyPhase.ON_RESULT, content=text),
                )
                if attachment.mode.value == "enforce":
                    message = self._deny_message(decision, "Response blocked by Service Policy.")
                    if message is not None:
                        return message
            return None

        return _check

    def for_tool(self) -> Callable[[str, dict[str, Any]], None]:
        def _check(tool_name: str, arguments: dict[str, Any]) -> None:
            if not self._enabled:
                return
            for attachment in self.config.attachments:
                if attachment.mode.value == "enforce":
                    self._gates[attachment.name](tool_name, arguments)
                else:
                    self._evaluate(
                        attachment,
                        self._make_event(
                            attachment,
                            ServicePolicyPhase.ON_CALL,
                            tool_name=tool_name,
                            arguments=arguments,
                        ),
                    )

        return _check

    def for_model(self) -> Callable[[list[Any]], None]:
        def _check(prompts: list[Any]) -> None:
            if not self._enabled:
                return
            content = json.dumps(prompts, default=str)
            for attachment in self.config.attachments:
                decision = self._evaluate(
                    attachment,
                    self._make_event(attachment, ServicePolicyPhase.ON_CALL, content=content),
                )
                if attachment.mode.value == "enforce":
                    message = self._deny_message(decision, "Model call blocked by Service Policy.")
                    if message is not None:
                        raise PermissionError(message)

        return _check

    def for_tool_result(self) -> Callable[[str, dict[str, Any], Any], None]:
        def _check(tool_name: str, arguments: dict[str, Any], result: Any) -> None:
            if not self._enabled:
                return
            for attachment in self.config.attachments:
                decision = self._evaluate(
                    attachment,
                    self._make_event(
                        attachment,
                        ServicePolicyPhase.ON_RESULT,
                        tool_name=tool_name,
                        arguments=arguments,
                        result=result,
                    ),
                )
                if attachment.mode.value == "enforce":
                    message = self._deny_message(decision, "Tool result blocked by Service Policy.")
                    if message is not None:
                        raise PermissionError(message)

        return _check
