# Apps Authorization Compiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Each worker owns only the files named in its task, must preserve unrelated edits, and must not revert work from other agents.

**Goal:** Compile each APX tool's user or App-service-principal identity into one authorization plan that drives AppKit dispatch, Databricks App resources, OAuth scopes, App-to-App grants, family group permissions, validation, and a deterministic deploy summary.

**Architecture:** Add one internal Python authorization compiler beside the existing resource compiler. It infers operation identity from current dependency declarations plus an optional explicit `execution=` override, partitions resource requirements by identity, and exposes projections consumed by the Python manifest and deploy reconciler. The generated TypeScript host dispatches user operations through AppKit OBO and service operations through the App's ambient credentials. Reconciliation remains additive and uses current Databricks bundle shapes; A2A URLs are resolved exactly through the explicitly selected profile before their App names are written to `databricks.yml`.

**Tech Stack:** Python 3.11+, Pydantic v2, FastAPI, Databricks SDK/CLI, ruamel.yaml, TypeScript, `@databricks/appkit`, Vitest, pytest.

**Spec:** `docs/superpowers/specs/2026-09-01-apps-authorization-compiler-design.md`

## Global Constraints

- Use `databricks-core` and `databricks-apps` guidance for every Databricks operation.
- Never infer a Databricks profile. Local mocked tests take an explicit profile argument; live read-only verification uses `--profile fevm` because the user selected it.
- Do not deploy or mutate the `fevm` workspace. Live user-only/App-only proof is a separately approved release gate.
- Preserve the platform-created persistent service principal per App. Never create, share, store, or print service-principal credentials.
- Reconciliation is additive: preserve existing resources, scopes, App permissions, users, groups, and service principals.
- Match resources by complete type plus natural identifier, never by the final identifier segment alone.
- Keep `ResourceSpec` identity-neutral. Identity belongs to the operation using the resource.
- `AuthorizationPlan` is the only derived source for host manifest, bundle reconciliation, validation, and summary.
- Add no dependency; reuse stdlib, Pydantic, ruamel.yaml, the installed Databricks SDK, and AppKit.
- Preserve the existing untracked generated directories under `python/examples/contract-parsing-agent/agent_server/` and `python/examples/databricks-tools-core/build/`.
- Before each commit run the narrow tests, `git diff --check`, and stage only task-owned files.

## File Map

- Create `python/src/apx_agent/_apps_authorization.py`: inference, plan models, aggregation, validation, summary.
- Modify `_tool.py` and `_tool_factory.py`: one optional public `execution` field.
- Modify `_resources.py`: current resource/scope projections and collision-resistant handles.
- Modify `_apps_host_manifest.py`: project only from `AuthorizationPlan`.
- Modify `_compile.py` and `_appkit_tool_bridge.py`: preserve distinct service and user clients.
- Modify `typescript/src/internal/appkit-host.ts`: select OBO or service dispatch per tool.
- Modify `cli.py`: compile, resolve, validate, summarize, and reconcile every Apps deploy.
- Modify `_project_gen.py` only if needed to round-trip family policy without creating duplicate config.
- Update focused Python/TypeScript tests and the four operator docs named below.

---

### Task 1: Add execution metadata and fail-closed identity inference

**Files:**

- Create: `python/src/apx_agent/_apps_authorization.py`
- Modify: `python/src/apx_agent/_tool.py`
- Modify: `python/src/apx_agent/_tool_factory.py`
- Modify: `python/src/apx_agent/__init__.py`
- Test: `python/tests/test_tool.py`
- Test: `python/tests/test_tool_factory.py`
- Create: `python/tests/test_apps_authorization.py`

**Step 1: Write failing metadata tests**

Prove `@tool(execution="service")` and `build_tool(call, name="lookup", description="Lookup", execution="user")` store the same `ToolMetadata.execution` field, preserve existing metadata, and reject invalid/empty values.

Run:

```bash
cd python && uv run --frozen pytest tests/test_tool.py tests/test_tool_factory.py -q
```

Expected: failures because `execution` is not accepted.

**Step 2: Implement the public seam**

Define `ExecutionIdentity = Literal["user", "service"]`. Add `execution: ExecutionIdentity | None = None` to `ToolMetadata`, the decorator overload/implementation, and `build_tool()`. Use `dataclasses.replace()` to merge factory metadata; do not create a second attribute. Export the alias if sibling tool metadata types are public.

**Step 3: Write failing inference tests**

Cover:

- `Dependencies.Client` => service.
- `UserClient`, `Workspace`, and `Sql` => user.
- headers/principal/request require request context but do not select credential identity.
- progress/state do not select identity.
- matching explicit identity succeeds; conflicting explicit identity fails with tool name and both values.
- mixed user/service client dependencies fail.
- a resource-bearing or raw-scope tool with no credential dependency defaults user.
- a pure no-resource/no-client tool uses service and no request context.

**Step 4: Implement operation inference**

Create frozen `OperationAuthorization(name, execution_identity, requires_request_context, resources, user_api_scopes)`. Reuse `_inspect_tool_fn`, existing dependency callables, `get_resources()`, `get_user_api_scopes()`, and `ToolMetadata`; do not build a second annotation parser.

**Step 5: Verify and commit**

```bash
cd python && uv run --frozen pytest tests/test_tool.py tests/test_tool_factory.py tests/test_apps_authorization.py -q
git diff --check
git add python/src/apx_agent/_apps_authorization.py python/src/apx_agent/_tool.py python/src/apx_agent/_tool_factory.py python/src/apx_agent/__init__.py python/tests/test_apps_authorization.py python/tests/test_tool.py python/tests/test_tool_factory.py
git commit -m "feat: infer App tool execution identity"
```

---

### Task 2: Compile the identity-partitioned authorization plan

**Files:**

- Modify: `python/src/apx_agent/_apps_authorization.py`
- Modify: `python/src/apx_agent/_resources.py`
- Test: `python/tests/test_apps_authorization.py`
- Test: `python/tests/test_resources.py`

**Step 1: Write failing partition tests**

Prove user-only resources create only scopes; service-only resources create only App-SP bindings; dual-use resources create one of each; the agent model endpoint and background telemetry are service-only; raw scopes on service operations fail; App peers remain service A2A dependencies; and output ordering is independent of tool order.

**Step 2: Add the aggregate model**

Add frozen `AppDependency(url)` and `AuthorizationPlan(operations, user_resources, service_resources, user_api_scopes, app_dependencies)`. Implement `compile_authorization_plan(agent, model=effective.model)` using the existing full-agent traversal. Dedupe by `(kind, identifier)` and sort.

**Step 3: Update resource projections**

Add supported `job` and `app` kinds. Use current Apps OAuth vocabulary already emitted by repository scaffolds/current manifests: `sql`, `dashboards.genie`, `serving.serving-endpoints`, `vectorsearch.vector-search-endpoints`, plus explicit catalog scopes. Preserve minimum permissions such as `CAN_USE`, `CAN_QUERY`, and `CAN_MANAGE_RUN`.

The live CLI 1.12.1 schema verified this A2A DAB shape:

```yaml
- app:
    name: resolved-app-name
    permission: CAN_USE
```

**Step 4: Make handles collision-resistant**

Use a readable prefix plus a short SHA-256 suffix from complete type+identifier. Test that `main.sales.orders` and `other.sales.orders` differ, identical inputs remain stable, and matching uses type+identifier rather than handle.

**Step 5: Verify and commit**

```bash
cd python && uv run --frozen pytest tests/test_apps_authorization.py tests/test_resources.py -q
git diff --check
git add python/src/apx_agent/_apps_authorization.py python/src/apx_agent/_resources.py python/tests/test_apps_authorization.py python/tests/test_resources.py
git commit -m "feat: compile Apps authorization plans"
```

---

### Task 3: Project the plan into the Python AppKit manifest

**Files:**

- Modify: `python/src/apx_agent/_apps_host_manifest.py`
- Modify: `python/src/apx_agent/_apps_authorization.py`
- Test: `python/tests/test_apps_host_manifest.py`

**Step 1: Write failing manifest tests**

Assert per-tool `execution_identity`, `requires_request_context`, and compatibility `requires_user_context == (execution_identity == "user")`. Assert top-level user/service resources, scopes, and A2A dependencies equal the same plan.

**Step 2: Use one plan**

Make `compile_apps_host_manifest()` compile once. Remove its independent resource/scope collection. Retain existing serialized fields needed by the runtime cutover, but derive them from the plan. Assert serialized output cannot contain tokens, secrets, or headers.

**Step 3: Verify and commit**

```bash
cd python && uv run --frozen pytest tests/test_apps_host_manifest.py tests/test_apps_authorization.py -q
git diff --check
git add python/src/apx_agent/_apps_host_manifest.py python/src/apx_agent/_apps_authorization.py python/tests/test_apps_host_manifest.py
git commit -m "feat: project authorization plan to AppKit manifest"
```

---

### Task 4: Preserve separate clients through Python compilation

**Files:**

- Modify: `python/src/apx_agent/_compile.py`
- Modify: `python/src/apx_agent/_appkit_tool_bridge.py`
- Test: `python/tests/test_compile.py`
- Test: `python/tests/test_appkit_tool_bridge.py`

**Step 1: Write failing dual-client tests**

With distinct sentinel clients, prove `Client` resolves service, `UserClient`/`Workspace`/`Sql` resolve user, missing user client fails only for user tools, service/pure tools run tokenless, and request metadata can accompany a service tool without changing credentials.

**Step 2: Split the context**

Replace the single `ws` with explicit `service_ws: WorkspaceClient` and `user_ws: WorkspaceClient | None`. Update every repository constructor. Do not retain a fallback that maps both identities to one client. Emit a bounded tool-specific error when a user dependency is requested without `user_ws`.

**Step 3: Select identity before client creation**

The bridge finds the tool and operation authorization first. User tools validate the forwarded token and build OBO. Service tools neither require nor read a forwarded user token and use `WorkspaceClient()` ambient App credentials. Both paths pass sanitized request metadata only when declared.

**Step 4: Verify and commit**

```bash
cd python && uv run --frozen pytest tests/test_compile.py tests/test_compile_advanced_agents.py tests/test_appkit_tool_bridge.py -q
git diff --check
git add python/src/apx_agent/_compile.py python/src/apx_agent/_appkit_tool_bridge.py python/tests/test_compile.py python/tests/test_appkit_tool_bridge.py
git commit -m "fix: preserve App user and service identities"
```

---

### Task 5: Dispatch identities in the TypeScript AppKit host

**Files:**

- Modify: `typescript/src/internal/appkit-host.ts`
- Test: `typescript/tests/appkit-host.test.ts`

**Step 1: Write failing dispatch tests**

Fixture one user tool, one service tool, and one service tool requiring request metadata. Assert user invokes `provider.asUser(req)`; service does not; missing user credentials rejects only user execution; top-level authorization fields survive parsing.

**Step 2: Extend manifest and static resources**

Model `execution_identity`, request-context requirement, resource partitions, scopes, and A2A dependencies. Build AppKit static resource declarations from the manifest rather than empty arrays.

**Step 3: Split at the existing execute seam**

Select `asUser(req)` only for user tools and use unscoped provider context for service tools. Preserve the single approval/effect/cancellation pipeline and existing header allowlist. Do not add another bridge endpoint.

**Step 4: Verify and commit**

```bash
cd typescript && npm test -- --run tests/appkit-host.test.ts
cd typescript && npm run build
git diff --check
git add typescript/src/internal/appkit-host.ts typescript/tests/appkit-host.test.ts
git commit -m "feat: dispatch AppKit tools by execution identity"
```

---

### Task 6: Parse App-family policy and resolve A2A exactly

**Files:**

- Modify: `python/src/apx_agent/_apps_authorization.py`
- Modify: `python/src/apx_agent/cli.py`
- Test: `python/tests/test_apps_authorization.py`
- Test: `python/tests/test_deploy_apps.py`

**Step 1: Test family policy**

Parse:

```toml
[tool.apx.apps.permissions]
can_use_groups = ["apx-app-users"]
can_manage_groups = ["apx-app-admins"]
```

Missing blocks yield empty tuples, duplicates sort/dedupe, empty names fail, unrelated config remains untouched.

**Step 2: Implement the narrow reader**

Use stdlib `tomllib` for only this block into frozen `AppFamilyPermissions`. Do not broaden runtime `AgentConfig` or add a general config framework.

**Step 3: Test exact A2A resolution**

Mock Apps with id/name/url. Exact URL (trailing slash normalized only) resolves one `ResolvedAppDependency(id, name, url)`; zero or multiple matches fail; hostname guessing is forbidden; the caller's explicit profile reaches SDK construction; no credentials enter errors or summaries.

**Step 4: Implement resolution**

Reuse the deploy WorkspaceClient seam and `WorkspaceClient(profile=profile).apps.list()`. Retain immutable App ID for validation/audit, but emit matched App name because current DAB schema requires `app.name`.

**Step 5: Verify and commit**

```bash
cd python && uv run --frozen pytest tests/test_apps_authorization.py tests/test_deploy_apps.py -q
git diff --check
git add python/src/apx_agent/_apps_authorization.py python/src/apx_agent/cli.py python/tests/test_apps_authorization.py python/tests/test_deploy_apps.py
git commit -m "feat: resolve App family authorization"
```

---

### Task 7: Reconcile the complete plan on every Apps deploy

**Files:**

- Modify: `python/src/apx_agent/_apps_authorization.py`
- Modify: `python/src/apx_agent/cli.py`
- Modify only if required: `python/src/apx_agent/_project_gen.py`
- Test: `python/tests/test_apps_authorization.py`
- Test: `python/tests/test_deploy_apps.py`
- Test only if generator changes: `python/tests/test_project_gen.py`
- Test: `python/tests/test_cli.py`

**Step 1: Write failing reconciliation tests**

Starting from explicit existing resources/scopes/user/group/service-principal permissions, prove:

- minimum service resources are added;
- user-only resources are not App-SP bindings;
- user scopes union/sort;
- A2A adds `app.name + CAN_USE`;
- groups add native `CAN_USE`/`CAN_MANAGE`;
- explicit entries are preserved;
- full type+identifier matching prevents duplicates;
- same-tail identifiers coexist;
- second reconciliation is a no-op;
- unknown generated scopes, conflicting group policy, or unresolved A2A fail before deploy;
- `--auto-update-yml` remains accepted but no longer gates behavior.

**Step 2: Replace the optional updater**

Rename `_auto_update_databricks_yml()` to `_reconcile_apps_authorization()`. Load round-trip YAML, add service resources/scopes/resolved Apps/groups, validate effective result, and write only on semantic change. Delete `_warn_missing_user_api_scopes()` once no caller needs the legacy path.

**Step 3: Wire normal deploy**

Compile one plan after agent load, resolve A2A with explicit profile, read family policy, print the summary, and reconcile before `databricks bundle deploy`. The legacy flag may report that reconciliation is already automatic.

**Step 4: Add deterministic summary**

Exact sections: sorted user operations, service operations, user scopes, service resources with permissions, A2A URL to name and ID, audience groups, admin groups. Never include tokens, secrets, request headers, or env values.

**Step 5: Handle generated configuration conservatively**

If the existing generator has a natural deployment-policy input, round-trip the family block and test it. Otherwise leave generator defaults empty and document manual configuration; do not duplicate the policy in `AgentConfig`.

**Step 6: Verify and commit**

```bash
cd python && uv run --frozen pytest tests/test_apps_authorization.py tests/test_deploy_apps.py tests/test_cli.py tests/test_project_gen.py -q
git diff --check
git add python/src/apx_agent/_apps_authorization.py python/src/apx_agent/cli.py python/tests/test_apps_authorization.py python/tests/test_deploy_apps.py python/tests/test_cli.py
git add python/src/apx_agent/_project_gen.py python/tests/test_project_gen.py 2>/dev/null || true
git commit -m "feat: reconcile Apps authorization automatically"
```

---

### Task 8: Update operator documentation and design status

**Files:**

- Modify: `docs/superpowers/specs/2026-09-01-apps-authorization-compiler-design.md`
- Modify: `docs/tools/custom-tools.md`
- Modify: `docs/multi-agent/a2a.md`
- Modify: `docs/deploy/apps-vs-model-serving.md`
- Modify: `docs/reference/configuration.md`

**Step 1: Document shipped behavior**

Cover inferred dependency identities, explicit `execution=`, mixed-identity rejection, request context versus credentials, family groups, automatic additive reconciliation, compatibility flag behavior, exact A2A resolution, and one persistent platform-created service principal per App.

**Step 2: Update status accurately**

Only after Tasks 1-7 pass, set status to `Approved; implemented on codex/appkit-parity`. Record implementation divergence as a note instead of rewriting an approved decision.

**Step 3: Verify and commit**

```bash
rg -n "execution=|Dependencies.Client|can_use_groups|auto-update-yml|CAN_USE|service principal" docs/tools/custom-tools.md docs/multi-agent/a2a.md docs/deploy/apps-vs-model-serving.md docs/reference/configuration.md
git diff --check
git add docs/superpowers/specs/2026-09-01-apps-authorization-compiler-design.md docs/tools/custom-tools.md docs/multi-agent/a2a.md docs/deploy/apps-vs-model-serving.md docs/reference/configuration.md
git commit -m "docs: explain Apps authorization compilation"
```

---

### Task 9: Run cross-language reality gates and review

**Files:** Modify only files needed to repair failures within the approved design. Preserve both pre-existing untracked generated directories.

**Step 1: Build AppKit runtime**

```bash
cd typescript && npm ci && npm run build
```

**Step 2: Run focused gates**

```bash
cd typescript && npm test -- --run tests/appkit-host.test.ts
cd python && uv run --frozen pytest tests/test_apps_authorization.py tests/test_apps_host_manifest.py tests/test_appkit_tool_bridge.py tests/test_compile.py tests/test_resources.py tests/test_deploy_apps.py tests/test_project_gen.py -q
```

**Step 3: Run full gates**

```bash
make check
cd python && uv run --frozen pytest
```

**Step 4: Prove profile discipline read-only**

```bash
databricks current-user me --profile fevm -o json >/dev/null
databricks bundle schema --profile fevm >/dev/null
```

Do not run bundle deploy, permission mutation, or resource mutation.

**Step 5: Review complete branch**

```bash
git status --short
git diff --check
git diff --stat 875e8732..HEAD
git log --oneline 875e8732..HEAD
```

Review for identity collapse, privilege broadening, profile inference, destructive reconciliation, secret exposure, and duplicated authorization derivation. Confirm one plan compiler drives all projections. Commit only if a gate repair changed files; do not create an empty verification commit.

## Deferred Live Validation Gate

After local implementation, report exact proposed `fevm` mutations and ask for separate deployment authorization. The proof uses one user-only resource and one App-only resource, verifies success on the intended identity and failure on the other, records App ID/name, resource IDs, commit, commands, timestamps, and results, and cleans up only proof-specific resources. This gate is deliberately outside Tasks 1-9.
