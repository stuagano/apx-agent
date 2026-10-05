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

model = compile_agent(
    agent,
    target="responses_agent",
    model="system.ai.claude-sonnet-4-6",
    requirements=needs,
)
```

`responses_agent` returns an actual `mlflow.pyfunc.ResponsesAgent` model with
`predict` and `predict_stream`. It uses the existing APX Responses execution path.
Save and load it through MLflow:

```python
import mlflow.pyfunc

mlflow.pyfunc.save_model("orders-model", python_model=model)
loaded = mlflow.pyfunc.load_model("orders-model")
result = loaded.predict({"input": [{"role": "user", "content": "Order 123?"}]})
```

Saving an artifact does not register or deploy it. Include APX and your tool
dependencies in the model environment. Cached runtime handlers are rebuilt after
loading. Explicit checkpointers, stores and tool closures must be serializable;
use MLflow models-from-code when constructing external runtime resources.

Existing Apps code can continue using `compile_to_responses_agent`, which returns
the original `non_streaming` and `streaming` handlers for AgentServer decorators.

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
    session_store="orders-sessions",  # An existing managed Session Store.
)
```

The result is a real `DurableAgentServer` FastAPI application. It registers the
native invocation handler. For example, send this body to `/api/invocations`:

```json
{
  "id": "588dc28c-c0b2-47ea-ac50-e09b99c5ba42",
  "session_id": "order-conversation-123",
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
| `sessions` | Declared session backend, explicit conversation store or LlmAgent checkpointer | Declared managed session or explicit LlmAgent checkpointer / `session_store` |
| `approvals` | LlmAgent checkpointer | Declared managed session or explicit LlmAgent checkpointer / `session_store`; app-scoped |
| `long_term_memory` | Already finalized, reachable declared store | Rejected |
| `recovery` | Rejected by this factory | Rejected; no recovery handler is registered |
| `streaming` | Existing streaming handler | Rejected; complete message output only |

A configured checkpointer does not prove persistence across process restarts.
It can be an in-memory saver or a separately configured synchronous persistent
saver. A report is a compatibility check, not a live storage or deployment probe.
SDK event streaming is distinct from the token-streaming requirement above.

With a session binding, supply top-level `session_id` for continuity across
invocation IDs. Session IDs inside `input` are rejected: the SDK must see the
session before scheduling execution, so it can serialize turns in that session. The
keys are namespaced by agent name. These sessions are **shared app-auth state**,
not isolated user conversations. Resume an interrupted approval with the same
top-level session and `{"resume": "approve"}` as input. The underlying SDK
records invocation completion separately from the handler's `interrupted` result.

`session_store` creates the SDK's `DatabricksSessionStoreSaver` with the explicit
`service_ws` client. It binds an existing store; it does not provision one or
silently fall back to memory. Do not combine it with `checkpointer`. Store access
errors propagate. Persisted graph checkpoints and automatic runtime recovery are
separate capabilities; recovery remains unsupported by this compiler.

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

Declare `target: durable_agent_server` in your agent specification. Generation
selects the native entrypoint and includes `apx-agent[langgraph,agentbricks]` in
the project dependencies. Omitting `target` preserves the existing ResponsesAgent
default. Existing Python and AppKit host choices remain available.

Declare a managed session with `session.type: managed` and `session.store_name`.
The same declaration is passed to `compile_agent(config=config, service_ws=ws)`
and inspected by `inspect_target(agent, config=config)`. Local native execution
keeps that remote store binding. Managed sessions bind an existing store and
normalize `auto_create` to false; explicit provisioning requests are rejected.

The older `AGENT_SESSION_STORE` environment variable remains compatible with
hand-authored projects, but cannot disagree with a declared store. Explicit
checkpointers remain available through `compile_agent(..., checkpointer=...)`;
they cannot be combined with a declared managed store. Unsupported target/state
combinations fail validation instead of silently substituting local memory.

## Deploy the durable target into an App Space

Put these choices in one agent specification, for example `orders.yaml`:

```yaml
name: orders
target: durable_agent_server
model: system.ai.claude-sonnet-4-6
deploy:
  space: your-space
session:
  type: managed
  store_name: orders-sessions
```

```sh
apx-agent agents run orders.yaml
apx-agent agents deploy orders.yaml --target apps --profile your-selected-profile
```

Use an APX installation with the `agentbricks` extra for native local execution
or deployment. The space and Session Store must already exist, with the required
access and tracing configured. APX generates the native `space` field and omits
dedicated-app resources, scopes, scaling and the keepalive job. No manual bundle
cleanup is needed. The route reads inherited policy and refuses to move an
existing app between spaces or widen its grants.

Deployment first applies the bundle, verifies the app's space and LIQUID compute,
then uses the Agent Bricks SDK to create or reuse its managed Runtime Store.
The SDK checks app and service-principal ownership. For generated projects APX
passes the derived connection coordinates as bundle variables, reapplies the
bundle and starts the app. Source configuration stays unchanged. Runtime Store
variables are SDK-owned, not user overrides. Older hand-authored bundles retain
their existing binding behavior and conflict checks.

The managed Runtime Store records native invocations. `session.store_name`
separately binds an existing conversation checkpoint store. Neither binding adds
long-term memory or a recovery handler to this compiler. `/readyz` checks Runtime
Store reachability and reports whether the SDK is durable; it does not execute a
model or tool. Local tests cover SDK ownership validation and the deployment
sequence with a fake workspace; they do not prove a new live deployment.

The local tests exercise MLflow save/load with prediction and streaming, real graph/tool execution, an actual SDK HTTP invocation
and result readback, idempotent invocation IDs, checkpoint history, approval
pause/resume, and capability refusals. Managed session tests reconstruct the
server and saver against a local REST fake and read persisted checkpoints back.
They do not establish live workspace
permissions, managed-store durability, or production deployment readiness.
