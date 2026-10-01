# Evaluate chains with recorded tool results

From your agent project, run `apx-agent eval run chain-eval.json --fixtures`.
It uses `[tool.apx.agent].model` unless you pass `--model`, prints each case's
outcome and failing step paths, and exits 1 if any case is `wrong`. Expected
escalations with evidence pass. Choose your authentication explicitly through
the process environment. Fixture mode rejects live-evaluation options and
does not inherit the project's MLflow experiment.

For structured reports in Python, use the API below. To compare candidate
models on the same fixtures, use [model sweeps](model-sweeps.md).

Use `evaluate_chain(..., fixtures=...)` to run today's model and instructions
against fixed tool results. It runs the normal sequential compiler, including
typed output validation, guardrails, and opt-in escalation. The original tool
functions and their injected dependencies are not executed or resolved.

Every completed step is compared with an explicit expected JSON output. A bad
middle step makes the case wrong even if the final answer is correct. This is
exact expected-output evaluation; schema validity alone does not prove facts.

```python
import json
from dataclasses import asdict
from pathlib import Path
from apx_agent import evaluate_chain

# pipeline is your named SequentialAgent with output_schema on its leaves.
fixtures = json.loads(Path("chain-eval.json").read_text())
report = evaluate_chain(pipeline, model="your-model-endpoint", fixtures=fixtures)
print(json.dumps(asdict(report), indent=2))
assert report.outcome_counts.get("wrong", 0) == 0
```

Model calls remain real and may incur cost. Choose the model and authentication
explicitly. Author callbacks and guardrails also remain real: this mode replaces
tool execution, not arbitrary Python code. It does not verify live permissions,
tool correctness, data freshness, or deployment behavior.

## Fixture format

Supply a nonempty list of JSON objects. A successful case lists every leaf in
execution order. `path` starts with the root sequence name; nested sequences
add path components. Each successful step includes `expected_output` matching
its declared Pydantic schema. Recorded tool calls are scoped to that step.

```json
{
  "id": "customer-found",
  "request": "Check customer EXAMPLE-001.",
  "expected_outcome": "correct",
  "steps": [
    {
      "path": ["triage", "lookup"],
      "tools": [
        {
          "name": "lookup_customer",
          "args": {"customer_id": "EXAMPLE-001"},
          "output": {"customer_id": "EXAMPLE-001", "exists": true}
        }
      ],
      "expected_output": {"customer_id": "EXAMPLE-001", "exists": true}
    }
  ]
}
```

Tool names and arguments must match exactly as JSON: object-key order does not
matter; string, number, and boolean values are distinct. All recorded calls must
be consumed. Repeated identical calls consume successive matching records.
Different calls within a step can execute in any order. Missing, extra, unknown,
or mismatched calls fail the case; there is no fallback to the real tool.
Replayed results use native tool-message formatting, including string results
and content-block lists, so unavailability is interpreted as in normal execution.

Fixtures are caller-supplied data. Store only content appropriate for the
evaluation environment: model prompts and reports may contain fixture data.
The loader never imports executable code or schemas named by fixture files.

## Outcomes

| Outcome | Requirements |
| --- | --- |
| `correct` | Every step matches its expected validated output, every recorded tool call is consumed, and no failure occurs. |
| `escalated_with_evidence` | The expected failure path and reason match a valid terminal escalation, with nonempty evidence equal to the observed correct prior outputs and no downstream execution. |
| `wrong` | Any output, tool-call, execution, or escalation mismatch. A valid schema or plausible final answer cannot hide a failed step. |

For an expected escalation, set `expected_outcome` to
`"escalated_with_evidence"` and add:

```json
{
  "expected_failure": {
    "step": ["triage", "lookup"],
    "reason": "unavailable"
  }
}
```

List the exact prefix through that failed step. Prior steps require expected
outputs; the failed step omits `expected_output`. Reasons are `unavailable`,
`schema_miss`, and `timeout`. The agent itself must opt into escalation; evaluation
does not change its failure policy. A first-step escalation has no completed
evidence and cannot receive `escalated_with_evidence` credit. Timers remain real;
use deterministic unavailability fixtures for repeatable failure scenarios.

The report includes case IDs, final responses, duration, step-level expected and
actual outputs, `passed` flags, diagnostic codes, and aggregate `outcome_counts`.
An ordinary runtime error makes that case `wrong`; caller cancellation still
propagates. Invalid fixtures raise before any model call, including invalid
cases later in the list.

## Supported scope

This mode supports named `SequentialAgent` trees and typed local leaves. Names
must be nonempty and unique among siblings. It rejects remote bindings, other
composition types, deferred tools, and tools that mutate `Dependencies.State`:
recorded return values do not reproduce state mutations. Use normal evaluation
for those flows.

The existing `evaluate_chain(agent, model=..., evalset=..., experiment=...)`
trace-coverage mode is unchanged. Fixture mode does not query historical traces,
call MLflow judges, or log a tracking experiment. It rejects combinations with
`evalset`, `experiment`, `scorers`, identity options, or custom trace lookback.
Its outcome fields are separate from trace-based `sub_agent_coverage`.
Fixture reports leave trace coverage fields empty; trace-coverage reports leave
the new outcome fields unscored (`None` and empty counts).

## Runnable example

The data-triage example includes a small typed three-step chain and two
**synthetic** tool-result fixtures (success and unavailable lookup). They are
not captured production records and do not evaluate the deployed six-step agent.

From the repository's `python` directory:

```bash
DATABRICKS_CONFIG_PROFILE=<your-profile> uv run --frozen python \
  examples/data-triage-agent/eval/replay_example.py --model <your-model-endpoint>
```

The script prints the full report as JSON and exits nonzero if any case is
wrong. The example lives in `python/examples/data-triage-agent/eval/replay_example.py`;
its data is in `python/examples/data-triage-agent/fixtures/chain-eval.json`.
