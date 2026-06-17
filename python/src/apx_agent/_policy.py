"""Policy primitives — ALLOW / ASK / DENY governance for agent actions.

Extends the binary guard model (allow / raise) in :mod:`._guards` with a
three-verdict policy system modeled on the omniagents policy design:

* **ALLOW** — the action proceeds.
* **DENY** — the action is blocked; the agent receives an error.
* **ASK** — the action pauses for human approval. The tool call aborts
  with a structured "approval required" message that the LLM relays to
  the user; once the user approves (via :class:`ApprovalStore`), the
  agent's retry of the same call is allowed through.

Policies compose with max-action semantics: DENY beats ASK beats ALLOW.
A policy may abstain by returning ``None``.

Two policy types are provided:

* :class:`FunctionPolicy` — wraps a Python callable.
* :class:`PromptPolicy` — sends the action to an LLM classifier with a
  natural-language rubric and parses the verdict.

The bridge into the existing agent loop is :class:`PolicyGate`, which is
a ``before_tool``-compatible hook: attach it to an ``LlmAgent`` via
``before_tool=PolicyGate([...])`` (or compose with existing guards via
:func:`._guards.compose`).

ASK semantics (turn-boundary approval):

1. A policy returns ASK → the gate records a pending approval in its
   :class:`ApprovalStore` and raises :class:`ApprovalRequired`. The tool
   call fails with a message containing the approval ID.
2. The LLM sees the tool error and relays the approval request to the
   user in its reply.
3. The user approves with ``store.approve(approval_id)`` (surfaced via
   an API endpoint or UI control by the embedding application).
4. The agent retries the same tool call (same name + arguments). The
   gate finds the granted approval, consumes it (one-shot), and allows
   the call without re-evaluating ASK policies.

True mid-turn suspension (LangGraph ``interrupt()`` + checkpointer) is a
future enhancement; this module deliberately implements the simpler
turn-boundary flow that works with the existing stateless serving layer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

# Phases a policy can intercept. MVP wires "tool_call" (via PolicyGate);
# the other phases are declared now so policy authors can target them and
# future gates (request/response interception) pick them up unchanged.
POLICY_PHASES = ("request", "response", "tool_call", "tool_result")


class PolicyAction(IntEnum):
    """Verdict severity, ordered so ``max()`` implements composition.

    DENY beats ASK beats ALLOW — composing a list of results takes the
    maximum action across them.
    """

    ALLOW = 0
    ASK = 1
    DENY = 2


@dataclass
class PolicyResult:
    """Outcome of a single policy evaluation.

    :param action: The verdict — :class:`PolicyAction` ALLOW / ASK / DENY.
    :param reason: Optional human-readable explanation, surfaced to the
        user when the action is ASK or DENY,
        e.g. ``"Sending email to an external domain requires approval."``.
    :param set_labels: Labels to attach to the session as a side effect,
        e.g. ``{"risk": "high", "topic": "finance"}``. Merged across
        composed policies (later policies win on key conflicts).
    """

    action: PolicyAction
    reason: str | None = None
    set_labels: dict[str, str] = field(default_factory=dict)


@dataclass
class PolicyEvent:
    """The action under evaluation, passed to every policy.

    :param phase: Which lifecycle point fired — one of
        :data:`POLICY_PHASES`, e.g. ``"tool_call"``.
    :param tool_name: Tool being invoked when phase is ``tool_call`` /
        ``tool_result``, e.g. ``"send_email"``. ``None`` for request /
        response phases.
    :param arguments: Tool arguments for ``tool_call`` phase,
        e.g. ``{"to": "bob@example.com", "subject": "hi"}``. ``None``
        when not applicable.
    :param content: Message text for ``request`` / ``response`` phases.
        ``None`` for tool phases.
    :param context: Free-form caller context (principal, session id,
        usage counters). Empty dict when the caller has nothing to add.
    """

    phase: str
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    content: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


class FunctionPolicy:
    """A policy backed by a Python callable.

    The callable receives a :class:`PolicyEvent` and returns a
    :class:`PolicyResult` or ``None`` to abstain.

    :param fn: The evaluator,
        e.g. ``lambda ev: PolicyResult(PolicyAction.DENY) if ev.tool_name == "rm" else None``.
    :param phases: Which phases this policy applies to. Defaults to
        ``("tool_call",)`` — the only phase the MVP gate fires.
    :param name: Optional display name for logs and reasons. Falls back
        to the callable's ``__name__``.
    """

    def __init__(
        self,
        fn: Callable[[PolicyEvent], PolicyResult | None],
        *,
        phases: Sequence[str] = ("tool_call",),
        name: str | None = None,
    ) -> None:
        bad = [p for p in phases if p not in POLICY_PHASES]
        if bad:
            raise ValueError(f"Unknown policy phase(s): {bad}. Valid: {POLICY_PHASES}")
        self._fn = fn
        self.phases = tuple(phases)
        self.name = name or getattr(fn, "__name__", "function_policy")

    def evaluate(self, event: PolicyEvent) -> PolicyResult | None:
        """Run the wrapped callable for *event*.

        :param event: The action under evaluation.
        :returns: The callable's verdict, or ``None`` when the event's
            phase isn't in this policy's ``phases`` (abstain).
        """
        if event.phase not in self.phases:
            return None
        return self._fn(event)


_PROMPT_POLICY_SYSTEM = """\
You are a security policy classifier for an AI agent. Evaluate whether the
following agent action complies with the policy rubric.

Respond with EXACTLY one word on the first line: ALLOW, ASK, or DENY.
- ALLOW: the action clearly complies with the rubric.
- ASK: the action is ambiguous or consequential enough that a human should approve it.
- DENY: the action clearly violates the rubric.
Optionally add a one-sentence reason on the second line."""


class PromptPolicy:
    """A policy that classifies actions with an LLM against a rubric.

    Sends the event (tool name + arguments, or message content) together
    with a natural-language rubric to a serving endpoint and parses the
    one-word verdict from the response.

    Fail-closed by design: if the LLM call fails or the response cannot
    be parsed into a verdict, the policy returns DENY with the failure
    in the reason. For a governance primitive, an unreadable verdict
    must not silently become ALLOW.

    :param rubric: The natural-language rule,
        e.g. ``"Deny if the agent is about to send email to a domain other than example.com."``.
    :param model: Databricks serving endpoint for the classifier,
        e.g. ``"databricks-claude-sonnet-4-6"``. A small/cheap model is
        recommended — this runs on every gated action.
    :param phases: Which phases this policy applies to. Defaults to
        ``("tool_call",)``.
    :param name: Optional display name for logs and reasons.
    """

    def __init__(
        self,
        rubric: str,
        *,
        model: str,
        phases: Sequence[str] = ("tool_call",),
        name: str | None = None,
    ) -> None:
        bad = [p for p in phases if p not in POLICY_PHASES]
        if bad:
            raise ValueError(f"Unknown policy phase(s): {bad}. Valid: {POLICY_PHASES}")
        self.rubric = rubric
        self.model = model
        self.phases = tuple(phases)
        self.name = name or "prompt_policy"

    def _build_user_message(self, event: PolicyEvent) -> str:
        """Render *event* into the classifier's user prompt.

        :param event: The action under evaluation.
        :returns: A prompt string containing the rubric and a JSON
            rendering of the action.
        """
        if event.phase in ("tool_call", "tool_result"):
            action_desc = json.dumps(
                {"tool": event.tool_name, "arguments": event.arguments},
                default=str,
            )
        else:
            action_desc = json.dumps({"content": event.content}, default=str)
        return (
            f"POLICY RUBRIC:\n{self.rubric}\n\n"
            f"AGENT ACTION (phase={event.phase}):\n{action_desc}"
        )

    def evaluate(self, event: PolicyEvent) -> PolicyResult | None:
        """Classify *event* against the rubric via the LLM.

        :param event: The action under evaluation.
        :returns: The parsed verdict, or ``None`` when the event's phase
            isn't in this policy's ``phases`` (abstain). LLM/parse
            failures return DENY (fail-closed), never ``None``.
        """
        if event.phase not in self.phases:
            return None

        from ._llm import get_llm

        try:
            from langchain_core.messages import HumanMessage, SystemMessage

            llm = get_llm(self.model)
            response = llm.invoke([
                SystemMessage(content=_PROMPT_POLICY_SYSTEM),
                HumanMessage(content=self._build_user_message(event)),
            ])
            text = str(getattr(response, "content", response) or "")
        except Exception as exc:
            # Fail-closed: a classifier outage must not become a silent ALLOW.
            logger.exception("PromptPolicy %r LLM call failed", self.name)
            return PolicyResult(
                action=PolicyAction.DENY,
                reason=f"Policy classifier unavailable ({exc}); denying by default.",
            )

        return self._parse_verdict(text)

    def _parse_verdict(self, text: str) -> PolicyResult:
        """Extract the one-word verdict from the classifier response.

        :param text: Raw LLM response text. The verdict is expected as
            the first word of the first non-empty line; the second line
            (when present) is taken as the reason.
        :returns: The parsed :class:`PolicyResult`. Unparseable text
            returns DENY (fail-closed).
        """
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        first_word = lines[0].split()[0].upper().strip(".:,") if lines else ""
        reason = lines[1] if len(lines) > 1 else None
        verdicts = {
            "ALLOW": PolicyAction.ALLOW,
            "ASK": PolicyAction.ASK,
            "DENY": PolicyAction.DENY,
        }
        if first_word in verdicts:
            return PolicyResult(action=verdicts[first_word], reason=reason)
        # Fail-closed on garbage output.
        return PolicyResult(
            action=PolicyAction.DENY,
            reason=f"Policy classifier returned unparseable verdict: {text[:120]!r}",
        )


def evaluate_policies(
    policies: Iterable[Any],
    event: PolicyEvent,
) -> PolicyResult:
    """Evaluate *policies* against *event* with max-action composition.

    DENY beats ASK beats ALLOW. Abstentions (``None``) are skipped.
    Reasons from non-ALLOW results are joined; ``set_labels`` dicts are
    merged across all results (later policies win on key conflicts).

    :param policies: Policy objects exposing
        ``evaluate(event) -> PolicyResult | None`` —
        :class:`FunctionPolicy`, :class:`PromptPolicy`, or any
        compatible duck-typed object.
    :param event: The action under evaluation.
    :returns: The composed result. With no policies (or all abstaining),
        returns ALLOW.
    """
    final_action = PolicyAction.ALLOW
    reasons: list[str] = []
    labels: dict[str, str] = {}
    for policy in policies:
        result = policy.evaluate(event)
        if result is None:
            continue
        labels.update(result.set_labels)
        if result.action > final_action:
            final_action = result.action
        if result.action != PolicyAction.ALLOW and result.reason:
            reasons.append(result.reason)
    return PolicyResult(
        action=final_action,
        reason="; ".join(reasons) if reasons else None,
        set_labels=labels,
    )


def _call_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    """Stable fingerprint for a (tool, arguments) pair.

    Used to match an approval grant to the agent's retry of the same
    call — the retry must carry identical arguments to consume the
    approval.

    :param tool_name: Tool being invoked, e.g. ``"send_email"``.
    :param arguments: The tool's argument dict.
    :returns: A hex digest, e.g. ``"a3f2..."``.
    """
    canonical = json.dumps(
        {"tool": tool_name, "args": arguments}, sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Approval:
    """A pending or resolved approval request.

    :param id: Unique approval ID surfaced to the user,
        e.g. ``"appr-1a2b3c4d"``.
    :param tool_name: Tool that triggered the ASK, e.g. ``"send_email"``.
    :param arguments: The exact arguments the approval covers.
    :param fingerprint: :func:`_call_fingerprint` of (tool, arguments) —
        the retry must match this to consume the approval.
    :param status: ``"pending"`` | ``"approved"`` | ``"denied"``.
    :param reason: The policy reason that triggered the ASK, shown to
        the approver.
    """

    id: str
    tool_name: str
    arguments: dict[str, Any]
    fingerprint: str
    status: str
    reason: str | None


class ApprovalStore:
    """In-memory registry of approval requests (thread-safe).

    One instance per :class:`PolicyGate` by default; share an instance
    across gates when one approval surface (e.g. a dev-UI endpoint)
    serves multiple agents.

    Suitable for single-process deployments (Databricks Apps,
    ``apx-agent run``). A durable backend (Delta / Lakebase) can
    implement the same four methods when multi-replica serving needs
    shared approval state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._approvals: dict[str, Approval] = {}

    def request(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        reason: str | None = None,
    ) -> Approval:
        """Record a pending approval for a (tool, arguments) call.

        If a pending request already exists for the identical call,
        returns it instead of minting a duplicate — repeated agent
        retries before the user acts shouldn't pile up requests.

        :param tool_name: Tool that triggered the ASK.
        :param arguments: The tool's argument dict.
        :param reason: The policy reason, shown to the approver.
        :returns: The pending :class:`Approval` (new or pre-existing).
        """
        fp = _call_fingerprint(tool_name, arguments)
        with self._lock:
            for existing in self._approvals.values():
                if existing.fingerprint == fp and existing.status == "pending":
                    return existing
            approval = Approval(
                id=f"appr-{uuid.uuid4().hex[:8]}",
                tool_name=tool_name,
                arguments=dict(arguments),
                fingerprint=fp,
                status="pending",
                reason=reason,
            )
            self._approvals[approval.id] = approval
            return approval

    def approve(self, approval_id: str) -> Approval:
        """Mark a pending approval as granted.

        :param approval_id: The ID from :meth:`request`,
            e.g. ``"appr-1a2b3c4d"``.
        :returns: The updated :class:`Approval`.
        :raises KeyError: If no approval with that ID exists.
        """
        with self._lock:
            approval = self._approvals[approval_id]
            approval.status = "approved"
            return approval

    def deny(self, approval_id: str) -> Approval:
        """Mark a pending approval as refused.

        :param approval_id: The ID from :meth:`request`.
        :returns: The updated :class:`Approval`.
        :raises KeyError: If no approval with that ID exists.
        """
        with self._lock:
            approval = self._approvals[approval_id]
            approval.status = "denied"
            return approval

    def list_pending(self) -> list[Approval]:
        """Return all approvals awaiting a decision.

        :returns: Pending approvals in insertion order. Empty list when
            nothing is waiting.
        """
        with self._lock:
            return [a for a in self._approvals.values() if a.status == "pending"]

    def consume_granted(self, tool_name: str, arguments: dict[str, Any]) -> Approval | None:
        """Find and consume a granted approval matching this exact call.

        One-shot: the matched approval is removed so a later identical
        call requires a fresh approval.

        :param tool_name: Tool being retried.
        :param arguments: The retry's argument dict — must be identical
            to the approved call's arguments.
        :returns: The consumed :class:`Approval`, or ``None`` when no
            granted approval matches.
        """
        fp = _call_fingerprint(tool_name, arguments)
        with self._lock:
            for approval_id, approval in self._approvals.items():
                if approval.fingerprint == fp and approval.status == "approved":
                    del self._approvals[approval_id]
                    return approval
        return None

    def consume_denied(self, tool_name: str, arguments: dict[str, Any]) -> Approval | None:
        """Find and consume a refused approval matching this exact call.

        Lets the gate convert a user refusal into a DENY on the agent's
        retry, instead of asking again in a loop.

        :param tool_name: Tool being retried.
        :param arguments: The retry's argument dict.
        :returns: The consumed :class:`Approval`, or ``None`` when no
            refused approval matches.
        """
        fp = _call_fingerprint(tool_name, arguments)
        with self._lock:
            for approval_id, approval in self._approvals.items():
                if approval.fingerprint == fp and approval.status == "denied":
                    del self._approvals[approval_id]
                    return approval
        return None


class ApprovalRequired(PermissionError):
    """Raised by :class:`PolicyGate` when a policy verdict is ASK.

    Subclasses :class:`PermissionError` so the existing guard-error
    handling path (tool aborts, LLM sees the error message as the tool
    result) applies unchanged.

    :param approval: The pending approval recorded for this call.
    """

    def __init__(self, approval: Approval) -> None:
        self.approval = approval
        reason_suffix = f" Reason: {approval.reason}" if approval.reason else ""
        super().__init__(
            f"APPROVAL REQUIRED: the call to {approval.tool_name!r} is paused "
            f"pending human approval (approval ID: {approval.id}).{reason_suffix} "
            f"Tell the user this action needs their approval and give them the "
            f"approval ID. Do not retry until the user confirms they approved it."
        )


class PolicyGate:
    """Adapts a policy list into a ``before_tool``-compatible hook.

    Attach to an agent directly::

        from apx_agent import Agent, PolicyGate, FunctionPolicy

        gate = PolicyGate([my_policy, my_prompt_policy])
        agent = Agent(tools=[...], before_tool=gate)

    or compose with existing guards::

        from apx_agent import compose
        agent = Agent(tools=[...], before_tool=compose(rate_limit, gate))

    On each tool call the gate:

    1. Consumes a matching *granted* approval, if any → ALLOW (skip
       policy evaluation — the human already decided).
    2. Consumes a matching *refused* approval, if any → DENY.
    3. Otherwise evaluates the policies. DENY raises
       :class:`PermissionError`; ASK records a pending approval and
       raises :class:`ApprovalRequired`; ALLOW returns normally.

    Labels from policy results accumulate on :attr:`labels` — the
    embedding application can flush them to the session/conversation
    store between turns.

    :param policies: Policy objects exposing
        ``evaluate(event) -> PolicyResult | None``.
    :param approval_store: Shared approval registry. A private
        :class:`ApprovalStore` is created when omitted.
    :param context: Static context merged into every
        :class:`PolicyEvent` (principal, session id, ...). Empty dict
        when omitted.
    """

    def __init__(
        self,
        policies: Sequence[Any],
        *,
        approval_store: ApprovalStore | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.policies = list(policies)
        self.approvals = approval_store if approval_store is not None else ApprovalStore()
        self._context = dict(context) if context else {}
        # Accumulated set_labels from policy results; the embedding app
        # reads + clears between turns to persist onto the session.
        self.labels: dict[str, str] = {}

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        """The ``before_tool`` hook body — evaluate policies for one call.

        :param tool_name: Tool the agent is about to invoke,
            e.g. ``"send_email"``.
        :param arguments: The tool's argument dict.
        :raises ApprovalRequired: When the composed verdict is ASK and
            no granted approval matches this call.
        :raises PermissionError: When the composed verdict is DENY, or
            the user refused a previous approval request for this call.
        """
        # 1. Human already granted this exact call — allow, one-shot.
        granted = self.approvals.consume_granted(tool_name, arguments)
        if granted is not None:
            logger.info(
                "PolicyGate: approval %s consumed for %s", granted.id, tool_name,
            )
            return

        # 2. Human refused this exact call — deny instead of re-asking.
        refused = self.approvals.consume_denied(tool_name, arguments)
        if refused is not None:
            raise PermissionError(
                f"The user refused approval for this call to {tool_name!r} "
                f"(approval ID: {refused.id}). Do not retry it."
            )

        event = PolicyEvent(
            phase="tool_call",
            tool_name=tool_name,
            arguments=arguments,
            context=self._context,
        )
        result = evaluate_policies(self.policies, event)
        self.labels.update(result.set_labels)

        if result.action == PolicyAction.DENY:
            raise PermissionError(
                result.reason or f"Policy denied the call to {tool_name!r}."
            )
        if result.action == PolicyAction.ASK:
            approval = self.approvals.request(
                tool_name, arguments, reason=result.reason,
            )
            raise ApprovalRequired(approval)
        # ALLOW — fall through.
