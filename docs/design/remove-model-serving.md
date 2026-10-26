# Remove the Model Serving deploy target

Sub-project 4 of [`agent-toml-source-of-truth.md`](agent-toml-source-of-truth.md).

**Status:** shipped — PR A (#1003) CLI target, PR B (#1004) hosting code, PR C (#1005) docs.

## Decision

apx-agent deploys agents to **Databricks Apps only**. The Mosaic AI Model Serving
deploy target (`agents deploy --target model-serving` and everything only it
uses) is removed.

The other half of the original sub-project 4, "the YAML spec path (#152)", is
already done: #152 is closed, and `apx-agent agents run|deploy <spec>.yaml`
materializes a spec into a project. Nothing remains to build there.

## Scope

**Removed: hosting an agent on Model Serving.** That means:
- the serving deploy branch, its dry-run plan and its health gate;
- the model-serving scaffold;
- `log_agent` and `mlflow_resources_for`;
- the serving halves of hot-swap and canary;
- serving-endpoint cost, logs and delete;
- the `endpoints` command group.

**Kept, though the code mentions serving:**
- **The `responses_agent` runtime** (`compile_to_responses_agent`, `ApxResponsesAgent`). The `samples-*` Apps run on it.
- **Apps UC registration** (`_apps_registry.register_apps_manifest`, `set_uc_tags_for_agent`, `_resolve_project_uc_name`).
- **Model invocation.** The agent's LLM is an AI Gateway binding: `model=`, calls to `/serving-endpoints/{model}/invocations`, embeddings endpoints, and Vector Search.
- **`_external_model.py`.** It provisions an external-model endpoint as an LLM proxy (e.g. Bedrock behind AI Gateway). That's an LLM provider path, not agent hosting.
- **`eval --endpoint-url` / `endpoint_predict_fn`.** They target an App's `/invocations`.
- **The Apps hot-swap and canary** (`_hot_swap_apps.py`, `_canary_apps.py`).
- **`APX_AGENT_MODEL_OVERRIDE`.** The runtime model override is read by the responses and chat runtimes, and Apps hot-swap uses it.
- **`supervisor add --app`**, and `advertise` / `publish` with `--endpoint-type apps`.

## Delivery: three PRs, in order

Each PR merges before the next is cut from `main`. They are never stacked.

### PR A: the user-facing cut

| Surface | Change |
|---|---|
| `agents deploy --target model-serving` | Fails before any side effect: `Model Serving was removed: apx deploys agents to Databricks Apps. Run apx-agent agents deploy --target apps.` `model-serving` stays an accepted value for this release only, so the message can be specific. `--target apps` and auto-detection are unchanged. |
| Serving-only `deploy` flags | Removed: `--no-deploy`, `--publish-tools/--no-publish-tools`, `--set-uc-tags/--no-set-uc-tags`, `--agent-name`, `--capture-env-vars`, `--allow-env-var`, `--scale-to-zero/--no-scale-to-zero`, `--workload-size`, `--env-suffix`, `--version`, and the unused `--hot-swap`. Remove a flag only after checking the Apps branch never reads it. `--name`, `--model` and `--experiment` get the same check and stay if the Apps branch uses them. |
| `_detect_target` | Its model-serving rule goes; every project resolves to Apps. |
| `agents scaffold --target model-serving` | The same error, naming `scaffold --target apps`. The interactive prompt stops offering it. |
| `agents hot-swap`, `canary status/deploy/promote/rollback/analyze` | Default `--target` changes from `model-serving` to `apps`. `--target model-serving` gives the same error. Serving-only options (`--endpoint`, `--version`, `--workload-size`, `--no-scale-to-zero`) are removed. |
| `agents cost` | Removed. It only reads serving-endpoint billing. |
| `agents logs --endpoint/--served-model/--build` | Removed; `--app` stays. |
| `agents delete --endpoint` | Removed; `--app` and `--purge` stay. |
| `endpoints list`, `endpoints url` | The group is removed. |
| Doctor and fleet text | Hints that say `deploy --target model-serving` now point at Apps. |

PR A deletes no library code; PR B does that. The serving branches become
unreachable, and the CLI no longer offers them.

### PR B: dead code

- **`log_agent`** (`_chat_agent.py`) and **`mlflow_resources_for`** (`_resources.py`): deleted.
- **The serving deploy branch in `cli.py`:** deleted. That's the serving half of `agents deploy`, `_emit_serving_deploy_plan`, `_serving_health_gate` and `_MODEL_SERVING_ONLY_DEPLOY_FLAGS`.
- **`_scaffold_model_serving`** and its templates (`_SCAFFOLD_PYPROJECT/_AGENT/_APP/_GITIGNORE/_README`): deleted.
- **`_canary.py`:** deleted. First move `_percentile` (used by `_dev.py` and `_eval_sweep.py`) to a shared module.
- **`_hot_swap.py`:** `hot_swap_model`, `HotSwapResult` and `get_active_override` are deleted. The `APX_AGENT_MODEL_OVERRIDE` runtime read stays where it lives today.
- **`_cost.py`:** `cost_for_endpoint` and `cost_for_agent` are deleted. `usd_for_token_usage` stays (budget guard, dev UI).
- **`cli.py` serving helpers:** `_hot_swap_model_serving`, `_resolve_served_model_name`, the canary serving branches and the endpoint teardown are deleted.
- **`__init__.py`:** the serving exports are removed from the public API. The changelog lists them.
- **`AgentConfig.host`:** the `"model_serving"` literal is removed (`"apps"` only). A config declaring it gets a clear validation error.
- **Stale docstrings in shared code:**
  - `RuntimeTarget` and `compile_to_responses_agent` describe `responses_agent` as the Apps runtime, not "the legacy model-serving contract";
  - `_apps_registry`, `_readyz` and `_obo` drop their Model Serving wording.

### PR C: docs and examples

- **`docs/deploy/apps-vs-model-serving.md`:** deleted, along with every link to it (`README.md`, `docs/README.md`, the deploy overview).
- **Deploy guides** (`docs/deploy/overview.md`, `troubleshooting.md`, `agentbricks-platform.md`): Model Serving sections and steps are removed.
- **`README.md`, `python/README.md`, `docs/positioning.md`:** Apps only.
- **`docs/get-started/{cli,quickstart,migration,dev-ui}.md`:** removed commands and flags are taken out. `migration.md` gets a "Model Serving removed" note naming `--target apps`.
- **Docs that tie `responses_agent` to Model Serving:** they describe it as an Apps runtime. LLM and embedding invocation through AI Gateway stays.
- **Example READMEs** (`shortage-intelligence-agent`, `customer_triage`, `data-triage-agent`, `memory_demo`): "Model Serving" options are removed, and their deploy steps use `--target apps`.
- **`samples-*/agent.py` docstrings:** the `--target model-serving` mention is dropped.
- **Not edited:**
  - design history (`docs/design/`, `docs/superpowers/`, `docs/engine-scope/`);
  - vendored Databricks skill docs (`python/examples/databricks-skills/`);
  - `CHANGELOG.md` history.

## Testing

- **PR A:** a test file pins the error for `deploy`, `scaffold`, `hot-swap` and each canary command, and pins the Apps default for `hot-swap` and canary. Serving tests that exercised these commands are deleted, each listed by name in the plan's ledger.
- **PR B:**
  - A guard test asserts that no public export and no CLI command references a Model Serving deploy.
  - `test_in_repo_projects.py` shows that every in-repo app still serves what it does today.
  - Serving-only tests are deleted: `test_canary.py`, the serving half of `test_hot_swap.py`, `test_cost.py`'s endpoint tests, `test_log_agent_experiment.py`, and the serving cases in the mixed files. Each is ledgered. The `APX_AGENT_MODEL_OVERRIDE` runtime tests and every Apps test stay.
- **PR C:**
  - A guard test lets user docs and READMEs mention Model Serving *hosting* only to say it was removed. It matches hosting phrases (`--target model-serving`, "deploy to Model Serving"), not "serving endpoint", which also names LLM endpoints that stay.
  - Every rewritten deploy example is checked against the Apps CLI's real flags.
- **Every PR:**
  - `make check`;
  - pre-commit;
  - pyright;
  - the PR CI subset under xdist;
  - a full suite run without Databricks credentials (`HOME=/tmp/nohome`, no `DATABRICKS_*`), because CI has none.

## Out of scope

- Renaming `endpoint_predict_fn` / `--endpoint-url`, which do target Apps.
- An Apps equivalent of `agents cost`. Nothing in the repo shows how Apps billing is attributed.
- Removing `model-serving` as an accepted `--target` value. That happens in a later release, once the explicit error has had its say.
