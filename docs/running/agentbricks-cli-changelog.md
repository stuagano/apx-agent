# Agent Bricks CLI changelog

This is the changelog for the `databricks-agentbricks` package. Databricks
publishes a GitHub release for some versions and only a git tag for others, and
a release body is a pull-request list. Each section below is written from the
unpacked sdist, diffed against the previous PyPI release. A line marked
*(diff)* comes from that sdist. A line marked *(notes)* is in the GitHub
release body. *(diff, notes)* is both. A *(notes)* line that the sdist does not
contain is called out as such (usually CI or tests, which the sdist omits).

Dates are the PyPI `upload_time` (UTC). The GitHub `published_at` is named when
a release exists. Every release requires Python `>=3.10`.

The apx extra in `python/pyproject.toml` is
`databricks-agentbricks[langgraph]>=0.9.0,<0.11`. That range installs 0.9.x and
0.10.x. It does not install 0.11.0, and this page does not change the pin.

PyPI versions, checked 2026-11-23 with `pip index versions databricks-agentbricks`
and the [PyPI JSON](https://pypi.org/pypi/databricks-agentbricks/json): 0.2.0,
0.3.0, 0.4.0, 0.5.0, 0.5.1, 0.6.0, 0.7.0, 0.7.1, 0.8.0, 0.9.0, 0.10.0, 0.11.0.
There is no 0.1.0.

## How to add the next version

1. List versions with `pip index versions databricks-agentbricks` or the PyPI
   JSON `releases` object. If a version is missing from this page, add it.
2. Download the new sdist and the previous one:
   `pip download --no-deps --no-binary :all: databricks-agentbricks==X`.
3. Diff `pyproject.toml` (dependencies, extras, console scripts) and
   `src/databricks_agentbricks/cli` plus `src/databricks_agentkit`. Write the
   section from that diff. Do not copy a behavior claim the diff does not show.
4. Request `GET /repos/databricks/databricks-ai-bridge/releases/tags/databricks-agentbricks-vX`.
   Link the release only when that call returns a release. A tag is not a
   release: 0.2.0 through 0.5.1 have tags and no release body.
5. Insert a new section directly under this heading, newest first, with the
   same headings and the same *(diff)* / *(notes)* marks:

```markdown
## X.Y.Z

**Released:** YYYY-MM-DD (PyPI `upload_time`).

**Release notes:** link, or "None."

**Compared with:** the previous PyPI sdist.

### Commands and flags

### Behavior

### Removals and renames

### Dependencies

### What this means for apx-agent
```

Use `None.` when a heading has nothing to report.

## 0.11.0

**Released:** 2026-11-20 (PyPI `2026-11-20T22:31:41Z`).

**Release notes:** [databricks-agentbricks v0.11.0](https://github.com/databricks/databricks-ai-bridge/releases/tag/databricks-agentbricks-v0.11.0) (published `2026-11-20T22:56:22Z`).

**Compared with:** 0.10.0.

### Commands and flags

- *(diff, notes)* `agentbricks memory changes list` and `agentbricks memory changes get`.
  List is paged write history (`--store` required, plus `--actor-id`,
  `--source-type`, `--path` / `--path-prefix`, `--memory-entry-id`,
  `--page-size`, `--page-token`). Get returns one change and a content diff
  against the previous version. There is no restore command.
- *(diff)* `experimental models upgrade` is the same command. Its report adds
  median quality, median latency, and median cost per request (before, after,
  and the change).

### Behavior

- *(diff, notes)* The optimizer evaluates candidates on temporary per-model
  clones named `<service>_exp_<id>_<model>`. A clone's model stays fixed;
  switching candidates selects a different clone. Clones this search created
  are deleted on exit.
- *(diff, notes)* Deploy readiness distinguishes `unreachable` (connection
  failure, timeout, refused) from `booting` (HTTP status while the app is
  still starting). In 0.10.0 a connection error was reported as `booting`.
- *(diff, notes)* When the probe cannot tell whether the agent answers, deploy
  still completes and prints: "The deployment completed, but readiness is
  unverified."
- *(diff, notes)* `agentbricks deployments wait` exits 75 while a deploy is
  still running, including one started with `deploy --no-wait`. Telemetry no
  longer records that exit 75 as a failure. Exit 1 remains a failed deploy.
- *(notes)* The release also lists interrupt handling and local deploy
  correlation. The sdist changes `diagnostics.py`, deploy state, and the
  deploy service in that direction; test-only notes are not in the sdist.

### Removals and renames

None.

### Dependencies

None. The base dependencies and the `loadtest`, `langgraph`, `openai`, and
`upgrade` extras are unchanged from 0.10.0.

### What this means for apx-agent

The current extra does not install 0.11.0. `memory changes` is the write
history for a managed memory store (diffs, not a restore) once an environment
actually has 0.11.0. `apx-agent` model upgrade calls
`databricks_agentkit.model_upgrades.optimize_prompts_and_models`, so the
median report and the per-model clones are that optimizer. Native deploy shells
out to `agentbricks deploy`, so the `unreachable` / unverified readiness text
is what that CLI prints. This release does not unblock sandbox downscope
(#1146), a guardrail API (#911), UC Agent Services (#575), or the load-test
warm-up abort. See [Still absent](#still-absent).

## 0.10.0

**Released:** 2026-11-13 (PyPI `2026-11-13T23:49:47Z`).

**Release notes:** [databricks-agentbricks v0.10.0](https://github.com/databricks/databricks-ai-bridge/releases/tag/databricks-agentbricks-v0.10.0) (published `2026-11-14T00:19:06Z`).

**Compared with:** 0.9.0.

### Commands and flags

- *(diff, notes)* `memory pipeline create` and `update` accept `--trigger
  always-on` in addition to `manual` and `scheduled`. The API value is
  `ALWAYS_ON`. Help text: distill as sessions update, plus daily maintenance.
  Pausing an always-on pipeline sets it back to `manual`. Resuming refuses to
  downgrade always-on to `scheduled`.
- *(diff, notes)* `-o` / `--output` is still the root option from 0.2.0. The
  CLI now reads the last `-o` / `--output` on the raw command line after the
  subcommand, including when a usage error fires before the root callback.
- *(diff, notes)* Store permission `--permission` still accepts `READ` and
  `WRITE`. Help and errors show those values in uppercase.

### Behavior

- *(diff, notes)* `memory pipeline run` and `memory pipeline runs get` print
  run stats: actors succeeded, actors skipped, actors failed, and entries
  written.
- *(diff, notes)* `deploy` refuses a missing `app.yaml`, and refuses one whose
  command is still a placeholder (`# todo: set your run command` or
  `replace_with`). It no longer scaffolds `app.yaml`. A directory with
  `agent.toml` and no `app.yaml` is told to add one or re-run `agentbricks
  init`. A directory with neither is told it is not a project.
- *(diff, notes)* Before deploy creates resources, a bound memory store and a
  bound session store are preflighted. `FEATURE_DISABLED` (the stores preview
  is off; the HTTP status is 404) raises with: a workspace admin can enable it
  in Settings > Feature Previews, and it can take a few minutes.
- *(diff, notes)* JSON from `deploy`, `deployments list`, and `deployments get`
  includes `details_url` when the Agents UI link is available.
- *(diff, notes)* Removing the last tool drops the `tools` key instead of
  leaving `tools = []`. Adding a tool replaces an empty `tools = []`, which
  cannot hold a `[[tools]]` table, so remove-then-add keeps `agent.toml` valid.
- *(diff, notes)* LangGraph AI-message `usage_metadata.total_tokens` is
  recomputed as `input_tokens + output_tokens` when both are ints.
  databricks-langchain 0.21.0 counts prompt-cache tokens in `input_tokens` and
  copies a gateway `total_tokens` that leaves them out.
- *(notes)* Gemini thought signatures across tool calls are in the release
  notes. The 0.9.0 to 0.10.0 diff of `langgraph/messages.py` is the
  `total_tokens` fix; signature handling is already described in that module
  in 0.9.0.
- *(notes)* Failure telemetry and captured local startup output are in the
  notes. The sdist adds `diagnostics.py`, `telemetry/`, and an output tail.
  Those are CLI diagnostics, not a new author command.

### Removals and renames

- *(diff)* `AppManifest.scaffold` is gone. Deploy uses
  `AppManifest.require_deployable` instead of writing a fresh `app.yaml`.

### Dependencies

None. Same pins as 0.9.0.

### What this means for apx-agent

0.10.x is the newest release the current extra can install. Authors can pass
`--trigger always-on` on `agentbricks memory pipeline create` and `update`.
apx's own docs still show `--trigger scheduled` as the usual schedule; that
flag still means about every 24 hours. `apx-agent doctor` warns when managed
stores have no pipeline and prints the `memory pipeline create` command; it
does not choose the trigger. Native `agents run` calls `agentbricks dev`, which
writes an app manifest for the local run. `deploy` itself will not invent
`app.yaml` anymore, so a project deployed with the CLI needs a real command in
`app.yaml`. apx does not write `[[tools]]`; the remove/re-add fix is the
upstream CLI. The `total_tokens` correction is in the LangGraph message helper
the durable runtime ships. A workspace with managed stores disabled fails the
deploy preflight with the admin hint instead of failing later. This release
does not unblock #1146, #911, #575, or load-test warm-up. See
[Still absent](#still-absent).

## 0.9.0

**Released:** 2026-11-06 (PyPI `2026-11-06T21:46:37Z`).

**Release notes:** [databricks-agentbricks v0.9.0](https://github.com/databricks/databricks-ai-bridge/releases/tag/databricks-agentbricks-v0.9.0) (published `2026-11-06T23:05:37Z`).

**Compared with:** 0.8.0.

### Commands and flags

None. The Click command tree is unchanged.

### Behavior

- *(diff, notes)* A user-auth sandbox whose policy includes a Databricks access
  token requests `files`, `sql`, `workspace.workspace`, and read scopes for
  catalogs, schemas, and tables. 0.8.0 requested only `workspace.workspace` for
  that case.
- *(diff, notes)* `agentbricks dev` shows a spinner ("Starting the agent…")
  while startup output has gone quiet.
- *(diff)* Under `agentbricks dev`, the chat app can read
  `AGENTBRICKS_DEV_BOUND_MEMORY_STORE` (the memory store name from
  `agent.toml`). The comment in the sdist says memory is off in dev and the
  name is display-only.
- *(diff, notes)* Scaffolded LangGraph and OpenAI agents default to
  `system.ai.claude-sonnet-5-5`. 0.8.0 used `system.ai.claude-sonnet-4-5`.
- *(diff, notes)* The packaged chat app templates change the Memory page
  (entries and pipeline in tabs), the Tools page, and the local-run
  explanation. That UI is the Agent Bricks template, not the apx `/_apx` UI.
- *(diff, notes)* The reflection model used by `experimental models upgrade`
  rejects a litellm result that is not a `ModelResponse`.
- *(notes)* The release notes also list CI and test pull requests (MLflow
  worker count, waiting on `dev` in the template suites, AutoReviewer). Tests
  are not in the sdist. AutoReviewer is not in this package.

### Removals and renames

None.

### Dependencies

None. Same pins as 0.8.0, including `databricks-langchain==0.21.0` on the
`langgraph` extra.

### What this means for apx-agent

0.9.0 is the floor of the apx extra. `apx-agent agents run` on a durable
project is `agentbricks dev`, so the spinner and the "stores are off in dev"
behavior are what that command does. apx scaffolds its own projects; the
template default-model change does not rewrite an apx `agent.py`. User-auth
sandbox scope requests matter when `agent.toml` has a sandbox tool with a
Databricks token. `SANDBOX_DOWNSCOPE_ENABLED` is still `False`. The invocation
error helper `describe_failure` is already in 0.8.0; apx's durable host uses
that contract from this pin onward.

## 0.8.0

**Released:** 2026-10-30 (PyPI `2026-10-30T20:43:49Z`).

**Release notes:** [databricks-agentbricks v0.8.0](https://github.com/databricks/databricks-ai-bridge/releases/tag/databricks-agentbricks-v0.8.0) (published `2026-10-30T21:00:15Z`).

**Compared with:** 0.7.1.

### Commands and flags

- *(diff, notes)* `agentbricks endpoint loadtest`, on the new `loadtest` extra
  (`locust>=2.31`). Flags: `--concurrency`, `--max-duration`, `--max-requests`,
  `--report-dir`, `--requests`, `--save-responses`, `--warmup` / `--no-warmup`,
  plus the shared invoke options (`--path`, `--auth` / `--no-auth`, `--sse`,
  `--timeout`, `--url`, `--method`, `--query`, `--routing-key`).
- *(diff, notes)* `memory stores permissions` and `sessions stores permissions`:
  `get`, `list`, `grant`, `revoke`. Grant takes `--permission` (`READ` or
  `WRITE`) and exactly one of `--user` or `--service-principal`.
- *(diff)* `endpoint invoke` keeps the same HTTP flags. They moved onto a
  shared decorator used by `loadtest`; they were not removed.

### Behavior

- *(diff)* Warm-up is on by default. A response that is not HTTP 2xx raises
  before Locust sends a measured request. The runner error is "Load test
  warm-up failed: … No measured requests were sent." `--no-warmup` skips that
  request. This is unchanged in 0.9.0, 0.10.0, and 0.11.0.
- *(diff, notes)* `databricks_agentkit.runtime.errors.describe_failure` maps a
  failed invocation to a redacted `InvocationFailure` (no credentials, no
  traceback). A Databricks SDK error in the chain is treated as upstream.
- *(diff)* `SANDBOX_DOWNSCOPE_ENABLED` stays `False` (set in 0.7.1).
- *(notes)* `agentbricks dev` reports success only once the agent is up, and
  it binds the selected `--app-port` only. The dev service and presentation
  modules change in this sdist; the release notes name those two behaviors.
- *(notes)* Deploy waits until the agent answers before reporting success.
  `clients/app_probe.py` is new in this sdist.
- *(notes)* A failed invocation says why, instead of a bare 500. That is
  `describe_failure` above.
- *(notes)* The release notes also list chat-app UI rows, a trailing blank
  line on a TTY, worker affinity, and E2E/CI cleanup. The trailing blank line
  is CLI presentation. The test deletions are not in the sdist.

### Removals and renames

- *(diff)* `services/invoke_service.py` is removed. Invoke goes through
  `services/endpoint_service.py` and `services/invoke/`.

### Dependencies

- *(diff, notes)* New extra `loadtest`: `locust>=2.31`. Other extras are
  unchanged.

### What this means for apx-agent

The extra's lower bound is 0.9.0, so a normal install is not 0.8.0. This is
the release that added `endpoint loadtest` and `describe_failure`. apx does
not wrap load test; [endpoint-loadtest.md](endpoint-loadtest.md) records a
0.10.0 run whose warm-up got HTTP 502 and sent no samples. Store permission
commands are upstream. apx does not grant them. Sandbox downscope is already
off.

## 0.7.1

**Released:** 2026-10-29 (PyPI `2026-10-29T23:08:40Z`).

**Release notes:** [databricks-agentbricks v0.7.1](https://github.com/databricks/databricks-ai-bridge/releases/tag/databricks-agentbricks-v0.7.1) (published `2026-10-29T23:20:57Z`).

**Compared with:** 0.7.0.

### Commands and flags

- *(diff, notes)* `tools add sandbox` no longer requires `--scope`. `--scope`
  and `--permission` stay on the command but are hidden, and passing either
  one raises: they are not supported, because `system.ai.sandbox` does not
  accept resource scopes. Sandbox code runs with the selected identity's own
  Unity Catalog access (`--auth user` by default, or `--auth app`).

### Behavior

- *(diff, notes)* `SANDBOX_DOWNSCOPE_ENABLED = False` is introduced in
  `databricks_agentkit.runtime.tool_manifest`. A `[[tools]]` entry with a
  non-empty `policy.downscope` fails to load. The constant does not exist in
  0.7.0; that version required `--scope` on `tools add sandbox`.
- *(diff)* The constant is still `False` in 0.8.0, 0.9.0, 0.10.0, and 0.11.0.
  It is never `True` in any published sdist.

### Removals and renames

None. The flags are hidden and rejected, not deleted from the function.

### Dependencies

None.

### What this means for apx-agent

This is the release that blocks `policy.downscope` on sandbox tools (#1146).
apx reads `agent.toml` and does not write `[[tools]]`. `ToolScope` on a
platform tool stays a compile error until this constant flips. The 0.7.1
notes tell an existing project to delete `downscope` from that tool's
`policy` and grant the sandbox identity access to the data it needs.

## 0.7.0

**Released:** 2026-10-23 (PyPI `2026-10-23T20:40:13Z`).

**Release notes:** [databricks-agentbricks v0.7.0](https://github.com/databricks/databricks-ai-bridge/releases/tag/databricks-agentbricks-v0.7.0) (published `2026-10-23T20:48:48Z`).

**Compared with:** 0.6.0.

### Commands and flags

- *(diff, notes)* `experimental models upgrade`, `apply`, and `rollback`.
  Upgrade flags include `--apply`, `--budget`, `--candidates`, `--train-data`,
  `--val-data`, `--scorer`, `--predict`, `--prompt`, `--run-on`, `--wait`,
  `--timeout-hours`, `--models-only`, `--quality-weight`, `--latency-weight`,
  `--cost-weight`, `--latency-gate`, and `-c`. Rollback takes `--role`.
- *(diff, notes)* `deployments wait` (`--timeout`, `--source`).
- *(diff, notes)* `deploy` gains `--no-wait`, `--allowed-origin`, and
  `--clear-allowed-origins`.
- *(diff, notes)* `deployments delete` gains `--delete-source`.
- *(diff, notes)* `init` gains `--name`.
- *(diff, notes)* `memory stores list` gains `--name` and `--all`.
- *(diff, notes)* `memory pipeline runs list` (`--page-size`, `--page-token`).
  `runs get` was added in 0.6.0.

### Behavior

- *(diff, notes)* Model upgrade lives in `databricks_agentkit.model_upgrades`
  and installs with the `upgrade` extra. `apply` and `rollback` need that
  extra locally; the upgrade job installs it next to the agent project.
- *(diff, notes)* `deploy --no-wait` returns after the foreground checks and
  continues in a child process. `deployments wait` follows that deploy.
- *(diff)* CORS allowed origins are stored on the project from the new deploy
  flags (`databricks_agentkit.runtime.cors`).
- *(notes)* Interactive human-in-the-loop with on-behalf-of auth, finding
  stores by name, and a busy `dev` port failing early are in the release
  notes. The sdist grows the deploy service, apps client, init, and dev
  service in those areas. Test-only notes are not in the sdist.

### Removals and renames

None on the command tree. `cli/tracing.py` shrinks because tracing moves to
`services/tracing_service.py` and `clients/mlflow_trace_client.py`.

### Dependencies

- *(diff)* Base: `requests>=2.28.1,<3`.
- *(diff, notes)* New extra `upgrade`: `gepa>=0.0.26`, `litellm>=1.64.0`,
  `mlflow>=3.11.0`.

### What this means for apx-agent

Below the extra's lower bound. This is where `experimental models upgrade`
and `deploy --no-wait` start. apx's model-upgrade path calls the optimizer
package added here. Native deploy does not add an apx wrapper around
`deployments wait`. Sandbox `--scope` is still required in this version; the
gate is 0.7.1.

## 0.6.0

**Released:** 2026-10-16 (PyPI `2026-10-16T20:45:03Z`).

**Release notes:** [databricks-agentbricks v0.6.0](https://github.com/databricks/databricks-ai-bridge/releases/tag/databricks-agentbricks-v0.6.0) (published `2026-10-16T20:57:28Z`).

**Compared with:** 0.5.1.

### Commands and flags

- *(diff, notes)* `experimental` group. The help text says these commands can
  change between releases without a deprecation period.
- *(diff, notes)* `experimental models bind` (`--default`, `--role`, SERVICE),
  `list`, `set` (MODEL), `status`, `unbind`.
- *(diff, notes)* `experimental prompts bind` (`--key`, PROMPT), `list`,
  `unbind` (KEY).
- *(diff, notes)* `memory pipeline pause`, `memory pipeline resume`, and
  `memory pipeline runs get`.
- *(diff, notes)* `tools add uc-function` gains `--auth`.

### Behavior

- *(diff, notes)* `MemoryStore.put` is new. It is not on `MemoryStore` in
  0.5.1. LangGraph and OpenAI memory modules gain history helpers
  (`langgraph/history.py`, `openai/history.py`).
- *(diff, notes)* `databricks_agentkit.runtime.prompt_registry` is new. Deploy
  can provision model-service bindings from `agent.toml`
  (`services/deployment/model_binding_provisioner.py`).
- *(diff, notes)* Pipeline create/update send `dreamer_policy` (model and
  trigger) on the memory-pipeline API.
- *(notes)* `deploy --output json` includes the deployed memory and session
  stores. `presentation/deploy.py` grows in this sdist.
- *(notes)* An exported `PORT` must not move `agentbricks dev` off
  `--app-port`, and an active `VIRTUAL_ENV` must not leak into the local run.
  `dev_service.py` changes. A failed app lookup is not treated as a missing
  app (`clients/apps_client.py`).
- *(notes)* The notes also list CLI telemetry, a stale
  `app.agentbricksdev.yaml` overwrite, template `mcp<2` pins, and
  `databricks-langchain` 0.21.0 for sandbox results. The langchain pin is
  already exact in 0.5.1; this release's dependency change is the OpenAI
  extra below.

### Removals and renames

None on the command tree.

### Dependencies

- *(diff)* `openai` extra: `openai-agents>=0.8.0` becomes
  `openai-agents>=0.15.0`.

### What this means for apx-agent

Below the extra's lower bound. Prompt bind/list and the prompt-registry
module start here; apx's [prompt-registry.md](prompt-registry.md) describes
the 0.9.0-and-later interface. UC function tools can be added with `--auth`.
`MemoryStore.put` is the managed-memory write the later AgentKit adapter
sits on. apx's memory pipeline doctor text uses `memory pipeline create`,
which already exists; pause, resume, and run lookup start in this release.

## 0.5.1

**Released:** 2026-10-14 (PyPI `2026-10-14T20:16:09Z`).

**Release notes:** None. Tag `databricks-agentbricks-v0.5.1` exists. The
GitHub Releases API returns 404 for it.

**Compared with:** 0.5.0.

### Commands and flags

None.

### Behavior

None. Python sources under `src/` are byte-identical to 0.5.0.

### Removals and renames

None.

### Dependencies

- *(diff)* `langgraph` extra: `databricks-langchain>=0.17.0` becomes
  `databricks-langchain==0.21.0`. The same pin is written into the four
  scaffold `pyproject.toml` files (LangGraph, OpenAI, and both custom-agent
  templates).

### What this means for apx-agent

No runtime change. The langgraph extra's langchain pin, which 0.9.0 and
0.10.0 still use, starts here. The `total_tokens` correction in 0.10.0 is
specifically about databricks-langchain 0.21.0.

## 0.5.0

**Released:** 2026-10-09 (PyPI `2026-10-09T21:02:24Z`).

**Release notes:** None. Tag `databricks-agentbricks-v0.5.0` exists. The
GitHub Releases API returns 404 for it.

**Compared with:** 0.4.0.

### Commands and flags

- *(diff)* `memory pipeline create` and `update` gain `--trigger`. Accepted
  values are `manual` (`MANUAL_ONLY`) and `scheduled` (`SCHEDULED`). There is
  no `always-on` until 0.10.0. `update` also gains `--model`.

### Behavior

- *(diff)* The package is reorganized. CLI code moves to `clients/`,
  `presentation/`, `projects/`, and `services/`. Deploy and dev shrink because
  the work moves into `services/deploy_service.py` and `services/dev_service.py`.
  The command names from 0.4.0 stay.
- *(diff)* Root modules that moved or were replaced include `agent_project.py`
  (now `projects/agent_project.py`), `project_config.py`, `project_types.py`,
  `render.py`, `theme.py`, `tool_access.py`, `databricks_cli.py`,
  `lakebase_runtime_store.py`, and `legacy_lakebase_runtime_store.py`. The
  Lakebase runtime module under `databricks_agentkit` remains.

### Removals and renames

- *(diff)* Those root module paths are the renames. Importing
  `databricks_agentbricks.agent_project` no longer matches this sdist; the
  path is `databricks_agentbricks.projects.agent_project`.

### Dependencies

None.

### What this means for apx-agent

apx imports `databricks_agentbricks.projects.agent_project.AgentProject` and
`databricks_agentbricks.clients.managed_runtime_store`. Those paths exist
from this release forward, not in 0.4.0. The pipeline trigger apx docs call
`scheduled` (about every 24 hours) and `manual` starts here.

## 0.4.0

**Released:** 2026-10-02 (PyPI `2026-10-02T23:30:29Z`).

**Release notes:** None. Tag `databricks-agentbricks-v0.4.0` exists. The
GitHub Releases API returns 404 for it.

**Compared with:** 0.3.0.

### Commands and flags

- *(diff)* `agentbricks doctor` (argument: a directory).
- *(diff)* `agentbricks memory pipeline`: `create`, `list`, `get`, `update`,
  `delete`, `run`. Create takes `--display-name`, `--instructions`,
  `--memory-store`, `--model`, `--session-store`. No `--trigger` yet.
- *(diff)* `endpoint invoke` drops `--session-id` and adds `--routing-key`
  (sent as `X-Routing-Key`). Session continuity stays in the request body.
- *(diff)* `tools add sandbox` gains
  `--databricks-access-token-included` / `--no-databricks-access-token-included`.
  `--scope` is still required.

### Behavior

- *(diff)* The memory-pipeline client and the Lakebase runtime store grow in
  this sdist. `databricks_agentkit/langgraph/responses.py` is added.
- *(diff)* `doctor` is a project check command, separate from apx's
  `apx-agent doctor`.

### Removals and renames

- *(diff)* Invoke's `--session-id` flag is removed in favor of `--routing-key`.

### Dependencies

- *(diff)* Base: `packaging>=23.2`.
- *(diff)* `langgraph` extra: `langchain-mcp-adapters>=0.2.1` becomes
  `>=0.3.0`.

### What this means for apx-agent

This is the first sdist with `memory pipeline` and with `agentbricks doctor`.
apx's replacement for its own memory extraction is `agentbricks memory
pipeline create`, which starts here; `--trigger` is 0.5.0. The durable host
apx compiles to is already `DurableAgentServer` in 0.2.0. This release is
below the extra's lower bound.

## 0.3.0

**Released:** 2026-09-25 (PyPI `2026-09-25T23:43:16Z`).

**Release notes:** None. Tag `databricks-agentbricks-v0.3.0` exists. The
GitHub Releases API returns 404 for it.

**Compared with:** 0.2.0.

### Commands and flags

- *(diff)* The root group and the console script change from `ab` to
  `agentbricks`. The same subcommands remain. Global flags stay `-p` /
  `--profile` and `-o` / `--output`.

### Behavior

- *(diff)* Tracing gains UC warehouse resolution and
  `uc_trace_tables` (metric tables from a trace destination). That is a
  helper used by the tracing commands, not a new subcommand.
  `databricks_agentbricks/trace_tables.py` is added.
- *(diff)* Help text and deploy/dev wording are updated to the `agentbricks`
  command name.

### Removals and renames

- *(diff)* Console script `ab` is removed. The script is `agentbricks`.

### Dependencies

None.

### What this means for apx-agent

apx, its docs, and its deploy path call `agentbricks`, the name from this
release on. `ab` is only 0.2.0.

## 0.2.0

**Released:** 2026-09-25 (PyPI `2026-09-25T16:34:58Z`).

**Release notes:** None. Tag `databricks-agentbricks-v0.2.0` exists. The
GitHub Releases API returns 404 for it. This is the first version on PyPI.

**Compared with:** nothing. This is the baseline sdist.

### Commands and flags

- *(diff)* Console script `ab`. Root flags: `-p` / `--profile`, `-o` /
  `--output`.
- *(diff)* `login`, `logout`, `init`, `dev`, `deploy`, `deployments`
  (`list`, `get`, `logs`, `start`, `stop`, `delete`), `endpoint invoke`.
- *(diff)* `memory` (`bind`, `unbind`, `stores`, `entries`) and `sessions`
  (`bind`, `unbind`, `create`, `list`, `get`, `update`, `delete`, `fork`,
  `items`).
- *(diff)* `tools` (`list`, `remove`, `add sandbox`, `add mcp`,
  `add uc-function`, `add genie-one`, `add genie-agent`). Sandbox add takes
  `--auth`, `--name`, `--permission`, and required `--scope`.
- *(diff)* `tracing` (`bind`, `unbind`, `list`, `get`).
- *(diff)* Not in this version: `doctor`, `memory pipeline`, `experimental`,
  `endpoint loadtest`, store `permissions`, `deploy --no-wait`.

### Behavior

- *(diff)* The README describes `DurableAgentServer` as the runtime to use,
  with `AgentApp` kept as a deprecated alias, and Lakebase-backed durable
  execution. Deploy reads `agent.toml` and rolls out a Databricks App.

### Removals and renames

None. First release.

### Dependencies

- *(diff)* Base: `click>=8.4`, `databricks-ai-bridge[memory]>=0.22.0`,
  `fastapi>=0.129.0`, `sqlalchemy[asyncio]>=2.0`, `uvicorn>=0.20.0`,
  `databricks-sdk>=0.124.0`, `mlflow-skinny>=3.10.1`, `psycopg[binary]>=3.1`,
  `PyYAML>=6.0`, `rich>=13.7`, `tomli>=2.0`, `tomlkit>=0.13`.
- *(diff)* Extra `langgraph`: `databricks-langchain>=0.17.0`,
  `langgraph>=1.1.0`, `langchain>=1.0.0`, `langchain-mcp-adapters>=0.2.1`,
  `mlflow>=3.10.1`, `opentelemetry-exporter-otlp-proto-grpc>=1.25.0`,
  `databricks-agents>=1.9.3`.
- *(diff)* Extra `openai`: `openai-agents>=0.8.0`, `databricks-openai>=0.13.0`,
  and the same `mlflow`, OpenTelemetry, and `databricks-agents` pins.

### What this means for apx-agent

Historical. The extra does not install it. The durable server, `agent.toml`,
managed memory and session commands, and `endpoint invoke` all start here,
under the `ab` command.

## Still absent

Checked in the unpacked sdists, not only in release notes.

- **Load-test warm-up.** From 0.8.0 through 0.11.0, `endpoint loadtest`
  warm-up aborts when the response is not HTTP 2xx and sends no measured
  requests. Use `--no-warmup` to sample that response. The command does not
  exist before 0.8.0. apx does not ship a wrapper (#1132). The recorded run is
  [endpoint-loadtest.md](endpoint-loadtest.md).
- **Sandbox downscope (#1146).** `SANDBOX_DOWNSCOPE_ENABLED = False` in 0.7.1,
  0.8.0, 0.9.0, 0.10.0, and 0.11.0. No sdist sets it to `True`. A sandbox
  `policy.downscope` fails project load. 0.2.0 through 0.7.0 still accept
  `tools add sandbox --scope`.
- **Guardrail API (#911).** No sdist from 0.2.0 through 0.11.0 contains the
  string `guardrail`. Unity AI Gateway external service policies are not this
  CLI. apx `doctor` still names the UI attach step.
- **UC Agent Services (#575).** No sdist from 0.2.0 through 0.11.0 contains
  `Agent Services`. Nothing here registers an agent as a Unity Catalog Agent
  Service.
