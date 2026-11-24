"""Eval dataset helpers: UC name detection, graceful errors, record sources (#1247)."""

import json
from types import SimpleNamespace

import pandas as pd
import pytest
import requests
from click.testing import CliRunner
from databricks.sdk.errors import NotFound, PermissionDenied
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import RESOURCE_DOES_NOT_EXIST

from apx_agent import _eval_datasets
from apx_agent._eval_datasets import (
    EvalDatasetError,
    add_eval_records,
    eval_rows_from_file,
    get_eval_dataset,
    is_dataset_name,
    traces_for_dataset,
)
from apx_agent.cli import main


@pytest.mark.parametrize("reference,expected", [
    ("main.evals.smoke", True),
    ("evals.jsonl", False),
    ("evals/cases.json", False),
    ("a.b.c.d", False),
    ("main.evals", False),
])
def test_is_dataset_name(reference, expected):
    assert is_dataset_name(reference) is expected


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} error", response=response)


def _patch_datasets(monkeypatch, *, get=None, create=None):
    import mlflow.genai.datasets as datasets

    if get is not None:
        monkeypatch.setattr(datasets, "get_dataset", get)
    if create is not None:
        monkeypatch.setattr(datasets, "create_dataset", create)


def _raise(exc):
    def fn(**kwargs):
        raise exc
    return fn


@pytest.mark.parametrize("exc", [
    MlflowException("Dataset not found.", error_code=RESOURCE_DOES_NOT_EXIST),
    NotFound("Table 'main.evals.smoke' does not exist."),
    _http_error(404),
])
def test_missing_table_says_how_to_create_it(monkeypatch, exc):
    _patch_datasets(monkeypatch, get=_raise(exc))
    with pytest.raises(EvalDatasetError, match="main.evals.smoke not found") as info:
        get_eval_dataset("main.evals.smoke")
    assert "apx-agent eval dataset add main.evals.smoke" in str(info.value)


@pytest.mark.parametrize("exc", [PermissionDenied("User lacks SELECT."), _http_error(403)])
def test_missing_grant_names_the_grants(monkeypatch, exc):
    _patch_datasets(monkeypatch, get=_raise(exc))
    with pytest.raises(EvalDatasetError, match="Permission denied to read eval dataset main.evals.smoke") as info:
        get_eval_dataset("main.evals.smoke")
    assert "SELECT on the table" in str(info.value)


class _Dataset:
    def __init__(self):
        self.merged = []

    def merge_records(self, records):
        self.merged.append(records)
        return self


def test_add_creates_a_missing_dataset_then_merges(monkeypatch):
    dataset = _Dataset()
    created = []

    def create(**kwargs):
        created.append(kwargs)
        return dataset

    _patch_datasets(monkeypatch, get=_raise(NotFound("missing")), create=create)
    rows = [{"inputs": {"question": "hi"}}]
    assert add_eval_records("main.evals.smoke", rows, experiment_id="42") is dataset
    assert created == [{"name": "main.evals.smoke", "experiment_id": "42"}]
    assert dataset.merged == [rows]


def test_add_does_not_create_when_read_is_denied(monkeypatch):
    _patch_datasets(monkeypatch, get=_raise(PermissionDenied("no")), create=lambda **k: pytest.fail("created"))
    with pytest.raises(EvalDatasetError, match="Permission denied to read"):
        add_eval_records("main.evals.smoke", [], experiment_id=None)


def test_add_create_denied_names_create_table(monkeypatch):
    _patch_datasets(monkeypatch, get=_raise(NotFound("missing")), create=_raise(PermissionDenied("no")))
    with pytest.raises(EvalDatasetError, match="CREATE TABLE on the schema"):
        add_eval_records("main.evals.smoke", [], experiment_id="42")


def test_add_merge_denied_names_modify(monkeypatch):
    class Denied:
        def merge_records(self, records):
            raise _http_error(403)

    _patch_datasets(monkeypatch, get=lambda **k: Denied())
    with pytest.raises(EvalDatasetError, match="MODIFY on the table"):
        add_eval_records("main.evals.smoke", [], experiment_id=None)


def test_other_errors_keep_the_upstream_message(monkeypatch):
    _patch_datasets(monkeypatch, get=_raise(ValueError("Invalid dataset name")))
    with pytest.raises(EvalDatasetError, match="Could not read eval dataset main.evals.smoke: Invalid dataset name"):
        get_eval_dataset("main.evals.smoke")


def test_rows_from_jsonl_unwrap_double_nested_inputs(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join([
        json.dumps({"inputs": {"question": "one"}, "expectations": {"expected_response": "1"}}),
        "",
        json.dumps({"inputs": {"inputs": {"question": "two"}}}),
        json.dumps({"inputs": {"inputs": "three"}}),
    ]))
    assert eval_rows_from_file(path) == [
        {"inputs": {"question": "one"}, "expectations": {"expected_response": "1"}},
        {"inputs": {"question": "two"}},
        {"inputs": {"inputs": "three"}},
    ]


@pytest.mark.parametrize("content", ['{"inputs": {}}', "[1, 2]", "not json"])
def test_rows_from_file_reject_non_row_lists(tmp_path, content):
    path = tmp_path / "cases.json"
    path.write_text(content)
    with pytest.raises(EvalDatasetError, match="cases.json"):
        eval_rows_from_file(path)


def _traces():
    human = {"assessment_name": "correct", "source": {"source_type": "HUMAN"}, "expectation": {"value": "yes"}}
    judge = {"assessment_name": "correct", "source": {"source_type": "LLM_JUDGE"}}
    return pd.DataFrame({"trace_id": ["t1", "t2", "t3"], "assessments": [[human], [judge], None]})


def test_labeled_keeps_traces_with_human_assessments(monkeypatch):
    calls = []

    def search(experiment, **kwargs):
        calls.append((experiment, kwargs))
        return _traces()

    monkeypatch.setattr("apx_agent._mlflow_tracing.search_traces_for_experiment", search)
    df = traces_for_dataset("42", filter_string="tags.env = 'prod'", max_results=10, labeled=True)
    assert list(df["trace_id"]) == ["t1"]
    assert calls == [("42", {"return_type": "pandas", "filter_string": "tags.env = 'prod'", "max_results": 10})]
    assert len(traces_for_dataset("42", filter_string=None, max_results=None, labeled=False)) == 3


def test_labeled_without_assessments_column_is_empty(monkeypatch):
    monkeypatch.setattr("apx_agent._mlflow_tracing.search_traces_for_experiment",
                        lambda experiment, **kwargs: pd.DataFrame({"trace_id": ["t1"]}))
    assert len(traces_for_dataset("42", filter_string=None, max_results=None, labeled=True)) == 0


# --- CLI ---------------------------------------------------------------------


def test_run_rejects_a_reference_that_is_neither_file_nor_dataset():
    result = CliRunner().invoke(main, ["eval", "run", "missing.jsonl"])
    assert result.exit_code == 2
    assert "neither an existing file nor a catalog.schema.table" in result.output


def test_run_reports_a_missing_dataset_without_traceback(monkeypatch):
    _patch_datasets(monkeypatch, get=_raise(NotFound("Table does not exist.")))
    result = CliRunner().invoke(main, ["eval", "run", "main.evals.smoke", "--endpoint-url", "https://x.test", "--token", "t"])
    assert result.exit_code == 1
    assert "Eval dataset main.evals.smoke not found" in result.output
    assert "Traceback" not in result.output


def test_run_rejects_fixtures_with_a_dataset():
    result = CliRunner().invoke(main, ["eval", "run", "main.evals.smoke", "--fixtures"])
    assert result.exit_code == 2
    assert "--fixtures reads a local fixtures file" in result.output


def test_run_passes_the_dataset_and_records_it(monkeypatch):
    dataset = SimpleNamespace(dataset_id="d-123")
    _patch_datasets(monkeypatch, get=lambda **k: dataset)
    monkeypatch.setattr("apx_agent.cli._read_apx_agent_config", lambda path=None: {})
    seen = []
    monkeypatch.setattr("apx_agent.eval_against_endpoint", lambda url, data, **k: seen.append(data) or SimpleNamespace(run_id="r1"))
    recorded = []
    monkeypatch.setattr(_eval_datasets, "record_eval_dataset", lambda run_id, **k: recorded.append((run_id, k)))
    result = CliRunner().invoke(main, ["eval", "run", "main.evals.smoke", "--endpoint-url", "https://x.test", "--token", "t"])
    assert result.exit_code == 0, result.output
    assert seen == [dataset]
    assert recorded == [("r1", {"key": "main.evals.smoke", "uri": "d-123"})]


def test_dataset_add_needs_a_source():
    result = CliRunner().invoke(main, ["eval", "dataset", "add", "main.evals.smoke"])
    assert result.exit_code == 2
    assert "at least one EVALSET file or --from-traces" in result.output


def test_dataset_add_rejects_non_uc_names(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text("[]")
    result = CliRunner().invoke(main, ["eval", "dataset", "add", "smoke", str(path)])
    assert result.exit_code == 2
    assert "not a catalog.schema.table name" in result.output


def test_dataset_add_trace_options_need_from_traces(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text("[]")
    result = CliRunner().invoke(main, ["eval", "dataset", "add", "main.evals.smoke", str(path), "--labeled"])
    assert result.exit_code == 2
    assert "need --from-traces" in result.output


def test_dataset_add_from_traces_needs_an_experiment(monkeypatch):
    monkeypatch.setattr("apx_agent.cli._read_apx_agent_config", lambda path=None: {})
    result = CliRunner().invoke(main, ["eval", "dataset", "add", "main.evals.smoke", "--from-traces"])
    assert result.exit_code == 2
    assert "--from-traces needs --experiment" in result.output


def test_dataset_add_merges_files_and_labeled_traces(monkeypatch, tmp_path):
    monkeypatch.setattr("apx_agent.cli._read_apx_agent_config", lambda path=None: {"experiment": "/Shared/agent"})
    monkeypatch.setattr("apx_agent._mlflow_tracing._resolve_experiment_id", lambda experiment: "42")
    monkeypatch.setattr("apx_agent._mlflow_tracing.search_traces_for_experiment", lambda experiment, **kwargs: _traces())
    merged = []
    monkeypatch.setattr(_eval_datasets, "add_eval_records",
                        lambda name, records, *, experiment_id: merged.append((name, records, experiment_id)))
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"inputs": {"question": "one"}}))
    result = CliRunner().invoke(main, ["eval", "dataset", "add", "main.evals.smoke", str(path), "--from-traces", "--labeled"])
    assert result.exit_code == 0, result.output
    assert merged[0] == ("main.evals.smoke", [{"inputs": {"question": "one"}}], "42")
    assert list(merged[1][1]["trace_id"]) == ["t1"]
    assert "Merged 1 traces from experiment /Shared/agent into main.evals.smoke." in result.output


def test_dataset_add_with_no_matching_traces_fails(monkeypatch):
    monkeypatch.setattr("apx_agent._mlflow_tracing._resolve_experiment_id", lambda experiment: "42")
    monkeypatch.setattr("apx_agent._mlflow_tracing.search_traces_for_experiment",
                        lambda experiment, **kwargs: pd.DataFrame({"trace_id": []}))
    monkeypatch.setattr(_eval_datasets, "add_eval_records", lambda *a, **k: pytest.fail("merged"))
    result = CliRunner().invoke(main, ["eval", "dataset", "add", "main.evals.smoke", "--from-traces", "--experiment", "42"])
    assert result.exit_code == 1
    assert "No traces matched" in result.output


def test_endpoint_predict_takes_dataset_fields_as_keywords(monkeypatch):
    """mlflow calls predict_fn(**row_inputs); a {"question": ...} row arrives as a keyword."""
    import io

    from apx_agent._eval import endpoint_predict_fn

    bodies = []

    def urlopen(req, timeout):
        bodies.append(json.loads(req.data))
        return io.BytesIO(json.dumps({"output": [{"content": [{"text": "glue"}]}]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    predict = endpoint_predict_fn("https://x.test", token="t", stream=False)
    assert predict(question="What is apx?") == "glue"
    assert predict({"question": "Positional?"}) == "glue"
    assert [body["input"][0]["content"] for body in bodies] == ["What is apx?", "Positional?"]
