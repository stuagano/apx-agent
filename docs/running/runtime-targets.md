# Build locally and deploy to Databricks Apps

Use Databricks Apps as the default destination for new APX agents. Declare the
agent's tools, instructions, composition and governance, run it locally, then
deploy the application. APX generates the server entrypoint, dependencies and
bundle configuration.

## Custom Agent Bricks apps that scale to zero

**Run your custom agent when it is needed; let its app compute scale to zero
when idle.** APX compiles your agent declaration to `DurableAgentServer` and
deploys it as a custom Agent Bricks app into an App Space on the on-demand
runtime. This is the scale-to-zero deployment path described below.

Keep state outside the app process: the managed Runtime Store persists native
invocations, the managed Session Store persists conversation checkpoints, and
managed memory persists facts across conversations. The app can stop without
discarding that external state. On the next request, the platform starts the
app again; expect a cold start. Persisting state does not by itself provide
automatic recovery of an interrupted tool execution.

Scale-to-zero belongs to the App Space hosting model; selecting a Python server
class alone does not enable it. Use a workspace with the custom Agent Bricks
App Space deployment path enabled. APX verifies App Space membership and
`LIQUID` compute during deployment and omits dedicated-instance scaling and
keepalive configuration. The standard dedicated Apps deployment remains a
separate hosting choice.

Databricks describes the on-demand, zero-to-one hosting behavior in
[Serverless Micro Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/governed-agentic-app-building).
That page's Beta FAQ still restricts its documented creation flow to Genie App
Builder; the custom Agent Bricks deployment route here requires the corresponding
workspace support. Scale-to-zero describes app compute, not the billing or
lifecycle of separately provisioned memory, sessions, model endpoints or tools.

## Start with an Apps project

```sh
uv add apx-agent
uv run apx-agent doctor
uv run apx-agent agents scaffold my-agent --target apps
cd my-agent
uv sync
uv run apx-agent agents run
```

After testing locally, deploy from the generated project directory:

```sh
uv run apx-agent agents deploy --target apps --profile your-selected-profile
```

Replace the profile placeholder with your chosen Databricks profile. Edit the
generated agent declaration as you develop. The standard Apps path uses MLflow
AgentServer with Responses-compatible handlers; it does not require you to save
or register an MLflow model.

## Choose the server behavior when needed

Apps is the deployment destination. The server determines how requests execute.
The standard Apps server remains the default. For native durable invocations
and managed session binding in an App Space, declare
`target: durable_agent_server` as shown below. APX currently requires this explicit
choice; it does not automatically select a server from requested capabilities.

The native durable integration supports request-user clients, declared APX
memory and persisted message streaming. Service-identity LlmAgents can opt into
managed-checkpoint recovery. Request-user recovery, raw request/header
dependencies and remote OBO forwarding remain unsupported. Check the
[compatibility table](#compatibility-checks) before choosing it.

In an agent specification, `target` selects the compiler output.
The CLI's `--target apps` selects the deployment destination. These are separate
choices despite sharing the word "target".

For the durable target, `apx-agent agents deploy --target apps` compiles the
existing operation authorization plan into `agent.toml` in the staged app
source directory. Its native `[auth.user]` declaration requires request-user
authentication for user tools or declared memory and includes their deduplicated
API scopes. Service-only agents keep `required = false`, preserving eligibility
for service-identity recovery. AgentKit reads this manifest at startup.
The staged manifest is generated; change the APX agent/tool declarations to
change it. APX refuses to overwrite an authored manifest there.

The deployment still uses Databricks Bundles: APX reconciles scopes for ordinary
Apps and checks inherited scopes for App Spaces. Generating the native manifest
does not grant data permissions or enable request-user recovery.

## Deploy the durable target into an App Space

Use this route for a custom Agent Bricks app that can scale to zero. Declare
remote session and memory stores for state that must survive the app stopping:

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
memory:
  type: managed
  store_name: orders-memory
```

```sh
apx-agent agents run orders.yaml
apx-agent agents deploy orders.yaml --target apps --profile your-selected-profile
```

Use an APX installation with the `agentbricks` extra for native local execution
or deployment. The space, Session Store and memory store must already exist, with the required
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
separately binds an existing conversation checkpoint store. Long-term memory is a separate `memory` declaration; neither session binding
adds it or enables recovery; recovery requires an explicit `recovery: true`
declaration and compatible agent. `/readyz` checks Runtime
Store reachability and reports whether the SDK is durable; it does not execute a
model or tool. Local tests cover SDK ownership validation and the deployment
sequence with a fake workspace; they do not prove a new live deployment.

## Migrate from ResponsesAgent to DurableAgentServer

You can keep an APX agent's instructions, compatible tools and supported graph
composition while changing how it is served. This is a serving migration, not
an automatic conversion of an MLflow model artifact or its stored conversations.

### 1. Start from the agent source and check compatibility

If your ResponsesAgent was produced by APX, reuse the original APX declaration,
before it was compiled into a model. If you wrote a custom ResponsesAgent subclass,
APX cannot import its `predict` implementation as an agent declaration: first
express its instructions, tools and orchestration in APX, and verify equivalent
behavior. Custom preprocessing and postprocessing need explicit treatment too.

Check the [compatibility table](#compatibility-checks) against the behavior your
customers actually use. Keep the existing Responses deployment if it needs
raw request/header dependencies, remote OBO forwarding or request-user crash
recovery; the current APX durable target does not implement those capabilities.
UserClient, SQL and Principal dependencies use the SDK's request-user client.
Declared managed memory uses the AgentKit API and the authenticated principal;
legacy UC memory entries require explicit migration. Do not
replace user-scoped access with service credentials to make a migration pass.

For an existing APX `LlmAgent`, inspect the proposed session binding before
constructing a server:

```python
from apx_agent import RuntimeRequirements, inspect_target

# agent is your existing APX declaration, not a compiled ResponsesAgent model.
report = inspect_target(
    agent,
    target="durable_agent_server",
    session_store="orders-sessions",
    requirements=RuntimeRequirements(sessions=True),
)
report.require_compatible()
```

Include every required behavior in `RuntimeRequirements`; for example, add
`streaming=True` if clients require streamed tokens. Declared identity, session
and memory needs are also checked. Pass `config=config` when those declarations
live in an `AgentConfig`. This is an offline compatibility check, not verification
that the named store exists or is accessible. Managed session binding currently
requires `LlmAgent`.

### 2. Generate a separate Apps deployment

Copy your agent specification, retaining the tools, instructions and other
compatible declarations. Give the new deployment a distinct name, set
`target: durable_agent_server`, and add `deploy.space` and the managed session
binding shown in [the App Space example](#deploy-the-durable-target-into-an-app-space).
Use an existing space and Session Store with the required access. Install the
`agentbricks` extra in the APX environment used to run and deploy it.

Run the copied specification locally, then deploy it with `--target apps` and
your selected profile. Local execution retains the declared remote session
binding. Keep the original endpoint available during validation; APX refuses
to move an existing app between spaces. For Python-authored agents, use the
[native server API](#python-api-construct-a-native-durable-server) with the
original APX declaration and an explicit service client.

### 3. Update callers for the native invocation contract

The durable target does not accept ResponsesAgent requests unchanged.

| Client concern | Responses path | Native durable path |
|---|---|---|
| Input | Responses `input` items | `/api/invocations` with `id`, top-level `session_id`, and `input.messages` containing user text |
| Conversation continuity | Existing Responses history/session handling | Stable top-level `session_id` with a configured checkpoint store |
| Retry | Existing client behavior | Reuse the same invocation `id` for the same request; use a new ID for a new turn |
| Result | Responses output items or streaming events | Native invocation envelope; handler `status` and serialized LangChain `messages` are inside `output` |
| Streaming | Responses SSE events | Set top-level `stream: true`; consume persisted `agent.message.delta` events containing serialized LangChain message chunks |
| Approval resume | Existing Responses approval handling | New invocation ID, same session, and `input: {"resume": "approve"}` |

Use the [native request example](#python-api-construct-a-native-durable-server)
to update your client. Do not forward Responses content items, tool-result
messages, `custom_inputs` or user tokens into the native input. Check the handler's
`output.status`: an invocation can finish while the agent is paused for approval.

### 4. Establish new sessions and validate before switching traffic

APX does not migrate existing Responses conversation history, checkpoints,
pending approvals or long-term memory into the managed Session Store. An existing
declared APX memory backend can be retained separately; validate that the
authenticated principal IDs match its existing scopes. Plan a
new-session cutover; let existing conversations and pending approvals finish
on the original endpoint. Reusing a session ID alone does not transfer state.
Native session keys also include the agent name, so keep that name stable after
cutover. With request-user authentication, the SDK isolates invocation/session
IDs by caller and APX also scopes graph checkpoints by authenticated principal.
App-auth-only agents retain shared app-scoped sessions and approvals.

On the new deployment, verify a real tool call, a second turn in the same session,
same-request retry, and approval pause/resume if used. Verify checkpoint readback
after recreating the server against the same store. Confirm the intended service
identity and access controls. A successful `/readyz` check verifies Runtime Store
reachability, not these agent behaviors or automatic recovery.

Once those checks pass, direct new conversations to the new app. Keep the original
deployment available for rollback; switching back does not transfer conversations
created on the durable deployment. Retire the old endpoint only after its remaining
sessions and consumers have been accounted for.

## Generated projects

Declare `target: durable_agent_server` in your agent specification. Generation
selects the native entrypoint and includes `apx-agent[langgraph,agentbricks]` in
the project dependencies. Omitting `target` preserves the existing Responses-compatible Apps
entrypoint. Existing Python and AppKit host choices remain available.

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

## Python API: construct a native durable server

Install `apx-agent[agentbricks]` in the host project's dependencies. The normal
package and `all` extra do not require the optional server SDK.

```python
from databricks.sdk import WorkspaceClient
from apx_agent import LlmAgent, compile_agent

agent = LlmAgent(name="orders", instruction="Help with order questions.")

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
| `user_identity` | Existing OBO handling | SDK request-user client for UserClient, SQL and Principal; raw Request/Headers and remote OBO forwarding rejected |
| `sessions` | Declared session backend, explicit conversation store or LlmAgent checkpointer | Declared managed session or explicit LlmAgent checkpointer / `session_store` |
| `approvals` | LlmAgent checkpointer | Declared managed session or explicit LlmAgent checkpointer / `session_store`; user-scoped when request-user auth is required |
| `long_term_memory` | Supported with a reachable declared store; managed memory uses AgentKit | Supported with AgentKit managed memory or another declared backend; actor comes from trusted caller identity |
| `recovery` | Not implemented by this factory | Opt-in managed-checkpoint continuation for service-identity LlmAgents; request-user recovery remains unsupported |
| `streaming` | Existing streaming handler | Supported: persisted incremental deltas, or a persisted validated final message when output checks require buffering |

A configured checkpointer does not prove persistence across process restarts.
It can be an in-memory saver or a separately configured synchronous persistent
saver. A report is a compatibility check, not a live storage or deployment probe.
APX emits model message chunks through the SDK's ordered event store. A model
that does not support incremental output may emit a complete message in one
chunk. Event replay does not re-execute tools; it is distinct from crash recovery.

With a session binding, supply top-level `session_id` for continuity across
invocation IDs. Session IDs inside `input` are rejected: the SDK must see the
session before scheduling execution, so it can serialize turns in that session. The
keys are namespaced by agent name, and by authenticated principal for
request-user execution. App-auth-only sessions remain shared app state. Resume an interrupted approval with the same
top-level session and `{"resume": "approve"}` as input. The underlying SDK
records invocation completion separately from the handler's `interrupted` result.

`session_store` creates the SDK's `DatabricksSessionStoreSaver` with the explicit
`service_ws` client. It binds an existing store; it does not provision one or
silently fall back to memory. Do not combine it with `checkpointer`. Store access
errors propagate. Persisted graph checkpoints and automatic runtime recovery are
separate capabilities; enable recovery explicitly for compatible agents below.

UserClient, SQL and Principal dependencies automatically require SDK request-user
authentication. An explicit `RuntimeRequirements(user_identity=True)` or an SDK
manifest requiring user auth also enables it. The SDK owns transient credentials;
APX resolves the principal from the authenticated client's current-user API and
never substitutes the service client. Raw Request/Headers dependencies and
per-hop remote-agent forwarding remain rejected because their full trusted
request context is not wired.

Declare `memory` alongside `session` when long-term recall is required. APX uses
[workspace-scoped managed memory](https://docs.databricks.com/aws/en/agents/agent-memory/managed-memory)
through the AgentKit SDK. This is separate from the managed Session Store and
works with either ResponsesAgent or the native server targets.

```yaml
memory:
  type: managed
  store_name: orders-memory
```

Install `apx-agent[agentbricks]`, then explicitly provision the store:

```bash
apx-agent memory provision --store orders-memory --profile <profile>
```

Grant the app service principal access using AgentKit's
`memory_store.grant_permission(principal_id)`. The compiler binds the existing
store with its explicit workspace client; it never creates one during a request.
Local execution uses the same remote store when given that workspace client.

Memory tools derive `actor_id` from trusted caller identity. Native servers
require request-user authentication for these tools. APX checks ownership before
returning, updating or deleting an entry by ID. Actor IDs partition data; access
control is at the store level. Use separate stores for strict security boundaries.
Missing or degraded memory fails native compilation.

Search uses BM25, returns at most 100 results, and has no search pagination.
Tags, importance and arbitrary metadata are not persisted or filtered by this
backend. List pagination is handled by the SDK.

**Migrating legacy UC memory:** `catalog.schema.name` is rejected with an explicit
migration message. Provision a workspace memory store, export the old entries
with authorized access, and import their contents with a reviewed mapping from
legacy scopes to verified actor IDs. Confirm per-user readback before changing
`store_name`. APX does not automatically move historical data or checkpoints.
Direct Python callers now pass `ws=workspace_client` to `ManagedMemoryStore`
instead of `api=workspace_client.api_client`.

Set top-level `stream: true` on an invocation to consume native SSE. Each
`agent.message.delta` event carries a `message` containing a serialized LangChain
message chunk, including content or tool-call fragments. The SDK persists these
events and assigns replay cursors; use its invocation events endpoint to replay
them. APX waits for event persistence before continuing generation, so write
failures propagate instead of dropping chunks. Complete results still use the
native output envelope. Approval pauses and session token budgets remain enforced.
Output guardrails, output schemas and post-model/agent hooks buffer generation
until validation finishes. Streaming remains supported: a successful turn emits
`agent.message.completed` with the final validated message. Intermediate model
and tool messages are not published on this path. Rejected output emits no
message event. Clients receive a validated final message instead of incremental
tokens, with the same native event persistence and replay mechanism.

### Opt into service-identity recovery

For a service-identity `LlmAgent`, use a managed Session Store and explicitly
enable recovery:

```yaml
name: background-agent
target: durable_agent_server
recovery: true
model: system.ai.claude-sonnet-4-6
deploy:
  space: your-space
session:
  type: managed
  store_name: background-sessions
```

The equivalent Python requirement is `RuntimeRequirements(recovery=True)` with
`session_store="background-sessions"` or an explicit
`DatabricksSessionStoreSaver`. A durable Runtime Store must also be bound on the
deployed server so the SDK can dispatch replacement attempts. In-memory
checkpointers do not qualify. Recovery currently requires an unwrapped
`LlmAgent`: compositions, agent-level guardrails and before/after-agent callbacks, templated
instructions, output schemas/keys and agent timeouts are rejected.

APX records the invocation ID, input digest, message boundary and starting token
budget in checkpoint metadata. A replacement worker continues the current
invocation's checkpoint; if no checkpoint exists for it, it submits the original
input. Already-completed checkpointed results are returned without re-executing
the graph. Changed input for the same invocation and recovery behind a newer
session checkpoint are rejected. Approval pauses remain paused, and recovered
turns recalculate their budget from the saved baseline rather than counting the
same tokens twice. Budget checkpoint failures propagate.

**Recovery is at-least-once for uncommitted work.** Tools and callbacks must be
safe to replay. An external write can succeed immediately before the worker
dies, leaving no committed tool result; that tool may run again. Use a stable
business/idempotency key or reconcile the write with the external system. APX
does not promise exactly-once external effects. Stream events from interrupted
attempts can remain in the event history; consumers must account for attempt
boundaries when displaying recovered generation.

AgentKit 0.4.0 rejects
request-user execution after worker failure because credentials are transient.
Managed-memory tools require that request-user identity, so enabling memory,
streaming and automatic background recovery together requires SDK support for
re-establishing the caller's authorized execution context.

Waking from idle scale-to-zero and loading persisted sessions and memory is a
separate supported path; it does not require enabling crash recovery.

## Optional: package an MLflow model

Use this path when you need an MLflow model artifact for registration or
Model Serving. It is not a prerequisite for Apps deployment, tracing or
evaluation. The same APX agent declaration can be compiled into a
`ResponsesAgent` model with `predict` and `predict_stream`.

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

## Verification boundaries

The local tests exercise MLflow save/load with prediction and streaming, real graph/tool execution, an actual SDK HTTP invocation
and result readback, idempotent invocation IDs, checkpoint history, approval
pause/resume, and capability refusals. Managed session tests reconstruct the
server and saver against a local REST fake and read persisted checkpoints back.
They do not establish live workspace
permissions, managed-store durability, or production deployment readiness.
