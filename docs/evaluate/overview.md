# Evaluation + MLflow experiments

## MLflow experiments

Each agent gets its own experiment so runs, traces, and eval results stay organized. Set the experiment at three layers, in priority order:

1. **CLI flag** — `apx-agent agents deploy --experiment ...`, `apx-agent eval run --experiment ...`
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

## Trace-linked human feedback

Use trace feedback when reviewers already work in an existing annotation or review application. The review backend keeps its own UI and workflow; APX only writes the resulting human assessment to the original MLflow trace and reads it back in a stable shape.

```bash
apx-agent traces feedback tr-123 \
  --name domain_quality \
  --value 4 \
  --comment "Correct answer, weak rationale" \
  --source review-app \
  --idempotency-key review-row-123 \
  --evidence screenshot_uri=s3://bucket/review.png \
  --evidence feature=claims_search \
  --format json

apx-agent traces feedback-view tr-123 --format json
```

The write command accepts boolean, integer, float, or string values. Each `--evidence KEY=VALUE` entry is stored as string metadata; it can reference an external screenshot or artifact, but APX does not upload or manage that artifact. `feedback-view` returns the trace tags plus normalized feedback and expectation assessments.

### Runtime and access

- Install `apx-agent[eval]`. The current supported boundary is MLflow `>=3.14,<3.15`; APX writes with `mlflow.log_feedback` and reads with `mlflow.get_trace`.
- The commands use MLflow's active tracking configuration and do not choose a Databricks CLI profile. For Databricks-hosted MLflow, authenticate the process against the intended workspace before invoking them.
- The authenticated principal must be able to read the target trace and write an assessment to it. APX does not bypass workspace permissions or substitute another identity.
- Feedback is recorded with MLflow `HUMAN` provenance. `--source` becomes the assessment source identifier; when omitted, APX uses `apx.trace_feedback`.

### Replay and correction behavior

`--idempotency-key` provides best-effort duplicate prevention. APX first reads the trace assessments and reuses the assessment carrying the same key instead of writing another one. Use a stable key that is unique per external review record and trace. Without a key, every invocation creates a new assessment.

The metadata key `apx.feedback.idempotency_key` is reserved and cannot be supplied through `--evidence`. APX does not currently update or delete an existing assessment: submitting a corrected review requires a new idempotency key, while retrying the old key returns the original assessment.

This path complements the MLflow Review App workflow exposed by `apx-agent label start` and `label align`; it does not create a replacement review UI, schedule production scoring, or perform autonomous remediation.
