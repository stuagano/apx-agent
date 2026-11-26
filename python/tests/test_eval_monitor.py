"""`apx-agent eval monitor` — mocked MLflow production-monitoring lifecycle (#1271).

No live workspace: every mlflow entry point is monkeypatched on the helper module.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from mlflow.genai.scorers import ScorerSamplingConfig
from mlflow.genai.scorers.base import ScorerStatus

from apx_agent import _eval_monitor, cli
from apx_agent._doctor import _TRACE_DESTINATION_TAG as TRACE_TAG

UC_DEST = "main.agents.claims"


@dataclass
class FakeScorer:
    name: str
    sample_rate: float | None = None
    filter_string: str | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @property
    def status(self) -> ScorerStatus:
        return ScorerStatus.STARTED if (self.sample_rate or 0) > 0 else ScorerStatus.STOPPED

    def register(self, **kw: Any) -> FakeScorer:
        self.calls.append(("register", kw))
        return FakeScorer(name=kw["name"], calls=self.calls)

    def start(self, **kw: Any) -> FakeScorer:
        self.calls.append(("start", kw))
        cfg = kw["sampling_config"]
        return FakeScorer(self.name, cfg.sample_rate, cfg.filter_string, self.calls)

    def stop(self, **kw: Any) -> FakeScorer:
        self.calls.append(("stop", kw))
        return FakeScorer(self.name, 0.0, self.filter_string, self.calls)


@pytest.fixture
def workspace(monkeypatch):
    """Patch the helper module's mlflow surface; return the mutable fake state."""
    state = SimpleNamespace(
        tags={}, registered={"quality-v2": FakeScorer("quality-v2", 0.0)},
        warehouse_calls=[], align_runs=[SimpleNamespace(registered_as="quality-v2")],
    )
    monkeypatch.setattr(_eval_monitor, "_mlflow",
                        SimpleNamespace(get_experiment=lambda eid: SimpleNamespace(tags=dict(state.tags))))
    monkeypatch.setattr(_eval_monitor, "_resolve_experiment_id", lambda ref: "123" if ref else None)
    monkeypatch.setattr(_eval_monitor, "list_scorers",
                        lambda *, experiment_id: list(state.registered.values()))

    def fake_get_scorer(*, name, experiment_id):
        if name not in state.registered:
            raise RuntimeError(f"Scorer {name} not found")
        return state.registered[name]

    monkeypatch.setattr(_eval_monitor, "get_scorer", fake_get_scorer)
    monkeypatch.setattr(_eval_monitor, "set_databricks_monitoring_sql_warehouse_id",
                        lambda wid, *, experiment_id: state.warehouse_calls.append((wid, experiment_id)))
    monkeypatch.setattr(_eval_monitor, "list_align_runs",
                        lambda *, experiment_id, max_results: state.align_runs[:max_results])

    def forbid_delete(**kw):
        raise AssertionError("eval monitor must never delete a scorer")

    import mlflow.genai.scorers as scorers_mod
    monkeypatch.setattr(scorers_mod, "delete_scorer", forbid_delete)
    monkeypatch.setattr(cli, "_label_mlflow_setup", lambda profile: None)
    monkeypatch.setattr(cli, "_read_apx_agent_config", lambda *a: {"experiment": "/Shared/claims"})
    return state


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(cli.main, ["eval", "monitor", *args])


# --- helper ---------------------------------------------------------------


@pytest.mark.unit
def test_start_registered_scorer_passes_sampling_config(workspace):
    report = _eval_monitor.start(
        experiment_id="123", scorer_name="quality-v2", sample_rate=0.1,
        filter_string="attributes.status = 'OK'", warehouse_id=None, scorer_obj=None,
    )
    kind, kw = workspace.registered["quality-v2"].calls[-1]
    assert kind == "start"
    assert kw["experiment_id"] == "123" and kw["name"] == "quality-v2"
    assert kw["sampling_config"] == ScorerSamplingConfig(sample_rate=0.1, filter_string="attributes.status = 'OK'")
    assert report.scorers[0].status == "STARTED" and report.scorers[0].sample_rate == 0.1


@pytest.mark.unit
def test_start_registers_code_scorer_when_missing(workspace):
    code = FakeScorer("fresh")
    report = _eval_monitor.start(
        experiment_id="123", scorer_name="fresh", sample_rate=0.5,
        filter_string=None, warehouse_id=None, scorer_obj=code,
    )
    assert [c[0] for c in code.calls] == ["register", "start"]
    assert code.calls[0][1] == {"name": "fresh", "experiment_id": "123"}
    assert report.scorers[0].name == "fresh"


@pytest.mark.unit
def test_start_skips_register_when_already_registered(workspace):
    code = FakeScorer("quality-v2")
    _eval_monitor.start(
        experiment_id="123", scorer_name="quality-v2", sample_rate=0.5,
        filter_string=None, warehouse_id=None, scorer_obj=code,
    )
    assert code.calls == []
    assert workspace.registered["quality-v2"].calls[-1][0] == "start"


@pytest.mark.unit
def test_start_unregistered_without_code_scorer_explains(workspace):
    with pytest.raises(_eval_monitor.MonitorError, match="not registered.*label align"):
        _eval_monitor.start(
            experiment_id="123", scorer_name="ghost", sample_rate=0.5,
            filter_string=None, warehouse_id=None, scorer_obj=None,
        )


@pytest.mark.unit
def test_start_uc_traces_require_warehouse(workspace):
    workspace.tags = {TRACE_TAG: UC_DEST}
    with pytest.raises(_eval_monitor.MonitorError) as exc:
        _eval_monitor.start(
            experiment_id="123", scorer_name="quality-v2", sample_rate=0.1,
            filter_string=None, warehouse_id=None, scorer_obj=None,
        )
    msg = str(exc.value)
    assert UC_DEST in msg and "--warehouse-id" in msg and "CAN USE" in msg and "CAN EDIT" in msg
    assert workspace.registered["quality-v2"].calls == []


@pytest.mark.unit
def test_start_sets_warehouse_before_starting(workspace):
    workspace.tags = {TRACE_TAG: UC_DEST}
    report = _eval_monitor.start(
        experiment_id="123", scorer_name="quality-v2", sample_rate=0.1,
        filter_string=None, warehouse_id="wh-1", scorer_obj=None,
    )
    assert workspace.warehouse_calls == [("wh-1", "123")]
    assert report.warehouse_id == "wh-1" and report.trace_destination == UC_DEST


@pytest.mark.unit
def test_start_uc_traces_with_existing_warehouse_tag(workspace):
    workspace.tags = {TRACE_TAG: UC_DEST, _eval_monitor.WAREHOUSE_TAG: "wh-0"}
    report = _eval_monitor.start(
        experiment_id="123", scorer_name="quality-v2", sample_rate=0.1,
        filter_string=None, warehouse_id=None, scorer_obj=None,
    )
    assert workspace.warehouse_calls == []
    assert report.warehouse_id == "wh-0"


@pytest.mark.unit
def test_stop_sets_rate_zero_and_keeps_scorer(workspace):
    report = _eval_monitor.stop(experiment_id="123", scorer_name="quality-v2")
    assert workspace.registered["quality-v2"].calls[-1] == ("stop", {"name": "quality-v2", "experiment_id": "123"})
    assert report.scorers[0].status == "STOPPED"
    assert "quality-v2" in workspace.registered


@pytest.mark.unit
def test_status_lists_all_or_one(workspace):
    workspace.registered["other"] = FakeScorer("other", 0.25, "tags.env = 'prod'")
    assert [s.name for s in _eval_monitor.status(experiment_id="123", scorer_name=None).scorers] == ["quality-v2", "other"]
    one = _eval_monitor.status(experiment_id="123", scorer_name="other").scorers
    assert len(one) == 1 and one[0].status == "STARTED" and one[0].filter_string == "tags.env = 'prod'"
    with pytest.raises(_eval_monitor.MonitorError, match="not registered"):
        _eval_monitor.status(experiment_id="123", scorer_name="ghost")


@pytest.mark.unit
def test_default_scorer_from_newest_align_run(workspace):
    assert _eval_monitor.default_scorer_name("123") == "quality-v2"
    workspace.align_runs = []
    with pytest.raises(_eval_monitor.MonitorError, match="--scorer"):
        _eval_monitor.default_scorer_name("123")


@pytest.mark.unit
def test_resolve_experiment_falls_back_to_env(monkeypatch):
    seen = []
    monkeypatch.setattr(_eval_monitor, "_resolve_experiment_id", lambda ref: seen.append(ref) or "9")
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "9")
    assert _eval_monitor.resolve_experiment(None) == "9"
    assert _eval_monitor.resolve_experiment("/Shared/x") == "9"
    assert seen == ["9", "/Shared/x"]


@pytest.mark.unit
def test_resolve_experiment_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(_eval_monitor.MonitorError, match="--experiment"):
        _eval_monitor.resolve_experiment(None)


@pytest.mark.unit
def test_load_scorer_imports_real_scorer(monkeypatch, tmp_path):
    (tmp_path / "judges_mod_1271.py").write_text(
        "from mlflow.genai.scorers import scorer\n\n"
        "@scorer\ndef long_enough(outputs) -> bool:\n    return len(str(outputs)) > 10\n"
        "not_a_scorer = 3\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert _eval_monitor.load_scorer("judges_mod_1271:long_enough").name == "long_enough"
    with pytest.raises(_eval_monitor.MonitorError, match="not an mlflow.genai Scorer"):
        _eval_monitor.load_scorer("judges_mod_1271:not_a_scorer")
    with pytest.raises(_eval_monitor.MonitorError, match="module:attr"):
        _eval_monitor.load_scorer("judges_mod_1271")


# --- CLI --------------------------------------------------------------------


@pytest.mark.unit
def test_cli_start_defaults_to_aligned_judge_json(workspace):
    res = _invoke("start", "--sample-rate", "0.1", "--filter", "attributes.status = 'OK'", "--format", "json")
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["experiment_id"] == "123"
    assert payload["scorers"] == [{
        "name": "quality-v2", "status": "STARTED", "sample_rate": 0.1,
        "filter_string": "attributes.status = 'OK'",
    }]


@pytest.mark.unit
def test_cli_start_text_mentions_delay_and_warehouse(workspace):
    workspace.tags = {TRACE_TAG: UC_DEST}
    res = _invoke("start", "--scorer", "quality-v2", "--sample-rate", "0.2", "--warehouse-id", "wh-9")
    assert res.exit_code == 0, res.output
    assert workspace.warehouse_calls == [("wh-9", "123")]
    assert "quality-v2: STARTED  sample_rate=0.2" in res.output
    assert UC_DEST in res.output and "15-20 minutes" in res.output


@pytest.mark.unit
def test_cli_start_uc_without_warehouse_fails_with_guidance(workspace):
    workspace.tags = {TRACE_TAG: UC_DEST}
    res = _invoke("start", "--sample-rate", "0.1")
    assert res.exit_code != 0
    assert "--warehouse-id" in res.output and "CAN USE" in res.output


@pytest.mark.unit
@pytest.mark.parametrize("rate", ["0", "-0.1", "1.5"])
def test_cli_start_rejects_out_of_range_sample_rate(workspace, rate):
    res = _invoke("start", "--sample-rate", rate)
    assert res.exit_code == 2
    assert "--sample-rate" in res.output


@pytest.mark.unit
def test_cli_start_requires_sample_rate(workspace):
    res = _invoke("start")
    assert res.exit_code == 2 and "--sample-rate" in res.output


@pytest.mark.unit
def test_cli_explicit_experiment_and_profile_reach_setup(workspace, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_label_mlflow_setup", lambda profile: seen.setdefault("profile", profile))
    monkeypatch.setattr(_eval_monitor, "_resolve_experiment_id", lambda ref: seen.setdefault("ref", ref) and "123")
    res = _invoke("status", "--experiment", "/Users/me/exp", "--profile", "fevm-classic")
    assert res.exit_code == 0, res.output
    assert seen == {"profile": "fevm-classic", "ref": "/Users/me/exp"}


@pytest.mark.unit
def test_cli_stop_and_status(workspace):
    res = _invoke("stop", "--format", "json")
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["scorers"][0]["status"] == "STOPPED"
    res = _invoke("status")
    assert res.exit_code == 0, res.output
    assert "quality-v2: STOPPED" in res.output and "not Unity Catalog" in res.output


@pytest.mark.unit
def test_cli_stop_unknown_scorer_errors(workspace):
    res = _invoke("stop", "--scorer", "ghost")
    assert res.exit_code == 1
    assert "not registered" in res.output
