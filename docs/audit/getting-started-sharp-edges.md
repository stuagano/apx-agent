# Getting-started sharp edges

## Executive summary

The apx-agent getting-started experience has two distinct failure modes. The first is **documentation drift**: code behaves correctly but the docs describe a different, often broken invocation pattern (missing flags, wrong commands, absent prerequisites, capabilities that only exist in local dev). The second is **silent degradation**: the system starts cleanly and logs nothing alarming, but a core capability — memory, session persistence, schema grounding, identity passthrough — silently does nothing. The silent failures are the more dangerous class: they pass `apx doctor`, pass `/readyz`, and surface only as confusingly inert agent behavior. Both classes share a root cause: the framework evolved rapidly and documentation / surface-level health checks did not keep pace.

---

## High

### `memory="persistent"` silently does nothing — `table_name` required but never mentioned

**What happens.** `normalize_memory_knob("persistent")` produces a delta config with `table_name=None`; `_build_memory_store` raises `ValueError`, which is swallowed into a `logger.warning`; the agent starts normally with no memory tools attached, and `/readyz` reports `memory: "ok"` because the `_apx_memory_degraded` sentinel is only set when `ws is None`.

**File.** `python/src/apx_agent/_memory_wiring.py`

**Fix.** In `normalize_memory_knob`, auto-derive a default `table_name` (e.g. `main.default.apx_memories`) so the one-liner actually works; or, at minimum, re-raise the `ValueError` at agent construction time instead of swallowing it, and set `_apx_memory_degraded` whenever `store is None` regardless of `ws`.

---

### `/readyz` reports `memory: ok` when memory build fails with `ws` present

**What happens.** `attach_declared_memory` sets `_apx_memory_degraded` only when `store is None and ws is None`; on a real deployed app `ws` is not `None`, so a failed build (e.g. missing `table_name`) leaves the sentinel unset and the deploy gate reports green while memory is absent.

**File.** `python/src/apx_agent/_memory_wiring.py`

**Fix.** Set `_apx_memory_degraded` whenever `store is None` after a build attempt, regardless of `ws`.

---

### Scaffold comment leads user into silent failure with no corrective guidance

**What happens.** The scaffolded `agent.py` comment says "uncomment `memory=\"persistent\"` to add memory"; doing so triggers the same `table_name`-missing silent failure described above, and `/readyz` still reports green.

**File.** `python/src/apx_agent/cli.py`

**Fix.** Fix the underlying `normalize_memory_knob` default (see above), or change the scaffold comment to `memory="persistent"  # also set table_name='catalog.schema.apx_memories'`.

---

### Scaffolded `start_server.py` passes no `session_store` — sessions never persist in deployed Apps

**What happens.** `compile_to_responses_agent(agent, model=MODEL)` in the scaffold template omits `session_store`; `resolve_session_store` (which reads `agent.session_config`) is only called in the `apx serve` local path, never from the template — so `CoworkerAgent(memory="persistent")` persists sessions locally but silently drops all history in production.

**File.** `python/src/apx_agent/cli.py`

**Fix.** Inside `compile_to_responses_agent`, fall back to reading `agent.session_config` when no explicit `session_store` is passed (preferred — fixes all existing deploys on upgrade).

---

### `ws=WorkspaceClient()` in docs causes 502 crash in deployed Apps

**What happens.** `getting-started.md` and `data-agent.md` both show `ws=WorkspaceClient()` as the pattern to use for live schema introspection; in a deployed App, the default credential chain produces both M2M and PAT tokens, the SDK picks both, and the import-time conflict crashes the process with a 502 that has no user-visible hint.

**File.** `docs/getting-started.md`

**Fix.** Export a safe `workspace_client()` factory from `apx_agent.__init__` (wrapping the private `_make_workspace_client`) and replace every `WorkspaceClient()` example in user-facing docs with it.

---

### Expired token passes preflight; agent misattributes auth failure as a data problem

**What happens.** `_preflight_databricks_auth()` runs only an offline config check — no network call — so an expired PAT passes; `sql_tools._run_sql` swallows the resulting 401 into a `{"error": ..., "rows": []}` tool result; the LLM instructions tell the agent to "try a broader filter or verify the column name" on any error, so the agent suggests schema changes instead of "your credentials are expired."

**File.** `python/src/apx_agent/cli.py`

**Fix.** Add a live workspace round-trip to `_preflight_databricks_auth()`; add auth-error detection in `_run_sql`; add an explicit auth-error clause to `build_instructions_from_schema` so the LLM surfaces credential issues rather than deflecting.

---

## Medium

### Interactive scaffold wizard fires without warning — doc never mentions it

**What happens.** `getting-started.md` implies `apx scaffold my-agent` runs silently and "creates my-agent/ in the current directory"; a new user in a TTY is surprised by numbered menus and free-text prompts with no preparation.

**File.** `docs/getting-started.md`

**Fix.** Add a sentence noting that scaffold launches a short setup wizard; document `--no-interactive --catalog samples --schema nyctaxi` for CI/scripted use.

---

### "Already grounded against samples.nyctaxi" claim is conditionally false when auth is absent at scaffold time

**What happens.** When workspace auth fails during scaffold, `.apx/schema.json` is not written; `DataAgent` silently falls back to ungrounded instructions ("call SQL to discover tables at the start of every session") — the opposite of what line 102 of the docs promises.

**File.** `docs/getting-started.md`

**Fix.** Qualify the claim: explain that grounding is baked only if auth was available at scaffold time, and tell users to `ls .apx/schema.json` to confirm.

---

### Setup wizard writes `.env` but banner check reads `os.environ` — banner persists after save

**What happens.** After completing the setup wizard and saving, the "First time here? Open Setup" banner reappears on every page reload because it checks `os.environ` (never updated) rather than reading the `.env` file from disk.

**File.** `python/src/apx_agent/_ui_chat.py`

**Fix.** Change the banner check to read `.env` from disk using the `_read_env_file` helper already present in `_ui_setup.py`.

---

### Setup wizard updates instructions but leaves `DataAgent` constructor catalog/schema unchanged

**What happens.** `_persist_instructions()` rewrites only the `instructions=` kwarg in `agent.py`; the `DataAgent("samples", "nyctaxi")` positional args are left untouched, so the baked schema is silently skipped on the next start (equality check fails) while the instructions reference a different catalog.

**File.** `python/src/apx_agent/_dev.py`

**Fix.** After `_persist_instructions()`, also rewrite the two positional catalog/schema string literals in the `DataAgent(...)` call using the existing AST machinery; or append a clear note to the `save_setup` response telling users which constructor args to update manually.

---

### Model-serving deploy example missing required `--model` flag — immediate `UsageError`

**What happens.** `getting-started.md` line 125 (and three other locations) show `apx deploy --target model-serving --name <...>` without `--model`; the CLI raises `UsageError` immediately.

**Files.** `docs/getting-started.md`, `README.md`, `docs/apps-vs-model-serving.md`

**Fix.** Add `--model <endpoint-name>` to every model-serving example; canonical form: `apx deploy --target model-serving --model databricks-claude-sonnet-4-6 --name <catalog.schema.model>`.

---

### All subprocess output suppressed during 2-5 minute deploy — user sees nothing

**What happens.** `_run_databricks_cmd` uses `capture_output=True` for all three blocking calls (bundle validate, deploy, run); the Databricks CLI's progress bars are completely suppressed; a user cannot distinguish "working normally" from "hung."

**File.** `python/src/apx_agent/cli.py`

**Fix.** Print an expected-duration note before each long step; for a fuller fix, stream subprocess stdout as lines arrive.

---

### `readyz` failure dumps raw Python dict instead of actionable diagnosis

**What happens.** Line 3404 of `cli.py` interpolates the `checks` dict directly into the error string, producing output like `{'llm': 'fail', 'tracing': 'unavailable', ...}` with no guidance on which env var is misconfigured or how to get logs.

**File.** `python/src/apx_agent/cli.py`

**Fix.** Parse known check keys and emit one line per failing check with a concrete next step (env var to verify, log command to run); include `--no-readyz-gate` as the escape hatch.

---

### Silent fallback when `introspect_schema` fails with `ws=` but no `warehouse_id=`

**What happens.** When a user passes `ws=` without `warehouse_id=`, the SDK call fails, the bare `except Exception: return {}` swallows the error silently, and the agent falls back to ungrounded mode with no log message in the developer's terminal.

**File.** `python/src/apx_agent/_schema.py`

**Fix.** Add a `logger.warning` in the `except` block noting that schema introspection returned no tables and the agent is falling back to ungrounded mode.

---

### Lakebase config example omits `host`; engine silently falls back to `localhost`

**What happens.** The documented TOML example for `type=lakebase` omits `host`; `build_lakebase_engine` silently connects to `localhost:5432` with no warning log; the failure surface as a generic Postgres connection-refused error at first query.

**File.** `python/src/apx_agent/_lakebase_engine.py`

**Fix.** Add a `logger.warning` when `host is None` and `instance_name` is provided; add `host = "$LAKEBASE_HOST"` to the docs TOML example.

---

### After-deploy section describes `/_apx/*` routes that don't exist on deployed apps

**What happens.** Step 1 of the "After deploy" section says the deployed URL redirects to `/_apx/agent`; the deployed Apps endpoint (AgentServer) mounts `/invocations`, `/health`, `/readyz`, `/mcp` — not the dev UI routes — so users hit a 404.

**File.** `docs/getting-started.md`

**Fix.** Rewrite the "After deploy" verification steps to use `/readyz`, the workspace Apps UI, and MLflow Experiments — and add a note that `/_apx/*` is local-dev-only.

---

### No guidance on finding the serving endpoint URL or name after model-serving deploy

**What happens.** After a successful `--target model-serving` deploy, the CLI prints only the UC registered model name and version number — no endpoint name, no invocations URL, no navigation hint.

**File.** `python/src/apx_agent/cli.py`

**Fix.** Capture the `agents.deploy()` return value and print the endpoint name and full invocations URL; add a sentence to the docs noting the endpoint name derives from the last segment of the `--name` UC path.

---

### Identity (OBO) passthrough difference for model-serving is undocumented — silently changes security behavior

**What happens.** Model-serving callers who omit `custom_inputs={"user_token": ...}` cause all SQL queries to run as the endpoint service principal; no error is raised, UC row-level grants silently evaluate against the SP, and `getting-started.md` gives no hint this differs from Apps behavior.

**File.** `docs/getting-started.md`

**Fix.** Add a callout to the model-serving deploy step explaining that OBO requires explicit `user_token` in `custom_inputs`; link to `apps-vs-model-serving.md#identity`.

---

## Low

### Bare `apx doctor` before venv activation — inconsistent with rest of doc

**What happens.** The "Verify setup" and Troubleshooting sections use bare `apx doctor` while every other command in the same doc uses `uv run apx ...`; a user without an activated venv gets "command not found."

**File.** `docs/getting-started.md`

**Fix.** Change both bare `apx doctor` blocks to `uv run apx doctor`.

---

### Stopped warehouse troubleshooting describes wrong symptom

**What happens.** The doc says "if a SQL query returns nothing, the warehouse is probably stopped"; the actual failure surfaces as an explicit error message from the agent, not empty results — so users who see an error don't match it to this hint.

**File.** `docs/getting-started.md`

**Fix.** Change to: "If the agent reports a SQL error, the warehouse may be stopped — check `/_apx/probe/checks`. Empty results with no error mean the query ran but found no matching rows."

---

### Duplicate `uv add apx-agent` in scaffold block implies two installs are needed

**What happens.** Line 61 repeats `uv add apx-agent` (already done in Prerequisites) with a comment "install apx into your current env," confusing readers about whether two separate envs need managing.

**File.** `docs/getting-started.md`

**Fix.** Remove line 61; optionally add a comment on the `cd my-agent && uv sync` line noting the new agent gets its own isolated venv.

---

### `apx refresh-schema` has no `--catalog`/`--schema` escape hatch after manual agent edit

**What happens.** A user who edits `agent.py` to point at a new catalog/schema has no way to re-bake the schema without re-scaffolding; `apx refresh-schema` silently re-introspects the original catalog/schema from the manifest.

**File.** `python/src/apx_agent/cli.py`

**Fix.** Add `--catalog` and `--schema` flags to `apx refresh-schema` to override the manifest values.

---

### `apx doctor` has no SQL warehouse check

**What happens.** `apx doctor` returns all-green when no SQL warehouse exists; the failure surfaces only on the first chat query as a `RuntimeError: No SQL warehouse available` in the agent response.

**File.** `python/src/apx_agent/_doctor.py`

**Fix.** Add a WARN (not FAIL) check that calls `ws.warehouses.list()` when `online=True` and auth is OK, gated to DataAgent/CoworkerAgent projects.

---

### `readyz` gate skips silently when `app_url` is empty

**What happens.** When `_poll_app_ready` returns a running app with no URL, the `readyz_gate` block is bypassed with zero log output — no "skipped," no warning.

**File.** `python/src/apx_agent/cli.py`

**Fix.** Add one `log()` line in the empty-URL branch: "WARNING: readyz gate requested but no app URL resolved — skipping."

---

### `bundle run` non-zero exit is logged as "(continuing)" — users dismiss a real failure signal

**What happens.** When `bundle run` fails for a real reason, the error is tagged "(continuing)" in the log — users are trained to ignore it — and the poll timeout 5 minutes later gives no pointer back to the earlier stderr.

**File.** `python/src/apx_agent/cli.py`

**Fix.** Re-surface the `bundle run` stderr in the timeout error message so root cause and failure report are co-located.

---

### Lakebase error message omits `instance_name` (required) but lists `host` (optional)

**What happens.** The `ValueError` in `normalize_memory_knob` lists `(host, database, embedding_model, embedding_dim)` — omitting the actually-required `instance_name` — sending users to add the wrong field first.

**File.** `python/src/apx_agent/_models.py`

**Fix.** Replace `host` with `instance_name` in the error message and note that `host` is optional.

---

### Workspace client `None` propagation in custom tools

**What happens.** `_get_workspace_client()` returns `None` when `app.state.workspace_client` is unset; a custom tool using `Dependencies.Client` gets `AttributeError: 'NoneType'...` rather than a clean 503.

**File.** `python/src/apx_agent/_defaults.py`

**Fix.** Add a null guard: raise `HTTPException(503, "Databricks workspace client not available — check credentials and restart")` when the client is `None`.

---

### OBO fallback in per-tool HTTP routes not surfaced at WARNING level

**What happens.** When an OBO token is absent on direct tool routes, the fallback to SP identity is logged at INFO — easy to miss in a noisy startup log — and the effective identity is not printed.

**File.** `python/src/apx_agent/_defaults.py`

**Fix.** Upgrade to WARNING and include the effective auth path in the message.

---

### `apx doctor` does not check model serving endpoint reachability

**What happens.** `apx doctor` returns all-green when the configured LLM endpoint doesn't exist; the failure only surfaces on the first chat message.

**File.** `python/src/apx_agent/_doctor.py`

**Fix.** Add an optional `ws.serving_endpoints.get(model_name)` existence check gated on `online=True`, emitting WARN with a fix hint if the endpoint is missing.

---

### No timing expectation set for first deploy

**What happens.** No duration estimate appears anywhere in `getting-started.md`; a user watching `# databricks bundle deploy` go silent for 3 minutes has no frame of reference.

**File.** `docs/getting-started.md`

**Fix.** Add one sentence after the deploy code block: "First deploy typically takes 3-5 minutes; subsequent deploys ~60-90 s with warm compute."

---

### `databricks-agents` package prerequisite is undocumented

**What happens.** The model-serving deploy path raises a `ClickException` ("install with: pip install databricks-agents") that is clear but unexpected; the package is not mentioned in Prerequisites.

**File.** `docs/getting-started.md`

**Fix.** Add `uv add databricks-agents` note to the model-serving parenthetical; consider a `model-serving` optional extra in `pyproject.toml`.

---

## Patterns

**1. Silent degradation over loud failure.** The framework consistently catches exceptions at integration boundaries (`_build_memory_store`, `introspect_schema`, `_run_sql`, `bundle run`) and downgrades them to `logger.warning`. This is defensively correct — the agent stays up — but it removes the user's ability to distinguish "configured correctly and idle" from "misconfigured and doing nothing." Every swallowed exception needs either a visible diagnostic or a structured sentinel that surfaces at `/readyz`.

**2. Health checks that don't check health.** `apx doctor` has no live model probe, no warehouse check, and no network round-trip on `apx run`. `/readyz` has a sentinel bug that causes memory failures to report `ok`. The result is that a user can get all-green from every health signal while core capabilities are absent. Health checks need to be end-to-end, not structural.

**3. Local-dev assumptions baked into tutorial artifacts.** The "After deploy" section assumes `/_apx/*` routes, the `ws=WorkspaceClient()` pattern, and implicit `.env` loading — all of which work in `apx run` but break in production Apps. The tutorial was written from the local-dev perspective and the production delta was never fully documented.

**4. Documentation written for the happy path, not the auth-absent path.** Claims like "already grounded against samples.nyctaxi," "add `memory='persistent'` when you want facts to survive restarts," and the setup wizard's implicit auth assumption are only true when workspace credentials are present at exactly the right moment. The docs describe the success case; the conditional requirements are unstated.

**5. Multi-step features with single-step documentation.** Model-serving deploy requires two flags, a separate package install, explicit OBO wiring, and produces no endpoint URL — but is documented as a one-liner parenthetical. Memory configuration requires a valid `table_name`, a matching `session_store` in the scaffold template, and a working `ws` — but is documented as a single kwarg. Anywhere the feature requires multiple coordinated pieces, the docs mention only one.
