# Apps Authorization Compiler Design

**Status:** Approved; implemented locally on `codex/apps-auth-compiler`; live `fevm` validation pending

**Date:** 2026-09-01

**Implementation base:** `codex/appkit-parity` at `875e8732`

**Live validation profile:** `fevm` (explicitly selected by the user)

**Related design:** `docs/superpowers/specs/2026-09-01-appkit-runtime-parity-design.md`

> **Implementation status (2026-09-01):** Tasks 1–7 implement this approved
> design locally. The compiler now infers and validates tool identities,
> dispatches Apps tools under distinct user/service credentials, reconciles the
> generated bundle additively on every Apps deploy, resolves A2A peers under an
> explicit profile, and prints a deterministic authorization summary before
> mutation. The separately consented live `fevm` user-only/App-only proof has
> not run; this is not a production-verification claim.

## Decision

APX will compile each declared operation into one effective Databricks Apps
authorization plan. The plan distinguishes calls made as the requesting user
from calls made as the App service principal, then drives all generated
authorization surfaces from that single result:

- App service-principal resource bindings and minimum permissions;
- user OAuth scopes for on-behalf-of-user (OBO) execution;
- App-to-App `CAN_USE` bindings;
- App audience and administrator group permissions;
- the internal AppKit host manifest;
- deployment validation and an operator-readable authorization summary.

Databricks IAM, Unity Catalog, and resource ACLs remain the enforcement
authority. APX compiles and validates intent; it does not implement a parallel
authorization engine or store credentials.

Every deployed Databricks App keeps its platform-created, persistent service
principal. A family of Apps shares group policy and generated configuration,
not service-principal credentials.

## Goals

1. Preserve the documented distinction between `Dependencies.Client`
   (App service principal) and `Dependencies.UserClient` / `Workspace` / `Sql`
   (requesting user).
2. Eliminate the current behavior in which every `ResourceSpec` produces both
   an App-service-principal grant and an OBO scope regardless of the operation
   using it.
3. Make normal Apps deployment reconcile the complete declared authorization
   contract without requiring manual GUID lookup, OAuth-secret management, or
   `set-permissions` commands.
4. Fail before deployment when identity, scopes, resource grants, or
   App-to-App bindings are contradictory or incomplete.
5. Standardize `CAN_USE` and `CAN_MANAGE` group policy across a collection of
   Apps while retaining a unique identity and audit boundary per App.
6. Prove the identity boundary locally and, after separate deployment consent,
   against user-only and App-only resources in the `fevm` workspace.

## Non-goals

- Sharing one service principal or its credentials across multiple Apps.
- Replacing Unity Catalog, Databricks IAM, resource ACLs, or AppKit execution
  context with APX-local authorization decisions.
- Adding a second per-App plugin manifest beside `ResourceSpec`, the internal
  Apps host manifest, and `databricks.yml`.
- Automatically deleting grants, scopes, resources, or explicit App
  permissions that an operator previously configured.
- Guessing App IDs, resource IDs, group names, or a Databricks profile.
- Making a live deployment as part of local implementation. Live mutation
  remains separately consented after local and repository gates pass.
- Expanding the AppKit runtime cutover's accepted surface. Stateful tools,
  dynamic APX `ASK`, and durable APX conversation storage remain governed by
  the approved runtime-parity design.

## Pre-implementation Baseline and Confirmed Defects

This section records the state that motivated the approved decision. The
implementation-status note above records which of these gaps are now addressed
locally; it does not change the approved design or claim live validation.

APX already supplies most mechanical primitives:

- `ResourceSpec(kind, identifier)` and `attach_resources()`;
- full-agent resource and raw-scope collection;
- resource-to-`databricks.yml` projection with minimum permissions;
- OBO-scope derivation;
- an internal Apps host manifest containing resources, scopes, effects, and
  App-to-App intent;
- additive bundle resource/scope merging;
- a generated AppKit host and loopback Python tool bridge.

Four behaviors prevent this from being a coherent authorization contract:

1. `CompileContext` contains one workspace client, and its dependency resolver
   maps both the App-client and user-client dependencies to that client.
2. The AppKit bridge always constructs that client from the forwarded user
   token, so intentional service-principal execution is impossible there.
3. Every tool manifest hard-codes `requires_user_context=true`.
4. Every collected resource produces both an App-SP resource grant and an OBO
   user scope, even when only one identity uses it.

Additional deployment gaps are also confirmed:

- reconciliation is optional behind `--auto-update-yml`;
- the normal precheck warns only about missing scopes and does not fail closed;
- App-to-App `CAN_USE` intent is not materialized into the bundle;
- existing resource matching can collide when two same-kind UC objects share
  the same final name;
- no common App-family group permission declaration or effective-contract
  summary exists.

## Architecture

```text
Agent and tool declarations
        |
        v
Identity inference + explicit override
        |
        v
AuthorizationPlan
  - operation identity
  - request-context requirement
  - user resource requirements
  - service resource requirements
  - OBO scopes
  - App-to-App dependencies
  - App access groups
        |
        +--> AppsHostManifest / AppKit dispatch
        |
        +--> databricks.yml reconciliation
        |
        +--> deployment validation + summary
        |
        v
Databricks IAM / Unity Catalog / resource ACL enforcement
```

`AuthorizationPlan` is an internal compiled data model. It is the sole source
for the generated manifest, bundle changes, validation, and summary. It does
not become a second author-facing configuration file.

## Identity Model

### Credential identity

An operation has one Databricks credential identity:

```python
ExecutionIdentity = Literal["user", "service"]
```

The identity is inferred from existing dependency declarations:

| Dependency | Compiled identity |
|---|---|
| `Dependencies.Client` | `service` |
| `Dependencies.UserClient` | `user` |
| `Dependencies.Workspace` | `user` |
| `Dependencies.Sql` | `user` |

`Dependencies.Headers`, `Dependencies.Principal`, and `Dependencies.Request`
require request context but do not decide which credentials access Databricks.
`Dependencies.Progress` and `Dependencies.State` do not decide identity.

### Explicit override

The existing `ToolMetadata` and `@tool` decorator gain an optional execution
field; `build_tool()` exposes the same field rather than creating another
metadata mechanism:

```python
@tool(effect="update", execution="service")
def run_reconciliation(ws: Dependencies.Client) -> str:
    ...
```

Explicit execution is for background work, application-owned operations, and
closure-based tools whose signature cannot reveal the client they use.

Rules:

- An explicit value must agree with dependency inference.
- A tool containing both user-client and service-client dependencies is
  rejected. It must be split into two auditable operations.
- A resource-bearing tool with no credential dependency and no explicit value
  defaults to `user`, preserving today's fail-closed behavior.
- A pure tool with no Databricks resource or client runs without OBO under the
  service execution path; it receives request metadata only if it declares a
  request-context dependency.
- `Dependencies.Client` now resolves to the real App client. This is a bug fix
  to the documented public contract, not a new behavior choice.
- Model invocation, background Jobs, App-to-App calls, and application-owned
  telemetry are service-scoped.
- User-governed SQL, Genie, catalog discovery, Files, and UC access remain
  user-scoped unless the author explicitly and consistently declares service
  execution.

### Request context

Credential identity and request context are separate facts. A service-scoped
tool may still consume `Dependencies.Headers` for attribution. In that case APX
forwards bounded identity/request headers but does not use the forwarded OAuth
token to construct the service client's credentials.

The plan therefore records both:

```text
execution_identity: user | service
requires_request_context: true | false
```

## Resource Authorization

`ResourceSpec` remains identity-neutral. The compiler associates a tool's
resources with that tool's compiled execution identity.

Aggregation rules:

- A resource used only by `user` operations contributes required OBO scopes,
  but no App-SP data grant.
- A resource used only by `service` operations contributes the minimum native
  App resource permission, but no user scope.
- A resource used by both identities contributes both once.
- The agent's own model endpoint is always service-scoped.
- Model Serving sub-agent endpoints are service-scoped.
- Databricks App peers are service-scoped and require `CAN_USE`.
- Raw scopes declared through `require_user_api_scopes()` are allowed only for
  user-scoped operations. A service operation must declare a concrete resource
  or an existing native service permission path.

The implementation reuses the current minimum permissions, including
`CAN_QUERY`, `CAN_USE`, `CAN_RUN`, `EXECUTE`, `SELECT`, `USE_CONNECTION`, and
`CAN_CONNECT_AND_CREATE`. Jobs and Databricks Apps join the common declaration
path because this design requires `CAN_MANAGE_RUN` and `CAN_USE`. Plugin-specific
structured resources whose shape does not fit `(kind, identifier)`, such as a
secret's scope/key pair or Lakebase branch/database pair, retain their existing
typed bundle/plugin configuration; the authorization validator and summary
still include them.

The implementation must resolve the current OAuth scope vocabulary from
authoritative current platform/AppKit metadata before changing mappings. Any
Databricks CLI discovery command uses the explicitly selected `fevm` profile;
the ambient `DATABRICKS_CONFIG_PROFILE` is ignored.

## AppKit Runtime Dispatch

The Python execution context will carry distinct clients:

```text
service_client = App runtime service principal
user_client    = forwarded user OAuth token
```

Dependency resolution maps `Dependencies.Client` to `service_client` and the
user client dependencies to `user_client`. Local/non-Apps compatibility may
use one CLI-configured client only when no Apps identity boundary exists; Apps
runtime execution never silently substitutes the service client for a missing
user client.

The TypeScript host consumes each compiled tool identity:

- user tools use AppKit's user-scoped execution path and require a forwarded
  access token;
- service tools use a true service-scoped dispatch path, not
  `requiresUserContext=false` metadata attached to a provider that AppKit still
  wraps with `.asUser(req)`;
- request identity headers remain available for audit/context when declared;
- the bridge receives only the credential material appropriate to the chosen
  identity;
- tool effects and AppKit's native approval behavior remain unchanged.

The TypeScript manifest interface will consume the complete Python manifest
shape instead of silently ignoring top-level resources, scopes, and App-to-App
requirements. Concrete per-Agent resources continue compiling directly to the
bundle. The static APX AppKit plugin manifest remains limited to invariant
plugin-wide requirements.

## App-to-App Authorization

APX already distinguishes Databricks Apps URLs from Model Serving endpoints.
The compiler will turn each App peer into a native App resource binding with
`CAN_USE` for the caller App's service principal.

The existing peer URL is not sufficient to invent an App ID. During an
authenticated deployment, APX performs read-only discovery under the selected
profile and matches the exact deployed App URL to one App ID. Zero or multiple
matches fail before mutation with a precise remediation message. Environment-
backed peer URLs are resolved through the existing environment-resolution
contract before matching. APX never derives an ID from hostname text.

The accepted App-resource bundle shape and ID field are verified against the
current CLI/bundle schema before implementation. The compiler does not guess a
field name from stale training data.

## App-Family Permissions

APX adds one group-only Apps permission block:

```toml
[tool.apx.apps.permissions]
can_use_groups = ["apx-app-users"]
can_manage_groups = ["apx-app-admins"]
```

It compiles to native App permissions:

```yaml
permissions:
  - group_name: apx-app-users
    level: CAN_USE
  - group_name: apx-app-admins
    level: CAN_MANAGE
```

This is the collection-level administration mechanism. Each App retains its
own identity; Apps share the same user/operator groups through reusable APX
configuration. Existing explicit user, service-principal, or group permissions
in `databricks.yml` are preserved. A generated entry that conflicts with an
explicit entry fails rather than overwriting it.

## Bundle Reconciliation

Normal generated-App deployment reconciles the compiled authorization plan.
The existing `--auto-update-yml` option remains accepted for compatibility but
is no longer required to materialize declared authorization on the AppKit
deployment path.

Reconciliation is additive and idempotent:

1. Load the round-trip YAML document.
2. Compile the authorization plan from the finalized Agent.
3. Match existing resources by complete resource type and natural identifier.
4. Reuse an equivalent existing entry even when its local key differs.
5. Add missing service resources with minimum permissions.
6. Add missing user scopes.
7. Resolve and add App-to-App `CAN_USE` bindings.
8. Add configured App-family group permissions.
9. Validate the resulting effective contract.
10. Write only when the document changed.

The current final-segment slug remains a display/local handle, not resource
identity. When APX must create a handle, it uses a deterministic full-identifier
digest suffix to prevent same-tail collisions. A matching handle bound to a
different resource is a hard error.

Reconciliation never removes an existing resource, scope, or permission.
Privilege removal is a destructive governance change and remains separately
reviewed.

## Validation and Failure Behavior

Deployment fails before bundle validation when any of these conditions hold:

- explicit execution contradicts a dependency identity;
- one tool mixes user and service client dependencies;
- a user operation lacks a required OAuth scope;
- a service operation lacks a native App resource grant;
- a raw OBO scope is assigned to a service operation;
- a Job is declared for OBO execution;
- an App peer cannot resolve uniquely to a current App ID;
- an App-to-App dependency lacks `CAN_USE` after reconciliation;
- a generated group permission conflicts with explicit bundle configuration;
- two resource declarations collide on a local handle but identify different
  resources;
- an unknown or obsolete OAuth scope is about to be written;
- the Apps runtime would fall back from missing user identity to the App
  service principal.

Errors name the operation, identity, resource, and smallest corrective action.
They never include forwarded tokens, client secrets, or secret values.

## Effective Authorization Summary

Before mutation, deploy/dry-run prints a deterministic summary derived from the
same plan used for reconciliation:

```text
Operation             Identity   Resource                    Authorization
inspect_orders        user       main.sales.orders           sql + user UC grants
run_reconciliation    service    Job 4815162342               CAN_MANAGE_RUN
invoke_model          service    databricks-claude-sonnet     CAN_QUERY
call_inspector        service    inspector App                CAN_USE

App access
apx-app-users         CAN_USE
apx-app-admins        CAN_MANAGE
```

The summary reports identifiers and permission names, never credentials. It
also labels plugin-specific resources preserved from explicit bundle config.

## Compatibility

- `ResourceSpec(kind, identifier)` remains valid and identity-neutral.
- Existing resource-bearing tools without an inferable client remain user-
  scoped and fail closed, matching the current AppKit bridge posture.
- `Dependencies.Client` begins behaving as its documentation promises in the
  AppKit/compiled path. A test that depended on the current accidental OBO
  mapping must be corrected rather than preserved.
- Existing bundle resources, scopes, and permissions remain in place.
- `--auto-update-yml` remains accepted and idempotent.
- The Python Apps host remains the rollback path required by the approved
  runtime-cutover design until live proof and cutover gates pass.
- No new production dependency, credential store, or public listener is added.

## Verification

### Static and focused tests

1. `@tool` and `build_tool` accept and validate explicit execution identity.
2. Dependency inspection infers user and service identity correctly.
3. Contradictory and mixed-client declarations fail compilation.
4. Request-context requirements remain distinct from credential identity.
5. User-only resources produce scopes without App-SP grants.
6. Service-only resources produce minimum App-SP grants without user scopes.
7. A resource used under both identities produces one grant and one scope.
8. The agent model endpoint is service-only and adds no user model scope.
9. A tokenless service bridge call succeeds using the App client.
10. A tokenless user bridge call fails closed before tool execution.
11. A token-bearing user call receives the OBO client, while
    `Dependencies.Client` in the same runtime receives the App client.
12. The TypeScript host selects genuinely distinct user/service dispatch.
13. App-to-App URLs resolve exactly and produce native `CAN_USE` resources.
14. App-family groups produce native `CAN_USE` / `CAN_MANAGE` permissions.
15. Same-tail UC identifiers do not collide.
16. Bundle reconciliation is deterministic, additive, and idempotent.
17. Missing/contradictory authorization fails before deployment.
18. The effective summary matches the reconciled plan.
19. Existing AppKit parity, effects, approval, proxy, cancellation, and
    generated-example tests remain green.

### Repository gates

After focused tests:

```bash
cd typescript && npm ci && npm run build
make check
cd python && uv run --frozen pytest
```

The sanitizer/check sequence and final `git status --short` must show no
unexplained lockfile or generated-file churn.

### Live `fevm` identity proof

Live proof occurs only after local gates and separate deployment consent. Every
Databricks command includes `--profile fevm`; the ambient profile is ignored.

The proof requires named disposable targets and records redacted evidence that:

1. A user-only resource succeeds as the signed-in `fevm` user and fails as the
   App service principal.
2. An App-only resource succeeds as the App service principal and fails as the
   signed-in user.
3. A user operation without a forwarded token fails closed and never retries
   as the App principal.
4. A service operation succeeds without a forwarded user token.
5. App-to-App invocation succeeds only after the caller App receives
   `CAN_USE`.
6. App logs and generated artifacts contain no forwarded token or secret.

The proof receipt records the authenticated user, App names/IDs, non-secret
resource identifiers, deployment commit, commands, timestamps, and observed
statuses. It never records credentials.

## Delivery Boundaries

Implementation is split into independently owned slices that converge on the
same authorization-plan contract:

1. Python identity inference, metadata, and dual-client dependency resolution.
2. Authorization-plan resource/scope partitioning and manifest projection.
3. TypeScript AppKit user/service dispatch and manifest consumption.
4. Bundle reconciliation, App/group permissions, A2A resolution, validation,
   and summary.
5. Cross-language integration tests and repository gates.

Commits remain distinct from the current runtime-parity commits. No live
deployment is bundled into implementation commits.

## Resolved Design Choices

- Identity belongs to an operation, not to `ResourceSpec`.
- Existing `Dependencies.*` types are the canonical inference signal.
- Explicit execution metadata is an override for otherwise ambiguous tools,
  not a mandatory annotation on every tool.
- Mixed user/service client dependencies in one tool are rejected.
- User-only resources do not grant the App service principal access.
- Service-only resources do not request user OAuth scopes.
- Collection-level administration uses groups, not shared credentials.
- Concrete Agent resources compile directly to `databricks.yml`; static AppKit
  plugin manifests describe only invariant plugin requirements.
- Reconciliation is automatic, additive, and non-destructive.
- Databricks remains the final authorization authority.
