# Compliance posture

## Watchdog integration

For compliance posture (cross-domain policies, violation lifecycle, owner accountability), apx-agent integrates with [databricks-watchdog](https://github.com/stuagano/databricks-watchdog) rather than rolling its own policy engine. Three pieces:

```python
from apx_agent import Agent, WatchdogClient, WatchdogGuard, emit_agent_metadata

# 1. Adapter for the watchdog policy-decision and violation-report calls.
#    The transport is pluggable — use the no-op default until the wire API
#    is pinned down, or pass a callable that hits watchdog's HTTP/MCP surface.
watchdog = WatchdogClient(transport=my_transport)

# 2. Bridge watchdog decisions into the existing apx-agent hooks.
guard = WatchdogGuard(watchdog, agent_name="customer_triage")

agent = Agent(
    instructions="...",
    tools=[...],
    input_guardrails=[guard.for_input()],
    output_guardrails=[guard.for_output()],     # supports redact decisions
    before_tool=guard.for_tool(),
    before_model=guard.for_model(),
)

# 3. Emit the agent's metadata for watchdog's crawler.
metadata = emit_agent_metadata(agent, name="customer_triage",
                               model="databricks-claude-sonnet-4-6")
# → JSON-serializable dict: name, model, instructions, tools (with UC sync metadata),
#   sub-agents, resources. Drop into a UC manifest table, MLflow tag, or
#   whatever stable shape watchdog crawls.
```

Watchdog returns `WatchdogDecision(action, reason, policy_id, domain, redacted_content, metadata)`. `reject` short-circuits the call; `redact` rewrites content (for outputs); `allow` is pass-through. **On transport failure (or a non-decision response) the client fails *closed* — `action="reject"` — by default**, so a configured-but-unreachable governance gate blocks rather than silently allowing (fail-open governance is worse than none). Construct `WatchdogClient(transport=..., fail_closed=False)` to restore fail-open for availability-first deployments. The default no-op transport never errors, so agents with no watchdog configured are unaffected.

### End-to-end wiring

The three wire-protocol contracts are pinned:

| Contract | Mechanism | Helper |
|---|---|---|
| Metadata for watchdog's crawler | UC tags on the registered model (`apx.agent.*`) | `set_uc_tags_for_agent(agent, registered_model_name=..., model=...)` |
| Runtime policy decisions | Guardrails MCP `evaluate_operation` | `make_mcp_transport(mcp_url, tool_name="evaluate_operation")` |
| Violation reports | INSERT into Watchdog-owned `runtime_violations` Delta table | `make_uc_violation_writer(violations_table, ws=...)` |

```python
from apx_agent import (
    Agent, WatchdogClient, WatchdogGuard,
    make_watchdog_transport, set_uc_tags_for_agent, log_agent,
)
from databricks.sdk import WorkspaceClient

ws = WorkspaceClient()

# One-time at deploy: write metadata as UC tags on the registered model
log_agent(agent, model="databricks-claude-sonnet-4-6",
          registered_model_name="main.agents.customer_triage")
set_uc_tags_for_agent(
    agent,
    registered_model_name="main.agents.customer_triage",
    model="databricks-claude-sonnet-4-6",
    name="customer_triage",
)

# Runtime: Guardrails MCP for evaluate + UC table for violations
transport = make_watchdog_transport(
    mcp_url="https://guardrails.example.com/mcp",  # Guardrails MCP (not Watchdog MCP)
    mcp_tool_name="evaluate_operation",
    violations_table="platform.watchdog.runtime_violations",
    ws=ws,
    warehouse_id="wh-prod",
)
watchdog = WatchdogClient(transport=transport)
guard = WatchdogGuard(watchdog, agent_name="customer_triage")

agent = Agent(
    instructions="...",
    tools=[...],
    input_guardrails=[guard.for_input()],
    output_guardrails=[guard.for_output()],
    before_tool=guard.for_tool(),
    before_model=guard.for_model(),
)
```

The MCP transport calls `tools/call` on the **Guardrails** streamable HTTP MCP endpoint (`evaluate_operation`); the UC writer auto-creates the `runtime_violations` table on first use (`auto_create=True`) and `INSERT`s a row per reject/redact decision. Watchdog MCP (13 query tools) remains the posture-query surface — use `apx-agent watchdog status` which defaults to Guardrails `get_agent_compliance`. Both transports are pluggable — pass a custom `transport` to `WatchdogClient` for a different wire shape.

See also Watchdog's [apx-agent integration guide](https://github.com/stuagano/databricks-watchdog/blob/main/docs/guide/how-to/apx-agent-integration.md).

## Audit log schema

Every framework-emitted MLflow span carries a consistent `apx.*` attribute set so downstream consumers (watchdog, compliance dashboards, ad-hoc SQL over the traces table) can query without knowing anything about specific agents.

```python
from apx_agent import AuditAttrs
# AuditAttrs.AGENT_NAME           = "apx.agent.name"
# AuditAttrs.SESSION_ID           = "apx.session.id"
# AuditAttrs.OPERATION            = "apx.operation"  # predict | tool_call | model_call | sub_agent_call
# AuditAttrs.USER_TOKEN_PROVIDED  = "apx.user.token_provided"
# AuditAttrs.TOOL_NAME            = "apx.tool.name"
# AuditAttrs.TOOL_UC_FUNCTION     = "apx.tool.uc_function"
# AuditAttrs.TOOL_INPUT_KEYS      = "apx.tool.input_keys"
# AuditAttrs.TOOL_INPUT_HASH      = "apx.tool.input_hash"
# AuditAttrs.TOOL_OUTPUT_TYPE     = "apx.tool.output_type"
# AuditAttrs.TOOL_OUTPUT_SIZE     = "apx.tool.output_size"
# AuditAttrs.TOOL_DURATION_MS     = "apx.tool.duration_ms"
# AuditAttrs.MODEL_ENDPOINT       = "apx.model.endpoint"
# AuditAttrs.MODEL_INPUT_TOKENS   = "apx.model.input_tokens"
# AuditAttrs.MODEL_OUTPUT_TOKENS  = "apx.model.output_tokens"
# AuditAttrs.WATCHDOG_ACTION      = "apx.watchdog.action"
# AuditAttrs.WATCHDOG_POLICY_ID   = "apx.watchdog.policy_id"
# ...
```

Where they're set automatically:

| Site | Attributes recorded |
|---|---|
| `predict` / `predict_stream` | `apx.operation`, `apx.model.endpoint`, `apx.session.id`, `apx.user.token_provided`, `apx.model.streaming`, `apx.model.input_messages` |
| Tool call lifecycle (via `_AgentCallbackHandler`) | `apx.tool.name`, `apx.tool.input_keys`, `apx.tool.input_hash`, `apx.tool.output_type`, `apx.tool.output_size`, `apx.tool.duration_ms` |
| Model call lifecycle | `apx.model.input_tokens`, `apx.model.output_tokens` (when the LLM returns usage info) |
| Watchdog reject/redact | `apx.watchdog.action`, `apx.watchdog.policy_id`, `apx.watchdog.reason`, `apx.watchdog.domain`, `apx.agent.name` |

Custom code can add attributes through the same schema:

```python
from apx_agent import set_audit_attrs, safe_span

with safe_span("custom_step") as span:
    set_audit_attrs(
        span,
        operation="sub_agent_call",
        subagent_endpoint="endpoints/billing",
        tool_duration_ms=123,
    )
```

Typos fail loud — `set_audit_attrs` raises `ValueError` for unknown kwargs so the audit schema stays canonical instead of drifting across call sites. Use `hash_for_audit(value)` to fingerprint inputs/outputs without exfiltrating raw content.

Watchdog and any other consumer can query the trace table (e.g. `system.access.audit_logs` or a workspace trace export) by `apx.*` keys to produce compliance reports without parsing agent-specific schemas.

## Local guards — zero-latency runtime checks

`_guards.py` ships a small set of in-process callables that plug into the existing hooks. Pair with `WatchdogGuard` for layered governance — local checks for things that should fail in microseconds, watchdog for cross-domain posture evaluation.

```python
from apx_agent import (
    Agent, RateLimit, ToolAllowlist, prompt_injection_heuristic, compose,
    WatchdogGuard,
)

agent = Agent(
    instructions="...",
    tools=[...],
    input_guardrails=[
        prompt_injection_heuristic(),           # fast: regex over the user message
        WatchdogGuard(watchdog).for_input(),    # slow: full posture eval
    ],
    before_tool=compose(
        RateLimit(per_minute=60),
        ToolAllowlist({"classify_intent", "get_recent_orders"}),
        WatchdogGuard(watchdog).for_tool(),
    ),
)
```

| Guard | Purpose |
|---|---|
| `RateLimit(per_minute=..., burst=..., principal_key=...)` | In-process token-bucket per principal (default: global bucket). Pass a `principal_key` callable to scope per-user. Thread-safe. |
| `prompt_injection_heuristic(patterns=..., message=...)` | Regex pass over message content. Default pattern set is small + high-specificity (favors false negatives over false positives — the slower-loop watchdog catches the long tail). |
| `ToolAllowlist({names})` / `ToolDenylist({names})` | Gate tool calls by name. |
| `compose(*callbacks)` | Chain N callbacks. Short-circuits on the first non-`None` return (for `input_guardrails`-style hooks) or first exception (for `before_*` hooks). |

These are runtime helpers, not a policy engine. Compliance posture (cross-domain rules, ontology, violation tracking, owner accountability) lives in [databricks-watchdog](https://github.com/stuagano/databricks-watchdog) — wire `WatchdogGuard` next to these for the full layered story.
