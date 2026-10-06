# Bind agent chat to an AI Gateway model service

APX sends the declared chat model unchanged through Unity Catalog AI Gateway.
Declare an existing model service, or a supported Databricks foundation-model
name. Deployment does not create or reconcile a Model Serving endpoint for chat.

```toml
[tool.apx.agent]
name = "support-agent"
model = "main.ai.support_chat"
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
4. Remove the old `[tool.apx.agent.gateway]` chat settings. These legacy
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

## Explicit Model Serving tools

Existing endpoints can still be used by explicitly declared endpoint tools,
including `foundation_model.py`. Those tools retain their own endpoint and
permission contracts. This chat migration does not delete endpoints or change
those tools; remove an obsolete endpoint only after checking its other callers.
