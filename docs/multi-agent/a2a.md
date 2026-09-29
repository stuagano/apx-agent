# A2A discovery and app-to-app auth

APX uses A2A as the runtime boundary for a logical leaf deployed in another
application. The public graph remains declarative; application code does not
construct the HTTP transport.

## Declare a logical binding

Name the leaf normally in the graph and bind that name in `pyproject.toml`:

```toml
[tool.apx.agent]
experiment = "/Shared/research-assistant"

[tool.apx.agent.bindings]
pricing = "$PRICING_APP_URL"
```

```python
from apx_agent import Agent, RouterAgent, SequentialAgent

data = Agent(name="data", description="Looks up governed account facts.")
pricing = Agent(name="pricing", description="Produces an approved price.")
review = SequentialAgent([data, pricing], name="review")
root = RouterAgent(agents=[data, review])
```

The environment variable resolves to the peer's HTTP(S) A2A card location.
APX validates and stores that transport binding privately during startup. The
generated agent module and visible topology still contain the logical
`pricing` leaf, not the environment reference, card location, or runtime
transport type.

A binding fails closed unless it resolves to one uniquely named leaf and a
valid card location. Bound leaves are supported in sequential, parallel,
model-routed, and keyword-routed positions. Remote loop completion and remote
handoff are not supported by the current A2A control surface, so a bound leaf
cannot occupy `LoopAgent` or `HandoffAgent` control positions.

Existing URL-based sub-agent declarations remain supported for compatibility.
Use a named binding for a deterministic graph edge so the authored graph stays
about roles and control rather than transport.

## One root behind every ingress

`create_app()` finalizes the declared root once and serves it through:

- `POST /invocations`
- `POST /responses`
- A2A `message/send` on `POST /`

All three execute the same graph semantics. A direct router branch completes
normally without contacting a peer. A branch that reaches a bound leaf makes
the A2A call at that leaf and returns its result into the surrounding graph.

## Discovery

An Apps-hosted APX agent publishes `GET /.well-known/agent.json`. The card
contains its logical name, description, capabilities, skills, and MCP endpoint.
The same application handles A2A JSON-RPC on `POST /`, including
`message/send`.

This is the existing A2A discovery and request surface; named graph bindings do
not introduce a second card format or a new transport protocol.

## App-to-app authentication

The A2A trust boundary is **per-hop user OBO**. The caller's user token is
forwarded to the peer; the hop runs under the user's Unity Catalog grants —
not any app service principal. That holds whether the apps have distinct SPs,
share one SP, or run across environments.

For sibling Databricks Apps, authentication is enforced at the Apps SSO
gateway and again at the APX A2A handler:

1. The caller authenticates to the peer through the Apps gateway as the user.
2. APX forwards the calling user's OBO token on every hop
   (`X-Forwarded-Access-Token` and `Authorization: Bearer`) so the peer's
   tools perform user-scoped governed access.
3. The gateway injects caller identity headers into the application.
4. **FMAPI uses the callee app's own identity.** When app A calls app B, B's
   model calls use B's own SP token, not A's OBO token (#633).

SP-to-SP `CAN_USE` is a **fallback**, not the trust boundary. It is only
meaningful when the platform provisions distinct per-app service principals
*and* OBO is unavailable. Some admin-provisioned app groups share a single
SP across every app in the group; in that topology a `CAN_USE` grant is a
self-grant and cannot establish A2A trust. Do not hang mesh identity on it.

Share permission policy across an application family when appropriate; do not
share credentials. Do not assume every app has a distinct service principal.

Unity Catalog ABAC policies that consume Databricks identity attributes
evaluate them from the authenticated user identity carried by the forwarded
OBO credential in step 2 — never from request headers. APX does not copy,
synthesize, or accept identity attributes on the wire, and the contract does
not extend to step 4's service-principal calls. See [Identity attributes and
remote-agent ABAC](../reference/identity-attribute-abac.md).

Inside the Apps runtime, A2A `POST /` fails closed if neither
`X-Forwarded-Access-Token` nor `Authorization: Bearer` reaches the handler.
Local `apx-agent run` remains available for the unauthenticated local
development loop. An operator intentionally serving without gateway user
identity must explicitly opt into service-principal fallback with
`APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK=true`.

Tool, MCP, and Dev UI routes live under the configured API prefix.
`/invocations` and `/responses` remain at their protocol-defined root paths.

## Deployment authorization

For an Apps deployment, APX adds each declared binding that resolves to a
direct Databricks Apps location to the existing native authorization plan. The
configured peer location must resolve uniquely to one application under the
explicitly selected deployment profile. APX then projects that application as
a native bundle resource with `CAN_USE` while preserving existing resources
and permissions. That grant is the distinct-SP fallback: it does not replace
per-hop user OBO, and it is inapplicable (a self-grant) where apps share one
SP. Zero or multiple matches fail closed. `apx-agent doctor` WARNs when a
project declares Databricks App peers so the OBO path stays the documented
trust boundary.

The authorization summary reports the resolved application identity without
credentials. The reconciliation is additive: it does not delete or downgrade
existing grants.

## Distributed tracing

Outbound A2A calls carry MLflow tracing context. The receiving
`/invocations`, `/responses`, or A2A `message/send` handler continues that
context before opening its request span, so receiver spans have real parent
relationships to the caller's active graph span.

For independently deployed applications to persist this as one inspectable
trace, both must use the same governed MLflow experiment or trace location.
Propagation alone cannot merge records written to different destinations.

The experiment destination comes from each application's deployment/runtime
configuration. It is not selected by a caller-controlled request header, and
no experiment field is added to the A2A card. See [Tracing](../running/tracing.md#distributed-tracing-across-apps).

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| HTML login redirect | The Apps gateway intercepted an unauthenticated call | Authenticate through the Apps gateway. |
| Gateway `401` | Caller identity was not accepted at the peer gateway | Authenticate through the Apps gateway so the user token is forwarded. A compiled SP-to-SP `CAN_USE` grant is only a fallback for distinct-SP topologies; it is a self-grant where apps share one SP. |
| APX `401` on A2A `POST /` | The request reached the handler without proxy or bearer identity | Call through the gateway, or explicitly opt into service-principal fallback when that is the intended trust model. |
| Model API `401` inside the peer | The peer service identity lacks model permission | Grant the peer application's service identity access to its declared model resource. |
| Startup binding error | Missing, ambiguous, blank, malformed, loop, or handoff binding | Correct the named leaf and environment-backed card location; do not bypass validation. |
