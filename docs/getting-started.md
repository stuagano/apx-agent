# Getting started

## Python

```python
from apx_agent import Agent, genie_tool, lineage_tool, uc_function_tool

agent = Agent(
    instructions="You investigate missing data in Databricks tables.",
    tools=[
        lineage_tool(),
        genie_tool("abc123", description="Answer data questions"),
        uc_function_tool("main.tools.classify_intent"),
    ],
)
```

Deploy as a Mosaic AI agent (Model Serving):

```python
import mlflow
from databricks import agents
from apx_agent import log_agent

with mlflow.start_run():
    info = log_agent(
        agent,
        model="databricks-claude-sonnet-4-6",
        registered_model_name="main.agents.data_triage",
    )

agents.deploy("main.agents.data_triage", model_version=info.registered_model_version)
```

`log_agent` walks the agent tree, collects every declared resource (UC functions, Genie spaces, sub-agent endpoints, the LLM endpoint), and hands MLflow the full list. No manual `resources=[...]` to maintain.

Host as a Databricks App instead (same agent, different runtime):

```python
from apx_agent import create_app
app = create_app(agent)  # uvicorn-compatible FastAPI app
```

```bash
cd python
uv sync
uvicorn my_app:app --reload
```

## TypeScript

```typescript
import { createApp, server } from '@databricks/appkit';
import { createAgentPlugin, lineageTool, genieTool, ucFunctionTool } from 'appkit-agent';

createApp({
  plugins: [
    server(),
    createAgentPlugin({
      model: 'databricks-claude-sonnet-4-6',
      instructions: 'You investigate missing data.',
      tools: [
        lineageTool(),
        genieTool('abc123', { description: 'Answer data questions' }),
        ucFunctionTool('main.tools.classify_intent'),
      ],
    }),
  ],
});
```

```bash
cd typescript
npm install
npm run dev
```
