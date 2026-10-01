"""Sweep comparisons must not turn missing telemetry into good measurements."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pandas as pd
import pytest
from click.testing import CliRunner
from ctk import Artifact, verify
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from apx_agent import Agent, SequentialAgent, _compile, evaluate_sweep
from apx_agent.cli import main


def trace(name, duration, cost, state="OK"):
    return SimpleNamespace(
        info=SimpleNamespace(trace_id=name, execution_duration=duration,
                             cost={"total_cost": cost}, state=state),
        data=SimpleNamespace(spans=[SimpleNamespace(
            span_type="LLM", attributes={"mlflow.llm.cost": {"total_cost": cost}},
        )]),
    )


def test_live_sweep_preserves_metrics_identity_and_missing_cost(monkeypatch):
    from apx_agent import _eval_sweep

    rows = [{"inputs": {"question": "a"}}, {"inputs": {"question": "b"}}]
    calls = []

    def evaluate(agent, **kwargs):
        calls.append(kwargs)
        assert kwargs["evalset"] == rows
        kwargs["evalset"][0]["inputs"]["question"] = "mutated"
        costs = [0.1, 0.2] if kwargs["model"] == "first" else [0.1, None]
        return SimpleNamespace(metrics={"correctness/mean": 0.5}, run_id=kwargs["model"],
                               result_df=pd.DataFrame({"trace": [trace(str(i), d, c) for i, (d, c) in enumerate(zip([0, 900], costs))]}))

    monkeypatch.setattr(_eval_sweep, "evaluate", evaluate)
    report = evaluate_sweep(Agent(), models=["first", "second"], evalset=rows,
                            scorers=[], user_token="test-token", workspace_host="https://example.test", experiment="test")
    first, second = report
    assert first.metrics == {"correctness/mean": 0.5}
    assert first.latency_p50_ms == 0 and first.latency_p95_ms == 900
    assert first.cost_usd == pytest.approx(0.3)
    assert first.cost_cases == 2 and second.cost_cases == 1
    assert second.cost_usd is None
    assert first.case_count == first.latency_cases == 2
    assert not first.errors and not second.errors
    assert rows[0]["inputs"]["question"] == "a"
    assert all(call["user_token"] == "test-token" and call["scorers"] == [] for call in calls)


@pytest.mark.parametrize("models", [[], ["a"], ["a", "a"], ["a", " "], "ab", ["a", 2]])
def test_invalid_models_fail_before_execution(models):
    with pytest.raises(ValueError, match="models"):
        evaluate_sweep(Agent(), models=models, evalset=[{"inputs": {"question": "hi"}}])


def test_live_sweep_rejects_hot_swap_that_would_run_one_model_twice(monkeypatch):
    from apx_agent import _eval_sweep

    monkeypatch.setenv("APX_AGENT_MODEL_OVERRIDE", "pinned-model")

    def must_not_run(*args, **kwargs):
        pytest.fail("An overridden model must never run under the candidate's name")

    monkeypatch.setattr(_eval_sweep, "evaluate", must_not_run)
    with pytest.raises(ValueError, match="APX_AGENT_MODEL_OVERRIDE"):
        evaluate_sweep(Agent(), models=["a", "b"], evalset=[{"inputs": {}}])


@pytest.mark.parametrize("data", [[], [{"inputs": {}, "outputs": "cached"}], [{"inputs": {}, "trace": "old"}], [{"question": "missing inputs"}]])
def test_invalid_evalsets_fail_before_execution(data):
    with pytest.raises(ValueError, match="evalset"):
        evaluate_sweep(Agent(), models=["a", "b"], evalset=data)


def test_failure_continues_but_cancellation_propagates(monkeypatch):
    from apx_agent import _eval_sweep

    calls = []

    def fail(agent, **kwargs):
        calls.append(kwargs["model"])
        raise RuntimeError("secret must not enter report")

    monkeypatch.setattr(_eval_sweep, "evaluate", fail)
    report = evaluate_sweep(Agent(), models=["a", "b"], evalset=[{"inputs": {}}])
    assert calls == ["a", "b"]
    assert all(row.errors == ("Evaluation failed (RuntimeError)",) for row in report)
    assert "secret" not in json.dumps([asdict(row) for row in report])

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(_eval_sweep, "evaluate", cancel)
    with pytest.raises(KeyboardInterrupt):
        evaluate_sweep(Agent(), models=["a", "b"], evalset=[{"inputs": {}}])


@pytest.mark.parametrize("traces", [[], [None, None], [trace("same", 20, 1), trace("same", 20, 1)], [trace("a", 20, 1, "ERROR"), trace("b", 40, 1)]])
def test_incomplete_and_failed_cases_are_visible(monkeypatch, traces):
    from apx_agent import _eval_sweep

    monkeypatch.setattr(_eval_sweep, "evaluate", lambda *a, **k: SimpleNamespace(
        metrics={}, run_id="run", result_df=pd.DataFrame({"trace": traces}),
    ))
    report = evaluate_sweep(Agent(), models=["a", "b"], evalset=[{"inputs": {}}, {"inputs": {}}])
    assert all(row.errors for row in report)


def test_fixture_sweep_runs_real_graph_and_cli_reads_back_report(monkeypatch, tmp_path):
    monkeypatch.setenv("APX_AGENT_MODEL_OVERRIDE", "unused-by-fixtures")

    class Output(BaseModel):
        answer: int

    agent = SequentialAgent([Agent(name="leaf", output_schema=Output)], name="root")
    fixtures = [{"id": "one", "request": "answer", "expected_outcome": "correct",
                 "steps": [{"path": ["root", "leaf"], "tools": [], "expected_output": {"answer": 1}}]}]
    endpoints = []

    def model(endpoint, **kwargs):
        endpoints.append(endpoint)
        return GenericFakeChatModel(messages=iter([AIMessage(content='{"answer":1}' if endpoint == "good" else '{"answer":2}')]))

    monkeypatch.setattr(_compile, "_build_chat_databricks", model)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda module: agent)
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(fixtures))
    result = CliRunner().invoke(main, ["eval", "sweep", str(path), "--fixtures", "--model", "good", "--model", "bad", "--format", "json"])
    assert result.exit_code == 0, result.output
    saved = tmp_path / "comparison.json"
    saved.write_text(result.output)
    verify(Artifact(str(saved), is_json=True, must_contain='"wrong": 1'))
    report = json.loads(saved.read_text())
    assert endpoints == ["good", "bad"]
    assert report[0]["outcome_counts"] == {"correct": 1, "escalated_with_evidence": 0, "wrong": 0}
    assert report[1]["outcome_counts"]["wrong"] == 1
    assert all(row["cost_usd"] is None and row["latency_cases"] == 1 for row in report)


def test_cli_failure_prints_comparison_and_exits_nonzero(monkeypatch, tmp_path):
    from apx_agent import _eval_sweep

    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda module: Agent())

    def fail(*args, **kwargs):
        raise RuntimeError("private")

    monkeypatch.setattr(_eval_sweep, "evaluate", fail)
    path = tmp_path / "evalset.json"
    path.write_text('[{"inputs": {}}]')
    result = CliRunner().invoke(main, ["eval", "sweep", str(path), "--model", "a", "--model", "b"])
    assert result.exit_code == 1
    assert "unavailable" in result.output and "RuntimeError" in result.output
    assert "private" not in result.output


@pytest.mark.parametrize("bad_cost", [None, -1, float("nan"), float("inf"), True])
def test_partial_span_pricing_never_looks_like_total_cost(monkeypatch, bad_cost):
    from apx_agent import _eval_sweep

    value = trace("priced", 10, 0.4)
    value.data.spans.append(SimpleNamespace(span_type="LLM", attributes={"mlflow.llm.cost": {"total_cost": bad_cost}}))
    monkeypatch.setattr(_eval_sweep, "evaluate", lambda *a, **k: SimpleNamespace(
        metrics={}, run_id="run", result_df=pd.DataFrame({"trace": [value]}),
    ))
    report = evaluate_sweep(Agent(), models=["a", "b"], evalset=[{"inputs": {}}])
    assert all(row.cost_usd is None and row.cost_cases == 0 for row in report)


def test_missing_latency_is_explicit_and_invalid_score_is_preserved(monkeypatch):
    from apx_agent import _eval_sweep

    monkeypatch.setattr(_eval_sweep, "evaluate", lambda *a, **k: SimpleNamespace(
        metrics={"score/mean": float("nan")}, run_id="run",
        result_df=pd.DataFrame({"trace": [trace("one", None, 0)]}),
    ))
    report = evaluate_sweep(Agent(), models=["a", "b"], evalset=[{"inputs": {}}])
    assert report[0].metrics == {"score/mean": None}
    assert report[0].latency_p50_ms is None and report[0].latency_cases == 0
    assert report[0].cost_usd == 0
    assert report[0].errors


def test_cli_json_keeps_evaluator_progress_on_stderr(monkeypatch, tmp_path):
    from apx_agent import _eval_sweep

    def evaluate(*args, **kwargs):
        print("MLflow progress")
        return SimpleNamespace(metrics={}, run_id="run", result_df=pd.DataFrame({"trace": [trace("one", 0, 0)]}))

    monkeypatch.setattr(_eval_sweep, "evaluate", evaluate)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda module: Agent())
    path = tmp_path / "evalset.json"
    path.write_text('[{"inputs": {}}]')
    result = CliRunner().invoke(main, ["eval", "sweep", str(path), "--model", "a", "--model", "b", "--format", "json"])
    assert result.exit_code == 0
    assert len(json.loads(result.stdout)) == 2
    assert "MLflow progress" in result.stderr


def test_partial_judge_errors_do_not_masquerade_as_complete_scores(monkeypatch):
    from apx_agent import _eval_sweep

    monkeypatch.setattr(_eval_sweep, "evaluate", lambda *a, **k: SimpleNamespace(
        metrics={"score/mean": 1}, run_id="run", result_df=pd.DataFrame({
            "trace": [trace("a", 10, 1), trace("b", 20, 1)],
            "score/error_message": [None, "private judge failure"],
        }),
    ))
    report = evaluate_sweep(Agent(), models=["a", "b"], evalset=[{"inputs": {}}, {"inputs": {}}])
    assert report[0].errors == ("Case 2: scorer score failed",)


@pytest.mark.parametrize("options", [
    {}, {"evalset": [{"inputs": {}}], "fixtures": []},
    {"fixtures": [], "judge_model": "judge"}, {"fixtures": [], "user_token": "test"},
    {"fixtures": [], "scorers": []}, {"fixtures": [], "experiment": "exp"},
])
def test_conflicting_inputs_fail_before_execution(options):
    with pytest.raises(ValueError):
        evaluate_sweep(Agent(), models=["a", "b"], **options)


def test_real_mlflow_evaluation_consumes_native_serialized_traces(monkeypatch, tmp_path):
    import mlflow
    from mlflow.genai.scorers import scorer
    from mlflow.types.agent import ChatAgentMessage, ChatAgentResponse

    from apx_agent import _eval

    endpoints = []

    def compile_agent(agent, *, model):
        endpoints.append(model)

        @mlflow.trace(span_type="AGENT")
        def predict(messages, custom_inputs=None):
            with mlflow.start_span(name="model", span_type="LLM") as span:
                span.set_attribute("mlflow.llm.cost", {"input_cost": 0.1, "output_cost": 0.2, "total_cost": 0.3})
            return ChatAgentResponse(messages=[ChatAgentMessage(role="assistant", content=model, id="answer")])

        return SimpleNamespace(predict=predict)

    @scorer
    def selected(outputs):
        return outputs == "first"

    monkeypatch.setattr(_eval, "compile_to_chat_agent", compile_agent)
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    # set_experiment also writes this environment variable for child processes.
    monkeypatch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)
    old_uri = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
    try:
        mlflow.tracing.enable()
        report = evaluate_sweep(Agent(), models=["first", "second"],
                                evalset=[{"inputs": {"question": "choose"}}],
                                scorers=[selected], experiment="sweep-integration")
    finally:
        mlflow.set_tracking_uri(old_uri)
    assert endpoints == ["first", "second"]
    assert not report[0].errors and not report[1].errors
    assert report[0].metrics["selected/mean"] == 1
    assert report[1].metrics["selected/mean"] == 0
    assert all(row.latency_cases == 1 and row.latency_p50_ms is not None for row in report)
    assert all(row.cost_usd == pytest.approx(0.3) for row in report)
