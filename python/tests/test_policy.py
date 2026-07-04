"""Tests for _policy.py — ALLOW / ASK / DENY policies and approvals.

Covers:
  1. PolicyAction max-action composition (DENY beats ASK beats ALLOW).
  2. FunctionPolicy evaluation, abstention, and phase filtering.
  3. PromptPolicy verdict parsing, fail-closed behavior, and LLM-stub flow.
  4. ApprovalStore lifecycle: request → approve/deny → consume (one-shot).
  5. PolicyGate as a before_tool hook: ALLOW passes, DENY raises
     PermissionError, ASK raises ApprovalRequired; the full approve-retry
     and refuse-retry flows; label accumulation.
"""

from __future__ import annotations

from typing import Any

import pytest

from apx_agent._policy import (
    Approval,
    ApprovalRequired,
    ApprovalStore,
    FunctionPolicy,
    PolicyAction,
    PolicyEvent,
    PolicyGate,
    PolicyResult,
    PromptPolicy,
    evaluate_policies,
)


# ---------------------------------------------------------------------------
# Composition — evaluate_policies max-action semantics
# ---------------------------------------------------------------------------


def _tool_event(tool: str = "send_email", args: dict[str, Any] | None = None) -> PolicyEvent:
    """Build a tool_call PolicyEvent for tests."""
    return PolicyEvent(
        phase="tool_call",
        tool_name=tool,
        arguments=args if args is not None else {"to": "bob@example.com"},
    )


def _const_policy(action: PolicyAction, reason: str | None = None,
                  labels: dict[str, str] | None = None) -> FunctionPolicy:
    """A FunctionPolicy that always returns the given verdict."""
    return FunctionPolicy(
        lambda ev: PolicyResult(action=action, reason=reason,
                                set_labels=labels or {}),
        name=f"const_{action.name.lower()}",
    )


def test_compose_deny_beats_ask_beats_allow():
    event = _tool_event()
    # ALLOW + ASK → ASK (ASK outranks ALLOW)
    r = evaluate_policies(
        [_const_policy(PolicyAction.ALLOW), _const_policy(PolicyAction.ASK, "needs ok")],
        event,
    )
    assert r.action == PolicyAction.ASK
    # ASK + DENY → DENY (DENY outranks everything)
    r = evaluate_policies(
        [_const_policy(PolicyAction.ASK, "needs ok"),
         _const_policy(PolicyAction.DENY, "blocked")],
        event,
    )
    assert r.action == PolicyAction.DENY
    # Both non-ALLOW reasons must be preserved in the composed reason —
    # losing one would hide which policy fired from the user.
    assert "needs ok" in r.reason
    assert "blocked" in r.reason


def test_compose_empty_and_abstain_is_allow():
    event = _tool_event()
    # No policies → ALLOW (nothing to enforce)
    assert evaluate_policies([], event).action == PolicyAction.ALLOW
    # All policies abstain (return None) → ALLOW
    abstainer = FunctionPolicy(lambda ev: None, name="abstainer")
    assert evaluate_policies([abstainer], event).action == PolicyAction.ALLOW


def test_compose_merges_labels_later_wins():
    event = _tool_event()
    r = evaluate_policies(
        [
            _const_policy(PolicyAction.ALLOW, labels={"risk": "low", "topic": "email"}),
            _const_policy(PolicyAction.ALLOW, labels={"risk": "high"}),
        ],
        event,
    )
    # Later policy overwrites "risk"; "topic" from the first survives.
    assert r.set_labels == {"risk": "high", "topic": "email"}


# ---------------------------------------------------------------------------
# FunctionPolicy
# ---------------------------------------------------------------------------


def test_function_policy_phase_filtering():
    # Policy registered for tool_call only must abstain on a response event.
    policy = FunctionPolicy(
        lambda ev: PolicyResult(action=PolicyAction.DENY),
        phases=("tool_call",),
    )
    response_event = PolicyEvent(phase="response", content="hello")
    # Abstains (None) because the phase doesn't match — NOT a deny.
    assert policy.evaluate(response_event) is None
    # Same policy fires on a tool_call event.
    assert policy.evaluate(_tool_event()).action == PolicyAction.DENY


def test_function_policy_rejects_unknown_phase():
    with pytest.raises(ValueError):
        FunctionPolicy(lambda ev: None, phases=("not_a_phase",))


def test_function_policy_receives_event_data():
    seen: list[PolicyEvent] = []

    def spy(ev: PolicyEvent) -> PolicyResult | None:
        seen.append(ev)
        return PolicyResult(action=PolicyAction.ALLOW)

    policy = FunctionPolicy(spy)
    policy.evaluate(_tool_event("run_sql", {"query": "SELECT 1"}))
    # The policy must see the actual tool name and arguments — a gate that
    # passed empty/wrong data would make every policy decision meaningless.
    assert seen[0].tool_name == "run_sql"
    assert seen[0].arguments == {"query": "SELECT 1"}


# ---------------------------------------------------------------------------
# PromptPolicy — verdict parsing and fail-closed behavior
# ---------------------------------------------------------------------------


class _StubLlm:
    """LLM stub returning a fixed response text via .invoke().

    Mirrors the langchain chat-model interface PromptPolicy uses
    (invoke(messages) -> message-with-.content). A real stub class (not
    MagicMock) so an unexpected call signature fails loudly.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[Any] = []

    def invoke(self, messages: Any) -> Any:
        self.calls.append(messages)

        class _Msg:
            content = self._text

        return _Msg()


class _RaisingLlm:
    """LLM stub whose invoke always raises — simulates endpoint outage."""

    def invoke(self, messages: Any) -> Any:
        raise RuntimeError("endpoint unavailable")


@pytest.mark.parametrize("text,expected", [
    ("ALLOW", PolicyAction.ALLOW),
    ("ASK\nThis sends external email.", PolicyAction.ASK),
    ("DENY\nViolates the rubric.", PolicyAction.DENY),
    ("allow.", PolicyAction.ALLOW),          # lowercase + punctuation tolerated
    ("  DENY:  \n  reason here  ", PolicyAction.DENY),
])
def test_prompt_policy_parses_verdicts(text, expected, monkeypatch):
    policy = PromptPolicy("rubric", model="test-endpoint")
    stub = _StubLlm(text)
    monkeypatch.setattr("apx_agent._llm.get_llm", lambda endpoint, **kw: stub)

    result = policy.evaluate(_tool_event())
    assert result.action == expected


def test_prompt_policy_reason_extracted_from_second_line(monkeypatch):
    policy = PromptPolicy("rubric", model="test-endpoint")
    stub = _StubLlm("ASK\nThis emails an external domain.")
    monkeypatch.setattr("apx_agent._llm.get_llm", lambda endpoint, **kw: stub)

    result = policy.evaluate(_tool_event())
    # The classifier's explanation must surface as the reason — it's what
    # the user sees when asked to approve.
    assert result.reason == "This emails an external domain."


def test_prompt_policy_fail_closed_on_garbage(monkeypatch):
    policy = PromptPolicy("rubric", model="test-endpoint")
    stub = _StubLlm("I think this is probably fine, go ahead!")
    monkeypatch.setattr("apx_agent._llm.get_llm", lambda endpoint, **kw: stub)

    result = policy.evaluate(_tool_event())
    # Unparseable verdict must DENY, not ALLOW — a confused classifier
    # becoming a silent ALLOW would defeat the entire governance layer.
    assert result.action == PolicyAction.DENY
    assert "unparseable" in result.reason.lower()


def test_prompt_policy_fail_closed_on_llm_error(monkeypatch):
    policy = PromptPolicy("rubric", model="test-endpoint")
    monkeypatch.setattr("apx_agent._llm.get_llm", lambda endpoint, **kw: _RaisingLlm())

    result = policy.evaluate(_tool_event())
    # Endpoint outage → DENY (fail-closed), with the failure in the reason.
    assert result.action == PolicyAction.DENY
    assert "unavailable" in result.reason.lower() or "denying" in result.reason.lower()


def test_prompt_policy_rubric_and_action_in_prompt(monkeypatch):
    policy = PromptPolicy(
        "Deny email to non-example.com domains.", model="test-endpoint",
    )
    stub = _StubLlm("ALLOW")
    monkeypatch.setattr("apx_agent._llm.get_llm", lambda endpoint, **kw: stub)

    policy.evaluate(_tool_event("send_email", {"to": "bob@external.io"}))
    # The classifier must receive both the rubric and the actual tool
    # arguments — without either, the verdict is uninformed.
    sent = str(stub.calls[0])
    assert "Deny email to non-example.com domains." in sent
    assert "bob@external.io" in sent


def test_prompt_policy_abstains_on_unmatched_phase(monkeypatch):
    policy = PromptPolicy("rubric", model="test-endpoint", phases=("tool_call",))
    # No LLM stub needed: the phase check must short-circuit BEFORE any
    # LLM call — if it didn't, this test would error on the real get_llm.
    assert policy.evaluate(PolicyEvent(phase="response", content="x")) is None


# ---------------------------------------------------------------------------
# ApprovalStore
# ---------------------------------------------------------------------------


def test_approval_request_dedupes_pending():
    store = ApprovalStore()
    a1 = store.request("send_email", {"to": "bob@x.com"})
    a2 = store.request("send_email", {"to": "bob@x.com"})
    # Identical pending call must NOT mint a second approval — agent
    # retries before the user acts would otherwise pile up requests.
    assert a1.id == a2.id
    # A different argument set is a different approval.
    a3 = store.request("send_email", {"to": "eve@x.com"})
    assert a3.id != a1.id


def test_approval_consume_granted_is_one_shot():
    store = ApprovalStore()
    a = store.request("send_email", {"to": "bob@x.com"})
    store.approve(a.id)
    # First consume returns the grant…
    consumed = store.consume_granted("send_email", {"to": "bob@x.com"})
    assert consumed is not None
    assert consumed.id == a.id
    # …second identical call finds nothing — the approval was one-shot.
    assert store.consume_granted("send_email", {"to": "bob@x.com"}) is None


def test_approval_consume_requires_exact_arguments():
    store = ApprovalStore()
    a = store.request("send_email", {"to": "bob@x.com"})
    store.approve(a.id)
    # Different args must NOT consume the approval — otherwise an agent
    # could get "send to bob" approved and then send to eve.
    assert store.consume_granted("send_email", {"to": "eve@x.com"}) is None
    # The original grant is still there for the correct call.
    assert store.consume_granted("send_email", {"to": "bob@x.com"}) is not None


def test_approval_deny_flow():
    store = ApprovalStore()
    a = store.request("rm_table", {"name": "main.gold.facts"})
    store.deny(a.id)
    assert store.consume_granted("rm_table", {"name": "main.gold.facts"}) is None
    denied = store.consume_denied("rm_table", {"name": "main.gold.facts"})
    assert denied is not None
    assert denied.status == "denied"


def test_approval_unknown_id_raises():
    store = ApprovalStore()
    with pytest.raises(KeyError):
        store.approve("appr-nope")
    with pytest.raises(KeyError):
        store.deny("appr-nope")


def test_approval_records_decider_and_timestamp():
    """#469: approve/deny stamp who decided and when (was: no attribution)."""
    store = ApprovalStore()
    a = store.request("send_email", {"to": "bob@x.com"})
    assert a.decided_by is None and a.decided_at is None  # pending: unset

    approved = store.approve(a.id, decided_by="alice@corp")
    assert approved.decided_by == "alice@corp"
    assert approved.decided_at is not None  # ISO timestamp stamped

    b = store.request("rm_table", {"name": "main.gold.facts"})
    denied = store.deny(b.id, decided_by="carol@corp")
    assert denied.decided_by == "carol@corp"
    assert denied.decided_at is not None


def test_approval_decider_absent_is_recorded_as_none():
    """A decision surface with no user identity records None, not a fake value."""
    store = ApprovalStore()
    a = store.request("send_email", {"to": "bob@x.com"})
    approved = store.approve(a.id)  # no decided_by
    assert approved.decided_by is None
    assert approved.decided_at is not None  # timestamp still recorded


def test_list_pending():
    store = ApprovalStore()
    a1 = store.request("t1", {"x": 1})
    a2 = store.request("t2", {"x": 2})
    store.approve(a1.id)
    pending = store.list_pending()
    # Only the un-acted request remains pending.
    assert [p.id for p in pending] == [a2.id]


# ---------------------------------------------------------------------------
# PolicyGate — the before_tool integration
# ---------------------------------------------------------------------------


def test_gate_allow_passes():
    gate = PolicyGate([_const_policy(PolicyAction.ALLOW)])
    # Must not raise.
    gate("any_tool", {"a": 1})


def test_gate_deny_raises_permission_error_with_reason():
    gate = PolicyGate([_const_policy(PolicyAction.DENY, "not allowed here")])
    with pytest.raises(PermissionError, match="not allowed here"):
        gate("send_email", {"to": "bob@x.com"})


def test_gate_ask_raises_approval_required_and_records_pending():
    gate = PolicyGate([_const_policy(PolicyAction.ASK, "external email")])
    with pytest.raises(ApprovalRequired) as exc_info:
        gate("send_email", {"to": "bob@x.com"})
    approval = exc_info.value.approval
    # The pending approval must reference the exact blocked call.
    assert approval.tool_name == "send_email"
    assert approval.arguments == {"to": "bob@x.com"}
    assert approval.status == "pending"
    # The error message must carry the approval ID — it's the handle the
    # LLM relays to the user, and the policy reason for context.
    assert approval.id in str(exc_info.value)
    assert "external email" in str(exc_info.value)
    # The gate's store must list it as pending.
    assert [p.id for p in gate.approvals.list_pending()] == [approval.id]


def test_gate_full_ask_approve_retry_flow():
    """The end-to-end ASK happy path: ask → approve → retry succeeds once."""
    gate = PolicyGate([_const_policy(PolicyAction.ASK, "needs human")])
    args = {"to": "bob@x.com", "subject": "Q2 report"}

    # Turn 1: tool call blocked with ASK.
    with pytest.raises(ApprovalRequired) as exc_info:
        gate("send_email", args)
    approval_id = exc_info.value.approval.id

    # Human approves out-of-band.
    gate.approvals.approve(approval_id)

    # Turn 2: identical retry passes WITHOUT re-evaluating the ASK policy.
    gate("send_email", args)  # must not raise

    # Turn 3: the approval was one-shot — a third identical call asks again.
    with pytest.raises(ApprovalRequired):
        gate("send_email", args)


def test_gate_full_ask_refuse_retry_flow():
    """ASK → user refuses → retry becomes a hard DENY (no re-ask loop)."""
    gate = PolicyGate([_const_policy(PolicyAction.ASK, "needs human")])
    args = {"to": "eve@x.com"}

    with pytest.raises(ApprovalRequired) as exc_info:
        gate("send_email", args)
    gate.approvals.deny(exc_info.value.approval.id)

    # Retry after refusal must be a plain PermissionError telling the
    # agent not to retry — NOT another ApprovalRequired (re-ask loop).
    with pytest.raises(PermissionError, match="refused") as exc2:
        gate("send_email", args)
    assert not isinstance(exc2.value, ApprovalRequired)


def test_gate_approval_does_not_bypass_other_calls():
    """An approval for call A must not leak to a different call B."""
    gate = PolicyGate([_const_policy(PolicyAction.ASK, "needs human")])

    with pytest.raises(ApprovalRequired) as exc_info:
        gate("send_email", {"to": "bob@x.com"})
    gate.approvals.approve(exc_info.value.approval.id)

    # Different arguments → still asks (does NOT consume bob's approval).
    with pytest.raises(ApprovalRequired):
        gate("send_email", {"to": "eve@x.com"})

    # The original approved call still goes through.
    gate("send_email", {"to": "bob@x.com"})


def test_gate_accumulates_labels():
    gate = PolicyGate([
        _const_policy(PolicyAction.ALLOW, labels={"risk": "low"}),
    ])
    gate("t", {"a": 1})
    assert gate.labels == {"risk": "low"}


def test_gate_shared_approval_store():
    """Two gates sharing one store see each other's approvals."""
    shared = ApprovalStore()
    gate_a = PolicyGate([_const_policy(PolicyAction.ASK)], approval_store=shared)
    gate_b = PolicyGate([_const_policy(PolicyAction.ASK)], approval_store=shared)

    with pytest.raises(ApprovalRequired) as exc_info:
        gate_a("tool", {"x": 1})
    shared.approve(exc_info.value.approval.id)

    # gate_b consumes the approval granted via gate_a's request — one
    # approval surface (UI endpoint) can serve multiple agents.
    gate_b("tool", {"x": 1})  # must not raise


def test_gate_is_compatible_with_compose():
    """PolicyGate must stack with existing guards via compose()."""
    from apx_agent import ToolDenylist, compose

    gate = PolicyGate([_const_policy(PolicyAction.ALLOW)])
    hook = compose(ToolDenylist(["rm"]), gate)

    # Denylisted tool blocked by the legacy guard before the gate runs.
    with pytest.raises(PermissionError):
        hook("rm", {})
    # Non-denylisted tool passes both layers.
    hook("send_email", {"to": "bob@x.com"})


def test_approval_required_is_permission_error():
    """ApprovalRequired must subclass PermissionError so the existing
    guard-error path (tool aborts, LLM sees message) handles it unchanged."""
    store = ApprovalStore()
    approval = store.request("t", {})
    exc = ApprovalRequired(approval)
    assert isinstance(exc, PermissionError)
