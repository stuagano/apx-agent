# AppKit Runtime Cutover Design

**Status:** Approved

**Date:** 2026-09-01

**Live validation target:** Databricks profile `fevm`, app `contract-parsing-agent`

## Decision

The generated Databricks Apps host will use the behavior AppKit natively
supports. APX will not build adapters to reproduce Python-host behavior that
AppKit does not expose.

Commit `15657d09` keeps Python as the default while this supported surface is
implemented and verified. The final cutover changes the missing
`APX_APPS_HOST` default to AppKit. `APX_APPS_HOST=python` remains a rollback
option for one release.

## AppKit-Owned Surface

AppKit owns:

- `/invocations`, `/responses`, and `/chat`
- the model/tool loop
- SSE streaming and cancellation
- AppKit thread storage and thread routes
- static effect-based HITL approval
- AppKit telemetry
- static assets and the public server lifecycle

APX does not add a custom `ThreadStore`, approval adapter, model-loop wrapper,
or AppKit fork.

## Python Bridge Surface

The private Python process remains a loopback-only sidecar. It owns:

- execution of declared Python tools
- dependency injection and Databricks Apps OBO clients
- existing `before_tool` and `after_tool` hooks
- existing APX MCP, A2A, feedback, topology/development, trace, and readiness
  routes

The sidecar uses the normal APX application wiring so these existing routes are
mounted once. The Node host proxies an explicit path allowlist to it. There is
no configurable empty proxy list and no catch-all proxy.

The current manifest-backed tool call remains a small private JSON request with
`args`. Databricks Apps identity, token, host, and request correlation stay in
forwarded headers and are never copied into response bodies or logs.

## Tool Effects and Approval

APX reuses its existing `@tool` metadata object to carry AppKit's native
`effect` value: `read`, `write`, `update`, or `destructive`. No new metadata
class or policy vocabulary is introduced.

- `@tool(effect="read")` opts a tool out of AppKit mutation approval.
- Other explicit effects map directly to AppKit.
- Undecorated tools and `@tool` declarations without an effect default to
  `update`, because an unknown Python function must not be assumed read-only.
- AppKit's native approval gate is the only approval flow in the AppKit host.
- Python `before_tool` hooks still run and may deny execution.
- An argument-dependent APX `ASK` is unsupported in the AppKit host and fails
  closed without executing the tool. The package does not add a second approval
  store or preflight protocol.
- AppKit's documented non-streaming behavior remains unchanged: a request with
  tools requiring native approval receives its normal `400` response.

## Explicitly Unsupported Under AppKit

The following Python-host behavior is not part of this cutover:

- `Dependencies.State` tools
- APX argument-dependent `ASK` approval and resume
- APX conversation storage as AppKit thread storage
- Python-host thread/checkpoint resume semantics

The bridge returns a bounded, explicit unsupported error for a stateful tool;
it never runs it without state. Users needing these behaviors can select
`APX_APPS_HOST=python` during the rollback period.

## Route Proxy and Readiness

The generated Node host proxies only the exact APX auxiliary prefixes mounted
by the sidecar, including MCP, A2A discovery/transport, feedback,
topology/development, traces, and `/readyz`. Agent transport and AppKit thread
routes are never proxied.

The proxy preserves method, query, body streaming, response status, content
type, forwarded identity, and request correlation. It removes hop-by-hop and
host headers. Upstream errors return a bounded `502` without credentials or
sidecar internals.

The public `/readyz` is the sidecar readiness result. AppKit process liveness is
already implied because Node serves the response; if the sidecar is unavailable,
readiness fails.

## Generated Lifecycle

The existing supervisor script starts the loopback Python sidecar and AppKit
server, forwards termination, and exits non-zero when either child fails. The
cutover does not add a process manager, RPC dependency, storage service, or
second public listener.

The generated build/start contract remains:

- TypeScript host validation with `tsc --noEmit`
- Node start through `scripts/start.mjs`
- Python sidecar through the generated `agent_server.appkit_bridge:app`

## Verification

### Local supported-surface matrix

Tests cover:

- sync and async stateless Python tools
- argument validation and JSON-safe results
- OBO identity forwarding and missing-token failure
- `before_tool` allow/deny and `after_tool` callbacks
- fail-closed APX `ASK`
- explicit and conservative-default AppKit effects
- AppKit approve and deny behavior
- explicit stateful-tool rejection
- streaming and cancellation
- MCP and A2A routes
- feedback, topology/development, and trace routes
- proxied `/readyz` success and sidecar-unavailable failure
- generated build/start and child-process failure behavior
- missing `APX_APPS_HOST` selecting AppKit and explicit `python` rollback

Use AppKit's real testing context for OBO/tool dispatch. Run the repository's
required gates:

```bash
cd typescript && npm ci && npm run build
make check
cd python && uv run --frozen pytest
```

### Live `fevm` proof

The existing `contract-parsing-agent` app is a disposable target. Every
Databricks CLI command passes `--profile fevm`; the ambient profile is ignored.

The proof records:

1. Generated AppKit build and successful start.
2. Authenticated browser access.
3. OBO SQL execution as the signed-in user.
4. One explicit read tool and one mutating tool through AppKit approval.
5. Approval deny and approval allow behavior.
6. MCP/A2A, feedback, topology/development, trace, and readiness routes.
7. Correlated AppKit/APX telemetry sufficient to diagnose a tool call.
8. Application logs free of forwarded tokens and other secrets.

## Cutover Gate

AppKit becomes the missing-environment default only after the local matrix,
full repository gates, and live `--profile fevm` proof pass. The same commit
updates generated templates, deployment staging, tests, and documentation.

Stateful tools, APX dynamic approval, and durable AppKit threads are accepted
differences, not hidden parity claims. They do not block cutover.
