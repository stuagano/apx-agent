"""databricks-watchdog integration — sketch.

`databricks-watchdog <https://github.com/stuagano/databricks-watchdog>`_ is
the compliance posture layer for Unity Catalog: declarative cross-domain
policies (security / data quality / cost / agent governance), violation
lifecycle tracking, owner accountability, and 13 MCP tools for AI
assistants to query and act on governance posture.

This module is the apx-agent side of the integration. Three pieces, each
designed so the runtime contract is stable while the wire-level transport
to watchdog can evolve:

  * ``WatchdogClient`` — adapter for the watchdog policy-decision and
    violation-report calls. The HTTP / MCP transport isn't finalized
    yet, so ``WatchdogClient`` takes a pluggable ``transport`` callable.
    The default transport is a no-op stub (allow everything, drop
    violation reports) so this module is safe to import + use before
    watchdog answers the open questions in
    ``docs/future-work/gap-plan-2026-05-18.md``.

  * ``WatchdogGuard`` — a small adapter that produces callables wired
    through the existing apx-agent hooks (``input_guardrails``,
    ``before_tool``, ``before_model``). Users plug these into ``Agent``
    just like any other guardrail / callback. When watchdog returns
    ``action="reject"`` the guard short-circuits and reports a violation.

  * ``emit_agent_metadata(agent)`` — produces the agent's
    crawler-facing metadata shape (resources, tools, sub-agents,
    instructions). Today returns a dict; future versions will write
    it to whatever stable location watchdog's crawler reads from
    (MLflow tags, a UC manifest, a known endpoint path).

Open questions blocking the real wiring — tracked in the gap plan:

  1. What's the canonical metadata shape watchdog wants from agent
     assets? UC tags may already cover most of it.
  2. Which of watchdog's 13 MCP tools is the runtime policy-decision
     entry point? Or is there a non-MCP HTTP API for low-latency
     runtime calls?
  3. Where do runtime violations get reported? A UC table watchdog
     already reads from? An MCP tool call? A direct write?

The shapes below are designed to absorb whatever answers come back —
``WatchdogClient`` takes an arbitrary callable transport, and
``WatchdogDecision`` has the union of fields watchdog already produces
in its evaluator output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ._agents import BaseAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchdogDecision:
    """A single policy decision returned by watchdog.

    Attributes:
        action: One of ``"allow"``, ``"reject"``, ``"redact"``. Defaults
            to ``"allow"`` so the no-op stub transport produces a
            pass-through decision.
        reason: Human-readable explanation surfaced to the user (when
            ``action`` is ``"reject"``) or attached as a violation reason.
        policy_id: Watchdog's identifier for the policy that produced
            this decision. Pass back when reporting violations so
            watchdog can aggregate by policy.
        domain: Governance domain (``"security"``, ``"data_quality"``,
            ``"cost"``, ``"agent"``, etc.) for trace attribution.
        redacted_content: When ``action="redact"``, the rewritten content
            (e.g. PII stripped). When absent or empty, callers fall back
            to the original content unchanged.
        metadata: Free-form dict for additional context watchdog wants
            to surface (owner email, remediation link, etc.).
    """

    action: str = "allow"
    reason: str | None = None
    policy_id: str | None = None
    domain: str | None = None
    redacted_content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# Transport callable shape: receives a request dict, returns a dict the
# WatchdogClient parses into a WatchdogDecision. Plugged in by callers
# once the watchdog-side wire protocol is pinned down.
TransportFn = Callable[[dict[str, Any]], dict[str, Any]]


def _noop_transport(_request: dict[str, Any]) -> dict[str, Any]:
    """Default transport — allow everything, report nothing.

    Returns the shape ``WatchdogClient`` expects for an allow decision.
    Used until a real transport is wired in.
    """
    return {"action": "allow"}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class WatchdogClient:
    """Adapter for talking to a databricks-watchdog deployment.

    Args:
        endpoint: The watchdog HTTP / MCP endpoint URL. Stored for
            future use; the default transport doesn't consult it.
        transport: Optional transport callable taking a request dict
            and returning a decision dict. When omitted, a no-op
            allow-everything transport is used so this module is safe
            to import and exercise before watchdog's wire API is
            finalized.

    Subclass to replace ``evaluate`` / ``report_violation`` entirely
    when more control is needed.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        transport: TransportFn | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._transport = transport or _noop_transport

    def evaluate(
        self,
        *,
        operation: str,
        context: dict[str, Any] | None = None,
    ) -> WatchdogDecision:
        """Ask watchdog whether ``operation`` should proceed.

        Args:
            operation: A stable operation key — ``"input_message"``,
                ``"tool_call"``, ``"model_call"``, etc. Watchdog uses
                this plus the context to route to the right policy
                evaluator.
            context: Operation-specific context (the tool name, args,
                user id, calling agent endpoint, etc.). Passed verbatim
                to the transport.

        Returns:
            A ``WatchdogDecision``. Falls back to ``allow`` (with a
            warning) on transport failure so a watchdog outage doesn't
            black-hole production traffic — the runtime decides its
            own fail-open vs fail-closed policy on top of this.
        """
        request: dict[str, Any] = {
            "operation": operation,
            "context": context or {},
        }
        try:
            response = self._transport(request)
        except Exception as e:
            logger.warning(
                "Watchdog transport failed for operation %s: %s — falling back to allow.",
                operation, e,
            )
            return WatchdogDecision(action="allow", reason=f"watchdog transport error: {e}")
        if not isinstance(response, dict):
            logger.warning("Watchdog transport returned %s, expected dict — allowing.", type(response))
            return WatchdogDecision(action="allow")
        return WatchdogDecision(
            action=str(response.get("action", "allow")),
            reason=response.get("reason"),
            policy_id=response.get("policy_id"),
            domain=response.get("domain"),
            redacted_content=response.get("redacted_content"),
            metadata=response.get("metadata") or {},
        )

    def report_violation(
        self,
        decision: WatchdogDecision,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Report a runtime block / redaction back to watchdog.

        Used so the watchdog compliance dashboard reflects what actually
        happened at runtime, not just what static crawls discovered.
        Failures are logged-and-continued — the agent doesn't have a
        useful response to "I couldn't report a violation".
        """
        payload = {
            "type": "violation_report",
            "decision": {
                "action": decision.action,
                "reason": decision.reason,
                "policy_id": decision.policy_id,
                "domain": decision.domain,
                "metadata": decision.metadata,
            },
            "context": context or {},
        }
        try:
            self._transport(payload)
        except Exception as e:
            logger.warning("Watchdog violation report failed: %s", e)


# ---------------------------------------------------------------------------
# Guard adapters — plug into existing apx-agent hooks
# ---------------------------------------------------------------------------


class WatchdogGuard:
    """Bridge ``WatchdogClient`` decisions into apx-agent's hook callables.

    Usage::

        from apx_agent import Agent, WatchdogClient, WatchdogGuard

        watchdog = WatchdogClient(transport=my_transport)
        guard = WatchdogGuard(watchdog, agent_name="customer_triage")

        agent = Agent(
            ...,
            input_guardrails=[guard.for_input()],
            before_tool=guard.for_tool(),
            before_model=guard.for_model(),
        )

    Each ``for_*`` method returns a callable wired through the right hook
    signature. Reject decisions raise / short-circuit per the hook's
    contract; redact decisions modify content where the hook supports
    it; allow decisions are pass-through.
    """

    def __init__(
        self,
        client: WatchdogClient,
        *,
        agent_name: str | None = None,
    ) -> None:
        self.client = client
        self.agent_name = agent_name

    def _context(self, **extra: Any) -> dict[str, Any]:
        base: dict[str, Any] = {}
        if self.agent_name:
            base["agent_name"] = self.agent_name
        base.update(extra)
        return base

    def for_input(self) -> Callable[[Any], str | None]:
        """Return an ``input_guardrails``-compatible callable.

        Signature: ``(messages) -> str | None`` — return ``None`` to let
        the request through, return a string to short-circuit with that
        string as the response.
        """
        def _check(messages: Any) -> str | None:
            decision = self.client.evaluate(
                operation="input_message",
                context=self._context(messages=_summarise_messages(messages)),
            )
            if decision.action == "reject":
                self.client.report_violation(
                    decision,
                    self._context(messages=_summarise_messages(messages)),
                )
                return decision.reason or "Request blocked by Watchdog policy."
            return None
        return _check

    def for_output(self) -> Callable[[str], str | None]:
        """Return an ``output_guardrails``-compatible callable.

        Signature: ``(text) -> str | None`` — return ``None`` to let the
        response through, return a string to replace the response. When
        watchdog returns ``action="redact"`` and ``redacted_content`` is
        set, the redacted content replaces the response.
        """
        def _check(text: str) -> str | None:
            decision = self.client.evaluate(
                operation="output_message",
                context=self._context(text_length=len(text)),
            )
            if decision.action == "reject":
                self.client.report_violation(decision, self._context())
                return decision.reason or "Response blocked by Watchdog policy."
            if decision.action == "redact" and decision.redacted_content:
                self.client.report_violation(decision, self._context())
                return decision.redacted_content
            return None
        return _check

    def for_tool(self) -> Callable[[str, dict[str, Any]], None]:
        """Return a ``before_tool``-compatible callable.

        Signature: ``(tool_name, arguments) -> None`` — raise to abort.
        """
        def _check(tool_name: str, arguments: dict[str, Any]) -> None:
            decision = self.client.evaluate(
                operation="tool_call",
                context=self._context(tool_name=tool_name, arguments=arguments),
            )
            if decision.action == "reject":
                self.client.report_violation(
                    decision,
                    self._context(tool_name=tool_name, arguments=arguments),
                )
                raise PermissionError(
                    decision.reason or f"Tool {tool_name!r} blocked by Watchdog policy."
                )
        return _check

    def for_model(self) -> Callable[[Any], None]:
        """Return a ``before_model``-compatible callable.

        Signature: ``(prompts) -> None`` — raise to abort the LLM call.
        """
        def _check(prompts: Any) -> None:
            decision = self.client.evaluate(
                operation="model_call",
                context=self._context(prompt_count=_count_prompts(prompts)),
            )
            if decision.action == "reject":
                self.client.report_violation(decision, self._context())
                raise PermissionError(
                    decision.reason or "Model call blocked by Watchdog policy."
                )
        return _check


# ---------------------------------------------------------------------------
# Metadata emission
# ---------------------------------------------------------------------------


def emit_agent_metadata(
    agent: "BaseAgent",
    *,
    name: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Produce the agent's crawler-facing metadata for watchdog.

    Returns a JSON-serializable dict suitable for posting to watchdog's
    crawl endpoint, writing as an MLflow tag, or persisting to a UC
    manifest table. The exact destination depends on which integration
    shape watchdog accepts (one of the open questions in the gap plan).

    The returned shape:

    .. code-block:: python

        {
            "name": "customer_triage",
            "model": "databricks-claude-sonnet-4-6",
            "instructions": "...",
            "tools": [
                {"name": "classify_intent", "uc_name": "main.tools.classify_intent",
                 "grants": ["agent_consumers"], "description": "..."},
                ...
            ],
            "sub_agents": ["endpoints/billing", ...],
            "resources": [
                {"kind": "uc_function", "identifier": "main.tools.classify_intent"},
                {"kind": "genie_space", "identifier": "abc-123"},
                ...
            ],
        }

    Args:
        agent: The apx-agent ``BaseAgent`` to introspect.
        name: Optional agent name. Defaults to the agent's ``_name``
            attribute when set.
        model: Optional LLM endpoint name. Included in the metadata
            and as a ``serving_endpoint`` resource when set.
    """
    from ._resources import _iter_sub_agents, _iter_tool_fns, collect_resource_specs, get_resources
    from ._tool import get_tool_metadata

    resolved_name = name or getattr(agent, "_name", None)

    tools_meta: list[dict[str, Any]] = []
    for fn in _iter_tool_fns(agent):
        meta = get_tool_metadata(fn)
        tools_meta.append({
            "name": fn.__name__,
            "description": (fn.__doc__ or "").strip(),
            "uc_name": meta.uc_name if meta else None,
            "grants": list(meta.grants) if meta else [],
            "resources": [
                {"kind": s.kind, "identifier": s.identifier}
                for s in get_resources(fn)
            ],
        })

    resources = [
        {"kind": s.kind, "identifier": s.identifier}
        for s in collect_resource_specs(agent, model=model)
    ]

    return {
        "name": resolved_name,
        "model": model,
        "instructions": getattr(agent, "_instructions", None) or "",
        "tools": tools_meta,
        "sub_agents": list(_iter_sub_agents(agent)),
        "resources": resources,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _summarise_messages(messages: Any) -> dict[str, Any]:
    """Compress a message list into a summary suitable for the context dict.

    Avoids shipping full message bodies to watchdog on every call.
    """
    if not isinstance(messages, list):
        return {"count": 0}
    return {
        "count": len(messages),
        "last_role": getattr(messages[-1], "role", None) if messages else None,
    }


def _count_prompts(prompts: Any) -> int:
    if isinstance(prompts, list):
        return len(prompts)
    return 1
