# Unity Gateway model services in Apps agents

[Unity Gateway](https://docs.databricks.com/aws/en/ai-gateway/) governs model
access, cost and traffic in Unity Catalog. A **model service** is a UC
securable with a three-part name, `catalog.schema.service`. Pay-per-token
foundation models live under `system.ai` (for example
`system.ai.claude-sonnet-4-5`). Custom services live in any schema you can
create services in.

apx-agent calls a model service through the OpenAI-compatible Unity Gateway
path, `https://<workspace>/ai-gateway/mlflow/v1`, using the three-part name as
the model id. Model Serving endpoints are not used for this call.

## Status

| Piece | Status |
|---|---|
| Unity Gateway | Generally available ([release notes](https://docs.databricks.com/aws/en/release-notes/unity-gateway/)) |
| Model service as a Databricks Apps resource | **Public Preview** ([docs](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/unity-gateway)) |
| Service policies (guardrails) | Beta; attached in the Unity Gateway UI, not by apx (see [external guardrails](../safety/unity-gateway-external-guardrails.md)) |

Before you deploy, confirm the workspace is in a region that supports Unity
Gateway and that the Apps model-service resource preview is available to it.

## Declare it

The agent's chat model and `foundation_model_tool` both take a model service:

```python
from apx_agent import Agent, foundation_model_tool

agent = Agent(
    model="system.ai.claude-sonnet-4-5",          # chat model: a model service
    tools=[
        foundation_model_tool(
            "main.agents.reasoner",               # custom model service
            name="ask_reasoner",
            description="Ask the reasoning specialist a hard question.",
        ),
    ],
)
```

- A dotted name must be a three-part `catalog.schema.service`.
- A chat model or `foundation_model_tool` named `databricks-<id>` is still
  accepted. apx sends it as the model service `system.ai.<id>`, so
  `foundation_model_tool("databricks-claude-opus-4-7")` calls
  `system.ai.claude-opus-4-7` and declares that `MODEL_SERVICE` resource.
- Any other plain name, such as a custom Model Serving endpoint, is rejected
  when the tool is built. Put that model behind a Unity Gateway model service
  and pass its three-part name.

## What deploy emits

Each model service the agent reaches becomes an Apps `uc_securable`
resource. `EXECUTE` is the only permission level:

```yaml
resources:
  - name: claude-sonnet-4-5-1a2b3c4d
    uc_securable:
      securable_full_name: system.ai.claude-sonnet-4-5
      securable_type: MODEL_SERVICE
      permission: EXECUTE
```

How it lands depends on the [deploy path](overview.md#which-deploy-path-runs):

| Path | Model service grant |
|---|---|
| `apps_api` | Sent as the App resource above. |
| Databricks Bundle | Written to the app's `resources` in `databricks.yml`. |
| Agent Bricks CLI | Bound in the staged `agent.toml` as `[experimental.model_services.<role>]`. The chat model takes the `agent` role. `agentbricks deploy` grants the App service principal `EXECUTE`. |
| App Space | The space must already carry each tool's model service. The chat model's service is not required, so existing space deploys keep working. |

When you add the resource, Databricks grants the App service principal
`USE CATALOG` on the parent catalog, `USE SCHEMA` on the parent schema, and
`EXECUTE` on the service. Each grant succeeds only if the deployer holds
`MANAGE` on that object, or all account users already hold the privilege.
`system.ai` pay-per-token services work without extra setup. Removing the
resource revokes only `EXECUTE`.

## Who calls the service

Both the chat model and every `foundation_model_tool` call Unity
Gateway as the **App service principal**, the identity the `MODEL_SERVICE`
resource grants. A Model Serving `CAN_QUERY` grant does **not** grant access to
a Unity Gateway service.

The deploy also keeps the `ai-gateway` user API scope it has always declared
for the chat model.

## Budgets, rate limits and usage

Unity Gateway enforces what is configured on each model service, so these
apply to apx agents with no apx configuration:

- **Rate limits** are set per service in the Unity Gateway UI. Over the limit,
  the call fails. `foundation_model_tool` returns the error text to the
  calling LLM instead of raising.
- **Budgets** set per-user thresholds and hard caps on spend.
- **Usage** is tracked in Unity Gateway system tables
  (`system.ai_gateway.usage`), attributed to the App service principal.
  Attribution is accurate for standard Databricks Apps. Apps in a micro-VM
  environment, such as Genie app builder apps, do not report accurate
  attribution.

apx does not author service policies. They are Beta and are not needed for the
query path.

## Diagnose it

`apx-agent doctor` (unless `--offline`) looks up each declared model service with
`GET /api/2.1/unity-catalog/model-services/<name>` and names the failure:

| Result | Meaning |
|---|---|
| `Model service ... was not found` | The name is wrong, or the service is not in this workspace's metastore. |
| `Access to model service ... was denied` | The checking identity lacks `EXECUTE` (or `USE CATALOG` / `USE SCHEMA`). At runtime, the App service principal needs these, and the `MODEL_SERVICE` resource grants them. |
| `Unity Gateway is unavailable` | The workspace region or preview does not support Unity Gateway. |

At runtime, `foundation_model_tool` returns the same explanations to the LLM.
The lookup runs as you, not the App service principal, so a green check does
not prove the App's runtime access.
