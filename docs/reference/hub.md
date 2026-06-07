# Hub + publish

## Hub

A lightweight registry for Apps-deployed agents. Agents self-register on startup. Provides a browseable index and powers cross-agent discovery in Apps deployments. UC + Mosaic AI's agent registry serves the same role for Model Serving deployments.

```toml
# python/pyproject.toml
[tool.apx.agent]
name = "data_triage_agent"
description = "Investigate missing data"
model = "databricks-claude-sonnet-4-6"
registry = "$AGENT_HUB_URL"
```

A worked example lives at [`python/examples/agent-hub/`](../python/examples/agent-hub/).

## Publish — register as a Supervisor sub-agent

Once an apx-agent is deployed as a Model Serving endpoint, `publish_to_supervisor` adds it as a sub-agent on an existing Mosaic AI Supervisor Agent. The supervisor's LLM routes among declared sub-agents — yours sits alongside Knowledge Assistants, Genie spaces, and other agents in the supervisor's tool list.

```python
from apx_agent import create_supervisor_agent, publish_to_supervisor
from databricks.sdk import WorkspaceClient

ws = WorkspaceClient()

# One-time: create the supervisor (or use the ID of an existing one)
supervisor = create_supervisor_agent(
    display_name="Data Ops Supervisor",
    description="Routes data-team queries to specialists.",
    instructions="Pick the right specialist for the user's question.",
    ws=ws,
)

# Per agent: register the deployed serving endpoint as a sub-agent
publish_to_supervisor(
    supervisor_agent_id=supervisor.id,
    serving_endpoint="data-triage",
    description="Triages questions about missing data in Databricks tables.",
    ws=ws,
)
```

User identity threads from the supervisor down through your sub-agent automatically — when the supervisor is called from AI Playground, the user's identity flows to the data-triage endpoint, scoped to *its* declared resources. No SP secrets, no CAN_USE grants between apps; the platform handles the chain.

`extra_tool_kwargs={...}` is the escape hatch for additional `Tool` fields if the preview SDK surface evolves before this helper is updated.

The Supervisor SDK (`databricks.sdk.service.supervisoragents`) is preview and may not be in older SDK versions. The helper raises a friendly `ImportError` pointing at the upgrade path when it's missing.
