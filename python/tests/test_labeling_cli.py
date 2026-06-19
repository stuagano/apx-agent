import json
import pytest
from types import SimpleNamespace
from click.testing import CliRunner
from apx_agent import cli, _labeling


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _stub_mlflow_setup(monkeypatch):
    # The label commands point MLflow/databricks-agents at the workspace; no-op
    # that in unit tests so they never touch real tracking/auth.
    monkeypatch.setattr(cli, "_label_mlflow_setup", lambda profile: None)


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
                            schema_name="j"))
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
    res = runner.invoke(cli.main, ["label", "start", "--uc-name", "c.s.x", "--judge", "j", "--scale", "1-5"])
    assert res.exit_code != 0
    assert "exactly one" in res.output.lower() or "no agent" in res.output.lower()


@pytest.mark.unit
def test_label_start_errors_when_multiple_agents(runner, monkeypatch):
    a1 = SimpleNamespace(uc_name="c.s.a1", name="a1", tags={"apx.mlflow.experiment_id": "123"})
    a2 = SimpleNamespace(uc_name="c.s.a2", name="a2", tags={"apx.mlflow.experiment_id": "456"})
    monkeypatch.setattr(cli, "_connect_workspace", lambda p: (object(), object()))
    monkeypatch.setattr(cli, "_fleet_resolve", lambda ws, **kw: [a1, a2])
    res = runner.invoke(cli.main, ["label", "start", "--uc-name", "c.s.x", "--judge", "j", "--scale", "1-5"])
    assert res.exit_code != 0
    assert "exactly one" in res.output.lower()


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


@pytest.mark.unit
def test_label_start_explicit_target_no_fleet(runner, monkeypatch):
    # Apps-deployed agents aren't fleet-discoverable: --agent-name + --experiment
    # targets the agent directly, with NO fleet resolution.
    called: dict = {}
    monkeypatch.setattr(cli, "_fleet_resolve",
                        lambda *a, **k: (called.setdefault("fleet", True), [])[1])
    monkeypatch.setattr(_labeling, "start_session",
                        lambda **kw: (called.update(kw), _labeling.StartResult(
                            run_id="r", session_url="https://x/sme", trace_count=3,
                            schema_name="j"))[1])
    res = runner.invoke(cli.main, [
        "label", "start", "--agent-name", "payroll-coworker", "--experiment", "999",
        "--judge", "j", "--scale", "1-5", "--format", "json",
    ])
    assert res.exit_code == 0, res.output
    assert "fleet" not in called, "explicit target must not invoke fleet resolution"
    assert called["experiment_id"] == "999"
    assert called["agent_name"] == "payroll-coworker"


@pytest.mark.unit
def test_label_start_explicit_requires_experiment(runner):
    res = runner.invoke(cli.main, [
        "label", "start", "--agent-name", "x", "--judge", "j", "--scale", "1-5"])
    assert res.exit_code != 0
    assert "experiment" in res.output.lower()


@pytest.mark.unit
def test_label_align_explicit_target_no_fleet(runner, monkeypatch):
    called: dict = {}
    monkeypatch.setattr(cli, "_fleet_resolve",
                        lambda *a, **k: (called.setdefault("fleet", True), [])[1])
    monkeypatch.setattr(_labeling, "align_judge",
                        lambda **kw: (called.update(kw), _labeling.AlignResult(
                            judge_name="j", guidelines=[], registered_as="j"))[1])
    res = runner.invoke(cli.main, [
        "label", "align", "--experiment", "999",
        "--judge", "j", "--run", "r1", "--format", "json"])
    assert res.exit_code == 0, res.output
    assert "fleet" not in called, "explicit align target must not invoke fleet resolution"
    assert called["experiment_id"] == "999"
