# Compare models on the same evaluation cases

`evaluate_sweep` runs a fixed dataset across two or more distinct model endpoint
names, in order. It reuses `evaluate` for live tools and MLflow scorers, or
`evaluate_chain` for [recorded-tool fixtures](chain-fixtures.md).
Live sweeps require the evaluation extra: `pip install 'apx-agent[eval]'`.

## Python

```python
from dataclasses import asdict
from apx_agent import Agent, evaluate_sweep

agent = Agent(instructions="Answer the question concisely.")
comparison = evaluate_sweep(
    agent,
    models=["endpoint-a", "endpoint-b"],  # Replace with your endpoints.
    evalset=[{
        "inputs": {"question": "What is 2 + 2?"},
        "expectations": {"expected_response": "4"},
    }],
    judge_model="databricks",  # Same judge for both candidate models.
    experiment="/Users/you@example.com/agents/model-comparison",
)
for result in comparison:
    print(asdict(result))
```

The result is a list of `SweepResult` values in the requested model order.
`metrics` preserves MLflow's aggregate metric names, such as
`correctness/mean`. Use `scorers=[...]` to reuse your own scorers; an explicit
empty list disables scoring. Unavailable metric values become `None` and are
identified in `errors`.

Live evalsets must be nonempty lists of rows with `inputs`, or pandas DataFrames
with the same columns. Each model receives a deep copy. Precomputed `outputs`,
`trace`, and `trace_id` columns are rejected: every candidate must run a fresh
prediction. File/URI resolution belongs to the caller; the CLI accepts JSON
and JSONL files. Existing `evaluate` dataset support is unchanged.

For typed chain checks, pass `fixtures` instead of `evalset`:

```python
comparison = evaluate_sweep(
    chain,
    models=["endpoint-a", "endpoint-b"],
    fixtures=recorded_cases,
)
```

This uses real models with recorded tool results. `outcome_counts` contains
`correct`, `escalated_with_evidence`, and `wrong`; it does not introduce a judge
or collapse these into a new score. The existing fixture restrictions apply.
Fixture mode cannot be combined with live scorers, a judge, an experiment, or
OBO identity arguments. All fixtures are validated before any model runs.

## CLI

Authenticate against an explicitly chosen profile before running the command.
The sweep uses the process's existing authentication and MLflow tracking
configuration; it does not select or switch a Databricks profile.
Live sweeps reject an active `APX_AGENT_MODEL_OVERRIDE`: that hot-swap setting
would otherwise route every candidate to the same endpoint. Unset it before
running a live comparison. Fixture sweeps compile their requested models directly.
Replace `your-profile` and the endpoint names below with your chosen profile
and model endpoints.

```bash
DATABRICKS_CONFIG_PROFILE="your-profile" apx-agent eval sweep evalset.jsonl \
  --module agent:agent \
  --model endpoint-a --model endpoint-b \
  --judge-model databricks \
  --experiment /Users/you@example.com/agents/model-comparison
```

The default table puts quality, p50/p95 milliseconds, timing coverage, estimated
LLM USD, pricing coverage, and status on one row per model. Use `--format json`
for a machine-readable list. Evaluator progress goes to stderr so stdout can
be saved directly as JSON.

```bash
DATABRICKS_CONFIG_PROFILE="your-profile" apx-agent eval sweep fixtures.json \
  --module agent:agent --fixtures \
  --model endpoint-a --model endpoint-b --format json > comparison.json
```

`--user-token` preserves the existing OBO path in live evaluation. Python also
accepts `workspace_host`. Live CLI runs honor `[tool.apx.agent].experiment` when
`--experiment` is omitted; fixture runs do not inherit that setting.

## Interpret the measurements

- **Latency:** live mode reads each returned MLflow trace's execution duration
  in milliseconds. Fixture mode uses each case's existing elapsed timer,
  including compilation and fixture checks. These two modes measure different
  boundaries. Judge runtime and overall sweep wall time are not prediction
  latency. Percentiles reuse APX's existing comparison helper: sort the samples,
  select `round(n * percentile / 100) - 1`, clamped to the sample bounds.
  `latency_cases / case_count` shows coverage; missing latency is an error.
- **Cost:** `cost_usd` is an **MLflow-reported LLM estimate for evaluated case
  traces**, not an invoice or the total cost of running the sweep. It is only
  populated when every expected case has a unique trace, a finite nonnegative
  trace cost, and cost data on every instrumented LLM span. Missing or partial
  prices produce `None` (`unavailable` in the table); a reported zero remains
  zero. `cost_cases` shows how many cases were priced. Uninstrumented calls
  cannot be accounted for. Scorer/judge calls, MLflow's prediction-validation
  calls, tools, infrastructure, and billing discounts are excluded. Fixture
  cost is currently unavailable. Endpoint-wide `cost_for_endpoint` billing is
  deliberately not attributed to evaluation runs.
- **Failures:** per-model exceptions become error rows; later models still
  run. Cancellation propagates. Missing/duplicate traces, failed traces, and
  scorer errors are visible rather than silently improving averages. The CLI
  prints the comparison and exits 1 for execution or telemetry errors. A valid
  fixture run with `wrong` outcomes is still a completed evaluation and exits
  0; apply your own quality threshold to the results.

`run_id` links live rows to their MLflow evaluation runs. Models run sequentially
to avoid adding cross-model contention; MLflow still controls concurrency
within each live evaluation. Small samples are diagnostic, not statistically
conclusive. The sweep does not automatically pick a winner or change the
agent's configured model. Tools and callbacks retain their normal effects;
use recorded fixtures when you need fixed tool results across candidates.
