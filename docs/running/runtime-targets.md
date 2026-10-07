# Build locally and deploy to Databricks Apps

Use Databricks Apps as the default destination for new APX agents. Declare the
agent's tools, instructions, composition and governance, run it locally, then
deploy the application. APX supplies the runtime launcher and compiles the
dependencies and deployment manifests for the selected runtime.

APX connects the agent to the rest of Databricks from one declaration: data,
tools, caller identity, memory, sessions, tracing and deployment. Its compiler
turns those requirements, composition and policies into native bindings and
executable behavior, checking whether the selected runtime can preserve them.
Databricks supplies the runtime, authorization services and managed infrastructure.
Agent Bricks owns native store provisioning, grants and deployment; generated
manifests are compiler output, not another configuration customers must maintain.

If a native Agent Bricks template already expresses the agent you need, use it
directly. APX adds value when declarations replace repeated Databricks integration,
composition or policy wiring, or when you need to move between supported serving contracts. The
compiler currently supports `responses_agent` and `durable_agent_server`;
the compatibility checks below define their limits.

## Deploy custom agents through Agent Bricks

**Declare the agent in APX; let Agent Bricks manage its deployment.** APX
compiles your declaration to `DurableAgentServer`, emits the native manifests,
and hands deployment to the Agent Bricks CLI. You do not need to select or
understand an App Space to use this workflow. Infrastructure placement is a
Databricks product responsibility, not another APX runtime target.

`DurableAgentServer`, the invocation API, and the managed Runtime/Session/Memory
stores are the Agent Bricks product surface, documented canonically in
[`databricks/databricks-ai-bridge` › `integrations/agentbricks`](https://github.com/databricks/databricks-ai-bridge/tree/main/integrations/agentbricks)
(README + `cli.md`). This page tracks that contract and adds what APX layers on
top: the declarative `[tool.apx.agent]` envelope, governance wiring, and
behavior observed on live deployments. Where the two could drift, upstream is
the source of truth for the API and stores; APX owns the declaration and policy.

### How a durable agent works

A durable agent is **a worker plus a database.** The worker runs your agent
code. The database — the managed Runtime Store — holds the record of every job.
The worker is disposable; the database is not. That split is the whole idea:
because the record of what's happening lives in the database and not in the
worker's memory, the worker can be stopped, killed, and replaced without losing
anything.

(There are actually three managed stores, each with its own job: the **Runtime
Store** keeps the record of invocations — requests, status, heartbeats, events,
results; the **Session Store** keeps conversation history; the **Memory Store**
keeps long-term facts recalled across conversations. For the rest of this
section "the database" means the Runtime Store, the one recovery depends on.)

Three things can happen to the worker:

**1. Nobody's using it → it goes to sleep (scale to zero).** With no traffic for
a while, Databricks shuts the worker off so you stop paying for idle compute.
The database is untouched. The next request starts a fresh worker, which reads
the database back and carries on — you pay a few seconds of cold start and lose
nothing. This is automatic, needs no configuration, and is the normal resting
state.

**2. The worker dies mid-job → the job isn't lost.** If a worker crashes, OOMs,
or is redeployed while running an invocation, the job's progress is already in
the database, so a new worker can pick it up and finish it. That is what
"durable" means. (This is opt-in: enable it with `@app.recover` /
`RuntimeRequirements(recovery=True)`, service-identity agents only.)

**3. The catch: who notices an abandoned job and restarts it?** A *living worker*
does. The "scan for abandoned jobs and restart them" routine runs **inside a
worker** (a running job refreshes a heartbeat; a scan loop in the worker watches
for heartbeats that have gone stale). So:

- **Several workers** (`instances > 1`): if one dies, a surviving worker's scan
  loop notices and restarts the job right away — true automatic recovery.
- **One worker**: if the only worker dies, nobody is left to notice. The job
  sits safely in the database and waits until *any* worker exists again — the
  next request's cold start, or a redeploy — and that worker's scan loop then
  finishes it.

**The job is never lost. It may just have to wait for a worker to be around to
pick it up.** (This is why a single-worker hard crash doesn't replay until
something starts a new worker — it has nothing to do with Databricks Apps
restarting the process.)

Two limits worth knowing: a **request-user** (`auth = "user"`) job can't be
recovered — the forwarded caller credential is deliberately never written to the
database, so a restart fails fast with `MCP_USER_AUTH_RECOVERY_UNSUPPORTED`
before your code runs. And recovery is **at-least-once**: a restarted job may
re-run external side effects, so tools must be idempotent (safe to run twice).

### What the Agent Bricks CLI gives you out of the box

`agentbricks deploy` (which APX calls under the hood) is the product that stands
up all of the above. From one command you get, with no extra wiring:

- A hosted app on Databricks Apps with a **URL** to share.
- **Scale-to-zero** when idle and cold-start on the next request — the sleep/wake
  behavior above. You don't turn it on; it's how the serverless Apps tier works.
- **The managed Runtime Store** (a dedicated Postgres/Lakebase database per
  deployment) — provisioned, schema-initialized, and owned by the app's service
  principal. No manual Lakebase grant or Postgres attachment.
- **Model access through the AI Gateway using the app's own identity** — no model
  keys to configure.
- Any **Session / Memory store** declared in config, created if missing and
  granted to the app, plus **tracing** wired in.
- The invocation API (`POST /api/invocations` and friends), streaming, and
  background/reconnect — the one HTTP contract.

Scale-to-zero is a property of that hosting tier, not a flag: there is no
`--scale-to-zero` switch because it is the default, and no server-class setting
turns it on or off. The scaling dial you *do* get is the instance count
(`--instances` / a declared min–max), which sets how many workers run — and
whether a warm floor stays up instead of idling to zero.

### What APX adds on top

If the raw CLI already does all that, what is APX for? APX does not replace it —
it **declares** the agent so you write intent instead of wiring. The CLI hands
deployment to Agent Bricks; APX compiles your one declaration into the inputs
that deployment consumes, and adds the parts the raw product leaves to you:

- **One declarative envelope.** A single `[tool.apx.agent]` block in
  `pyproject.toml` carries the agent's name, model, instructions, tools,
  composition (`sub_agents`), memory/session choice, and deploy/scaling — instead
  of hand-maintaining `agent.toml`, store bindings, and bundle files separately.
  Generated manifests are compiler output, not config you keep in sync.
- **Governance and identity wiring.** Per-tool identity and UC/secret ceilings
  (tool-scoped auth), caller-identity passthrough (OBO) across tools and across
  agents (A2A), and the guardrails that refuse unsafe combinations — e.g. a
  scaled deployment with an in-memory session store is rejected at compile and at
  boot, because that silently loses history across workers.
- **Compatibility checking before you deploy.** `inspect_target` /
  `compile_agent` check whether the chosen runtime can actually preserve what you
  declared (identity, sessions, approvals, memory, streaming, recovery) and fail
  with a named reason instead of silently degrading.
- **Portability.** The same declaration compiles to the durable Apps target or to
  a `ResponsesAgent` model for Model Serving, so moving between serving contracts
  is a target switch, not a rewrite.

Rule of thumb: **the CLI gives you the running, durable, scale-to-zero app; APX
gives you the declaration, governance, and safety checks that make a fleet of
them maintainable.** Where the two could drift, the CLI/product is the source of
truth for the API and stores; APX owns the declaration and policy.

One honesty note for operators: deployment `--json-output` reports the real
`hosting.space` and `hosting.compute_size`, but the
`hosting.idle_scale_down_observed` / `hosting.wake_observed` fields stay `null` —
a `/readyz` check proves the app is ready, not that an idle-down and wake
actually happened. For a deployment where the full sleep → wake → crash → replay
cycle *was* observed directly, see "Observed deployed lifecycle" below.
Databricks documents the underlying hosting in
[Serverless Micro Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/governed-agentic-app-building)
and the product interface in the
[Agent Bricks CLI guide](https://docs.databricks.com/aws/en/agents/custom-agents/agent-bricks-cli).

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
generated agent declaration as you develop. New Apps scaffolds declare
`durable_agent_server` and use the packaged native launcher. They contain
`agent.py` and `pyproject.toml`, without a generated server package, Bundle,
Lakebase quickstart or Bundle CI pipeline. APX binds a managed Session Store
automatically; declare `session.store_name` only to override its derived name.
Managed memory remains an explicit opt-in. Local execution uses those remote
stores, so deploy once before the first local run if they do not exist yet.

Use the same declaration to inspect and remove a native deployment:

```sh
uv run apx-agent status --profile your-selected-profile --json-output
uv run apx-agent agents logs --profile your-selected-profile
uv run apx-agent destroy --profile your-selected-profile
```

Status includes the live App even when the optional APX deploy-state record is
missing. Logs use the native Databricks Apps command. Teardown uses the native
SDK to check the Runtime Store's App name and service-principal ownership before
deleting it, then deletes the App. Shared Session Stores and Memory Stores remain.
If ownership cannot be verified or cleanup fails, teardown stops with the App
and APX deploy-state record retained for a retry. An already-deleted Runtime
Store is safe to retry; a store left behind without its App identity requires
manual ownership investigation.

The same cleanup guard applies to `agents delete` and its `--purge` canaries.
Those commands stop before deleting model or registry records if App teardown
fails. Native projects do not need Bundle teardown; existing named App Space
projects resolve the selected Bundle target and clean its Runtime Store before
running Bundle destroy. For a deployment made with an explicit App-name override,
use `agents logs --app NAME` and `agents delete --app NAME --uc-name MODEL` to
select that deployment explicitly.

## Choose the server behavior when needed

Apps is the deployment destination. The server determines how requests execute.
New Apps projects default to `durable_agent_server`. To create a compatibility
project with MLflow AgentServer, the dev UI and Bundle deployment, run
`apx-agent agents scaffold my-agent --runtime responses_agent`.
`--target model-serving` retains its existing layout and Responses-compatible
contract; it rejects `--runtime durable_agent_server`.

Existing declarations that omit `target` still mean `responses_agent`; this
change applies to new scaffolds, not configuration loading or running agents.
Re-scaffolding with `--force` preserves the existing runtime and refuses a
runtime change. Gallery declarations with an explicit target also retain it
unless `--runtime` is supplied. Unsupported state/runtime combinations fail
validation rather than silently switching runtimes.

The legacy `--lakebase` quickstart and `--ci github|gitlab` Bundle templates
require `--runtime responses_agent`. Native projects omit these defaults;
declare managed stores as shown below when persistence is needed.

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

Generating the native manifest does not grant the calling user data permissions
or enable request-user recovery.

### Deploy through the Agent Bricks CLI

Native agents deploy through the installed `agentbricks` CLI by default.
Keep using `apx-agent agents deploy --target apps --profile <profile>`. New native
projects need no customer-authored YAML: edit `agent.py` and `pyproject.toml`.
There is no generated `agent_server/` directory. APX supplies the launcher as
part of its installed package, so launcher fixes arrive with APX upgrades.
APX stages the source and dependencies, compiles authentication and managed store
declarations into `.build/agent.toml`, and writes the native command and
environment to `.build/app.yaml`. These are generated deployment artifacts.
APX then invokes `agentbricks deploy` using the same Python environment and
explicit profile. This path does not generate, validate, deploy or run a Bundle.
Agent Bricks owns the runtime store, declared store grants, tracing
experiment, source upload, and rollout. APX retains prerequisite checks, native
readiness verification, and the deployment version record.

An existing `databricks.yml` keeps its compatibility path: APX honors its build
script and resolves its variables with `databricks bundle validate` before
handing native rollout to Agent Bricks. It never silently discards a Bundle's
custom configuration. Bundle-specific canary, hot-swap and destroy commands
remain Bundle workflows; native projects use the deployment product for lifecycle
operations. For native model changes, edit the declaration and redeploy.

Generated deployment names use `agent-bricks-<name>` (30 characters
maximum). Existing unprefixed Apps are never silently renamed: regenerate the
project or explicitly select a prefixed `--app-name`, which targets a different
App. Existing managed stores must still pass APX's read-access checks. The CLI
binds tracing to `/Users/<deploying-user>/<app-name>-<bundle-target>` unless
`--no-auto-experiment` is selected. CLI-reported store/tracing failures fail APX
deployment even when the CLI exits zero.

Customers do not need to declare `deploy.space` for this path. The installed CLI
does not expose a named-space selector; that alone says nothing about the
platform's internal placement. Existing explicit `deploy.space` declarations
retain their compatibility path so APX does not silently drop a named governance
boundary. ResponsesAgent deployments retain their existing path.

The CLI path rejects unsupported configuration before rollout:
additional Bundle resources, service-resource grants, app-family group grants,
resource-backed environment references, arbitrary target configuration overrides,
and `--no-run`. An explicit instance count must be fixed at 1–5; omit it to leave
compute selection to the product. Declare user scopes on
tools; do not supply SDK-owned runtime-store or tracing environment overrides.
These checks prevent a migration from silently dropping an existing contract.
New native projects also reject Bundle `--var` flags; use the agent declaration
for model and store settings, and `--env KEY=VALUE` for additional runtime
environment values. `--env` values are removed from staged `app.yaml` after the
CLI returns, including on rollout failure. The dev/prod labels select tracing
and deployment records; use a distinct declared agent name for a distinct App.

### Check prerequisites before building the App

Run `apx-agent doctor` in the generated project with the intended workspace
profile selected. For durable agents and managed memory, doctor checks the local
AgentKit APIs, declared image dependencies, and read access to each declared
managed memory store, session store, and App Space. `doctor --offline` marks
workspace availability as unverified. An installed SDK alone does not prove that
the required workspace APIs are available.

Deployment runs the same prerequisite checks before building or provisioning,
then checks the staged image manifest and lock before uploading. Missing or
incompatible AgentKit dependencies fail the gate; an absent lock produces a
warning because the image must resolve its dependencies at build time.

Explicit disabled/unsupported API responses point to workspace preview and
regional availability. Permission errors, missing resources/API routes, and
connectivity errors are reported separately. On the Agent Bricks deployment
path, a missing managed store is a warning: the native CLI creates or reuses the
declared store and configures its grants. A missing API route can return the same
error, so APX keeps availability unverified until native provisioning succeeds.
Permission, authentication, explicit feature-disabled and connectivity errors
still stop deployment. Other deployment paths require existing stores; a named
App Space must also exist. Doctor remains read-only and reports missing stores
as failures for local execution. These checks do not enable previews, create
stores, or change permissions. The Agent Bricks
CLI itself [does not require a workspace enablement setting](https://docs.databricks.com/aws/en/agents/custom-agents/agent-bricks-cli).
Successful resource reads prove access for the checking identity, not runtime
write permissions. Durable Runtime Store provisioning and readiness remain
deployment-time checks.

## Deploy a durable agent

A native agent automatically gets a managed Session Store for conversation
checkpoints. Declare managed memory only when facts must also survive across
conversations. Placement is managed by Agent Bricks:

In a generated native project, keep the agent in `agent.py`:

```python
from apx_agent import LlmAgent

agent = LlmAgent(name="orders", tools=[])
```

Declare its runtime and managed stores in `pyproject.toml`:

```toml
[tool.apx.agent]
name = "orders"
module = "agent:agent"
target = "durable_agent_server"
model = "system.ai.claude-sonnet-4-6"

# Optional long-term memory. Sessions need no declaration.
[tool.apx.agent.memory]
type = "managed"
```

APX derives `apx-orders-sessions` automatically and `apx-orders-memory` when
memory is declared. It resolves these names when loading the configuration, so
doctor, local execution and native deployment use the same bindings.
`apx-agent doctor --offline` shows the resolved names without connecting to a
workspace. Online doctor and deployment prerequisite output also include them.

Set `store_name` explicitly to bind a shared or existing store; an explicit name
always wins. Names are workspace-scoped, so use distinct agent names such as
`orders-dev` and `orders-prod` for separate environments in one workspace. The
existing naming convention lowercases names, replaces non-alphanumeric characters
other than hyphens with hyphens, and truncates to fit the store-name limit. Use
explicit store names if two agent names normalize to the same resource name.

Renaming the agent changes an automatic session binding and any omitted memory
binding; pin the old `store_name` values to retain existing state. Generated
projects write the resolved names into `pyproject.toml`, making those bindings
explicit and stable through later project renames. APX never migrates or deletes
stores as part of name resolution.

```sh
apx-agent agents deploy --target apps --profile your-selected-profile
# Once the declared stores are provisioned, local execution uses the same stores:
apx-agent agents run
```

YAML agent specifications remain an optional authoring input. They compile to
the same Python project and native deployment artifacts.

Use an APX installation with the `agentbricks` extra for native local execution
or deployment. The resolved Session Store and memory store names need not
already exist when deploying through Agent Bricks. APX compiles their bindings
into `agent.toml`; Agent Bricks creates or reuses the stores, provisions the
Runtime Store and tracing, and reconciles grants. Local execution connects to
those remote stores, so provision them through deployment before the first local
run if they do not exist yet.

### Compatibility: an explicitly named App Space

An existing project may declare a particular governance boundary:

```toml
[tool.apx.agent.deploy]
space = "your-space"
```

This is an advanced placement constraint, not a prerequisite for using Agent
Bricks. APX preserves that explicit requirement through the existing Bundle
deployment path. The space must already exist, with the required
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

The managed Runtime Store records native invocations. Native agents also bind a
managed Session Store for conversation checkpoints; `session.store_name`
overrides only its name. Long-term memory is a separate `memory` declaration; neither session binding
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
compatible declarations. Give the new deployment a distinct name and set
`target: durable_agent_server`. APX binds a managed Session Store automatically;
declare `session.store_name` only when reusing an existing store.
Install the
`agentbricks` extra in the APX environment used to run and deploy it.

Deploy it with `--target apps` and your selected profile to provision new stores.
With existing stores, you can test locally first. Local execution retains the declared remote session
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

Declare `target: durable_agent_server` in your agent specification. New native
projects contain `agent.py` and `pyproject.toml`, plus any declared skills or
custom sources. They include `apx-agent[langgraph,agentbricks]` in their
dependencies. Local `agents run` and the compiled deployment command use the same
packaged ASGI factory. Deployment starts it with `python -m apx_agent._serve`;
the runtime declaration selects DurableAgentServer without `APX_APPS_HOST`.
Project discovery reads that declaration before legacy filename conventions.

Omitting `target` preserves the existing Responses-compatible Apps entrypoint.
Existing Bundle and named App Space projects retain their generated launchers.
The Python compatibility host remains available for ResponsesAgent projects.
The APX TypeScript runtime and generated AppKit host are retired. Move former
AppKit projects to `target = "durable_agent_server"`. APX adds the managed session
binding automatically. Browser clients call `POST /api/invocations` with an invocation UUID, `session_id`,
and `input.messages`; the SDK owns persisted invocation and session handling.
Use the [discovery example](../../python/examples/plg-discovery) for streaming
and the [contract example](../../python/examples/contract-parsing-agent) for
business API routes mounted directly on DurableAgentServer.

A durable `AgentConfig` with no session declaration receives a managed Session
Store named from `AgentConfig.name`. Declare `session.type: managed` with
`session.store_name` only to override that name. The resolved binding is passed
to `compile_agent(config=config, service_ws=ws)` and inspected by
`inspect_target(agent, config=config)`. Local native execution keeps that remote
store binding. At runtime, managed sessions bind the provisioned store and
normalize `auto_create` to false. Store creation belongs to native deployment;
requesting creation during agent execution is rejected.

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

The invocation body carries a client-generated UUID `id`, an optional top-level
`session_id` (which groups invocations into one application session and is
distinct from the invocation `id` and from the `X-Routing-Key` sticky-routing
header), the agent `input`, and optional `background` / `stream` flags. The
Agent Bricks endpoint behavior (upstream contract) is:

| Endpoint | Behavior |
|---|---|
| `POST /api/invocations` | Defaults to synchronous: `200` with the result under `output`. `stream: true` returns SSE events. `background: true` returns `202` with a status URL; adding `stream: true` also includes an events URL. |
| `GET /api/invocations/{id}` | Invocation status, and its `output` once completed. |
| `GET /api/invocations/{id}/events?after={cursor}` | Replays events after the given event ID, so a client can reconnect. |

The `id` is also an idempotency key: repeating the same request reuses the
existing invocation while its record is retained; reusing the `id` for a
*different* request returns `409`. APX adds only `GET /readyz` on top of this
contract.

## Compatibility checks

`RuntimeRequirements` describes required behavior. `inspect_target` returns a
`TargetReport` with `capabilities` and `unsatisfied`. `compile_agent` applies the
same checks before constructing the optional host. Existing user/tool identity
and memory/session declarations are included; omitting an explicit requirement
does not erase those declarations.

| Requirement | ResponsesAgent handlers | Native durable target in this release |
|---|---|---|
| `user_identity` | Existing OBO handling | SDK request-user client for UserClient, SQL and Principal; raw Request/Headers and remote OBO forwarding rejected |
| `sessions` | Declared session backend, explicit conversation store or LlmAgent checkpointer | Automatic managed session, or an explicit LlmAgent checkpointer / `session_store` |
| `approvals` | LlmAgent checkpointer | Automatic managed session, or an explicit LlmAgent checkpointer / `session_store`; user-scoped when request-user auth is required |
| `long_term_memory` | Supported with a reachable declared store; managed memory uses AgentKit | Supported with AgentKit managed memory or another declared backend; actor comes from trusted caller identity |
| `recovery` | Not implemented by this factory | Opt-in managed-checkpoint continuation for service-identity LlmAgents; request-user recovery remains unsupported |
| `streaming` | Existing streaming handler | Supported: persisted incremental deltas, or a persisted validated final message when output checks require buffering |

A configured checkpointer does not prove persistence across process restarts.
It can be an in-memory saver or a separately configured synchronous persistent
saver. A report is a compatibility check, not a live storage or deployment probe.
APX emits model message chunks through the SDK's ordered event store. A model
that does not support incremental output may emit a complete message in one
chunk. Event replay does not re-execute tools; it is distinct from crash recovery.

The installed `DurableAgentServer` contract is invoke, read-back, and event replay:
`POST /api/invocations`, `GET /api/invocations/{id}`, and
`GET /api/invocations/{id}/events`. APX adds only `GET /readyz`. There is no cancel,
disconnect, or reconnect route. Closing an SSE client stops that connection; the
SDK keeps the invocation running, and a later events request replays persisted
events from `after`. That is not cancellation and does not roll back a tool that
already ran. APX's `cancellable` tool wrapper and `CancellationRegistry` remain
the legacy in-process kill switch. Native handlers do not register them, so a
governance kill in that process does not stop a native invocation.

Native handlers also do not continue an inbound MLflow trace. The invocation
context passed by the SDK carries `invocation_id`, `session_id`, `attempt`,
`request_auth`, and `emit`. It has no trace headers. AgentKit may open its own
root span when its tracing destination and experiment are both configured.
`continue_trace_from_headers` stays on the legacy `/invocations` and
`/responses` routes, where those routes still own the caller-facing contract.

Those routes are still live consumers, not leftovers. `create_app` mounts
`POST /invocations` through `chat_agent_for` and `POST /responses` through the
Responses compiler. Responses-target project generation still writes
`create_app(agent=agent, config=config)` into `agent_server/start_server.py`.
The discovery card, health check, and `/mcp` endpoints are mounted beside those
routes. Dev-UI chat and trace browsing remain on this host. Retire any of them
only after each remaining caller has moved to `/api/invocations`.

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

Declare `memory` when long-term recall is required. The native session binding is automatic and separate. APX uses
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
separate supported path; it does not require enabling crash recovery. Recovery of
an interrupted attempt is scheduled by a live worker's scan loop, so in a
single-worker deployment a hard process exit leaves the stale attempt waiting in
the Runtime Store until a worker exists again — see "How a durable agent
operates" and "Observed deployed lifecycle".

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

### Observed deployed lifecycle

The following was observed once on a live `DurableAgentServer` deployment
(LIQUID compute, Agent Bricks Runtime Store on Lakebase, `durable=true`, recovery
enabled). It is evidence from one workspace, not a per-workspace guarantee —
verify your own deployment. Each observation maps onto the operating model in
"How a durable agent operates" above.

- **Idle scale-to-zero, then wake.** With no traffic, compute idled to
  `compute_status.state = STOPPED` ("App scaled to zero"), `app_status.state =
  UNAVAILABLE`; the deployment and all stores were untouched. An *authenticated*
  request then returned HTTP 202 and compute returned to `ACTIVE`/`RUNNING` in
  under 10s. An *unauthenticated* request only hit the OAuth edge redirect
  (HTTP 302) and did **not** wake compute — it never reached the app. (This is
  the "idle scale-to-zero" row of the model: compute parks, state persists, the
  next authenticated request cold-starts a worker that reloads from the stores.)
- **Durable replay survives a hard worker death.** An invocation killed
  mid-flight by `os._exit` (attempt 1, `recovery=false`) later completed as
  attempt 2 (**`recovery=true`**) with its original input intact, out of the
  Lakebase Runtime Store. But it did **not** replay immediately: `os._exit` was
  the only worker, so it killed the recovery scan loop along with the process,
  and with no live worker nothing was left to notice the stale heartbeat. The
  control plane kept reporting `ACTIVE`/`RUNNING` and the app returned HTTP 502;
  `databricks apps start` was a no-op. Only a **redeploy** started a new worker —
  whose scan loop then found the stale attempt in the Runtime Store and replayed
  it to completion. (This is the "worker failure" path of the model, in its
  single-worker form: durable state was never lost; what was gated was a live
  worker to act on it. A multi-replica deployment would instead have a surviving
  replica's scan loop pick it up without a redeploy.)

Operational note: `databricks apps deploy` build steps can fail transiently
with an opaque `[BUILD][ERROR] Unexpected error ... contact support`; a plain
retry succeeded and it was not a requirements problem.
