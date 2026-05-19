"""databricks-watchdog integration.

`databricks-watchdog <https://github.com/stuagano/databricks-watchdog>`_ is
the compliance posture layer for Unity Catalog: declarative cross-domain
policies (security / data quality / cost / agent governance), violation
lifecycle tracking, owner accountability, and 13 MCP tools for AI
assistants to query and act on governance posture.

The integration has three wire-protocol contracts, all answered now:

  1. **Metadata shape**: UC tags on the registered model. Watchdog's
     crawler reads tags off the model record. ``set_uc_tags_for_agent``
     writes them.
  2. **Runtime policy decisions**: watchdog's MCP tools.
     ``make_mcp_transport`` produces a transport that calls a named
     MCP tool with the operation context.
  3. **Violation reports**: an INSERT into a watchdog-owned UC Delta
     table. ``make_uc_violation_writer`` produces a transport that
     handles ``violation_report`` requests.

Three public pieces sit on top of those:

  * ``WatchdogClient`` — adapter that dispatches evaluate /
    report_violation calls through a pluggable ``transport`` callable.
    Default transport is a no-op stub so this module is safe to import
    without a real watchdog wired up.

  * ``WatchdogGuard`` — produces callables for the existing apx-agent
    hooks (``input_guardrails``, ``output_guardrails``, ``before_tool``,
    ``before_model``). Reject short-circuits; redact rewrites; allow
    passes through. Violation reports fire automatically on reject /
    redact.

  * ``emit_agent_metadata(agent)`` — produces the crawler-facing
    metadata dict. ``set_uc_tags_for_agent(...)`` writes it into UC
    as tags on the registered model.

Wiring it up end-to-end::

    from apx_agent import (
        Agent, WatchdogClient, WatchdogGuard,
        make_watchdog_transport, set_uc_tags_for_agent,
    )

    # 1. One-time at deploy: emit metadata as UC tags
    set_uc_tags_for_agent(
        agent,
        registered_model_name="main.agents.customer_triage",
        model="databricks-claude-sonnet-4-6",
    )

    # 2. Runtime: combined transport (MCP for evaluate + UC table for violations)
    transport = make_watchdog_transport(
        mcp_url="https://watchdog.example.com/mcp",
        mcp_tool_name="evaluate_operation",
        violations_table="main.watchdog.runtime_violations",
        ws=ws,
        warehouse_id="wh-prod",
    )
    watchdog = WatchdogClient(transport=transport)

    # 3. Bridge to agent hooks
    guard = WatchdogGuard(watchdog, agent_name="customer_triage")
    agent = Agent(
        ...,
        input_guardrails=[guard.for_input()],
        before_tool=guard.for_tool(),
        before_model=guard.for_model(),
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ._sql import run_sql

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


# ---------------------------------------------------------------------------
# UC tags writer — metadata that watchdog's crawler reads
# ---------------------------------------------------------------------------


# UC tag value size is finite; trim very long fields to keep tag writes
# from rejecting on length. The full structured metadata still lives in
# apx.agent.metadata as JSON so callers that need detail can parse it.
_TAG_VALUE_MAX = 4000


def _truncate(value: str, limit: int = _TAG_VALUE_MAX) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 14] + "...[truncated]"


def _build_uc_tag_payload(metadata: dict[str, Any]) -> dict[str, str]:
    """Render an emit_agent_metadata() dict into the apx.agent.* UC tag set.

    The structured fields (tools / sub_agents / resources) are JSON-encoded
    so watchdog can parse the full shape. Scalar fields (name / model) get
    flat string tags so they're queryable by direct equality from UC.
    """
    import json

    tools_short = [t.get("name", "") for t in metadata.get("tools") or []]
    uc_functions = [
        t.get("uc_name") for t in metadata.get("tools") or []
        if t.get("uc_name")
    ]
    sub_agents = list(metadata.get("sub_agents") or [])
    resources = metadata.get("resources") or []

    tags: dict[str, str] = {
        "apx.agent.name": str(metadata.get("name") or ""),
        "apx.agent.model": str(metadata.get("model") or ""),
        "apx.agent.tool_count": str(len(tools_short)),
        "apx.agent.tools": _truncate(json.dumps(tools_short)),
        "apx.agent.uc_functions": _truncate(",".join(uc_functions)),
        "apx.agent.sub_agents": _truncate(",".join(sub_agents)),
        "apx.agent.resources": _truncate(json.dumps(resources)),
        "apx.agent.metadata": _truncate(json.dumps(metadata, default=str)),
    }
    # Drop empty values — UC rejects tags with empty keys, and watchdog
    # doesn't gain anything from "apx.agent.sub_agents = ''".
    return {k: v for k, v in tags.items() if v}


def set_uc_tags_for_agent(
    agent: "BaseAgent",
    *,
    registered_model_name: str,
    model: str | None = None,
    name: str | None = None,
    mlflow_client: Any | None = None,
) -> dict[str, str]:
    """Write the agent's metadata as UC tags on its registered model.

    These tags are what watchdog's crawler reads to discover agents and
    their declared resources. Run this at deploy time (typically right
    after ``databricks.agents.deploy(...)`` succeeds) so the dashboard
    picks up the new agent on the next crawl.

    Args:
        agent: The apx-agent ``BaseAgent`` to introspect.
        registered_model_name: UC three-part name of the registered model
            (``catalog.schema.model``) — what was passed as
            ``registered_model_name`` to ``log_agent``.
        model: Optional LLM endpoint name. Included in tags.
        name: Optional agent name override. Defaults to the agent's
            ``_name`` attribute.
        mlflow_client: Optional ``mlflow.tracking.MlflowClient`` instance.
            When omitted, a default one is constructed (uses the active
            MLflow tracking URI).

    Returns:
        The dict of tag keys-values that were written. Useful for logging
        and for tests.

    Raises:
        ImportError: if mlflow isn't installed (``apx-agent[eval]`` extra).
    """
    try:
        from mlflow.tracking import MlflowClient
    except ImportError as e:  # pragma: no cover — only without extra
        raise ImportError(
            "set_uc_tags_for_agent requires mlflow. "
            "Install with: pip install 'apx-agent[eval]'"
        ) from e

    client = mlflow_client or MlflowClient()
    metadata = emit_agent_metadata(agent, name=name, model=model)
    tags = _build_uc_tag_payload(metadata)

    for key, value in tags.items():
        try:
            client.set_registered_model_tag(
                name=registered_model_name, key=key, value=value,
            )
        except Exception as e:
            logger.warning(
                "Failed to write UC tag %s on %s: %s — continuing with remaining tags.",
                key, registered_model_name, e,
            )
    return tags


# ---------------------------------------------------------------------------
# UC violations writer — transport that handles violation_report only
# ---------------------------------------------------------------------------


def _sql_str_literal(value: str) -> str:
    """SQL-escape a string for inline literal use — single quotes doubled."""
    return "'" + value.replace("'", "''") + "'"


def make_uc_violation_writer(
    violations_table: str,
    *,
    ws: Any,
    warehouse_id: str | None = None,
    auto_create: bool = True,
) -> TransportFn:
    """Return a transport that handles only ``violation_report`` requests.

    Inserts a row into the watchdog runtime-violations Delta table per
    report. Non-violation-report requests (evaluate calls) pass through
    as no-op allow decisions so this transport can be composed with an
    evaluate transport via ``make_watchdog_transport``.

    Schema (auto-created when ``auto_create=True`` on first use)::

        CREATE TABLE IF NOT EXISTS {violations_table} (
            ts TIMESTAMP NOT NULL,
            agent_name STRING,
            operation STRING,
            action STRING,
            reason STRING,
            policy_id STRING,
            domain STRING,
            context STRING,        -- JSON
            metadata STRING        -- JSON
        ) USING DELTA

    Args:
        violations_table: Three-part UC name of the Delta table watchdog
            reads from.
        ws: ``WorkspaceClient`` used for the SQL Statements API.
        warehouse_id: Optional SQL warehouse. When omitted, the SDK
            auto-discovers one.
        auto_create: Create the violations table on first use. Set
            ``False`` when watchdog provisions the table out-of-band.
    """
    import json
    import time

    if violations_table.count(".") != 2:
        raise ValueError(
            f"violations_table must be a three-part UC name (catalog.schema.table); "
            f"got {violations_table!r}"
        )

    created = {"_value": False}

    def _ensure_table() -> None:
        if created["_value"] or not auto_create:
            return
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {violations_table} ("
            f"  ts TIMESTAMP NOT NULL,"
            f"  agent_name STRING,"
            f"  operation STRING,"
            f"  action STRING,"
            f"  reason STRING,"
            f"  policy_id STRING,"
            f"  domain STRING,"
            f"  context STRING,"
            f"  metadata STRING"
            f") USING DELTA"
        )
        try:
            run_sql(ws, ddl, warehouse_id=warehouse_id)
        except Exception as e:
            logger.warning(
                "make_uc_violation_writer: CREATE TABLE IF NOT EXISTS %s failed: %s — "
                "subsequent inserts may fail.",
                violations_table, e,
            )
        created["_value"] = True

    def _transport(req: dict[str, Any]) -> dict[str, Any]:
        if req.get("type") != "violation_report":
            # Not a violation report — pass through with allow so this
            # writer can be composed with an evaluate transport.
            return {"action": "allow"}
        _ensure_table()
        decision = req.get("decision") or {}
        context = req.get("context") or {}
        ts = time.time()
        # Inline values via _sql_str_literal — bind params aren't supported
        # for every column type via Statements API consistently, and the
        # data shape here is well-controlled (no user-typed SQL).
        sql = (
            f"INSERT INTO {violations_table} "
            f"(ts, agent_name, operation, action, reason, policy_id, domain, context, metadata) "
            f"VALUES ("
            f"  CAST({ts} AS TIMESTAMP), "
            f"  {_sql_str_literal(str(context.get('agent_name') or ''))}, "
            f"  {_sql_str_literal(str(context.get('operation') or req.get('operation') or ''))}, "
            f"  {_sql_str_literal(str(decision.get('action') or ''))}, "
            f"  {_sql_str_literal(str(decision.get('reason') or ''))}, "
            f"  {_sql_str_literal(str(decision.get('policy_id') or ''))}, "
            f"  {_sql_str_literal(str(decision.get('domain') or ''))}, "
            f"  {_sql_str_literal(json.dumps(context, default=str))}, "
            f"  {_sql_str_literal(json.dumps(decision.get('metadata') or {}, default=str))}"
            f")"
        )
        try:
            run_sql(ws, sql, warehouse_id=warehouse_id)
        except Exception as e:
            logger.warning(
                "Failed to insert violation row into %s: %s",
                violations_table, e,
            )
        return {}

    return _transport


# ---------------------------------------------------------------------------
# MCP evaluate transport
# ---------------------------------------------------------------------------


def make_mcp_transport(
    mcp_url: str,
    *,
    tool_name: str,
    timeout_seconds: float = 5.0,
    auth_headers: dict[str, str] | None = None,
) -> TransportFn:
    """Return a transport that calls a watchdog MCP tool for evaluate requests.

    Uses the streamable-HTTP MCP client (``mcp.client.streamable_http``)
    to connect to watchdog's MCP endpoint, invoke ``tool_name`` with the
    operation context, and parse the result into the response shape
    ``WatchdogClient.evaluate`` expects.

    Non-evaluate requests (violation reports) pass through as no-op so
    this transport can be composed with a violation writer via
    ``make_watchdog_transport``.

    Args:
        mcp_url: HTTPS URL of watchdog's streamable MCP endpoint.
        tool_name: Name of the MCP tool to call (one of watchdog's 13).
            The tool's expected ``arguments`` shape is the request dict
            this transport receives, minus the ``type`` discriminator.
        timeout_seconds: Per-call timeout. Default 5s.
        auth_headers: Optional headers to send on the connection
            (e.g. ``{"Authorization": "Bearer <token>"}``).

    The MCP call runs synchronously via ``asyncio.run`` — fine for the
    apx-agent runtime since the WatchdogClient API is sync. For
    high-frequency call sites, consider wrapping this in a thread pool
    or replacing with a custom async-aware transport.
    """
    import asyncio
    import json

    def _transport(req: dict[str, Any]) -> dict[str, Any]:
        if req.get("type") == "violation_report":
            # Violation reports go to the UC table writer, not MCP.
            return {}

        async def _call() -> dict[str, Any]:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(mcp_url, headers=auth_headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        name=tool_name,
                        arguments={
                            "operation": req.get("operation"),
                            "context": req.get("context") or {},
                        },
                    )
                    # MCP tool results are returned as a list of content blocks.
                    # We expect the tool to return a single JSON-encoded text block
                    # that parses to {"action": ..., "reason": ..., ...}.
                    for block in getattr(result, "content", None) or []:
                        text = getattr(block, "text", None)
                        if not text:
                            continue
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            logger.warning(
                                "Watchdog MCP tool %s returned non-JSON text — allowing.",
                                tool_name,
                            )
                            return {"action": "allow"}
                    return {"action": "allow"}

        try:
            return asyncio.run(asyncio.wait_for(_call(), timeout_seconds))
        except Exception as e:
            logger.warning(
                "Watchdog MCP call to %s failed: %s — falling back to allow.",
                tool_name, e,
            )
            return {"action": "allow", "reason": f"watchdog mcp transport error: {e}"}

    return _transport


# ---------------------------------------------------------------------------
# Combined transport — evaluate via MCP + violations via UC table
# ---------------------------------------------------------------------------


def make_watchdog_transport(
    *,
    mcp_url: str,
    mcp_tool_name: str,
    violations_table: str,
    ws: Any,
    warehouse_id: str | None = None,
    mcp_timeout_seconds: float = 5.0,
    mcp_auth_headers: dict[str, str] | None = None,
) -> TransportFn:
    """Return a transport bundling MCP evaluate + UC table violation writing.

    This is the canonical watchdog transport: evaluate decisions come
    from watchdog's MCP tool, violation reports flow into watchdog's
    UC Delta table. Both wire-protocol contracts answered, both
    composed into one callable that ``WatchdogClient(transport=...)``
    consumes.

    Args:
        mcp_url: Watchdog's streamable-HTTP MCP endpoint.
        mcp_tool_name: The MCP tool name for policy evaluation.
        violations_table: Three-part UC name of the violations Delta
            table watchdog reads from.
        ws: ``WorkspaceClient`` used for SQL writes.
        warehouse_id: Optional SQL warehouse for the violation writes.
        mcp_timeout_seconds: MCP call timeout.
        mcp_auth_headers: Optional MCP connection headers.
    """
    eval_transport = make_mcp_transport(
        mcp_url,
        tool_name=mcp_tool_name,
        timeout_seconds=mcp_timeout_seconds,
        auth_headers=mcp_auth_headers,
    )
    violation_transport = make_uc_violation_writer(
        violations_table,
        ws=ws,
        warehouse_id=warehouse_id,
    )

    def _combined(req: dict[str, Any]) -> dict[str, Any]:
        if req.get("type") == "violation_report":
            return violation_transport(req)
        return eval_transport(req)

    return _combined
