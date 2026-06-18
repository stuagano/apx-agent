import json
import pytest
from types import SimpleNamespace
from click.testing import CliRunner
from apx_agent import cli, _labeling


@pytest.fixture
def runner():
    return CliRunner()


@pytest.mark.unit
def test_label_start_happy_path(runner, monkeypatch):
    agent = SimpleNamespace(uc_name="c.s.payroll", name="payroll",
                            tags={"apx.mlflow.experiment_id": "123"})
    monkeypatch.setattr(cli, "_connect_workspace", lambda p: (object(), object()))
    monkeypatch.setattr(cli, "_fleet_resolve", lambda ws, **kw: [agent])
    monkeypatch.setattr(_labeling, "start_session",
                        lambda **kw: _labeling.StartResult(
                            run_id="payroll_j-20260617T000000Z",
                            session_url="https://x/sme", trace_count=5,
                            dataset_name="payroll_label_x", schema_name="j"))
    res = runner.invoke(cli.main, [
        "label", "start", "--uc-name", "c.s.payroll",
        "--judge", "j", "--scale", "1-5", "--format", "json",
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["session_url"] == "https://x/sme"
    assert payload["run_id"] == "payroll_j-20260617T000000Z"


@pytest.mark.unit
def test_label_start_errors_when_not_one_agent(runner, monkeypatch):
    monkeypatch.setattr(cli, "_connect_workspace", lambda p: (object(), object()))
    monkeypatch.setattr(cli, "_fleet_resolve", lambda ws, **kw: [])
    res = runner.invoke(cli.main, ["label", "start", "--judge", "j", "--scale", "1-5"])
    assert res.exit_code != 0
    assert "exactly one" in res.output.lower() or "no agent" in res.output.lower()


@pytest.mark.unit
def test_label_align_happy_path(runner, monkeypatch):
    agent = SimpleNamespace(uc_name="c.s.payroll", name="payroll",
                            tags={"apx.mlflow.experiment_id": "123"})
    monkeypatch.setattr(cli, "_connect_workspace", lambda p: (object(), object()))
    monkeypatch.setattr(cli, "_fleet_resolve", lambda ws, **kw: [agent])
    monkeypatch.setattr(_labeling, "align_judge",
                        lambda **kw: _labeling.AlignResult(
                            judge_name="j", guidelines=["be precise"], registered_as="j"))
    res = runner.invoke(cli.main, [
        "label", "align", "--uc-name", "c.s.payroll",
        "--judge", "j", "--run", "payroll_j-20260617T000000Z", "--format", "json",
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["registered_as"] == "j"
    assert payload["guidelines"] == ["be precise"]
