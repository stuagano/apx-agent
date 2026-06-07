# Evaluation + MLflow experiments

## MLflow experiments

Each agent gets its own experiment so runs, traces, and eval results stay organized. Set the experiment at three layers, in priority order:

1. **CLI flag** — `apx deploy --experiment ...`, `apx eval --experiment ...`
2. **`[tool.apx.agent]` in pyproject** — `experiment = "/Users/me/agents/my_agent"`
3. **Direct kwarg** — `log_agent(..., experiment=...)`, `evaluate(..., experiment=...)`

When none is set, MLflow's currently-active experiment (or default) is used.

```toml
# pyproject.toml
[tool.apx.agent]
name = "customer_triage"
model = "databricks-claude-sonnet-4-6"
experiment = "/Users/me@company.com/agents/customer_triage"
```

```python
log_agent(agent, model="...", registered_model_name="...",
          experiment="/Users/me@company.com/agents/triage")

evaluate(agent, model="...", evalset=[...],
         experiment="/Users/me@company.com/agents/triage")
```

On Databricks-hosted MLflow, experiment names are workspace paths (e.g. `/Users/you@company.com/agents/my_agent` or `/Shared/agents/my_agent`). Local MLflow accepts plain names. Errors from `mlflow.set_experiment` surface with a friendly message pointing at the path convention.

## Evaluation

`apx_agent.evaluate(agent, model=..., evalset=..., scorers=...)` runs Mosaic AI Agent Evaluation against the agent in-process — no deploy, no HTTP, fast feedback during authoring and CI. The agent compiles to a `ChatAgent` once; each evalset entry runs through the compiled graph; results come back as a standard `mlflow.genai.evaluate` result.

```python
from apx_agent import Agent, evaluate, lineage_tool

agent = Agent(
    instructions="Investigate missing data.",
    tools=[lineage_tool()],
)

result = evaluate(
    agent,
    model="databricks-claude-sonnet-4-6",
    evalset=[
        {"request": "what feeds main.sales.orders?", "expected_response": "..."},
        {"request": "trace lineage for main.finance.revenue", "expected_response": "..."},
    ],
    # scorers default to Correctness + RelevanceToQuery from mlflow.genai.scorers;
    # pass scorers=[...] for custom judges.
)
```

The wrapper tolerates the common eval-dataset shapes — bare strings, `{"request": ...}`, `{"input": ...}`, `{"prompt": ...}`, `{"messages": [...]}`. Pass `user_token=...` (and `workspace_host=...`) to evaluate as a specific user via the OBO path — useful for testing UC-grant boundaries during eval.

Requires the `eval` extra (mlflow). apx-agent isn't on PyPI yet — from a git clone:

```bash
cd apx-agent/python
pip install -e '.[eval]'
```
