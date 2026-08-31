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

These examples use `--experiment` plus `--agent-name` to address an
Apps-deployed agent that is not fleet-discoverable. For a Unity Catalog
registered fleet agent, use `--uc-name catalog.schema.agent` instead; both
addressing forms are supported.

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

## Sampled production scoring

MLflow `3.14` can run a registered scorer asynchronously against sampled
production traces. Use that native lifecycle instead of creating a separate APX
scheduler or replaying the agent in a Lakeflow Job. The scorer evaluates the
outputs already recorded on each selected trace and writes its assessment back
to that trace.

Start with a small sampling rate and one calibrated decision. The process that
runs this setup must be authenticated to the intended Databricks workspace; APX
does not choose a profile or deploy monitoring on your behalf.

```python
import mlflow
from mlflow.genai.scorers import ScorerSamplingConfig, get_scorer
from mlflow.tracing import set_databricks_monitoring_sql_warehouse_id

experiment = mlflow.set_experiment("/Shared/claims-agent-production")

set_databricks_monitoring_sql_warehouse_id(
    "0123456789abcdef",
    experiment_id=experiment.experiment_id,
)

# `label align --new-version domain_quality-v2` registered this scorer.
scorer = get_scorer(
    name="domain_quality-v2",
    experiment_id=experiment.experiment_id,
)
active_scorer = scorer.start(
    experiment_id=experiment.experiment_id,
    sampling_config=ScorerSamplingConfig(
        sample_rate=0.10,
        filter_string=(
            "attributes.status = 'OK' AND "
            "tags.`apx.agent.name` = 'claims-agent'"
        ),
    ),
)
print(active_scorer.sampling_config)
```

Both `register()` and `start()` are required for a new scorer. The example uses
`get_scorer()` because the calibration flow already registered a versioned
judge. `start()` is experimental in MLflow `3.14`; keep the APX-supported
`mlflow>=3.14,<3.15` boundary and revalidate this setup before upgrading.

The authenticated principal needs `CAN USE` on the monitoring SQL warehouse
and `CAN EDIT` on the experiment. Scorer registration may create or update
Databricks-managed monitoring resources, so run setup as the intended operator,
not from an end-user request path.

### Verify and roll back

Confirm the scorer is registered with the intended sample rate, then inspect a
sampled trace for the assessment named `domain_quality-v2`:

```python
from mlflow.genai.scorers import get_scorer, list_scorers

for registered in list_scorers(experiment_id=experiment.experiment_id):
    print(registered.name, registered.sampling_config)

current = get_scorer(
    name="domain_quality-v2",
    experiment_id=experiment.experiment_id,
)
```

```bash
apx-agent traces feedback-view TRACE_ID --format json
```

Registration alone does not prove that production scoring is active. Verify
that new eligible traces receive the assessment and record the delay before
increasing the sample rate. To stop new scoring without deleting the registered
judge:

```python
current.stop(experiment_id=experiment.experiment_id)
```

Stopping preserves the scorer for inspection or restart and does not remove
assessments already attached to traces.

### Production scorecard

Review a fixed observation window and record the numerator and denominator for
every rate. Do not describe the scorer as production-ready from an aggregate
judge score alone.

| Signal | Calculation | Required evidence |
| --- | --- | --- |
| Coverage | Eligible traces with the scorer assessment / eligible traces | The same time window and trace filter used by the scorer |
| Human-validated false-positive rate | Judge-flagged traces marked non-actionable by a human / judge-flagged traces reviewed by a human | Reviewer feedback using the same decision boundary and judge-compatible name |
| Agent latency | Trace `execution_time_ms`, reported separately from scoring | Median and tail latency for eligible scored and unscored traces |
| Scoring delay | Time from trace completion to scorer assessment, when available | Assessment and trace timestamps from the same workspace |
| Cost | Observed Databricks usage attributable to the scorer during the window | Billing records or a documented `unknown`; never infer exact per-trace cost from the sample rate |

False-positive rate is unknown until humans review judge-flagged traces. Use
`traces feedback` or the per-app feedback API to record those decisions; do not
treat missing human feedback as agreement. Compare latency between scored and
unscored traces to verify that asynchronous scoring did not change request-path
latency.

`apx-agent agents cost` reports the agent serving endpoint's usage. It is useful
request-path context but is not proof of the monitoring scorer's cost. Attribute
monitoring cost from workspace billing data when possible, otherwise report it
as unavailable rather than folding it into endpoint cost.

Promote the sample rate only after coverage is stable, the human-reviewed
false-positive rate meets the threshold chosen during calibration, latency has
not regressed, and cost is understood. This pattern schedules scoring and
records assessments; it does not create alerts, a dashboard, issue tickets, or
autonomous remediation.

## Human issue triage

Treat a low or failing judge assessment as a review candidate, not as a defect
or remediation instruction. The judge ranks evidence; a human decides whether
the trace represents an actionable issue. Keep the first loop to one issue
class and one bounded review batch.

### 1. Define one issue class

Write the decision before selecting traces:

| Field | Example |
| --- | --- |
| Issue class | `unsupported_claim` |
| Scorer and version | `domain_quality-v2` |
| Candidate condition | Judge value below the calibrated acceptance threshold |
| Known positive | A confirmed trace where the answer invents a policy requirement |
| Known exclusion | A correct answer that explicitly says the source data is incomplete |
| Impact signal | Affected workflow, user segment, or governed data operation |
| Human outcome | `actionable` or `not_actionable`, with rationale |
| Owner | The team that can investigate the underlying agent behavior |

Do not combine unrelated failure modes under a generic `bad_response` class.
If reviewers need different evidence or remediation owners, use separate issue
classes.

### 2. Select and rank candidates

Start from traces that carry the configured scorer assessment. Use the same
experiment, scorer version, time window, and eligibility filter as the
production scorecard. Exclude traces already reviewed for this issue class
unless they are deliberate rechecks.

Rank a small candidate batch with explicit ordered criteria:

1. Higher business, permission, safety, or data-integrity impact first.
2. More severe judge failures before borderline failures.
3. Repeated patterns before isolated examples when the impact is comparable.
4. Novel failures before duplicates once recurrence is established.

Keep known positives and known exclusions in the batch as controls. Do not hide
them from reviewers or use a proprietary combined priority score: the ordering
should remain inspectable and adjustable for each issue class.

`apx-agent traces list --format json` supplies trace IDs and request-path
metadata for initial narrowing. Inspect the scorer and prior human assessments
on a candidate before routing it:

```bash
apx-agent traces list \
  --experiment 123456789 \
  --agent claims-agent \
  --limit 100 \
  --format json

apx-agent traces feedback-view TRACE_ID --format json
```

The list command does not interpret judge-specific values or compute a
universal priority. Rank with the issue definition in the existing review
workflow or MLflow trace view, where the team already owns its taxonomy and
business-impact data.

### 3. Route a bounded review batch

Use the existing review surface rather than creating an APX triage UI:

- If the candidate selection is expressible as an MLflow trace filter, route a
  bounded batch through `apx-agent label start --filter ... --limit ...`.
- If a customer review application already selected explicit trace IDs, keep
  assignment and ordering there and use the APX feedback adapter for write-back.

For the MLflow Review App path:

```bash
apx-agent label start \
  --experiment 123456789 \
  --agent-name claims-agent \
  --judge domain_quality-v2 \
  --scale 1-5 \
  --filter "attributes.status = 'OK'" \
  --limit 25 \
  --assignee reviewer@example.com
```

The filter and limit define eligibility, not severity ordering. Use this path
only when its selected set matches the issue-class batch. Otherwise route the
explicit ranked IDs through the existing review application instead of
claiming that `label start` preserved an ordering it does not implement.

### 4. Record the human decision

Write the review back to the original trace. Keep the human feedback name
judge-compatible when it validates that judge's decision boundary, and include
the issue class in evidence metadata so later analysis can separate failure
modes:

```bash
apx-agent traces feedback TRACE_ID \
  --name domain_quality-v2 \
  --value not_actionable \
  --comment "The answer correctly discloses that the source is incomplete." \
  --source claims-review \
  --idempotency-key claims-review-row-481 \
  --evidence issue_class=unsupported_claim
```

Use a new idempotency key for a corrected decision. Reviewer identity, source,
rationale, and supporting references remain attached to the trace; do not copy
screenshots or customer workflow data into APX unless the existing application
already exposes an approved reference.

### 5. Validate before opening an issue

For each observation window, record:

- candidate count and reviewed count;
- actionable count and human-validated false-positive rate;
- recurrence across workflows, users, tools, and data segments;
- representative trace IDs for confirmed positives and exclusions;
- the scorer version, threshold, and issue-class definition used.

Open or route an engineering issue only when a human confirms an actionable
class with reproducible evidence, an affected behavior, and an owner. Include
the smallest safe reproduction and representative trace IDs according to the
team's data-handling policy. A high judge score count without human validation
is a monitoring signal, not a confirmed issue.

If reviewers reject most candidates, refine the rubric, threshold, or issue
definition and repeat a small batch. If confirmed examples expose judge drift,
feed the resolved labels back into the calibration workflow. APX does not
create tickets, assign remediation, change prompts, or redeploy agents from
triage results.
