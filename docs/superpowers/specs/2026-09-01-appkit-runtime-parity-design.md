# AppKit Runtime Parity Design

**Status:** Approved

**Date:** 2026-09-01

**Live validation target:** Databricks profile `fevm`, app `contract-parsing-agent`

## Problem

APX can generate a Databricks AppKit host, but that host is not yet a safe
replacement for the Python Apps host. The current bridge executes a Python
LangChain tool directly, defaults missing policy to `ALLOW`, rejects stateful
tools, uses AppKit's in-memory thread store, and does not expose the complete
APX route and lifecycle surface. Making that path the default would silently
drop governance, identity, persistence, protocol, and operational behavior.

Commit `15657d09` keeps the Python host as the default until this design's
parity gates pass. AppKit remains available through explicit
`APX_APPS_HOST=appkit` selection during implementation and validation.

## Goals

1. Make AppKit the transport and agent-loop owner while enforcing the APX
   governance and identity behavior that AppKit can represent.
2. Give Python and AppKit hosts one governed Python tool-execution path.
3. Preserve APX state, durable conversations, MCP/A2A, feedback, topology,
   development, tracing, health, and readiness behavior.
4. Prove parity locally and against the existing `contract-parsing-agent` app
   on the explicitly selected `fevm` profile.
5. Cut over the generated default only after the proof is recorded in tests.

## Non-goals

- Reimplementing the AppKit model loop, streaming protocol, cancellation, or
  approval UI in Python.
- Reimplementing APX policy, audit, callbacks, dependency injection, or state
  semantics in TypeScript.
- Adding a general-purpose cross-language RPC framework or a public sidecar API.
- Preserving compatibility with the incomplete internal bridge.
- Adapting APX's argument-dependent `ASK` flow to AppKit's static approval
  gate. The AppKit host supports AppKit's declared effect-based HITL only.
- Keeping the current live `contract-parsing-agent` deployment available during
  parity validation; the user explicitly approved breaking it.

## Ownership Boundary

| Surface | Owner | Reason |
| --- | --- | --- |
| `/invocations`, `/responses`, `/chat` | AppKit | Native Responses API, SSE, agent loop, limits, cancellation, and statically declared HITL |
| Thread HTTP routes and message history | AppKit API backed by APX storage | Preserve AppKit's contract and APX durability |
| Tool policy, execution, callbacks, audit, injected dependencies, state | Shared Python APX executor | One governance implementation for both hosts |
| User identity and token forwarding | AppKit request scope to Python executor | Preserve Databricks Apps OBO identity end to end |
| MCP/A2A, feedback, topology, development, traces, health/readiness | Existing Python APX routes | Reuse shipped behavior instead of recreating it |
| Static assets and server lifecycle | AppKit | Native generated Apps host behavior |

The Node process is the public listener. It starts the private Python runtime,
waits for its readiness endpoint, mounts AppKit, and proxies only the named
APX-owned routes. The Python runtime binds to loopback on an ephemeral or
generated private port and is not independently exposed by the Databricks App.
If the sidecar exits or never becomes ready, AppKit readiness fails and tool or
APX route calls fail closed.

## Shared Governed Tool Executor

The existing Python host and the private AppKit route must call the same APX
executor. That executor is extracted from the current agent/tool path; it is not
a second policy engine or an AppKit-specific tool wrapper.

Execution order is fixed:

1. Resolve the declared tool by its APX name.
2. Validate arguments with the declared schema.
3. Resolve the authenticated user, forwarded token, conversation/thread ID,
   request ID, and tool-call ID from the request context.
4. Load declared dependencies and the current APX state snapshot.
5. Run `before_tool` policy. A missing decision, exception, or unknown action is
   an error, never `ALLOW`.
6. Return `DENY` without invoking the tool. Under the AppKit host, an APX `ASK`
   is an unsupported fail-closed denial, not a second approval flow.
7. Invoke the sync or async Python tool with the same dependency injection used
   by the Python host.
8. Atomically persist the state delta only after successful execution.
9. Run callbacks and audit recording with the result or normalized failure.
10. Return a JSON-safe result to AppKit.

The private request is a small JSON contract containing `tool_name`, `args`,
`thread_id`, and `tool_call_id`. Identity and request correlation continue to
travel in the existing forwarded headers. The response is either a tool result,
a denial, or a normalized error. These are data shapes at the private boundary,
not new public Python classes.

The raw `_make_langchain_tool(...).ainvoke()` AppKit bridge is removed as an
execution boundary after the shared executor is in use.

## Tool Metadata and Approval

APX remains authoritative for policy. AppKit annotations are transport hints
that allow its UI to pause before a statically declared mutating call; they do
not grant permission.

- APX read-only tools map to AppKit `effect: "read"`.
- APX mutating tools map to the narrowest supported AppKit value among
  `write`, `update`, and `destructive`.
- A Python tool without trustworthy effect metadata is treated as mutating.
- `requires_user_context` remains true when the APX declaration requires OBO.
- AppKit approval is necessary but not sufficient for a statically declared
  mutating tool: after approval, the Python executor still evaluates APX policy.
- APX `DENY` always wins.
- APX `ASK` is not adapted into a second approval system. Under the AppKit host,
  it fails closed without invoking the tool; AppKit approval remains driven only
  by static effect metadata.
- AppKit documents that non-streaming `/invocations` and `/responses` cannot
  complete its native HITL. Requests whose statically declared tools require
  AppKit approval retain AppKit's explicit `400` behavior; they are not silently
  executed.

Approval decisions are scoped to the initiating user, stream, tool call, and
arguments. An approval cannot be replayed for a different call.

This exception follows AppKit's actual contract. AppKit evaluates its approval
gate before `ToolProvider.executeAgentTool`, but APX can compute an
argument-dependent `ASK` only inside that callback. AppKit exposes neither a
policy-preflight hook nor approval context to the provider. This package does
not add an adapter, duplicate approval system, or AppKit fork to bridge that
gap. If AppKit later adds a preflight authorization hook, APX `ASK` can be
reconsidered separately.

## Identity and OBO

The Node bridge forwards only the existing Databricks Apps identity, access
token, host, and request-correlation headers. It does not log token values or
place them in JSON bodies. The Python executor constructs dependencies exactly
as the Python host does today.

In a deployed app, a tool declared as requiring user context fails closed when
the forwarded user or access token is missing. Local tests may inject explicit
fake headers; there is no production service-principal fallback for a missing
user token. The live proof must show that an OBO SQL action executes as the
signed-in `fevm` user.

## Thread and State Model

AppKit's `ThreadStore` is replaced with a minimal adapter over the existing APX
conversation store. The adapter implements AppKit's five required operations:
`create`, `get`, `list`, `addMessage`, and `delete`. AppKit thread IDs are APX
conversation IDs. User ownership checks remain enforced on every operation.

AppKit does not pass a thread ID to `ToolProvider.executeAgentTool`. The public
request therefore enters one AsyncLocalStorage context containing a mutable
request-scoped record. The custom `ThreadStore` writes the resolved or newly
created thread ID into that record before the model loop begins. The tool bridge
reads it when calling Python. This context is per request, so concurrent requests
from the same user do not share thread state.

APX state is stored with the conversation and loaded by the shared executor.
Each tool receives the existing `Dependencies.State` proxy over a snapshot. On
success, only its delta is committed atomically. Denial, approval timeout,
cancellation, validation failure, and tool failure commit no delta. Updates for
one conversation are serialized to prevent lost writes; no process-global state
map is added.

`/chat` supports multi-turn state because it accepts a thread ID. A client may
create a thread through AppKit's thread route or use the ID returned by the first
chat. `/invocations` remains an AppKit one-shot thread operation and returns its
generated `thread_id`; it is not used as a substitute for multi-turn chat.

## APX Route Proxy

The Node host proxies an explicit allowlist of existing APX-owned paths to the
private Python runtime. The implementation derives the exact list from the
current Python route registration and generated-host contract, including:

- MCP and A2A surfaces
- feedback
- topology and development surfaces
- trace access
- health and readiness

Agent transport and AppKit thread routes are never proxied. There is no catch-all
proxy and no configurable empty default path. Request method, status, content
type, streaming body, identity headers, and correlation headers are preserved.
Hop-by-hop headers are removed. Proxy errors return a bounded error without
sidecar internals or credentials.

`/readyz` is composed: it succeeds only when both the AppKit host and the Python
runtime are ready. Liveness remains process-local so a temporary downstream
failure does not force a restart loop.

## Errors, Cancellation, and Observability

The private executor uses stable categories for validation, authentication,
policy denial, approval required, cancellation, tool failure, and internal
failure. User-facing messages stay bounded; full exceptions are recorded through
the existing APX tracing/audit path with secrets redacted.

AppKit's abort signal cancels the private HTTP request. The Python route stops
awaiting the tool and propagates cancellation where the existing tool contract
allows it. A completed tool is never reported as cancelled, and a cancelled or
failed tool does not commit state.

AppKit owns model- and stream-level telemetry. APX owns policy and tool audit
events. Request ID, AppKit thread ID, tool-call ID, and MLflow trace ID are
carried across the boundary so the two records can be correlated without a new
tracing backend.

## Generated Host Lifecycle

Generated AppKit projects keep the existing build and start entry points. The
start command launches the private Python runtime and the AppKit server under one
supervisor process. Shutdown forwards termination, waits a bounded interval, and
reaps both children. Startup failure from either child exits non-zero.

The runtime uses package artifacts already present in the generated project. No
new production dependency or external service is introduced.

## Verification

### Local parity matrix

One declaration is exercised through both Python and AppKit hosts for:

- sync and async tools
- argument validation and JSON-safe results
- read, update, and destructive annotations
- policy allow, deny, unsupported `ASK`, and missing-policy failure
- OBO identity present, missing, and downstream failure
- injected dependencies
- state read/write across turns, failed-call rollback, and concurrent isolation
- durable thread create/get/list/add/delete and process restart
- streaming events, cancellation, and AppKit static HITL
- MCP and A2A
- feedback
- topology and development routes
- trace/audit correlation and secret redaction
- health and composed readiness
- generated build and start behavior

Tests use AppKit's real testing context for tool dispatch and OBO behavior, not a
mocked direct method call. The repository gate remains `make check`, preceded on
a fresh worktree by `cd typescript && npm ci && npm run build` as required by the
repository instructions.

### Live `fevm` proof

The existing `contract-parsing-agent` app is the disposable validation target.
Every Databricks CLI command passes `--profile fevm`; ambient
`DATABRICKS_CONFIG_PROFILE` is ignored.

The proof records:

1. Generated AppKit build and successful app start.
2. Authenticated browser access.
3. OBO SQL execution as the signed-in user.
4. A read-only tool call and a statically mutating fixture through AppKit HITL.
5. AppKit approve/deny behavior and fail-closed APX `ASK` behavior.
6. Multi-turn thread and `Dependencies.State` persistence.
7. Feedback submission.
8. MCP/A2A, topology/development, trace, health, and readiness routes.
9. Correlated AppKit/APX trace and audit records.
10. Application logs free of forwarded tokens and other secrets.

## Cutover and Removal

Implementation lands in independently testable slices while Python remains the
default. The generated default changes to AppKit only in the final slice, after
the local parity matrix and live `fevm` proof pass. The same change updates the
design status and tests the missing-environment default.

The explicit Python host selector remains as a rollback path for one release.
Removal of the Python transport is a separate decision based on adoption and
rollback evidence; it is not part of this parity change. The obsolete direct
tool bridge and duplicate AppKit policy/audit hooks are deleted during cutover so
there is only one governed executor.

## Acceptance Criteria

AppKit parity is complete only when all of the following are true:

- The same APX declaration produces equivalent governed behavior through both
  hosts for every row in the local matrix.
- Stateful tools work across durable AppKit threads without process-local state.
- No tool can execute because policy metadata or identity context was missing.
- AppKit static HITL composes with APX `ALLOW`/`DENY`, and unsupported APX
  `ASK` fails closed without execution.
- Required APX routes and readiness behavior remain reachable through the Node
  listener.
- Generated AppKit build/start and the full repository gate pass.
- The `contract-parsing-agent` live proof passes on `--profile fevm` and its logs
  show no credential leakage.
- Only then does missing `APX_APPS_HOST` select AppKit.
