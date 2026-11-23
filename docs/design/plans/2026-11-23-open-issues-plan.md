# Open issues implementation plan (2026-11-23)

> Review plan for Stuart. Do not implement from this document until he says so.
> Other plans in this repo live under `docs/design/plans/`. This file is that location's copy of the requested open-issues plan.

**Goal:** One sequenced plan for all 19 issues open on `main` at `ec90732c` (2026-11-22).

**How this was checked:** Each issue body and comment was read. The cited modules were read on this commit, not from the issue's line numbers. `databricks-agentbricks` 0.10.0 (the version in `python/uv.lock`) was unpacked and its load-test and `DurableAgentServer` failure mapping were read. Agent Services docs were fetched on 2026-11-23 (page last updated 2026-09-11). External service-policy docs were fetched the same day (page last updated 2026-11-18).

**Sizes:** S is one module or a doc plus a focused test. M is several modules, a set of examples, or a relock. L is a live multi-principal proof or a platform-shaped feature. These are scope, not a calendar.

## CI on main

`main` at `ec90732c` is green.

| Run | Result |
|---|---|
| [CI workflow_dispatch on `ec90732c`](https://github.com/stuagano/apx-agent/actions/runs/45001530416) (2026-11-22) | success: `changes`, `typecheck`, `test (3.13)`, `typecheck-ts (22)` |
| [CI push `ac1f667`](https://github.com/stuagano/apx-agent/actions/runs/44877943103) (2026-11-21, "record live session continuity") | **failure in `typecheck-ts (22)` only**. `test (3.13)` passed. The next main CI runs, including the workflow_dispatch above, are green. Not a current-main failure. |

Scheduled "Post-merge CI" runs in 6–13 seconds and are not the pytest gate. The gate is the `CI` workflow (`python/` pytest on 3.13, plus TypeScript typecheck). Local claim-vs-reality remains `make check` and `pre-commit run --all-files`.

## Verdict

| Issue | Verdict | Size of what is left | Blocked? |
|---|---|---|---|
| [#1201](https://github.com/stuagano/apx-agent/issues/1201) | Already shipped (#1208). Close. | — | no |
| [#1053](https://github.com/stuagano/apx-agent/issues/1053) | Already shipped (#1220). Close. | — | no |
| [#1130](https://github.com/stuagano/apx-agent/issues/1130) | Decision and code shipped. Close. | — | no |
| [#1132](https://github.com/stuagano/apx-agent/issues/1132) | Investigation recorded. The gap is #1207. Close. | — | no |
| [#1069](https://github.com/stuagano/apx-agent/issues/1069) | Shipped in #1221, with two checks the SDK cannot read. Close. | — | no |
| [#946](https://github.com/stuagano/apx-agent/issues/946) | Shipped in #1211. TypeScript withdrawn. Close. | — | no |
| [#982](https://github.com/stuagano/apx-agent/issues/982) | Superseded by the Du Bois restore (#1011). Close. | — | no |
| [#1141](https://github.com/stuagano/apx-agent/issues/1141) | Lakebase removal shipped. Gallery vs docs tool lists still differ. | S | no |
| [#1207](https://github.com/stuagano/apx-agent/issues/1207) | Real contract gap. Do not rewrite 502 to 200. | S, plus a live re-run | live app for the acceptance run |
| [#981](https://github.com/stuagano/apx-agent/issues/981) | Launchers regenerated. Extras, comments, and `doctor --durable` are not. | M | no |
| [#1052](https://github.com/stuagano/apx-agent/issues/1052) | Router split shipped. Some chat JS is still inline. | M | no |
| [#911](https://github.com/stuagano/apx-agent/issues/911) | DENY mapping and doctor abstention shipped. Attachment has no API. `ASK` is unmapped. | S now; declare-and-enforce later | attachment API |
| [#855](https://github.com/stuagano/apx-agent/issues/855) | Declare/provision code shipped. Three live residuals open. | M | disposable workspace |
| [#856](https://github.com/stuagano/apx-agent/issues/856) | SDK adapter shipped. One live code remains: unsupported-service. | S | a workspace that returns `FEATURE_DISABLED` |
| [#864](https://github.com/stuagano/apx-agent/issues/864) | Four of six live items have records. Two are blocked. Do not delete the custom runtime. | L | second principal; AgentKit has no cancel route |
| [#862](https://github.com/stuagano/apx-agent/issues/862) | Umbrella. Keep open. Do not implement as one PR. | — | children |
| [#947](https://github.com/stuagano/apx-agent/issues/947) | Decided 2026-11-23: `user:` is the memory pipeline plus the memory tools. `app:` is deploy-time config ([#1233](https://github.com/stuagano/apx-agent/issues/1233)). No store. | S, docs and a drop warning | no |
| [#1146](https://github.com/stuagano/apx-agent/issues/1146) | Still rejected by agentbricks 0.10.0. | S once the flag flips | `SANDBOX_DOWNSCOPE_ENABLED = False` |
| [#575](https://github.com/stuagano/apx-agent/issues/575) | Still Beta. Do not build. | L at GA | GA gates still false |

## Sequencing

### Wave 0 — close, no code

Do these as issue comments. No pull request.

#1201, #1053, #1130, #1132, #1069, #946, #982.

#1141 can join this wave only after the one-paragraph drift note in Wave 1, or the close comment can say the tool-list drift is intentional. Prefer the short doc so the issue's last checkbox is true.

### Wave 1 — independent, can start now

Four pull requests, no shared files if scoped as below:

1. **#1141 residual** — `docs/coworkers/` and `python/src/apx_agent/coworker_gallery/`. Doc only, or make the six pairs identical.
2. **#1207** — a contract test under `python/tests/` and a paragraph in `docs/running/endpoint-loadtest.md`. Do not change the HTTP status DurableAgentServer returns.
3. **#981** — example `pyproject.toml` / `agent.py` comments and locks. Do not touch gallery YAML.
4. **#1052 residual** — `python/src/apx_agent/_ui_chat.py` and `python/src/apx_agent/_static/vendor/`. The router in `_dev_routes/` stays as it is.
5. **#911 `ASK` only** — `python/src/apx_agent/_gateway_refusal.py` and `python/tests/test_gateway_refusal.py`. Leave the doctor abstention alone.

Any of these can merge without the others.

### Wave 2 — code is done; a live workspace is the remaining proof

Not parallel with each other on the same disposable app. They can be parallel with Wave 1.

- **#855** grant-to-another-principal, and in-app startup. Opt-in tests already exist. Needs an operator-owned store, a second service principal, and an already-deployed disposable app. Do not deploy or turn on recovery to make the probe pass.
- **#856** unsupported-service. Needs a workspace whose managed-memory API returns `FEATURE_DISABLED`, `NOT_IMPLEMENTED`, or `UNIMPLEMENTED`. `fevm` and `fevm-stable` return `NOT_FOUND` for a missing store, and that does not count.
- **#864** approval continuation (`apx-ab7`) needs a second user OAuth principal on workspace `7474660401027952`. Downstream caller identity needs a two-app probe and stays tied to #816 / #755 (not in this set). Do not start this by deleting `_chat_agent.py` or `_responses_agent.py`.
- **#1207 acceptance run** is this wave even though the doc/test PR is Wave 1. Re-run `agentbricks endpoint loadtest --no-warmup` against a disposable app whose tool fails with a persisted invocation. Default `--warmup` will still abort; that abort is upstream.

### Wave 3 — do not write code

- **#1146** until `databricks_agentkit.runtime.tool_manifest.SANDBOX_DOWNSCOPE_ENABLED` is `True` in the locked agentbricks.
- **#575** until the Agent Services limitations list drops runtime invocation, null `owner` / `full_name`, and Global Search. A Catalog Explorer paragraph added since the August gate check does not flip the gate.
- **#911 attachment** (issue steps 1 and 2) until External policies can be attached and read back through REST or Terraform. The docs on 2026-11-18 still say UI only.
- **#947** is decided (2026-11-23). `user:` is the memory pipeline and the memory tools, as soft preference memory, not a key-value store. `app:` is deploy-time config and stays on [#1233](https://github.com/stuagano/apx-agent/issues/1233); `{app:…}` is not seeded from `agent.toml` / `[tool.apx]` in this pass. Do not extend `ConversationStore`. Do not add a Lakebase table.

### Wave 4 — tracker only

**#862** stays open as the parent. Refresh its status comment from this plan. Do not open a "platform-first" PR that edits the keep/replace table by deleting files.

---

## Already shipped (close)

### #1201 — registry row without grant statements

**Current state.** Shipped in `e34c7d5c` (`fix(deploy): record the agent registry when grants cannot run`, PR #1208). The issue's three asks are in the code:

- `agents deploy --registry-no-grants` and `[tool.apx.deploy] registry_grants = false` (`cli._registry_grants_enabled`).
- Before `GRANT`, `provision_registry` reads effective privileges with `WorkspaceClient.grants.get_effective` and skips privileges already held (`python/src/apx_agent/_registry.py`).
- A grant failure is caught after the tables exist, the agent row and tool rows are still written, and the deploy logs `registry row written, access not granted` instead of exiting. `_deploy_registry_step` documents that this warning is not a deploy error. Create-table and row-write failures still exit with "the app is STILL LIVE", which is the right failure: the row was not written.

**Tests already on main.** `python/tests/test_registry_provisioning.py::test_grant_failure_after_the_row_is_written_warns`, `test_deploy_step_grant_failure_warns_instead_of_exiting`, `python/tests/test_deploy_apps.py` (`test_registry_no_grants_flag_still_records`, `test_registry_grants_setting_still_records`).

**Proposed approach.** Close the issue, citing #1208. No further flag.

### #1053 — delete the `[tool.apx.agent]` migrator

**Current state.** Shipped in #1220 (`8eb180e9`). `CHANGELOG.md` (Unreleased) says `apx-agent agents migrate-config` was removed after 0.7.7. `_migrate_config.py` is gone. `cli.py` no longer has `allow_agent_table`. A leftover `[tool.apx.agent]` table fails to load (`_tool_config.LEGACY_AGENT_TABLE`, also checked from `_inspection.py` and doctor).

`_tool_config.py` (271 lines) is still the programmatic tool factory (`load_config_tools`) and the rejector for `[[tool.apx.tools]]`. It is not a second config reader for agent settings. Deleting it would remove a public import (`apx_agent.load_config_tools`). That is out of scope for this issue.

**Proposed approach.** Close. The "announce in the changelog" checkbox is the Unreleased note. Do not schedule a second deletion of `_tool_config.py`.

### #1130 — `model_upgrades` vs prompt optimization

**Current state.** Choice (a) is what the code does, and it is written down.

- `docs/evaluate/overview.md` ("Improve a failing step"): the Edit tab calls AgentKit `optimize_prompts_and_models`; `label align` registers the judge.
- `docs/adr/0001-build-on-agent-bricks.md` maps #1130 to that split.
- `python/src/apx_agent/_dev.py` (`_optimize_instructions_sync`) imports `databricks_agentkit.model_upgrades.optimize_prompts_and_models`. The judge is the scorer `label align` registered. `mlflow.genai.optimize_prompts` is not in that module.
- `python/examples/model-upgrades/upgrade_sample_agent.py` is the sample agent. Its docstring says the module does not itself submit a job; `upgrade()` calls `optimize_prompts_and_models` with the label-aligned judge `domain_quality-v2`.
- `python/tests/test_model_upgrades_example.py` asserts the example and the dev UI call `optimize_prompts_and_models` and do not call `mlflow.genai.optimize_prompts`.

`label align` remains its own command (`docs/evaluate/advanced.md`). There is no recorded live scored search in the repo. The issue's done-when is a written decision plus one end-to-end example, and no second optimization loop. Both are met. A live search is an experimental upstream command and spends model budget; it is not required to close.

**Proposed approach.** Close. Do not add another optimizer.

### #1132 — load-test with `agentbricks endpoint loadtest`

**Current state.** ADR 0001 says use the upstream command and do not wrap it. `docs/running/endpoint-loadtest.md` records the 2026-11-21 run against `agent-bricks-apx-dur-gen`: warm-up `POST /api/invocations` returned HTTP 502, Locust sent 0 measured requests, the app stayed `RUNNING` / `ACTIVE`. The contract gap is filed as #1207. `CHANGELOG.md` points at that page.

The issue's done-when is a recorded run and a filed gap when the command does not fit. Both happened. A capacity number was never the acceptance bar (the doc says the page is not one).

**Proposed approach.** Close #1132. The follow-up is #1207, not an apx Locust wrapper.

### #1069 — App Space preflight

**Current state.** Shipped on this commit (#1221) and covered by tests.

- Region and GovCloud fail closed in `check_space_region` (`python/src/apx_agent/_doctor.py`) and deploy calls `_require_space_region` from `validate_space_deployment` (`python/src/apx_agent/_app_space.py`). Tests: `python/tests/test_doctor.py` and `python/tests/test_deploy_apps.py` (GovCloud host and `us-gov-west-1`).
- Workspace partner-powered AI is `check_space_partner_ai`, required at deploy by `_require_partner_ai`. An unreadable setting is a warning. Account-level partner AI is an explicit `SKIP`: `AccountSettingsAPI.llm_proxy_partner_powered_account` needs account-admin credentials.
- Cross-Geo processing is an explicit `SKIP`: the SDK exposes no setting to read.
- Package proxy: `space_package_bypasses` fails doctor and `reject_package_bypasses` fails the staged deploy when a VCS URL, direct URL, or custom index is present. Deploy rewrites apx's own `git+https` requirement into a wheel and exports a hash-free `requirements.txt` (`_apps_api_deploy.py`). The supported install path is recorded in `docs/deploy/agentbricks-platform.md` from the `apx-u9t-space` rollout (2026-11-22): `pip install -r requirements.txt` via `http://package-proxy.host.local:8184`. A `uv sync` against public PyPI failed DNS inside the space. A git URL is not how the space install works.

**Proposed approach.** Close. The two SKIP rows are the honest remainder of "check everything on the docs page." Automating them waits on an account-admin API and a Cross-Geo setting in the SDK, which this issue should not grow a private client for.

### #946 — generated platform client

**Current state.** Shipped in #1211. `python/src/apx_agent/_platform_client.py` defines `PlatformOperation` and `generate_platform_client`. The design doc `docs/design/plans/2026-10-17-generated-platform-client.md` records the 2026-11-20 decision: Python only. TypeScript models and a browser fetch client are withdrawn (#873, #825). `python/tests/test_platform_client_reality_ctk.py` generates a client, `ctk.verify` reads it back, imports it, and checks the method calls the `WorkspaceClient` attribute and returns that result. No in-repo agent declares operations, so no generated file is committed. The requirement is "an agent that declares operations commits the client," not "the repo must contain one."

**Proposed approach.** Close. Do not generate a TypeScript client.

### #982 — per-page AgentKit restyle

**Current state.** The issue was filed against #979, which moved the dev UI toward the AgentKit light palette. `60f62f55` (`feat: restore the Du Bois dev UI`, #1011) put the dark Du Bois tokens back. `python/src/apx_agent/_ui_theme.py` is dark (`color-scheme:dark`, `--apx-bg:#11171C`) and `python/tests/test_ui_theme.py` locks those values.

The small nits in the issue are stale against this theme:

- `.tool-fails-card` and `.trace-diff-card` in `_dev.py` use `background:var(--apx-panel)` and `border:1px solid var(--apx-border)`. They are not border-equals-background.
- The hex guard already globs `_ui_*.py` plus `_dev.py` (`test_hex_guard_covers_files_the_old_list_missed`).
- The "Du Bois" comments in `python/dev-ui/topology/src/types.ts` match the restored theme.
- Topology `<Background color="#37444F">` was invisible on the light canvas. The canvas is dark again. Recheck only if the light restyle is revived.

**Proposed approach.** Close as superseded by #1011. A later AgentKit restyle is a new issue; it would have to change `test_ui_theme.py` on purpose, not sneak past it.

---

## Wave 1 — ready to change

### #1141 — coworker templates off Lakebase

**Current state.** The Lakebase / `responses_agent` half is done. Changelog (Unreleased) says the six gallery coworkers and `docs/coworkers/` (plus `financial-reporting.yaml`) use `type: managed` for session and memory. None of those files declare `type: lakebase` or `target: responses_agent`. Memory choice (a) was applied: managed full-text, no per-template pgvector table.

The last checkbox is not done. All six pairs differ. On `payroll.yaml` the gallery has `tools: []` and a commented Genie block; `docs/coworkers/payroll.yaml` declares a `type: sql` tool. The same tools-block drift is why `diff` reports all six pairs. `financial-reporting.yaml` exists only under `docs/coworkers/` and is already `type: managed`.

**Proposed approach.** Pick one and write it down in `docs/coworkers/README` or the gallery package comment:

- Keep the docs copies as the worked examples (SQL tool filled in) and the packaged gallery as the scaffold skeleton (`tools: []`), and say so in one paragraph. Or
- Make the six docs copies byte-identical to the gallery, and leave `financial-reporting.yaml` as the docs-only example.

Do not reintroduce Lakebase. Do not change memory back to `off`.

**Files.** `python/src/apx_agent/coworker_gallery/*.yaml`, `docs/coworkers/*.yaml`, and whichever test reads both (`python/tests/test_reference_yamls.py` if it asserts equality).

**Depends on.** None. #855's live store proof is not required to finish this checkbox.

**Verify.** `diff` the six pairs. `cd python && uv run --frozen pytest -k 'reference_yaml or coworker'`. `make check` if the YAML fixtures are wide.

### #1207 — durable invocation failure is HTTP 502, so loadtest records no sample

**Current state.** This is not "the app returned a gateway 502 because it was down." On the recorded run the app stayed `RUNNING`. The tool raised `databricks.sdk.errors.platform.ResourceAlreadyExists` (`/Shared/apx-aub-1121-sink/attempt-1.json` already exists). AgentKit persisted the invocation as `failed`. `GET /api/invocations/{id}` returned HTTP 200 with `error.code: UPSTREAM_ERROR`, `upstream_status: 409`. The `POST` returned HTTP 502.

That mapping is `databricks_agentkit.runtime.errors.describe_failure` in agentbricks 0.10.0. If the exception chain contains a Databricks SDK error, the failure is upstream. 429 becomes `UPSTREAM_RATE_LIMITED`, timeouts become `UPSTREAM_TIMEOUT` / 504, and every other SDK error becomes `UPSTREAM_ERROR` / **502**. `DurableAgentServer` then returns that status with a JSON body `{"id", "status": "failed", "error": ...}` (`runtime/app.py`, `InvocationFailedError` handler). A plain `ValueError` stays HTTP 500 `AGENT_ERROR`. That 500 path is already pinned by `python/tests/test_native_invocation_contract.py::test_native_execution_failure_remains_persisted_500`.

`agentbricks endpoint loadtest` treats any status outside 200–299 as `StreamError` and discards the body (`services/loadtest/request.py`). The default warm-up request runs that check before Locust starts (`services/loadtest/_locustfile.py`). On failure the runner exits with `No measured requests were sent` (`runner.py`). Measured requests, once Locust is running, already count that `StreamError` in `summary.json` `errors` and `counts.failed`. The CLI has `--warmup/--no-warmup` (default on) in `databricks_agentbricks/cli/endpoint.py`. The recorded run did not pass `--no-warmup`.

Returning HTTP 200 with `status: failed` would make loadtest count a **success**. `perform_request` does not look at `status` inside a 2xx body. That misses the issue's "counted error" bar and would contradict the AgentKit contract the 500 test pins.

**Proposed approach.**

1. Add a durable-server test next to the 500 test: a `ResourceAlreadyExists` (or another Databricks SDK error) raised from `run_graph_turn` returns HTTP 502, body `status: failed`, `error.code == "UPSTREAM_ERROR"`, `upstream_status == 409`, and `GET` of that id returns 200 with the same error. This freezes the contract so a later change does not "fix" the load test by hiding the failure.
2. In `docs/running/endpoint-loadtest.md`, state that a JSON 502 from a persisted failure is the app answering, and that default warm-up aborts before any sample. To count those errors, re-run with `--no-warmup`. Abort-on-warm-up for a JSON invocation body is an upstream `agentbricks` change (treat a JSON `status: failed` as "the endpoint answered" during warm-up, and still count it as an error). Do not vendor a Locust wrapper. #1132 forbids one.
3. Acceptance of the issue itself is a new recorded run, not the unit test. Use a disposable app and a tool that fails cleanly (the aub create-only sink will 502 on every request, which is a valid error sample, not a capacity number). Record argv including `--no-warmup`, the `summary.json` `counts.failed`, and `databricks apps get` still `RUNNING`. Put the sanitized output next to the existing 2026-11-21 file.

**Files.** `python/tests/test_native_invocation_contract.py` (or a sibling), `docs/running/endpoint-loadtest.md`. No change to `compile_durable_handlers` or `describe_failure`.

**Depends on.** None for the test and the doc. The acceptance run needs the same class of disposable app as the 2026-11-21 run (`fevm-stable`). It does not depend on #1132 staying open.

**External blocker.** Changing default warm-up is upstream agentbricks, not this repo. The `--no-warmup` re-run is available in 0.10.0 today.

**Verify.** The new pytest. For the live run, the doc must show a non-empty `counts.failed` (or `errors`) and zero requirement that warm-up returned 2xx. `make check` for the test.

### #981 — example launchers for DurableAgentServer

**Current state.** The issue's premise ("only `examples/data-inspector` has been regenerated") is out of date. Every `agent_server/start_server.py` under `python/` is byte-identical to `_project_gen._START_SERVER_CONTENT` (701 bytes, `target="durable_agent_server"`). That includes the examples, `hello-world`, `payroll-coworker`, and the `samples-*` apps.

What is not done, against the issue's expected result:

- Many of those projects do not depend on `apx-agent[agentbricks]`. `data-inspector`, `memory_demo`, `data-triage-agent`, and `plg-discovery` do. `slack-agent`, `customer_triage`, the fleet leaves, `hello-world`, and the `samples-*` apps do not.
- Comments still describe the old runtime. `python/hello-world/agent.py` still says the Apps boilerplate is `compile_to_responses_agent`. `python/examples/slack-agent/pyproject.toml` still says the langgraph extra is required for `compile_to_responses_agent`. Ten `agent.py` files among launcher projects still mention `responses_agent` (`hello-world`, `payroll-coworker`, and eight `samples-*`).
- `target = "apps"` in `[tool.apx.deploy]` is the hosting destination, not the runtime (changelog, "Native CLI vocabulary"). Do not rewrite that to `durable_agent_server`. The runtime target belongs on the agent / in the launcher, which already passes `target="durable_agent_server"`.
- `data-triage-agent` has the extra and no `start_server.py`. Treat it only if it is actually an Apps example; do not add a launcher to a library example that never deploys.

**Proposed approach.** For each project that already has `agent_server/start_server.py` and is meant to deploy to Apps:

- Add `apx-agent[agentbricks]` (or `apx-agent[eval,agentbricks]` where eval is already there).
- Replace the `compile_to_responses_agent` comments with the durable launcher.
- Run `apx-agent doctor --offline --durable` in that project.
- Relock with `scripts/check-uv-lock-registry.sh --fix` before commit, as the issue says. A private index re-poisons `uv.lock`.

Do not regenerate `start_server.py` again; it already matches the template. Do one example first (`slack-agent` or `hello-world`) and then the rest in the same shape, so a lockfile failure does not fan out.

**Files.** Those projects' `pyproject.toml`, `uv.lock`, and the stale comments in `agent.py` / `agent_server/__init__.py`. Not `_project_gen.py`, unless `doctor --offline --durable` shows the template itself is wrong.

**Depends on.** None. Independent of #1141.

**Verify.** `diff` each `start_server.py` against the template string (they must stay equal). `doctor --offline --durable` exit 0 in each touched project. `scripts/check-uv-lock-registry.sh` clean. `make check` if a shared test snapshots example manifests.

### #1052 — dev UI router split; chat JS as static assets

**Current state.** The 2,900-line closure is gone. `build_dev_ui_router` in `python/src/apx_agent/_dev.py` (about line 2243) only builds a `DevRouteContext` and calls `_dev_routes.mount`. Families live in `python/src/apx_agent/_dev_routes/` (`chat`, `tools`, `traces`, `eval_ui`, `setup`, `grounding`, `hub`, `approvals`, and the others). `_dev.py` is 2,274 lines, most of it the trace page CSS and helpers, not 87 route closures.

Chat JS is partly static. `_render_agent_ui` loads `/_apx/vendor/legacy-chat-prelude.js`, `legacy-chat-history.js`, `legacy-chat-app.js`, and `native-chat.js` from `python/src/apx_agent/_static/vendor/`. `_ui_chat.py` is 1,610 lines (the issue cited 2,139). It still embeds a shell script (the dashboard tab switcher, starting around line 264) and inlines those vendor files into the HTML for some hosts (`page.replace` of the script tags). `_ui_agentkit_chat.py` no longer exists.

**Proposed approach.** Do not split the router again. Finish the asset move:

- Move the remaining inline dashboard script in `_ui_chat.py` to a vendor file, same pattern as `legacy-chat-app.js`.
- Keep the inline-for-some-hosts behavior only if a host cannot serve `/_apx/vendor/`. If every durable and dev host now serves that route, stop rewriting the tags and let the browser load the files.
- Add one test that fetches the chat page and asserts the script `src` is the vendor path, plus a small node or browser check that the extracted tab switcher changes the frame `src`. The issue's point is that an HTML-string assertion passed while the live page threw (#1011 → #1014). A string contains-check is not the verification.

**Files.** `python/src/apx_agent/_ui_chat.py`, `python/src/apx_agent/_static/vendor/`, `python/tests/test_ui_chat.py` or the existing chat UI tests. Not `_dev_routes/`.

**Depends on.** None. Do not couple this to #982 (that restyle is closed by the Du Bois restore).

**Verify.** Existing UI tests, plus the new script-src / tab test. `make check`. If a browser tool is available, load `/_apx/agent` and confirm the chat frame and a tab click. If it is not, say so; the node test is the stand-in.

### #911 — Unity AI Gateway external guardrails

**Current state.** Three layers, two of them done as far as the platform allows.

1. **Read-back.** `check_gateway_guardrails` (`_doctor.py`) WARNs and lists the model service plus declared `service_policies` attachments. It calls no API. The comment matches the docs: a `ModelService` payload has no policy field, and serving-endpoint guardrails do not govern the AI Gateway chat call. Reporting OK would be a false pass.
2. **Declare and fail closed at deploy.** Not built. Correct, while attachment is UI-only.
3. **DENY mapping.** `python/src/apx_agent/_gateway_refusal.py` maps a gateway `databricks_service_policy` DENY to `GatewayRefusal`, writes the service-policy audit attributes, and does not put the prompt in the message. Tests in `python/tests/test_gateway_refusal.py`. The gateway returns HTTP 200 with that object; apx turns it into a typed refusal. That matches the issue's step 3 for `DENY`.

Docs fetched 2026-11-23 (page updated 2026-11-18) still say: "You attach external service policies through the Unity Gateway UI. Attaching them through the REST API or Terraform isn't available." New since the issue was written: an MCP tool call can return `ASK` (hold for user approval), not only `ALLOW` / `DENY`. `gateway_refusal` does not mention `ASK`.

**Proposed approach.** Now, in one small PR: map `ASK` the way a human-approval pause already works if the payload is the same shape the gateway uses for other holds; if the payload shape is not in the repo or the docs, add a test that an unrecognized `ASK` does not become a generic 500 and does not get treated as `ALLOW`. Do not invent the vendor request body.

Do not add `guardrails = [...]` to agent config until a read API exists. A deploy check that cannot read the attachment cannot fail closed without lying. Leave the doctor WARN as the step-1 implementation.

**Files.** `_gateway_refusal.py`, `test_gateway_refusal.py`. Doctor only if the WARN text should mention `ASK`.

**Depends on.** None for the `ASK` mapping. The declare-and-enforce half waits on Databricks. Not blocked by #575 (Agent Services are a different product).

**Verify.** `cd python && uv run --frozen pytest tests/test_gateway_refusal.py tests/test_doctor.py -k guardrail`. No live attach.

---

## Wave 2 — live proof, code already there

### #855 — declare and provision managed session and memory stores

**Current state.** The declaration and provision path shipped with #866 and the follow-ups. Omitted store names are derived. Doctor stays read-only. A missing store on a native deploy is deferred to Agent Bricks instead of failing the preflight (`check_agent_prerequisites` with `provision_stores=True` records a WARN). Legacy three-part names fail explicitly. `docs/design/managed-state-contract-map.md` records a 2026-10-21 `DEFAULT` probe: `provision_managed_memory` twice was idempotent, session create/get worked, actor isolation held, stores were deleted afterward. The 2026-11-20 `apx-l79` note says the later residuals were **not** run.

Still open, and the opt-in tests already name them (`python/tests/test_managed_state_live_reality_ctk.py`):

| Case | What it needs | What does not count |
|---|---|---|
| Grant to another principal | `APX_MANAGED_STATE_LIVE=1` and `APX_MANAGED_STATE_GRANT_SP` of another sandbox SP | A skipped test |
| Permission denied | A profile that cannot read an existing store | `NOT_FOUND`. #856's 2026-11-20 denied probe (`a-team-dev-memory`, `PERMISSION_DENIED`) covers the memory GET. Point #855 at that record if the same call is the acceptance; do not re-run it just to have a second copy. |
| App-process startup | pytest inside an already deployed disposable app, declared stores, `recovery=false`, no retention | `/readyz` alone, or a local ASGI stand-in |

**Proposed approach.** Do not write another provisioner. Run the existing opt-in cases the contract map already specifies, on operator-owned disposable resources, and append a sanitized record to `docs/design/managed-state-contract-map.md`. Unsupported-service is #856's residual; do not move it here to make this issue look smaller, and do not move this issue's startup case onto #856.

**Files.** The contract-map doc, and the probe test only if a run shows the assertion is wrong. No production change unless the run finds one.

**Depends on.** #854 is the parent and is not in this set. Independent of #1141's YAML cleanup.

**External blocker.** No second principal and no disposable app means the issue stays open. Do not deploy, grant, or enable recovery to manufacture the run.

**Verify.** The junit file from the contract map's example command, with `verified` properties, plus the dated record. A skip is not a pass.

### #856 — managed-memory SDK, unsupported-service left

**Current state.** The hand-written UC REST backend is gone. `_memory_managed.py` uses the AgentKit workspace-scoped store. The 2026-11-20 update on the issue is accurate against the contract map: permission-denied was verified (`AgentCliError` / `PERMISSION_DENIED` on `a-team-dev-memory`, probe `verified=true`). Create/read/search/update/delete and actor isolation have earlier live records. Pagination and the BM25 top-N cap are documented, not claimed as full parity. Migration of old rows stays on #859, which was closed only for a bounded import slice.

The open checkbox is live unsupported-service. Configured workspaces return absence (`NOT_FOUND`), not `FEATURE_DISABLED`, `NOT_IMPLEMENTED`, or `UNIMPLEMENTED`. The probe correctly refuses to treat absence as "feature off" (`docs/design/managed-state-contract-map.md`, failure-path probe 2026-11-20).

**Proposed approach.** No adapter rewrite. Either:

- Run `APX_MANAGED_STATE_UNSUPPORTED_PROFILE` against a workspace where managed memory is actually disabled, and record the code, or
- If no such workspace exists, say so on the issue and leave it open. Do not relax the test to accept `NOT_FOUND`.

**Files.** Contract-map doc only, on a real run. Code touch only if the real error type differs from the three codes the test allows; then update the test and the adapter together, with the observed code quoted.

**Depends on.** None for the wait. Shares the probe file with #855; do not edit it for both issues in one PR.

**Verify.** Probe JSON `verified=true` and one of those three error codes. `make check` only if the assertion changes.

### #864 — managed durable execution vs custom runtime

**Current state.** Native `DurableAgentServer` is the Apps host (`compile_to_durable_agent_server` in `python/src/apx_agent/_durable_agent.py`). The issue's implementation checklist from October is largely done. The live ledger in `docs/design/durable-host-consumers.md` (updated 2026-11-21) is the source of truth, and it is ahead of the issue body:

| Live item | State on 2026-11-21 |
|---|---|
| Session/invocation after worker loss | Recorded. `docs/evidence/apx-h0f-2026-11-20/`. Explicit SNAPSHOT redeploy, not automatic capacity restore. Service identity, one worker. |
| Replay-safe external effects | Recorded. `docs/evidence/apx-aub-2026-11-20/` and `apx-aub-2026-11-21/`. 2 raw attempts, 1 effective mutation. Not exactly-once. |
| Approval continuation after replacement | **Not run.** `docs/evidence/apx-ab7-2026-11-21/`. One OAuth profile on workspace `7474660401027952` (`fevm-stable`). `DurableAgentServer` still raises `MCP_USER_AUTH_RECOVERY_UNSUPPORTED` for request-user recovery. |
| Trace correlation | Tag join recorded (`apx-pzd-2026-11-21`). Inbound header continuation is not: `DurableAgentServer._invoke_request` does not call `continue_trace_from_headers`. That call exists on the compatibility host (`_invocations.py`). |
| Disconnect / reconnect / cancellation | Disconnect replay recorded (`apx-4ag-2026-11-21`). **Cancel is unsupported by the SDK:** `POST .../cancel` is 404, `DELETE` is 405. `CancellationRegistry` stays. |
| Downstream delegation with caller identity | Not run. Needs two apps and a UC audit. Coordinate #816 and #755. |

`docs/design/durable-host-consumers.md` also freezes the ChatAgent callers (`cli.py` Model Serving and eval, `_run_once.py`, `_eval.py`, `_dev.py`, `_invocations.py`, `_a2a.py`, `create_app` for `responses_agent`). Deleting `_chat_agent.py` still breaks those. ADR 0001 deprecates `responses_agent` after one release; that deprecation issue is #1134, not this one, and it has not removed the compiler.

**Proposed approach.** Do not open a deletion PR. Three separate follow-ups, each allowed to stop:

1. **Approval.** Blocked until a second principal exists on that workspace. The local regression is already described in `docs/design/durable-approval-continuation.md`. The live procedure is there. Do not weaken "wrong principal is denied."
2. **Trace headers.** Only if AgentKit grows a hook on `_invoke_request`. Wrapping the server to call `continue_trace_from_headers` beside the SDK is a second tracing setup. #862 says not to add one. File the gap against the SDK and keep the tag-join record as the evidence we have.
3. **Cancel.** Do not add an apx cancel route the SDK will 404. Document "not supported" as the contract until AgentKit ships one. The disconnect/replay record can stay the acceptance for the half that exists.
4. **Downstream OBO.** Wait for #816 / #755, or a dedicated two-app probe. No service-identity fallback.

**Files.** Evidence docs and the consumer inventory when a run happens. Production files only to fix a bug a run actually shows.

**Depends on.** #855 / #856 for store behavior, not for these proofs. Parent #862.

**External blockers.** Second Databricks user on `7474660401027952`. AgentKit request-user recovery (`MCP_USER_AUTH_RECOVERY_UNSUPPORTED`). No cancel method on `DurableAgentServer`.

**Verify.** A sanitized evidence directory with invocation ids, principals, and HTTP statuses, in the same shape as `apx-h0f` / `apx-aub`. Local `python/tests/test_durable_recovery.py` does not close a live item. `python/tests/test_durable_host_consumers.py` must be updated before any ChatAgent import is removed, and removal is not this wave.

---

## Wave 3 — blocked on the platform

### #1146 — `policy.downscope` on `[[tools]]`

**Current state.** Still blocked on agentbricks 0.10.0, not only 0.9.0. In the locked wheel, `databricks_agentkit.runtime.tool_manifest.SANDBOX_DOWNSCOPE_ENABLED = False`. A sandbox tool with a non-empty `policy.downscope` fails `AgentProject` load. Genie rejects `policy.downscope` outright. Non-sandbox tools raise "Only sandbox bindings accept policy.downscope." ADR 0001 decision 5 and `docs/design/agent-toml-source-of-truth.md` already say apx reads `agent.toml` and does not write `[[tools]]`. `ToolScope` on a platform tool is a compile error (`_tool_scope.attach_scope`). `ScopeGuard` still runs for custom Python tools.

**Proposed approach.** When a future agentbricks sets the flag to `True` and `agentbricks doctor` accepts a sandbox downscope:

- Add a `check_agent_toml` finding that an authored `policy.downscope` is present, loads, and matches the declared ceiling. Do not generate the `[[tools]]` entry.
- Keep `ToolScope` / `ScopeGuard` for Python tools only.
- The read-back test the issue asks for is: write a fixture `agent.toml` with one sandbox `policy.downscope`, load it with `AgentProject.load`, run the real `agentbricks doctor`, and assert the downscope survived. That test fails today; do not skip it, and do not land it red.

**Files (later).** `python/src/apx_agent/_agent_toml.py`, a fixture under `python/tests/`, ADR 0001's "as of 0.9.0" sentence (update the version).

**Depends on.** None in-repo. Blocked on the upstream flag.

**Verify.** The doctor read-back above, then `make check`.

### #575 — UC Agent Services as a registry, at GA

**Current state.** No `_agent_services.py`. The August 2026 gate comment is still the right decision, with one doc change to record.

Fetched 2026-11-23. Page last updated **2026-09-11**. Banner is still **Beta**. Limitations still say:

- "Runtime invocation is not available. Agents cannot be called through a registered agent service."
- "`full_name` and `owner` fields return `null` in GET responses in this release."
- "Unity Catalog Global Search does not surface agent services."
- "SQL DDL for agent services is not available."

What changed since the 2026-08-05 check: Catalog Explorer lists an agent service under its schema, and one sentence says granting `EXECUTE` lets the user invoke it. That sentence contradicts the limitations bullet. Treat the limitations list as the gate, which is what the issue itself says. Do not start the REST adapter on the strength of one sentence.

The chosen design (additive mirror on `agents advertise`, keep `main.apx.agent_registry` for the endpoint URL) is still right, because Agent Services still have no field for the URL A2A routing needs. The issue's sketch of an SDK-free REST client copied from `_memory_managed.py` is stale: that module is now the AgentKit adapter (#856). When GA arrives, use the supported SDK if it has one; do not copy the deleted REST pattern.

**Proposed approach.** No code. On the next doc change, comment on the issue with the 2026-09-11 quotes and the Catalog Explorer contradiction. Build only when all three hard gates are gone from the limitations section.

**Depends on.** Independent of #1201. The homegrown registry stays.

**Verify.** Not applicable until the gates flip. Then the issue's own module sketch, updated to the SDK that exists that day, plus a read-back of `owner` and an invocation through the service.

### #947 — `user:` / `app:` keyed state

**Current state.** Decided 2026-11-23, recorded in `docs/design/keyed-state-scopes.md`. The 2026-10-21 check still holds: managed session stores are one thread, managed memory is recall, and `ConversationStore.session_state` is one conversation. None of those is an exact `user:` / `app:` key-value store. Do not add `get_scoped_state` / `set_scoped_state`, and do not seed scopes from ChatAgent. Phase 3's host is the durable session store. `session_keys` are unprefixed keys for one session; `_agents.py` rejects `user:` / `app:` on them.

`user:` is the Agent Bricks memory pipeline (distilled about every 24h, or on a manual run) plus the memory tools for an immediate save. Reads go through `recall` / search, not a key get. Author steps are in `docs/running/sessions-and-memory.md`. Writing `state["user:…"]` does not enter memory.

`app:` is agent config set at deploy time (`agent.toml`, `[tool.apx]`), not runtime state. Agents do not write it. `{app:…}` renders only when that key is already in the in-graph state dict; nothing seeds it from config. That seed is left on [#1233](https://github.com/stuagano/apx-agent/issues/1233).

In-graph templating for `{user:…}` / `{app:…}` / `{temp:…}` exists (`_compile.py`, `_TEMPLATE_KEY`). `partition_state` drops `user:` and `app:` from conversation `set_session_state`. A tool write, an `output_key` with that prefix, and those persistence drops log a one-time warning that points at the docs.

**Proposed approach.** Close #947 on the doc, the warning, and the author guidance. Do not add a Lakebase table (ADR 0001, #862). Leave #1233 open until `app:` config is either seeded into initial state or the issue is closed on the documentation alone.

**Depends on.** Nothing further for `user:`. #1233 is independent. #855 does not unblock either.

**Verify.** The warning tests: a `user:` / `app:` key dropped by `persist_session_state` or omitted by `save_session_state` logs once and is not written back as session state. No exact cross-session key round-trip, because that store is not being built.

---

## Wave 4 — umbrella

### #862 — platform-first tracker

**Current state.** The 2026-10-06 file table and the 2026-10-07 / 2026-10-13 comments are behind `main`. Several children the comments still list as open are closed: #868 (lifecycle, via #871), #863 (App Space declaration and deploy; the live idle/wake proof failed on a `zoneinfo` error and was explicitly not rescheduled), #870, #860, #858 (memory extraction removed, #1189). TypeScript Gateway chat (#869) is moot if the TypeScript runtime stayed retired (#873). This plan's open children inside the 19 are #864, #855, #856, and #575. #947 was decided on 2026-11-23 (`user:` is memory; `app:` moved to #1233). #815, #816, and #755 are still named by #864 and are not in this set.

`_memory_managed.py`, `_durable_agent.py`, and the Lakebase modules are in the keep/replace table. Nothing in this plan authorizes deleting a KEEP or KEEP COMPATIBILITY row.

**Proposed approach.** Paste a short status comment on #862 that points here. Do not edit twenty files to "finish the umbrella." Retire a custom subsystem only when its row's tracking issue has a live record and a consumer inventory (`test_durable_host_consumers.py` for the ChatAgent row).

**Depends on.** Waves 2 and 3.

**Verify.** The comment matches the ledger. No production diff.

---

## Suggested first week of work, if implementation is approved

1. Close the Wave 0 issues with comments. No branch.
2. One PR for the #1141 tool-list note (S).
3. One PR for the #1207 contract test and the `--no-warmup` paragraph (S). Schedule the live re-run separately; do not block the test PR on it.
4. One PR for #981, starting with a single example so the lockfile fix is proven before the rest are relocked (M).
5. Leave #1052 and the #911 `ASK` mapping as the next two independent PRs. Do not start #864, #1146, or #575. #947 is the memory-pipeline decision, not a store.
