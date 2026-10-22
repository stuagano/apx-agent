# Spec: Apps API deploy path

**Status:** implemented in #913 (`backend = "apps_api"`) · **Date:** 2026-10-09

This is the implementation spec for a new durable deploy path,
`python/src/apx_agent/_apps_api_deploy.py`. It records the reviewed seven-step
sequence plus the corrections that the draft still got wrong against current
code. It is not a license to implement until this draft is accepted.

Code wins where a platform doc disagrees with it. In particular,
`docs/deploy/agentbricks-platform.md` says APX "declares and binds" session
stores and has "no APX create path." That is today's behavior. This spec does
not change it until a public AgentKit session-create method is verified.

## Decision

Durable deploys stay on the existing Agent Bricks CLI path
(`deploy_native_project` → `databricks_agentbricks.cli.app deploy`) unless the
declaration selects the new Apps API path. The new path owns its own app name,
source command, and rollout. It must not inherit the CLI path's
`agent-bricks-` prefix or 30-character name limit.

The new path is selected by one new declaration field:

```toml
[tool.apx.agent.deploy]
backend = "apps_api"   # absent or "agentbricks" keeps today's path
space = "app-space"    # optional; present means App Space, absent means dedicated
```

`backend` is a closed set: `agentbricks` (default) or `apps_api`. Anything else
is a config error. `space`, `instances`, and `autoscale` keep today's
`DeployConfig` rules: a space name is a non-empty literal, and a space cannot
be combined with `instances` or `autoscale`.

This is the routing table. "Four rows" means these four, and no others.

| Row | `backend` | `space` | `agents deploy` | `doctor --durable` Deploy stage |
|---|---|---|---|---|
| 1 | absent or `agentbricks` | absent | today's `deploy_native_project` | today's `validate_cli_deployment` |
| 2 | absent or `agentbricks` | set | today's space bundle path | today's `validate_space_bundle` |
| 3 | `apps_api` | set | this spec, App Space branch | preflight the same Apps API space rules, offline |
| 4 | `apps_api` | absent | this spec, dedicated branch | preflight the same Apps API dedicated rules, offline |

Rows 1 and 2 are unchanged. Doctor does not deploy, create apps, or create
stores. For rows 3 and 4 it checks the declaration and the staged source shape
only, with the same offline posture as today.

## Explicit deployment attachments

Dedicated `apps_api` projects may declare deployment-owned service attachments
without inventing service tools or changing the identity of user operations:

```toml
[[tool.apx.agent.deploy.resources]]
kind = "experiment"
identifier = "1694681152367532"
permission = "CAN_MANAGE"

[[tool.apx.agent.deploy.resources]]
kind = "sql_warehouse"
identifier = "e60471096d7311eb"
permission = "CAN_USE"

[[tool.apx.agent.deploy.resources]]
kind = "serving_endpoint"
identifier = "databricks-claude-opus-4-8"
permission = "CAN_QUERY"
```

These three kinds and their corresponding permissions are the closed supported
set. Identifiers must be literal platform identifiers, without environment or
template expansion. Unknown fields and duplicate declarations fail validation.
App Space and other backends reject nonempty declarations.

The backend renders real service-tool resources first, then these attachments in
declaration order. Duplicate resource identities or generated-name collisions
fail before staging or workspace mutations. No legacy grants are imported: the
result replaces the App's resource array, and an absent list preserves the
existing derived-only behavior. Attachments do not modify compiled tool identity,
user resources, user API scopes, or runtime authorization. A serving endpoint
attachment does not authorize the chat model's AI Gateway scope.
Experiment selection and tracing configuration remain separate declarations.

## Steps

`deploy_apps_api(...)` runs these seven steps in order. Each remote resource
step is get-or-create. A rerun is safe because a step that already succeeded
finds the resource and continues. A rerun is not a checkpoint resume: there is
no saved cursor, and a step always redoes its local work.

Nothing in this path deletes an app, a store, an experiment, or uploaded
source on failure. Nothing is rolled back. An app is never moved between
spaces, and a space app is never silently turned into a dedicated app.

### 1. Stage the source

Start from `stage_native_source`. It already copies `agent.py`, `tools.py`,
`sub_agents`, `agent_server`, `scripts`, `README.md`, `.apx-agent`, `.apx`,
`skills`, and `apx_agent-*.whl`, and it refuses an unowned or symlinked
`.build`.

Add an explicit `include` list on the Apps API deploy declaration. Those paths
are copied into the same staged tree after the standard files. `include` is
empty unless declared. It does not become a grab-everything glob.

Then `build_native_manifest` writes `agent.toml`. For a composite agent (any
non-`LlmAgent`), the generated manifest must not contain a `session_store`
section, even when the durable target auto-attached a managed session.
`inspect_target` already drops that auto-session for composites and still
compiles; `build_native_manifest` does not yet match it, and this path makes
it match. An explicit `session_store=` on a composite still raises
`session_store currently requires LlmAgent`. An `LlmAgent` with an effective
managed session still writes `session_store`.

Then write `app.yaml` with the declared entrypoint and the env from step 5.
The command is `["python", "-m", <entrypoint>]`. There is no default of
`apx_agent._serve` on this path. The entrypoint is a new required declaration
for `backend = "apps_api"`:

```toml
[tool.apx.agent.deploy]
backend = "apps_api"
entrypoint = "app"    # module run as python -m app
```

### 2. Ensure the declared stores exist

Managed memory uses the existing `provision_managed_memory` contract:
`memory_stores.get`, create only when the error is not-found, and raise
anything else, including permission errors. A permission error stops the
deploy. The wrapper appends a grant hint, not a CLI error string; this path
keeps that existing message rather than inventing a new one.

A session store is considered only when the effective session is managed and
survived composite suppression. That is an `LlmAgent` whose declared or
auto-attached session is managed. Composite agents skip this step even if
durable auto-attached a session. The effective-session rule is new for this
path. Current doctor still probes `config.session` and does not apply that
suppression.

Do not call a session-create API in the first implementation. No public
AgentKit method has been verified. Until one is verified, this step preflights
the existing store with `session_stores.get` and fails loudly when it is
missing or unreadable. That is stricter than current doctor: a durable deploy
with `provision_stores` warns and continues on a missing session store. Adding
`session_stores.create` is a separate change that names the verified method.

### 3. Ensure the app exists

`apps.get(name)`. If it is missing, `apps.create(name, space=..., description=...)`.
A space deploy passes the declared space. A dedicated deploy omits `space`.
This path never sends `no_compute`.

Space membership is checked both ways, not only the way
`validate_space_deployment` checks it today:

- An existing app whose space differs from the declared space is refused.
  That is today's "refusing to move" rule.
- A declared space app whose existing app has no space is refused.
- A dedicated declaration whose existing app belongs to a space is refused.

An existing app whose placement already matches is reused, including an
existing dedicated app. The deploy waits until that app's compute reports
`ACTIVE` before grants. It does not create the space. A missing space fails
with the existing named space error.

### 4. Grants

Dedicated app: apply the authorization plan's `user_api_scopes` and service
resources through the apps create-update API with an explicit `update_mask`.
The mask lists only the fields this step is allowed to send. No other app
field is included.

App Space app: it inherits scope and resources from the space, so this step
sends no app update. It still runs the existing online space check
(`validate_space_deployment`'s scope and service-resource checks), using the
current scope fallback. A space app cannot reach this update, so a forbidden
grant field cannot be sent.

Tool-level Unity Catalog and workspace grants are not part of this path. The
existing authorization plan reports them, as it does today, and does not
attempt them here.

### 5. Runtime store and env

This stays after app creation. `get_or_create_backend(client, app_name, app_sp)`
needs the app service principal, which does not exist until step 3.

`runtime_store_env` grows a placement argument rather than a weaker LIQUID
rule. The check is both ways:

- A space deploy reads the raw REST app payload, because the installed SDK
  deserializes `LIQUID` as `None`. The app must have `space` equal to the
  declared space and `compute_size == "LIQUID"`. Anything else fails. "Check
  LIQUID only when a space is declared" is not the rule: that wording would
  accept a non-LIQUID space app.
- A dedicated deploy does not enter the space/LIQUID branch. It still calls
  `get_or_create_backend` with the app's service principal and returns that
  env.

The Runtime Store env is merged with APX env: `APX_MODEL`, `MLFLOW_TRACKING_URI`,
the declared env table, and the memory store name when one was provisioned.
The session store name is included only for the effective managed session from
step 2. The experiment is resolved with the existing auto-experiment logic.

Experiment and trace grants stay best-effort, matching `_grant_experiment_to_sp`
and `_grant_trace_uc_tables_to_sp`. The experiment grant is `CAN_MANAGE`, not
`CAN_EDIT`. A grant failure is logged and does not fail the deploy. Agent
Bricks' stricter `trace_grant == "granted"` policy is not imported here.

### 6. Upload and roll out

This path does not call `deploy_native_project`. Upload is:

```text
databricks sync .build <ws_path> --exclude uv.lock
databricks apps deploy <name> --source-code-path <ws_path>
```

`<ws_path>` defaults to `/Workspace/Users/<me>/apx_deployments/<name>`, where
`<me>` is the current workspace user. The app name is the declared app name,
not an `agent-bricks-` prefixed name.

The sync excludes the project root `uv.lock` only. The staged lock at
`.build/uv.lock` is what the app must install from, and it must be part of the
uploaded tree. Before upload, `_stage_build_manifest` still owns that lock:

- wheel deploys regenerate `.build/uv.lock` with `uv lock`;
- other deploys copy the source lock;
- `_sanitize_uv_lock` then rewrites Databricks pypi-proxy URLs to public PyPI.

`UV_FROZEN` is unset only for that one `uv lock` subprocess. The code does not
do this today; this spec adds it because a frozen environment would make the
regeneration fail or no-op. Sanitization and the unknown-mirror warning stay
as they are.

### 7. Verify

Wait until the app reports `RUNNING`. Then run the existing `/readyz` gate
with `durable: true`, then the existing native execution smoke
(`_check_native_execution`, one `POST /api/invocations`). Session readback
runs only when an effective managed session store exists.

- `/readyz` and the smoke invocation fail the deploy when they fail.
`_maybe_write_deploy_state` stays best-effort. It logs a missing record and
does not fail the deploy.
- `register_uc` stays on its current default policy and is not redefined by
this path. If that policy currently fails the deploy, it still does. If it
does not, this spec does not make it start doing so.

## Failures

Permission errors while creating a memory store, creating the app, reading a
required session store, or confirming space placement stop the deploy. The
error names the failed call and the permission or space mismatch. It does not
delete the resources created by earlier steps.

A failed verify leaves the new deployment in place and exits non-zero. The
next run reuses the app and replaces the uploaded source.

## Migration

PR 3 is the only place `python/examples/data-triage-agent` changes, and only
after the live proof on the PR 2 branch has passed. The proof reuses the
existing app; a failed proof must not delete it.

The bundle name and the app name to reuse are different facts and must stay
distinct:

- `pyproject.toml` declares `bundle_name` and `app_name` as
  `mcp-data-triage-agent`. The `mcp-` prefix is required for External MCP
  discovery.
- The draft's empty `mcp-data-triage-durable` app is a different name. The
  proof must name the exact app it reuses. If the live app is
  `mcp-data-triage-agent`, say that. If it is
  `mcp-data-triage-durable`, the declaration changes to that name deliberately,
  with the `mcp-` prefix preserved.

`databricks.yml` is not deleted just because its deploy config moved into
`[tool.apx.agent.deploy]`. It currently carries facts the declaration does
not: command `python -m app`, `DATA_INSPECTOR_URL`, the experiment variable,
the local `databricks_tools_core` tree, and the extra source files `app.py`,
`api.py`, `prompts.py`, and `integrations/`. Those move into `entrypoint`,
`env`, and `include` first. The bundle file is deleted only after a staged
source read-back shows those files and env values present.

The agent is a composite (`KeywordRouter`), and the project explicitly
declares no managed session. The proof asserts that no managed session store
was created.

## Testing

Unit tests use a mocked `WorkspaceClient`. They assert:

- the seven steps run in the order above, and a second run does not call
  create for a resource the first run already created;
- `apps.create` receives `space` for a space deploy, omits it for a dedicated
  deploy, and never receives `no_compute`;
- a dedicated update carries an explicit `update_mask` and no unlisted field;
- a space deploy issues no app grant update at all, not merely a skipped-flag
  setting, and still runs the space scope check;
- the raw payload LIQUID check runs for a space deploy and does not run for a
  dedicated deploy;
- an existing matching app is reused;
- space A to space B, space to dedicated, and dedicated to space are all
  refused, with no create and no update;
- session preflight runs only for an effective `LlmAgent` managed session;
  a composite skips it even when `config.session.type == "managed"`, and no
  session create method is called;
- staged `app.yaml` has the declared command and merged env;
- a claim-vs-reality read-back of the staged `agent.toml` shows no
  `session_store` for a composite and shows it for an `LlmAgent`.

Routing tests cover all four table rows for both `agents deploy` and
`doctor --durable`. Doctor tests stay offline and assert which preflight runs,
not that a workspace object was created.

The live proof is one deploy of data-triage into the declared space, already
approved for that app. It checks `/readyz` with `durable: true`, that `/mcp`
lists the tools, one real `/api/invocations` call, a non-empty Runtime Store
project, and no managed session store. It does not grant tool-level UC or
workspace permissions.

## Delivery

Three PRs, each opened against `main`, never stacked:

1. The fixes already found at `803c7e55`: preflight, composite sessions, and
   the space scope fallback. Confirm that commit is still the intended base
   before opening the PR. This PR does not add the Apps API path.
2. The `backend`, `entrypoint`, and `include` declaration fields, the routing
   table, and `_apps_api_deploy.py`. This PR does not migrate data-triage.
3. The data-triage migration, opened only after the live proof on PR 2 passes.

Issue `#908` is doctor routing and lands before PR 2. Its routing is temporary:
PR 2 replaces it with the full four-row table. `#908` must say that, so it is
not treated as the final doctor contract. Confirm `#908` and `#905` still
match these descriptions before relying on them. `#905` is the composite
`agent.toml` session omission in step 1, not a new session-create API.

## Out of scope

- Implementing session-store creation before the AgentKit method is verified.
- Changing Agent Bricks naming, rollout, or trace-grant strictness.
- Tool-level UC and workspace grants.
- Deleting a failed app or rolling back an uploaded source tree.
- Editing the data-triage example before PR 3.
