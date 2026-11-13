# Design: the A2A trust model — per-hop user OBO, not SP-to-SP CAN_USE

**Status:** accepted (describes current code) · **Date:** 2026-09-29 · **Issue:** #814

apx-agent lets an agent call another agent across apps (`sub_agents=[url]` + A2A).
The question this doc answers: **what is the trust boundary on that hop?**

**The invariant: A2A trust is per-hop user OBO.** The caller's user token is
forwarded to the callee, and the downstream hop runs under the *user's* Unity
Catalog grants — not any service principal's. This holds whether the two apps
have distinct SPs, share one SP, or run in different environments.

The app-to-app `CAN_USE` grant is **infrastructure reachability** (can this app's
SP open a connection to the peer), **not access control**. The two are different
layers and both exist; only OBO decides what the caller can actually do.

---

## Why not SP-to-SP CAN_USE

The earlier deployed A2A proof (#561–563) used two apps with **distinct** service
principals and an explicit SP-to-SP `CAN_USE` grant, and it was tempting to read
that grant as the trust boundary. It isn't, for two reasons:

1. **It's degenerate under a shared SP.** Some Databricks deployment models group
   apps under one admin-provisioned SP shared across the group; apps don't each
   get a distinct SP. There, SP-to-SP `CAN_USE` between two apps in the group is
   an SP granting to itself — the "boundary" doesn't exist.
2. **It's a per-environment topology detail.** Whether distinct SPs exist is an
   infra fact apx-agent shouldn't bake into its trust model. OBO forwarding is
   invariant across topologies; `CAN_USE` is not.

So `CAN_USE` is the fallback/plumbing layer (still fine, still emitted, and the
only path where two distinct SPs exist and OBO is genuinely unavailable), and
per-hop user OBO is the primary invariant.

---

## What the code already does (grounded, file:line)

This model is not aspirational — it is what the runtime does today:

- **OBO forwarded per hop, unconditionally when present.** When a compiled agent
  calls a remote sub-agent, the caller's token is forwarded as
  `X-Forwarded-Access-Token` + `Authorization: Bearer` — one rule, `_obo.py` `remote_hop_headers`, used by every remote hop (#1050).
  The only gate is the absence of a user token (local dev / no user identity),
  which is the legitimate "no user in scope" case, not a misconfiguration.
- **Fail-closed when OBO is absent in Apps.** In the Databricks Apps runtime, a
  request with no OBO token raises rather than silently falling back to the app
  SP, unless `APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK=true` is set explicitly
  (`_obo.py`, `resolve_no_obo_or_raise`).
- **Origin trust on the hop.** Credentials are only forwarded to a base URL that
  matches the configured peer (`_remote.py`, `_trusted_origin`); an untrusted
  origin drops `Authorization`/`X-Forwarded-Access-Token` and warns.
- **CAN_USE is emitted as infra, never enforced as access.** The app-to-app
  dependency is materialized as `permission: CAN_USE` in the bundle
  (`_resources.py`, `_apps_host_manifest.py`), and the authorization plan surfaces
  each Apps-peer dependency as `trust: per-hop user OBO (CAN_USE = infra
  reachability)` (`_apps_authorization.py`, `authorization_summary_lines`). No
  code path asserts the grant as an access decision.

### On the native durable host

`durable_agent_server` keeps the same invariant. The SDK's `RequestAuthContext`
holds the request-user token; apx never copies it. Each sub-agent call asks
`request_auth.client_for("user").config.authenticate()` at call time and hands the
result to the outbound call, which forwards it as `X-Forwarded-Access-Token` +
`Authorization: Bearer` on the direct-HTTP transport. Under `agentbricks dev`
(outside an App) the SDK ignores request headers and its "user" client is the
developer's own profile, so that credential is what bound peers receive (#1178).
A local verdict inside an App forwards nothing, because there the "user" client
is the App's service principal. A closed request context fails closed, and
request-user invocations are never recovered in the background. A peer that
redirects the hop to sign-in fails with "redirected to sign-in", naming the
missing credential.

Databricks Apps peers (`apps/<name>`) use the Apps SDK transport only when no
request-user token is present. The SDK's `BearerAuth` hook authenticates that
call as the default WorkspaceClient — inside an App, the calling app's
service principal — and overwrites `Authorization` after `extra_headers` are
applied, so the user token cannot ride that path. When a request-user token
is present, `RemoteDatabricksAgent` skips the SDK and uses the direct-HTTP
transport, which forwards `X-Forwarded-Access-Token` + `Authorization:
Bearer`. Supervisor long-task continuation is the exception: it exists only
on the SDK stream, and the direct-HTTP path cannot resume a paused task, so
a long-running peer of an authenticated caller still runs as the app.

## Operate-time signal

`apx-agent doctor` (`check_sub_agents`, `_doctor.py`) reminds, when a declared
sub-agent is a Databricks App peer, that trust rides on per-hop user OBO — so the
operator must ensure the caller's OBO reaches the served agent (fail-closed
unless the fallback is on), and that SP-to-SP `CAN_USE` is not the access
boundary and is degenerate under a shared SP. It stays a note on an OK check, not
a failure — a topology apx-agent can't fully see from a local project must not
redden doctor.

## Role-required App Spaces: refused (#1070)

An App Space can require a role (`Space.assume_group_id` in the Databricks SDK).
Apps in such a space act with **that group's** permissions, not the signed-in
user's: user tokens assume the role instead of the usual OBO downscoping. That is
a third identity path, and it breaks the invariant above. A tool declared to run
as the user, or a sub-agent hop that forwards the user's token, would silently
run with the role's grants.

**Decision: refuse, don't warn.** The App Space preflight
(`_app_space.py`, `validate_space_deployment`) reads `assume_group_id` from the
space it already reads for scopes. When a role is set and the plan has any tool
with `execution_identity == "user"` or any `http(s)` sub-agent hop, deploy fails
with an `App Space role:` error that names each one. A role-required space with
only service-principal tools deploys, and the authorization summary adds an
`App Space role:` section naming the group. A space without a role is unchanged.

`apx-agent doctor` reports the same thing as the `App Space role` row
(`check_space_prerequisites`, `_doctor.py`): FAIL on the conflict, OK otherwise,
and WARN when it can't read the space, the same way it handles the
partner-powered AI setting. Offline, or when the agent doesn't import, the row is
a SKIP.

## Out of scope

Platform-side SP semantics. This doc is about apx-agent not assuming a trust
boundary (distinct per-app SPs) that some environments don't provide.

See also:
[`served-path-guards-and-identity.md`](served-path-guards-and-identity.md) (the
single-app fail-closed OBO identity model this extends across hops).
