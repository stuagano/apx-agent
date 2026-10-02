# Evaluate your agent

Start with one useful question: does the agent produce the expected answer?
Use the same entry point as you add cases, inspect typed steps, or compare models.

| What do you want to know? | Start here |
| --- | --- |
| Does my agent answer correctly? | `apx-agent eval run cases.json --judge-model databricks` |
| Which step went wrong? | `apx-agent eval run chain-cases.json --fixtures` |
| Which model works best? | `apx-agent eval sweep cases.json --model endpoint-a --model endpoint-b --judge-model databricks` |

The first two commands use `[tool.apx.agent].model` from your project, unless you
pass `--model`. A sweep names its candidates explicitly. Commands load
`agent:agent` by default; use `--module module:variable` for another declaration.
Model defaults follow the same project discovery as agent loading, including
parent directories and `APX_PYPROJECT`. The selected project's `.apx.local`
overrides its `pyproject.toml`; an explicit `--model` takes precedence over both.

## Run your first evaluation

In an APX project, install the evaluation extra:

```bash
uv add 'apx-agent[eval]'
```

For a minimal example, use this declaration in `agent.py`. If you already have
an agent, keep its declaration and write a case relevant to its job instead.

```python
from apx_agent import Agent

agent = Agent(instructions="Answer arithmetic questions concisely.")
```

Set your chosen model in the existing `[tool.apx.agent]` section of
`pyproject.toml` (or pass `--model` when running):

```toml
[tool.apx.agent]
name = "arithmetic"
model = "your-model-endpoint"
```

Save one case as `cases.json`:

```json
[
  {
    "inputs": {"question": "What is 2 + 2?"},
    "expectations": {"expected_response": "4"}
  }
]
```

Authenticate with your chosen Databricks profile, then run the case. Replace
`your-profile` and `your-model-endpoint` with your own values.

```bash
DATABRICKS_CONFIG_PROFILE="your-profile" uv run apx-agent eval run cases.json --judge-model databricks
```

This runs the real model and tools, then scores the response with MLflow's
default correctness, relevance, and safety scorers. `--judge-model databricks` selects
the workspace's default judge; it does not select the candidate model.
Model and judge calls may incur cost. The command prints the MLflow result;
a successful exit means evaluation completed, not that a quality threshold
was met. Set your acceptance criteria from the scores.

To save runs in a particular experiment, set `[tool.apx.agent].experiment` or
pass `--experiment`. Otherwise MLflow uses its active/default experiment.
See [experiment setup](advanced.md#mlflow-experiments) when you need to organize
or share results.

The equivalent Python entry point is `evaluate(agent, model=..., evalset=...)`.
It uses the same input/expectation rows. Pass `scorers=[...]` for your own
scorers; `scorers=[]` runs without scoring.

## Find the failing step

For a named sequential chain with typed outputs, use
[recorded-tool fixtures](chain-fixtures.md). These cases specify expected
intermediate outputs as well as the tool results each step should receive.

```bash
DATABRICKS_CONFIG_PROFILE="your-profile" uv run apx-agent eval run chain-cases.json --fixtures
```

The report shows each case's outcome and the paths of failing steps.
`correct` and an expected `escalated_with_evidence` pass; any `wrong` case
makes this command exit 1. Invalid fixture structure or incompatible flags
exit 2; malformed JSON exits 1.
The model still runs; real tool calls are replaced by recorded results.
Callbacks and guardrails retain their normal effects.

Fixture checks use `evaluate_chain(..., fixtures=...)` internally.
They do not inherit the project's experiment and reject live-evaluation
options such as `--judge-model`, `--experiment`, and `--user-token`.
Choose authentication through the process environment, as in the example.

## Compare models

Once you have useful cases, run them across candidate models:

```bash
DATABRICKS_CONFIG_PROFILE="your-profile" uv run apx-agent eval sweep cases.json \
  --model endpoint-a --model endpoint-b --judge-model databricks
```

[Model sweeps](model-sweeps.md) report quality, p50/p95 latency, and
trace-attributed LLM cost estimates where available. Use the same judge across
candidates. Add `--fixtures` instead of `--judge-model` for recorded-tool
cases. Use `--format json` to save a comparison.
A completed sweep can contain wrong answers; apply your own quality threshold.
Missing cost is unavailable, not zero.

## Improve a failing step

In the Edit tab, choose the agent variable in the **Instructions** selector,
then select **Improve instructions** and name your registered judge. GEPA
scores candidates through the full running agent using the existing eval
dataset, changing only the selected leaf's instructions for each trial.
The candidate appears in the editor for review; **Save** is the only action
that writes it to the source file. Restart afterward to load the saved code.

The target must be a local leaf declared in the editable source, with nonempty
literal instructions, and reachable from the running agent. Remote-bound children
must be edited and evaluated in their owning project. Save pending edits and
restart before optimizing. Changes made in the editor or source file while
optimization runs cause the candidate preview to be rejected. Model, judge,
reflection, and live tool calls may incur cost and retain their normal effects.
Use representative cases that exercise the selected child; a higher aggregate
score does not prove every step improved.

This improves agent instructions. [MemAlign](advanced.md#golden-set-and-judge-calibration)
aligns the judge to human feedback and remains a separate action.

## Other checks and advanced workflows

Use these when the task calls for them; they are not prerequisites for a first
scored evaluation.

- `eval lint`: static declaration checks.
- `eval test`: smoke prompts to check execution, without quality scoring.
- `eval chain`: inspect which sub-agents appeared in MLflow traces. For typed
  step correctness, use `eval run --fixtures`.
- `eval report`: compare previously logged evaluation runs.
- `eval run --endpoint-url URL`: evaluate a deployed App with its live tools,
  authentication, and network path.
- [Full CLI reference](../get-started/cli.md).

## MLflow experiments

[Configure experiments and result destinations](advanced.md#mlflow-experiments).

## Trace-linked human feedback

[Attach review decisions and evidence to traces](advanced.md#trace-linked-human-feedback).

## Golden set and judge calibration

[Build a representative dataset and calibrate a judge](advanced.md#golden-set-and-judge-calibration).

## Sampled production scoring

[Sample production traces, verify results, and roll back](advanced.md#sampled-production-scoring).

## Human issue triage

[Turn reviewed evidence into confirmed issues](advanced.md#human-issue-triage).
