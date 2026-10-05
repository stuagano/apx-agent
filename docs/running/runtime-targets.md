# Compile one agent for different host contracts

APX owns the agent declaration: tools, instructions, composition and governance.
A compilation target owns the host interface. The targets share the existing
LangGraph compiler and governed turn execution; the durable target does not
compile through ResponsesAgent or accept its request schema.

This is the first two-target implementation. It does not automatically select
every possible framework, host or storage backend.

```python
from apx_agent import LlmAgent, RuntimeRequirements, compile_agent, inspect_target

def shipping_status(order_id: str) -> str:
    """Return a status from the application's order service."""
    return "Processing"

agent = LlmAgent(name="orders", tools=[shipping_status])
needs = RuntimeRequirements(streaming=True)
report = inspect_target(agent, target="responses_agent", requirements=needs)
report.require_compatible()

handlers = compile_agent(
    agent,
    target="responses_agent",
    model="system.ai.claude-sonnet-4-6",
    requirements=needs,
)
```

`responses_agent` returns the existing `CompiledResponsesAgent` named handlers:
`non_streaming` and `streaming`. Apply the existing MLflow AgentServer decorators
to those handlers. This API does not create a registered MLflow model or a new
Model Serving deployment. Existing deployment commands retain their behavior.

## Native durable host

Install `apx-agent[agentbricks]` in the host project's dependencies. The normal
package and `all` extra do not require the optional server SDK.

```python
from databricks.sdk import WorkspaceClient
from apx_agent import compile_agent

# Configure this service identity explicitly for your deployment.
service_client = WorkspaceClient(profile="your-selected-profile")
app = compile_agent(
    agent,
    target="durable_agent_server",
    model="system.ai.claude-sonnet-4-6",
    service_ws=service_client,
)
```

The result is a real `DurableAgentServer` FastAPI application. It registers the
native invocation handler. For example, send this body to `/api/invocations`:

```json
{
  "id": "588dc28c-c0b2-47ea-ac50-e09b99c5ba42",
  "input": {
    "messages": [{"role": "user", "content": "Check order 123"}]
  }
}
```

The native handler returns `status` and serialized LangChain `messages` inside
the server's `output` envelope. Inputs are user text messages, supplied either
as a list or under `messages`. Responses API content items, tool results,
credentials, `custom_inputs`, and caller-controlled model selection are not
accepted by this target.

## Compatibility checks

`RuntimeRequirements` describes required behavior. `inspect_target` returns a
`TargetReport` with `capabilities` and `unsatisfied`. `compile_agent` applies the
same checks before constructing the optional host. Existing user/tool identity
and memory/session declarations are included; omitting an explicit requirement
does not erase those declarations.

| Requirement | ResponsesAgent handlers | Native durable target in this release |
|---|---|---|
| `user_identity` | Existing OBO handling | Rejected; app-auth only |
| `sessions` | Explicit conversation store or LlmAgent checkpointer | Explicit LlmAgent checkpointer |
| `approvals` | Explicit LlmAgent checkpointer | Explicit LlmAgent checkpointer; app-scoped |
| `long_term_memory` | Already finalized, reachable declared store | Rejected |
| `recovery` | Rejected by this factory | Rejected; no recovery handler is registered |
| `streaming` | Existing streaming handler | Rejected; complete message output only |

A configured checkpointer does not prove persistence across process restarts.
It can be an in-memory saver or a separately configured synchronous persistent
saver. A report is a compatibility check, not a live storage or deployment probe.
SDK event streaming is distinct from the token-streaming requirement above.

With an explicit checkpointer, supply `input.session_id` for continuity across
invocation IDs; otherwise the handler uses the SDK's context session ID. The
keys are namespaced by agent name. These sessions are **shared app-auth state**,
not isolated user conversations. Resume an interrupted approval with the same
session and `{"session_id": "...", "resume": "approve"}`. The underlying SDK
records invocation completion separately from the handler's `interrupted` result.

User-dependent tools, per-hop remote agents and request-context dependencies are
refused by the app-auth target. The service client is never passed as the user
client. Request-user invocations and recovery attempts are also rejected at
runtime. An Agent Bricks manifest requiring user authentication is incompatible
with this initial target and is rejected when the server is constructed.

The SDK can support managed memory, sessions, recovery and request-user clients.
That does not make those integrations implemented in APX. A future target
extension must wire and test their identity and lifecycle contracts before
changing the corresponding report entries to supported.

## Generated projects

Both project generation and Apps scaffolding emit `agent_server/start_managed.py`.
Set `APX_APPS_HOST=agentbricks` to select it after adding the optional dependency.
`python` remains the default, and `appkit` retains its existing behavior.
The generated entrypoint rejects declared memory/session configuration rather
than dropping it. To bind a checkpointer, author the entrypoint with the explicit
`compile_agent(..., checkpointer=...)` API above. No storage is provisioned by
changing the host selector alone.

The local tests exercise real graph/tool execution, an actual SDK HTTP invocation
and result readback, idempotent invocation IDs, checkpoint history, approval
pause/resume, and capability refusals. They do not establish live workspace
permissions, managed-store durability, or production deployment readiness.
