# Per-App Trace Feedback API

## Purpose

Give every deployed APX application the same authenticated HTTP surface for
reading and attaching human feedback to its MLflow traces. An existing review
application can call the APX app that produced a trace instead of embedding
APX-specific Python code or deploying a separate feedback service.

The capability becomes available when an app upgrades and redeploys with this
APX version. It does not modify already-running deployments automatically.

## Chosen Architecture

APX adds one framework-neutral FastAPI router to the shared `setup_agent()`
path used by both `create_app()` and `mount_mcp_endpoints()`. The router is
mounted independently of `APX_DEV_UI`, so disabling the developer console does
not remove the production feedback API.

The API delegates all storage and normalization to the existing
`attach_feedback()` and `get_feedback_view()` functions. MLflow assessments
remain the only persistence mechanism; the router adds no database, queue,
cache, or parallel feedback model.

The router exposes:

- `POST /_apx/feedback`
- `GET /_apx/feedback/{trace_id:path}`

This prefix avoids the existing `/_apx/traces/{trace_id:path}` catch-all and
keeps feedback available whether or not the trace-viewing developer UI is
mounted.

## Alternatives Considered

### Central fleet service

A central service would provide one URL for all apps, but it would also require
cross-app routing, a broad credential boundary, deployment inventory, and a new
operated service. That is unnecessary for the current requirement and weakens
the rule that access to a trace is governed by the app and workspace that own
it.

### Developer UI extension

Adding writes beneath `/_apx/traces` would reuse nearby routes, but those routes
are optional and include a catch-all trace path. A production integration must
not disappear with `APX_DEV_UI=0` or depend on developer-console authorization.

### Copied adapter in each application

Per-project adapter code would make every owner repeat authentication, payload,
error, and MLflow logic. Mounting one APX-owned router in the shared runtime
path provides the same per-app topology without implementation drift.

## HTTP Contract

### Attach feedback

`POST /_apx/feedback` accepts a strict JSON object with:

- `trace_id`: required non-empty string
- `name`: required non-empty assessment name
- `value`: required boolean, integer, float, or string
- `comment`: optional human rationale
- `idempotency_key`: optional replay key
- `evidence`: optional string-to-string metadata, including screenshot or
  artifact URIs supplied by the review application

Unknown fields are rejected. The body does not accept `source`, tokens,
workspace hosts, or user identity. APX derives assessment identity from the
authenticated request.

The response is the existing `TraceFeedbackResult` shape:

- `trace_id`
- `feedback_id`
- `name`
- `created`

`created=false` means the existing best-effort idempotency check found an
assessment with the same reserved idempotency metadata key. This check does not
claim atomic exactly-once behavior across concurrent app replicas.

### Read feedback

`GET /_apx/feedback/{trace_id:path}` returns the existing
`TraceFeedbackView`: trace tags plus normalized feedback and expectation
assessments. It performs a fresh MLflow read under the caller's identity and
does not serve a process-local cached authorization result.

## Authentication and Identity

In a deployed Databricks App, detected by the existing Apps-runtime signal,
both endpoints fail closed unless the request contains an Apps-provided
`X-Forwarded-Access-Token`. APX also requires `X-Forwarded-Email` or
`X-Forwarded-User` so a human source identifier can be recorded.

The router uses the existing `extract_obo_headers()` contract:

- The OBO token comes from `X-Forwarded-Access-Token`.
- The workspace API host comes from the deployment-provided
  `DATABRICKS_HOST`.
- `X-Forwarded-Host` is never used as the workspace API host inside a
  Databricks App.
- The assessment source identifier prefers the forwarded email and otherwise
  uses the forwarded user ID.

The request body cannot override those values. `APX_DEV_UI_TOKEN` is not an
authentication option for this API, and the app service principal is not used
as a fallback when deployed OBO context is missing or invalid.

For local development outside Databricks Apps, the router retains the existing
MLflow behavior: ambient developer credentials may be used and the default
`apx.trace_feedback` source identifier applies. Forwarded identity headers do
not turn a local process into deployed-app mode.

## Request-Scoped MLflow Access

The module-level MLflow 3.14 feedback functions create a global
`TracingClient`, while `MlflowClient` does not expose `log_feedback()` or
`log_assessment()`. Mutating `DATABRICKS_TOKEN` or other process environment per
request would be unsafe under concurrency and could silently use the app
service principal.

APX therefore creates a small private feedback adapter per deployed request.
It constructs MLflow's Databricks tracing store with a credentials provider
bound to the trusted `DATABRICKS_HOST` and that request's OBO token. The adapter
exposes only the `get_trace()` and `log_feedback()` behavior already consumed by
the feedback helpers. Its MLflow-internal imports remain isolated in one module
and covered by contract tests against the repository's pinned MLflow 3.14
version.

This is preferable to reimplementing Databricks MLflow REST paths and protobuf
payloads in APX. If MLflow later publishes a request-scoped assessment client,
the private adapter can switch without changing the HTTP or helper contracts.

## Validation and Errors

The router validates the HTTP body before calling MLflow and preserves the
existing reserved-key protection for `apx.feedback.idempotency_key`.

Responses use these categories:

- `401` when a deployed request lacks its OBO token or forwarded human identity
- `403` when MLflow denies the authenticated caller access
- `404` when the requested trace does not exist
- `422` for malformed JSON, unknown fields, or invalid feedback values
- `503` when the optional MLflow feedback dependency is unavailable
- `502` for other sanitized MLflow failures

Error responses and logs never include access tokens, authorization headers, or
credential-bearing objects. The implementation passes only the individual
normalized header values needed by the private adapter; it does not log or
serialize the request-header collection.

## Mounting Behavior

The feedback router is included once through the shared `setup_agent()` path,
before optional developer UI mounting. Both public runtime constructors are
tested to prove the routes exist:

- `create_app()` for the standard APX FastAPI runtime
- `mount_mcp_endpoints()` for the MLflow `AgentServer` Databricks Apps runtime

The implementation must not add separate per-constructor feedback behavior.
If repeated setup can occur on the same FastAPI instance, inclusion is guarded
by one app-state marker so routes are not registered twice.

Router construction does not import MLflow. A core-only APX installation can
therefore start normally and returns the sanitized `503` only if a caller uses
the feedback endpoint without the `eval` extra installed.

## Testing

Implementation follows red-green-refactor with focused tests for:

- route presence through both public runtime constructors
- route availability when `APX_DEV_UI=0`
- strict request parsing and rejection of caller-supplied source identity
- deployed requests failing closed without OBO or forwarded human identity
- trusted-host selection from `DATABRICKS_HOST`, never `X-Forwarded-Host`
- request-scoped OBO credentials used for both reads and writes
- no ambient service-principal fallback in deployed mode
- successful feedback creation and normalized readback
- existing idempotency replay behavior and reserved metadata protection
- distinct permission-denied, missing-trace, validation, and sanitized upstream
  errors
- idempotent router mounting

Focused tests run first. The repository gate then requires the TypeScript build
in a fresh worktree followed by `make check`.

## Rollout and Compatibility

The change is additive. Existing CLI commands and private Python helpers keep
their current behavior. Applications receive the endpoint only after upgrading
APX and redeploying; there is no workspace deployment or fleet mutation in this
change.

The HTTP route is the reusable adapter contract. The helper dataclasses remain
private until a concrete Python consumer requires a supported import, avoiding
a second public integration surface in this slice.

## Non-Goals

- A central fleet feedback service or global app registry
- A replacement review or annotation UI
- Service-principal or unattended machine-to-machine submission
- Shared secrets or `APX_DEV_UI_TOKEN` authentication
- A second feedback database, queue, cache, or synchronization process
- Atomic exactly-once idempotency across concurrent replicas
- Automated redeployment of existing Databricks Apps
- Monitoring, scheduling, evaluation jobs, or autonomous remediation
