# Evaluate against a Unity Catalog eval dataset

An MLflow evaluation dataset is a Unity Catalog table that
`mlflow.genai.datasets` creates and versions. `apx-agent eval run` scores one
by name, the same way it scores a local file, and records on the run which
dataset it scored. APX uses the upstream API unchanged. It adds no
`agent.toml` keys and no dataset format of its own.

Install the evaluation extra. It brings `mlflow[databricks]`, which includes
`databricks-agents`:

```bash
uv add 'apx-agent[eval]'
```

Point MLflow at your workspace, the same as for any Databricks-hosted eval:

```bash
export MLFLOW_TRACKING_URI=databricks
export DATABRICKS_CONFIG_PROFILE=your-profile
```

## Build a dataset

`apx-agent eval dataset add NAME` creates the dataset `NAME`
(`catalog.schema.table`) if it does not exist, then merges records into it.
Records with the same inputs merge into one record, so re-running the command
updates the dataset and does not duplicate rows.

From local eval files (JSON or JSONL in the `eval run` format):

```bash
apx-agent eval dataset add main.evals.support cases.jsonl more-cases.json \
  --experiment /Users/you@company.com/agents/support
```

Rows nested twice (`{"inputs": {"inputs": {...}}}`) are stored nested once,
the shape the dataset expects.

From traces, with `mlflow.search_traces` on the experiment:

```bash
apx-agent eval dataset add main.evals.support --from-traces \
  --filter "tags.environment = 'prod'" --max-results 200 --labeled
```

The `trace` column carries each trace's inputs. Its expectation assessments
(for example from a labeling session) become the record's expectations.
`--labeled` keeps only traces with a human assessment, so the dataset holds
cases an SME has reviewed. The traces are read with their spans, so the
workspace's trace store must be reachable from where you run the command.

`--experiment` takes an experiment path or id and falls back to `experiment`
in `[tool.apx]`. A new dataset is linked to that experiment. `--from-traces`
reads traces from it.

## Score it

Pass the dataset name where you would pass a file:

```bash
apx-agent eval run main.evals.support --judge-model databricks
apx-agent eval run main.evals.support --endpoint-url https://my-agent.<workspace>.databricksapps.com
```

An existing local path always wins: `eval run cases.json` reads the file. A
name that is not an existing path and has three dot-separated parts is read
as a dataset. `--fixtures` takes local fixture files only.

The rows reach the agent as MLflow passes them. MLflow calls the predict
function with each row's `inputs` as keyword arguments, and APX's predict
functions accept those keywords, so a row stored as
`{"inputs": {"question": "..."}}` works for in-process and endpoint runs.

## What the run records

`mlflow.genai.evaluate` logs the dataset as the run's input, with its name and
digest. APX reads that input back and tags the run with the same `apx.<kind>.*`
tags it uses for resolved Prompt Registry versions
([tracing](tracing.md#resolved-artifact-versions)):

| Tag | Value |
| --- | --- |
| `apx.eval_dataset.key` | What you passed: the UC name or the file path |
| `apx.eval_dataset.uri` | The MLflow dataset id for a managed dataset, otherwise the file path |
| `apx.eval_dataset.version` | The digest of the dataset input MLflow logged |

Local files get the same tags, so every `eval run` records what it scored.
The command prints the digest:

```text
Scored dataset main.evals.support (digest 4c1f0e2a) on run 9b2d...
```

To compare two runs, compare `apx.eval_dataset.version`. The same digest means
the same records.

These are eval-run tags only. Agent traces never carry them.

## From Python

```python
import mlflow
from apx_agent import add_eval_records, evaluate, get_eval_dataset, record_eval_dataset

add_eval_records("main.evals.support", [{"inputs": {"question": "..."}}], experiment_id="123")
dataset = get_eval_dataset("main.evals.support")
result = evaluate(agent, model="your-model-endpoint", evalset=dataset)
record_eval_dataset(result.run_id, key="main.evals.support", uri=dataset.dataset_id)
```

`add_eval_records` also takes a `mlflow.search_traces` DataFrame. The CLI calls
`record_eval_dataset` for you; from Python, call it after `evaluate`.

## Errors

Dataset calls raise `EvalDatasetError` (`apx-agent` prints it without a
traceback). The message says what to fix:

| Cause | Message |
| --- | --- |
| The table, schema, or catalog does not exist | `Eval dataset NAME not found: ... Create it with apx-agent eval dataset add NAME <evalset.jsonl>` |
| Reading is denied | `Permission denied to read ...` — needs `USE CATALOG`, `USE SCHEMA`, and `SELECT` on the table |
| Creating is denied | `Permission denied to create ...` — needs `USE CATALOG`, `USE SCHEMA`, and `CREATE TABLE` on the schema |
| Merging is denied | `Permission denied to write ...` — needs `USE CATALOG`, `USE SCHEMA`, and `MODIFY` on the table |
| Anything else | `Could not <read/create/write> eval dataset NAME: <upstream message>` |

`add` creates a dataset only when the read reports it missing. A denied read
stops there and does not try to create a table.

## Limits

- The run records the digest, not a Delta table version.
  `mlflow.genai.datasets.get_dataset` takes no version, so `eval run` always
  scores the dataset's current records.
- Creating a dataset needs an experiment. Pass `--experiment`, set
  `experiment` in `[tool.apx]`, or set `MLFLOW_EXPERIMENT_ID`.
