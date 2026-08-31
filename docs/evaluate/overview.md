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

### Per-app HTTP API

Every APX FastAPI application exposes the same trace-feedback routes after it
upgrades and redeploys:

```http
POST /_apx/feedback
Content-Type: application/json

{
  "trace_id": "tr-123",
  "name": "domain_quality",
  "value": 4,
  "comment": "Correct answer, weak rationale",
  "idempotency_key": "review-row-123",
  "evidence": {
    "screenshot_uri": "s3://bucket/review.png",
    "feature": "claims_search"
  }
}
```

```http
GET /_apx/feedback/tr-123
```

Call these routes through the Databricks Apps gateway as a signed-in user. The
gateway supplies the OBO token and user identity; APX writes and reads MLflow as
that user. The JSON body cannot select a source identity, workspace host, or
service principal. Missing OBO fails closed, and `APX_DEV_UI_TOKEN` does not
authorize this API.

The endpoints remain available when `APX_DEV_UI=0`. Existing deployments must
upgrade APX and redeploy before they expose the routes.

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

## Golden set and judge calibration

Use a small, representative set before aligning a judge or trusting its score in
production. A useful first pass is 25–50 traces: enough to expose recurring
disagreements while remaining small enough for reviewers to discuss carefully.

### 1. Define the decision and label

Choose one judge and one review question. Keep the feedback name identical to
the registered judge name when the human labels will calibrate that judge.
Write the rating scale, evidence reviewers should consider, and the decision
boundary before selecting traces; otherwise disagreements mix rubric ambiguity
with judge error.

### 2. Select representative traces

Include normal high-volume requests, business-critical cases, known failures,
edge cases, and relevant permission or data segments. Do not build the set only
from the worst judge scores. Reserve some traces as a held-out validation set;
do not include those traces in alignment.

Every trace sent through `label start` must already carry the selected judge's
baseline assessment. Run the judge over the candidate traces first, then create
the review session with a narrow MLflow filter and an explicit limit:

```bash
apx-agent label start \
  --experiment 123456789 \
  --agent-name claims-agent \
  --judge domain_quality \
  --scale 1-5 \
  --filter "attributes.status = 'OK'" \
  --limit 50 \
  --assignee reviewer-one@example.com \
  --assignee reviewer-two@example.com
```

Save the printed run ID; `label align` uses the `apx.label.run` tag to retrieve
the same traces later.

### 3. Collect reviewer overlap

Have at least two reviewers independently label a shared calibration subset.
They should record a value and short rationale grounded in trace evidence. An
existing review application can submit the same judge-named feedback through
`traces feedback` or `POST /_apx/feedback`; the MLflow Review App path records
labels directly in the session created above.

APX does not assign the same trace to multiple reviewers or calculate
inter-rater agreement. Coordinate overlap and record the resolved label in the
existing review process.

Inspect disputed traces before alignment:

```bash
apx-agent traces feedback-view TRACE_ID --format json
```

Resolve rubric ambiguity first. Keep legitimate differences in the training
set only when the agreed rationale makes the desired decision clear; otherwise
exclude the trace and refine the label instructions.

### 4. Align a versioned judge

After the assigned reviewers finish the session, align from the saved run ID.
Register a new judge name so the original remains available for comparison and
rollback:

```bash
apx-agent label align \
  --experiment 123456789 \
  --judge domain_quality \
  --run domain_quality-20260831T190000Z \
  --new-version domain_quality-v2
```

Alignment requires `apx-agent[align]`. It fails if the run has no human labels
rather than silently producing an ungrounded judge.

### 5. Validate and promote

Run the original and aligned judges over the held-out traces. Compare agreement
with the resolved human labels, false positives, false negatives, and coverage
across the segments used during selection. Also record latency and cost if the
aligned judge will score production traffic.

Choose acceptance thresholds before inspecting results. Promote the new judge
only when it improves the target decision without a material held-out or
segment regression. Keep the original judge registered until the replacement
has passed its production observation window.
