# Deployment troubleshooting — `apx-agent agents deploy` failure modes and how to read them

Real-world failure modes observed when running `apx-agent agents deploy` against Databricks workspaces, in the order you're most likely to hit them. Every diagnosis here came from an actual deploy that broke, not from speculation — if a section reads thin it's because that mode hasn't been seen yet in practice.

Sibling docs:

- [`docs/running/lakebase-recipe.md`](../running/lakebase-recipe.md) — durable state side (sessions, memory, examples on Postgres).

## 1. Pre-flight checklist

Run these in order before `apx-agent agents deploy`. Five minutes here saves a failed-deploy round-trip.

```bash
# 1. apx-agent eval lint — static checks against your agent definition.
#    Catches missing instructions, unknown model endpoints, env-var refs
#    that aren't set, tools that reference UC objects that don't exist.
apx-agent eval lint --module agent:agent

# 2. apx-agent eval test — run a real prompt against the agent locally.
#    If this fails locally, deploy will fail too.
apx-agent eval test --module agent:agent --prompt "ping"

# 3. apx-agent agents describe — show the resource list the deploy will declare.
#    Compare against what UC / Genie / Vector Search actually has.
apx-agent agents describe --module agent:agent

# 4. Confirm workspace profile works.
databricks --profile prod current-user me

# 5. Confirm target catalog + schema exist (and you can write them).
databricks --profile prod schemas get main.agents
```

## 2. Common deploy failures

### LLM endpoint not accessible / model name resolves wrong

**Symptom.** Deploy succeeds, first chat call returns `RESOURCE_DOES_NOT_EXIST` or the model name is rejected.

**Diagnosis.** The agent chat model is called through **Unity Catalog AI Gateway** (`{host}/ai-gateway/mlflow/v1`). `get_llm` sends the declared model string unchanged. `system.ai.claude-sonnet-4-6` is a Gateway **model service** name (`catalog.schema.name`). `databricks-claude-sonnet-4-6` names a Model Serving endpoint; finding that endpoint does not establish that the same string resolves through Gateway. Confirm the model service exists and that the caller can access it. Serving endpoints still matter for explicit endpoint tools, embeddings, and sub-agent endpoints. Provider-prefixed chat declarations such as `bedrock:...` are rejected; configure the provider in AI Gateway and declare its model-service name instead. See [the migration guide](../reference/bedrock-via-ai-gateway.md).

**Fix.**

```bash
# Use the workspace profile selected for this project:
databricks ai-gateway get-model-service \
  model-services/system.ai.claude-sonnet-4-6 --profile <profile>
```

For an Apps project, set the model on the agent in `agent.py`:

```python
agent = Agent(..., model="system.ai.claude-sonnet-4-6")
```

Then validate the deployment plan locally:

```bash
apx-agent agents deploy --target apps --dry-run
```

If you need to swap the model on a deployed agent without redeploying, use `apx-agent agents hot-swap --llm-endpoint` — see `python/src/apx_agent/cli.py` for the command.
### Which App am I deploying?

`apx-agent agents deploy --target apps --profile <profile>` deploys an App.
New native projects declare `target="durable_agent_server"` on the
agent in `agent.py`; APX compiles that declaration and delegates rollout to Agent
Bricks. The normal native flow does not require an authored Bundle. Existing
Bundle projects retain their build and configuration; native durable projects
without a named space still delegate rollout to Agent Bricks.

The declaration's `target=` on the agent selects the **runtime contract**. Run
`apx-agent agents deploy --dry-run` to inspect the detected plan.
Native Apps `/readyz` verifies durable runtime initialization;
use a separate invocation to prove model and tool execution.

## 3. Trace and observability gotchas

### MLflow autolog must be enabled before the SDK client is created

If you build a custom serving wrapper (the builder-app pattern), this ordering matters:

```python
# CORRECT — autolog first, then the client picks it up.
import mlflow
mlflow.anthropic.autolog()         # or mlflow.openai.autolog() etc.

from anthropic import Anthropic
client = Anthropic()

# WRONG — autolog won't patch the already-instantiated client.
from anthropic import Anthropic
client = Anthropic()
import mlflow; mlflow.anthropic.autolog()
```

The autolog hook patches the module at import time; an instance constructed before the patch won't carry the hook through.

### `apx.*` span attributes need explicit `set_audit_attrs` calls

The framework exposes `apx.tool.name`, `apx.principal.id`, `apx.cost.usd`, etc., as MLflow span attributes — but only when the tool calls `set_audit_attrs(...)`. There is no automatic `apx.tool.name = <function>.__name__` capture. If your traces are missing these fields, audit the tool body:

```python
from apx_agent import set_audit_attrs, current_active_span

@tool(uc="main.tools.lookup_customer")
def lookup_customer(customer_id: str) -> dict:
    # set_audit_attrs takes the active span first, then short-key fields
    # (see _audit._KWARG_TO_KEY for the supported keys). A None span is a no-op.
    set_audit_attrs(
        current_active_span(),
        tool_name="lookup_customer",
        tool_uc_function="main.tools.lookup_customer",
    )
    # ... rest of the tool
```

### `apx-agent traces export` needs a reachable MLflow experiment

`apx-agent traces export --experiment <path>` queries MLflow's tracking server. If you're running it from a shell that has `MLFLOW_TRACKING_URI=file://...` set (a stale local override), it'll silently target your local store and find no traces.

```bash
# Force the workspace tracking URI for one command:
MLFLOW_TRACKING_URI=databricks apx-agent traces export --experiment /Users/you@databricks.com/apx-smoke
```

## 4. Cleaning up after a failed deploy

```bash
# Delete the App and its UC version-ledger model (--uc-name is required).
apx-agent agents delete --uc-name <catalog.schema.model> --app <app-name> --profile <profile>
```

The MLflow experiment and its traces can stay. They document the attempt and cost nothing.

## 5. Cross-references

- Lakebase / durable state: [`docs/running/lakebase-recipe.md`](../running/lakebase-recipe.md)
- CLI source: [`python/src/apx_agent/cli.py`](../../python/src/apx_agent/cli.py)
