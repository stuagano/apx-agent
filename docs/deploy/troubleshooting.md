# Deployment troubleshooting — `apx deploy` failure modes and how to read them

Real-world failure modes observed when running `apx deploy` against Databricks workspaces, in the order you're most likely to hit them. Every diagnosis here came from an actual deploy that broke, not from speculation — if a section reads thin it's because that mode hasn't been seen yet in practice.

Sibling docs:

- [`docs/running/lakebase-recipe.md`](../running/lakebase-recipe.md) — durable state side (sessions, memory, examples on Postgres).
- [`typescript/README.md`](../../typescript/README.md) — JS/TS surface; the deploy story is Python-only today.

## 1. Pre-flight checklist

Run these in order before `apx deploy`. Five minutes here saves the 20–40 minute round-trip on a failed deployment.

```bash
# 1. apx lint — static checks against your agent definition.
#    Catches missing instructions, unknown model endpoints, env-var refs
#    that aren't set, tools that reference UC objects that don't exist.
apx lint --module agent:agent

# 2. apx test — run a real prompt against the agent locally.
#    If this fails locally, deploy will fail too.
apx test --module agent:agent --prompt "ping"

# 3. apx info — show the resource list MLflow will record on the model.
#    Compare against what UC / Genie / Vector Search actually has.
apx info --module agent:agent

# 4. Confirm workspace profile works.
databricks --profile prod current-user me

# 5. Confirm target catalog + schema exist (and you can write them).
databricks --profile prod schemas get main.agents
```

Then, the env-var hygiene step that bit us today:

```bash
# 6. Stop MLflow from baking your dev-shell secrets into the model image.
#    Without this, anything that looks like a token in your environment
#    (ATLASSIAN_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, ...) gets
#    captured into the model's loader env and travels with the artifact.
export MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING=false
```

The serving endpoint will pick up secrets through Databricks Secrets / serving env vars at runtime — it does not need them baked into the model image, and you do not want them there.

## 2. Common deploy failures

### `RESOURCE_DOES_NOT_EXIST: Could not find experiment with ID None`

**Symptom.** `apx deploy` crashes inside `mlflow.start_run()` before any model logging happens. Stack trace points at `mlflow.tracking.client._get_experiment` returning a `None`-id experiment.

**Diagnosis.** `MLFLOW_TRACKING_URI=databricks` (the default in a workspace shell) requires an active experiment context. The outer `mlflow.start_run()` in the CLI runs *before* `log_agent`'s internal `mlflow.set_experiment(...)` call — so the outer run has nowhere to land.

**Fix.** Already patched in `apx deploy` as of commit `41e82eee` (CLI now calls `mlflow.set_experiment(effective_experiment)` before the outer `start_run`).

Still possible if **you** pass `--experiment` pointing at a path that doesn't exist:

```bash
# Pre-create the experiment once; mlflow does NOT auto-create folder paths.
mlflow experiments create --experiment-name /Users/you@databricks.com/apx-smoke
```

Or set the default in `pyproject.toml` so the CLI picks it up:

```toml
[tool.apx.agent]
experiment = "/Users/you@databricks.com/apx-smoke"
```

### Deployment stuck in `DEPLOYMENT_CREATING` / "Container creation pending" for >30 min

**Symptom.** `databricks.agents.deploy(...)` returns immediately, the serving endpoint shows up in the UI, status sits at `DEPLOYMENT_CREATING` for 30+ minutes.

**Diagnosis.** Either (a) the workspace's serving build queue is backed up, or (b) your dependency closure is large enough (>140 packages, or any heavy native build like `psycopg[binary]` + `torch`) that the image build is genuinely slow.

**Fix.**

```bash
# Inspect what's happening on the build side.
databricks --profile prod serving-endpoints get <endpoint-name> \
  | python3 -c "import sys, json; d=json.load(sys.stdin); print(json.dumps(d.get('state'), indent=2))"

# If it's been >45 min with no movement, delete and retry — usually faster
# than waiting for the queue to clear.
databricks --profile prod serving-endpoints delete <endpoint-name>
```

If the deploy succeeds on retry against an empty queue but consistently fails when re-run, the agent has too much in its closure. Split it:

- Move heavy dependencies behind a runtime import (so they only load when the tool fires).
- Move that tool out into a `uc_function_tool(...)` so it runs in UC, not in the serving container.

### `ModuleNotFoundError: No module named 'databricks.agents'`

**Symptom.** `apx deploy` raises a `ClickException` saying "databricks-agents is required for deployment".

**Diagnosis.** `databricks-agents` is not in `apx-agent`'s base dependencies — it's a Databricks-only package and pulls in a large transitive closure. The CLI imports it lazily so dev installs stay light.

**Fix.**

```bash
# Either install ad-hoc:
pip install databricks-agents

# Or pin it in your project's pyproject.toml as a [deploy] extra:
# [project.optional-dependencies]
# deploy = ["databricks-agents>=0.20.0"]
pip install '.[deploy]'
```

### Env vars accidentally captured into the model image

**Symptom.** `mlflow models predict ...` against the logged model leaks environment variables in its output, or worse, the deployed endpoint silently uses a secret value from your *dev shell* instead of from the workspace's secret scope.

**Diagnosis.** MLflow's default behavior is to record env vars referenced by the model's loader code into the model image's metadata so the runtime can re-inject them. That's helpful for `MLFLOW_S3_ENDPOINT_URL`-shaped config and harmful for API keys.

**Fix.**

```bash
# Before running apx deploy:
export MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING=false
```

Persist this in your shell rc, the project's `.envrc`, or the `[env]` block of whatever task runner you use. Inject runtime secrets via `databricks secrets` + serving endpoint env vars instead.

### `PERMISSION_DENIED` on UC model register

**Symptom.** `log_agent` runs to MLflow log step OK, then fails on the register-to-UC step with a UC-side `PERMISSION_DENIED`.

**Diagnosis.** You need both `USE SCHEMA` on the schema **and** `CREATE MODEL` on the schema. `CREATE FUNCTION` is enough for `publish_tools_to_uc` but not for `log_agent`.

**Fix.**

```sql
-- Run as a metastore admin or the schema owner:
GRANT USE SCHEMA ON SCHEMA main.agents TO `your.user@databricks.com`;
GRANT CREATE MODEL ON SCHEMA main.agents TO `your.user@databricks.com`;
```

Verify:

```bash
databricks --profile prod schemas get-permissions main.agents \
  | grep -E "USE_SCHEMA|CREATE_MODEL"
```

### LLM endpoint not accessible / model name resolves wrong

**Symptom.** Deploy succeeds, first `predict` against the endpoint returns `RESOURCE_DOES_NOT_EXIST` or `ENDPOINT_NOT_FOUND` from inside the agent.

**Diagnosis.** The `--model` value passed to `apx deploy` is a **serving endpoint name**, not a UC path or a foundation-model alias. `databricks-claude-sonnet-4-6` is an endpoint name. `system.ai.claude-sonnet-4-6` is not — that's a UC function reference and won't work here.

**Fix.**

```bash
# Confirm the endpoint exists in this workspace before deploying.
databricks --profile prod serving-endpoints get databricks-claude-sonnet-4-6 \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['state']['ready'])"

# If it returns READY, use that exact string for --model.
apx deploy --module agent:agent \
  --model databricks-claude-sonnet-4-6 \
  --name main.agents.my_agent
```

If you need to swap the model on a deployed agent without re-logging, use `apx hot-swap-model` — see `python/src/apx_agent/cli.py` for the command.

### Apps mode vs Mosaic AI mode — which one am I deploying?

`apx deploy` chains `databricks.agents.deploy(...)` and produces a **Mosaic AI Agents serving endpoint**. That's the right path when the agent is going to be:

- Called via `/invocations` from another service.
- Evaluated by `mlflow.evaluate` against a UC eval table.
- Tagged + tracked in `apx list` / topology / watchdog.

It is **not** the path when the agent is the backend of a Databricks App (FastAPI on Apps compute). For that path you don't run `apx deploy` at all — you run the agent inside a FastAPI app and deploy *that* with `databricks apps deploy`. The two surfaces are independent: the same agent can be served via both, but you only run `apx deploy` for the Mosaic AI side.

## 3. Build queue and provisioning

`DEPLOYMENT_CREATING` covers a stack of stages: image build, container provisioning, model load, health-check pass. Expected timing:

| Window | What it usually means |
|---|---|
| 0–5 min | Healthy. Image cached or trivially small. |
| 5–15 min | Healthy. Standard image build with a moderate dep closure. |
| 15–30 min | Watch it. Large dep closure (psycopg, torch, langchain stack), or queue contention. |
| 30–45 min | Probably stuck. Re-check the workspace's serving capacity. |
| >45 min | Almost always stuck. Delete + retry. |

Check workspace capacity if multiple deploys are slow:

```bash
databricks --profile prod serving-endpoints list \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
states = {}
for ep in data.get('endpoints', []):
    s = ep.get('state', {}).get('ready', 'UNKNOWN')
    states[s] = states.get(s, 0) + 1
print(states)
"
```

If you see a lot of endpoints in `NOT_READY` or `CREATING`, the workspace has capacity pressure. File a ticket with the workspace admin rather than retrying.

## 4. Trace and observability gotchas

### MLflow autolog must be enabled before the SDK client is created

If you build a custom serving wrapper (the builder-app pattern), this ordering matters:

```python
# CORRECT — autolog first, then the client picks it up.
import mlflow
mlflow.anthropic.autolog()         # or mlflow.openai.autolog() etc.

from apx_agent import ClaudeSDKClient
client = ClaudeSDKClient(...)

# WRONG — autolog won't patch the already-instantiated client.
from apx_agent import ClaudeSDKClient
client = ClaudeSDKClient(...)
import mlflow; mlflow.anthropic.autolog()
```

The autolog hook patches the module at import time; an instance constructed before the patch won't carry the hook through.

### `apx.*` span attributes need explicit `set_audit_attrs` calls

The framework exposes `apx.tool.name`, `apx.principal.id`, `apx.cost.usd`, etc., as MLflow span attributes — but only when the tool calls `set_audit_attrs(...)`. There is no automatic `apx.tool.name = <function>.__name__` capture. If your traces are missing these fields, audit the tool body:

```python
from apx_agent import set_audit_attrs

@tool(uc="main.tools.lookup_customer")
def lookup_customer(customer_id: str) -> dict:
    set_audit_attrs(tool_name="lookup_customer", principal_id="auto")
    # ... rest of the tool
```

### `apx export-traces` needs a reachable MLflow experiment

`apx export-traces --experiment <path>` queries MLflow's tracking server. If you're running it from a shell that has `MLFLOW_TRACKING_URI=file://...` set (a stale local override), it'll silently target your local store and find no traces.

```bash
# Force the workspace tracking URI for one command:
MLFLOW_TRACKING_URI=databricks apx export-traces --experiment /Users/you@databricks.com/apx-smoke
```

## 5. Cleaning up after a failed deploy

```bash
# 1. Tear down the serving endpoint. Cheap and the standard first step.
databricks --profile prod serving-endpoints delete <endpoint-name>

# 2. Optional — delete the UC model VERSION that failed to deploy.
#    Default is to KEEP it; version history is useful for forensic comparison.
#    Only delete if the version is corrupt or contains accidentally captured secrets.
databricks --profile prod registered-models delete-version main.agents.my_agent --version N

# 3. The MLflow run can stay. It documents the attempt, costs nothing, and
#    `apx eval --run-id` later can compare a healthy run against the failed
#    one to find what changed.
```

Do **not** delete the registered model (`main.agents.my_agent` without `--version`) on a failed deploy — that wipes every prior version too. Almost never the right move.

## 6. Cross-references

- Lakebase / durable state: [`docs/running/lakebase-recipe.md`](../running/lakebase-recipe.md)
- TypeScript surface: [`typescript/README.md`](../../typescript/README.md)
- CLI source: [`python/src/apx_agent/cli.py`](../../python/src/apx_agent/cli.py)
- The `apx deploy` flow (publish-tools → log_agent → agents.deploy → set_uc_tags): commit `7a857b75`
- The `mlflow.set_experiment` ordering fix: commit `41e82eee`
