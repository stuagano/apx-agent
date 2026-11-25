# Bind agent chat to an AI Gateway model service

APX sends chat through Unity Catalog AI Gateway. A declared
`catalog.schema.service` name is sent as written. A `databricks-<id>`
foundation endpoint is sent as `system.ai.<id>`. Deployment does not create
or reconcile a Model Serving endpoint for chat.

```python
agent = Agent(name="support-agent", model="main.ai.support_chat")
```

`main.ai.support_chat` is an example name: create and authorize that service
before using it. A model service can route to a Databricks-hosted model or an
external provider. See [Databricks model services](https://docs.databricks.com/aws/en/ai-gateway/model-services)
for provider configuration and routing.

## Migrate a provider-prefixed declaration

Replace `model = "bedrock:anthropic.claude-..."` with the fully qualified name
of an AI Gateway model service configured for that provider:

1. Configure the external provider and model service in AI Gateway.
2. Grant the calling service principal `EXECUTE` on the model service and
   `USE CATALOG` / `USE SCHEMA` on its parents.
3. Set APX `model` to that service name, for example `main.ai.support_chat`.
4. Remove the old `gateway=` chat settings from the agent. These legacy
   endpoint settings do not configure Gateway model-service policies.
5. Verify a bounded request with the intended deployment identity before
   deployment. APX validates the declaration locally; that does not prove the
   service exists or that the deployed principal can invoke it.

Provider-prefixed declarations fail during configuration loading, before Apps
builds or provisioning. Direct Python `get_llm` calls apply the same validation.
APX preserves valid authored names and does not translate a Model Serving
endpoint into a Gateway model-service binding.

## Check the binding

Inspect the service with the selected workspace profile:

```bash
databricks ai-gateway get-model-service \
  model-services/main.ai.support_chat --profile <PROFILE>
```

A successful metadata read alone is not an inference authorization test. Make
a bounded call through the same factory the agent uses:

```python
from databricks.sdk import WorkspaceClient
from apx_agent import get_llm

llm = get_llm(
    "main.ai.support_chat",
    workspace_client=WorkspaceClient(profile="<PROFILE>"),
    max_tokens=16,
)
reply = llm.invoke("Reply with OK only.")
assert reply.content
```

See [querying model services](https://docs.databricks.com/aws/en/ai-gateway/query-model-services)
for supported model names and request formats. Chat uses the service identity;
APX tools continue to enforce their declared user or service identity.

An external guardrail on that service is attached in the Unity Gateway UI.
The steps, and what a gateway `DENY` or `ASK` looks like in the agent, are in
[unity-gateway-external-guardrails.md](../safety/unity-gateway-external-guardrails.md).

## Model Serving endpoints

`foundation_model_tool` no longer queries Model Serving: like chat, it calls a
Unity Gateway model service as the App service principal. Knowledge Assistant
tools, embeddings and sub-agent endpoints still use their serving endpoints.
This migration does not delete endpoints; remove an obsolete endpoint only
after checking its other callers.
