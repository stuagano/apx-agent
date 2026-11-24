"""An eval run scores a managed dataset and records it — on a real MLflow store (#1247).

Uses MLflow's own SQL tracking store (SQLite in tmp_path), where
``mlflow.genai.datasets`` runs without Databricks. Only the agent's model is
faked: the CLI builds the dataset, ``eval run`` resolves it by name,
``mlflow.genai.evaluate`` scores it through apx's in-process predict, and the
run tags are read back from the store.
"""

import json
from types import SimpleNamespace

import mlflow
import pytest
from click.testing import CliRunner
from mlflow import MlflowClient
from mlflow.genai.scorers import scorer

from apx_agent import Agent, _eval
from apx_agent._audit import AuditAttrs
from apx_agent.cli import main

NAME = "main.evals.smoke"


@scorer
def answered(outputs) -> bool:
    return bool(outputs)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    monkeypatch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
    experiment_id = mlflow.create_experiment("datasets")
    mlflow.set_experiment(experiment_id=experiment_id)
    yield experiment_id
    mlflow.set_tracking_uri(None)


def test_run_scores_a_managed_dataset_and_tags_its_digest(store, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cases = tmp_path / "cases.jsonl"
    cases.write_text("\n".join(json.dumps(row) for row in [
        {"inputs": {"question": "What is apx?"}, "expectations": {"expected_response": "glue"}},
        {"inputs": {"inputs": {"question": "Who owns pins?"}}},
    ]))
    runner = CliRunner()
    added = runner.invoke(main, ["eval", "dataset", "add", NAME, str(cases), "--experiment", "datasets"])
    assert added.exit_code == 0, added.output
    assert f"Merged 2 records from {cases} into {NAME}." in added.output

    asked = []

    class ChatAgent:
        def predict(self, messages, custom_inputs=None):
            asked.append(messages[-1].content)
            return SimpleNamespace(messages=[SimpleNamespace(role="assistant", content="an answer")])

    monkeypatch.setattr("apx_agent.cli._read_apx_agent_config", lambda path=None: {"model": "fake"})
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda module: Agent())
    monkeypatch.setattr(_eval, "compile_to_chat_agent", lambda agent, model: ChatAgent())
    monkeypatch.setattr(_eval, "_default_scorers", lambda judge_model=None: [answered])

    ran = runner.invoke(main, ["eval", "run", NAME, "--experiment", "datasets"])
    assert ran.exit_code == 0, ran.output
    # Rows stored single-nested reach the agent as fields, not as a missing ``inputs`` arg.
    # (mlflow also calls predict once on the first row to check tracing.)
    assert set(asked) == {"What is apx?", "Who owns pins?"}

    dataset = mlflow.genai.datasets.get_dataset(name=NAME)
    runs = MlflowClient().search_runs([store])
    assert len(runs) == 1
    run = runs[0]
    logged = run.inputs.dataset_inputs[0].dataset
    assert logged.name == NAME
    assert run.data.tags[AuditAttrs.EVAL_DATASET_KEY] == NAME
    assert run.data.tags[AuditAttrs.EVAL_DATASET_URI] == dataset.dataset_id
    assert run.data.tags[AuditAttrs.EVAL_DATASET_VERSION] == logged.digest
    assert logged.digest
    assert f"Scored dataset {NAME} (digest {logged.digest}) on run {run.info.run_id}." in ran.output
